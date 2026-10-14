"""Signed Telnyx webhook fixtures against the real public HTTP app."""

import base64
import time

from aiohttp.test_utils import TestClient, TestServer

from smsgw.provider.telnyx import TelnyxProvider
from smsgw.web import build_app
from tests.conftest import inbound_event


def sign(priv, body: bytes) -> dict:
    ts = str(int(time.time()))
    return {
        "telnyx-signature-ed25519": base64.b64encode(priv.sign(ts.encode() + b"|" + body)).decode(),
        "telnyx-timestamp": ts,
        "Content-Type": "application/json",
    }


async def test_signed_webhook_is_persisted_once(env, keypair):
    priv, pub = keypair
    provider = TelnyxProvider("k", pub)
    woke = []
    app = build_app(env.db, provider, lambda: woke.append(1))
    async with TestClient(TestServer(app)) as client:
        body = inbound_event("+15555550001", "JOIN", msg_id="abc")
        r1 = await client.post("/telnyx/webhook", data=body, headers=sign(priv, body))
        r2 = await client.post("/telnyx/webhook", data=body, headers=sign(priv, body))  # Telnyx retry
        assert r1.status == 200 and r2.status == 200
        rows = await env.db.pending_inbound()
        assert len(rows) == 1 and rows[0]["key"] == "message.received:abc"
        assert woke == [1]

        bad = await client.post("/telnyx/webhook", data=body, headers={"Content-Type": "application/json"})
        assert bad.status == 401
        forged = dict(sign(priv, b"other"))
        assert (await client.post("/telnyx/webhook", data=body, headers=forged)).status == 401

        # Inbox processing runs the full flow (not allowlisted -> polite refusal).
        await env.bridge._process_inbound_row(rows[0])
        await env.drain()
        assert env.provider.sent[-1]["text"] == "Ask the group admin to add your number."
    await provider.close()


async def test_media_route_and_expiry(env, keypair):
    _, pub = keypair
    provider = TelnyxProvider("k", pub)
    url = await env.bridge.media.publish(b"\xff\xd8jpeg", "image/jpeg", 60)
    path = url.split("hook.example.org", 1)[1]
    app = build_app(env.db, provider, lambda: None)
    async with TestClient(TestServer(app)) as client:
        r = await client.get(path)
        assert r.status == 200 and await r.read() == b"\xff\xd8jpeg"
        assert (await client.get("/m/not-a-token.jpg")).status == 404
        await env.db.execute("UPDATE media_tokens SET expires_at=0")
        assert (await client.get(path)).status == 404
        assert await env.bridge.media.cleanup() == 1
    await provider.close()
