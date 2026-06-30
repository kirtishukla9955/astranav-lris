"""
briefing.py — Feature D: AI Mission Briefing (LLM-Generated Plain-English Summary)

Generates a 3-5 sentence plain-English mission note suitable for a non-technical
judge audience, explaining why a candidate landing/traverse site is (or isn't)
a strong choice.

Architecture:
  - generate_mission_briefing() is the single public entry point.
  - It accepts pre-computed LMRSResponse/RouteResponse to avoid redundant A* runs
    when called from endpoints that already hold those objects.
  - LLM path:  Anthropic claude-sonnet-4-6, max_tokens=1000.
               API key from ANTHROPIC_API_KEY env var (never hardcoded).
  - Fallback:  If the API is unreachable, key is missing, or any exception occurs,
               a deterministic template summary is returned so the demo never breaks.
  - This module is intentionally separate from copilot.py:
      copilot.py  — ad-hoc Q&A driven by a user question string
      briefing.py — one-shot structured summary, no user prompt required
"""

import os
from typing import Optional

from cost_grid import CostGrid
from lmrs import compute_lmrs
from pathfinder import build_route
from confidence import apply_confidence_to_route
from schemas import (
    LMRSResponse,
    MissionBriefingResponse,
    RouteResponse,
)

# ── LLM configuration ─────────────────────────────────────────────────────────

LLM_MODEL = "claude-sonnet-4-6"
LLM_MAX_TOKENS = 1000

# ── Prompt template ───────────────────────────────────────────────────────────

_PROMPT_TEMPLATE = """\
You are a planetary scientist summarising a candidate lunar south-polar landing site \
for a panel of non-technical judges at the ISRO Bharatiya Antariksh Hackathon 2026.

Write exactly 3-5 sentences in plain, engaging English. Do not use bullet points or \
section headings — write flowing prose. Cover: (1) whether this site is a strong landing \
candidate and why, (2) the key resource advantage (ice volume and depth), \
(3) the most significant risk (thermal, hazard, or communication), and \
(4) a brief traversal note (route energy and distance).

Site data:
- Region: {region_id}
- Target: Lat {lat:.5f}°, Lon {lon:.5f}°
- Lunar Mining Readiness Score (LMRS): {lmrs_score:.1f} / 100
- Ice Volume: {ice_volume_m3:.0f} m³ at {ice_depth_m:.1f} m depth (extraction difficulty: {extraction_difficulty:.1f})
- Earth Comm Line-of-Sight: {los} (signal strength {signal_pct:.0f}%)
- Thermal Risk Score: {thermal_risk:.1f} / 100 (shadow exposure {shadow_min:.1f} min, route energy {energy_wh:.1f} Wh)
- Route: {distance_m:.0f} m total distance, {waypoint_count} waypoints
- Overall detection confidence: {confidence:.2f}

Write the briefing now:
"""


# ── Fallback template ─────────────────────────────────────────────────────────

def _fallback_briefing(
    lat: float,
    lon: float,
    region_id: str,
    lmrs_resp: LMRSResponse,
    route_resp: Optional[RouteResponse],
) -> str:
    """
    Deterministic template-based summary used when the Anthropic API is unavailable.
    Produces a grammatically complete 3-sentence note from raw numeric values.
    """
    score = lmrs_resp.lmrs_score
    rai = lmrs_resp.rai
    comm = lmrs_resp.comm_visibility
    thermal = lmrs_resp.thermal_risk
    conf = lmrs_resp.confidence

    # Qualitative descriptors
    score_label = (
        "an excellent" if score >= 70
        else "a moderate" if score >= 45
        else "a challenging"
    )
    comm_label = (
        "strong Earth communications are available" if comm.earth_line_of_sight
        else "direct Earth communications are obstructed by crater walls"
    )
    thermal_label = (
        "low thermal risk" if thermal.thermal_risk_score < 40
        else "moderate thermal risk" if thermal.thermal_risk_score < 70
        else "elevated thermal risk"
    )

    dist_note = ""
    if route_resp and route_resp.total_distance_m > 0:
        dist_note = (
            f" The planned traverse covers {route_resp.total_distance_m:.0f} m "
            f"at a projected energy cost of {route_resp.total_energy_wh:.1f} Wh."
        )

    return (
        f"Site ({lat:.5f}°, {lon:.5f}°) in region '{region_id}' represents "
        f"{score_label} landing candidate with an LMRS score of {score:.1f}/100 "
        f"and a detection confidence of {conf:.2f}. "
        f"The site holds an estimated {rai.ice_volume_m3:.0f} m³ of subsurface ice "
        f"at {rai.ice_depth_m:.1f} m depth "
        f"(extraction difficulty index: {rai.extraction_difficulty_score:.1f}), "
        f"while {comm_label}. "
        f"Thermal analysis indicates {thermal_label} "
        f"({thermal.shadow_exposure_time_min:.1f} min shadow exposure, "
        f"{thermal.thermal_risk_score:.1f}/100 risk score).{dist_note}"
    )


