# Transcend Micro — USB serial protocol (reverse-engineered)

Device: Somnetics Transcend Micro 510 (FTDI FT231X bridge, `VID_0403 PID_6015`).
Source: decompiled `Somnetics.TranscendGo.*` assemblies (TranSyncGo client) — the
desktop "mini PAP" software, which serves the whole Transcend family (miniCPAP /
Transcend 3 / Micro), hence the "mini" naming throughout.
Validated live against a real device on 2026-06-07.

See [README.md](README.md) for the full toolchain (download → parse → SleepHQ upload).

> **What the log contains:** discrete, time-stamped *events* and summary stats —
> therapy start/stop, pressure changes, apneas/hypopneas, leak, snore/flow-limit ratios.
> **What it does NOT contain:** any continuous waveform — no flow rate, mask-pressure
> trace, respiratory rate, tidal volume, or minute ventilation. The Transcend is a
> compliance/event recorder, not a full data-logger, so those channels are simply
> unavailable (this is also why OSCAR never supported detailed Transcend graphs).
> Vendor-stated retention: the device holds **3–6 months** of compliance data
> depending on use (My SleepDash manual 104378 p.7) — download before it wraps.

## Transport
- USB‑to‑serial bridge. Two are used across hardware revisions: the **FTDI FT231X**
  (`VID_0403 PID_6015`) and a **Silicon Labs CP210x** (`VID_10C4 PID_EA60`) — the app
  detects either (FTDI by `Manufacturer=FTDI`+`PNPClass=Ports`, or the CP210x by its
  description string). Your unit may enumerate as one or the other.
- Serial port, **38400 baud, 8 data bits, no parity, 1 stop bit** (8N1).
- `RTS=false, DTR=false, DiscardNull=true`.
- Commands are ASCII. App writes the command **one char at a time** (device echoes each
  char), then a terminating **`\r`** (CR).
- Device replies: `<echoed-cmd>\r<RESPONSE>\r`. Response complete once **two `\r`** seen.
- Response = text between the two CRs. First **3 chars = response code**, remainder = args.
- Command codes start with `T` (transmit); expected response code = same with `T`→`R`
  (e.g. send `Ta8` → expect `Ra8`).

## Commands used for data extraction
| Cmd  | Resp | Meaning                | Response args |
|------|------|------------------------|---------------|
| `Tbd`| `Rbd`| Event log header       | rev(2) fullFlag(2) serial(64 hex→ascii) fwChk(8 hex→ascii) dataChk(4 LE) **eventsInQueue(4 hex LE)** offset(4 hex LE) reserved(12) |
| `Tff`| `Rff`| Device type            | type(4) |
| `Ta8`| `Ra8`| Event data **address** | address(4 hex → int) |
| `Ta9`| `Ra9`| Read compliance block  | args sent = StartAddress(4 hex UPPER) + NumBytesToRead(4 hex UPPER); response = CompData (hex) |

Other commands exist (pressure `Ta1`/`R41`, monitor `Ta3`, flow `Tc3`, patient hours
`Tb8`, calibration `Tb3`, push blower `T11`, …) but are not needed to pull the event log.

> **Destructive — never sent by this toolkit:** `Taf` **Reset Compliance** erases the
> device's event log (the official app gates it behind a confirmation prompt), and `Tb4`
> rewrites the calibration. This toolkit is read-only except for the guarded config writes
> in `settings.py`; it never issues `Taf` or `Tb4`.

## Status / usage commands (decoded & live-validated)
These return plain **comma-separated decimals** (not hex), one value per `ResponseArgument`:

