"""HTTP clients: the MAS admin API, the Matrix client API, SMTP, and the bridge's internal API."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.parse import quote

import aiohttp

log = logging.getLogger("smsgw.login.clients")

ADMIN_SCOPE = "urn:mas:admin"


class UpstreamError(Exception):
    def __init__(self, what: str, status: int, body: str = "") -> None:
        super().__init__(f"{what}: HTTP {status} {body[:300]}")
        self.status = status


async def _check(resp: aiohttp.ClientResponse, what: str) -> dict:
    if resp.status >= 400:
        raise UpstreamError(what, resp.status, await resp.text())
    if resp.status == 204:
        return {}
    return await resp.json()


class MasAdmin:
    """The subset of the MAS admin API (v1) this service uses. Auth: client credentials grant."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str, client_id: str, client_secret: str) -> None:
        self.http = session
        self.base = base_url.rstrip("/")
        self.auth = aiohttp.BasicAuth(client_id, client_secret)
        self._token: str | None = None
        self._token_exp = 0.0

    async def _bearer(self) -> str:
        if self._token is None or time.time() > self._token_exp:
            async with self.http.post(f"{self.base}/oauth2/token", auth=self.auth,
                                      data={"grant_type": "client_credentials", "scope": ADMIN_SCOPE}) as r:
                body = await _check(r, "MAS token")
            self._token = body["access_token"]
            self._token_exp = time.time() + int(body.get("expires_in", 300)) - 30
        return self._token

    async def _req(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {await self._bearer()}"}
        async with self.http.request(method, f"{self.base}/api/admin/v1{path}", json=json, params=params,
                                     headers=headers) as r:
            return await _check(r, f"MAS {method} {path}")

    async def user_by_username(self, username: str) -> str | None:
        try:
            body = await self._req("GET", f"/users/by-username/{quote(username, safe='')}")
        except UpstreamError as e:
            if e.status == 404:
                return None
            raise
        return body["data"]["id"]

    async def create_user(self, username: str, displayname: str | None = None) -> str:
        body = await self._req("POST", "/users", json={"username": username, "displayname": displayname})
        return body["data"]["id"]

    async def set_password(self, user_id: str, password: str, *, check: bool = False) -> None:
        """``check``: apply MAS's password policy (for passwords a person chose)."""
        await self._req("POST", f"/users/{user_id}/set-password",
                        json={"password": password, "skip_password_check": not check})

    async def user_emails(self, user_id: str) -> list[str]:
        body = await self._req("GET", "/user-emails", params={"filter[user]": user_id, "page[first]": "50"})
        return [e["attributes"]["email"] for e in body.get("data", [])]

    async def active_sessions(self, user_id: str) -> list[dict]:
        """The user's signed-in apps: compatibility (password/code sign-in) and OAuth 2.0 sessions."""
        out: list[dict] = []
        for kind in ("compat", "oauth2"):
            body = await self._req("GET", f"/{kind}-sessions",
                                   params={"filter[user]": user_id, "filter[status]": "active", "page[first]": "100"})
            for s in body.get("data", []):
                a = s["attributes"]
                device = a.get("device_id")
                if kind == "oauth2":
                    # The device is a scope token: urn:matrix:client:device:X (or the unstable MSC2967 prefix).
                    device = next((t.rsplit(":", 1)[1] for t in (a.get("scope") or "").split()
                                   if ":device:" in t), None)
                out.append({"id": s["id"], "kind": kind, "device_id": device, "user_id": a.get("user_id"),
                            "name": a.get("human_name") or a.get("user_agent") or "Unknown app",
                            "last_active": a.get("last_active_at"), "created": a.get("created_at")})
        return out

    async def finish_session(self, kind: str, session_id: str) -> None:
        await self._req("POST", f"/{kind}-sessions/{session_id}/finish")

    async def deactivate(self, user_id: str) -> None:
        await self._req("POST", f"/users/{user_id}/deactivate", json={"skip_erase": False})

    async def add_email(self, user_id: str, email: str) -> None:
        try:
            await self._req("POST", "/user-emails", json={"user_id": user_id, "email": email})
        except UpstreamError as e:
            if e.status != 409:  # already attached
                raise

    async def registration_token_valid(self, token: str) -> bool:
        body = await self._req("GET", "/user-registration-tokens",
                               params={"filter[valid]": "true", "page[first]": "100"})
        return any(t.get("attributes", {}).get("token") == token for t in body.get("data", []))


class MatrixClient:
    """Matrix client-server calls made with a user's own credentials."""

    def __init__(self, session: aiohttp.ClientSession, client_url: str) -> None:
        self.http = session
        self.base = client_url.rstrip("/")

    async def password_login(self, username: str, password: str, device_name: str) -> dict:
        payload = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": username},
            "password": password,
            "initial_device_display_name": device_name,
            "refresh_token": True,
        }
        async with self.http.post(f"{self.base}/_matrix/client/v3/login", json=payload) as r:
            return await _check(r, "Matrix login")

    async def whoami(self, access_token: str) -> str | None:
        async with self.http.get(f"{self.base}/_matrix/client/v3/account/whoami",
                                 headers={"Authorization": f"Bearer {access_token}"}) as r:
            if r.status in (401, 403):
                return None
            return (await _check(r, "Matrix whoami"))["user_id"]

    async def whoami_device(self, access_token: str) -> tuple[str, str | None] | None:
        async with self.http.get(f"{self.base}/_matrix/client/v3/account/whoami",
                                 headers={"Authorization": f"Bearer {access_token}"}) as r:
            if r.status in (401, 403):
                return None
            body = await _check(r, "Matrix whoami")
            return body["user_id"], body.get("device_id")

    async def logout(self, access_token: str) -> None:
        async with self.http.post(f"{self.base}/_matrix/client/v3/logout", json={},
                                  headers={"Authorization": f"Bearer {access_token}"}) as r:
            await _check(r, "Matrix logout")

    async def set_displayname(self, access_token: str, mxid: str, name: str) -> None:
        async with self.http.put(f"{self.base}/_matrix/client/v3/profile/{quote(mxid, safe='')}/displayname",
                                 json={"displayname": name},
                                 headers={"Authorization": f"Bearer {access_token}"}) as r:
            await _check(r, "Matrix set displayname")


