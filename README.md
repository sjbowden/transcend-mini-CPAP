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
   ──collect.py───▶  raw event log  ──parse.py──▶  decoded therapy data
                                                          │
                                                  convert.py (sleephq/)
                                                          ▼
                                          ResMed‑format SD tree ──upload.py──▶ SleepHQ
```

Runs natively on **macOS, Linux, and Windows** — `collect.py` is pure Python
(pyserial), auto‑detects the device by USB VID:PID, and drives the same pull →
convert → upload pipeline on every OS. See [`packaging/MACOS.md`](packaging/MACOS.md)
for macOS setup, or [`packaging/WINDOWS.md`](packaging/WINDOWS.md) for Windows
(including the standalone `.exe`).

## Quick start

**One-time setup** (skip if you've already done this on this machine):
```bash
brew install python                          # macOS; Linux/Windows: see packaging/*.md
python3 -m venv .venv && .venv/bin/pip install pyserial
```
`transcend`/`pipeline.sh` auto‑detect and prefer `./.venv/bin/python3` if it
exists, so no `source .venv/bin/activate` is needed — just run them directly.
Then set up SleepHQ credentials (step 4 below) if you want the upload stage.

**The recurring routine** — plug the CPAP into USB, then:
```bash
./transcend
```
(`./transcend` is a thin wrapper around `pipeline.sh` — same flags, same env
vars, just a nicer name to type.)

That pulls every session currently on the device, converts it to a ResMed/SleepHQ
tree, and uploads it. It always re‑pulls everything the device is holding (not just
new nights), but that's cheap and safe: SleepHQ dedups by file hash, so re‑uploading
nights it already has is a no‑op on its side.

```bash
./pipeline.sh --no-upload    # just pull + convert; inspect sleephq/out/ before sending
./pipeline.sh --dry-run      # convert, then print what WOULD upload; sends nothing
```

Everything past this point explains what each stage does individually and how to
set it up the first time — you don't need to re‑read it for routine use.

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
| `pipeline.sh` | End‑to‑end orchestrator: pull → convert → upload (with stage‑skip flags); OS‑agnostic, calls `collect.py` |
| `app.py` | **Cross‑platform GUI** (macOS/Linux/Windows, plain tkinter) — Pull / Convert / Upload buttons + read‑only settings view, port auto‑prefilled. See [`packaging/MACOS.md`](packaging/MACOS.md) / [`packaging/WINDOWS.md`](packaging/WINDOWS.md) |
| `transport.py` | Serial backends, auto‑selected: **pyserial** (native macOS/Linux/Windows) or the **powershell.exe bridge** (WSL, no usbipd). `find_port()` locates the device by USB VID:PID on any OS |
| `collect.py` | Pure‑Python, cross‑platform event‑log collector (same `dump.txt` format as `collect.ps1`); what `pipeline.sh` and `app.py` call |
| `collect.ps1` | Original PowerShell collector — kept for reference; native Windows/WSL runs now go through `collect.py` |
| `parse.py` | Decodes the event log → `events.csv`, `sessions.csv`, and a printed summary |
| `pap.ps1` | PowerShell serial transport, used by `transport.py`'s WSL bridge backend |
| `settings.py` | View and (carefully) edit device settings — EZEX, ramp, pressures |
| `sleephq/convert.py` | Converts the parsed sessions into a ResMed‑format SD‑card tree SleepHQ can ingest |
| `sleephq/upload.py` | Uploads that tree to SleepHQ over its REST API (stdlib‑only: OAuth, import creation, file upload, processing) |
| `sleephq/edf.py` | Minimal EDF/EDF+ reader + ResMed‑flavoured writer (per‑record CRC‑16/CCITT) |
| `sleephq/templates/` | Bundled header‑only, PHI‑stripped ResMed EDF templates (STR/BRP/PLD) so the converter is self‑contained |
| `tests/` | Unit tests (decoder round‑trip, multi‑dump merge, converter end‑to‑end) — `python3 -m unittest discover -s tests`; no device needed |
| [`docs/NOTES.md`](docs/NOTES.md) | Distilled facts from the official Somnetics manuals (log capacity, setting constraints, stat definitions); the PDFs themselves are git‑ignored |

Personal data (`dump.txt`, `*.csv`, `sleephq/out/`) is git‑ignored.

## Requirements

- A Transcend Micro (or family) CPAP on a **data‑capable** USB cable. Depending on hardware
  revision it enumerates as either an **FTDI** serial port (`VID_0403 PID_6015`) or a
  **Silicon Labs CP210x** (`VID_10C4 PID_EA60`) — both work, and `--port auto` finds
  either one for you.
  - On a USB‑C Mac, if the device enumerates and then immediately drops, that's a
    Type‑C power‑negotiation quirk on some ports, not a bad cable — a USB‑C‑to‑USB‑A
    adapter fixes it (see [`packaging/MACOS.md`](packaging/MACOS.md)).
- **macOS or Linux** (native, via pyserial) — see [`packaging/MACOS.md`](packaging/MACOS.md).
- **Windows** (the device's COM port, native pyserial), or **WSL** — `collect.py`'s
  powershell.exe bridge needs no `usbipd` under WSL.
- Python 3.8+ for `parse.py` / `convert.py` / `sleephq/upload.py` (standard library only).
  `pyserial` is needed only for the direct‑serial transport (native macOS/Linux/Windows,
  or a usbipd‑attached port under WSL); the WSL powershell‑bridge path needs nothing extra.

## Usage

### macOS / Linux / Windows app
```bash
pip install pyserial
python3 app.py        # GUI: Pull / Convert / Upload buttons + settings view
```
Plain tkinter (`python-tk` on Homebrew, ships by default on python.org/Windows
Python), so the same GUI runs everywhere; the port field prefills with the
detected device (or `auto`). [`packaging/MACOS.md`](packaging/MACOS.md) covers
macOS setup; [`packaging/WINDOWS.md`](packaging/WINDOWS.md) covers Windows,
including building a standalone `TranscendSync.exe` with PyInstaller. The CLI
equivalent, on any OS:
```bash
python3 collect.py --port auto --out dump.txt    # auto-detects the device by USB VID:PID
python3 collect.py --port COM3 --out dump.txt    # ...or name the port explicitly
```

### All in one: `pipeline.sh`
The whole flow — pull from the device → convert → upload to SleepHQ — is wired together:
```bash
./pipeline.sh                 # pull -> convert -> upload (all data on the device)
./pipeline.sh --no-upload     # pull + convert only (inspect sleephq/out/ first)
./pipeline.sh --no-pull       # reuse the existing dump.txt (skip the device)
./pipeline.sh --dry-run       # convert, then show what WOULD upload (sends nothing)
PORT=/dev/cu.usbserial-XXXX ./pipeline.sh   # explicit port (default: auto-detect)
```
It calls the bundled `sleephq/upload.py` (override with `SLEEPHQ_UPLOADER=…`), which
needs credentials saved at `~/.sleephq_credentials` (see step 4 below). Each run
uploads *all* nights on the device as a new import; SleepHQ merges by date on its side. The
individual stages are below.

### 1. Download the event log
```bash
python3 collect.py --port auto --out dump.txt
```
The device is a request/response protocol at 38400 8N1; `collect.py` reads the
event‑log header, walks the ring buffer, and writes the raw blocks to `dump.txt` —
the same format the original `collect.ps1` produces, if you'd rather run that
under Windows PowerShell. The download is non‑destructive, but the device only
holds **3–6 months** of data (vendor‑stated) — pull at least every ~3 months or
the oldest nights are lost.

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
```bash
# One-time: create an API client in SleepHQ's Account Settings, then save
# ~/.sleephq_credentials (chmod 600):
#   SLEEPHQ_CLIENT_ID=...
#   SLEEPHQ_CLIENT_SECRET=...
#   # SLEEPHQ_TEAM_ID=...   (optional; default team is used if omitted)

python3 sleephq/upload.py --data-dir sleephq/out --all --dry-run   # preview, sends nothing
python3 sleephq/upload.py --data-dir sleephq/out --all \
    --import-name "Transcend (all, $(date +%Y-%m-%d))"
```
`sleephq/upload.py` is stdlib‑only (no `requests`) and drives the SleepHQ API directly:
OAuth2 password grant → create an import → `POST` each file → `process_files`. It sends
each file as bytes (not a streamed handle, which makes some HTTP clients use chunked
transfer‑encoding that SleepHQ rejects as *"corrupted during upload"*), hashes each file
as `content_hash = md5(file_bytes + filename)`, and uploads the **full per‑session file
set** (`BRP/PLD/EVE/CSL`), not just `STR.edf` — a partial set fails as *"some files were
missing."* It also sends an explicit `User-Agent`; SleepHQ's Cloudflare front end blocks
the default `urllib`/`Python-urllib` signature outright.

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
