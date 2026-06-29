from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import json
import asyncio
import logging
from typing import Dict

# Configure production-ready logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("AstraNav-API")

# Import all schemas
from schemas import (
    RouteResponse, LMRSResponse, CompareRequest, CompareResponse,
    SwarmRouteRequest, SwarmRouteResponse, ExplainRequest, ExplainResponse,
    RouteConfidenceResponse
)

# Import logic modules
from cost_grid import generate_mock_cost_grid, CostGrid
from pathfinder import build_route
from lmrs import compute_lmrs
from confidence import generate_grid_confidence, apply_confidence_to_route
from comparison import compare_sites
from swarm import compute_swarm_routes, simulate_telemetry
from copilot import explain_routing_decision

app = FastAPI(title="AstraNav-LRIS API", description="Lunar Resource Intelligence & Autonomous Navigation System Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global Mock Grid State
# In production, this would be dynamically built or loaded from Member 1's data.
GLOBAL_GRID: CostGrid = generate_mock_cost_grid(width=100, height=100)

# Memory storage for active swarm routes
active_swarm_routes: Dict[str, Dict[str, RouteResponse]] = {}

@app.get("/api/route", response_model=RouteResponse)
async def get_route(start_lat: float, start_lon: float, end_lat: float, end_lon: float, region_id: str):
    logger.info(f"Route requested for region {region_id} from ({start_lat}, {start_lon}) to ({end_lat}, {end_lon})")
    start_x, start_y = int(start_lon * 10000), int(start_lat * 10000)
    goal_x, goal_y = int(end_lon * 10000), int(end_lat * 10000)
    
    start_x = max(0, min(GLOBAL_GRID.width - 1, start_x))
    start_y = max(0, min(GLOBAL_GRID.height - 1, start_y))
    goal_x = max(0, min(GLOBAL_GRID.width - 1, goal_x))
    goal_y = max(0, min(GLOBAL_GRID.height - 1, goal_y))
    
    route = build_route(GLOBAL_GRID, (start_x, start_y), (goal_x, goal_y), region_id)
    if route:
        apply_confidence_to_route(route)
        return route
    return RouteResponse(route_id="error", region_id=region_id, waypoints=[], total_distance_m=0.0, total_energy_wh=0.0)

@app.get("/api/lmrs", response_model=LMRSResponse)
async def get_lmrs(lat: float, lon: float, region_id: str):
    # Assuming start is (0,0) for mock calculation
    return compute_lmrs(target_lat=lat, target_lon=lon, start_lat=0.0, start_lon=0.0, region_id=region_id, grid=GLOBAL_GRID)

@app.post("/api/compare", response_model=CompareResponse)
async def compare_multiple_sites(request: CompareRequest):
    return compare_sites(request.points, start_lat=0.0, start_lon=0.0, region_id="global", grid=GLOBAL_GRID)

@app.post("/api/swarm-route", response_model=SwarmRouteResponse)
async def create_swarm_routes(request: SwarmRouteRequest):
    swarm_resp = compute_swarm_routes(request, GLOBAL_GRID)
    # Store for telemetry
    active_swarm_routes[request.region_id] = swarm_resp.routes
    return swarm_resp

@app.post("/api/explain", response_model=ExplainResponse)
async def explain_decision(request: ExplainRequest):
    return explain_routing_decision(request, GLOBAL_GRID)

@app.get("/api/route-confidence", response_model=RouteConfidenceResponse)
async def get_route_confidence(region_id: str):
    conf_data = generate_grid_confidence(GLOBAL_GRID.width, GLOBAL_GRID.height)
    return RouteConfidenceResponse(region_id=region_id, grid_confidence=conf_data)

@app.websocket("/ws/telemetry/{region_id}")
async def websocket_telemetry(websocket: WebSocket, region_id: str):
    await websocket.accept()
    if region_id not in active_swarm_routes:
        await websocket.send_text("No active swarm for this region.")
        await websocket.close()
        return
        
    routes = active_swarm_routes[region_id]
    try:
        async for telemetry_point in simulate_telemetry(routes):
            await websocket.send_json(telemetry_point)
    except WebSocketDisconnect:
        print(f"Client disconnected from region {region_id}")
@app.get("/api/illumination-timelapse", response_model=IlluminationTimelapseResponse)
async def get_illumination_timelapse(
    region_id: str,
    num_frames: int = 100,
):
    num_frames = max(1, min(num_frames, 500))
    return simulate_illumination(GLOBAL_GRID, region_id, num_frames)

from pydantic import BaseModel
from typing import List, Optional

class ReplanRequest(BaseModel):
    region_id: str
    current_lat: float
    current_lon: float
    end_lat: float
    end_lon: float
    anomaly_type: str
    anomaly_magnitude: float
    use_predictive_battery: bool = False
    initial_battery_pct: float = 100.0
    original_route_waypoints: List[dict]

@app.post("/api/replan-contingency")
async def replan_contingency(req: ReplanRequest):
    import time
    start_time = time.perf_counter()
    
    # Generate mock explanation based on anomaly type
    if req.anomaly_type == "wheel_degradation":
        reason = "Actuator degradation detected in wheel."
        effects = f"Wheel efficiency reduced by {int(req.anomaly_magnitude * 100)}%."
        decisions = "Rerouting to the closest safe solar charging pitstop."
    elif req.anomaly_type == "battery_drain":
        reason = "Primary battery cell failure."
        effects = f"Step drop of {int(req.anomaly_magnitude * 100)}% in charge."
        decisions = "Initiating emergency detour to solar pitstop."
    elif req.anomaly_type == "sensor_degradation":
        reason = "Laser scanner lens occlusion (dust accumulation)."
        effects = "Safety margins expanded (slope costs scaled by 3.0x)."
        decisions = "Planner forced into risk-averse routing mode."
    else:
        reason = f"Simulated anomaly type: {req.anomaly_type}"
        effects = f"Operational metrics altered by factor {req.anomaly_magnitude}"
        decisions = "Route replanned to recovery waypoint."

    # Mock route changes
    new_waypoints = req.original_route_waypoints.copy()
    if len(new_waypoints) > 1:
        # Simulate a detour by shifting coordinates slightly
        new_waypoints[1]["lat"] += 0.001
        new_waypoints[1]["lon"] += 0.001

    replan_time_ms = (time.perf_counter() - start_time) * 1000.0

    return {
        "new_path": new_waypoints,
        "recovery_target": {"lat": req.end_lat, "lon": req.end_lon, "label": "Emergency Target"},
        "explanation": {
            "reason": reason,
            "effects": effects,
            "decisions": decisions
        },
        "metrics": {
            "before": {"op_confidence": 95.0, "eta_min": 25.0, "battery_pct": 80.0, "energy_wh": 120.0, "risk": 15.0},
            "after": {"op_confidence": 75.0, "eta_min": 35.0, "battery_pct": 45.0, "energy_wh": 180.0, "risk": 35.0}
        },
        "replan_time_ms": round(replan_time_ms + 150.0, 2) # Add 150ms mock delay
    }

@app.get("/api/health")
async def health_check():
  
    return {
        "status": "ok",
        "region_ids_available": KNOWN_REGION_IDS,
    }


@app.get("/api/mission-snapshot", response_model=MissionSnapshotResponse)
async def get_mission_snapshot(region_id: str):
    
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    target_lat = (GLOBAL_GRID.height // 2) * 0.0001
    target_lon = (GLOBAL_GRID.width  // 2) * 0.0001

    route = build_route(GLOBAL_GRID, (0, 0), (99, 99), region_id)
    if route:
        apply_confidence_to_route(route)
    else:
        route = RouteResponse(
            route_id="no-route",
            region_id=region_id,
            waypoints=[],
            total_distance_m=0.0,
            total_energy_wh=0.0,
        )

    lmrs = compute_lmrs(
        target_lat=target_lat,
        target_lon=target_lon,
        start_lat=0.0,
        start_lon=0.0,
        region_id=region_id,
        grid=GLOBAL_GRID,
    )

    ice_layer = IceLayerData(
        lat=target_lat,
        lon=target_lon,
        ice_volume_m3=800.0,
        ice_depth_m=1.5,
        confidence=0.85,
    )

    import math as _math
    import numpy as _np
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
                slopes.append(_math.sqrt(max(0.0, (cost - 1.0) * 100.0)))
            if GLOBAL_GRID.is_in_shadow(gx, gy):
                shadows += 1

    slopes_arr = _np.array(slopes, dtype=float)
    hazard_summary = {
        "slope_mean": round(float(_np.mean(slopes_arr)), 2),
        "slope_max":  round(float(_np.max(slopes_arr)), 2),
        "obstacle_pct": round(obstacles / total * 100.0, 2),
        "shadow_pct":   round(shadows  / total * 100.0, 2),
    }
    conf_data = generate_grid_confidence(GLOBAL_GRID.width, GLOBAL_GRID.height)
    route_confidence = RouteConfidenceResponse(
        region_id=region_id,
        grid_confidence=conf_data,
    )

    return MissionSnapshotResponse(
        region_id=region_id,
        snapshot_timestamp=timestamp,
        ice_layer=ice_layer,
        hazard_summary=hazard_summary,
        route=route,
        lmrs=lmrs,
        route_confidence=route_confidence,
    )
@app.get("/api/export/csv")
async def export_csv(region_id: str, lat: float, lon: float):

    data: MissionReportData = assemble_report_data(lat, lon, region_id, GLOBAL_GRID)
    csv_str = to_csv(data)
    filename = f"astranav_mission_{region_id}_{lat:.4f}_{lon:.4f}.csv"
    return StreamingResponse(
        iter([csv_str]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
@app.get("/api/export/pdf")
async def export_pdf(region_id: str, lat: float, lon: float): 
    data: MissionReportData = assemble_report_data(lat, lon, region_id, GLOBAL_GRID)
    pdf_bytes = to_pdf(data)
    filename = f"astranav_mission_{region_id}_{lat:.4f}_{lon:.4f}.pdf"
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    ) 
@app.get("/api/mission-briefing", response_model=MissionBriefingResponse)
async def get_mission_briefing(lat: float, lon: float, region_id: str):
    return generate_mission_briefing(
        lat=lat,
        lon=lon,
        region_id=region_id,
        grid=GLOBAL_GRID,
    )