class Mailer:
    def __init__(self, host: str, port: int, starttls: bool, username: str | None, password: str | None,
                 sender: str) -> None:
        self.host, self.port, self.starttls = host, port, starttls
        self.username, self.password, self.sender = username, password, sender

    def _send(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=20) as s:
            if self.starttls:
                s.starttls(context=ssl.create_default_context())
            if self.username:
                s.login(self.username, self.password or "")
            s.send_message(msg)

    async def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        await asyncio.get_running_loop().run_in_executor(None, self._send, msg)


class BridgeNotifier:
    """The bridge's internal API: a person now uses the app (invite them, stop SMS to their number),
    and the SMS member list the app shows (D14)."""

    def __init__(self, session: aiohttp.ClientSession, bridge_url: str, secret: str) -> None:
        self.http = session
        self.base = bridge_url.rstrip("/")
        self.url = f"{self.base}/internal/app-user"
        self.secret = secret

    async def app_user(self, mxid: str, number: str | None) -> None:
        async with self.http.post(self.url, json={"mxid": mxid, "number": number},
                                  headers={"Authorization": f"Bearer {self.secret}"}) as r:
            await _check(r, "bridge app-user")

    async def sms_members(self, action: str, body: dict) -> dict:
        """action: list | add | remove. The bridge checks the caller (body["by"]) itself."""
        async with self.http.post(f"{self.base}/internal/sms-members/{action}", json=body,
                                  headers={"Authorization": f"Bearer {self.secret}"}) as r:
            return await _check(r, f"bridge sms-members {action}")
