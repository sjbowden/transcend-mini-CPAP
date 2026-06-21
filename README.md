# transcend-mini-CPAP

Pull therapy data off a **Somnetics Transcend Micro** CPAP over USB and (optionally)
get it into [SleepHQ](https://sleephq.com) — neither of which the device officially
supports.

> **Naming:** the device is a **Transcend Micro (510)**; Somnetics' desktop software
> is called **"mini" / "mini PAP"** (its installer icon is literally "MiniCPAP"),
> which is where this repo's name comes from. One desktop app — and, as far as the
> decompile shows, one serial protocol — serves the whole Transcend family
> (miniCPAP / Transcend 3 / Micro).

The Transcend talks a proprietary ASCII protocol over an FTDI USB‑to‑serial bridge,
and **no open‑source tool reads it** (OSCAR and SleepHQ support ResMed / Philips /
Fisher&Paykel / Löwenstein, not Transcend). This project reverse‑engineers that
protocol — from the vendor's own decompiled Windows app — and provides a full
pipeline from the device to a CSV and to a SleepHQ‑importable dataset.

```
 device (USB/FTDI)        dump.txt            events.csv / sessions.csv      SleepHQ
   ──collect.ps1──▶  raw event log  ──parse.py──▶  decoded therapy data
                                                          │
                                                  convert.py (sleephq/)
                                                          ▼
                                          ResMed‑format SD tree ──upload──▶ SleepHQ
```

## What you get

| Data | Available? | Notes |
|---|---|---|
| Usage / therapy hours | ✅ | from StartTherapy/EndTherapy events |
| AHI, apnea & hypopnea counts | ✅ | time‑stamped events |
| Pressure (incl. APAP changes) | ✅ | curve from pressure‑change events + ~5‑min averages |
| Leak | ✅ | ~5‑min AverageLeak; graph vent‑compensated to ResMed‑style unintentional leak by default (`--raw-leak` keeps raw); peak from MaximumLeak |
| Snore / flow‑limit ratios | ✅ (summary) | one whole‑night ratio each (flat line, not a trace) |
| **Flow waveform, resp. rate, tidal volume, minute ventilation** | ❌ | **the Transcend does not record these** |

The Transcend is a *compliance/event recorder*, not a full data‑logger, so the
breathing/flow graphs are genuinely empty — there is no source data to plot.

## Repository contents

| File | Purpose |
|---|---|
| [`PROTOCOL.md`](PROTOCOL.md) | The reverse‑engineered serial wire protocol (commands, framing, the 5‑byte event format, all 28 event types) |
| `pipeline.sh` | End‑to‑end orchestrator: pull → convert → upload (with stage‑skip flags) |
| `app.py` | **Windows GUI** — Pull / Convert / Upload buttons + read‑only settings view (see [`packaging/WINDOWS.md`](packaging/WINDOWS.md)) |
| `transport.py` | Serial backends: **pyserial** (native Windows / usbipd) or the **powershell.exe bridge** (WSL default), auto‑selected |
| `collect.py` | Pure‑Python event‑log collector (same `dump.txt` format as `collect.ps1`) |
| `collect.ps1` | PowerShell collector — current default in `pipeline.sh` until the pyserial path is live‑validated |
| `parse.py` | Decodes the event log → `events.csv`, `sessions.csv`, and a printed summary |
| `pap.ps1` | PowerShell serial transport, used by `transport.py`'s bridge backend |
| `settings.py` | View and (carefully) edit device settings — EZEX, ramp, pressures |
| `sleephq/convert.py` | Converts the parsed sessions into a ResMed‑format SD‑card tree SleepHQ can ingest |
| `sleephq/edf.py` | Minimal EDF/EDF+ reader + ResMed‑flavoured writer (per‑record CRC‑16/CCITT) |
| `sleephq/templates/` | Bundled header‑only, PHI‑stripped ResMed EDF templates (STR/BRP/PLD) so the converter is self‑contained |
| `tests/` | Unit tests (decoder round‑trip, multi‑dump merge, converter end‑to‑end) — `python3 -m unittest discover -s tests`; no device needed |
| [`docs/NOTES.md`](docs/NOTES.md) | Distilled facts from the official Somnetics manuals (log capacity, setting constraints, stat definitions); the PDFs themselves are git‑ignored |

Personal data (`dump.txt`, `*.csv`, `sleephq/out/`) is git‑ignored.

## Requirements

- A Transcend Micro (or family) CPAP on a USB cable. Depending on hardware revision it enumerates as
  either an **FTDI** serial port (`VID_0403 PID_6015`) or a **Silicon Labs CP210x**
  (`VID_10C4 PID_EA60`) — both work; just point `-Port` at whichever COM port appears.
- **Windows** (the device's COM port), or **WSL** — `collect.ps1` is driven through
  `powershell.exe`'s `System.IO.Ports`, so no `usbipd` is needed under WSL.
- Python 3.8+ for `parse.py` / `convert.py` (standard library only).
  `pyserial` is needed only for the direct-serial transport on native Windows
  (or a usbipd-attached port under WSL); the WSL powershell-bridge path needs
  nothing extra.

## Usage

### Windows app
```powershell
pip install pyserial
python app.py        # GUI: Pull / Convert / Upload buttons + settings view
```
Pure Python end‑to‑end on native Windows (no PowerShell helpers, no WSL).
[`packaging/WINDOWS.md`](packaging/WINDOWS.md) covers building a standalone
`TranscendSync.exe` with PyInstaller. The CLI equivalents:
```bash
python collect.py --port COM3 --out dump.txt    # pure-Python pull (pyserial)
```

### All in one: `pipeline.sh`
The whole flow — pull from the device → convert → upload to SleepHQ — is wired together:
```bash
./pipeline.sh                 # pull -> convert -> upload (all data on the device)
./pipeline.sh --no-upload     # pull + convert only (inspect sleephq/out/ first)
./pipeline.sh --no-pull       # reuse the existing dump.txt (skip the device)
./pipeline.sh --dry-run       # convert, then show what WOULD upload (sends nothing)
PORT=COM4 ./pipeline.sh       # device on a different COM port
```
It calls the SleepHQ uploader at `~/cpap/sleephq_upload.py` (override with
`SLEEPHQ_UPLOADER=…`), which needs credentials saved at `~/.sleephq_credentials`. Each run
uploads *all* nights on the device as a new import; SleepHQ merges by date on its side. The
individual stages are below.

### 1. Download the event log
```powershell
# Windows PowerShell (device on COM3 by default)
powershell -ExecutionPolicy Bypass -File collect.ps1 -Port COM3 -OutFile dump.txt
```
The device is a request/response protocol at 38400 8N1; `collect.ps1` reads the
event‑log header, walks the ring buffer, and writes the raw blocks to `dump.txt`.
The download is non‑destructive, but the device only holds **3–6 months** of data
(vendor‑stated) — pull at least every ~3 months or the oldest nights are lost.

### 2. Decode to CSV
```bash
python3 parse.py dump.txt
# -> events.csv (every event), sessions.csv (per‑night summary), and a printed summary:
#    Device serial, AHI, usage, pressure, leak per session.
```

### 3. (Optional) Convert for SleepHQ
```bash
python3 sleephq/convert.py dump.txt --out sleephq/out
# --min-minutes N   drop sessions shorter than N (default 5; excludes factory/QA blips)
# --since YYYY-MM-DD only include sessions on/after this date
# --serial XXX      override device serial (default: taken from the dump)
# --mask CODE       ResMed mask-type code for SleepHQ's settings panel (default 2 = pillows)
# --raw-leak        keep raw uncompensated leak (default vent-compensates the leak graph)
# --pressure-reason-flags  annotate why APAP raised pressure (events 23-28); off by default
```

This writes a ResMed‑style SD‑card tree (`STR.edf`, `Identification.json`, and per
session `BRP/PLD/EVE/CSL` files). Since SleepHQ has no Transcend parser, the data is
encoded as a **ResMed AirSense 11** using the Transcend's own serial number, so it
appears as a separate machine (rename it / set your day‑split in the SleepHQ UI).

> **Self‑contained:** the EDF format templates ship in [`sleephq/templates/`](sleephq/templates/)
> — header‑only, PHI‑stripped ResMed signal definitions (no serial, no patient data, no
> therapy records). No real ResMed machine or SD card is needed to run the converter.

### 4. Upload to SleepHQ
Upload the generated tree via the SleepHQ API (OAuth2 password grant → create an
import → `POST` each file → `process_files`). Two gotchas learned the hard way:

- **Send each file as bytes, not a streamed file handle** — a handle makes `requests`
  use chunked transfer‑encoding, which SleepHQ rejects as *"corrupted during upload."*
- `content_hash` must be `md5(file_bytes + filename)`, and a ResMed import needs the
  **full per‑session file set** (`BRP/PLD/EVE/CSL`), not just `STR.edf` — otherwise it
  fails with *"some files were missing."*

## Settings (read & edit)

`settings.py` reads and (carefully) edits the device configuration over the same serial
link. **Read‑only is risk‑free:**

```bash
python3 settings.py --port COM3 --show          # print all settings
python3 settings.py --port COM3 --snapshot a.json   # save config (for blob mapping)
python3 settings.py --port COM3 --diff a.json       # diff current vs a saved snapshot
```

> **The official Windows desktop app under‑reports the APAP *minimum* pressure** (it shows a
> stuck `10` regardless of the real value — an initialization‑order bug, root‑caused in
> [`PROTOCOL.md`](PROTOCOL.md)). This `--show` read, and the BLE/MySleepDash mobile app, are
> correct; if the desktop app's minimum disagrees, trust the device read, not the desktop app.

Editing uses **read‑modify‑write**: it changes only the requested field, preserves the
opaque blob verbatim, sends the write, checks the `R55` ack, then **reads back to verify**
— and auto‑saves a timestamped backup before every write (`--restore FILE` rolls back). It
also range‑checks each value and enforces the device's cross‑field rules (min ≤ start ≤ max,
and GentleRise pressure ≥ 1 cmH₂O below the *starting* therapy pressure — matching the official
app, which bounds the ramp by where therapy starts, not the APAP min) before sending anything.

```bash
python3 settings.py --port COM3 --set-ezex 2              # comfort: pressure relief 0–3
python3 settings.py --port COM3 --set-ramp-time 20        # comfort: ramp minutes
python3 settings.py --port COM3 --dry-run --set-ezex 3    # show exact bytes, send nothing
python3 settings.py --port COM3 --set-min 11 --set-max 14 --allow-prescription
```

> **Safety / responsibility.** The official app's password only gates *prescription*
> settings in its own UI — the device firmware accepts writes with **no authentication**.
> So this tool imposes the boundary: comfort settings (EZEX, ramp) edit freely;
> prescription pressures (min/max/start) require `--allow-prescription`. Those are
> clinician‑set values — changing them is your responsibility; verify with your provider.
> Calibration is never writable. Every write is reversible via the auto‑saved backup.

### The `ConfigurationData` blob — now decoded

The config response carries a 15‑char "opaque" blob (with an `aa55` magic marker) that turned
out **not** to be opaque at all — single‑field sweeps fully mapped it as
**`CCCC aa55 GGGG SS F`**:

- **`CCCC` (chars 0–3) = the pressure‑sensor calibration offset × 10** (signed). It read `0000`
  for months only because the offset was `+0.0`; setting it via the app's calibrate feature
  moved it exactly (`−0.3`→`fffd`, `+0.9`→`0009`, `−0.9`→`fff7`). The 5‑char **`Reserved`** field
  carries the *same* offset in raw sensor counts (`×~64`).
- **`aa55`** — magic marker.
- **`GGGG` (chars 8–11) = `0100`** — constant; `0x0100` = unity in 8.8 fixed‑point, so very likely
  the calibration **gain** (the app exposes only the offset, so we can't sweep it to confirm).
- **`SS` (chars 12–13) = `StartingTherapyPressure × 10`** (confirmed 11→`6e` … 15→`96`).
- **`F` (last nibble) = a sticky "config‑modified" latch**: `0` only in the original
  clinic‑provisioned config, `1` after the first local write, and it stays `1` through every
  write since (an official‑app write that regenerated the blob, and a Reset‑Compliance, both
  left it `1`). **Min and max do not appear in the blob.**

So the firmware regenerates the calibration/`SS`/`F` bytes itself. The tool always sends the blob
back **unchanged** (read‑modify‑write), which is also what keeps a config write from disturbing
the calibration stored inside it; `settings.py` verifies the *named* fields and reports any
firmware‑side blob change as an informational note, not a failure. `--snapshot`/`--diff` show
exactly which bytes moved. Because the calibration lives in those bytes, `--restore` **refuses by
default if the snapshot's calibration differs from the device** (it would otherwise silently
change your pressure‑sensor calibration); override only deliberately with
`--allow-calibration-change`.

## How it was reverse‑engineered

The wire protocol and data format were lifted from the official **TranscendGo** Windows
client (a .NET ClickOnce app) by decompiling its `Somnetics.TranscendGo.*` assemblies
with ILSpy — the `TranSyncManager` / `ComplianceEventFactory` classes contain the
command set, framing, and the 5‑byte event decoder. Everything was then validated live
against a real device. Full details in [`PROTOCOL.md`](PROTOCOL.md).

All of the app's assemblies have been mined (`Business`, `Data`, `Common`, `Client`, and the
resource satellite): the wire protocol, data model, config layout, event format & logging
phases, the cloud API, and the exact compliance/percentile math are all recovered and
documented. Firmware version/update and Dry mode live only on the Bluetooth/iOS path, and the
`ConfigurationData` blob is a fixed factory block — so the USB‑serial surface is fully
characterized.

## Privacy

This toolkit is **fully local** — it talks only to the device over USB and writes files on
your machine; it never contacts any server. For contrast, the official **TranscendGo** app
uploads your event log *and* prescription to Somnetics' cloud (`api.mytransync.com`),
identified by device serial + email with no client-side authentication (see
[`PROTOCOL.md`](PROTOCOL.md)). Nothing here phones home.

## Disclaimer

For personal use with your own device and data. Not affiliated with Somnetics,
ResMed, or SleepHQ. CPAP data is not a substitute for medical advice — discuss your
therapy with a clinician.
