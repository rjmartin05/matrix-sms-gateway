"""Persistent, paced outbound SMS/MMS queue.

Guarantees:
- Compliance gate at send time: a row is only sent if the recipient is currently opted in
  (kind group/dm/system), or, for direct replies to an inbound text (kind reply), not opted out.
  So STOP takes effect for messages already queued.
- No duplicate sends across restarts: a row is marked 'sending' (committed) before the API call;
  rows found in 'sending' at startup become 'unknown' and are never retried.
- Per-recipient ordering; carrier-class buckets don't head-of-line block other carriers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Awaitable, Callable

from .config import CarrierClass, Config
from .db import Database
from .provider.base import Provider, SendError
from .segments import normalize_typography

log = logging.getLogger("smsgw.outbound")

BACKOFF_BASE = 5.0
BACKOFF_MAX = 600.0


def classify_carrier(carrier: str | None, classes: list[CarrierClass], unknown: str) -> str:
    if carrier:
        c = carrier.lower()
        for cls in classes:
            if any(m in c for m in cls.match):
                return cls.name
        return "other"
    return unknown


class RateLimiter:
    """Sliding one-minute windows per carrier class + persisted daily segment caps."""

    def __init__(self, db: Database, classes: list[CarrierClass], clock: Callable[[], float] = time.monotonic):
        self.db = db
        self.classes = {c.name: c for c in classes}
        self.windows: dict[str, deque[float]] = {}
        self.clock = clock

    async def allowed(self, cls_name: str, segments: int) -> tuple[bool, str | None]:
        cls = self.classes.get(cls_name)
        if cls is None:
            return True, None
        if cls.per_minute:
            win = self.windows.setdefault(cls_name, deque())
            now = self.clock()
            while win and now - win[0] >= 60:
                win.popleft()
            if len(win) >= cls.per_minute:
                return False, "per_minute"
        if cls.daily_segments:
            used = await self.db.daily_segments(cls_name)
            if used + segments > cls.daily_segments:
                return False, "daily_cap"
        return True, None

    async def record(self, cls_name: str, segments: int) -> None:
        cls = self.classes.get(cls_name)
        if cls is None:
            return
        if cls.per_minute:
            self.windows.setdefault(cls_name, deque()).append(self.clock())
        if cls.daily_segments:
            await self.db.add_daily_segments(cls_name, segments)


class OutboundQueue:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        provider: Provider,
        *,
        on_alert: Callable[[str, str], Awaitable[None]] | None = None,
        on_blocked: Callable[[str, str], Awaitable[None]] | None = None,
        on_sent: Callable[[], Awaitable[None]] | None = None,
        estimate_segments: Callable[[str], int] | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.provider = provider
        self.limiter = RateLimiter(db, cfg.carrier_classes)
        self.wakeup = asyncio.Event()
        self.on_alert = on_alert
        self.on_blocked = on_blocked
        self.on_sent = on_sent
        self.estimate_segments = estimate_segments or (lambda _t: 1)
        self._consecutive_network_errors = 0
        self._daily_cap_alerted: set[str] = set()
        self._stopped = False

    def notify(self) -> None:
        self.wakeup.set()

    async def _alert(self, key: str, text: str) -> None:
        if self.on_alert:
            try:
                await self.on_alert(key, text)
            except Exception:
                log.exception("alert failed")

    async def _recipient_ok(self, row) -> tuple[bool, str | None]:
        kind = row["kind"]
        if kind == "optout_confirm":
            # The one message allowed after STOP. If Telnyx already blocked the number, the API
            # refuses it (40300), so this can't double up with Telnyx's own confirmation.
            return True, None
        rows = await self.db.fetchall("SELECT status FROM members WHERE number=?", (row["to_number"],))
        statuses = {r["status"] for r in rows}
        if "opted_out" in statuses:
            return False, "recipient opted out"
        if kind == "reply":
            return True, None
        if row["room_id"]:
            m = await self.db.get_member(row["room_id"], row["to_number"])
            if m is None or m.status != "opted_in":
                return False, "recipient not opted in"
            return True, None
        return ("opted_in" in statuses), (None if "opted_in" in statuses else "recipient not opted in")

    async def run(self) -> None:
        n = await self.db.recover_interrupted_sends()
        if n:
            log.warning("%d message(s) were interrupted mid-send and will not be retried", n)
            await self._alert("interrupted", f"{n} SMS were interrupted by a restart and not resent.")
        interval = 1.0 / max(self.cfg.global_per_second, 0.01)
        while not self._stopped:
            try:
                sent = await self.step()
            except Exception:
                log.exception("outbound step failed")
                sent = False
            if sent:
                await asyncio.sleep(interval)
            else:
                self.wakeup.clear()
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass

    def stop(self) -> None:
        self._stopped = True
        self.wakeup.set()

    async def step(self) -> bool:
        """Send at most one message. Returns True if an API call was made."""
        now = time.time()
        rows = await self.db.due_outbound(now)
        seen: set[str] = set()
        max_age = self.cfg.max_queue_age_hours * 3600
        for row in rows:
            to = row["to_number"]
            if to in seen:
                continue  # keep per-recipient order
            seen.add(to)
            if now - row["created_at"] > max_age:
                await self.db.update_outbound(row["id"], status="expired", error="too old to send")
                continue
            if row["next_attempt_at"] > now:
                continue
            ok, why = await self._recipient_ok(row)
            if not ok:
                await self.db.update_outbound(row["id"], status="skipped", error=why)
                continue
            cls = classify_carrier(row["carrier"], self.cfg.carrier_classes, self.cfg.unknown_carrier_class)
            seg_est = 1 if row["media_url"] else self.estimate_segments(normalize_typography(row["text"]))
            allowed, reason = await self.limiter.allowed(cls, seg_est)
            if not allowed:
                if reason == "daily_cap" and cls not in self._daily_cap_alerted:
                    self._daily_cap_alerted.add(cls)
                    await self._alert(f"cap:{cls}", f"Daily segment cap reached for {cls}; holding messages.")
                continue
            await self._send(row, cls)
            return True
        return False

    async def _send(self, row, cls: str) -> None:
        attempts = row["attempts"] + 1
        await self.db.update_outbound(row["id"], status="sending", attempts=attempts)
        try:
            res = await self.provider.send(
                from_number=row["from_number"],
                to_number=row["to_number"],
                text=normalize_typography(row["text"]),  # D9: bridge-side, Telnyx smart_encoding stays off
                media_url=row["media_url"],
            )
        except SendError as e:
            await self._handle_error(row, attempts, e)
            return
        except Exception as e:  # unexpected: treat as transient but never lose the row
            await self._handle_error(row, attempts, SendError("transient", repr(e)))
            return
        self._consecutive_network_errors = 0
        carrier = res.carrier or row["carrier"]
        await self.db.update_outbound(
            row["id"], status="sent", sent_at=time.time(), provider_id=res.provider_id,
            parts=res.parts, carrier=carrier, error=None,
        )
        if res.carrier:
            await self.db.execute(
                "UPDATE members SET carrier=? WHERE number=? AND (carrier IS NULL OR carrier != ?)",
                (res.carrier, row["to_number"], res.carrier),
            )
        real_cls = classify_carrier(carrier, self.cfg.carrier_classes, self.cfg.unknown_carrier_class)
        await self.limiter.record(real_cls, res.parts)
        await self.db.add_usage("mms_out" if row["media_url"] else "sms_out", res.parts)
        if self.on_sent:
            try:
                await self.on_sent()
            except Exception:
                log.exception("on_sent hook failed")

    async def _handle_error(self, row, attempts: int, e: SendError) -> None:
        log.warning("send %s to %s failed (%s): %s", row["id"], row["to_number"], e.kind, e)
        if e.kind == "blocked":
            self._consecutive_network_errors = 0
            await self.db.update_outbound(row["id"], status="blocked", error=str(e))
            if self.on_blocked:
                await self.on_blocked(row["to_number"], str(e))
            return
        if e.kind == "spend_limit":
            # Telnyx's daily spend cap: hold (old messages expire per max_queue_age_hours) and alert.
            self._consecutive_network_errors = 0
            await self.db.update_outbound(
                row["id"], status="pending", attempts=attempts - 1, next_attempt_at=time.time() + 1800, error=str(e)
            )
            await self._alert("spend_limit", "Telnyx daily spend limit reached; texts are on hold.")
            return
        if e.kind == "permanent":
            self._consecutive_network_errors = 0
            await self.db.update_outbound(row["id"], status="failed", error=str(e))
            await self._count_failure(row["to_number"])
            return
        if e.kind == "transient":
            self._consecutive_network_errors += 1
            if self._consecutive_network_errors == 5:
                await self._alert("unreachable", f"Telnyx unreachable: 5 consecutive send errors. Last: {e}")
        if attempts >= self.cfg.max_attempts:
            await self.db.update_outbound(row["id"], status="failed", error=f"gave up: {e}")
            await self._count_failure(row["to_number"])
            return
        delay = e.retry_after or min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (attempts - 1))
        if e.kind == "rate_limited":
            delay = max(delay, 60.0)
        await self.db.update_outbound(
            row["id"], status="pending", next_attempt_at=time.time() + delay, error=str(e)
        )

    async def _count_failure(self, number: str) -> None:
        await self.db.execute(
            "UPDATE members SET consecutive_failures=consecutive_failures+1 WHERE number=?", (number,)
        )
        r = await self.db.fetchone(
            "SELECT MAX(consecutive_failures) AS n, MAX(display_name) AS name FROM members WHERE number=?",
            (number,),
        )
        if r and r["n"] == self.cfg.failure_alert_threshold:
            await self._alert(
                f"fail:{number}", f"{r['name']} ({number}) has failed {r['n']} deliveries in a row."
            )

    async def delivery_update(self, provider_id: str, status: str, failed: bool, delivered: bool, error: str | None) -> None:
        row = await self.db.outbound_by_provider_id(provider_id)
        if row is None:
            return
        await self.db.update_outbound(row["id"], delivery_status=status, error=error or row["error"])
        if delivered:
            await self.db.execute("UPDATE members SET consecutive_failures=0 WHERE number=?", (row["to_number"],))
        elif failed:
            await self._count_failure(row["to_number"])
