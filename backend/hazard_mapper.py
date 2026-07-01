"""
backend/hazard_mapper.py
------------------------
F4.2 — Terrain Hazard Masking  |  F4.3 — Shadow-Aware Pathfinder Integration

Converts TMC-2 DEM (GeoTIFF) and OHRC optical imagery (GeoTIFF) into a fully
populated CostGrid that the A* pathfinder consumes.

Science basis (ISRO-specified constants — IMMUTABLE):
  No-Go: slope > 15° or boulder diameter > 0.5 m
  PSR (permanently shadowed regions): lat < -85° -> always shadowed
  Sun geometry at south pole: elevation ~0-2°, highly oblique -> long shadow rays

OHRC capabilities:
  Resolution: 0.32 m/pixel @ 100 km altitude
  -> 0.5 m boulder = ~1.6 pixels (detectable with connected components)
  -> Crater rim bright ring + dark floor = detectable with LoG + pattern matching
  -> Orthrorectified GeoTIFF from PRADAN: single panchromatic band, uint16 or uint8

Pipeline:
  1. Load DEM -> compute slope via np.gradient
  2. Load OHRC optical -> detect boulders (bright blobs) + craters (dark bowls)
  3. Ray-cast shadow map from low-angle sun geometry
  4. Build CostGrid: apply hazard masks + shadow map

References:
  TMC-2 DEM resolution: 30 m/pixel (Chandrayaan-2)
  OHRC resolution: 0.32 m/pixel (boulder detection threshold 0.5 m)
  Mahanti et al. 2014 — Lunar boulder detection from orbital imagery
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, List, Optional

import numpy as np
from scipy import ndimage  # type: ignore[import]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Science constants (ISRO-specified — DO NOT change)
# ---------------------------------------------------------------------------
SLOPE_NOGO_DEG: float = 15.0        # No-Go slope threshold (degrees)
BOULDER_NOGO_M: float = 0.5         # Boulder diameter threshold (metres)
CELL_RESOLUTION_M: float = 30.0     # DEM cell size (metres)
PSR_LAT_THRESHOLD: float = -85.0    # Latitude below which PSR is forced TRUE

# OHRC instrument constants (Chandrayaan-2 specification)
OHRC_PIXEL_RES_M: float = 0.32      # Native OHRC resolution (metres/pixel)
OHRC_MIN_BOULDER_PX: int = 2        # Min pixels for a 0.5m boulder at 0.32m/px (ceil(0.5/0.32)=2)
# Crater size range detectable from OHRC (metres)
CRATER_MIN_DIAM_M: float = 5.0      # Smallest crater to flag (nav hazard)
CRATER_MAX_DIAM_M: float = 500.0    # Largest crater to map (landform)

# ---------------------------------------------------------------------------
# Rasterio — graceful import
# ---------------------------------------------------------------------------
try:
    import rasterio  # type: ignore[import]
    _RASTERIO_OK = True
except ImportError:
    _RASTERIO_OK = False
    logger.warning("rasterio not installed — GeoTIFF I/O disabled (synthetic fallback active)")

# Root-level imports (sys.path manipulation for backend/ sub-package)
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_BACKEND_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from schemas import HazardMask  # noqa: E402  (after sys.path setup)
from cost_grid import CostGrid  # noqa: E402


# ---------------------------------------------------------------------------
# F4.2 — Functions
# ---------------------------------------------------------------------------

def load_dem(filepath: str) -> tuple[np.ndarray, Any]:
    """
    Load a TMC-2 DEM GeoTIFF (single-band float32, metres).

    Returns
    -------
    (elevation_m : np.ndarray float32,  transform : rasterio.Affine | None)
    """
    if not _RASTERIO_OK or not os.path.exists(filepath):
        logger.warning("DEM not found / rasterio unavailable (%s) — using synthetic fallback", filepath)
        return _synthetic_dem_fallback()

    with rasterio.open(filepath) as src:
        dem = src.read(1).astype(np.float32)
        transform = src.transform

    logger.debug("DEM loaded: shape=%s  min=%.1f m  max=%.1f m", dem.shape, dem.min(), dem.max())
    return dem, transform


def _synthetic_dem_fallback(rows: int = 128, cols: int = 128) -> tuple[np.ndarray, None]:
    """Generate a Gaussian crater-bowl DEM when no real file is available."""
    rng = np.random.default_rng(42)
    yy, xx = np.mgrid[0:rows, 0:cols].astype(np.float32)
    dem = np.zeros((rows, cols), dtype=np.float32)
    for (cy, cx, r0, depth) in [(38, 45, 14, 1800), (90, 82, 10, 1200)]:
        r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
        dem -= depth * np.exp(-(r / r0) ** 2)
    dem += (rng.standard_normal((rows, cols)) * 15).astype(np.float32)
    return dem, None


def compute_slope(dem: np.ndarray, resolution_m: float = CELL_RESOLUTION_M) -> np.ndarray:
    """
    Compute terrain slope in degrees from a DEM using finite differences.

    Formula:
      dz/dx, dz/dy via np.gradient
      slope_deg = arctan(sqrt((dz/dx)² + (dz/dy)²))

    Science: slope > 15° = No-Go zone for Pragyan-class rovers.
    """
    dzdx = np.gradient(dem, resolution_m, axis=1)
    dzdy = np.gradient(dem, resolution_m, axis=0)
    slope_deg = np.degrees(np.arctan(np.sqrt(dzdx ** 2 + dzdy ** 2)))
    return slope_deg.astype(np.float32)


def detect_boulders_from_optical(
    optical_filepath: str,
    min_diameter_m: float = BOULDER_NOGO_M,
) -> np.ndarray:
    """
    Detect boulders from CH2_OHRC imagery using local-contrast thresholding.

    Why local contrast instead of global threshold:
      OHRC panchromatic images have strong albedo variation across a scene
      (shadowed crater floors ~15 DN vs sunlit rims ~200 DN). A global
      threshold confuses shadows for non-boulders. Local contrast isolates
      compact bright anomalies (boulders) regardless of background.

    Algorithm:
      1. Load OHRC GeoTIFF (single panchromatic band, uint8 or uint16)
      2. Local contrast: pixel - Gaussian_blur(pixel, sigma=10px)
         -> boulders appear as sharp bright peaks; terrain is smooth
      3. Threshold at local_contrast > mean(contrast) + 1.5*std(contrast)
      4. Connected-component labelling (scipy.ndimage.label)
      5. Flag any component whose bounding-box >= min_diameter_m / pixel_res

    OHRC physics:
      Resolution = 0.32 m/px -> 0.5 m boulder = 1.6 px -> detected at >= 2 px blob
      Boulders appear as isolated bright specks with sharp edges in oblique sun.

    Returns
    -------
    obstacle_mask : np.ndarray (bool), True = boulder >= 0.5 m detected
    """
    if not _RASTERIO_OK or not os.path.exists(optical_filepath):
        logger.warning("Optical file not found (%s) — no boulder detection", optical_filepath)
        return _synthetic_boulder_fallback()

    with rasterio.open(optical_filepath) as src:
        img = src.read(1).astype(np.float32)
        pixel_res_m = abs(src.transform[0])   # metres per pixel from GeoTransform
        if pixel_res_m <= 0:
            pixel_res_m = OHRC_PIXEL_RES_M

    # Normalise to 0-1 for consistent thresholding
    img_norm = (img - img.min()) / (img.max() - img.min() + 1e-6)

    # Local contrast: remove smooth background with Gaussian blur
    blur_sigma = max(3.0, 10.0 * OHRC_PIXEL_RES_M / pixel_res_m)
    background = ndimage.gaussian_filter(img_norm, sigma=blur_sigma)
    local_contrast = img_norm - background

    # Threshold on local contrast
    lc_mean = local_contrast.mean()
    lc_std  = local_contrast.std()
    bright_local = local_contrast > (lc_mean + 1.5 * lc_std)

    # Connected-component labelling
    labeled, n_features = ndimage.label(bright_local)
    min_pixels = max(OHRC_MIN_BOULDER_PX, min_diameter_m / pixel_res_m)

    obstacle = np.zeros(img.shape, dtype=bool)
    for label_id in range(1, n_features + 1):
        component = labeled == label_id
        rows_idx, cols_idx = np.where(component)
        if len(rows_idx) == 0:
            continue
        height_px = rows_idx.max() - rows_idx.min() + 1
        width_px  = cols_idx.max() - cols_idx.min() + 1
        # Flag if any bounding-box dimension >= min boulder size
        if height_px >= min_pixels or width_px >= min_pixels:
            obstacle |= component

    n_boulders = int(ndimage.label(obstacle)[1])
    logger.info(
        "Boulder detection (OHRC %.2fm/px): %d boulders >= %.1fm, %d pixels flagged",
        pixel_res_m, n_boulders, min_diameter_m, int(obstacle.sum())
    )
    return obstacle


def detect_craters_from_optical(
    optical_filepath: str,
    min_diam_m: float = CRATER_MIN_DIAM_M,
    max_diam_m: float = CRATER_MAX_DIAM_M,
) -> tuple[np.ndarray, list]:
    """
    Map craters from CH2_OHRC imagery using Laplacian-of-Gaussian blob detection.

    Crater signature in OHRC panchromatic:
      - Dark circular floor (low albedo, shadowed)
      - Bright annular rim (ejecta + sun-facing inner wall)
      - Pattern: ring of local maxima around a local minimum

    Algorithm:
      1. Compute Laplacian (second derivative) of the smoothed image
         Negative LoG response = dark blob = crater floor candidate
      2. Multi-scale detection: test scales matching crater diameters 5-500m
      3. Local minimum finding -> crater centre candidates
      4. Rim verification: check that a bright annulus exists around each candidate
      5. Return binary map + list of crater dicts {cx, cy, radius_m, confidence}

    Science:
      Craters > 5m diameter are navigation hazards (rover cannot cross the rim).
      Craters of all sizes are ice-candidate sites (PSR interiors).

    Parameters
    ----------
    optical_filepath : str  -- path to OHRC GeoTIFF
    min_diam_m       : float -- smallest crater to detect (metres)
    max_diam_m       : float -- largest crater to detect (metres)

    Returns
    -------
    crater_mask : np.ndarray (bool)  -- True at every detected crater pixel
    craters     : list[dict]         -- [{cx, cy, radius_px, radius_m, confidence}]
    """
    if not _RASTERIO_OK or not os.path.exists(optical_filepath):
        logger.warning("Optical file not found (%s) — no crater detection", optical_filepath)
        return np.zeros((128, 128), dtype=bool), []

    with rasterio.open(optical_filepath) as src:
        img = src.read(1).astype(np.float32)
        pixel_res_m = abs(src.transform[0])
        if pixel_res_m <= 0:
            pixel_res_m = OHRC_PIXEL_RES_M

    rows, cols = img.shape

    # Normalise
    img_norm = (img - img.min()) / (img.max() - img.min() + 1e-6)

    # ── Multi-scale LoG blob detection ───────────────────────────────────────
    # Each scale sigma corresponds to a crater radius:
    #   sigma = radius_px / sqrt(2)
    min_r_px = max(2, (min_diam_m / 2.0) / pixel_res_m)
    max_r_px = min(min(rows, cols) // 4, (max_diam_m / 2.0) / pixel_res_m)

    # Build log-spaced sigma values
    n_scales = max(3, int(np.log2(max_r_px / min_r_px) * 4))
    sigmas = np.logspace(
        np.log10(min_r_px / np.sqrt(2)),
        np.log10(max_r_px / np.sqrt(2)),
        n_scales
    )

    # LoG response stack (sigma-normalised: multiply by sigma^2)
    log_stack = np.zeros((n_scales, rows, cols), dtype=np.float32)
    for i, sigma in enumerate(sigmas):
        smoothed = ndimage.gaussian_filter(img_norm, sigma=sigma)
        lap = ndimage.laplace(smoothed)
        log_stack[i] = -(lap * sigma ** 2)   # negative = dark blob

    # Max projection across scales
    log_max = log_stack.max(axis=0)
    best_scale_idx = log_stack.argmax(axis=0)

    # ── Find local maxima in LoG response ────────────────────────────────────
    response_thresh = log_max.mean() + 1.5 * log_max.std()
    candidate_mask = log_max > response_thresh

    # Dilate + label to find peak centres
    labeled, n_cands = ndimage.label(candidate_mask)

    craters = []
    crater_mask = np.zeros((rows, cols), dtype=bool)

    for cid in range(1, n_cands + 1):
        comp = labeled == cid
        ys, xs = np.where(comp)
        if len(ys) == 0:
            continue

        # Weighted centroid by LoG response
        weights = log_max[ys, xs]
        cy = int(np.round(np.average(ys, weights=weights)))
        cx = int(np.round(np.average(xs, weights=weights)))

        # Radius from best scale index at centroid
        sc_idx = int(best_scale_idx[cy, cx])
        sigma_px = float(sigmas[sc_idx])
        radius_px = sigma_px * np.sqrt(2)
        radius_m  = radius_px * pixel_res_m

        if radius_m < min_diam_m / 2.0 or radius_m > max_diam_m / 2.0:
            continue

        # ── Rim verification: bright annulus check ────────────────────────────
        r_inner = max(1, int(radius_px * 0.8))
        r_outer = max(2, int(radius_px * 1.3))
        yy, xx = np.ogrid[-cy:rows - cy, -cx:cols - cx]
        dist2   = yy ** 2 + xx ** 2
        rim_ring  = (dist2 >= r_inner ** 2) & (dist2 <= r_outer ** 2)
        floor_circ = dist2 < r_inner ** 2

        if not rim_ring.any() or not floor_circ.any():
            continue

        rim_brightness   = float(img_norm[rim_ring].mean())
        floor_brightness = float(img_norm[floor_circ].mean())
        contrast_ratio = rim_brightness / (floor_brightness + 0.01)

        # Accept if rim is brighter than floor (crater pattern)
        if contrast_ratio < 1.05:
            continue

        confidence = float(np.clip((contrast_ratio - 1.0) * 2.0, 0.1, 1.0))

        # Add to mask (floor + rim)
        full_crater = dist2 <= r_outer ** 2
        crater_mask |= full_crater

        craters.append({
            "cx": cx,
            "cy": cy,
            "radius_px": float(radius_px),
            "radius_m":  float(radius_m),
            "diameter_m": float(radius_m * 2),
            "confidence": confidence,
            "rim_brightness": rim_brightness,
            "floor_brightness": floor_brightness,
        })

    logger.info(
        "Crater detection (OHRC %.2fm/px): %d craters found, diam range %.1f-%.1f m",
        pixel_res_m,
        len(craters),
        min((c["diameter_m"] for c in craters), default=0),
        max((c["diameter_m"] for c in craters), default=0),
    )
    return crater_mask, craters


def _synthetic_boulder_fallback(rows: int = 128, cols: int = 128) -> np.ndarray:
    """Scatter synthetic boulder patches when no optical file is available."""
    rng = np.random.default_rng(77)
    obstacle = np.zeros((rows, cols), dtype=bool)
    yy, xx = np.mgrid[0:rows, 0:cols]
    # Place ~3% as small boulder footprints
    for _ in range(int(rows * cols * 0.03 / 9)):
        cy = rng.integers(1, rows - 1)
        cx = rng.integers(1, cols - 1)
        for dr in range(-1, 2):
            for dc in range(-1, 2):
                obstacle[cy + dr, cx + dc] = True
    return obstacle


def build_hazard_masks(
    slope: np.ndarray,
    boulder_mask: np.ndarray,
) -> List[HazardMask]:
    """
    Build a list of HazardMask objects for every grid cell.

    No-Go rules (ISRO-specified):
      is_obstacle = True  if slope > 15° OR boulder_mask == True

    Returns
    -------
    List[HazardMask] — one entry per cell, in row-major order
    """
    rows, cols = slope.shape
    masks: List[HazardMask] = []
    for y in range(rows):
        for x in range(cols):
            s = float(slope[y, x])
            is_obs = bool(s > SLOPE_NOGO_DEG or boulder_mask[y, x])
            masks.append(HazardMask(
                lat=float(y) * 0.0001,
                lon=float(x) * 0.0001,
                slope_deg=s,
                is_obstacle=is_obs,
            ))
    return masks


def load_shadow_map(
    dem: np.ndarray,
    sun_azimuth_deg: float = 180.0,
    sun_elevation_deg: float = 2.0,
) -> np.ndarray:
    """
    Compute topographic shadow map via horizon-angle ray casting.

    Algorithm
    ---------
    For each cell, cast a ray in the direction of the sun (azimuth).
    If any terrain along that ray rises above the sun's elevation angle
    as seen from the query cell, the cell is in shadow.

    PSR forcing: cells near the south pole (< −85° lat, approximated by
    the lower 25% of the DEM in south-polar geometry) are forced into
    permanent shadow — they never see the sun regardless of topography.

    Science:
      South-pole sun elevation ≈ 0–2° (near-horizon illumination)
      Permanently Shadowed Regions (PSR) ≈ 25 K (−248°C)

    Parameters
    ----------
    dem            : elevation array (metres)
    sun_azimuth_deg: azimuth of sun from north (0°=N, 90°=E, 180°=S)
    sun_elevation_deg: solar elevation angle above horizon (degrees)

    Returns
    -------
    shadow_mask : np.ndarray (bool) — True = in shadow
    """
    rows, cols = dem.shape
    shadow = np.zeros((rows, cols), dtype=bool)

    sun_elev_rad = np.radians(sun_elevation_deg)
    # Direction vector in grid coords (row_step, col_step)
    az_rad = np.radians(sun_azimuth_deg)
    dr = -np.cos(az_rad)   # row direction (north = -row)
    dc =  np.sin(az_rad)   # col direction (east = +col)

    # Normalise to unit step
    step_len = max(abs(dr), abs(dc)) if max(abs(dr), abs(dc)) > 0 else 1.0
    dr /= step_len
    dc /= step_len
    ds_m = CELL_RESOLUTION_M * step_len   # physical step distance in metres

    for y in range(rows):
        for x in range(cols):
            base_elev = dem[y, x]
            r, c = float(y) + dr, float(x) + dc
            dist_m = ds_m
            in_shadow = False

            while 0 <= int(round(r)) < rows and 0 <= int(round(c)) < cols:
                ri, ci = int(round(r)), int(round(c))
                terrain_elev = dem[ri, ci]
                # Angle from query cell to this terrain point
                dz = terrain_elev - base_elev
                terrain_angle = np.arctan2(dz, dist_m)
                if terrain_angle > sun_elev_rad:
                    in_shadow = True
                    break
                r += dr
                c += dc
                dist_m += ds_m

            shadow[y, x] = in_shadow

    # Force PSR: lower 25% of grid rows (south-pole geometry approximation)
    psr_row_cutoff = int(rows * 0.25)
    shadow[:psr_row_cutoff, :] = True

    logger.debug(
        "Shadow map: %.1f%% in shadow  (PSR forced + topographic)",
        100.0 * shadow.mean()
    )
    return shadow


def _fast_shadow_map(dem: np.ndarray) -> np.ndarray:
    """
    Fast approximate shadow map using crater-bowl geometry (production fallback).

    For large grids (>256×256), the per-pixel ray cast above is slow.
    This version uses ndimage-based local minimum comparison as a proxy.
    """
    rows, cols = dem.shape
    shadow = np.zeros((rows, cols), dtype=bool)

    # Cells below their neighbourhood minimum-by-a-margin are likely in crater shadow
    local_max = ndimage.maximum_filter(dem, size=11)
    shadow = (dem < local_max - 200)   # 200 m below local peak → shadowed

    # PSR: force lower 25% of rows
    shadow[:rows // 4, :] = True
    return shadow


def build_real_cost_grid(
    dem_path: str,
    optical_path: Optional[str] = None,
) -> CostGrid:
    """
    Build a fully physics-calibrated CostGrid from real (or synthetic) geodata.

    Replaces generate_mock_cost_grid() in main.py.

    Steps
    -----
    1. Load DEM (GeoTIFF or synthetic fallback)
    2. Compute slope array
    3. Detect boulders from OHRC optical (if path given)
    4. Compute shadow map (topographic ray-cast, PSR forcing)
    5. Create CostGrid, apply HazardMask per cell, apply shadow map

    Science:
      No-Go:  slope > 15° or boulder > 0.5 m  → traversal_cost = ∞
      Shadow: crater PSR ≈ 25 K → heavy battery penalty (handled in pathfinder)
    """
    logger.info("Building real cost grid from DEM=%s  optical=%s", dem_path, optical_path)

    # 1. Load DEM
    dem, _ = load_dem(dem_path)
    rows, cols = dem.shape

    # 2. Slope
    slope = compute_slope(dem)

    # 3. Boulder mask
    if optical_path:
        boulder_mask = detect_boulders_from_optical(optical_path)
        # Resize boulder mask to match DEM if resolutions differ
        if boulder_mask.shape != dem.shape:
            boulder_mask = _resize_mask(boulder_mask, rows, cols)
    else:
        boulder_mask = np.zeros((rows, cols), dtype=bool)

    # 4. Shadow map (use fast approximation for large grids)
    if rows * cols > 64 * 64:
        logger.info("Grid > 64×64 — using fast shadow approximation")
        shadow = _fast_shadow_map(dem)
    else:
        shadow = load_shadow_map(dem)

    # 5. Build CostGrid
    grid = CostGrid(width=cols, height=rows, resolution_m=CELL_RESOLUTION_M)

    # Apply hazard masks (vectorised loop — no nested Python loop over cells)
    slope_nogo = slope > SLOPE_NOGO_DEG
    combined_nogo = slope_nogo | boulder_mask

    # Apply directly to the numpy grid arrays (faster than cell-by-cell)
    grid.grid[combined_nogo] = np.inf
    # For passable cells, add slope penalty: cost = 1 + slope² / 100
    passable = ~combined_nogo
    grid.grid[passable] = 1.0 + (slope[passable] ** 2) / 100.0

    # Apply shadow map
    grid.apply_shadow_map(shadow)

    nogo_pct = 100.0 * combined_nogo.sum() / combined_nogo.size
    shadow_pct = 100.0 * shadow.sum() / shadow.size
    logger.info(
        "CostGrid built: %d×%d  no-go=%.1f%%  shadow=%.1f%%",
        cols, rows, nogo_pct, shadow_pct
    )
    return grid


def _resize_mask(mask: np.ndarray, target_rows: int, target_cols: int) -> np.ndarray:
    """Resize a boolean mask to target shape using nearest-neighbour sampling."""
    from scipy.ndimage import zoom  # type: ignore[import]
    zy = target_rows / mask.shape[0]
    zx = target_cols / mask.shape[1]
    resized = zoom(mask.astype(np.float32), (zy, zx), order=0)
    return resized > 0.5
