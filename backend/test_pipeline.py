"""
backend/test_pipeline.py
------------------------
Integration tests for the AstraNav-LRIS Member 2 geospatial pipeline.

Run with:
    pytest backend/test_pipeline.py -v

Tests covered:
  1. test_cpr_dop_thresholds        — CPR > 1.0 AND DOP < 0.13 logic
  2. test_ice_volume_nonzero        — pipeline returns positive volume on synthetic data
  3. test_slope_nogo_zone           — cells with slope > 15° have infinite traversal cost
  4. test_battery_model_shadow_penalty — shadow segment uses more energy than sunlit
  5. test_confidence_decreases_at_borders — border ice cells have lower confidence
  6. test_synthetic_fallback_runs   — ice_detector fallback works without GeoTIFF
  7. test_hazard_mapper_fallback    — hazard_mapper fallback works without GeoTIFF
  8. test_data_loader_reset         — data_loader singleton can be reset and reinitialised

Science basis (ISRO DFSAR specification — constants are IMMUTABLE):
  CPR_THRESHOLD = 1.0   (Circular Polarization Ratio > 1.0 = ice candidate)
  DOP_THRESHOLD = 0.13  (Degree of Polarization < 0.13 = ice candidate)
  SLOPE_NOGO_DEG = 15.0 (No-Go slope threshold)
  PSR temperature: ~25 K (−248°C), causes heavy battery drain via heaters
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Path setup — make root and backend importable
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)

for p in [_ROOT, _THIS_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Test 1: CPR / DOP threshold logic
# ---------------------------------------------------------------------------

def test_cpr_dop_thresholds():
    """
    F4.1 — CPR > 1.0 AND DOP < 0.13 must both be True for ice detection.
    Verifies the ISRO-specified dual-criterion threshold logic.
    """
    from backend.ice_detector import (
        compute_cpr, compute_dop, detect_ice_mask,
        CPR_THRESHOLD, DOP_THRESHOLD,
    )

    rows, cols = 10, 10
    # Ice zone: HV >> HH → CPR > 1.0 and DOP < 0.13
    hh = np.full((rows, cols), 0.30, dtype=np.float32)
    hv = np.full((rows, cols), 0.05, dtype=np.float32)

    # Embed one ice pixel at (5, 5)
    hv[5, 5] = 0.60    # HV > HH → CPR > 1.0
    hh[5, 5] = 0.35    # mild HH boost → DOP ≈ (0.35 - 0.60)/(0.95) ≈ 0.26 → wait, need to check

    # Better: engineer exact values so CPR > 1.0 and DOP < 0.13
    # CPR = hv/hh > 1.0 → hv > hh
    # DOP = |hh-hv|/(hh+hv) < 0.13 → |hh-hv| < 0.13*(hh+hv)
    # If hh=0.50, hv=0.55: CPR=1.1, DOP=|0.50-0.55|/1.05 = 0.0476 < 0.13 ✓
    hh[5, 5] = 0.50
    hv[5, 5] = 0.55

    cpr = compute_cpr(hh, hv)
    dop = compute_dop(hh, hv)
    ice_mask = detect_ice_mask(cpr, dop)

    # Ice at (5,5)
    assert cpr[5, 5] > CPR_THRESHOLD,   f"CPR={cpr[5,5]:.3f} should be > {CPR_THRESHOLD}"
    assert dop[5, 5] < DOP_THRESHOLD,   f"DOP={dop[5,5]:.3f} should be < {DOP_THRESHOLD}"
    assert ice_mask[5, 5],              "Cell (5,5) should be detected as ice"

    # Non-ice background: CPR < 1.0
    cpr_bg = float(cpr[0, 0])
    assert cpr_bg < CPR_THRESHOLD, f"Background CPR={cpr_bg:.3f} should be < {CPR_THRESHOLD}"
    assert not ice_mask[0, 0], "Background cell should NOT be ice"


# ---------------------------------------------------------------------------
# Test 2: Ice volume is positive on synthetic data
# ---------------------------------------------------------------------------

def test_ice_volume_nonzero():
    """
    F4.1 — run_ice_pipeline() must return positive total_volume_m3 on the
    built-in synthetic fallback (no GeoTIFF required).
    """
    from backend.ice_detector import run_ice_pipeline

    # run_ice_pipeline falls back to synthetic data when file not found
    result = run_ice_pipeline("nonexistent_path/test.tif")

    assert result["ice_cell_count"] > 0, \
        "Synthetic fallback should detect at least 1 ice cell"
    assert result["total_volume_m3"] > 0.0, \
        "Synthetic fallback should have positive ice volume"
    assert result["cpr"].max() > 1.0, \
        f"Max CPR={result['cpr'].max():.3f} should exceed threshold 1.0"
    assert result["dop"].min() < 0.13, \
        f"Min DOP={result['dop'].min():.4f} should be below threshold 0.13"


# ---------------------------------------------------------------------------
# Test 3: Slope > 15° → infinite traversal cost
# ---------------------------------------------------------------------------

def test_slope_nogo_zone():
    """
    F4.2/F4.3 — Cells with slope > 15° must have traversal_cost = inf in the CostGrid.
    Science: Pragyan-class rovers cannot traverse slopes steeper than 15°.
    """
    from backend.hazard_mapper import compute_slope, SLOPE_NOGO_DEG
    from cost_grid import CostGrid
    from schemas import HazardMask

    rows, cols = 20, 20

    # Create a DEM with a steep section
    dem = np.zeros((rows, cols), dtype=np.float32)
    # Steep wall: 100 m height over 30 m horizontal → arctan(100/30) ≈ 73°
    dem[10:, :] = 100.0

    slope = compute_slope(dem, resolution_m=30.0)

    # Verify that slope is > 15° in the steep region
    steep_mask = slope > SLOPE_NOGO_DEG
    assert steep_mask.any(), "DEM should contain at least one steep cell (> 15°)"

    # Build a tiny CostGrid and apply masks
    grid = CostGrid(width=cols, height=rows, resolution_m=30.0)
    for y in range(rows):
        for x in range(cols):
            s = float(slope[y, x])
            is_obs = s > SLOPE_NOGO_DEG
            mask = HazardMask(lat=0.0, lon=0.0, slope_deg=s, is_obstacle=is_obs)
            grid.apply_hazard_mask(mask, x, y)

    # Check that steep cells are impassable
    steep_coords = list(zip(*np.where(steep_mask)))
    for (gy, gx) in steep_coords[:5]:   # spot-check first 5
        cost = grid.get_traversal_cost(int(gx), int(gy))
        assert cost == float("inf"), \
            f"Cell ({gx},{gy}) slope={slope[gy,gx]:.1f}° should have inf cost, got {cost}"


# ---------------------------------------------------------------------------
# Test 4: Shadow → higher battery energy than sunlit
# ---------------------------------------------------------------------------

def test_battery_model_shadow_penalty():
    """
    F8 — A route segment in PSR shadow (25 K) must consume more energy
    than an identical segment in sunlit conditions (300 K).

    Science: PSR heater power = 25W + ramp → significant thermal drain.
    """
    from legacy.battery_model import predict_energy_wh, get_model

    # Ensure model is loaded (retrain if needed)
    get_model()

    # Identical kinematics, different thermal environments
    slope_deg  = 5.0
    speed_mps  = 0.05
    distance_m = 30.0

    energy_psr    = predict_energy_wh(25.0,  True,  slope_deg, speed_mps, distance_m)  # PSR 25K
    energy_sunlit = predict_energy_wh(300.0, False, slope_deg, speed_mps, distance_m)  # Sunlit

    assert energy_psr > 0.0,    f"PSR energy must be positive, got {energy_psr:.4f}"
    assert energy_sunlit > 0.0, f"Sunlit energy must be positive, got {energy_sunlit:.4f}"
    assert energy_psr > energy_sunlit, (
        f"PSR energy ({energy_psr:.3f} Wh) must exceed sunlit ({energy_sunlit:.3f} Wh) "
        f"due to heater overhead. Science: PSR ≈ 25 K, heater ≈ 25–50W."
    )


# ---------------------------------------------------------------------------
# Test 5: Ice border cells have lower confidence
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="Legacy function generate_grid_confidence_from_sar no longer exists for MVP")
def test_confidence_decreases_at_borders():
    """
    F9 — Border cells of the ice mask should have lower confidence than
    interior cells, due to the border-erosion penalty in the SAR overlay.

    Science: ice polygon border cells have elevated false-positive rate
    (edge uncertainty from speckle noise in DFSAR).
    """
    pass


# ---------------------------------------------------------------------------
# Test 6: Synthetic fallback runs without GeoTIFF
# ---------------------------------------------------------------------------

def test_synthetic_fallback_runs():
    """
    The ice detector must work without any real GeoTIFF files via its internal
    synthetic fallback. Verifies all required dict keys are present.
    """
    from backend.ice_detector import run_ice_pipeline

    result = run_ice_pipeline("/nonexistent/path.tif")

    required_keys = {"ice_mask", "cpr", "dop", "epsilon", "volume_m3",
                     "total_volume_m3", "ice_cell_count"}
    for key in required_keys:
        assert key in result, f"Missing key '{key}' in pipeline result"

    assert isinstance(result["ice_mask"], np.ndarray)
    assert result["ice_mask"].dtype == bool
    assert isinstance(result["total_volume_m3"], float)
    assert isinstance(result["ice_cell_count"], int)


# ---------------------------------------------------------------------------
# Test 7: Hazard mapper fallback runs without GeoTIFF
# ---------------------------------------------------------------------------

def test_hazard_mapper_fallback():
    """
    build_real_cost_grid must work without real DEM or optical files via
    its synthetic fallback. The resulting CostGrid must have some infinite
    (no-go) cells from steep slopes.
    """
    from backend.hazard_mapper import build_real_cost_grid

    grid = build_real_cost_grid("/nonexistent/dem.tif", None)

    assert grid.width > 0,  "CostGrid must have positive width"
    assert grid.height > 0, "CostGrid must have positive height"

    # At least some cells should be impassable (steep crater walls)
    inf_cells = 0
    for y in range(0, grid.height, 4):
        for x in range(0, grid.width, 4):
            if grid.get_traversal_cost(x, y) == float("inf"):
                inf_cells += 1
    assert inf_cells > 0, "Crater DEM fallback should produce some no-go cells"

    # Some cells should be in shadow (PSR forcing)
    shadow_cells = sum(
        1 for y in range(0, grid.height, 4)
        for x in range(0, grid.width, 4)
        if grid.is_in_shadow(x, y)
    )
    assert shadow_cells > 0, "Shadow map should have at least some shadowed cells"


# ---------------------------------------------------------------------------
# Test 8: Data loader singleton reset
# ---------------------------------------------------------------------------

def test_data_loader_reset():
    """
    data_loader.reset_cache() must clear singletons so they're re-built
    on next call. Verifies the singleton pattern works correctly.
    """
    from backend import data_loader

    # Prime the cache
    data_loader.reset_cache()
    result1 = data_loader.get_ice_pipeline()

    # Reset and re-fetch — should be a fresh (but identical) result
    data_loader.reset_cache()
    result2 = data_loader.get_ice_pipeline()

    assert result1["ice_cell_count"] == result2["ice_cell_count"], \
        "Re-initialised pipeline should produce the same ice_cell_count (deterministic seed)"
    assert abs(result1["total_volume_m3"] - result2["total_volume_m3"]) < 0.1, \
        "Re-initialised pipeline should produce the same total volume"


# ---------------------------------------------------------------------------
# Entry point for direct execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import subprocess
    subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd=_ROOT
    )
