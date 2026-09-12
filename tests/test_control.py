"""Write-path safety.

These are the tests that matter most in the project: everything else produces
a wrong number, this produces a wrong battery protection setting. Each check
here corresponds to a way that could go wrong.
"""

from __future__ import annotations

import struct

import pytest

from jkbms.control import (SWITCH_REGISTERS, Register, WriteError, WritePlan,
                           build_write_command, get_register)
from jkbms.crc import check_sum8
from jkbms.settings import diff_frames


def settings_frame(**at_offset) -> bytes:
    raw = bytearray(300)
    raw[:4] = b"\x55\xAA\xEB\x90"
    raw[4] = 0x01
    for offset, value in at_offset.items():
        struct.pack_into("<I", raw, int(offset), value)
    raw[-1] = sum(raw[:-1]) & 0xFF
    return bytes(raw)


# ---------------------------------------------------------------- framing
def test_write_frame_is_a_valid_20_byte_command():
    frame = build_write_command(0x1D, 1)
    assert len(frame) == 20
    assert frame[:4] == b"\xAA\x55\x90\xEB"
    assert frame[4] == 0x1D
    assert struct.unpack_from("<I", frame, 6)[0] == 1
    assert check_sum8(frame)


def test_write_frame_uses_the_same_envelope_as_reads():
    """Reads are confirmed working on hardware, so reuse is the safe choice."""
    from jkbms.transports.ble_link import build_command
    assert build_write_command(0x96, 0) == build_command(0x96, 0)


def test_oversized_value_is_refused():
    with pytest.raises(WriteError, match="uint32"):
        build_write_command(0x1D, 2 ** 32)


# ---------------------------------------------------------------- refusals
def test_unverifiable_register_cannot_be_written():
    """No known settings offset means no read-back check, so no write."""
    blind = Register("mystery", 0x40, "unknown", verify_offset=None)
    with pytest.raises(WriteError, match="could not be verified"):
        WritePlan(blind, 1)


def test_protection_threshold_needs_explicit_acknowledgement():
    with pytest.raises(WriteError, match="protection threshold"):
        WritePlan(get_register("cell_ovp"), 4.2)


def test_protection_threshold_proceeds_once_acknowledged():
    plan = WritePlan(get_register("cell_ovp"), 4.2,
                     i_understand_this_changes_protection=True)
    assert plan.raw_value == 4200


def test_out_of_range_threshold_is_refused_even_when_acknowledged():
    """An acknowledgement is not permission to set something impossible."""
    with pytest.raises(WriteError, match="outside the accepted range"):
        WritePlan(get_register("cell_ovp"), 6.0,
                  i_understand_this_changes_protection=True)
    with pytest.raises(WriteError, match="outside the accepted range"):
        WritePlan(get_register("cell_uvp"), 0.5,
                  i_understand_this_changes_protection=True)


def test_unknown_setting_name_lists_the_known_ones():
    with pytest.raises(WriteError, match="Known:"):
        get_register("nonsense")


# ---------------------------------------------------------------- verification
def test_verified_when_exactly_the_intended_word_changes():
    plan = WritePlan(get_register("charge"), 1)
    before = settings_frame(**{"122": 0})
    after = settings_frame(**{"122": 1})
    ok, message = plan.check_result(before, after)
    assert ok, message
    assert "exactly as intended" in message


def test_flagged_when_nothing_changed():
    """The likeliest failure: the register number is wrong for this firmware."""
    plan = WritePlan(get_register("charge"), 1)
    frame = settings_frame(**{"122": 0})
    ok, message = plan.check_result(frame, frame)
    assert not ok
    assert "NOT APPLIED" in message
    assert "register number is probably wrong" in message


def test_flagged_when_some_other_field_moved():
    """A wrong register that lands elsewhere must be caught immediately."""
    plan = WritePlan(get_register("charge"), 1)
    before = settings_frame(**{"122": 0, "18": 4200})
    after = settings_frame(**{"122": 0, "18": 1})     # OVP clobbered instead
    ok, message = plan.check_result(before, after)
    assert not ok
    assert "UNEXPECTED CHANGE" in message
    assert "Restore from your settings backup" in message


def test_flagged_when_the_right_field_takes_the_wrong_value():
    plan = WritePlan(get_register("charge"), 1)
    before = settings_frame(**{"122": 0})
    after = settings_frame(**{"122": 7})
    ok, message = plan.check_result(before, after)
    assert not ok
    assert "APPLIED BUT WRONG" in message


def test_diff_finds_only_real_changes():
    a = settings_frame(**{"122": 0, "126": 1})
    b = settings_frame(**{"122": 1, "126": 1})
    assert diff_frames(a, b) == [(122, 0, 1)]


# ---------------------------------------------------------------- switches
def test_every_switch_is_verifiable():
    for reg in SWITCH_REGISTERS:
        assert reg.verifiable, f"{reg.name} has no verify offset"
        assert not reg.is_protection


def test_switches_only_accept_zero_or_one():
    for reg in SWITCH_REGISTERS:
        WritePlan(reg, 0)
        WritePlan(reg, 1)
        with pytest.raises(WriteError):
            WritePlan(reg, 2)
