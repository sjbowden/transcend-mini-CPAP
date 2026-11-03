#!/usr/bin/env python3
"""Transcend Sync — minimal cross-platform GUI for the Transcend Micro toolchain.

Runs on macOS, Linux and Windows: it's plain tkinter (ships with python.org
Python and Homebrew's python-tk) and drives the same pure-Python CLI stages.

Buttons map 1:1 onto the CLI stages (see README.md):
    Pull        collect.py        device -> dump.txt
    Convert     sleephq/convert   dump.txt -> ResMed SD tree (sleephq/out)
    Import      transcend_import  dump.txt -> ~/projects/cpap (stored dump + SleepHQ Local)
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

def app_paths(frozen, here, localappdata, env_cpap_repo, home=None):
    """Where the app keeps the dump and the converted tree, and where the cpap
    repo (whose transcend_import.py Import runs) lives.

    A one-file PyInstaller exe unpacks into a temporary %TEMP%\\_MEIxxxx folder
    that is deleted when the app closes, and __file__ points into it, so the
    frozen app keeps its data in %LOCALAPPDATA%\\TranscendSync instead. Run from
    source, files sit beside app.py.

    The cpap repo defaults to ~/projects/cpap; CPAP_REPO, when set, wins."""
    home = home or os.path.expanduser("~")
    if frozen:
        data = os.path.join(localappdata or home, "TranscendSync")
    else:
        data = here
    cpap = env_cpap_repo or os.path.join(home, "projects", "cpap")
    return {"dump": os.path.join(data, "dump.txt"),
            "out": os.path.join(data, "sleephq", "out"),
            "cpap_repo": cpap,
            "importer": os.path.join(cpap, "transcend_import.py")}


def import_command(py, importer, dump, tz="", since=""):
    """argv for transcend_import.py. A blank zone keeps the zone in force; the
    importer itself refuses a zone change without --since when the dump holds
    nights it hasn't seen, and says so."""
    cmd = [py, importer, dump]
    if tz.strip():
        cmd += ["--tz", tz.strip()]
    if since.strip():
        cmd += ["--since", since.strip()]
    return cmd


def import_env(frozen, here, environ):
    """transcend_import.py decodes with this repo's parse.py, found through
    TRANSCEND_REPO (its default is ~/personal/transcend-mini-CPAP). Run from
    source, point it at this checkout unless already set. A frozen exe has no
    parse.py on disk, so TRANSCEND_REPO must come from the environment."""
    env = dict(environ)
    if not frozen:
        env.setdefault("TRANSCEND_REPO", here)
    return env


def wsl_split(path):
    """\\\\wsl.localhost\\<distro>\\a\\b (or \\\\wsl$\\...) -> (distro, "/a/b"); else None."""
    p = (path or "").replace("/", "\\")
    for prefix in ("\\\\wsl.localhost\\", "\\\\wsl$\\"):
        if p.lower().startswith(prefix.lower()):
            distro, _, rest = p[len(prefix):].partition("\\")
            if distro:
                return distro, "/" + rest.replace("\\", "/").strip("/")
    return None


def to_wsl_path(path):
    """A Windows path as WSL sees it: C:\\x\\y -> /mnt/c/x/y; a WSL UNC path -> its
    Linux path; anything else unchanged."""
    split = wsl_split(path)
    if split:
        return split[1]
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        return "/mnt/" + path[0].lower() + path[2:].replace("\\", "/")
    return path


def wsl_import_command(distro, linux_repo, dump, tz="", since="", transcend_repo=None):
    """Run transcend_import.py inside WSL, for a cpap repo that lives there:
    SQLite can't lock SleepHQ Local's cache through \\\\wsl.localhost ("database
    is locked"), so Windows Python could store the dump but not refresh the
    viewer. WSL's own python3 (which has the repo's dependencies) does both."""
    inner = ["env"]
    if transcend_repo:
        inner.append(f"TRANSCEND_REPO={to_wsl_path(transcend_repo)}")
    inner += import_command("python3", "transcend_import.py", to_wsl_path(dump), tz, since)
    return ["wsl.exe", "-d", distro, "--cd", linux_repo, "--"] + inner


