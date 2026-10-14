"""SMS segment counting (GSM 03.38 7-bit vs UCS-2)."""

from __future__ import annotations

from dataclasses import dataclass

# GSM 03.38 basic character set.
GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# Extension table: each costs two septets (escape + char).
GSM7_EXT = frozenset("^{}\\[~]|€\f")


@dataclass(frozen=True)
class SegmentInfo:
    encoding: str  # "GSM-7" or "UCS-2"
    units: int  # septets for GSM-7, UTF-16 code units for UCS-2
    segments: int


def is_gsm7(text: str) -> bool:
    return all(c in GSM7_BASIC or c in GSM7_EXT for c in text)


def count_segments(text: str) -> SegmentInfo:
    if not text:
        return SegmentInfo("GSM-7", 0, 1)
    if is_gsm7(text):
        units = sum(2 if c in GSM7_EXT else 1 for c in text)
        single, multi, enc = 160, 153, "GSM-7"
    else:
        units = len(text.encode("utf-16-le")) // 2
        single, multi, enc = 70, 67, "UCS-2"
    if units <= single:
        return SegmentInfo(enc, units, 1)
    return SegmentInfo(enc, units, -(-units // multi))


def truncate(text: str, limit: int, ellipsis: str = "...") -> str:
    """Trim to ``limit`` characters (whitespace-collapsed), appending ``ellipsis`` if cut."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(ellipsis))].rstrip() + ellipsis
