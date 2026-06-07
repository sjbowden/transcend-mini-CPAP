#!/usr/bin/env python3
"""Convert a Transcend miniCPAP event-log dump into a ResMed-format SD-card tree
that SleepHQ's ResMed parser can ingest.

Produces under --out:
    Identification.json
    STR.edf                         (one record per day: usage, pressure, AHI, leak)
    DATALOG/YYYYMMDD/<ts>_EVE.edf    (apnea/hypopnea event flags per session)

Usage: python3 convert.py ../dump.txt --out out
Notes / approximations (Transcend gives summary+events, not waveforms):
  * Appears in SleepHQ as a ResMed device (uses the Transcend's own serial) - no flow-rate graph.
  * All apneas mapped to "Obstructive Apnea" (Transcend doesn't classify obs/central).
  * Leak assumed L/min on the Transcend side -> converted to L/s for ResMed.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parse as tparse  # noqa: E402
import edf as edflib    # noqa: E402

DEFAULT_SERIAL = "TRANSCEND0"   # placeholder; real serial comes from the dump header or --serial
TEMPLATE = os.path.expanduser("~/cpap/data/STR.edf")
EPOCH = datetime(1970, 1, 1)

# Transcend event type ids
T_APNEA, T_HYPOP = 9, 10
T_PMIN_USED, T_PMAX_USED = 16, 17
T_PMIN_SET, T_PMAX_SET = 13, 14
T_PAVG = 12
T_LEAK_AVG, T_LEAK_MAX = 22, 21


def session_metrics(s):
    """Derive per-session summary + event list from a build_sessions() session."""
    evs = s["evs"]
    def vals(tid): return [e["value"] for e in evs if e["type"] == tid]
    dur_min = (s["end"] - s["start"]).total_seconds() / 60 if s["end"] else 0.0
    leak_avg = vals(T_LEAK_AVG)
    pmin_used = vals(T_PMIN_USED) or [s["start_pressure"]]
    pmax_used = vals(T_PMAX_USED) or [s["start_pressure"]]
    pavg = vals(T_PAVG)
    # event subdata = duration in seconds; ResMed EVE annotations allow 0-second durations
    apnea_evs = [(e["dt"], max(0, int(round(e["value"]))), "Obstructive Apnea") for e in evs if e["type"] == T_APNEA]
    hypop_evs = [(e["dt"], max(0, int(round(e["value"]))), "Hypopnea") for e in evs if e["type"] == T_HYPOP]
    return {
        "start": s["start"], "end": s["end"], "dur_min": dur_min,
        "apneas": len(apnea_evs), "hypopneas": len(hypop_evs),
        "pmin_used": min(pmin_used), "pmax_used": max(pmax_used),
        "pavg": mean(pavg) if pavg else (min(pmin_used) + max(pmax_used)) / 2,
        "pmin_set": (vals(T_PMIN_SET) or [min(pmin_used)])[-1],
        "pmax_set": (vals(T_PMAX_SET) or [max(pmax_used)])[-1],
        "leak_avg": mean(leak_avg) if leak_avg else 0.0,
        "leak_max": max(vals(T_LEAK_MAX) or leak_avg or [0.0]),
        "events": sorted(apnea_evs + hypop_evs),
    }


def resmed_day(dt):
    """ResMed noon-to-noon session day for a start datetime."""
    return (dt - timedelta(hours=12)).date()


def build_str(days_sorted, out_path, serial):
    """Write STR.edf by cloning the template's last record and overriding fields."""
    import struct
    tmpl = edflib.Edf(TEMPLATE)              # validated reader (gain/offset, correct field order)
    raw = tmpl.raw                           # reuse the bytes Edf already read (no second read)
    nsig = tmpl.hdr["n_signals"]
    hdr_len = tmpl.hdr["hdr_bytes"]
    head, sighdr = raw[:256], raw[256:hdr_len]
    # signal table: label -> (offset_in_samples, ns, gain, offset, index)
    sample_off, acc = {}, 0
    for i, s in enumerate(tmpl.signals):
        sample_off[s["label"]] = (acc, s["ns"], s["gain"], s["offset"], i)
        acc += s["ns"]
    rec_samps = acc
    rec_bytes = rec_samps * 2
    donor = list(struct.unpack_from(
        "<%dh" % rec_samps, tmpl.data_raw, (tmpl.hdr["n_records"] - 1) * rec_bytes))

    def enc(label, phys):
        off, ns, gain, offset, _ = sample_off[label]
        return int(round((phys - offset) / gain))

    ZERO = ["Flow.95", "Flow.5", "BlowFlow.50", "AmbHumidity.50", "HumTemp.50",
            "HTubeTemp.50", "HTubePow.50", "HumPow.50", "MinVent.50", "MinVent.95",
            "MinVent.Max", "RespRate.50", "RespRate.95", "RespRate.Max",
            "TidVol.50", "TidVol.95", "TidVol.Max", "CSR", "RIN", "CAI", "UAI"]
    OFF = ["S.EPR.EPREnable", "S.EPR.ClinEnable", "S.EPR.Level", "S.HumEnable",
           "S.ClimateControl", "S.TempEnable", "HeatedTube", "Humidifier"]
    SPO2 = ["SpO2.50", "SpO2.95", "SpO2.Max", "SpO2Thresh"]

    records = bytearray()
    warnings = []
    first_day = None
    for day, sessions in days_sorted:
        if first_day is None:
            first_day = day
        rec = list(donor)
        def clamp(label, val, lo=-32768, hi=32767):
            c = max(lo, min(hi, val))
            if c != val:
                warnings.append(f"{day}: {label} {val} out of range -> clamped to {c}")
            return c
        def setv(label, phys):
            off = sample_off[label][0]
            rec[off] = clamp(label, enc(label, phys))
        # naive local wall-clock noon: MaskOn/MaskOff are wall-clock minutes after noon,
        # so working in naive local time is correct and immune to DST offset changes.
        noon = datetime(day.year, day.month, day.day, 12)
        total_dur = sum(m["dur_min"] for m in sessions)
        apneas = sum(m["apneas"] for m in sessions)
        hypops = sum(m["hypopneas"] for m in sessions)
        hrs = total_dur / 60 if total_dur > 0 else None
        ai = apneas / hrs if hrs else 0
        hi = hypops / hrs if hrs else 0

        setv("Date", (datetime(day.year, day.month, day.day) - EPOCH).days)
        setv("Duration", round(total_dur))
        setv("MaskEvents", len(sessions))
        # MaskOn/MaskOff arrays (minutes after noon), pad -1
        mon_off = sample_off["MaskOn"][0]
        moff_off = sample_off["MaskOff"][0]
        for k in range(20):
            rec[mon_off + k] = -1
            rec[moff_off + k] = -1
        if len(sessions) > 20:
            warnings.append(f"{day}: {len(sessions)} sessions, only first 20 fit ResMed MaskOn slots")
        for k, m in enumerate(sessions[:20]):
            start_wall = m["start"].replace(tzinfo=None)
            end_wall = (m["end"] or m["start"]).replace(tzinfo=None)
            rec[mon_off + k] = clamp("MaskOn", round((start_wall - noon).total_seconds() / 60), 0, 1440)
            rec[moff_off + k] = clamp("MaskOff", round((end_wall - noon).total_seconds() / 60), 0, 1440)

        pmin_used = min(m["pmin_used"] for m in sessions)
        pmax_used = max(m["pmax_used"] for m in sessions)
        pavg = mean([m["pavg"] for m in sessions])
        setv("S.A.MinPress", min(m["pmin_set"] for m in sessions))
        setv("S.A.MaxPress", max(m["pmax_set"] for m in sessions))
        for lbl, v in [("BlowPress.95", pmax_used), ("BlowPress.5", pmin_used),
                       ("MaskPress.50", pavg), ("MaskPress.95", pmax_used), ("MaskPress.Max", pmax_used),
                       ("TgtIPAP.50", pavg), ("TgtIPAP.95", pmax_used), ("TgtIPAP.Max", pmax_used),
                       ("TgtEPAP.50", pavg), ("TgtEPAP.95", pmax_used), ("TgtEPAP.Max", pmax_used)]:
            setv(lbl, v)

        leak_avg = mean([m["leak_avg"] for m in sessions]) / 60.0   # L/min -> L/s
        leak_max = max(m["leak_max"] for m in sessions) / 60.0
        setv("Leak.50", leak_avg); setv("Leak.70", leak_avg)
        setv("Leak.95", leak_max); setv("Leak.Max", leak_max)

        setv("AHI", ai + hi); setv("AI", ai); setv("HI", hi); setv("OAI", ai)
        for lbl in ZERO + OFF:
            rec[sample_off[lbl][0]] = 0
        for lbl in SPO2:
            rec[sample_off[lbl][0]] = -1

        body = struct.pack("<%dh" % (rec_samps - 1), *rec[:-1])
        crc = edflib.crc_ccitt(body)
        records += body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])

    # rewrite header start date/time, n_records, recording serial
    sd = datetime(first_day.year, first_day.month, first_day.day, 12)
    head = bytearray(head)
    head[88:168] = edflib.fld(f"Startdate {sd.day:02d}-{edflib._mon(sd)}-{sd.year} "
                              f"X X X SRN={serial} MID=46 VID=3", 80)
    head[168:176] = edflib.fld(sd.strftime("%d.%m.%y"), 8)
    head[176:184] = edflib.fld("12.00.00", 8)
    head[236:244] = edflib.fld(str(len(days_sorted)), 8)
    with open(out_path, "wb") as f:
        f.write(bytes(head) + sighdr + records)
    return warnings


