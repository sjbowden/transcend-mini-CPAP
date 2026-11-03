#!/usr/bin/env bash
# pipeline.sh — end-to-end Transcend -> local analysis.
#
# Pulls the device's event log over USB, converts it to a ResMed-format SD-card
# tree, and runs the ResMed analyzer (~/projects/cpap/cpap_analyzer.py) on it.
# Run the whole thing, or skip stages.
#
#   ./pipeline.sh                 # pull -> convert -> analyze (all data on the device)
#   ./pipeline.sh --no-analyze    # pull + convert only (inspect sleephq/out first)
#   ./pipeline.sh --no-pull       # reuse the existing dump.txt (skip the device)
#   ./pipeline.sh --no-convert    # skip the convert stage (re-analyze existing out/)
#   PORT=/dev/cu.usbserial-XX ./pipeline.sh   # explicit port (default: auto-detect)
#   MASK=3 ./pipeline.sh          # ResMed mask-type code written into the STR record
#   CPAP_ANALYZER=/path/to/cpap_analyzer.py ./pipeline.sh
#   PYTHON=/path/to/python3 ./pipeline.sh   # override interpreter (see below)
#
# Output: charts + cpap_data.csv in ./analysis — never the analyzer's own
# directory, so the ResMed results there are left alone.
#
# Requires: Python 3 with pyserial (macOS/Linux) or Windows/WSL, the device on
# USB, and (for analyze) pandas + matplotlib. The pull stage goes through
# collect.py, which picks the serial backend itself (pyserial on macOS/Linux,
# the powershell bridge for a COM port under WSL) — so this script is the same
# on every OS. Uses ./.venv/bin/python3 automatically if present (that's
# normally where pyserial is installed); override with PYTHON=. The analyzer
# runs under ANALYZER_PYTHON (default python3), since the venv usually has
# pyserial but not pandas.
#
# Uploading to SleepHQ is no longer part of the pipeline; sleephq/upload.py
# still works standalone if ever needed.
set -euo pipefail

PORT="${PORT:-auto}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DUMP="$HERE/dump.txt"
OUT="$HERE/sleephq/out"
ANALYSIS="$HERE/analysis"
ANALYZER="${CPAP_ANALYZER:-$HOME/projects/cpap/cpap_analyzer.py}"
ANALYZER_PYTHON="${ANALYZER_PYTHON:-python3}"
# Prefer a venv sitting next to this script (where pyserial actually lives)
# over whatever "python3" happens to resolve to on PATH — a bare `python3`
# with no pyserial makes port "auto" fail with the same error as "no device
# found", which is a confusing way to discover you forgot to activate .venv.
if [ -z "${PYTHON:-}" ] && [ -x "$HERE/.venv/bin/python3" ]; then
  PYTHON="$HERE/.venv/bin/python3"
else
  PYTHON="${PYTHON:-python3}"
fi

pull=1 convert=1 analyze=1
# bash < 4.4 (macOS still ships 3.2) treats "$@" as unbound under `set -u`
# when there are no positional parameters, so guard the loop -- `./transcend`
# with no arguments is the common case.
for a in ${@+"$@"}; do
  case "$a" in
    --no-pull)    pull=0 ;;
    --no-convert) convert=0 ;;
    --no-analyze) analyze=0 ;;
    -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
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

if [ "$analyze" = 1 ]; then
  echo "==> [3/3] Analyzing -> $ANALYSIS ..."
  [ -f "$ANALYZER" ] || { echo "  ERROR: analyzer not found at $ANALYZER (set CPAP_ANALYZER)" >&2; exit 1; }
  mkdir -p "$ANALYSIS"
  "$ANALYZER_PYTHON" "$ANALYZER" --data-dir "$OUT" --output-dir "$ANALYSIS"
else
  echo "==> [3/3] Analyze skipped."
fi

echo "==> Pipeline complete."
