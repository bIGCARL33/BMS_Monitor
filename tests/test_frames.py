"""Framing: sync, checksum validation, resync after noise, split reads."""

from __future__ import annotations

from jkbms.frames import CANDIDATE_LENGTHS, Frame, FrameAssembler, FrameType
from jkbms.profiles import JK02_32S

from .synth import build_cell_info_frame

CELLS = [3.912, 3.908, 3.915, 3.910]


def a_frame(**kwargs) -> bytes:
    return build_cell_info_frame(JK02_32S, CELLS, **kwargs)


def test_assembles_a_single_frame():
    frames = FrameAssembler().feed(a_frame())
    assert len(frames) == 1
    assert frames[0].is_cell_info
    assert frames[0].type_name == "CELL_INFO"
    assert len(frames[0]) == 300


def test_frame_counter_is_exposed():
    frames = FrameAssembler().feed(a_frame(counter=42))
    assert frames[0].counter == 42


def test_leading_noise_is_discarded():
    asm = FrameAssembler()
    frames = asm.feed(b"\x00\x11\x22garbage" + a_frame())
    assert len(frames) == 1
    assert asm.discarded == len(b"\x00\x11\x22garbage")


def test_frame_split_across_many_reads():
    """Bytes arrive in whatever chunks the OS feels like; framing must not care."""
    data = a_frame()
    asm = FrameAssembler()
    collected = []
    for start in range(0, len(data), 7):
        collected.extend(asm.feed(data[start : start + 7]))
    assert len(collected) == 1


def test_header_split_across_a_read_boundary():
    data = a_frame()
    asm = FrameAssembler()
    assert asm.feed(data[:2]) == []
    assert len(asm.feed(data[2:])) == 1


def test_back_to_back_frames():
    asm = FrameAssembler()
    frames = asm.feed(a_frame(counter=1) + a_frame(counter=2))
    assert [f.counter for f in frames] == [1, 2]


def test_corrupt_checksum_is_rejected():
    data = bytearray(a_frame())
    data[100] ^= 0xFF          # breaks the trailing sum8
    assert FrameAssembler().feed(bytes(data)) == []


def test_recovers_after_a_corrupt_frame():
    """A bad record must not poison the stream -- the next good one still lands."""
    bad = bytearray(a_frame(counter=1))
    bad[100] ^= 0xFF
    good = a_frame(counter=2)
    asm = FrameAssembler()
    frames = asm.feed(bytes(bad) + good + good)
    assert frames, "assembler never resynchronised after corruption"
    assert all(f.counter == 2 for f in frames)


def test_alternate_record_length_is_accepted():
    """Length is discovered by checksum, not assumed."""
    assert 320 in CANDIDATE_LENGTHS
    frames = FrameAssembler().feed(a_frame(length=320))
    assert len(frames) == 1
    assert len(frames[0]) == 320


def test_buffer_does_not_grow_without_bound_on_pure_noise():
    asm = FrameAssembler()
    for _ in range(50):
        asm.feed(b"\xDE\xAD\xBE\xEF" * 32)
    assert asm.pending < 16


def test_frame_type_describe_handles_unknown():
    assert FrameType.describe(0x01) == "SETTINGS"
    assert FrameType.describe(0x7F) == "UNKNOWN_0x7F"


def test_hexdump_is_offset_labelled():
    dump = Frame(a_frame()).hexdump()
    lines = dump.splitlines()
    assert lines[0].startswith("0000  55 AA EB 90")
    assert lines[1].startswith("0016")
