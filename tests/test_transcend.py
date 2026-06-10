"""Tests for the dump decoder (parse.py) and the SleepHQ converter (sleephq/).

No device needed: events are synthesized with enc(), the inverse of
parse.decode_event's bit layout (see PROTOCOL.md).

Run:  python3 -m unittest discover -s tests
"""
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sleephq"))
import parse  # noqa: E402
import edf    # noqa: E402

CONVERT = os.path.join(ROOT, "sleephq", "convert.py")


def enc(dt, etype, sub):
    """Encode one 5-byte event record (inverse of parse.decode_event).
    dt is the device's UTC wall clock, minute resolution."""
    w1 = (dt.year - 2000) << 9 | dt.month << 5 | dt.day
    w2 = dt.hour << 11 | dt.minute << 5 | etype
    h = lambda w: format(w, "04x")[2:4] + format(w, "04x")[0:2]   # 16-bit little-endian
    return h(w1) + h(w2) + format(sub, "02x")


def write_dump(path, blocks):
    """blocks = list of (start_address, [records])."""
    with open(path, "w") as f:
        for addr, recs in blocks:
            f.write(f"BLOCK {addr} {''.join(recs)}\n")


def utc(ev):
    """Decoded event's datetime back in naive UTC (decode_event converts to local)."""
    return ev["dt"].astimezone(timezone.utc).replace(tzinfo=None)


class TestDecode(unittest.TestCase):
    def test_roundtrip(self):
        dt = datetime(2026, 6, 1, 22, 35)
        ev = parse.decode_event(enc(dt, 9, 12))
        self.assertEqual(utc(ev), dt)
        self.assertEqual(ev["type"], 9)
        self.assertEqual(ev["name"], "ApneaDetected")
        self.assertEqual(ev["value"], 12)          # scale 1.0

    def test_scaled_value(self):
        ev = parse.decode_event(enc(datetime(2026, 1, 2, 3, 4), 1, 80))
        self.assertEqual(ev["name"], "StartTherapy")
        self.assertEqual(ev["value"], 8.0)         # scale 0.1

    def test_empty_and_malformed(self):
        self.assertIsNone(parse.decode_event("f" * 10))
        self.assertIsNone(parse.decode_event("F" * 10))
        self.assertIsNone(parse.decode_event("abcd"))

    def test_invalid_date_decodes_with_dt_none(self):
        # month 0 cannot exist -> the record still decodes, but with dt=None
        # (load_events later drops dt-less events)
        w1 = (26 << 9) | (0 << 5) | 1                  # year 2026, month 0, day 1
        w2 = (3 << 11) | (4 << 5) | 9                  # 03:04, ApneaDetected
        h = lambda w: format(w, "04x")[2:4] + format(w, "04x")[0:2]
        ev = parse.decode_event(h(w1) + h(w2) + "01")
        self.assertIsNone(ev["dt"])
        self.assertEqual(ev["raw_dt"], "2026-00-01 03:04")


class TestHeader(unittest.TestCase):
    def test_parse_header(self):
        serial, firmware = "B1234567", "12.0"
        a = ("0" * 4
             + serial.encode().hex().ljust(64, "0")     # 32 bytes, zero-padded
             + firmware.encode().hex().ljust(8, "0")    # 4 bytes
             + "0" * 4
             + "2c01"                                   # 300 events, little-endian
             + "0500")                                  # offset 5
        hdr = parse.parse_header("HEADER Rbd" + a)
        self.assertEqual(hdr["serial"], serial)
        self.assertEqual(hdr["firmware"], firmware)
        self.assertEqual(hdr["events_in_queue"], 300)
        self.assertEqual(hdr["offset"], 5)


class TestLoadEvents(unittest.TestCase):
    def setUp(self):
        self.t0 = datetime(2026, 6, 1, 22, 0)
        # complete session with byte-identical twin apneas (same minute, same duration)
        self.recs = [enc(self.t0, 1, 80),
                     enc(self.t0 + timedelta(minutes=30), 9, 12),
                     enc(self.t0 + timedelta(minutes=30), 9, 12),
                     enc(self.t0 + timedelta(minutes=40), 22, 7),
                     enc(self.t0 + timedelta(hours=7), 2, 0)]

    def test_empty_block_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "dump.txt")
            with open(p, "w") as f:
                f.write("BLOCK 100 " + "".join(self.recs) + "\n")
                f.write("BLOCK 200 \n")    # bare Ra9 response: empty payload
                f.write("BLOCK 300\n")
            _, events = parse.load_events(p)
        self.assertEqual(len(events), 5)

    def test_twin_records_within_one_dump_both_kept(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "dump.txt")
            write_dump(p, [(150, self.recs)])
            _, events = parse.load_events(p)
        self.assertEqual(sum(1 for e in events if e["type"] == 9), 2)

    def test_overlapping_dumps_dedupe_by_address(self):
        extra = [enc(self.t0 + timedelta(days=1), 1, 80),
                 enc(self.t0 + timedelta(days=1, hours=6), 2, 0)]
        with tempfile.TemporaryDirectory() as d:
            d1, d2 = os.path.join(d, "d1.txt"), os.path.join(d, "d2.txt")
            write_dump(d1, [(150, self.recs)])
            write_dump(d2, [(150, self.recs + extra)])   # second pull re-reads the queue
            _, events = parse.load_events([d1, d2])
        self.assertEqual(len(events), 7)                 # 5 overlap + 2 new
        self.assertEqual(sum(1 for e in events if e["type"] == 9), 2)
        sessions = parse.build_sessions(events)
        self.assertEqual(len(sessions), 2)
        self.assertTrue(all(s["end"] for s in sessions))


