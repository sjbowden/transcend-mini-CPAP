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
`Tb8`, calibration `Tb3`, reset compliance `Taf`, …) but are not needed to pull the event log.

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

**iOS-app names** for the user-changeable fields (the app gates the prescription pressures):
`EZEX` = **AirRelief** (0–3); `StartingRampPressure` = **GentleRise Pressure** (4–10 cmH₂O);
`RampDurationMinutes` = **GentleRise Duration** (0 = disabled, to 45 min in 5‑min steps).

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
5  RampStart               ×1.0
6  RampEnd                 ×1.0
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
