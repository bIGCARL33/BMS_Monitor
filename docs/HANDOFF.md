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

**Writes do not work from this tool yet, and it is not a register-number
problem.** Session of 2026-09-12 settled this conclusively:

* The settings-frame *layout* for offsets 6–138 is now strongly corroborated
  against `syssi/esphome-jk-bms`'s `decode_jk02_settings_` (a maintained,
  widely-deployed open-source implementation of this same JK02 protocol) —
  every field from offset 6 to 74 matches name-for-name, and two of ours were
  wrong and got corrected: `charge`/`discharge`/`balancer` switches are at
  offsets 118/122/126 (not 122/126/130 — offset 130 is nominal battery
  capacity, confirmed by our own dump reading a plausible 40.000 Ah there,
  not a boolean).
* The write frame was missing its length byte (byte 5): every real write
  needs it set to the value's byte width (4, for every register here), not
  the 0 that only bare read commands use. Fixed in `build_write_command`,
  cross-checked against the same reference project's `build_frame()`.
* **Neither fix made a write actually take effect.** A benign, non-protection
  register (`0x20`, nominal capacity — chosen specifically because getting it
  wrong has zero safety impact) was written with the corrected frame, byte-
  perfect against a real device's captured traffic, and the board still
  silently ignored it. This ruled out "wrong register" and "board refuses
  writes while a fault is active" (tested after clearing an unrelated fault,
  see below) as explanations.
* **The actual cause: a BLE traffic capture from JK's own Android app
  (branded "EnjPower" for this board) shows its write frames carry ~9 extra
  non-zero bytes between the value and the checksum that this tool always
  sends as zero.** Confirmed by direct comparison: identical register,
  identical value, identical checksum scheme (plain sum8, verified correct
  byte-for-byte) — the only difference is those bytes, and only the app's
  write actually changed the setting. This is almost certainly some form of
  per-write authentication tied to the parameter password. The app's protocol
  logic is not in its Java layer (which is a thin `BluetoothGatt` wrapper) —
  it's compiled into native Qt/C++ libraries (`libenjpower_arm64-v8a.so`,
  `libprotocore.so`), likely driven by a data file rather than hardcoded
  constants (the native lib exports a generic `EProtoMatch`/`EProtoProto`
  protocol-description engine plus AES/file-decrypt routines, not per-device
  register tables). Finding the exact algorithm needs disassembling that
  native code or locating and decrypting whatever config it reads — not
  attempted; a real chunk of work with no guaranteed payoff.
* Until that auth mechanism is replicated, **every write from this tool will
  be silently ignored, and the code correctly reports this** (`NOT APPLIED:
  no settings changed`) rather than claiming success. Nothing has ever been
  altered on the pack by this tool's writes.

The write-side rules (backup before first write, read-back diff, refuse
anything without a known offset) all still apply and are exactly what caught
this — do not weaken them because writes don't work yet; the day they start
working is the day a silent wrong-register write becomes possible again.

**The current field.** Every reading so far has been `0.000 A`, so its scale
and sign have never been exercised. Do not trust any capacity or energy figure
until a known load has confirmed that discharge reads **negative**.

---

## A real fault, found and fixed: cell count 8 vs. actual 4 (2026-09-12)

While chasing the write problem above, the JK app's own alarm surfaced a
genuine, unrelated safety issue: **"Cell quantity abnormal."** The BMS was
configured for 8 series cells; this pack is 4S4P (4 series groups) — matching
both `docs/HANDOFF.md`'s own pack description and offset 114 in our settings
dump, which read `8`. With the fault active, `Chg`/`Dsg`/`Bal` all read OFF in
the app (all three had read ON minutes earlier via this tool, before the fault
tripped) — a live protection fault, not a communication error, and *also* a
strong candidate explanation for why even a harmless write was refused: a
faulted BMS refusing configuration changes is normal, sensible behavior,
though writes still failed identically after this was fixed (see above).

**Fixed via the app** (this tool cannot write settings yet): cell count
corrected from 8 to 4. Alarm cleared, all three switches returned to ON
immediately. The parameter password was also changed from its factory default
to something private during this session.

