"""End-to-end flows against fake Matrix + fake Telnyx."""

import io
import json
import time

from PIL import Image

from smsgw import compliance
from smsgw.provider.base import SendError
from tests.conftest import GROUP_NUMBER, ROOM

DAVE = "+15555550001"
SUE = "+15555550002"
STRANGER = "+15555559999"
ADMIN = "@evan:example.org"
ALICE = "@alice:example.org"


async def add(env, number, name):
    env.port.power[ADMIN] = 100
    await env.matrix({"type": "m.room.message", "sender": ADMIN, "content": {"msgtype": "m.text", "body": f"!sms add {number} {name}"}})


async def joined(env, number, name):
    await add(env, number, name)
    await env.sms(number, "JOIN")
    await env.drain()
    env.provider.sent.clear()


def texts_to(env, number):
    return [s["text"] for s in env.provider.sent if s["to"] == number]


async def consent(env, number):
    rows = await env.db.fetchall("SELECT event, source FROM consent_log WHERE number=? ORDER BY id", (number,))
    return [(r["event"], r["source"]) for r in rows]


# ---- opt-in ---------------------------------------------------------------------------

async def test_add_sends_nothing(env):
    await add(env, DAVE, "Dave Miller")
    await env.drain()
    assert env.provider.sent == []
    m = await env.db.get_member(ROOM, DAVE)
    assert m.status == "allowlisted"


async def test_join_not_allowlisted(env):
    await env.sms(STRANGER, "join")
    await env.drain()
    assert texts_to(env, STRANGER) == ["Ask the group admin to add your number."]
    assert await consent(env, STRANGER) == [("join_rejected", "bridge")]
    assert await env.db.get_member(ROOM, STRANGER) is None


async def test_join_allowlisted(env):
    await add(env, DAVE, "Dave Miller")
    await env.sms(DAVE, " Join ")
    await env.drain()
    assert texts_to(env, DAVE) == [compliance.opt_in("Example Chat"), compliance.opt_in_followup("Example Chat")]
    assert "STOP, CANCEL, END or QUIT" in texts_to(env, DAVE)[1]
    assert (await env.db.get_member(ROOM, DAVE)).status == "opted_in"
    assert ("opt_in", "bridge") in await consent(env, DAVE)
    assert env.port.ghost_mxid(DAVE) in env.port.rooms[ROOM]
    assert env.port.displaynames[env.port.ghost_mxid(DAVE)] == "Dave Miller (SMS)"


async def test_allowlisted_but_not_joined_is_ignored(env):
    await add(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "hey is this the group?")
    await env.drain()
    assert env.provider.sent == []
    assert not [e for e in env.port.sent if e["sender"] == env.port.ghost_mxid(DAVE)]


async def test_unknown_number_plain_text_ignored(env):
    await env.sms(STRANGER, "hello")
    await env.drain()
    assert env.provider.sent == []


# ---- opt-out --------------------------------------------------------------------------

async def test_stop_with_telnyx_autoresponse_sends_nothing(env):
    await joined(env, DAVE, "Dave Miller")
    await joined(env, SUE, "Sue")
    # A group message is queued for Dave, then he texts STOP before it goes out.
    await env.sms(SUE, "pizza at 6?")
    await env.echo()
    await env.sms(DAVE, "STOP", autoresponse="STOP")
    await env.drain()
    assert texts_to(env, DAVE) == []  # neither the queued message nor any confirmation from us
    assert (await env.db.get_member(ROOM, DAVE)).status == "opted_out"
    assert ("opt_out", "telnyx_autoresponse") in await consent(env, DAVE)
    assert (ROOM, env.port.ghost_mxid(DAVE)) in env.port.left
    row = await env.db.fetchone("SELECT status, error FROM outbound WHERE to_number=? AND kind='group'", (DAVE,))
    assert row["status"] == "skipped"


