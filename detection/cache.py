"""
detection/cache.py
------------------
In-process LRU-style cache for computed rasters and GeoJSON layers.

Caching strategy
----------------
- Rasters are expensive (connected-component labelling, gradient computation).
  Generate once per region on first request; store in a dict keyed by region_id.
- Cache is populated lazily on first GET; never expires during a single server run.
- Thread-safe for the asyncio single-threaded event loop — no locks needed.
- In production: swap this for Redis or Memcached by replacing _RASTER_CACHE
  with async get/set calls.
"""

from __future__ import annotations

from typing import Optional

from .models import IceLayerResponse, HazardLayerResponse
from .synthetic import SyntheticRasters, generate_synthetic_rasters
from .pipeline import build_ice_layer, build_hazard_layer

# Matches the regions in backend/data/region_registry.py
REGION_ORIGINS: dict[str, tuple[float, float]] = {
    "shackleton-east": (-89.55, 44.00),
    "haworth":         (-87.85, -0.05),
    "shackleton":      (-89.90, -0.70),
    "faustini":        (-87.30, 77.00),
    "degerlache":      (-88.50, -87.10),
}

# Grid dimensions (rows × cols) matching RegionConfig in region_registry.py
REGION_GRID_DIMS: dict[str, tuple[int, int]] = {
    "shackleton-east": (40, 40),
    "haworth":         (30, 30),
    "shackleton":      (28, 46),
    "faustini":        (28, 46),
    "degerlache":      (28, 46),
}

# In-process cache storage
_RASTER_CACHE: dict[str, SyntheticRasters] = {}
_ICE_CACHE: dict[str, IceLayerResponse] = {}
_HAZARD_CACHE: dict[str, HazardLayerResponse] = {}


def get_available_regions() -> list[str]:
    return list(REGION_ORIGINS.keys())


def _get_rasters(region_id: str) -> SyntheticRasters:
    if region_id not in _RASTER_CACHE:
        origin = REGION_ORIGINS.get(region_id, (-89.55, 44.00))
        dims = REGION_GRID_DIMS.get(region_id, (40, 40))
        # Use 5.0 m cell size to match PolarGrid, upsample to 64×64 for SAR fidelity
        rasters = generate_synthetic_rasters(
            region_id=region_id,
            rows=max(dims[0], 40),   # at least 40 rows for meaningful ICE detection
            cols=max(dims[1], 40),
            cell_size_m=30.0,        # DFSAR posting interval ~ 30 m
        )
        _RASTER_CACHE[region_id] = rasters
    return _RASTER_CACHE[region_id]


def get_ice_layer(region_id: str) -> IceLayerResponse:
    """Return cached ice GeoJSON; generate on first access."""
    if region_id not in _ICE_CACHE:
        origin = REGION_ORIGINS.get(region_id, (-89.55, 44.00))
        rasters = _get_rasters(region_id)
        _ICE_CACHE[region_id] = build_ice_layer(
            rasters=rasters,
            region_id=region_id,
            origin_lat=origin[0],
            origin_lon=origin[1],
        )
    return _ICE_CACHE[region_id]


def get_hazard_layer(region_id: str) -> HazardLayerResponse:
    """Return cached hazard GeoJSON; generate on first access."""
    if region_id not in _HAZARD_CACHE:
        origin = REGION_ORIGINS.get(region_id, (-89.55, 44.00))
        rasters = _get_rasters(region_id)
        _HAZARD_CACHE[region_id] = build_hazard_layer(
            rasters=rasters,
            region_id=region_id,
            origin_lat=origin[0],
            origin_lon=origin[1],
        )
    return _HAZARD_CACHE[region_id]


def invalidate_region(region_id: str) -> None:
    """Force cache miss for a region (useful for testing)."""
    _RASTER_CACHE.pop(region_id, None)
    _ICE_CACHE.pop(region_id, None)
    _HAZARD_CACHE.pop(region_id, None)


def prewarm_all() -> None:
    """Eagerly populate caches for all known regions."""
    for region_id in REGION_ORIGINS:
        get_ice_layer(region_id)
        get_hazard_layer(region_id)
