"""Code-based sign-up / sign-in / switch for app-managed accounts (D13).

A verified email or phone gets a normal Matrix session without a browser: the service sets a fresh
random MAS password on the account and performs the password login itself, returning only the
resulting tokens. That is why only accounts marked code_managed can use code login: their password
is never known to anyone. An existing password account becomes code-managed only through the
"switch" flow, which requires a session for that account (or the admin CLI).

Phone numbers are verified in the inbound direction: the app shows a code, the person texts it to
the bridge number, and the bridge forwards it here. No code is ever sent by SMS.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable

from ..bridge import normalize_number
from .clients import BridgeNotifier, Mailer, MasAdmin, MatrixClient, UpstreamError
from .config import LoginConfig
from .db import LoginDB

log = logging.getLogger("smsgw.login")

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
SMS_CODE_TEMPLATE = "Sign-in code {code}"
MAX_NAME = 64
MAX_USERNAME = 24


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Identifier:
    channel: str  # email | sms
    value: str

    @property
    def field(self) -> str:
        return "email" if self.channel == "email" else "phone"


def username_base(display_name: str) -> str:
    s = unicodedata.normalize("NFKD", display_name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", ".", s).strip(".")[:MAX_USERNAME].strip(".")
    return s or "member"


class LoginService:
    def __init__(self, cfg: LoginConfig, db: LoginDB, mas: MasAdmin, matrix: MatrixClient,
                 mailer: Mailer | None, bridge: BridgeNotifier | None, now: Callable[[], float] = time.time) -> None:
        self.cfg, self.db, self.mas, self.matrix = cfg, db, mas, matrix
        self.mailer, self.bridge, self.now = mailer, bridge, now

    # ---- input --------------------------------------------------------------------

    def identifier(self, body: dict) -> Identifier:
        email, phone = body.get("email"), body.get("phone")
        if bool(email) == bool(phone):
            raise ServiceError(400, "bad_request", "Give either an email address or a phone number.")
        if email:
            email = str(email).strip().lower()
            if not EMAIL_RE.match(email):
                raise ServiceError(400, "bad_email", "That doesn't look like an email address.")
            if self.mailer is None:
                raise ServiceError(400, "email_disabled", "Email codes aren't available. Use your phone number.")
            return Identifier("email", email)
        number = normalize_number(str(phone))
        if number is None:
            raise ServiceError(400, "bad_phone", "That doesn't look like a phone number.")
        if self.cfg.sms_number is None:
            raise ServiceError(400, "phone_disabled", "Phone sign-in isn't available. Use your email address.")
        return Identifier("sms", number)

    # ---- starting a verification ---------------------------------------------------

    async def _rate_limit(self, ident: Identifier, ip: str | None) -> None:
        since = self.now() - 3600
        if await self.db.count_starts(f"id:{ident.value}", since) >= self.cfg.starts_per_hour:
            raise ServiceError(429, "rate_limited", "Too many codes requested. Try again in an hour.")
        if ip and await self.db.count_starts(f"ip:{ip}", since) >= self.cfg.starts_per_hour_ip:
            raise ServiceError(429, "rate_limited", "Too many codes requested. Try again in an hour.")
        await self.db.add_start(f"id:{ident.value}", self.now())
        if ip:
            await self.db.add_start(f"ip:{ip}", self.now())

    def _hash(self, vid: str, code: str) -> str:
        return hmac.new(self.cfg.code_secret.encode(), f"{vid}:{code}".encode(), hashlib.sha256).hexdigest()

    async def _issue(self, purpose: str, ident: Identifier, ip: str | None, *, mxid: str | None = None,
                     display_name: str | None = None, invite_hash: str | None = None) -> dict:
        await self._rate_limit(ident, ip)
        vid = secrets.token_urlsafe(18)
        code = f"{secrets.randbelow(10**6):06d}"
        await self.db.add_verification(vid, purpose, ident.channel, ident.value, self._hash(vid, code),
                                       self.now() + self.cfg.code_ttl_seconds, mxid=mxid, display_name=display_name,
                                       invite_hash=invite_hash)
        out = {"verification_id": vid, "channel": ident.channel, "expires_in": self.cfg.code_ttl_seconds}
        if ident.channel == "email":
            assert self.mailer
            minutes = self.cfg.code_ttl_seconds // 60
            try:
                await self.mailer.send(
                    ident.value, f"{self.cfg.app_name} sign-in code: {code}",
                    f"Your {self.cfg.app_name} sign-in code is {code}\n\n"
                    f"It expires in {minutes} minutes. If you didn't ask for it, ignore this email.\n",
                )
            except Exception as e:
                log.error("sending code email failed: %s", e)
                await self.db.transition(vid, "pending", "failed")
                raise ServiceError(502, "email_failed", "Couldn't send the email. Try again later.")
        else:
            out["sms_to"] = self.cfg.sms_number
            out["sms_body"] = SMS_CODE_TEMPLATE.format(code=code)
        log.info("%s verification %s started (%s)", purpose, vid[:6], ident.channel)
        return out

    async def start_signup(self, body: dict, ip: str | None) -> dict:
        ident = self.identifier(body)
        name = " ".join(str(body.get("display_name") or "").split())[:MAX_NAME]
        if not name:
            raise ServiceError(400, "bad_name", "Enter your name.")
        existing = await self.db.account_by(ident.field, ident.value)
        if existing is not None and existing["sms_status"] == "sms":
            # Someone in the group added this number (D14): they already have an account; this just
            # signs them in to it. No invite needed, and the name they typed doesn't replace theirs.
            return await self._issue("login", ident, ip, mxid=existing["mxid"])
        invite_hash = await self._check_join(body)
        if existing is not None:
            raise ServiceError(409, "already_registered", "That's already registered. Sign in instead.")
        return await self._issue("signup", ident, ip, display_name=name, invite_hash=invite_hash)

    async def _check_join(self, body: dict) -> str | None:
        """Sign-up needs a one-time invite or (if allowed) the shared join token. Returns the invite's hash.
        The invite is only checked here; it's spent when the account is created, so a failed or
        abandoned verification doesn't burn it."""
        invite = str(body.get("invite") or "").strip()
        if invite:
            h = self._invite_hash(invite)
            if await self.db.live_invite(h, self.now()) is None:
                raise ServiceError(403, "bad_invite",
                                   "That invite link has expired or was already used. Ask the group admin for a new one.")
            return h
        token = str(body.get("join_token") or "").strip()
        if not self.cfg.allow_join_token:
            raise ServiceError(403, "bad_invite", "Sign up with the invite link the group admin sent you.")
        if not token or not await self.mas.registration_token_valid(token):
            raise ServiceError(403, "bad_join_token", "That join code isn't valid. Ask the group admin for one.")
        return None

    # ---- invites --------------------------------------------------------------------

    def _invite_hash(self, code: str) -> str:
        return hmac.new(self.cfg.code_secret.encode(), f"invite:{code}".encode(), hashlib.sha256).hexdigest()

    async def create_invite(self, created_by: str | None, note: str | None = None) -> dict:
        if not (self.cfg.public_url and self.cfg.android_package):
            raise ServiceError(400, "invites_disabled",
                               "Invite links need app.public_url and app.android_package in login.yaml.")
        code = secrets.token_urlsafe(16)  # 128 bits: not guessable, so no rate limit needed on checks
        expires_at = self.now() + self.cfg.invite_ttl_seconds
        await self.db.add_invite(self._invite_hash(code), created_by, (note or "")[:100] or None, self.now(), expires_at)
        log.info("invite created by %s (expires in %d days)", created_by, self.cfg.invite_ttl_seconds // 86400)
        return {"code": code, "url": f"{self.cfg.public_url}/join/{code}", "expires_at": expires_at}

    async def start_login(self, body: dict, ip: str | None) -> dict:
        ident = self.identifier(body)
        acct = await self.db.account_by(ident.field, ident.value)
        if acct is None or not acct["code_managed"] or acct["sms_status"] == "removed":
            raise ServiceError(404, "no_account", "No account signs in with that. Sign up, or use your password.")
        return await self._issue("login", ident, ip, mxid=acct["mxid"])

    async def start_switch(self, access_token: str | None, body: dict, ip: str | None) -> dict:
        mxid = await self.matrix.whoami(access_token) if access_token else None
        if mxid is None:
            raise ServiceError(401, "unauthorized", "Sign in with your password first.")
        if not mxid.endswith(f":{self.cfg.domain}"):
            raise ServiceError(403, "forbidden", "Only accounts on this server can switch.")
        ident = self.identifier(body)
        other = await self.db.account_by(ident.field, ident.value)
        if other is not None and other["mxid"] != mxid:
            raise ServiceError(409, "already_registered", "Another account already uses that.")
        return await self._issue("switch", ident, ip, mxid=mxid)

    # ---- checking codes ------------------------------------------------------------

    async def _live(self, vid: str) -> dict:
        v = await self.db.get_verification(vid)
        if v is None:
            raise ServiceError(404, "not_found", "That code request has expired. Start again.")
        v = dict(v)
        if v["status"] == "used":
            raise ServiceError(410, "already_used", "That code was already used. Start again.")
        if v["status"] == "failed" or (v["status"] == "pending" and self.now() > v["expires_at"]):
            raise ServiceError(410, "expired", "That code has expired. Start again.")
        return v

    async def verify_code(self, vid: str, code: str) -> dict:
        """Email codes: the person types the code into the app."""
        v = await self._live(vid)
        if v["channel"] != "email":
            raise ServiceError(400, "wrong_channel", "Text the code to us instead.")
        if v["status"] == "pending":
            n = await self.db.claim_attempt(vid, self.cfg.code_max_attempts)
            if n is None:
                if (await self._live(vid))["status"] != "verified":  # else a concurrent request won
                    await self.db.transition(vid, "pending", "failed")
                    raise ServiceError(429, "too_many_attempts", "Too many wrong codes. Start again.")
            elif not hmac.compare_digest(self._hash(vid, str(code).strip()), v["code_hash"]):
                if n >= self.cfg.code_max_attempts:
                    await self.db.transition(vid, "pending", "failed")
                    raise ServiceError(429, "too_many_attempts", "Too many wrong codes. Start again.")
                raise ServiceError(400, "wrong_code", "That code isn't right.")
            else:
                await self.db.transition(vid, "pending", "verified")
        return await self._complete(vid)

    async def sms_code(self, number: str, code: str) -> bool:
        """Phone codes: the bridge forwards an inbound "Sign-in code NNNNNN" text from ``number``."""
        number = normalize_number(number) or number
        row = await self.db.pending_for(number, "sms")
        if row is None or self.now() > row["expires_at"]:
            log.info("sms code from unknown or expired request")
            return False
        n = await self.db.claim_attempt(row["id"], self.cfg.code_max_attempts)
        if n is None:
            return False
        if not hmac.compare_digest(self._hash(row["id"], code), row["code_hash"]):
            if n >= self.cfg.code_max_attempts:
                await self.db.transition(row["id"], "pending", "failed")
            return False
        return await self.db.transition(row["id"], "pending", "verified")

    async def poll(self, vid: str) -> dict:
        """Phone codes: the app polls until the text has arrived."""
        v = await self._live(vid)
        if v["status"] == "pending":
            return {"status": "pending"}
        return await self._complete(vid)

    # ---- completing ----------------------------------------------------------------

    async def _complete(self, vid: str) -> dict:
        if not await self.db.transition(vid, "verified", "used"):
            raise ServiceError(409, "already_used", "That code was already used. Start again.")
        v = dict(await self.db.get_verification(vid))
        try:
            return await self._session_for(v)
        except ServiceError:
            raise
        except Exception:
            # Let the app retry (MAS or Synapse briefly down) without a new code.
            await self.db.transition(vid, "used", "verified")
            log.exception("completing %s verification failed", v["purpose"])
            raise ServiceError(503, "try_again", "The server is busy. Try again in a minute.")

    async def _session_for(self, v: dict) -> dict:
        ident = Identifier(v["channel"], v["identifier"])
        phone = ident.value if ident.channel == "sms" else None
        if v["purpose"] == "signup" and not v["mxid"]:
            if v.get("invite_hash") and not await self.db.use_invite(v["invite_hash"], v["id"], self.now()):
                raise ServiceError(410, "bad_invite",
                                   "That invite link has expired or was already used. Ask the group admin for a new one.")
            try:
                username, user_id = await self._create_user(v["display_name"])
            except ServiceError:
                # No account was made and this verification is spent: give the invite back.
                if v.get("invite_hash"):
                    await self.db.release_invite(v["invite_hash"], v["id"])
                raise
            mxid = f"@{username}:{self.cfg.domain}"
            # Recorded first, so a retry after a later failure reuses this account.
            await self.db.execute("UPDATE verifications SET mxid=? WHERE id=?", (mxid, v["id"]))
            await self.db.upsert_account(mxid, user_id, username, code_managed=1, **{ident.field: ident.value})
            if v.get("invite_hash"):
                await self.db.set_invite_user(v["invite_hash"], mxid)
        else:
            mxid = v["mxid"]
            username = mxid[1:].split(":", 1)[0]
            acct = await self.db.account_by("mxid", mxid)
            user_id = acct["mas_user_id"] if acct else await self.mas.user_by_username(username)
            if user_id is None:
                raise ServiceError(404, "no_account", "That account no longer exists.")
            if acct is not None and acct["sms_status"] == "removed":
                raise ServiceError(404, "no_account", "That account no longer exists.")
            # switch: from here on the old password stops working; codes are the way in.
            # An SMS member's account (D14) becomes an ordinary app account at its first sign-in.
            await self.db.upsert_account(mxid, user_id, username, code_managed=1, sms_status=None,
                                         **{ident.field: ident.value})
        if ident.channel == "email":
            await self.mas.add_email(user_id, ident.value)

        password = secrets.token_urlsafe(32)
        await self.mas.set_password(user_id, password)
        sess = await self.matrix.password_login(username, password, self.cfg.device_name)

        if v["purpose"] == "signup" and v["display_name"]:
            try:
                await self.matrix.set_displayname(sess["access_token"], mxid, v["display_name"])
            except Exception as e:
                log.warning("setting display name for %s failed: %s", mxid, e)
        if self.bridge is not None:
            try:
                await self.bridge.app_user(mxid, phone)
            except Exception as e:
                log.error("bridge app-user for %s failed: %s", mxid, e)
        log.info("%s complete for %s", v["purpose"], mxid)
        return {
            "status": "ok",
            "homeserver": self.cfg.public_homeserver_url,
            "user_id": sess["user_id"],
            "device_id": sess.get("device_id"),
            "access_token": sess["access_token"],
            "refresh_token": sess.get("refresh_token"),
            "expires_in_ms": sess.get("expires_in_ms"),
        }

    async def _create_user(self, display_name: str) -> tuple[str, str]:
        base = username_base(display_name)
        for n in range(1, 50):
            username = base if n == 1 else f"{base}{n}"
            if await self.mas.user_by_username(username) is not None:
                continue
            try:
                return username, await self.mas.create_user(username, display_name)
            except UpstreamError as e:
                if e.status in (400, 409):  # taken on the homeserver side, or a race
                    continue
                raise
        raise ServiceError(409, "no_username", "Couldn't pick a username. Ask the group admin.")

    # ---- SMS members (D14) ------------------------------------------------------------

    async def sms_account(self, name: str, number: str) -> dict:
        """The bridge is adding ``number``: return the account it should post as, creating one if
        needed. app_user=True means the number already belongs to someone using the app."""
        number = normalize_number(number) or number
        acct = await self.db.account_by("phone", number)
        if acct is not None:
            if acct["sms_status"] is None:
                return {"mxid": acct["mxid"], "app_user": True}
            await self.db.upsert_account(acct["mxid"], acct["mas_user_id"], acct["username"], sms_status="sms")
            return {"mxid": acct["mxid"], "app_user": False}
        name = " ".join(name.split())[:MAX_NAME] or "Member"
        username, user_id = await self._create_user(name)
        mxid = f"@{username}:{self.cfg.domain}"
        await self.db.upsert_account(mxid, user_id, username, code_managed=1, sms_status="sms", phone=number)
        log.info("created %s for an SMS member", mxid)
        return {"mxid": mxid, "app_user": False}

    async def drop_sms_account(self, mxid: str) -> None:
        """A removed SMS member who never used the app can't sign in with their number any more.
        The Matrix account stays (its password is random and unknown), so re-adding them reuses it."""
        acct = await self.db.account_by("mxid", mxid)
        if acct is not None and acct["sms_status"] == "sms":
            await self.db.upsert_account(mxid, acct["mas_user_id"], acct["username"], sms_status="removed")

    async def sms_members(self, access_token: str | None, action: str, body: dict) -> dict:
        """The app's "add someone by text" screen: the caller is whoever owns the access token."""
        if self.bridge is None:
            raise ServiceError(503, "unavailable", "Adding people by text isn't set up.")
        mxid = await self.matrix.whoami(access_token) if access_token else None
        if mxid is None:
            raise ServiceError(401, "unauthorized", "Sign in again.")
        req: dict = {"by": mxid, "room_id": str(body.get("room_id") or "")}
        if action == "add":
            req["name"] = " ".join(str(body.get("display_name") or "").split())[:MAX_NAME]
        if action in ("add", "remove"):
            number = normalize_number(str(body.get("phone") or ""))
            if number is None:
                raise ServiceError(400, "bad_phone", "That doesn't look like a phone number.")
            req["number"] = number
        res = await self.bridge.sms_members(action, req)
        if res.get("ok") is False:
            status = {"forbidden": 403, "rate_limited": 429, "not_found": 404, "bad_name": 400,
                      "bad_phone": 400}.get(res.get("error"), 409)
            raise ServiceError(status, res.get("error") or "refused", res.get("message") or "Couldn't do that.")
        return res

    # ---- the signed-in person's own account, in the app instead of MAS's web pages ----

    async def _me(self, access_token: str | None) -> tuple[str, str | None, str]:
        """(mxid, device_id, MAS user id) of whoever owns ``access_token``."""
        me = await self.matrix.whoami_device(access_token) if access_token else None
        if me is None:
            raise ServiceError(401, "unauthorized", "Sign in again.")
        mxid, device = me
        if not mxid.endswith(f":{self.cfg.domain}"):
            raise ServiceError(403, "forbidden", "Only accounts on this server.")
        user_id = await self.mas.user_by_username(mxid[1:].split(":", 1)[0])
        if user_id is None:
            raise ServiceError(404, "no_account", "That account no longer exists.")
        return mxid, device, user_id

    async def account(self, access_token: str | None) -> dict:
        mxid, device, user_id = await self._me(access_token)
        acct = await self.db.account_by("mxid", mxid)
        sessions = sorted(await self.mas.active_sessions(user_id),
                          key=lambda s: s.get("last_active") or s.get("created") or "", reverse=True)
        emails = await self.mas.user_emails(user_id)
        return {
            "user_id": mxid,
            "sign_in": "codes" if acct and acct["code_managed"] else "password",
            "phone": acct["phone"] if acct else None,
            "email": (acct["email"] if acct and acct["email"] else None) or (emails[0] if emails else None),
            "sessions": [{"id": s["id"], "kind": s["kind"], "device_id": s["device_id"], "name": s["name"],
                          "last_active": s["last_active"], "current": bool(device) and s["device_id"] == device}
                         for s in sessions],
        }

    async def end_session(self, access_token: str | None, body: dict) -> dict:
        _, device, user_id = await self._me(access_token)
        kind, sid = str(body.get("kind") or ""), str(body.get("id") or "")
        if kind not in ("compat", "oauth2") or not sid:
            raise ServiceError(400, "bad_request", "Which device?")
        # Only the caller's own active sessions are listed, so an ID from someone else isn't found.
        mine = {(s["kind"], s["id"]): s for s in await self.mas.active_sessions(user_id)}
        s = mine.get((kind, sid))
        if s is None:
            raise ServiceError(404, "not_found", "That device is already signed out.")
        if device and s["device_id"] == device:
            raise ServiceError(400, "current_session", "That's this phone. Use Sign out instead.")
        await self.mas.finish_session(kind, sid)
        return {"ok": True}

    async def change_password(self, access_token: str | None, body: dict) -> dict:
        mxid, _, user_id = await self._me(access_token)
        acct = await self.db.account_by("mxid", mxid)
        if acct and acct["code_managed"]:
            raise ServiceError(400, "uses_codes", "You sign in with codes, so there's no password to change.")
        current, new = str(body.get("current") or ""), str(body.get("new") or "")
        if len(new) < 8:
            raise ServiceError(400, "weak_password", "Use at least 8 characters.")
        username = mxid[1:].split(":", 1)[0]
        try:
            # Checking the current password means signing in with it; that session is dropped at once.
            sess = await self.matrix.password_login(username, current, self.cfg.device_name)
        except UpstreamError as e:
            if e.status in (401, 403):
                raise ServiceError(403, "wrong_password", "Your current password isn't right.")
            raise
        try:
            await self.matrix.logout(sess["access_token"])
        except Exception as e:
            log.warning("dropping the password-check session for %s failed: %s", mxid, e)
        try:
            await self.mas.set_password(user_id, new, check=True)
        except UpstreamError as e:
            if e.status == 400:
                raise ServiceError(400, "weak_password", "Choose a stronger password.")
            raise
        log.info("password changed for %s", mxid)
        return {"ok": True}

    async def delete_account(self, access_token: str | None, body: dict) -> dict:
        mxid, _, user_id = await self._me(access_token)
        if str(body.get("confirm") or "") != mxid:
            raise ServiceError(400, "confirm", "Confirm with your user ID to delete your account.")
        await self.mas.deactivate(user_id)
        await self.db.execute("DELETE FROM accounts WHERE mxid=?", (mxid,))
        log.info("account %s deleted by its owner", mxid)
        return {"ok": True}

    # ---- admin --------------------------------------------------------------------

    async def admin_link(self, mxid: str, ident: Identifier) -> None:
        """Admin CLI: make an existing account code-managed for this email/phone. Its password stops
        working at the next code login; SMS to the phone stops then too, not now."""
        username = mxid[1:].split(":", 1)[0]
        user_id = await self.mas.user_by_username(username)
        if user_id is None:
            raise ServiceError(404, "no_account", f"{mxid} doesn't exist in MAS")
        other = await self.db.account_by(ident.field, ident.value)
        if other is not None and other["mxid"] != mxid:
            raise ServiceError(409, "already_registered", f"{ident.value} already belongs to {other['mxid']}")
        await self.db.upsert_account(mxid, user_id, username, code_managed=1, **{ident.field: ident.value})
        if ident.channel == "email":
            await self.mas.add_email(user_id, ident.value)
