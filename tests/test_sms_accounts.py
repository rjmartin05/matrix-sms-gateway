"""D14: SMS members as real accounts, added by any group member, with an invite text."""

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from smsgw.bridge import SMS_MEMBERS_EVENT
from smsgw.login.service import ServiceError
from smsgw.web import build_internal_app as build_bridge_internal_app
from tests.conftest import ROOM
from tests.test_flows import ADMIN, ALICE, DAVE, SUE, consent, texts_to
from tests.test_login import SECRET, sms_code_of, svc  # noqa: F401  (svc is a fixture)

INVITE = "{brand}: {adder} added you to the group text. Reply JOIN to get group messages, STOP to opt out."
BOB = "@bob:example.org"


class LoopBridge:
    """The login service's view of the bridge, wired straight to the real Bridge."""

    def __init__(self, bridge) -> None:
        self.b = bridge

    async def app_user(self, mxid, number):
        await self.b.app_user(mxid, number)

    async def sms_members(self, action, body):
        return await self.b.sms_members_api(action, body)


@pytest_asyncio.fixture
async def acc(env, svc):  # noqa: F811
    env.cfg.sms_accounts = True
    env.cfg.invite_text = INVITE
    env.bridge.sms_account = svc.sms_account
    env.bridge.drop_sms_account = svc.drop_sms_account
    svc.bridge = LoopBridge(env.bridge)
    env.port.rooms[ROOM].update({ALICE: "Alice Smith", BOB: "Bob", ADMIN: "Evan"})
    env.port.power[ADMIN] = 100
    env.svc = svc
    return env


async def app_add(env, by, phone, name, room_id=ROOM):
    tok = f"tok-{by}"
    env.svc.matrix.tokens[tok] = by
    return await env.svc.sms_members(tok, "add", {"room_id": room_id, "phone": phone, "display_name": name})


async def app_remove(env, by, phone):
    tok = f"tok-{by}"
    env.svc.matrix.tokens[tok] = by
    return await env.svc.sms_members(tok, "remove", {"room_id": ROOM, "phone": phone})


async def test_member_adds_someone_by_text(acc):
    res = await app_add(acc, ALICE, "(555) 555-0001", "Dave Miller")
    assert res["ok"] and res["invite_sent"] and res["mxid"] == "@dave.miller:example.org"
    await acc.drain()
    assert texts_to(acc, DAVE) == [
        "Example Chat: Alice added you to the group text. Reply JOIN to get group messages, STOP to opt out."]
    assert ("allowlisted", "member") in await consent(acc, DAVE)
    acct = await acc.svc.db.account_by("phone", DAVE)
    assert acct["mxid"] == "@dave.miller:example.org" and acct["sms_status"] == "sms"
    # A plain name: the one Dave keeps if he switches to the app.
    assert acc.port.displaynames["@dave.miller:example.org"] == "Dave Miller"

    await acc.sms(DAVE, "JOIN")
    await acc.sms(DAVE, "hi all")
    posted = [e for e in acc.port.sent if e["content"].get("body") == "hi all"]
    assert posted and posted[0]["sender"] == "@dave.miller:example.org"
    assert acc.port.state[(ROOM, SMS_MEMBERS_EVENT, "")] == {"users": ["@dave.miller:example.org"]}

    # The invite went out once: adding again (e.g. a rename) doesn't text again.
    await acc.drain()
    acc.provider.sent.clear()
    await app_add(acc, ALICE, DAVE, "Dave M")
    await acc.drain()
    assert texts_to(acc, DAVE) == []


async def test_no_invite_text_unless_configured(acc):
    acc.cfg.invite_text = None
    res = await app_add(acc, ALICE, DAVE, "Dave Miller")
    await acc.drain()
    assert res["ok"] and not res["invite_sent"] and "Ask them to text JOIN" in res["message"]
    assert texts_to(acc, DAVE) == []


async def test_no_invite_text_to_a_number_that_opted_out(acc):
    await app_add(acc, ALICE, DAVE, "Dave Miller")
    await acc.sms(DAVE, "JOIN")
    await acc.sms(DAVE, "STOP")
    await app_remove(acc, ADMIN, DAVE)
    acc.provider.sent.clear()
    res = await app_add(acc, BOB, DAVE, "Dave Miller")
    await acc.drain()
    assert res["ok"] and not res["invite_sent"]
    assert texts_to(acc, DAVE) == []


async def test_invite_text_not_sent_once_removed(acc):
    await app_add(acc, ALICE, DAVE, "Dave Miller")
    await app_remove(acc, ALICE, DAVE)
    await acc.drain()
    assert texts_to(acc, DAVE) == []


