"""End-to-end flows against fake Matrix + fake Telnyx."""

import io
import json
import time

from PIL import Image

from smsgw import compliance
from smsgw.provider.base import SendError
from tests.conftest import GROUP_NUMBER, ROOM

DAVE = "+17175550001"
SUE = "+17175550002"
STRANGER = "+17175559999"
ADMIN = "@ryan:example.org"
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
    assert texts_to(env, DAVE) == [compliance.opt_in("Messaging Chat"), compliance.opt_in_followup("Messaging Chat")]
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
        num = f"+1717555010{i}"
        await joined(env, num, f"Person {i}")
        await env.sms(num, kw, autoresponse="STOP")
        assert (await env.db.get_member(ROOM, num)).status == "opted_out", kw


async def test_stop_without_flag_sends_registered_confirmation_once(env):
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "stop")
    await env.drain()
    assert texts_to(env, DAVE) == [compliance.opt_out("Messaging Chat")]
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
    await env.sms(DAVE, "help")
    await env.sms(STRANGER, "help")
    await env.drain()
    assert texts_to(env, DAVE)[0].startswith("Messaging Chat commands:")
    assert texts_to(env, STRANGER) == [compliance.help_registered("Messaging Chat", "help@example.org")]


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
    env.port.rooms[ROOM][ADMIN] = "Ryan Martin"
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {
        "msgtype": "m.text", "body": "> <@ryan:example.org> Saturday...\n\nI'm in",
        "m.relates_to": {"m.in_reply_to": {"event_id": parent}}}})
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {
        "msgtype": "m.text", "body": "* I'm in!", "m.new_content": {"msgtype": "m.text", "body": "I'm in!"},
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$x"}}})
    await env.drain()
    assert texts_to(env, DAVE) == [
        'Alice Cooper: Re Ryan: "Saturday at the p..." I\'m in',
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
    await env.sms(SUE, "#photos off")
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
    assert sue["media_url"] is None and sue["text"] == "Alice sent a photo"
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
    await env.sms(DAVE, "#who")
    await env.drain()
    assert texts_to(env, DAVE)[-1] == "Members:\n1) Alice Cooper\n2) Dave Miller"
    await env.sms(DAVE, "#dm alice see you at 6")
    await env.drain()
    assert texts_to(env, DAVE)[-1] == "Sent to Alice Cooper"
    dm_room = await env.db.get_dm_room(env.port.ghost_mxid(DAVE), ALICE)
    assert dm_room and env.port.sent[-1]["room_id"] == dm_room and env.port.sent[-1]["content"]["body"] == "see you at 6"


async def test_dm_ambiguous_and_none(env):
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM]["@d1:example.org"] = "Sam Smith"
    env.port.rooms[ROOM]["@d2:example.org"] = "Sam Jones"
    await env.sms(DAVE, "#dm sam hi")
    await env.sms(DAVE, "#dm bob hi")
    await env.drain()
    assert texts_to(env, DAVE)[-2] == "Which sam? 2) Sam Jones 3) Sam Smith. Reply #dm 2 or #dm 3 with your message"
    assert texts_to(env, DAVE)[-1] == 'No member named "bob". Text #who for the list'


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
    assert texts_to(env, DAVE)[-1] == "DM from Alice Cooper: you free? (reply: #r msg)"
    await env.sms(DAVE, "#r yes after 5")
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
    await env.sms(DAVE, "#dm sue lunch?")
    await env.drain()
    assert texts_to(env, SUE) == ["DM from Dave Miller: lunch?"]
    await env.sms(SUE, "#r sure")
    await env.drain()
    assert texts_to(env, DAVE)[-2:] == ["Sent to Sue Lee", "DM from Sue Lee: sure"] or \
        "DM from Sue Lee: sure" in texts_to(env, DAVE)


async def test_bare_r_is_group_message_and_prefixed_r_needs_a_dm(env):
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "r u coming")
    post = [e for e in env.port.sent if e["sender"] == env.port.ghost_mxid(DAVE)][-1]
    assert post["room_id"] == ROOM and post["content"]["body"] == "r u coming"
    await env.sms(DAVE, "#r u coming")
    await env.drain()
    assert texts_to(env, DAVE)[-1].startswith("Nobody has sent you a DM yet.")


