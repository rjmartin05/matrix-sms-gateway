"""Name matching for DM targets and SMS @mentions.

Matching is case-insensitive on whole words, against each member's display name (as a word prefix,
so "Dave" matches "Dave Miller") and username (localpart). The longest match wins; the remaining
words are the message. Ambiguity is never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    number: int  # position in the WHO list (1-based)
    name: str  # display name, without the SMS suffix
    username: str  # Matrix localpart, or "" for none
    key: str  # opaque identifier (mxid)


@dataclass(frozen=True)
class MatchResult:
    target: Candidate | None
    message: str = ""
    ambiguous: tuple[Candidate, ...] = ()
    query: str = ""  # the word(s) we tried to match, for error replies


def _words(s: str) -> list[str]:
    return s.lower().split()


def _clean(word: str) -> str:
    return word.lower().lstrip("@").rstrip(":,")


def _match_len(msg_words: list[str], cand: Candidate) -> tuple[int, bool]:
    """Return (words consumed, whether the whole name/username was consumed)."""
    best, complete = 0, False
    name_words = _words(cand.name)
    k = 0
    while (
        k < len(name_words)
        and k < len(msg_words)
        and _clean(msg_words[k]) == name_words[k].rstrip(":,")
    ):
        k += 1
    if k:
        best, complete = k, k == len(name_words)
    if cand.username and msg_words and _clean(msg_words[0]) == cand.username.lower():
        if best < 1 or (best == 1 and not complete):
            best, complete = 1, True
    return best, complete


def match(rest: str, candidates: list[Candidate], *, need_message: bool = True) -> MatchResult:
    words = rest.split()
    if not words:
        return MatchResult(None)
    max_k = len(words) - 1 if need_message else len(words)

    first = _clean(words[0])
    if first.isdigit():
        n = int(first)
        for c in candidates:
            if c.number == n:
                return MatchResult(c, " ".join(words[1:]), query=words[0])
        return MatchResult(None, query=words[0])

    scored: list[tuple[int, bool, Candidate]] = []
    for c in candidates:
        k, complete = _match_len(words, c)
        k_capped = min(k, max_k)
        if k_capped > 0:
            scored.append((k_capped, complete and k_capped == k, c))
    if not scored:
        return MatchResult(None, query=words[0].lstrip("@"))
    best = max(s[0] for s in scored)
    top = [s for s in scored if s[0] == best]
    if len(top) > 1:
        complete_only = [s for s in top if s[1]]
        if len(complete_only) == 1:
            top = complete_only
    query = " ".join(words[:best]).lstrip("@")
    if len(top) == 1:
        return MatchResult(top[0][2], " ".join(words[best:]), query=query)
    return MatchResult(None, ambiguous=tuple(sorted((s[2] for s in top), key=lambda c: c.number)), query=query)


def ambiguous_reply(result: MatchResult) -> str:
    opts = " ".join(f"{c.number}) {c.name}" for c in result.ambiguous)
    first = result.ambiguous[0].number
    return f"Which {result.query}? {opts}. Text DM {first} your message."


def none_reply(result: MatchResult) -> str:
    return f"No member named {result.query}. Text WHO for the list."


_MENTION_RE = re.compile(r"(?<!\S)@(\S+)")


@dataclass(frozen=True)
class Mention:
    start: int
    end: int
    target: Candidate


def find_mentions(text: str, candidates: list[Candidate]) -> list[Mention]:
    """Resolve unambiguous ``@Name`` / ``@Full Name`` mentions in an SMS body."""
    out: list[Mention] = []
    for m in _MENTION_RE.finditer(text):
        tail = text[m.start() + 1 :]
        res = match(tail, candidates, need_message=False)
        if res.target is None or res.query.isdigit():
            continue
        consumed = res.query
        # Find the end offset of the consumed words in the original text.
        words = tail.split()
        n = len(consumed.split())
        pos = 0
        for w in words[:n]:
            pos = tail.index(w, pos) + len(w)
        matched = tail[:pos].rstrip(":,.!?")
        end = m.start() + 1 + len(matched)
        if out and m.start() < out[-1].end:
            continue
        out.append(Mention(m.start(), end, res.target))
    return out
