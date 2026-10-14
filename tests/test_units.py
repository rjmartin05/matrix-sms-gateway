import base64
import json
import time

import pytest

from smsgw import commands, compliance, names
from smsgw.bridge import normalize_number, parse_poll_start, strip_reply_fallback
from smsgw.provider.base import SignatureError
from smsgw.provider.telnyx import TelnyxProvider
from smsgw.segments import count_segments


# ---- segments -----------------------------------------------------------------------

def test_gsm7_single_and_multi():
    assert count_segments("a" * 160).segments == 1
    assert count_segments("a" * 161).segments == 2
    assert count_segments("a" * 306).segments == 2
    assert count_segments("a" * 307).segments == 3


def test_gsm7_extension_chars_cost_two():
    info = count_segments("{" * 80)
    assert info.encoding == "GSM-7" and info.units == 160 and info.segments == 1
    assert count_segments("{" * 81).segments == 2


def test_ucs2_from_emoji_and_reply_arrow():
    assert count_segments("hi 👍").encoding == "UCS-2"
    assert count_segments("↩ Dave: Saturday?… ok").encoding == "UCS-2"
    assert count_segments("x" * 70 + "é").encoding == "GSM-7"  # é is in GSM-7
    assert count_segments("€").units == 2


def test_ucs2_segments_count_utf16_units():
    assert count_segments("ж" * 70).segments == 1
    assert count_segments("ж" * 71).segments == 2
    assert count_segments("😀" * 35).segments == 1  # 70 UTF-16 units
    assert count_segments("😀" * 36).segments == 2


def test_system_prefixes_are_gsm7():
    brand = "Messaging Chat"
    for text in (
        compliance.opt_in(brand), compliance.opt_in_followup(brand), compliance.opt_out(brand),
        compliance.help_registered(brand, "ryan@ryanjmartin.net"), commands.help_text("ryan@ryanjmartin.net"),
    ):
        assert count_segments(text).encoding == "GSM-7", text


def test_help_text_matches_brief():
    assert commands.help_text("ryan@ryanjmartin.net") == (
        "Messaging Chat commands:\n"
        "VOTE 1 - answer latest poll\n"
        "DM name msg - private message\n"
        "R msg - reply to last DM\n"
        "WHO - list members\n"
        "NAME Dave Miller - set your name\n"
        "PHOTOS OFF/ON\n"
        "STOP - leave, START - rejoin\n"
        "Questions: ryan@ryanjmartin.net"
    )
    assert count_segments(commands.help_text("ryan@ryanjmartin.net")).segments == 2


def test_registered_texts_exact():
    assert compliance.opt_in("Messaging Chat") == (
        "Messaging Chat: You've joined the group text relay. Group messages will be sent to this number; "
        "reply to post to the group. Message frequency varies. Msg&data rates may apply. "
        "Reply HELP for help, STOP to opt out."
    )
    assert compliance.opt_out("Messaging Chat") == (
        "Messaging Chat: You are unsubscribed and will receive no further messages. Reply START to rejoin."
    )


# ---- commands -----------------------------------------------------------------------

@pytest.mark.parametrize("text,name", [
    ("STOP", "STOP"), (" stop ", "STOP"), ("Stop all", "STOP"), ("unsubscribe", "STOP"),
    ("cancel", "STOP"), ("END", "STOP"), ("quit", "STOP"), ("start", "START"), ("UNSTOP", "START"),
    ("join", "JOIN"), ("help", "HELP"), ("INFO", "HELP"), ("?", "HELP"), ("commands", "HELP"),
    ("who", "WHO"), ("VOTE 2", "VOTE"), ("vote #3", "VOTE"), ("photos off", "PHOTOS"),
    ("DM Dave see you", "DM"), ("r on my way", "R"), ("NAME Dave Miller", "NAME"),
])
def test_commands_recognized(text, name):
    p = commands.parse(text)
    assert p is not None and p.name == name


@pytest.mark.parametrize("text", [
    "yes", "YES", "Yes!", "stop it lol", "please stop", "cancel saturday?", "who is driving?",
    "vote for pizza", "photos from saturday are up", "the end", "started yet?",
    "join us saturday", "help me move", "",
])
def test_plain_messages_are_not_commands(text):
    assert commands.parse(text) is None


def test_known_ambiguity_name_command():
    # Documented: "NAME <2-4 words>" is always the command. The reply makes mistakes visible.
    assert commands.parse("Name the place").name == "NAME"
    assert commands.parse("name the place we should go on saturday night") is None  # >4 words


def test_vote_and_name_args():
    assert commands.parse("vote 2").args == {"choice": 2}
    assert commands.parse("NAME  dave   miller").args == {"name": "dave miller"}
    assert commands.parse("PHOTOS ON").args == {"on": True}


def test_dm_without_message_is_error():
    p = commands.parse("DM Dave")
    assert p.name == "DM" and p.error
    assert commands.parse("dm").error


# ---- names --------------------------------------------------------------------------