async def test_name_command(env):
    await joined(env, DAVE, "Dave")
    await env.sms(DAVE, "#name Dave Miller")
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
    assert texts_to(env, DAVE) == ["Messaging Chat poll from Alice: Saturday or Sunday? Reply #vote 1 (Saturday), #vote 2 (Sunday)."]
    await env.sms(DAVE, "#vote 2")
    resp = env.port.sent[-1]
    assert resp["type"] == "org.matrix.msc3381.poll.response"
    assert resp["content"]["org.matrix.msc3381.poll.response"]["answers"] == ["s2"]
    await env.matrix({"type": "org.matrix.msc3381.poll.response", "sender": ALICE, "content": {
        "m.relates_to": {"rel_type": "m.reference", "event_id": "$poll"},
        "org.matrix.msc3381.poll.response": {"answers": ["s2"]}}})
    await env.sms(DAVE, "#VOTE 5")
    await env.matrix({"type": "org.matrix.msc3381.poll.end", "sender": ALICE, "content": {
        "m.relates_to": {"rel_type": "m.reference", "event_id": "$poll"}}})
    await env.drain()
    msgs = texts_to(env, DAVE)
    assert "Vote recorded: Sunday" in msgs
    assert "Reply #vote 1 to #vote 2." in msgs
    assert msgs[-1] == "Messaging Chat poll results: Saturday or Sunday? Saturday 0, Sunday 2"
    await env.sms(DAVE, "/vote 1")
    await env.drain()
    assert texts_to(env, DAVE)[-1] == "No open poll right now."


async def test_announce_with_poll(env):
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM][ADMIN] = "Ryan Martin"
    await env.matrix({"type": "m.room.message", "sender": ADMIN, "content": {"msgtype": "m.text",
                      "body": "!announce Saturday 9am. Who's in? | yes | no"}})
    await env.drain()
    assert texts_to(env, DAVE) == ["ANNOUNCEMENT from Ryan: Saturday 9am. Who's in? Reply #vote 1 (yes), #vote 2 (no)."]
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
    nums = [f"+1717555020{i}" for i in range(5)]
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
    env.port.rooms[ROOM][ADMIN] = "Ryan Martin"
    parent = await env.port.send_text(ROOM, ADMIN, "Saturday at the park works for everyone?")
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {
        "msgtype": "m.text", "body": "I'm in", "m.relates_to": {"m.in_reply_to": {"event_id": parent}}}})
    await env.drain()
    text = texts_to(env, DAVE)[0]
    assert text == 'Alice Cooper: Re Ryan: "Saturday at the p..." I\'m in'
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


# ---- v1 review: # syntax, name resolution, spend, normalization -----------------------

def ghost_posts(env, number):
    return [e for e in env.port.sent if e["sender"] == env.port.ghost_mxid(number)]


async def test_bare_command_words_go_to_group(env):
    await joined(env, DAVE, "Dave Miller")
    for text in ("who", "vote 1", "photos off", "more info?", "?"):
        await env.sms(DAVE, text)
    await env.drain()
    assert [e["content"]["body"] for e in ghost_posts(env, DAVE)] == ["who", "vote 1", "photos off", "more info?", "?"]
    assert texts_to(env, DAVE) == []


async def test_unknown_hash_command_replies_and_is_not_posted(env):
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "#tbt great day")
    await env.drain()
    assert texts_to(env, DAVE) == ["Unknown command, message not posted. Text it without the # to post it."]
    assert ghost_posts(env, DAVE) == []