| Cmd  | Resp | Meaning            | Response args (decoded) |
|------|------|--------------------|-------------------------|
| `Tbc`| `Rbc`| Blower runtime     | `hours,minutes,seconds` — total blower on-time. **This is the figure the app shows as "usage."** |
| `Tb8`| `Rb8`| Patient hours      | `hours,minutes,seconds,#sessions≥8h,#sessions6–8h,#sessions4–6h` — actual breathing time (shorter than blower time) + a session-length histogram |
| `Tff`| `Rff`| Device type        | a 4-char code string (e.g. `8011`); the CPAP/APAP/EZEX *class* is taken from the serial's first char, not this |
| `T6d`| `R6d`| Device state       | **opaque** — the app declares no fields and never decodes it |
| `Ta3`| `Ra3`| Monitor data (live)| `pressureGoal×0.1, measuredPressure×0.1, lungFlow×0.1, leak×0.1, mode` — real-time only |
| `Tc3`| `Tc3`| Flow (live)        | `hoseFlow, baselineFlow` — real-time only |
| `T60`| `R60`| Pressure sensor    | `pressure×0.1` (live) |
| `Tb3`| `Rb3`| Calibration offset | `sign, offset×0.1` (display only) |

Example (this device): `Tbc → Rbc7,2,36` = 7 h 2 m 36 s blower; `Tb8 → Rb86,31,18,0,1,0`
= 6 h 31 m 18 s patient time, 1 session of 6–8 h.

> **Firmware version is not exposed over USB.** There is **no** get-firmware-version
> command in the entire serial command set — the only firmware datum on the wire is the
> **checksum** in the `Tbd` header (`FirmwareChecksum`, e.g. `ecb8`). The human-readable
> version the *mobile* app shows (e.g. `1.6.0`) comes over Bluetooth, a separate protocol.

## Settings (configuration read / write)
The device configuration is read with `Tab`→`Rab` and written with `Tcc`→`R55`
(AutoPAP / CPAP+EZEX) or `Tac`→`R55` (StandardCPAP). **Device type = first character of
the serial**: `A`=StandardCPAP, `B`=AutoPAP, `C`=CPAP+EZEX. The same transport as reads.

> The official app's password only locks these in **its own UI** — the device firmware
> accepts config writes with **no authentication**. The password is a single static
> string printed in the public clinician guide (104214 p.7): `juniper`. `settings.py`
> therefore imposes the boundary itself (comfort settings free; prescription pressures
> behind a flag).

**AutoPAP / EZEX config args** (positional, big-endian hex; read scales shown, write = inverse):

| Pos | Field | Len | Read | Write |
|-----|-------|-----|------|-------|
| 0 | StartingTherapyPressure | 4 | hex/10 | int(v·10)→hex |
| 1 | **ConfigurationData** | 15 | opaque (≈ calib offset + start + latch) | passthrough |
| 2 | MinimumTherapyPressure | 4 | hex/10 | int(v·10)→hex |
| 3 | MaximumTherapyPressure | 4 | hex/10 | int(v·10)→hex |
| 4 | **Reserved** | 5 | opaque (= calib offset ×~64) | passthrough |
| 5 | RampDurationMinutes | 4 | hex | int→hex |
| 6 | EZEX (relief level) | 4 | hex/10 | int(v·10)→hex |
| 7 | StartingRampPressure | 4 | hex/10 | int(v·10)→hex |

(StandardCPAP layout: StartingTherapyPressure(4), ConfigurationData(28), RampDuration(4),
Reserved(4), StartingRampPressure(4) — no min/max.) Hex is uppercase, left-padded to the
field width (`CommandArgumentFormatter`: `int(value·scale).ToString("X")`). The two opaque
fields (`ConfigurationData`, `Reserved`) **must be written back verbatim** — the tool sends
them unchanged — but `ConfigurationData` is **not static**: the firmware regenerates part of
it after a write. Decoded live by single-field sweeps (2026-06-20), `ConfigurationData` is
15 hex chars **`0000aa550100` `SS` `F`**:

- chars 0–3 — **calibration offset × 10**, signed 16-bit (0.1 cmH₂O units). Decoded by setting
  the offset via the app's calibrate feature and reading back: `+0.0`→`0000`, `−0.3`→`fffd`
  (−3), `+0.9`→`0009`, `−0.9`→`fff7` (−9) — matches the `Tb3` calibration getter exactly. It
  read `0000` for months only because the offset was `+0.0`; it is **not** a constant.