async def test_stop_family_keywords(env):
    for i, kw in enumerate(["cancel", "END", "quit", "Unsubscribe", "stop all"]):
        num = f"+1555555010{i}"
        await joined(env, num, f"Person {i}")
        await env.sms(num, kw, autoresponse="STOP")
        assert (await env.db.get_member(ROOM, num)).status == "opted_out", kw


async def test_stop_without_flag_sends_registered_confirmation_once(env):
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "stop")
    await env.drain()
    assert texts_to(env, DAVE) == [compliance.opt_out("Example Chat")]
    assert (await env.db.get_member(ROOM, DAVE)).status == "opted_out"


async def test_stop_without_flag_but_telnyx_blocked_sends_nothing(env):
    await joined(env, DAVE, "Dave Miller")
    env.provider.blocked.add(DAVE)  # Telnyx blocked and replied itself
    await env.sms(DAVE, "stop")
    await env.drain()
    assert texts_to(env, DAVE) == []


async def test_yes_is_a_group_message(env):
    await joined(env, DAVE, "Dave Miller")
    await joined(env, SUE, "Sue")
    await env.sms(DAVE, "yes")
    ghost_posts = [e for e in env.port.sent if e["sender"] == env.port.ghost_mxid(DAVE)]
    assert ghost_posts[-1]["content"]["body"] == "yes"
    assert (await env.db.get_member(ROOM, DAVE)).status == "opted_in"


async def test_start_resubscribes_previous_member(env):
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "STOP", autoresponse="STOP")
    await env.sms(DAVE, "START", autoresponse="START")
    await env.drain()
    assert texts_to(env, DAVE) == []  # Telnyx sent the confirmation
    assert (await env.db.get_member(ROOM, DAVE)).status == "opted_in"
    assert ("resubscribe", "telnyx_autoresponse") in await consent(env, DAVE)
    assert env.port.ghost_mxid(DAVE) in env.port.rooms[ROOM]


async def test_start_from_never_joined_or_stranger_does_nothing(env):
    await add(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "START", autoresponse="START")
    await env.sms(STRANGER, "start", autoresponse="START")
    await env.drain()
    assert env.provider.sent == []
    assert (await env.db.get_member(ROOM, DAVE)).status == "allowlisted"
    assert await consent(env, STRANGER) == [("start_ignored", "telnyx_autoresponse")]


async def test_join_after_stop_is_logged_not_sent(env):
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "STOP", autoresponse="STOP")
    await env.sms(DAVE, "JOIN")
    await env.drain()
    assert texts_to(env, DAVE) == []
    assert (await env.db.get_member(ROOM, DAVE)).status == "opted_out"
    assert ("join_while_opted_out", "bridge") in await consent(env, DAVE)


async def test_unexpected_40300_marks_opted_out(env):
    await joined(env, DAVE, "Dave Miller")
    await joined(env, SUE, "Sue")
    env.provider.blocked.add(DAVE)
    await env.sms(SUE, "hello all")
    await env.echo()
    await env.drain()
    assert (await env.db.get_member(ROOM, DAVE)).status == "opted_out"
    assert ("opt_out", "send_error") in await consent(env, DAVE)


async def test_help_autoresponded_by_telnyx_no_double(env):
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "HELP", autoresponse="HELP")
    await env.drain()
    assert texts_to(env, DAVE) == []


async def test_help_unflagged_member_gets_command_list(env):
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "?")
    await env.sms(STRANGER, "help")
    await env.drain()
    assert texts_to(env, DAVE)[0].startswith("Example Chat commands:")
    assert texts_to(env, STRANGER) == [compliance.help_registered("Example Chat", "help@example.org")]


# ---- relay ----------------------------------------------------------------------------

async def test_matrix_to_sms_fanout(env):
    await joined(env, DAVE, "Dave Miller")
    await joined(env, SUE, "Sue")
    env.port.rooms[ROOM][ALICE] = "Alice Cooper"
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {"msgtype": "m.text", "body": "Saturday?"}})
    await env.drain()
    assert texts_to(env, DAVE) == ["Alice Cooper: Saturday?"]
    assert texts_to(env, SUE) == ["Alice Cooper: Saturday?"]


