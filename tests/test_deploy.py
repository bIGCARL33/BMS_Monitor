"""The systemd unit and its installer.

A broken unit file fails at 3am on a board nobody is looking at, so the parts
that can be checked without systemd are checked here. ``systemd-analyze
verify`` runs too when it is available -- it is what caught
``StartLimitIntervalSec`` sitting in the wrong section, where systemd ignores
it silently and a failing unit restarts forever.
"""

from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
UNIT = DEPLOY / "jkbms-dashboard.service"
INSTALLER = DEPLOY / "install-service.sh"

SUBSTITUTIONS = {
    "__USER__": "jre",
    "__GROUP__": "jre",
    "__WORKDIR__": "/home/jre/Projects/BMS_Monitor",
    "__PYTHON__": "/usr/bin/python3",
    "__BLE_ADDR__": "C8:47:80:46:6E:37",
    "__LOGDIR__": "/var/lib/jkbms",
}


def rendered() -> str:
    text = UNIT.read_text()
    for key, value in SUBSTITUTIONS.items():
        text = text.replace(key, value)
    return text


def parsed() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(rendered())
    return parser


def test_installer_substitutes_every_placeholder():
    """A missed placeholder becomes a literal __USER__ in a live unit file."""
    import re
    placeholders = set(re.findall(r"__[A-Z_]+__", UNIT.read_text()))
    installer = INSTALLER.read_text()
    missing = [p for p in placeholders if p not in installer]
    assert not missing, f"installer never substitutes: {missing}"


def test_nothing_unsubstituted_after_rendering():
    import re
    assert not re.findall(r"__[A-Z_]+__", rendered())


def test_required_sections_present():
    assert {"Unit", "Service", "Install"} <= set(parsed().sections())


def test_start_limit_lives_in_unit_not_service():
    """systemd v229 moved these; in [Service] they are silently ignored."""
    conf = parsed()
    assert "StartLimitIntervalSec" in conf["Unit"]
    assert "StartLimitBurst" in conf["Unit"]
    assert "StartLimitIntervalSec" not in conf["Service"]


def test_strftime_codes_are_percent_escaped():
    """systemd eats a single %; the program must receive %Y not a specifier."""
    exec_start = parsed()["Service"]["ExecStart"]
    assert "%%Y%%m%%d" in exec_start
    # A bare %Y would be consumed by systemd as an unknown specifier.
    assert "-o /var/lib/jkbms/pack-%%Y" in " ".join(exec_start.split())


def test_dated_log_name_so_a_restart_cannot_clobber():
    """CsvLogger refuses to truncate; without a dated name a restart would fail."""
    exec_start = parsed()["Service"]["ExecStart"]
    assert "%%H%%M%%S" in exec_start, "needs time as well as date for fast restarts"


def test_waits_for_bluetooth():
    after = parsed()["Unit"]["After"]
    assert "bluetooth" in after


def test_restarts_but_is_rate_limited():
    conf = parsed()
    assert conf["Service"]["Restart"] == "on-failure"
    assert int(conf["Unit"]["StartLimitBurst"]) <= 10


def test_log_directory_is_writable_under_hardening():
    """ProtectSystem=strict makes everything read-only unless excepted."""
    service = parsed()["Service"]
    assert service.get("ProtectSystem") == "strict"
    assert SUBSTITUTIONS["__LOGDIR__"] in service.get("ReadWritePaths", "")


def test_installer_is_executable_and_valid_bash():
    import os
    assert os.access(INSTALLER, os.X_OK), "installer is not executable"
    result = subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()


def test_installer_rejects_a_malformed_ble_address():
    """A wrong address here becomes a restart loop visible only in the journal."""
    result = subprocess.run(["bash", str(INSTALLER), "not-an-address"],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "does not look like a BLE address" in result.stderr


def test_installer_requires_an_address():
    result = subprocess.run(["bash", str(INSTALLER)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "no BLE address given" in result.stderr


@pytest.mark.skipif(shutil.which("systemd-analyze") is None,
                    reason="systemd-analyze not available")
def test_systemd_accepts_the_unit(tmp_path):
    target = tmp_path / "jkbms-dashboard.service"
    target.write_text(rendered())
    result = subprocess.run(["systemd-analyze", "verify", str(target)],
                            capture_output=True, text=True)
    combined = result.stdout + result.stderr
    # Unknown-key warnings are the class of bug this exists to catch.
    assert "Unknown key" not in combined, combined
    assert "Failed to parse" not in combined, combined


# ------------------------------------------------- start.sh
START = DEPLOY.parent / "start.sh"


def test_start_script_is_valid_bash():
    assert START.exists()
    r = subprocess.run(["bash", "-n", str(START)], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()


def test_start_script_never_pkills_by_pattern():
    """pkill -f can match this script's own ancestors and kill the session."""
    text = START.read_text()
    code = "\n".join(l.split("#", 1)[0] for l in text.splitlines())
    assert "pkill" not in code, (
        "start.sh must not pkill by pattern; it filters its own ancestors out "
        "of a pgrep list instead")


def test_start_script_excludes_its_own_ancestors():
    text = START.read_text()
    assert "ancestors()" in text
    assert "grep -qx" in text


def test_start_script_only_removes_git_tracked_backups():
    """A user's real settings backups must never be deleted."""
    text = START.read_text()
    assert "git ls-files --error-unmatch" in text, (
        "the cleanup must be conditional on the files being git-tracked test "
        "artifacts, not any backup the user has saved")
