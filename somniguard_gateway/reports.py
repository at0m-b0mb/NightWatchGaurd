"""
reports.py — SOMNI‑Guard sleep‑report feature extraction and PDF generation.

Two responsibilities:
1. ``compute_summary(session_id)`` — query telemetry, compute aggregate
   metrics (SpO₂ stats, HR stats, desaturation events, movement events,
   GSR stats), return a plain dict.

2. ``generate_pdf(session, summary)`` — render the summary dict plus raw
   telemetry into a ReportLab PDF, save to REPORT_DIR, return the file path.

⚠️  All metrics are non‑clinical educational estimates.  The generated PDF
    contains a prominent disclaimer.

Educational prototype — not a clinically approved device.
"""

import hashlib
import hmac
import json
import math
import os
from datetime import datetime

import config as cfg
import database as db

# ReportLab imports — fail gracefully if not installed so that imports of
# this module don't crash the test suite when ReportLab is absent.
try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )
    _REPORTLAB_AVAILABLE = True
except ImportError:
    _REPORTLAB_AVAILABLE = False


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def compute_summary(session_id):
    """
    Compute aggregate sleep metrics from stored telemetry.

    Metrics computed:
    - SpO₂: min, max, mean, count of desaturation events (< threshold).
    - HR:   min, max, mean (from valid readings).
    - Accel: count of movement events (vector‑magnitude > threshold).
    - GSR:   min, max, mean conductance in µS (from valid readings).
    - Session: total rows, duration estimate from timestamp range.

    Args:
        session_id (int): Session to summarise.

    Returns:
        dict: Summary metrics dict.  All numeric values are rounded to 2 dp.
    """
    rows = db.get_telemetry(session_id)

    spo2_vals, hr_vals, gsr_vals = [], [], []
    desats = 0
    movements = 0
    prev_mag = None

    for r in rows:
        if r["valid_spo2"] and r["spo2"] is not None:
            spo2_vals.append(r["spo2"])
            if r["spo2"] < cfg.DESATURATION_THRESHOLD_PCT:
                desats += 1
        if r["valid_spo2"] and r["hr"] is not None:
            hr_vals.append(r["hr"])
        if r["valid_gsr"] and r["gsr_conductance_us"] is not None:
            gsr_vals.append(r["gsr_conductance_us"])
        if r["valid_accel"] and None not in (r["accel_x"], r["accel_y"], r["accel_z"]):
            mag = math.sqrt(r["accel_x"]**2 + r["accel_y"]**2 + r["accel_z"]**2)
            if prev_mag is not None and abs(mag - prev_mag) > cfg.MOVEMENT_THRESHOLD_G:
                movements += 1
            prev_mag = mag

    def _stats(lst):
        if not lst:
            return {"min": None, "max": None, "mean": None, "count": 0}
        return {
            "min":   round(min(lst), 2),
            "max":   round(max(lst), 2),
            "mean":  round(sum(lst) / len(lst), 2),
            "count": len(lst),
        }

    ts_vals = [r["timestamp_ms"] for r in rows if r["timestamp_ms"] is not None]
    duration_s = (
        round((max(ts_vals) - min(ts_vals)) / 1000.0, 1)
        if len(ts_vals) >= 2
        else 0
    )

    return {
        "session_id":          session_id,
        "total_telemetry_rows": len(rows),
        "duration_s":          duration_s,
        "spo2":                _stats(spo2_vals),
        "hr":                  _stats(hr_vals),
        "gsr":                 _stats(gsr_vals),
        "desaturation_events": desats,
        "movement_events":     movements,
        "generated_at":        datetime.utcnow().isoformat() + "Z",
        "non_clinical_note":   (
            "NON-CLINICAL EDUCATIONAL PROTOTYPE. "
            "Values are approximations and must NOT be used for diagnosis."
        ),
    }


