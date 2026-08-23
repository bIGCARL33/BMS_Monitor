"""Command line interface.

Subcommands follow the order you should actually run them in:

    ports      list serial ports, so you know what to point --port at
    scan       scan for the BMS over BLE
    sniff      dump raw bytes to a capture file -- proves the BMS is talking
    probe      score candidate layouts against a capture, pick the right one
    monitor    print live readings
    log        write live readings to CSV

``probe`` sits deliberately between "can I see bytes" and "can I trust numbers".
Do not skip it: a stale offset produces plausible-looking wrong values, which is
worse than no data at all.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Iterator, Sequence

from .csvlog import CsvLogger
from .decode import decode
from .discover import discover
from .frames import Frame
from .profiles import PROFILES, candidate_profiles, get_profile
from .transports.base import Transport, TransportError
from .transports.replay import ReplayTransport, load_capture
from .verify import PackExpectation, verify

__all__ = ["main", "build_parser"]

_DEFAULT_CAPTURE = Path("captures/capture.bin")


# ---------------------------------------------------------------- helpers
def _expectation(args: argparse.Namespace) -> PackExpectation:
    return PackExpectation(cell_count=None if args.cells == 0 else args.cells)


def _open_transport(args: argparse.Namespace) -> Transport:
    """Build the transport the user asked for, with a clear error if under-specified."""
    if getattr(args, "replay", None):
        return ReplayTransport(args.replay)
    if getattr(args, "port", None):
        from .transports.serial_link import SerialTransport
        return SerialTransport(
            args.port, baud=args.baud, device_address=args.address,
            poll_interval_s=args.interval)
    if getattr(args, "ble", None):
        from .transports.ble_link import BleTransport
        return BleTransport(args.ble, poll_interval_s=args.interval)
    raise TransportError(
        "no link selected: pass --port COM3 (UART), --ble <address> (BLE), "
        "or --replay <capture file>")


def _resolve_profile(args: argparse.Namespace, transport_frames: list[Frame]):
    """Return the profile to decode with, auto-probing when asked to."""
    if args.profile != "auto":
        return get_profile(args.profile), None
    if not transport_frames:
        raise TransportError("no frames captured, cannot auto-select a profile")
    ranked = _rank_profiles(transport_frames[-1], _expectation(args))
    if not ranked:
        raise TransportError("no candidate layout could be scored")
    best_profile, best_result = ranked[0]
    return best_profile, best_result


def _rank_profiles(frame: Frame, expect: PackExpectation):
    """Score every candidate layout against ``frame``, best first."""
    scored = []
    for profile in candidate_profiles():
        reading = decode(frame, profile)
        if not reading.cells_v:
            continue
        result = verify(reading, expect)
        scored.append((profile, result))
    scored.sort(
        key=lambda pair: (pair[1].trustworthy, pair[1].consistency_score,
                          pair[1].score),
        reverse=True)
    return scored


def _collect(transport: Transport, *, count: int, timeout_s: float) -> list[Frame]:
    frames: list[Frame] = []
    with transport:
        for frame in transport.cell_info_frames(limit=count, timeout_s=timeout_s):
            frames.append(frame)
    return frames


# ---------------------------------------------------------------- commands
def cmd_ports(args: argparse.Namespace) -> int:
    try:
        from serial.tools import list_ports  # type: ignore[import-untyped]
    except ImportError:
        print("pyserial is not installed. Run: pip install pyserial", file=sys.stderr)
        return 2
    found = list(list_ports.comports())
    if not found:
        print("no serial ports found")
        return 1
    for port in found:
        print(f"{port.device:12s} {port.description}")
    print("\nNote: a port here only proves Windows sees the USB-TTL adapter.")
    print("It says nothing about whether the BMS is talking. Run 'sniff' next.")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    from .transports.ble_link import scan
    try:
        devices = scan(args.timeout)
    except TransportError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not devices:
        print("no BLE devices found. Is the pack awake and nothing else connected?")
        return 1
    for address, name, rssi in devices:
        rssi_text = f"{rssi:4d} dBm" if rssi is not None else "  ? dBm"
        print(f"{address}  {rssi_text}  {name or '(unnamed)'}")
    print("\nConnect with:  jkbms monitor --ble <address>")
    return 0


def cmd_sniff(args: argparse.Namespace) -> int:
    """Dump raw bytes to a file and report whether anything framed up.

    This is the command that answers the only question that matters at the
    start: is the BMS actually talking, or is the COM port just an idle adapter?
    """
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    transport = _open_transport(args)
    total = 0
    frames = 0
    types: dict[str, int] = {}
    deadline = time.monotonic() + args.timeout

    with transport, out.open("wb") as handle:
        stream = getattr(transport, "raw_stream", None)
        if stream is None:
            print("selected transport cannot dump raw bytes", file=sys.stderr)
            return 2
        for chunk in stream(timeout_s=args.timeout):
            handle.write(chunk)
            handle.flush()
            total += len(chunk)
            for frame in transport.assembler.feed(chunk):
                frames += 1
                types[frame.type_name] = types.get(frame.type_name, 0) + 1
                if frames == 1 and not args.quiet:
                    print(f"first frame ({len(frame)} bytes, {frame.type_name}):")
                    print(frame.hexdump())
                    print()
            if time.monotonic() >= deadline:
                break

    print(f"captured {total} bytes to {out}")
    if not total:
        print("\nNothing came back at all. Check, in this order:")
        print("  1. TX/RX crossed at the JST header")
        print("  2. GND connected; VBAT left disconnected")
        print("  3. adapter set to 3.3 V logic")
        print("  4. board protocol set to #001 (JK Modbus V1.0) -- if it is not,")
        print("     the UART path is closed and you need the BLE path instead")
        return 1
    print(f"framed {frames} record(s): "
          + (", ".join(f"{k}x{v}" for k, v in sorted(types.items())) or "none"))
    if not frames:
        print("\nBytes arrived but nothing checksummed. The link is alive but the")
        print("framing is off -- inspect the capture and try 'probe --discover'.")
        return 1
    print(f"\nNext:  jkbms probe --replay {out}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Score candidate layouts against real frames and report the evidence."""
    if args.replay:
        raw = load_capture(args.replay)
        transport = ReplayTransport(raw)
        frames = _collect(transport, count=args.count, timeout_s=0)
    else:
        frames = _collect(_open_transport(args), count=args.count,
                          timeout_s=args.timeout)

    if not frames:
        print("no cell-info frames available to probe", file=sys.stderr)
        return 1

    frame = frames[-1]
    print(f"probing against a {len(frame)}-byte {frame.type_name} frame "
          f"({len(frames)} captured)\n")

    if args.discover:
        print("== layout discovery (no profile assumed) ==")
        print(discover(frame.raw).report())
        print()

    print("== candidate profile scores ==")
    expect = _expectation(args)
    ranked = _rank_profiles(frame, expect)
    if not ranked:
        print("no candidate layout produced a usable cell block.")
        print("Run again with --discover and inspect the hexdump.")
        return 1

    for profile, result in ranked[: args.top]:
        print(result.report())
        print()

    best_profile, best_result = ranked[0]
    if best_result.trustworthy:
        print(f"CONFIRMED: {best_profile.name}")
        print("Every cross-field consistency check passed. The frame's own")
        print("redundancy agrees with this layout, so it is safe to log with:")
        print(f"  jkbms log --profile {best_profile.name} ...")
        return 0

    print(f"BEST GUESS: {best_profile.name} "
          f"(consistency {best_result.consistency_score:.2f}) -- NOT CONFIRMED")
    print("\nDo not log against this yet. Failing checks:")
    for check in best_result.failures:
        print(f"  - {check.name}: {check.detail}")
    print("\nCross-check the cell voltages against a multimeter on the cell taps")
    print("before trusting anything, and re-run with --discover to see where the")
    print("real fields actually are.")
    return 1


