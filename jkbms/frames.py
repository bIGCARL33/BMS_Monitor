"""Framing for the modern JK response protocol (``55 AA EB 90``).

The BMS answers with fixed-length records that begin with a four byte header,
carry a frame-type byte and a rolling counter, and end with an 8-bit additive
checksum over everything that precedes it.

Documented record lengths vary between firmware revisions and between the BLE
and UART links (300 and 320 bytes are both reported in the wild). Rather than
hardcode one, the assembler tries every candidate length and accepts the first
whose trailing checksum validates. That makes framing self-verifying: a wrong
length simply fails the checksum instead of silently yielding a shifted record.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Iterator, Sequence

from .crc import check_sum8

__all__ = [
    "HEADER",
    "CANDIDATE_LENGTHS",
    "FrameType",
    "Frame",
    "FrameAssembler",
]

HEADER = b"\x55\xAA\xEB\x90"

#: Record lengths to try, shortest first. Extend via ``FrameAssembler(lengths=...)``
#: if a firmware revision turns up that uses something else.
CANDIDATE_LENGTHS: tuple[int, ...] = (300, 308, 320)

#: Beyond this much unparsed data we assume the leading header was spurious and
#: resynchronise on the next one.
_MAX_SLACK = 64


class FrameType(enum.IntEnum):
    """Value of byte 4 of a response record."""

    SETTINGS = 0x01
    CELL_INFO = 0x02
    DEVICE_INFO = 0x03

    @classmethod
    def describe(cls, value: int) -> str:
        try:
            return cls(value).name
        except ValueError:
            return f"UNKNOWN_0x{value:02X}"


@dataclass(frozen=True)
class Frame:
    """One checksum-validated response record."""

    raw: bytes

    @property
    def type_byte(self) -> int:
        return self.raw[4]

    @property
    def type_name(self) -> str:
        return FrameType.describe(self.type_byte)

    @property
    def counter(self) -> int:
        """Rolling frame counter the BMS increments per record."""
        return self.raw[5]

    @property
    def is_cell_info(self) -> bool:
        return self.type_byte == FrameType.CELL_INFO

    def hexdump(self, width: int = 16) -> str:
        """Classic offset/hex/ascii dump, for eyeballing an unknown layout."""
        lines = []
        for start in range(0, len(self.raw), width):
            chunk = self.raw[start : start + width]
            hexpart = " ".join(f"{b:02X}" for b in chunk)
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"{start:04d}  {hexpart:<{width * 3 - 1}}  {text}")
        return "\n".join(lines)

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self.raw)


@dataclass
class FrameAssembler:
    """Incremental byte-stream to :class:`Frame` converter.

    Feed it whatever arrives from a serial port or BLE notification; it buffers,
    synchronises on the header, and yields only records whose checksum passes.
    """

    lengths: Sequence[int] = CANDIDATE_LENGTHS
    _buffer: bytearray = field(default_factory=bytearray, init=False, repr=False)
    #: Bytes thrown away while hunting for a valid header. Non-zero after a
    #: resync, which is worth surfacing when debugging a noisy link.
    discarded: int = field(default=0, init=False)

    def feed(self, data: bytes) -> list[Frame]:
        """Append ``data`` and return every complete frame now available."""
        self._buffer.extend(data)
        return list(self._drain())

    def reset(self) -> None:
        self._buffer.clear()

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def _drain(self) -> Iterator[Frame]:
        max_len = max(self.lengths)
        while True:
            start = self._buffer.find(HEADER)
            if start < 0:
                # Keep the last few bytes: a header may be split across reads.
                keep = len(HEADER) - 1
                if len(self._buffer) > keep:
                    self.discarded += len(self._buffer) - keep
                    del self._buffer[:-keep]
                return

            if start:
                self.discarded += start
                del self._buffer[:start]

            frame = self._try_frame()
            if frame is not None:
                yield frame
                continue

            # Nothing validated. If we still might just be short of data, wait.
            if len(self._buffer) < max_len + _MAX_SLACK:
                return

            # Enough data went by without a valid checksum: this header was
            # noise or a truncated record. Skip it and hunt for the next one.
            self.discarded += len(HEADER)
            del self._buffer[: len(HEADER)]

    def _try_frame(self) -> Frame | None:
        for length in sorted(self.lengths):
            if len(self._buffer) < length:
                break
            candidate = bytes(self._buffer[:length])
            if check_sum8(candidate):
                del self._buffer[:length]
                return Frame(candidate)
        return None
