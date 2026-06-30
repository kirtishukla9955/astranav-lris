import sys
import os
import logging
from datetime import timezone, datetime
from typing import Optional, Literal
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse

# Setup isolated sys.path to safely import from SID_4_BACKS and root, swapping pathfinder temporarily
_BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LEGACY = os.path.join(_BACKEND, "legacy")
orig_pathfinder = sys.modules.get("pathfinder")
if "pathfinder" in sys.modules:
    sys.modules.pop("pathfinder")

sys.path.insert(0, _LEGACY)
try:
    import pathfinder as root_pathfinder
    from SID_4_BACKS.illumination import simulate_illumination
    from SID_4_BACKS.report import assemble_report_data, to_csv, to_pdf
    from SID_4_BACKS.briefing import generate_mission_briefing
    from cost_grid import generate_mock_cost_grid, CostGrid
    from confidence import apply_confidence_to_route as root_apply_confidence, generate_grid_confidence as root_gen_confidence
    from lmrs import compute_lmrs as root_compute_lmrs
    from schemas import (
        RouteResponse as RootRouteResponse,
        LMRSResponse as RootLMRSResponse,
        Waypoint as RootWaypoint,
        ResourceAccessibilityIndex as RootRAI,
        CommVisibility as RootComm,
        ThermalRisk as RootThermal,
        IceLayerData as RootIce,
        HazardSummary as RootHazard,
        MissionReportData as RootReport,
    )
finally:
    sys.path.pop(0)
    if orig_pathfinder:
        sys.modules["pathfinder"] = orig_pathfinder

