"""
report.py — Feature C: Exportable Mission Report (CSV + PDF)

Assembles an ISRO-style mission packet for a given site and exports it as either
a flat CSV (waypoint table + summary row) or a styled single-page PDF.

Conventions:
  - assemble_report_data() is the single source-of-truth for report content.
    Both to_csv() and to_pdf() consume a MissionReportData object, so changing
    the report layout requires editing only this module + the schema.
  - All ice/hazard data is MOCK DATA (clearly labelled) standing in for
    Member 1's real Chandrayaan-2 ingestion pipeline.
  - CSV uses Python's stdlib csv module (no heavy dependency).
  - PDF uses reportlab (no system-level GTK/Cairo required; works on Windows/Linux/macOS).
"""

import csv
import io
import math
import os
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from cost_grid import CostGrid
from lmrs import compute_lmrs
from pathfinder import build_route
from confidence import apply_confidence_to_route
from schemas import (
    HazardSummary,
    IceLayerData,
    MissionReportData,
    RouteResponse,
)

# ── Constants ─────────────────────────────────────────────────────────────────

# MOCK DATA: default grid sample size used when computing hazard statistics.
# In production, use the real OHRC-derived slope/obstacle raster from Member 1.
HAZARD_SAMPLE_STRIDE: int = 5

# Default route start/end (grid corners).  MOCK DATA — replace with real
# landing/traverse coordinates from mission planning.
DEFAULT_START_X: int = 0
DEFAULT_START_Y: int = 0
DEFAULT_GOAL_X: int = 99
DEFAULT_GOAL_Y: int = 99

# PDF styling
PDF_BRAND_COLOR = colors.HexColor("#0A1628")   # deep space navy (header bg)
PDF_ACCENT_COLOR = colors.HexColor("#00D4FF")  # electric cyan (accent)
PDF_WARN_COLOR = colors.HexColor("#FF6B35")    # warning orange
MISSION_NAME = "AstraNav-LRIS"
MISSION_SUBTITLE = "ISRO Bharatiya Antariksh Hackathon 2026 — Problem Statement 08"


# ── Hazard sampling ───────────────────────────────────────────────────────────

def _compute_hazard_summary(grid: CostGrid, stride: int = HAZARD_SAMPLE_STRIDE) -> HazardSummary:
    """
    Samples the GLOBAL_GRID to derive aggregate hazard statistics.

    MOCK DATA: slope is back-calculated from traversal cost using the inverse of
    the formula in cost_grid.py: slope = sqrt(max(0, (cost - 1.0) * 100)).
    A real implementation would read the raw HazardMask raster from Member 1.
    """
    slopes = []
    obstacle_count = 0
    shadow_count = 0
    total_sampled = 0

    for gy in range(0, grid.height, stride):
        for gx in range(0, grid.width, stride):
            cost = grid.get_traversal_cost(gx, gy)
            is_shadow = grid.is_in_shadow(gx, gy)
            total_sampled += 1

            if cost == float("inf"):
                obstacle_count += 1
                slopes.append(35.0)  # treat as max slope for stats
            else:
                # Inverse of: grid[y,x] = 1 + slope^2 / 100  →  slope = sqrt((cost-1)*100)
                slope_deg = math.sqrt(max(0.0, (cost - 1.0) * 100.0))
                slopes.append(slope_deg)

            if is_shadow:
                shadow_count += 1

    slope_arr = np.array(slopes, dtype=float)
    return HazardSummary(
        slope_mean_deg=round(float(np.mean(slope_arr)), 2),
        slope_max_deg=round(float(np.max(slope_arr)), 2),
        obstacle_cell_pct=round(obstacle_count / total_sampled * 100.0, 2),
        shadow_cell_pct=round(shadow_count / total_sampled * 100.0, 2),
    )


# ── Report assembly ───────────────────────────────────────────────────────────

