"""Build synthetic JK cell-info frames for testing.

These are constructed *from* a profile, so a test that decodes one under the
same profile is checking the decoder, not the offsets. To check the offsets you
need real hardware -- that is what ``jkbms probe`` is for.

The value of a synthetic frame here is that it is internally consistent in
exactly the way a real one is: pack voltage equals the cell sum, the average and
delta fields agree with the block. So the verifier can be tested for both the
"layout is right" and "layout is wrong" cases.
"""

from __future__ import annotations

import struct

from jkbms.crc import sum8
from jkbms.frames import HEADER
from jkbms.profiles import Field, Profile

__all__ = ["build_cell_info_frame"]


def _put(buf: bytearray, spec: Field | None, si_value: float) -> None:
    """Write an SI value into ``buf`` at ``spec``, undoing the profile's scale."""
    if spec is None:
        return
    raw = int(round(si_value / spec.scale))
    fmt = {"u8": "<B", "u16": "<H", "u32": "<I", "i16": "<h", "i32": "<i"}[spec.kind]
    struct.pack_into(fmt, buf, spec.offset, raw)


def build_cell_info_frame(
    profile: Profile,
    cells_v: list[float],
    *,
    current_a: float = -2.5,
    soc_pct: int = 78,
    temp1_c: float = 24.5,
    temp2_c: float = 25.1,
    temp_mos_c: float = 27.0,
    remaining_ah: float = 14.2,
    nominal_ah: float = 18.0,
    cycles: int = 37,
    resistance_ohm: float = 0.152,
    length: int = 300,
    counter: int = 5,
    pack_v_override: float | None = None,
) -> bytes:
    """Produce a checksum-valid cell-info record laid out per ``profile``."""
    buf = bytearray(length)
    buf[0:4] = HEADER
    buf[4] = 0x02          # cell info
    buf[5] = counter

    for index, volts in enumerate(cells_v):
        struct.pack_into("<H", buf, profile.cells_offset + 2 * index,
                         int(round(volts * 1000)))

    if profile.resistance_offset is not None:
        for index in range(len(cells_v)):
            struct.pack_into("<H", buf, profile.resistance_offset + 2 * index,
                             int(round(resistance_ohm * 1000)))

    if profile.cell_enable_mask is not None:
        mask = (1 << len(cells_v)) - 1
        struct.pack_into("<I", buf, profile.cell_enable_mask.offset, mask)

    pack_v = pack_v_override if pack_v_override is not None else sum(cells_v)
    _put(buf, profile.pack_v, pack_v)
    _put(buf, profile.current_a, current_a)
    _put(buf, profile.power_w, abs(pack_v * current_a))
    _put(buf, profile.soc_pct, soc_pct)
    _put(buf, profile.remaining_ah, remaining_ah)
    _put(buf, profile.nominal_ah, nominal_ah)
    _put(buf, profile.cycles, cycles)
    _put(buf, profile.temp_mos_c, temp_mos_c)
    _put(buf, profile.temp1_c, temp1_c)
    _put(buf, profile.temp2_c, temp2_c)
    _put(buf, profile.avg_cell_v, sum(cells_v) / len(cells_v))
    _put(buf, profile.delta_cell_v, max(cells_v) - min(cells_v))
    _put(buf, profile.max_cell_index, cells_v.index(max(cells_v)))
    _put(buf, profile.min_cell_index, cells_v.index(min(cells_v)))

    buf[-1] = sum8(bytes(buf[:-1]))
    return bytes(buf)
