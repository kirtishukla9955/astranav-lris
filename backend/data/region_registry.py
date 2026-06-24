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

    # ── Step 2: shadow band & sunlit rim ─────────────────────────────────────
    shadow_start, shadow_end = cfg.shadow_band_rows
    for r in range(shadow_start, min(shadow_end + 1, cfg.rows)):
        for c in range(cfg.cols):
            grid.mark_shadow(r, c, illumination=0.0, temperature_k=25.0)

    rim_start, rim_end = cfg.sunlit_rim_rows
    for r in range(rim_start, min(rim_end + 1, cfg.rows)):
        for c in range(cfg.cols):
            illumination = 0.7 + 0.3 * (r - rim_start) / max(rim_end - rim_start, 1)
            grid.mark_illuminated(r, c, illumination=illumination, temperature_k=200.0)

    # ── Step 3: hazard layer ─────────────────────────────────────────────────
    try:
        hazard_geojson = fetch_hazard_layer(region_id)
        for feature in hazard_geojson.get("features", []):
            _rasterise_polygon_bbox(grid, feature["geometry"]["coordinates"][0], hazard=True)
    except KeyError:
        pass  # no hazard data for this region — non-fatal

    # ── Step 4: ice layer ────────────────────────────────────────────────────
    try:
        ice_geojson = fetch_ice_layer(region_id)
        for feature in ice_geojson.get("features", []):
            props = feature["properties"]
            _rasterise_polygon_bbox(
                grid,
                feature["geometry"]["coordinates"][0],
                ice_volume_m3=props.get("volume_m3", 0.0),
                ice_confidence=props.get("confidence", 0.0),
            )
    except KeyError:
        pass  # no ice data — non-fatal

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
