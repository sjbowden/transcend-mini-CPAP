#!/usr/bin/env python3
"""Decode a Transcend miniCPAP raw event-log dump (from collect.ps1) into therapy data.

Usage: python3 parse.py dump.txt [older-dump.txt ...]
Multiple dumps are merged (overlapping records deduplicated by queue address);
pass them oldest-first. Writes events.csv (every event) and sessions.csv
(per-therapy-session summary), and prints a summary to stdout.
"""
import sys, csv
from datetime import datetime, timezone

EVENT_TYPES = {
    1: ("StartTherapy", 0.1), 2: ("EndTherapy", 1.0),
    5: ("RampStart", 1.0), 6: ("RampEnd", 1.0),
    7: ("LeakReport", 1.0), 8: ("SupplyVoltage", 1.0),
    9: ("ApneaDetected", 1.0), 10: ("HypopneaDetected", 1.0),
    11: ("PressureReduced", 0.1), 12: ("PressureAverage", 0.1),
    13: ("MinimumPressureSetting", 0.1), 14: ("MaximumPressureSetting", 0.1),
    15: ("EZEXLevel", 0.1), 16: ("MinimumPressureUsed", 0.1),
    17: ("MaximumPressureUsed", 0.1), 18: ("FlowLimitedRatio", 0.1),
    19: ("SnoringRatio", 0.1), 20: ("MinimumLeak", 1.0),
    21: ("MaximumLeak", 1.0), 22: ("AverageLeak", 1.0),
    23: ("PressureIncreasedFromApneas", 0.1), 24: ("PressureIncreasedFromHypopneas", 0.1),
    25: ("PressureIncreasedFromCombination", 0.1), 26: ("PressureIncreasedFromSnoring", 0.1),
    27: ("PressureIncreasedFromFlowLimitedBreathing", 0.1), 28: ("PressureIncreasedFromCommand", 0.1),
}


def swap16(h4):
    """little-endian: hex chars [b0b1 b2b3] -> b2b3b0b1"""
    return h4[2:4] + h4[0:2]


def hexbits(h4):
    return bin(int(swap16(h4), 16))[2:].zfill(16)


def decode_event(rec):
    """rec = 10 hex chars (5 bytes). Returns dict or None for empty (all f)."""
    if len(rec) != 10 or rec.lower() == "f" * 10:
        return None
    w1 = hexbits(rec[0:4])
    w2 = hexbits(rec[4:8])
    year  = int(w1[0:7], 2) + 2000
    month = int(w1[7:11], 2)
    day   = int(w1[11:16], 2)
    hour  = int(w2[0:5], 2)
    minute= int(w2[5:11], 2)
    etype = int(w2[11:16], 2)
    sub   = int(rec[8:10], 16)
    name, scale = EVENT_TYPES.get(etype, (f"Other({etype})", 1.0))
    try:
        # device stores UTC; convert to local
        dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc).astimezone()
    except ValueError:
        return {"dt": None, "raw_dt": f"{year}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}",
                "type": etype, "name": name, "value": sub * scale, "sub": sub, "raw": rec}
    return {"dt": dt, "raw_dt": dt.strftime("%Y-%m-%d %H:%M"),
            "type": etype, "name": name, "value": round(sub * scale, 2), "sub": sub, "raw": rec}


def parse_header(line):
    a = line.split(None, 1)[1][3:]  # strip "HEADER Rbd"
    def asc(h):
        return bytes.fromhex(h).decode("ascii", "ignore").rstrip("\x00")
    return {
        "serial": asc(a[4:68]),
        "firmware": asc(a[68:76]),
        "events_in_queue": int(swap16(a[80:84]), 16),
        "offset": int(swap16(a[84:88]), 16),
    }


