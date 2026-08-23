"""Candidate byte-layouts for the cell-info record.

READ THIS BEFORE TRUSTING A NUMBER OUT OF THIS FILE.

The JK02 byte map has shifted between firmware revisions, and the layouts below
are transcribed from community reverse-engineering, not from a vendor spec. They
are *hypotheses*. Nothing in this package assumes one of them is correct:

* ``jkbms.verify`` scores a profile against a real captured frame using internal
  consistency (does the pack voltage equal the sum of the cells? does the BMS's
  own average-cell field equal the mean of the cell block?).
* ``jkbms.discover`` finds the cell block and pack-voltage field from first
  principles, with no profile at all.

The workflow is: capture a frame, run ``jkbms probe``, and let the evidence pick
the layout. Only then start logging.

Offsets are absolute byte positions from the start of the record (the ``0x55`` of
the header), so they can be read straight off a hexdump.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

__all__ = ["Kind", "Field", "Profile", "PROFILES", "get_profile"]

Kind = Literal["u8", "u16", "u32", "i16", "i32"]

_WIDTH: dict[str, int] = {"u8": 1, "u16": 2, "u32": 4, "i16": 2, "i32": 4}


@dataclass(frozen=True)
class Field:
    """One scalar in the record: where it is, how wide, and its unit scale."""

    offset: int
    kind: Kind
    #: Multiply the raw integer by this to reach the SI unit named in the
    #: profile attribute (volts, amps, watts, degrees C, amp-hours).
    scale: float = 1.0

    @property
    def width(self) -> int:
        return _WIDTH[self.kind]

    @property
    def end(self) -> int:
        return self.offset + self.width


@dataclass(frozen=True)
class Profile:
    """A complete candidate layout for one firmware family."""

    name: str
    description: str

    #: First byte of the contiguous little-endian uint16 millivolt cell block.
    cells_offset: int
    #: How many cell slots the block reserves (not how many are populated).
    cell_slots: int

    pack_v: Field
    current_a: Field
    soc_pct: Field | None = None
    power_w: Field | None = None
    remaining_ah: Field | None = None
    nominal_ah: Field | None = None
    cycles: Field | None = None
    temp_mos_c: Field | None = None
    temp1_c: Field | None = None
    temp2_c: Field | None = None

    #: The BMS's own computed aggregates. These are the load-bearing fields for
    #: verification: they must agree with the cell block if the layout is right.
    avg_cell_v: Field | None = None
    delta_cell_v: Field | None = None
    max_cell_index: Field | None = None
    min_cell_index: Field | None = None

    #: uint32 bitmask, one bit per populated cell slot.
    cell_enable_mask: Field | None = None
    #: Start of the per-cell resistance block (uint16, milliohms).
    resistance_offset: int | None = None

    @property
    def cells_end(self) -> int:
        return self.cells_offset + 2 * self.cell_slots

    #: Fields living after the per-cell blocks. A firmware revision that adds or
    #: removes one scalar ahead of these shifts all of them together, which is
    #: the most common way a documented map goes stale.
    TAIL_FIELDS = (
        "temp_mos_c", "pack_v", "power_w", "current_a", "temp1_c", "temp2_c",
        "soc_pct", "remaining_ah", "nominal_ah", "cycles",
    )

    def tail_shifted(self, delta: int, name: str | None = None) -> "Profile":
        """Return this profile with the post-cell-block fields moved ``delta`` bytes.

        The cell block, its enable mask and the BMS aggregate fields stay put:
        the cell array starts at byte 6 (4 header + type + counter) on every
        revision seen, and the aggregates are pinned to the end of the block by
        ``cell_slots``. ``jkbms probe`` scores a span of these variants so a
        stale tail offset shows up as evidence rather than as plausible-looking
        wrong numbers.
        """
        if delta == 0 and name is None:
            return self
        changes: dict[str, object] = {"name": name or f"{self.name}{delta:+d}"}
        for attr in self.TAIL_FIELDS:
            spec = getattr(self, attr)
            if spec is not None:
                changes[attr] = replace(spec, offset=spec.offset + delta)
        return replace(self, **changes)  # type: ignore[arg-type]


_MV = 1e-3          # millivolts -> volts, millidegrees of nothing, etc.
_MA = 1e-3          # milliamps -> amps
_MW = 1e-3          # milliwatts -> watts
_MAH = 1e-3         # milliamp-hours -> amp-hours
_DECI_C = 0.1       # tenths of a degree C -> degrees C


#: The 32-slot layout used by firmware from roughly v11 onward. This is the
#: family the JK-BD4A8S4P is reported to belong to.
JK02_32S = Profile(
    name="jk02_32s",
    description="Modern 32-cell-slot layout (firmware ~v11+). Expected default.",
    cells_offset=6,
    cell_slots=32,
    cell_enable_mask=Field(70, "u32"),
    avg_cell_v=Field(74, "u16", _MV),
    delta_cell_v=Field(76, "u16", _MV),
    max_cell_index=Field(78, "u8"),
    min_cell_index=Field(79, "u8"),
    resistance_offset=80,
    temp_mos_c=Field(144, "i16", _DECI_C),
    pack_v=Field(150, "u32", _MV),
    power_w=Field(154, "u32", _MW),
    current_a=Field(158, "i32", _MA),
    temp1_c=Field(162, "i16", _DECI_C),
    temp2_c=Field(164, "i16", _DECI_C),
    soc_pct=Field(173, "u8"),
    remaining_ah=Field(174, "u32", _MAH),
    nominal_ah=Field(178, "u32", _MAH),
    cycles=Field(182, "u32"),
)

#: The older 24-slot layout. Kept as a candidate so ``probe`` can rule it in or
#: out from evidence rather than assumption.
JK02_24S = Profile(
    name="jk02_24s",
    description="Older 24-cell-slot layout (firmware ~v10 and earlier).",
    cells_offset=6,
    cell_slots=24,
    cell_enable_mask=Field(54, "u32"),
    avg_cell_v=Field(58, "u16", _MV),
    delta_cell_v=Field(60, "u16", _MV),
    max_cell_index=Field(62, "u8"),
    min_cell_index=Field(63, "u8"),
    resistance_offset=64,
    temp_mos_c=Field(112, "i16", _DECI_C),
    pack_v=Field(118, "u32", _MV),
    power_w=Field(122, "u32", _MW),
    current_a=Field(126, "i32", _MA),
    temp1_c=Field(130, "i16", _DECI_C),
    temp2_c=Field(132, "i16", _DECI_C),
    soc_pct=Field(141, "u8"),
    remaining_ah=Field(142, "u32", _MAH),
    nominal_ah=Field(146, "u32", _MAH),
    cycles=Field(150, "u32"),
)

PROFILES: dict[str, Profile] = {p.name: p for p in (JK02_32S, JK02_24S)}


def get_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        known = ", ".join(sorted(PROFILES))
        raise KeyError(f"unknown profile {name!r}; known profiles: {known}") from None


def candidate_profiles(max_shift: int = 8) -> list[Profile]:
    """Every base profile plus small tail shifts, for the probe to score.

    A firmware that added or removed one field ahead of the pack-voltage block
    shifts the tail by a couple of bytes; enumerating those variants costs
    nothing and catches the most common near-miss.
    """
    out: list[Profile] = []
    for base in PROFILES.values():
        for delta in range(-max_shift, max_shift + 1):
            out.append(base.tail_shifted(delta) if delta else base)
    return out