from api.models import (
    IlluminationTimelapseResponse,
    MissionSnapshotResponse,
    MissionBriefingResponse,
    MissionBriefingRequest,
    RouteResponse,
    LMRSResponse,
    IceLayerData,
    RouteConfidenceResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Integrated Features"])

# Cached singleton mock grid to support legacy CostGrid operations
GLOBAL_GRID: CostGrid = generate_mock_cost_grid(width=100, height=100)

@router.get("/api/illumination-timelapse", response_model=IlluminationTimelapseResponse)
async def get_illumination_timelapse(region_id: str, num_frames: int = 100):
    num_frames = max(1, min(num_frames, 500))
    try:
        return simulate_illumination(
            grid=GLOBAL_GRID,
            region_id=region_id,
            num_frames=num_frames,
        )
    except Exception as e:
        logger.error(f"Timelapse generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/mission-snapshot", response_model=MissionSnapshotResponse)
async def get_mission_snapshot(region_id: str):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    # Extract coordinates in middle of grid
    target_lat = (GLOBAL_GRID.height // 2) * 0.0001
    target_lon = (GLOBAL_GRID.width  // 2) * 0.0001
    
    # Build route and LMRS via root packages (loaded at module level)
    route = root_pathfinder.build_route(GLOBAL_GRID, (0, 0), (99, 99), region_id)
    if route:
        root_apply_confidence(route)
    else:
        route = RootRouteResponse(
            route_id="no-route",
            region_id=region_id,
            waypoints=[],
            total_distance_m=0.0,
            total_energy_wh=0.0,
        )
        
    lmrs = root_compute_lmrs(
        target_lat=target_lat,
        target_lon=target_lon,
        start_lat=0.0,
        start_lon=0.0,
        region_id=region_id,
        grid=GLOBAL_GRID,
    )
    
    conf_data = root_gen_confidence(GLOBAL_GRID.width, GLOBAL_GRID.height)

    ice_layer = IceLayerData(
        lat=target_lat,
        lon=target_lon,
        ice_volume_m3=800.0,
        ice_depth_m=1.5,
        confidence=0.85,
    )

    # Compile hazard summary stats
    import math
    import numpy as np
    slopes, obstacles, shadows = [], 0, 0
    stride = 5
    total = 0
    for gy in range(0, GLOBAL_GRID.height, stride):
        for gx in range(0, GLOBAL_GRID.width, stride):
            cost = GLOBAL_GRID.get_traversal_cost(gx, gy)
            total += 1
            if cost == float("inf"):
                obstacles += 1
                slopes.append(35.0)
            else:
                slopes.append(math.sqrt(max(0.0, (cost - 1.0) * 100.0)))
            if GLOBAL_GRID.is_in_shadow(gx, gy):
                shadows += 1

    slopes_arr = np.array(slopes, dtype=float)
    hazard_summary = {
        "slope_mean": round(float(np.mean(slopes_arr)), 2),
        "slope_max":  round(float(np.max(slopes_arr)), 2),
        "obstacle_pct": round(obstacles / total * 100.0, 2),
        "shadow_pct":   round(shadows  / total * 100.0, 2),
    }

    # Format route response to match backend structure
    formatted_route = {
        "region_id": region_id,
        "start": {"lat": 0.0, "lon": 0.0},
        "end": {"lat": target_lat, "lon": target_lon},
        "route_found": route.route_id != "no-route",
        "total_distance_m": route.total_distance_m,
        "total_energy_wh": route.total_energy_wh,
        "total_pitstops": len([w for w in route.waypoints if w.type == "solar_pitstop"]),
        "total_waypoints": len(route.waypoints),
        "ice_seeking_mode": False,
        "use_predictive_battery": False,
        "waypoints": [
            {
                "lat": w.lat,
                "lon": w.lon,
                "type": w.type,
                "cumulative_distance_m": w.cumulative_distance_m,
                "cumulative_energy_wh": w.cumulative_energy_wh,
                "battery_pct_remaining": getattr(w, "battery_pct_remaining", 100.0),
                "is_shadowed": w.is_in_shadow,
                "solar_illumination": getattr(w, "confidence", 1.0),
            }
            for w in route.waypoints
        ],
        "warnings": []
    }

    formatted_lmrs = {
        "region_id": region_id,
        "lat": target_lat,
        "lon": target_lon,
        "lmrs_score": lmrs.lmrs_score,
        "weights_used": {
            "rai": 0.45,
            "comm_visibility": 0.25,
            "thermal_risk": 0.30,
        },
        "rai": {
            "score": lmrs.lmrs_score,
            "ice_volume_m3": lmrs.rai.ice_volume_m3,
            "ice_depth_m": lmrs.rai.ice_depth_m,
            "confidence": lmrs.confidence,
            "extraction_difficulty": min(1.0, max(0.0, lmrs.rai.extraction_difficulty_score / 100.0)),
            "nearest_ice_distance_m": 0.0,
        },
        "comm_visibility": {
            "score": lmrs.comm_visibility.signal_strength_pct,
            "los_fraction": 1.0 if lmrs.comm_visibility.earth_line_of_sight else 0.0,
            "occlusion_reason": None,
        },
        "thermal_risk": {
            "score": lmrs.thermal_risk.thermal_risk_score,
            "energy_cost_wh": lmrs.thermal_risk.total_energy_wh,
            "mean_temperature_k": 120.0,
            "dark_dwell_fraction": lmrs.thermal_risk.shadow_exposure_time_min / 100.0,
        },
        "data_freshness": {}
    }

    return MissionSnapshotResponse(
        region_id=region_id,
        snapshot_timestamp=timestamp,
        ice_layer=ice_layer,
        hazard_summary=hazard_summary,
        route=formatted_route, # type: ignore
        lmrs=formatted_lmrs, # type: ignore
        route_confidence=RouteConfidenceResponse(
            region_id=region_id,
            grid_confidence=conf_data
        )
    )

@router.get("/api/export/csv")
async def export_csv(region_id: str, lat: float, lon: float):
    try:
        data = assemble_report_data(lat, lon, region_id, GLOBAL_GRID)
        csv_str = to_csv(data)
        filename = f"astranav_mission_{region_id}_{lat:.4f}_{lon:.4f}.csv"
        return StreamingResponse(
            iter([csv_str]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        logger.error(f"CSV export failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/export/pdf")
async def export_pdf(region_id: str, lat: float, lon: float):
    try:
        data = assemble_report_data(lat, lon, region_id, GLOBAL_GRID)
        pdf_bytes = to_pdf(data)
        filename = f"astranav_mission_{region_id}_{lat:.4f}_{lon:.4f}.pdf"
        return StreamingResponse(
            iter([pdf_bytes]),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        logger.error(f"PDF export failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def _map_lmrs_to_root(lmrs_obj) -> RootLMRSResponse:
    """Map a backend or client-supplied LMRS shape back to RootLMRSResponse."""
    if isinstance(lmrs_obj, dict):
        lmrs_score = lmrs_obj.get("lmrs_score", 0.0)
        confidence = lmrs_obj.get("confidence", 0.8)
        rai_data = lmrs_obj.get("rai", {})
        comm_data = lmrs_obj.get("comm_visibility", {})
        thermal_data = lmrs_obj.get("thermal_risk", {})
        
        ext_diff = rai_data.get("extraction_difficulty_score", rai_data.get("extraction_difficulty", 0.5))
        los = comm_data.get("earth_line_of_sight", comm_data.get("los_fraction", 1.0) >= 0.8)
        sig = comm_data.get("signal_strength_pct", comm_data.get("score", 100.0))
        
        thermal_score = thermal_data.get("thermal_risk_score", thermal_data.get("score", 0.0))
        shadow_min = thermal_data.get("shadow_exposure_time_min", thermal_data.get("dark_dwell_fraction", 0.0) * 120.0)
        energy_wh = thermal_data.get("total_energy_wh", thermal_data.get("energy_cost_wh", 0.0))
        
        ice_vol = rai_data.get("ice_volume_m3", 500.0)
        ice_dep = rai_data.get("ice_depth_m", 1.5)
        
        lat = lmrs_obj.get("lat", 0.0)
        lon = lmrs_obj.get("lon", 0.0)
        region_id = lmrs_obj.get("region_id", "default")
    else:
        lmrs_score = lmrs_obj.lmrs_score
        confidence = getattr(lmrs_obj, "confidence", 0.8)
        
        ice_vol = lmrs_obj.rai.ice_volume_m3
        ice_dep = lmrs_obj.rai.ice_depth_m
        ext_diff = getattr(lmrs_obj.rai, "extraction_difficulty_score", getattr(lmrs_obj.rai, "extraction_difficulty", 0.5))
        
        los = getattr(lmrs_obj.comm_visibility, "earth_line_of_sight", getattr(lmrs_obj.comm_visibility, "los_fraction", 1.0) >= 0.8)
        sig = getattr(lmrs_obj.comm_visibility, "signal_strength_pct", getattr(lmrs_obj.comm_visibility, "score", 100.0))
        
        thermal_score = getattr(lmrs_obj.thermal_risk, "thermal_risk_score", getattr(lmrs_obj.thermal_risk, "score", 0.0))
        shadow_min = getattr(lmrs_obj.thermal_risk, "shadow_exposure_time_min", getattr(lmrs_obj.thermal_risk, "dark_dwell_fraction", 0.0) * 120.0)
        energy_wh = getattr(lmrs_obj.thermal_risk, "total_energy_wh", getattr(lmrs_obj.thermal_risk, "energy_cost_wh", 0.0))
        
        lat = lmrs_obj.lat
        lon = lmrs_obj.lon
        region_id = lmrs_obj.region_id

    return RootLMRSResponse(
        lat=lat,
        lon=lon,
        region_id=region_id,
        lmrs_score=lmrs_score,
        rai=RootRAI(
            ice_volume_m3=ice_vol,
            ice_depth_m=ice_dep,
            extraction_difficulty_score=ext_diff,
        ),
        comm_visibility=RootComm(
            earth_line_of_sight=los,
            signal_strength_pct=sig,
        ),
        thermal_risk=RootThermal(
            total_energy_wh=energy_wh,
            shadow_exposure_time_min=shadow_min,
            thermal_risk_score=thermal_score,
        ),
        confidence=confidence,
    )

def _map_route_to_root(route_obj) -> Optional[RootRouteResponse]:
    """Map a backend or client-supplied RouteResponse shape to RootRouteResponse."""
    if route_obj is None:
        return None
        
    if isinstance(route_obj, dict):
        route_id = route_obj.get("route_id", "default")
        region_id = route_obj.get("region_id", "default")
        total_distance = route_obj.get("total_distance_m", 0.0)
        total_energy = route_obj.get("total_energy_wh", 0.0)
        waypoints_in = route_obj.get("waypoints", [])
    else:
        route_id = getattr(route_obj, "route_id", "default")
        region_id = route_obj.region_id
        total_distance = route_obj.total_distance_m
        total_energy = route_obj.total_energy_wh
        waypoints_in = route_obj.waypoints

    waypoints_out = []
    for w in waypoints_in:
        if isinstance(w, dict):
            w_lat = w.get("lat", 0.0)
            w_lon = w.get("lon", 0.0)
            w_type = w.get("type", "transit")
            w_dist = w.get("cumulative_distance_m", 0.0)
            w_energy = w.get("cumulative_energy_wh", 0.0)
            w_shadow = w.get("is_shadowed", w.get("is_in_shadow", False))
            w_conf = w.get("solar_illumination", w.get("confidence", 1.0))
        else:
            w_lat = w.lat
            w_lon = w.lon
            w_type = w.type
            w_dist = w.cumulative_distance_m
            w_energy = w.cumulative_energy_wh
            w_shadow = getattr(w, "is_shadowed", getattr(w, "is_in_shadow", False))
            w_conf = getattr(w, "solar_illumination", getattr(w, "confidence", 1.0))
            
        waypoints_out.append(RootWaypoint(
            lat=w_lat,
            lon=w_lon,
            type=w_type, # type: ignore
            cumulative_distance_m=w_dist,
            cumulative_energy_wh=w_energy,
            is_in_shadow=w_shadow,
            confidence=w_conf,
        ))

    return RootRouteResponse(
        route_id=route_id,
        region_id=region_id,
        waypoints=waypoints_out,
        total_distance_m=total_distance,
        total_energy_wh=total_energy,
    )

@router.get("/api/mission-briefing", response_model=MissionBriefingResponse)
async def get_mission_briefing(lat: float, lon: float, region_id: str):
    try:
        res = generate_mission_briefing(
            lat=lat,
            lon=lon,
            region_id=region_id,
            grid=GLOBAL_GRID,
        )
        return MissionBriefingResponse(
            lat=lat,
            lon=lon,
            region_id=region_id,
            briefing_text=res.briefing_text,
            briefing=res.briefing_text,
            generated_by=res.generated_by, # type: ignore
        )
    except Exception as e:
        logger.error(f"Mission briefing GET failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/mission-briefing", response_model=MissionBriefingResponse)
async def post_mission_briefing(request: MissionBriefingRequest):
    try:
        root_lmrs = _map_lmrs_to_root(request.lmrs)
        root_route = _map_route_to_root(request.route)
        
        res = generate_mission_briefing(
            lat=request.lmrs.lat,
            lon=request.lmrs.lon,
            region_id=request.lmrs.region_id,
            grid=GLOBAL_GRID,
            lmrs_resp=root_lmrs,
            route_resp=root_route,
        )
        return MissionBriefingResponse(
            lat=request.lmrs.lat,
            lon=request.lmrs.lon,
            region_id=request.lmrs.region_id,
            briefing_text=res.briefing_text,
            briefing=res.briefing_text,
            generated_by=res.generated_by, # type: ignore
        )
    except Exception as e:
        logger.error(f"Mission briefing POST failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
