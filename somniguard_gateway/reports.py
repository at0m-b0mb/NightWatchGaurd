"""
reports.py -- SOMNI-Guard sleep-report feature extraction and PDF generation.

Two responsibilities:
1. compute_summary(session_id) -- query telemetry, compute aggregate
   metrics (SpO2 stats, HR stats, desaturation events, movement events,
   GSR stats), return a plain dict.

2. generate_pdf(session, summary) -- render the summary dict plus raw
   telemetry into a ReportLab PDF, save to REPORT_DIR, return the file path.

All metrics are non-clinical educational estimates. The generated PDF
contains a prominent disclaimer.

Educational prototype -- not a clinically approved device.
"""

import hashlib
import hmac
import json
import math
import os
from datetime import datetime, timezone

import config as cfg
import database as db

# ReportLab imports -- fail gracefully if not installed.
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
# Unicode font registration
# ---------------------------------------------------------------------------
# ReportLab's built-in fonts (Helvetica, Times-Roman) only support the
# characters in WinAnsiEncoding (roughly Windows-1252).  Characters outside
# that range -- subscript digits (SpO2), non-breaking hyphens, emoji, Greek
# letters -- render as black squares (■).
#
# We attempt to register DejaVu Sans (a full Unicode TTF available via
# `sudo apt install fonts-dejavu-core` on Raspberry Pi OS).  If the font
# file is not found we fall back to plain ASCII-safe text strings throughout.

_FONT_NORMAL = "Helvetica"
_FONT_BOLD   = "Helvetica-Bold"
_UNICODE_FONTS_REGISTERED = False


def _try_register_unicode_fonts() -> bool:
    """Register DejaVu Sans if a TTF file can be found on the system.

    Returns True on success so the rest of the module can choose between
    Unicode strings and ASCII-safe fallbacks.
    """
    if not _REPORTLAB_AVAILABLE:
        return False
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu-sans/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/System/Library/Fonts/Helvetica.ttc",  # macOS fallback
        ]
        bold_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu-sans/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]

        reg_normal = reg_bold = False
        for path in candidates:
            if os.path.isfile(path):
                pdfmetrics.registerFont(TTFont("UniFont", path))
                reg_normal = True
                break
        for path in bold_candidates:
            if os.path.isfile(path):
                pdfmetrics.registerFont(TTFont("UniFont-Bold", path))
                reg_bold = True
                break

        if reg_normal:
            global _FONT_NORMAL, _FONT_BOLD
            _FONT_NORMAL = "UniFont"
            _FONT_BOLD   = "UniFont-Bold" if reg_bold else "UniFont"
            print("[SOMNI][REPORTS] Unicode font registered: {}".format(_FONT_NORMAL))
            return True
    except Exception as exc:
        print("[SOMNI][REPORTS] Unicode font registration failed ({}); "
              "using ASCII-safe text.".format(exc))
    return False


if _REPORTLAB_AVAILABLE:
    _UNICODE_FONTS_REGISTERED = _try_register_unicode_fonts()


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _t(unicode_str: str, ascii_str: str) -> str:
    """Return unicode_str when a Unicode font is registered, ascii_str otherwise.

    Use this for every string that contains characters outside Windows-1252
    (subscripts, special hyphens, Greek letters, emoji).
    """
    return unicode_str if _UNICODE_FONTS_REGISTERED else ascii_str


