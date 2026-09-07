"""Dashboard buffer and HTTP surface."""

from __future__ import annotations

import json
import threading
import urllib.request

from jkbms.dashboard import ReadingBuffer, build_server
from jkbms.decode import decode
from jkbms.frames import Frame
from jkbms.profiles import JK02_32S
from jkbms.verify import PackExpectation, verify

from .synth import build_cell_info_frame

CELLS = [3.912, 3.908, 3.915, 3.910]


def a_reading(cells=None):
    return decode(Frame(build_cell_info_frame(JK02_32S, cells or CELLS)), JK02_32S)


def test_empty_buffer_reports_not_ready():
    snap = ReadingBuffer().snapshot()
    assert snap["ready"] is False
    assert snap["samples"] == 0


def test_snapshot_carries_the_decoded_values():
    buf = ReadingBuffer()
    buf.add(a_reading())
    snap = buf.snapshot()
    assert snap["ready"] is True
    assert round(snap["pack_v"], 3) == round(sum(CELLS), 3)
    assert len(snap["cells_v"]) == 4


def test_deviation_is_computed_against_the_mean():
    """The balance chart plots deviation; four identical bars would say nothing."""
    buf = ReadingBuffer()
    buf.add(a_reading())
    dev = buf.snapshot()["cell_dev_mv"]
    assert len(dev) == 4
    # Deviations about a mean must sum to ~zero.
    assert abs(sum(dev)) < 0.5
    # And the highest cell must read positive, the lowest negative.
    assert dev[CELLS.index(max(CELLS))] > 0
    assert dev[CELLS.index(min(CELLS))] < 0


def test_history_is_bounded():
    buf = ReadingBuffer(history=5)
    for _ in range(20):
        buf.add(a_reading())
    assert len(buf.snapshot()["history"]) == 5
    assert buf.snapshot()["samples"] == 20


def test_verification_state_is_exposed():
    buf = ReadingBuffer()
    reading = a_reading()
    buf.add(reading)
    buf.set_verification(verify(reading, PackExpectation(cell_count=4)))
    assert buf.snapshot()["confirmed"] is True


def test_unconfirmed_layout_is_surfaced_with_reasons():
    """The page must be able to shout when the numbers may be wrong."""
    from jkbms.profiles import JK02_24S
    buf = ReadingBuffer()
    bad = decode(Frame(build_cell_info_frame(JK02_32S, CELLS)), JK02_24S)
    buf.add(bad)
    buf.set_verification(verify(bad, PackExpectation(cell_count=4)))
    snap = buf.snapshot()
    assert snap["confirmed"] is False
    assert snap["failures"]


def test_error_is_reported_before_any_frame():
    buf = ReadingBuffer()
    buf.set_error("cannot connect")
    snap = buf.snapshot()
    assert snap["ready"] is False
    assert snap["error"] == "cannot connect"


def test_snapshot_is_json_serialisable():
    buf = ReadingBuffer()
    buf.add(a_reading())
    json.dumps(buf.snapshot())      # must not raise


def test_http_server_serves_page_and_state():
    buf = ReadingBuffer()
    buf.add(a_reading())
    server = build_server(buf, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert "<title>JK BMS Live</title>" in page
        assert "deviation from pack mean" in page

        state = json.loads(
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state").read())
        assert state["ready"] is True
        assert len(state["cells_v"]) == 4

        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope")
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_page_has_no_external_resources():
    """It is served offline on a bench; nothing may need the internet."""
    from jkbms.dashboard import PAGE
    for bad in ("http://", "https://", "//cdn", "<script src"):
        assert bad not in PAGE, f"page references external resource: {bad}"


def test_page_defines_dark_theme_for_both_signals():
    from jkbms.dashboard import PAGE
    assert "prefers-color-scheme: dark" in PAGE
    assert '[data-theme="dark"]' in PAGE


def test_favicon_is_answered_not_404():
    """An unexplained console 404 makes real errors harder to spot."""
    import urllib.error
    server = build_server(ReadingBuffer(), "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico")
        assert resp.status == 204
    finally:
        server.shutdown(); server.server_close()


def test_zero_reference_line_sits_above_the_marks():
    """The fills carry a surface ring; the midline must not be painted over."""
    from jkbms.dashboard import PAGE
    mid = PAGE[PAGE.index(".devbar .mid"):PAGE.index(".devbar .fill")]
    fill = PAGE[PAGE.index(".devbar .fill"):PAGE.index(".legend")]
    assert "z-index:3" in mid
    assert "z-index:2" in fill


# ------------------------------------------------- headless / LAN access
def test_primary_address_returns_something_usable():
    """The headless path must print an address another machine can reach."""
    import ipaddress
    from jkbms.cli import _primary_address
    addr = _primary_address()
    assert addr
    # Either a real IP, or a hostname fallback -- never the useless 0.0.0.0.
    assert addr != "0.0.0.0"
    try:
        ipaddress.ip_address(addr)
    except ValueError:
        assert addr.strip(), "fallback must still be a non-empty hostname"


def test_lan_flag_binds_all_interfaces(tmp_path, capsys, monkeypatch):
    """--lan is the headless case: bind 0.0.0.0 and say so out loud."""
    import threading
    from jkbms import cli
    from .test_pipeline import capture_bytes

    cap = tmp_path / "cap.bin"
    cap.write_bytes(capture_bytes(2))

    bound = {}
    real_build = cli.build_server

    def spy(buffer, host, port):
        bound["host"] = host
        server = real_build(buffer, "127.0.0.1", 0)
        # Stop almost immediately; we are testing the bind decision, not serving.
        threading.Timer(0.4, server.shutdown).start()
        return server

    monkeypatch.setattr(cli, "build_server", spy)
    cli.main(["dashboard", "--replay", str(cap), "--lan", "--no-browser"])
    assert bound["host"] == "0.0.0.0"
    out = capsys.readouterr().out
    assert "from another machine" in out
    assert "no auth" in out.lower()


def test_default_binds_loopback_only(tmp_path, capsys, monkeypatch):
    import threading
    from jkbms import cli
    from .test_pipeline import capture_bytes

    cap = tmp_path / "cap.bin"
    cap.write_bytes(capture_bytes(2))
    bound = {}
    real_build = cli.build_server

    def spy(buffer, host, port):
        bound["host"] = host
        server = real_build(buffer, "127.0.0.1", 0)
        threading.Timer(0.4, server.shutdown).start()
        return server

    monkeypatch.setattr(cli, "build_server", spy)
    cli.main(["dashboard", "--replay", str(cap), "--no-browser"])
    assert bound["host"] == "127.0.0.1"
    assert "no auth" not in capsys.readouterr().out.lower()


def test_port_already_in_use_is_explained(tmp_path, capsys):
    """Two dashboards is a normal mistake; the error should name the fix."""
    from jkbms import cli
    from .test_pipeline import capture_bytes

    blocker = build_server(ReadingBuffer(), "127.0.0.1", 0)
    port = blocker.server_address[1]
    cap = tmp_path / "cap.bin"
    cap.write_bytes(capture_bytes(1))
    try:
        rc = cli.main(["dashboard", "--replay", str(cap), "--no-browser",
                       "--http-port", str(port)])
        assert rc == 2
        err = capsys.readouterr().err
        assert "cannot bind" in err
        assert "already be running" in err
    finally:
        blocker.server_close()
