from __future__ import annotations

import base64
import itertools
import json
import time

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from smsgw.bridge import Bridge
from smsgw.config import CarrierClass, Config, Pricing, RoomConfig
from smsgw.db import Database
from smsgw.media import MediaStore
from smsgw.outbound import OutboundQueue
from smsgw.provider.base import SendError, SendResult
from smsgw.provider.telnyx import TelnyxProvider

ROOM = "!group:example.org"
GROUP_NUMBER = "+15550000000"
DOMAIN = "example.org"


def make_config(tmp_path) -> Config:
    return Config(
        homeserver_url="http://hs", domain=DOMAIN, as_id="sms", as_token="as", hs_token="hs",
        bot_localpart="smsbot", bot_displayname="SMS Bridge", ghost_prefix="sms_", ghost_name_suffix=" (SMS)",
        appservice_host="127.0.0.1", appservice_port=0, public_base_url="https://hook.example.org",
        public_host="127.0.0.1", public_port=0, database=str(tmp_path / "t.db"), media_dir=str(tmp_path / "media"),
        media_ttl_hours=24, link_ttl_days=7, telnyx_api_key="KEY", telnyx_public_key="", telnyx_messaging_profile_id=None,
        webhook_tolerance_seconds=300, brand="Example Chat", contact_email="help@example.org",
        rooms=[RoomConfig(ROOM, GROUP_NUMBER)], admin_power_level=50, admins=[], alerts_room="!alerts:example.org",
        global_per_second=1000, unknown_carrier_class="unknown",
        carrier_classes=[
            CarrierClass("att", ("at&t", "att"), per_minute=15),
            CarrierClass("tmobile", ("t-mobile",), daily_segments=1000),
            CarrierClass("unknown", (), per_minute=15),
        ],
        max_attempts=3, max_queue_age_hours=12, failure_alert_threshold=3, pricing=Pricing(),
        poll_style="unstable", reply_quote_format='Re {name}: "{quote}" ', reply_quote_chars=20,
        image_max_px=1200, image_max_bytes=600_000, inbound_media_max_bytes=5_000_000,
    )


class FakePort:
    bot_mxid = f"@smsbot:{DOMAIN}"

    def __init__(self) -> None:
        self.rooms: dict[str, dict[str, str]] = {ROOM: {self.bot_mxid: "SMS Bridge"}}
        self.sent: list[dict] = []
        self.left: list[tuple[str, str]] = []
        self.displaynames: dict[str, str] = {}
        self.power: dict[str, int] = {}
        self.events: dict[str, dict] = {}
        self.media: dict[str, bytes] = {}
        self._ids = itertools.count(1)
        self.pinned: list[str] = []

    def ghost_mxid(self, number: str) -> str:
        return f"@sms_{number.lstrip('+')}:{DOMAIN}"

    def number_for_ghost(self, mxid: str) -> str | None:
        if mxid.startswith("@sms_") and mxid.endswith(f":{DOMAIN}"):
            return "+" + mxid[5:].split(":")[0]
        return None

    async def resolve_room(self, room: str) -> str:
        return room

    async def ensure_ghost(self, number: str, displayname: str) -> None:
        self.displaynames[self.ghost_mxid(number)] = displayname

    async def set_displayname(self, mxid: str, name: str) -> None:
        self.displaynames[mxid] = name
        for members in self.rooms.values():
            if mxid in members:
                members[mxid] = name

    async def join(self, room_id: str, mxid: str) -> None:
        self.rooms.setdefault(room_id, {})[mxid] = self.displaynames.get(mxid, mxid[1:].split(":")[0])

    async def leave(self, room_id: str, mxid: str) -> None:
        self.rooms.get(room_id, {}).pop(mxid, None)
        self.left.append((room_id, mxid))

    def _new_event(self, room_id, sender, etype, content) -> str:
        eid = f"$e{next(self._ids)}"
        evt = {"event_id": eid, "room_id": room_id, "sender": sender, "type": etype, "content": content}
        self.events[eid] = evt
        self.sent.append(evt)
        return eid

    async def send_text(self, room_id, sender, body, *, html=None, mention_user_ids=None, mention_room=False,
                        notice=False, txn_id=None) -> str:
        content = {"msgtype": "m.notice" if notice else "m.text", "body": body}
        if html:
            content["formatted_body"] = html
        content["m.mentions"] = {"user_ids": mention_user_ids or [], "room": mention_room}
        return self._new_event(room_id, sender, "m.room.message", content)

    async def send_media(self, room_id, sender, data, mimetype, filename, *, txn_id=None) -> str:
        return self._new_event(room_id, sender, "m.room.message",
                               {"msgtype": "m.image", "body": filename, "url": "mxc://x/y", "info": {"mimetype": mimetype}})

    async def send_poll_start(self, room_id, sender, question, answers, style) -> str:
        return self._new_event(room_id, sender, "org.matrix.msc3381.poll.start", {"q": question, "answers": answers})

    async def send_poll_response(self, room_id, sender, poll_event_id, answer_id, style, *, txn_id=None) -> str:
        return self._new_event(room_id, sender, "org.matrix.msc3381.poll.response",
                               {"m.relates_to": {"rel_type": "m.reference", "event_id": poll_event_id},
                                "org.matrix.msc3381.poll.response": {"answers": [answer_id]}})

    async def joined_members(self, room_id: str) -> dict[str, str]:
        return dict(self.rooms.get(room_id, {}))

    async def display_name(self, room_id: str, mxid: str) -> str:
        return self.rooms.get(room_id, {}).get(mxid) or mxid[1:].split(":")[0]

    async def power_level(self, room_id: str, mxid: str) -> int:
        return self.power.get(mxid, 0)

    async def get_event(self, room_id: str, event_id: str) -> dict | None:
        return self.events.get(event_id)

    async def download(self, mxc: str) -> bytes:
        return self.media[mxc]

    async def create_dm(self, creator: str, invitee: str) -> str:
        rid = f"!dm{next(self._ids)}:{DOMAIN}"
        self.rooms[rid] = {creator: creator, invitee: invitee}
        return rid

    async def pin(self, room_id: str, event_id: str) -> None:
        self.pinned.append(event_id)


