#!/usr/bin/env python3
"""Serial transport for the Transcend Micro PAP protocol (see PROTOCOL.md).

Two interchangeable backends:

* PySerialTransport   — talks to the port directly via pyserial. Use on native
  Windows (COM3) or anywhere the port is a real device node for this Python
  (e.g. a usbipd-attached /dev/ttyUSB0 under WSL).
* PowershellTransport — shells out to powershell.exe + pap.ps1, the original
  WSL-friendly path (WSL2 cannot see COM ports without usbipd).

make_transport() picks automatically: a COMx port from non-Windows Python
(i.e. WSL) -> the powershell bridge; everything else -> pyserial. Callers do:

    with make_transport(port) as t:
        resp = t.command("Tbd")        # -> "Rbd...." (3-char code + args)

Timing mirrors the official client and pap.ps1: 60 ms settle + input flush
before each command, each char written individually with a 12 ms gap, then a
CR terminator; the response is complete once TWO CRs have been seen (the
device echoes the command, so CR #1 ends the echo, CR #2 ends the response).
Open one transport per command for settings traffic (the device handles a
single command per connection most reliably); a download may hold one open
session for all of its block reads, as the official client does.
"""
import os
import shutil
import subprocess
import sys
import time

PAP_PS1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pap.ps1")
BAUD = 38400
CHAR_GAP_S = 0.012     # gap between echoed command characters
SETTLE_S = 0.06        # quiet period + input flush before each command


class TransportError(RuntimeError):
    """Transport-level failure: port unavailable, helper missing, timeout."""


def is_wsl():
    if sys.platform != "linux":
        return False
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def make_transport(port, prefer="auto"):
    """Return a transport for `port`. `prefer` = 'auto' | 'pyserial' |
    'powershell' | an already-built transport-like object (ducks through,
    for tests)."""
    if hasattr(prefer, "command"):
        return prefer
    if prefer in (None, "auto"):
        # A COMx name only means something to Windows; from WSL/Linux it must
        # go through powershell.exe. A /dev/... port is always pyserial.
        prefer = ("powershell" if sys.platform != "win32"
                  and port.upper().startswith("COM") else "pyserial")
    if prefer == "powershell":
        return PowershellTransport(port)
    if prefer == "pyserial":
        return PySerialTransport(port)
    raise ValueError(f"unknown transport {prefer!r}")


class PySerialTransport:
    """Direct serial access via pyserial (imported lazily so the rest of the
    toolchain stays stdlib-only)."""

    def __init__(self, port, timeout=10.0):
        self.port_name = port
        self.timeout = timeout
        self._ser = None

    def open(self):
        try:
            import serial
        except ImportError:
            raise TransportError(
                "pyserial is not installed — pip install pyserial, "
                "or use --transport powershell")
        ser = serial.Serial()
        ser.port = self.port_name
        ser.baudrate = BAUD
        ser.bytesize = serial.EIGHTBITS
        ser.parity = serial.PARITY_NONE
        ser.stopbits = serial.STOPBITS_ONE
        ser.timeout = 0.02      # poll in small slices; command() owns the deadline
        ser.rts = False         # set BEFORE open so the control lines never pulse
        ser.dtr = False
        try:
            ser.open()
        except serial.SerialException as e:
            raise TransportError(f"cannot open {self.port_name}: {e}")
        self._ser = ser
        return self

    def close(self):
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def command(self, cmd, timeout=None):
        """Send one command, return the response (3-char code + args), or ""
        if the device stayed silent until the deadline."""
        ser = self._ser
        if ser is None:
            raise TransportError("port not open")
        deadline = time.monotonic() + (timeout or self.timeout)
        time.sleep(SETTLE_S)
        ser.reset_input_buffer()
        for ch in cmd:
            ser.write(ch.encode("ascii"))
            ser.flush()
            time.sleep(CHAR_GAP_S)
        ser.write(b"\r")
        ser.flush()
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = ser.read(ser.in_waiting or 1)
            if chunk:
                buf += chunk.replace(b"\x00", b"")     # DiscardNull
                if buf.count(b"\r") >= 2:
                    break
        s = buf.decode("ascii", "replace")
        i = s.find("\r")
        return "" if i < 0 else s[i:].strip("\r")


class PowershellTransport:
    """One powershell.exe + pap.ps1 process per command — the original WSL
    path. Slower than pyserial (~2 s process overhead per command) but needs
    no Linux-side drivers or usbipd."""

    def __init__(self, port, timeout=10.0):
        self.port_name = port
        self.timeout = timeout

    def open(self):
        return self

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def command(self, cmd, timeout=None):
        script = PAP_PS1
        if shutil.which("wslpath"):
            script = subprocess.check_output(["wslpath", "-w", PAP_PS1],
                                             text=True).strip()
        t = int(timeout or self.timeout)
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", script, "-Port", self.port_name,
                 "-Command", cmd, "-TimeoutSec", str(t)],
                capture_output=True, text=True, timeout=t + 30,
            )
        except FileNotFoundError:
            raise TransportError("powershell.exe not found — this backend "
                                 "requires Windows or WSL")
        except subprocess.TimeoutExpired:
            raise TransportError(f"timed out waiting for response to {cmd!r} "
                                 f"on {self.port_name}")
        out = [ln.rstrip("\r") for ln in proc.stdout.splitlines() if ln.strip()]
        return out[0] if out else ""
