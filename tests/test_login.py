"""App login service (D13) against fake MAS / Matrix / SMTP, plus the bridge's side of it."""

import itertools
import re

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from smsgw.login.clients import UpstreamError
from smsgw.login.config import LoginConfig
from smsgw.login.db import LoginDB
from smsgw.login.service import LoginService, ServiceError, username_base
from smsgw.login.web import build_internal_app, build_public_app
from smsgw.web import build_internal_app as build_bridge_internal_app
from tests.conftest import GROUP_NUMBER, ROOM
from tests.test_flows import ADMIN, DAVE, STRANGER, consent, joined, texts_to

DOMAIN = "example.org"
TOKEN = "join-me-123"
EMAIL = "dave@example.net"
SECRET = "internal-secret"


class FakeMas:
    def __init__(self) -> None:
        self.users: dict[str, str] = {}  # username -> id
        self.passwords: dict[str, str] = {}  # id -> password
        self.emails: list[tuple[str, str]] = []
        self.tokens = {TOKEN}
        self.homeserver_taken: set[str] = set()
        self._ids = itertools.count(1)

    async def user_by_username(self, username):
        return self.users.get(username)

    async def create_user(self, username, displayname=None):
        if username in self.homeserver_taken:
            raise UpstreamError("MAS POST /users", 409)
        uid = f"01USER{next(self._ids)}"
        self.users[username] = uid
        return uid

    async def set_password(self, user_id, password):
        self.passwords[user_id] = password

    async def add_email(self, user_id, email):
        self.emails.append((user_id, email))

    async def registration_token_valid(self, token):
        return token in self.tokens


class FakeMatrix:
    def __init__(self, mas: FakeMas) -> None:
        self.mas = mas
        self.tokens: dict[str, str] = {}  # access token -> mxid
        self.displaynames: dict[str, str] = {}
        self.fail_next = 0
        self._ids = itertools.count(1)

    async def password_login(self, username, password, device_name):
        if self.fail_next:
            self.fail_next -= 1
            raise UpstreamError("Matrix login", 502)
        uid = self.mas.users.get(username)
        if uid is None or self.mas.passwords.get(uid) != password:
            raise UpstreamError("Matrix login", 403)
        tok = f"syt_{next(self._ids)}"
        self.tokens[tok] = f"@{username}:{DOMAIN}"
        return {"user_id": f"@{username}:{DOMAIN}", "access_token": tok, "refresh_token": "ref",
                "device_id": "DEV", "expires_in_ms": 300000}

    async def whoami(self, token):
        return self.tokens.get(token)

    async def set_displayname(self, token, mxid, name):
        self.displaynames[mxid] = name


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, to, subject, body):
        self.sent.append((to, subject, body))

    def last_code(self) -> str:
        return re.search(r"\b(\d{6})\b", self.sent[-1][2]).group(1)


class FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def app_user(self, mxid, number):
        self.calls.append((mxid, number))


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def login_config(tmp_path) -> LoginConfig:
    return LoginConfig(
        domain=DOMAIN, client_url="http://hs", public_homeserver_url="https://example.org",
        mas_url="http://mas/auth", mas_client_id="client", mas_client_secret="s", public_host="127.0.0.1",
        public_port=0, internal_host="127.0.0.1", internal_port=0, internal_secret=SECRET, bridge_url="http://bridge",
        database=str(tmp_path / "login.db"), code_secret="pepper", code_ttl_seconds=600, code_max_attempts=3,
        starts_per_hour=5, starts_per_hour_ip=20, sms_number=GROUP_NUMBER, smtp_host="smtp", smtp_port=587,
        smtp_starttls=True, smtp_username=None, smtp_password=None, email_from="noreply@example.org",
        app_name="Example Chat", device_name="Example Chat app", client_ip_header="CF-Connecting-IP",
    )


@pytest_asyncio.fixture
async def svc(tmp_path):
    db = LoginDB(str(tmp_path / "login.db"))
    await db.open()
    mas = FakeMas()
    s = LoginService(login_config(tmp_path), db, mas, FakeMatrix(mas), FakeMailer(), FakeBridge(), now=Clock())
    yield s
    await db.close()


def sms_code_of(started: dict) -> str:
    return started["sms_body"].split()[-1]


async def signup_email(svc, name="Dave Miller", email=EMAIL) -> dict:
    v = await svc.start_signup({"display_name": name, "email": email, "join_token": TOKEN}, "1.2.3.4")
    return await svc.verify_code(v["verification_id"], svc.mailer.last_code())


# ---- sign-up ----------------------------------------------------------------------------

