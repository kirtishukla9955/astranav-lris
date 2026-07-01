"""
backend/ice_detector.py
-----------------------
F4.1 — Volumetric Ice Estimation

Converts DFSAR GeoTIFF rasters (HH + HV polarisation bands) into a complete
ice detection result including volumetric estimates.

Science basis (ISRO DFSAR specification — constants are IMMUTABLE):
  Ice candidates: CPR > 1.0 AND DOP < 0.13
  Dielectric constant: ε ≈ 2.5 (dust) → ε ≈ 3.5 (ice mixture)
  Ice volume = depth × cell_area, depth scales linearly with (ε − 2.5)/(3.5 − 2.5) × 5 m
  PSR temperature: ~25 K (−248°C), causes heavy battery drain via heaters
  No-Go: slope > 15° or boulder diameter > 0.5 m

References:
  Spudis et al. 2013 — MINI-RF Chandrayaan-1 CPR signatures
  ISRO DFSAR Algorithm Theoretical Basis Document (public)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Science constants (ISRO-specified — DO NOT change)
# ---------------------------------------------------------------------------

CPR_THRESHOLD: float = 1.0          # Circular Polarization Ratio > 1.0 = ice candidate
DOP_THRESHOLD: float = 0.13         # Degree of Polarization < 0.13 = ice candidate
DIELECTRIC_DUST: float = 2.5        # Pure lunar dust dielectric constant
DIELECTRIC_ICE_MIX: float = 3.5     # Subsurface ice mixture dielectric constant
MAX_DEPTH_M: float = 5.0            # Maximum profiling depth in metres
CELL_RESOLUTION_M: float = 30.0     # Grid cell size in metres (matches TMC-2 DEM)

# ---------------------------------------------------------------------------
# Rasterio — graceful import so tests can run without GeoTIFF files
# ---------------------------------------------------------------------------
try:
    import rasterio  # type: ignore[import]
    from rasterio.transform import rowcol  # type: ignore[import]
    _RASTERIO_OK = True
except ImportError:
    _RASTERIO_OK = False
    logger.warning("rasterio not installed — GeoTIFF I/O disabled (synthetic fallback active)")

# Root-level schemas import (sys.path manipulation for backend/ sub-package)
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_BACKEND_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from schemas import IceLayerData
except ImportError:
    # Fallback dataclass if schemas not accessible
    from dataclasses import dataclass

    @dataclass  # type: ignore[no-redef]
    class IceLayerData:  # type: ignore[no-redef]
        lat: float
        lon: float
        ice_volume_m3: float
        ice_depth_m: float
        confidence: float


# ---------------------------------------------------------------------------
# F4.1 — Core functions
# ---------------------------------------------------------------------------

def load_sar_bands(filepath: str) -> tuple[np.ndarray, np.ndarray]:
    """
    Load DFSAR GeoTIFF and return (HH, HV) as float32 linear-power arrays.

    The GeoTIFF stores values in dB scale (band 1 = HH_dB, band 2 = HV_dB).
    Conversion: linear = 10^(dB / 10)

    Science: CPR > 1.0 AND DOP < 0.13 → ice candidate
             (ISRO DFSAR specification)
    """
    if not _RASTERIO_OK:
        raise ImportError("rasterio is required to load SAR GeoTIFFs. Run: pip install rasterio")

    with rasterio.open(filepath) as src:
        if src.count < 2:
            raise ValueError(f"DFSAR GeoTIFF must have ≥ 2 bands (HH, HV). Found {src.count}.")

        hh_db = src.read(1).astype(np.float32)
        hv_db = src.read(2).astype(np.float32)

    # dB → linear power: linear = 10^(val / 10)
    hh_lin = np.power(10.0, hh_db / 10.0, dtype=np.float32)
    hv_lin = np.power(10.0, hv_db / 10.0, dtype=np.float32)

    # Clip to avoid numerical artifacts from nodata edges
    hh_lin = np.clip(hh_lin, 1e-9, None)
    hv_lin = np.clip(hv_lin, 1e-9, None)

    logger.debug("SAR loaded: shape=%s  HH range=[%.4f, %.4f]  HV range=[%.4f, %.4f]",
                 hh_lin.shape, hh_lin.min(), hh_lin.max(), hv_lin.min(), hv_lin.max())
    return hh_lin, hv_lin


def compute_cpr(hh: np.ndarray, hv: np.ndarray) -> np.ndarray:
    """
    Compute Circular Polarization Ratio (CPR = HV / HH).

    CPR > 1.0 is the ISRO threshold for an ice candidate.
    Division-by-zero guarded with np.where (returns 0 where HH == 0).
    """
    return np.where(hh > 0, hv / hh, 0.0).astype(np.float32)


def compute_dop(hh: np.ndarray, hv: np.ndarray) -> np.ndarray:
    """
    Compute Degree of Polarization (DOP = |HH − HV| / (HH + HV)).

    DOP < 0.13 is the ISRO threshold for an ice candidate.
    Division-by-zero guarded (returns 0 where sum == 0).
    """
    denom = hh + hv
    return np.where(denom > 0, np.abs(hh - hv) / denom, 0.0).astype(np.float32)


def detect_ice_mask(cpr: np.ndarray, dop: np.ndarray) -> np.ndarray:
    """
    Apply ISRO dual-criterion ice detection.

    Returns boolean array: True where CPR > 1.0 AND DOP < 0.13.

    Science rationale:
      - CPR > 1 → enhanced cross-pol backscatter (volume scattering from ice grains)
      - DOP < 0.13 → low polarization degree (depolarising, disordered ice structure)
    """
    return ((cpr > CPR_THRESHOLD) & (dop < DOP_THRESHOLD))


def estimate_dielectric(cpr: np.ndarray, ice_mask: np.ndarray) -> np.ndarray:
    """
    Estimate dielectric constant ε from CPR using linear interpolation.

    Formula (applied only where ice_mask=True):
      ε = 2.5 + (3.5 − 2.5) × clip((CPR − 1.0) / 2.0, 0, 1)

    Physical basis:
      ε = 2.5 → pure lunar regolith (dust)
      ε = 3.5 → water-ice saturated regolith mixture
    """
    cpr_norm = np.clip((cpr - CPR_THRESHOLD) / 2.0, 0.0, 1.0)
    epsilon = DIELECTRIC_DUST + (DIELECTRIC_ICE_MIX - DIELECTRIC_DUST) * cpr_norm
    # Zero out non-ice cells
    return np.where(ice_mask, epsilon, DIELECTRIC_DUST).astype(np.float32)


def estimate_ice_volume(
    ice_mask: np.ndarray,
    epsilon: np.ndarray,
    cell_res_m: float = CELL_RESOLUTION_M,
) -> np.ndarray:
    """
    Estimate volumetric ice content per grid cell (m³).

    Depth model:
      depth_m = MAX_DEPTH_M × (ε − 2.5) / (3.5 − 2.5)

    Volume per cell (m³):
      vol = depth_m × cell_res_m² × ice_mask

    Science: ice depth scales linearly with dielectric excess above dust baseline.
    Max profiling depth = 5.0 m (DFSAR L-band penetration limit).
    """
    depth_m = MAX_DEPTH_M * (epsilon - DIELECTRIC_DUST) / (DIELECTRIC_ICE_MIX - DIELECTRIC_DUST)
    depth_m = np.maximum(depth_m, 0.0)   # no negative depth
    vol = depth_m * cell_res_m * cell_res_m * ice_mask.astype(np.float32)
    return vol.astype(np.float32)


def run_ice_pipeline(sar_filepath: str) -> dict:
    """
    End-to-end ice detection pipeline from a DFSAR GeoTIFF.

    Steps:
      1. Load HH, HV bands (dB → linear power)
      2. Compute CPR = HV / HH
      3. Compute DOP = |HH − HV| / (HH + HV)
      4. Detect ice mask: CPR > 1.0 AND DOP < 0.13
      5. Estimate dielectric constant ε
      6. Estimate ice volume per cell

    Returns
    -------
    dict with keys:
      ice_mask      : np.ndarray (bool)    — True where ice detected
      cpr           : np.ndarray (float32) — Circular Polarization Ratio
      dop           : np.ndarray (float32) — Degree of Polarization
      epsilon       : np.ndarray (float32) — Dielectric constant (2.5–3.5)
      volume_m3     : np.ndarray (float32) — Volume per cell (m³)
      total_volume_m3 : float              — Total estimated ice volume (m³)
      ice_cell_count  : int                — Number of ice-positive cells
      transform       : rasterio.Affine    — Georeference transform (if available)
    """
    if not _RASTERIO_OK or not os.path.exists(sar_filepath):
        logger.warning("SAR file not found or rasterio unavailable (%s) — using synthetic fallback.", sar_filepath)
        return _synthetic_fallback()

    logger.info("Ice pipeline: loading SAR bands from %s", sar_filepath)
    hh, hv = load_sar_bands(sar_filepath)

    cpr = compute_cpr(hh, hv)
    dop = compute_dop(hh, hv)
    ice_mask = detect_ice_mask(cpr, dop)
    epsilon = estimate_dielectric(cpr, ice_mask)
    volume_m3 = estimate_ice_volume(ice_mask, epsilon)

    ice_cell_count = int(ice_mask.sum())
    total_volume = float(volume_m3.sum())

    # Store transform for lat/lon lookups
    transform = None
    if _RASTERIO_OK:
        try:
            with rasterio.open(sar_filepath) as src:
                transform = src.transform
        except Exception:
            pass

    logger.info(
        "Ice pipeline complete: %d ice cells / %d total  total_vol=%.1f m³",
        ice_cell_count, cpr.size, total_volume
    )

    return {
        "ice_mask": ice_mask,
        "cpr": cpr,
        "dop": dop,
        "epsilon": epsilon,
        "volume_m3": volume_m3,
        "total_volume_m3": total_volume,
        "ice_cell_count": ice_cell_count,
        "transform": transform,
    }


def _synthetic_fallback() -> dict:
    """
    Generate a minimal synthetic ice pipeline result when no GeoTIFF is available.
    Used for unit tests and cold-start scenarios.

    Embeds two crater-interior ice zones matching the science thresholds:
      CPR > 1.0 AND DOP < 0.13
    """
    rows, cols = 128, 128
    rng = np.random.default_rng(42)

    # Background (non-ice): low CPR, high DOP
    hh = np.full((rows, cols), 0.30, dtype=np.float32)
    hv = np.full((rows, cols), 0.05, dtype=np.float32)
    hh += (rng.standard_normal((rows, cols)) * 0.02).astype(np.float32)
    hv += (rng.standard_normal((rows, cols)) * 0.005).astype(np.float32)

    # Ice zones: two circular patches
    yy, xx = np.mgrid[0:rows, 0:cols].astype(np.float32)
    for (cy, cx, r0) in [(38, 45, 14), (90, 82, 10)]:
        r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
        ice = r < r0
        strength = np.clip(1.0 - r / r0, 0, 1).astype(np.float32)
        hv = np.where(ice, hv + strength * 0.40, hv)
        hh = np.where(ice, hh + strength * 0.08, hh)

    hh = np.clip(hh, 1e-9, None).astype(np.float32)
    hv = np.clip(hv, 1e-9, None).astype(np.float32)

    cpr = compute_cpr(hh, hv)
    dop = compute_dop(hh, hv)
    ice_mask = detect_ice_mask(cpr, dop)
    epsilon = estimate_dielectric(cpr, ice_mask)
    volume_m3 = estimate_ice_volume(ice_mask, epsilon)

    return {
        "ice_mask": ice_mask,
        "cpr": cpr,
        "dop": dop,
        "epsilon": epsilon,
        "volume_m3": volume_m3,
        "total_volume_m3": float(volume_m3.sum()),
        "ice_cell_count": int(ice_mask.sum()),
        "transform": None,
    }


def get_point_ice_data(
    lat: float,
    lon: float,
    pipeline_result: dict,
    transform: Any = None,
) -> "IceLayerData":
    """
    Extract IceLayerData for a specific lat/lon coordinate.

    Converts the lat/lon to pixel indices using the rasterio Affine transform.
    Falls back to array-centre values if transform is unavailable.

    Returns an IceLayerData schema object for use in LMRS scoring.
    """
    ice_mask: np.ndarray = pipeline_result["ice_mask"]
    volume_m3: np.ndarray = pipeline_result["volume_m3"]
    epsilon: np.ndarray = pipeline_result["epsilon"]
    cpr: np.ndarray = pipeline_result["cpr"]
    dop: np.ndarray = pipeline_result["dop"]
    rows, cols = ice_mask.shape

    # Convert lat/lon → pixel row/col
    row, col = rows // 2, cols // 2  # default: centre
    if transform is not None and _RASTERIO_OK:
        try:
            r, c = rowcol(transform, lon, lat)
            row = int(np.clip(r, 0, rows - 1))
            col = int(np.clip(c, 0, cols - 1))
        except Exception as exc:
            logger.debug("rowcol conversion failed (%s) — using array centre", exc)

    # Science values at the query cell
    is_ice = bool(ice_mask[row, col])
    vol = float(volume_m3[row, col])
    eps = float(epsilon[row, col])
    depth = float(MAX_DEPTH_M * (eps - DIELECTRIC_DUST) / (DIELECTRIC_ICE_MIX - DIELECTRIC_DUST))
    depth = max(0.0, min(depth, MAX_DEPTH_M))

    # Confidence: higher CPR margin + lower DOP → more confident
    cpr_val = float(cpr[row, col])
    dop_val = float(dop[row, col])
    cpr_conf = np.clip((cpr_val - CPR_THRESHOLD) / 1.5, 0.0, 1.0)
    dop_conf = np.clip((DOP_THRESHOLD - dop_val) / DOP_THRESHOLD, 0.0, 1.0)
    raw_conf = float(0.55 * cpr_conf + 0.45 * dop_conf) if is_ice else 0.1
    confidence = float(np.clip(raw_conf, 0.05, 0.97))

    return IceLayerData(
        lat=lat,
        lon=lon,
        ice_volume_m3=vol if is_ice else 0.0,
        ice_depth_m=depth if is_ice else 0.0,
        confidence=confidence,
    )
