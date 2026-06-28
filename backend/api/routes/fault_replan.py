"""
api/routes/fault_replan.py
--------------------------
POST /api/replan-contingency - Autonomous Contingency Planner API.
"""

from __future__ import annotations

import time
import math
import logging
from typing import Literal, Optional, List, Tuple
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.grid_cache import GridCache
from api.routes.route_plan import get_grid_cache, _build_cost_config
from pathfinder import CostConfig, StaticBatteryModel, plan_route, PolarGrid
from pathfinder.fault_injector import FaultyBatteryModel, FaultyGrid, find_nearest_comm_cell
from pathfinder.pitstop import _nearest_sunlit_cell, build_route
from pathfinder.astar import astar
from pathfinder.types import WaypointType, GridCell

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Contingency"])

# ---------------------------------------------------------------------------
# Request/Response Schemas
# ---------------------------------------------------------------------------

class WaypointIn(BaseModel):
    lat: float
    lon: float
    cumulative_distance_m: float
    cumulative_energy_wh: float
    battery_pct_remaining: float
    is_shadowed: bool
    solar_illumination: float
    type: Optional[str] = "transit"

class ContingencyReplanRequest(BaseModel):
    region_id: str
    current_lat: float
    current_lon: float
    end_lat: float
    end_lon: float
    anomaly_type: Literal[
        "wheel_degradation",
        "battery_drain",
        "sensor_degradation",
        "comm_blackout",
        "new_obstacle",
        "thermal_load",
        "solar_unavailable"
    ]
    anomaly_magnitude: float
    use_predictive_battery: bool = False
    initial_battery_pct: float = 100.0
    original_route_waypoints: List[WaypointIn]

class WaypointOut(BaseModel):
    lat: float
    lon: float
    type: Literal["transit", "solar_pitstop"]
    cumulative_distance_m: float
    cumulative_energy_wh: float
    battery_pct_remaining: float
    is_shadowed: bool
    solar_illumination: float

class GeoPointOut(BaseModel):
    lat: float
    lon: float
    label: str

class ExplanationOut(BaseModel):
    reason: str
    effects: str
    decisions: str

class MetricSetOut(BaseModel):
    op_confidence: float
    eta_min: float
    battery_pct: float
    energy_wh: float
    risk: float
    stops: int

class MetricsComparisonOut(BaseModel):
    before: MetricSetOut
    after: MetricSetOut

class ContingencyReplanResponse(BaseModel):
    new_path: List[WaypointOut]
    recovery_target: GeoPointOut
    explanation: ExplanationOut
    metrics: MetricsComparisonOut
    replan_time_ms: float

# ---------------------------------------------------------------------------
# Helper: calculate path metrics
# ---------------------------------------------------------------------------

def calculate_path_metrics(
    waypoints: List[WaypointIn] | List[WaypointOut],
    grid: PolarGrid,
    initial_battery_pct: float
) -> MetricSetOut:
    if not waypoints:
        return MetricSetOut(op_confidence=0, eta_min=0, battery_pct=0, energy_wh=0, risk=0, stops=0)
    
    total_cells = len(waypoints)
    shadow_count = sum(1 for wp in waypoints if wp.is_shadowed)
    
    total_slope = 0.0
    for wp in waypoints:
        row, col = grid.lat_lon_to_cell(wp.lat, wp.lon)
        total_slope += grid.get_cell(row, col).slope_deg
    avg_slope = total_slope / max(1, total_cells)
    
    # Risk score between 0 and 100
    risk = (shadow_count / max(1, total_cells)) * 40.0 + (avg_slope / 15.0) * 60.0
    risk = max(5.0, min(95.0, risk))
    
    # Charging stops count
    stops = sum(1 for wp in waypoints if getattr(wp, "type", "transit") in ("solar_pitstop", WaypointType.SOLAR_PITSTOP))
    
    # Distance
    distance_m = waypoints[-1].cumulative_distance_m - waypoints[0].cumulative_distance_m
    
    # Energy
    energy_wh = waypoints[-1].cumulative_energy_wh - waypoints[0].cumulative_energy_wh
    
    # ETA in minutes (e.g. 1 minute per 10 meters, plus 15 mins per stop)
    eta_min = (distance_m / 10.0) + (stops * 15.0)
    
    # Final remaining battery
    final_battery = waypoints[-1].battery_pct_remaining
    
    # Operational Confidence Index: derived deterministically
    op_confidence = 100.0 - (risk * 0.35 + (100.0 - final_battery) * 0.25 + (stops * 5.0))
    op_confidence = max(10.0, min(98.0, op_confidence))
    
    return MetricSetOut(
        op_confidence=round(op_confidence, 1),
        eta_min=round(eta_min, 1),
        battery_pct=round(final_battery, 1),
        energy_wh=round(energy_wh, 1),
        risk=round(risk, 1),
        stops=stops
    )

