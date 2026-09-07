"""Turn a validated cell-info frame into a reading, given a layout profile."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .frames import Frame
from .profiles import Field, Profile

__all__ = ["Reading", "decode", "read_field"]

_FORMATS: dict[str, str] = {
    "u8": "<B", "u16": "<H", "u32": "<I", "i16": "<h", "i32": "<i",
}

#: A populated cell always sits inside this window. Anything outside it is an
#: empty slot or a misread, never a real lithium cell.
CELL_MIN_V = 0.5
CELL_MAX_V = 5.0


def read_field(raw: bytes, spec: Field | None) -> float | None:
    """Decode one scalar, or None if the field is absent or out of bounds."""
    if spec is None or spec.end > len(raw):
        return None
    (value,) = struct.unpack_from(_FORMATS[spec.kind], raw, spec.offset)
    return value * spec.scale


@dataclass
class Reading:
    """One decoded sample. Every field may be None if the profile omits it."""

    timestamp: datetime
    profile: str
    #: Voltages of the populated cell slots only, in slot order.
    cells_v: list[float] = field(default_factory=list)
    pack_v: float | None = None
    current_a: float | None = None
    power_w: float | None = None
    soc_pct: float | None = None
    remaining_ah: float | None = None
    nominal_ah: float | None = None
    cycles: float | None = None
    temp_mos_c: float | None = None
    temp1_c: float | None = None
    temp2_c: float | None = None
    #: The BMS's own aggregates, kept separate from ours so verification can
    #: compare the two instead of conflating them.
    bms_avg_cell_v: float | None = None
    bms_delta_cell_v: float | None = None
    resistances_ohm: list[float] = field(default_factory=list)
    raw: bytes = b""

    # -- aggregates computed from the cell block ---------------------------
    @property
    def cell_count(self) -> int:
        return len(self.cells_v)

    @property
    def cell_min_v(self) -> float | None:
        return min(self.cells_v) if self.cells_v else None

    @property
    def cell_max_v(self) -> float | None:
        return max(self.cells_v) if self.cells_v else None

    @property
    def cell_avg_v(self) -> float | None:
        return sum(self.cells_v) / len(self.cells_v) if self.cells_v else None

    @property
    def cell_delta_v(self) -> float | None:
        if not self.cells_v:
            return None
        return max(self.cells_v) - min(self.cells_v)

    @property
    def cell_sum_v(self) -> float | None:
        return sum(self.cells_v) if self.cells_v else None

    def __str__(self) -> str:
        parts = []
        if self.pack_v is not None:
            parts.append(f"{self.pack_v:6.3f} V")
        if self.current_a is not None:
            parts.append(f"{self.current_a:+7.3f} A")
        if self.soc_pct is not None:
            parts.append(f"SOC {self.soc_pct:3.0f}%")
        if self.cells_v:
            cells = " ".join(f"{v:.3f}" for v in self.cells_v)
            delta_mv = (self.cell_delta_v or 0.0) * 1000
            parts.append(f"[{cells}] d={delta_mv:.0f}mV")
        temps = [t for t in (self.temp1_c, self.temp2_c) if t is not None]
        if temps:
            parts.append("T " + "/".join(f"{t:.1f}C" for t in temps))
        return "  ".join(parts)


def _populated_cells(raw: bytes, profile: Profile) -> tuple[list[int], list[float]]:
    """Find the populated cell slots and their voltages.

    Returns ``(slot_indices, volts)``. The slot indices matter because the
    per-cell resistance block is indexed by slot, not by position among the
    populated cells -- on a partly-filled board those differ.

    The enable mask is used when it looks sane; otherwise we fall back to
    plausibility filtering. Empty slots read as 0 mV, so trailing zeros drop out
    either way -- but the mask also catches a gap in the middle, which a naive
    "stop at the first zero" scan would get wrong.
    """
    end = min(profile.cells_end, len(raw))
    if end <= profile.cells_offset:
        return [], []
    count = (end - profile.cells_offset) // 2
    raw_mv = list(struct.unpack_from(f"<{count}H", raw, profile.cells_offset))

    mask_val = read_field(raw, profile.cell_enable_mask)
    mask = int(mask_val) if mask_val is not None else None
    # bin().count() rather than int.bit_count(): the latter is 3.10+, and
    # JetPack 5 ships Python 3.8. Same answer, one interpreter generation wider.
    if mask is not None and 0 < bin(mask).count("1") <= count:
        slots = [i for i in range(count) if mask >> i & 1]
        volts = [raw_mv[i] / 1000.0 for i in slots]
        if volts and all(CELL_MIN_V <= v <= CELL_MAX_V for v in volts):
            return slots, volts

    slots = [i for i, mv in enumerate(raw_mv) if CELL_MIN_V <= mv / 1000.0 <= CELL_MAX_V]
    return slots, [raw_mv[i] / 1000.0 for i in slots]


def _resistances(raw: bytes, profile: Profile, slots: list[int]) -> list[float]:
    """Per-cell resistances in ohms, for the given slot indices."""
    start = profile.resistance_offset
    if start is None or not slots:
        return []
    if start + 2 * (max(slots) + 1) > len(raw):
        return []
    return [
        struct.unpack_from("<H", raw, start + 2 * slot)[0] / 1000.0 for slot in slots
    ]


def decode(frame: Frame, profile: Profile, *, timestamp: datetime | None = None) -> Reading:
    """Decode a cell-info frame under ``profile``.

    No validation happens here -- this will happily decode under a wrong
    profile. Run the result through :mod:`jkbms.verify` before believing it.
    """
    raw = frame.raw
    slots, cells = _populated_cells(raw, profile)
    return Reading(
        timestamp=timestamp or datetime.now(timezone.utc),
        profile=profile.name,
        cells_v=cells,
        pack_v=read_field(raw, profile.pack_v),
        current_a=read_field(raw, profile.current_a),
        power_w=read_field(raw, profile.power_w),
        soc_pct=read_field(raw, profile.soc_pct),
        remaining_ah=read_field(raw, profile.remaining_ah),
        nominal_ah=read_field(raw, profile.nominal_ah),
        cycles=read_field(raw, profile.cycles),
        temp_mos_c=read_field(raw, profile.temp_mos_c),
        temp1_c=read_field(raw, profile.temp1_c),
        temp2_c=read_field(raw, profile.temp2_c),
        bms_avg_cell_v=read_field(raw, profile.avg_cell_v),
        bms_delta_cell_v=read_field(raw, profile.delta_cell_v),
        resistances_ohm=_resistances(raw, profile, slots),
        raw=raw,
    )
