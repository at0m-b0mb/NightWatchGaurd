# SOMNI‑Guard Preliminary Hazard Analysis (PHA)

> **Educational prototype — not a clinically approved device.**
> This PHA is produced as an academic exercise aligned with IEC 62304 /
> ISO 14971 risk‑analysis concepts.  Severity and likelihood scores are
> educational estimates only.

**Severity scale (S)**

| Level | Label | Description |
|-------|-------|-------------|
| 4 | Catastrophic | Death or severe permanent harm |
| 3 | Critical | Serious injury requiring medical intervention |
| 2 | Marginal | Minor injury or treatment delay |
| 1 | Negligible | Inconvenience; no clinical impact |

**Likelihood scale (L)**

| Level | Label | Description |
|-------|-------|-------------|
| 4 | Frequent | Likely to occur many times in device lifetime |
| 3 | Probable | Will occur several times |
| 2 | Occasional | Might occur once or twice |
| 1 | Remote | Unlikely but conceivable |

**Risk priority (R = S × L)**

| R | Priority |
|---|---------|
| 12–16 | High — must mitigate before deployment |
| 6–9   | Medium — mitigate or accept with documented rationale |
| 1–4   | Low — accept or monitor |

---

## PHA Table

### H‑01: Misdiagnosed sleep‑apnea severity due to under‑counted desaturations

| Field | Detail |
|-------|--------|
| **Hazard ID** | H‑01 |
| **Hazard** | Sleep‑apnea severity under‑counted; clinician under‑treats patient |
| **Cause chain** | Attack path G1.1 (telemetry replay) or G1.3.2 (sensor replaced by signal generator) → gateway receives normal‑looking SpO₂ values during true desaturation events → feature extractor counts zero desaturations → report indicates mild/no apnea |
| **Assets affected** | A1, A5 (tampered); A4 (report corrupted) |
| **Severity (S)** | 3 — Critical (under‑treatment of sleep apnea can lead to cardiovascular events) |
| **Likelihood (L)** | 2 — Occasional (requires physical or network access) |
| **Risk (R)** | 6 — Medium |
| **Mitigations** | (1) HMAC on telemetry packets (future phase) detects replay. (2) Firmware validity flags alert on sensor removal (A1.3.1 partially mitigated). (3) Report signing detects post‑hoc DB tampering. (4) Plausibility checks on SpO₂ (reject values outside 70–100 %). |

---

### H‑02: Misdiagnosed sleep‑apnea severity due to over‑counted desaturations

| Field | Detail |
|-------|--------|
| **Hazard ID** | H‑02 |
| **Hazard** | False desaturation events inflate apnea index; clinician over‑treats patient |
| **Cause chain** | Poor electrode contact or motion artefact in MAX30102 → raw IR/Red counts noisy → educational SpO₂ approximation yields falsely low values → feature extractor records desaturation |
| **Assets affected** | A5 (signal artefact); A1 (firmware); A4 (report) |
| **Severity (S)** | 2 — Marginal (over‑treatment with CPAP rarely causes serious harm in this context) |
| **Likelihood (L)** | 3 — Probable (motion artefact during sleep is common) |
| **Risk (R)** | 6 — Medium |
| **Mitigations** | (1) IR "no‑finger" threshold in firmware (config.SPO2_IR_MIN_VALID) — reading marked invalid=True if IR too low. (2) Dashboard clearly labels data as non‑clinical. (3) Future phase: motion‑artefact rejection using concurrent ADXL345 data. |

---

### H‑03: False arousal events due to tampered accelerometer data

| Field | Detail |
|-------|--------|
| **Hazard ID** | H‑03 |
| **Hazard** | Arousal index inflated or deflated; sleep architecture report is inaccurate |
| **Cause chain** | Attack path G2.1 or G2.2 → attacker modifies stored accelerometer telemetry rows → feature extractor counts wrong number of movement events → report misstates sleep fragmentation |
| **Assets affected** | A4 (telemetry rows); A2 (DB on Pi 5) |
| **Severity (S)** | 2 — Marginal |
| **Likelihood (L)** | 2 — Occasional |
| **Risk (R)** | 4 — Low |
| **Mitigations** | (1) SQLCipher encryption makes offline modification harder. (2) LUKS2 on SD card. (3) Report HMAC signing detects post‑hoc changes to the final report (but not individual telemetry rows — future: per‑row checksums). |

---

### H‑04: Patient data confidentiality breach

| Field | Detail |
|-------|--------|
| **Hazard ID** | H‑04 |
| **Hazard** | Physiological data disclosed to unauthorised party |
| **Cause chain** | Attack path G3.1 (brute‑force/phishing) or G2.2 (SD removal) → attacker gains access to all session data and reports |
| **Assets affected** | A3 (dashboard); A4 (DB); A2 (Pi 5) |
| **Severity (S)** | 3 — Critical (privacy harm; regulatory implications in real device) |
| **Likelihood (L)** | 2 — Occasional |
| **Risk (R)** | 6 — Medium |
| **Mitigations** | (1) Dashboard bound to localhost only. (2) bcrypt password hashing + rate‑limited login. (3) LUKS2 + SQLCipher for data at rest. (4) HTTPS for any future LAN access. |

