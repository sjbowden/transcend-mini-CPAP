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
  * Leak is L/min on the Transcend side -> converted to L/s for ResMed. Scale VALIDATED
    against the official app (6/6 night: our mean 6.5-7.0 LPM vs the app's 6.96 LPM).
    Note ResMed "leak" = unintentional/excess leak, which is the convention this matches.
  * Leak is a ~5-minute AVERAGE (one event per ~5 min), not 2 s like ResMed, so it can
    never show instantaneous spikes — it's a slowly drifting envelope. We linearly
    interpolate between the 5-min points (see interp()) so it reads as a slope, not a
    staircase; this adds no real resolution.
"""
import argparse
import bisect
import json
import os
import sys
from datetime import datetime, timedelta
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parse as tparse  # noqa: E402
import edf as edflib    # noqa: E402

DEFAULT_SERIAL = "TRANSCEND0"   # placeholder; real serial comes from the dump header or --serial
# Bundled, PHI-stripped ResMed EDF header templates (signal definitions only) -> self-contained.
_TPL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
TEMPLATE = os.path.join(_TPL, "STR.edf")
BRP_TEMPLATE = os.path.join(_TPL, "BRP.edf")
PLD_TEMPLATE = os.path.join(_TPL, "PLD.edf")

# Synthetic STR baseline (per-signal physical values for the few "settings" signals we keep;
# everything else defaults to 0). Replaces cloning a real ResMed record as a donor.
STR_BASELINE = {
    "Mode": 1,                  # AutoSet / APAP
    "S.C.StartPress": 4.0, "S.C.Press": 4.0, "S.A.StartPress": 4.0,
    "S.AFH.StartPress": 4.0, "S.AFH.MaxPress": 20.0, "S.AFH.MinPress": 4.0,
    "S.PtAccess": 1, "S.ABFilter": 1, "S.Mask": 2, "S.Tube": 2,
}
EPOCH = datetime(1970, 1, 1)

# Transcend event type ids
T_APNEA, T_HYPOP = 9, 10
T_PMIN_USED, T_PMAX_USED = 16, 17
T_PMIN_SET, T_PMAX_SET = 13, 14
T_EZEX = 15
T_PAVG = 12
T_LEAK_AVG, T_LEAK_MAX = 22, 21
T_SNORE, T_FLOWLIM = 19, 18
T_PRESS_CHANGE = {11, 23, 24, 25, 26, 27, 28}  # PressureReduced + PressureIncreasedFrom*


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
        "ezex": (vals(T_EZEX) or [0.0])[-1],      # AirRelief/EZEX level 0-3 -> ResMed EPR
        "leak_avg": mean(leak_avg) if leak_avg else 0.0,
        "leak_max": max(vals(T_LEAK_MAX) or leak_avg or [0.0]),
        "events": sorted(apnea_evs + hypop_evs),
        # Snore/flow-limit are ONE end-of-session summary ratio each (not a time series) —
        # see PROTOCOL.md event phases. Take the session's value (0 if none logged).
        "snore": (vals(T_SNORE) or [0.0])[-1],
        "flowlim": (vals(T_FLOWLIM) or [0.0])[-1],
        # raw periodic sample lists, for app-style nearest-rank percentiles in build_str
        "pavg_samples": pavg,                 # PressureAverage (cmH2O), ~5-min cadence
        "leak_samples": leak_avg,             # AverageLeak (L/min), ~5-min cadence
        # time series of (datetime, physical value) for the detail-graph channels
        "pressure_pts": sorted([(s["start"], s["start_pressure"])]
                               + [(e["dt"], e["value"]) for e in evs if e["type"] in T_PRESS_CHANGE]),
        "leak_pts": sorted((e["dt"], e["value"] / 60.0) for e in evs if e["type"] == T_LEAK_AVG),
    }


def resmed_day(dt):
    """ResMed noon-to-noon session day for a start datetime.
    Equivalent to the app's GetSessionDate with cutoffHour=12 (see PROTOCOL.md)."""
    return (dt - timedelta(hours=12)).date()


def pctile(samples, p):
    """Nearest-rank percentile matching the official app's desktop method:
    sorted[round(p*n) - 1] (round-half-up), clamped. Returns None if no samples."""
    s = sorted(samples)
    if not s:
        return None
    k = max(1, min(len(s), int(p * len(s) + 0.5)))
    return s[k - 1]


def stepper(points, start, default):
    """Build f(t_sec)->value, a step function holding each (datetime,value) until the next.
    Returns the scalar `default` if there are no points (write_signal_edf fills it flat)."""
    pts = sorted((max(0.0, (dt - start).total_seconds()), v) for dt, v in points
                 if (dt - start).total_seconds() >= -60)
    if not pts:
        return default
    ts = [t for t, _ in pts]
    vs = [v for _, v in pts]

    def f(t):
        i = bisect.bisect_right(ts, t) - 1
        return vs[i] if i >= 0 else default   # hold the default until the first event
    return f


def interp(points, start, default):
    """Like stepper() but linearly *interpolates* between points instead of holding.

    Used for the leak channel: the device logs one AverageLeak every ~5 min, so a flat
    staircase reads like an artificial ramp; sloped lines between the 5-min points look
    truer to a slowly drifting average. (No new resolution — the data is still 5-min.)
    Holds flat before the first point and after the last."""
    pts = sorted((max(0.0, (dt - start).total_seconds()), v) for dt, v in points
                 if (dt - start).total_seconds() >= -60)
    if not pts:
        return default
    ts = [t for t, _ in pts]
    vs = [v for _, v in pts]

    def f(t):
        if t <= ts[0]:
            return vs[0]
        if t >= ts[-1]:
            return vs[-1]
        i = bisect.bisect_right(ts, t) - 1
        span = ts[i + 1] - ts[i]
        if span <= 0:
            return vs[i]
        frac = (t - ts[i]) / span
        return vs[i] + frac * (vs[i + 1] - vs[i])
    return f


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

    def enc(label, phys):
        off, ns, gain, offset, _ = sample_off[label]
        return int(round((phys - offset) / gain))

    # synthesize the baseline record (0s + a few constant settings); no real donor needed
    donor = [0] * rec_samps
    for lbl, phys in STR_BASELINE.items():
        donor[sample_off[lbl][0]] = enc(lbl, phys)

    ZERO = ["Flow.95", "Flow.5", "BlowFlow.50", "AmbHumidity.50", "HumTemp.50",
            "HTubeTemp.50", "HTubePow.50", "HumPow.50", "MinVent.50", "MinVent.95",
            "MinVent.Max", "RespRate.50", "RespRate.95", "RespRate.Max",
            "TidVol.50", "TidVol.95", "TidVol.Max", "CSR", "RIN", "CAI", "UAI"]
    OFF = ["S.HumEnable", "S.ClimateControl", "S.TempEnable", "HeatedTube", "Humidifier"]
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
        setv("S.A.MinPress", min(m["pmin_set"] for m in sessions))
        setv("S.A.MaxPress", max(m["pmax_set"] for m in sessions))

        # Pool the day's periodic samples; use app-style nearest-rank percentiles
        # (PROTOCOL.md "How the official app computes its numbers"), falling back to the
        # used-range proxies for short sessions that logged no periodic samples.
        pres_samples = [v for m in sessions for v in m["pavg_samples"]]   # cmH2O
        leak_samples = [v for m in sessions for v in m["leak_samples"]]   # L/min

        def pp(p, fallback):
            v = pctile(pres_samples, p)
            return v if v is not None else fallback
        p50 = pp(0.50, mean([m["pavg"] for m in sessions]))
        p95 = pp(0.95, pmax_used)
        p05 = pp(0.05, pmin_used)
        pmax_p = max(pres_samples) if pres_samples else pmax_used
        for lbl, v in [("BlowPress.95", p95), ("BlowPress.5", p05),
                       ("MaskPress.50", p50), ("MaskPress.95", p95), ("MaskPress.Max", pmax_p),
                       ("TgtIPAP.50", p50), ("TgtIPAP.95", p95), ("TgtIPAP.Max", pmax_p),
                       ("TgtEPAP.50", p50), ("TgtEPAP.95", p95), ("TgtEPAP.Max", pmax_p)]:
            setv(lbl, v)

        def lk(p, fallback):            # leak percentile, L/min -> L/s
            v = pctile(leak_samples, p)
            return (v if v is not None else fallback) / 60.0
        leak_avg_fb = mean([m["leak_avg"] for m in sessions])
        leak_max_fb = max(m["leak_max"] for m in sessions)
        setv("Leak.50", lk(0.50, leak_avg_fb))
        setv("Leak.70", lk(0.70, leak_avg_fb))
        setv("Leak.95", lk(0.95, leak_max_fb))
        setv("Leak.Max", (max(leak_samples) if leak_samples else leak_max_fb) / 60.0)

        # EZEX/AirRelief -> ResMed EPR (exhale pressure relief). The Transcend's EZEX is the
        # analogue of EPR; map the level (0-3) so SleepHQ shows relief when it's enabled.
        ezex = max(int(round(m["ezex"])) for m in sessions)
        if ezex > 0:
            setv("S.EPR.EPREnable", 1); setv("S.EPR.ClinEnable", 1); setv("S.EPR.Level", ezex)
        else:
            for lbl in ("S.EPR.EPREnable", "S.EPR.ClinEnable", "S.EPR.Level"):
                rec[sample_off[lbl][0]] = 0

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

    for label, tpl in [("STR.edf", TEMPLATE), ("BRP.edf", BRP_TEMPLATE), ("PLD.edf", PLD_TEMPLATE)]:
        if not tpl or not os.path.exists(tpl):
            sys.exit(f"Bundled template {label} missing at {tpl} (should ship in sleephq/templates/).")

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
            ts = start.strftime("%Y%m%d_%H%M%S")
            dur_sec = int(m["dur_min"] * 60)
            leak_lps = m["leak_avg"] / 60.0          # Transcend L/min -> ResMed L/s
            # events
            anns = [(int((dt - start).total_seconds()), dur, label) for dt, dur, label in m["events"]]
            anns = [(o, d, l) for o, d, l in anns if o >= 0]
            edflib.write_eve(os.path.join(folder, f"{ts}_EVE.edf"), start, anns, serial)
            # CSL: annotation file with just "Recording starts"
            edflib.write_eve(os.path.join(folder, f"{ts}_CSL.edf"), start, [], serial)
            # time-varying channels from the event log (step functions over the night)
            press_f = stepper(m["pressure_pts"], start, m["pavg"])
            leak_f = interp(m["leak_pts"], start, leak_lps)   # 5-min average -> sloped, not staircase
            # snore/flow-limit are a single whole-night ratio (logged at session end), not a
            # time series -> render as a flat line at that value rather than a spurious end spike.
            snore_f = m["snore"]
            flow_f = m["flowlim"]
            # BRP: flow not recorded by Transcend (->0); pressure follows the APAP curve
            edflib.write_signal_edf(os.path.join(folder, f"{ts}_BRP.edf"), BRP_TEMPLATE,
                                    start, serial, dur_sec, {"Press.40ms": press_f})
            # PLD: pressure/leak/snore/flow-limit time series; respiratory channels we lack -> 0
            edflib.write_signal_edf(os.path.join(folder, f"{ts}_PLD.edf"), PLD_TEMPLATE,
                                    start, serial, dur_sec,
                                    {"MaskPress.2s": press_f, "Press.2s": press_f,
                                     "Leak.2s": leak_f, "Snore.2s": snore_f, "FlowLim.2s": flow_f})
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
