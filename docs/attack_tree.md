# SOMNI‑Guard Attack Tree

> **Educational prototype — not a clinically approved device.**
> This attack tree is produced as an academic exercise in medical‑device
> threat modelling.  It does not represent a complete security assessment
> of any real clinical system.

---

## 1. Notation

| Symbol | Meaning |
|--------|---------|
| **G**  | Goal node (internal node) |
| **A**  | Attack leaf (terminal action an attacker takes) |
| **AND** | All child nodes must be achieved for the parent to succeed |
| **OR**  | Any one child node is sufficient for the parent to succeed |

---

## 2. Text Representation

```
G0 (OR): Misdiagnose SOMNI‑Guard patient condition
├── G1 (OR): Tamper with telemetry (Pico → Pi 5)
│   ├── G1.1 (AND): Replay valid telemetry to mask a real apnea event
│   │   ├── A1.1.1  Capture a segment of valid telemetry from the wire
│   │   ├── A1.1.2  Craft a replay packet with a forged timestamp
│   │   └── A1.1.3  Inject replay packet into the ingestion channel
│   ├── G1.2 (OR): Wi‑Fi / TCP‑layer attacks (future Wi‑Fi phase)
│   │   ├── A1.2.1  ARP spoofing to redirect Pico→Pi 5 traffic
│   │   ├── A1.2.2  Wi‑Fi de‑auth / DoS to drop telemetry packets
│   │   ├── A1.2.3  TCP SYN flood to exhaust ingestion‑service connections
│   │   └── A1.2.4  TCP session hijack (sequence‑number prediction)
│   └── G1.3 (OR): Physical sensor tampering
│       ├── A1.3.1  Remove sensor from patient → valid=False; attacker
│       │           exploits lack of alert to suppress apnea detection
│       └── A1.3.2  Replace sensor with signal generator injecting
│                   normal‑looking waveform during actual apnea
│
├── G2 (OR): Tamper with stored data / reports on Pi 5
│   ├── G2.1 (AND): Direct database modification via remote shell
│   │   ├── A2.1.1  Exploit a vulnerability in the ingestion service
│   │   │           or Flask app to gain OS shell access
│   │   └── A2.1.2  Run SQL UPDATE/DELETE statements against
│   │               the SQLite database to alter telemetry or reports
│   ├── G2.2 (AND): Physical SD‑card removal and offline modification
│   │   ├── A2.2.1  Obtain physical access to the Pi 5 and remove SD card
│   │   └── A2.2.2  Mount the partition offline, decrypt (if LUKS key
│   │               is known or not set), and modify database or report files
│   ├── G2.3 (AND): Key‑store compromise leading to forged report signatures
│   │   ├── A2.3.1  Extract the HMAC signing key from the Pi 5 filesystem
│   │   │           (e.g., via shell access or SD removal)
│   │   └── A2.3.2  Use the extracted key to sign a tampered report,
│   │               making it appear authentic
│   └── G2.4 (OR): Bypass report‑integrity check
│       ├── A2.4.1  Delete the signature file / field so the checker
│       │           skips validation (if checker treats missing = pass)
│       └── A2.4.2  Exploit a bug in the signature‑verification logic
│                   (e.g., length‑extension, timing side‑channel)
│
└── G3 (OR): Misuse or compromise Web Dashboard
    ├── G3.1 (OR): Authentication bypass
    │   ├── A3.1.1  Brute‑force the login form (weak password / no lockout)
    │   └── A3.1.2  Phishing: trick the clinician into visiting a fake
    │               login page to harvest credentials
    ├── G3.2 (OR): Client‑side injection
    │   ├── A3.2.1  Stored XSS: inject a malicious script into a session
    │   │           name field that is rendered unescaped in the dashboard
    │   └── A3.2.2  CSRF: trick the authenticated clinician's browser into
    │               sending a state‑changing request (e.g., delete session)
    └── G3.3 (OR): Server‑side exploitation
        ├── A3.3.1  SQL injection via an unparameterised query in the
        │           Flask route that fetches session telemetry
        └── A3.3.2  Buffer overflow / memory‑corruption in a C extension
                    or underlying library used by the Flask application
```

---

## 3. Graphviz DOT Representation

See `docs/attack_tree.dot` for the Graphviz source.  Render with:

```bash
dot -Tsvg docs/attack_tree.dot -o docs/attack_tree.svg
dot -Tpng docs/attack_tree.dot -o docs/attack_tree.png
```

---

## 4. Alignment with Assets

| Attack path | Asset(s) targeted |
|-------------|------------------|
| G1.1 (replay) | A1 (Pico), A2 (transport) |
| G1.2 (Wi‑Fi/TCP) | A1, A2 |
| G1.3 (physical sensor) | A5, A1 |
| G2.1 (remote shell → DB) | A2, A4 |
| G2.2 (SD removal) | A2, A4 |
| G2.3 (key compromise) | A2, A4 |
| G2.4 (signature bypass) | A4 |
| G3.1 (auth bypass) | A3 |
| G3.2 (XSS/CSRF) | A3, A4 |
| G3.3 (SQLi/overflow) | A3, A4 |

---

## 5. G4 — Compromise Tailscale Mesh (new sub-tree)

```
G4 (OR): Compromise Tailscale mesh to bypass TB5
├── G4.1 (AND): Enrol a rogue device into the tailnet
│   ├── A4.1.1  Steal the Tailscale account credentials (phishing /
│   │           credential stuffing against tailscale.com)
│   └── A4.1.2  Authenticate a rogue device with the stolen account →
│               rogue node receives 100.x.x.x IP → can reach Pi 5
│
├── G4.2 (OR): Misconfigured Tailscale ACL grants excess access
│   ├── A4.2.1  Default "allow all" ACL left in place → every tailnet
│   │           node (not just clinical staff) can reach port 5000
│   └── A4.2.2  Incorrect tag assignment → untrusted device receives
│               somni-clinician or somni-dev tag and is permitted access
│
├── G4.3 (AND): Tailscale coordination-server supply-chain attack
│   ├── A4.3.1  Tailscale Inc. infrastructure compromised → attacker
│   │           can inject rogue WireGuard public keys into tailnet
│   └── A4.3.2  Rogue keys allow MITM of WireGuard tunnels →
│               intercept or tamper with dashboard traffic
│
└── G4.4 (AND): WireGuard key material theft on the Pi 5
    ├── A4.4.1  Attacker gains OS shell on Pi 5 (via G2.1 path above)
    └── A4.4.2  Read /var/lib/tailscale/tailscaled.state → extract
                node private key → impersonate Pi 5 on the tailnet
```

### 5.1 Alignment with Assets and PHA

| Attack path | Asset(s) targeted | Hazard(s) |
|-------------|------------------|-----------|
| G4.1 (rogue enrolment) | A6 (tailnet), A3 (dashboard), A4 (data) | H-07 |
| G4.2 (ACL misconfiguration) | A6, A3, A4 | H-07 |
| G4.3 (supply-chain) | A6 | H-07 |
| G4.4 (key theft on Pi 5) | A6, A2 | H-04, H-07 |

### 5.2 Mitigations for G4

| Attack | Mitigation |
|--------|-----------|
| G4.1 (credential theft) | Enable 2FA on Tailscale account; use Tailscale device keys with short expiry |
| G4.2 (ACL misconfiguration) | Apply tag-based ACL (see tailscale_setup.md §7); audit ACL in Tailscale admin console |
| G4.3 (supply-chain) | Accept residual risk; monitor Tailscale security advisories |
| G4.4 (key file theft) | Requires prior OS shell — mitigated by L2-C6 (least-privilege); LUKS2 |