- chars 4–7 `aa55` — magic marker (constant).
- chars 8–11 `0100` — **constant**; did not move when the calibration offset was swept ±0.9, so
  it's not the offset. `0x0100` = 256 = unity in 8.8 fixed-point → **likely the calibration
  gain/slope** (the app exposes only the offset, not the gain, so we can't vary it to confirm).
- chars 12–13 `SS` — **`StartingTherapyPressure ×10`** in hex. Confirmed across a 5-point serial
  sweep (11.0→`6e` (0x6E=110), 12.0→`78`, 13.0→`82`, 14.0→`8c`, 15.0→`96`) plus a 6th point from
  an official-app write (13.7→`89` (0x89=137)). **Min and max do NOT appear** anywhere in the
  blob — verified twice: a serial ± sweep and an app write that changed min/max/ramp/EZEX/ramp-
  pressure together left the blob byte-identical.
- char 14 `F` — a 1-nibble **sticky latch**. It was `0` only in the original clinic-provisioned
  config we first read; the first local write flipped it to `1`, and it **stays `1`** through
  every write since — including an **official-app** write that changed the start pressure and so
  regenerated the blob (`SS` `78`→`89` for start 12.0→13.7) yet left `F` = `1`. So ordinary
  writes (app *or* serial) do **not** clear it (the "app resets the dirty bit" hypothesis is
  disproven, as is the earlier "tracks ramp" guess — ramp 0/5/10 all read `1`). It behaves like
  a one-way "config modified since factory/clinic provisioning" latch; presumably only a factory
  reset clears it (a **"Reset Compliance" did NOT** clear it — `F` stayed `1` — so it's tied to
  config, not compliance/usage state). Semantics still unproven, but no longer behaviorally open.

So the blob is a firmware-derived record of the **calibration offset** + **start pressure** (plus
the gain candidate and the latch), **not** a user-mappable comfort-flag field. The tool always
sends it verbatim; the firmware rewriting the calibration/`SS`/`F` bytes means a post-write
read-back difference *confined to `ConfigurationData`* is expected and benign (the named settings
still verify exactly). Calibration `Tb4` is writable but **must not be touched** (it recalibrates
the pressure sensor) — and because the offset lives in the blob, RMW preserving it verbatim is
what keeps a config write from disturbing calibration.

**The `Reserved` field (5 hex) also holds the calibration offset**, in a *different* unit: its low
16 bits = **offset × ~64** (signed, looks like raw sensor counts), high nibble constant `0`. Same
sweep: `+0.0`→`00000`, `−0.3`→`0ffed` (−19), `+0.9`→`0003a` (+58), `−0.9`→`0ffc6` (−58). So the
offset is stored twice — `ConfigurationData[0:4]` at 0.1 cmH₂O resolution (×10) and `Reserved` at
raw-count resolution (×~64). `Reserved` stayed `00000` through every pressure/ramp/EZEX change, so
it is calibration-only, not a checksum.

**Bit accounting — `ConfigurationData` (60 bits = 15 hex chars):**

| Bits | Span | Content | Status |
|-----:|------|---------|--------|
| 16 | chars 0–3 | calibration offset × 10 (signed) | **decoded** |
| 16 | chars 4–7 `aa55` | magic signature | identified (not data) |
| 16 | chars 8–11 `0100` | likely calibration **gain** (unity = 0x0100) | constant; can't vary to confirm |
| 8  | chars 12–13 `SS` | `StartingTherapyPressure ×10` | **decoded** |
| 3  | high 3 bits of nibble `F` | constant `0` | inert |
| 1  | low bit of nibble `F` | sticky `0→1` "modified" latch | characterized; exact meaning unproven |

**The blob is essentially fully explained.** 24 bits are decoded data (calibration offset + start
pressure), 16 are the magic, 16 are the gain (constant, strong hypothesis), and the last nibble is
the latch. The earlier "32 inert constant bits" turned out to be the **calibration block** — it
only looked inert because the offset was `+0.0` and the gain is fixed at unity. **Your calibration
hypothesis (the constant bits are calibration) was correct.** The one remaining soft spot is
confirming `0100` is the gain (the app doesn't expose a gain control to sweep).

**App names** for the user-changeable fields. The field names above follow the **Windows**
app (`EZEX`, `Ramp`) because they come from decompiling it; the **iOS** app uses friendlier
labels for the same settings (both apps gate the prescription pressures):
`EZEX` = **AirRelief** (0–3); `StartingRampPressure` = **GentleRise Pressure** (4–10 cmH₂O);
`RampDurationMinutes` = **GentleRise Duration** (0 = disabled, to 45 min in 5‑min steps).
The clinician guide (104214 p.8) adds a relative cap: GentleRise Pressure can be set
**up to 1 cmH₂O below the Therapy Pressure** — and on APAP "Therapy Pressure" here is the
**StartingTherapyPressure**, not the min. Confirmed against the official app (2026-06-20): it
set GentleRise 9.5 with min 10.0 (only 0.5 below the min) but start 12.0/13.7 (≥1 below start),
so the ramp is bounded by where therapy *starts*, not the APAP floor. `StartingTherapyPressure`
itself lies between min and max (the apps enforce both; firmware acceptance untested).

**Not every device feature is on the serial link.** *Dry mode* (a post-therapy option that
runs the blower to dry the tube and mask) has no *setting* visible here: toggling it on/off
changes neither the config (`Tab`), the device state (`T6d`), nor the live monitor (`Ta3`)
while idle, and it never touches the blob — the enable flag is app/Bluetooth-side. But the
drying *cycle itself* is wire-visible after the fact: running it advanced the **blower-time**
counter `Tbc` (+2m19s in one test) while the **patient-time** counter `Tb8` stayed put. This
is the general rule for the two counters — `Tbc` = all blower runtime (ramp + mask-off + dry
cycles), `Tb8` = actual breathing only — which is why blower time exceeds patient time.

**"Reset Compliance" is a single bare `Taf` command; the firmware does the rest.** The desktop
app's handler just sends one parameterless `Taf` (`class ResetComplianceCommand : base("Taf")`,
no `CommandArgument` fields) and resets its own in-memory `EventsInQueue` counter — it does **not**
orchestrate per-field clears, so all of the selectivity below is firmware-defined. It's the
device's only erase. Verified live (2026-06-20) by a full before/after read:

*Cleared (zeroed/erased):*
- **Event log** — the compliance/event records (a re-pull returned **0 valid records**, down from
  596 events / 13 sessions).
- **`Tb8`** patient therapy time → `0h00m00s` (was 13h58m08s).
- **Session histogram** (≥8h / 6–8h / 4–6h) → `0 / 0 / 0`.
- App-side only: its `EventsInQueue` display counter → 0 (not a device effect).

*Preserved (untouched):*
- **`Tbc`** blower runtime → unchanged (`14h41m59s`) — a **lifetime hardware/motor-hours counter**,
  not resettable from the UI (`Tb8` is the resettable patient/compliance figure).
- **Prescription** (min/max/start) and **comfort** (EZEX, ramp duration, GentleRise pressure).
- **`ConfigurationData`** (incl. the `F` latch, still `1`) and **`Reserved`** — so the `F` latch is
  *config-modified* state, not compliance state; a compliance reset does **not** clear it
  (presumably only a factory reset would). Calibration offset (`Tb3`) is preserved too.

**Reset taxonomy on this device** (three distinct "resets", from least to most destructive):

| Reset | How | Clears | Keeps |
|-------|-----|--------|-------|
| **Button reset** | hold power button until LEDs stop flashing (104347 troubleshooting) | error/alarm conditions only — reboots to Standby | everything else (config, data, `Tbc`, `F`) |
| **Reset Compliance** | app / `Taf` | event log, `Tb8`, session histogram | config, calibration, `Tbc` (lifetime), `F` latch |
| **Factory reset** | Somnetics support only (104378 p.18) | unobserved — presumably config + the `F` latch | — |

The button reset was tested live (2026-06-20): it left the entire config/data baseline byte-for-byte
identical. There is **no user-accessible factory reset**, confirmed three ways: (1) the **serial
command set contains no factory-reset/restore-defaults command** — the full `PAPCommand` set
enumerated from the decompile is the getters + `T11` (push blower), `T00` (no-op), `Tcc`/`Tac`
(write config), `Tb4` (write calibration), and `Taf` (reset compliance), nothing else; (2) the
Micro 510 manual's only "reset" is the power-button soft reset above; (3) the mobile-app guide
says to factory-reset "by contacting Transcend Support." So clearing the `F` latch (and reverting
the prescription to factory defaults) can't be triggered or observed without Somnetics.

### Bug in the official desktop app: it under-reports the APAP minimum

The **Windows desktop app (`Somnetics.TranscendGo.Client` v1.1.2.0) displays a wrong, too-low
minimum pressure** — it showed `10` for a device whose real minimum was 11, then 12, then 13.
Start and max display correctly; only the minimum is wrong, it never tracks the real value, and
it's independent of the clinician lock. **Root-caused in the decompiled `…Business` settings
view-model — an initialization-order bug**, not a parse failure:

- The default backing field is `private double _startingTherapyPressure = 10.0;`.
- On config load (the `Get…ConfigurationCommand` handler), fields are assigned in this order:
  `MinimumTherapyPressure = command.MinimumTherapyPressure;` **then** (a line later)
  `StartingTherapyPressure = command.StartingTherapyPressure;`.
- The `MinimumTherapyPressure` setter clamps the min so it can't exceed the start:
  `if (value > _startingTherapyPressure) value = _startingTherapyPressure;`.
- But at the moment the min is assigned, `_startingTherapyPressure` **still holds the default
  `10.0`** (start hasn't loaded yet). So any real min > 10 is clamped down to 10, and the min is
  never recomputed after the real start loads.

This reproduces every symptom: real min 11/12/13 all show as `10`; start/max are fine; it ignores
the lock; and it re-clamps on every reopen (so editing the min + Update + reopen reverts to 10).
Confirmed live: setting the device to min 13 / start 14 over serial made the desktop app show
`min 10 / start 14`, exactly as the code predicts, while the **BLE/MySleepDash app and this
toolkit's serial read both showed the correct 13**. Takeaway: for the Micro 510, **don't trust
the desktop app's minimum** — the device, the serial read, and the mobile app are authoritative.

**It's a *cold-load-only* bug.** The clamp only misfires while `_startingTherapyPressure` still
holds its default — i.e. on the first config load. An in-session **re-read** (after `start` has
been populated with the real value) clamps `min` against the real start and shows it correctly.
Confirmed live: after a "Reset Compliance" the app re-read the config and the minimum **corrected
itself** to the real value, because by then `_startingTherapyPressure` was already 12, not 10.

## Download algorithm (from `TranSyncManager.GetEventStrings`)
1. `Ta8` → `address`.
2. `Ta9` with (StartAddress=address, NumBytes=50)  — primes/reads the 50-byte header region.
3. `nextStart = address + 50`, `readSize = 1000`. Loop:
   - `Ta9` with (StartAddress=nextStart, NumBytes=readSize) → CompData hex string.
   - Split CompData into **10-hex-char (5-byte) records**. Discard records that are all `f`
     (0xFF… = empty/erased flash).
   - If number of valid records == `readSize/5` (== 200) the block was full → `nextStart += readSize`, continue.
   - Otherwise stop.

## 5-byte compliance event record (10 hex chars)  — `ComplianceEventFactory.GetEvent`
Let the record be bytes `b0 b1 b2 b3 b4` (hex chars 0..9).

- **word1** = little-endian u16 from hex chars [0:4]  (i.e. swap the two bytes).
  Render as 16-bit binary, MSB first:
  - `year`  = bits[0:7]  (top 7) + 2000
  - `month` = bits[7:11] (4 bits)
  - `day`   = bits[11:16](low 5)
- **word2** = little-endian u16 from hex chars [4:8].
  - `hour`      = bits[0:5]  (top 5)
  - `minute`    = bits[5:11] (6 bits)
  - `eventType` = bits[11:16](low 5)
- **subdata** = hex chars [8:10] = one byte (0–255), scaled per event type (see below).
- Timestamp = `DateTime(year,month,day,hour,minute, UTC)` → converted to local time.

### Event types (`eventType` → name, subdata scale)
```
1  StartTherapy            ×0.1   (cmH2O)
2  EndTherapy              ×1.0
5  RampStart               ×1.0   subdata = ramp start pressure ×10 (40 → 4.0 cmH2O); ÷10 for cmH2O
6  RampEnd                 ×1.0   subdata = 1 (completion flag, not a pressure)
7  LeakReport              ×1.0
8  SupplyVoltage           ×0.1   volts (confirmed: raw 168→16.8 V … 126→12.6 V — a 4S Li-ion
                                  rail, 14.8 V nominal / 16.8 V full / ~12.6 V low). Measures the
                                  battery/system rail, NOT the 19 V adapter input, so mains does
                                  NOT read ~190. Power source from the SHAPE, not an absolute
                                  level: declining across a night (e.g. 16.8→14.7) = battery
                                  discharge; flat ~14.5 V held for hours = float-held on mains.
9  ApneaDetected           ×1.0
10 HypopneaDetected        ×1.0
11 PressureReduced         ×0.1
12 PressureAverage         ×0.1
13 MinimumPressureSetting  ×0.1
14 MaximumPressureSetting  ×0.1
15 EZEXLevel               ×0.1
16 MinimumPressureUsed     ×0.1
17 MaximumPressureUsed     ×0.1
18 FlowLimitedRatio        ×0.1
19 SnoringRatio            ×0.1
20 MinimumLeak             ×1.0
21 MaximumLeak             ×1.0
22 AverageLeak             ×1.0
23 PressureIncreasedFromApneas            ×0.1
24 PressureIncreasedFromHypopneas         ×0.1
25 PressureIncreasedFromCombination       ×0.1
26 PressureIncreasedFromSnoring           ×0.1
27 PressureIncreasedFromFlowLimitedBreathing ×0.1
28 PressureIncreasedFromCommand           ×0.1
(other) Other
```

### When each event is logged (event → phase)
Each event class is tagged with a phase in the decompiled `ComplianceEventFactory`, which
tells you **when** in a session it is written — important for interpreting the stats:

| Phase | Events | Meaning |
|-------|--------|---------|
| **Start of session** | `1` StartTherapy, `13` MinPressure**Setting**, `14` MaxPressure**Setting**, `15` EZEXLevel | a prescription snapshot at mask-on |
| **Periodic (~5 min)** | `12` PressureAverage, `22` AverageLeak | sampled through the night → genuine time series |
| **Detailed compliance** | `11` PressureReduced, `23`–`28` PressureIncreasedFrom* | logged when APAP changes pressure (with the reason) |
| **End of session** | `2` EndTherapy, `16` MinPressure**Used**, `17` MaxPressure**Used**, `18` FlowLimitedRatio, `19` SnoringRatio, `20` MinLeak, `21` MaxLeak | one-per-session summary at mask-off |

**Consequence:** `FlowLimitedRatio` and `SnoringRatio` (and the Min/Max **Used**/**Leak**
values) are **single whole-night summary numbers**, *not* time series — there is exactly one
of each per session, at its end. Only `PressureAverage`/`AverageLeak` (and the pressure-change
events) are sampled over time.

**Ramp event pairing caveats** (vendor manuals 104214 p.4, 104143 pp.14, 17–18):
pressing the ramp button when a ramp is configured-but-inactive **starts a ramp
mid-session**, so a session can contain multiple `RampStart`/`RampEnd` pairs; and
press-and-hold *accelerates* a running ramp to therapy pressure — still logged as a
completion (there is no abort-to-zero, consistent with `RampEnd` subdata always = 1).
Session boundaries can also appear without button presses: **SleepStart** auto-starts
therapy on breathing into the mask, therapy **auto-restarts after a power failure**,
and gross leak can trigger an auto-shutdown (104347 pp.13, 20).

## How the official app computes its numbers (for parity)
Recovered from the decompiled compliance/charting view-models — match these to reproduce the
app's figures exactly:

- **Session** = one `StartTherapy`→`EndTherapy` pair (events time-sorted, `SupplyVoltage`
  skipped, orphans before a start dropped, kept only if end ≥ start). No min-length filter.
- **Day assignment** = `hour ≥ cutoffHour ? date : date−1` (cutoff is user-configurable;
  the desktop app **ships defaulting to midnight** — 104207 pp.3–4 — so official-app daily
  numbers differ from this repo's `resmed_day()` noon split unless the cutoff is changed;
  at **noon** the two are identical).
- **Averages are time-weighted by minutes**: `AvgPressure = ΣpressureMin⁻¹·Σ(pressure·min)`
  i.e. `TotalPressure/TotalPressureMinutes`; same for leak. (Not a plain mean of samples.)
- **Percentiles are nearest-rank, no interpolation**: desktop uses `sorted[round(p·n)−1]`
  (round-half-up), the mobile report uses `sorted[ceil(p·n)−1]`. Leak P95/P90 over the
  `AverageLeak` samples; pressure P95/P90 over the pressure samples (then ÷10).
- **AHI** = `(apneas + hypopneas) / hours`, rounded 2 dp away-from-zero; **AI**/**HI** the same.
- **% time in apnea** = `Σ(apneaDurationSec) / (hours·3600) · 100` — which confirms the
  **apnea/hypopnea subdata is a duration in seconds**.
- **Pressures are stored ×10 internally** (event subdata already decoded with the ×0.1 scale).
- **AHI denominator is therapy time** (patient hours, `Tb8`-style), not blower time
  (104207 p.7: "events per therapy time in hours").
- **Compliance buckets**: days ≥4 h, 4–6 h, 6–8 h, ≥8 h; `%≥4h = daysWith4h / daysInRange·100`.
  The reportable 30-day rule is **4+ hours on 70% of nights** (104207 p.8).
- **Device type** comes from the **serial's first char** (`A`/`B`/`C`), *not* the `Tff` code
  (`8011` is an unused opaque value).

## Cloud sync (TranSync) — the official app uploads your data
The Windows app's `TranSyncManager` POSTs JSON to **`https://api.mytransync.com`**:

| Endpoint | Body | Purpose |
|----------|------|---------|
| `POST /deviceevents/post` | `{SerialNumber, EmailAddress, Prescription{EZEX,Min,Max,RampPressure,RampTime}, Events[]}` | upload the raw event strings + your prescription |
| `POST /deviceevents/fetch` | `{SerialNumber, EmailAddress}` | download a device's stored data |

`ServiceWrapper.CallRemote` sends only an `Accept: application/json` header — **no API key,
token, or Authorization of any kind.** From the client side the data is identified by
**serial + email alone** (any server-side protection is not visible in the client). This is
the official app's behavior; **this toolkit never contacts any server — it is fully local.**

## Capabilities that are Bluetooth/iOS-only (not on the USB serial link)
- **Firmware version** (e.g. `1.6.0`) — the serial protocol exposes only a firmware
  *checksum* (`Tbd`); the readable version is shown by the iOS app over Bluetooth.
- **Firmware update** — performed by the iOS app over Bluetooth; there is **no** firmware-
  update path in the Windows app or over USB serial.
- **Dry mode** enable, and assorted app preferences (e.g. compliance cut-off hour) — set
  app-side, never written to a device register these commands can read.
