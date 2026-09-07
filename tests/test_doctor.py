"""Preflight checks.

``evaluate`` is pure, so every platform can be simulated here -- including a
Jetson, which is the point: the Jetson-specific advice has to be right before
it reaches a Jetson.
"""

from __future__ import annotations

from jkbms.doctor import FAIL, OK, WARN, Environment, evaluate, report


def find(findings, name):
    for f in findings:
        if f.name == name:
            return f
    raise AssertionError(f"no finding named {name!r}: {[f.name for f in findings]}")


def healthy(**overrides) -> Environment:
    base = dict(
        python_version=(3, 10, 12), has_pyserial=True, has_bleak=True,
        serial_ports=("/dev/ttyUSB0",), groups=("jre", "dialout"),
        bluetooth_adapters=("hci0",), bluetooth_ready=True,
        bluetooth_detail="hci0", board_model="", nvgetty_active=False,
        machine="x86_64")
    base.update(overrides)
    return Environment(**base)


def test_healthy_machine_is_all_clear():
    findings = evaluate(healthy())
    assert all(f.status == OK for f in findings), report(findings)
    assert "All clear" in report(findings)


# ---------------------------------------------------------------- python floor
def test_python_38_passes_the_floor():
    """JetPack 5 ships 3.8; it must not be reported as too old."""
    assert find(evaluate(healthy(python_version=(3, 8, 10))), "python").status == OK


def test_python_37_fails():
    f = find(evaluate(healthy(python_version=(3, 7, 9))), "python")
    assert f.status == FAIL
    assert "below" in f.detail


# ---------------------------------------------------------------- bluetooth
def test_powered_off_radio_names_the_rfkill_fix():
    f = find(evaluate(healthy(bluetooth_ready=False,
                              bluetooth_detail="soft blocked by rfkill")), "bluetooth radio")
    assert f.status == FAIL
    assert "rfkill unblock" in f.fix


def test_missing_adapter_on_a_pc_talks_about_the_adapter():
    f = find(evaluate(healthy(bluetooth_adapters=(), bluetooth_ready=False)),
             "bluetooth radio")
    assert f.status == FAIL
    assert "M.2" not in f.fix


def test_missing_adapter_on_a_jetson_talks_about_the_M2_card():
    """On an Orin Nano this is usually an unfitted wireless card, not BlueZ."""
    env = healthy(bluetooth_adapters=(), bluetooth_ready=False,
                  board_model="NVIDIA Jetson Orin Nano Developer Kit")
    f = find(evaluate(env), "bluetooth radio")
    assert f.status == FAIL
    assert "M.2" in f.fix and "antenna" in f.fix.lower()


# ---------------------------------------------------------------- jetson
def test_jetson_is_detected_from_the_device_tree_model():
    env = healthy(board_model="NVIDIA Jetson Orin Nano Developer Kit")
    assert env.is_jetson
    assert find(evaluate(env), "board").detail.startswith("NVIDIA Jetson")


def test_tegra_release_string_also_counts_as_a_jetson():
    assert healthy(board_model="# R36 (release), REVISION: 3.0 ... tegra").is_jetson


def test_a_desktop_is_not_a_jetson():
    env = healthy(board_model="To Be Filled By O.E.M.")
    assert not env.is_jetson
    assert [f for f in evaluate(env) if f.name == "board"] == []


def test_nvgetty_holding_a_uart_is_a_blocking_failure():
    """The serial console owns /dev/ttyTHS*, so the port opens busy."""
    env = healthy(board_model="NVIDIA Jetson Orin Nano", nvgetty_active=True)
    f = find(evaluate(env), "nvgetty")
    assert f.status == FAIL
    assert "disable nvgetty" in f.fix


def test_nvgetty_reported_ok_on_a_quiet_jetson():
    env = healthy(board_model="NVIDIA Jetson Orin Nano", nvgetty_active=False)
    assert find(evaluate(env), "nvgetty").status == OK


def test_nvgetty_is_not_mentioned_on_a_desktop():
    findings = evaluate(healthy())
    assert [f for f in findings if f.name == "nvgetty"] == []


# ---------------------------------------------------------------- permissions
def test_missing_dialout_is_blocking_when_a_port_exists():
    f = find(evaluate(healthy(groups=("jre",))), "dialout group")
    assert f.status == FAIL
    assert "usermod -aG dialout" in f.fix


def test_missing_dialout_is_only_a_warning_with_no_serial_port():
    """On a BLE-only setup this cannot block anything."""
    f = find(evaluate(healthy(groups=("jre",), serial_ports=())), "dialout group")
    assert f.status == WARN


# ---------------------------------------------------------------- packages
def test_missing_bleak_warns_with_the_apt_command():
    f = find(evaluate(healthy(has_bleak=False)), "bleak (BLE)")
    assert f.status == WARN
    assert "apt install" in f.fix


def test_missing_pyserial_warns():
    assert find(evaluate(healthy(has_pyserial=False)), "pyserial (UART)").status == WARN


# ---------------------------------------------------------------- reporting
def test_report_puts_the_fix_under_each_problem():
    text = report(evaluate(healthy(groups=("jre",))))
    lines = text.splitlines()
    idx = next(i for i, l in enumerate(lines) if "dialout" in l and "FAIL" in l)
    assert "->" in lines[idx + 1]


def test_report_counts_blocking_problems():
    env = healthy(groups=("jre",), bluetooth_ready=False, bluetooth_adapters=("hci0",))
    assert "2 blocking problem(s)" in report(evaluate(env))


def test_report_distinguishes_warnings_from_blockers():
    text = report(evaluate(healthy(has_pyserial=False, serial_ports=())))
    assert "Nothing blocking" in text


def test_ok_findings_never_print_a_fix():
    text = report(evaluate(healthy()))
    assert "->" not in text