---

### H‑05: Dashboard displays wrong session / corrupted report

| Field | Detail |
|-------|--------|
| **Hazard ID** | H‑05 |
| **Hazard** | Clinician reviews the wrong patient's data or a visually plausible but tampered report |
| **Cause chain** | Attack path G3.2.1 (stored XSS) → attacker‑controlled script manipulates DOM to swap session labels or alter displayed values without changing the DB |
| **Assets affected** | A3 (dashboard) |
| **Severity (S)** | 3 — Critical (potential misdiagnosis) |
| **Likelihood (L)** | 2 — Occasional |
| **Risk (R)** | 6 — Medium |
| **Mitigations** | (1) Jinja2 auto‑escaping on all template variables. (2) Content‑Security‑Policy header disabling inline scripts. (3) Report HMAC displayed alongside summary so clinician can verify. |

---

### H‑06: Device unavailability during sleep session (DoS)

| Field | Detail |
|-------|--------|
| **Hazard ID** | H‑06 |
| **Hazard** | Telemetry not recorded for part of the night; session report is incomplete |
| **Cause chain** | Attack path G1.2.2 (Wi‑Fi de‑auth) or G1.2.3 (TCP SYN flood) → ingestion service drops packets → gaps in telemetry → feature extractor cannot detect events during gap |
| **Assets affected** | A1, A2, A4 |
| **Severity (S)** | 2 — Marginal (incomplete session, not a misread) |
| **Likelihood (L)** | 2 — Occasional |
| **Risk (R)** | 4 — Low |
| **Mitigations** | (1) Pico buffers telemetry locally (RingBuffer in utils.py) and retransmits on reconnect (future phase). (2) Gateway detects gaps in timestamps and flags the report as "incomplete". (3) Rate‑limiting on ingestion port mitigates SYN flood. |

---

## PHA ↔ Attack Tree Alignment Summary

| Hazard | Attack path(s) | Assets |
|--------|---------------|--------|
| H‑01 | G1.1, G1.3.2 | A1, A4, A5 |
| H‑02 | (artefact, no attack) | A1, A4, A5 |
| H‑03 | G2.1, G2.2 | A2, A4 |
| H‑04 | G3.1, G2.2 | A2, A3, A4 |
| H‑05 | G3.2.1 | A3 |
| H‑06 | G1.2.2, G1.2.3 | A1, A2, A4 |

---

### H‑07: Unauthorised dashboard access via Tailscale misconfiguration or rogue peer

| Field | Detail |
|-------|--------|
| **Hazard ID** | H-07 |
| **Hazard** | An unauthorised device joins the Tailscale tailnet and accesses the SOMNI-Guard web dashboard, viewing or altering patient data |
| **Cause chain** | Attack path G4.1 (Tailscale credentials stolen → rogue device enrolled) or G4.2 (ACL left at default "allow all") → device receives 100.x.x.x IP → passes TAILSCALE_ONLY check → reaches Flask login page → brute-forces or phishes credentials → reads / modifies patient reports |
| **Assets affected** | A6 (tailnet), A3 (dashboard), A4 (patient data) |
| **Severity (S)** | 3 — Critical (patient data confidentiality breach; potential misdiagnosis if reports are altered) |
| **Likelihood (L)** | 2 — Occasional (requires credential theft OR ACL misconfiguration; Tailscale account is a separate authentication layer) |
| **Risk (R)** | 6 — Medium |
| **Mitigations** | (1) Enable 2FA on the Tailscale account (primary control). (2) Apply tag-based ACL to restrict port 5000 access to `tag:somni-clinician` and `tag:somni-dev` nodes only. (3) Flask authentication (bcrypt) remains required even inside the tailnet — defence in depth. (4) Monitor Tailscale admin console for unexpected device enrolments. (5) Set short device-key expiry in Tailscale settings so stale devices are automatically de-authorised. |

---

## PHA ↔ Attack Tree Alignment Summary (updated)

| Hazard | Attack path(s) | Assets |
|--------|---------------|--------|
| H-01 | G1.1, G1.3.2 | A1, A4, A5 |
| H-02 | (artefact, no attack) | A1, A4, A5 |
| H-03 | G2.1, G2.2 | A2, A4 |
| H-04 | G3.1, G2.2, G4.4 | A2, A3, A4 |
| H-05 | G3.2.1 | A3 |
| H-06 | G1.2.2, G1.2.3 | A1, A2, A4 |
| H-07 | G4.1, G4.2, G4.3, G4.4 | A3, A4, A6 |
