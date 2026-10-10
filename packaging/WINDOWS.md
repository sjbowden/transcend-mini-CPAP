# Building the Windows app

The GUI (`app.py`) runs three ways, in increasing order of effort:

## 1. Plain Python (no build at all)

On Windows, with [Python 3.8+](https://python.org) installed:

```powershell
pip install pyserial
python app.py
```

`--transport` auto-selects pyserial on native Windows, so no PowerShell helper
is involved — the whole stack is Python talking straight to COM3.

## 2. Standalone .exe (PyInstaller)

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

## 3. From WSL (development convenience)

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
