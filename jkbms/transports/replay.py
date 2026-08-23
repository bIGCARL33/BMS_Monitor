"""Replay a captured byte stream as if it were a live link.

Every capture ``jkbms sniff`` writes can be fed back through the full decode,
verify and log pipeline. That means offset work, CSV column changes and test
coverage all happen away from the bench, with the pack disconnected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ..frames import Frame
from .base import Transport, TransportError

__all__ = ["ReplayTransport", "load_capture"]


def load_capture(path: str | Path) -> bytes:
    """Load a capture written as raw binary, or as whitespace-separated hex."""
    target = Path(path)
    if not target.exists():
        raise TransportError(
            f"no capture at {target}. Record one first:\n"
            f"  jkbms sniff --port /dev/ttyUSB0 -o {target}")
    if target.is_dir():
        raise TransportError(f"{target} is a directory, not a capture file")
    data = target.read_bytes()
    if not data:
        raise TransportError(
            f"{target} is empty -- the sniff captured no bytes, so there is "
            "nothing to replay")
    return _parse_hex(data) or data


def _parse_hex(data: bytes) -> bytes | None:
    """Interpret ``data`` as a hex-text capture, or None if it is binary.

    Captures worth keeping usually acquire a comment header saying what board
    and firmware they came from -- a capture nobody can identify later is worth
    much less -- so ``#`` lines are stripped before the hex is parsed.
    """
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return None

    body = " ".join(line.split("#", 1)[0] for line in text.splitlines())
    for separator in (":", ",", "-"):
        body = body.replace(separator, " ")
    digits = "".join(body.split())
    if not digits or len(digits) % 2:
        return None
    if any(c not in "0123456789abcdefABCDEF" for c in digits):
        return None
    return bytes.fromhex(digits)


class ReplayTransport(Transport):
    """Feed a previously captured stream through the normal frame pipeline."""

    def __init__(self, source: str | Path | bytes, *, chunk_size: int = 64) -> None:
        super().__init__()
        self._data = source if isinstance(source, bytes) else load_capture(source)
        self._chunk = chunk_size

    def open(self) -> None:  # pragma: no cover - nothing to do
        pass

    def close(self) -> None:  # pragma: no cover - nothing to do
        pass

    def frames(self, *, limit: int | None = None,
               timeout_s: float | None = None) -> Iterator[Frame]:
        count = 0
        for start in range(0, len(self._data), self._chunk):
            for frame in self.assembler.feed(self._data[start : start + self._chunk]):
                yield frame
                count += 1
                if limit is not None and count >= limit:
                    return