class TestBuildSessions(unittest.TestCase):
    def test_open_session_has_no_end(self):
        t0 = datetime(2026, 6, 1, 22, 0)
        recs = [enc(t0, 1, 80), enc(t0 + timedelta(minutes=10), 22, 7)]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "dump.txt")
            write_dump(p, [(100, recs)])
            _, events = parse.load_events(p)
        sessions = parse.build_sessions(events)
        self.assertEqual(len(sessions), 1)
        self.assertIsNone(sessions[0]["end"])
        self.assertEqual(len(sessions[0]["evs"]), 1)


class TestWriteEve(unittest.TestCase):
    def test_annotations_roundtrip_and_record_timestamps(self):
        anns = [(1800, 12, "Obstructive Apnea"), (5400, 0, "Hypopnea")]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "EVE.edf")
            edf.write_eve(p, datetime(2026, 6, 1, 22, 0), anns, "TESTSER1")
            e = edf.Edf(p)
            got = e.annotations()
            # EDF+ timekeeping TAL: each 64-byte record opens with its own onset
            for r, onset in enumerate(["0", "1800", "5400"]):
                rec = e.data_raw[r * 64:(r + 1) * 64]
                self.assertTrue(rec.startswith(f"+{onset}\x14\x14\x00".encode()),
                                f"record {r} timekeeping TAL: {rec[:12]!r}")
        self.assertEqual([(o, du, t) for o, du, t in got],
                         [("+0", "0", "Recording starts"),
                          ("+1800", "12", "Obstructive Apnea"),
                          ("+5400", "0", "Hypopnea")])


class TestNoDonorSerial(unittest.TestCase):
    """Guard: the ResMed donor-device serial purged from git history must never return
    (e.g. via a patch ported from a pre-rewrite clone)."""

    def test_tracked_files_clean(self):
        needle = ("23243" "362472").encode()   # split so this file doesn't trip its own scan
        try:
            out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                                 capture_output=True, check=True).stdout
        except Exception:
            self.skipTest("not a git checkout")
        offenders = []
        for name in filter(None, out.split(b"\x00")):
            path = os.path.join(ROOT, name.decode())
            with open(path, "rb") as fh:
                if needle in fh.read():
                    offenders.append(name.decode())
        self.assertEqual(offenders, [], "donor serial reintroduced in tracked files")


