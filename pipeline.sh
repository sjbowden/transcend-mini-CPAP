#!/usr/bin/env bash
# pipeline.sh — end-to-end Transcend -> the cpap repo (SleepHQ Local).
#
# Pulls the device's event log over USB and hands it to the cpap repo's
# transcend_import.py (~/projects/cpap), which stores the dump in
# transcend/dumps/ and refreshes SleepHQ Local, where Transcend nights show
# beside the ResMed ones. Run the whole thing, or skip stages.
#
#   ./pipeline.sh                 # pull -> import
#   ./pipeline.sh --no-pull       # import the existing dump.txt (skip the device)
#   ./pipeline.sh --no-import     # pull only
#   ./pipeline.sh --convert       # also write the ResMed-format tree (sleephq/out)
#   SLEPT_TZ=Asia/Tokyo SLEPT_SINCE="2026-10-16 17:00" ./pipeline.sh
#                                 # travelling: zone slept in, and (first import
#                                 # there) when you arrived, local time
#   PORT=/dev/cu.usbserial-XX ./pipeline.sh   # explicit port (default: auto-detect)
#   MASK=3 ./pipeline.sh --convert            # ResMed mask-type code for the STR record
#   CPAP_REPO=/path/to/cpap ./pipeline.sh     # default ~/projects/cpap
#   PYTHON=/path/to/python3 ./pipeline.sh     # override interpreter (see below)
#
# Requires: Python 3 with pyserial (macOS/Linux) or Windows/WSL, and the device
# on USB. The pull stage goes through collect.py, which picks the serial
# backend itself (pyserial on macOS/Linux, the powershell bridge for a COM port
# under WSL) — so this script is the same on every OS. Uses ./.venv/bin/python3
# automatically if present (that's normally where pyserial is installed);
# override with PYTHON=. The import runs under IMPORT_PYTHON (default python3,
# which needs numpy, pandas and matplotlib), with TRANSCEND_REPO set to this
# checkout so it decodes with this repo's parse.py.
#
# Uploading to SleepHQ is no longer part of the pipeline; sleephq/upload.py
# still works standalone on a --convert tree if ever needed.
set -euo pipefail

PORT="${PORT:-auto}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DUMP="$HERE/dump.txt"
OUT="$HERE/sleephq/out"
CPAP_REPO="${CPAP_REPO:-$HOME/projects/cpap}"
IMPORTER="$CPAP_REPO/transcend_import.py"
IMPORT_PYTHON="${IMPORT_PYTHON:-python3}"
# Prefer a venv sitting next to this script (where pyserial actually lives)
# over whatever "python3" happens to resolve to on PATH — a bare `python3`
# with no pyserial makes port "auto" fail with the same error as "no device
# found", which is a confusing way to discover you forgot to activate .venv.
if [ -z "${PYTHON:-}" ] && [ -x "$HERE/.venv/bin/python3" ]; then
  PYTHON="$HERE/.venv/bin/python3"
else
  PYTHON="${PYTHON:-python3}"
fi

pull=1 convert=0 import=1
# bash < 4.4 (macOS still ships 3.2) treats "$@" as unbound under `set -u`
# when there are no positional parameters, so guard the loop -- `./transcend`
# with no arguments is the common case.
for a in ${@+"$@"}; do
  case "$a" in
    --no-pull)    pull=0 ;;
    --convert)    convert=1 ;;
    --no-import)  import=0 ;;
    -h|--help)    sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a (try --help)" >&2; exit 2 ;;
  esac
done

if [ "$pull" = 1 ]; then
  echo "==> [1/3] Pulling event log from $PORT ..."
  "$PYTHON" "$HERE/collect.py" --port "$PORT" --out "$DUMP"
  [ -s "$DUMP" ] || { echo "  ERROR: $DUMP is empty — is the device connected? ($PORT)" >&2; exit 1; }
else
  echo "==> [1/3] Pull skipped (using existing $DUMP)."
fi

if [ "$convert" = 1 ]; then
  echo "==> [2/3] Converting -> $OUT ..."
  "$PYTHON" "$HERE/sleephq/convert.py" "$DUMP" --out "$OUT" ${MASK:+--mask "$MASK"}
else
  echo "==> [2/3] Convert skipped (pass --convert for the ResMed-format tree)."
fi

if [ "$import" = 1 ]; then
  echo "==> [3/3] Importing into $CPAP_REPO ..."
  [ -f "$IMPORTER" ] || { echo "  ERROR: importer not found at $IMPORTER (set CPAP_REPO)" >&2; exit 1; }
  ( cd "$CPAP_REPO" && TRANSCEND_REPO="${TRANSCEND_REPO:-$HERE}" "$IMPORT_PYTHON" "$IMPORTER" "$DUMP" \
      ${SLEPT_TZ:+--tz "$SLEPT_TZ"} ${SLEPT_SINCE:+--since "$SLEPT_SINCE"} )
  echo "  Commit transcend/ in $CPAP_REPO to keep the dump."
else
  echo "==> [3/3] Import skipped."
fi

echo "==> Pipeline complete."
