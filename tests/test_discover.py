"""Discovery: can we find the layout with no profile at all?

This is the safety net for the case the brief flags -- a firmware whose byte map
matches nothing in profiles.py. If discovery can independently point at the same
offsets a profile claims, that is strong corroboration.
"""

from __future__ import annotations

from jkbms.discover import discover, find_cell_blocks
from jkbms.profiles import JK02_32S

from .synth import build_cell_info_frame

CELLS = [3.912, 3.908, 3.915, 3.910]


def test_finds_the_cell_block_at_the_documented_offset():
    raw = build_cell_info_frame(JK02_32S, CELLS)
    block = discover(raw).best_block
    assert block is not None
    assert block.offset == JK02_32S.cells_offset
    assert block.count == 4


def test_finds_pack_voltage_where_the_profile_says_it_is():
    """Independent corroboration of the profile's pack_v offset."""
    raw = build_cell_info_frame(JK02_32S, CELLS)
    findings = discover(raw)
    assert JK02_32S.pack_v.offset in findings.pack_v_candidates


def test_finds_the_average_cell_field():
    raw = build_cell_info_frame(JK02_32S, CELLS)
    findings = discover(raw)
    assert JK02_32S.avg_cell_v.offset in findings.avg_cell_candidates


def test_reports_no_block_for_a_frame_without_cells():
    findings = discover(bytes(300))
    assert findings.best_block is None
    assert "no cell-voltage run" in findings.report()


def test_runs_are_maximal_not_fragmented():
    """A 4-cell block should be reported once, not as overlapping 3-cell pieces."""
    raw = build_cell_info_frame(JK02_32S, CELLS)
    at_offset = [b for b in find_cell_blocks(raw) if b.offset == JK02_32S.cells_offset]
    assert len(at_offset) == 1
    assert at_offset[0].count == 4


def test_widely_spread_values_are_not_treated_as_one_block():
    """Cells in a pack track each other; a huge spread means it is not a block."""
    raw = bytearray(300)
    for i, mv in enumerate((2100, 4400, 2050, 4450)):
        raw[6 + 2 * i : 8 + 2 * i] = mv.to_bytes(2, "little")
    blocks = [b for b in find_cell_blocks(bytes(raw)) if b.offset == 6]
    assert not blocks or blocks[0].count < 4


def test_discovery_works_on_an_eight_cell_pack():
    cells = [3.90 + 0.002 * i for i in range(8)]
    raw = build_cell_info_frame(JK02_32S, cells)
    block = discover(raw).best_block
    assert block is not None and block.count == 8


def test_report_is_human_readable():
    raw = build_cell_info_frame(JK02_32S, CELLS)
    text = discover(raw).report()
    assert "cell-block candidates" in text
    assert "pack voltage" in text
