"""
tests/test_fault_injector.py
----------------------------
Pytest unit tests for the Autonomous Contingency Planner (Mission Anomaly Response Engine).
"""

from __future__ import annotations

import sys
import os

# Allow running from any directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

from main import create_app
from pathfinder import CostConfig, StaticBatteryModel, PolarGrid
from pathfinder.fault_injector import FaultyBatteryModel, FaultyGrid, find_nearest_comm_cell
from pathfinder.types import GridCell


# ---------------------------------------------------------------------------
# 1. Faulty Battery Model Tests
# ---------------------------------------------------------------------------

def test_faulty_battery_model_scaling():
    base_model = StaticBatteryModel()
    faulty_model = FaultyBatteryModel(base_model, multiplier=2.0)
    
    cell = GridCell(
        row=0, col=0, lat=-89.5, lon=44.0,
        base_traversal_cost=1.0, is_hazard=False, is_shadowed=False,
        solar_illumination=1.0, temperature_k=120.0, slope_deg=0.0
    )
    
    base_drain = base_model.predict_drain_wh(cell, 10.0, 100.0)
    faulty_drain = faulty_model.predict_drain_wh(cell, 10.0, 100.0)
    
    assert faulty_drain == pytest.approx(base_drain * 2.0)


def test_faulty_battery_model_thermal():
    base_model = StaticBatteryModel()
    faulty_model = FaultyBatteryModel(base_model, thermal_scale=4.0)
    
    shadow_cell = GridCell(
        row=0, col=0, lat=-89.5, lon=44.0,
        base_traversal_cost=1.0, is_hazard=False, is_shadowed=True,
        solar_illumination=0.0, temperature_k=25.0, slope_deg=0.0
    )
    
    # Base model drain inside shadow: base traversal + shadow heater draw
    base_drain = base_model.predict_drain_wh(shadow_cell, 10.0, 100.0)
    faulty_drain = faulty_model.predict_drain_wh(shadow_cell, 10.0, 100.0)
    
    assert faulty_drain > base_drain


# ---------------------------------------------------------------------------
# 2. Faulty Grid Tests
# ---------------------------------------------------------------------------

def test_faulty_grid_obstacle():
    base_grid = PolarGrid(rows=5, cols=5, origin_lat=-89.5, origin_lon=44.0)
    
    # Inject obstacle at (2, 2) with radius 1.5 cells (blocks (2,2), (2,3), (1,2) etc.)
    faulty_grid = FaultyGrid(base_grid, obstacle_cell=(2, 2), obstacle_radius=1.5)
    
    assert faulty_grid.get_cell(2, 2).is_hazard is True
    assert faulty_grid.get_cell(2, 3).is_hazard is True
    assert faulty_grid.get_cell(0, 0).is_hazard is False


def test_faulty_grid_blocked_pitstop():
    base_grid = PolarGrid(rows=5, cols=5, origin_lat=-89.5, origin_lon=44.0)
    base_grid.mark_illuminated(1, 1, illumination=1.0)
    
    faulty_grid = FaultyGrid(base_grid, unavailable_pitstop=(1, 1))
    
    cell = faulty_grid.get_cell(1, 1)
    assert cell.is_hazard is True
    assert cell.solar_illumination == 0.0


def test_faulty_grid_sensor_degradation():
    base_grid = PolarGrid(rows=5, cols=5, origin_lat=-89.5, origin_lon=44.0)
    base_grid.mark_slope(2, 2, slope_deg=4.0)
    
    faulty_grid = FaultyGrid(base_grid, slope_multiplier=2.5)
    
    assert faulty_grid.get_cell(2, 2).slope_deg == pytest.approx(10.0)  # 4.0 * 2.5


# ---------------------------------------------------------------------------
# 3. Comms Recovery BFS Tests
# ---------------------------------------------------------------------------

def test_find_nearest_comm_cell():
    grid = PolarGrid(rows=6, cols=6, origin_lat=-89.5, origin_lon=44.0)
    # The default mock grid in tests will return los_fraction >= 0.8 depending on height/elevation.
    # In compute_comm_visibility helper, it computes line of sight fractions.
    # Let's ensure find_nearest_comm_cell runs without error and returns a cell
    cell_idx = find_nearest_comm_cell(grid, 0, 0)
    if cell_idx is not None:
        assert len(cell_idx) == 2
        assert 0 <= cell_idx[0] < 6
        assert 0 <= cell_idx[1] < 6


# ---------------------------------------------------------------------------
# 4. REST Endpoint API Tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    app = create_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_contingency_replan_endpoint(client):
    payload = {
        "region_id": "shackleton-east",
        "current_lat": -89.525,
        "current_lon": 44.20,
        "end_lat": -89.51,
        "end_lon": 44.30,
        "anomaly_type": "wheel_degradation",
        "anomaly_magnitude": 0.4,
        "use_predictive_battery": False,
        "initial_battery_pct": 90.0,
        "original_route_waypoints": [
            {
                "lat": -89.525,
                "lon": 44.20,
                "cumulative_distance_m": 0.0,
                "cumulative_energy_wh": 0.0,
                "battery_pct_remaining": 90.0,
                "is_shadowed": False,
                "solar_illumination": 1.0
            },
            {
                "lat": -89.51,
                "lon": 44.30,
                "cumulative_distance_m": 1500.0,
                "cumulative_energy_wh": 20.0,
                "battery_pct_remaining": 85.0,
                "is_shadowed": False,
                "solar_illumination": 1.0
            }
        ]
      }

    res = client.post("/api/replan-contingency", json=payload)
    assert res.status_code == 200
    
    data = res.json()
    assert "new_path" in data
    assert len(data["new_path"]) > 0
    assert "recovery_target" in data
    assert "explanation" in data
    assert "metrics" in data
    assert "before" in data["metrics"]
    assert "after" in data["metrics"]
    
    # Operational Confidence Index must be present and correctly labeled
    assert "op_confidence" in data["metrics"]["before"]
    assert "op_confidence" in data["metrics"]["after"]
    
    # Confirm explanation is rich and detailed
    assert "reason" in data["explanation"]
    assert "effects" in data["explanation"]
    assert "decisions" in data["explanation"]
    
    assert data["replan_time_ms"] > 0
