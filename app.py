#!/usr/bin/env python3
"""Transcend Sync — minimal cross-platform GUI for the Transcend Micro toolchain.

Runs on macOS, Linux and Windows: it's plain tkinter (ships with python.org
Python and Homebrew's python-tk) and drives the same pure-Python CLI stages.

Buttons map 1:1 onto the CLI stages (see README.md):
    Pull        collect.py        device -> dump.txt
    Convert     sleephq/convert   dump.txt -> ResMed SD tree (sleephq/out)
    Analyze     cpap_analyzer.py  sleephq/out -> charts + CSV (your local analyzer)
    Settings    settings.py       read-only view of the device configuration

Run with a normal Python (pip install pyserial) on Windows, or see
packaging/WINDOWS.md to build a standalone .exe with PyInstaller.

Device settings are intentionally NOT editable here — the CLI's deliberate
friction (--allow-prescription, typed confirmation, auto-backup) is a safety
feature; use settings.py for changes.
"""
import contextlib
import io
import os
import queue
import shutil
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "sleephq"))

import collect as tcollect       # noqa: E402
import settings as tsettings     # noqa: E402
import convert as tconvert       # noqa: E402
import transport as ttransport   # noqa: E402
from transport import TransportError  # noqa: E402

def app_paths(frozen, here, localappdata, env_analyzer, home=None):
    """Where the app keeps the dump, the converted tree and the analysis
    output, and which analyzer script Analyze runs.

    A one-file PyInstaller exe unpacks into a temporary %TEMP%\\_MEIxxxx folder
    that is deleted when the app closes, and __file__ points into it, so the
    frozen app keeps its data in %LOCALAPPDATA%\\TranscendSync instead. Run from
    source, files sit beside app.py.

    The analyzer is the separate ResMed analyzer (~/projects/cpap), which needs
    pandas + matplotlib, so it always runs as a script under a real Python.
    CPAP_ANALYZER, when set, wins. Its output goes to its own "analysis" folder,
    never the analyzer's default (its own directory), so a Transcend run can't
    overwrite the ResMed charts and CSV."""
    home = home or os.path.expanduser("~")
    if frozen:
        data = os.path.join(localappdata or home, "TranscendSync")
    else:
        data = here
    return {"dump": os.path.join(data, "dump.txt"),
            "out": os.path.join(data, "sleephq", "out"),
            "analysis": os.path.join(data, "analysis"),
            "analyzer": env_analyzer or os.path.join(home, "projects", "cpap", "cpap_analyzer.py")}


FROZEN = getattr(sys, "frozen", False)
_PATHS = app_paths(FROZEN, HERE, os.environ.get("LOCALAPPDATA"),
                   os.environ.get("CPAP_ANALYZER"))
DUMP = _PATHS["dump"]
OUT = _PATHS["out"]
ANALYSIS = _PATHS["analysis"]
ANALYZER = _PATHS["analyzer"]
if FROZEN:
    os.makedirs(os.path.dirname(DUMP), exist_ok=True)


def is_store_stub(path):
    """The Microsoft Store "python.exe" in ...\\WindowsApps is a placeholder
    that opens the Store (or prints "Python was not found") instead of
    running anything, and it often comes first on PATH even with a real
    Python installed."""
    return "windowsapps" in (path or "").lower()


def find_python(frozen, env_python=None, which=shutil.which,
                localappdata=None, glob=None):
    """Interpreter for the external analyzer (it needs pandas + matplotlib,
    so it can't run inside the frozen exe). From source: this Python. Frozen:
    CPAP_PYTHON if set, else python/py on PATH unless it's the Store stub,
    else the newest per-user python.org install, else None."""
    if not frozen:
        return sys.executable
    if env_python:
        return env_python
    for name in ("python", "py"):
        hit = which(name)
        if hit and not is_store_stub(hit):
            return hit
    if localappdata:
        import glob as _glob
        found = (glob or _glob.glob)(os.path.join(localappdata, "Programs", "Python",
                                                  "Python3*", "python.exe"))

        def ver(p):
            tail = os.path.basename(os.path.dirname(p))[len("Python"):]
            return int(tail) if tail.isdigit() else 0
        if found:
            return max(found, key=ver)
    return None


def python_exe():
    return find_python(FROZEN, os.environ.get("CPAP_PYTHON"),
                       localappdata=os.environ.get("LOCALAPPDATA"))


