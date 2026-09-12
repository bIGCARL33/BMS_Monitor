# Handoff — state of play

Written for whoever picks this up next, including a Claude Code session
running on the Jetson itself. Read `README.md` for what the tool does and
`docs/JETSON.md` for the platform specifics; this file is only *where things
stand and what to do next*.

---

## The setup, as confirmed on hardware

| | |
|---|---|
| Board | `JK_BD4A8S4P`, firmware **15.41**, serial `50913310848` |
| Hardware version | **`15H`** |
| Pack | 4S4P, HAKADI 21700, ~16–20 Ah |
| Link | **BLE only**, address `C8:47:80:46:6E:37` |
| Host | Jetson Orin Nano Dev Kit Super, JetPack 6 (Ubuntu 22.04, Python 3.10) |
| bleak | 3.0.2 from pip (Ubuntu 22.04 has no `python3-bleak`) |

**The UART path is closed on this board and is not worth revisiting.** The
hardware version has no "A", and JK's guidance is that PC connectivity over
UART requires one. A wired attempt returned zero bytes with a cleanly opened
port. `jkbms deviceinfo` reports this. The Jetson's 40-pin native UART is
irrelevant for the same reason.

---

## What is verified, and how

**Cell-info offsets: confirmed.** `jkbms probe` passes every cross-field
consistency check on real frames, and `--discover` — which assumes no layout
at all — independently lands on the same offsets (6, 150, 74, 76). Two
mechanisms that share no code agreeing on live data is the evidence.

**Device-info offsets: confirmed.** They are NUL-terminated ASCII, so a wrong
offset gives mojibake rather than a plausible wrong value. Reading
`JK_BD4A8S4P` at offset 6 is the proof. A real frame is committed at
`tests/fixtures/device_info_bd4a8s4p.hex`.

**BLE API: confirmed against bleak 3.0.2.** Every call site was checked
against the installed major version; `tests/test_ble_api.py` guards it.

## What is NOT verified — read before writing

**The write register numbers.** `jkbms/control.py` maps `charge` → `0x1D`,
`discharge` → `0x1E`, `balancer` → `0x1F`, and the two thresholds. These come
from community reverse-engineering and have **never been tested on firmware
15.41**. The 20-byte frame envelope *is* confirmed — it is the same one the
working reads use — but the register numbers are hypotheses.

This is why every write takes a settings backup first, then re-reads and diffs
to prove exactly the intended word changed. The diff distinguishes:

* *nothing changed* → register wrong for this firmware, **nothing was altered**
* *wrong field changed* → restore from backup, do not repeat
* *right field, wrong value* → reported as such

**The current field.** Every reading so far has been `0.000 A`, so its scale
and sign have never been exercised. Do not trust any capacity or energy figure
until a known load has confirmed that discharge reads **negative**.

**The settings-frame layout.** Far weaker evidence than the cell-info map.
`jkbms settings` deliberately leads with a word-by-word dump rather than a
decode, so offsets can be pinned by recognising values the operator already
knows.

---

## The pack, as of 2026-09-12

Two readings, three weeks apart, both at rest:

| | Aug 23 | Sep 12 | change |
|---|---|---|---|
| group 1 | 3.447 | 3.442 | −5 mV |
| group 2 | 3.446 | 3.442 | −4 mV |
| group 3 | 3.446 | 3.472 | **+26 mV** |
| group 4 | 3.446 | 3.473 | **+27 mV** |
| spread | 1.0 mV | **31.0 mV** | |

The split is clean: groups 1–2 low, 3–4 high, ~30.5 mV apart, almost nothing
within each pair. Random cell variance does not sort itself into halves.

A charger was briefly connected between the readings, which explains the
upward movement (passive balancing can only bleed cells down, never raise
them). It does not explain the pairing.

Three hypotheses, needing different responses:

1. **Real imbalance** — groups 1–2 self-discharging faster.
2. **Incomplete relaxation** after the partial charge.
3. **Measurement artifact** — sense-wire resistance or ADC offset on channels
   1–2. The suspiciously clean 2+2 split makes this worth ruling out.

**A multimeter across each group's taps distinguishes them.** If all four read
~3.457 V, the BMS's sensing is off and the imbalance is not real. This is the
single most useful physical measurement outstanding.

---

## Next steps, in order

1. **`./start.sh`** — clears anything holding the single BLE connection, runs
   `doctor`, confirms the board is advertising, opens the console.
2. **`settings save baseline.hex`** — the config export JK's software does not
   provide, and the only rollback that will exist.
3. **`settings`** — the word dump. Match values you know (cell count, any
   threshold) to pin the settings offsets from evidence.
4. **`set balancer on`** — the first write. The balancer is the right one to
   start with: it changes no threshold, it is what the 31 mV split needs, and
   flipping it back restores the previous state exactly. **Report exactly what
   the read-back verification says** — that result is what confirms or refutes
   register `0x1F` on this firmware.
5. **Multimeter cross-check** — per the table above, and the current sign
   under a known load.
6. **Log continuously** — `./start.sh --dashboard`, or install the systemd
   unit (`deploy/install-service.sh`). A single snapshot cannot distinguish
   drift from a step change; the imbalance question needs a time series.

---

## Things that have already cost time

* The board allows **one BLE connection**. A dashboard left running, or a
  `bluetoothctl` session, makes the board stop advertising — which surfaces as
  "Device ... was not found", not as anything mentioning a conflict.
  `start.sh` clears both.
* `python3-bleak` **does not exist** in Ubuntu 22.04. Use pip.
* The CSV logger **refuses to truncate** an existing log. Use `--append`,
  `--force`, or a dated filename.
* Python floor is **3.8** (JetPack 5 compatibility) and enforced by
  `tests/test_compat.py`. `int.bit_count()` is 3.10-only and once broke every
  decode on that platform.

---

## Repository

Branch `claude/jk-bms-laptop-monitoring-oo2fdt`. 224 tests, none needing
hardware: `python3 -m pytest tests/ -q`.

`https://github.com/bIGCARL33/BMS_Monitor` is empty — pushing from a machine
with the operator's own credentials is the way to fill it. Note the repo is
**public**, and the BLE address, BMS serial, and committed test fixture would
become public with it.
