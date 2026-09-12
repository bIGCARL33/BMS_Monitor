# JK BMS Monitor

Monitor a JK BMS (JK-BD4A8S4P, 4S4P NMC pack) and log to CSV.
**No phone app anywhere in the loop.**

Runs on a laptop or on an always-on board — developed against native Linux on
both x86-64 and a Jetson Orin Nano (aarch64); cross-platform otherwise (swap
`/dev/ttyUSB0` for `COM3` on Windows).

Reads per-cell-group voltages, pack voltage, current, SOC and temperatures over
either the wired UART link or BLE, and writes a flat CSV shaped for import into
the Battery Pack Test Analyzer workbook.

## The design principle

A misaligned byte offset produces plausible-looking wrong numbers, which is
worse than no data. The JK02 byte map has shifted between firmware revisions, so
this tool never assumes a documented offset is correct. Instead it exploits the
fact that the frame carries redundant information:

- the pack voltage must equal the sum of the cell voltages
- the BMS's own average-cell field must equal the mean of the cell block
- its delta field must equal the block's max minus min
- reported power must equal |V × I|

Those are **cross-field consistency checks**, and a wrong offset fails them.
`jkbms probe` scores every candidate layout against a real frame and only says
`CONFIRMED` when the frame's own redundancy agrees. Until then it refuses to
call the numbers trustworthy.

There is also `probe --discover`, which finds the cell block and pack-voltage
field from first principles with no profile at all — the fallback if a firmware
turns up that matches nothing in `profiles.py`.

## Install

Requires **Python ≥ 3.8**, so it runs on JetPack 5 (Ubuntu 20.04) as well as
current desktops. `tests/test_compat.py` enforces that floor.

New machine? Run `jkbms doctor` first — it checks the interpreter, both link
libraries, the Bluetooth radio, serial ports and group membership, and prints
the fix for anything broken. Every check corresponds to a failure that
otherwise looks like a wiring fault.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"      # or ".[serial]" / ".[ble]" for just one path
```

### No venv? No problem

`python3 -m venv` fails on Debian/Ubuntu unless `python3-venv` is installed. You
do not need it: the core package has **no dependencies at all**, and only the
serial transport needs pyserial. From the project root:

```bash
sudo apt install -y python3-serial
python3 -m jkbms.cli ports
```

`python3 -m jkbms.cli` is the same program as the `jkbms` command. Everything in
this README works either way.

### Serial permissions

On Linux you also need permission to open the port:

```bash
sudo usermod -aG dialout $USER    # then log out and back in
```

Without it, opening the port fails in a way that looks exactly like a dead
link -- which is a genuinely expensive hour to lose.

## Use it in this order

```bash
jkbms ports                                      # what serial ports exist
jkbms sniff --port /dev/ttyUSB0 -o captures/run1.bin   # is the BMS talking?
jkbms probe --replay captures/run1.bin --discover
jkbms log --port /dev/ttyUSB0 -o run1.csv --profile jk02_32s
```

The middle step is the one not to skip.

### Path A — wired UART

```bash
jkbms sniff --port /dev/ttyUSB0
jkbms monitor --port /dev/ttyUSB0
```

Requires the board's UART protocol to be set to **#001 (JK BMS RS485 Modbus
V1.0)** and its device address to **0**. If the board didn't ship on #001 this
path is closed without one phone session, because setting the protocol requires
a working connection. It costs one command to find out — run `sniff` and see.

JK's own Monitor GUI is Windows-only, so on Linux it is not available as a
second opinion on decoded values. That leaves `probe` plus a multimeter as the
only verification route — see step 3 of the runbook.

### Path B — BLE (preferred, needs no board-side configuration)

```bash
jkbms scan
jkbms monitor --ble <address>
```

The BMS is already advertising, so there is nothing to configure. It accepts
**one BLE connection at a time** — close any other app holding the link. On
Linux, bleak talks to BlueZ, so `bluetooth.service` must be running.

### Replay — no hardware needed

Every capture can be fed back through the full pipeline:

```bash
jkbms probe --replay captures/run1.bin --discover
jkbms log   --replay captures/run1.bin -o run1.csv
```

Useful for working on offsets, CSV columns or tests with the pack disconnected.

## Commands

| Command | What it does |
|---|---|
| `ports` | List serial ports |
| `scan` | Scan for the BMS over BLE |
| `sniff` | Dump raw bytes to a file, report whether anything framed up |
| `probe` | Score candidate layouts against real frames; `--discover` for a no-profile search |
| `monitor` | Print live readings |
| `log` | Write live readings to CSV |
| `dashboard` | Live browser view at http://127.0.0.1:8765 |
| `deviceinfo` | Board model, firmware, and whether UART can work |
| `loopback` | Prove the USB adapter works, with the BMS disconnected |
| `doctor` | Check this machine can talk to the BMS at all |
| `settings` | Read/export the settings frame — the config export JK doesn't provide |
| `console` | Full-screen monitor **and control** in one terminal |
| `set` | Change one setting non-interactively (scriptable) |

## Changing settings

```bash
jkbms console --ble <address>
```

A full-screen terminal UI: live pack readout at the top, command prompt at the
bottom, no browser and no second machine. Cell voltages are drawn as deviation
from the pack mean for the same reason the web dashboard does it — four groups
within a millivolt make four identical full-length bars, which is worse than no
chart.

Commands: `status`, `cells`, `switches`, `settings`, `settings save FILE`,
`settings diff FILE`, and `set NAME VALUE` for the charge/discharge MOSFETs,
the balancer, and protection thresholds. Up/down recalls history.

`--plain` gives the line-by-line version instead, and it falls back there
automatically if the terminal cannot do curses or output is redirected.

### Scripting a change

A full-screen UI is for a human at a keyboard. For a script — or an agent —
there is a one-shot form:

```bash
jkbms set --ble <address> balancer on --yes
echo $?      # 0 verified, non-zero refused or unverified
```

`--yes` skips the prompt. It does **not** skip the settings backup or the
read-back verification, and the command refuses outright if the cell-info
layout is not confirmed on that board — writing to a link you do not yet
understand is the one thing never worth automating.

Writing to a BMS is not like reading from one. A wrong read offset gives a
wrong number; a wrong write changes how a lithium pack protects itself, and
JK's software has no config export, so there is no undo unless you build one.
Three rules follow, and the code enforces them rather than documenting them:

1. **A settings backup is taken before the first write of a session.** That
   file *is* the config export JK omits.
2. **Every write is verified by read-back.** Write, re-read the settings frame,
   diff. Exactly the intended word changed, or you are told loudly — including
   the case where the register number turns out to be wrong for your firmware
   and something else moved instead.
3. **A setting with no known location in the settings frame cannot be written
   at all**, because such a write could not be checked.

Protection thresholds (`cell_ovp`, `cell_uvp`) additionally require an explicit
acknowledgement and are range-clamped. The switches are the safe place to start:
flipping one back restores the previous state exactly.

**The register numbers are unverified on firmware 15.41.** They come from
community reverse-engineering. The read-back diff is what makes acting on them
defensible — it is designed to catch a wrong register on the first attempt.

## Live dashboard

```bash
jkbms dashboard --ble <address>              # opens a browser
jkbms dashboard --ble <address> -o run1.csv  # and log at the same time
```

Reads the pack here and serves a page on localhost -- a browser cannot open a
BLE or serial link itself. Binds to `127.0.0.1`, so it is not exposed to the
network. Standard library only; no web framework.

Preview it without hardware by replaying a capture at a realistic pace:

```bash
jkbms dashboard --replay captures/run1.bin --replay-pace 1.0
```

Cell voltages are drawn as **deviation from the pack mean**, not bars from
zero: four cells within a millivolt of each other make four identical
full-height bars, which is worse than no chart. Pack voltage and current get
separate charts rather than a shared dual axis, whose crossing points would
mean nothing. The page shouts if the layout is unconfirmed.

## CSV output

One row per sample, stable column order, units in the column names:

```
timestamp_iso, elapsed_s, pack_v, current_a, power_w, soc_pct,
remaining_ah, nominal_ah, cycles, temp_mos_c, temp1_c, temp2_c,
cell01_v … cellNN_v, cell_count, cell_min_v, cell_max_v, cell_avg_v,
cell_delta_mv, cell_sum_v, profile
```

Absent values are written blank rather than `0` — a spreadsheet skips blanks but
happily averages zeros into a wrong answer. `--resistance` adds per-cell
resistance columns. Rows are flushed as they are written, so an unplugged
adapter costs one sample, not the run.

## Layout

```
jkbms/
  crc.py            Modbus CRC-16 and the 8-bit additive checksum
  frames.py         55 AA EB 90 framing; length discovered by checksum
  profiles.py       Candidate byte-layouts — hypotheses, not truths
  decode.py         Frame + profile -> Reading
  verify.py         Cross-field consistency scoring
  discover.py       Find fields from first principles, no profile assumed
  deviceinfo.py     Board identity; self-verifying ASCII offsets
  doctor.py         Environment preflight
  csvlog.py         CSV writer
  dashboard.py      Live browser view (stdlib http.server)
  cli.py            Command line interface
  transports/       serial (Modbus), BLE (bleak), replay
