"""Name resolution for #dm targets and SMS @mentions.

Auto-send only on an *exact* match (case-insensitive): full display name, a first name that only one
member has, a username (Matrix localpart), or a WHO number. The longest exact match wins when
splitting the name from the message. Several exact matches are never guessed between.

If nothing matches exactly, #dm falls back to a typo-tolerant suggestion (Damerau-Levenshtein, plus
prefix matches like "dav"). Suggestions are never auto-sent: the member confirms with #yes or re-sends
with a WHO number. @mentions use exact matching only; anything else stays plain text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_NAME_WORDS = 4
MAX_SUGGESTIONS = 3
_TRAIL = ":,.!?;"


@dataclass(frozen=True)
class Candidate:
    number: int  # position in the WHO list (1-based)
    name: str  # display name, without the SMS suffix
    username: str  # Matrix localpart, or "" for none
    key: str  # opaque identifier (mxid)


@dataclass(frozen=True)
class Resolution:
    kind: str  # exact | ambiguous | fuzzy | none
    target: Candidate | None = None
    candidates: tuple[Candidate, ...] = ()
    message: str = ""
    query: str = ""


def _words(s: str) -> list[str]:
    return s.casefold().split()


def _clean(word: str) -> str:
    return word.casefold().lstrip("@").rstrip(_TRAIL)


def exact_matches(phrase_words: list[str], cands: list[Candidate], *, allow_number: bool = True) -> list[Candidate]:
    """Exact matches for the given (already split) words. Full name/username beat first name."""
    words = [_clean(w) for w in phrase_words]
    if not words or not all(words):
        return []
    phrase = " ".join(words)
    if allow_number and len(words) == 1 and phrase.isdigit():
        n = int(phrase)
        return [c for c in cands if c.number == n]
    strong = [
        c for c in cands
        if " ".join(_words(c.name)) == phrase or (c.username and c.username.casefold() == phrase)
    ]
    if strong:
        return strong
    if len(words) == 1:
        return [c for c in cands if _words(c.name) and _words(c.name)[0] == phrase]
    return []


def damerau_levenshtein(a: str, b: str) -> int:
    """Optimal string alignment distance (adjacent transpositions count as one edit)."""
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def _fuzzy_score(phrase: str, cand: Candidate) -> float | None:
    """Lower is better; None = not plausible. Prefix matches score 0.5, typos score their distance."""
    nw = _words(cand.name)
    targets = {" ".join(nw)}
    if nw:
        targets.add(nw[0])
        targets.add(nw[-1])
    if cand.username:
        targets.add(cand.username.casefold())
    limit = 1 if len(phrase) <= 4 else 2
    best: float | None = None
    for t in targets:
        if len(phrase) >= 2 and t.startswith(phrase):
            score = 0.5
        else:
            dist = damerau_levenshtein(phrase, t)
            if dist > limit:
                continue
            score = float(dist)
        best = score if best is None else min(best, score)
    return best


def resolve_dm(rest: str, cands: list[Candidate]) -> Resolution:
    """Split ``rest`` ("dave miller see you") into a target and a message."""
    words = rest.split()
    if len(words) < 2:
        return Resolution("none", query=words[0] if words else "")
    max_k = min(MAX_NAME_WORDS, len(words) - 1)
    for k in range(max_k, 0, -1):
        found = exact_matches(words[:k], cands)
        if not found:
            continue
        query = " ".join(words[:k]).lstrip("@")
        message = " ".join(words[k:])
        if len(found) == 1:
            return Resolution("exact", found[0], message=message, query=query)
        return Resolution("ambiguous", candidates=tuple(sorted(found, key=lambda c: c.number)),
                          message=message, query=query)

    # No exact match: suggestions only. The longest split with any plausible hit wins (so a typo'd
    # full name doesn't leave the last name in the message); within it, the best score wins.
    best: tuple[float, int] | None = None
    scored: dict[int, list[tuple[float, Candidate]]] = {}
    for k in range(min(3, len(words) - 1), 0, -1):
        phrase = " ".join(_clean(w) for w in words[:k])
        if not phrase or phrase.isdigit():
            continue
        hits = [(s, c) for c in cands if (s := _fuzzy_score(phrase, c)) is not None]
        if hits:
            scored[k] = hits
            best = (min(s for s, _ in hits), k)
            break
    if best is None:
        return Resolution("none", query=words[0].lstrip("@"))
    score, k = best
    tied = sorted((c for s, c in scored[k] if s == score), key=lambda c: c.number)[:MAX_SUGGESTIONS]
    return Resolution(
        "fuzzy",
        target=tied[0] if len(tied) == 1 else None,
        candidates=tuple(tied),
        message=" ".join(words[k:]),
        query=" ".join(words[:k]).lstrip("@"),
    )


def _dm_choices(cands: tuple[Candidate, ...]) -> str:
    opts = [f"#dm {c.number}" for c in cands]
    return opts[0] if len(opts) == 1 else ", ".join(opts[:-1]) + " or " + opts[-1]


def _listing(cands: tuple[Candidate, ...]) -> str:
    return " ".join(f"{c.number}) {c.name}" for c in cands)


def ambiguous_reply(res: Resolution) -> str:
    return f"Which {res.query}? {_listing(res.candidates)}. Reply {_dm_choices(res.candidates)} with your message"


def fuzzy_reply(res: Resolution) -> str:
    if res.target is not None:
        return f'No exact match for "{res.query}". Did you mean {_listing(res.candidates)}? Reply #yes to send'
    return (f'No exact match for "{res.query}". Did you mean {_listing(res.candidates)}? '
            f"Reply {_dm_choices(res.candidates)} with your message")


def none_reply(res: Resolution) -> str:
    return f'No member named "{res.query}". Text #who for the list'


_MENTION_RE = re.compile(r"(?<!\S)@(\S+)")


@dataclass(frozen=True)
class Mention:
    start: int
    end: int
    target: Candidate


def find_mentions(text: str, cands: list[Candidate]) -> list[Mention]:
    """Resolve ``@Name`` / ``@Full Name`` mentions that match exactly one member. Others stay plain text."""
    out: list[Mention] = []
    for m in _MENTION_RE.finditer(text):
        if out and m.start() < out[-1].end:
            continue
        tail_start = m.start() + 1
        tokens = list(re.finditer(r"\S+", text[tail_start:]))[:MAX_NAME_WORDS]
        for k in range(len(tokens), 0, -1):
            found = exact_matches([t.group() for t in tokens[:k]], cands, allow_number=False)
            if len(found) > 1:
                break  # ambiguous: leave as plain text
            if len(found) == 1:
                last = tokens[k - 1]
                end = tail_start + last.start() + len(last.group().rstrip(_TRAIL))
                out.append(Mention(m.start(), end, found[0]))
                break
    return out
