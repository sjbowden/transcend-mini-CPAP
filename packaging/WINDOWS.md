# Running on Windows

Covers setting a Windows machine up from scratch, the day-to-day routine
(`pipeline.sh` and `./transcend` are bash — they do **not** run here), and
building the standalone `.exe`.

## 0. Setting up a fresh Windows machine

```powershell
winget install Python.Python.3.12 --scope user   # per-user: no admin needed
winget install Git.Git                           # if you need to clone
git clone https://github.com/sjbowden/transcend-mini-CPAP.git
cd transcend-mini-CPAP
pip install pyserial
```

Use the **python.org build** (what winget installs), not the Microsoft Store
one: Store Python lives in a sandboxed `WindowsApps` directory with redirected
file access, which breaks PyInstaller in section 2.

> **If `python --version` says "Python was not found"** you are hitting the
> Store *execution alias* stubs rather than a real interpreter — even after
> installing Python. Turn them off in **Settings → Apps → Advanced app settings
> → App execution aliases**, or call the interpreter by full path
> (`%LOCALAPPDATA%\Programs\Python\Python312\python.exe`).

`--scope user` keeps `pip install` out of a system directory, so no step here
needs an elevated PowerShell. Neither does anything else in this file:
serial-port access, `-ExecutionPolicy Bypass` (per-process) and the PyInstaller
build are all ordinary-user operations.

For the upload stage, credentials go in `%USERPROFILE%\.sleephq_credentials`
(i.e. `C:\Users\<you>\.sleephq_credentials`) — `~` expands there on Windows.
Same JSON as everywhere else; the `chmod 600` the other docs mention has no
Windows equivalent, so rely on the file being inside your user profile.

## 1. The routine, on Windows

`pipeline.sh` / `./transcend` are shell scripts and are **not usable here**
without WSL or Git Bash. Run the stages directly (or use the GUI in section 2):

```powershell
python collect.py --port auto --out dump.txt
python parse.py dump.txt
python sleephq\convert.py dump.txt --out sleephq\out
python sleephq\upload.py --data-dir sleephq\out --all --dry-run
python sleephq\upload.py --data-dir sleephq\out --all
```

`--port auto` finds the device by USB VID:PID, so you rarely need `--port COM3`.

### Travelling: pass `--tz`

The device stores UTC and both `parse.py` and `convert.py` render it in **this
machine's** local zone by default. If you sleep in another timezone without
changing the laptop clock, every night lands at the wrong time of day and
sometimes the wrong date — a 23:00 Tokyo night shows up as 07:00 the same
morning on a US-Pacific clock, which SleepHQ then files as a daytime nap.

Pass the zone you actually slept in:

```powershell
python parse.py dump.txt --tz Asia/Tokyo
python sleephq\convert.py dump.txt --out sleephq\out --tz Asia/Tokyo
```

An IANA name needs Python 3.9+; a fixed offset (`+09:00`) works anywhere. For a
trip spanning zones, convert each leg separately with `--since`/`--until` so
each carries its own `--tz`:

```powershell
python sleephq\convert.py dump.txt --out out-jp --tz +09:00 --since 2026-11-02 --until 2026-11-10
python sleephq\convert.py dump.txt --out out-tw --tz +08:00 --since 2026-11-11 --until 2026-11-18
```

Keep the laptop on one timezone throughout. The uploader deduplicates on
`MD5(file content + filename)` and both depend on the rendered timestamp, so
converting the same nights under two different zones produces two different
sets of files — duplicate nights in SleepHQ rather than a no-op re-upload.

## 2. Plain Python GUI (no build at all)

With Python installed as above:

```powershell
python app.py
```

`--transport` auto-selects pyserial on native Windows, so no PowerShell helper
is involved — the whole stack is Python talking straight to COM3.

## 3. Standalone .exe (PyInstaller)

Build **on Windows** (PyInstaller is not a cross-compiler):

```powershell
pip install pyserial pyinstaller
pyinstaller --onefile --windowed --name TranscendSync `
  --paths . --paths sleephq `
  --add-data "sleephq/templates;templates" `
  app.py
```

Produces `dist/TranscendSync.exe` (~15–30 MB, no Python install needed).
Notes:

- `--add-data` bundles the EDF templates; `convert.py` finds them via
  `sys._MEIPASS` when frozen.
- **SmartScreen**: unsigned PyInstaller exes get an "unrecognized app" warning
  on first run (More info → Run anyway), and occasionally antivirus false
  positives. Fine for personal use; distribution would need a code-signing
  certificate.
- **Upload from the frozen exe** shells out to `python` on PATH to run the
  bundled SleepHQ uploader (`SLEEPHQ_UPLOADER`, default `sleephq/upload.py`
  next to the app) — the exe itself is not a Python interpreter. If no Python
  is on PATH, run the upload stage separately.

## 4. From WSL (development convenience)

`python3 app.py` works under WSLg (Windows 11 draws the window), and the
`auto` transport routes COMx through the powershell.exe bridge, so the GUI is
fully usable from WSL too — just slower per command than native pyserial.

## Validation status

The pyserial backend mirrors `pap.ps1`/`collect.ps1` timing (60 ms settle,
12 ms char gap, two-CR framing, nulls discarded, RTS/DTR held low) and is
covered by unit tests against a fake serial port, and **validated against the
real device on 2026-10-10**: a pyserial pull and a `collect.ps1` pull taken
back-to-back were byte-identical (whole file, modulo CRLF), and
`settings.py --show --transport pyserial` matched the bridge read field for field.

That was confirmed on **both** stacks: over USB/IP into WSL
(`usbipd attach --wsl`, device as `/dev/ttyUSB0`) and on the **native Windows
driver** (Python 3.12.10 + pyserial, device on COM3). All three dumps — native
pyserial, usbipd pyserial, and `collect.ps1` — are byte-identical once CRLF is
normalised.

One caveat remains: the unit held only **one block** of data, so the
multi-block walk is still unexercised on real hardware. `collect.ps1` therefore
stays in the tree as the reference implementation; re-run the diff when the
device holds >200 records.

Running the repo from native Windows while it lives in WSL: invoke the script by
its UNC path, e.g.
`python \\wsl.localhost\Ubuntu\home\<you>\projects\transcend\collect.py --port COM3 --out dump.txt`.
Python puts the *script's* directory on `sys.path`, so `transport.py` imports
fine even though the working directory is elsewhere (Windows processes cannot
have a UNC working directory).

Note while attached to WSL the device disappears from Windows, so COM3 and the
vendor app cannot see it until `usbipd detach --busid <id>`.
