# TODO

## NEXT: confirm the RampStart pressure encoding (needs the ramp-night dump)

**Why.** `session_metrics()` in `sleephq/convert.py` draws the ramp rise from the
`RampStart` (5) event, whose subdata byte is the ramp start pressure — but we don't know
whether the device stores it **raw** (`4` = 4 cmH₂O) or **×10** (`40` = 4.0 cmH₂O). The
code currently guesses by magnitude (`ramp_start_p = rsv / 10.0 if rsv > 20 else rsv`).
Both encodings are unambiguous for the legal 4–10 cmH₂O range, so the guess works, but it
should be replaced with the confirmed encoding. A dump from a night with ramp enabled is
on hand (this machine) — that settles it.

**How to confirm** (no device needed, just the dump):
```bash
python3 parse.py path/to/ramp-night-dump.txt
grep -n "RampStart\|RampEnd" events.csv
```
The `value`/`raw_subdata` columns (identical for type 5, scale 1.0) hold the answer.
Compare against the ramp start pressure the device was configured with that night
(`settings.py --show` → `StartingRampPressure` / "GentleRise Pressure"; factory default
4.0). For a 4.0 cmH₂O setting: subdata ≈ **40** → ×10 encoding; subdata ≈ **4** → raw.

**Then make these changes:**
1. `sleephq/convert.py` `session_metrics()`: replace the
   `rsv / 10.0 if rsv > 20 else rsv` heuristic with the confirmed encoding, and fix the
   hedging comment above it ("decoded x1 -> divide by 10 to cmH2O" if ×10 confirmed).
2. `tests/test_transcend.py`: the synthetic dump already encodes RampStart as
   `enc(t0, 5, 40)` (×10 assumption). Make it match the confirmed encoding and add an
   assertion that the BRP/PLD pressure curve starts at the ramp start pressure
   (not therapy pressure) so the encoding is locked in by a test.
3. `PROTOCOL.md`: record the confirmed subdata semantics in the event-type table for
   type 5 — and for type 6 (`RampEnd`): note what its subdata byte carried in this dump
   (currently assumed meaningless; the grep above shows it).
4. While the dump is at hand: sanity-check the converted output
   (`python3 sleephq/convert.py <dump> --out /tmp/ramp-check`) — the ramp night's
   pressure curve should rise from the ramp start pressure over `RampStart→RampEnd`,
   and STR `S.RampEnable`/`S.RampTime` should match the device setting.

**Do NOT commit the dump** — it carries the device serial and therapy data
(`*.txt` dumps and `events.csv` are git-ignored already; keep it that way).

## Enhancing the SleepHQ upload

What the converter (`sleephq/convert.py`) emits today and how to make SleepHQ show
more of what the Transcend actually records. Legend:
**✅ data already in hand** · **⚠️ needs a unit/scale check first** · **❌ blocked (device has no such data — do not fabricate)**.

### Settings fidelity — make SleepHQ's "settings" panel match the device
- ✅ **DONE — prescription min/max from the event log.** `build_str` sets `S.A.MinPress`/
  `S.A.MaxPress` from the per-session `MinimumPressureSetting`/`MaximumPressureSetting`
  events (13/14), so the displayed APAP range matches the device (no live read needed — the
  prescription is in the dump).
- ✅ **DONE — EZEX → ResMed EPR.** Per-session `EZEXLevel` (15) drives `S.EPR.EPREnable`/
  `S.EPR.Level` (was forced off). Verified: the 06-06 night maps to EPR Level 3 (EZEX was 3
  then, even though the device now reads 0).
