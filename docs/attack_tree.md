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