async def test_sms_to_group_excludes_sender(env):
    await joined(env, DAVE, "Dave Miller")
    await joined(env, SUE, "Sue")
    await env.sms(DAVE, "on my way")
    post = [e for e in env.port.sent if e["sender"] == env.port.ghost_mxid(DAVE)][-1]
    # The ghost's event comes back through the appservice transaction:
    await env.matrix(dict(post))
    await env.drain()
    assert texts_to(env, SUE) == ["Dave Miller: on my way"]
    assert texts_to(env, DAVE) == []


async def test_reprocessing_does_not_double_send(env):
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM][ALICE] = "Alice"
    evt = {"type": "m.room.message", "sender": ALICE, "event_id": "$same", "content": {"msgtype": "m.text", "body": "hi"}}
    await env.matrix(dict(evt))
    await env.matrix(dict(evt))
    await env.drain()
    assert texts_to(env, DAVE) == ["Alice: hi"]


async def test_reply_and_edit_rendering(env):
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM][ALICE] = "Alice Cooper"
    parent = await env.port.send_text(ROOM, ADMIN, "Saturday at the park works for everyone?")
    env.port.rooms[ROOM][ADMIN] = "Evan Parker"
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {
        "msgtype": "m.text", "body": "> <@evan:example.org> Saturday...\n\nI'm in",
        "m.relates_to": {"m.in_reply_to": {"event_id": parent}}}})
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {
        "msgtype": "m.text", "body": "* I'm in!", "m.new_content": {"msgtype": "m.text", "body": "I'm in!"},
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$x"}}})
    await env.drain()
    assert texts_to(env, DAVE) == [
        "Alice Cooper: ↩ Evan: Saturday at the par… I'm in",
        "Alice Cooper: (edited) I'm in!",
    ]


async def test_reactions_and_redactions_ignored(env):
    await joined(env, DAVE, "Dave Miller")
    await env.matrix({"type": "m.reaction", "sender": ALICE, "content": {}})
    await env.matrix({"type": "m.room.redaction", "sender": ALICE, "content": {}})
    await env.drain()
    assert env.provider.sent == []


async def test_bang_commands_not_relayed(env):
    await joined(env, DAVE, "Dave Miller")
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {"msgtype": "m.text", "body": "!help"}})
    await env.drain()
    assert env.provider.sent == []


async def test_image_mms_and_photos_off(env):
    await joined(env, DAVE, "Dave Miller")
    await joined(env, SUE, "Sue")
    await env.sms(SUE, "PHOTOS OFF")
    await env.drain()
    env.provider.sent.clear()
    buf = io.BytesIO()
    Image.new("RGB", (4000, 3000), (200, 10, 10)).save(buf, "PNG")
    env.port.media["mxc://hs/img"] = buf.getvalue()
    env.port.rooms[ROOM][ALICE] = "Alice"
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {
        "msgtype": "m.image", "body": "IMG_2041.png", "url": "mxc://hs/img", "info": {"mimetype": "image/png"}}})
    await env.drain()
    dave = [s for s in env.provider.sent if s["to"] == DAVE][0]
    sue = [s for s in env.provider.sent if s["to"] == SUE][0]
    assert dave["media_url"].startswith("https://hook.example.org/m/") and dave["text"] == "Alice:"
    assert sue["media_url"] is None and sue["text"] == "[Alice sent a photo]"
    row = await env.db.fetchone("SELECT path FROM media_tokens")
    with Image.open(row["path"]) as im:
        assert max(im.size) <= 1200 and im.format == "JPEG"


