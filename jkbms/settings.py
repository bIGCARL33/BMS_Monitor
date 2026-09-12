"""The settings record (frame type 0x01) -- reading, exporting, comparing.

This exists before any write support, deliberately, for three reasons.

**There is no config export.** JK's own software offers none; the usual advice
is to screenshot the settings page. A raw settings frame saved to a file is a
real export, and it is the only undo that will exist if a write goes wrong.

**A write can be verified.** Write, re-read, diff. If exactly the intended
field changed, the write did what it claimed. If something else moved, you find
out in seconds rather than at the next charge cycle. That loop is what makes
writing to a battery protection device defensible at all.

**The field map is not known.** Unlike the cell-info offsets -- confirmed
against real hardware by cross-field redundancy -- the settings layout here is
a *hypothesis* with weaker evidence behind it. So this module leads with
:func:`describe_words`, which decodes nothing and simply lays out every 32-bit
word with its offset and plausible interpretations. Identifying a field from
values you recognise on your own board beats trusting a table.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Sequence

from .frames import Frame

__all__ = [
    "SETTINGS_FRAME_TYPE", "Word", "describe_words", "diff_frames",
    "CANDIDATE_FIELDS", "guess_fields",
]

SETTINGS_FRAME_TYPE = 0x01

#: Candidate field names by byte offset. Offsets 6-138 line up field-by-field
#: (name, scale and unit) against syssi/esphome-jk-bms's decode_jk02_settings_
#: -- a maintained, widely-deployed implementation of this same JK02 protocol
#: -- cross-checked against this board's own dump: offset 78 there reads 400
#: raw (0.400 A), matching that project's "max balance current" field and this
#: project's own "0.4 A passive" balancer description in control.py; offset
#: 130 reads 40000 raw (40.000 Ah), which fits "nominal battery capacity" and
#: not a 0/1 switch. That is corroboration for THIS board, not confirmation on
#: this firmware -- still shown as guesses, and still only acted on via a
#: read-back diff.
CANDIDATE_FIELDS = {
    6: "smart sleep voltage?",
    10: "cell UVP?",
    14: "cell UVP recovery?",
    18: "cell OVP?",
    22: "cell OVP recovery?",
    26: "balance trigger voltage?",
    30: "SOC 100% voltage?",
    34: "SOC 0% voltage?",
    38: "cell request charge voltage?",
    42: "cell request float voltage?",
    46: "power off voltage?",
    50: "max charge current?",
    54: "charge OCP delay?",
    58: "charge OCP recovery?",
    62: "max discharge current?",
    66: "discharge OCP delay?",
    70: "discharge OCP recovery?",
    74: "short-circuit protection recovery time?",
    78: "max balance current? (spec: 0.4 A)",
    82: "charge OTP?",
    86: "charge OTP recovery?",
    90: "discharge OTP?",
    94: "discharge OTP recovery?",
    98: "charge UTP?",
    102: "charge UTP recovery?",
    106: "MOSFET OTP?",
    110: "MOSFET OTP recovery?",
    114: "cell count?",
    118: "charge switch?",
    122: "discharge switch?",
    126: "balancer switch?",
    130: "nominal battery capacity?",
    134: "short-circuit protection delay?",
    138: "start balance voltage?",
}


@dataclass(frozen=True)
class Word:
    """One 32-bit little-endian word of the settings frame."""

    offset: int
    raw: int
    guess: str = ""

    @property
    def as_volts(self) -> float:
        """The value read as millivolts."""
        return self.raw / 1000.0

    @property
    def as_amps(self) -> float:
        """The value read as milliamps."""
        return self.raw / 1000.0

    @property
    def looks_like_a_switch(self) -> bool:
        return self.raw in (0, 1)

    @property
    def looks_like_a_cell_voltage(self) -> bool:
        """A plausible per-cell threshold in millivolts."""
        return 1500 <= self.raw <= 4500

    @property
    def looks_like_a_small_count(self) -> bool:
        return 1 <= self.raw <= 32

    def annotation(self) -> str:
        """Every reading that is physically plausible, so the eye can pick."""
        notes = []
        if self.looks_like_a_switch:
            notes.append("switch(0/1)")
        if self.looks_like_a_cell_voltage:
            notes.append(f"{self.as_volts:.3f} V")
        if self.looks_like_a_small_count and not self.looks_like_a_switch:
            notes.append(f"count={self.raw}")
        if 100 <= self.raw <= 1_000_000:
            notes.append(f"{self.as_amps:.2f} A?")
        return "  ".join(notes)


def words(raw: bytes, start: int = 6, end: int | None = None) -> list:
    """Every aligned 32-bit word from ``start``, with candidate names attached."""
    stop = (len(raw) - 1 if end is None else end)
    out = []
    for offset in range(start, stop - 3, 4):
        (value,) = struct.unpack_from("<I", raw, offset)
        out.append(Word(offset, value, CANDIDATE_FIELDS.get(offset, "")))
    return out


def describe_words(frame: Frame | bytes, *, only_interesting: bool = False) -> str:
    """Lay out the settings frame word by word.

    Decodes nothing and asserts nothing. The point is to let someone who knows
    what their board is configured for recognise the values and so pin the
    offsets down from evidence.
    """
    raw = frame.raw if isinstance(frame, Frame) else frame
    lines = [f"settings frame, {len(raw)} bytes",
             "",
             f"{'offset':>6}  {'hex':>10}  {'value':>12}  {'plausible as':<28}  guess",
             "-" * 92]
    for word in words(raw):
        if only_interesting and word.raw == 0:
            continue
        lines.append(
            f"{word.offset:>6}  {word.raw:>10X}  {word.raw:>12}  "
            f"{word.annotation():<28}  {word.guess}")
    lines.append("")
    lines.append("Nothing above is confirmed. Match values you already know "
                 "(cell count, any\nthreshold you have set) to pin the offsets, "
                 "then verify by writing and\nre-reading -- never from the guess "
                 "column alone.")
    return "\n".join(lines)


def guess_fields(frame: Frame | bytes) -> dict:
    """Read the candidate fields as a plain dict. Values are unverified."""
    raw = frame.raw if isinstance(frame, Frame) else frame
    out = {}
    for offset, name in CANDIDATE_FIELDS.items():
        if offset + 4 <= len(raw):
            (value,) = struct.unpack_from("<I", raw, offset)
            out[name.rstrip("?")] = value
    return out


def diff_frames(before: bytes, after: bytes) -> list:
    """Byte offsets that changed between two settings frames.

    This is the verification half of any write: change one thing, re-read, and
    confirm that exactly one field moved and it was the intended one. Returns
    ``(offset, before_word, after_word)`` per changed 32-bit word.
    """
    changes = []
    limit = min(len(before), len(after))
    for offset in range(6, limit - 3, 4):
        (was,) = struct.unpack_from("<I", before, offset)
        (now,) = struct.unpack_from("<I", after, offset)
        if was != now:
            changes.append((offset, was, now))
    return changes


def render_diff(changes: Sequence) -> str:
    if not changes:
        return "no settings changed"
    lines = [f"{len(changes)} word(s) changed:"]
    for offset, was, now in changes:
        name = CANDIDATE_FIELDS.get(offset, "")
        lines.append(f"  offset {offset:>4}: {was} -> {now}"
                     + (f"   ({name})" if name else ""))
    return "\n".join(lines)
