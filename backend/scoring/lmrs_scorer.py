"""
scoring/lmrs_scorer.py
-----------------------
Lunar Mining Readiness Score (LMRS) computation.

Computes a 0–100 composite score from three sub-scores:

  RAI  (Resource Accessibility Index)
    → ice volume + depth + confidence vs extraction difficulty

  CommVisibility
    → simplified LOS check vs fixed Earth direction vector
      (Earth elevation ≈ 18° above horizon at lunar south pole)

  ThermalRisk
    → Wh cost to reach the point from the reference lander,
      inverted so higher = safer = higher score

All sub-scores normalised independently to [0, 100] before weighting.
Weights default to (0.45, 0.25, 0.30) — biased toward RAI per spec.

This module is framework-agnostic (no FastAPI imports).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Any

from pathfinder import CostConfig, PolarGrid, StaticBatteryModel, plan_route
from pathfinder.types import WaypointType


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Earth elevation angle above horizon at the lunar south pole (degrees).
# At lat ≈ −90°, Earth's declination + libration keeps it ≈ 18° elevation.
EARTH_ELEVATION_DEG: float = 18.0

# Reference "worst-case" energy cost used for thermal score normalisation.
# A rover crossing a 40×40 grid entirely in shadow at ~0.2 Wh/m, 5m cells:
#   40 cells × 5 m × 0.2 Wh/m ≈ 40 Wh minimum; deep shadow missions ≈ 500 Wh
MAX_ENERGY_WH: float = 500.0

# If no ice candidate exists within this radius, RAI gets a strong penalty.
NO_ICE_RADIUS_PENALTY_M: float = 2000.0


# ---------------------------------------------------------------------------
# Output dataclasses (mirrored by Pydantic models in api/models.py)
# ---------------------------------------------------------------------------

@dataclass
class RAIBreakdown:
    score: float
    ice_volume_m3: float
    ice_depth_m: float
    confidence: float
    extraction_difficulty: float
    nearest_ice_distance_m: float


@dataclass
class CommVisibilityBreakdown:
    score: float
    los_fraction: float
    occlusion_reason: Optional[str]


@dataclass
class ThermalRiskBreakdown:
    score: float
    energy_cost_wh: float
    mean_temperature_k: float
    dark_dwell_fraction: float


@dataclass
class LMRSResult:
    lmrs_score: float
    rai: RAIBreakdown
    comm_visibility: CommVisibilityBreakdown
    thermal_risk: ThermalRiskBreakdown


# ---------------------------------------------------------------------------
# Default weights
# ---------------------------------------------------------------------------

@dataclass
class LMRSWeights:
    rai: float = 0.45
    comm_visibility: float = 0.25
    thermal_risk: float = 0.30

    def __post_init__(self) -> None:
        total = self.rai + self.comm_visibility + self.thermal_risk
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"LMRS weights must sum to 1.0 (got {total:.3f})")


# ===========================================================================
# Sub-score computers
# ===========================================================================

def compute_rai(
    lat: float,
    lon: float,
    grid: PolarGrid,
    ice_features: list[dict[str, Any]],
) -> RAIBreakdown:
    """
    Resource Accessibility Index.

    Algorithm
    ---------
    1. Find the nearest ice-candidate polygon to (lat, lon) using
       centroid distance.
    2. Retrieve volume_m3, depth_m, confidence from its properties.
    3. Compute extraction_difficulty = f(depth, distance_to_rover).
    4. Normalise RAI to [0, 100].

    If no ice is present in the region, returns score=0 with sensible defaults.
    """
    if not ice_features:
        return RAIBreakdown(
            score=0.0,
            ice_volume_m3=0.0,
            ice_depth_m=0.0,
            confidence=0.0,
            extraction_difficulty=1.0,
            nearest_ice_distance_m=NO_ICE_RADIUS_PENALTY_M,
        )

    row_q, col_q = grid.lat_lon_to_cell(lat, lon)
    cell_q = grid.get_cell(row_q, col_q)

    best_dist_m = math.inf
    best_props: dict[str, Any] = {}

    for feature in ice_features:
        ring = feature["geometry"]["coordinates"][0]
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        c_lat = sum(lats) / len(lats)
        c_lon = sum(lons) / len(lons)

        row_c, col_c = grid.lat_lon_to_cell(c_lat, c_lon)
        dist_m = grid.cell_distance_m(row_q, col_q, row_c, col_c)

        if dist_m < best_dist_m:
            best_dist_m = dist_m
            best_props = feature["properties"]

    volume_m3: float = float(best_props.get("volume_m3", 0.0))
    depth_m: float = float(best_props.get("depth_m", 5.0))
    confidence: float = float(best_props.get("confidence", 0.0))

    # Extraction difficulty: depth-weighted + distance-weighted, 0–1
    # Deeper ice + farther rover access = harder
    depth_factor = min(depth_m / 5.0, 1.0)           # 5 m = max science depth
    dist_factor = min(best_dist_m / NO_ICE_RADIUS_PENALTY_M, 1.0)
    extraction_difficulty = 0.6 * depth_factor + 0.4 * dist_factor

    # RAI raw: high volume + high confidence + low difficulty = high score
    # Volume normalised against a generous upper bound (10 000 m³)
    volume_norm = min(volume_m3 / 10_000.0, 1.0)
    rai_raw = (
        0.45 * volume_norm
        + 0.30 * confidence
        + 0.25 * (1.0 - extraction_difficulty)
    )
    rai_score = round(rai_raw * 100.0, 1)

    return RAIBreakdown(
        score=rai_score,
        ice_volume_m3=volume_m3,
        ice_depth_m=depth_m,
        confidence=confidence,
        extraction_difficulty=round(extraction_difficulty, 3),
        nearest_ice_distance_m=round(best_dist_m, 1),
    )


def compute_comm_visibility(
    lat: float,
    lon: float,
    grid: PolarGrid,
    orbiter_elevation_deg: float = EARTH_ELEVATION_DEG,
) -> CommVisibilityBreakdown:
    """
    Communication line-of-sight visibility sub-score.

    Simplified model (hackathon):
    - Earth sits at ~18° elevation above the horizon.
    - A cell on the sunlit crater rim has clear LOS  → los_fraction ≈ 0.9
    - A cell in the permanently shadowed crater floor is likely occluded
      by the rim → los_fraction ≈ 0.25
    - The local terrain slope is compared against the Earth elevation angle;
      if slope_deg > (90 − earth_elevation_deg), local terrain occludes signal.

    Production upgrade path: replace with a proper ray-marching DEM scan
    using the actual slope/aspect rasters from Member 1.
    """
    row, col = grid.lat_lon_to_cell(lat, lon)
    cell = grid.get_cell(row, col)

    occlusion_reason: Optional[str] = None

    # 1. Local slope occlusion — if ground slopes away steeper than Earth is up
    slope_occludes = cell.slope_deg > (90.0 - orbiter_elevation_deg)
    if slope_occludes:
        occlusion_reason = "steep_local_terrain"

    # 2. Crater rim occlusion inferred from illumination / shadow state
    if cell.is_shadowed and cell.solar_illumination == 0.0:
        # Deeply shadowed cell — crater rim almost certainly blocks line-of-sight
        if occlusion_reason is None:
            occlusion_reason = "crater_rim_occlusion"
        los_fraction = 0.22
    elif cell.solar_illumination > 0.6:
        # Sunlit rim — clear LOS with minor atmospheric margin
        los_fraction = 0.88 + 0.07 * cell.solar_illumination   # 0.88–0.95
        los_fraction = min(los_fraction, 0.95)
    elif cell.solar_illumination > 0.0:
        # Partial illumination / edge case
        los_fraction = 0.50 + 0.30 * cell.solar_illumination
        if occlusion_reason is None:
            occlusion_reason = "partial_rim_shadow"
    else:
        # Default: unlit but not permanently shadowed (ambiguous)
        los_fraction = 0.45
        if occlusion_reason is None:
            occlusion_reason = "unknown_shadow_state"

    if slope_occludes:
        los_fraction *= 0.4   # local terrain significantly degrades link budget

    los_fraction = round(max(0.0, min(1.0, los_fraction)), 3)
    comm_score = round(los_fraction * 100.0, 1)

    return CommVisibilityBreakdown(
        score=comm_score,
        los_fraction=los_fraction,
        occlusion_reason=occlusion_reason,
    )


def compute_thermal_risk(
    lat: float,
    lon: float,
    grid: PolarGrid,
    lander_lat: float,
    lander_lon: float,
    cost_config: Optional[CostConfig] = None,
    max_energy_wh: float = MAX_ENERGY_WH,
) -> ThermalRiskBreakdown:
    """
    Thermal / energy risk sub-score.

    Runs the pathfinder from the reference lander to the query point,
    then inverts the energy cost to a 0–100 score where 100 = zero cost.

    If the point is unreachable (route_found=False), score = 0.
    """
    if cost_config is None:
        cost_config = CostConfig(battery_model=StaticBatteryModel())

    result = plan_route(
        grid=grid,
        start_lat=lander_lat,
        start_lon=lander_lon,
        end_lat=lat,
        end_lon=lon,
        config=cost_config,
    )

    if not result.route_found:
        # Unreachable from lander — worst possible thermal score
        row, col = grid.lat_lon_to_cell(lat, lon)
        cell = grid.get_cell(row, col)
        return ThermalRiskBreakdown(
            score=0.0,
            energy_cost_wh=max_energy_wh,
            mean_temperature_k=cell.temperature_k,
            dark_dwell_fraction=1.0,
        )

    energy_wh = result.total_energy_wh
    thermal_score = max(0.0, 100.0 * (1.0 - energy_wh / max_energy_wh))

    # Dark dwell fraction: fraction of TRANSIT waypoints that are shadowed
    transit_wps = [
        wp for wp in result.waypoints
        if wp.type == WaypointType.TRANSIT
    ]
    shadowed_count = sum(1 for wp in transit_wps if wp.is_shadowed)
    dark_fraction = (shadowed_count / len(transit_wps)) if transit_wps else 0.0

    # Temperature at destination cell
    row_d, col_d = grid.lat_lon_to_cell(lat, lon)
    dest_temp_k = grid.get_cell(row_d, col_d).temperature_k

    return ThermalRiskBreakdown(
        score=round(thermal_score, 1),
        energy_cost_wh=round(energy_wh, 3),
        mean_temperature_k=round(dest_temp_k, 1),
        dark_dwell_fraction=round(dark_fraction, 3),
    )


# ===========================================================================
# Top-level LMRS scorer
# ===========================================================================

def compute_lmrs(
    lat: float,
    lon: float,
    grid: PolarGrid,
    ice_features: list[dict[str, Any]],
    lander_lat: float,
    lander_lon: float,
    weights: Optional[LMRSWeights] = None,
    cost_config: Optional[CostConfig] = None,
) -> LMRSResult:
    """
    Compute the full LMRS for a single (lat, lon) point.

    Returns an LMRSResult with all three sub-score breakdowns and
    the weighted composite score.
    """
    if weights is None:
        weights = LMRSWeights()

    rai = compute_rai(lat, lon, grid, ice_features)
    comm = compute_comm_visibility(lat, lon, grid)
    thermal = compute_thermal_risk(lat, lon, grid, lander_lat, lander_lon, cost_config)

    composite = round(
        weights.rai * rai.score
        + weights.comm_visibility * comm.score
        + weights.thermal_risk * thermal.score,
        1,
    )
    composite = max(0.0, min(100.0, composite))

    return LMRSResult(
        lmrs_score=composite,
        rai=rai,
        comm_visibility=comm,
        thermal_risk=thermal,
    )