async def test_inbound_mms_posts_image_and_text(env):
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "look", media=[{"url": "https://telnyx/media/1", "content_type": "image/jpeg"}])
    posts = [e for e in env.port.sent if e["sender"] == env.port.ghost_mxid(DAVE)]
    assert posts[-2]["content"]["msgtype"] == "m.image"
    assert posts[-1]["content"]["body"] == "look"


async def test_sms_mention_becomes_matrix_mention(env):
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM][ALICE] = "Alice Cooper"
    await env.sms(DAVE, "@Alice Cooper you coming?")
    post = [e for e in env.port.sent if e["sender"] == env.port.ghost_mxid(DAVE)][-1]
    assert post["content"]["m.mentions"]["user_ids"] == [ALICE]
    assert 'href="https://matrix.to/#/@alice:example.org"' in post["content"]["formatted_body"]


# ---- commands -------------------------------------------------------------------------

async def test_who_and_dm_matrix_user(env):
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM][ALICE] = "Alice Cooper"
    await env.sms(DAVE, "WHO")
    await env.drain()
    assert texts_to(env, DAVE)[-1] == "Members:\n1) Alice Cooper\n2) Dave Miller"
    await env.sms(DAVE, "DM alice see you at 6")
    await env.drain()
    assert texts_to(env, DAVE)[-1] == "Sent to Alice Cooper"
    dm_room = await env.db.get_dm_room(env.port.ghost_mxid(DAVE), ALICE)
    assert dm_room and env.port.sent[-1]["room_id"] == dm_room and env.port.sent[-1]["content"]["body"] == "see you at 6"


async def test_dm_ambiguous_and_none(env):
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM]["@d1:example.org"] = "Sam Smith"
    env.port.rooms[ROOM]["@d2:example.org"] = "Sam Jones"
    await env.sms(DAVE, "DM sam hi")
    await env.sms(DAVE, "DM bob hi")
    await env.drain()
    assert texts_to(env, DAVE)[-2] == "Which sam? 2) Sam Jones 3) Sam Smith. Text DM 2 your message."
    assert texts_to(env, DAVE)[-1] == "No member named bob. Text WHO for the list."


async def test_matrix_dm_to_ghost_and_r_reply(env):
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM][ALICE] = "Alice Cooper"
    ghost = env.port.ghost_mxid(DAVE)
    await env.matrix({"type": "m.room.member", "room_id": "!dmA:example.org", "sender": ALICE, "state_key": ghost,
                      "content": {"membership": "invite"}, "unsigned": {"invite_room_state": []}})
    env.port.rooms["!dmA:example.org"][ALICE] = "Alice Cooper"
    await env.matrix({"type": "m.room.message", "room_id": "!dmA:example.org", "sender": ALICE,
                      "content": {"msgtype": "m.text", "body": "you free?"}})
    await env.drain()
    assert texts_to(env, DAVE)[-1] == "DM from Alice Cooper: you free? (reply: R msg)"
    await env.sms(DAVE, "R yes after 5")
    assert env.port.sent[-1]["room_id"] == "!dmA:example.org"
    assert env.port.sent[-1]["content"]["body"] == "yes after 5"


async def test_encrypted_dm_invite_is_refused(env):
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM][ALICE] = "Alice"
    ghost = env.port.ghost_mxid(DAVE)
    await env.matrix({"type": "m.room.member", "room_id": "!enc:example.org", "sender": ALICE, "state_key": ghost,
                      "content": {"membership": "invite"},
                      "unsigned": {"invite_room_state": [{"type": "m.room.encryption", "content": {}}]}})
    assert ("!enc:example.org", ghost) in env.port.left
    assert await env.db.dm_room_info("!enc:example.org") is None


async def test_sms_to_sms_dm(env):
    await joined(env, DAVE, "Dave Miller")
    await joined(env, SUE, "Sue Lee")
    await env.sms(DAVE, "DM sue lunch?")
    await env.drain()
    assert texts_to(env, SUE) == ["DM from Dave Miller: lunch?"]
    await env.sms(SUE, "R sure")
    await env.drain()
    assert texts_to(env, DAVE)[-2:] == ["Sent to Sue Lee", "DM from Sue Lee: sure"] or \
        "DM from Sue Lee: sure" in texts_to(env, DAVE)