FROZEN = getattr(sys, "frozen", False)
_PATHS = app_paths(FROZEN, HERE, os.environ.get("LOCALAPPDATA"),
                   os.environ.get("CPAP_REPO"))
DUMP = _PATHS["dump"]
OUT = _PATHS["out"]
CPAP_REPO = _PATHS["cpap_repo"]
IMPORTER = _PATHS["importer"]
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
    """Interpreter for the cpap repo's importer (it needs numpy, pandas and
    matplotlib, so it can't run inside the frozen exe). From source: this Python. Frozen:
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
        for label, fn in [("Pull", self.do_pull), ("Import", self.do_import),
                          ("Pull → Import", self.do_all),
                          ("Convert", self.do_convert),
                          ("Show settings", self.do_settings)]:
            b = ttk.Button(top, text=label, command=lambda f=fn: self.run_task(f))
            b.pack(side="left", padx=2)
            self.buttons.append(b)

        # Only for travel: the zone slept in, passed to transcend_import.py as
        # --tz / --since. Blank keeps the zone already recorded.
        trip = ttk.Frame(root, padding=(8, 0, 8, 8))
        trip.pack(fill="x")
        ttk.Label(trip, text="Zone slept in (blank = unchanged):").pack(side="left")
        self.tz = tk.StringVar(value="")
        ttk.Entry(trip, textvariable=self.tz, width=20).pack(side="left", padx=(4, 16))
        ttk.Label(trip, text="Arrived (local time, first import there):").pack(side="left")
        self.since = tk.StringVar(value="")
        ttk.Entry(trip, textvariable=self.since, width=18).pack(side="left", padx=4)

        self.log_widget = scrolledtext.ScrolledText(root, state="disabled",
                                                    font=("Consolas", 9))
        self.log_widget.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.q = queue.Queue()
        self.busy = False
        root.after(100, self._pump)
        self.log(f"dump:     {DUMP}")
        self.log(f"output:   {OUT}")
        self.log(f"importer: {IMPORTER}"
                 + ("" if os.path.exists(IMPORTER) else "   (NOT FOUND — set CPAP_REPO)"))
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

    def do_import(self):
        """Hand the dump to the cpap repo: transcend_import.py stores it in
        transcend/dumps/ (identical bytes are a no-op) and refreshes SleepHQ
        Local, which shows Transcend nights beside the ResMed ones."""
        if not os.path.exists(DUMP):
            self.log(f"No dump at {DUMP} — Pull first.")
            return
        if not os.path.exists(IMPORTER):
            self.log(f"Importer not found at {IMPORTER} — set CPAP_REPO to the cpap repo.")
            return
        env = import_env(FROZEN, HERE, os.environ)
        wsl = wsl_split(CPAP_REPO) if sys.platform == "win32" else None
        if wsl:
            cmd = wsl_import_command(wsl[0], wsl[1], DUMP, self.tz.get(), self.since.get(),
                                     env.get("TRANSCEND_REPO"))
            cwd, how = None, f"WSL {wsl[0]}"
        else:
            py = python_exe()
            if not py:
                self.log("No Python found to run the importer — install Python from "
                         "python.org (with numpy, pandas and matplotlib) or set CPAP_PYTHON.")
                return
            cmd = import_command(py, IMPORTER, DUMP, self.tz.get(), self.since.get())
            cwd, how = CPAP_REPO, py
        self.log(f"==> Importing into {CPAP_REPO} (with {how}) ...")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # no console flash on Windows
        proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", creationflags=flags)
        for line in proc.stdout:
            self.log(line.rstrip())
        rc = proc.wait()
        if rc:
            self.log(f"Import FAILED (exit {rc}).")
        else:
            self.log("Import complete. Commit transcend/ in the cpap repo to keep the dump.")

    def do_all(self):
        self.do_pull()
        self.do_import()

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
