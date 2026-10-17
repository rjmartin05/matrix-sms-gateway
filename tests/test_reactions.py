"""Phone reaction texts -> Matrix reactions."""

from smsgw import reactions
from tests.conftest import ROOM
from tests.test_flows import ALICE, DAVE, SUE, joined, texts_to

# Verbatim from a live Google Messages webhook (2026-10-17).
GOOGLE_THUMBS = " ​👍​ to “ Ryan Martin: Sms test 3 ” "


def test_parse_google_sample():
    assert reactions.parse(GOOGLE_THUMBS) == reactions.Reaction(False, "👍", "Ryan Martin: Sms test 3")


def test_parse_ios_and_removals():
    assert reactions.parse("Loved “Alice: hi”") == reactions.Reaction(False, "❤️", "Alice: hi")
    assert reactions.parse("Laughed at \"Alice: hi\"") == reactions.Reaction(False, "😂", "Alice: hi")
    assert reactions.parse("Reacted 🎉 to “Alice: hi”") == reactions.Reaction(False, "🎉", "Alice: hi")
    assert reactions.parse("Removed a heart from “Alice: hi”") == reactions.Reaction(True, "❤️", "Alice: hi")
    assert reactions.parse("Removed 👍 from “Alice: hi”") == reactions.Reaction(True, "👍", "Alice: hi")
    # ZWJ emoji survive the invisible-character stripping.
    assert reactions.parse("👨‍👩‍👧 to “Alice: hi”").key == "👨‍👩‍👧"


def test_parse_ignores_ordinary_texts():
    for t in ["going to “Joe's” later", "Sms test 3", "to “x”", "👍", "I loved “Dune”, you?", "#vote 1"]:
        assert reactions.parse(t) is None, t


def test_quote_matching():
    assert reactions.quote_matches("Alice: hi", "Alice: hi")
    assert reactions.quote_matches("alice:  hi", "Alice: hi")
    assert reactions.quote_matches("Alice: this is a long message…", "Alice: this is a long message about Saturday")
    assert not reactions.quote_matches("Alice: hi…", "Alice: hi there")  # too short to trust a prefix
    assert not reactions.quote_matches("Alice: hi", "Alice: hi there")


async def _alice_says(env, body, event_id="$alice1"):
    env.port.rooms[ROOM][ALICE] = "Alice Cooper"
    evt = {"type": "m.room.message", "sender": ALICE, "event_id": event_id, "content": {"msgtype": "m.text", "body": body}}
    env.port.events[event_id] = dict(evt, room_id=ROOM)
    await env.matrix(evt)
    await env.drain()


def _reactions(env):
    return [e for e in env.port.sent if e["type"] == "m.reaction"]


def _posts_by(env, number):
    return [e for e in env.port.sent if e["type"] == "m.room.message" and e["sender"] == env.port.ghost_mxid(number)]


async def test_google_reaction_becomes_matrix_reaction(env):
    await joined(env, DAVE, "Dave Miller")
    await joined(env, SUE, "Sue")
    await _alice_says(env, "Sms test 3")
    assert texts_to(env, DAVE) == ["Alice Cooper: Sms test 3"]
    env.provider.sent.clear()

    await env.sms(DAVE, GOOGLE_THUMBS.replace("Ryan Martin", "Alice Cooper"))
    await env.echo()
    await env.drain()

    [r] = _reactions(env)
    assert r["sender"] == env.port.ghost_mxid(DAVE)
    assert r["content"]["m.relates_to"] == {"rel_type": "m.annotation", "event_id": "$alice1", "key": "👍"}
    assert _posts_by(env, DAVE) == []
    assert env.provider.sent == []  # reactions are never fanned out to SMS


async def test_unmatched_reaction_posts_as_text(env):
    await joined(env, DAVE, "Dave Miller")
    await _alice_says(env, "Sms test 3")
    await env.sms(DAVE, "👍 to “Somebody: something we never sent”")
    assert _reactions(env) == []
    assert _posts_by(env, DAVE)[-1]["content"]["body"] == "👍 to “Somebody: something we never sent”"


async def test_reaction_removal_redacts(env):
    await joined(env, DAVE, "Dave Miller")
    await _alice_says(env, "hi there")
    await env.sms(DAVE, "Loved “Alice Cooper: hi there”")
    [r] = _reactions(env)
    await env.sms(DAVE, "Removed a heart from “Alice Cooper: hi there”")
    assert env.port.redacted == [(env.port.ghost_mxid(DAVE), r["event_id"])]
    assert _posts_by(env, DAVE) == []
    # Removing again is a no-op, not a posted message.
    await env.sms(DAVE, "Removed a heart from “Alice Cooper: hi there”")
    assert len(env.port.redacted) == 1 and _posts_by(env, DAVE) == []


async def test_reaction_to_edit_targets_original(env):
    await joined(env, DAVE, "Dave Miller")
    await _alice_says(env, "Saturday", event_id="$orig")
    env.provider.sent.clear()
    edit = {"type": "m.room.message", "sender": ALICE, "event_id": "$edit",
            "content": {"msgtype": "m.text", "body": "* Sunday", "m.new_content": {"msgtype": "m.text", "body": "Sunday"},
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"}}}
    env.port.events["$edit"] = dict(edit, room_id=ROOM)
    await env.matrix(edit)
    await env.drain()
    [sent] = texts_to(env, DAVE)
    await env.sms(DAVE, f"👍 to “{sent}”")
    [r] = _reactions(env)
    assert r["content"]["m.relates_to"]["event_id"] == "$orig"


async def test_reaction_to_own_message(env):
    from tests.conftest import inbound_event
    await joined(env, DAVE, "Dave Miller")
    # Production persists every webhook in `inbound` before processing (web.py); do the same here.
    evt = env.provider.parse(inbound_event(DAVE, "Sms test1"))
    await env.db.add_inbound(evt.key, evt.event_type, evt.raw)
    await env.bridge.handle_provider_event(evt)
    [post] = _posts_by(env, DAVE)
    # Verbatim shape from a live Google Messages webhook (2026-10-17): reacting to your own text.
    await env.sms(DAVE, " ​😂​ to “ Sms test1 ” ")
    [r] = _reactions(env)
    assert r["content"]["m.relates_to"] == {"rel_type": "m.annotation", "event_id": post["event_id"], "key": "😂"}
    assert len(_posts_by(env, DAVE)) == 1