async def test_r_without_prior_dm_is_group_message(env):
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "r u coming")
    post = [e for e in env.port.sent if e["sender"] == env.port.ghost_mxid(DAVE)][-1]
    assert post["room_id"] == ROOM and post["content"]["body"] == "r u coming"


async def test_name_command(env):
    await joined(env, DAVE, "Dave")
    await env.sms(DAVE, "NAME Dave Miller")
    await env.drain()
    assert (await env.db.get_member(ROOM, DAVE)).display_name == "Dave Miller"
    assert env.port.displaynames[env.port.ghost_mxid(DAVE)] == "Dave Miller (SMS)"


# ---- polls ----------------------------------------------------------------------------

async def test_poll_flow(env):
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM][ALICE] = "Alice Cooper"
    start = {"type": "org.matrix.msc3381.poll.start", "sender": ALICE, "event_id": "$poll", "content": {
        "org.matrix.msc3381.poll.start": {"question": {"org.matrix.msc1767.text": "Saturday or Sunday?"},
                                          "answers": [{"id": "s1", "org.matrix.msc1767.text": "Saturday"},
                                                      {"id": "s2", "org.matrix.msc1767.text": "Sunday"}]}}}
    await env.matrix(start)
    await env.drain()
    assert texts_to(env, DAVE) == ["Example Chat poll from Alice: Saturday or Sunday? Reply VOTE 1 (Saturday), VOTE 2 (Sunday)."]
    await env.sms(DAVE, "vote 2")
    resp = env.port.sent[-1]
    assert resp["type"] == "org.matrix.msc3381.poll.response"
    assert resp["content"]["org.matrix.msc3381.poll.response"]["answers"] == ["s2"]
    await env.matrix({"type": "org.matrix.msc3381.poll.response", "sender": ALICE, "content": {
        "m.relates_to": {"rel_type": "m.reference", "event_id": "$poll"},
        "org.matrix.msc3381.poll.response": {"answers": ["s2"]}}})
    await env.sms(DAVE, "VOTE 5")
    await env.matrix({"type": "org.matrix.msc3381.poll.end", "sender": ALICE, "content": {
        "m.relates_to": {"rel_type": "m.reference", "event_id": "$poll"}}})
    await env.drain()
    msgs = texts_to(env, DAVE)
    assert "Vote recorded: Sunday" in msgs
    assert "Reply VOTE 1-2." in msgs
    assert msgs[-1] == "Example Chat poll results: Saturday or Sunday? Saturday 0, Sunday 2"
    await env.sms(DAVE, "vote 1")
    await env.drain()
    assert texts_to(env, DAVE)[-1] == "No open poll right now."


async def test_announce_with_poll(env):
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM][ADMIN] = "Evan Parker"
    await env.matrix({"type": "m.room.message", "sender": ADMIN, "content": {"msgtype": "m.text",
                      "body": "!announce Saturday 9am. Who's in? | yes | no"}})
    await env.drain()
    assert texts_to(env, DAVE) == ["ANNOUNCEMENT from Evan: Saturday 9am. Who's in? Reply VOTE 1 (yes), VOTE 2 (no)."]
    ann = [e for e in env.port.sent if e["sender"] == env.port.bot_mxid and "ANNOUNCEMENT" in e["content"].get("body", "")][0]
    assert ann["content"]["m.mentions"]["room"] is True and ann["event_id"] in env.port.pinned
    assert (await env.db.latest_open_poll(ROOM))["question"] == "Saturday 9am. Who's in?"


async def test_admin_commands_need_power(env):
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {"msgtype": "m.text", "body": f"!sms add {DAVE} Dave"}})
    assert env.port.sent[-1]["content"]["body"] == "Admins only."
    assert await env.db.get_member(ROOM, DAVE) is None


