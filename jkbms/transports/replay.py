"""Replay a captured byte stream as if it were a live link.

Every capture ``jkbms sniff`` writes can be fed back through the full decode,
verify and log pipeline. That means offset work, CSV column changes and test
coverage all happen away from the bench, with the pack disconnected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ..frames import Frame
from .base import Transport

__all__ = ["ReplayTransport", "load_capture"]


def load_capture(path: str | Path) -> bytes:
    """Load a capture written as raw binary, or as whitespace-separated hex."""
    data = Path(path).read_bytes()
    # A hex capture only ever contains hex digits and whitespace/punctuation.
    sample = data[:4096]
    if sample and all(c in b"0123456789abcdefABCDEF \t\r\n:,-" for c in sample):
        text = data.decode("ascii", "ignore")
        for sep in (":", ",", "-"):
            text = text.replace(sep, " ")
        return bytes.fromhex("".join(text.split()))
    return data


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