def _stream_readings(args: argparse.Namespace) -> Iterator:
    """Shared live pipeline for monitor and log."""
    transport = _open_transport(args)
    expect = _expectation(args)
    profile = None
    verified = False

    with transport:
        for frame in transport.cell_info_frames(timeout_s=args.timeout):
            if profile is None:
                profile, _ = _resolve_profile(args, [frame])
                print(f"# using profile: {profile.name}", file=sys.stderr)
            reading = decode(frame, profile)
            if not verified:
                result = verify(reading, expect)
                if result.trustworthy:
                    print("# layout confirmed by cross-field checks",
                          file=sys.stderr)
                else:
                    print("# WARNING: layout NOT confirmed -- values may be wrong.",
                          file=sys.stderr)
                    for check in result.failures:
                        print(f"#   {check.name}: {check.detail}", file=sys.stderr)
                    print("#   Run 'jkbms probe --discover' before trusting this.",
                          file=sys.stderr)
                verified = True
            yield reading


def cmd_monitor(args: argparse.Namespace) -> int:
    count = 0
    try:
        for reading in _stream_readings(args):
            print(f"{reading.timestamp.strftime('%H:%M:%S')}  {reading}")
            count += 1
            if args.count and count >= args.count:
                break
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    return 0 if count else 1


