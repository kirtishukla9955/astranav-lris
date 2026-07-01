"""
detection/pipeline.py
---------------------
Layer 1 — Volumetric Ice Detection Core
Layer 2 — Morphological Hazard Mapping

Converts SyntheticRasters (or real GeoTIFF arrays) into GeoJSON
FeatureCollection payloads matching the schema in detection/models.py.

Vectorisation strategy
-----------------------
Connected-component labelling (scipy.ndimage.label) turns the binary ice_mask
into individual ice polygons — each component becomes one GeoJSON Feature.
Same approach for hazard zones.  This avoids exporting thousands of 1-pixel
features and instead produces clean, merge polygons the frontend can render.
"""

from __future__ import annotations

import math
import uuid
from typing import Optional

import numpy as np
from scipy import ndimage  # type: ignore[import]

from .models import (
    CPR_THRESHOLD,
    DOP_THRESHOLD,
    DIELECTRIC_DUST,
    DIELECTRIC_ICE,
    MAX_ICE_DEPTH_M,
    SLOPE_NOGO_DEG,
    BOULDER_NOGO_M,
    IceFeature,
    IceFeatureProperties,
    IceLayerResponse,
    HazardFeature,
    HazardFeatureProperties,
    HazardLayerResponse,
    PolygonGeometry,
)
from .synthetic import SyntheticRasters


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

# Lunar mean radius in metres (IAU 2015)
LUNAR_RADIUS_M: float = 1_737_400.0
DEG_LAT_M: float = (math.pi / 180.0) * LUNAR_RADIUS_M  # ≈ 30 328 m/°


def _cell_to_latlon(
    row: int,
    col: int,
    origin_lat: float,
    origin_lon: float,
    cell_size_m: float,
) -> tuple[float, float]:
    """Convert (row, col) grid index to (lat, lon) degrees."""
    dlat = cell_size_m / DEG_LAT_M
    cos_lat = math.cos(math.radians(abs(origin_lat)))
    dlon = cell_size_m / (DEG_LAT_M * cos_lat) if cos_lat > 1e-9 else dlat
    lat = origin_lat + row * dlat
    lon = origin_lon + col * dlon
    return lat, lon


def _bbox_polygon(
    min_row: int,
    max_row: int,
    min_col: int,
    max_col: int,
    origin_lat: float,
    origin_lon: float,
    cell_size_m: float,
) -> list[list[list[float]]]:
    """
    Build a GeoJSON Polygon ring from a bounding-box of grid cells.

    Returns [outer_ring] where outer_ring is a closed list of [lon, lat].
    """
    lat0, lon0 = _cell_to_latlon(min_row, min_col, origin_lat, origin_lon, cell_size_m)
    lat1, lon1 = _cell_to_latlon(max_row + 1, max_col + 1, origin_lat, origin_lon, cell_size_m)

    outer_ring = [
        [lon0, lat0],
        [lon1, lat0],
        [lon1, lat1],
        [lon0, lat1],
        [lon0, lat0],  # close ring
    ]
    return [outer_ring]


# ---------------------------------------------------------------------------
# Layer 1 — Ice Detection Pipeline
# ---------------------------------------------------------------------------

def build_ice_layer(
    rasters: SyntheticRasters,
    region_id: str,
    origin_lat: float = -89.55,
    origin_lon: float = 44.00,
    min_component_cells: int = 3,
) -> IceLayerResponse:
    """
    Convert a SyntheticRasters ice_mask into a GeoJSON FeatureCollection.

    Algorithm
    ---------
    1. Label connected components of the ice_mask.
    2. For each component larger than *min_component_cells*:
       a. Compute bounding-box polygon (lon, lat coords).
       b. Aggregate CPR, DOP, dielectric, depth, volume, confidence.
       c. Emit one IceFeature per component.

    Parameters
    ----------
    rasters           : SyntheticRasters from generate_synthetic_rasters()
    region_id         : str   echoed back in the response
    origin_lat        : float lat of grid cell (row=0, col=0)
    origin_lon        : float lon of grid cell (row=0, col=0)
    min_component_cells : int   discard tiny noise blobs smaller than this
    """
    labeled_array, n_features = ndimage.label(rasters.ice_mask)

    features: list[IceFeature] = []
    feat_counter = 1

    for label_id in range(1, n_features + 1):
        component = labeled_array == label_id
        cell_count = int(component.sum())

        if cell_count < min_component_cells:
            continue  # skip noise

        rows_idx, cols_idx = np.where(component)
        min_row, max_row = int(rows_idx.min()), int(rows_idx.max())
        min_col, max_col = int(cols_idx.min()), int(cols_idx.max())

        # Aggregate science values over the component
        mean_cpr = float(rasters.cpr[component].mean())
        mean_dop = float(rasters.dop[component].mean())
        mean_eps = float(rasters.dielectric[component].mean())
        mean_depth = float(rasters.ice_depth_m[component].mean())
        total_vol = float(rasters.ice_volume_m3[component].sum())
        mean_conf = float(rasters.confidence[component].mean())

        # Hard-enforce science thresholds (should always pass since ice_mask already filters)
        mean_cpr = max(mean_cpr, CPR_THRESHOLD + 0.001)
        mean_dop = min(mean_dop, DOP_THRESHOLD - 0.001)

        coords = _bbox_polygon(
            min_row, max_row, min_col, max_col,
            origin_lat, origin_lon, rasters.cell_size_m,
        )

        feat = IceFeature(
            geometry=PolygonGeometry(coordinates=coords),
            properties=IceFeatureProperties(
                ice_id=f"ICE-{feat_counter:03d}",
                cpr=round(mean_cpr, 4),
                dop=round(mean_dop, 4),
                dielectric_constant=round(mean_eps, 4),
                depth_m=round(mean_depth, 2),
                volume_m3=round(total_vol, 1),
                confidence=round(mean_conf, 4),
                cell_size_m=rasters.cell_size_m,
            ),
        )
        features.append(feat)
        feat_counter += 1

    return IceLayerResponse(
        region_id=region_id,
        features=features,
        metadata={
            "pipeline": "synthetic-dfsar-v1",
            "thresholds": {
                "cpr_min": CPR_THRESHOLD,
                "dop_max": DOP_THRESHOLD,
                "dielectric_dust": DIELECTRIC_DUST,
                "dielectric_ice": DIELECTRIC_ICE,
                "max_depth_m": MAX_ICE_DEPTH_M,
            },
            "grid": {
                "rows": rasters.rows,
                "cols": rasters.cols,
                "cell_size_m": rasters.cell_size_m,
            },
            "statistics": {
                "total_ice_features": len(features),
                "total_ice_volume_m3": round(float(rasters.ice_volume_m3.sum()), 1),
                "ice_coverage_pct": round(
                    float(rasters.ice_mask.sum()) / (rasters.rows * rasters.cols) * 100, 2
                ),
            },
        },
    )


