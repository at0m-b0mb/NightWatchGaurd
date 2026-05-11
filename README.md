# SOMNI-Guard

Educational sleep-monitoring prototype with a secure Raspberry Pi 5 gateway
and a Pico 2 W sensor. The gateway runs a Flask + gunicorn dashboard with
mandatory two-factor authentication, threshold-based clinical alerts, a
live multi-patient monitor, full patient / device / audit management, and a
TLS + HMAC + replay-protected REST API for telemetry ingestion.

**Repo:** https://github.com/at0m-b0mb/NightWatchGaurd

## Quick install (gateway)

```bash
git clone https://github.com/at0m-b0mb/NightWatchGaurd.git
cd NightWatchGaurd
sudo bash scripts/setup_gateway_pi5.sh
```

> **All install, configuration, security and operator documentation lives
> in one place: [GUIDE.md](GUIDE.md).**

---

⚠️ Educational prototype — **not** a clinically approved medical device.
