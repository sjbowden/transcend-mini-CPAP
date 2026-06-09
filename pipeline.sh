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
#   PORT=COM4 ./pipeline.sh       # device on a different COM port (default COM3)
#   SLEEPHQ_UPLOADER=/path/to/sleephq_upload.py ./pipeline.sh
#
# Requires: Windows/WSL with the device on a COM port (for the pull stage), Python 3,
# and the SleepHQ uploader (a separate tool) with credentials at ~/.sleephq_credentials.
set -euo pipefail

PORT="${PORT:-COM3}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DUMP="$HERE/dump.txt"
OUT="$HERE/sleephq/out"
UPLOADER="${SLEEPHQ_UPLOADER:-$HOME/cpap/sleephq_upload.py}"

pull=1 convert=1 upload=1 dry=""
for a in "$@"; do
  case "$a" in
    --no-pull)    pull=0 ;;
    --no-convert) convert=0 ;;
    --no-upload)  upload=0 ;;
    --dry-run)    dry="--dry-run" ;;
    -h|--help)    sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a (try --help)" >&2; exit 2 ;;
  esac
done

win() { wslpath -w "$1"; }   # WSL path -> Windows path, for powershell.exe

if [ "$pull" = 1 ]; then
  echo "==> [1/3] Pulling event log from $PORT ..."
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(win "$HERE/collect.ps1")" \
    -Port "$PORT" -OutFile "$(win "$DUMP")"
  [ -s "$DUMP" ] || { echo "  ERROR: $DUMP is empty — is the device connected on $PORT?" >&2; exit 1; }
else
  echo "==> [1/3] Pull skipped (using existing $DUMP)."
fi

if [ "$convert" = 1 ]; then
  echo "==> [2/3] Converting -> $OUT ..."
  python3 "$HERE/sleephq/convert.py" "$DUMP" --out "$OUT"
else
  echo "==> [2/3] Convert skipped."
fi

if [ "$upload" = 1 ]; then
  echo "==> [3/3] Uploading to SleepHQ ..."
  [ -f "$UPLOADER" ] || { echo "  ERROR: uploader not found at $UPLOADER (set SLEEPHQ_UPLOADER)" >&2; exit 1; }
  python3 "$UPLOADER" --data-dir "$OUT" --all \
    --import-name "Transcend (all, $(date +%Y-%m-%d))" $dry
else
  echo "==> [3/3] Upload skipped."
fi

echo "==> Pipeline complete."
