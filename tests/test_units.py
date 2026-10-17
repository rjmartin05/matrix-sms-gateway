import base64
import json
import time

import pytest

from smsgw import commands, compliance, names
from smsgw.bridge import normalize_number, parse_poll_start, strip_reply_fallback
from smsgw.provider.base import SignatureError
from smsgw.provider.telnyx import TelnyxProvider
from smsgw.segments import count_segments, is_gsm7, normalize_typography

EMAIL = "help@example.com"


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


def test_ucs2_from_emoji_and_typography():
    assert count_segments("hi \U0001F44D").encoding == "UCS-2"
    assert count_segments("Saturday\u2026 ok").encoding == "UCS-2"
    assert count_segments("x" * 70 + "\u00e9").encoding == "GSM-7"  # e-acute is in GSM-7
    assert count_segments("\u20ac").units == 2


def test_ucs2_segments_count_utf16_units():
    assert count_segments("\u0436" * 70).segments == 1
    assert count_segments("\u0436" * 71).segments == 2
    assert count_segments("\U0001F600" * 35).segments == 1  # 70 UTF-16 units
    assert count_segments("\U0001F600" * 36).segments == 2


# ---- typography normalization (D9) ----------------------------------------------------

def test_normalize_maps_smart_punctuation():
    assert normalize_typography("\u2018a\u2019 \u201cb\u201d c\u2013d e\u2014f g\u2026") == "'a' \"b\" c-d e-f g..."
    assert normalize_typography("a\u00a0b\u202fc\u200bd") == "a b cd"
    assert normalize_typography("\u2032 \u2033 \u00ab\u00bb \u2022 \u2212") == "' \" \"\" - -"


def test_normalize_leaves_emoji_and_real_unicode():
    family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"  # ZWJ sequence must survive
    assert normalize_typography(f"ok {family} \u2764\ufe0f") == f"ok {family} \u2764\ufe0f"
    assert normalize_typography("caf\u00e9 \u0436") == "caf\u00e9 \u0436"
    assert normalize_typography("don\u2019t \U0001F44D") == "don't \U0001F44D"


def test_ios_style_message_stays_gsm7_after_normalization():
    ios = "Don\u2019t forget \u2014 it\u2019s \u201cSaturday\u201d at 9\u2026 Sue\u2019s bringing chairs."
    assert count_segments(ios).encoding == "UCS-2"  # 70-char segments before
    fixed = normalize_typography(ios)
    info = count_segments(fixed)
    assert info.encoding == "GSM-7" and info.segments == 1
    assert fixed == "Don't forget - it's \"Saturday\" at 9... Sue's bringing chairs."


def test_long_ios_message_segment_savings():
    ios = ("We\u2019re meeting at the park \u2014 bring a chair if you can\u2026 " * 3).strip()
    assert count_segments(ios).segments == 3            # UCS-2: 67 per segment
    assert count_segments(normalize_typography(ios)).segments == 2  # GSM-7: 153 per segment


# ---- system texts ---------------------------------------------------------------------

def _all_system_texts():
    brand = "Example Chat"
    yield compliance.opt_in(brand)
    yield compliance.opt_in_followup(brand)
    yield compliance.opt_out(brand)
    yield compliance.help_registered(brand, EMAIL)
    yield commands.help_text(EMAIL)
    yield commands.help_text(EMAIL, dm_pending=True)
    yield commands.UNKNOWN
    for p in ("#vote x", "#dm dave", "#r", "#name", "#photos maybe", "#who me"):
        yield commands.parse(p).error


def test_system_texts_are_gsm7_basic_without_brackets():
    for text in _all_system_texts():
        assert is_gsm7(text), text
        assert not set(text) & set("[]{}\\^~|\u20ac"), text  # GSM-7 extension table chars


def test_help_text_exact():
    assert commands.help_text(EMAIL) == (
        "Example Chat commands:\n"
        "#vote 1 - answer latest poll\n"
        "#dm name msg - private message\n"
        "#r msg - reply to last DM\n"
        "#who - list members\n"
        "#name Dave Miller - set your name\n"
        "#photos off/on\n"
        "#avatar + photo - your pic\n"
        "STOP - leave, START - rejoin\n"
        "Questions: help@example.com"
    )
    assert count_segments(commands.help_text(EMAIL)).segments == 2


