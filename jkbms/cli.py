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
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator, Sequence

from .csvlog import CsvLogError, CsvLogger
from .dashboard import ReadingBuffer, build_server
from .decode import decode
from .deviceinfo import decode_device_info
from .discover import discover
from .doctor import evaluate, probe_environment, report
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
        return ReplayTransport(args.replay,
                               pace_s=getattr(args, "replay_pace", 0.0) or 0.0)
    if getattr(args, "port", None):
        from .transports.serial_link import SerialTransport
        return SerialTransport(
            args.port, baud=args.baud, device_address=args.address,
            poll_interval_s=args.interval, legacy=getattr(args, "legacy", False))
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


def _log_path(raw: str) -> Path:
    """Expand strftime codes in an output path.

    Lets a long-running service write one file per start
    (``-o 'pack-%Y%m%d-%H%M%S.csv'``) so a restart can never land on top of the
    previous run.
    """
    return Path(datetime.now().strftime(raw))


def _primary_address() -> str:
    """Best guess at this machine's LAN address, for the headless case.

    Connecting a UDP socket sets a route without sending anything, which picks
    the interface the default route would use -- more reliable than
    gethostbyname, which often returns 127.0.1.1 on Debian derivatives.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))       # TEST-NET-1: routable, never real
        return probe.getsockname()[0]
    except OSError:
        return socket.gethostname()
    finally:
        probe.close()


# ---------------------------------------------------------------- commands
def cmd_loopback(args: argparse.Namespace) -> int:
    """Prove the adapter itself works, with the BMS out of the picture.

    A silent link has two possible causes and they need opposite responses:
    the adapter/driver/permissions are broken, or they are fine and the BMS
    simply is not answering. Jumpering the adapter's own TX to its own RX
    separates them in ten seconds, which beats re-checking wiring against a
    board that was never going to reply.
    """
    try:
        import serial  # type: ignore[import-untyped]
    except ImportError:
        print("pyserial is not installed.", file=sys.stderr)
        return 2

    print(f"Loopback test on {args.port} at {args.baud} baud.")
    print("Jumper the adapter's TX pin directly to its own RX pin.")
    print("Disconnect it from the BMS first -- this tests the adapter alone.\n")
    if not args.yes:
        try:
            input("Press Enter when the jumper is in place (Ctrl-C to abort)... ")
        except (KeyboardInterrupt, EOFError):
            print("\naborted")
            return 1

    probe = b"JKBMS-LOOPBACK-" + bytes(range(16))
    try:
        with serial.Serial(args.port, args.baud, timeout=1.0) as link:
            link.reset_input_buffer()
            link.write(probe)
            link.flush()
            echo = link.read(len(probe))
    except Exception as exc:
        print(f"error: cannot open {args.port}: {exc}", file=sys.stderr)
        return 2

    if echo == probe:
        print(f"PASS -- {len(echo)} bytes echoed intact.")
        print("The adapter, its driver and your permissions all work.")
        print("So a silent link is the BMS not answering, not your USB side.")
        print("Next: check TX/RX crossing at the JST, then the board's protocol"
              " setting,\nor switch to BLE, which needs no board-side config.")
        return 0

    if not echo:
        print("FAIL -- nothing came back.")
        print("With TX jumpered to RX the adapter must hear itself, so this is")
        print("the adapter, its driver, or the jumper -- not the BMS.")
        print("Check the jumper is on the adapter's own TX and RX pins.")
    else:
        print(f"PARTIAL -- sent {len(probe)} bytes, got {len(echo)} back:")
        print(f"  sent {probe.hex(' ')}")
        print(f"  got  {echo.hex(' ')}")
        print("Garbled echo usually means a baud or voltage-level problem.")
    return 1


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check the environment and print the fix for anything broken.

    Worth running first on any new machine. Every check here corresponds to a
    failure that otherwise looks like a wiring fault.
    """
    findings = evaluate(probe_environment())
    print(report(findings))
    return 1 if any(f.failed for f in findings) else 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Serve a live browser view of the pack.

    The reading happens here because a browser cannot open a BLE or serial
    link; the page just renders what this process already holds.
    """
    import threading
    import webbrowser

    buffer = ReadingBuffer()
    logger = (CsvLogger(_log_path(args.output), append=args.append,
                        overwrite=args.force) if args.output else None)
    stop = threading.Event()

    def reader() -> None:
        try:
            for reading in _stream_readings(args, buffer=buffer):
                if logger is not None:
                    logger.write(reading)
                if stop.is_set():
                    return
        except TransportError as exc:
            buffer.set_error(str(exc))
        except Exception as exc:  # pragma: no cover - hardware dependent
            buffer.set_error(f"{type(exc).__name__}: {exc}")

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    host = "0.0.0.0" if args.lan else args.host
    try:
        server = build_server(buffer, host, args.http_port)
    except OSError as exc:
        stop.set()
        raise TransportError(
            f"cannot bind {host}:{args.http_port}: {exc}. "
            f"Another dashboard may already be running; try --http-port "
            f"{args.http_port + 1}.") from exc

    local_url = f"http://127.0.0.1:{args.http_port}/"
    if host == "0.0.0.0":
        # A headless board is the whole reason for this mode, so print the
        # address the *other* machine needs rather than a useless 0.0.0.0 URL.
        addr = _primary_address()
        print(f"dashboard on http://{addr}:{args.http_port}/  (from another machine)")
        print(f"            {local_url}  (on this machine)")
        print("NOTE: reachable by anyone on this network. There is no auth, and")
        print("      the page exposes pack telemetry only -- but it is open.")
    else:
        print(f"dashboard on {local_url}")
    if logger is not None:
        print(f"also logging to {args.output}")
    print("Ctrl-C to stop.")
    # Opening a browser on a headless board just logs an error.
    if not args.no_browser and host != "0.0.0.0" and os.environ.get("DISPLAY"):
        threading.Timer(0.5, lambda: webbrowser.open(local_url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        stop.set()
        server.server_close()
        if logger is not None:
            logger.close()
            print(f"wrote {logger.rows_written} rows to {args.output}")
    return 0


def cmd_deviceinfo(args: argparse.Namespace) -> int:
    """Print the board's identity, firmware, and whether UART can work.

    Worth running first on any new board: the hardware-version string decides
    whether the wired path is even possible, which otherwise costs an evening
    of wiring checks to discover.
    """
    if args.replay:
        from .frames import FrameAssembler
        frames = [f for f in FrameAssembler().feed(load_capture(args.replay))
                  if f.type_byte == 0x03]
        if not frames:
            print("no device-info frame in that capture. Record a fresh one -- "
                  "the board sends it on connect.", file=sys.stderr)
            return 1
    else:
        transport = _open_transport(args)
        frames = []
        with transport:
            for frame in transport.frames(timeout_s=args.timeout):
                if frame.type_byte == 0x03:
                    frames.append(frame)
                    break
        if not frames:
            print("no device-info frame arrived", file=sys.stderr)
            return 1

    print(decode_device_info(frames[-1]).report())
    return 0


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
    # Built-in 16550 UARTs clutter the list on Linux and are never the adapter;
    # show them, but put the USB devices where they can be found.
    usb = [p for p in found if "USB" in p.device or "ACM" in p.device]
    for port in usb + [p for p in found if p not in usb]:
        print(f"{port.device:12s} {port.description}")
    if usb:
        print(f"\nLikely adapter: {usb[0].device} ({usb[0].description})")
    print("\nNote: a port here only proves the OS sees the USB-TTL adapter.")
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
    identified = [d for d in devices if d[3]]
    for address, name, rssi, is_jk in devices:
        rssi_text = f"{rssi:4d} dBm" if rssi is not None else "  ? dBm"
        mark = "JK " if is_jk else "  ?"
        print(f"{mark} {address}  {rssi_text}  {name or '(unnamed)'}")

    best = devices[0]
    if identified:
        print(f"\nIdentified a JK board at {best[0]}.")
    else:
        # Be explicit that this is a fallback listing. Presenting an unrelated
        # device as the BMS wastes far more time than saying "unconfirmed".
        print("\nNo device advertised JK's service UUID or naming, so the above")
        print("is every named device found -- none is confirmed to be the BMS.")
        print("The board may advertise under a serial number; sniff will tell you")
        print("for certain, because only the BMS answers with 55 AA EB 90.")
    print("\nTry:")
    print(f"  jkbms sniff --ble {best[0]} -o captures/ble1.bin")
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


def _stream_readings(args: argparse.Namespace, *,
                     buffer: "ReadingBuffer | None" = None) -> Iterator:
    """Shared live pipeline for monitor, log and the dashboard."""
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
                if buffer is not None:
                    buffer.set_verification(result)
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
            if buffer is not None:
                buffer.add(reading)
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
    out_path = _log_path(args.output)
    logger = CsvLogger(out_path, include_resistance=args.resistance,
                       append=args.append, overwrite=args.force)
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
    print(f"wrote {count} rows to {out_path}")
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
        group.add_argument("--replay-pace", type=float, default=0.0, metavar="SEC",
                           help="seconds between replayed frames; use with "
                                "'dashboard' to preview it without hardware")
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

    p = sub.add_parser("doctor",
                       help="check this machine can talk to the BMS at all")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("dashboard", help="live browser view of the pack")
    _add_link_args(p)
    _add_pack_args(p)
    p.add_argument("--http-port", type=int, default=8765, help="web port (default 8765)")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default 127.0.0.1, local only)")
    p.add_argument("--lan", action="store_true",
                   help="bind all interfaces so another machine can view it -- "
                        "the usual choice on a headless board. No auth.")
    p.add_argument("-o", "--output", default=None,
                   help="also log to this CSV while the dashboard runs")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser")
    p.add_argument("--append", action="store_true",
                   help="continue an existing CSV instead of refusing")
    p.add_argument("--force", action="store_true", help="overwrite an existing CSV")
    p.add_argument("--timeout", type=float, default=None, help="stop after N seconds")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("deviceinfo",
                       help="print board model, firmware, and UART capability")
    _add_link_args(p)
    p.add_argument("--timeout", type=float, default=20.0, help="seconds to wait")
    p.set_defaults(func=cmd_deviceinfo)

    p = sub.add_parser("loopback",
                       help="prove the adapter works, with the BMS disconnected")
    p.add_argument("--port", required=True, help="serial port, e.g. /dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=115200, help="baud (default 115200)")
    p.add_argument("-y", "--yes", action="store_true",
                   help="skip the 'jumper in place?' prompt")
    p.set_defaults(func=cmd_loopback)

    p = sub.add_parser("scan", help="scan for the BMS over BLE")
    p.add_argument("--timeout", type=float, default=10.0, help="scan seconds")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("sniff", help="dump raw bytes and report framing")
    _add_link_args(p, replay=False)
    p.add_argument("-o", "--output", default=str(_DEFAULT_CAPTURE),
                   help=f"capture file (default {_DEFAULT_CAPTURE})")
    p.add_argument("--timeout", type=float, default=15.0, help="capture seconds")
    p.add_argument("--quiet", action="store_true", help="suppress the hexdump")
    p.add_argument("--legacy", action="store_true",
                   help="poll with the legacy 4E 57 frame instead of Modbus, to "
                        "check whether this board answers it at all")
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
    p.add_argument("--append", action="store_true",
                   help="continue an existing CSV instead of refusing")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing CSV")
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
    except CsvLogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