def load_events(paths):
    """Read one or more collect.ps1 dumps -> (header dict, sorted list of decoded events).

    Multiple dumps merge safely: the device queue is append-only and re-read in full on
    each pull, so overlapping dumps repeat records. Records are deduplicated by their
    absolute queue address + raw bytes — NOT raw bytes alone, since two same-minute
    events with equal subdata (e.g. two 12 s apneas) are byte-identical yet distinct.
    Timestamps are minute-resolution, so same-minute ordering relies on queue order;
    the stable sort below preserves it (pass dumps oldest-first)."""
    if isinstance(paths, str):
        paths = [paths]
    header, events, seen = {}, [], set()
    for path in paths:
        with open(path) as f:
            for lineno, line in enumerate(f):
                line = line.strip()
                if line.startswith("HEADER "):
                    header = parse_header(line)   # last dump's header wins
                elif line.startswith("BLOCK "):
                    parts = line.split()
                    comp = parts[2] if len(parts) > 2 else ""  # final block can be empty
                    try:
                        addr = int(parts[1])
                    except (IndexError, ValueError):
                        addr = (path, lineno)     # unknown address: never dedupe across files
                    for i in range(0, len(comp) - 9, 10):
                        rec = comp[i:i + 10].lower()
                        key = (addr, i, rec) if isinstance(addr, tuple) else (addr + i // 2, rec)
                        if key in seen:
                            continue
                        seen.add(key)
                        ev = decode_event(rec)
                        if ev:
                            events.append(ev)
    events = [e for e in events if e["dt"] is not None]
    events.sort(key=lambda e: e["dt"])
    return header, events


def build_sessions(events):
    """Group events into therapy sessions (StartTherapy(1)..EndTherapy(2))."""
    sessions, cur = [], None
    for e in events:
        if e["type"] == 1:  # StartTherapy
            if cur:
                sessions.append(cur)
            cur = {"start": e["dt"], "end": None, "start_pressure": e["value"], "evs": []}
        elif cur is not None:
            cur["evs"].append(e)
            if e["type"] == 2:  # EndTherapy
                cur["end"] = e["dt"]
                sessions.append(cur)
                cur = None
    if cur:
        sessions.append(cur)
    return sessions


def main():
    paths = sys.argv[1:] if len(sys.argv) > 1 else ["dump.txt"]
    header, events = load_events(paths)

    if header:
        print(f"Device serial : {header['serial']}")
        print(f"Firmware      : {header['firmware']}")
        print(f"Events in queue: {header['events_in_queue']}")
    print(f"Events decoded : {len(events)}")
    if not events:
        return
    print(f"Date range    : {events[0]['raw_dt']}  ->  {events[-1]['raw_dt']}")

    # write every event
    with open("events.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["datetime_local", "type_id", "event", "value", "raw_subdata", "raw_hex"])
        for e in events:
            w.writerow([e["raw_dt"], e["type"], e["name"], e["value"], e["sub"], e["raw"]])

    sessions = build_sessions(events)

    def pick(evs, tid):
        vals = [x["value"] for x in evs if x["type"] == tid]
        return vals[-1] if vals else ""

    rows = []
    for s in sessions:
        evs = s["evs"]
        dur_min = round((s["end"] - s["start"]).total_seconds() / 60, 1) if s["end"] else ""
        apneas = sum(1 for x in evs if x["type"] == 9)
        hypops = sum(1 for x in evs if x["type"] == 10)
        hrs = (dur_min / 60) if isinstance(dur_min, float) and dur_min > 0 else None
        ahi = round((apneas + hypops) / hrs, 1) if hrs else ""
        rows.append({
            "start": s["start"].strftime("%Y-%m-%d %H:%M"),
            "end": s["end"].strftime("%H:%M") if s["end"] else "",
            "dur_min": dur_min,
            "apneas": apneas, "hypopneas": hypops, "AHI": ahi,
            "min_pressure": pick(evs, 16), "max_pressure": pick(evs, 17),
            "avg_leak": pick(evs, 22), "max_leak": pick(evs, 21),
            "snore_ratio": pick(evs, 19),
        })

    with open("sessions.csv", "w", newline="") as f:
        cols = ["start", "end", "dur_min", "apneas", "hypopneas", "AHI",
                "min_pressure", "max_pressure", "avg_leak", "max_leak", "snore_ratio"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    total_hrs = sum(r["dur_min"] for r in rows if isinstance(r["dur_min"], float)) / 60
    print(f"\nTherapy sessions: {len(rows)}   total usage: {total_hrs:.1f} h")
    print("\nMost recent sessions:")
    print(f"  {'start':16} {'dur(min)':>8} {'apn':>4} {'hyp':>4} {'AHI':>5} {'minP':>5} {'maxP':>5} {'avgLeak':>7}")
    for r in rows[-10:]:
        print(f"  {r['start']:16} {str(r['dur_min']):>8} {r['apneas']:>4} {r['hypopneas']:>4} "
              f"{str(r['AHI']):>5} {str(r['min_pressure']):>5} {str(r['max_pressure']):>5} {str(r['avg_leak']):>7}")
    print("\nWrote events.csv and sessions.csv")


if __name__ == "__main__":
    main()