def test_help_lists_yes_only_while_pending():
    assert "#yes" not in commands.help_text(EMAIL)
    pending = commands.help_text(EMAIL, dm_pending=True)
    assert "#yes - send your pending DM" in pending
    assert count_segments(pending).segments == 2


def test_join_followup_mentions_commands():
    t = compliance.opt_in_followup("Example Chat")
    assert "STOP, CANCEL, END or QUIT" in t and "HELP" in t and "#" in t
    assert count_segments(t).segments == 1


def test_registered_texts_exact():
    assert compliance.opt_in("Example Chat") == (
        "Example Chat: You've joined the group text relay. Group messages will be sent to this number; "
        "reply to post to the group. Message frequency varies. Msg&data rates may apply. "
        "Reply HELP for help, STOP to opt out."
    )
    assert compliance.opt_out("Example Chat") == (
        "Example Chat: You are unsubscribed and will receive no further messages. Reply START to rejoin."
    )


# ---- commands -----------------------------------------------------------------------

@pytest.mark.parametrize("text,name", [
    ("STOP", "STOP"), (" stop ", "STOP"), ("Stop all", "STOP"), ("unsubscribe", "STOP"),
    ("cancel", "STOP"), ("END", "STOP"), ("quit", "STOP"), ("start", "START"), ("UNSTOP", "START"),
    ("join", "JOIN"), ("help", "HELP"), ("HELP ", "HELP"), ("info", "HELP"), (" INFO ", "HELP"),
    ("#who", "WHO"), ("/WHO", "WHO"), ("#vote 2", "VOTE"), ("#Vote #3", "VOTE"), ("#vote1", "VOTE"),
    ("#photos off", "PHOTOS"), ("/photos ON", "PHOTOS"), ("#dm Dave see you", "DM"), ("#r on my way", "R"),
    ("#name Dave Miller", "NAME"), ("#help", "HELP"), ("/help", "HELP"), ("#yes", "YES"), ("#YES", "YES"),
])
def test_commands_recognized(text, name):
    p = commands.parse(text)
    assert p is not None and p.name == name and p.error is None


@pytest.mark.parametrize("text", [
    # Bare words are always group messages now, except whole-message carrier keywords.
    "yes", "YES", "Yes!", "who", "WHO", "vote 2", "photos off", "DM Dave hi", "r u coming",
    "NAME Dave Miller", "info please", "more info?", "?", "commands", "name the place",
    "stop it lol", "please stop", "cancel saturday?", "who is driving?", "the end", "started yet?",
    "join us saturday", "help me move", "#1 fan of pizza", "# of people coming?", "",
])
def test_plain_messages_are_not_commands(text):
    assert commands.parse(text) is None


@pytest.mark.parametrize("text", ["#blessed", "#tbt great day", "/s", "#vot 1", "#info", "#commands"])
def test_unknown_hash_word(text):
    p = commands.parse(text)
    assert p.name == "UNKNOWN"
    assert p.error == "Unknown command, message not posted. Text it without the # to post it."
    assert count_segments(p.error).encoding == "GSM-7" and count_segments(p.error).segments == 1


def test_command_args():
    assert commands.parse("#vote 2").args == {"choice": 2}
    assert commands.parse("#name  dave   miller").args == {"name": "dave miller"}
    assert commands.parse("#name the place").args == {"name": "the place"}  # prefixed: always NAME
    assert commands.parse("#photos ON").args == {"on": True}
    assert commands.parse("#dm Dave see you there").args == {"rest": "Dave see you there"}
    assert commands.parse("#r  on my   way").args == {"text": "on my way"}


@pytest.mark.parametrize("text", ["#dm", "#dm Dave", "#vote", "#vote two", "#r", "#name", "#photos",
                                  "#photos maybe", "#who is here", "#yes please", "#help me"])
def test_malformed_prefixed_commands_reply_with_error(text):
    p = commands.parse(text)
    assert p is not None and p.error


# ---- name resolution ----------------------------------------------------------------

CANDS = [
    names.Candidate(1, "Alice Cooper", "alice", "@alice:x"),
    names.Candidate(4, "Dave Miller", "dave.miller", "@dave.miller:x"),
    names.Candidate(9, "Dave Smith", "dsmith", "@dsmith:x"),
    names.Candidate(12, "Evan Parker", "evan", "@evan:x"),
    names.Candidate(15, "Sue Lee", "", "@sms_1555:x"),
]


