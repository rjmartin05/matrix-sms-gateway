"""Public HTTP listener (behind the Cloudflare tunnel at sms-hook.*).

Only three routes are exposed: the signed Telnyx webhook, short-lived media, and a health check.
The internal listener (docker network only) takes calls from the login service.
"""

from __future__ import annotations

import hmac
import logging
from typing import Awaitable, Callable

from aiohttp import web

from .db import Database
from .provider.base import Provider, SignatureError

log = logging.getLogger("smsgw.web")

MAX_WEBHOOK_BYTES = 256 * 1024


def build_app(db: Database, provider: Provider, on_inbound: Callable[[], Awaitable[None] | None]) -> web.Application:
    app = web.Application(client_max_size=MAX_WEBHOOK_BYTES)

    async def webhook(request: web.Request) -> web.Response:
        body = await request.read()
        try:
            provider.verify(body, request.headers)
        except SignatureError as e:
            log.warning("rejected webhook from %s: %s", request.remote, e)
            return web.Response(status=401)
        try:
            evt = provider.parse(body)
        except (ValueError, KeyError) as e:
            log.warning("unparseable webhook: %s", e)
            return web.Response(status=400)
        if evt is None:
            return web.Response(status=200)
        # Persist first, process async: Telnyx gets a fast 200 and retries are deduped by key.
        if await db.add_inbound(evt.key, evt.event_type, evt.raw):
            res = on_inbound()
            if res is not None:
                await res
        return web.Response(status=200)

    async def media(request: web.Request) -> web.StreamResponse:
        token = request.match_info["name"].split(".", 1)[0]
        row = await db.get_media_token(token)
        if row is None:
            raise web.HTTPNotFound()
        return web.FileResponse(
            row["path"],
            headers={"Content-Type": row["content_type"], "Cache-Control": "private, no-store",
                     "X-Robots-Tag": "noindex"},
        )

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_post("/telnyx/webhook", webhook)
    app.router.add_get("/m/{name}", media)
    app.router.add_get("/healthz", health)
    return app


def build_internal_app(on_app_user: Callable[[str, str | None], Awaitable[None]], secret: str,
                       sms_members: Callable[[str, dict], Awaitable[dict]] | None = None) -> web.Application:
    app = web.Application(client_max_size=16 * 1024)

    async def app_user(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {secret}"):
            return web.Response(status=401)
        try:
            body = await request.json()
            mxid, number = str(body["mxid"]), body.get("number")
        except (ValueError, KeyError, TypeError):
            return web.Response(status=400)
        if not mxid.startswith("@"):
            return web.Response(status=400)
        await on_app_user(mxid, str(number) if number else None)
        return web.json_response({"ok": True})

    async def members(request: web.Request) -> web.Response:
        # The login service forwards the app's "add someone by text" requests, with the caller's mxid.
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {secret}"):
            return web.Response(status=401)
        if sms_members is None:
            return web.Response(status=404)
        try:
            body = await request.json()
            if not isinstance(body, dict) or not str(body.get("by", "")).startswith("@"):
                raise ValueError
        except ValueError:
            return web.Response(status=400)
        return web.json_response(await sms_members(request.match_info["action"], body))

    app.router.add_post("/internal/app-user", app_user)
    app.router.add_post("/internal/sms-members/{action:list|add|remove}", members)
    return app