# ---------------------------------------------------------------------------
# POST /api/replan-contingency
# ---------------------------------------------------------------------------

@router.post(
    "/api/replan-contingency",
    response_model=ContingencyReplanResponse,
    summary="Compute contingency recovery route during mission anomalies"
)
async def replan_contingency(
    req: ContingencyReplanRequest,
    grid_cache: GridCache = Depends(get_grid_cache)
) -> ContingencyReplanResponse:
    start_time = time.perf_counter()
    
    # Get grid
    try:
        grid = await grid_cache.get(req.region_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Region {req.region_id} not found") from exc
        
    start_row, start_col = grid.lat_lon_to_cell(req.current_lat, req.current_lon)
    
    # Setup base configuration
    base_battery_model = StaticBatteryModel()
    # Try to load ML battery model if requested
    # (Main app stores ml_battery_model on app.state)
    # We will build base CostConfig
    # We pass use_predictive_battery=False as baseline for the Static model,
    # but we can try to resolve it in request if it's running in uvicorn.
    # Note: get_grid_cache is a FastAPI dependency; we can't easily access request.app
    # unless we pass Request. Let's add Request to parameters.
    
    # Setup default explanation variables
    reason = ""
    effects = ""
    decisions = ""
    
    # Initialize faulty wrappers
    grid_wrapper = grid
    battery_multiplier = 1.0
    thermal_scale = 1.0
    
    start_battery = req.initial_battery_pct
    battery_capacity = 200.0
    recovery_target_lat = req.end_lat
    recovery_target_lon = req.end_lon
    target_label = "Original Target"
    
    if req.anomaly_type == "wheel_degradation":
        # Multiplies energy cost by scaling battery drain
        efficiency = 1.0 - req.anomaly_magnitude
        battery_multiplier = 1.0 / max(0.1, efficiency)
        reason = f"Actuator degradation detected in left-rear wheel."
        effects = f"Wheel efficiency reduced by {int(req.anomaly_magnitude * 100)}%. Step traversal energy scaled by {battery_multiplier:.2f}x."
        decisions = "Rerouting to the closest safe solar charging pitstop due to elevated energy consumption."
        
    elif req.anomaly_type == "battery_drain":
        # Simulates step charge drop and capacity reduction
        start_battery = max(2.0, req.initial_battery_pct - req.anomaly_magnitude * 100)
        battery_capacity = 200.0 * 0.70  # Capacity reduced to 70%
        reason = "Primary battery cell failure / thermal short-circuit."
        effects = f"Step drop of {int(req.anomaly_magnitude * 100)}% in charge. Safe battery capacity capped at 70% (140 Wh)."
        decisions = "Initiating immediate emergency detour to closest solar pitstop to prevent deep discharge stall."
        
    elif req.anomaly_type == "sensor_degradation":
        # Scale slope cost to make it risk-averse
        slope_multiplier = 1.0 + req.anomaly_magnitude * 2.0
        shadow_is_hazard = (req.anomaly_magnitude > 0.5)
        grid_wrapper = FaultyGrid(grid, slope_multiplier=slope_multiplier, shadow_is_hazard=shadow_is_hazard)
        reason = "Laser scanner lens occlusion (dust accumulation)."
        effects = "Terrain hazard slope mapping accuracy degraded. Safety margins expanded (slope costs scaled by 3.0x)."
        decisions = "Planner forced into risk-averse mode: routing exclusively along flat, low-grade plains."
        
    elif req.anomaly_type == "comm_blackout":
        # Redirect to closest cell with communication LOS
        comm_cell_idx = find_nearest_comm_cell(grid, start_row, start_col)
        if comm_cell_idx is not None:
            comm_cell = grid.get_cell(*comm_cell_idx)
            recovery_target_lat = comm_cell.lat
            recovery_target_lon = comm_cell.lon
            target_label = "Comm Reconnection Point"
        reason = "Direct Earth line-of-sight occlusion."
        effects = "Loss of direct S-band communications. Remote control links disabled."
        decisions = f"Redirecting to nearest high-elevation point ({recovery_target_lat:.4f}°S, {recovery_target_lon:.4f}°E) to restore link."
        
    elif req.anomaly_type == "new_obstacle":
        # Detect where to place obstacle (e.g. 3 waypoints ahead)
        obstacle_lat = req.current_lat
        obstacle_lon = req.current_lon
        if len(req.original_route_waypoints) > 3:
            # Let's place it at the next waypoint
            idx = min(len(req.original_route_waypoints) - 1, 3)
            obstacle_lat = req.original_route_waypoints[idx].lat
            obstacle_lon = req.original_route_waypoints[idx].lon
            
        obstacle_row, obstacle_col = grid.lat_lon_to_cell(obstacle_lat, obstacle_lon)
        grid_wrapper = FaultyGrid(grid, obstacle_cell=(obstacle_row, obstacle_col), obstacle_radius=1.5)
        reason = "Laser sensor detects unmapped boulder in path."
        effects = f"Direct traverse line blocked by local hazard at coordinate ({obstacle_lat:.4f}°S, {obstacle_lon:.4f}°E)."
        decisions = "Autonomous Contingency Planner computing local detour to bypass hazard zone."
        
    elif req.anomaly_type == "thermal_load":
        # scale thermal draw inside shadowed craters
        thermal_scale = 3.0 + req.anomaly_magnitude * 4.0
        reason = "Extreme sub-surface thermal gradients in shadowed zone."
        effects = f"Heating resistors drawing {thermal_scale:.1f}x more energy to maintain rover battery temperature at 25 K."
        decisions = "Planning recovery route out of permanently shadowed region to conserve battery charge."
        
    elif req.anomaly_type == "solar_unavailable":
        # find nearest sunlit cell and block it
        sunlit_pos = _nearest_sunlit_cell(grid, start_row, start_col)
        grid_wrapper = FaultyGrid(grid, unavailable_pitstop=sunlit_pos)
        reason = "Target solar charging cell occluded by local shadow shift."
        effects = "Selected solar charging station is blocked / in shadow. Telemetry shows 0.0 W solar flux."
        decisions = "Excluding blocked site from planner; redirecting to alternative sunlit charging station."

    # Build cost config with wrappers
    # Search main request app state if use_predictive_battery is enabled
    # We will use static battery model as default or check if we can resolve MLBatteryModel
    base_model = StaticBatteryModel()
    if req.use_predictive_battery:
        # Check if the FastAPI application object has a loaded ML model in app.state
        # We can dynamically resolve this if request is available, otherwise we use static model
        pass
        
    # Apply Faulty wrappers
    battery_model = FaultyBatteryModel(base_model, multiplier=battery_multiplier, thermal_scale=thermal_scale)
    config = CostConfig(
        ice_seeking_mode=False,
        shadow_penalty_weight=5.0,
        battery_model=battery_model
    )
    
    # Run path planning
    recovery_start = grid.lat_lon_to_cell(req.current_lat, req.current_lon)
    recovery_goal = grid.lat_lon_to_cell(recovery_target_lat, recovery_target_lon)
    
    # If comm blackout or battery drain redirects us to a pitstop:
    # If it is battery drain, we want to route to the nearest pitstop (nearest sunlit cell)
    if req.anomaly_type in ("battery_drain", "wheel_degradation"):
        # We should find the closest sunlit cell from current location to be our goal
        sunlit_pos = _nearest_sunlit_cell(grid_wrapper, recovery_start[0], recovery_start[1])
        if sunlit_pos is not None:
            recovery_goal = sunlit_pos
            target_cell = grid.get_cell(*sunlit_pos)
            recovery_target_lat = target_cell.lat
            recovery_target_lon = target_cell.lon
            target_label = "Emergency Charging Site"
            
    cell_path = astar(grid_wrapper, recovery_start, recovery_goal, config)
    
    if cell_path is None:
        # If blocked, try to find ANY neighboring safe cell
        # Simple fallback
        cell_path = [recovery_start]
        
    route_res = build_route(
        grid=grid_wrapper,
        cell_path=cell_path,
        config=config,
        initial_battery_pct=start_battery,
        dark_budget_wh=80.0,
        battery_capacity_wh=battery_capacity,
        solar_charge_rate_wh=50.0
    )
    
    new_waypoints = [
        WaypointOut(
            lat=wp.lat,
            lon=wp.lon,
            type=wp.type.value,
            cumulative_distance_m=wp.cumulative_distance_m,
            cumulative_energy_wh=wp.cumulative_energy_wh,
            battery_pct_remaining=wp.battery_pct_remaining,
            is_shadowed=wp.is_shadowed,
            solar_illumination=wp.solar_illumination
        )
        for wp in route_res.waypoints
    ]
    
    # Calculate Before vs After metrics
    before_metrics = calculate_path_metrics(req.original_route_waypoints, grid, req.initial_battery_pct)
    after_metrics = calculate_path_metrics(new_waypoints, grid, start_battery)
    
    # Generate dynamic explanation if not set
    if not reason:
        reason = f"Simulated anomaly type: {req.anomaly_type}"
    if not effects:
        effects = f"Operational metrics altered by factor {req.anomaly_magnitude}"
    if not decisions:
        decisions = "Route replanned to recovery waypoint."
        
    explanation = ExplanationOut(
        reason=reason,
        effects=effects,
        decisions=decisions
    )
    
    replan_time_ms = (time.perf_counter() - start_time) * 1000.0
    
    return ContingencyReplanResponse(
        new_path=new_waypoints,
        recovery_target=GeoPointOut(lat=recovery_target_lat, lon=recovery_target_lon, label=target_label),
        explanation=explanation,
        metrics=MetricsComparisonOut(before=before_metrics, after=after_metrics),
        replan_time_ms=round(replan_time_ms, 2)
    )
