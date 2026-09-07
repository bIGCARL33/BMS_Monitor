"""Environment preflight.

Bringing this up on a new machine tends to fail in a handful of predictable
ways -- a missing package, a group you are not in, a radio that is switched
off, an interpreter older than the code. Each of those presents at the bench as
"it doesn't work", and several are indistinguishable from a wiring fault. That
is an expensive way to find out you needed one ``usermod``.

``jkbms doctor`` checks them all at once and prints the fix next to each
failure.

Structure: :func:`probe_environment` does all the I/O and returns plain facts;
:func:`evaluate` is a pure function from those facts to findings. So the
judgement half is testable without a Jetson, a BMS, or a Bluetooth radio.
"""

from __future__ import annotations

import glob
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Sequence

__all__ = ["Finding", "Environment", "probe_environment", "evaluate", "report"]

OK, WARN, FAIL = "ok", "warn", "fail"

#: The floor the package is tested against. JetPack 5 ships Python 3.8.
MIN_PYTHON = (3, 8)


@dataclass
class Finding:
    name: str
    status: str
    detail: str
    fix: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL


@dataclass
class Environment:
    """Plain facts about the machine. No judgement -- see :func:`evaluate`."""

    python_version: tuple = field(default_factory=lambda: sys.version_info[:3])
    has_pyserial: bool = False
    has_bleak: bool = False
    serial_ports: Sequence[str] = ()
    groups: Sequence[str] = ()
    #: Names under /sys/class/bluetooth, e.g. ("hci0",).
    bluetooth_adapters: Sequence[str] = ()
    #: True when at least one adapter is powered and unblocked.
    bluetooth_ready: bool = False
    bluetooth_detail: str = ""
    #: Contents of the device-tree model string, when present.
    board_model: str = ""
    #: True when nvgetty (Jetson's serial console) is holding a UART.
    nvgetty_active: bool = False
    machine: str = ""

    @property
    def is_jetson(self) -> bool:
        model = self.board_model.lower()
        return "jetson" in model or "tegra" in model


