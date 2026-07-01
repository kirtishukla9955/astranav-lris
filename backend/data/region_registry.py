"""
data/region_registry.py
-----------------------
Maps region_id → grid construction parameters.

This registry is the single source of truth for how a named region
is instantiated as a PolarGrid.  In production, this config would be
loaded from PostGIS or a YAML file; for the hackathon it's a hard-coded
dict behind a simple accessor.

Also exposes ``build_grid_for_region()`` which:
  1. Reads Member 1's mock ice + hazard layers
  2. Constructs a PolarGrid
  3. Marks hazard/shadow/ice cells from the GeoJSON features
     (using a simplified bounding-box rasterisation — good enough for
      the hackathon; replace with rasterio.features.rasterize() in prod)

The reference lander site per region (used as the LMRS thermal-risk origin)
is also stored here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pathfinder import PolarGrid
from data.mock_fixtures import fetch_hazard_layer, fetch_ice_layer


# ---------------------------------------------------------------------------
# Region config schema
# ---------------------------------------------------------------------------

@dataclass
class RegionConfig:
    region_id: str
    display_name: str
    origin_lat: float      # lat of (row=0, col=0) corner
    origin_lon: float      # lon of (row=0, col=0) corner
    rows: int
    cols: int
    cell_size_m: float     # grid resolution; default 5 m
    lander_lat: float      # reference lander site for LMRS thermal cost
    lander_lon: float
    shadow_band_rows: tuple[int, int]   # (start_row, end_row) of dark crater floor
    sunlit_rim_rows: tuple[int, int]    # (start_row, end_row) of sunlit rim band


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

REGION_REGISTRY: dict[str, RegionConfig] = {
    "shackleton-east": RegionConfig(
        region_id="shackleton-east",
        display_name="Shackleton Crater — Eastern Rim",
        origin_lat=-89.55,
        origin_lon=44.00,
        rows=40,
        cols=40,
        cell_size_m=5.0,
        lander_lat=-89.525,
        lander_lon=44.20,
        shadow_band_rows=(8, 28),    # rows 8–28 = permanently shadowed crater floor
        sunlit_rim_rows=(30, 39),    # rows 30–39 = sunlit rim
    ),
    "haworth": RegionConfig(
        region_id="haworth",
        display_name="Haworth Crater",
        origin_lat=-87.85,
        origin_lon=-0.05,
        rows=30,
        cols=30,
        cell_size_m=5.0,
        lander_lat=-87.82,
        lander_lon=0.10,
        shadow_band_rows=(5, 20),
        sunlit_rim_rows=(22, 29),
    ),
    "shackleton": RegionConfig(
        region_id="shackleton",
        display_name="Shackleton Rim - 89.9°S",
        origin_lat=-89.9,
        origin_lon=-0.7,
        rows=28,
        cols=46,
        cell_size_m=5.0,
        lander_lat=-89.9,
        lander_lon=-0.7,
        shadow_band_rows=(0, 0),
        sunlit_rim_rows=(0, 0),
    ),
    "faustini": RegionConfig(
        region_id="faustini",
        display_name="Faustini Crater - 87.3°S",
        origin_lat=-87.3,
        origin_lon=77.0,
        rows=28,
        cols=46,
        cell_size_m=5.0,
        lander_lat=-87.3,
        lander_lon=77.0,
        shadow_band_rows=(0, 0),
        sunlit_rim_rows=(0, 0),
    ),
    "degerlache": RegionConfig(
        region_id="degerlache",
        display_name="de Gerlache Rim - 88.5°S",
        origin_lat=-88.5,
        origin_lon=-87.1,
        rows=28,
        cols=46,
        cell_size_m=5.0,
        lander_lat=-88.5,
        lander_lon=-87.1,
        shadow_band_rows=(0, 0),
        sunlit_rim_rows=(0, 0),
    ),
}


def get_region_config(region_id: str) -> RegionConfig:
    cfg = REGION_REGISTRY.get(region_id)
    if cfg is None:
        available = list(REGION_REGISTRY.keys())
        raise KeyError(f"Unknown region '{region_id}'. Available: {available}")
    return cfg


# ---------------------------------------------------------------------------
# Grid builder
# ---------------------------------------------------------------------------

def build_grid_for_region(region_id: str) -> PolarGrid:
    """
    Construct and populate a PolarGrid for the given region.

    Steps
    -----
    1. Instantiate grid from RegionConfig.
    2. Apply shadow band and sunlit rim from static config.
    3. Rasterise hazard polygons from Member 1's hazard-layer (bbox method).
    4. Rasterise ice polygons and inject volume/confidence.

    Returns
    -------
    Fully populated PolarGrid ready for A* planning.
    """
    cfg = get_region_config(region_id)

    grid = PolarGrid(
        rows=cfg.rows,
        cols=cfg.cols,
        origin_lat=cfg.origin_lat,
        origin_lon=cfg.origin_lon,
        cell_size_m=cfg.cell_size_m,
    )

    # ── Step 2 & 3: Procedural Crater Generation ─────────────────────────────
    # Instead of sparse mock GeoJSONs, we procedurally generate craters,
    # shadows, and hazards using a seeded RNG to create a beautiful,
    # realistic map that matches the frontend's procedural fallback.
    import random
    import math
    
    # Use a unique seed per region so each map looks visually distinct
    REGION_SEEDS_BACKEND = {
        "shackleton-east": 1337,   # was wrongly assigned to shackleton on the frontend
        "shackleton":      9271,   # now consistent with frontend
        "faustini":        7391,
        "degerlache":      4256,
        "haworth":         6628,
    }
    seed = REGION_SEEDS_BACKEND.get(region_id, 5021)
    rng = random.Random(seed)

    
    craters = []
    crater_count = 5 + int(rng.random() * 3)
    for i in range(crater_count):
        cx = 4 + rng.random() * (cfg.cols - 8)
        cy = 4 + rng.random() * (cfg.rows - 8)
        r = 3 + rng.random() * 4.2
        craters.append((cx, cy, r))
        
    for r_i in range(cfg.rows):
        for c_i in range(cfg.cols):
            for (cx, cy, r) in craters:
                d = math.hypot(c_i - cx, r_i - cy)
                if d < r * 0.78:
                    grid.mark_shadow(r_i, c_i, 0.0, 25.0)
                elif d < r:
                    grid.mark_hazard(r_i, c_i)
                    
    # Carve corridors in the hazard rims
    for (cx, cy, r) in craters:
        gap_count = 2 if r > 5 else 1
        gap_angles = [rng.random() * math.pi * 2 for _ in range(gap_count)]
        gap_width = 0.5
        for r_i in range(cfg.rows):
            for c_i in range(cfg.cols):
                cell = grid.get_cell(r_i, c_i)
                if not cell.is_hazard or cell.is_shadowed:
                    continue
                angle = math.atan2(r_i - cy, c_i - cx)
                in_gap = False
                for ga in gap_angles:
                    diff = abs(angle - ga)
                    if diff > math.pi: diff = math.pi * 2 - diff
                    if diff < gap_width / 2:
                        in_gap = True
                        break
                if in_gap:
                    grid.get_cell(r_i, c_i).is_hazard = False
                    
    # Scatter boulder hazards
    boulder_count = int(cfg.cols * cfg.rows * 0.035)
    for _ in range(boulder_count):
        bx = int(rng.random() * cfg.cols)
        by = int(rng.random() * cfg.rows)
        cell = grid.get_cell(by, bx)
        if not cell.is_shadowed and not cell.is_hazard:
            grid.mark_hazard(by, bx)
            
    # Scatter ice candidates
    for (cx, cy, r) in craters:
        for r_i in range(cfg.rows):
            for c_i in range(cfg.cols):
                cell = grid.get_cell(r_i, c_i)
                if cell.is_shadowed:
                    cpr = 0.5 + rng.random() * 1.0
                    dop = rng.random() * 0.25
                    dc = 2.0 + rng.random() * 2.0
                    if cpr > 1.0 and dop < 0.13 and dc > 2.5:
                        dist_factor = 1 - math.hypot(c_i - cx, r_i - cy) / (r * 0.78)
                        base_vol = 800 + rng.random() * 5200
                        shift_mult = min(2.0, max(0.1, (dc - 2.5) * 2))
                        vol = round(base_vol * shift_mult * (0.5 + dist_factor * 0.6))
                        conf = min(0.97, max(0.42, 0.5 + dist_factor * 0.4 + (rng.random() - 0.5) * 0.15))
                        grid.mark_ice(r_i, c_i, vol, conf)

    # Ensure landing zone (2, 2) is safe
    grid.get_cell(2, 2).is_shadowed = False
    grid.get_cell(2, 2).is_hazard = False

    return grid


# ---------------------------------------------------------------------------
# Simplified polygon → grid rasterisation (bounding-box method)
# ---------------------------------------------------------------------------

def _rasterise_polygon_bbox(
    grid: PolarGrid,
    ring: list[list[float]],
    hazard: bool = False,
    ice_volume_m3: float = 0.0,
    ice_confidence: float = 0.0,
) -> None:
    """
    Mark all grid cells whose centres fall within the bounding box of a
    polygon ring.  [lon, lat] coordinate order (GeoJSON standard).

    Good enough for the hackathon; replace with shapely.contains() or
    rasterio.features.rasterize() for production accuracy.
    """
    lons = [pt[0] for pt in ring]
    lats = [pt[1] for pt in ring]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)

    row_min, col_min = grid.lat_lon_to_cell(min_lat, min_lon)
    row_max, col_max = grid.lat_lon_to_cell(max_lat, max_lon)

    for r in range(min(row_min, row_max), max(row_min, row_max) + 1):
        for c in range(min(col_min, col_max), max(col_min, col_max) + 1):
            if not grid.in_bounds(r, c):
                continue
            if hazard:
                grid.mark_hazard(r, c)
            if ice_volume_m3 > 0:
                grid.mark_ice(r, c, ice_volume_m3, ice_confidence)
