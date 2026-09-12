"""Full-screen UI layout.

``render`` is a pure function from state to lines precisely so the layout can
be checked here rather than by squinting at a terminal.
"""

from __future__ import annotations

from jkbms.decode import decode
from jkbms.frames import Frame
from jkbms.profiles import JK02_32S
from jkbms.tui import MIN_SCALE_MV, ScreenState, _bar, render

from .synth import build_cell_info_frame

# The real imbalance measured on the Jetson: two pairs, ~31 mV apart.
SPLIT = [3.442, 3.442, 3.472, 3.473]
FLAT = [3.4470, 3.4460, 3.4460, 3.4460]


def reading(cells=None, **kw):
    return decode(Frame(build_cell_info_frame(JK02_32S, cells or SPLIT, **kw)),
                  JK02_32S)


def screen(cells=None, width=78, height=26, **kw):
    return render(ScreenState(reading=reading(cells), now="12:00:00",
                              status="live", **kw), width, height)


# ---------------------------------------------------------------- layout
def test_no_line_exceeds_the_width():
    """A line over the width wraps and destroys the layout."""
    for width in (40, 60, 78, 100, 200):
        for line in screen(width=width):
            assert len(line) <= width, f"{len(line)} > {width}: {line!r}"


def test_fills_the_height_so_the_prompt_sits_at_the_bottom():
    for height in (20, 24, 40):
        lines = screen(height=height)
        assert len(lines) == height, f"{len(lines)} lines for height {height}"
        assert lines[-1].startswith("jkbms>")


def test_both_stat_rows_align():
    """Columns that do not line up read as sloppy and slow the eye down."""
    lines = screen()
    pack = next(l for l in lines if l.startswith("  PACK"))
    soc = next(l for l in lines if l.startswith("  SOC"))
    assert pack.index("CURRENT") == soc.index("SPREAD")
    assert pack.index("POWER") == soc.index("TEMP")


def test_shows_the_pack_and_cell_values():
    text = "\n".join(screen())
    assert "13.829" in text          # pack
    assert "3.4420" in text and "3.4730" in text
    assert "31.0" in text            # spread mV


def test_waiting_state_before_any_frame():
    lines = render(ScreenState(), 78, 24)
    assert any("waiting for the first frame" in l for l in lines)
    assert lines[-1].startswith("jkbms>")


def test_prompt_text_is_shown():
    lines = render(ScreenState(reading=reading(), prompt="set balancer on"),
                   78, 24)
    assert lines[-1] == "jkbms> set balancer on"


# ---------------------------------------------------------------- bars
def test_bar_is_centred_and_signed():
    above = _bar(+10, 10)
    below = _bar(-10, 10)
    assert above.index("|") == below.index("|"), "midline must not move"
    assert above.split("|")[1].strip("# ") == ""
    assert above.split("|")[1].count("#") > 0, "positive grows right"
    assert below.split("|")[0].count("#") > 0, "negative grows left"


def test_zero_deviation_draws_no_bar():
    assert _bar(0.0, 10).count("#") == 0


def test_a_balanced_pack_is_not_amplified_into_alarm():
    """Sub-millivolt noise must not fill the bar on a healthy pack."""
    lines = screen(cells=FLAT)
    bars = [l for l in lines if l.strip().startswith(("1 ", "2 ", "3 ", "4 "))]
    worst = max(line.count("#") for line in bars)
    assert worst <= 6, f"a 1 mV spread drew {worst} blocks; scale floor failed"


def test_scale_floor_is_respected():
    assert _bar(0.5, 0.1).count("#") <= (MIN_SCALE_MV and 40)
    tiny = _bar(0.5, 0.0)      # scale below the floor
    assert tiny.count("#") < 12


def test_real_imbalance_is_clearly_visible():
    """The 31 mV split must read at a glance -- that is the whole point."""
    lines = screen(cells=SPLIT)
    bars = [l for l in lines if l.strip().startswith(("1 ", "2 ", "3 ", "4 "))]
    assert len(bars) == 4
    assert max(line.count("#") for line in bars) >= 10


# ---------------------------------------------------------------- switches
def test_switch_states_render():
    lines = screen(switches={"charge": 1, "discharge": 0, "balancer": 1})
    row = next(l for l in lines if "SWITCHES" in l)
    assert "charge ON" in row and "discharge off" in row and "balancer ON" in row


def test_unread_switches_say_so():
    row = next(l for l in screen() if "SWITCHES" in l)
    assert "unread" in row


def test_unknown_switch_value_is_not_guessed():
    lines = screen(switches={"charge": None})
    row = next(l for l in lines if "SWITCHES" in l)
    assert "charge ?" in row


# ---------------------------------------------------------------- log pane
def test_log_shows_the_most_recent_lines():
    log = [f"line {i}" for i in range(200)]
    lines = screen(log=log, height=26)
    text = "\n".join(lines)
    assert "line 199" in text
    assert "line 0" not in text


def test_long_log_lines_are_clipped_not_wrapped():
    lines = screen(log=["x" * 500], width=78)
    assert all(len(l) <= 78 for l in lines)
