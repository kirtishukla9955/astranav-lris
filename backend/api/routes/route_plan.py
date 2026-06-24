"""
api/routes/route_plan.py
------------------------
GET  /api/route          — Shadow-Hopping Pathfinder
POST /api/swarm/plan     — Multi-Rover Swarm Planning

Both endpoints share the same underlying plan_route() call from the
pathfinder package.  The swarm endpoint simply calls it N times
(independently per rover) and aggregates results.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http_status

from api.models import (
    GeoPointOut,
    RouteResponse,
    RoverRouteOut,
    SwarmPlanRequest,
    SwarmPlanResponse,
    WaypointOut,
)
from core.grid_cache import GridCache
from data.region_registry import get_region_config
from pathfinder import CostConfig, StaticBatteryModel, plan_route
from pathfinder.types import RouteResult

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Routing"])


# ---------------------------------------------------------------------------
# Dependency: pull GridCache from app state
# ---------------------------------------------------------------------------

def get_grid_cache(request: Request) -> GridCache:
    return request.app.state.grid_cache


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _route_result_to_waypoints(result: RouteResult) -> list[WaypointOut]:
    return [
        WaypointOut(
            lat=wp.lat,
            lon=wp.lon,
            type=wp.type.value,
            cumulative_distance_m=wp.cumulative_distance_m,
            cumulative_energy_wh=wp.cumulative_energy_wh,
            battery_pct_remaining=wp.battery_pct_remaining,
            is_shadowed=wp.is_shadowed,
            solar_illumination=wp.solar_illumination,
        )
        for wp in result.waypoints
    ]


def _build_cost_config(
    ice_seeking: bool,
    shadow_penalty_weight: float,
    use_predictive_battery: bool,
    request: Request | None = None,
) -> CostConfig:
    """
    Construct a CostConfig.

    When use_predictive_battery=True, attempts to use the ML battery model
    stored in app.state.ml_battery_model (loaded at startup by main.py).
    Falls back to StaticBatteryModel transparently if:
      - use_predictive_battery is False, OR
      - the ML model pickle was not found / failed to load.
    """
    battery_model = StaticBatteryModel()

    if use_predictive_battery and request is not None:
        ml_model = getattr(request.app.state, "ml_battery_model", None)
        if ml_model is not None:
            battery_model = ml_model  # type: ignore[assignment]

    return CostConfig(
        ice_seeking_mode=ice_seeking,
        shadow_penalty_weight=shadow_penalty_weight,
        battery_model=battery_model,
    )


# ---------------------------------------------------------------------------
# GET /api/route
# ---------------------------------------------------------------------------

@router.get(
    "/api/route",
    response_model=RouteResponse,
    summary="Shadow-Hopping Pathfinder",
    description=(
        "Compute an optimal rover route between two coordinates, automatically "
        "inserting **solar pitstop** waypoints whenever the dark-dwell battery "
        "budget would be exceeded.  Shadowed cells are expensive (not impassable); "
        "hazard cells (slope >15°, boulders >0.5 m) are hard walls."
    ),
    responses={
        404: {"description": "region_id not found in registry"},
        422: {"description": "No valid path (start/end inside hazard, or grid fully blocked)"},
    },
)
async def get_route(
    start_lat: Annotated[float, Query(description="Start latitude (degrees)")],
    start_lon: Annotated[float, Query(description="Start longitude (degrees)")],
    end_lat: Annotated[float, Query(description="End / goal latitude (degrees)")],
    end_lon: Annotated[float, Query(description="End / goal longitude (degrees)")],
    region_id: Annotated[str, Query(description="Region identifier from the registry")],
    ice_seeking: Annotated[bool, Query(description="Bias route toward high-volume ice cells")] = False,
    initial_battery_pct: Annotated[float, Query(ge=0, le=100, description="Starting charge 0–100")] = 100.0,
    dark_budget_wh: Annotated[float, Query(gt=0, description="Max Wh in shadow before forced pitstop")] = 80.0,
    shadow_penalty_weight: Annotated[float, Query(gt=0, description="Cost multiplier for shadowed cells")] = 5.0,
    use_predictive_battery: Annotated[bool, Query(description="Use ML battery model (Feature 5)")] = False,
    request: Request = None,
    cache: GridCache = Depends(get_grid_cache),
) -> RouteResponse:

    # ── Region validation ────────────────────────────────────────────────────
    try:
        get_region_config(region_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ── Build / retrieve grid ────────────────────────────────────────────────
    try:
        grid = await cache.get(region_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ── Run pathfinder ───────────────────────────────────────────────────────
    config = _build_cost_config(ice_seeking, shadow_penalty_weight, use_predictive_battery, request)

    result = plan_route(
        grid=grid,
        start_lat=start_lat,
        start_lon=start_lon,
        end_lat=end_lat,
        end_lon=end_lon,
        config=config,
        initial_battery_pct=initial_battery_pct,
        dark_budget_wh=dark_budget_wh,
    )

    if not result.route_found:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "detail": "no_route_found",
                "reason": result.warnings[0] if result.warnings else "unknown",
            },
        )

    logger.info(
        "route: region=%s dist=%.1f m energy=%.1f Wh pitstops=%d",
        region_id, result.total_distance_m, result.total_energy_wh, result.total_pitstops,
    )

    return RouteResponse(
        region_id=region_id,
        start=GeoPointOut(lat=start_lat, lon=start_lon),
        end=GeoPointOut(lat=end_lat, lon=end_lon),
        route_found=True,
        total_distance_m=result.total_distance_m,
        total_energy_wh=result.total_energy_wh,
        total_pitstops=result.total_pitstops,
        total_waypoints=result.total_waypoints,
        ice_seeking_mode=ice_seeking,
        use_predictive_battery=use_predictive_battery,
        waypoints=_route_result_to_waypoints(result),
        warnings=result.warnings,
    )


# ---------------------------------------------------------------------------
# POST /api/swarm/plan
# ---------------------------------------------------------------------------

@router.post(
    "/api/swarm/plan",
    response_model=SwarmPlanResponse,
    summary="Multi-Rover Swarm Route Planning",
    description=(
        "Plan independent routes for up to **2 rovers** simultaneously.  "
        "Each rover is planned independently via the same A* engine.  "
        "Collision avoidance between rovers is not implemented (noted for judges)."
    ),
    responses={
        404: {"description": "region_id not found"},
    },
)
async def swarm_plan(
    body: SwarmPlanRequest,
    request: Request = None,
    cache: GridCache = Depends(get_grid_cache),
) -> SwarmPlanResponse:

    try:
        get_region_config(body.region_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        grid = await cache.get(body.region_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    plans: list[RoverRouteOut] = []
    global_warnings: list[str] = []

    for rover in body.rovers:
        config = _build_cost_config(
            ice_seeking=rover.ice_seeking,
            shadow_penalty_weight=body.shadow_penalty_weight,
            use_predictive_battery=body.use_predictive_battery,
            request=request,
        )
        result = plan_route(
            grid=grid,
            start_lat=rover.start_lat,
            start_lon=rover.start_lon,
            end_lat=rover.end_lat,
            end_lon=rover.end_lon,
            config=config,
            initial_battery_pct=rover.initial_battery_pct,
            dark_budget_wh=body.dark_budget_wh,
        )

        if not result.route_found:
            global_warnings.append(
                f"rover '{rover.rover_id}': no route found — "
                + (result.warnings[0] if result.warnings else "unknown reason")
            )

        plans.append(
            RoverRouteOut(
                rover_id=rover.rover_id,
                route_found=result.route_found,
                total_distance_m=result.total_distance_m,
                total_energy_wh=result.total_energy_wh,
                total_pitstops=result.total_pitstops,
                total_waypoints=result.total_waypoints,
                waypoints=_route_result_to_waypoints(result) if result.route_found else [],
                warnings=result.warnings,
            )
        )

    logger.info(
        "swarm: region=%s rovers=%d planned", body.region_id, len(plans)
    )

    return SwarmPlanResponse(
        region_id=body.region_id,
        total_rovers=len(plans),
        plans=plans,
        collision_avoidance="not_implemented",
        warnings=global_warnings,
    )