async def test_others_cant_rename_someone_on_the_list(acc):
    await app_add(acc, ALICE, DAVE, "Dave Miller")
    with pytest.raises(ServiceError) as e:
        await app_add(acc, BOB, DAVE, "Not Dave")
    assert e.value.status == 403
    assert (await acc.db.get_member(ROOM, DAVE)).display_name == "Dave Miller"
    assert (await app_add(acc, ADMIN, DAVE, "Dave M"))["ok"]


def test_invite_text_placeholders_are_checked(tmp_path, monkeypatch):
    from smsgw.config import ConfigError, load

    for k in ("SMSGW_AS_TOKEN", "SMSGW_HS_TOKEN", "TELNYX_API_KEY", "TELNYX_PUBLIC_KEY"):
        monkeypatch.setenv(k, "x")
    base = (
        "homeserver: {address: http://hs, domain: example.org}\n"
        "public: {base_url: https://hook.example.org}\n"
        "compliance: {brand: Example, contact_email: a@b.co, invite_text: '%s'}\n"
        "rooms: [{room: '!r:example.org', number: '+15550000000'}]\n"
    )
    p = tmp_path / "c.yaml"
    p.write_text(base % "{brand}: {name} added you. Reply STOP to opt out.")
    with pytest.raises(ConfigError, match="invite_text"):
        load(str(p))
    p.write_text(base % "{brand}: {adder} added you. Reply JOIN, or STOP to opt out.")
    assert load(str(p)).invite_text.startswith("{brand}")


async def test_adder_can_remove_only_until_they_join(acc):
    await app_add(acc, ALICE, SUE, "Sue")
    await acc.sms(SUE, "JOIN")
    with pytest.raises(ServiceError) as e:
        await app_remove(acc, ALICE, SUE)  # joined: admins only
    assert e.value.status == 403 and "Only an admin" in e.value.message
    acc.svc.matrix.tokens["tok-a"] = ALICE
    listed = {m["name"]: m for m in (await acc.svc.sms_members("tok-a", "list", {"room_id": ROOM}))["members"]}
    assert not listed["Sue"]["can_remove"]
    assert (await app_remove(acc, ADMIN, SUE))["ok"]


async def test_only_admin_or_adder_can_remove(acc):
    await app_add(acc, ALICE, DAVE, "Dave Miller")
    with pytest.raises(ServiceError) as e:
        await app_remove(acc, BOB, DAVE)
    assert e.value.status == 403
    res = await app_remove(acc, ALICE, DAVE)
    assert res["ok"] and (await acc.db.get_member(ROOM, DAVE)).status == "removed"
    # A removed member who never used the app can't sign in with their number.
    assert (await acc.svc.db.account_by("phone", DAVE))["sms_status"] == "removed"
    with pytest.raises(ServiceError) as e:
        await acc.svc.start_login({"phone": DAVE}, None)
    assert e.value.code == "no_account"
    # An admin can remove anyone; re-adding reuses the same account.
    await app_add(acc, BOB, DAVE, "Dave Miller")
    assert (await acc.svc.db.account_by("phone", DAVE))["sms_status"] == "sms"
    assert (await app_remove(acc, ADMIN, DAVE))["ok"]


async def test_only_group_members_can_add_and_adds_are_limited(acc):
    with pytest.raises(ServiceError) as e:
        await app_add(acc, "@stranger:example.org", DAVE, "Dave")
    assert e.value.status == 403
    acc.cfg.adds_per_day = 1
    await app_add(acc, ALICE, DAVE, "Dave")
    with pytest.raises(ServiceError) as e:
        await app_add(acc, ALICE, SUE, "Sue")
    assert e.value.status == 429
    assert (await app_add(acc, ADMIN, SUE, "Sue"))["ok"]  # admins aren't limited
    acc.cfg.members_can_add = False
    with pytest.raises(ServiceError) as e:
        await app_add(acc, BOB, "+15555550003", "Carl")
    assert e.value.status == 403


async def test_cant_add_someone_who_uses_the_app(acc):
    v = await acc.svc.start_signup({"display_name": "Sue", "phone": SUE, "join_token": "join-me-123"}, None)
    assert await acc.svc.sms_code(SUE, sms_code_of(v))
    await acc.svc.poll(v["verification_id"])
    with pytest.raises(ServiceError) as e:
        await app_add(acc, ALICE, SUE, "Sue")
    assert e.value.code == "uses_app"


