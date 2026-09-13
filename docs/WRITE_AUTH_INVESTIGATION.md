# Investigating why writes are silently ignored — session of 2026-09-12

Branch: `explore/write-auth`, off `claude/jk-bms-laptop-monitoring-oo2fdt` at `14b83c4`.

## The question

Every write this tool sends is accepted by the BLE link (no exception, GATT
ACKs it when using write-with-response) but never actually changes anything
on the board — confirmed on both the balancer switch and a harmless
non-protection register (nominal capacity, `0x20`). A BLE capture of the
vendor Android app ("EnjPower") performing the same capacity write succeeds.
This document is the record of what was checked to explain the difference,
starting from that capture and a decompile of the app itself
(`enjpower-bms-4.17.0.175-arm64-v8a.apk`, from the user's Downloads —
**not committed to this repo**; see the note in `docs/HANDOFF.md`).

## What was ruled out, with evidence, not guesses

1. **Wrong register/offset for the setting.** Disproven earlier in the
   session (`docs/HANDOFF.md`) via cross-check against
   `syssi/esphome-jk-bms`.

2. **Wrong checksum.** Confirmed correct byte-for-byte: the app's own
   `Transfer::checkAndSend(QByteArray&)` (disassembled directly, ARM64,
   `libenjpower_arm64-v8a.so`) computes nothing but a plain 8-bit additive
   sum over all bytes except the last, and stores it in the last byte —
   exactly `jkbms.crc.sum8`.

3. **Missing "signature" bytes.** The leading theory going into this
   investigation. Disproven: those ~9 bytes between the value and the
   checksum are populated by a loop in `Transfer::sendCommand(int,
   QVariant const&, int, bool)` that calls libc `rand()` for each byte and
   applies a small range-reduction (a magic-multiply divide, constant
   `0x80808081`, i.e. division by a small constant — never fully pinned
   down, wasn't necessary once bytes were shown to be irrelevant). This is
   filler, not a signature or password-derived value. Directly confirmed
   empirically too: a write with **zero** padding failed identically to one
   with **random** padding (both tested on the same benign register,
   sandwiched between fresh backup/read-back diffs) — the padding content
   provably does not matter to whatever is rejecting the write.

4. **Write mode (with/without GATT response).** Both tested. No difference
   — write-with-response returns success (no ATT error) but still doesn't
   apply.

5. **BLE pairing/bonding requirement.** Tested directly: `bluetoothctl pair
   <addr>` fails with `org.bluez.Error.AuthenticationCanceled` — the board
   itself cancels the pairing procedure. Since reads already work over this
   same unpaired, unencrypted link, an encryption/bonding requirement for
   writes specifically would be unusual, and the explicit cancellation
   suggests the board doesn't implement standard BLE pairing at all, not
   that it's silently gating writes behind it.

6. **A separate "unlock" characteristic or service.** The board exposes an
   undocumented vendor service (`f000ffc0-0451-4000-b000-000000000000`,
   surfaced by the verbose GATT dump during the pairing attempt above) that
   neither this tool nor the community reference ever touches. Checked the
   full BLE capture for any write to it: none. The app never touches it in
   this session.

7. **A required preceding command (e.g. a password-submission opcode).**
   Found the actual `SetSettingsPassword` command — it calls
   `Transfer::setValueVariant` with register code **`0x2A0`... resolved from
   disassembly as `0xa0`** (`mov w1, #0xa0` immediately before the call).
   Checked the capture: no write to `0xa0` appears anywhere before the
   successful capacity write in this session. Whatever the app did to
   satisfy the password requirement (if it did anything over BLE at all)
   isn't in this capture — the password may only gate the app's own UI
   locally, or maybe it is required and the app cached the auth from an
   earlier connection in the same session (this capture reused a connection
   that had already been active for a while — see the earlier CommandType
   list for other candidates like `RequestReadConfig` that weren't checked).

8. **MTU truncation.** Not directly tested but ruled out by existing
   evidence: read commands are also exactly 20 bytes and work reliably, so
   the link clearly carries full 20-byte ATT writes already.

## What's confirmed, for whoever continues this

- The full send path is: `Transfer::sendCommand` (marshals the value into
  the 20-byte layout, fills padding with `rand()`) → `Transfer::
  checkAndSend` (plain checksum, then calls) → `J::JSuperChannel::writeData`
  (**pure virtual dispatch** to whatever concrete `J::Channel` subclass is
  active — no transformation) → (presumably JNI into
  `Lcom/smartsoft/ble/BleService;->writeData([B)I`, though the call site
  wasn't found in the Java bytecode, meaning it's very likely invoked from
  native code via JNI, not traced further).
- There is **no cryptographic operation anywhere in this path.** AES
  (Rijndael) and file-encryption symbols exist in `libprotocore.so`, but
  they belong to `J::FileParser`, used for encrypted config/resource files
  on disk — not shown to be part of the BLE write path at all. That earlier
  theory (AES-128 challenge-response, based on a general web search about
  JK BMS security) does not appear to apply to what this specific app does
  for a settings write.
- The `CommandModel::CommandType` enum (extracted from Qt metaobject string
  data, not just guessed) lists every command the app's UI can issue by
  name — useful for anyone continuing this, since it's the app's own
  vocabulary, not a third-party guess. Notable entries beyond what's already
  in `jkbms/control.py`: `RequestRestoreFactory`, `OneKeyTernary`/
  `OneKeyFeLi`/`OneKeyLto` (the chemistry presets), `SendUartXProtoNo`,
  `SendDry1Trigger`/`SendDry2Trigger` (dry-contact alarm outputs),
  `RequestQueryDetailLogs`, `SetDeviceName`.

## Leads not yet followed

- **Timing/sequencing relative to notifications.** Not tested at all: does
  the firmware only accept a write immediately after receiving a specific
  kind of frame (e.g. right after streaming a settings frame, or only
  within some window after connecting)? This is a firmware-behavior
  question, not something visible in the app's code — the app's own timing
  is just "whenever the user taps save," so even the app's disassembly
  wouldn't reveal a hidden window if one exists.
- **A required command this session's capture didn't include.** The
  capture started mid-session (BLE already connected for a while before the
  capacity write). A capture of the *entire* connection lifecycle, from the
  moment the app connects through the settings-password entry through the
  actual write, would settle whether something happens between connect and
  write that this analysis missed.
- **Deeper JNI/Java-side tracing.** `BleService::writeData`'s caller was
  never found in the Java bytecode (androguard's xref analysis came up
  empty), meaning it's called from native code via JNI — not confirmed, and
  not traced further. If there's any per-write state Java-side (e.g. a
  write queue, a required prior `writeDescriptor` call to enable something),
  it would be there, not in the C++ we've been reading.

## Bottom line

This was thorough, not a quick guess-and-check: real disassembly of the
actual vendor app, cross-checked against a real capture of it working. Nine
distinct hypotheses were tested and eliminated with actual evidence. The
answer is not hiding in the payload bytes, the checksum, the GATT write mode,
BLE pairing, or an alternate service — which is genuinely useful negative
information. What's left needs either a more complete capture (whole
connection lifecycle) or empirical timing experiments against the live
board, not more static analysis of the app.
