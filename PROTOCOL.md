# Transcend miniCPAP — USB serial protocol (reverse-engineered)

Device: Somnetics Transcend (FTDI FT231X bridge, `VID_0403 PID_6015`).
Source: decompiled `Somnetics.TranscendGo.*` assemblies (TranSyncGo client).
Validated live against a real device on 2026-06-07.

See [README.md](README.md) for the full toolchain (download → parse → SleepHQ upload).

> **What the log contains:** discrete, time-stamped *events* and summary stats —
> therapy start/stop, pressure changes, apneas/hypopneas, leak, snore/flow-limit ratios.
> **What it does NOT contain:** any continuous waveform — no flow rate, mask-pressure
> trace, respiratory rate, tidal volume, or minute ventilation. The Transcend is a
> compliance/event recorder, not a full data-logger, so those channels are simply
> unavailable (this is also why OSCAR never supported detailed Transcend graphs).

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
> accepts config writes with **no authentication**. `settings.py` therefore imposes the
> boundary itself (comfort settings free; prescription pressures behind a flag).

**AutoPAP / EZEX config args** (positional, big-endian hex; read scales shown, write = inverse):

| Pos | Field | Len | Read | Write |
|-----|-------|-----|------|-------|
| 0 | StartingTherapyPressure | 4 | hex/10 | int(v·10)→hex |
| 1 | **ConfigurationData** | 15 | opaque | passthrough |
| 2 | MinimumTherapyPressure | 4 | hex/10 | int(v·10)→hex |
| 3 | MaximumTherapyPressure | 4 | hex/10 | int(v·10)→hex |
| 4 | **Reserved** | 5 | opaque | passthrough |
| 5 | RampDurationMinutes | 4 | hex | int→hex |
| 6 | EZEX (relief level) | 4 | hex/10 | int(v·10)→hex |
| 7 | StartingRampPressure | 4 | hex/10 | int(v·10)→hex |

(StandardCPAP layout: StartingTherapyPressure(4), ConfigurationData(28), RampDuration(4),
Reserved(4), StartingRampPressure(4) — no min/max.) Hex is uppercase, left-padded to the
field width (`CommandArgumentFormatter`: `int(value·scale).ToString("X")`). The two opaque
fields (`ConfigurationData`, `Reserved`) **must be written back verbatim**. They appear to
be a **factory/firmware‑fixed** block (`ConfigurationData` carries an `aa55` magic marker):
the iOS app exposes only the named fields below, so no user setting changes them, and they
can't be bit‑mapped by diffing. Calibration `Tb4` is writable but **must not be touched**
(it recalibrates the pressure sensor).

**App names** for the user-changeable fields. The field names above follow the **Windows**
app (`EZEX`, `Ramp`) because they come from decompiling it; the **iOS** app uses friendlier
labels for the same settings (both apps gate the prescription pressures):
`EZEX` = **AirRelief** (0–3); `StartingRampPressure` = **GentleRise Pressure** (4–10 cmH₂O);
`RampDurationMinutes` = **GentleRise Duration** (0 = disabled, to 45 min in 5‑min steps).

**Not every device feature is on the serial link.** *Dry mode* (a post-therapy option that
runs the blower to dry the tube and mask) has no *setting* visible here: toggling it on/off
changes neither the config (`Tab`), the device state (`T6d`), nor the live monitor (`Ta3`)
while idle, and it never touches the blob — the enable flag is app/Bluetooth-side. But the
drying *cycle itself* is wire-visible after the fact: running it advanced the **blower-time**
counter `Tbc` (+2m19s in one test) while the **patient-time** counter `Tb8` stayed put. This
is the general rule for the two counters — `Tbc` = all blower runtime (ramp + mask-off + dry
cycles), `Tb8` = actual breathing only — which is why blower time exceeds patient time.

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
8  SupplyVoltage           ×1.0
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

## How the official app computes its numbers (for parity)
Recovered from the decompiled compliance/charting view-models — match these to reproduce the
app's figures exactly:

- **Session** = one `StartTherapy`→`EndTherapy` pair (events time-sorted, `SupplyVoltage`
  skipped, orphans before a start dropped, kept only if end ≥ start). No min-length filter.
- **Day assignment** = `hour ≥ cutoffHour ? date : date−1` (default cutoff is configurable;
  at **noon** this equals this repo's `resmed_day()` noon split exactly).
- **Averages are time-weighted by minutes**: `AvgPressure = ΣpressureMin⁻¹·Σ(pressure·min)`
  i.e. `TotalPressure/TotalPressureMinutes`; same for leak. (Not a plain mean of samples.)
- **Percentiles are nearest-rank, no interpolation**: desktop uses `sorted[round(p·n)−1]`
  (round-half-up), the mobile report uses `sorted[ceil(p·n)−1]`. Leak P95/P90 over the
  `AverageLeak` samples; pressure P95/P90 over the pressure samples (then ÷10).
- **AHI** = `(apneas + hypopneas) / hours`, rounded 2 dp away-from-zero; **AI**/**HI** the same.
- **% time in apnea** = `Σ(apneaDurationSec) / (hours·3600) · 100` — which confirms the
  **apnea/hypopnea subdata is a duration in seconds**.
- **Pressures are stored ×10 internally** (event subdata already decoded with the ×0.1 scale).
- **Compliance buckets**: days ≥4 h, 4–6 h, 6–8 h, ≥8 h; `%≥4h = daysWith4h / daysInRange·100`.
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
