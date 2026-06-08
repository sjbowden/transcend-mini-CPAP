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

Other commands exist (config `Tab`/`Rab`, pressure `Ta1`/`R41`, monitor `Ta3`, flow `Tc3`,
patient hours `Tb8`, calibration `Tb3`, reset compliance `Taf`, …) but are not needed to
pull the event log.

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