def assemble_report_data(
    lat: float,
    lon: float,
    region_id: str,
    grid: CostGrid,
) -> MissionReportData:
    """
    Builds the intermediate MissionReportData consumed by both to_csv() and to_pdf().

    Calls compute_lmrs() and build_route() so both export formats stay in sync.
    MOCK DATA sections clearly labelled.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── LMRS ──────────────────────────────────────────────────────────────────
    lmrs_resp = compute_lmrs(
        target_lat=lat,
        target_lon=lon,
        start_lat=0.0,
        start_lon=0.0,
        region_id=region_id,
        grid=grid,
    )

    # ── Route ─────────────────────────────────────────────────────────────────
    goal_x = max(0, min(grid.width - 1, int(lon * 10000)))
    goal_y = max(0, min(grid.height - 1, int(lat * 10000)))

    route: Optional[RouteResponse] = build_route(
        grid,
        (DEFAULT_START_X, DEFAULT_START_Y),
        (goal_x, goal_y),
        region_id,
    )
    if route:
        apply_confidence_to_route(route)
    else:
        # Fallback: empty route if pathfinder cannot reach target.
        from schemas import RouteResponse as _RR
        route = _RR(
            route_id="no-route",
            region_id=region_id,
            waypoints=[],
            total_distance_m=0.0,
            total_energy_wh=0.0,
        )

    # ── Ice Layer (MOCK DATA) ─────────────────────────────────────────────────
    # MOCK DATA - Replace with real Member 1 Chandrayaan-2 DFSAR ice detection output.
    mock_ice = IceLayerData(
        lat=lat,
        lon=lon,
        ice_volume_m3=800.0,
        ice_depth_m=1.5,
        confidence=0.85,
    )

    # ── Hazard Summary (MOCK DATA — back-calculated from cost grid) ───────────
    hazard_summary = _compute_hazard_summary(grid)

    return MissionReportData(
        region_id=region_id,
        generated_at=timestamp,
        lat=lat,
        lon=lon,
        lmrs=lmrs_resp,
        ice_layer=mock_ice,
        hazard_summary=hazard_summary,
        waypoints=route.waypoints,
        total_distance_m=route.total_distance_m,
        total_energy_wh=route.total_energy_wh,
    )


# ── CSV export ────────────────────────────────────────────────────────────────

def to_csv(data: MissionReportData) -> str:
    """
    Generates a flat CSV string from MissionReportData.

    Layout:
      Section 1 — Summary metrics (region, LMRS, RAI, Comm, Thermal, Hazard)
      Section 2 — Blank separator row
      Section 3 — Waypoint table (one row per waypoint)

    Returns the CSV as a plain string (caller wraps in StreamingResponse).
    """
    buf = io.StringIO()
    writer = csv.writer(buf)

    # ── Summary section ────────────────────────────────────────────────────────
    writer.writerow(["# AstraNav-LRIS Mission Report"])
    writer.writerow(["# Generated", data.generated_at])
    writer.writerow(["# Region", data.region_id])
    writer.writerow(["# Target Lat", data.lat, "Target Lon", data.lon])
    writer.writerow([])

    writer.writerow(["=== LMRS SUMMARY ==="])
    writer.writerow(["Metric", "Value"])
    writer.writerow(["LMRS Score (0-100)", round(data.lmrs.lmrs_score, 2)])
    writer.writerow(["Overall Confidence", round(data.lmrs.confidence, 3)])
    writer.writerow([])

    writer.writerow(["=== RESOURCE ACCESSIBILITY INDEX ==="])
    writer.writerow(["Ice Volume (m³)", data.lmrs.rai.ice_volume_m3])
    writer.writerow(["Ice Depth (m)", data.lmrs.rai.ice_depth_m])
    writer.writerow(["Extraction Difficulty Score", round(data.lmrs.rai.extraction_difficulty_score, 2)])
    writer.writerow([])

    writer.writerow(["=== COMM VISIBILITY ==="])
    writer.writerow(["Earth Line of Sight", data.lmrs.comm_visibility.earth_line_of_sight])
    writer.writerow(["Signal Strength (%)", round(data.lmrs.comm_visibility.signal_strength_pct, 2)])
    writer.writerow([])

    writer.writerow(["=== THERMAL RISK ==="])
    writer.writerow(["Total Energy (Wh)", round(data.lmrs.thermal_risk.total_energy_wh, 2)])
    writer.writerow(["Shadow Exposure (min)", round(data.lmrs.thermal_risk.shadow_exposure_time_min, 2)])
    writer.writerow(["Thermal Risk Score", round(data.lmrs.thermal_risk.thermal_risk_score, 2)])
    writer.writerow([])

    writer.writerow(["=== HAZARD SUMMARY (MOCK DATA) ==="])
    writer.writerow(["Mean Slope (°)", data.hazard_summary.slope_mean_deg])
    writer.writerow(["Max Slope (°)", data.hazard_summary.slope_max_deg])
    writer.writerow(["Obstacle Cells (%)", data.hazard_summary.obstacle_cell_pct])
    writer.writerow(["Shadow Cells (%)", data.hazard_summary.shadow_cell_pct])
    writer.writerow([])

    writer.writerow(["=== ICE LAYER (MOCK DATA) ==="])
    writer.writerow(["Ice Volume (m³)", data.ice_layer.ice_volume_m3])
    writer.writerow(["Ice Depth (m)", data.ice_layer.ice_depth_m])
    writer.writerow(["Detection Confidence", data.ice_layer.confidence])
    writer.writerow([])

    writer.writerow(["=== ROUTE SUMMARY ==="])
    writer.writerow(["Total Distance (m)", round(data.total_distance_m, 2)])
    writer.writerow(["Total Energy (Wh)", round(data.total_energy_wh, 2)])
    writer.writerow(["Waypoint Count", len(data.waypoints)])
    writer.writerow([])

    # ── Waypoint table ─────────────────────────────────────────────────────────
    writer.writerow(["=== WAYPOINTS ==="])
    writer.writerow([
        "Index", "Lat", "Lon", "Type",
        "Cumulative Distance (m)", "Cumulative Energy (Wh)",
        "In Shadow", "Confidence",
    ])
    for i, wpt in enumerate(data.waypoints):
        writer.writerow([
            i + 1,
            round(wpt.lat, 6),
            round(wpt.lon, 6),
            wpt.type,
            round(wpt.cumulative_distance_m, 2),
            round(wpt.cumulative_energy_wh, 2),
            wpt.is_in_shadow,
            round(wpt.confidence, 3),
        ])

    return buf.getvalue()


# ── PDF export ────────────────────────────────────────────────────────────────

def _make_table_style(header_bg=PDF_BRAND_COLOR) -> TableStyle:
    return TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  header_bg),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0),  8),
        ("BOTTOMPADDING", (0, 0), (-1, 0),  6),
        ("TOPPADDING",    (0, 0), (-1, 0),  6),
        ("BACKGROUND",    (0, 1), (-1, -1), colors.HexColor("#F0F4F8")),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, colors.HexColor("#EEF2F7")]),
        ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 1), (-1, -1), 7),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
    ])


def to_pdf(data: MissionReportData) -> bytes:
    """
    Generates a styled one-page (A4) PDF mission report using reportlab.

    Layout:
      - Mission header with region, timestamp, coordinates
      - LMRS score badge + sub-score table
      - Hazard & ice summary table
      - Route overview
      - Waypoint table (up to first 30 waypoints to keep report single-page)

    Returns raw PDF bytes (caller wraps in StreamingResponse).
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        title=f"AstraNav-LRIS Mission Report — {data.region_id}",
        author="AstraNav-LRIS System",
    )

    styles = getSampleStyleSheet()
    story = []

    # ─── Header banner ──────────────────────────────────────────────────────
    header_style = ParagraphStyle(
        "Header",
        parent=styles["Normal"],
        fontSize=18,
        textColor=colors.white,
        fontName="Helvetica-Bold",
        leading=22,
    )
    sub_style = ParagraphStyle(
        "SubHeader",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#A0C4E0"),
        fontName="Helvetica",
        leading=12,
    )
    body_style = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#1A2B3C"),
        fontName="Helvetica",
        leading=11,
    )
    section_style = ParagraphStyle(
        "Section",
        parent=styles["Normal"],
        fontSize=9,
        textColor=PDF_BRAND_COLOR,
        fontName="Helvetica-Bold",
        leading=13,
        spaceBefore=6,
        spaceAfter=3,
    )

    # Banner table (full-width dark header)
    banner_data = [[
        Paragraph(f"<b>{MISSION_NAME}</b> — Mission Report", header_style),
        Paragraph(
            f"{MISSION_SUBTITLE}<br/>"
            f"Region: <b>{data.region_id}</b> &nbsp;|&nbsp; "
            f"Generated: {data.generated_at}",
            sub_style,
        ),
    ]]
    banner_table = Table(banner_data, colWidths=["40%", "60%"])
    banner_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), PDF_BRAND_COLOR),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
    ]))
    story.append(banner_table)
    story.append(Spacer(1, 0.3 * cm))

    # ─── Target coordinates ──────────────────────────────────────────────────
    coord_data = [[
        Paragraph("<b>Target Coordinates</b>", body_style),
        Paragraph(f"Lat: {data.lat:.6f}°   Lon: {data.lon:.6f}°", body_style),
        Paragraph("<b>Route Distance</b>", body_style),
        Paragraph(f"{data.total_distance_m:.1f} m", body_style),
        Paragraph("<b>Route Energy</b>", body_style),
        Paragraph(f"{data.total_energy_wh:.1f} Wh", body_style),
    ]]
    coord_table = Table(coord_data, colWidths=["15%", "18%", "15%", "15%", "15%", "22%"])
    coord_table.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#E8F0FE")),
        ("FONTSIZE",      (0, 0), (-1, -1), 7),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#BBCFE8")),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(coord_table)
    story.append(Spacer(1, 0.3 * cm))

    # ─── LMRS score summary ──────────────────────────────────────────────────
    story.append(Paragraph("LUNAR MINING READINESS SCORE (LMRS)", section_style))

    lmrs = data.lmrs
    score_color = (
        colors.HexColor("#00C851") if lmrs.lmrs_score >= 70
        else colors.HexColor("#FF8800") if lmrs.lmrs_score >= 40
        else colors.HexColor("#FF3547")
    )
    score_style = ParagraphStyle(
        "Score",
        parent=styles["Normal"],
        fontSize=28,
        textColor=score_color,
        fontName="Helvetica-Bold",
        alignment=1,
    )
    score_label = ParagraphStyle(
        "ScoreLabel",
        parent=styles["Normal"],
        fontSize=7,
        textColor=colors.HexColor("#555555"),
        fontName="Helvetica",
        alignment=1,
    )

    lmrs_table_data = [
        ["Sub-Score", "Value", "Notes"],
        ["LMRS Overall", f"{lmrs.lmrs_score:.1f} / 100", f"Confidence: {lmrs.confidence:.2f}"],
        ["Resource Accessibility Index",
         f"Vol: {lmrs.rai.ice_volume_m3:.0f} m³ | Depth: {lmrs.rai.ice_depth_m:.1f} m",
         f"Extraction Difficulty: {lmrs.rai.extraction_difficulty_score:.1f}"],
        ["Comm Visibility",
         f"LOS: {'Yes' if lmrs.comm_visibility.earth_line_of_sight else 'No'}",
         f"Signal: {lmrs.comm_visibility.signal_strength_pct:.0f}%"],
        ["Thermal Risk",
         f"Score: {lmrs.thermal_risk.thermal_risk_score:.1f} / 100",
         f"Shadow: {lmrs.thermal_risk.shadow_exposure_time_min:.1f} min | Energy: {lmrs.thermal_risk.total_energy_wh:.1f} Wh"],
    ]

    lmrs_table = Table(lmrs_table_data, colWidths=["35%", "35%", "30%"])
    lmrs_table.setStyle(_make_table_style())
    # Highlight overall LMRS row
    lmrs_table.setStyle(TableStyle([
        ("TEXTCOLOR",     (1, 1), (1, 1), score_color),
        ("FONTNAME",      (1, 1), (1, 1), "Helvetica-Bold"),
        ("FONTSIZE",      (1, 1), (1, 1), 9),
    ]))
    story.append(lmrs_table)
    story.append(Spacer(1, 0.3 * cm))

    # ─── Hazard & ice summary ────────────────────────────────────────────────
    story.append(Paragraph("HAZARD & ICE SUMMARY  ⚠ MOCK DATA", section_style))

    hazard_ice_data = [
        ["Metric", "Value"],
        ["Mean Slope",       f"{data.hazard_summary.slope_mean_deg:.1f}°"],
        ["Max Slope",        f"{data.hazard_summary.slope_max_deg:.1f}°"],
        ["Obstacle Cells",   f"{data.hazard_summary.obstacle_cell_pct:.1f}%"],
        ["Shadow Cells",     f"{data.hazard_summary.shadow_cell_pct:.1f}%"],
        ["Ice Volume",       f"{data.ice_layer.ice_volume_m3:.0f} m³"],
        ["Ice Depth",        f"{data.ice_layer.ice_depth_m:.1f} m"],
        ["Ice Confidence",   f"{data.ice_layer.confidence:.2f}"],
    ]
    hazard_table = Table(hazard_ice_data, colWidths=["50%", "50%"])
    hazard_table.setStyle(_make_table_style())
    story.append(hazard_table)
    story.append(Spacer(1, 0.3 * cm))

    # ─── Waypoint table (capped at 30 for page fit) ──────────────────────────
    displayed_wpts = data.waypoints[:30]
    story.append(Paragraph(
        f"ROUTE WAYPOINTS  (showing {len(displayed_wpts)} of {len(data.waypoints)})",
        section_style,
    ))

    wpt_header = ["#", "Lat", "Lon", "Type", "Dist (m)", "Energy (Wh)", "Shadow", "Conf"]
    wpt_rows = [wpt_header]
    for i, wpt in enumerate(displayed_wpts):
        wpt_rows.append([
            str(i + 1),
            f"{wpt.lat:.5f}",
            f"{wpt.lon:.5f}",
            wpt.type,
            f"{wpt.cumulative_distance_m:.1f}",
            f"{wpt.cumulative_energy_wh:.1f}",
            "Y" if wpt.is_in_shadow else "N",
            f"{wpt.confidence:.2f}",
        ])

    wpt_table = Table(wpt_rows, colWidths=["5%", "12%", "12%", "14%", "12%", "13%", "9%", "9%"])
    wpt_table.setStyle(_make_table_style())
    # Highlight solar pitstop rows
    for row_idx, wpt in enumerate(displayed_wpts, start=1):
        if wpt.type == "solar_pitstop":
            wpt_table.setStyle(TableStyle([
                ("BACKGROUND", (0, row_idx), (-1, row_idx), colors.HexColor("#FFF3CD")),
                ("TEXTCOLOR",  (3, row_idx), (3, row_idx), colors.HexColor("#856404")),
                ("FONTNAME",   (3, row_idx), (3, row_idx), "Helvetica-Bold"),
            ]))
    story.append(wpt_table)
    story.append(Spacer(1, 0.2 * cm))

    # ─── Footer note ─────────────────────────────────────────────────────────
    footer_style = ParagraphStyle(
        "Footer",
        parent=styles["Normal"],
        fontSize=6,
        textColor=colors.HexColor("#888888"),
        fontName="Helvetica-Oblique",
    )
    story.append(Paragraph(
        "⚠ Ice layer and hazard data are MOCK DATA placeholders. "
        "Replace with real Chandrayaan-2 DFSAR + OHRC pipeline outputs (Member 1) before operational use.",
        footer_style,
    ))

    doc.build(story)
    return buf.getvalue()
