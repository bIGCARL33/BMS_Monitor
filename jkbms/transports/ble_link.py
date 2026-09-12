"""BLE transport, via Bleak (BlueZ on Linux, WinRT on Windows).

This is the path that needs no board-side configuration: the BMS advertises
already, so there is nothing to set from a phone app. Requires the pack to be
awake and, importantly, *not* connected to anything else -- the JK accepts one
BLE connection at a time, so a phone app or a second script holding the link
will make this fail to connect.

On Linux everything goes through BlueZ, so the radio has to be powered and
bluetooth.service running -- a blocked or powered-off adapter is the single
most common reason a scan comes back empty. ``_adapter_error`` turns those
into instructions rather than tracebacks.

Protocol: write 20-byte commands to characteristic FFE1 on service FFE0 and read
the reply back from notifications on the same characteristic. Notifications
arrive in 20-byte fragments that concatenate into the same ``55 AA EB 90``
records the UART link produces, so everything downstream is shared.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Iterator

from ..crc import sum8
from ..frames import Frame
from .base import Transport, TransportError

__all__ = [
    "BleTransport", "build_command", "scan", "JK_SERVICE_UUID", "JK_CHAR_UUID",
    "CMD_DEVICE_INFO", "CMD_CELL_INFO", "CMD_SETTINGS",
]

JK_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
JK_CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

_CMD_HEADER = b"\xAA\x55\x90\xEB"
_CMD_LEN = 20

CMD_DEVICE_INFO = 0x97
CMD_CELL_INFO = 0x96
#: Nominally "asks the board to send its settings record" (frame type 0x01).
#: On firmware 15.41 this is a no-op once the link is up: the board pushes
#: exactly one settings frame, unprompted, immediately after connecting
#: (alongside device info), and does not answer this command afterwards --
#: confirmed by sending it four times over 32s on an open connection with
#: zero replies. Getting a settings frame at any other point in the session
#: requires disconnecting and reconnecting; see Console.fetch_settings().
CMD_SETTINGS = 0x95

#: Names JK boards advertise under. Matched case-insensitively as a prefix.
_NAME_HINTS = ("jk-", "jk_", "jkbms")


def build_command(command: int, value: int = 0) -> bytes:
    """Build a 20-byte BLE command frame.

    Layout: ``AA 55 90 EB <cmd> <len> <value u32 LE>`` zero-padded to 19 bytes,
    with byte 19 an 8-bit additive checksum of the preceding 19.
    """
    body = bytearray(_CMD_LEN - 1)
    body[0:4] = _CMD_HEADER
    body[4] = command
    body[5] = 0x00
    body[6:10] = value.to_bytes(4, "little")
    return bytes(body) + bytes((sum8(body),))


def _looks_like_jk(name: str | None) -> bool:
    if not name:
        return False
    lowered = name.lower()
    return any(lowered.startswith(hint) for hint in _NAME_HINTS)


def _adapter_error(exc: BaseException) -> str | None:
    """Translate a BlueZ adapter problem into something actionable, or None.

    A powered-off or rfkill-blocked radio is by far the most common reason a
    scan fails, and bleak surfaces it as an exception type that did not exist
    in older releases -- so match on the message rather than the class.
    """
    text = str(exc).lower()
    if "no powered bluetooth adapters" in text or "powered_off" in text:
        return (
            "the Bluetooth radio is off or blocked. Turn it on:\n"
            "    rfkill list bluetooth          # check for a soft/hard block\n"
            "    sudo rfkill unblock bluetooth\n"
            "    sudo systemctl start bluetooth\n"
            "    bluetoothctl power on"
        )
    if "bluetooth" in text and ("not available" in text or "no adapter" in text):
        return (
            "no Bluetooth adapter is available to BlueZ. Check "
            "'systemctl status bluetooth' and that the radio is not blocked "
            "(rfkill list bluetooth)."
        )
    if "dbus" in text or "org.bluez" in text:
        return (
            "could not reach BlueZ over D-Bus. Is bluetooth.service running? "
            "Check 'systemctl status bluetooth'."
        )
    return None


async def scan_async(
    timeout_s: float = 10.0,
) -> list[tuple[str, str, int | None, bool]]:
    """Scan for BLE devices.

    Returns ``(address, name, rssi, looks_like_jk)``. The flag matters: the
    advertised name is user-changeable and some boards advertise a bare serial
    number, so when nothing matches the JK hints we still list what was found
    -- but the caller must be able to say "this is a guess" rather than
    presenting an unrelated speaker as the BMS.
    """
    try:
        from bleak import BleakScanner  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise TransportError(
            "bleak is not installed. Run: sudo apt install python3-bleak"
        ) from exc

    try:
        found = await BleakScanner.discover(timeout=timeout_s, return_adv=True)
    except Exception as exc:  # pragma: no cover - hardware dependent
        hint = _adapter_error(exc)
        raise TransportError(f"BLE scan failed: {hint or exc}") from exc
    jk: list[tuple[str, str, int | None, bool]] = []
    others: list[tuple[str, str, int | None, bool]] = []
    for device, adv in found.values():
        name = adv.local_name or device.name or ""
        # A matching service UUID is real evidence; a matching name is a hint.
        is_jk = (_looks_like_jk(name)
                 or JK_SERVICE_UUID in [u.lower() for u in adv.service_uuids])
        (jk if is_jk else others).append(
            (device.address, name, adv.rssi, is_jk))
    # Strongest signal first within each group.
    key = lambda e: -(e[2] if e[2] is not None else -999)  # noqa: E731
    return sorted(jk, key=key) or sorted(others, key=key)


def scan(timeout_s: float = 10.0) -> list[tuple[str, str, int | None, bool]]:
    """Blocking wrapper around :func:`scan_async`."""
    return asyncio.run(scan_async(timeout_s))


class BleTransport(Transport):
    """Connect to a JK BMS over BLE and yield response frames.

    The async Bleak client is driven from a private event loop so that callers
    get the same simple blocking iterator the serial transport provides.

    That loop is not reentrant: ``console.py`` reads frames from a background
    thread while the main thread calls ``request()``/``send_raw()`` to fetch
    or write settings. Without serializing those, a call from one thread while
    the other has ``run_until_complete`` in flight raises "This event loop is
    already running" -- silently refusing every settings read and write.
    ``_loop_lock`` is the fix; ``_closed`` lets the reader thread stop cleanly
    instead of racing ``close()`` for a loop that is being torn down.
    """

    def __init__(self, address: str, *, connect_timeout_s: float = 20.0,
                 poll_interval_s: float = 1.0) -> None:
        super().__init__()
        self.address = address
        self.connect_timeout_s = connect_timeout_s
        self.poll_interval_s = poll_interval_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._queue: asyncio.Queue[bytes] | None = None
        self._loop_lock = threading.Lock()
        self._closed = threading.Event()

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        try:
            from bleak import BleakClient  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise TransportError(
                "bleak is not installed. Run: sudo apt install python3-bleak"
            ) from exc

        self._closed.clear()
        self._loop = asyncio.new_event_loop()
        self._queue = asyncio.Queue()

        async def connect():
            client = BleakClient(self.address, timeout=self.connect_timeout_s)
            await client.connect()
            await client.start_notify(JK_CHAR_UUID, self._on_notify)
            # The board only starts streaming cell info after it has been asked
            # for device info; skipping this yields a silent connection.
            await client.write_gatt_char(
                JK_CHAR_UUID, build_command(CMD_DEVICE_INFO), response=False)
            await asyncio.sleep(0.2)
            await client.write_gatt_char(
                JK_CHAR_UUID, build_command(CMD_CELL_INFO), response=False)
            return client

        try:
            self._client = self._loop.run_until_complete(connect())
        except Exception as exc:  # pragma: no cover - hardware dependent
            self._shutdown_loop()
            hint = _adapter_error(exc)
            if hint is None:
                hint = (f"{exc}. The JK allows one BLE connection at a time -- "
                        "make sure no phone app or other script is holding it.")
            raise TransportError(f"cannot connect to {self.address}: {hint}") from exc

    def request(self, command: int, value: int = 0) -> None:
        """Send a command frame on the open link.

        Used to ask for the settings record and to write settings. Writes go
        through :mod:`jkbms.control`, which builds the frame and enforces the
        read-back verification -- do not call this with a raw write code
        directly.
        """
        if self._loop is None or self._client is None:
            raise TransportError("transport is not open")
        with self._loop_lock:
            if self._closed.is_set():
                raise TransportError("transport is closed")
            self._loop.run_until_complete(self._client.write_gatt_char(
                JK_CHAR_UUID, build_command(command, value), response=False))

    def send_raw(self, frame: bytes) -> None:
        """Write a pre-built 20-byte command frame."""
        if self._loop is None or self._client is None:
            raise TransportError("transport is not open")
        if len(frame) != _CMD_LEN:
            raise TransportError(f"command frame must be {_CMD_LEN} bytes")
        with self._loop_lock:
            if self._closed.is_set():
                raise TransportError("transport is closed")
            self._loop.run_until_complete(self._client.write_gatt_char(
                JK_CHAR_UUID, frame, response=False))

    def _on_notify(self, _sender, data: bytearray) -> None:
        assert self._queue is not None
        self._queue.put_nowait(bytes(data))

    def _shutdown_loop(self) -> None:
        if self._loop is not None:
            self._loop.close()
            self._loop = None

    def close(self) -> None:
        self._closed.set()
        with self._loop_lock:
            if self._client is not None and self._loop is not None:
                try:
                    self._loop.run_until_complete(self._client.disconnect())
                except Exception:  # pragma: no cover - best effort teardown
                    pass
            self._client = None
            self._shutdown_loop()

    # -- data --------------------------------------------------------------
    def raw_stream(self, *, timeout_s: float | None = None) -> Iterator[bytes]:
        """Yield raw notification payloads as they arrive."""
        if self._loop is None or self._queue is None or self._client is None:
            raise TransportError("transport is not open")
        loop, queue, client = self._loop, self._queue, self._client

        async def next_chunk(budget: float) -> bytes | None:
            try:
                return await asyncio.wait_for(queue.get(), timeout=budget)
            except asyncio.TimeoutError:
                # Nudge the board; some firmware stops streaming unprompted.
                await client.write_gatt_char(
                    JK_CHAR_UUID, build_command(CMD_CELL_INFO), response=False)
                return None

        deadline = None if timeout_s is None else loop.time() + timeout_s
        while deadline is None or loop.time() < deadline:
            if self._closed.is_set():
                return
            budget = self.poll_interval_s
            if deadline is not None:
                budget = min(budget, max(0.01, deadline - loop.time()))
            with self._loop_lock:
                if self._closed.is_set():
                    return
                chunk = loop.run_until_complete(next_chunk(budget))
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