async def test_switching_to_the_app_keeps_the_same_account(acc):
    await app_add(acc, ALICE, DAVE, "Dave Miller")
    await acc.sms(DAVE, "JOIN")
    await acc.drain()
    # Dave installs the app and "signs up" with his number: that signs him in to his account, no invite needed.
    v = await acc.svc.start_signup({"display_name": "David", "phone": DAVE}, None)
    assert await acc.svc.sms_code(DAVE, sms_code_of(v))
    sess = await acc.svc.poll(v["verification_id"])
    assert sess["user_id"] == "@dave.miller:example.org"
    assert (await acc.svc.db.account_by("phone", DAVE))["sms_status"] is None
    assert (await acc.db.get_member(ROOM, DAVE)).status == "app"
    assert "@dave.miller:example.org" in acc.port.rooms[ROOM]  # never left
    assert acc.port.state[(ROOM, SMS_MEMBERS_EVENT, "")] == {"users": []}
    # He's an app user now: what he posts reaches SMS members, and he's no longer texted.
    await app_add(acc, ALICE, SUE, "Sue")
    await acc.sms(SUE, "JOIN")
    await acc.drain()
    acc.provider.sent.clear()
    await acc.matrix({"type": "m.room.message", "sender": "@dave.miller:example.org",
                      "content": {"msgtype": "m.text", "body": "from the app"}})
    await acc.drain()
    assert texts_to(acc, SUE)[-1] == "Dave Miller: from the app"
    assert texts_to(acc, DAVE) == []


async def test_list_for_the_app(acc):
    await app_add(acc, ALICE, DAVE, "Dave Miller")
    await app_add(acc, BOB, SUE, "Sue")
    acc.svc.matrix.tokens["tok-a"] = ALICE
    res = await acc.svc.sms_members("tok-a", "list", {"room_id": ROOM})
    by_name = {m["name"]: m for m in res["members"]}
    assert by_name["Dave Miller"]["can_remove"] and not by_name["Sue"]["can_remove"]
    assert by_name["Dave Miller"]["mxid"] == "@dave.miller:example.org"
    assert by_name["Dave Miller"]["number"] == DAVE and "number" not in by_name["Sue"]  # not Alice's to see
    with pytest.raises(ServiceError) as e:
        await acc.svc.sms_members("bad-token", "list", {"room_id": ROOM})
    assert e.value.status == 401


async def test_only_relevant_events_are_kept(acc):
    other = {"type": "m.room.message", "room_id": "!elsewhere:example.org", "sender": ALICE,
             "content": {"msgtype": "m.text", "body": "private chat"}}
    assert not await acc.bridge.wants_matrix_event(other)
    assert not await acc.bridge.wants_matrix_event({**other, "content": {"msgtype": "m.text", "body": "!help"}})
    assert await acc.bridge.wants_matrix_event({**other, "room_id": ROOM})
    acc.port.rooms["!botdm:example.org"] = {acc.port.bot_mxid: "bot", ADMIN: "Evan"}
    assert await acc.bridge.wants_matrix_event(
        {**other, "room_id": "!botdm:example.org", "content": {"msgtype": "m.text", "body": "!help"}})
    await app_add(acc, ALICE, DAVE, "Dave Miller")
    invite = {"type": "m.room.member", "room_id": "!dm:example.org", "sender": ALICE,
              "state_key": "@dave.miller:example.org", "content": {"membership": "invite"}}
    assert await acc.bridge.wants_matrix_event(invite)
    assert not await acc.bridge.wants_matrix_event({**invite, "state_key": BOB})


async def test_members_can_add_with_a_command_too(acc):
    await acc.matrix({"type": "m.room.message", "sender": ALICE,
                      "content": {"msgtype": "m.text", "body": f"!sms add {DAVE} Dave Miller"}})
    assert "They got a text" in acc.port.sent[-1]["content"]["body"]
    await acc.matrix({"type": "m.room.message", "sender": BOB,
                      "content": {"msgtype": "m.text", "body": f"!sms remove {DAVE}"}})
    assert "Only an admin can remove someone who has joined" in acc.port.sent[-1]["content"]["body"]
    await acc.matrix({"type": "m.room.message", "sender": BOB, "content": {"msgtype": "m.text", "body": "!sms list"}})
    assert acc.port.sent[-1]["content"]["body"] == "Admins only."


async def test_internal_endpoint_needs_the_secret(acc):
    app = build_bridge_internal_app(acc.bridge.app_user, SECRET, acc.bridge.sms_members_api)
    async with TestClient(TestServer(app)) as c:
        r = await c.post("/internal/sms-members/list", json={"by": ALICE, "room_id": ROOM})
        assert r.status == 401
        r = await c.post("/internal/sms-members/list", json={"by": ALICE, "room_id": ROOM},
                         headers={"Authorization": f"Bearer {SECRET}"})
        assert r.status == 200 and (await r.json())["ok"]
        r = await c.post("/internal/sms-members/delete", json={"by": ALICE},
                         headers={"Authorization": f"Bearer {SECRET}"})
        assert r.status == 404

