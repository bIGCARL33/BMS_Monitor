"""Decoding and, more importantly, the verification that guards it."""

from __future__ import annotations

import pytest

from jkbms.decode import decode
from jkbms.frames import Frame
from jkbms.profiles import JK02_24S, JK02_32S, get_profile
from jkbms.verify import PackExpectation, verify

from .synth import build_cell_info_frame

CELLS = [3.912, 3.908, 3.915, 3.910]
EXPECT = PackExpectation(cell_count=4)


def reading_for(profile=JK02_32S, decode_with=None, **kwargs):
    frame = Frame(build_cell_info_frame(profile, kwargs.pop("cells", CELLS), **kwargs))
    return decode(frame, decode_with or profile)


def test_decodes_cell_voltages():
    reading = reading_for()
    assert reading.cell_count == 4
    assert reading.cells_v == pytest.approx(CELLS, abs=1e-3)


def test_decodes_scalars_with_correct_units():
    reading = reading_for(current_a=-2.5, soc_pct=78, temp1_c=24.5)
    assert reading.pack_v == pytest.approx(sum(CELLS), abs=1e-3)
    assert reading.current_a == pytest.approx(-2.5, abs=1e-3)
    assert reading.soc_pct == 78
    assert reading.temp1_c == pytest.approx(24.5, abs=0.05)
    assert reading.nominal_ah == pytest.approx(18.0, abs=1e-3)


def test_negative_current_survives_the_signed_decode():
    """Discharge must read negative, not as a huge positive uint32."""
    assert reading_for(current_a=-12.75).current_a == pytest.approx(-12.75, abs=1e-3)


def test_aggregates_are_computed_from_the_cell_block():
    reading = reading_for()
    assert reading.cell_min_v == pytest.approx(3.908)
    assert reading.cell_max_v == pytest.approx(3.915)
    assert reading.cell_delta_v == pytest.approx(0.007, abs=1e-4)
    assert reading.cell_sum_v == pytest.approx(sum(CELLS), abs=1e-3)


def test_resistances_follow_the_populated_slots():
    reading = reading_for(resistance_ohm=0.152)
    assert len(reading.resistances_ohm) == 4
    assert all(r == pytest.approx(0.152, abs=1e-3) for r in reading.resistances_ohm)


def test_empty_slots_are_not_reported_as_cells():
    """A 4S pack on a 32-slot board must decode as 4 cells, not 32."""
    assert reading_for().cell_count == 4


def test_eight_cells_on_the_same_board():
    cells = [3.90 + 0.001 * i for i in range(8)]
    reading = reading_for(cells=cells)
    assert reading.cell_count == 8


# ---------------------------------------------------------------- verify
def test_correct_profile_is_confirmed():
    result = verify(reading_for(), EXPECT)
    assert result.trustworthy, result.report()
    assert result.consistency_score == pytest.approx(1.0)


def test_wrong_profile_is_not_confirmed():
    """The whole point: decoding 32S data under the 24S map must not pass.

    This is the failure the brief warns about -- a stale offset yielding
    plausible-looking wrong numbers. Verification has to catch it.
    """
    result = verify(reading_for(decode_with=JK02_24S), EXPECT)
    assert not result.trustworthy, result.report()


def test_tail_shifted_profile_is_not_confirmed():
    """Even a two-byte tail shift must be rejected, not tolerated."""
    shifted = JK02_32S.tail_shifted(2)
    result = verify(reading_for(decode_with=shifted), EXPECT)
    assert not result.trustworthy, result.report()


def test_pack_voltage_disagreeing_with_cells_fails_the_check():
    """If pack_v does not equal the cell sum, something is wrong -- say so."""
    reading = reading_for(pack_v_override=25.0)   # cells sum to ~15.6
    result = verify(reading, EXPECT)
    assert not result.trustworthy
    assert any("pack_v == sum(cells)" in c.name for c in result.failures)


def test_unexpected_cell_count_is_flagged():
    reading = reading_for(cells=[3.9] * 8)
    result = verify(reading, PackExpectation(cell_count=4))
    assert any("cell count" in c.name for c in result.failures)


def test_cell_count_check_can_be_disabled():
    reading = reading_for(cells=[3.9] * 8)
    result = verify(reading, PackExpectation(cell_count=None))
    assert not any("cell count" in c.name for c in result.checks)


def test_power_check_is_skipped_at_negligible_current():
    """Near zero current the BMS's whole-watt rounding proves nothing."""
    result = verify(reading_for(current_a=0.0), EXPECT)
    assert not any("power" in c.name for c in result.checks)


def test_trustworthy_requires_real_evidence_not_just_plausibility():
    """A profile with no cross-field checks available must never be 'confirmed'."""
    from jkbms.verify import VerificationResult, Check
    result = VerificationResult(profile="x", checks=[
        Check("pack voltage in range", True, 1.0, "", kind="plausibility"),
        Check("soc in 0..100", True, 1.0, "", kind="plausibility"),
    ])
    assert not result.trustworthy


def test_get_profile_rejects_unknown_names():
    with pytest.raises(KeyError, match="unknown profile"):
        get_profile("nope")