def write_identification(path, serial):
    # Match the MID=46 VID=3 platform stamped in the STR/EVE recording fields
    # (AirSense 11 AutoSet). The serial distinguishes it from any real AS11.
    obj = {"FlowGenerator": {"IdentificationProfiles": {"Product": {
        "SerialNumber": serial, "ProductCode": "39517",
        "ProductName": "AirSense11AutoSet", "ProductGeographicIdentifier": "USA"}}}}
    with open(path, "w") as f:
        json.dump(obj, f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dump", help="Transcend dump.txt from collect.ps1")
    ap.add_argument("--out", default="out")
    ap.add_argument("--min-minutes", type=float, default=5.0,
                    help="drop sessions shorter than this (default 5; excludes factory/QA blips)")
    ap.add_argument("--since", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                    help="only include sessions on/after this date (YYYY-MM-DD)")
    ap.add_argument("--serial", default=None,
                    help="device serial for the ResMed files (default: from dump header)")
    args = ap.parse_args()

    if not os.path.exists(TEMPLATE):
        sys.exit(f"Template STR.edf not found at {TEMPLATE}.\n"
                 "This converter clones a real ResMed STR.edf as its format template. "
                 "Point TEMPLATE at one, or place a ResMed STR.edf there.")

    header, events = tparse.load_events(args.dump)
    serial = args.serial or header.get("serial") or DEFAULT_SERIAL
    sessions = [session_metrics(s) for s in tparse.build_sessions(events)]
    sessions = [m for m in sessions if m["end"] and m["dur_min"] >= args.min_minutes]
    if args.since:
        sessions = [m for m in sessions if resmed_day(m["start"]) >= args.since]
    if not sessions:
        sys.exit("No sessions left after filtering (try --min-minutes 0 or an earlier --since).")

    # group by ResMed day
    by_day = {}
    for m in sessions:
        by_day.setdefault(resmed_day(m["start"]), []).append(m)
    days_sorted = sorted(by_day.items())

    os.makedirs(args.out, exist_ok=True)
    write_identification(os.path.join(args.out, "Identification.json"), serial)
    warnings = build_str(days_sorted, os.path.join(args.out, "STR.edf"), serial)

    # per-session EVE files
    n_eve = 0
    for day, daysessions in days_sorted:
        folder = os.path.join(args.out, "DATALOG", day.strftime("%Y%m%d"))
        os.makedirs(folder, exist_ok=True)
        for m in daysessions:
            start = m["start"]
            anns = [(int((dt - start).total_seconds()), dur, label) for dt, dur, label in m["events"]]
            anns = [(o, d, l) for o, d, l in anns if o >= 0]
            fn = f"{start.strftime('%Y%m%d_%H%M%S')}_EVE.edf"
            edflib.write_eve(os.path.join(folder, fn), start, anns, serial)
            n_eve += 1

    print(f"Device serial : {serial}  (written as ResMed SRN={serial})")
    print(f"Sessions      : {len(sessions)} over {len(days_sorted)} days")
    print(f"Wrote         : STR.edf ({len(days_sorted)} day-records), {n_eve} EVE files, Identification.json")
    print(f"Output dir    : {os.path.abspath(args.out)}")
    if warnings:
        print(f"\n{len(warnings)} value warning(s) (possible unit/scale issue):")
        for w in warnings[:10]:
            print("  " + w)


if __name__ == "__main__":
    main()