async def test_signup_by_email(svc):
    v = await svc.start_signup({"display_name": " Dave  Miller ", "email": "Dave@Example.net", "join_token": TOKEN}, None)
    assert v["channel"] == "email" and "sms_body" not in v
    to, subject, body = svc.mailer.sent[-1]
    assert to == EMAIL and svc.mailer.last_code() in subject
    sess = await svc.verify_code(v["verification_id"], svc.mailer.last_code())
    assert sess["user_id"] == "@dave.miller:example.org"
    assert sess["access_token"] and sess["refresh_token"] == "ref"
    assert sess["homeserver"] == "https://example.org"
    acct = await svc.db.account_by("email", EMAIL)
    assert acct["mxid"] == "@dave.miller:example.org" and acct["code_managed"] == 1
    assert svc.mas.emails == [(acct["mas_user_id"], EMAIL)]
    assert svc.matrix.displaynames["@dave.miller:example.org"] == "Dave Miller"
    assert svc.bridge.calls == [("@dave.miller:example.org", None)]


async def test_signup_by_phone_text_us_a_code(svc):
    v = await svc.start_signup({"display_name": "Dave", "phone": "(555) 555-0001", "join_token": TOKEN}, None)
    assert v["sms_to"] == GROUP_NUMBER and re.fullmatch(r"Sign-in code \d{6}", v["sms_body"])
    assert await svc.poll(v["verification_id"]) == {"status": "pending"}
    assert not await svc.sms_code(STRANGER, sms_code_of(v))  # wrong phone
    assert await svc.sms_code(DAVE, sms_code_of(v))
    sess = await svc.poll(v["verification_id"])
    assert sess["user_id"] == "@dave:example.org"
    assert (await svc.db.account_by("phone", DAVE))["mxid"] == "@dave:example.org"
    assert svc.bridge.calls == [("@dave:example.org", DAVE)]
    assert svc.mailer.sent == []  # nothing sent anywhere for a phone code


async def test_signup_needs_valid_join_token(svc):
    with pytest.raises(ServiceError) as e:
        await svc.start_signup({"display_name": "Dave", "email": EMAIL, "join_token": "nope"}, None)
    assert e.value.code == "bad_join_token"
    assert svc.mailer.sent == []


async def test_signup_username_collisions(svc):
    svc.mas.users["dave.miller"] = "01OTHER"
    svc.mas.homeserver_taken.add("dave.miller2")
    sess = await signup_email(svc)
    assert sess["user_id"] == "@dave.miller3:example.org"


async def test_signup_twice_is_refused(svc):
    await signup_email(svc)
    with pytest.raises(ServiceError) as e:
        await svc.start_signup({"display_name": "Dave", "email": EMAIL, "join_token": TOKEN}, None)
    assert e.value.code == "already_registered"


def test_username_base():
    assert username_base("Zoë O'Brien-Smith") == "zoe.o.brien.smith"
    assert username_base("李") == "member"
    assert len(username_base("x" * 80)) <= 24


# ---- login ------------------------------------------------------------------------------

async def test_login_rotates_password(svc):
    first = await signup_email(svc)
    uid = (await svc.db.account_by("email", EMAIL))["mas_user_id"]
    pw1 = svc.mas.passwords[uid]
    v = await svc.start_login({"email": EMAIL}, None)
    sess = await svc.verify_code(v["verification_id"], svc.mailer.last_code())
    assert sess["user_id"] == first["user_id"]
    assert svc.mas.passwords[uid] != pw1


async def test_login_unknown_or_password_account(svc):
    with pytest.raises(ServiceError) as e:
        await svc.start_login({"email": "nobody@example.net"}, None)
    assert e.value.code == "no_account"
    await svc.db.upsert_account("@pat:example.org", "01PAT", "pat", email="pat@example.net", code_managed=0)
    with pytest.raises(ServiceError) as e:
        await svc.start_login({"email": "pat@example.net"}, None)
    assert e.value.code == "no_account"


async def test_wrong_codes_lock_the_request(svc):
    v = await svc.start_signup({"display_name": "Dave", "email": EMAIL, "join_token": TOKEN}, None)
    code = svc.mailer.last_code()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(2):
        with pytest.raises(ServiceError) as e:
            await svc.verify_code(v["verification_id"], wrong)
        assert e.value.code == "wrong_code"
    with pytest.raises(ServiceError) as e:
        await svc.verify_code(v["verification_id"], wrong)
    assert e.value.code == "too_many_attempts"
    with pytest.raises(ServiceError) as e:
        await svc.verify_code(v["verification_id"], code)
    assert e.value.code == "expired"