class FakeProvider:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail_with: list[SendError] = []
        self.blocked: set[str] = set()
        self.carriers: dict[str, str] = {}
        self._ids = itertools.count(1)

    async def send(self, *, from_number, to_number, text, media_url=None) -> SendResult:
        if self.fail_with:
            raise self.fail_with.pop(0)
        if to_number in self.blocked:
            raise SendError("blocked", "40300 Blocked due to STOP message", "40300")
        self.sent.append({"from": from_number, "to": to_number, "text": text, "media_url": media_url})
        return SendResult(f"msg-{next(self._ids)}", 1, self.carriers.get(to_number))

    def verify(self, body, headers) -> None:
        pass

    def parse(self, body: bytes):
        return TelnyxProvider.parse(None, body)  # parse doesn't use self

    async def fetch_media(self, url, max_bytes):
        return b"\x89PNG fake", "image/png"

    async def close(self) -> None:
        pass


def inbound_event(frm: str, text: str, *, to: str = GROUP_NUMBER, autoresponse: str | None = None,
                  msg_id: str | None = None, media: list | None = None) -> bytes:
    payload = {
        "id": msg_id or f"in-{time.time_ns()}",
        "from": {"phone_number": frm, "carrier": "T-Mobile USA"},
        "to": [{"phone_number": to}],
        "text": text,
        "media": media or [],
    }
    if autoresponse:
        payload["autoresponse_type"] = autoresponse
    return json.dumps({"data": {"event_type": "message.received", "id": f"evt-{payload['id']}", "payload": payload}}).encode()


@pytest_asyncio.fixture
async def env(tmp_path):
    cfg = make_config(tmp_path)
    db = Database(cfg.database)
    await db.open()
    port = FakePort()
    provider = FakeProvider()
    queue = OutboundQueue(cfg, db, provider)
    media = MediaStore(db, cfg.media_dir, cfg.public_base_url)
    bridge = Bridge(cfg, db, port, provider, queue, media)
    await bridge.start()

    class Env:
        pass

    e = Env()
    e.cfg, e.db, e.port, e.provider, e.queue, e.bridge = cfg, db, port, provider, queue, bridge

    async def sms(frm, text, **kw):
        evt = provider.parse(inbound_event(frm, text, **kw))
        await bridge.handle_provider_event(evt)

    async def drain():
        while await queue.step():
            pass

    async def matrix(evt):
        evt.setdefault("event_id", f"$m{time.time_ns()}")
        evt.setdefault("room_id", ROOM)
        await bridge.handle_matrix_event(evt)

    echoed: set[str] = set()

    async def echo():
        """Replay events the bridge sent, like Synapse's appservice transactions would."""
        for evt in list(port.sent):
            if evt["event_id"] not in echoed:
                echoed.add(evt["event_id"])
                await bridge.handle_matrix_event(dict(evt))

    e.sms, e.drain, e.matrix, e.echo = sms, drain, matrix, echo
    yield e
    await db.close()


@pytest.fixture
def keypair():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv, base64.b64encode(pub).decode()
