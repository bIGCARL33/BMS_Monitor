"""Common transport interface.

A transport's only job is to produce checksum-validated :class:`~jkbms.frames.Frame`
objects. Decoding, verification and logging sit above it and are identical
whether the bytes arrived over UART, BLE, or a replayed capture file.
"""

from __future__ import annotations

import abc
from typing import Iterator

from ..frames import Frame, FrameAssembler

__all__ = ["Transport", "TransportError"]


class TransportError(RuntimeError):
    """Raised when a link cannot be opened or produces no usable data."""


class Transport(abc.ABC):
    """Base class: open a link, yield frames, close."""

    def __init__(self) -> None:
        self.assembler = FrameAssembler()

    @abc.abstractmethod
    def open(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    @abc.abstractmethod
    def frames(self, *, limit: int | None = None,
               timeout_s: float | None = None) -> Iterator[Frame]:
        """Yield frames as they arrive, stopping after ``limit`` if given."""

    def cell_info_frames(self, *, limit: int | None = None,
                         timeout_s: float | None = None) -> Iterator[Frame]:
        """Yield only cell-info frames, honouring ``limit`` on those alone."""
        seen = 0
        for frame in self.frames(timeout_s=timeout_s):
            if not frame.is_cell_info:
                continue
            yield frame
            seen += 1
            if limit is not None and seen >= limit:
                return

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