class TestConvertEndToEnd(unittest.TestCase):
    def _dump(self, d):
        t0 = datetime(2026, 6, 1, 22, 0)
        recs = [enc(t0, 1, 80),
                enc(t0, 5, 40),                            # RampStart @ 4.0 cmH2O
                enc(t0 + timedelta(minutes=10), 6, 0),     # RampEnd -> 10 min ramp
                enc(t0 + timedelta(minutes=15), 15, 20),   # EZEX level 2
                enc(t0 + timedelta(minutes=15), 13, 60),   # MinimumPressureSetting 6.0
                enc(t0 + timedelta(minutes=15), 14, 150)]  # MaximumPressureSetting 15.0
        recs += [enc(t0 + timedelta(minutes=5 * k), 22, 7) for k in range(1, 80)]
        recs += [enc(t0 + timedelta(minutes=30), 9, 12),
                 enc(t0 + timedelta(minutes=45), 24, 85),   # PressureIncreasedFromHypopneas -> 8.5
                                                            # (consistent with MaxPressureUsed 9.5 below)
                 enc(t0 + timedelta(hours=7), 16, 78),
                 enc(t0 + timedelta(hours=7), 17, 95),
                 enc(t0 + timedelta(hours=7), 2, 0)]
        # open session (no EndTherapy), 90 min of leak events
        t1 = datetime(2026, 6, 3, 22, 0)
        recs += [enc(t1, 1, 80)]
        recs += [enc(t1 + timedelta(minutes=5 * k), 22, 9) for k in range(1, 19)]
        # open session too short to survive --min-minutes (2 min)
        t2 = datetime(2026, 6, 5, 22, 0)
        recs += [enc(t2, 1, 80), enc(t2 + timedelta(minutes=2), 22, 9)]
        path = os.path.join(d, "dump.txt")
        write_dump(path, [(100, recs)])
        return path

    def _run(self, d, *extra):
        out = os.path.join(d, "out")
        r = subprocess.run([sys.executable, CONVERT, self._dump(d), "--out", out,
                            "--serial", "TESTSER1", *extra],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return out, r.stdout

    def test_outputs_and_truncation_note(self):
        with tempfile.TemporaryDirectory() as d:
            out, stdout = self._run(d)
            # the 2-min open session is filtered: note must count exactly 1, not 2
            self.assertIn("1 session(s) had no EndTherapy", stdout)
            self.assertTrue(os.path.exists(os.path.join(out, "Identification.json")))
            days = sorted(os.listdir(os.path.join(out, "DATALOG")))
            self.assertEqual(days, ["20260601", "20260603"])
            for day in days:
                exts = sorted(f.split("_")[-1] for f in
                              os.listdir(os.path.join(out, "DATALOG", day)))
                self.assertEqual(exts, ["BRP.edf", "CSL.edf", "EVE.edf", "PLD.edf"])
            e = edf.Edf(os.path.join(out, "STR.edf"))
            self.assertEqual(e.hdr["n_records"], 2)
            idx = {s["label"]: i for i, s in enumerate(e.signals)}
            self.assertEqual(e.signal_phys(idx["Duration"]), [420.0, 90.0])
            ahi = e.signal_phys(idx["AHI"])
            self.assertAlmostEqual(ahi[0], 1 / 7, places=1)   # 1 apnea / 7 h
            self.assertEqual(ahi[1], 0.0)

    def test_min_minutes_zero_keeps_short_session(self):
        with tempfile.TemporaryDirectory() as d:
            _, stdout = self._run(d, "--min-minutes", "0")
            self.assertIn("2 session(s) had no EndTherapy", stdout)

    def test_str_settings_panel(self):
        # day 1 has ramp/EZEX/set-pressure events; day 2 (open session) has none
        with tempfile.TemporaryDirectory() as d:
            out, _ = self._run(d, "--mask", "3")
            e = edf.Edf(os.path.join(out, "STR.edf"))
            idx = {s["label"]: i for i, s in enumerate(e.signals)}
            g = lambda lbl: [round(v, 3) for v in e.signal_phys(idx[lbl])]
            self.assertEqual(g("S.Mask"), [3, 3])             # --mask code, every record
            self.assertEqual(g("S.A.MinPress"), [6, 8])       # set events; day-2 fallback = start_pressure
            self.assertEqual(g("S.A.MaxPress"), [15, 8])
            self.assertEqual(g("S.EPR.EPREnable"), [2, 1])    # 2 = On, 1 = Off
            self.assertEqual(g("S.EPR.Level"), [2, 0])
            self.assertEqual(g("S.RampEnable"), [3, 1])       # 3 = On, 1 = Off
            self.assertEqual(g("S.RampTime"), [10, 0])        # snapped to 5-min increments

    def test_ramp_curve_starts_at_ramp_start_pressure(self):
        # Locks the RampStart subdata encoding: 40 -> 4.0 cmH2O (x10). The day-1 pressure
        # curve must rise FROM the ramp start pressure (4.0), not begin flat at the therapy
        # pressure (8.0 from StartTherapy sub=80). If the x10 decode regressed, this fails.
        with tempfile.TemporaryDirectory() as d:
            out, _ = self._run(d)
            day = os.path.join(out, "DATALOG", "20260601")
            pld = next(f for f in os.listdir(day) if f.endswith("PLD.edf"))
            e = edf.Edf(os.path.join(day, pld))
            idx = {s["label"]: i for i, s in enumerate(e.signals)}
            press = e.signal_phys(idx["Press.2s"])
            self.assertAlmostEqual(press[0], 4.0, delta=0.3)  # starts at the ramp start pressure
            self.assertGreater(max(press), 7.5)               # ramps up toward therapy pressure

    def test_pressure_reason_flags_opt_in(self):
        # default: no pressure-reason annotations; --pressure-reason-flags adds them to EVE.
        def eve_bytes(out):
            day = os.path.join(out, "DATALOG", "20260601")
            f = next(x for x in os.listdir(day) if x.endswith("EVE.edf"))
            with open(os.path.join(day, f), "rb") as fh:
                return fh.read()
        with tempfile.TemporaryDirectory() as d:
            out, _ = self._run(d)
            self.assertNotIn(b"Pressure increase", eve_bytes(out))     # off by default
        with tempfile.TemporaryDirectory() as d:
            out, _ = self._run(d, "--pressure-reason-flags")
            self.assertIn(b"Pressure increase (hypopnea)", eve_bytes(out))


if __name__ == "__main__":
    unittest.main()
