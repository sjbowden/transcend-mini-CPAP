"""Minimal EDF / EDF+ reader+writer tailored for ResMed STR.edf and DATALOG files."""
import struct
from datetime import datetime

def _s(b): return b.decode("ascii", "replace")

class Edf:
    def __init__(self, path=None):
        self.signals = []   # list of dicts
        self.records = []   # list of list-of-arrays (per signal)
        self.hdr = {}
        if path: self.read(path)

    def read(self, path):
        with open(path, "rb") as f:
            raw = f.read()
        self.raw = raw          # full file bytes, retained for writers reusing the template
        h = raw[:256]
        self.hdr = dict(
            version=_s(h[0:8]).strip(),
            patient=_s(h[8:88]).strip(),
            recording=_s(h[88:168]).strip(),
            startdate=_s(h[168:176]),
            starttime=_s(h[176:184]),
            hdr_bytes=int(_s(h[184:192])),
            reserved=_s(h[192:236]).strip(),
            n_records=int(_s(h[236:244])),
            rec_dur=float(_s(h[244:252])),
            n_signals=int(_s(h[252:256])),
        )
        ns = self.hdr["n_signals"]
        p = 256
        def take(n, cnt):
            nonlocal p
            out = [_s(raw[p+i*n:p+(i+1)*n]).strip() for i in range(cnt)]
            p += n*cnt
            return out
        labels = take(16, ns)
        transducer = take(80, ns)
        phys_dim = take(8, ns)
        phys_min = [float(x) for x in take(8, ns)]
        phys_max = [float(x) for x in take(8, ns)]
        dig_min = [float(x) for x in take(8, ns)]
        dig_max = [float(x) for x in take(8, ns)]
        prefilt = take(80, ns)
        n_samp = [int(x) for x in take(8, ns)]
        take(32, ns)  # reserved per signal
        for i in range(ns):
            span_d = (dig_max[i]-dig_min[i]) or 1
            self.signals.append(dict(
                label=labels[i], dim=phys_dim[i],
                pmin=phys_min[i], pmax=phys_max[i],
                dmin=dig_min[i], dmax=dig_max[i], ns=n_samp[i],
                gain=(phys_max[i]-phys_min[i])/span_d,
                offset=phys_min[i] - dig_min[i]*((phys_max[i]-phys_min[i])/span_d),
            ))
        # data records
        self.data_raw = raw[p:]
        return self

    def signal_phys(self, idx):
        """Return list of physical values for signal idx across all records (digital->phys)."""
        sig = self.signals[idx]
        ns = sig["ns"]
        rec_samps = sum(s["ns"] for s in self.signals)
        rec_bytes = rec_samps*2
        out = []
        off0 = sum(s["ns"] for s in self.signals[:idx])
        for r in range(self.hdr["n_records"]):
            base = r*rec_bytes + off0*2
            vals = struct.unpack_from("<%dh" % ns, self.data_raw, base)
            for v in vals:
                out.append(v*sig["gain"]+sig["offset"])
        return out

    def annotations(self):
        """Parse EDF+ 'EDF Annotations' signal TALs -> list of (onset, dur, text)."""
        idx = next((i for i,s in enumerate(self.signals) if "Annotation" in s["label"]), None)
        if idx is None: return []
        ns = self.signals[idx]["ns"]
        rec_samps = sum(s["ns"] for s in self.signals)
        rec_bytes = rec_samps*2
        off0 = sum(s["ns"] for s in self.signals[:idx])*2
        anns = []
        for r in range(self.hdr["n_records"]):
            base = r*rec_bytes + off0
            chunk = self.data_raw[base:base+ns*2]
            for tal in chunk.split(b"\x00"):
                if not tal or b"\x14" not in tal: continue
                try: txt = tal.decode("latin-1")
                except: continue
                # onset[21]dur14text14
                parts = txt.split("\x14")
                head = parts[0]
                onset = head.split("\x15")[0]
                dur = head.split("\x15")[1] if "\x15" in head else ""
                for label in parts[1:]:
                    if label: anns.append((onset, dur, label))
        return anns


# --------------------------------------------------------------------------
# Writers (ResMed-flavoured EDF / EDF+)
# --------------------------------------------------------------------------
def crc_ccitt(data, crc=0xFFFF):
    """ResMed per-record CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF)."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def fld(s, n):
    """Left-justified ASCII header field padded to n bytes."""
    b = str(s).encode("latin-1")[:n]
    return b + b" " * (n - len(b))


def _mon(dt):
    return ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"][dt.month-1]


def write_eve(path, start_dt, anns, serial):
    """Write a ResMed EDF+ EVE.edf annotation file.
    anns = list of (onset_sec:int, dur_sec:int, label:str)."""
    records = [(0, 0, "Recording starts")] + list(anns)
    n_rec = len(records)
    header = (
        fld("0", 8)
        + fld("X X X X 0000 0000", 80)
        + fld(f"Startdate {start_dt.day:02d}-{_mon(start_dt)}-{start_dt.year} "
              f"X X X SRN={serial} MID=46 VID=3", 80)
        + fld(start_dt.strftime("%d.%m.%y"), 8)
        + fld(start_dt.strftime("%H.%M.%S"), 8)
        + fld("768", 8)
        + fld("EDF+D", 44)
        + fld(str(n_rec), 8)
        + fld("0.00", 8)
        + fld("2", 4)
    )
    # signal headers (2 signals: EDF Annotations[31], Crc16[1])
    sh = (
        fld("EDF Annotations", 16) + fld("Crc16", 16)
        + fld("", 80) + fld("", 80)               # transducer
        + fld("", 8) + fld("", 8)                  # phys dim
        + fld("-32768.0", 8) + fld("-32768.0", 8)  # phys min
        + fld("32767.00", 8) + fld("32767.00", 8)  # phys max
        + fld("-32768", 8) + fld("-32768", 8)      # dig min
        + fld("32767", 8) + fld("32767", 8)        # dig max
        + fld("", 80) + fld("", 80)                # prefilter
        + fld("31", 8) + fld("1", 8)               # n samples
        + fld("", 32) + fld("", 32)                # reserved
    )
    body = bytearray()
    for onset, dur, label in records:
        tal = b"+0\x14\x14\x00" + f"+{onset}\x15{dur}\x14{label}\x14\x00".encode("latin-1")
        ann = tal + b"\x00" * (62 - len(tal))
        assert len(ann) == 62, f"annotation too long: {label}"
        crc = crc_ccitt(ann)
        body += ann + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    with open(path, "wb") as f:
        f.write(header + sh + body)
