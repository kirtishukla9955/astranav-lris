import math
from schemas import (
    IceLayerData, 
    ResourceAccessibilityIndex, 
    CommVisibility, 
    ThermalRisk, 
    LMRSResponse
)
from cost_grid import CostGrid
from pathfinder import build_route
import confidence # we will create this next

def calculate_rai(ice_data: IceLayerData, distance_m: float) -> ResourceAccessibilityIndex:
    """
    Calculates the Resource Accessibility Index (RAI) by combining ice data with extraction difficulty.
    """
    # MOCK MATH for Resource Accessibility Index
    # Base score on volume (e.g. max 1000 m3 -> 100)
    volume_score = min(100, (ice_data.ice_volume_m3 / 1000.0) * 100)
    
    # Depth penalty (e.g. 1m depth -> 0.9, 10m depth -> 0.1)
    depth_penalty = max(0.1, 1.0 - (ice_data.ice_depth_m / 10.0))
    
    # Distance penalty (e.g. 1000m -> 0.5)
    distance_penalty = max(0.1, 1.0 - (distance_m / 2000.0))
    
    # Extraction difficulty is inverse of accessibility
    extraction_difficulty = 1.0 - (depth_penalty * distance_penalty)
    
    return ResourceAccessibilityIndex(
        ice_volume_m3=ice_data.ice_volume_m3,
        ice_depth_m=ice_data.ice_depth_m,
        extraction_difficulty_score=extraction_difficulty * 100
    )

def check_comm_visibility(lat: float, lon: float, grid: CostGrid) -> CommVisibility:
    """
    Checks line-of-sight against a fixed Earth direction vector.
    """
    # MOCK MATH for Earth Line of Sight
    grid_x = int(lon * 10000)
    grid_y = int(lat * 10000)
    
    # Prevent out of bounds
    grid_x = max(0, min(grid.width - 1, grid_x))
    grid_y = max(0, min(grid.height - 1, grid_y))
    
    is_shadow = grid.is_in_shadow(grid_x, grid_y)
    
    # If in shadow (crater), earth comms might be blocked by crater walls
    has_los = not is_shadow
    signal_pct = 95.0 if has_los else 20.0
    
    return CommVisibility(
        earth_line_of_sight=has_los,
        signal_strength_pct=signal_pct
    )

def calculate_thermal_risk(energy_wh: float, shadow_exposure_min: float) -> ThermalRisk:
    """
    Calculates thermal risk based on energy consumption and shadow exposure.
    """
    # Max risk if > 1000 Wh or > 30 min shadow
    energy_risk = min(100.0, (energy_wh / 1000.0) * 100)
    shadow_risk = min(100.0, (shadow_exposure_min / 30.0) * 100)
    
    total_risk = (energy_risk * 0.4) + (shadow_risk * 0.6)
    
    return ThermalRisk(
        total_energy_wh=energy_wh,
        shadow_exposure_time_min=shadow_exposure_min,
        thermal_risk_score=total_risk
    )

def compute_lmrs(
    target_lat: float, 
    target_lon: float, 
    start_lat: float, 
    start_lon: float, 
    region_id: str, 
    grid: CostGrid
) -> LMRSResponse:
    """
    Computes the full Lunar Mining Readiness Score (LMRS) for a given target site.
    """
    # 1. Route distance & energy
    start_x, start_y = int(start_lon * 10000), int(start_lat * 10000)
    goal_x, goal_y = int(target_lon * 10000), int(target_lat * 10000)
    
    # Prevent out of bounds
    start_x = max(0, min(grid.width - 1, start_x))
    start_y = max(0, min(grid.height - 1, start_y))
    goal_x = max(0, min(grid.width - 1, goal_x))
    goal_y = max(0, min(grid.height - 1, goal_y))
    
    route = build_route(grid, (start_x, start_y), (goal_x, goal_y), region_id)
    
    dist_m = route.total_distance_m if route else 1000.0
    energy_wh = route.total_energy_wh if route else 500.0
    
    # Calculate total shadow time from waypoints
    shadow_time_min = 0.0
    if route:
        # Assuming speed is 0.05 m/s as in pathfinder
        shadow_wpts = sum(1 for w in route.waypoints if w.is_in_shadow)
        shadow_time_min = (shadow_wpts * grid.resolution_m / 0.05) / 60.0
        
    # 2. Mock Ice Data (Feature 1)
    # MOCK DATA - Replace with real Member 1 API call
    mock_ice = IceLayerData(
        lat=target_lat,
        lon=target_lon,
        ice_volume_m3=800.0,
        ice_depth_m=1.5,
        confidence=0.85
    )
    
    # 3. Sub-scores
    rai = calculate_rai(mock_ice, dist_m)
    comm = check_comm_visibility(target_lat, target_lon, grid)
    thermal = calculate_thermal_risk(energy_wh, shadow_time_min)
    
    # 4. Total LMRS (Weighted combination)
    score = 50.0 
    score += (rai.ice_volume_m3 / 1000.0) * 30.0 # up to +30
    score += (comm.signal_strength_pct / 100.0) * 20.0 # up to +20
    score -= (thermal.thermal_risk_score / 100.0) * 30.0 # up to -30
    score -= (rai.extraction_difficulty_score / 100.0) * 20.0 # up to -20
    
    score = max(0.0, min(100.0, score))
    
    # 5. Confidence Score
    conf_score = confidence.route_segment_confidence(dist_m, energy_wh, shadow_time_min)
    
    return LMRSResponse(
        lat=target_lat,
        lon=target_lon,
        region_id=region_id,
        lmrs_score=score,
        rai=rai,
        comm_visibility=comm,
        thermal_risk=thermal,
        confidence=conf_score
    )
