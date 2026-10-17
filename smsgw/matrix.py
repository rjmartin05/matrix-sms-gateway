"""mautrix-python implementation of MatrixPort, plus the appservice event intake."""

from __future__ import annotations

import io
import logging
import re
from typing import Awaitable, Callable

from mautrix.appservice import AppService, IntentAPI
from mautrix.appservice.state_store import ASStateStore
from mautrix.client import ClientAPI
from mautrix.client.state_store.memory import MemoryStateStore
from mautrix.errors import MForbidden, MNotFound
from mautrix.types import (
    Event,
    EventID,
    EventType,
    Membership,
    RoomAlias,
    RoomCreatePreset,
    RoomID,
    UserID,
)

from .config import Config

log = logging.getLogger("smsgw.matrix")


class StateStore(ASStateStore, MemoryStateStore):
    def __init__(self) -> None:
        ASStateStore.__init__(self)
        MemoryStateStore.__init__(self)


def _image_size(data: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


class MautrixPort:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.az = AppService(
            server=cfg.homeserver_url,
            domain=cfg.domain,
            as_token=cfg.as_token,
            hs_token=cfg.hs_token,
            bot_localpart=cfg.bot_localpart,
            id=cfg.as_id,
            state_store=StateStore(),
            bridge_name="sms",
        )
        self.bot_mxid = str(self.az.bot_mxid)
        self._ghost_re = re.compile(
            rf"^@{re.escape(cfg.ghost_prefix)}(\d{{8,15}}):{re.escape(cfg.domain)}$"
        )

    # ---- lifecycle ------------------------------------------------------------------

    async def start(self, on_event: Callable[[dict], Awaitable[None]]) -> None:
        async def handler(evt: Event) -> None:
            await on_event(evt.serialize())

        self.az.matrix_event_handler(handler)
        await self.az.start(self.cfg.appservice_host, self.cfg.appservice_port)
        bot = self.az.intent
        await bot.ensure_registered()
        try:
            await bot.set_displayname(self.cfg.bot_displayname)
        except Exception:
            log.warning("could not set bot displayname", exc_info=True)

    async def stop(self) -> None:
        await self.az.stop()

    # ---- identity -------------------------------------------------------------------

    def intent(self, mxid: str) -> IntentAPI:
        if mxid == self.bot_mxid:
            return self.az.intent
        return self.az.intent.user(UserID(mxid))

    def ghost_mxid(self, number: str) -> str:
        return f"@{self.cfg.ghost_prefix}{number.lstrip('+')}:{self.cfg.domain}"

    def number_for_ghost(self, mxid: str) -> str | None:
        m = self._ghost_re.match(mxid or "")
        return f"+{m.group(1)}" if m else None

    # ---- MatrixPort -----------------------------------------------------------------

    async def resolve_room(self, room: str) -> str:
        if room.startswith("!"):
            return room
        info = await self.az.intent.resolve_room_alias(RoomAlias(room))
        return str(info.room_id)

    async def ensure_ghost(self, number: str, displayname: str) -> None:
        intent = self.intent(self.ghost_mxid(number))
        await intent.ensure_registered()
        await intent.set_displayname(displayname)

    async def set_displayname(self, mxid: str, name: str) -> None:
        intent = self.intent(mxid)
        await intent.ensure_registered()
        await intent.set_displayname(name)

    async def join(self, room_id: str, mxid: str) -> None:
        intent = self.intent(mxid)
        if mxid == self.bot_mxid:
            # No retries: mautrix's default (4, ~100 s total) stalls startup when the bot isn't
            # invited yet. Bridge.start() logs and continues; the bot joins on invite instead.
            await intent.join_room(RoomID(room_id), max_retries=0)
            return
        await intent.ensure_registered()
        await intent.ensure_joined(RoomID(room_id))

    async def leave(self, room_id: str, mxid: str) -> None:
        try:
            await self.intent(mxid).leave_room(RoomID(room_id))
        except (MForbidden, MNotFound):
            pass

    async def _send(self, room_id: str, sender: str, etype: str, content: dict, txn_id: str | None) -> str:
        intent = self.intent(sender)
        event_type = EventType.find(etype, EventType.Class.MESSAGE)
        return str(await intent.send_message_event(RoomID(room_id), event_type, content, txn_id=txn_id))

    async def send_text(
        self,
        room_id: str,
        sender: str,
        body: str,
        *,
        html: str | None = None,
        mention_user_ids: list[str] | None = None,
        mention_room: bool = False,
        notice: bool = False,
        txn_id: str | None = None,
    ) -> str:
        content: dict = {"msgtype": "m.notice" if notice else "m.text", "body": body}
        if html:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = html
        mentions: dict = {}
        if mention_user_ids:
            mentions["user_ids"] = mention_user_ids
        if mention_room:
            mentions["room"] = True
        content["m.mentions"] = mentions
        return await self._send(room_id, sender, "m.room.message", content, txn_id)

    async def send_reaction(self, room_id: str, sender: str, event_id: str, key: str, *, txn_id: str | None = None) -> str:
        content = {"m.relates_to": {"rel_type": "m.annotation", "event_id": event_id, "key": key}}
        return await self._send(room_id, sender, "m.reaction", content, txn_id)

    async def redact(self, room_id: str, sender: str, event_id: str) -> None:
        # ClientAPI.redact directly: IntentAPI.redact pre-checks the redact power level, which a
        # ghost redacting its own reaction doesn't need.
        await ClientAPI.redact(self.intent(sender), RoomID(room_id), EventID(event_id))

    async def send_media(
        self, room_id: str, sender: str, data: bytes, mimetype: str, filename: str, *, txn_id: str | None = None
    ) -> str:
        intent = self.intent(sender)
        mxc = await intent.upload_media(data, mime_type=mimetype, filename=filename)
        info: dict = {"mimetype": mimetype, "size": len(data)}
        if mimetype.startswith("image/"):
            msgtype = "m.image"
            size = _image_size(data)
            if size:
                info["w"], info["h"] = size
        elif mimetype.startswith("video/"):
            msgtype = "m.video"
        elif mimetype.startswith("audio/"):
            msgtype = "m.audio"
        else:
            msgtype = "m.file"
        content = {"msgtype": msgtype, "body": filename, "filename": filename, "url": str(mxc), "info": info,
                   "m.mentions": {}}
        return await self._send(room_id, sender, "m.room.message", content, txn_id)

    async def send_poll_start(
        self, room_id: str, sender: str, question: str, answers: list[tuple[str, str]], style: str
    ) -> str:
        fallback = question + "\n" + "\n".join(f"{i + 1}. {t}" for i, (_, t) in enumerate(answers))
        if style == "stable":
            content = {
                "m.poll": {
                    "question": {"m.text": [{"body": question}]},
                    "kind": "m.disclosed",
                    "max_selections": 1,
                    "answers": [{"m.id": a, "m.text": [{"body": t}]} for a, t in answers],
                },
                "m.text": [{"body": fallback}],
            }
            etype = "m.poll.start"
        else:
            content = {
                "org.matrix.msc3381.poll.start": {
                    "question": {"org.matrix.msc1767.text": question},
                    "kind": "org.matrix.msc3381.poll.disclosed",
                    "max_selections": 1,
                    "answers": [{"id": a, "org.matrix.msc1767.text": t} for a, t in answers],
                },
                "org.matrix.msc1767.text": fallback,
            }
            etype = "org.matrix.msc3381.poll.start"
        return await self._send(room_id, sender, etype, content, None)

    async def send_poll_response(
        self, room_id: str, sender: str, poll_event_id: str, answer_id: str, style: str, *, txn_id: str | None = None
    ) -> str:
        rel = {"rel_type": "m.reference", "event_id": poll_event_id}
        if style == "stable":
            content = {"m.relates_to": rel, "m.selections": [answer_id]}
            etype = "m.poll.response"
        else:
            content = {"m.relates_to": rel, "org.matrix.msc3381.poll.response": {"answers": [answer_id]}}
            etype = "org.matrix.msc3381.poll.response"
        intent = self.intent(sender)
        await intent.ensure_joined(RoomID(room_id))
        return await self._send(room_id, sender, etype, content, txn_id)

    async def joined_members(self, room_id: str) -> dict[str, str]:
        members = await self.az.intent.get_joined_members(RoomID(room_id))
        out: dict[str, str] = {}
        for mxid, m in members.items():
            out[str(mxid)] = (m.displayname if m and m.displayname else str(mxid)[1:].split(":", 1)[0])
        return out

    async def display_name(self, room_id: str, mxid: str) -> str:
        try:
            ev = await self.az.intent.get_state_event(RoomID(room_id), EventType.ROOM_MEMBER, mxid)
            if ev and getattr(ev, "membership", None) in (Membership.JOIN, Membership.INVITE) and ev.displayname:
                return ev.displayname
        except (MNotFound, MForbidden):
            pass
        return mxid[1:].split(":", 1)[0]

    async def power_level(self, room_id: str, mxid: str) -> int:
        # Room v12+: creators have implicit unlimited power and aren't listed in m.room.power_levels,
        # so the create event must be passed in or the creator reads as users_default (0).
        state = await self.az.intent.get_state(RoomID(room_id))
        create = next((e for e in state if e.type == EventType.ROOM_CREATE), None)
        pl = next((e.content for e in state if e.type == EventType.ROOM_POWER_LEVELS), None)
        if pl is None:
            return 100 if create is not None and create.sender == mxid else 0
        return pl.get_user_level(UserID(mxid), create)

    async def get_event(self, room_id: str, event_id: str) -> dict | None:
        ev = await self.az.intent.get_event(RoomID(room_id), event_id)
        return ev.serialize() if ev else None

    async def download(self, mxc: str) -> bytes:
        return await self.az.intent.download_media(mxc)

    async def create_dm(self, creator: str, invitee: str) -> str:
        intent = self.intent(creator)
        await intent.ensure_registered()
        # No m.room.encryption in initial_state: the bridge can't read E2EE.
        room_id = await intent.create_room(
            preset=RoomCreatePreset.TRUSTED_PRIVATE,
            is_direct=True,
            invitees=[UserID(invitee)],
        )
        return str(room_id)

    async def pin(self, room_id: str, event_id: str) -> None:
        bot = self.az.intent
        try:
            pinned = await bot.get_pinned_messages(RoomID(room_id))
        except MNotFound:
            pinned = []
        await bot.set_pinned_messages(RoomID(room_id), [*pinned, event_id])