Worth re-checking whether this misconfiguration is connected to the 31 mV
group-imbalance question below — the prior two readings in that table were
both taken while the pack may have been running with this same wrong cell
count. Treat that table as pre-fix data; a fresh baseline post-fix would be
more trustworthy than extending the old series.

---

## The pack, as of 2026-09-12 — the 31 mV split looks resolved

Three readings now:

| | Aug 23 | Sep 12 (pre-fix) | Sep 12 (post cell-count fix) |
|---|---|---|---|
| group 1 | 3.447 | 3.442 | 3.418 |
| group 2 | 3.446 | 3.442 | 3.417 |
| group 3 | 3.446 | 3.472 | 3.413 |
| group 4 | 3.446 | 3.473 | 3.415 |
| spread | 1.0 mV | **31.0 mV** | **4–5 mV** |

The "pre-fix" reading was taken while the BMS had the cell-count-8-vs-4
misconfiguration described above (fault status at the time is unknown — it
may or may not have been actively tripped, but the wrong cell count was
already in effect). Immediately after correcting it, the spread dropped back
to roughly the Aug 23 baseline, with groups 3–4 coming *down* to meet 1–2
rather than 1–2 catching up. That fits hypothesis 3 below far better than a
real imbalance: a real 27 mV of extra charge in two groups doesn't just
disappear when an unrelated setting is corrected, but a sensing glitch tied
to the fault state would.

**Not fully closed** — this is one post-fix reading, and SOC/voltage moved
too (0% → 44%, ~13.8 V → 13.66 V), so some of this could be the pack simply
having relaxed further or discharged slightly in the intervening time, not
purely the fix. Treat "measurement artifact, now resolved" as the leading
hypothesis, not a confirmed conclusion.

Original hypotheses, for reference:

1. **Real imbalance** — groups 1–2 self-discharging faster. Now the weaker
   explanation.
2. **Incomplete relaxation** after a partial charge (the pre-fix reading
   followed a brief charger connection).
3. **Measurement artifact** tied to the cell-count fault — now the leading
   explanation.

**A multimeter across each group's taps still settles it definitively** if
there's any remaining doubt — this was never run. If a future reading shows
the split coming back, that would revive hypothesis 1 or 2.

---

## Next steps, in order

`./start.sh` → `settings save` → `settings` → `set balancer on` (the original
plan here) has already been run this session — see "What is NOT verified"
above for exactly what happened: the console-side bugs blocking it are fixed,
but the write itself is confirmed not to work yet, for reasons unrelated to
which register number is used. Re-running that sequence will reproduce the
same `NOT APPLIED` result until the auth mechanism is solved. What's actually
next:

1. **Decide on the write-auth reverse-engineering.** Either commit real time
   to disassembling `libenjpower_arm64-v8a.so` / `libprotocore.so`, or accept
   read-only + app-driven writes for now. Not a quick follow-up — see above.
2. **Multimeter cross-check** — per the table below, and the current sign
   under a known load. Unaffected by the write question and still the single
   most useful physical measurement outstanding.
3. **Log continuously, now that the cell-count fault is fixed** —
   `./start.sh --dashboard`, or install the systemd unit
   (`deploy/install-service.sh`). The prior imbalance readings were taken
   while the pack may have been misconfigured; a fresh time series is worth
   more than extending the old one.

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

Branch `claude/jk-bms-laptop-monitoring-oo2fdt`, pushed to
`https://github.com/bIGCARL33/BMS_Monitor` as of 2026-09-12. 224+ tests, none
needing hardware: `python3 -m pytest tests/ -q`. Note the repo is **public**,
and the BLE address, BMS serial, and committed test fixture are public with
it.

A decompiled copy of the EnjPower Android app (`enjpower-bms-*.apk`, used to
find the write-frame bytes above) is **not** part of this repo and should
stay out of it — it's the vendor's compiled software, not something to
redistribute. Any findings extracted from it belong here as prose/hex, not as
copied binaries or decompiled source.
