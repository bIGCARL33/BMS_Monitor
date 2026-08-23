"""Link layers: UART/Modbus, BLE, and replay of captured streams."""

from __future__ import annotations

from .base import Transport, TransportError
from .replay import ReplayTransport, load_capture

__all__ = ["Transport", "TransportError", "ReplayTransport", "load_capture"]


def __getattr__(name: str):
    """Import the hardware transports lazily.

    pyserial and bleak are optional: someone replaying a capture on a machine
    with neither installed should not hit an ImportError just for touching this
    package.
    """
    if name == "SerialTransport":
        from .serial_link import SerialTransport
        return SerialTransport
    if name == "BleTransport":
        from .ble_link import BleTransport
        return BleTransport
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
