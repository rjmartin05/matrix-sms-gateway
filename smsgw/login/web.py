"""HTTP: the public app API (behind the tunnel) and the internal listener the bridge calls."""

from __future__ import annotations

import hmac
import json
import logging
import re

from aiohttp import web

from .service import LoginService, ServiceError

log = logging.getLogger("smsgw.login.web")

MAX_BODY = 16 * 1024
_SMS_CODE_RE = re.compile(r"^\d{6}$")


def _json_error(e: ServiceError) -> web.Response:
    return web.json_response({"error": e.code, "message": e.message}, status=e.status)


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ServiceError(400, "bad_request", "Expected a JSON body.")
    if not isinstance(body, dict):
        raise ServiceError(400, "bad_request", "Expected a JSON object.")
    return body


def _bearer(request: web.Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return auth[7:].strip() or None


def build_public_app(svc: LoginService, client_ip_header: str | None) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY)

    def ip(request: web.Request) -> str | None:
        return (request.headers.get(client_ip_header) if client_ip_header else None) or request.remote

    def handler(fn):
        async def wrapped(request: web.Request) -> web.Response:
            try:
                return web.json_response(await fn(request))
            except ServiceError as e:
                return _json_error(e)
        return wrapped

    @handler
    async def signup(request):
        return await svc.start_signup(await _body(request), ip(request))

    @handler
    async def login(request):
        return await svc.start_login(await _body(request), ip(request))

    @handler
    async def switch(request):
        return await svc.start_switch(_bearer(request), await _body(request), ip(request))

    @handler
    async def verify(request):
        body = await _body(request)
        return await svc.verify_code(str(body.get("verification_id", "")), str(body.get("code", "")))

    @handler
    async def poll(request):
        return await svc.poll(request.match_info["vid"])

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_post("/v1/signup", signup)
    app.router.add_post("/v1/login", login)
    app.router.add_post("/v1/switch", switch)
    app.router.add_post("/v1/verify", verify)
    app.router.add_get("/v1/verify/{vid}", poll)
    app.router.add_get("/healthz", health)
    return app


def build_internal_app(svc: LoginService, secret: str) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY)

    async def sms_code(request: web.Request) -> web.Response:
        if not hmac.compare_digest(_bearer(request) or "", secret):
            return web.Response(status=401)
        try:
            body = await _body(request)
        except ServiceError as e:
            return _json_error(e)
        code = str(body.get("code", ""))
        if not _SMS_CODE_RE.match(code):
            return web.json_response({"matched": False})
        return web.json_response({"matched": await svc.sms_code(str(body.get("number", "")), code)})

    app.router.add_post("/internal/sms-code", sms_code)
    return app