def sign_summary(summary_json):
    """
    Compute HMAC‑SHA256 of a JSON summary string.

    Uses the shared PICO_HMAC_KEY from config.  This allows the dashboard
    to detect if a stored report was tampered with post‑generation.

    Args:
        summary_json (str): JSON string of the summary dict.

    Returns:
        str: Hex‑encoded HMAC‑SHA256 digest.
    """
    key = cfg.PICO_HMAC_KEY.encode("utf-8")
    return hmac.new(key, summary_json.encode("utf-8"), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------

def generate_pdf(session_row, summary):
    """
    Render a sleep‑session summary PDF and save it to REPORT_DIR.

    Args:
        session_row (sqlite3.Row): Session record (joined with patient data).
        summary     (dict):       Output of compute_summary().

    Returns:
        str: Absolute path to the generated PDF file.

    Raises:
        RuntimeError: If ReportLab is not installed.
    """
    if not _REPORTLAB_AVAILABLE:
        raise RuntimeError(
            "ReportLab is not installed. Run: pip install reportlab>=4.2.0"
        )

    os.makedirs(cfg.REPORT_DIR, exist_ok=True)

    session_id   = session_row["id"]
    patient_name = session_row["patient_name"]
    # sqlite3.Row doesn't have .get(); use try/except for optional columns
    try:
        patient_dob = session_row["patient_dob"] or "N/A"
    except (IndexError, KeyError):
        patient_dob = "N/A"
    started_at   = session_row["started_at"]
    ended_at     = session_row["ended_at"] or "ongoing"
    device_id    = session_row["device_id"]

    filename = "somni_report_session_{}_{}.pdf".format(
        session_id,
        datetime.utcnow().strftime("%Y%m%d_%H%M%S"),
    )
    pdf_path = os.path.join(cfg.REPORT_DIR, filename)

    doc    = SimpleDocTemplate(pdf_path, pagesize=A4,
                               rightMargin=2*cm, leftMargin=2*cm,
                               topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        "SomniTitle",
        parent=styles["Title"],
        fontSize=18,
        spaceAfter=6,
        textColor=colors.HexColor("#1a3a5c"),
    )
    h2_style = ParagraphStyle(
        "SomniH2",
        parent=styles["Heading2"],
        fontSize=13,
        spaceBefore=14,
        spaceAfter=4,
        textColor=colors.HexColor("#1a3a5c"),
    )
    disclaimer_style = ParagraphStyle(
        "Disclaimer",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.red,
        backColor=colors.HexColor("#fff3f3"),
        borderPad=4,
        leading=12,
    )
    normal = styles["Normal"]

    story = []

    # ---- Header ----
    story.append(Paragraph("SOMNI‑Guard Sleep Monitoring Report", title_style))
    story.append(Paragraph(
        "⚠️  NON‑CLINICAL EDUCATIONAL PROTOTYPE — NOT FOR DIAGNOSTIC USE",
        disclaimer_style,
    ))
    story.append(Spacer(1, 0.4*cm))

    # ---- Patient / Session info ----
    story.append(Paragraph("Patient & Session Information", h2_style))
    info_data = [
        ["Patient Name:", str(patient_name)],
        ["Date of Birth:", str(patient_dob)],
        ["Device ID:",    str(device_id)],
        ["Session Start:", str(started_at)],
        ["Session End:",  str(ended_at)],
        ["Duration (s):", str(summary.get("duration_s", "N/A"))],
        ["Total Readings:", str(summary.get("total_telemetry_rows", 0))],
    ]
    info_table = Table(info_data, colWidths=[4.5*cm, 11*cm])
    info_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#dce6f1")),
        ("FONTNAME",   (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE",   (0, 0), (-1, -1), 10),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.whitesmoke, colors.white]),
        ("GRID",       (0, 0), (-1, -1), 0.5, colors.HexColor("#aaaaaa")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 0.4*cm))

    # ---- SpO₂ summary ----
    story.append(Paragraph("SpO₂ / Heart Rate Summary (non‑clinical)", h2_style))
    spo2  = summary.get("spo2", {})
    hr    = summary.get("hr",   {})
    vitals_data = [
        ["Metric", "Min", "Max", "Mean", "# Valid Readings"],
        ["SpO₂ (%)",
         _fmt(spo2.get("min")), _fmt(spo2.get("max")),
         _fmt(spo2.get("mean")), str(spo2.get("count", 0))],
        ["Heart Rate (bpm)",
         _fmt(hr.get("min")), _fmt(hr.get("max")),
         _fmt(hr.get("mean")), str(hr.get("count", 0))],
    ]
    vitals_table = Table(vitals_data, colWidths=[5*cm, 2.5*cm, 2.5*cm, 2.5*cm, 3*cm])
    vitals_table.setStyle(_summary_table_style())
    story.append(vitals_table)
    story.append(Spacer(1, 0.3*cm))

    # ---- Events ----
    story.append(Paragraph("Sleep Events (non‑clinical heuristics)", h2_style))
    events_data = [
        ["Event Type", "Count", "Threshold Used"],
        ["Desaturation events (SpO₂ < {}%)".format(
            cfg.DESATURATION_THRESHOLD_PCT),
         str(summary.get("desaturation_events", 0)),
         "SpO₂ < {}%".format(cfg.DESATURATION_THRESHOLD_PCT)],
        ["Movement/Arousal events",
         str(summary.get("movement_events", 0)),
         "Δ|accel| > {}g".format(cfg.MOVEMENT_THRESHOLD_G)],
    ]
    events_table = Table(events_data, colWidths=[8*cm, 2.5*cm, 5*cm])
    events_table.setStyle(_summary_table_style())
    story.append(events_table)
    story.append(Spacer(1, 0.3*cm))

    # ---- GSR summary ----
    story.append(Paragraph("Galvanic Skin Response Summary (non‑clinical)", h2_style))
    gsr = summary.get("gsr", {})
    gsr_data = [
        ["Metric", "Min (µS)", "Max (µS)", "Mean (µS)", "# Valid Readings"],
        ["Conductance",
         _fmt(gsr.get("min")), _fmt(gsr.get("max")),
         _fmt(gsr.get("mean")), str(gsr.get("count", 0))],
    ]
    gsr_table = Table(gsr_data, colWidths=[4*cm, 2.8*cm, 2.8*cm, 2.8*cm, 3*cm])
    gsr_table.setStyle(_summary_table_style())
    story.append(gsr_table)
    story.append(Spacer(1, 0.3*cm))

    # ---- Raw telemetry sample ----
    rows = db.get_telemetry(session_id, limit=50)
    if rows:
        story.append(Paragraph(
            "Raw Telemetry Sample (first {} of {} readings)".format(
                len(rows), summary.get("total_telemetry_rows", 0)
            ),
            h2_style,
        ))
        tel_data = [["t (ms)", "SpO₂%", "HR bpm", "Ax g", "Ay g", "Az g", "GSR µS"]]
        for r in rows:
            tel_data.append([
                str(r["timestamp_ms"]),
                _fmt(r["spo2"]),
                _fmt(r["hr"]),
                _fmt(r["accel_x"]),
                _fmt(r["accel_y"]),
                _fmt(r["accel_z"]),
                _fmt(r["gsr_conductance_us"]),
            ])
        tel_table = Table(tel_data, colWidths=[2.2*cm]*7)
        tel_table.setStyle(_summary_table_style(header_only_bold=True))
        story.append(tel_table)
        story.append(Spacer(1, 0.3*cm))

    # ---- Footer disclaimer ----
    story.append(Spacer(1, 0.5*cm))
    story.append(Paragraph(
        "IMPORTANT DISCLAIMER: This report is produced by an educational prototype "
        "device (SOMNI‑Guard) and is NOT a regulated medical device.  SpO₂ and "
        "heart‑rate values are computed using simplified approximations and have NOT "
        "been clinically validated.  This report must NOT be used for diagnosis, "
        "treatment decisions, or any patient‑safety purpose.  Always consult a "
        "qualified healthcare professional.",
        disclaimer_style,
    ))
    story.append(Paragraph(
        "Generated: {}  |  Session ID: {}  |  SOMNI‑Guard v0.2".format(
            summary.get("generated_at", ""), session_id
        ),
        ParagraphStyle("Footer", parent=normal, fontSize=7, textColor=colors.grey),
    ))

    doc.build(story)
    return pdf_path


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _fmt(value, decimals=2):
    """Format a number or return '—' if None."""
    if value is None:
        return "—"
    try:
        return "{:.{}f}".format(float(value), decimals)
    except (TypeError, ValueError):
        return str(value)


def _summary_table_style(header_only_bold=False):
    """Return a standard TableStyle for summary tables."""
    style = [
        ("BACKGROUND",    (0, 0), (-1, 0),  colors.HexColor("#1a3a5c")),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4fa")]),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#aaaaaa")),
        ("ALIGN",         (1, 0), (-1, -1), "CENTER"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    return TableStyle(style)
