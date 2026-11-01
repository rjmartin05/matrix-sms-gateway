"""HTTP: the public app API (behind the tunnel) and the internal listener the bridge calls."""

from __future__ import annotations

import html
import hmac
import json
import logging
import re
from urllib.parse import urlencode

from aiohttp import web

from .service import LoginService, ServiceError

log = logging.getLogger("smsgw.login.web")

MAX_BODY = 16 * 1024
_SMS_CODE_RE = re.compile(r"^\d{6}$")
_INVITE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# Shown only when Android didn't hand the link to the app (app not installed, or links not verified yet).
# It doesn't check the code, so it can't be used to test codes. It jumps to the Play Store app right
# away; the button covers browsers that block the automatic jump.
_JOIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Join {app}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem;line-height:1.5}}
a.b{{display:block;text-align:center;background:#1f6fb2;color:#fff;padding:.9rem;border-radius:.6rem;text-decoration:none;font-weight:600}}</style>
</head><body>
<h1>Join {app}</h1>
<p>Opening Google Play&hellip; Install {app}, then tap your invite link again to finish signing up.</p>
<p><a class="b" href="{play}">Get {app} on Google Play</a></p>
<script>location.replace({market});</script>
</body></html>
"""


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
            except web.HTTPException:
                raise  # e.g. 413 from client_max_size
            except Exception:
                # MAS / Synapse down or returning something unexpected: keep the JSON contract.
                log.exception("%s %s failed", request.method, request.path)
                return _json_error(ServiceError(503, "try_again", "The server is busy. Try again in a minute."))
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

    @handler
    async def account(request):
        return await svc.account(_bearer(request))

    @handler
    async def account_end_session(request):
        return await svc.end_session(_bearer(request), await _body(request))

    @handler
    async def account_password(request):
        return await svc.change_password(_bearer(request), await _body(request))

    @handler
    async def account_use_password_start(request):
        return await svc.start_use_password(_bearer(request), ip(request))

    @handler
    async def account_use_password(request):
        return await svc.use_password(_bearer(request), await _body(request))

    @handler
    async def account_delete(request):
        return await svc.delete_account(_bearer(request), await _body(request))

    @handler
    async def sms_members_list(request):
        return await svc.sms_members(_bearer(request), "list", {"room_id": request.query.get("room_id", "")})

    @handler
    async def sms_members_add(request):
        return await svc.sms_members(_bearer(request), "add", await _body(request))

    @handler
    async def sms_members_remove(request):
        return await svc.sms_members(_bearer(request), "remove", await _body(request))

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    cfg = svc.cfg

    async def join_page(request: web.Request) -> web.Response:
        code = request.match_info["code"]
        if not _INVITE_RE.match(code) or not cfg.android_package:
            raise web.HTTPNotFound()
        # Play hands "referrer" to the app at install time, and the app picks up the code on first open.
        query = urlencode({"id": cfg.android_package, "referrer": f"invite={code}"})
        play = cfg.install_url or f"https://play.google.com/store/apps/details?{query}"
        market = f"market://details?{query}" if not cfg.install_url else play
        page = _JOIN_PAGE.format(app=html.escape(cfg.app_name), play=html.escape(play, quote=True),
                                 market=json.dumps(market).replace("</", "<\\/"))
        return web.Response(text=page, content_type="text/html",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    async def assetlinks(_: web.Request) -> web.Response:
        # Verified Android App Links: proves this host belongs to the app, so Android opens
        # https://<host>/join/... straight in the app with no browser or chooser.
        if not (cfg.android_package and cfg.android_cert_sha256):
            raise web.HTTPNotFound()
        return web.json_response([{
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {"namespace": "android_app", "package_name": cfg.android_package,
                       "sha256_cert_fingerprints": cfg.android_cert_sha256},
        }])

    app.router.add_get("/join/{code}", join_page)
    app.router.add_get("/.well-known/assetlinks.json", assetlinks)
    app.router.add_post("/v1/signup", signup)
    app.router.add_post("/v1/login", login)
    app.router.add_post("/v1/switch", switch)
    app.router.add_post("/v1/verify", verify)
    app.router.add_get("/v1/verify/{vid}", poll)
    app.router.add_get("/v1/sms-members", sms_members_list)
    app.router.add_post("/v1/sms-members", sms_members_add)
    app.router.add_post("/v1/sms-members/remove", sms_members_remove)
    app.router.add_get("/v1/account", account)
    app.router.add_post("/v1/account/sessions/end", account_end_session)
    app.router.add_post("/v1/account/password", account_password)
    app.router.add_post("/v1/account/use-password/start", account_use_password_start)
    app.router.add_post("/v1/account/use-password", account_use_password)
    app.router.add_post("/v1/account/delete", account_delete)
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

    async def invites(request: web.Request) -> web.Response:
        if not hmac.compare_digest(_bearer(request) or "", secret):
            return web.Response(status=401)
        try:
            body = await _body(request)
            return web.json_response(await svc.create_invite(str(body.get("created_by") or "") or None,
                                                             str(body.get("note") or "") or None))
        except ServiceError as e:
            return _json_error(e)

    async def sms_accounts(request: web.Request) -> web.Response:
        if not hmac.compare_digest(_bearer(request) or "", secret):
            return web.Response(status=401)
        try:
            body = await _body(request)
            if request.match_info.get("action") == "drop":
                await svc.drop_sms_account(str(body.get("mxid") or ""))
                return web.json_response({"ok": True})
            return web.json_response(await svc.sms_account(str(body.get("name") or ""), str(body.get("number") or "")))
        except ServiceError as e:
            return _json_error(e)

    app.router.add_post("/internal/sms-code", sms_code)
    app.router.add_post("/internal/invites", invites)
    app.router.add_post("/internal/sms-accounts", sms_accounts)
    app.router.add_post("/internal/sms-accounts/{action:drop}", sms_accounts)
    return app
