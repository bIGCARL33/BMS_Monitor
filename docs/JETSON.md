# Running on a Jetson Orin Nano

The package is plain Python and already runs on Linux/aarch64, so most of this
is setup rather than porting. Four things differ from a laptop, and one of them
decides the whole approach.

---

## Start here

```bash
python3 -m jkbms.cli doctor
```

Checks the interpreter version, both link libraries, the Bluetooth radio, serial
ports, `dialout` membership, and (on a Jetson) whether `nvgetty` is holding a
UART. Every one of those failures presents at the bench as a dead link, and
several are indistinguishable from a wiring fault. Run it before touching the
pack.

---

## 1. Python version — check this first

| JetPack | Ubuntu | Python |
|---|---|---|
| 5.x | 20.04 | **3.8** |
| 6.x | 22.04 | 3.10 |

The package supports **3.8 and up**, so either works. That is deliberate and
enforced by `tests/test_compat.py` — the code originally used
`int.bit_count()`, which is 3.10-only and would have crashed every decode on
JetPack 5.

If you write code against this package, run the test suite before deploying. A
3.10-only construct will pass on your desktop and fail on the board.

---

## 2. BLE is the path — and on this board, the only one

Your JK-BD4A8S4P reports hardware version **`15H`**. JK's guidance is that PC
connectivity over UART requires an `A` in that string. It has none, which is why
the wired link returns zero bytes no matter how it is wired. Confirm on your own
board with:

```bash
python3 -m jkbms.cli deviceinfo --ble C8:47:80:46:6E:37
```

So on the Jetson, ignore the 40-pin UART header. It is a genuinely nice feature
— native 3.3 V serial at `/dev/ttyTHS*`, no USB adapter needed — and it is
useless against a board that will not answer.

### Bluetooth on an Orin Nano

The Orin Nano Developer Kit takes an **M.2 Key-E** wireless card. Some SKUs ship
with one fitted, some do not. If `doctor` reports no adapter, that is usually a
missing or unseated card with its antennas disconnected, not a BlueZ problem —
check the hardware before spending an evening on the stack.

With a card fitted:

```bash
sudo rfkill unblock bluetooth
sudo systemctl enable --now bluetooth
python3 -m jkbms.cli scan
```

The BMS advertises under its **serial number**, not a name containing "JK", so
it will be listed as unidentified. That is expected; `sniff` is what confirms
it.

---

## 3. Headless — view the dashboard from another machine

A Jetson usually has no screen, and the dashboard defaults to `127.0.0.1`.
Use `--lan`:

```bash
python3 -m jkbms.cli dashboard --ble C8:47:80:46:6E:37 --lan -o run1.csv
```

It prints the address to open from your laptop. **There is no authentication** —
anyone on the network can read the page. It exposes pack telemetry and nothing
else, but decide whether that is acceptable on your network. On anything shared,
leave the default binding and use an SSH tunnel instead:

```bash
ssh -N -L 8765:localhost:8765 jre@jetson     # from the laptop
```

Then open `http://localhost:8765/` locally.

---

## 4. Always-on — run it as a service

This is what a Jetson is for. A pack watched continuously, logging to CSV,
surviving reboots.

```bash
sudo ./deploy/install-service.sh C8:47:80:46:6E:37
```

The installer validates the address format, runs the service as *you* rather
than root (BlueZ access and log ownership both want that), prints what it will
do, and asks before writing to `/etc`.

```bash
journalctl -u jkbms-dashboard -f          # follow
systemctl restart jkbms-dashboard         # restart; starts a new dated CSV
systemctl disable --now jkbms-dashboard   # stop and remove from boot
```

Two details in the unit that are load-bearing:

**Dated log filenames.** `ExecStart` writes
`pack-%Y%m%d-%H%M%S.csv`. The logger *refuses* to truncate an existing file —
losing a discharge run to a re-run command is unacceptable — so without a unique
name per start, a restart would fail rather than overwrite. Dated names sidestep
both failure modes. (The `%%` escaping in the unit is systemd's; the program
receives `%Y`.)

**One instance only.** The JK accepts a single BLE connection. A second copy of
the dashboard, or an open `bluetoothctl` session, will make the service fail to
connect and restart-loop. If the unit is running, stop it before running the CLI
by hand.

### If it restart-loops

```bash
journalctl -u jkbms-dashboard -n 50
python3 -m jkbms.cli doctor
```

Almost always one of: wrong BLE address, radio blocked, or something else
holding the connection.

---

## Disk

CSV at one row/second is roughly **10 MB/day** with four cells. On the Jetson's
eMMC or an SD card that is fine for weeks, but it does not self-prune. Either
raise `--interval` for long unattended runs, or add a logrotate rule for
`/var/lib/jkbms/*.csv`.

`--interval 5` cuts it to ~2 MB/day and loses nothing for trend work; keep 1 s
only when you care about transients.

---

## Still unverified on any platform

The **current field**. Every reading so far has been at 0.000 A, so its scale
and sign have never been exercised. Before trusting any capacity or energy
figure, put a known load on the pack and check that discharge reads **negative**.

Consistency checks cannot catch this: they prove the layout is self-consistent,
not that it is correctly scaled. See step 3 of `RUNBOOK.md`.

---

## Electrical

Everything in `RUNBOOK.md` still applies. The Jetson changes one thing for the
worse: it is mains-powered, so the "run the laptop on battery" mitigation is
gone. If you ever do connect the UART, the Jetson's ground and the pack negative
are now tied through the mains earth — use an isolator, or don't.

Over BLE there is no galvanic connection at all, which is another quiet argument
for the wireless path on a permanently installed board.
