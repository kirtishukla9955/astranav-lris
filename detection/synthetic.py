"""
detection/synthetic.py
----------------------
Scientifically-calibrated synthetic raster generator.

Produces in-memory NumPy arrays that simulate what a Chandrayaan-2 DFSAR
Level-2 GRD product and a TMC-2 DEM would look like for a south-polar crater
region.  Used at startup so the service works without any real ISRO files.

Crater geometry
---------------
Each crater is modelled as:
  DEM elevation: z = -depth * exp(-(r/r0)^2)    (Gaussian bowl)
  SAR HH power : elevated inside crater (icy, low scattering)
  SAR HV power : elevated at crater interior (double-bounce ice signature)
  CPR = HV/HH  : >1.0 inside ice zone
  DOP = |HH-HV|/(HH+HV) : <0.13 inside ice zone

Physics references
------------------
  - Putzig et al. 2023 (Mars ice CPR analogy)
  - Spudis et al. 2013 (MINI-RF Chandrayaan-1 CPR signatures)
  - ISRO DFSAR Algorithm Theoretical Basis Document (public)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .models import (
    CPR_THRESHOLD,
    DOP_THRESHOLD,
    DIELECTRIC_DUST,
    DIELECTRIC_ICE,
    MAX_ICE_DEPTH_M,
    SLOPE_NOGO_DEG,
    BOULDER_NOGO_M,
)

# ---------------------------------------------------------------------------
# Raster specification
# ---------------------------------------------------------------------------

# Default cell size in metres (matches PolarGrid.cell_size_m = 5 m but we
# use 30 m here to represent the actual DFSAR posting interval)
DEFAULT_CELL_SIZE_M: float = 30.0

# Grid dimensions (rows × cols)
DEFAULT_ROWS: int = 64
DEFAULT_COLS: int = 64


@dataclass
class SyntheticRasters:
    """All synthetic layers for one region, as numpy float32 arrays."""

    rows: int
    cols: int
    cell_size_m: float

    # SAR bands (linear power, not dB)
    hh: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=np.float32))
    hv: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=np.float32))

    # Derived SAR products
    cpr: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=np.float32))
    dop: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=np.float32))
    dielectric: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=np.float32))

    # Ice detection outputs
    ice_mask: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=bool))
    ice_depth_m: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=np.float32))
    ice_volume_m3: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=np.float32))
    confidence: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=np.float32))

    # DEM + terrain
    elevation_m: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=np.float32))
    slope_deg: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=np.float32))
    shadow_mask: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=bool))

    # OHRC obstacle mask (boulder_diameter > BOULDER_NOGO_M)
    obstacle_mask: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=bool))


# ---------------------------------------------------------------------------
# Region seeds
# ---------------------------------------------------------------------------

_REGION_SEEDS: dict[str, int] = {
    "shackleton-east": 1337,
    "haworth": 5021,
    "shackleton": 4242,
    "faustini": 8080,
    "degerlache": 3141,
}

_DEFAULT_SEED: int = 9999


# ---------------------------------------------------------------------------
# Core generator
# ---------------------------------------------------------------------------

def generate_synthetic_rasters(
    region_id: str,
    rows: int = DEFAULT_ROWS,
    cols: int = DEFAULT_COLS,
    cell_size_m: float = DEFAULT_CELL_SIZE_M,
) -> SyntheticRasters:
    """
    Generate a full set of synthetic raster layers for *region_id*.

    Parameters
    ----------
    region_id  : str   must match one of the keys in REGION_REGISTRY
    rows, cols : int   grid dimensions (same as PolarGrid for the region)
    cell_size_m: float raster resolution in metres

    Returns
    -------
    SyntheticRasters  — all numpy arrays, float32, shape (rows, cols)
    """
    seed = _REGION_SEEDS.get(region_id, _DEFAULT_SEED)
    rng = np.random.default_rng(seed)

    out = SyntheticRasters(rows=rows, cols=cols, cell_size_m=cell_size_m)

    # ── Step 1: DEM — Gaussian crater bowls ─────────────────────────────────
    dem = np.zeros((rows, cols), dtype=np.float32)

    # Number and positions of craters (seeded, so consistent)
    n_craters = int(rng.integers(3, 7))
    craters: list[tuple[float, float, float, float]] = []   # (cy, cx, r0, depth)
    for _ in range(n_craters):
        cy = rng.uniform(0.15, 0.85) * rows
        cx = rng.uniform(0.15, 0.85) * cols
        r0 = rng.uniform(0.06, 0.14) * min(rows, cols)     # crater radius in cells
        depth = rng.uniform(800, 2500)                      # depth in metres
        craters.append((cy, cx, r0, depth))

    yy, xx = np.mgrid[0:rows, 0:cols].astype(np.float32)
    for (cy, cx, r0, depth) in craters:
        r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
        dem -= depth * np.exp(-(r / r0) ** 2)

    # Add gentle background terrain noise
    dem += (rng.standard_normal((rows, cols)) * 20).astype(np.float32)
    out.elevation_m = dem

    # ── Step 2: Slope from DEM ───────────────────────────────────────────────
    dzdx = np.gradient(dem, cell_size_m, axis=1)
    dzdy = np.gradient(dem, cell_size_m, axis=0)
    slope = np.degrees(np.arctan(np.sqrt(dzdx ** 2 + dzdy ** 2))).astype(np.float32)
    out.slope_deg = slope

    # ── Step 3: Shadow mask — crater interiors are permanently shadowed ───────
    shadow = np.zeros((rows, cols), dtype=bool)
    for (cy, cx, r0, depth) in craters:
        r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
        # Inner 75% of crater radius = permanently shadowed
        shadow |= (r < r0 * 0.75)
    out.shadow_mask = shadow

    # ── Step 4: SAR bands (synthetic HH, HV) ────────────────────────────────
    # Background: HH = 0.3, HV = 0.05  (rocky regolith, low CPR)
    hh = np.full((rows, cols), 0.30, dtype=np.float32)
    hv = np.full((rows, cols), 0.05, dtype=np.float32)

    # Add speckle noise (Rayleigh-distributed, 15% coefficient of variation)
    hh += (rng.standard_normal((rows, cols)) * 0.04).astype(np.float32)
    hv += (rng.standard_normal((rows, cols)) * 0.008).astype(np.float32)

    # Ice signatures inside shadowed crater interiors:
    # HV is boosted relative to HH → CPR > 1.0, DOP < 0.13
    for (cy, cx, r0, depth) in craters:
        r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
        ice_zone = r < r0 * 0.60                           # ice in innermost 60%

        # Gradient of ice strength toward centre
        ice_strength = np.clip(1.0 - r / (r0 * 0.60), 0, 1).astype(np.float32)
        ice_strength[~ice_zone] = 0.0

        # Boost HV strongly, boost HH mildly → CPR rises above 1.0
        hv = np.where(
            ice_zone,
            hv + ice_strength * rng.uniform(0.25, 0.55, (rows, cols)).astype(np.float32),
            hv,
        )
        hh = np.where(
            ice_zone,
            hh + ice_strength * rng.uniform(0.05, 0.15, (rows, cols)).astype(np.float32),
            hh,
        )

    # Ensure strictly positive (no log-of-zero errors later)
    hh = np.clip(hh, 1e-6, None).astype(np.float32)
    hv = np.clip(hv, 1e-6, None).astype(np.float32)
    out.hh = hh
    out.hv = hv

    # ── Step 5: CPR and DOP ──────────────────────────────────────────────────
    cpr = (hv / hh).astype(np.float32)
    dop = (np.abs(hh - hv) / (hh + hv)).astype(np.float32)
    out.cpr = cpr
    out.dop = dop

    # ── Step 6: Ice mask — ISRO thresholds ───────────────────────────────────
    ice_mask = (cpr > CPR_THRESHOLD) & (dop < DOP_THRESHOLD)
    out.ice_mask = ice_mask

    # ── Step 7: Dielectric constant ε (2.5 dust → 3.5 ice) ──────────────────
    # Linear interpolation on normalised CPR excess above threshold
    cpr_excess = np.clip((cpr - CPR_THRESHOLD) / 2.0, 0.0, 1.0)
    dielectric = (
        DIELECTRIC_DUST + (DIELECTRIC_ICE - DIELECTRIC_DUST) * cpr_excess
    ).astype(np.float32)
    out.dielectric = dielectric

    # ── Step 8: Ice depth & volume ───────────────────────────────────────────
    # depth_m = MAX_ICE_DEPTH_M × (ε − 2.5) / (3.5 − 2.5)
    depth_m = (
        MAX_ICE_DEPTH_M
        * (dielectric - DIELECTRIC_DUST)
        / (DIELECTRIC_ICE - DIELECTRIC_DUST)
    ).astype(np.float32)
    depth_m = np.where(ice_mask, depth_m, 0.0).astype(np.float32)

    cell_area_m2 = cell_size_m ** 2
    volume_m3 = (depth_m * cell_area_m2).astype(np.float32)

    out.ice_depth_m = depth_m
    out.ice_volume_m3 = volume_m3

    # ── Step 9: Confidence score ─────────────────────────────────────────────
    # CPR margin above threshold (larger → more confident)
    cpr_margin = np.clip((cpr - CPR_THRESHOLD) / 1.5, 0.0, 1.0)
    # DOP margin below threshold (further below → more confident)
    dop_margin = np.clip((DOP_THRESHOLD - dop) / DOP_THRESHOLD, 0.0, 1.0)
    conf = (0.55 * cpr_margin + 0.45 * dop_margin).astype(np.float32)
    out.confidence = np.where(ice_mask, conf, 0.0).astype(np.float32)

    # ── Step 10: OHRC obstacle mask — scatter boulder hazards ────────────────
    obstacle = np.zeros((rows, cols), dtype=bool)

    # Boulder density: ~3.5% of non-shadow, non-steep cells
    flat_mask = (slope <= SLOPE_NOGO_DEG) & (~shadow)
    flat_indices = np.argwhere(flat_mask)
    if len(flat_indices) > 0:
        n_boulders = max(1, int(len(flat_indices) * 0.035))
        chosen = rng.choice(len(flat_indices), size=min(n_boulders, len(flat_indices)), replace=False)
        for idx in chosen:
            r_i, c_i = flat_indices[idx]
            # 3×3 kernel simulates a boulder footprint at 30 m resolution
            for dr in range(-1, 2):
                for dc in range(-1, 2):
                    nr, nc = r_i + dr, c_i + dc
                    if 0 <= nr < rows and 0 <= nc < cols:
                        obstacle[nr, nc] = True

    out.obstacle_mask = obstacle

    return out
