"""Inbound SMS command parsing.

Every command has a strict *shape*. Text whose first word looks like a command but doesn't fit the
shape is treated as a normal group message ("Who is driving?" is not WHO, "vote for pizza" is not
VOTE). Opt-in/out/help keywords match Telnyx's rule: the whole message, case-insensitive, surrounding
whitespace ignored (developers.telnyx.com/docs/messaging/messages/configuration-and-usage).

"YES" is deliberately NOT a command: in a group chat it's a normal reply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

# Telnyx's default opt-out / opt-in keyword sets (advanced-opt-in-out docs).
OPT_OUT_KEYWORDS = frozenset({"STOP", "STOPALL", "STOP ALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"})
OPT_IN_KEYWORDS = frozenset({"START", "UNSTOP"})
HELP_KEYWORDS = frozenset({"HELP", "INFO", "?", "COMMANDS"})

NAME_MAX_LEN = 40
_NAME_RE = re.compile(r"^[^\W\d_][\w.'\- ]*$", re.UNICODE)


@dataclass(frozen=True)
class Parsed:
    name: str
    args: dict = field(default_factory=dict)
    error: str | None = None  # set when the shape is a command but unusable (reply with it)


@dataclass(frozen=True)
class Command:
    name: str
    parse: Callable[[str, str], Parsed | None]  # (normalized_upper, original) -> Parsed | None
    help_line: str | None = None


def _norm(text: str) -> str:
    return " ".join(text.split()).upper()


def _exact(name: str, keywords: frozenset[str]) -> Callable[[str, str], Parsed | None]:
    def parse(up: str, _orig: str) -> Parsed | None:
        return Parsed(name) if up in keywords else None

    return parse


_VOTE_RE = re.compile(r"^VOTE\s*#?\s*(\d{1,2})$")


def _parse_vote(up: str, _orig: str) -> Parsed | None:
    m = _VOTE_RE.match(up)
    return Parsed("VOTE", {"choice": int(m.group(1))}) if m else None


def _rest_after(word: str, orig: str) -> str | None:
    m = re.match(rf"^\s*{word}\s+(.*\S)\s*$", orig, re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else None


def _parse_dm(up: str, orig: str) -> Parsed | None:
    if up == "DM":
        return Parsed("DM", error="Text DM name your message. Text WHO for the list.")
    rest = _rest_after("DM", orig)
    if rest is None:
        return None
    words = rest.split()
    if len(words) < 2:
        return Parsed("DM", error="Text DM name your message. Text WHO for the list.")
    return Parsed("DM", {"rest": rest})


def _parse_reply(up: str, orig: str) -> Parsed | None:
    rest = _rest_after("R", orig)
    return Parsed("R", {"text": rest}) if rest is not None else None


def _parse_name(up: str, orig: str) -> Parsed | None:
    rest = _rest_after("NAME", orig)
    if rest is None:
        return None
    rest = " ".join(rest.split())
    # Keep this strict so "name the place" style chatter still reaches the group.
    if len(rest) > NAME_MAX_LEN or len(rest.split()) > 4 or not _NAME_RE.match(rest):
        return None
    return Parsed("NAME", {"name": rest})


def _parse_photos(up: str, _orig: str) -> Parsed | None:
    if up in ("PHOTOS OFF", "PHOTO OFF"):
        return Parsed("PHOTOS", {"on": False})
    if up in ("PHOTOS ON", "PHOTO ON"):
        return Parsed("PHOTOS", {"on": True})
    return None


# Order matters: first match wins, and help lines render in this order.
REGISTRY: list[Command] = [
    Command("STOP", _exact("STOP", OPT_OUT_KEYWORDS)),
    Command("START", _exact("START", OPT_IN_KEYWORDS)),
    Command("JOIN", _exact("JOIN", frozenset({"JOIN"}))),
    Command("HELP", _exact("HELP", HELP_KEYWORDS)),
    Command("VOTE", _parse_vote, "VOTE 1 - answer latest poll"),
    Command("DM", _parse_dm, "DM name msg - private message"),
    Command("R", _parse_reply, "R msg - reply to last DM"),
    Command("WHO", _exact("WHO", frozenset({"WHO"})), "WHO - list members"),
    Command("NAME", _parse_name, "NAME Dave Miller - set your name"),
    Command("PHOTOS", _parse_photos, "PHOTOS OFF/ON"),
]

HELP_HEADER = "Messaging Chat commands:"
HELP_FOOTER = "STOP - leave, START - rejoin"


def help_text(contact_email: str) -> str:
    lines = [HELP_HEADER]
    lines += [c.help_line for c in REGISTRY if c.help_line]
    lines.append(HELP_FOOTER)
    lines.append(f"Questions: {contact_email}")
    return "\n".join(lines)


def parse(text: str) -> Parsed | None:
    """Return the parsed command, or None if this is a normal group message."""
    up = _norm(text)
    if not up:
        return None
    for cmd in REGISTRY:
        result = cmd.parse(up, text)
        if result is not None:
            return result
    return None