async def test_code_expires(svc):
    v = await svc.start_signup({"display_name": "Dave", "email": EMAIL, "join_token": TOKEN}, None)
    svc.now.t += 601
    with pytest.raises(ServiceError) as e:
        await svc.verify_code(v["verification_id"], svc.mailer.last_code())
    assert e.value.code == "expired"


async def test_code_is_single_use(svc):
    v = await svc.start_signup({"display_name": "Dave", "email": EMAIL, "join_token": TOKEN}, None)
    await svc.verify_code(v["verification_id"], svc.mailer.last_code())
    with pytest.raises(ServiceError) as e:
        await svc.verify_code(v["verification_id"], svc.mailer.last_code())
    assert e.value.code == "already_used"


async def test_new_request_replaces_old(svc):
    v1 = await svc.start_signup({"display_name": "Dave", "phone": DAVE, "join_token": TOKEN}, None)
    v2 = await svc.start_signup({"display_name": "Dave", "phone": DAVE, "join_token": TOKEN}, None)
    assert not await svc.sms_code(DAVE, sms_code_of(v1))
    assert await svc.sms_code(DAVE, sms_code_of(v2))


async def test_transient_failure_can_be_retried(svc):
    v = await svc.start_signup({"display_name": "Dave", "email": EMAIL, "join_token": TOKEN}, None)
    svc.matrix.fail_next = 1
    with pytest.raises(ServiceError) as e:
        await svc.verify_code(v["verification_id"], svc.mailer.last_code())
    assert e.value.code == "try_again"
    sess = await svc.verify_code(v["verification_id"], "anything")  # already verified; code not re-checked
    assert sess["user_id"] == "@dave:example.org"


async def test_rate_limit_per_identifier(svc):
    for _ in range(5):
        await svc.start_signup({"display_name": "Dave", "email": EMAIL, "join_token": TOKEN}, None)
    with pytest.raises(ServiceError) as e:
        await svc.start_signup({"display_name": "Dave", "email": EMAIL, "join_token": TOKEN}, None)
    assert e.value.status == 429
    svc.now.t += 3601
    await svc.start_signup({"display_name": "Dave", "email": EMAIL, "join_token": TOKEN}, None)


async def test_sms_code_attempts_are_limited(svc):
    v = await svc.start_signup({"display_name": "Dave", "phone": DAVE, "join_token": TOKEN}, None)
    for _ in range(3):
        assert not await svc.sms_code(DAVE, "999999" if sms_code_of(v) != "999999" else "888888")
    assert not await svc.sms_code(DAVE, sms_code_of(v))


# ---- switching an existing password account to codes -----------------------------------

async def password_user(svc, username="pat"):
    uid = await svc.mas.create_user(username)
    svc.mas.passwords[uid] = "their-own-password"
    sess = await svc.matrix.password_login(username, "their-own-password", "phone")
    return uid, sess["access_token"]


async def test_switch_to_codes(svc):
    uid, token = await password_user(svc)
    v = await svc.start_switch(token, {"phone": "+1 555 555 0001"}, None)
    assert await svc.sms_code(DAVE, sms_code_of(v))
    sess = await svc.poll(v["verification_id"])
    assert sess["user_id"] == "@pat:example.org"
    assert svc.mas.passwords[uid] != "their-own-password"
    acct = await svc.db.account_by("phone", DAVE)
    assert acct["mxid"] == "@pat:example.org" and acct["code_managed"] == 1
    assert svc.bridge.calls == [("@pat:example.org", DAVE)]
    # From now on code login works for them.
    v = await svc.start_login({"phone": DAVE}, None)
    assert await svc.sms_code(DAVE, sms_code_of(v))
    assert (await svc.poll(v["verification_id"]))["user_id"] == "@pat:example.org"


async def test_switch_needs_a_session(svc):
    with pytest.raises(ServiceError) as e:
        await svc.start_switch("bogus", {"email": EMAIL}, None)
    assert e.value.status == 401


async def test_switch_cannot_take_someone_elses_email(svc):
    await signup_email(svc)
    _, token = await password_user(svc)
    with pytest.raises(ServiceError) as e:
        await svc.start_switch(token, {"email": EMAIL}, None)
    assert e.value.code == "already_registered"


async def test_admin_link(svc):
    uid, _ = await password_user(svc)
    await svc.admin_link("@pat:example.org", svc.identifier({"email": "pat@example.net"}))
    assert svc.mas.passwords[uid] == "their-own-password"  # unchanged until they actually sign in
    assert svc.bridge.calls == []
    v = await svc.start_login({"email": "pat@example.net"}, None)
    await svc.verify_code(v["verification_id"], svc.mailer.last_code())
    assert svc.mas.passwords[uid] != "their-own-password"