def _read(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            # Device-tree strings are NUL-terminated.
            return handle.read().decode("utf-8", "replace").strip("\x00").strip()
    except OSError:
        return ""


def _module_present(name: str) -> bool:
    try:
        __import__(name)
    except Exception:
        return False
    return True


def _bluetooth_state(adapters: Sequence[str]) -> tuple:
    """Return ``(ready, detail)`` for the Bluetooth radio.

    rfkill is the authority on soft/hard blocks; a present adapter that is
    blocked looks identical to no adapter from the application's side.
    """
    if not adapters:
        return False, "no adapter under /sys/class/bluetooth"

    rfkill = shutil.which("rfkill")
    if rfkill:
        try:
            out = subprocess.run([rfkill, "list", "bluetooth"], capture_output=True,
                                 text=True, timeout=5).stdout.lower()
            if "soft blocked: yes" in out:
                return False, "soft blocked by rfkill"
            if "hard blocked: yes" in out:
                return False, "hard blocked (physical switch or firmware)"
        except (OSError, subprocess.SubprocessError):
            pass

    # An adapter that is down reports no address bound yet on some stacks; the
    # power state is the more reliable signal.
    for adapter in adapters:
        if _read(f"/sys/class/bluetooth/{adapter}/rfkill0/soft") == "1":
            return False, f"{adapter} soft blocked"
    return True, ", ".join(adapters)


def _service_active(name: str) -> bool:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False
    try:
        out = subprocess.run([systemctl, "is-active", name], capture_output=True,
                             text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return out == "active"


def probe_environment() -> Environment:
    """Gather the facts. All the I/O lives here."""
    adapters = sorted(os.path.basename(p)
                      for p in glob.glob("/sys/class/bluetooth/hci*"))
    ready, detail = _bluetooth_state(adapters)
    ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
                   + glob.glob("/dev/ttyTHS*") + glob.glob("/dev/ttyAMA*"))
    model = _read("/proc/device-tree/model") or _read("/etc/nv_tegra_release")
    try:
        groups = os.getgroups()
        import grp
        names = []
        for gid in groups:
            try:
                names.append(grp.getgrgid(gid).gr_name)
            except KeyError:
                pass
    except Exception:
        names = []

    return Environment(
        python_version=sys.version_info[:3],
        has_pyserial=_module_present("serial"),
        has_bleak=_module_present("bleak"),
        serial_ports=ports,
        groups=names,
        bluetooth_adapters=adapters,
        bluetooth_ready=ready,
        bluetooth_detail=detail,
        board_model=model,
        nvgetty_active=_service_active("nvgetty"),
        machine=platform.machine(),
    )


def evaluate(env: Environment) -> list:
    """Turn facts into findings. Pure -- no I/O, so it is testable anywhere."""
    out = []

    version = ".".join(str(p) for p in env.python_version)
    if tuple(env.python_version[:2]) >= MIN_PYTHON:
        out.append(Finding("python", OK, f"{version} on {env.machine or 'unknown'}"))
    else:
        out.append(Finding(
            "python", FAIL, f"{version} is below the {MIN_PYTHON[0]}.{MIN_PYTHON[1]} floor",
            "install a newer python3, or run on JetPack 6 (Ubuntu 22.04)"))

    if env.is_jetson:
        out.append(Finding("board", OK, env.board_model or "Jetson"))

    # --- links -----------------------------------------------------------
    if env.has_bleak:
        out.append(Finding("bleak (BLE)", OK, "installed"))
    else:
        out.append(Finding("bleak (BLE)", WARN, "not installed",
                           "sudo apt install -y python3-bleak"))

    if env.bluetooth_ready:
        out.append(Finding("bluetooth radio", OK, env.bluetooth_detail))
    elif env.bluetooth_adapters:
        out.append(Finding(
            "bluetooth radio", FAIL, env.bluetooth_detail,
            "sudo rfkill unblock bluetooth && sudo systemctl start bluetooth "
            "&& bluetoothctl power on"))
    else:
        # On a Jetson this is usually a missing or unseated M.2 card rather
        # than a software problem, which is worth saying before someone spends
        # an hour on BlueZ.
        fix = ("check the M.2 Key-E wireless card is fitted and its antennas "
               "connected; then: sudo systemctl start bluetooth"
               if env.is_jetson else
               "check the adapter is present, then: sudo systemctl start bluetooth")
        out.append(Finding("bluetooth radio", FAIL, "no adapter found", fix))

    if env.has_pyserial:
        out.append(Finding("pyserial (UART)", OK, "installed"))
    else:
        out.append(Finding("pyserial (UART)", WARN, "not installed",
                           "sudo apt install -y python3-serial"))

    if env.serial_ports:
        out.append(Finding("serial ports", OK, " ".join(env.serial_ports)))
    else:
        out.append(Finding("serial ports", WARN, "none present",
                           "plug in the USB-TTL adapter, or use BLE"))

    # --- permissions ------------------------------------------------------
    if "dialout" in env.groups:
        out.append(Finding("dialout group", OK, "member"))
    elif env.serial_ports:
        out.append(Finding(
            "dialout group", FAIL, "not a member; opening a port will fail",
            f"sudo usermod -aG dialout {os.environ.get('USER', '$USER')}  "
            "(then log out and back in)"))
    else:
        out.append(Finding("dialout group", WARN,
                           "not a member (only matters for the UART path)",
                           f"sudo usermod -aG dialout {os.environ.get('USER', '$USER')}"))

    # --- Jetson specifics -------------------------------------------------
    if env.nvgetty_active:
        out.append(Finding(
            "nvgetty", FAIL,
            "the serial console is holding a Jetson UART; /dev/ttyTHS* will be busy",
            "sudo systemctl stop nvgetty && sudo systemctl disable nvgetty"))
    elif env.is_jetson:
        out.append(Finding("nvgetty", OK, "not holding a UART"))

    return out


def report(findings: Sequence[Finding]) -> str:
    """Render findings, fixes last so the eye lands on what to do."""
    marks = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
    lines = []
    for finding in findings:
        lines.append(f"[{marks[finding.status]}] {finding.name:18s} {finding.detail}")
        if finding.fix and finding.status != OK:
            lines.append(f"           -> {finding.fix}")

    failures = [f for f in findings if f.status == FAIL]
    warnings = [f for f in findings if f.status == WARN]
    lines.append("")
    if failures:
        lines.append(f"{len(failures)} blocking problem(s). Fix those first; each one "
                     "presents at the bench as a dead link.")
    elif warnings:
        lines.append("Nothing blocking. The warnings only matter for the paths "
                     "they name.")
    else:
        lines.append("All clear.")
    return "\n".join(lines)