tests/              159 tests, no hardware required
  fixtures/         a real frame from a JK-BD4A8S4P, firmware 15.41
deploy/             systemd unit + installer for always-on boards
docs/RUNBOOK.md     Bench procedure, wiring, electrical cautions
docs/PROTOCOL.md    What is verified vs. what is assumed
docs/JETSON.md      Running on a Jetson Orin Nano (headless, as a service)
```

## Safety

Read `docs/RUNBOOK.md` before connecting anything. In brief:

- **Do not connect VBAT** on the UART header — it carries full pack voltage.
  Some board revisions top-mount the connector, reversing the pinout end for
  end. Meter against B− first.
- Unplug the UART pigtail before powering the board; adapter TX back-feeds the
  logic rail through the input clamp diodes and can brown out the MCU.
- BMS UART ground sits at pack negative and there is no isolator in the loop.
  Run the laptop on battery, charger off the pack, during serial sessions.
- **Never press the LI-ION / LIFEPO4 / LTO preset buttons** after configuring —
  they overwrite everything, and JK's software has no config export.

## Testing

```bash
python -m pytest tests/ -q
```

Runs without hardware. Note the synthetic frames in `tests/synth.py` are built
*from* a profile, so they validate the decoder and the verifier — not the
offsets themselves. Only real hardware plus `probe` can do that.

## Always-on boards

For a Jetson, Pi or similar watching a pack continuously, see
[`docs/JETSON.md`](docs/JETSON.md). In short:

```bash
python3 -m jkbms.cli doctor                          # preflight
python3 -m jkbms.cli dashboard --ble <addr> --lan    # headless: view from a laptop
sudo ./deploy/install-service.sh <addr>              # run it at boot
```

The logger refuses to truncate an existing CSV — a discharge run cannot be
repeated from memory — so use `--append`, `--force`, or a dated name like
`-o 'pack-%Y%m%d-%H%M%S.csv'`. The systemd unit uses the last of those, which is
what makes a restart safe.
