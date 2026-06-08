#!/usr/bin/env python3
"""Transcend miniCPAP — view and (carefully) edit device settings over USB.

Reads/writes the device configuration using the same reverse-engineered serial
protocol as the data download (see PROTOCOL.md). The official app's password only
gates *prescription* settings in its own UI — the device firmware accepts config
writes with no authentication — so this tool imposes the safety boundary itself:
comfort settings (EZEX, ramp) are editable freely; prescription pressures require
an explicit --allow-prescription flag.

Examples:
    python3 settings.py --show                      # read-only: print all settings
    python3 settings.py --set-ezex 2                # comfort change (read-modify-write)
    python3 settings.py --set-ramp-time 20
    python3 settings.py --set-min 11 --set-max 14 --allow-prescription
    python3 settings.py --dry-run --set-ezex 3      # show the exact bytes, send nothing
    python3 settings.py --restore backup-XXXX.json  # roll back to a saved snapshot
    python3 settings.py --snapshot a.json           # capture config for blob mapping
    python3 settings.py --diff a.json               # diff current vs a saved snapshot

Every write auto-saves a timestamped backup first and verifies by reading back.
Calibration is never writable.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

PAP_PS1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pap.ps1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse as tparse  # noqa: E402  (reuse parse_header for the Tbd header)

# Config field layouts, mirroring the decompiled (Get|Write)Configuration* commands.
# kind: "scaled" = hex int /10 on read, *10 on write (pressures, EZEX level);
#       "int"    = hex int as-is (ramp minutes);
#       "opaque" = raw substring, passed through verbatim on write.
LAYOUT_APAP = [          # AutoPAP ('B') and CPAP+EZEX ('C'): read Tab/Rab, write Tcc/R55
    ("StartingTherapyPressure", 4, "scaled"),
    ("ConfigurationData", 15, "opaque"),
    ("MinimumTherapyPressure", 4, "scaled"),
    ("MaximumTherapyPressure", 4, "scaled"),
    ("Reserved", 5, "opaque"),
    ("RampDurationMinutes", 4, "int"),
    ("EZEX", 4, "scaled"),
    ("StartingRampPressure", 4, "scaled"),
]
LAYOUT_CPAP = [          # StandardCPAP ('A'): read Tab/Rab, write Tac/R55
    ("StartingTherapyPressure", 4, "scaled"),
    ("ConfigurationData", 28, "opaque"),
    ("RampDurationMinutes", 4, "int"),
    ("Reserved", 4, "opaque"),
    ("StartingRampPressure", 4, "scaled"),
]
WRITE_CMD = {"APAP": "Tcc", "CPAP": "Tac"}
WRITE_ACK = "R55"

COMFORT = {"EZEX", "RampDurationMinutes", "StartingRampPressure"}
PRESCRIPTION = {"MinimumTherapyPressure", "MaximumTherapyPressure", "StartingTherapyPressure"}
RANGES = {  # physical validation (ranges confirmed against the iOS app's limits)
    "MinimumTherapyPressure": (4.0, 20.0), "MaximumTherapyPressure": (4.0, 20.0),
    "StartingTherapyPressure": (4.0, 20.0), "StartingRampPressure": (4.0, 10.0),
    "RampDurationMinutes": (0, 45), "EZEX": (0, 3),
}
# The iOS app's user-facing names for the settings it exposes (shown alongside in --show).
APP_NAMES = {
    "EZEX": "AirRelief", "StartingRampPressure": "GentleRise Pressure",
    "RampDurationMinutes": "GentleRise Duration",
}


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def pap(commands, port):
    """Run PAP commands via pap.ps1; return one response string per command.

    One process (one port open/close) per command — the device handles a single
    command per connection most reliably, mirroring the official app's ProcessCommand.
    """
    win = PAP_PS1
    if shutil.which("wslpath"):
        win = subprocess.check_output(["wslpath", "-w", PAP_PS1], text=True).strip()
    responses = []
    for c in commands:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", win,
             "-Port", port, "-Command", c],
            capture_output=True, text=True, timeout=60,
        )
        out = [ln.rstrip("\r") for ln in proc.stdout.splitlines() if ln.strip()]
        if not out:
            sys.exit(f"Transport error: no response to {c!r}\n{proc.stderr.strip()}")
        responses.append(out[0])
    return responses


# ---------------------------------------------------------------------------
# Encode / decode
# ---------------------------------------------------------------------------
def decode_value(kind, hexstr):
    if kind == "opaque":
        return hexstr
    iv = int(hexstr, 16)
    return iv / 10.0 if kind == "scaled" else iv


def encode_value(kind, value, length):
    if kind == "opaque":
        return value
    iv = int(round(value * 10)) if kind == "scaled" else int(round(value))
    return format(iv, "X").rjust(length, "0")


def read_usage(port):
    """Device usage counters: Tbc (blower runtime) and Tb8 (patient therapy time).

    Both reply with plain comma-separated decimals (no hex), per the decompiled
    GetBlowerTimeCommand / GetPatientHoursCommand field definitions:
      Tbc -> Rbc<hours>,<minutes>,<seconds>                       (blower on-time)
      Tb8 -> Rb8<h>,<m>,<s>,<#sessions>=8h>,<#6-8h>,<#4-6h>       (patient therapy)
    The app reports the *blower* figure as "usage"; patient time is shorter (it
    excludes ramp / mask-off / blower-on-but-not-breathing).
    """
    bc, b8 = pap(["Tbc", "Tb8"], port)
    out = {}
    if bc.startswith("Rbc"):
        v = [int(float(x)) for x in bc[3:].split(",") if x != ""]
        if len(v) >= 3:
            out["blower"] = tuple(v[:3])
    if b8.startswith("Rb8"):
        v = [int(float(x)) for x in b8[3:].split(",") if x != ""]
        if len(v) >= 6:
            out["patient"], out["sessions"] = tuple(v[:3]), tuple(v[3:6])
    return out


def print_usage(port):
    u = read_usage(port)
    if not u:
        return
    print("Usage:")
    if "blower" in u:
        h, m, s = u["blower"]
        print(f"  Blower runtime         = {h}h {m:02d}m {s:02d}s   (what the app shows as usage)")
    if "patient" in u:
        h, m, s = u["patient"]
        print(f"  Patient therapy time   = {h}h {m:02d}m {s:02d}s")
    if "sessions" in u:
        a, b, c = u["sessions"]
        print(f"  Session histogram      = >=8h: {a}   6-8h: {b}   4-6h: {c}")


def read_config(port):
    """Return dict: serial, device_type, layout key, fields{name:value}, raw args."""
    tbd, tab = pap(["Tbd", "Tab"], port)
    if not tbd.startswith("Rbd"):
        sys.exit(f"Unexpected header response: {tbd!r}")
    hdr = tparse.parse_header("HEADER " + tbd)
    serial = hdr["serial"]
    dtype = {"A": "CPAP", "B": "APAP", "C": "APAP"}.get(serial[:1], "APAP")
    layout = LAYOUT_APAP if dtype == "APAP" else LAYOUT_CPAP
    if not tab.startswith("Rab"):
        sys.exit(f"Unexpected config response: {tab!r}")
    args = tab[3:]
    need = sum(n for _, n, _ in layout)
    if len(args) < need:
        sys.exit(f"Config response too short ({len(args)} < {need}): {tab!r}")
    fields, raw, pos = {}, {}, 0
    for name, n, kind in layout:
        chunk = args[pos:pos + n]
        fields[name] = decode_value(kind, chunk)
        raw[name] = chunk
        pos += n
    return {"serial": serial, "device_type": dtype, "layout": layout,
            "fields": fields, "raw": raw, "response": tab, "firmware": hdr.get("firmware")}


def build_write(cfg):
    """Build the write command string (e.g. 'Tcc<args>') from cfg['fields']/raw."""
    parts = []
    for name, n, kind in cfg["layout"]:
        if kind == "opaque":
            parts.append(cfg["raw"][name])              # passthrough verbatim
        else:
            parts.append(encode_value(kind, cfg["fields"][name], n))
    return WRITE_CMD[cfg["device_type"]] + "".join(parts)


# ---------------------------------------------------------------------------
# Display / snapshot
# ---------------------------------------------------------------------------
def fmt(name, value):
    if name in ("RampDurationMinutes",):
        return f"{int(value)} min"
    if name == "EZEX":
        return f"level {int(round(value))}"
    if name in ("ConfigurationData", "Reserved"):
        return value + f"  (hex, {len(value)} chars — opaque)"
    return f"{value:.1f} cmH2O"


def print_config(cfg):
    print(f"Device serial : {cfg['serial']}  (type {cfg['device_type']}, fw-checksum {cfg['firmware']})")
    print("Settings:")
    for name, _, _ in cfg["layout"]:
        tag = "(prescription)" if name in PRESCRIPTION else ("(comfort)" if name in COMFORT else "")
        app = f"[{APP_NAMES[name]}] " if name in APP_NAMES else ""
        print(f"  {name:24} = {fmt(name, cfg['fields'][name]):28} {app}{tag}")


def snapshot_dict(cfg):
    return {"timestamp": datetime.now().isoformat(timespec="seconds"),
            "serial": cfg["serial"], "device_type": cfg["device_type"],
            "fields": cfg["fields"], "raw": cfg["raw"], "response": cfg["response"]}


def save_json(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
    return path


# ---------------------------------------------------------------------------
# Write (read-modify-write + verify)
# ---------------------------------------------------------------------------
def apply_and_write(cfg, changes, args):
    """changes: {field: new_value}. Validate, confirm, backup, write, verify."""
    # validate ranges + prescription gate
    for name, val in changes.items():
        lo, hi = RANGES[name]
        if not (lo <= val <= hi):
            sys.exit(f"Refusing: {name}={val} out of allowed range [{lo}, {hi}].")
        if name in PRESCRIPTION and not args.allow_prescription:
            sys.exit(f"{name} is a PRESCRIPTION setting. Re-run with --allow-prescription "
                     "to change it (and verify the value with your clinician).")

    new = dict(cfg["fields"])
    new.update(changes)
    # cross-field sanity for APAP
    if cfg["device_type"] == "APAP":
        if new["MinimumTherapyPressure"] > new["MaximumTherapyPressure"]:
            sys.exit("Refusing: min pressure > max pressure.")
        if not (new["MinimumTherapyPressure"] <= new["StartingTherapyPressure"] <= new["MaximumTherapyPressure"]):
            sys.exit("Refusing: starting pressure must be between min and max.")

    if any(n in PRESCRIPTION for n in changes):
        print("WARNING: changing a PRESCRIPTION setting — these are clinician-set therapy "
              "values. Verify with your provider.\n")

    print("Pending changes:")
    for name in changes:
        print(f"  {name:24} {fmt(name, cfg['fields'][name])}  ->  {fmt(name, new[name])}")

    new_cfg = dict(cfg, fields=new)
    cmd = build_write(new_cfg)
    print(f"\nWrite command: {cmd}   (expect ack {WRITE_ACK})")

    if args.dry_run:
        print("[dry-run] nothing sent.")
        return
    if not args.yes:
        if input("Proceed? type 'yes': ").strip().lower() != "yes":
            sys.exit("Aborted.")

    backup = save_json(snapshot_dict(cfg), f"settings-backup-{datetime.now():%Y%m%d-%H%M%S}.json")
    print(f"Backed up current settings to {backup}")

    resp = pap([cmd], args.port)[0]
    if not resp.startswith(WRITE_ACK):
        sys.exit(f"Write FAILED — device replied {resp!r} (expected {WRITE_ACK}). "
                 f"No change should have taken; current settings are saved in {backup}.")
    print(f"Device acknowledged ({resp}). Verifying...")

    after = read_config(args.port)
    ok = all(abs(after["fields"][n] - new[n]) < 1e-6 if isinstance(new[n], float)
             else after["fields"][n] == new[n] for n in changes)
    unchanged = all(after["raw"][n] == cfg["raw"][n] for n, _, k in cfg["layout"] if k == "opaque")
    if ok and unchanged:
        print("Verified: settings updated and opaque data preserved.")
    else:
        print(f"WARNING: read-back did not match. Inspect with --show; restore with "
              f"--restore {backup} if needed.")
        print_config(after)


def restore(path, args):
    saved = json.loads(open(path).read())
    cur = read_config(args.port)
    if saved["serial"] != cur["serial"]:
        sys.exit(f"Refusing: backup serial {saved['serial']} != device {cur['serial']}.")
    cfg = dict(cur, fields=saved["fields"], raw=saved["raw"])
    cmd = build_write(cfg)
    print(f"Restoring settings from {path}:")
    print_config(cfg)
    print(f"\nWrite command: {cmd}")
    if args.dry_run:
        print("[dry-run] nothing sent."); return
    if not args.yes and input("Proceed? type 'yes': ").strip().lower() != "yes":
        sys.exit("Aborted.")
    resp = pap([cmd], args.port)[0]
    print("OK" if resp.startswith(WRITE_ACK) else f"FAILED: {resp!r}")


def diff_blob(path, args):
    saved = json.loads(open(path).read())
    cur = read_config(args.port)
    print(f"Diff vs {path} (saved {saved.get('timestamp')}):")
    changed = False
    for name in cur["fields"]:
        a, b = saved["raw"].get(name), cur["raw"][name]
        if a != b:
            changed = True
            mark = ""
            if name in ("ConfigurationData", "Reserved") and a and len(a) == len(b):
                bits = [i for i in range(len(a)) if a[i] != b[i]]
                mark = f"  (differs at hex positions {bits})"
            print(f"  {name:24} {a}  ->  {b}{mark}")
    if not changed:
        print("  (identical)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="COM3")
    ap.add_argument("--show", action="store_true", help="print current settings (read-only)")
    ap.add_argument("--set-ezex", type=int, metavar="0-3")
    ap.add_argument("--set-ramp-time", type=int, metavar="MIN")
    ap.add_argument("--set-ramp-pressure", type=float, metavar="CMH2O")
    ap.add_argument("--set-min", type=float, metavar="CMH2O")
    ap.add_argument("--set-max", type=float, metavar="CMH2O")
    ap.add_argument("--set-start", type=float, metavar="CMH2O")
    ap.add_argument("--allow-prescription", action="store_true",
                    help="permit changing min/max/start pressure (clinician settings)")
    ap.add_argument("--dry-run", action="store_true", help="show the exact bytes, send nothing")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--snapshot", metavar="FILE", help="save current config to FILE")
    ap.add_argument("--diff", metavar="FILE", help="diff current config against a saved snapshot")
    ap.add_argument("--restore", metavar="FILE", help="re-write a saved snapshot to the device")
    args = ap.parse_args()

    if args.restore:
        return restore(args.restore, args)
    if args.diff:
        return diff_blob(args.diff, args)

    changes = {}
    for opt, field in [("set_ezex", "EZEX"), ("set_ramp_time", "RampDurationMinutes"),
                       ("set_ramp_pressure", "StartingRampPressure"),
                       ("set_min", "MinimumTherapyPressure"), ("set_max", "MaximumTherapyPressure"),
                       ("set_start", "StartingTherapyPressure")]:
        v = getattr(args, opt)
        if v is not None:
            changes[field] = v

    cfg = read_config(args.port)

    if args.snapshot:
        print(f"Saved snapshot to {save_json(snapshot_dict(cfg), args.snapshot)}")
    if changes:
        apply_and_write(cfg, changes, args)
    elif args.show or not (args.snapshot):
        print_config(cfg)
        print()
        print_usage(args.port)


if __name__ == "__main__":
    main()
