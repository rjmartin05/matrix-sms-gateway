"""Recognize phone "reaction" texts and match them to the bridged message they quote.

Over plain SMS, phones send reactions as ordinary texts that quote the message reacted to.
Verified sample (Google Messages, 2026-10-17, from a live Telnyx webhook):

    '\\u200a\\u200b👍\\u200b to “\\u200aRyan Martin: Sms test 3\\u200a”\\u200a'

The iPhone formats ("Loved “…”", "Removed a heart from “…”", iOS 18 "Reacted 🎉 to “…”") and the
generic "Removed 👍 from “…”" are from memory, not verified against a live sample. A text that
doesn't match, or whose quote can't be found among recent outbound texts, is posted as a normal
message, so a wrong guess here costs nothing but a missed conversion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Invisible padding Google Messages wraps around the emoji and quote. Not U+200D (ZWJ) or U+FE0F:
# those are part of emoji sequences.
_INVISIBLE_RE = re.compile("[ ​‎‏⁠﻿]")
_Q = r"[“\"](?P<q>.+)[”\"]"

IOS_VERBS = {
    "loved": "❤️",
    "liked": "👍",
    "disliked": "👎",
    "laughed at": "😂",
    "emphasized": "‼️",
    "questioned": "❓",
}
IOS_NOUNS = {
    "heart": "❤️",
    "like": "👍",
    "dislike": "👎",
    "laugh": "😂",
    "exclamation": "‼️",
    "question mark": "❓",
}

_IOS_ADD = re.compile(rf"^(?P<v>{'|'.join(IOS_VERBS)})\s+{_Q}$", re.IGNORECASE | re.DOTALL)
_IOS_REMOVE = re.compile(rf"^removed an?\s+(?P<n>{'|'.join(IOS_NOUNS)})\s+from\s+{_Q}$", re.IGNORECASE | re.DOTALL)
_EMOJI_ADD = re.compile(rf"^(?:reacted\s+)?(?P<e>\S{{1,16}})\s+to\s+{_Q}$", re.IGNORECASE | re.DOTALL)
_EMOJI_REMOVE = re.compile(rf"^removed\s+(?P<e>\S{{1,16}})\s+from\s+{_Q}$", re.IGNORECASE | re.DOTALL)

_ELLIPSIS = ("…", "...")
MIN_PREFIX_CHARS = 12  # a truncated quote must be at least this long to match by prefix


@dataclass(frozen=True)
class Reaction:
    remove: bool
    key: str  # the emoji
    quote: str


def _is_emoji(token: str) -> bool:
    return not any(ch.isalnum() or ch in "\"“”'" for ch in token)


def parse(text: str) -> Reaction | None:
    clean = _INVISIBLE_RE.sub("", text).strip()
    if not clean or len(clean) > 2000:
        return None
    if m := _IOS_ADD.match(clean):
        return Reaction(False, IOS_VERBS[m["v"].lower()], m["q"].strip())
    if m := _IOS_REMOVE.match(clean):
        return Reaction(True, IOS_NOUNS[m["n"].lower()], m["q"].strip())
    if (m := _EMOJI_ADD.match(clean)) and _is_emoji(m["e"]):
        return Reaction(False, m["e"], m["q"].strip())
    if (m := _EMOJI_REMOVE.match(clean)) and _is_emoji(m["e"]):
        return Reaction(True, m["e"], m["q"].strip())
    return None


def _norm(s: str) -> str:
    s = _INVISIBLE_RE.sub("", s)
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return " ".join(s.split()).casefold()


def quote_matches(quote: str, sent_text: str) -> bool:
    q, t = _norm(quote), _norm(sent_text)
    if not q:
        return False
    if q == t:
        return True
    for e in _ELLIPSIS:
        if q.endswith(e):
            stem = q[: -len(e)].rstrip()
            return len(stem) >= MIN_PREFIX_CHARS and t.startswith(stem)
    return False