async def test_hash_help_matches_bare_help(env):
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "#help")
    await env.sms(DAVE, "HELP")
    await env.sms(DAVE, "info")
    await env.drain()
    a, b, c = texts_to(env, DAVE)
    assert a == b == c and a.startswith("Messaging Chat commands:\n#vote 1") and "#yes" not in a


async def setup_people(env):
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM][ALICE] = "Alice Cooper"
    env.port.rooms[ROOM]["@d1:example.org"] = "Sam Smith"
    env.port.rooms[ROOM]["@d2:example.org"] = "Sam Jones"


async def test_fuzzy_dm_held_then_sent_with_yes(env):
    await setup_people(env)
    await env.sms(DAVE, "#dm Alcie see you at 6")
    await env.drain()
    assert texts_to(env, DAVE)[-1] == 'No exact match for "Alcie". Did you mean 1) Alice Cooper? Reply #yes to send'
    assert await env.db.get_dm_room(env.port.ghost_mxid(DAVE), ALICE) is None  # nothing sent yet
    await env.sms(DAVE, "HELP")
    await env.drain()
    assert "#yes - send your pending DM" in texts_to(env, DAVE)[-1]
    await env.sms(DAVE, "#yes")
    await env.drain()
    dm_room = await env.db.get_dm_room(env.port.ghost_mxid(DAVE), ALICE)
    assert env.port.sent[-1]["room_id"] == dm_room and env.port.sent[-1]["content"]["body"] == "see you at 6"
    assert texts_to(env, DAVE)[-1] == "Sent to Alice Cooper"
    await env.sms(DAVE, "#yes")
    await env.drain()
    assert texts_to(env, DAVE)[-1] == "Nothing to send."


async def test_unique_partial_name_is_not_auto_sent(env):
    await setup_people(env)
    await env.sms(DAVE, "#dm ali hi")
    await env.drain()
    assert texts_to(env, DAVE)[-1].startswith('No exact match for "ali". Did you mean 1) Alice Cooper?')
    assert await env.db.get_dm_room(env.port.ghost_mxid(DAVE), ALICE) is None


async def test_new_dm_replaces_pending_and_pending_expires(env):
    await setup_people(env)
    await env.sms(DAVE, "#dm Alcie first message")
    await env.sms(DAVE, "#dm sam hi")  # ambiguous: replaces (clears) the pending one
    await env.sms(DAVE, "#yes")
    await env.drain()
    assert texts_to(env, DAVE)[-1] == "Nothing to send."
    await env.sms(DAVE, "#dm Alcie second")
    await env.db.execute("UPDATE pending_dms SET expires_at=0")
    await env.sms(DAVE, "#yes")
    await env.drain()
    assert texts_to(env, DAVE)[-1] == "Nothing to send."
    assert await env.db.get_dm_room(env.port.ghost_mxid(DAVE), ALICE) is None


async def test_multiple_fuzzy_candidates_listed_no_pending(env):
    await setup_people(env)
    await env.sms(DAVE, "#dm sma hi")
    await env.sms(DAVE, "#yes")
    await env.drain()
    msgs = texts_to(env, DAVE)
    assert msgs[-2] == ('No exact match for "sma". Did you mean 3) Sam Jones 4) Sam Smith? '
                        "Reply #dm 3 or #dm 4 with your message")
    assert msgs[-1] == "Nothing to send."


async def test_dm_by_who_number_and_self(env):
    await setup_people(env)
    await env.sms(DAVE, "#dm 1 by number")
    await env.sms(DAVE, "#dm Dave Miller note to self")
    await env.drain()
    assert texts_to(env, DAVE)[-2] == "Sent to Alice Cooper"
    assert texts_to(env, DAVE)[-1] == "That's you. Text #who for the list."


