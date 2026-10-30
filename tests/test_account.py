"""In-app account management: the signed-in person's own account, without MAS's web pages."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from smsgw.login.service import ServiceError
from smsgw.login.web import build_public_app
from tests.test_login import EMAIL, signup_email, svc  # noqa: F401  (svc is a fixture)


async def password_account(svc, username="pat", password="their-own-password"):  # noqa: F811
    uid = await svc.mas.create_user(username)
    svc.mas.passwords[uid] = password
    sess = await svc.matrix.password_login(username, password, "phone")
    return uid, sess["access_token"]


async def test_account_overview(svc):  # noqa: F811
    sess = await signup_email(svc)
    second = await svc.matrix.password_login("dave.miller", svc.mas.passwords[
        (await svc.db.account_by("email", EMAIL))["mas_user_id"]], "tablet")
    info = await svc.account(sess["access_token"])
    assert info["user_id"] == "@dave.miller:example.org"
    assert info["sign_in"] == "codes" and info["email"] == EMAIL and info["phone"] is None
    devices = {s["device_id"]: s["current"] for s in info["sessions"]}
    assert devices == {sess["device_id"]: True, second["device_id"]: False}


async def test_sign_out_another_device(svc):  # noqa: F811
    _, token = await password_account(svc)
    other = await svc.matrix.password_login("pat", "their-own-password", "tablet")
    info = await svc.account(token)
    theirs = next(s for s in info["sessions"] if not s["current"])
    assert theirs["device_id"] == other["device_id"]
    assert await svc.end_session(token, {"kind": theirs["kind"], "id": theirs["id"]}) == {"ok": True}
    assert [s["current"] for s in (await svc.account(token))["sessions"]] == [True]
    # This phone's own session goes through Sign out, not here.
    mine = (await svc.account(token))["sessions"][0]
    with pytest.raises(ServiceError) as e:
        await svc.end_session(token, {"kind": mine["kind"], "id": mine["id"]})
    assert e.value.code == "current_session"


async def test_cannot_end_someone_elses_session(svc):  # noqa: F811
    _, pat = await password_account(svc, "pat")
    _, sam = await password_account(svc, "sam")
    sams = (await svc.account(sam))["sessions"][0]
    with pytest.raises(ServiceError) as e:
        await svc.end_session(pat, {"kind": sams["kind"], "id": sams["id"]})
    assert e.value.code == "not_found"
    assert svc.mas.sessions[sams["id"]]["active"]


async def test_change_password(svc):  # noqa: F811
    uid, token = await password_account(svc)
    sessions_before = sum(x["active"] for x in svc.mas.sessions.values())
    with pytest.raises(ServiceError) as e:
        await svc.change_password(token, {"current": "nope", "new": "a-new-password"})
    assert e.value.code == "wrong_password"
    with pytest.raises(ServiceError) as e:
        await svc.change_password(token, {"current": "their-own-password", "new": "short"})
    assert e.value.code == "weak_password"
    with pytest.raises(ServiceError) as e:  # MAS's own policy
        await svc.change_password(token, {"current": "their-own-password", "new": "password"})
    assert e.value.code == "weak_password"
    assert await svc.change_password(token, {"current": "their-own-password", "new": "a-new-password"})
    assert svc.mas.passwords[uid] == "a-new-password"
    # The sign-in used to check the old password doesn't linger as a device.
    assert sum(x["active"] for x in svc.mas.sessions.values()) == sessions_before


async def test_code_accounts_have_no_password_to_change(svc):  # noqa: F811
    sess = await signup_email(svc)
    with pytest.raises(ServiceError) as e:
        await svc.change_password(sess["access_token"], {"current": "x", "new": "a-new-password"})
    assert e.value.code == "uses_codes"


async def test_delete_account(svc):  # noqa: F811
    sess = await signup_email(svc)
    token = sess["access_token"]
    with pytest.raises(ServiceError) as e:
        await svc.delete_account(token, {"confirm": "@someone:example.org"})
    assert e.value.code == "confirm"
    uid = (await svc.db.account_by("email", EMAIL))["mas_user_id"]
    assert await svc.delete_account(token, {"confirm": "@dave.miller:example.org"}) == {"ok": True}
    assert uid in svc.mas.deactivated
    assert await svc.db.account_by("email", EMAIL) is None


async def test_account_http_needs_a_session(svc):  # noqa: F811
    _, token = await password_account(svc)
    async with TestClient(TestServer(build_public_app(svc, None))) as c:
        r = await c.get("/v1/account")
        assert r.status == 401 and (await r.json())["error"] == "unauthorized"
        r = await c.get("/v1/account", headers={"Authorization": f"Bearer {token}"})
        assert r.status == 200 and (await r.json())["sign_in"] == "password"
