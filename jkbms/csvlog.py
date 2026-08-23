"""CSV logging, shaped for import into the Battery Pack Test Analyzer workbook.

One row per sample, stable column order, no merged headers or units rows -- the
things that make a CSV awkward to import. Units are baked into the column names
so a spreadsheet never has to guess.

The cell columns are fixed at open time from the first reading, so the header
cannot drift mid-file if a cell momentarily reads out of range.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from .decode import Reading

__all__ = ["CsvLogger"]

#: Columns that always appear, in order, before the per-cell columns.
_LEADING = [
    "timestamp_iso", "elapsed_s", "pack_v", "current_a", "power_w",
    "soc_pct", "remaining_ah", "nominal_ah", "cycles",
    "temp_mos_c", "temp1_c", "temp2_c",
]

#: Columns that always appear after the per-cell columns.
_TRAILING = [
    "cell_count", "cell_min_v", "cell_max_v", "cell_avg_v", "cell_delta_mv",
    "cell_sum_v", "profile",
]


def _fmt(value: float | None, places: int) -> str:
    """Format a number for the sheet, or an empty cell if it is absent.

    Empty beats 0 here: a spreadsheet averaging a column of zeros silently
    produces a wrong answer, whereas blanks are skipped.
    """
    return "" if value is None else f"{value:.{places}f}"


@dataclass
class CsvLogger:
    """Append decoded readings to a CSV file."""

    path: Path
    #: Also write per-cell resistance columns when the BMS reports them.
    include_resistance: bool = False

    _handle: TextIO | None = field(default=None, init=False, repr=False)
    # csv.writer is a factory function, not a class, so there is no public type
    # to annotate this with.
    _writer: Any = field(default=None, init=False, repr=False)
    _cell_count: int = field(default=0, init=False)
    _t0: float | None = field(default=None, init=False)
    #: Decided once, when the header is written, so the row width can never
    #: disagree with the header if a later frame omits resistances.
    _has_resistance_cols: bool = field(default=False, init=False)
    rows_written: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    def _header(self) -> list[str]:
        cells = [f"cell{i + 1:02d}_v" for i in range(self._cell_count)]
        resistance = (
            [f"cell{i + 1:02d}_ohm" for i in range(self._cell_count)]
            if self._has_resistance_cols else []
        )
        return _LEADING + cells + resistance + _TRAILING

    def _open(self, reading: Reading) -> None:
        self._cell_count = reading.cell_count
        self._has_resistance_cols = bool(
            self.include_resistance and reading.resistances_ohm)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" is required on Windows or csv emits blank rows between records.
        self._handle = self.path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._handle)
        self._writer.writerow(self._header())
        self._t0 = reading.timestamp.timestamp()

    def write(self, reading: Reading) -> None:
        if self._writer is None:
            self._open(reading)
        assert self._writer is not None and self._t0 is not None

        cells = list(reading.cells_v[: self._cell_count])
        cells += [None] * (self._cell_count - len(cells))  # type: ignore[list-item]

        row: list[str] = [
            reading.timestamp.isoformat(timespec="milliseconds"),
            f"{reading.timestamp.timestamp() - self._t0:.3f}",
            _fmt(reading.pack_v, 3),
            _fmt(reading.current_a, 3),
            _fmt(reading.power_w, 2),
            _fmt(reading.soc_pct, 0),
            _fmt(reading.remaining_ah, 4),
            _fmt(reading.nominal_ah, 4),
            _fmt(reading.cycles, 0),
            _fmt(reading.temp_mos_c, 1),
            _fmt(reading.temp1_c, 1),
            _fmt(reading.temp2_c, 1),
        ]
        row += [_fmt(v, 4) for v in cells]

        if self._has_resistance_cols:
            res = list(reading.resistances_ohm[: self._cell_count])
            res += [None] * (self._cell_count - len(res))  # type: ignore[list-item]
            row += [_fmt(v, 4) for v in res]

        delta = reading.cell_delta_v
        row += [
            str(reading.cell_count),
            _fmt(reading.cell_min_v, 4),
            _fmt(reading.cell_max_v, 4),
            _fmt(reading.cell_avg_v, 4),
            _fmt(None if delta is None else delta * 1000, 1),
            _fmt(reading.cell_sum_v, 3),
            reading.profile,
        ]

        self._writer.writerow(row)
        self.rows_written += 1
        if self._handle is not None:
            # Flush every row: these runs are long and a crash or an unplugged
            # adapter should never cost the whole capture.
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
            self._writer = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()