async def test_status_report(env):
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM][ALICE] = "Alice"
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {"msgtype": "m.text", "body": "hello"}})
    await env.drain()
    s = await env.bridge.status_text(ROOM)
    assert "1 opted_in" in s and "SMS out 3" in s and "Est. cost $" in s


# ---- queue robustness -----------------------------------------------------------------

async def test_interrupted_send_not_retried(env):
    await joined(env, DAVE, "Dave Miller")
    await env.db.enqueue(source_key="x", room_id=ROOM, from_number=GROUP_NUMBER, to_number=DAVE, kind="group", text="hi")
    await env.db.execute("UPDATE outbound SET status='sending' WHERE source_key='x'")
    assert await env.db.recover_interrupted_sends() == 1
    await env.drain()
    assert texts_to(env, DAVE) == []


async def test_transient_retry_then_success(env):
    await joined(env, DAVE, "Dave Miller")
    env.provider.fail_with = [SendError("transient", "boom")]
    await env.db.enqueue(source_key="y", room_id=ROOM, from_number=GROUP_NUMBER, to_number=DAVE, kind="group", text="hi")
    await env.drain()
    assert texts_to(env, DAVE) == []
    await env.db.execute("UPDATE outbound SET next_attempt_at=0 WHERE source_key='y'")
    await env.drain()
    assert texts_to(env, DAVE) == ["hi"]


async def test_per_minute_pacing(env):
    nums = [f"+1555555020{i}" for i in range(5)]
    for i, n in enumerate(nums):
        await joined(env, n, f"P{i}")
        env.provider.carriers[n] = "AT&T Wireless"
        await env.db.execute("UPDATE members SET carrier='AT&T Wireless' WHERE number=?", (n,))
    env.queue.limiter.classes["att"] = env.queue.limiter.classes["att"].__class__("att", ("at&t",), per_minute=3)
    env.queue.limiter.windows.clear()
    env.port.rooms[ROOM][ALICE] = "Alice"
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {"msgtype": "m.text", "body": "x"}})
    await env.drain()
    assert len(env.provider.sent) == 3
    pending = await env.db.fetchone("SELECT COUNT(*) AS n FROM outbound WHERE status='pending'")
    assert pending["n"] == 2


async def test_daily_cap_holds(env):
    await joined(env, DAVE, "Dave Miller")
    await env.db.execute("UPDATE members SET carrier='T-Mobile USA' WHERE number=?", (DAVE,))
    await env.db.add_daily_segments("tmobile", 1000)
    env.port.rooms[ROOM][ALICE] = "Alice"
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {"msgtype": "m.text", "body": "x"}})
    await env.drain()
    assert texts_to(env, DAVE) == []
    alerts = [e for e in env.port.sent if e["room_id"] == "!alerts:example.org"]
    assert alerts and "Daily segment cap" in alerts[-1]["content"]["body"]


async def test_old_messages_expire(env):
    await joined(env, DAVE, "Dave Miller")
    await env.db.enqueue(source_key="old", room_id=ROOM, from_number=GROUP_NUMBER, to_number=DAVE, kind="group", text="stale")
    await env.db.execute("UPDATE outbound SET created_at=? WHERE source_key='old'", (time.time() - 13 * 3600,))
    await env.drain()
    assert texts_to(env, DAVE) == []


async def test_delivery_failures_alert(env):
    await joined(env, DAVE, "Dave Miller")
    for i in range(3):
        await env.db.enqueue(source_key=f"f{i}", room_id=ROOM, from_number=GROUP_NUMBER, to_number=DAVE, kind="group", text="x")
    await env.drain()
    for row in await env.db.fetchall("SELECT provider_id FROM outbound WHERE source_key LIKE 'f%'"):
        body = json.dumps({"data": {"event_type": "message.finalized", "id": "e", "payload": {
            "id": row["provider_id"], "to": [{"phone_number": DAVE, "status": "delivery_failed"}]}}}).encode()
        await env.bridge.handle_provider_event(env.provider.parse(body))
    assert (await env.db.get_member(ROOM, DAVE)).consecutive_failures == 3
    alerts = [e for e in env.port.sent if e["room_id"] == "!alerts:example.org"]
    assert any("failed 3 deliveries" in a["content"]["body"] for a in alerts)


