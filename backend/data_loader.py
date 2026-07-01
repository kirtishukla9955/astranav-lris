"""
backend/data_loader.py
----------------------
Singleton entry-point for all geospatial data ingestion.

Provides lazy-initialised, cached singletons for:
  - Ice detection pipeline results (from DFSAR SAR GeoTIFF)
  - Real CostGrid (from TMC-2 DEM + OHRC optical)

Usage in main.py (root level):
    from backend.data_loader import get_ice_pipeline, get_real_cost_grid
    GLOBAL_GRID = get_real_cost_grid()

If GeoTIFFs do not exist yet, run first:
    python scripts/generate_synthetic_data.py

Science basis:
  Ice candidates: CPR > 1.0 AND DOP < 0.13 (ISRO DFSAR specification)
  No-Go: slope > 15° or boulder diameter > 0.5 m
  PSR temperature: ~25 K (−248°C), causes heavy battery drain via heaters
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path resolution — works whether imported as `backend.data_loader` (package)
# or run directly from backend/ directory
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ---------------------------------------------------------------------------
# Data directory layout
# ---------------------------------------------------------------------------
DATA_DIR     = os.path.join(_THIS_DIR, "data")
SAR_PATH     = os.path.join(DATA_DIR, "sar",     "synthetic_dfsar.tif")
DEM_PATH     = os.path.join(DATA_DIR, "dem",     "synthetic_dem.tif")
OPTICAL_PATH = os.path.join(DATA_DIR, "optical", "synthetic_ohrc.tif")
THERMAL_PATH = os.path.join(DATA_DIR, "thermal", "synthetic_temp.tif")

# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------
_ice_pipeline_result = None
_real_grid = None


def get_ice_pipeline() -> dict:
    """
    Return the cached ice detection pipeline result.

    On first call, runs the full DFSAR → CPR/DOP → ice mask → volume pipeline.
    Subsequent calls return the cached result instantly.

    Returns dict with keys:
      ice_mask, cpr, dop, epsilon, volume_m3,
      total_volume_m3, ice_cell_count, transform

    Science:
      CPR > 1.0 AND DOP < 0.13 → ice candidate (ISRO DFSAR specification)
    """
    global _ice_pipeline_result
    if _ice_pipeline_result is None:
        # Import here to avoid circular imports at module level
        from backend.ice_detector import run_ice_pipeline  # type: ignore[import]

        logger.info("DataLoader: initialising ice pipeline from %s", SAR_PATH)
        _ice_pipeline_result = run_ice_pipeline(SAR_PATH)
        logger.info(
            "DataLoader: ice pipeline ready — %d ice cells, total=%.1f m³",
            _ice_pipeline_result["ice_cell_count"],
            _ice_pipeline_result["total_volume_m3"],
        )
    return _ice_pipeline_result


def get_real_cost_grid():
    """
    Return the cached CostGrid built from real (or synthetic) DEM + optical data.

    On first call, loads the DEM, computes slope, detects boulders, computes
    shadow map, and assembles the CostGrid used by the A* pathfinder.

    Replaces generate_mock_cost_grid() in root main.py.

    Science:
      No-Go: slope > 15° or boulder > 0.5 m → traversal_cost = ∞
      Shadow: PSR ~25 K → heavy battery drain penalty in pathfinder
    """
    global _real_grid
    if _real_grid is None:
        from backend.hazard_mapper import build_real_cost_grid  # type: ignore[import]

        logger.info("DataLoader: building real CostGrid from DEM=%s", DEM_PATH)
        _real_grid = build_real_cost_grid(DEM_PATH, OPTICAL_PATH)
        logger.info("DataLoader: CostGrid ready (%d×%d)", _real_grid.width, _real_grid.height)
    return _real_grid


def reset_cache() -> None:
    """
    Clear all cached singletons (useful for testing).
    """
    global _ice_pipeline_result, _real_grid
    _ice_pipeline_result = None
    _real_grid = None
    logger.debug("DataLoader: cache cleared")
