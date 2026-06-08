# TODO

## Enhancing the SleepHQ upload

What the converter (`sleephq/convert.py`) emits today and how to make SleepHQ show
more of what the Transcend actually records. Legend:
**✅ data already in hand** · **⚠️ needs a unit/scale check first** · **❌ blocked (device has no such data — do not fabricate)**.

### Settings fidelity — make SleepHQ's "settings" panel match the device
- ✅ **Feed live config into STR instead of `STR_BASELINE` constants.** `build_str`
  currently hard-codes start/min/max pressure to 4.0/20.0 and EPR off. Pull the real
  values from `settings.py:read_config()` (min/max/start pressure, ramp time, ramp
  pressure, EZEX) so the displayed prescription is correct.
- ✅ **Map EZEX → ResMed EPR.** EZEX is the Transcend's pressure-relief; surface it via
  `S.EPR.EPREnable` / `S.EPR.Level` (currently forced to 0 in the `OFF` list) so SleepHQ
  shows relief is active. Also use the per-event `EZEXLevel` (event 15) if it varies.
- ✅ **Ramp into STR.** Populate the ramp-enable/duration/start-pressure settings fields
  from config + `RampStart`/`RampEnd` (events 5/6), which are currently dropped.

### Event flags — explain the pressure curve
- ✅ **Annotate ramp period** from `RampStart`/`RampEnd` (5/6) as an EVE/CSL marker.
- ✅ **Surface "why APAP raised pressure."** Events 23–28 (PressureIncreasedFrom
  Apneas/Hypopneas/Combination/Snoring/FlowLimited/Command) are folded into the pressure
  step today but their *reason* is discarded. Emit them as EVE annotations so the
  timeline explains each pressure increase.
- 🐞 **Fix Snore/FlowLimit modeling — they're per-night summaries, not time series.** The
  decompiled event phases prove `FlowLimitedRatio` (18) and `SnoringRatio` (19) are logged
  **once per session, at its end** (confirmed: 5 sessions → 5 each). `convert.py` currently
  builds `snore_pts`/`flowlim_pts` as PLD time-series channels, so they sit at 0 all night
  then jump at the very end — misleading. Move them to **STR daily-summary** stats (e.g.
  snore index `RIN`, a flow-limit summary), and drop the bogus PLD channels. Same applies to
  Min/Max **Used** (16/17) and Min/Max **Leak** (20/21) — single end-of-session values.

### Daily-summary accuracy (STR percentiles are proxies today)
- ⚠️ **Real, time-weighted pressure percentiles.** `BlowPress.95/.5`, `MaskPress.50/.95`,
  `TgtIPAP/EPAP` currently reuse min/max-used and a plain mean of `PressureAverage`.
  Integrate the pressure step function over each night for true 50th/95th percentiles.
- ⚠️ **Real leak percentiles.** `Leak.50/.70/.95/.Max` reuse avg/max for all four. Compute
  actual percentiles from the ~5-min `AverageLeak` series, and fold in `MinimumLeak`/
  `MaximumLeak` (events 20/21) as a per-report band.
- ✅ **Time-weight the averages, not the event count.** Sparse events held by `stepper`
  should weight summary stats by *duration at value*, not a uniform `mean()` of samples.

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
