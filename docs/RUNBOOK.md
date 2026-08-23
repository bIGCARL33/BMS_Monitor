# Bench runbook

Read the electrical section before connecting anything.

---

## Electrical cautions

**VBAT on the UART header carries full pack voltage.** The 4-pin 1.25 mm JST is
`GND / RX / TX / VBAT`. Connect GND, RX and TX only. Some board revisions
top-mount the connector, which reverses the pin order end for end — **meter each
pin against B− before plugging in**, every time, on every board.

**Unplug the UART pigtail before powering the board.** The adapter's TX line
back-feeds the BMS logic rail through the input clamp diodes and can bring the
MCU up browned out, which presents as a board that enumerates but never talks.

**No isolation in the loop.** BMS UART ground sits at pack negative and the
USB-TTL adapter has no isolator. Run the laptop **on battery** with its charger
unplugged, and keep any pack charger off, for the duration of a serial session.

**Power-up order:** B−, P−, B+, P+ first, then sampling connectors low → high,
then activate, then load or charger last. Teardown is the exact mirror.

**Never press the LI-ION / LIFEPO4 / LTO preset buttons** once configured. They
overwrite every setting, and JK's software has no config export — a screenshot of
the settings page is the only record you will have.

---

## First session

### 1. Establish that the BMS is talking

A device node appearing at `/dev/ttyUSB0` only proves the kernel sees the
USB-TTL adapter. It says nothing about the BMS.

```bash
jkbms ports
jkbms sniff --port /dev/ttyUSB0 -o captures/run1.bin
```

If this fails with a permission error, you are not in the `dialout` group:

```bash
id -nG | grep -qw dialout || sudo usermod -aG dialout $USER
```

Log out and back in afterwards -- group membership is only picked up at login.
A permission failure here looks identical to a dead link, so rule it out first.

**Nothing at all comes back** → work through, in order:

1. TX/RX crossed at the JST header
2. GND connected, VBAT *not* connected
3. adapter set to 3.3 V logic
4. board protocol set to #001 — if it isn't, this path is closed and you need
   Path B (BLE)

**Bytes arrive but nothing checksums** → the link is alive and the framing is
off. Keep the capture and go to step 2 with `--discover`.

If the UART path is dead, try BLE instead — it needs no board-side setup:

```bash
jkbms scan
jkbms sniff --ble <address> -o captures/run1.bin
```

On Linux this goes through BlueZ; check `systemctl status bluetooth` if the
scan finds nothing at all.

### 2. Establish that the numbers mean what you think

```bash
jkbms probe --replay captures/run1.bin --discover
```

`CONFIRMED` → the frame's own redundancy agrees with the layout. Proceed.

`NOT CONFIRMED` → **stop.** Do not log. Read the failing checks and the discovery
output; discovery reports where the cell block and pack-voltage field actually
are, which is usually enough to add or correct a profile in
`jkbms/profiles.py`.

### 3. Cross-check against physical reality

Consistency checks prove the layout is self-consistent. They cannot catch a
scale error that is wrong by the same factor everywhere.

On Windows you could cross-check against JK's Monitor GUI. That software is
Windows-only, so on Linux a multimeter is the only external reference you have.
Once per board, confirm:

- multimeter across each cell tap vs. the reported `cellNN_v`
- multimeter across the pack vs. `pack_v`
- a known load current vs. `current_a`, including its **sign** (discharge should
  read negative)

That takes five minutes once and retires the whole class of scale bugs.

### 4. Log

```bash
jkbms log --port /dev/ttyUSB0 -o run1.csv --profile jk02_32s --interval 1.0
```

Ctrl-C to stop. Rows are flushed as written, so an interrupted run keeps
everything up to the interruption.

---

## Configuration notes

Once connected, before drawing conclusions from SOC:

- **Cell count → 4**
- **Chemistry → Li-ion / NMC.** JK defaults are tuned for LFP and the NMC OCV
  curve is completely different, so SOC reads nonsense until this is right.
  Cell voltages are direct ADC reads and are correct regardless of this setting.
- **OVP ≈ 4.20–4.25 V**, **UVP ≈ 2.8–3.0 V**
- **Balance start** high in the charge curve, where the OCV slope is steep

Screenshot the settings page afterwards. There is no export.

**Balancing:** 0.4 A into a ~16–20 Ah parallel group is about 0.02 C. That holds
a balance; it will not establish one. Top-balance the four groups on the bench
first.

---

## Interpreting a log

- `cell_delta_mv` climbing under load points at a weak parallel group, not a
  balance problem.
- `pack_v` diverging from `cell_sum_v` mid-run means a sense-wire or a shifted
  offset — the logger records both columns precisely so this stays visible.
- `power_w` is the BMS's own figure, not `pack_v × current_a`. Comparing them is
  a free ongoing sanity check that the layout is still right.

---

## Out of scope

The separate 13S e-bike pack needs its own board — this one tops out at 8S.
