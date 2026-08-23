"""Locate fields in an unknown frame layout from first principles.

This exists because the documented JK02 offsets have moved between firmware
revisions, and a stale offset yields plausible-looking wrong numbers -- worse
than no data. Rather than trusting a byte map, this module searches the frame
for structure that only the real fields can have:

* The cell block is a run of consecutive little-endian uint16 values that all
  sit in the lithium-cell voltage window and cluster tightly together. Random
  bytes essentially never do this for four or more slots in a row.
* The pack voltage is a uint32 elsewhere in the frame whose millivolt value
  matches the sum of that cell run.
* The average and delta fields are uint16 values matching the mean and spread
  of the run.

Findings are reported with the evidence that produced them, so they can be
cross-checked rather than taken on faith.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

__all__ = ["CellBlock", "Findings", "find_cell_blocks", "discover"]

#: A populated lithium cell lives here. Deliberately wide: narrowing this to the
#: expected chemistry would hide a genuinely odd reading we want to see.
_CELL_MIN_MV = 2000
_CELL_MAX_MV = 4500
#: Cells in one pack track each other. A run spanning more than this is more
#: likely coincidence than a real cell block.
_MAX_SPREAD_MV = 600


@dataclass(frozen=True)
class CellBlock:
    """A candidate run of cell voltages found in the frame."""

    offset: int
    count: int
    voltages_mv: tuple[int, ...]

    @property
    def sum_mv(self) -> int:
        return sum(self.voltages_mv)

    @property
    def mean_mv(self) -> float:
        return self.sum_mv / self.count

    @property
    def spread_mv(self) -> int:
        return max(self.voltages_mv) - min(self.voltages_mv)

    def __str__(self) -> str:
        volts = " ".join(f"{mv / 1000:.3f}" for mv in self.voltages_mv)
        return (f"offset {self.offset}: {self.count} cells [{volts}] "
                f"sum={self.sum_mv / 1000:.3f} V spread={self.spread_mv} mV")


@dataclass
class Findings:
    """Everything discovery could establish about one frame."""

    frame_len: int
    cell_blocks: list[CellBlock] = field(default_factory=list)
    #: offset -> value in volts, for uint32 fields matching the cell sum.
    pack_v_candidates: dict[int, float] = field(default_factory=dict)
    #: offset -> value in volts, for uint16 fields matching the cell mean.
    avg_cell_candidates: dict[int, float] = field(default_factory=dict)
    #: offset -> value in volts, for uint16 fields matching the cell spread.
    delta_cell_candidates: dict[int, float] = field(default_factory=dict)

    @property
    def best_block(self) -> CellBlock | None:
        """The longest, tightest run -- the most likely true cell block."""
        if not self.cell_blocks:
            return None
        return max(self.cell_blocks, key=lambda b: (b.count, -b.spread_mv))

    def report(self) -> str:
        lines = [f"frame length {self.frame_len} bytes"]
        if not self.cell_blocks:
            lines.append("  no cell-voltage run found -- is this a cell-info frame?")
            return "\n".join(lines)

        lines.append(f"  cell-block candidates ({len(self.cell_blocks)}):")
        for block in sorted(self.cell_blocks, key=lambda b: (-b.count, b.offset)):
            marker = " <- best" if block is self.best_block else ""
            lines.append(f"    {block}{marker}")

        def render(label: str, found: dict[int, float], unit: str) -> None:
            if found:
                items = ", ".join(f"offset {o} = {v:.3f} {unit}"
                                  for o, v in sorted(found.items()))
                lines.append(f"  {label}: {items}")
            else:
                lines.append(f"  {label}: none found")

        render("pack voltage (u32 matching cell sum)", self.pack_v_candidates, "V")
        render("average cell (u16 matching mean)", self.avg_cell_candidates, "V")
        render("delta cell (u16 matching spread)", self.delta_cell_candidates, "V")
        return "\n".join(lines)


def find_cell_blocks(raw: bytes, min_cells: int = 3) -> list[CellBlock]:
    """Find every maximal run of consecutive plausible cell voltages.

    Runs are searched on even offsets relative to each start, and only maximal
    runs are kept, so a 4-cell block is reported once rather than as several
    overlapping 3-cell fragments.
    """
    blocks: list[CellBlock] = []
    limit = len(raw) - 1
    offset = 0
    while offset < limit:
        values: list[int] = []
        cursor = offset
        while cursor + 2 <= len(raw):
            (mv,) = struct.unpack_from("<H", raw, cursor)
            if not _CELL_MIN_MV <= mv <= _CELL_MAX_MV:
                break
            candidate = values + [mv]
            if max(candidate) - min(candidate) > _MAX_SPREAD_MV:
                break
            values = candidate
            cursor += 2

        if len(values) >= min_cells:
            blocks.append(CellBlock(offset, len(values), tuple(values)))
            offset = cursor  # maximal run: don't re-report its own suffixes
        else:
            offset += 1
    return blocks


def _scan_u32(raw: bytes, target_mv: float, tol_mv: float,
              exclude: range) -> dict[int, float]:
    found: dict[int, float] = {}
    for off in range(len(raw) - 3):
        if off in exclude:
            continue
        (value,) = struct.unpack_from("<I", raw, off)
        if abs(value - target_mv) <= tol_mv:
            found[off] = value / 1000.0
    return found


def _scan_u16(raw: bytes, target_mv: float, tol_mv: float,
              exclude: range) -> dict[int, float]:
    found: dict[int, float] = {}
    for off in range(len(raw) - 1):
        if off in exclude:
            continue
        (value,) = struct.unpack_from("<H", raw, off)
        if abs(value - target_mv) <= tol_mv:
            found[off] = value / 1000.0
    return found


def discover(raw: bytes, min_cells: int = 3) -> Findings:
    """Search ``raw`` for the cell block and the fields derived from it."""
    findings = Findings(frame_len=len(raw), cell_blocks=find_cell_blocks(raw, min_cells))
    block = findings.best_block
    if block is None:
        return findings

    # Exclude the cell block itself: values inside it trivially "match".
    inside = range(block.offset, block.offset + 2 * block.count)

    findings.pack_v_candidates = _scan_u32(raw, block.sum_mv, 60, inside)
    findings.avg_cell_candidates = _scan_u16(raw, block.mean_mv, 6, inside)
    if block.spread_mv > 4:
        # A spread near zero matches far too many uint16s to mean anything.
        findings.delta_cell_candidates = _scan_u16(raw, block.spread_mv, 3, inside)
    return findings