def test_exact_full_name_longest_match():
    r = names.resolve_dm("Dave Miller see you", CANDS)
    assert r.kind == "exact" and r.target.name == "Dave Miller" and r.message == "see you"
    r = names.resolve_dm("dave smith: lunch?", CANDS)
    assert r.kind == "exact" and r.target.name == "Dave Smith" and r.message == "lunch?"


def test_exact_unique_first_name_username_and_number():
    r = names.resolve_dm("sue lunch at noon?", CANDS)
    assert r.kind == "exact" and r.target.name == "Sue Lee" and r.message == "lunch at noon?"
    assert names.resolve_dm("dsmith yo", CANDS).target.name == "Dave Smith"
    assert names.resolve_dm("@evan hello", CANDS).target.name == "Evan Parker"
    r = names.resolve_dm("9 hello there", CANDS)
    assert r.kind == "exact" and r.target.name == "Dave Smith" and r.message == "hello there"


def test_shared_first_name_is_ambiguous():
    r = names.resolve_dm("Dave see you", CANDS)
    assert r.kind == "ambiguous" and r.target is None
    assert names.ambiguous_reply(r) == (
        "Which Dave? 4) Dave Miller 9) Dave Smith. Reply #dm 4 or #dm 9 with your message"
    )


def test_partial_first_name_never_auto_sends():
    r = names.resolve_dm("dav see you", CANDS)
    assert r.kind == "fuzzy" and r.target is None  # two Daves: list, no #yes
    assert names.fuzzy_reply(r) == (
        'No exact match for "dav". Did you mean 4) Dave Miller 9) Dave Smith? '
        "Reply #dm 4 or #dm 9 with your message"
    )
    r = names.resolve_dm("al hi there", CANDS)  # unique partial: still a suggestion, not a send
    assert r.kind == "fuzzy" and r.target.name == "Alice Cooper" and r.message == "hi there"
    assert names.fuzzy_reply(r) == 'No exact match for "al". Did you mean 1) Alice Cooper? Reply #yes to send'


def test_typos_suggest_with_damerau_levenshtein():
    r = names.resolve_dm("Evna running late", CANDS)  # transposition = 1 edit
    assert r.kind == "fuzzy" and r.target.name == "Evan Parker" and r.message == "running late"
    r = names.resolve_dm("Alcie Coper see you", CANDS)  # full-name typo, 2 edits, longer split wins
    assert r.kind == "fuzzy" and r.target.name == "Alice Cooper" and r.message == "see you"
    r = names.resolve_dm("Millr call me", CANDS)  # last name typo
    assert r.kind == "fuzzy" and r.target.name == "Dave Miller"


def test_no_plausible_match():
    r = names.resolve_dm("Bob hi", CANDS)
    assert r.kind == "none"
    assert names.none_reply(r) == 'No member named "Bob". Text #who for the list'
    assert names.resolve_dm("Zachary hi", CANDS).kind == "none"


def test_short_names_are_strict():
    # "Bo" vs "Sue": not plausible; 4-char limit allows only 1 edit.
    assert names.resolve_dm("Jo hi", CANDS).kind == "none"


def test_full_name_beats_shared_first_name():
    cands = CANDS + [names.Candidate(20, "Dave", "", "@dave:x")]
    r = names.resolve_dm("dave hi", cands)
    assert r.kind == "exact" and r.target.key == "@dave:x"


def test_damerau_levenshtein():
    assert names.damerau_levenshtein("evan", "evna") == 1
    assert names.damerau_levenshtein("dave", "dave") == 0
    assert names.damerau_levenshtein("kitten", "sitting") == 3


def test_mentions_exact_only():
    text = "hey @Dave Miller and @sue, see @nobody and @dav and @Dave"
    found = names.find_mentions(text, CANDS)
    assert [m.target.name for m in found] == ["Dave Miller", "Sue Lee"]
    assert text[found[0].start:found[0].end] == "@Dave Miller"
    assert text[found[1].start:found[1].end] == "@sue"


def test_mentions_ignore_who_numbers_and_typos():
    assert names.find_mentions("@4 hi @Evna", CANDS) == []
    assert [m.target.name for m in names.find_mentions("@dsmith hi", CANDS)] == ["Dave Smith"]


# ---- misc helpers -------------------------------------------------------------------

def test_normalize_number():
    assert normalize_number("(555) 555-1234") == "+15555551234"
    assert normalize_number("15555551234") == "+15555551234"
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