async def test_ambiguous_mention_stays_plain(env):
    await setup_people(env)
    await env.sms(DAVE, "@sam and @Alice Cooper, see you")
    post = ghost_posts(env, DAVE)[-1]
    assert post["content"]["m.mentions"]["user_ids"] == [ALICE]
    assert "@sam" in post["content"]["formatted_body"] and "@d1" not in post["content"]["formatted_body"]


async def test_outbound_typography_normalized(env):
    from smsgw.segments import count_segments
    await joined(env, DAVE, "Dave Miller")
    env.port.rooms[ROOM][ALICE] = "Alice"
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {
        "msgtype": "m.text", "body": "Don’t forget — it’s “Saturday”… \U0001F389"}})
    await env.drain()
    sent = texts_to(env, DAVE)[0]
    assert sent == "Alice: Don't forget - it's \"Saturday\"... \U0001F389"  # emoji untouched
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {
        "msgtype": "m.text", "body": "It’s at 9… bring chairs — thanks!"}})
    await env.drain()
    assert count_segments(texts_to(env, DAVE)[1]).encoding == "GSM-7"


async def test_spend_alert_once_at_60_percent(env):
    await joined(env, DAVE, "Dave Miller")
    env.cfg.pricing = env.cfg.pricing.__class__(daily_spend_limit=0.10)  # 60% = $0.06 ~ 8 SMS segments
    env.port.rooms[ROOM][ALICE] = "Alice"
    for i in range(12):
        await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {"msgtype": "m.text", "body": f"m{i}"}})
        await env.drain()
    alerts = [e["content"]["body"] for e in env.port.sent if e["room_id"] == "!alerts:example.org"]
    spend = [a for a in alerts if "Estimated Telnyx spend today" in a]
    assert len(spend) == 1 and "of the $0.10 daily limit" in spend[0]
    assert "Today (UTC): est. $" in await env.bridge.status_text(ROOM)


async def test_telnyx_spend_limit_holds_messages(env):
    await joined(env, DAVE, "Dave Miller")
    env.provider.fail_with = [SendError("spend_limit", "40333 Spend limit reached", "40333")]
    await env.db.enqueue(source_key="s", room_id=ROOM, from_number=GROUP_NUMBER, to_number=DAVE, kind="group", text="hi")
    await env.drain()
    row = await env.db.fetchone("SELECT status, attempts, next_attempt_at FROM outbound WHERE source_key='s'")
    assert row["status"] == "pending" and row["attempts"] == 0 and row["next_attempt_at"] > time.time() + 1000
    alerts = [e["content"]["body"] for e in env.port.sent if e["room_id"] == "!alerts:example.org"]
    assert any("spend limit reached" in a for a in alerts)


async def test_no_brackets_in_photo_off_and_link_texts(env):
    await joined(env, DAVE, "Dave Miller")
    await env.sms(DAVE, "#photos off")
    await env.drain()
    env.port.rooms[ROOM][ALICE] = "Alice"
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {"msgtype": "m.image", "body": "x.jpg"}})
    await env.matrix({"type": "m.room.message", "sender": ALICE, "content": {"msgtype": "m.video", "body": "v.mp4"}})
    await env.drain()
    for t in texts_to(env, DAVE):
        assert not set(t) & set("[]{}"), t
    assert texts_to(env, DAVE)[-2:] == ["Alice sent a photo", "Alice sent a video"]


async def test_sender_does_not_count_toward_shared_first_name(env):
    await joined(env, DAVE, "Dave Miller")
    await joined(env, SUE, "Dave Smith")
    await env.sms(DAVE, "#dm dave lunch?")
    await env.drain()
    assert texts_to(env, SUE) == ["DM from Dave Miller: lunch?"]
    assert texts_to(env, DAVE)[-1] == "Sent to Dave Smith"


async def test_bare_info_from_stranger_gets_registered_help(env):
    await env.sms(STRANGER, "INFO")
    await env.drain()
    assert texts_to(env, STRANGER) == [compliance.help_registered("Messaging Chat", "help@example.org")]
