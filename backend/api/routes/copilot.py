"""
api/routes/copilot.py
---------------------
POST /api/copilot/ask

Data-grounded Q&A copilot backed by Claude (claude-3-5-haiku-20241022).

Architecture
------------
1.  Context assembly — pull ice-layer, hazard-layer, and LMRS breakdown
    for the requested region (and optional lat/lon point).
2.  Serialize the context into a compact structured summary (JSON-like block).
3.  Call Claude with a system prompt that RESTRICTS answers to the provided
    data — explicitly instructs the model to say "I don't have data for that"
    rather than hallucinating.
4.  Return the answer + list of data_sources_used for frontend credibility UI.

Fallback
--------
If ANTHROPIC_API_KEY is not set, or the API call fails for any reason,
the endpoint returns a canned response with fallback_answer=True — it
does NOT raise an HTTP error.  The live demo will not crash on network hiccups.

Security
--------
The region_id and coordinates from the request are used to fetch from
in-memory mocks / app state — not injected into LLM prompts raw.
The question text is passed as user content but Claude's system prompt
enforces strict data-only answering.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Request

from api.models import (
    CopilotContextPoint,
    CopilotRequest,
    CopilotResponse,
)
from core.grid_cache import GridCache
from data.mock_fixtures import fetch_hazard_layer, fetch_ice_layer
from data.region_registry import get_region_config
from pathfinder import CostConfig, StaticBatteryModel
from scoring.lmrs_scorer import LMRSWeights, compute_lmrs

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Copilot"])

_CLAUDE_MODEL = "claude-3-5-haiku-20241022"
_FALLBACK_ANSWER = (
    "⚠️ The AI copilot is currently offline (API key not set or network error). "
    "Please consult the /api/lmrs and /api/route endpoints directly for quantitative data."
)

# Max tokens sent to Claude — keep context small for fast demo responses
_MAX_CONTEXT_ICE_FEATURES = 5
_MAX_CONTEXT_HAZARD_FEATURES = 5


# ---------------------------------------------------------------------------
# Context assembly helpers
# ---------------------------------------------------------------------------

def _summarise_ice(ice_features: list[dict]) -> str:
    """Compact text summary of ice candidates."""
    if not ice_features:
        return "No ice candidate polygons found in this region."
    lines = ["Ice candidates (CPR>1.0, DOP<0.13):"]
    for feat in ice_features[:_MAX_CONTEXT_ICE_FEATURES]:
        p = feat.get("properties", {})
        lines.append(
            f"  • {p.get('ice_id', '?')}: volume={p.get('volume_m3', '?')} m³, "
            f"depth={p.get('depth_m', '?')} m, confidence={p.get('confidence', '?')}, "
            f"CPR={p.get('cpr', '?')}, DOP={p.get('dop', '?')}, "
            f"dielectric={p.get('dielectric_constant', '?')}"
        )
    if len(ice_features) > _MAX_CONTEXT_ICE_FEATURES:
        lines.append(f"  … and {len(ice_features) - _MAX_CONTEXT_ICE_FEATURES} more.")
    return "\n".join(lines)


def _summarise_hazards(hazard_features: list[dict]) -> str:
    """Compact text summary of hazard polygons."""
    if not hazard_features:
        return "No hazard polygons in this region."
    lines = ["Hazards (slope>15° or boulder>0.5m → impassable):"]
    for feat in hazard_features[:_MAX_CONTEXT_HAZARD_FEATURES]:
        p = feat.get("properties", {})
        lines.append(
            f"  • {p.get('hazard_id', '?')}: type={p.get('hazard_type', '?')}, "
            f"severity={p.get('severity', '?')}"
            + (f", slope={p.get('slope_deg', '')}°" if "slope_deg" in p else "")
            + (f", max_boulder={p.get('max_boulder_diameter_m', '')} m" if "max_boulder_diameter_m" in p else "")
        )
    if len(hazard_features) > _MAX_CONTEXT_HAZARD_FEATURES:
        lines.append(f"  … and {len(hazard_features) - _MAX_CONTEXT_HAZARD_FEATURES} more.")
    return "\n".join(lines)


def _summarise_lmrs(lmrs_result) -> str:
    """Compact text summary of an LMRS computation result."""
    r = lmrs_result
    return (
        f"LMRS composite score: {r.lmrs_score}/100\n"
        f"  RAI (Resource Accessibility Index): {r.rai.score}/100 "
        f"[nearest ice: {r.rai.nearest_ice_distance_m:.0f} m, "
        f"volume: {r.rai.ice_volume_m3:.0f} m³, confidence: {r.rai.confidence:.2f}]\n"
        f"  Communication Visibility: {r.comm_visibility.score}/100 "
        f"[LOS fraction: {r.comm_visibility.los_fraction:.2f}"
        + (f", occlusion: {r.comm_visibility.occlusion_reason}" if r.comm_visibility.occlusion_reason else "") + "]\n"
        f"  Thermal Risk Score: {r.thermal_risk.score}/100 "
        f"[energy to reach: {r.thermal_risk.energy_cost_wh:.2f} Wh, "
        f"temp at dest: {r.thermal_risk.mean_temperature_k:.0f} K, "
        f"dark-dwell fraction: {r.thermal_risk.dark_dwell_fraction:.2f}]"
    )


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _call_claude(context_block: str, question: str) -> str:
    """
    Call the Anthropic Claude API synchronously.
    Returns the assistant's answer text, or raises on failure.
    """
    import anthropic  # lazy import — avoids import error if not installed

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    system_prompt = (
        "You are AstraNav Copilot, an expert assistant for the AstraNav-LRIS lunar "
        "ice-detection and rover-routing system. You are helping ISRO mission scientists "
        "interpret data from Chandrayaan-2 DFSAR radar and OHRC imagery.\n\n"
        "CRITICAL RULES:\n"
        "1. Answer ONLY from the data provided in the <DATA> block below.\n"
        "2. If the question cannot be answered from the provided data, say explicitly: "
        "'The available data does not cover this question. Please check [endpoint name].'\n"
        "3. Never hallucinate numbers, coordinates, or science facts not in the data block.\n"
        "4. Keep answers concise and factual — 3–6 sentences maximum unless the question "
        "requires a table or list.\n"
        "5. When citing numbers, always include units (m³, Wh, K, degrees, %).\n"
        "6. Domain glossary for context: CPR=Circular Polarization Ratio (>1.0 = ice candidate), "
        "DOP=Degree of Polarization (<0.13 = ice candidate), LMRS=Lunar Mining Readiness Score "
        "(0–100 composite of RAI + CommVisibility + ThermalRisk), PSR=Permanently Shadowed Region "
        "(~25 K), RAI=Resource Accessibility Index.\n\n"
        f"<DATA>\n{context_block}\n</DATA>"
    )

    message = client.messages.create(
        model=_CLAUDE_MODEL,
        max_tokens=512,
        system=system_prompt,
        messages=[{"role": "user", "content": question}],
    )
    return message.content[0].text.strip()


# ---------------------------------------------------------------------------
# POST /api/copilot/ask
# ---------------------------------------------------------------------------

@router.post(
    "/api/copilot/ask",
    response_model=CopilotResponse,
    summary="Data-Grounded Chat Copilot",
    description=(
        "Ask a natural-language question about a region's ice candidates, "
        "hazards, or LMRS score.  The answer is grounded strictly on the "
        "retrieved mission data — no hallucination.  Requires "
        "`ANTHROPIC_API_KEY` env var; returns a canned fallback if unavailable."
    ),
)
async def copilot_ask(
    body: CopilotRequest,
    request: Request,
) -> CopilotResponse:

    data_sources: list[str] = []
    context_parts: list[str] = []

    # ── Region validation (soft — proceed with empty data if region unknown) ──
    region_display = body.region_id
    try:
        cfg = get_region_config(body.region_id)
        region_display = cfg.display_name
    except KeyError:
        context_parts.append(f"Region '{body.region_id}' is not registered in the system.")

    # ── Ice layer ─────────────────────────────────────────────────────────────
    ice_features: list[dict] = []
    try:
        ice_geojson = fetch_ice_layer(body.region_id)
        ice_features = ice_geojson.get("features", [])
        context_parts.append(_summarise_ice(ice_features))
        data_sources.append("ice-layer")
    except KeyError:
        context_parts.append("Ice layer: no data available for this region.")

    # ── Hazard layer ──────────────────────────────────────────────────────────
    try:
        hazard_geojson = fetch_hazard_layer(body.region_id)
        hazard_features = hazard_geojson.get("features", [])
        context_parts.append(_summarise_hazards(hazard_features))
        data_sources.append("hazard-layer")
    except KeyError:
        context_parts.append("Hazard layer: no data available for this region.")

    # ── LMRS breakdown (only if a context_point is provided) ─────────────────
    if body.context_point is not None:
        lat = body.context_point.lat
        lon = body.context_point.lon
        try:
            cache: GridCache = request.app.state.grid_cache
            grid = await cache.get(body.region_id)
            cfg_reg = get_region_config(body.region_id)
            lmrs_result = compute_lmrs(
                lat=lat,
                lon=lon,
                grid=grid,
                ice_features=ice_features,
                lander_lat=cfg_reg.lander_lat,
                lander_lon=cfg_reg.lander_lon,
                weights=LMRSWeights(),
                cost_config=CostConfig(battery_model=StaticBatteryModel()),
            )
            context_parts.append(
                f"\nLMRS breakdown for ({lat:.4f}, {lon:.4f}):\n" + _summarise_lmrs(lmrs_result)
            )
            data_sources.append(f"lmrs@({lat:.4f},{lon:.4f})")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Copilot: LMRS computation failed for context_point: %s", exc)
            context_parts.append(f"LMRS at ({lat:.4f},{lon:.4f}): computation failed ({exc}).")

    context_block = f"Region: {region_display} (ID: {body.region_id})\n\n" + "\n\n".join(context_parts)

    # ── Call LLM ──────────────────────────────────────────────────────────────
    answer: str
    model_used: str
    try:
        answer = _call_claude(context_block, body.question)
        model_used = _CLAUDE_MODEL
        logger.info("Copilot: region=%s answered via Claude.", body.region_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Copilot: Claude call failed (%s); using fallback.", exc)
        answer = _FALLBACK_ANSWER
        model_used = "fallback"

    return CopilotResponse(
        answer=answer,
        data_sources_used=data_sources,
        region_id=body.region_id,
        context_point=body.context_point,
        model_used=model_used,
    )
