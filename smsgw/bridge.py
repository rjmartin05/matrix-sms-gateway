"""Bridge core: SMS <-> Matrix routing, SMS commands, polls, DMs, admin commands.

All provider webhooks and Matrix events go through durable inboxes (db.inbound / db.matrix_inbox)
and are processed here sequentially, so ordering is preserved and a crash never loses an event.
Outbound SMS is enqueued with a (source, recipient) unique key so reprocessing can't double-send.
"""

from __future__ import annotations

import asyncio
import calendar
import html
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from . import commands, compliance, names, reactions
from .config import Config
from .db import Database, Member, day_key
from .media import MediaStore, initials, initials_avatar, resize_to_jpeg, square_avatar
from .outbound import OutboundQueue
from .port import MatrixPort
from .provider.base import InboundMessage, Provider, WebhookEvent
from .segments import count_segments, is_gsm7, truncate

log = logging.getLogger("smsgw.bridge")

POLL_START = ("m.poll.start", "org.matrix.msc3381.poll.start")
POLL_RESPONSE = ("m.poll.response", "org.matrix.msc3381.poll.response")
POLL_END = ("m.poll.end", "org.matrix.msc3381.poll.end")
MAX_INBOUND_TEXT = 4000
MAX_LINK_MEDIA_BYTES = 50 * 1024 * 1024
ALERT_DEBOUNCE = 3600
PENDING_DM_TTL = 600  # seconds a non-exact #dm waits for #yes
REACTION_LOOKBACK = 7 * 86400  # how far back a reaction text's quote is matched
SMS_STATUSES = ("allowlisted", "opted_in", "opted_out")  # the bridge posts as these members (not "app")
# Room state listing who currently gets the group by text, so the app can tag them (D14).
SMS_MEMBERS_EVENT = "io.github.rjmartin05.smsgw.sms_members"
LOGIN_CODE_RE = re.compile(r"sign[- ]?in code:?\s*(\d{6})", re.IGNORECASE)  # the app's "text us" code (D13)

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FILENAME_RE = re.compile(r"^[\w\-. ]+\.(jpe?g|png|gif|heic|webp|mp4|mov|bmp|tiff?)$", re.IGNORECASE)


def normalize_number(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+"):
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None


def sanitize_inbound(text: str) -> str:
    text = _CTRL_RE.sub("", text.replace("\r\n", "\n"))
    return text[:MAX_INBOUND_TEXT].strip()


def strip_reply_fallback(body: str) -> str:
    lines = body.split("\n")
    if lines and lines[0].startswith("> <"):
        while lines and lines[0].startswith(">"):
            lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines)


def first_name(name: str) -> str:
    return name.split()[0] if name.split() else name


def _poll_text(obj: dict | str | None, *keys: str) -> str:
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return ""
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str):
            return v
        if isinstance(v, list) and v and isinstance(v[0], dict) and isinstance(v[0].get("body"), str):
            return v[0]["body"]
    return ""


def parse_poll_start(evt_type: str, content: dict) -> tuple[str, list[tuple[str, str]], int, str] | None:
    """Return (question, [(answer_id, text)], max_selections, style) or None."""
    if evt_type == "m.poll.start":
        p = content.get("m.poll") or {}
        q = _poll_text(p.get("question"), "m.text", "body")
        answers = [(a.get("m.id"), _poll_text(a, "m.text", "body")) for a in p.get("answers") or []]
        style = "stable"
    else:
        p = content.get("org.matrix.msc3381.poll.start") or {}
        q = _poll_text(p.get("question"), "org.matrix.msc1767.text", "body", "m.text")
        answers = [(a.get("id"), _poll_text(a, "org.matrix.msc1767.text", "body", "m.text")) for a in p.get("answers") or []]
        style = "unstable"
    answers = [(str(i), t) for i, t in answers if i]
    if not q or not answers:
        return None
    return q, answers, int(p.get("max_selections") or 1), style


def parse_poll_response(evt_type: str, content: dict) -> tuple[str, list[str]] | None:
    rel = (content.get("m.relates_to") or {}).get("event_id")
    if not rel:
        return None
    if evt_type == "m.poll.response":
        sel = content.get("m.selections")
        if isinstance(sel, dict):  # some drafts nested it
            sel = sel.get("answers")
    else:
        sel = (content.get("org.matrix.msc3381.poll.response") or {}).get("answers")
    return rel, [str(s) for s in (sel or [])]


@dataclass
class Rendered:
    text: str
    media_url: str | None = None
    photo_off_text: str | None = None  # used instead for PHOTOS OFF members


