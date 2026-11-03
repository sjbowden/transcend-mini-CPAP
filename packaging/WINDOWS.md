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

For the import stage you need the cpap repo (`~/projects/cpap`). If it lives in
WSL, nothing else: the app runs the importer inside WSL (see section 3). If it
is a Windows checkout, also `pip install numpy pandas matplotlib` and set
`CPAP_REPO` to it unless it is at `%USERPROFILE%\projects\cpap`.

## 1. The routine, on Windows

`pipeline.sh` / `./transcend` are shell scripts and are **not usable here**
without WSL or Git Bash. Run the stages directly (or use the GUI in section 2):

```powershell
python collect.py --port auto --out dump.txt
python parse.py dump.txt
python sleephq\convert.py dump.txt --out sleephq\out      # optional ResMed-format tree
```
then import from WSL, as in the README's step 4.

`--port auto` finds the device by USB VID:PID, so you rarely need `--port COM3`.

### Travelling: pass `--tz`

The device stores UTC and both `parse.py` and `convert.py` render it in **this
machine's** local zone by default. If you sleep in another timezone without
changing the laptop clock, every night lands at the wrong time of day and
sometimes the wrong date — a 23:00 Tokyo night shows up as 07:00 the same
morning on a US-Pacific clock, which then counts as a daytime nap. (The cpap
repo's importer has its own `--tz`/`--since`, the app's "Zone slept in" fields.)

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

Keep the laptop on one timezone throughout. File names and contents depend on
the rendered timestamp, so converting the same nights under two different zones
into one folder produces two sets of files — duplicate nights.

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
- **Where the exe keeps its files:** `%LOCALAPPDATA%\TranscendSync\` (`dump.txt`
  and `sleephq\out\`), shown in the first lines of the log. Not beside the exe:
  a one-file exe unpacks into a temporary `%TEMP%\_MEIxxxx` folder that Windows
  deletes when the app closes, so files kept there vanished with the window
  (fixed 2026-11-02). Run from source, files still sit beside `app.py`.
- **Import from the frozen exe** runs the cpap repo's `transcend_import.py`
  (numpy, pandas, matplotlib — too heavy to bundle); Pull, Convert and Show
  settings need nothing installed. The log's `importer:` line shows the script
  and says NOT FOUND if it isn't there; set `CPAP_REPO` to the repo.
  - **Repo in WSL** (dejavu): set `CPAP_REPO` to its `\\wsl.localhost\<distro>\...`
    path and `TRANSCEND_REPO` to this repo's (the exe has no `parse.py` on
    disk, and the importer's default is `~/personal/transcend-mini-CPAP`):
    `[Environment]::SetEnvironmentVariable("CPAP_REPO", "\\wsl.localhost\Ubuntu\home\<you>\projects\cpap", "User")`
    and the same for `TRANSCEND_REPO`. The app then runs the importer **inside
    WSL** (`wsl.exe -d <distro> --cd <repo> ... python3 transcend_import.py`)
    with the dump as `/mnt/c/...`. Windows Python can't do it: SQLite can't
    lock SleepHQ Local's cache through `\\wsl.localhost` ("database is locked",
    seen 2026-11-03), so it stores the dump but leaves the viewer stale.
  - **Windows checkout**: it runs under `CPAP_PYTHON` if set, else
    `python`/`py` on PATH — skipping the Microsoft Store placeholder in
    `WindowsApps`, which is often first on PATH — else the newest
    `%LOCALAPPDATA%\Programs\Python\Python3*` install; the log names it.
- **Smart App Control** (Windows 11, enforcing) judges every unsigned build
  separately, because each build is a new file with no reputation. On dejavu
  the 2026-11-02 build ran; the first 2026-11-03 build was blocked
  ("An Application Control policy has blocked this file", Code Integrity event
  3077 in `Microsoft-Windows-CodeIntegrity/Operational`) on every launch; a
  clean rebuild minutes later (`--clean`) ran. So a block is per-file, not
  permanent: rebuild and try again, or run from source — `pythonw app.py` is
  never blocked, since the python.org interpreter is signed. Only code signing
  makes the exe reliable. Do not turn Smart App Control off to get round it:
  it can't be turned back on without reinstalling Windows.
- `--tz` with a zone *name* (`Asia/Tokyo`) needs the `tzdata` package on
  Windows, which PyInstaller does not bundle by itself. The GUI never uses
  `--tz`; for the CLI, `pip install tzdata` or pass an offset (`+09:00`).

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
