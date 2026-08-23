"""Laptop-side monitoring for JK BMS boards in the modern (JK02) protocol family.

Targets the JK-BD4A8S4P on a 4S4P NMC pack, over either BLE or the UART link,
with no phone app anywhere in the loop.

The design principle throughout: never trust a documented byte offset. Capture a
frame, score candidate layouts against the frame's own internal redundancy, and
only log once a layout is confirmed. See :mod:`jkbms.verify` and
:mod:`jkbms.discover`.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .crc import check_modbus_crc, crc16_modbus, sum8
from .decode import Reading, decode
from .discover import discover, find_cell_blocks
from .frames import Frame, FrameAssembler, FrameType
from .profiles import PROFILES, Profile, get_profile
from .verify import PackExpectation, verify

__all__ = [
    "__version__",
    "Frame", "FrameAssembler", "FrameType",
    "Profile", "PROFILES", "get_profile",
    "Reading", "decode",
    "verify", "PackExpectation",
    "discover", "find_cell_blocks",
    "crc16_modbus", "check_modbus_crc", "sum8",
]