# ── LLM call ──────────────────────────────────────────────────────────────────

def _call_llm(prompt: str) -> str:
    """
    Calls Anthropic claude-sonnet-4-6 and returns the generated text.
    Raises any exception to the caller (which will fall back to template).
    """
    import anthropic  # deferred import so module loads even if package absent

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set.")

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=LLM_MODEL,
        max_tokens=LLM_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    # Extract the first text block from the response
    return message.content[0].text.strip()


# ── Public API ────────────────────────────────────────────────────────────────

def generate_mission_briefing(
    lat: float,
    lon: float,
    region_id: str,
    grid: CostGrid,
    lmrs_resp: Optional[LMRSResponse] = None,
    route_resp: Optional[RouteResponse] = None,
) -> MissionBriefingResponse:
    """
    Generates a plain-English mission briefing for a candidate landing site.

    Args:
        lat, lon, region_id: Target coordinates and region identifier.
        grid:       The CostGrid used if LMRS/route need to be computed.
        lmrs_resp:  Pre-computed LMRSResponse (skips recomputation if provided).
        route_resp: Pre-computed RouteResponse (skips recomputation if provided).

    Returns:
        MissionBriefingResponse with briefing_text and generated_by flag.
    """
    # ── Compute LMRS if not provided ─────────────────────────────────────────
    if lmrs_resp is None:
        lmrs_resp = compute_lmrs(
            target_lat=lat,
            target_lon=lon,
            start_lat=0.0,
            start_lon=0.0,
            region_id=region_id,
            grid=grid,
        )

    # ── Compute route if not provided ────────────────────────────────────────
    if route_resp is None:
        goal_x = max(0, min(grid.width - 1, int(lon * 10000)))
        goal_y = max(0, min(grid.height - 1, int(lat * 10000)))
        route_resp = build_route(grid, (0, 0), (goal_x, goal_y), region_id)
        if route_resp:
            apply_confidence_to_route(route_resp)

    # ── Build prompt ──────────────────────────────────────────────────────────
    waypoint_count = len(route_resp.waypoints) if route_resp else 0
    total_distance = route_resp.total_distance_m if route_resp else 0.0
    total_energy = route_resp.total_energy_wh if route_resp else 0.0

    prompt = _PROMPT_TEMPLATE.format(
        region_id=region_id,
        lat=lat,
        lon=lon,
        lmrs_score=lmrs_resp.lmrs_score,
        ice_volume_m3=lmrs_resp.rai.ice_volume_m3,
        ice_depth_m=lmrs_resp.rai.ice_depth_m,
        extraction_difficulty=lmrs_resp.rai.extraction_difficulty_score,
        los="Yes" if lmrs_resp.comm_visibility.earth_line_of_sight else "No",
        signal_pct=lmrs_resp.comm_visibility.signal_strength_pct,
        thermal_risk=lmrs_resp.thermal_risk.thermal_risk_score,
        shadow_min=lmrs_resp.thermal_risk.shadow_exposure_time_min,
        energy_wh=total_energy,
        distance_m=total_distance,
        waypoint_count=waypoint_count,
        confidence=lmrs_resp.confidence,
    )

    # ── Attempt LLM call; fall back gracefully on any failure ─────────────────
    try:
        briefing_text = _call_llm(prompt)
        generated_by: str = "llm"
    except Exception as exc:
        # Log the reason so developers can diagnose without breaking the demo.
        print(f"[briefing] LLM call failed ({type(exc).__name__}: {exc}). Using fallback template.")
        briefing_text = _fallback_briefing(lat, lon, region_id, lmrs_resp, route_resp)
        generated_by = "fallback_template"

    return MissionBriefingResponse(
        lat=lat,
        lon=lon,
        region_id=region_id,
        briefing_text=briefing_text,
        generated_by=generated_by,  # type: ignore[arg-type]
    )
