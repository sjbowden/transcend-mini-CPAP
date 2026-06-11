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
  external SleepHQ uploader (`SLEEPHQ_UPLOADER`, default
  `~/cpap/sleephq_upload.py`) — the exe itself is not a Python interpreter.
  If no Python is on PATH, run the upload stage separately.

## 3. From WSL (development convenience)

`python3 app.py` works under WSLg (Windows 11 draws the window), and the
`auto` transport routes COMx through the powershell.exe bridge, so the GUI is
fully usable from WSL too — just slower per command than native pyserial.

## Validation status

The pyserial backend mirrors `pap.ps1`/`collect.ps1` timing (60 ms settle,
12 ms char gap, two-CR framing, nulls discarded, RTS/DTR held low) and is
covered by unit tests against a fake serial port — but it has **not yet been
validated against the real device**. Before trusting it: run
`python collect.py --port COM3 --out dump-pyserial.txt` on Windows and diff
against a `collect.ps1` dump taken back-to-back (event records should be
identical; the queue may have gained a few events between pulls). Until then,
`pipeline.sh` keeps using `collect.ps1`.
