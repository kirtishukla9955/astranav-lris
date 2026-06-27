"""
illumination.py — Feature A: Lunar Day/Night Solar Illumination Sweep

Simulates how solar illumination sweeps across the crater rim over one compressed
lunar day (~29.5 Earth days → configurable num_frames discrete timesteps).

MOCK DATA / simplified model:
  - The sun is modelled as a unit vector rotating in azimuth (0° → 360°) at a fixed
    elevation angle. Real illumination would use LOLA DEM + SPICE ephemeris kernels
    and ray-casting against actual topographic normals. Replace simulate_illumination()
    with a real SPICE/DEM pipeline when Member 1's ingestion layer is ready.
  - Per-cell "illumination" is approximated by: how well the current sun azimuth aligns
    with the cell's effective "open" direction (derived from its position relative to the
    grid centre, as a proxy for crater-wall orientation). Cells in the static shadow_map
    are treated as permanently shadowed (deep crater floor) regardless of sun angle.
  - Pitstop eligibility mirrors find_nearest_sunlit_cell() in pathfinder.py:
      not permanently shadowed AND passable (cost < inf) AND illumination_pct >= 50.
"""

import math
from typing import List

from cost_grid import CostGrid
from schemas import (
    IlluminationCell,
    IlluminationFrame,
    IlluminationTimelapseResponse,
)

# ── Constants ─────────────────────────────────────────────────────────────────

# One real lunar day ≈ 708.7 hours.  We compress to a configurable window.
LUNAR_DAY_HOURS: float = 708.7

# Sun elevation above the lunar south pole horizon is low (~1.5° max).
# MOCK DATA: fixed constant; real model would interpolate from ephemeris.
SUN_ELEVATION_DEG: float = 1.5

# Stride used when sampling grid cells for the response payload.
# At stride=5 a 100×100 grid → 400 cells per frame (manageable payload).
CELL_STRIDE: int = 5

# Illumination threshold above which a sunlit rim cell is considered
# eligible as a Solar Charging Pitstop (mirrors pathfinder.py criterion).
PITSTOP_ILLUMINATION_THRESHOLD: float = 50.0


# ── Internal helpers ──────────────────────────────────────────────────────────

def _sun_angle_at_step(step: int, num_frames: int) -> float:
    """
    Returns the sun azimuth in degrees [0, 360) for a given timestep.
    MOCK DATA / simplified model: uniform linear sweep over one full rotation.
    """
    return (step / num_frames) * 360.0


def _cell_illumination_pct(
    grid_x: int,
    grid_y: int,
    grid_width: int,
    grid_height: int,
    sun_az_deg: float,
    is_permanently_shadowed: bool,
) -> float:
    """
    Estimates illumination percentage for a single cell given the current sun azimuth.

    MOCK DATA / simplified model:
    We treat each cell's "orientation" as the azimuth from the grid centre to the cell.
    The illumination is the cosine similarity between the sun direction and this
    orientation vector, clamped to [0, 1] and scaled to [0, 100].

    In production this would be replaced by a proper horizon-angle computation
    using the actual LOLA DEM topography (shadowing by crater walls).
    """
    if is_permanently_shadowed:
        # Deep crater floor — never reached by direct sunlight.
        return 0.0

    # Orientation of this cell relative to grid centre (proxy for topographic normal).
    cx = grid_width / 2.0
    cy = grid_height / 2.0
    dx = grid_x - cx
    dy = grid_y - cy

    if dx == 0 and dy == 0:
        # Centre cell — treat as isotropically lit; use elevation angle only.
        return max(0.0, math.sin(math.radians(SUN_ELEVATION_DEG)) * 100.0)

    cell_az_deg = math.degrees(math.atan2(dy, dx)) % 360.0

    # Angular difference between sun and cell orientation.
    diff = abs(sun_az_deg - cell_az_deg)
    if diff > 180.0:
        diff = 360.0 - diff

    # cos(0°) = 1 (sun facing cell directly) → 100 % illumination
    # cos(90°) = 0 → 0 % illumination (sun perpendicular)
    # Negative cosine (diff > 90°) → 0 % (cell in own shadow)
    cos_val = math.cos(math.radians(diff))
    illumination_pct = max(0.0, cos_val) * 100.0

    # Apply a low-elevation damping: south polar sun is very close to horizon,
    # so even facing cells receive attenuated light.
    # MOCK DATA: simple sin(elevation) factor.
    elevation_factor = math.sin(math.radians(SUN_ELEVATION_DEG))
    illumination_pct *= max(elevation_factor, 0.1)  # floor at 10 % for numerical stability

    return round(illumination_pct, 2)


def _build_frame(
    grid: CostGrid,
    timestep: int,
    sun_az_deg: float,
    stride: int,
) -> IlluminationFrame:
    """Builds one IlluminationFrame for the given sun azimuth."""
    cells: List[IlluminationCell] = []

    for gy in range(0, grid.height, stride):
        for gx in range(0, grid.width, stride):
            is_perm_shadow = grid.is_in_shadow(gx, gy)
            traversal_cost = grid.get_traversal_cost(gx, gy)
            is_passable = traversal_cost != float("inf")

            illumination_pct = _cell_illumination_pct(
                gx, gy,
                grid.width, grid.height,
                sun_az_deg,
                is_perm_shadow,
            )

            # Pitstop eligibility: mirrors find_nearest_sunlit_cell() logic in pathfinder.py
            is_pitstop = (
                not is_perm_shadow
                and is_passable
                and illumination_pct >= PITSTOP_ILLUMINATION_THRESHOLD
            )

            cells.append(
                IlluminationCell(
                    lat=gy * 0.0001,   # mock lat/lon convention (matches pathfinder.py)
                    lon=gx * 0.0001,
                    illumination_pct=illumination_pct,
                    is_pitstop_eligible=is_pitstop,
                )
            )

    return IlluminationFrame(
        timestep=timestep,
        sun_angle_deg=round(sun_az_deg, 2),
        cells=cells,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def simulate_illumination(
    grid: CostGrid,
    region_id: str,
    num_frames: int = 100,
    duration_hours: float = LUNAR_DAY_HOURS,
    cell_stride: int = CELL_STRIDE,
) -> IlluminationTimelapseResponse:
    """
    Simulates solar illumination sweeping over the crater rim for one (compressed)
    lunar day and returns a sequence of IlluminationFrames.

    Args:
        grid:           The CostGrid whose shadow_map and traversal costs are used.
        region_id:      Identifier for the lunar region (passed through to response).
        num_frames:     Number of discrete timesteps (default 100).
        duration_hours: Simulated window in hours (default ≈ one lunar day = 708.7 h).
        cell_stride:    Sampling stride over grid cells (default 5 = every 5th cell).

    Returns:
        IlluminationTimelapseResponse ready to be returned by the FastAPI endpoint.

    MOCK DATA / simplified model — replace _cell_illumination_pct() with a SPICE +
    DEM horizon-angle ray-cast when Member 1's terrain data pipeline is available.
    """
    frames: List[IlluminationFrame] = []

    for step in range(num_frames):
        sun_az = _sun_angle_at_step(step, num_frames)
        frame = _build_frame(grid, step, sun_az, cell_stride)
        frames.append(frame)

    return IlluminationTimelapseResponse(
        region_id=region_id,
        duration_hours=duration_hours,
        num_frames=num_frames,
        frames=frames,
    )
