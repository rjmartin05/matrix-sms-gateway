"""Inbound SMS command parsing.

Two kinds of input are special; everything else is a normal group message:

1. Carrier/registered keywords, only when they are the *entire* message (case-insensitive, surrounding
   whitespace ignored, same rule Telnyx uses: developers.telnyx.com/docs/messaging/messages/
   configuration-and-usage): STOP-family, START/UNSTOP, HELP, JOIN.
2. Member commands with a "#" prefix ("/" accepted as an alias): #vote, #dm, #r, #who, #name,
   #photos, #help, #yes. An unknown "#word" gets "Unknown command. Text HELP for the list."

"#" followed by a non-letter ("#1 fan", "# of people") is a normal message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Telnyx's default opt-out set (advanced-opt-in-out docs). STOPALL / STOP ALL are Telnyx keywords too:
# Telnyx blocks on them, so the bridge must treat them as opt-outs rather than post them to the group.
OPT_OUT_KEYWORDS = frozenset({"STOP", "STOPALL", "STOP ALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"})
OPT_IN_KEYWORDS = frozenset({"START", "UNSTOP"})
HELP_KEYWORDS = frozenset({"HELP"})
JOIN_KEYWORDS = frozenset({"JOIN"})

NAME_MAX_LEN = 40
UNKNOWN = "Unknown command. Text HELP for the list."

_PREFIX_RE = re.compile(r"^\s*[#/]([A-Za-z]+)(.*)$", re.DOTALL)  # "#vote1" works too


@dataclass(frozen=True)
class Parsed:
    name: str
    args: dict = field(default_factory=dict)
    error: str | None = None  # reply with this instead of acting


def _norm(text: str) -> str:
    return " ".join(text.split()).upper()


def _rest(raw: str) -> str:
    return " ".join(raw.split())


def _vote(rest: str) -> Parsed:
    m = re.fullmatch(r"#?\s*(\d{1,2})", rest)
    if not m:
        return Parsed("VOTE", error="Reply #vote and a number, like #vote 1.")
    return Parsed("VOTE", {"choice": int(m.group(1))})


def _dm(rest: str) -> Parsed:
    if len(rest.split()) < 2:
        return Parsed("DM", error="Text #dm name your message. Text #who for the list.")
    return Parsed("DM", {"rest": rest})


def _reply(rest: str) -> Parsed:
    if not rest:
        return Parsed("R", error="Text #r and your message.")
    return Parsed("R", {"text": rest})


def _name(rest: str) -> Parsed:
    if not rest:
        return Parsed("NAME", error="Text #name and your name, like #name Dave Miller.")
    if len(rest) > NAME_MAX_LEN:
        return Parsed("NAME", error=f"Names can be up to {NAME_MAX_LEN} characters.")
    return Parsed("NAME", {"name": rest})


def _photos(rest: str) -> Parsed:
    v = rest.upper()
    if v in ("ON", "OFF"):
        return Parsed("PHOTOS", {"on": v == "ON"})
    return Parsed("PHOTOS", error="Text #photos off or #photos on.")


def _bare(name: str):
    def parse(rest: str) -> Parsed:
        return Parsed(name) if not rest else Parsed(name, error=UNKNOWN)

    return parse


# Prefixed member commands. Order is the HELP order.
PREFIXED = {
    "VOTE": _vote,
    "DM": _dm,
    "R": _reply,
    "WHO": _bare("WHO"),
    "NAME": _name,
    "PHOTOS": _photos,
    "HELP": _bare("HELP"),
    "YES": _bare("YES"),
}

HELP_HEADER = "Example Chat commands:"
HELP_LINES = [
    "#vote 1 - answer latest poll",
    "#dm name msg - private message",
    "#r msg - reply to last DM",
    "#who - list members",
    "#name Dave Miller - set your name",
    "#photos off / #photos on",
    "STOP - leave, START - rejoin",
]
YES_HELP_LINE = "#yes - send your pending DM"


def help_text(contact_email: str, *, dm_pending: bool = False) -> str:
    lines = [HELP_HEADER, *HELP_LINES]
    if dm_pending:
        lines.insert(3, YES_HELP_LINE)  # right after the #dm line
    lines.append(f"Questions: {contact_email}")
    return "\n".join(lines)


def parse(text: str) -> Parsed | None:
    """Return the parsed command/keyword, or None if this is a normal group message."""
    up = _norm(text)
    if not up:
        return None
    if up in OPT_OUT_KEYWORDS:
        return Parsed("STOP")
    if up in OPT_IN_KEYWORDS:
        return Parsed("START")
    if up in HELP_KEYWORDS:
        return Parsed("HELP")
    if up in JOIN_KEYWORDS:
        return Parsed("JOIN")
    m = _PREFIX_RE.match(text)
    if not m:
        return None
    word = m.group(1).upper()
    handler = PREFIXED.get(word)
    if handler is None:
        return Parsed("UNKNOWN", error=UNKNOWN)
    return handler(_rest(m.group(2)))
