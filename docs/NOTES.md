# Vendor documentation — distilled notes

Facts extracted from the official Somnetics PDFs (reviewed 2026-06-10). The PDFs
themselves are **git-ignored** (Somnetics copyright — obtain from mytranscend.com);
this file keeps the project-relevant content with document numbers and page cites.

**The device this repo targets is a Somnetics Transcend Micro 510.** The desktop
software is called "mini" / "mini PAP" (its install icon is literally "MiniCPAP"),
which is where this repo's historical "miniCPAP" naming came from. One desktop app
serves the whole Transcend family — the Micro guides reuse screenshots showing a
B-prefix serial (AutoPAP class, this repo's serial taxonomy).

## Documents reviewed

| Doc | Title | Notes |
|---|---|---|
| 104347 Rev B (2026-02) | Transcend Micro 510 User Manual (41 pp) | device specs |
| 104214 Rev A (2022-05) | Transcend Micro Clinician Quick Guide (13 pp) | settings, password |
| 104207 Rev B (2026-01) | Transcend Desktop Software User Guide (12 pp) | stats definitions |
| 104378 Rev A (2026-05) | My SleepDash Mobile App manual (21 pp) | BLE app, log capacity |
| 104143 Rev F (2023-06) | Transcend Micro Quick Guide (40 pp, English) | buttons, LEDs |
| — | Desktop Software Install Guide (1 p) | "MiniCPAP" icon |
| — | Micro Driver Install Instructions (3 pp) | FTDI CDM driver |

## Highest-value facts

- **On-device log capacity: 3–6 months** of compliance data depending on use
  (104378 p.7). Download at least every ~3 months or the oldest nights are lost.
- **Clinician password is `juniper`** (104214 p.7) — a single static string, printed
  in the public PDF. Confirms it gates the desktop app's UI only; the firmware
  accepts config writes with no authentication (PROTOCOL.md).
- **Power rails** (104347 p.25): AC adapters PSA4/PSA5 output **19 VDC** 2.1 A;
  the PowerAway battery (BATM) is **14.8 VDC** 4,800 mAh (4S Li-ion, ~12–16.8 V
  terminal). → Leading hypothesis for SupplyVoltage event 8: **×0.1 V/count**
  (mains ≈ raw 190). Unverified against a dump — see TODO.md.
- **Ramp behavior** (104214 p.4; 104143 pp.14, 17–18):
  - Press-and-hold during a ramp **accelerates** it to therapy pressure (still a
    completion — consistent with RampEnd subdata always = 1; no abort-to-zero exists).
  - Pressing the button when ramp is configured-but-inactive **starts a ramp
    mid-session** → multiple RampStart/RampEnd pairs per session are legitimate.
  - In standby the button toggles **Auto Ramp** (auto-engage at therapy start) —
    a state not present in the serial config layout (app/BLE-side or unmapped).
- **Settings constraints** (104214 p.8; 104207 pp.5–6):
  - StartingRampPressure adjustable **up to 1 cmH₂O below Therapy Pressure**
    (a relative cap on top of the absolute 4–10 from the apps).
  - StartingTherapyPressure lies **between min and max** (already enforced
    by settings.py for APAP).
  - Therapy pressure changes in **0.1 cmH₂O increments** (confirms ×10 storage);
    ramp duration **5–45 min or OFF**; EZEX/AirRelief **OFF, 1–3**.
- **Desktop app's day grouping defaults to MIDNIGHT** ("Time of Day Cut Off",
  user-selectable; 104207 pp.3–4). The converter's noon-to-noon split matches
  SleepHQ/ResMed, but official-app daily numbers will differ unless the cutoff
  is changed.

## Statistics definitions (104207 p.7–8; 104378 p.10)

- **AHI = (apneas + hypopneas) / therapy hours** — denominator is *therapy* time
  (patient hours, `Tb8`-style), not blower time.
- Leak summary: Average, Median, P90, P95 — all **L/min**; no mention of vent
  compensation anywhere (consistent with raw/uncompensated leak).
- "Time in Apnea %" and "Time in Hypopnea %" exist → **hypopnea subdata is also a
  duration in seconds** (corroborates the apnea finding).
- **30-day compliance rule: 4+ hours of therapy on 70% of nights** (Medicare-style).
- The guides define AI/HI as raw *counts* — the decompile says per-hour rates;
  treat the decompile as authoritative (doc sloppiness).

## Device specs (104347)

- Pressure 4–20 cmH₂O, **auto-shutdown above 30 cmH₂O** (p.26). Accuracy: static
  ±0.2 @ 10 cmH₂O; dynamic +1.0/−2.0 cmH₂O or +10%/−20%, whichever greater (pp.26, 31).
- **Altitude 0–8000 ft, compensated automatically** — no user setting, no log field.
- Blower output saturates ≈ **58 L/min** at all pressures (p.32, ISO max-flow table —
  blower capacity, *not* a vent-flow curve; usable only as a leak sanity ceiling).
- **"Ventilator Mode: CPAP"** (p.26) is regulatory classification (not a ventilator/
  bilevel) — it does NOT mean fixed-pressure; the Micro runs auto mode (104214 p.7:
  "Auto mode is only available on Transcend 3 and Transcend Micro devices").
- **SleepStart**: therapy auto-starts on breathing into the mask; therapy
  **auto-restarts after a power failure** (p.13) — both produce session boundaries
  with no button press. Gross leak can trigger auto-shutdown (p.20) → unexpected
  EndTherapy.
- Drying mode runs the blower at low speed for a fixed **30 minutes** (p.14) —
  advances blower time (`Tbc`) but not patient time (`Tb8`).
- Internal coin-cell RTC backup (perchlorate notice, p.17) — why timestamps
  survive unplugging. Design life 5 years; warranty 3 (pp.24, 35).
- USB-C data port (p.4); BLE 5.0 for the MySleepDash app; FAA/DO-160 compliant (p.24).

## Mobile app (My SleepDash, 104378)

- This is the app PROTOCOL.md called "the iOS app" (AirRelief / GentleRise names);
  cross-platform (Android 12+ / iOS 17+), **BLE-only** sync, QR pairing, cloud over
  HTTPS — a much more locked-down successor to the legacy no-auth TranSync API.
- Sleep Score weighting: usage 70% (maxes at 7 h), mask leak 20%, mask on/off 5%,
  AHI 5% (pp.8–9). Compliance report tabs: Standard / Advanced / **FAA**.
- Only AirRelief + GentleRise pressure/duration are user-adjustable; prescription
  pressures display locked (pp.12, 17–18).
- Factory reset is support-mediated only; cloud data deletion via
  support@mytranscend.com (p.19).

## Driver / transport (Micro driver instructions; 104207)

- Micro's driver package is the **FTDI CDM** bundle (ftdibus.inf/ftdiport.inf) —
  the Micro also enumerates as FTDI VCP; a CP210x driver folder appears in a
  screenshot, confirming both bridges exist across revisions (matches PROTOCOL.md).
- Nothing in any PDF documents baud/protocol, log capacity-in-bytes, or that
  downloads clear the log (the only erase shown is the explicit "reset compliance"
  button — consistent with non-destructive `Ta9` reads).
