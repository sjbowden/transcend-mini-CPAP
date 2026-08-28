# Running on macOS

The whole toolchain is pure-Python and runs natively on macOS — no Windows, no
WSL, no PowerShell. The Windows-only pieces (`collect.ps1`, `pap.ps1`) are not
used here; `collect.py` does the same job over `pyserial`.

## 1. One-time setup

```sh
# Python 3 (Homebrew shown; the python.org installer works too)
brew install python

# The one dependency the device path needs:
pip3 install pyserial

# Only if you want the GUI (app.py) — tkinter isn't in Homebrew Python by default:
brew install python-tk
```

## 2. Cable + driver

Connect the CPAP's **USB-C data port** to your Mac with a **data-capable** USB-C
cable (a charge-only cable will look like nothing is plugged in). Then:

- **FTDI units (VID 0403)** — work out of the box. macOS has shipped the FTDI
  driver since OS X 10.9; the device appears as `/dev/cu.usbserial-XXXX`.
- **CP210x units (VID 10C4)** — work natively on recent macOS (Big Sur 11 and
  later), usually as `/dev/cu.usbserial-XXXX`. If **no** port shows up, install
  Silicon Labs' free **CP210x VCP driver**, after which it appears as
  `/dev/cu.SLAB_USBtoUART`.

You don't have to know which chip your unit has — step 3 finds it either way.

## 3. Find the port (optional — `auto` does this for you)

Every entry point now defaults to `--port auto`, which locates the CPAP by its
USB VID:PID. To see it yourself:

```sh
python3 -m serial.tools.list_ports -v
# look for VID:PID=0403:6015 (FTDI) or 10C4:EA60 (CP210x)
```

Use the `cu.` device (not `tty.`) if you ever pass a port explicitly.

## 4. Pull and parse your data

```sh
# Auto-detect the device and download the raw event log:
python3 collect.py                       # -> dump.txt
# ...or name the port explicitly:
python3 collect.py --port /dev/cu.usbserial-XXXX

# Decode it to CSVs you can open anywhere:
python3 parse.py dump.txt                 # -> events.csv, sessions.csv

# Read the device configuration (read-only):
python3 settings.py --show
```

`sessions.csv` is the per-night summary (usage, AHI, apneas/hypopneas, min/max
pressure, leak) — the natural thing to feed into your own dashboard.

## 5. Full pipeline (optional, for SleepHQ)

```sh
./pipeline.sh --no-upload        # pull + convert to a ResMed/SleepHQ SD tree
./pipeline.sh                    # + upload to SleepHQ
PORT=/dev/cu.usbserial-XXXX ./pipeline.sh --no-upload   # explicit port
```

`pipeline.sh` is now OS-agnostic — the pull stage calls `collect.py`, which
picks the serial backend itself.

Uploading needs a SleepHQ API client. In SleepHQ, go to Account Settings and
create one to get a Client ID and Client Secret, then create
`~/.sleephq_credentials` (`chmod 600`):

```
SLEEPHQ_CLIENT_ID=...
SLEEPHQ_CLIENT_SECRET=...
# SLEEPHQ_TEAM_ID=...   (optional; default team is used if omitted)
```

`sleephq/upload.py` (pure stdlib, no extra dependencies) reads those and
handles auth, import creation, file upload, and processing — see its
docstring for the full API flow.

## 6. GUI (optional)

```sh
python3 app.py
```

Plain tkinter, so it runs on macOS once `python-tk` is installed. The port field
prefills with the detected device (or `auto`).

## Troubleshooting

- **`no Transcend USB-serial device found`** — cable is charge-only, not seated
  (the port is recessed; a right-angle USB-C plug seats best), device asleep, or
  a CP210x unit needs the Silicon Labs driver. Confirm with the `list_ports`
  command in step 3.
- **`pyserial is not installed`** — `pip3 install pyserial` (mind which Python:
  `python3 -m pip install pyserial`).
- **Port shows under `tty.` but not `cu.`** — use the `cu.` name; on macOS `cu.`
  is the call-out (non-blocking) node you want for this.
- **Permission denied opening the port** — rare on macOS, but reconnect the
  cable or check no other app (e.g. Transcend's own software) holds the port.
- **Device enumerates then disappears after ~1 second (`cableChangeOccurred:
  powering off` in the kernel log)** — this is a USB-C Type-C power-delivery
  negotiation drop, not a driver or cable problem: the FTDI/CP210x chip comes
  up fine, but the CC-line PD negotiation on some Mac USB-C ports tears the
  device back down almost immediately. A plain **USB-C-to-USB-A adapter**
  sidesteps Type-C PD negotiation entirely and gives a stable connection. To
  confirm this is what's happening, watch the kernel log live while you plug
  in: `/usr/bin/log stream --predicate 'subsystem == "com.apple.iokit.IOUSBHostFamily" OR eventMessage CONTAINS "cableChangeOccurred"'`
  (see the two gotchas below for why `system_profiler` and plain `log` can
  mislead you here).
- **On macOS 26 ("Tahoe"), `system_profiler SPUSBDataType` returns nothing**
  — the data type was renamed to `SPUSBHostDataType`. Confirm with
  `system_profiler -listDataTypes | grep -i usb`, then use
  `system_profiler SPUSBHostDataType`.
- **`log show`/`log stream` fails with `(eval):log:1: too many arguments`** —
  zsh has a builtin `log` that shadows `/usr/bin/log`. Call the full path,
  e.g. `/usr/bin/log show --predicate '...' --last 2m`.
