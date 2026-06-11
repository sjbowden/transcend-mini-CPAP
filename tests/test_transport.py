"""Tests for transport.py (pyserial backend framing, backend selection) and
collect.py (download loop, dump format) — no device and no pyserial needed:
the serial layer is faked at the object level.

Run:  python3 -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import transport  # noqa: E402
import collect as tcollect  # noqa: E402
import parse  # noqa: E402
from test_transcend import enc  # noqa: E402  (synthetic event records)
from datetime import datetime, timedelta  # noqa: E402


class FakeSerial:
    """Duck-typed stand-in for serial.Serial: echoes every char, and when CR
    arrives appends `mapping[cmd]` + CR to the read buffer (response None =
    stay silent, like a dead device)."""

    def __init__(self, mapping):
        self.mapping = mapping
        self._pending = ""
        self.buf = b""
        self.char_writes = []          # each write() payload, to assert framing
        self.resets = 0
        self.closed = False

    def write(self, data):
        self.char_writes.append(data)
        for ch in data.decode("ascii"):
            if ch == "\r":
                resp = self.mapping.get(self._pending)
                self.buf += (self._pending + "\r").encode()      # echo
                if resp is not None:
                    self.buf += (resp + "\r").encode()
                self._pending = ""
            else:
                self._pending += ch

    def flush(self):
        pass

    def read(self, n=1):
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    @property
    def in_waiting(self):
        return len(self.buf)

    def reset_input_buffer(self):
        self.resets += 1
        self.buf = b""

    def close(self):
        self.closed = True


def pyserial_with(mapping):
    t = transport.PySerialTransport("FAKE")
    t._ser = FakeSerial(mapping)
    return t


class TestPySerialFraming(unittest.TestCase):
    def setUp(self):                      # zero the pacing delays for test speed
        self._gap, self._settle = transport.CHAR_GAP_S, transport.SETTLE_S
        transport.CHAR_GAP_S = transport.SETTLE_S = 0

    def tearDown(self):
        transport.CHAR_GAP_S, transport.SETTLE_S = self._gap, self._settle

    def test_command_roundtrip(self):
        t = pyserial_with({"Tbd": "Rbd1234"})
        self.assertEqual(t.command("Tbd"), "Rbd1234")

    def test_chars_written_individually(self):
        t = pyserial_with({"Tab": "Rab00"})
        t.command("Tab")
        # 3 single-char writes + the CR, each its own write() call
        self.assertEqual(t._ser.char_writes, [b"T", b"a", b"b", b"\r"])

    def test_input_flushed_before_send(self):
        t = pyserial_with({"Tbd": "Rbd"})
        t._ser.buf = b"stale\rgarbage\r"          # leftovers from a prior command
        self.assertEqual(t.command("Tbd"), "Rbd")
        self.assertEqual(t._ser.resets, 1)

    def test_nulls_discarded(self):
        t = pyserial_with({})
        t._ser.mapping["Tff"] = "Rff8011"
        # inject nulls into the response the way DiscardNull would see them
        orig_write = t._ser.write
        def noisy(data):
            orig_write(data)
            if data == b"\r":
                t._ser.buf = t._ser.buf.replace(b"R", b"\x00R")
        t._ser.write = noisy
        self.assertEqual(t.command("Tff"), "Rff8011")

    def test_silent_device_returns_empty(self):
        t = pyserial_with({"Tbd": None})          # echo only, no response CR
        self.assertEqual(t.command("Tbd", timeout=0.2), "")

    def test_closed_port_raises(self):
        t = transport.PySerialTransport("FAKE")
        with self.assertRaises(transport.TransportError):
            t.command("Tbd")


class TestBackendSelection(unittest.TestCase):
    def test_com_port_off_windows_uses_powershell(self):
        if sys.platform == "win32":
            self.skipTest("auto-selects pyserial on native Windows")
        t = transport.make_transport("COM3")
        self.assertIsInstance(t, transport.PowershellTransport)

    def test_dev_port_uses_pyserial(self):
        t = transport.make_transport("/dev/ttyUSB0")
        self.assertIsInstance(t, transport.PySerialTransport)

    def test_explicit_override(self):
        self.assertIsInstance(transport.make_transport("COM3", "pyserial"),
                              transport.PySerialTransport)
        self.assertIsInstance(transport.make_transport("/dev/ttyUSB0", "powershell"),
                              transport.PowershellTransport)

    def test_instance_passthrough(self):
        class T:
            def command(self, c, timeout=None):
                return ""
        t = T()
        self.assertIs(transport.make_transport("COM3", t), t)


class FakeTransport:
    def __init__(self, mapping):
        self.mapping = mapping
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def command(self, cmd, timeout=None):
        self.commands.append(cmd)
        return self.mapping.get(cmd, "")


class TestCollect(unittest.TestCase):
    ADDR = 100                                     # Ta8 -> 0x0064

    def _mapping(self, n_valid_last=3):
        t0 = datetime(2026, 6, 1, 22, 0)
        full = "".join(enc(t0 + timedelta(minutes=k), 22, 7) for k in range(200))
        partial = ("".join(enc(t0 + timedelta(hours=4, minutes=k), 22, 9)
                           for k in range(n_valid_last))
                   + "f" * 10 * (200 - n_valid_last))
        return {
            "Tbd": "Rbd" + "0" * 88,
            "Tff": "Rff8011",
            "Ta8": "Ra80064",
            "Ta9%04X%04X" % (self.ADDR, 50): "Ra9" + "f" * 100,        # prime read
            "Ta9%04X%04X" % (self.ADDR + 50, 1000): "Ra9" + full,      # full block
            "Ta9%04X%04X" % (self.ADDR + 1050, 1000): "Ra9" + partial, # last block
        }

    def test_download_and_dump_format(self):
        ft = FakeTransport(self._mapping())
        with tempfile.TemporaryDirectory() as d:
            dump = os.path.join(d, "dump.txt")
            blocks = tcollect.collect("COM_TEST", dump, transport=ft, log=lambda *_: None)
            self.assertEqual(blocks, 2)
            lines = open(dump).read().splitlines()
            self.assertTrue(lines[0].startswith("HEADER Rbd"))
            self.assertTrue(lines[1].startswith("DEVICE Rff"))
            self.assertTrue(lines[2].startswith("ADDR Ra8"))
            self.assertEqual(lines[3].split()[1], str(self.ADDR + 50))   # decimal address
            # the dump must be byte-compatible with parse.py
            _, events = parse.load_events(dump)
            self.assertEqual(len(events), 203)                           # 200 + 3 valid
        # exactly one prime read + two block reads, addresses advancing by 1000
        ta9s = [c for c in ft.commands if c.startswith("Ta9")]
        self.assertEqual(ta9s, ["Ta900640032", "Ta9009603E8", "Ta9047E03E8"])

    def test_stops_after_partial_block(self):
        ft = FakeTransport(self._mapping())
        with tempfile.TemporaryDirectory() as d:
            tcollect.collect("COM_TEST", os.path.join(d, "x"), transport=ft,
                             log=lambda *_: None)
        # no third block request after the partial one
        self.assertEqual(sum(1 for c in ft.commands if c.startswith("Ta9")), 3)

    def test_dead_device_raises(self):
        ft = FakeTransport({"Tbd": "", "Tff": "", "Ta8": ""})
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(transport.TransportError):
                tcollect.collect("COM_TEST", os.path.join(d, "x"), transport=ft,
                                 log=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
