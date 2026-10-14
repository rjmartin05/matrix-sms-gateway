"""Telnyx Messaging API v2 adapter.

Verified against developers.telnyx.com (2026-10-14):
- Webhooks are signed with Ed25519 over ``f"{telnyx-timestamp}|{raw body}"``; headers
  ``telnyx-signature-ed25519`` (base64) and ``telnyx-timestamp`` (unix seconds).
- Inbound keyword messages carry ``payload.autoresponse_type`` when Telnyx handled them.
- Sending to a STOP-blocked recipient returns error code 40300 (one docs page says HTTP 409).
Inference (confirm on live traffic): ``from.carrier`` on inbound and ``to[].carrier`` on send responses.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Mapping

import aiohttp
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .base import (
    DeliveryUpdate,
    InboundMedia,
    InboundMessage,
    SendError,
    SendResult,
    SignatureError,
    WebhookEvent,
)

log = logging.getLogger("smsgw.telnyx")

API_BASE = "https://api.telnyx.com/v2"
BLOCKED_CODES = {"40300"}
FAILED_STATUSES = {"delivery_failed", "sending_failed"}


def _first_error(body: object) -> tuple[str | None, str]:
    if isinstance(body, dict):
        errs = body.get("errors") or []
        if errs and isinstance(errs[0], dict):
            e = errs[0]
            return (str(e.get("code")) if e.get("code") is not None else None,
                    f"{e.get('title', '')}: {e.get('detail', '')}".strip(": "))
    return None, str(body)[:300]


class TelnyxProvider:
    def __init__(
        self,
        api_key: str,
        public_key_b64: str,
        messaging_profile_id: str | None = None,
        tolerance_seconds: int = 300,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.api_key = api_key
        self.public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        self.messaging_profile_id = messaging_profile_id
        self.tolerance = tolerance_seconds
        self._session = session

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ---- webhook --------------------------------------------------------------------

    def verify(self, body: bytes, headers: Mapping[str, str], now: float | None = None) -> None:
        sig = headers.get("telnyx-signature-ed25519")
        ts = headers.get("telnyx-timestamp")
        if not sig or not ts:
            raise SignatureError("missing signature headers")
        try:
            ts_int = int(ts)
        except ValueError as e:
            raise SignatureError("bad timestamp") from e
        if abs((now or time.time()) - ts_int) > self.tolerance:
            raise SignatureError("timestamp outside tolerance")
        try:
            self.public_key.verify(base64.b64decode(sig), ts.encode() + b"|" + body)
        except (InvalidSignature, ValueError) as e:
            raise SignatureError("invalid signature") from e

    def parse(self, body: bytes) -> WebhookEvent | None:
        raw = json.loads(body)
        data = raw.get("data") or {}
        event_type = data.get("event_type", "")
        payload = data.get("payload") or {}
        msg_id = payload.get("id") or data.get("id")
        if not msg_id:
            return None
        key = f"{event_type}:{msg_id}"
        if event_type == "message.received":
            frm = payload.get("from") or {}
            to = payload.get("to") or [{}]
            media = tuple(
                InboundMedia(m["url"], m.get("content_type") or "application/octet-stream", m.get("size"))
                for m in payload.get("media") or []
                if m.get("url")
            )
            msg = InboundMessage(
                provider_id=msg_id,
                from_number=frm.get("phone_number", ""),
                to_number=(to[0] or {}).get("phone_number", ""),
                text=payload.get("text") or "",
                media=media,
                carrier=frm.get("carrier"),
                autoresponse_type=payload.get("autoresponse_type") or None,
            )
            return WebhookEvent(key, event_type, message=msg, raw=raw)
        if event_type in ("message.sent", "message.finalized"):
            to = (payload.get("to") or [{}])[0] or {}
            status = to.get("status") or ""
            err_code, err_text = _first_error(payload)
            delivery = DeliveryUpdate(
                provider_id=msg_id,
                to_number=to.get("phone_number", ""),
                status=status,
                failed=status in FAILED_STATUSES,
                delivered=status == "delivered",
                error=f"{err_code} {err_text}" if err_code else None,
            )
            return WebhookEvent(key, event_type, delivery=delivery, raw=raw)
        return WebhookEvent(key, event_type, raw=raw)

    # ---- sending --------------------------------------------------------------------

    async def send(
        self, *, from_number: str, to_number: str, text: str, media_url: str | None = None
    ) -> SendResult:
        body: dict = {"from": from_number, "to": to_number, "text": text}
        if media_url:
            body["media_urls"] = [media_url]
            body["type"] = "MMS"
        if self.messaging_profile_id:
            body["messaging_profile_id"] = self.messaging_profile_id
        try:
            async with self.session.post(
                f"{API_BASE}/messages",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except (json.JSONDecodeError, aiohttp.ContentTypeError):
                    data = {"raw": (await resp.text())[:300]}
                status = resp.status
                retry_after = resp.headers.get("Retry-After")
        except (aiohttp.ClientError, TimeoutError) as e:
            raise SendError("transient", f"network: {e!r}") from e

        if 200 <= status < 300:
            d = data.get("data") or {}
            to = (d.get("to") or [{}])[0] or {}
            return SendResult(provider_id=d.get("id", ""), parts=int(d.get("parts") or 1), carrier=to.get("carrier"))

        code, detail = _first_error(data)
        msg = f"HTTP {status} {code or ''} {detail}".strip()
        if code in BLOCKED_CODES or status == 409:
            raise SendError("blocked", msg, code)
        if status == 429:
            ra = float(retry_after) if retry_after and retry_after.isdigit() else None
            raise SendError("rate_limited", msg, code, ra)
        if status >= 500:
            raise SendError("transient", msg, code)
        raise SendError("permanent", msg, code)

    async def fetch_media(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        """Download inbound MMS media. Tries without auth first, then with the API key."""
        for headers in ({}, {"Authorization": f"Bearer {self.api_key}"}):
            async with self.session.get(url, headers=headers) as resp:
                if resp.status in (401, 403) and not headers:
                    continue
                resp.raise_for_status()
                if resp.content_length and resp.content_length > max_bytes:
                    raise ValueError(f"media too large ({resp.content_length} bytes)")
                data = await resp.content.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError("media too large")
                return data, resp.headers.get("Content-Type", "application/octet-stream")
        raise ValueError("media fetch failed")
