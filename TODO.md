# TODO

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
  RampStart subdata encoding confirmed ×10 (see Closed) and locked in by a test.

### Event flags — explain the pressure curve
- ✅ **DONE — Snore/FlowLimit fixed.** `FlowLimitedRatio` (18) and `SnoringRatio` (19) are
  one-per-night end-of-session summaries (confirmed: 5 sessions → 5 each), so they're now a
  flat PLD line at the night's value instead of a spurious end-of-night spike.
- ✅ **DONE — ramp drawn in the pressure curve.** The `RampStart`/`RampEnd` (5/6) window is
  rendered as a rise from ~4 cmH₂O to therapy pressure, so the gentle-rise shows instead of a
  flat session start. (A separate EVE/CSL ramp *marker* is still possible but redundant now.)
- ✅ **DONE (opt-in) — "why APAP raised pressure."** Events 23–28 (PressureIncreasedFrom
  Apneas/Hypopneas/Combination/Snoring/FlowLimited/Command) can be emitted as EVE annotations
  via `--pressure-reason-flags` (OFF by default). Default-off because they're largely redundant
  with the apnea/hypopnea flags + the visible pressure rise, and SleepHQ may ignore the
  non-standard labels or count them as events (inflating totals) — enable it to check rendering.

### Daily-summary accuracy
- ✅ **DONE (leak) / ⚠️ N/A (pressure) — app-exact stat methods.** *Leak* STR percentiles use
  the app's nearest-rank method over the real `AverageLeak` samples (validated: `Leak.50` =
  7.2 LPM vs the app's 6.96). *Pressure* has **no periodic samples on this device** (zero
  `PressureAverage` events), so the pressure STR fields fall back to the per-session
  Min/Max-PressureUsed — approximate, not true percentiles. `.Max` fields use the Maximum*
  events. Details below kept for reference.
- ✅ **DONE / N-A — match the app's exact stat methods** (decompile — see PROTOCOL.md
  "How the official app computes its numbers"):
  - **Percentiles = nearest-rank** (`pctile`, `sorted[round(p·n)−1]`): applied to leak
    (`Leak.50/.70/.95`); `.Max` uses the device `MaximumLeak`/`MaximumPressureUsed` events.
    Pressure percentiles N/A — this device logs no periodic pressure samples.
  - **Time-weighted averages:** N/A — no periodic samples to weight on this device.
  - **AHI rounding:** moot — SleepHQ recomputes AHI from the EVE apnea/hypopnea flags, not
    the STR `AHI` field, so the field's rounding has no observable effect.
  - `MinimumLeak` (20): no distinct ResMed STR field for a leak floor — left unused.

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
- **RampStart pressure encoding confirmed (×10).** The ramp-night dump shows `RampStart` (5)
  subdata = **40** for a configured 4.0 cmH₂O GentleRise Pressure, so the byte is the ramp
  start pressure ×10 (÷10 → cmH₂O). `RampEnd` (6) subdata = **1** (completion flag, not a
  pressure). Replaced the `>20` heuristic in `session_metrics()` with the confirmed ÷10,
  documented in PROTOCOL.md, and locked in by `test_ramp_curve_starts_at_ramp_start_pressure`.
- **Blob comfort-flag mapping — not achievable, closed.** The iOS app exposes only named
  fields (AirRelief=EZEX, GentleRise Pressure/Duration, locked prescription pressures) and
  no auto-start/stop/alert toggle, so *no user setting writes the `ConfigurationData` blob*.
  It's a factory/firmware-fixed block (`aa55` magic) — can't be diff-mapped. `--snapshot`/
  `--diff` stay useful only to confirm writes preserve it verbatim.
