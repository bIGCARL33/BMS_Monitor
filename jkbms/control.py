"""Writing to the BMS: command construction, and the rules around it.

Reading a wrong offset gives a wrong number. Writing a wrong register changes
how a lithium pack's protection behaves, and JK's software has no config
export, so there is no undo unless one is built. That asymmetry sets the design
here.

**The frame format is known.** It is the same 20-byte structure the read
commands use, and those are confirmed working against the board -- so the
envelope is not in doubt.

**The register numbers are not.** They come from community reverse-engineering,
not a vendor spec, and this firmware (15.41) has not been checked against them.
Every register below is therefore a *hypothesis*.

What makes acting on a hypothesis acceptable is the verification loop, which
:class:`WritePlan` enforces rather than merely documents:

1. capture the settings frame first, and save it -- the undo that otherwise
   does not exist
2. send exactly one change
3. re-read the settings frame
4. diff. Exactly the intended word moved, or the write is reported as suspect.

A write that cannot be verified this way is refused. Anything touching a
protection threshold additionally requires the caller to pass
``i_understand_this_changes_protection=True``: those decide when the pack is
disconnected on over-voltage or under-voltage, and a wrong value there is a
safety problem, not an inconvenience.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .crc import sum8

__all__ = [
    "Register", "REGISTERS", "SWITCH_REGISTERS", "THRESHOLD_REGISTERS",
    "build_write_command", "WriteError", "WritePlan", "get_register",
]

_CMD_HEADER = b"\xAA\x55\x90\xEB"
_CMD_LEN = 20


class WriteError(RuntimeError):
    """Refused, or failed verification. Never raised for a confirmed write."""


@dataclass(frozen=True)
class Register:
    """A writable setting.

    ``code`` is the command byte; ``verify_offset`` is where the change is
    expected to appear in the settings frame, which is what makes the write
    checkable. A register with no ``verify_offset`` cannot be verified and so
    cannot be written.
    """

    name: str
    code: int
    #: Human description, shown in confirmation prompts.
    description: str
    #: Byte offset in the settings frame where this value is expected.
    verify_offset: int | None = None
    #: Inclusive range of accepted values, in the register's own raw units.
    minimum: int = 0
    maximum: int = 1
    #: Multiply a human value (volts, amps) by this to get the raw value.
    scale: float = 1.0
    unit: str = ""
    #: True when this decides how the pack protects itself.
    is_protection: bool = False

    @property
    def verifiable(self) -> bool:
        return self.verify_offset is not None

    def encode(self, human_value: float) -> int:
        raw = int(round(human_value / self.scale)) if self.scale != 1.0 \
            else int(human_value)
        if not self.minimum <= raw <= self.maximum:
            raise WriteError(
                f"{self.name}: {human_value}{self.unit} encodes to {raw}, outside "
                f"the accepted range {self.minimum}..{self.maximum}. Refusing.")
        return raw


#  Operational switches. These turn functions on and off; they do not change
#  any threshold, and flipping one back restores the previous state exactly.
#  That reversibility is why they are the right place to start writing.
SWITCH_REGISTERS = (
    Register("charge", 0x1D,
             "charge MOSFET -- allows current INTO the pack",
             verify_offset=118),
    Register("discharge", 0x1E,
             "discharge MOSFET -- allows current OUT of the pack",
             verify_offset=122),
    Register("balancer", 0x1F,
             "cell balancer (0.4 A passive)",
             verify_offset=126),
)

#  Protection thresholds. A wrong value here means the pack fails to disconnect
#  when it should. Gated behind an explicit acknowledgement.
THRESHOLD_REGISTERS = (
    Register("cell_ovp", 0x04, "per-cell over-voltage protection",
             verify_offset=18, minimum=2000, maximum=4500,
             scale=0.001, unit=" V", is_protection=True),
    Register("cell_uvp", 0x02, "per-cell under-voltage protection",
             verify_offset=10, minimum=1500, maximum=3600,
             scale=0.001, unit=" V", is_protection=True),
)

REGISTERS = {r.name: r for r in SWITCH_REGISTERS + THRESHOLD_REGISTERS}


def get_register(name: str) -> Register:
    try:
        return REGISTERS[name]
    except KeyError:
        known = ", ".join(sorted(REGISTERS))
        raise WriteError(f"unknown setting {name!r}. Known: {known}") from None


def build_write_command(code: int, value: int, length: int = 4) -> bytes:
    """Build the 20-byte write frame.

    Same envelope as the read commands, which are confirmed working against
    the board -- but byte 5 is not the fixed 0x00 the read commands use.
    Cross-checked against syssi/esphome-jk-bms's build_frame(), which sends
    byte 5 as the byte-width of the value being written (4 for every 32-bit
    register this project writes, 0 only for the argument-less read
    commands). This project originally always sent 0 here even for writes --
    every switch write before this fix went out with a byte 5 no real client
    of this protocol ever sends, which is one candidate explanation for why
    every one of them was silently ignored on firmware 15.41.
    """
    if not 0 <= code <= 0xFF:
        raise WriteError(f"command byte {code} out of range")
    if not 0 <= value <= 0xFFFFFFFF:
        raise WriteError(f"value {value} does not fit in a uint32")
    body = bytearray(_CMD_LEN - 1)
    body[0:4] = _CMD_HEADER
    body[4] = code
    body[5] = length
    body[6:10] = value.to_bytes(4, "little")
    return bytes(body) + bytes((sum8(body),))


@dataclass
class WritePlan:
    """One intended change, with the checks that must pass around it."""

    register: Register
    value: float
    #: Required for anything that changes how the pack protects itself.
    i_understand_this_changes_protection: bool = False
    _raw: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.register.verifiable:
            raise WriteError(
                f"{self.register.name} has no known location in the settings "
                f"frame, so a write to it could not be verified. Refusing: an "
                f"unverifiable write to a battery protection device is exactly "
                f"the thing this tool exists to avoid.")
        if self.register.is_protection and not self.i_understand_this_changes_protection:
            raise WriteError(
                f"{self.register.name} is a protection threshold -- it decides "
                f"when the pack disconnects. Pass "
                f"i_understand_this_changes_protection=True to proceed.")
        self._raw = self.register.encode(self.value)

    @property
    def raw_value(self) -> int:
        return self._raw

    def command(self) -> bytes:
        return build_write_command(self.register.code, self._raw)

    def describe(self) -> str:
        lines = [
            f"  setting   {self.register.name}  ({self.register.description})",
            f"  value     {self.value}{self.register.unit}  -> raw {self._raw}",
            f"  command   {self.command().hex(' ')}",
            f"  verify at settings offset {self.register.verify_offset}",
        ]
        if self.register.is_protection:
            lines.append("  *** this is a protection threshold ***")
        return "\n".join(lines)

    def check_result(self, before: bytes, after: bytes) -> tuple:
        """Compare settings frames around the write.

        Returns ``(ok, message)``. Not-ok means the board did something other
        than what was asked, and the operator needs to know immediately.
        """
        from .settings import diff_frames, render_diff

        changes = diff_frames(before, after)
        expected = self.register.verify_offset
        touched = {offset for offset, _, _ in changes}

        if not changes:
            return False, (
                "NOT APPLIED: no settings changed. The register number is "
                "probably wrong for this firmware, or the board rejected the "
                "write. Nothing was altered.")

        unexpected = touched - {expected}
        if unexpected:
            return False, (
                "UNEXPECTED CHANGE -- the board altered something other than "
                f"the intended field.\n{render_diff(changes)}\n"
                f"Expected only offset {expected}. Restore from your settings "
                "backup and do not repeat this write.")

        for offset, was, now in changes:
            if offset == expected:
                if now != self._raw:
                    return False, (
                        f"APPLIED BUT WRONG: offset {offset} became {now}, "
                        f"expected {self._raw} (was {was}).")
                return True, (
                    f"verified: offset {offset} {was} -> {now}, exactly as "
                    f"intended.")
        return False, "could not verify the change"


def format_switch_state(name: str, raw: int | None) -> str:
    if raw is None:
        return f"{name}: unknown"
    return f"{name}: {'ON' if raw else 'off'}"