def cmd_log(args: argparse.Namespace) -> int:
    logger = CsvLogger(Path(args.output), include_resistance=args.resistance)
    # Rewrite one line in place on a terminal; on a redirect that would smear
    # every sample onto a single line, so fall back to one line per sample.
    interactive = sys.stdout.isatty()
    count = 0
    try:
        with logger:
            for reading in _stream_readings(args):
                logger.write(reading)
                count += 1
                if not args.quiet:
                    end, lead = ("", "\r") if interactive else ("\n", "")
                    print(f"{lead}{count} rows  {reading}", end=end, flush=True)
                if args.count and count >= args.count:
                    break
    except KeyboardInterrupt:
        pass
    if not args.quiet and interactive:
        print()
    print(f"wrote {count} rows to {args.output}")
    return 0 if count else 1


# ---------------------------------------------------------------- parser
def _add_link_args(parser: argparse.ArgumentParser, *, replay: bool = True) -> None:
    group = parser.add_argument_group("link")
    group.add_argument("--port", help="serial port, e.g. COM3 or /dev/ttyUSB0")
    group.add_argument("--baud", type=int, default=115200, help="serial baud (default 115200)")
    group.add_argument("--address", type=int, default=1,
                       help="Modbus slave address for the poll frame (default 1)")
    group.add_argument("--ble", metavar="ADDR", help="BLE address from 'jkbms scan'")
    if replay:
        group.add_argument("--replay", metavar="FILE",
                           help="replay a capture file instead of live hardware")
    group.add_argument("--interval", type=float, default=1.0,
                       help="seconds between polls (default 1.0)")


def _add_pack_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cells", type=int, default=4,
                        help="expected cell-group count, 0 to disable the check "
                             "(default 4, matching the 4S4P pack)")
    parser.add_argument("--profile", default="auto",
                        choices=["auto", *sorted(PROFILES)],
                        help="layout to decode with; 'auto' probes and picks the "
                             "best-scoring one (default auto)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jkbms",
        description="Monitor a JK BMS from a laptop over UART or BLE. No phone app.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ports", help="list serial ports")
    p.set_defaults(func=cmd_ports)

    p = sub.add_parser("scan", help="scan for the BMS over BLE")
    p.add_argument("--timeout", type=float, default=10.0, help="scan seconds")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("sniff", help="dump raw bytes and report framing")
    _add_link_args(p, replay=False)
    p.add_argument("-o", "--output", default=str(_DEFAULT_CAPTURE),
                   help=f"capture file (default {_DEFAULT_CAPTURE})")
    p.add_argument("--timeout", type=float, default=15.0, help="capture seconds")
    p.add_argument("--quiet", action="store_true", help="suppress the hexdump")
    p.set_defaults(func=cmd_sniff)

    p = sub.add_parser("probe", help="score candidate layouts against real frames")
    _add_link_args(p)
    _add_pack_args(p)
    p.add_argument("--count", type=int, default=3, help="frames to collect")
    p.add_argument("--timeout", type=float, default=15.0, help="collection seconds")
    p.add_argument("--top", type=int, default=3, help="how many candidates to print")
    p.add_argument("--discover", action="store_true",
                   help="also search the frame for fields with no profile assumed")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("monitor", help="print live readings")
    _add_link_args(p)
    _add_pack_args(p)
    p.add_argument("--count", type=int, default=0, help="stop after N samples")
    p.add_argument("--timeout", type=float, default=None, help="stop after N seconds")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("log", help="write live readings to CSV")
    _add_link_args(p)
    _add_pack_args(p)
    p.add_argument("-o", "--output", default="bms_log.csv", help="CSV path")
    p.add_argument("--count", type=int, default=0, help="stop after N samples")
    p.add_argument("--timeout", type=float, default=None, help="stop after N seconds")
    p.add_argument("--resistance", action="store_true",
                   help="include per-cell resistance columns")
    p.add_argument("--quiet", action="store_true", help="no progress line")
    p.set_defaults(func=cmd_log)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TransportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