class App:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk, scrolledtext
        self.tk = tk
        self.root = root
        root.title("Transcend Sync")
        root.geometry("780x520")

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Port:").pack(side="left")
        # Keep the literal "auto" in the field rather than baking in a device
        # node: it re-resolves on every action, so replugging the CPAP onto a
        # different node mid-session still works. The detected port is logged
        # below, and device errors report the port actually opened.
        self.port = tk.StringVar(value="auto")
        ttk.Entry(top, textvariable=self.port, width=24).pack(side="left", padx=(4, 16))

        self.buttons = []
        for label, fn in [("Pull", self.do_pull), ("Convert", self.do_convert),
                          ("Analyze", self.do_analyze),
                          ("Pull → Convert → Analyze", self.do_all),
                          ("Show settings", self.do_settings)]:
            b = ttk.Button(top, text=label, command=lambda f=fn: self.run_task(f))
            b.pack(side="left", padx=2)
            self.buttons.append(b)

        self.log_widget = scrolledtext.ScrolledText(root, state="disabled",
                                                    font=("Consolas", 9))
        self.log_widget.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.q = queue.Queue()
        self.busy = False
        root.after(100, self._pump)
        self.log(f"dump:     {DUMP}")
        self.log(f"output:   {OUT}")
        self.log(f"analysis: {ANALYSIS}")
        self.log(f"analyzer: {ANALYZER}"
                 + ("" if os.path.exists(ANALYZER) else "   (NOT FOUND — set CPAP_ANALYZER)"))
        self.log(f"port:     auto -> {self._detected_port()}")
        self.log("Ready. Device settings are view-only here; use settings.py to change them.\n")

    # ------------------------------------------------------------------ infra
    @staticmethod
    def _detected_port():
        """What "auto" resolves to right now, for the startup banner only —
        never fatal, since the GUI must open even with no device or pyserial."""
        try:
            return ttransport.find_port() or (
                f"{ttransport.WSL_DEFAULT_PORT} (WSL bridge)" if ttransport.is_wsl()
                else "nothing detected — plug the CPAP in")
        except TransportError as e:
            return str(e)

    def log(self, msg):
        self.q.put(str(msg))

    def _pump(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self.log_widget.configure(state="normal")
                self.log_widget.insert("end", msg + "\n")
                self.log_widget.see("end")
                self.log_widget.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def run_task(self, fn):
        if self.busy:
            return
        self.busy = True
        for b in self.buttons:
            b.state(["disabled"])

        def worker():
            try:
                fn()
            except TransportError as e:
                self.log(f"DEVICE ERROR: {e}")
            except SystemExit as e:          # CLI helpers exit on failure
                self.log(f"ERROR: {e}")
            except Exception as e:
                self.log(f"ERROR: {type(e).__name__}: {e}")
            finally:
                self.q.put("")               # spacer
                self.busy = False
                self.root.after(0, lambda: [b.state(["!disabled"]) for b in self.buttons])

        threading.Thread(target=worker, daemon=True).start()

    def _captured(self, fn, *a, **kw):
        """Run fn capturing its stdout and stderr into the log pane; return
        what fn returned (an exit code, for the CLI mains)."""
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                return fn(*a, **kw)
        finally:
            for line in buf.getvalue().splitlines():
                self.log(line)

    # ------------------------------------------------------------------ tasks
    def do_pull(self):
        self.log(f"==> Pulling event log from {self.port.get()} ...")
        tcollect.collect(self.port.get(), DUMP, log=self.log)

    def do_convert(self):
        if not os.path.exists(DUMP):
            self.log(f"No dump at {DUMP} — Pull first.")
            return
        self.log(f"==> Converting -> {OUT} ...")
        self._captured(tconvert.main, [DUMP, "--out", OUT])

    def do_analyze(self):
        if not os.path.isdir(OUT):
            self.log(f"No converted data at {OUT} — Convert first.")
            return
        if not os.path.exists(ANALYZER):
            self.log(f"Analyzer not found at {ANALYZER} — set CPAP_ANALYZER.")
            return
        py = python_exe()
        if not py:
            self.log("No Python found to run the analyzer — install Python from "
                     "python.org (with pandas and matplotlib) or set CPAP_PYTHON.")
            return
        os.makedirs(ANALYSIS, exist_ok=True)
        self.log(f"==> Analyzing -> {ANALYSIS} (with {py}) ...")
        proc = subprocess.Popen([py, ANALYZER, "--data-dir", OUT,
                                 "--output-dir", ANALYSIS],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            self.log(line.rstrip())
        rc = proc.wait()
        if rc:
            self.log(f"Analyze FAILED (exit {rc}) — the analyzer needs a Python "
                     "with pandas and matplotlib.")
        else:
            self.log(f"Analysis complete: charts and cpap_data.csv in {ANALYSIS}")

    def do_all(self):
        self.do_pull()
        self.do_convert()
        self.do_analyze()

    def do_settings(self):
        self.log(f"==> Reading settings from {self.port.get()} (read-only) ...")
        cfg = tsettings.read_config(self.port.get())
        self._captured(tsettings.print_config, cfg)
        self._captured(tsettings.print_usage, self.port.get())


def main():
    import tkinter as tk
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
