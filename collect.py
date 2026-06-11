#!/usr/bin/env python3
"""Transcend Micro raw event-log collector — pure-Python port of collect.ps1.

Writes the exact same dump.txt format (HEADER / DEVICE / ADDR / BLOCK lines),
so parse.py and sleephq/convert.py consume it unchanged.

Usage:
    python3 collect.py --port COM3 --out dump.txt
    python3 collect.py --port /dev/ttyUSB0          # usbipd-attached under WSL
    python3 collect.py --transport powershell       # force the pap.ps1 bridge

The whole download runs over ONE open port session (like the official client
and collect.ps1). Backend selection is automatic — see transport.py.
"""
import argparse
import sys

from transport import make_transport, TransportError

READ_SIZE = 1000                 # bytes per Ta9 block read
RECORDS_PER_FULL_BLOCK = READ_SIZE // 5
MAX_BLOCKS = 200                 # runaway guard, same as collect.ps1


def collect(port, out_path, transport="auto", log=print):
    """Download the event log from `port` into `out_path`. Returns the number
    of BLOCK lines written. Raises TransportError on device-level failure."""
    lines = []
    with make_transport(port, transport) as t:
        lines.append("HEADER " + t.command("Tbd", timeout=8))
        lines.append("DEVICE " + t.command("Tff", timeout=8))

        addr_resp = t.command("Ta8", timeout=8)
        lines.append("ADDR " + addr_resp)
        if len(addr_resp) < 7 or not addr_resp.startswith("Ra8"):
            raise TransportError(
                f"No/short response to Ta8 (got {addr_resp!r}) — is the device "
                f"connected on {port} and awake?")
        address = int(addr_resp[3:], 16)

        # Prime read of the 50-byte header region (mirrors the official client)
        t.command("Ta9%04X%04X" % (address, 50), timeout=8)

        next_start = address + 50
        block = 0
        while True:
            resp = t.command("Ta9%04X%04X" % (next_start, READ_SIZE), timeout=12)
            if len(resp) < 3:
                lines.append(f"BLOCKERR Ta9@{next_start} -> '{resp}'")
                break
            comp = resp[3:]                       # strip Ra9
            lines.append(f"BLOCK {next_start} {comp}")
            valid = sum(1 for i in range(0, len(comp) - 9, 10)
                        if comp[i:i + 10].lower() != "f" * 10)
            block += 1
            log(f"block {block} @{next_start}: got {len(comp) // 10} records, "
                f"{valid} valid")
            if valid == RECORDS_PER_FULL_BLOCK:
                next_start += READ_SIZE
            else:
                break
            if block > MAX_BLOCKS:
                lines.append("ABORT too many blocks")
                break

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    log(f"Wrote {len(lines)} lines to {out_path}")
    return block


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="COM3")
    ap.add_argument("--out", default="dump.txt")
    ap.add_argument("--transport", choices=["auto", "pyserial", "powershell"],
                    default="auto")
    args = ap.parse_args(argv)
    try:
        collect(args.port, args.out, args.transport)
    except TransportError as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