# ---- hardening ------------------------------------------------------------------------

async def test_non_member_cannot_reach_sms_members(env):
    await joined(env, DAVE, "Dave Miller")
    outsider = "@outsider:example.org"
    ghost = env.port.ghost_mxid(DAVE)
    await env.matrix({"type": "m.room.member", "room_id": "!dmX:example.org", "sender": outsider, "state_key": ghost,
                      "content": {"membership": "invite"}, "unsigned": {"invite_room_state": []}})
    assert ghost not in env.port.rooms.get("!dmX:example.org", {})
    assert await env.db.dm_room_info("!dmX:example.org") is None
    await env.matrix({"type": "m.room.message", "room_id": "!botdm:example.org", "sender": outsider,
                      "content": {"msgtype": "m.text", "body": "!dm Dave"}})
    assert env.port.sent[-1]["content"]["body"] == "Only group members can do that."


async def test_long_matrix_message_is_capped(env):
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM][ALICE] = "Alice"
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {"msgtype": "m.text", "body": "word " * 1000}})
    await env.drain()
    text = texts_to(env, DAVE)[0]
    assert text.endswith("(more in the app)") and len(text) < 850


async def test_gsm7_reply_format_uses_ascii_ellipsis(env):
    from smsgw.segments import count_segments
    await joined(env, DAVE, "Dave Miller")
    env.cfg.reply_quote_format = 'Re {name}: "{quote}" '
    env.port.rooms[ROOM][ALICE] = "Alice Cooper"
    env.port.rooms[ROOM][ADMIN] = "Evan Parker"
    parent = await env.port.send_text(ROOM, ADMIN, "Saturday at the park works for everyone?")
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {
        "msgtype": "m.text", "body": "I'm in", "m.relates_to": {"m.in_reply_to": {"event_id": parent}}}})
    await env.drain()
    text = texts_to(env, DAVE)[0]
    assert text == 'Alice Cooper: Re Evan: "Saturday at the p..." I\'m in'
    assert count_segments(text).encoding == "GSM-7"


async def test_inbox_retries_in_order_after_transient_failure(env, monkeypatch):
    import asyncio
    from smsgw import bridge as bridge_mod
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM][ALICE] = "Alice"
    for i in (1, 2):
        await env.db.add_matrix_event(f"$r{i}", ROOM, {"type": "m.room.message", "room_id": ROOM, "event_id": f"$r{i}",
                                                      "sender": ALICE, "content": {"msgtype": "m.text", "body": f"msg {i}"}})
    calls = {"n": 0}
    real = env.port.display_name

    async def flaky(room_id, mxid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("synapse restarting")
        return await real(room_id, mxid)

    env.port.display_name = flaky
    sleeps = []
    real_sleep = asyncio.sleep

    async def fast_sleep(d):
        sleeps.append(d)
        await real_sleep(0)

    monkeypatch.setattr(bridge_mod.asyncio, "sleep", fast_sleep)  # skip the real backoff delay
    task = asyncio.create_task(env.bridge.run_matrix())
    for _ in range(200):
        rows = await env.db.fetchall("SELECT processed FROM matrix_inbox WHERE event_id LIKE '$r%'")
        if all(r["processed"] == 1 for r in rows):
            break
        await real_sleep(0.01)
    env.bridge.stop()
    await asyncio.wait_for(task, 5)
    await env.drain()
    assert sleeps and texts_to(env, DAVE) == ["Alice: msg 1", "Alice: msg 2"]
