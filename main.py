from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import json
import asyncio
from typing import Dict

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
