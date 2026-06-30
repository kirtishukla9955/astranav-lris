import asyncio
from typing import List, Dict, AsyncGenerator
from schemas import SwarmRouteRequest, SwarmRouteResponse, RouteResponse
from cost_grid import CostGrid
from pathfinder import build_route
from confidence import apply_confidence_to_route
import uuid

def compute_swarm_routes(
    request: SwarmRouteRequest, 
    grid: CostGrid
) -> SwarmRouteResponse:
    """
    Computes independent routes for multiple rovers towards a target.
    """
    routes = {}
    target_lat = request.target_point.get("lat", 0.0)
    target_lon = request.target_point.get("lon", 0.0)
    
    # Simple conversion mock
    goal_x, goal_y = int(target_lon * 10000), int(target_lat * 10000)
    # bounds check
    goal_x = max(0, min(grid.width - 1, goal_x))
    goal_y = max(0, min(grid.height - 1, goal_y))
    
    for idx, start_pt in enumerate(request.start_points):
        start_lat = start_pt.get("lat", 0.0)
        start_lon = start_pt.get("lon", 0.0)
        start_x, start_y = int(start_lon * 10000), int(start_lat * 10000)
        
        start_x = max(0, min(grid.width - 1, start_x))
        start_y = max(0, min(grid.height - 1, start_y))
        
        # Rover ID
        rover_id = f"rover-{idx+1}-{uuid.uuid4().hex[:6]}"
        
        route = build_route(grid, (start_x, start_y), (goal_x, goal_y), request.region_id)
        if route:
            apply_confidence_to_route(route)
            routes[rover_id] = route
            
    return SwarmRouteResponse(
        region_id=request.region_id,
        routes=routes
    )

async def simulate_telemetry(routes: Dict[str, RouteResponse]) -> AsyncGenerator[Dict, None]:
    """
    Simulates rover telemetry along precomputed routes for WebSocket streaming.
    Yields telemetry points every 500ms.
    """
    # Find the maximum number of waypoints across all routes
    max_steps = max((len(r.waypoints) for r in routes.values() if r.waypoints), default=0)
    
    for step in range(max_steps):
        telemetry_batch = []
        for rover_id, route in routes.items():
            if step < len(route.waypoints):
                wpt = route.waypoints[step]
                # Calculate simple battery pct drop based on energy
                # Assuming a 5000 Wh battery max
                battery_pct = max(0.0, 100.0 - (wpt.cumulative_energy_wh / 5000.0 * 100.0))
                
                telemetry_batch.append({
                    "rover_id": rover_id,
                    "lat": wpt.lat,
                    "lon": wpt.lon,
                    "battery_pct": round(battery_pct, 2),
                    "is_in_shadow": wpt.is_in_shadow
                })
        
        # Yield the current step for all rovers
        for item in telemetry_batch:
            yield item
            
        # Simulate real-time delay (500ms as requested)
        await asyncio.sleep(0.5)