CANDS = [
    names.Candidate(1, "Dave Miller", "dave.miller", "@dave.miller:x"),
    names.Candidate(2, "Dave Smith", "dsmith", "@dsmith:x"),
    names.Candidate(3, "Ryan Martin", "ryan", "@ryan:x"),
    names.Candidate(4, "Sue", "", "@sms_1555:x"),
]


def test_longest_match_wins():
    r = names.match("Dave Miller see you", CANDS)
    assert r.target.name == "Dave Miller" and r.message == "see you"


def test_ambiguous_does_not_guess():
    r = names.match("dave see you", CANDS)
    assert r.target is None and [c.name for c in r.ambiguous] == ["Dave Miller", "Dave Smith"]
    assert names.ambiguous_reply(r) == "Which dave? 1) Dave Miller 2) Dave Smith. Text DM 1 your message."


def test_no_match():
    r = names.match("Bob hi", CANDS)
    assert r.target is None and not r.ambiguous
    assert names.none_reply(r) == "No member named Bob. Text WHO for the list."


def test_match_by_number_and_username():
    assert names.match("2 hello there", CANDS).target.name == "Dave Smith"
    r = names.match("ryan hello", CANDS)
    assert r.target.name == "Ryan Martin" and r.message == "hello"
    assert names.match("dsmith yo", CANDS).target.name == "Dave Smith"


def test_name_must_leave_a_message():
    r = names.match("Dave Miller", CANDS)
    # "Dave Miller" consumes everything; capped to one word -> ambiguous Dave.
    assert r.target is None


def test_mentions():
    found = names.find_mentions("hey @Dave Miller and @sue, see @nobody", CANDS)
    assert [(m.target.name) for m in found] == ["Dave Miller", "Sue"]
    text = "hey @Dave Miller and @sue, see @nobody"
    assert text[found[0].start:found[0].end] == "@Dave Miller"
    assert text[found[1].start:found[1].end] == "@sue"


# ---- misc helpers -------------------------------------------------------------------

def test_normalize_number():
    assert normalize_number("(717) 555-1234") == "+17175551234"
    assert normalize_number("17175551234") == "+17175551234"
    assert normalize_number("+44 20 7946 0958") == "+442079460958"
    assert normalize_number("12345") is None


def test_strip_reply_fallback():
    assert strip_reply_fallback("> <@a:x> parent\n> more\n\nreal reply") == "real reply"
    assert strip_reply_fallback("> quoting myself") == "> quoting myself"


def test_parse_polls_both_styles():
    stable = {"m.poll": {"question": {"m.text": [{"body": "Sat or Sun?"}]}, "max_selections": 1,
                         "answers": [{"m.id": "a", "m.text": [{"body": "Sat"}]}, {"m.id": "b", "m.text": [{"body": "Sun"}]}]}}
    assert parse_poll_start("m.poll.start", stable) == ("Sat or Sun?", [("a", "Sat"), ("b", "Sun")], 1, "stable")
    unstable = {"org.matrix.msc3381.poll.start": {"question": {"org.matrix.msc1767.text": "Q?"},
                "answers": [{"id": "x", "org.matrix.msc1767.text": "One"}, {"id": "y", "org.matrix.msc1767.text": "Two"}]}}
    assert parse_poll_start("org.matrix.msc3381.poll.start", unstable)[1] == [("x", "One"), ("y", "Two")]


# ---- Telnyx signature ---------------------------------------------------------------

def _signed(priv, body: bytes, ts: int | None = None):
    ts = str(ts or int(time.time()))
    sig = base64.b64encode(priv.sign(ts.encode() + b"|" + body)).decode()
    return {"telnyx-signature-ed25519": sig, "telnyx-timestamp": ts}


def test_signature_valid(keypair):
    priv, pub = keypair
    p = TelnyxProvider("k", pub)
    body = b'{"data":{}}'
    p.verify(body, _signed(priv, body))


def test_signature_tampered_body(keypair):
    priv, pub = keypair
    p = TelnyxProvider("k", pub)
    with pytest.raises(SignatureError):
        p.verify(b'{"data":{"x":1}}', _signed(priv, b'{"data":{}}'))


def test_signature_replay_window(keypair):
    priv, pub = keypair
    p = TelnyxProvider("k", pub)
    body = b"{}"
    with pytest.raises(SignatureError):
        p.verify(body, _signed(priv, body, int(time.time()) - 3600))


def test_signature_missing_headers(keypair):
    _, pub = keypair
    with pytest.raises(SignatureError):
        TelnyxProvider("k", pub).verify(b"{}", {})


def test_parse_delivery_event(keypair):
    _, pub = keypair
    body = json.dumps({"data": {"event_type": "message.finalized", "id": "e1", "payload": {
        "id": "m1", "to": [{"phone_number": "+15551112222", "status": "delivery_failed"}],
        "errors": [{"code": "40002", "title": "Blocked as spam"}]}}}).encode()
    evt = TelnyxProvider("k", pub).parse(body)
    assert evt.key == "message.finalized:m1"
    assert evt.delivery.failed and not evt.delivery.delivered and "40002" in evt.delivery.error
