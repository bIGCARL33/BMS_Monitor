# Protocol notes

Split deliberately into what is **verified**, what is **reported by others**, and
what is **assumed**. The distinction matters: the assumed parts are exactly where
a silently-wrong reading comes from.

---

## Verified in this repo

### The poll frame is standard Modbus RTU

The frame captured from JK's Windows Monitor:

```
01 10 16 20 00 01 02 00 00 D6 F1
```

decodes cleanly as Modbus RTU, and its CRC-16 checks out
(`tests/test_crc.py::test_observed_poll_frame_has_valid_modbus_crc`):

| Bytes | Meaning |
|---|---|
| `01` | slave address 1 |
| `10` | function 0x10, write multiple registers |
| `16 20` | register 0x1620 |
| `00 01` | one register |
| `02` | two data bytes |
| `00 00` | value 0x0000 |
| `D6 F1` | CRC-16 `0xF1D6`, little-endian |

Because the CRC verifies, `build_poll_frame()` **constructs** this rather than
replaying a magic constant, which makes the device address a parameter.

### Framing is self-validating

Records begin `55 AA EB 90`, carry a type byte at offset 4 and a rolling counter
at offset 5, and end with an 8-bit additive checksum over everything before it.
Record length varies by firmware and link (300 / 308 / 320 all reported), so the
assembler tries each candidate and accepts the first whose checksum passes.
A wrong length fails the checksum rather than yielding a shifted record.

Frame types: `0x01` settings, `0x02` cell info, `0x03` device info.

---

## Reported by others, not verified here

- This board is in the **JK02_32S** family (modern JK format).
- Serial is **115200 8N1**.
- The legacy `4E 57` command frames get **no response** on this model. Older
  Python libraries built around them time out silently — which looks like a
  wiring fault and is not one.
- JK's GUI needs UART protocol **#001** and device address **0**; on a non-zero
  address it connects but greys out everything except the Parallel tab.
- JK's guidance is that PC connectivity requires an "A" in the hardware version
  string.

---

## Assumed — treat as hypotheses

Everything in `jkbms/profiles.py`. These offsets come from community
reverse-engineering, not a vendor spec, and the map has shifted between firmware
revisions.

`jk02_32s` (the expected default):

| Field | Offset | Type | Scale |
|---|---|---|---|
| cell voltages | 6 | 32 × u16 LE | mV |
| cell enable mask | 70 | u32 | bitmask |
| average cell | 74 | u16 | mV |
| delta cell | 76 | u16 | mV |
| max / min cell index | 78 / 79 | u8 | — |
| cell resistances | 80 | 32 × u16 | mΩ |
| MOS temperature | 144 | i16 | 0.1 °C |
| pack voltage | 150 | u32 | mV |
| power | 154 | u32 | mW |
| current | 158 | i32 | mA (signed: negative = discharge) |
| temp 1 / temp 2 | 162 / 164 | i16 | 0.1 °C |
| SOC | 173 | u8 | % |
| remaining capacity | 174 | u32 | mAh |
| nominal capacity | 178 | u32 | mAh |
| cycle count | 182 | u32 | — |

`jk02_24s` is the older 24-slot variant, kept as a candidate so `probe` can rule
it out from evidence rather than assumption.

### How the assumptions get tested

Cell offset 6 is near-certain — it is simply `4 header + 1 type + 1 counter`.
The **tail** (pack voltage onward) is what moves, so `Profile.tail_shifted()`
generates ±8-byte variants and `probe` scores them all.

Scoring uses the frame's internal redundancy:

| Check | Weight | Kind |
|---|---|---|
| `pack_v == sum(cells)` | 4.0 | consistency |
| `bms_avg == mean(cells)` | 3.0 | consistency |
| `bms_delta == max−min(cells)` | 3.0 | consistency |
| `power == \|V×I\|` (skipped near 0 A) | 2.0 | consistency |
| cell count matches pack | 2.0 | consistency |
| values in physical range | 0.5–1.0 each | plausibility |

Only **consistency** checks discriminate. Plausibility checks are necessary but
nowhere near sufficient — a garbage offset lands on a physically-plausible number
all the time, which is the exact failure mode being guarded against. A layout is
`CONFIRMED` only when at least two consistency checks were applicable and all of
them passed.

`probe --discover` goes further and assumes nothing: it scans for a run of
consecutive u16 values inside the lithium window that cluster within 600 mV, then
looks for a u32 elsewhere matching their sum and u16s matching their mean and
spread. If discovery and a profile independently agree on an offset, that is
strong corroboration.

---

## Wiring

4-pin 1.25 mm JST: **GND / RX / TX / VBAT**. Cross TX↔RX.

**Do not connect VBAT — it carries full pack voltage.** Some board revisions
top-mount the connector, which flips the pinout end for end. Meter against B−
before plugging in.

---

## BLE

- Service `0000ffe0-…`, characteristic `0000ffe1-…` (write + notify).
- Commands are 20 bytes: `AA 55 90 EB <cmd> <len> <u32 value LE>` zero-padded to
  19, then an 8-bit additive checksum.
- `0x97` = device info, `0x96` = cell info.
- The board wants a device-info request before it streams cell info; skipping it
  yields a connected-but-silent link.
- Notifications arrive in 20-byte fragments that concatenate into the same
  `55 AA EB 90` records, so everything downstream is shared with the UART path.
- **One BLE connection at a time.**
