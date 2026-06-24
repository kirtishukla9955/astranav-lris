import pytest
from cost_grid import CostGrid
from pathfinder import build_route
from schemas import HazardMask
from lmrs import calculate_rai, calculate_thermal_risk
from schemas import IceLayerData
from confidence import route_segment_confidence

def test_pathfinder_shadow_avoidance():
    # Create a simple 10x10 grid
    grid = CostGrid(10, 10, resolution_m=1.0)
    
    # Put a massive shadow block in the middle
    for y in range(3, 7):
        for x in range(3, 7):
            grid.shadow_map[y, x] = True
            
    # Route from 1,1 to 8,8
    route = build_route(grid, (1, 1), (8, 8), "test")
    assert route is not None
    
    # With a 25,000x penalty, the A* should completely route around the shadow.
    for wpt in route.waypoints:
        # Convert back from mock lat/lon coordinates
        x, y = int(round(wpt.lon / 0.0001)), int(round(wpt.lat / 0.0001))
        assert not grid.is_in_shadow(x, y), f"Pathfinder failed to avoid shadow at {x},{y}"

def test_lmrs_scoring_math():
    ice_data = IceLayerData(lat=0.0, lon=0.0, ice_volume_m3=1000.0, ice_depth_m=1.0, confidence=0.9)
    # Near perfect conditions
    rai = calculate_rai(ice_data, 100.0)
    assert rai.ice_volume_m3 == 1000.0
    
    # Depth penalty: max(0.1, 1.0 - 0.1) = 0.9
    # Distance penalty: max(0.1, 1.0 - 100/2000) = 0.95
    # Extraction difficulty: 1.0 - (0.9 * 0.95) = 0.145
    assert abs(rai.extraction_difficulty_score - 14.5) < 0.1

def test_thermal_risk_math():
    risk = calculate_thermal_risk(1000.0, 30.0)
    # Energy > 1000 = 100 risk. Shadow >= 30 = 100 risk. Total = 100.
    assert risk.thermal_risk_score == 100.0
    
    risk2 = calculate_thermal_risk(500.0, 15.0)
    # Energy 500 = 50 risk. Shadow 15 = 50 risk. Total = 50.
    assert risk2.thermal_risk_score == 50.0

def test_confidence_bounds():
    # Perfect conditions should have 1.0 confidence
    conf_max = route_segment_confidence(distance_m=0.0, energy_wh=0.0, shadow_time_min=0.0)
    assert conf_max == 1.0
    
    # Horrible conditions should clamp at 0.1, never go negative
    conf_min = route_segment_confidence(distance_m=10000.0, energy_wh=50000.0, shadow_time_min=100.0)
    assert conf_min == 0.1