# ---- HTTP -------------------------------------------------------------------------------

async def test_public_http(svc):
    async with TestClient(TestServer(build_public_app(svc, "CF-Connecting-IP"))) as c:
        r = await c.post("/v1/login", data="not json")
        assert r.status == 400 and (await r.json())["error"] == "bad_request"
        r = await c.post("/v1/signup", json={"display_name": "Dave", "phone": DAVE, "join_token": TOKEN},
                         headers={"CF-Connecting-IP": "9.9.9.9"})
        v = await r.json()
        assert r.status == 200 and v["sms_to"] == GROUP_NUMBER
        assert (await (await c.get(f"/v1/verify/{v['verification_id']}")).json()) == {"status": "pending"}
        await svc.sms_code(DAVE, sms_code_of(v))
        r = await c.get(f"/v1/verify/{v['verification_id']}")
        assert r.status == 200 and (await r.json())["user_id"] == "@dave:example.org"
        r = await c.post("/v1/switch", json={"email": EMAIL})
        assert r.status == 401
    assert await svc.db.count_starts("ip:9.9.9.9", 0) == 1


async def test_internal_http(svc):
    async with TestClient(TestServer(build_internal_app(svc, SECRET))) as c:
        r = await c.post("/internal/sms-code", json={"number": DAVE, "code": "123456"})
        assert r.status == 401
        r = await c.post("/internal/sms-code", json={"number": DAVE, "code": "123456"},
                         headers={"Authorization": f"Bearer {SECRET}"})
        assert (await r.json()) == {"matched": False}


# ---- bridge side ------------------------------------------------------------------------

async def test_bridge_forwards_sign_in_codes(env):
    got = []

    async def forward(number, code):
        got.append((number, code))

    env.bridge.forward_login_code = forward
    await env.sms(STRANGER, "Sign-in code 482913")
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "sign in code: 123456")
    await env.drain()
    assert got == [(STRANGER, "482913"), (DAVE, "123456")]
    assert env.provider.sent == []  # no reply to anyone
    assert not any("123456" in e["content"].get("body", "") for e in env.port.sent)  # not posted to the group


async def test_bridge_without_login_service_treats_code_as_text(env):
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "Sign-in code 123456")
    assert any(e["content"].get("body") == "Sign-in code 123456" for e in env.port.sent)


async def test_bridge_forward_failure_is_swallowed(env):
    async def forward(number, code):
        raise ConnectionError("login service down")

    env.bridge.forward_login_code = forward
    await env.sms(STRANGER, "Sign-in code 482913")  # doesn't raise, so the inbox isn't blocked


async def test_bridge_app_user_moves_member_off_sms(env):
    await joined(env, DAVE, "Dave Miller")
    ghost = env.port.ghost_mxid(DAVE)
    assert ghost in env.port.rooms[ROOM]
    await env.bridge.app_user("@dave:example.org", DAVE)
    assert env.port.invited == [(ROOM, "@dave:example.org")]
    assert (await env.db.get_member(ROOM, DAVE)).status == "app"
    assert ghost not in env.port.rooms[ROOM]
    assert ("moved_to_app", "login") in await consent(env, DAVE)
    # Group messages no longer go to their phone.
    await env.matrix({"type": "m.room.message", "sender": ADMIN, "content": {"msgtype": "m.text", "body": "hi all"}})
    await env.drain()
    assert texts_to(env, DAVE) == []
    # Idempotent; and texting START turns SMS back on.
    await env.bridge.app_user("@dave:example.org", DAVE)
    assert [e for e, _ in await consent(env, DAVE)].count("moved_to_app") == 1
    await env.sms(DAVE, "START")
    assert (await env.db.get_member(ROOM, DAVE)).status == "opted_in"


async def test_bridge_app_user_without_number_only_invites(env):
    await env.bridge.app_user("@new:example.org", None)
    assert env.port.invited == [(ROOM, "@new:example.org")]


async def test_bridge_internal_http(env):
    async with TestClient(TestServer(build_bridge_internal_app(env.bridge.app_user, SECRET))) as c:
        r = await c.post("/internal/app-user", json={"mxid": "@new:example.org"})
        assert r.status == 401
        r = await c.post("/internal/app-user", json={"mxid": "nope"}, headers={"Authorization": f"Bearer {SECRET}"})
        assert r.status == 400
        r = await c.post("/internal/app-user", json={"mxid": "@new:example.org", "number": None},
                         headers={"Authorization": f"Bearer {SECRET}"})
        assert r.status == 200
    assert env.port.invited == [(ROOM, "@new:example.org")]