- ✅ **DONE — ramp into STR.** `build_str` now derives the ramp duration from the
  `RampStart`/`RampEnd` (5/6) events (snapped to the device's 5-min increments) and sets
  `S.RampEnable` (3=On/1=Off) + `S.RampTime` per day — no live `settings.py` read needed.
  Start-pressure fidelity is the "NEXT" item above.

### Event flags — explain the pressure curve
- ✅ **DONE — Snore/FlowLimit fixed.** `FlowLimitedRatio` (18) and `SnoringRatio` (19) are
  one-per-night end-of-session summaries (confirmed: 5 sessions → 5 each), so they're now a
  flat PLD line at the night's value instead of a spurious end-of-night spike.
- ✅ **DONE — ramp drawn in the pressure curve.** The `RampStart`/`RampEnd` (5/6) window is
  rendered as a rise from ~4 cmH₂O to therapy pressure, so the gentle-rise shows instead of a
  flat session start. (A separate EVE/CSL ramp *marker* is still possible but redundant now.)
- ⬜ **TODO (speculative) — "why APAP raised pressure."** Events 23–28 (PressureIncreasedFrom
  Apneas/Hypopneas/Combination/Snoring/FlowLimited/Command) carry the reason but it's
  discarded. Could emit EVE annotations — but SleepHQ may not render non-standard EVE labels,
  so verify it displays before investing.

### Daily-summary accuracy
- ✅ **DONE (leak) / ⚠️ N/A (pressure) — app-exact stat methods.** *Leak* STR percentiles use
  the app's nearest-rank method over the real `AverageLeak` samples (validated: `Leak.50` =
  7.2 LPM vs the app's 6.96). *Pressure* has **no periodic samples on this device** (zero
  `PressureAverage` events), so the pressure STR fields fall back to the per-session
  Min/Max-PressureUsed — approximate, not true percentiles. `.Max` fields use the Maximum*
  events. Details below kept for reference.
- ⚠️ **Match the app's exact stat methods (recovered from the decompile — see PROTOCOL.md
  "How the official app computes its numbers").** Concretely:
  - **Percentiles = nearest-rank, no interpolation:** `sorted[round(p·n)−1]` (desktop). Use
    for leak P95/P90 (over the `AverageLeak` samples) and pressure P95/P90 (over pressure
    samples). Today `Leak.50/.70/.95/.Max` and `BlowPress.95/.5`/`MaskPress.*` reuse avg/max
    or a plain mean — replace with this.
  - **Averages = time-weighted by minutes** (`TotalX/TotalXMinutes`), not a uniform `mean()`.
  - **AHI** = `(apneas+hypopneas)/hours` rounded 2 dp away-from-zero; AI/HI same.
  - Fold `MinimumLeak`/`MaximumLeak` (20/21, one per session) in as a per-session band.

### Units & reconciliation (verify before trusting the graphs)
- ✅ **Leak unit validated.** Transcend leak is L/min → ÷60 → L/s for ResMed. Confirmed
  against the official app: 6/6 night, our mean 6.5–7.0 LPM vs the app's 6.96 LPM. SleepHQ
  shows the channel back in L/min, matching. (The app's other "3.48" screen is exactly ½ —
  an app display convention; we match the standard 6.96 average.)
- ℹ️ **Leak is a 5-min average, not 2 s** — so it shows a drifting envelope, never the sharp
  spikes ResMed draws. Now linearly interpolated between points (`interp()`) to avoid the
  staircase look. No fix possible for the resolution itself (device logs one `AverageLeak`
  per ~5 min); `MaximumLeak` (event 21) is logged too sparsely to reconstruct spikes.
- ✅ **Reconcile usage.** Cross-check STR `Duration`/`MaskOn`/`MaskOff` against the device
  counters `Tbc` (blower) / `Tb8` (patient time) so SleepHQ usage matches the device.

### Known hard limits (don't chase — no source data)
- ❌ Flow waveform, respiratory rate, tidal volume, minute ventilation — the Transcend is
  an event recorder, not a data-logger. BRP/PLD respiratory channels stay 0.
- ❌ Central vs obstructive apnea — device doesn't classify; all apneas map to Obstructive.
- ❌ SpO2 — no oximetry source (STR SpO2 fields stay -1).

## Closed
- **Blob comfort-flag mapping — not achievable, closed.** The iOS app exposes only named
  fields (AirRelief=EZEX, GentleRise Pressure/Duration, locked prescription pressures) and
  no auto-start/stop/alert toggle, so *no user setting writes the `ConfigurationData` blob*.
  It's a factory/firmware-fixed block (`aa55` magic) — can't be diff-mapped. `--snapshot`/
  `--diff` stay useful only to confirm writes preserve it verbatim.
