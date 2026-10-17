"""Generated initials avatars, #avatar, truncated-MMS download, and corrupt-photo fallback."""

import io

from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image

from smsgw.media import initials
from smsgw.provider.telnyx import TelnyxProvider
from tests.conftest import ROOM
from tests.test_flows import ALICE, DAVE, SUE, add, joined, texts_to


def _jpeg(size=(800, 600), color=(10, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _avatars_for(env, number):
    return [a for a in env.port.avatars if a[0] == env.port.ghost_mxid(number)]


def test_initials():
    assert initials("Ryan J Martin") == "RM"
    assert initials("dave") == "D"
    assert initials("  ") == "#"
    assert initials("(SMS)") == "#"


async def test_generated_avatar_on_add_and_not_repeated(env):
    await add(env, DAVE, "Dave Miller")
    [(mxid, png, mime)] = _avatars_for(env, DAVE)
    assert mime == "image/png"
    with Image.open(io.BytesIO(png)) as im:
        assert im.size == (256, 256)
    await env.sms(DAVE, "JOIN")
    await env.sms(DAVE, "hello")
    assert len(_avatars_for(env, DAVE)) == 1  # same initials: no re-upload


async def test_name_change_regenerates_only_when_initials_change(env):
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "#name Dave Mason")  # still DM
    assert len(_avatars_for(env, DAVE)) == 1
    await env.sms(DAVE, "#name Big Dave")  # BD
    assert len(_avatars_for(env, DAVE)) == 2
    assert env.port.displaynames[env.port.ghost_mxid(DAVE)] == "Big Dave (SMS)"


async def test_avatar_command_sets_custom_and_survives_rename(env):
    await joined(env, DAVE, "Dave Miller")
    photo = _jpeg()

    async def fetch(url, max_bytes):
        return photo, "image/jpeg"

    env.provider.fetch_media = fetch
    await env.sms(DAVE, "#avatar", media=[{"url": "https://telnyx/media/9", "content_type": "image/jpeg"}])
    await env.drain()
    _, data, mime = _avatars_for(env, DAVE)[-1]
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (512, 512)
    assert texts_to(env, DAVE)[-1] == "Profile picture updated."
    # Not posted to the group.
    assert not [e for e in env.port.sent if e["sender"] == env.port.ghost_mxid(DAVE) and e["type"] == "m.room.message"]
    n = len(_avatars_for(env, DAVE))
    await env.sms(DAVE, "#name Big Dave")
    assert len(_avatars_for(env, DAVE)) == n  # custom picture kept
    await env.sms(DAVE, "#avatar reset")
    await env.drain()
    assert _avatars_for(env, DAVE)[-1][2] == "image/png"
    assert texts_to(env, DAVE)[-1] == "Profile picture reset."


async def test_avatar_command_without_photo(env):
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "#avatar")
    await env.drain()
    assert texts_to(env, DAVE) == ["Text a photo with #avatar as the message to set your picture."]


async def test_corrupt_matrix_photo_falls_back_to_text(env):
    await joined(env, DAVE, "Dave Miller")
    env.port.media["mxc://hs/bad"] = _jpeg()[:600]  # truncated JPEG, like the pre-fix MMS uploads
    env.port.rooms[ROOM][ALICE] = "Alice"
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {
        "msgtype": "m.image", "body": "x.jpg", "url": "mxc://hs/bad", "info": {"mimetype": "image/jpeg"}}})
    await env.drain()
    assert texts_to(env, DAVE) == ["Alice sent a photo"]


async def test_fetch_media_reads_whole_body(keypair):
    big = _jpeg((3000, 2000)) + b"\0" * 400_000  # > several network chunks

    async def handler(request):
        resp = web.StreamResponse(headers={"Content-Type": "image/jpeg", "Content-Length": str(len(big))})
        await resp.prepare(request)
        for i in range(0, len(big), 8192):  # dribble it out in small writes
            await resp.write(big[i:i + 8192])
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/media", handler)
    async with TestServer(app) as server:
        p = TelnyxProvider("KEY", keypair[1], None)
        try:
            data, ctype = await p.fetch_media(str(server.make_url("/media")), 5_000_000)
        finally:
            await p.close()
    assert data == big and ctype == "image/jpeg"
