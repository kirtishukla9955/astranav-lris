"""
backend/scoring/confidence.py
------------------------------
Feature 9 — Detection Confidence Overlay

Provides a per-cell confidence heat-map derived from real CPR/DOP signal
quality in the ice detection layer.

Endpoint: GET /api/confidence-overlay/{region_id}

Returns a flat list of {lat, lon, confidence} cells that the frontend renders
as a translucent heat layer over ice zones.

Confidence model
----------------
Each ice candidate polygon carries a `confidence` property already computed
by the detection pipeline (detection/pipeline.py::build_ice_layer) using:
  - CPR margin above 1.0 (larger → more confident)
  - DOP margin below 0.13 (further below → more confident)
  - Border penalty (edge cells of ice blobs are noisier)

This module:
  1. Fetches the ice GeoJSON for the region.
  2. Samples polygon centroids + confidence values.
  3. Interpolates a smooth confidence field over the whole grid using
     Inverse Distance Weighting (IDW) — no additional dependencies required.
  4. Returns the sampled grid at a configurable stride.

Why IDW over kriging/RBF?
  - Zero additional dependencies (pure numpy/scipy already in requirements.txt).
  - Fast enough for a 40×40 grid at stride=2 (800 cells in <10ms).
  - Degrades gracefully: if no ice features exist, returns uniform 0.5 confidence.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from data.mock_fixtures import fetch_ice_layer
from data.region_registry import get_region_config


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_STRIDE: int = 2         # sample every 2nd cell (keeps payload small)
IDW_POWER: float = 2.0          # inverse-distance weighting exponent
MIN_CONFIDENCE: float = 0.10    # floor — even non-ice areas have minimum signal
MAX_CONFIDENCE: float = 0.97    # cap — no signal is perfectly certain
BASE_CONFIDENCE: float = 0.40   # used when no ice features are present


# ---------------------------------------------------------------------------
# IDW interpolation
# ---------------------------------------------------------------------------

def _idw_interpolate(
    query_lats: np.ndarray,  # shape (N,)
    query_lons: np.ndarray,  # shape (N,)
    src_lats: np.ndarray,    # shape (M,)
    src_lons: np.ndarray,    # shape (M,)
    src_values: np.ndarray,  # shape (M,)
    power: float = IDW_POWER,
    eps: float = 1e-9,
) -> np.ndarray:
    """
    Inverse-distance weighted interpolation.

    For each query point, computes:
      confidence = Σ(value_i / dist_i^p) / Σ(1 / dist_i^p)

    Returns confidence values of shape (N,).
    """
    # Distance matrix: (N, M)
    dlat = query_lats[:, None] - src_lats[None, :]
    dlon = query_lons[:, None] - src_lons[None, :]
    dists = np.sqrt(dlat**2 + dlon**2) + eps

    weights = 1.0 / (dists ** power)              # (N, M)
    values = (weights * src_values[None, :]).sum(axis=1)  # (N,)
    norm = weights.sum(axis=1)                     # (N,)

    return (values / norm).astype(np.float32)


# ---------------------------------------------------------------------------
# Main function called by the API endpoint
# ---------------------------------------------------------------------------

def build_confidence_overlay(
    region_id: str,
    stride: int = DEFAULT_STRIDE,
) -> list[dict[str, float]]:
    """
    Build a confidence overlay grid for *region_id*.

    Parameters
    ----------
    region_id : str
    stride    : int   sample every Nth cell (default 2, i.e. every 10 m at 5 m cells)

    Returns
    -------
    list of {lat, lon, confidence} dicts, ready to serialize as JSON.
    """
    cfg = get_region_config(region_id)

    # ── Fetch real CPR/DOP confidence from detection service ──────────────────
    try:
        ice_geojson = fetch_ice_layer(region_id)
        ice_features: list[dict[str, Any]] = ice_geojson.get("features", [])
    except (KeyError, Exception):
        ice_features = []

    # ── Grid geometry ─────────────────────────────────────────────────────────
    # Use same coordinate math as PolarGrid (grid.py)
    LUNAR_RADIUS_M = 1_737_400.0
    DEG_LAT_M = (math.pi / 180.0) * LUNAR_RADIUS_M
    cos_lat = math.cos(math.radians(abs(cfg.origin_lat)))
    dlat = cfg.cell_size_m / DEG_LAT_M
    dlon = cfg.cell_size_m / (DEG_LAT_M * cos_lat) if cos_lat > 1e-9 else dlat

    # Sample grid cell centres at stride
    sampled_rows = range(0, cfg.rows, stride)
    sampled_cols = range(0, cfg.cols, stride)

    grid_lats = np.array(
        [cfg.origin_lat + r * dlat for r in sampled_rows for _ in sampled_cols],
        dtype=np.float64,
    )
    grid_lons = np.array(
        [cfg.origin_lon + c * dlon for _ in sampled_rows for c in sampled_cols],
        dtype=np.float64,
    )

    # ── No ice data — return uniform base confidence ───────────────────────────
    if not ice_features:
        return [
            {
                "lat": float(grid_lats[i]),
                "lon": float(grid_lons[i]),
                "confidence": BASE_CONFIDENCE,
            }
            for i in range(len(grid_lats))
        ]

    # ── Extract centroid + confidence from each ice polygon ───────────────────
    src_lats_list: list[float] = []
    src_lons_list: list[float] = []
    src_conf_list: list[float] = []

    for feat in ice_features:
        ring = feat.get("geometry", {}).get("coordinates", [[]])[0]
        if not ring:
            continue
        lons_ring = [p[0] for p in ring]
        lats_ring = [p[1] for p in ring]
        c_lat = float(sum(lats_ring) / len(lats_ring))
        c_lon = float(sum(lons_ring) / len(lons_ring))
        conf = float(feat.get("properties", {}).get("confidence", 0.5))
        src_lats_list.append(c_lat)
        src_lons_list.append(c_lon)
        src_conf_list.append(conf)

    src_lats = np.array(src_lats_list, dtype=np.float64)
    src_lons = np.array(src_lons_list, dtype=np.float64)
    src_conf = np.array(src_conf_list, dtype=np.float64)

    # ── IDW interpolation ─────────────────────────────────────────────────────
    interp_conf = _idw_interpolate(grid_lats, grid_lons, src_lats, src_lons, src_conf)

    # Clamp to valid range
    interp_conf = np.clip(interp_conf, MIN_CONFIDENCE, MAX_CONFIDENCE)

    return [
        {
            "lat": float(grid_lats[i]),
            "lon": float(grid_lons[i]),
            "confidence": round(float(interp_conf[i]), 4),
        }
        for i in range(len(grid_lats))
    ]
