"""Score a decoded reading for internal consistency.

The premise: a cell-info record carries redundant information. The BMS reports
the cell block *and* its own average, delta and pack voltage. If a layout is
correct those agree to within rounding. If an offset is stale they disagree
wildly -- which turns "is this profile right?" into a measurement rather than a
matter of trusting documentation.

Two classes of check:

* **Consistency** -- cross-field agreement. Strong evidence, because a wrong
  offset would have to land on a value that coincidentally matches a derived
  quantity. Passing these is close to proof.
* **Plausibility** -- is the value physically sane in isolation. Weak evidence:
  necessary, nowhere near sufficient. A garbage offset frequently lands on a
  plausible-looking number, which is exactly the failure mode we are guarding
  against.

Scores are weighted so a profile cannot win on plausibility alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .decode import Reading

__all__ = ["Check", "VerificationResult", "verify", "PackExpectation"]


@dataclass(frozen=True)
class PackExpectation:
    """What the operator knows about the pack, used to sanity-check a decode.

    Defaults describe the 4S4P HAKADI 21700 NMC pack this project targets.
    """

    cell_count: int | None = 4
    cell_v_min: float = 2.5
    cell_v_max: float = 4.35
    pack_v_min: float = 8.0
    pack_v_max: float = 34.0
    #: Plausible magnitude ceiling for current, amps.
    current_max_a: float = 200.0
    temp_min_c: float = -30.0
    temp_max_c: float = 90.0


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    weight: float
    detail: str
    #: Consistency checks compare two independently-located fields; plausibility
    #: checks look at one value in isolation.
    kind: str = "consistency"


@dataclass
class VerificationResult:
    profile: str
    checks: list[Check] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Fraction of applicable weight that passed, 0.0 to 1.0."""
        total = sum(c.weight for c in self.checks)
        if not total:
            return 0.0
        return sum(c.weight for c in self.checks if c.passed) / total

    @property
    def consistency_score(self) -> float:
        """Same, restricted to the cross-field checks that carry real evidence."""
        items = [c for c in self.checks if c.kind == "consistency"]
        total = sum(c.weight for c in items)
        if not total:
            return 0.0
        return sum(c.weight for c in items if c.passed) / total

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    @property
    def trustworthy(self) -> bool:
        """Every consistency check passed and at least two were applicable.

        Two independent cross-field agreements is the bar for calling a layout
        confirmed without reaching for a multimeter.
        """
        applicable = [c for c in self.checks if c.kind == "consistency"]
        return len(applicable) >= 2 and all(c.passed for c in applicable)

    def report(self) -> str:
        lines = [
            f"profile {self.profile}: score {self.score:.2f} "
            f"(consistency {self.consistency_score:.2f})"
            f"{'  CONFIRMED' if self.trustworthy else ''}"
        ]
        for c in self.checks:
            lines.append(f"  [{'pass' if c.passed else 'FAIL'}] {c.name}: {c.detail}")
        return "\n".join(lines)


def _close(a: float, b: float, *, abs_tol: float, rel_tol: float = 0.0) -> bool:
    return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b)))


def verify(reading: Reading, expect: PackExpectation | None = None) -> VerificationResult:
    """Run every applicable check against ``reading``."""
    exp = expect or PackExpectation()
    result = VerificationResult(profile=reading.profile)
    add = result.checks.append

    cells = reading.cells_v
    cell_sum = reading.cell_sum_v

    # --- consistency: the checks that actually discriminate ----------------
    if cell_sum is not None and reading.pack_v is not None:
        ok = _close(cell_sum, reading.pack_v, abs_tol=0.15, rel_tol=0.02)
        add(Check(
            "pack_v == sum(cells)", ok, 4.0,
            f"sum(cells)={cell_sum:.3f} V vs pack_v={reading.pack_v:.3f} V "
            f"(diff {cell_sum - reading.pack_v:+.3f} V)",
        ))

    if cells and reading.bms_avg_cell_v is not None:
        mean = sum(cells) / len(cells)
        ok = _close(mean, reading.bms_avg_cell_v, abs_tol=0.006)
        add(Check(
            "bms avg == mean(cells)", ok, 3.0,
            f"mean={mean:.4f} V vs bms_avg={reading.bms_avg_cell_v:.4f} V",
        ))

    if cells and reading.bms_delta_cell_v is not None:
        delta = max(cells) - min(cells)
        ok = _close(delta, reading.bms_delta_cell_v, abs_tol=0.006)
        add(Check(
            "bms delta == max-min(cells)", ok, 3.0,
            f"delta={delta * 1000:.1f} mV vs bms_delta="
            f"{reading.bms_delta_cell_v * 1000:.1f} mV",
        ))

    if (reading.power_w is not None and reading.pack_v is not None
            and reading.current_a is not None):
        expected = abs(reading.pack_v * reading.current_a)
        # Skip near zero current: the BMS rounds power to whole watts, so at low
        # current the comparison is dominated by quantisation and proves nothing.
        if expected > 2.0:
            ok = _close(abs(reading.power_w), expected, abs_tol=1.5, rel_tol=0.06)
            add(Check(
                "power == |V*I|", ok, 2.0,
                f"|V*I|={expected:.1f} W vs power={abs(reading.power_w):.1f} W",
            ))

    if exp.cell_count is not None and cells:
        ok = len(cells) == exp.cell_count
        add(Check(
            "cell count matches pack", ok, 2.0,
            f"decoded {len(cells)} cells, expected {exp.cell_count}",
        ))

    # --- plausibility: necessary, not sufficient ---------------------------
    if cells:
        bad = [v for v in cells if not exp.cell_v_min <= v <= exp.cell_v_max]
        add(Check(
            "cell voltages in range", not bad, 1.0,
            f"{len(cells)} cells in "
            f"[{min(cells):.3f}, {max(cells):.3f}] V"
            + (f"; out of range: {bad}" if bad else ""),
            kind="plausibility",
        ))

    if reading.pack_v is not None:
        ok = exp.pack_v_min <= reading.pack_v <= exp.pack_v_max
        add(Check("pack voltage in range", ok, 1.0,
                  f"{reading.pack_v:.3f} V", kind="plausibility"))

    if reading.current_a is not None:
        ok = abs(reading.current_a) <= exp.current_max_a
        add(Check("current in range", ok, 1.0,
                  f"{reading.current_a:+.3f} A", kind="plausibility"))

    if reading.soc_pct is not None:
        ok = 0 <= reading.soc_pct <= 100
        add(Check("soc in 0..100", ok, 1.0,
                  f"{reading.soc_pct:.0f} %", kind="plausibility"))

    for label, value in (("temp1", reading.temp1_c), ("temp2", reading.temp2_c),
                         ("temp_mos", reading.temp_mos_c)):
        if value is None:
            continue
        ok = exp.temp_min_c <= value <= exp.temp_max_c
        add(Check(f"{label} in range", ok, 0.5,
                  f"{value:.1f} C", kind="plausibility"))

    return result
