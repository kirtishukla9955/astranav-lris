"""
api/routes/lmrs.py
------------------
GET  /api/lmrs           — Lunar Mining Readiness Score (single point)
POST /api/lmrs/compare   — Multi-site comparison (2–5 points)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.models import (
    CommVisibilityOut,
    LMRSCompareRequest,
    LMRSCompareResponse,
    LMRSResponse,
    LMRSWithLabel,
    RAIBreakdownOut,
    ThermalRiskOut,
    WeightsOut,
)
from core.grid_cache import GridCache
from data.mock_fixtures import fetch_ice_layer
from data.region_registry import get_region_config
from scoring.lmrs_scorer import (
    LMRSResult,
    LMRSWeights,
    compute_lmrs,
)
from pathfinder import CostConfig, StaticBatteryModel

logger = logging.getLogger(__name__)
router = APIRouter(tags=["LMRS"])


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

def get_grid_cache(request: Request) -> GridCache:
    return request.app.state.grid_cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result_to_response(
    lat: float,
    lon: float,
    region_id: str,
    result: LMRSResult,
    weights: LMRSWeights,
    use_predictive_battery: bool,
) -> LMRSResponse:
    now_iso = datetime.now(timezone.utc).isoformat()
    return LMRSResponse(
        region_id=region_id,
        lat=lat,
        lon=lon,
        lmrs_score=result.lmrs_score,
        weights_used=WeightsOut(
            rai=weights.rai,
            comm_visibility=weights.comm_visibility,
            thermal_risk=weights.thermal_risk,
        ),
        rai=RAIBreakdownOut(
            score=result.rai.score,
            ice_volume_m3=result.rai.ice_volume_m3,
            ice_depth_m=result.rai.ice_depth_m,
            confidence=result.rai.confidence,
            extraction_difficulty=result.rai.extraction_difficulty,
            nearest_ice_distance_m=result.rai.nearest_ice_distance_m,
        ),
        comm_visibility=CommVisibilityOut(
            score=result.comm_visibility.score,
            los_fraction=result.comm_visibility.los_fraction,
            occlusion_reason=result.comm_visibility.occlusion_reason,
        ),
        thermal_risk=ThermalRiskOut(
            score=result.thermal_risk.score,
            energy_cost_wh=result.thermal_risk.energy_cost_wh,
            mean_temperature_k=result.thermal_risk.mean_temperature_k,
            dark_dwell_fraction=result.thermal_risk.dark_dwell_fraction,
        ),
        data_freshness={
            "ice_layer_cached_at": now_iso,
            "hazard_layer_cached_at": now_iso,
        },
    )


async def _score_point(
    lat: float,
    lon: float,
    region_id: str,
    weights: LMRSWeights,
    cache: GridCache,
    use_predictive_battery: bool = False,
) -> LMRSResult:
    """Shared scoring logic for both single-point and compare endpoints."""
    grid = await cache.get(region_id)
    cfg = get_region_config(region_id)

    try:
        ice_geojson = fetch_ice_layer(region_id)
        ice_features = ice_geojson.get("features", [])
    except KeyError:
        ice_features = []

    cost_config = CostConfig(battery_model=StaticBatteryModel())

    return compute_lmrs(
        lat=lat,
        lon=lon,
        grid=grid,
        ice_features=ice_features,
        lander_lat=cfg.lander_lat,
        lander_lon=cfg.lander_lon,
        weights=weights,
        cost_config=cost_config,
    )


# ---------------------------------------------------------------------------
# GET /api/lmrs
# ---------------------------------------------------------------------------

@router.get(
    "/api/lmrs",
    response_model=LMRSResponse,
    summary="Lunar Mining Readiness Score — Single Point",
    description=(
        "Compute a **0–100 LMRS composite score** for any map coordinate.  "
        "Three sub-scores are combined: Resource Accessibility Index (RAI), "
        "Communication Visibility, and Thermal/Energy Risk.  "
        "Weights are configurable and must sum to 1.0."
    ),
    responses={
        404: {"description": "region_id not found"},
    },
)
async def get_lmrs(
    lat: Annotated[float, Query(description="Query latitude (degrees)")],
    lon: Annotated[float, Query(description="Query longitude (degrees)")],
    region_id: Annotated[str, Query(description="Region identifier")],
    rai_weight: Annotated[float, Query(gt=0, lt=1, description="Weight for RAI (must sum to 1 with others)")] = 0.45,
    comm_weight: Annotated[float, Query(gt=0, lt=1, description="Weight for comm-visibility")] = 0.25,
    thermal_weight: Annotated[float, Query(gt=0, lt=1, description="Weight for thermal-risk")] = 0.30,
    use_predictive_battery: Annotated[bool, Query()] = False,
    cache: GridCache = Depends(get_grid_cache),
) -> LMRSResponse:

    # Region check
    try:
        get_region_config(region_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Weights validation
    total = rai_weight + comm_weight + thermal_weight
    if abs(total - 1.0) > 0.02:
        raise HTTPException(
            status_code=422,
            detail=f"rai_weight + comm_weight + thermal_weight must sum to 1.0 (got {total:.3f})",
        )
    weights = LMRSWeights(rai=rai_weight, comm_visibility=comm_weight, thermal_risk=thermal_weight)

    result = await _score_point(lat, lon, region_id, weights, cache, use_predictive_battery)

    logger.info("lmrs: region=%s lat=%.4f lon=%.4f score=%.1f",
                region_id, lat, lon, result.lmrs_score)

    return _result_to_response(lat, lon, region_id, result, weights, use_predictive_battery)


# ---------------------------------------------------------------------------
# POST /api/lmrs/compare
# ---------------------------------------------------------------------------

@router.post(
    "/api/lmrs/compare",
    response_model=LMRSCompareResponse,
    summary="LMRS Multi-Site Comparison",
    description=(
        "Score **2–5 candidate sites** in one call.  "
        "Returns results ranked descending by LMRS score, with a `recommended` "
        "field identifying the winner — no client-side math required."
    ),
    responses={
        404: {"description": "region_id not found"},
    },
)
async def compare_lmrs(
    body: LMRSCompareRequest,
    cache: GridCache = Depends(get_grid_cache),
) -> LMRSCompareResponse:

    try:
        get_region_config(body.region_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Build weights (use defaults if body.weights is None)
    if body.weights is not None:
        weights = LMRSWeights(
            rai=body.weights.rai,
            comm_visibility=body.weights.comm_visibility,
            thermal_risk=body.weights.thermal_risk,
        )
    else:
        weights = LMRSWeights()

    # Score each point (sequential — for 2–5 points this is fast enough)
    scored: list[LMRSWithLabel] = []
    for pt in body.points:
        result = await _score_point(
            lat=pt.lat,
            lon=pt.lon,
            region_id=body.region_id,
            weights=weights,
            cache=cache,
            use_predictive_battery=body.use_predictive_battery,
        )
        response = _result_to_response(
            pt.lat, pt.lon, body.region_id, result, weights, body.use_predictive_battery
        )
        scored.append(
            LMRSWithLabel(
                label=pt.label,
                **response.model_dump(),
            )
        )

    # Sort descending by composite score
    scored.sort(key=lambda x: x.lmrs_score, reverse=True)
    best = scored[0]

    logger.info("lmrs/compare: region=%s sites=%d winner='%s' score=%.1f",
                body.region_id, len(scored), best.label, best.lmrs_score)

    return LMRSCompareResponse(
        region_id=body.region_id,
        recommended=best.label,
        recommended_score=best.lmrs_score,
        results=scored,
    )
