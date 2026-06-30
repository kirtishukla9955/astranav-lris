import numpy as np
from typing import List, Dict
from schemas import RouteResponse, Waypoint

def route_segment_confidence(distance_m: float, energy_wh: float, shadow_time_min: float) -> float:
    """
    Calculates confidence in the energy/thermal estimate for a route segment.
    Since the battery model and shadow map are estimates with uncertainty,
    confidence drops as distance, energy, and shadow time increase.
    Returns a value between 0.0 and 1.0.
    """
    # Base confidence
    confidence = 1.0
    
    # Variance increases with distance
    # Say, every 100m drops confidence by 1%
    dist_penalty = (distance_m / 100.0) * 0.01
    
    # Variance increases with high energy segments (more extreme slopes/temps)
    energy_penalty = (energy_wh / 500.0) * 0.02
    
    # Shadow map has uncertainty (craters have fuzzy boundaries)
    # The more time spent in shadow, the higher chance the shadow map was wrong somewhere
    shadow_penalty = (shadow_time_min / 10.0) * 0.05
    
    confidence -= (dist_penalty + energy_penalty + shadow_penalty)
    
    # Ensure bounds
    return max(0.1, min(1.0, confidence))

def apply_confidence_to_route(route: RouteResponse):
    """
    Mutates the route waypoints to attach confidence values.
    """
    for i in range(1, len(route.waypoints)):
        wpt = route.waypoints[i]
        prev_wpt = route.waypoints[i-1]
        
        # Segment metrics
        dist_m = wpt.cumulative_distance_m - prev_wpt.cumulative_distance_m
        energy_wh = wpt.cumulative_energy_wh - prev_wpt.cumulative_energy_wh
        shadow_time_min = (dist_m / 0.05) / 60.0 if wpt.is_in_shadow else 0.0
        
        wpt.confidence = route_segment_confidence(dist_m, energy_wh, shadow_time_min)
        
    if route.waypoints:
        route.waypoints[0].confidence = 1.0 # Start point is known

def generate_grid_confidence(width: int, height: int) -> List[Dict[str, float]]:
    """
    Generates a confidence overlay for the grid, representing shadow map certainty.
    Returns grid cell coordinates and their confidence values for Member 3's frontend.
    """
    grid_confidence = []
    # MOCK DATA: For MVP, generate a randomized smooth confidence map
    # A real implementation would use signal noise (CPR/DOP) from Member 1.
    for y in range(0, height, 5): # Stride for smaller payload
        for x in range(0, width, 5):
            # random confidence clustered around 0.8
            conf = min(1.0, max(0.0, np.random.normal(0.8, 0.15)))
            grid_confidence.append({
                "lat": y * 0.0001,
                "lon": x * 0.0001,
                "confidence": conf
            })
    return grid_confidence
