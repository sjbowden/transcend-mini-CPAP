#!/usr/bin/env bash
# pipeline.sh — end-to-end Transcend -> SleepHQ.
#
# Pulls the device's event log over USB, converts it to a ResMed/SleepHQ SD-card tree,
# and uploads it. Run the whole thing, or skip stages.
#
#   ./pipeline.sh                 # pull -> convert -> upload (all data on the device)
#   ./pipeline.sh --no-upload     # pull + convert only (inspect sleephq/out first)
#   ./pipeline.sh --no-pull       # reuse the existing dump.txt (skip the device)
#   ./pipeline.sh --no-convert    # skip the convert stage (re-upload existing out/)
#   ./pipeline.sh --dry-run       # convert, then show what WOULD upload (sends nothing)
#   PORT=/dev/cu.usbserial-XX ./pipeline.sh   # explicit port (default: auto-detect)
#   MASK=3 ./pipeline.sh          # ResMed mask-type code for SleepHQ's settings panel
#   SLEEPHQ_UPLOADER=/path/to/sleephq_upload.py ./pipeline.sh
#
# Requires: Python 3 with pyserial (macOS/Linux) or Windows/WSL, the device on
# USB, and the SleepHQ uploader (a separate tool) with credentials at
# ~/.sleephq_credentials. The pull stage goes through collect.py, which picks the
# serial backend itself (pyserial on macOS/Linux, the powershell bridge for a
# COM port under WSL) — so this script is the same on every OS.
set -euo pipefail

PORT="${PORT:-auto}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DUMP="$HERE/dump.txt"
OUT="$HERE/sleephq/out"
UPLOADER="${SLEEPHQ_UPLOADER:-$HOME/cpap/sleephq_upload.py}"
PYTHON="${PYTHON:-python3}"

pull=1 convert=1 upload=1 dry=""
for a in "$@"; do
  case "$a" in
    --no-pull)    pull=0 ;;
    --no-convert) convert=0 ;;
    --no-upload)  upload=0 ;;
    --dry-run)    dry="--dry-run" ;;
    -h|--help)    sed -n '2,17p' "$0"; exit 0 ;;
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
  echo "==> [2/3] Convert skipped."
fi

if [ "$upload" = 1 ]; then
  echo "==> [3/3] Uploading to SleepHQ ..."
  [ -f "$UPLOADER" ] || { echo "  ERROR: uploader not found at $UPLOADER (set SLEEPHQ_UPLOADER)" >&2; exit 1; }
  "$PYTHON" "$UPLOADER" --data-dir "$OUT" --all \
    --import-name "Transcend (all, $(date +%Y-%m-%d))" $dry
else
  echo "==> [3/3] Upload skipped."
fi

echo "==> Pipeline complete."
