# JK BMS Laptop Monitor

Monitor a JK BMS (JK-BD4A8S4P, 4S4P NMC pack) from a laptop and log to CSV.
**No phone app anywhere in the loop.**

Developed against native Linux; the package itself is cross-platform (swap
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

Requires Python ≥ 3.10 (≥ 3.12 if you also want to try `aiobmsble`).

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
  csvlog.py         CSV writer
  cli.py            Command line interface
  transports/       serial (Modbus), BLE (bleak), replay
tests/              61 tests, no hardware required
docs/RUNBOOK.md     Bench procedure, wiring, electrical cautions
docs/PROTOCOL.md    What is verified vs. what is assumed
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
