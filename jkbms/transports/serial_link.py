"""UART / RS485 transport for boards set to protocol #001 (JK Modbus V1.0).

The poll frame captured from JK's own Windows Monitor is::

    01 10 16 20 00 01 02 00 00 D6 F1

That decodes cleanly as Modbus RTU: slave 1, function 0x10 (write multiple
registers), register 0x1620, 1 register, 2 data bytes, value 0x0000, then CRC-16
``F1D6`` little-endian. The CRC checks out, so this module *builds* the frame
rather than replaying a magic constant -- which means the device address is a
parameter instead of being welded to 1.

The BMS answers with the usual ``55 AA EB 90`` records.

Do not expect the legacy ``4E 57`` command frames to work here. They get no
response at all on this board generation; older libraries built around them
time out silently, which reads like a wiring fault and is not one.

WIRING: the 4-pin 1.25 mm JST header is GND / RX / TX / VBAT. Cross TX and RX.
Do not connect VBAT -- it carries full pack voltage. Some board revisions
top-mount the connector, which reverses the pin order end for end; meter against
B- before plugging anything in.
"""

from __future__ import annotations

import time
from typing import Iterator

from ..crc import append_modbus_crc
from ..frames import Frame
from .base import Transport, TransportError

__all__ = ["SerialTransport", "build_poll_frame", "POLL_FRAME_REFERENCE"]

BAUD = 115200

#: Register the Monitor application pokes to request a data dump.
_POLL_REGISTER = 0x1620

#: The exact bytes observed on the wire, kept as a regression fixture for
#: ``build_poll_frame`` (see tests). Not used at runtime.
POLL_FRAME_REFERENCE = bytes.fromhex("011016200001020000D6F1")


def build_poll_frame(device_address: int = 1) -> bytes:
    """Construct the Modbus RTU request that asks the BMS for a data dump.

    ``device_address`` is the Modbus slave address. Note this is distinct from
    the BMS's own "device address" setting, which must be 0 for JK's GUI to show
    anything beyond the Parallel tab.
    """
    if not 0 <= device_address <= 247:
        raise ValueError("Modbus device address must be 0..247")
    payload = bytes((
        device_address,
        0x10,                       # write multiple registers
        _POLL_REGISTER >> 8, _POLL_REGISTER & 0xFF,
        0x00, 0x01,                 # one register
        0x02,                       # two data bytes
        0x00, 0x00,                 # value
    ))
    return append_modbus_crc(payload)


class SerialTransport(Transport):
    """Poll the BMS over a USB-to-TTL adapter and yield response frames."""

    def __init__(self, port: str, *, baud: int = BAUD, device_address: int = 1,
                 poll_interval_s: float = 1.0, read_timeout_s: float = 0.3) -> None:
        super().__init__()
        self.port = port
        self.baud = baud
        self.poll_frame = build_poll_frame(device_address)
        self.poll_interval_s = poll_interval_s
        self.read_timeout_s = read_timeout_s
        self._serial = None

    def open(self) -> None:
        try:
            import serial  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise TransportError(
                "pyserial is not installed. Run: pip install pyserial"
            ) from exc
        try:
            self._serial = serial.Serial(
                self.port, self.baud, timeout=self.read_timeout_s,
                bytesize=8, parity="N", stopbits=1,
            )
        except Exception as exc:  # pragma: no cover - hardware dependent
            raise TransportError(f"cannot open {self.port}: {exc}") from exc

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def _poll(self) -> None:
        assert self._serial is not None
        self._serial.reset_input_buffer()
        self._serial.write(self.poll_frame)
        self._serial.flush()

    def raw_stream(self, *, timeout_s: float | None = None) -> Iterator[bytes]:
        """Yield raw chunks as they arrive, polling on the configured interval.

        Used by ``jkbms sniff`` to capture bytes even when nothing decodes --
        the first thing to establish is whether the BMS is talking at all.
        """
        if self._serial is None:
            raise TransportError("transport is not open")
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        next_poll = 0.0
        while deadline is None or time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_poll:
                self._poll()
                next_poll = now + self.poll_interval_s
            waiting = self._serial.in_waiting or 1
            chunk = self._serial.read(waiting)
            if chunk:
                yield chunk

    def frames(self, *, limit: int | None = None,
               timeout_s: float | None = None) -> Iterator[Frame]:
        count = 0
        for chunk in self.raw_stream(timeout_s=timeout_s):
            for frame in self.assembler.feed(chunk):
                yield frame
                count += 1
                if limit is not None and count >= limit:
                    return