# ---------------------------------------------------------------------------
# Layer 2 — Morphological Hazard Mapping Pipeline
# ---------------------------------------------------------------------------

def build_hazard_layer(
    rasters: SyntheticRasters,
    region_id: str,
    origin_lat: float = -89.55,
    origin_lon: float = 44.00,
    min_component_cells: int = 2,
) -> HazardLayerResponse:
    """
    Convert slope > 15° zones and boulder obstacle masks into GeoJSON hazards.

    Two sub-masks are processed independently:
    - slope_mask   → hazard_type = "steep_slope" or "crater_wall"
    - obstacle_mask → hazard_type = "boulder_field"

    Each connected component becomes one HazardFeature polygon.
    """
    features: list[HazardFeature] = []
    feat_counter = 1

    # ── Slope hazards ────────────────────────────────────────────────────────
    slope_nogo = rasters.slope_deg > SLOPE_NOGO_DEG

    labeled_slope, n_slope = ndimage.label(slope_nogo)
    for label_id in range(1, n_slope + 1):
        component = labeled_slope == label_id
        if int(component.sum()) < min_component_cells:
            continue

        rows_idx, cols_idx = np.where(component)
        min_row, max_row = int(rows_idx.min()), int(rows_idx.max())
        min_col, max_col = int(cols_idx.min()), int(cols_idx.max())

        mean_slope = float(rasters.slope_deg[component].mean())
        max_slope = float(rasters.slope_deg[component].max())

        # Classify: crater wall if also in shadow zone, else steep_slope
        in_shadow_frac = float(rasters.shadow_mask[component].mean())
        htype = "crater_wall" if in_shadow_frac > 0.5 else "steep_slope"

        coords = _bbox_polygon(
            min_row, max_row, min_col, max_col,
            origin_lat, origin_lon, rasters.cell_size_m,
        )

        features.append(HazardFeature(
            geometry=PolygonGeometry(coordinates=coords),
            properties=HazardFeatureProperties(
                hazard_id=f"HAZ-{feat_counter:03d}",
                hazard_type=htype,    # type: ignore[arg-type]
                severity="no_go",
                slope_deg=round(max_slope, 1),
            ),
        ))
        feat_counter += 1

    # ── Boulder / obstacle hazards ───────────────────────────────────────────
    labeled_obs, n_obs = ndimage.label(rasters.obstacle_mask)
    for label_id in range(1, n_obs + 1):
        component = labeled_obs == label_id
        if int(component.sum()) < min_component_cells:
            continue

        rows_idx, cols_idx = np.where(component)
        min_row, max_row = int(rows_idx.min()), int(rows_idx.max())
        min_col, max_col = int(cols_idx.min()), int(cols_idx.max())

        # Diameter estimate from component extent
        extent_cells = max(max_row - min_row + 1, max_col - min_col + 1)
        est_diameter_m = round(extent_cells * rasters.cell_size_m, 1)

        coords = _bbox_polygon(
            min_row, max_row, min_col, max_col,
            origin_lat, origin_lon, rasters.cell_size_m,
        )

        features.append(HazardFeature(
            geometry=PolygonGeometry(coordinates=coords),
            properties=HazardFeatureProperties(
                hazard_id=f"HAZ-{feat_counter:03d}",
                hazard_type="boulder_field",
                severity="no_go",
                max_boulder_diameter_m=est_diameter_m,
            ),
        ))
        feat_counter += 1

    return HazardLayerResponse(
        region_id=region_id,
        features=features,
        metadata={
            "pipeline": "synthetic-ohrc-dem-v1",
            "thresholds": {
                "slope_nogo_deg": SLOPE_NOGO_DEG,
                "boulder_nogo_m": BOULDER_NOGO_M,
            },
            "statistics": {
                "total_hazard_features": len(features),
                "slope_nogo_pct": round(
                    float(slope_nogo.sum()) / (rasters.rows * rasters.cols) * 100, 2
                ),
                "obstacle_pct": round(
                    float(rasters.obstacle_mask.sum()) / (rasters.rows * rasters.cols) * 100, 2
                ),
            },
        },
    )