def _fmt(value, decimals=2):
    """Format a number or return a dash if None."""
    if value is None:
        return _t("—", "--")   # em dash or ASCII double-dash
    try:
        return "{:.{}f}".format(float(value), decimals)
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def compute_summary(session_id):
    """
    Compute aggregate sleep metrics from stored telemetry.

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
        "session_id":           session_id,
        "total_telemetry_rows": len(rows),
        "duration_s":           duration_s,
        "spo2":                 _stats(spo2_vals),
        "hr":                   _stats(hr_vals),
        "gsr":                  _stats(gsr_vals),
        "desaturation_events":  desats,
        "movement_events":      movements,
        "generated_at":         datetime.now(timezone.utc).isoformat() + "Z",
        "non_clinical_note":    (
            "NON-CLINICAL EDUCATIONAL PROTOTYPE. "
            "Values are approximations and must NOT be used for diagnosis."
        ),
    }


def sign_summary(summary_json):
    """
    Compute HMAC-SHA256 of a JSON summary string.

    Args:
        summary_json (str): JSON string of the summary dict.

    Returns:
        str: Hex-encoded HMAC-SHA256 digest.
    """
    key = cfg.PICO_HMAC_KEY.encode("utf-8")
    return hmac.new(key, summary_json.encode("utf-8"), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------

def generate_pdf(session_row, summary):
    """
    Render a sleep-session summary PDF and save it to REPORT_DIR.

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
    try:
        patient_dob = session_row["patient_dob"] or "N/A"
    except (IndexError, KeyError):
        patient_dob = "N/A"
    started_at = session_row["started_at"]
    ended_at   = session_row["ended_at"] or "ongoing"
    device_id  = session_row["device_id"]

    filename = "somni_report_session_{}_{}.pdf".format(
        session_id,
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
    )
    pdf_path = os.path.join(cfg.REPORT_DIR, filename)

    doc    = SimpleDocTemplate(pdf_path, pagesize=A4,
                               rightMargin=2*cm, leftMargin=2*cm,
                               topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()

    # Custom styles -- use the registered Unicode font if available
    title_style = ParagraphStyle(
        "SomniTitle",
        parent=styles["Title"],
        fontName=_FONT_BOLD,
        fontSize=18,
        spaceAfter=6,
        textColor=colors.HexColor("#1a3a5c"),
    )
    h2_style = ParagraphStyle(
        "SomniH2",
        parent=styles["Heading2"],
        fontName=_FONT_BOLD,
        fontSize=13,
        spaceBefore=14,
        spaceAfter=4,
        textColor=colors.HexColor("#1a3a5c"),
    )
    disclaimer_style = ParagraphStyle(
        "Disclaimer",
        parent=styles["Normal"],
        fontName=_FONT_NORMAL,
        fontSize=8,
        textColor=colors.red,
        backColor=colors.HexColor("#fff3f3"),
        borderPad=4,
        leading=12,
    )
    normal_style = ParagraphStyle(
        "SomniNormal",
        parent=styles["Normal"],
        fontName=_FONT_NORMAL,
    )
    footer_style = ParagraphStyle(
        "Footer",
        parent=styles["Normal"],
        fontName=_FONT_NORMAL,
        fontSize=7,
        textColor=colors.grey,
    )

    story = []

    # ---- Header ----
    story.append(Paragraph(
        _t("SOMNI‑Guard Sleep Monitoring Report",
           "SOMNI-Guard Sleep Monitoring Report"),
        title_style,
    ))
    story.append(Paragraph(
        _t("⚠️  NON‑CLINICAL EDUCATIONAL PROTOTYPE — NOT FOR DIAGNOSTIC USE",
           "[!] NON-CLINICAL EDUCATIONAL PROTOTYPE -- NOT FOR DIAGNOSTIC USE"),
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
        ("BACKGROUND",    (0, 0), (0, -1),  colors.HexColor("#dce6f1")),
        ("FONTNAME",      (0, 0), (0, -1),  _FONT_BOLD),
        ("FONTNAME",      (1, 0), (1, -1),  _FONT_NORMAL),
        ("FONTSIZE",      (0, 0), (-1, -1), 10),
        ("ROWBACKGROUNDS",(0, 0), (-1, -1), [colors.whitesmoke, colors.white]),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#aaaaaa")),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 0.4*cm))

    # ---- SpO2 / HR summary ----
    story.append(Paragraph(
        _t("SpO₂ / Heart Rate Summary (non-clinical)",
           "SpO2 / Heart Rate Summary (non-clinical)"),
        h2_style,
    ))
    spo2 = summary.get("spo2", {})
    hr   = summary.get("hr",   {})
    vitals_data = [
        ["Metric", "Min", "Max", "Mean", "# Valid Readings"],
        [_t("SpO₂ (%)", "SpO2 (%)"),
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
    story.append(Paragraph("Sleep Events (non-clinical heuristics)", h2_style))
    events_data = [
        ["Event Type", "Count", "Threshold Used"],
        [_t("Desaturation events (SpO₂ < {}%)".format(cfg.DESATURATION_THRESHOLD_PCT),
            "Desaturation events (SpO2 < {}%)".format(cfg.DESATURATION_THRESHOLD_PCT)),
         str(summary.get("desaturation_events", 0)),
         _t("SpO₂ < {}%".format(cfg.DESATURATION_THRESHOLD_PCT),
            "SpO2 < {}%".format(cfg.DESATURATION_THRESHOLD_PCT))],
        ["Movement/Arousal events",
         str(summary.get("movement_events", 0)),
         _t("Δ|accel| > {}g".format(cfg.MOVEMENT_THRESHOLD_G),
            "d|accel| > {}g".format(cfg.MOVEMENT_THRESHOLD_G))],
    ]
    events_table = Table(events_data, colWidths=[8*cm, 2.5*cm, 5*cm])
    events_table.setStyle(_summary_table_style())
    story.append(events_table)
    story.append(Spacer(1, 0.3*cm))

    # ---- GSR summary ----
    story.append(Paragraph(
        "Galvanic Skin Response Summary (non-clinical)", h2_style,
    ))
    gsr = summary.get("gsr", {})
    gsr_data = [
        ["Metric",
         _t("Min (µS)", "Min (uS)"),
         _t("Max (µS)", "Max (uS)"),
         _t("Mean (µS)", "Mean (uS)"),
         "# Valid Readings"],
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
        tel_header = [
            "t (ms)",
            _t("SpO₂%", "SpO2%"),
            "HR bpm",
            "Ax g", "Ay g", "Az g",
            _t("GSR µS", "GSR uS"),
        ]
        tel_data = [tel_header]
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
        "device (SOMNI-Guard) and is NOT a regulated medical device.  SpO2 and "
        "heart-rate values are computed using simplified approximations and have NOT "
        "been clinically validated.  This report must NOT be used for diagnosis, "
        "treatment decisions, or any patient-safety purpose.  Always consult a "
        "qualified healthcare professional.",
        disclaimer_style,
    ))
    story.append(Paragraph(
        "Generated: {}  |  Session ID: {}  |  SOMNI-Guard v0.4".format(
            summary.get("generated_at", ""), session_id
        ),
        footer_style,
    ))

    doc.build(story)
    return pdf_path


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _summary_table_style(header_only_bold=False):
    """Return a standard TableStyle for summary tables."""
    return TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  colors.HexColor("#1a3a5c")),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",      (0, 0), (-1, 0),  _FONT_BOLD),
        ("FONTNAME",      (0, 1), (-1, -1), _FONT_NORMAL),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4fa")]),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#aaaaaa")),
        ("ALIGN",         (1, 0), (-1, -1), "CENTER"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ])
