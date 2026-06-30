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

def test_illumination_frame_count():

    from SID_4_BACKS.illumination import simulate_illumination

    grid = CostGrid(20, 20, resolution_m=1.0)
    num_frames = 12  # small for speed
    result = simulate_illumination(grid, region_id="test-region", num_frames=num_frames)

    assert result.region_id == "test-region"
    assert result.num_frames == num_frames
    assert len(result.frames) == num_frames

    # Sun angle must sweep 0 → <360, increasing monotonically
    angles = [f.sun_angle_deg for f in result.frames]
    assert angles[0] == 0.0
    assert all(angles[i] < angles[i + 1] for i in range(len(angles) - 1)), \
        "Sun angle must increase monotonically across frames"
    assert angles[-1] < 360.0

    # Each frame must have at least one cell
    for frame in result.frames:
        assert len(frame.cells) > 0, f"Frame {frame.timestep} has no cells"

def test_illumination_pitstop_eligibility():
    from SID_4_BACKS.illumination import simulate_illumination
    import numpy as np

    grid = CostGrid(10, 10, resolution_m=1.0)
    # Mark a specific region as permanently shadowed
    shadow = np.zeros((10, 10), dtype=bool)
    shadow[2:5, 2:5] = True
    grid.apply_shadow_map(shadow)

    result = simulate_illumination(grid, region_id="pitstop-test", num_frames=4)

    for frame in result.frames:
        for cell in frame.cells:
            gx = int(round(cell.lon / 0.0001))
            gy = int(round(cell.lat / 0.0001))
            if grid.is_in_shadow(gx, gy):
                assert not cell.is_pitstop_eligible, \
                    f"Shadowed cell ({gx},{gy}) must not be pitstop-eligible"

def test_mission_snapshot_structure():
    from SID_4_BACKS.report import assemble_report_data

    grid = CostGrid(20, 20, resolution_m=1.0)
    data = assemble_report_data(lat=0.001, lon=0.001, region_id="snap-test", grid=grid)

    # Top-level fields
    assert data.region_id == "snap-test"
    assert data.lat == 0.001
    assert data.lon == 0.001
    assert data.generated_at != ""

    # LMRS sub-scores present
    assert 0.0 <= data.lmrs.lmrs_score <= 100.0
    assert data.lmrs.rai is not None
    assert data.lmrs.comm_visibility is not None
    assert data.lmrs.thermal_risk is not None

    # Ice layer fields (MOCK DATA)
    assert data.ice_layer.ice_volume_m3 > 0
    assert data.ice_layer.ice_depth_m > 0
    assert 0.0 <= data.ice_layer.confidence <= 1.0

    # Hazard summary bounds
    assert data.hazard_summary.slope_mean_deg >= 0.0
    assert data.hazard_summary.slope_max_deg >= data.hazard_summary.slope_mean_deg
    assert 0.0 <= data.hazard_summary.obstacle_cell_pct <= 100.0
    assert 0.0 <= data.hazard_summary.shadow_cell_pct <= 100.0


def test_csv_export_contains_waypoints():
    from SID_4_BACKS.report import assemble_report_data, to_csv

    grid = CostGrid(15, 15, resolution_m=1.0)
    data = assemble_report_data(lat=0.0005, lon=0.0005, region_id="csv-test", grid=grid)
    csv_str = to_csv(data)

    assert csv_str.startswith("# AstraNav-LRIS"), "CSV must start with mission header comment"
    assert "LMRS SUMMARY" in csv_str, "CSV must contain LMRS section"
    assert "WAYPOINTS" in csv_str, "CSV must contain waypoint section"
    assert "HAZARD SUMMARY" in csv_str, "CSV must contain hazard section"

    # Verify waypoint rows: each waypoint should appear as a row after the header row
    if data.waypoints:
        lines = csv_str.splitlines()
        wpt_idx = next(i for i, l in enumerate(lines) if "=== WAYPOINTS ===" in l)
        # Header row is wpt_idx+1, first data row is wpt_idx+2
        assert len(lines) > wpt_idx + 2, "CSV must have at least one waypoint data row"

def test_pdf_export_returns_bytes():
    from SID_4_BACKS.report import assemble_report_data, to_pdf

    grid = CostGrid(15, 15, resolution_m=1.0)
    data = assemble_report_data(lat=0.0005, lon=0.0005, region_id="pdf-test", grid=grid)
    pdf_bytes = to_pdf(data)

    assert isinstance(pdf_bytes, bytes), "to_pdf() must return bytes"
    assert len(pdf_bytes) > 0, "PDF must not be empty"
    assert pdf_bytes[:4] == b"%PDF", "PDF must start with PDF magic number %PDF"

def test_briefing_fallback_when_no_api_key(monkeypatch):
    # Ensure the env var is absent for this test
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from SID_4_BACKS.briefing import generate_mission_briefing

    grid = CostGrid(15, 15, resolution_m=1.0)
    result = generate_mission_briefing(
        lat=0.0005,
        lon=0.0005,
        region_id="briefing-test",
        grid=grid,
    )

    assert result.generated_by == "fallback_template", \
        "Must use fallback template when ANTHROPIC_API_KEY is absent"
    assert isinstance(result.briefing_text, str)
    assert len(result.briefing_text) > 50, "Briefing text must be a substantive paragraph"
    assert "briefing-test" in result.briefing_text, \
        "Briefing text must reference the region_id"

def test_briefing_response_schema():
    monkeypatch_env = {"ANTHROPIC_API_KEY": ""}  # empty key → fallback
    import os
    original = os.environ.get("ANTHROPIC_API_KEY")
    os.environ.pop("ANTHROPIC_API_KEY", None)

    try:
        from SID_4_BACKS.briefing import generate_mission_briefing
        from schemas import MissionBriefingResponse

        grid = CostGrid(10, 10, resolution_m=1.0)
        result = generate_mission_briefing(
            lat=0.0003,
            lon=0.0003,
            region_id="schema-test",
            grid=grid,
        )

        assert isinstance(result, MissionBriefingResponse)
        assert result.lat == 0.0003
        assert result.lon == 0.0003
        assert result.region_id == "schema-test"
        assert result.generated_by in ("llm", "fallback_template")
        assert len(result.briefing_text) > 0
    finally:
        # Restore original env state
        if original is not None:
            os.environ["ANTHROPIC_API_KEY"] = original
