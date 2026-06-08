# transcend-mini-CPAP

Pull therapy data off a **Somnetics Transcend miniCPAP** over USB and (optionally)
get it into [SleepHQ](https://sleephq.com) — neither of which the device officially
supports.

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
| Pressure (incl. APAP changes) | ✅ | stepped curve from pressure‑change events |
| Leak | ✅ | ~5‑minute AverageLeak reports |
| Snore / flow‑limit ratios | ✅ (sparse) | only a few events logged per night |
| **Flow waveform, resp. rate, tidal volume, minute ventilation** | ❌ | **the Transcend does not record these** |

The Transcend is a *compliance/event recorder*, not a full data‑logger, so the
breathing/flow graphs are genuinely empty — there is no source data to plot.

## Repository contents

| File | Purpose |
|---|---|
| [`PROTOCOL.md`](PROTOCOL.md) | The reverse‑engineered serial wire protocol (commands, framing, the 5‑byte event format, all 28 event types) |
| `collect.ps1` | Drives the serial port and downloads the raw event log → `dump.txt` |
| `parse.py` | Decodes the event log → `events.csv`, `sessions.csv`, and a printed summary |
| `pap.ps1` | Reusable serial transport (send a command, return the response) used by `settings.py` |
| `settings.py` | View and (carefully) edit device settings — EZEX, ramp, pressures |
| `sleephq/convert.py` | Converts the parsed sessions into a ResMed‑format SD‑card tree SleepHQ can ingest |
| `sleephq/edf.py` | Minimal EDF/EDF+ reader + ResMed‑flavoured writer (per‑record CRC‑16/CCITT) |
| `sleephq/templates/` | Bundled header‑only, PHI‑stripped ResMed EDF templates (STR/BRP/PLD) so the converter is self‑contained |

Personal data (`dump.txt`, `*.csv`, `sleephq/out/`) is git‑ignored.

## Requirements

- A Transcend miniCPAP on a USB cable (enumerates as an FTDI serial port,
  `VID_0403 PID_6015`).
- **Windows** (the device's COM port), or **WSL** — `collect.ps1` is driven through
  `powershell.exe`'s `System.IO.Ports`, so no `usbipd` is needed under WSL.
- Python 3.8+ for `parse.py` / `convert.py` (standard library only).

## Usage

### 1. Download the event log
```powershell
# Windows PowerShell (device on COM3 by default)
powershell -ExecutionPolicy Bypass -File collect.ps1 -Port COM3 -OutFile dump.txt
```
The device is a request/response protocol at 38400 8N1; `collect.ps1` reads the
event‑log header, walks the ring buffer, and writes the raw blocks to `dump.txt`.

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

Editing uses **read‑modify‑write**: it changes only the requested field, preserves the
opaque blob verbatim, sends the write, checks the `R55` ack, then **reads back to verify**
— and auto‑saves a timestamped backup before every write (`--restore FILE` rolls back).

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

### The opaque `ConfigurationData` blob

The config response carries a 15‑char opaque blob (`0000aa5501006e1`) with an `aa55` magic
marker. We tried to map its bits by differential diffing, but the iOS app turns out to
expose **only named fields** — *AirRelief* (=`EZEX`), *GentleRise Pressure*
(=`StartingRampPressure`), *GentleRise Duration* (=`RampDurationMinutes`), and the
(locked) prescription pressures. There is **no** auto‑start/stop or alert toggle, so
nothing the user can change writes the blob: it's a **factory/firmware‑fixed** block, not
user‑mappable. `--snapshot`/`--diff` remain useful to *confirm* that every write preserves
it verbatim (they do).

## How it was reverse‑engineered

The wire protocol and data format were lifted from the official **TranscendGo** Windows
client (a .NET ClickOnce app) by decompiling its `Somnetics.TranscendGo.*` assemblies
with ILSpy — the `TranSyncManager` / `ComplianceEventFactory` classes contain the
command set, framing, and the 5‑byte event decoder. Everything was then validated live
against a real device. Full details in [`PROTOCOL.md`](PROTOCOL.md).

## Disclaimer

For personal use with your own device and data. Not affiliated with Somnetics,
ResMed, or SleepHQ. CPAP data is not a substitute for medical advice — discuss your
therapy with a clinician.
