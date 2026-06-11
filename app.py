#!/usr/bin/env python3
"""Transcend Sync — minimal Windows GUI for the Transcend Micro toolchain.

Buttons map 1:1 onto the CLI stages (see README.md):
    Pull        collect.py        device -> dump.txt
    Convert     sleephq/convert   dump.txt -> ResMed SD tree (sleephq/out)
    Upload      SleepHQ uploader  sleephq/out -> SleepHQ (external tool)
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
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "sleephq"))

import collect as tcollect       # noqa: E402
import settings as tsettings     # noqa: E402
import convert as tconvert       # noqa: E402
from transport import TransportError  # noqa: E402

DUMP = os.path.join(HERE, "dump.txt")
OUT = os.path.join(HERE, "sleephq", "out")
UPLOADER = os.environ.get("SLEEPHQ_UPLOADER",
                          os.path.expanduser("~/cpap/sleephq_upload.py"))


def python_exe():
    """Interpreter for the external uploader. A frozen .exe is not a Python,
    so fall back to whatever python is on PATH."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("python") or shutil.which("py") or "python"


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
        self.port = tk.StringVar(value="COM3")
        ttk.Entry(top, textvariable=self.port, width=12).pack(side="left", padx=(4, 16))

        self.buttons = []
        for label, fn in [("Pull", self.do_pull), ("Convert", self.do_convert),
                          ("Upload", self.do_upload),
                          ("Pull → Convert → Upload", self.do_all),
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
        self.log(f"uploader: {UPLOADER}"
                 + ("" if os.path.exists(UPLOADER) else "   (NOT FOUND — set SLEEPHQ_UPLOADER)"))
        self.log("Ready. Device settings are view-only here; use settings.py to change them.\n")

    # ------------------------------------------------------------------ infra
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
        """Run fn capturing its stdout into the log pane."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(*a, **kw)
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

    def do_upload(self):
        if not os.path.exists(UPLOADER):
            self.log(f"Uploader not found at {UPLOADER} — set SLEEPHQ_UPLOADER.")
            return
        self.log("==> Uploading to SleepHQ ...")
        proc = subprocess.Popen(
            [python_exe(), UPLOADER, "--data-dir", OUT, "--all",
             "--import-name", f"Transcend (app, {date.today():%Y-%m-%d})"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            self.log(line.rstrip())
        if proc.wait() != 0:
            self.log(f"Upload FAILED (exit {proc.returncode}).")
        else:
            self.log("Upload complete.")

    def do_all(self):
        self.do_pull()
        self.do_convert()
        self.do_upload()

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