class Bridge:
    MAX_ATTEMPTS = 8
    MAX_SMS_CHARS = 800  # Matrix -> SMS body cap (~6 GSM-7 segments) so a pasted essay can't run up cost

    def __init__(
        self,
        cfg: Config,
        db: Database,
        port: MatrixPort,
        provider: Provider,
        queue: OutboundQueue,
        media: MediaStore,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.mx = port
        self.provider = provider
        self.queue = queue
        self.media = media
        self.room_by_number: dict[str, str] = {}
        self.number_by_room: dict[str, str] = {}
        self.inbound_wakeup = asyncio.Event()
        self.matrix_wakeup = asyncio.Event()
        self._alert_last: dict[str, float] = {}
        self._stopped = False
        queue.on_alert = self.alert
        queue.on_blocked = self.on_send_blocked
        queue.estimate_segments = lambda t: count_segments(t).segments
        queue.on_sent = self.check_spend
        # Set when the login service is configured: forwards (number, code) from a sign-in text.
        self.forward_login_code: Callable[[str, str], Awaitable[None]] | None = None
        # Set when the login service is configured: (created_by, note) -> {"url", "expires_at"}.
        self.create_invite: Callable[[str, str | None], Awaitable[dict]] | None = None
        # Set when appservice.sms_accounts is on (D14): (name, number) -> {"mxid", "app_user"} makes or
        # finds the real account for a number; drop_sms_account(mxid) stops a removed one signing in.
        self.sms_account: Callable[[str, str], Awaitable[dict]] | None = None
        self.drop_sms_account: Callable[[str], Awaitable[None]] | None = None
        self._sms: dict[str, str] = {}  # mxid -> number, for members the bridge posts as
        self._badges: dict[str, list[str]] = {}  # room -> SMS member list last published

    # ---- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        for rc in self.cfg.rooms:
            room_id = await self.mx.resolve_room(rc.room)
            self.room_by_number[rc.number] = room_id
            self.number_by_room[room_id] = rc.number
            try:
                await self.mx.join(room_id, self.mx.bot_mxid)
            except Exception as e:
                # Invite-only room: the bot joins when an admin invites it (see _handle_invite).
                log.error("bot can't join %s yet (%s); invite %s to the room", rc.room, e, self.mx.bot_mxid)
        if self.cfg.alerts_room and self.cfg.alerts_room.startswith("#"):
            self.cfg.alerts_room = await self.mx.resolve_room(self.cfg.alerts_room)
        await self.refresh_sms()

    def stop(self) -> None:
        self._stopped = True
        self.inbound_wakeup.set()
        self.matrix_wakeup.set()

    async def run_inbound(self) -> None:
        await self._run_loop(self.inbound_wakeup, self.db.pending_inbound, self._process_inbound_row, "key")

    async def run_matrix(self) -> None:
        await self._run_loop(self.matrix_wakeup, self.db.pending_matrix, self._process_matrix_row, "event_id")

    async def _run_loop(self, wakeup, fetch, process, key_col) -> None:
        """Process a durable inbox in order.

        A failure (typically Synapse restarting) stops the batch and retries the same row with
        backoff, so later events don't overtake it. Retries are safe: SMS enqueues are deduped by
        (source, recipient) and Matrix sends reuse deterministic txn IDs. After MAX_ATTEMPTS the
        row is parked (processed=2) and an alert is raised.
        """
        failures: dict[str, int] = {}
        mark = self.db.mark_inbound if key_col == "key" else self.db.mark_matrix
        while not self._stopped:
            rows = await fetch()
            blocked = False
            for row in rows:
                key = row[key_col]
                try:
                    await process(row)
                except Exception as e:
                    n = failures.get(key, 0) + 1
                    failures[key] = n
                    log.exception("failed processing %s (attempt %d)", key, n)
                    if n < self.MAX_ATTEMPTS:
                        blocked = True
                        await asyncio.sleep(min(300, 5 * 2 ** (n - 1)))
                        break
                    await mark(key, 2)
                    failures.pop(key, None)
                    await self.alert(f"stuck:{key_col}", f"Gave up processing {key} after {n} attempts: {e!r}")
                    continue
                failures.pop(key, None)
                await mark(key, 1)
            if not rows and not blocked:
                wakeup.clear()
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass

    async def _process_inbound_row(self, row) -> None:
        evt = self.provider.parse(row["raw"].encode())
        if evt is not None:
            await self.handle_provider_event(evt)

    async def _process_matrix_row(self, row) -> None:
        await self.handle_matrix_event(json.loads(row["raw"]))

    # ---- helpers --------------------------------------------------------------------

    def number_for(self, mxid: str) -> str | None:
        """The phone number of an SMS member the bridge posts as, or None for anyone else."""
        number = self._sms.get(mxid)
        if number is None and not self.cfg.sms_accounts:
            number = self.mx.number_for_ghost(mxid)
        return number

    def is_ghost(self, mxid: str) -> bool:
        return self.number_for(mxid) is not None

    def sms_display(self, name: str) -> str:
        # Real accounts (D14) keep a plain name, the one they'll have in the app; the app tags them instead.
        return name if self.cfg.sms_accounts else f"{name}{self.cfg.ghost_name_suffix}"

    async def refresh_sms(self) -> None:
        """Reload who the bridge posts as, and republish each room's SMS member list (D14)."""
        rows = await self.db.fetchall("SELECT room_id, number, mxid, status FROM members")
        self._sms = {r["mxid"]: r["number"] for r in rows if r["status"] in SMS_STATUSES}
        for room_id in self.number_by_room:
            users = sorted({r["mxid"] for r in rows if r["room_id"] == room_id and r["status"] == "opted_in"})
            if self._badges.get(room_id) == users:
                continue
            try:
                await self.mx.set_state(room_id, SMS_MEMBERS_EVENT, {"users": users})
                self._badges[room_id] = users
            except Exception:
                log.exception("publishing the SMS member list for %s failed", room_id)

    async def ensure_profile(self, m: Member, *, force: bool = False) -> None:
        """Display name and generated avatar for an SMS member. Ghosts get it every time; real accounts
        only when added or renamed, so a name or photo they later set in the app isn't overwritten."""
        if self.cfg.sms_accounts and not force:
            return
        await self.mx.set_displayname(m.mxid, self.sms_display(m.display_name))
        await self.ensure_avatar(m.mxid, m.number, m.display_name)

    def strip_suffix(self, name: str) -> str:
        sfx = self.cfg.ghost_name_suffix
        return name[: -len(sfx)] if sfx and name.endswith(sfx) else name

    async def ensure_avatar(self, mxid: str, number: str, name: str) -> None:
        """Give an SMS ghost a generated initials avatar, unless they set their own with #avatar.

        Without one, clients build initials from "Name (SMS)" and show "R(". Regenerated when the
        initials change (#name, !sms add). Best effort: never blocks messaging.
        """
        key = f"avatar:{number}"
        current = await self.db.get_meta(key)
        want = f"gen:{initials(name)}"
        if current == "custom" or current == want:
            return
        try:
            png = await asyncio.to_thread(initials_avatar, name, number)
            await self.mx.set_avatar(mxid, png, "image/png")
            await self.db.set_meta(key, want)
        except Exception:
            log.exception("couldn't set generated avatar for %s", number)

    async def in_group(self, mxid: str) -> bool:
        """Only members of a bridged room may DM SMS members (registration is token-gated, but still)."""
        for room_id in self.number_by_room:
            if mxid in await self.mx.joined_members(room_id):
                return True
        return False

    async def sender_name(self, room_id: str, mxid: str) -> str:
        num = self.number_for(mxid)
        if num:
            m = await self.db.get_member(room_id, num)
            if m:
                return m.display_name
        return self.strip_suffix(await self.mx.display_name(room_id, mxid))

    async def candidates(self, room_id: str) -> list[names.Candidate]:
        joined = await self.mx.joined_members(room_id)
        people: list[tuple[str, str, str]] = []
        for mxid, dn in joined.items():
            if mxid == self.mx.bot_mxid:
                continue
            num = self.number_for(mxid)
            if num:
                m = await self.db.get_member(room_id, num)
                if m is None or m.status != "opted_in":
                    continue
                people.append((m.display_name, "", mxid))
            else:
                localpart = mxid[1:].split(":", 1)[0]
                people.append((self.strip_suffix(dn or localpart), localpart, mxid))
        people.sort(key=lambda p: (p[0].casefold(), p[2]))
        return [names.Candidate(i + 1, n, u, k) for i, (n, u, k) in enumerate(people)]

    async def reply_sms(self, room_id: str | None, number: str, text: str, source_key: str, *, kind: str = "reply") -> None:
        from_number = self.number_by_room.get(room_id or "") or self.cfg.rooms[0].number
        await self.db.enqueue(
            source_key=source_key, room_id=room_id, from_number=from_number,
            to_number=number, kind=kind, text=text,
        )
        self.queue.notify()

    async def alert(self, key: str, text: str) -> None:
        log.warning("ALERT %s: %s", key, text)
        now = time.time()
        if now - self._alert_last.get(key, 0) < ALERT_DEBOUNCE:
            return
        self._alert_last[key] = now
        if self.cfg.alerts_room:
            try:
                await self.mx.send_text(self.cfg.alerts_room, self.mx.bot_mxid, f"SMS bridge: {text}", notice=True)
            except Exception:
                log.exception("failed to post alert")

    # ---- consent state changes ------------------------------------------------------

    async def _opt_out(self, number: str, *, source: str, keyword: str | None, provider_id: str | None, detail: str | None = None) -> None:
        rows = await self.db.fetchall("SELECT room_id, mxid, status FROM members WHERE number=?", (number,))
        await self.db.set_number_status_all_rooms(number, "opted_out")
        skipped = await self.db.skip_pending_for(number, "recipient opted out")
        await self.db.log_consent(
            number, "opt_out", source, keyword=keyword, provider_message_id=provider_id,
            detail=detail or (f"{skipped} queued message(s) dropped" if skipped else None),
        )
        for r in rows:
            if r["status"] not in ("removed", "app"):
                try:
                    await self.mx.leave(r["room_id"], r["mxid"])
                except Exception:
                    log.exception("ghost leave failed")
        await self.refresh_sms()

    async def _opt_in(self, member: Member, *, event: str, source: str, keyword: str, provider_id: str | None) -> None:
        await self.db.set_member_status(member.room_id, member.number, "opted_in")
        await self.db.execute("UPDATE members SET consecutive_failures=0 WHERE number=?", (member.number,))
        await self.db.log_consent(
            member.number, event, source, room_id=member.room_id, keyword=keyword, provider_message_id=provider_id
        )
        await self.ensure_profile(member)
        await self.mx.join(member.room_id, member.mxid)
        await self.refresh_sms()

    async def app_user(self, mxid: str, number: str | None) -> None:
        """The login service says ``mxid`` now uses the app (D13): invite them to the group rooms and,
        if ``number`` is an SMS member, stop texting it. Idempotent. Texting START or JOIN later
        turns texts back on."""
        for room_id in self.number_by_room:
            await self.mx.invite(room_id, mxid)
        if not number:
            return
        rows = await self.db.fetchall(
            "SELECT room_id, mxid FROM members WHERE number=? AND status IN ('allowlisted', 'opted_in', 'opted_out')",
            (number,))
        if not rows:
            return
        for r in rows:
            await self.db.set_member_status(r["room_id"], number, "app")
            if r["mxid"] == mxid:
                continue  # a real account (D14): the same user just signed in, so they stay in the room
            try:
                await self.mx.leave(r["room_id"], r["mxid"])
            except Exception:
                log.exception("ghost leave failed")
        await self.refresh_sms()
        skipped = await self.db.skip_pending_for(number, "moved to the app")
        await self.db.log_consent(number, "moved_to_app", "login", detail=f"{mxid}; {skipped} queued message(s) dropped")
        log.info("%s moved to the app as %s", number, mxid)

    async def on_send_blocked(self, number: str, error: str) -> None:
        """Telnyx refused a send because of an opt-out block we didn't know about."""
        rows = await self.db.fetchall("SELECT status FROM members WHERE number=?", (number,))
        if any(r["status"] == "opted_out" for r in rows):
            return
        await self._opt_out(number, source="send_error", keyword=None, provider_id=None, detail=error)

    # ---- provider events ------------------------------------------------------------

    async def handle_provider_event(self, evt: WebhookEvent) -> None:
        if evt.message:
            await self.handle_sms(evt.message)
        elif evt.delivery:
            d = evt.delivery
            await self.queue.delivery_update(d.provider_id, d.status, d.failed, d.delivered, d.error)

    async def handle_sms(self, msg: InboundMessage) -> None:
        room_id = self.room_by_number.get(msg.to_number)
        number = normalize_number(msg.from_number) or msg.from_number
        if room_id is None:
            log.warning("inbound to unconfigured number %s; ignoring", msg.to_number)
            return
        text = sanitize_inbound(msg.text)
        await self.db.add_usage("mms_in" if msg.media else "sms_in", count_segments(text).segments if text else 1)
        await self.check_spend()
        member = await self.db.get_member(room_id, number)
        if member and msg.carrier and member.carrier != msg.carrier:
            await self.db.set_member_field(room_id, number, "carrier", msg.carrier)
        src = f"in:{msg.provider_id}"

        # 1. Telnyx already handled a keyword (and sent the reply). Record it, send nothing.
        if msg.autoresponse_type:
            await self._handle_autoresponse(msg, room_id, number, member, text)
            return

        # 2. A sign-in code for the app goes to the login service, never to the group, and gets no reply
        #    (the sender may not have opted in to texts).
        m = LOGIN_CODE_RE.fullmatch(text) if text and self.forward_login_code else None
        if m:
            try:
                await self.forward_login_code(number, m.group(1))
            except Exception as e:
                log.error("forwarding sign-in code failed: %s", e)  # the app times out; they try again
            return

        cmd = commands.parse(text) if text else None

        # 3. Opt-in/out/help keywords Telnyx did *not* flag.
        if cmd and cmd.name == "STOP":
            await self._opt_out(number, source="bridge", keyword=text, provider_id=msg.provider_id,
                                detail="no autoresponse_type from Telnyx")
            # If Telnyx did block, this send is refused (40300) and nothing goes out, so no double reply.
            await self.db.enqueue(
                source_key=src, room_id=room_id, from_number=msg.to_number, to_number=number,
                kind="optout_confirm", text=compliance.opt_out(self.cfg.brand),
            )
            self.queue.notify()
            await self.alert("unflagged-stop", f"STOP from {number} arrived without a Telnyx autoresponse flag; check autoresp_configs.")
            return
        if cmd and cmd.name == "START":
            if await self._resubscribe(member, room_id, number, text, msg.provider_id, "bridge"):
                await self.reply_sms(room_id, number, compliance.opt_in(self.cfg.brand), src)
            return
        if cmd and cmd.name == "JOIN":
            await self._handle_join(member, room_id, number, text, msg.provider_id, src)
            return
        if cmd and cmd.name == "HELP":
            if member and member.status == "opted_in":
                await self.reply_sms(room_id, number, await self._help_for(member), src)
            elif not (member and member.status == "opted_out"):
                await self.reply_sms(room_id, number, compliance.help_registered(self.cfg.brand, self.cfg.contact_email), src)
            return

        # 4. Everything else requires an opted-in member.
        if member is None or member.status != "opted_in":
            log.info("ignoring message from %s (status %s)", number, member.status if member else "unknown")
            return

        if not msg.media and text and await self._try_reaction(member, text, msg.provider_id):
            return
        if cmd is not None and cmd.error:
            await self.reply_sms(room_id, number, cmd.error, src)
            return
        if cmd is not None:
            handled = await self._handle_member_command(cmd, member, msg, text, src)
            if handled:
                return
        await self._post_to_group(member, msg, text)

    async def _sms_avatar(self, member: Member, msg: InboundMessage, reset: bool, src: str) -> None:
        room_id, number = member.room_id, member.number
        if reset:
            await self.db.set_meta(f"avatar:{number}", "")
            await self.ensure_avatar(member.mxid, number, member.display_name)
            await self.reply_sms(room_id, number, "Profile picture reset.", src)
            return
        images = [m for m in msg.media if (m.content_type or "").startswith("image/")] or msg.media
        if not images:
            await self.reply_sms(room_id, number, "Text a photo with #avatar as the message to set your picture.", src)
            return
        try:
            data, _ = await self.provider.fetch_media(images[0].url, self.cfg.inbound_media_max_bytes)
            jpeg = await asyncio.to_thread(square_avatar, data)
            await self.mx.set_avatar(member.mxid, jpeg, "image/jpeg")
        except Exception:
            log.exception("#avatar from %s failed", number)
            await self.reply_sms(room_id, number, "Couldn't use that photo. Try another one.", src)
            return
        await self.db.set_meta(f"avatar:{number}", "custom")
        await self.reply_sms(room_id, number, "Profile picture updated.", src)

    async def _try_reaction(self, member: Member, text: str, provider_id: str) -> bool:
        """Turn a phone reaction text into a Matrix reaction. False = not a reaction we can place."""
        r = reactions.parse(text)
        if r is None:
            return False
        target = None
        for row in await self.db.recent_group_texts(member.room_id, member.number, time.time() - REACTION_LOOKBACK):
            if reactions.quote_matches(r.quote, row["text"]):
                target = row["source_key"][len("mx:"):]
                break
        if target is None:
            # Phones also let you react to your own sent texts; those quote the text without a name prefix.
            for event_id, sent in await self.db.recent_own_posts(member.room_id, member.number, time.time() - REACTION_LOOKBACK):
                if reactions.quote_matches(r.quote, sent):
                    target = event_id
                    break
        if target is None:
            log.info("reaction text from %s didn't match a recent message; posting as text", member.number)
            return False
        target = await self._edit_target(member.room_id, target)
        ghost = member.mxid
        try:
            if r.remove:
                reaction_id = await self.db.pop_reaction(member.room_id, member.number, target, r.key)
                if reaction_id:
                    await self.mx.redact(member.room_id, ghost, reaction_id)
            else:
                reaction_id = await self.mx.send_reaction(member.room_id, ghost, target, r.key, txn_id=f"react-{provider_id}")
                await self.db.save_reaction(member.room_id, member.number, target, r.key, reaction_id)
        except Exception as e:
            # e.g. the same reaction twice (M_DUPLICATE_ANNOTATION) or a redacted target. Dropping a
            # reaction is better than posting the raw "👍 to “…”" text or retrying forever.
            log.warning("reaction from %s on %s not applied: %s", member.number, target, e)
        return True

    async def _edit_target(self, room_id: str, event_id: str) -> str:
        """An edit (m.replace) went out as its own text; react to the original message instead."""
        try:
            evt = await self.mx.get_event(room_id, event_id)
        except Exception:
            return event_id
        rel = ((evt or {}).get("content") or {}).get("m.relates_to") or {}
        if rel.get("rel_type") == "m.replace" and rel.get("event_id"):
            return rel["event_id"]
        return event_id

    async def _handle_autoresponse(self, msg: InboundMessage, room_id: str, number: str, member: Member | None, text: str) -> None:
        kind = msg.autoresponse_type.upper()
        if kind in ("STOP", "OPT_OUT", "OPTOUT"):
            await self._opt_out(number, source="telnyx_autoresponse", keyword=text, provider_id=msg.provider_id)
        elif kind in ("START", "OPT_IN", "OPTIN"):
            await self._resubscribe(member, room_id, number, text, msg.provider_id, "telnyx_autoresponse")
        elif kind == "HELP":
            await self.db.log_consent(number, "help", "telnyx_autoresponse", room_id=room_id, keyword=text,
                                      provider_message_id=msg.provider_id)
        else:
            await self.db.log_consent(number, f"autoresponse:{kind}", "telnyx_autoresponse", room_id=room_id,
                                      keyword=text, provider_message_id=msg.provider_id)

    async def _resubscribe(self, member: Member | None, room_id: str, number: str, keyword: str, provider_id: str, source: str) -> bool:
        """START only re-subscribes numbers that previously JOINed and are still allowlisted."""
        if member is None or member.status == "removed" or not member.ever_joined:
            await self.db.log_consent(number, "start_ignored", source, room_id=room_id, keyword=keyword,
                                      provider_message_id=provider_id,
                                      detail="not allowlisted" if member is None or member.status == "removed" else "never joined")
            return False
        await self._opt_in(member, event="resubscribe", source=source, keyword=keyword, provider_id=provider_id)
        # Telnyx block rules are per messaging profile: lift opt-out on other rooms the number had joined.
        await self.db.execute(
            "UPDATE members SET status='opted_in' WHERE number=? AND status='opted_out' AND ever_joined=1",
            (number,),
        )
        return True

    async def _handle_join(self, member: Member | None, room_id: str, number: str, text: str, provider_id: str, src: str) -> None:
        if member is None or member.status == "removed":
            await self.db.log_consent(number, "join_rejected", "bridge", room_id=room_id, keyword=text,
                                      provider_message_id=provider_id, detail="not allowlisted")
            await self.reply_sms(room_id, number, compliance.not_allowlisted(self.cfg.members_can_add), src)
            return
        if member.status == "opted_out":
            # Telnyx keeps blocking until START/UNSTOP; any reply would be refused (40300).
            await self.db.log_consent(number, "join_while_opted_out", "bridge", room_id=room_id, keyword=text,
                                      provider_message_id=provider_id, detail="must text START")
            return
        already = member.status == "opted_in"
        if not already:
            await self._opt_in(member, event="opt_in", source="bridge", keyword=text, provider_id=provider_id)
        await self.reply_sms(room_id, number, compliance.opt_in(self.cfg.brand), src)
        await self.db.enqueue(
            source_key=src + ":2", room_id=room_id, from_number=self.number_by_room[room_id],
            to_number=number, kind="reply", text=compliance.opt_in_followup(self.cfg.brand),
        )
        self.queue.notify()

    async def _help_for(self, member: Member) -> str:
        pending = await self.db.get_pending_dm(member.room_id, member.number)
        return commands.help_text(self.cfg.brand, self.cfg.contact_email, dm_pending=pending is not None)

    async def _handle_member_command(self, cmd: commands.Parsed, member: Member, msg: InboundMessage, text: str, src: str) -> bool:
        room_id = member.room_id
        if cmd.name == "HELP":
            await self.reply_sms(room_id, member.number, await self._help_for(member), src)
            return True
        if cmd.name == "WHO":
            cands = await self.candidates(room_id)
            lines = [f"{c.number}) {c.name}" for c in cands]
            await self.reply_sms(room_id, member.number, "Members:\n" + "\n".join(lines), src)
            return True
        if cmd.name == "VOTE":
            await self._sms_vote(member, cmd.args["choice"], src, msg.provider_id)
            return True
        if cmd.name == "NAME":
            name = cmd.args["name"]
            await self.db.set_member_field(room_id, member.number, "display_name", name)
            await self.mx.set_displayname(member.mxid, self.sms_display(name))
            await self.ensure_avatar(member.mxid, member.number, name)
            await self.reply_sms(room_id, member.number, f"Name set to {name}.", src)
            return True
        if cmd.name == "AVATAR":
            await self._sms_avatar(member, msg, cmd.args["reset"], src)
            return True
        if cmd.name == "PHOTOS":
            on = cmd.args["on"]
            await self.db.set_member_field(room_id, member.number, "photos_on", 1 if on else 0)
            await self.reply_sms(
                room_id, member.number,
                "Photos on." if on else "Photos off. You'll get a text note instead.", src,
            )
            return True
        if cmd.name == "DM":
            await self._sms_dm(member, cmd.args["rest"], src, msg.provider_id)
            return True
        if cmd.name == "YES":
            pending = await self.db.get_pending_dm(room_id, member.number)
            if pending is None:
                await self.reply_sms(room_id, member.number, "Nothing to send.", src)
                return True
            await self.db.clear_pending_dm(room_id, member.number)
            await self._deliver_dm(member, pending["target_mxid"], pending["message"], src, msg.provider_id,
                                   confirm_name=pending["target_name"])
            return True
        if cmd.name == "R":
            if not member.last_dm_from:
                await self.reply_sms(room_id, member.number, "Nobody has sent you a DM yet. Text #dm name msg to start one.", src)
                return True
            await self._deliver_dm(member, member.last_dm_from, cmd.args["text"], src, msg.provider_id)
            return True
        return False

    async def _sms_vote(self, member: Member, choice: int, src: str, provider_id: str) -> None:
        poll = await self.db.latest_open_poll(member.room_id)
        if poll is None:
            await self.reply_sms(member.room_id, member.number, "No open poll right now.", src)
            return
        answers = json.loads(poll["answers"])
        if not 1 <= choice <= len(answers):
            await self.reply_sms(member.room_id, member.number, f"Reply #vote 1 to #vote {len(answers)}.", src)
            return
        answer_id, answer_text = answers[choice - 1]
        ghost = member.mxid
        await self.mx.send_poll_response(
            member.room_id, ghost, poll["event_id"], answer_id, poll["style"], txn_id=f"vote-{provider_id}"
        )
        await self.db.record_vote(poll["event_id"], ghost, [answer_id])
        await self.reply_sms(member.room_id, member.number, f"Vote recorded: {truncate(answer_text, 40)}", src)

    async def _sms_dm(self, member: Member, rest: str, src: str, provider_id: str) -> None:
        room_id = member.room_id
        await self.db.clear_pending_dm(room_id, member.number)  # a new #dm replaces any pending one
        me = member.mxid
        cands = await self.candidates(room_id)
        res = names.resolve_dm(rest, cands)
        if res.kind == "exact" and res.target.key == me:
            await self.reply_sms(room_id, member.number, "That's you. Text #who for the list.", src)
            return
        if res.kind == "ambiguous" and any(c.key == me for c in res.candidates):
            # You can't DM yourself, so you don't count toward a shared first name.
            others = tuple(c for c in res.candidates if c.key != me)
            res = (names.Resolution("exact", others[0], message=res.message, query=res.query) if len(others) == 1
                   else names.Resolution("ambiguous", candidates=others, message=res.message, query=res.query))
        if res.kind in ("fuzzy", "none"):
            res = names.resolve_dm(rest, [c for c in cands if c.key != me])  # never suggest yourself
        if res.kind == "exact":
            await self._deliver_dm(member, res.target.key, res.message, src, provider_id, confirm_name=res.target.name)
        elif res.kind == "ambiguous":
            await self.reply_sms(room_id, member.number, names.ambiguous_reply(res), src)
        elif res.kind == "fuzzy":
            if res.target is not None:
                await self.db.set_pending_dm(room_id, member.number, res.target.key, res.target.name,
                                             res.message, PENDING_DM_TTL)
            await self.reply_sms(room_id, member.number, names.fuzzy_reply(res), src)
        else:
            await self.reply_sms(room_id, member.number, names.none_reply(res), src)

    async def _deliver_dm(self, member: Member, target_mxid: str, text: str, src: str, provider_id: str, confirm_name: str | None = None) -> None:
        room_id = member.room_id
        sender_ghost = member.mxid
        target_num = self.number_for(target_mxid)
        name = confirm_name or await self.sender_name(room_id, target_mxid)
        if target_num:
            target = await self.db.get_member(room_id, target_num)
            if target is None or target.status != "opted_in":
                await self.reply_sms(room_id, member.number, f"{name} isn't receiving texts right now.", src)
                return
            await self.db.enqueue(
                source_key=src + ":dm", room_id=room_id, from_number=self.number_by_room[room_id],
                to_number=target_num, kind="dm", text=f"DM from {member.display_name}: {text}",
                carrier=target.carrier,
            )
            await self.db.set_member_field(room_id, target_num, "last_dm_from", sender_ghost)
        else:
            dm_room = await self.db.get_dm_room(sender_ghost, target_mxid)
            if dm_room is None:
                dm_room = await self.mx.create_dm(sender_ghost, target_mxid)
                await self.db.set_dm_room(sender_ghost, target_mxid, dm_room)
            await self.mx.send_text(dm_room, sender_ghost, text, txn_id=f"dm-{provider_id}")
        await self.reply_sms(room_id, member.number, f"Sent to {name}", src + ":ack")

    async def _post_to_group(self, member: Member, msg: InboundMessage, text: str) -> None:
        room_id = member.room_id
        ghost = member.mxid
        await self.ensure_profile(member)
        await self.mx.join(room_id, ghost)
        first_event = None
        for i, m in enumerate(msg.media):
            try:
                data, ctype = await self.provider.fetch_media(m.url, self.cfg.inbound_media_max_bytes)
            except Exception as e:
                log.warning("inbound media fetch failed: %s", e)
                await self.mx.send_text(room_id, ghost, "sent a photo that couldn't be downloaded",
                                        notice=True, txn_id=f"sms-{msg.provider_id}-m{i}")
                continue
            ctype = (m.content_type or ctype or "application/octet-stream").split(";")[0]
            if ctype in ("application/smil", "text/plain"):
                continue  # MMS container parts
            ext = ctype.split("/")[-1].replace("jpeg", "jpg")
            evt = await self.mx.send_media(room_id, ghost, data, ctype, f"photo-{i + 1}.{ext}",
                                           txn_id=f"sms-{msg.provider_id}-m{i}")
            first_event = first_event or evt
        if text:
            body, html_body, mention_ids = await self._render_mentions(room_id, text)
            evt = await self.mx.send_text(room_id, ghost, body, html=html_body, mention_user_ids=mention_ids,
                                          txn_id=f"sms-{msg.provider_id}")
            first_event = first_event or evt
        await self.db.map_message(msg.provider_id, first_event, room_id, member.number)

    async def _render_mentions(self, room_id: str, text: str) -> tuple[str, str | None, list[str]]:
        if "@" not in text:
            return text, None, []
        cands = await self.candidates(room_id)
        found = names.find_mentions(text, cands)
        if not found:
            return text, None, []
        parts: list[str] = []
        pos = 0
        ids: list[str] = []
        for m in found:
            parts.append(html.escape(text[pos:m.start]))
            parts.append(f'<a href="https://matrix.to/#/{html.escape(m.target.key)}">{html.escape(m.target.name)}</a>')
            ids.append(m.target.key)
            pos = m.end
        parts.append(html.escape(text[pos:]))
        return text, "".join(parts).replace("\n", "<br>"), sorted(set(ids))

    # ---- Matrix events --------------------------------------------------------------

    async def wants_matrix_event(self, evt: dict) -> bool:
        """Cheap filter used by the appservice handler before persisting.

        With real-account SMS members (D14) the registration covers every user, so Synapse sends the
        bridge every event on the server. Only the group rooms, SMS members' DM rooms and ! commands
        in rooms the bot is in are kept; everything else is dropped unread."""
        etype = evt.get("type")
        if etype == "m.room.member":
            return evt.get("content", {}).get("membership") == "invite" and (
                evt.get("state_key") == self.mx.bot_mxid or self.is_ghost(evt.get("state_key", ""))
            )
        if etype not in ("m.room.message",) + POLL_START + POLL_RESPONSE + POLL_END:
            return False
        if not self.cfg.sms_accounts:
            return True
        room_id = evt.get("room_id", "")
        if room_id in self.number_by_room or await self.db.dm_room_info(room_id) is not None:
            return True
        body = ((evt.get("content") or {}).get("body") or "").strip()
        return etype == "m.room.message" and body.startswith("!") and await self.mx.bot_joined(room_id)

    async def handle_matrix_event(self, evt: dict) -> None:
        etype = evt.get("type")
        room_id = evt.get("room_id", "")
        sender = evt.get("sender", "")
        content = evt.get("content") or {}
        if etype == "m.room.member":
            await self._handle_invite(evt)
            return
        if room_id in self.number_by_room:
            await self._handle_group_event(etype, room_id, sender, content, evt)
            return
        dm = await self.db.dm_room_info(room_id)
        if dm is not None:
            if etype == "m.room.message" and not self.is_ghost(sender) and sender != self.mx.bot_mxid:
                await self._handle_dm_from_matrix(room_id, dm[0], sender, content, evt["event_id"])
            return
        if etype == "m.room.message" and sender != self.mx.bot_mxid and not self.is_ghost(sender):
            body = (content.get("body") or "").strip()
            if body.startswith("!"):
                await self._handle_bang_command(room_id, sender, body, in_group=False)

    async def _handle_invite(self, evt: dict) -> None:
        target = evt.get("state_key", "")
        room_id = evt["room_id"]
        inviter = evt.get("sender", "")
        if target == self.mx.bot_mxid:
            await self.mx.join(room_id, target)
            return
        number = self.number_for(target)
        if number is None or self.is_ghost(inviter) or inviter == self.mx.bot_mxid:
            return
        rows = await self.db.fetchall("SELECT * FROM members WHERE number=? AND status != 'removed'", (number,))
        if not rows or not await self.in_group(inviter):
            return  # unknown number or inviter outside the group: don't join
        invite_state = (evt.get("unsigned") or {}).get("invite_room_state") or []
        encrypted = any(s.get("type") == "m.room.encryption" for s in invite_state)
        await self.mx.join(room_id, target)
        if encrypted:
            name = Member.from_row(rows[0]).display_name
            await self.mx.send_text(
                room_id, target,
                f"This chat is encrypted, so it can't reach {name} by text. "
                f"Send !dm {name} in the group and I'll open a chat that works.",
                notice=True,
            )
            await self.mx.leave(room_id, target)
            return
        await self.db.set_dm_room(target, inviter, room_id)

    async def _handle_group_event(self, etype: str, room_id: str, sender: str, content: dict, evt: dict) -> None:
        event_id = evt["event_id"]
        if etype in POLL_START:
            parsed = parse_poll_start(etype, content)
            if parsed is None:
                return
            q, answers, max_sel, style = parsed
            await self.db.add_poll(event_id, room_id, sender, q, answers, style, max_sel)
            if sender == self.mx.bot_mxid:
                return
            name = first_name(await self.sender_name(room_id, sender))
            opts = ", ".join(f"#vote {i + 1} ({truncate(t, 30)})" for i, (_, t) in enumerate(answers))
            text = f"{self.cfg.brand} poll from {name}: {q} Reply {opts}."
            await self._fan_out(room_id, sender, Rendered(text), f"mx:{event_id}")
            return
        if etype in POLL_RESPONSE:
            parsed_r = parse_poll_response(etype, content)
            if parsed_r:
                poll = await self.db.get_poll(parsed_r[0])
                if poll and not poll["closed"]:
                    await self.db.record_vote(parsed_r[0], sender, parsed_r[1])
            return
        if etype in POLL_END:
            rel = (content.get("m.relates_to") or {}).get("event_id")
            if rel and await self.db.close_poll(rel):
                poll = await self.db.get_poll(rel)
                await self._fan_out(room_id, sender, Rendered(await self._poll_results_text(poll)), f"mx:{event_id}")
            return
        if etype != "m.room.message" or sender == self.mx.bot_mxid:
            return
        body = (content.get("body") or "").strip()
        if body.startswith("!") and not self.is_ghost(sender):
            await self._handle_bang_command(room_id, sender, body, in_group=True)
            return
        rendered = await self.render_for_sms(room_id, sender, content)
        if rendered:
            await self._fan_out(room_id, sender, rendered, f"mx:{event_id}")

    async def _poll_results_text(self, poll) -> str:
        answers = json.loads(poll["answers"])
        counts = {a: 0 for a, _ in answers}
        for sel in await self.db.poll_votes(poll["event_id"]):
            for a in sel[: poll["max_selections"]]:
                if a in counts:
                    counts[a] += 1
        res = ", ".join(f"{truncate(t, 30)} {counts[a]}" for a, t in answers)
        return f"{self.cfg.brand} poll results: {poll['question']} {res}"

    async def _fan_out(self, room_id: str, sender: str, r: Rendered, source_key: str) -> None:
        exclude = self.number_for(sender)
        from_number = self.number_by_room[room_id]
        for m in await self.db.members(room_id, "opted_in"):
            if m.number == exclude:
                continue
            if r.media_url and not m.photos_on:
                text, media = r.photo_off_text or r.text, None
            else:
                text, media = r.text, r.media_url
            await self.db.enqueue(
                source_key=source_key, room_id=room_id, from_number=from_number, to_number=m.number,
                kind="group", text=text, media_url=media, carrier=m.carrier,
            )
        self.queue.notify()

    async def render_for_sms(self, room_id: str, sender: str, content: dict, *, dm: bool = False) -> Rendered | None:
        name = await self.sender_name(room_id, sender)
        prefix = f"DM from {name}" if dm else name
        relates = content.get("m.relates_to") or {}
        edited = relates.get("rel_type") == "m.replace"
        if edited:
            content = content.get("m.new_content") or content
        msgtype = content.get("msgtype")
        body = strip_reply_fallback(content.get("body") or "").strip()
        quote = ""
        reply_to = (relates.get("m.in_reply_to") or {}).get("event_id")
        if reply_to and not edited:
            quote = await self._reply_quote(room_id, reply_to)
        mark = "(edited) " if edited else ""

        if msgtype in ("m.text", "m.notice"):
            if not body:
                return None
            if len(body) > self.MAX_SMS_CHARS:
                body = truncate(body, self.MAX_SMS_CHARS) + " (more in the app)"
            return Rendered(f"{prefix}: {quote}{mark}{body}")
        if msgtype == "m.emote":
            return Rendered(f"* {name} {mark}{body}" if not dm else f"{prefix}: * {body}")
        if msgtype == "m.image" and not edited:
            caption = self._caption(content)
            url = content.get("url")
            if not url:
                return Rendered(f"{name} sent a photo")
            data = await self.mx.download(url)
            try:
                jpeg = await asyncio.to_thread(resize_to_jpeg, data, self.cfg.image_max_px, self.cfg.image_max_bytes)
            except Exception as e:
                # A corrupt/unsupported image is permanent; retrying the inbox row won't fix it.
                log.warning("couldn't convert photo %s for SMS: %s", url, e)
                return Rendered(f"{name} sent a photo" + (f": {caption}" if caption else ""))
            link = await self.media.publish(jpeg, "image/jpeg", self.cfg.media_ttl_hours * 3600)
            text = f"{prefix}: {quote}{caption}" if caption else f"{prefix}:"
            off = f"{name} sent a photo" + (f": {caption}" if caption else "")
            return Rendered(text, link, off)
        if msgtype in ("m.video", "m.audio", "m.file") and not edited:
            what = {"m.video": "a video", "m.audio": "audio", "m.file": "a file"}[msgtype]
            url = content.get("url")
            info = content.get("info") or {}
            if not url or (info.get("size") or 0) > MAX_LINK_MEDIA_BYTES:
                return Rendered(f"{name} sent {what}")
            data = await self.mx.download(url)
            link = await self.media.publish(
                data, info.get("mimetype") or "application/octet-stream",
                self.cfg.link_ttl_days * 86400, filename=content.get("filename") or body,
            )
            caption = self._caption(content)
            return Rendered(f"{prefix} sent {what}: {link}" + (f" {caption}" if caption else ""))
        if msgtype == "m.location":
            geo = content.get("geo_uri", "")
            m = re.match(r"geo:([-\d.]+),([-\d.]+)", geo)
            where = f"https://maps.google.com/?q={m.group(1)},{m.group(2)}" if m else geo
            return Rendered(f"{prefix} shared a location: {where}")
        return None

    @staticmethod
    def _caption(content: dict) -> str:
        body = (content.get("body") or "").strip()
        filename = content.get("filename")
        if not body or body == filename or (not filename and _FILENAME_RE.match(body)):
            return ""
        return body

    async def _reply_quote(self, room_id: str, event_id: str) -> str:
        try:
            parent = await self.mx.get_event(room_id, event_id)
        except Exception:
            return ""
        if not parent:
            return ""
        pname = first_name(await self.sender_name(room_id, parent.get("sender", "")))
        pc = parent.get("content") or {}
        pbody = strip_reply_fallback(pc.get("body") or "")
        if pc.get("msgtype") == "m.image":
            pbody = "photo"
        # Keep the quote in the same encoding as the format: no UCS-2 ellipsis in a GSM-7 format.
        ellipsis = "..." if is_gsm7(self.cfg.reply_quote_format) else "…"
        q = truncate(pbody, self.cfg.reply_quote_chars, ellipsis)
        return self.cfg.reply_quote_format.format(name=pname, quote=q)

    async def _handle_dm_from_matrix(self, room_id: str, ghost: str, sender: str, content: dict, event_id: str) -> None:
        number = self.number_for(ghost)
        if number is None:
            return  # they use the app now and read this chat themselves
        rows = await self.db.fetchall("SELECT * FROM members WHERE number=? AND status != 'removed'", (number,))
        member = next((Member.from_row(r) for r in rows if r["status"] == "opted_in"), None)
        if member is None:
            name = Member.from_row(rows[0]).display_name if rows else "This person"
            await self.mx.send_text(room_id, ghost, f"{name} isn't receiving texts right now.", notice=True)
            return
        body = (content.get("body") or "").strip()
        if body.startswith("!"):
            return
        rendered = await self.render_for_sms(member.room_id, sender, content, dm=True)
        if rendered is None:
            return
        text, media = rendered.text, rendered.media_url
        if media and not member.photos_on:
            text, media = rendered.photo_off_text or text, None
        if member.last_dm_from != sender:
            text += " (reply: #r msg)"
        await self.db.enqueue(
            source_key=f"mx:{event_id}", room_id=member.room_id, from_number=self.number_by_room[member.room_id],
            to_number=member.number, kind="dm", text=text, media_url=media, carrier=member.carrier,
        )
        await self.db.set_member_field(member.room_id, member.number, "last_dm_from", sender)
        self.queue.notify()

    # ---- spend ----------------------------------------------------------------------

    async def estimate_today_spend(self) -> float:
        p = self.cfg.pricing
        now = time.gmtime()
        day_start = calendar.timegm((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0))
        t = await self.db.day_traffic(day_start)
        return (
            t["sms_seg"] * (p.sms_part + p.sms_carrier_fee)
            + t["mms"] * (p.mms_out + p.mms_carrier_fee)
            + t["sms_in_seg"] * p.sms_part
            + t["mms_in"] * p.mms_in
        )

    async def check_spend(self) -> None:
        """Alert once per UTC day when estimated spend crosses the configured fraction of the limit (D8)."""
        p = self.cfg.pricing
        if p.daily_spend_limit <= 0:
            return
        est = await self.estimate_today_spend()
        threshold = p.daily_spend_limit * p.daily_spend_alert_fraction
        if est < threshold:
            return
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (f"spend_alert:{day_key()}", f"{est:.2f}")
        )
        if cur.rowcount == 0:
            return  # already alerted today (survives restarts)
        pct = round(100 * est / p.daily_spend_limit)
        await self.alert(
            f"spend:{day_key()}",
            f"Estimated Telnyx spend today is ${est:.2f} ({pct}% of the ${p.daily_spend_limit:.2f} daily limit). "
            "Telnyx stops sending at the limit.",
        )

    # ---- ! commands -----------------------------------------------------------------

    async def _is_admin(self, room_id: str, mxid: str) -> bool:
        if mxid in self.cfg.admins:
            return True
        try:
            return await self.mx.power_level(room_id, mxid) >= self.cfg.admin_power_level
        except Exception:
            log.exception("power level lookup failed")
            return False

    async def _handle_bang_command(self, cmd_room: str, sender: str, body: str, *, in_group: bool) -> None:
        group = cmd_room if in_group else self.room_by_number[self.cfg.rooms[0].number]
        parts = body.split(None, 2)
        head = parts[0].lower()
        is_admin = await self._is_admin(group, sender)

        async def say(text: str) -> None:
            await self.mx.send_text(cmd_room, self.mx.bot_mxid, text, notice=True)

        if head == "!help":
            lines = ["!dm Name - open an unencrypted chat with an SMS member"]
            if self.cfg.members_can_add and not is_admin:
                lines += ["!sms add +1XXXXXXXXXX Full Name - add someone by text",
                          "!sms remove +1XXXXXXXXXX - remove someone you added"]
            if is_admin:
                lines += [
                    "!sms add +1XXXXXXXXXX Full Name - allow a number",
                    "!sms remove +1XXXXXXXXXX - remove a number",
                    "!sms list - allowlist",
                    "!sms status - members, failures, usage and cost",
                    "!announce text - pinned announcement, texted to everyone; add | yes | no for an RSVP poll",
                    "!invite [who it's for] - one-time app sign-up link (send this to me privately)",
                ]
            await say("\n".join(lines))
            return
        if head == "!dm":
            await self._cmd_dm(group, cmd_room, sender, body[3:].strip(), say)
            return
        if head not in ("!sms", "!announce", "!invite"):
            return
        sub = parts[1].lower() if len(parts) > 1 else ""
        member_ok = head == "!sms" and sub in ("add", "remove") and await self.in_group(sender)
        if not is_admin and not member_ok:
            await say("Admins only.")
            return
        if head == "!invite":
            await self._cmd_invite(cmd_room, sender, body[len("!invite"):].strip(), say, in_group=in_group)
            return
        if head == "!announce":
            await self._cmd_announce(group, sender, body[len("!announce"):].strip(), say)
            return
        arg = parts[2] if len(parts) > 2 else ""
        if sub == "add":
            await self._cmd_add(group, sender, arg, say)
        elif sub == "remove":
            await self._cmd_remove(group, sender, arg, say)
        elif sub == "list":
            await self._cmd_list(group, say)
        elif sub == "status":
            await say(await self.status_text(group))
        else:
            await say("Usage: !sms add|remove|list|status")

    async def _cmd_invite(self, cmd_room: str, sender: str, note: str, say, *, in_group: bool) -> None:
        # Anyone who sees the link can use it: only post one in the admin room or a 1:1 chat with the bot.
        private = cmd_room == self.cfg.alerts_room or (
            not in_group and not set(await self.mx.joined_members(cmd_room)) - {self.mx.bot_mxid, sender})
        if not private:
            await say("Send !invite to me in a private chat or the admin room, so the link isn't shown to everyone.")
            return
        if self.create_invite is None:
            await say("Invite links need the login service (login.service_url in config.yaml).")
            return
        try:
            inv = await self.create_invite(sender, note or None)
        except Exception as e:
            log.error("creating invite failed: %s", e)
            await say("Couldn't create an invite right now. Try again in a minute.")
            return
        expires = time.strftime("%b %-d", time.gmtime(inv["expires_at"]))
        who = f" for {note}" if note else ""
        await say(f"One-time sign-up link{who}:\n{inv['url']}\n"
                  f"It works once and expires {expires}. Send it to them directly.")

    async def _cmd_add(self, room_id: str, sender: str, arg: str, say) -> None:
        bits = arg.split(None, 1)
        number = normalize_number(bits[0]) if bits else None
        if not number or len(bits) < 2:
            await say("Usage: !sms add +1XXXXXXXXXX Full Name")
            return
        await say((await self.add_sms_member(room_id, sender, number, bits[1]))["message"])

    async def _cmd_remove(self, room_id: str, sender: str, arg: str, say) -> None:
        number = normalize_number(arg.strip())
        if not number:
            await say("Usage: !sms remove +1XXXXXXXXXX")
            return
        await say((await self.remove_sms_member(room_id, sender, number))["message"])

    # ---- adding and removing SMS members (D14) --------------------------------------

    async def add_sms_member(self, room_id: str, by: str, number: str, name: str) -> dict:
        """Allow ``number`` in ``room_id`` on behalf of ``by`` (an admin, or any member when
        matrix.members_can_add). Used by !sms add and the app. Returns {"ok", "message", ...}."""
        name = " ".join(name.split())[: commands.NAME_MAX_LEN]
        if not name:
            return {"ok": False, "error": "bad_name", "message": "Enter their name."}
        is_admin = await self._is_admin(room_id, by)
        if not is_admin:
            if not self.cfg.members_can_add:
                return {"ok": False, "error": "forbidden", "message": "Only admins can add people by text."}
            if await self.db.count_adds(by, time.time() - 86400) >= self.cfg.adds_per_day:
                return {"ok": False, "error": "rate_limited",
                        "message": "You've added a lot of people today. Try again tomorrow."}
        existing = await self.db.get_member(room_id, number)
        if existing and existing.status == "app":
            return {"ok": False, "error": "uses_app", "message": f"{existing.display_name} already uses the app."}
        new = existing is None or existing.status == "removed"
        if not self.cfg.sms_accounts:
            mxid = self.mx.ghost_mxid(number)
        elif existing and not new:
            mxid = existing.mxid
        else:
            if self.sms_account is None:
                return {"ok": False, "error": "unavailable", "message": "The login service isn't connected."}
            acct = await self.sms_account(name, number)
            if acct.get("app_user"):
                return {"ok": False, "error": "uses_app", "message": "That number already has an account in the app."}
            mxid = acct["mxid"]
        m = await self.db.upsert_member(room_id, number, mxid, name, by)
        await self.ensure_profile(m, force=True)
        await self.db.log_consent(number, "allowlisted", "admin" if is_admin else "member", room_id=room_id,
                                  detail=f"by {by}")
        await self.refresh_sms()
        group_number = self.number_by_room[room_id]
        out = {"ok": True, "mxid": mxid, "status": m.status, "invite_sent": False}
        if m.status == "opted_in":
            return {**out, "message": f"Updated {name} ({number}); already opted in."}
        if new and self.cfg.invite_text and not m.ever_joined and not await self.db.ever_opted_out(number):
            adder = first_name(await self.sender_name(room_id, by))
            text = self.cfg.invite_text.format(brand=self.cfg.brand, adder=adder, number=group_number)
            await self.db.enqueue(source_key=f"invite:{room_id}:{time.time():.0f}", room_id=room_id,
                                  from_number=group_number, to_number=number, kind="invite", text=text)
            self.queue.notify()
            return {**out, "invite_sent": True,
                    "message": f"Added {name} ({number}). They got a text asking them to reply JOIN."}
        return {**out, "message": f"Added {name} ({number}). Ask them to text JOIN to {group_number}. "
                                  "Nothing is sent to them until they do."}

    async def remove_sms_member(self, room_id: str, by: str, number: str) -> dict:
        """Admins can remove anyone; members only people they added (D14). Nothing is texted to them."""
        m = await self.db.get_member(room_id, number)
        if m is None or m.status == "removed":
            return {"ok": False, "error": "not_found", "message": "That number isn't on the list."}
        if not (await self._is_admin(room_id, by) or m.added_by == by):
            return {"ok": False, "error": "forbidden",
                    "message": "Only an admin or the person who added them can remove them."}
        if m.status == "app":
            return {"ok": False, "error": "uses_app",
                    "message": f"{m.display_name} uses the app now. Remove them from the room instead."}
        await self.db.set_member_status(room_id, m.number, "removed")
        await self.db.skip_pending_for(m.number, "removed")
        await self.db.log_consent(m.number, "removed", "admin", room_id=room_id, detail=f"by {by}")
        await self.mx.leave(room_id, m.mxid)
        await self.refresh_sms()
        if self.cfg.sms_accounts and self.drop_sms_account and not any(
                r["status"] != "removed"
                for r in await self.db.fetchall("SELECT status FROM members WHERE number=?", (m.number,))):
            try:
                await self.drop_sms_account(m.mxid)
            except Exception:
                log.exception("couldn't retire the account of removed member %s", m.mxid)
        return {"ok": True, "message": f"Removed {m.display_name} ({m.number}). No text was sent to them."}

    async def sms_members_api(self, action: str, body: dict) -> dict:
        """Internal API for the app (via the login service): list | add | remove for body["by"]."""
        by = str(body["by"])
        room_id = str(body.get("room_id") or "") or self.room_by_number[self.cfg.rooms[0].number]
        if room_id not in self.number_by_room:
            return {"ok": False, "error": "not_found", "message": "That room isn't bridged to text."}
        if by not in await self.mx.joined_members(room_id):
            return {"ok": False, "error": "forbidden", "message": "Only group members can do that."}
        if action == "list":
            return {"ok": True, "room_id": room_id, "members": await self.list_sms_members(room_id, by)}
        number = normalize_number(str(body.get("number") or ""))
        if number is None:
            return {"ok": False, "error": "bad_phone", "message": "That doesn't look like a phone number."}
        if action == "add":
            return await self.add_sms_member(room_id, by, number, str(body.get("name") or ""))
        return await self.remove_sms_member(room_id, by, number)

    async def list_sms_members(self, room_id: str, by: str) -> list[dict]:
        """For the app's member screen: everyone on the SMS list, and whether ``by`` may remove them."""
        is_admin = await self._is_admin(room_id, by)
        out = []
        for m in await self.db.members(room_id):
            if m.status not in SMS_STATUSES:
                continue
            mine = is_admin or m.added_by == by
            # Numbers stay private, as in the group itself: only admins and whoever added someone see it.
            out.append({"mxid": m.mxid, "name": m.display_name, "status": m.status, "can_remove": mine,
                        **({"number": m.number} if mine else {})})
        return out

    async def _cmd_list(self, room_id: str, say) -> None:
        ms = await self.db.members(room_id)
        if not ms:
            await say("No SMS members yet. Add one with !sms add +1XXXXXXXXXX Full Name")
            return
        lines = [f"{m.display_name} {m.number} - {m.status}" + ("" if m.photos_on else ", photos off") for m in ms]
        await say("\n".join(lines))

    async def status_text(self, room_id: str) -> str:
        ms = await self.db.members(room_id)
        by: dict[str, list[Member]] = {}
        for m in ms:
            by.setdefault(m.status, []).append(m)
        lines = [
            "Members: " + ", ".join(f"{len(v)} {k}" for k, v in sorted(by.items())) if by else "Members: none",
        ]
        off = [m.display_name for m in ms if not m.photos_on and m.status == "opted_in"]
        if off:
            lines.append("Photos off: " + ", ".join(off))
        failing = [m for m in ms if m.consecutive_failures > 0]
        if failing:
            lines.append("Failing: " + ", ".join(f"{m.display_name} ({m.consecutive_failures})" for m in failing))
        q = await self.db.fetchone("SELECT COUNT(*) AS n FROM outbound WHERE status='pending'")
        f = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM outbound WHERE status IN ('failed','unknown','expired') AND created_at > ?",
            (time.time() - 30 * 86400,),
        )
        lines.append(f"Queue: {q['n']} pending; {f['n']} failed/unknown/expired in 30 days")
        u = await self.db.month_usage()
        p = self.cfg.pricing
        so, mo, si, mi = (u.get(k, (0, 0)) for k in ("sms_out", "mms_out", "sms_in", "mms_in"))
        cost = (
            so[1] * (p.sms_part + p.sms_carrier_fee)
            + mo[0] * (p.mms_out + p.mms_carrier_fee)
            + si[1] * p.sms_part
            + mi[0] * p.mms_in
        )
        today = await self.estimate_today_spend()
        if p.daily_spend_limit > 0:
            lines.append(f"Today (UTC): est. ${today:.2f} of ${p.daily_spend_limit:.2f} daily limit")
        lines.append(
            f"This month: SMS out {so[0]} ({so[1]} seg), MMS out {mo[0]}, SMS in {si[0]}, MMS in {mi[0]}. "
            f"Est. cost ${cost:.2f} (estimate from configured rates)"
        )
        return "\n".join(lines)

    async def _cmd_dm(self, group: str, cmd_room: str, sender: str, query: str, say) -> None:
        if not query:
            await say("Usage: !dm Name")
            return
        if not await self.in_group(sender):
            await say("Only group members can do that.")
            return
        cands = [c for c in await self.candidates(group) if self.is_ghost(c.key)]
        found = names.exact_matches(query.split(), cands)
        if len(found) > 1:
            await say("Which one? " + ", ".join(c.name for c in found))
            return
        if not found:
            await say(f"No SMS member matches {query} exactly. Use their full name, unique first name or WHO number.")
            return
        target = found[0]
        ghost = target.key
        room = await self.db.get_dm_room(ghost, sender)
        if room is None:
            room = await self.mx.create_dm(ghost, sender)
            await self.db.set_dm_room(ghost, sender, room)
        await say(f"Opened a chat with {target.name}. Check your invites.")

    async def _cmd_announce(self, room_id: str, sender: str, arg: str, say) -> None:
        if not arg:
            await say("Usage: !announce text, optionally followed by | option | option")
            return
        bits = [b.strip() for b in arg.split("|")]
        text, options = bits[0], [b for b in bits[1:] if b]
        if len(options) == 1:
            await say("A poll needs at least two options.")
            return
        name = first_name(await self.sender_name(room_id, sender))
        body = f"@room ANNOUNCEMENT from {name}: {text}"
        html_body = f"@room <b>ANNOUNCEMENT from {html.escape(name)}:</b> {html.escape(text)}"
        evt = await self.mx.send_text(room_id, self.mx.bot_mxid, body, html=html_body, mention_room=True)
        try:
            await self.mx.pin(room_id, evt)
        except Exception:
            log.exception("pin failed")
            await say("Posted, but I couldn't pin it (check my power level).")
        sms = f"ANNOUNCEMENT from {name}: {text}"
        if options:
            answers = [(f"a{i + 1}", o) for i, o in enumerate(options)]
            poll_id = await self.mx.send_poll_start(room_id, self.mx.bot_mxid, text, answers, self.cfg.poll_style)
            await self.db.add_poll(poll_id, room_id, self.mx.bot_mxid, text, answers, self.cfg.poll_style, 1)
            sms += " Reply " + ", ".join(f"#vote {i + 1} ({truncate(o, 30)})" for i, o in enumerate(options)) + "."
        await self._fan_out(room_id, self.mx.bot_mxid, Rendered(sms), f"mx:{evt}")
