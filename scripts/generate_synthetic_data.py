"""
scripts/generate_synthetic_data.py
------------------------------------
Generates scientifically-calibrated synthetic GeoTIFF rasters for the
AstraNav-LRIS pipeline when real ISRO Chandrayaan-2 data is unavailable.

Outputs (written to backend/data/):
  sar/synthetic_dfsar.tif   — 2-band GeoTIFF (HH, HV) in dB scale
  dem/synthetic_dem.tif     — 1-band DEM float32, metres
  optical/synthetic_ohrc.tif — 1-band optical reflectance uint8

Science basis
-------------
  Crater bowl DEM : z = -depth * exp(-(r/r0)^2)   (Gaussian bowl)
  SAR ice zone    : HV/HH > 1.0 inside crater interior (CPR > 1.0)
  DOP             : |HH-HV|/(HH+HV) < 0.13 in ice zone
  Temp range      : ~25 K (PSR floor) → ~380 K (sunlit rim)
  No-Go           : slope > 15°, boulders > 0.5 m

ISRO science constants (immutable):
  CPR_THRESHOLD = 1.0   (Circular Polarization Ratio > 1.0 = ice candidate)
  DOP_THRESHOLD = 0.13  (Degree of Polarization < 0.13 = ice candidate)
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np

# ---------------------------------------------------------------------------
# Rasterio — graceful import with helpful error
# ---------------------------------------------------------------------------
try:
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.crs import CRS
    _RASTERIO_OK = True
except ImportError:
    _RASTERIO_OK = False

# ---------------------------------------------------------------------------
# Path setup — run from any directory
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_SCRIPT_DIR)
_DATA_DIR = os.path.join(_ROOT, "backend", "data")

# Science constants (ISRO-specified — DO NOT change)
CPR_THRESHOLD: float = 1.0
DOP_THRESHOLD: float = 0.13
CELL_SIZE_M: float = 30.0           # TMC-2 posting interval
ROWS: int = 128
COLS: int = 128

# South-polar bounding box (Shackleton crater approximate)
ORIGIN_LAT: float = -89.60
ORIGIN_LON: float = 44.00

# IAU Moon spheroid (radius 1,737,400 m)
LUNAR_RADIUS_M: float = 1_737_400.0
DEG_LAT_M: float = (math.pi / 180.0) * LUNAR_RADIUS_M  # ≈ 30,328 m/°


def _ensure_dirs() -> None:
    for sub in ("sar", "dem", "optical", "thermal"):
        os.makedirs(os.path.join(_DATA_DIR, sub), exist_ok=True)


def _moon_transform(rows: int, cols: int, cell_size_m: float,
                    origin_lat: float, origin_lon: float) -> "rasterio.transform.Affine":
    """Build an Affine transform mapping pixels to lunar lat/lon."""
    cos_lat = math.cos(math.radians(abs(origin_lat)))
    dlat = cell_size_m / DEG_LAT_M
    dlon = cell_size_m / (DEG_LAT_M * cos_lat) if cos_lat > 1e-9 else dlat

    lat_max = origin_lat + rows * dlat
    lon_max = origin_lon + cols * dlon
    return from_bounds(origin_lon, origin_lat, lon_max, lat_max, cols, rows)


def _moon_crs() -> "rasterio.crs.CRS":
    """Custom WKT for IAU Moon 2000 geographic CRS."""
    try:
        return CRS.from_epsg(104903)   # ESRI Moon 2000 (if available)
    except Exception:
        return CRS.from_proj4("+proj=latlong +a=1737400 +b=1737400 +no_defs")


# ---------------------------------------------------------------------------
# Core raster generation
# ---------------------------------------------------------------------------

def _build_synthetic_arrays(
    rows: int = ROWS,
    cols: int = COLS,
    seed: int = 42,
) -> dict:
    """
    Generate all synthetic raster arrays.

    Returns dict with keys:
      dem_m, hh_db, hv_db, cpr, dop, optical_refl, slope_deg, shadow_mask
    """
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:rows, 0:cols].astype(np.float32)

    # ── DEM (Gaussian crater bowls) ──────────────────────────────────────────
    dem = np.zeros((rows, cols), dtype=np.float32)

    craters = [
        # (centre_row_frac, centre_col_frac, radius_frac, depth_m)
        (0.30, 0.35, 0.10, 1800.0),   # large crater — main ice target
        (0.70, 0.65, 0.08, 1200.0),   # secondary crater
        (0.50, 0.80, 0.06,  900.0),   # small crater
    ]
    crater_params = []
    for (cy_f, cx_f, r_f, depth) in craters:
        cy = cy_f * rows
        cx = cx_f * cols
        r0 = r_f * min(rows, cols)
        crater_params.append((cy, cx, r0, depth))
        r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
        dem -= depth * np.exp(-(r / r0) ** 2)

    # Gentle background terrain noise
    dem += (rng.standard_normal((rows, cols)) * 15).astype(np.float32)

    # ── Slope from DEM ───────────────────────────────────────────────────────
    dzdx = np.gradient(dem, CELL_SIZE_M, axis=1)
    dzdy = np.gradient(dem, CELL_SIZE_M, axis=0)
    slope_deg = np.degrees(np.arctan(np.sqrt(dzdx ** 2 + dzdy ** 2))).astype(np.float32)

    # ── Shadow mask (crater interiors = permanently shadowed) ─────────────────
    shadow = np.zeros((rows, cols), dtype=bool)
    for (cy, cx, r0, _) in crater_params:
        r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
        shadow |= (r < r0 * 0.75)

    # ── SAR bands (linear power, then convert to dB) ──────────────────────────
    hh_lin = np.full((rows, cols), 0.30, dtype=np.float32)
    hv_lin = np.full((rows, cols), 0.05, dtype=np.float32)

    # Speckle noise
    hh_lin += (rng.standard_normal((rows, cols)) * 0.04).astype(np.float32)
    hv_lin += (rng.standard_normal((rows, cols)) * 0.008).astype(np.float32)

    # Ice signatures: CPR > 1.0 and DOP < 0.13 inside crater ice zones
    for (cy, cx, r0, _) in crater_params:
        r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
        ice_zone = r < r0 * 0.60
        ice_strength = np.clip(1.0 - r / (r0 * 0.60), 0, 1).astype(np.float32)
        ice_strength[~ice_zone] = 0.0

        hv_boost = ice_strength * rng.uniform(0.30, 0.55, (rows, cols)).astype(np.float32)
        hh_boost = ice_strength * rng.uniform(0.05, 0.12, (rows, cols)).astype(np.float32)

        hv_lin = np.where(ice_zone, hv_lin + hv_boost, hv_lin)
        hh_lin = np.where(ice_zone, hh_lin + hh_boost, hh_lin)

    hh_lin = np.clip(hh_lin, 1e-6, None).astype(np.float32)
    hv_lin = np.clip(hv_lin, 1e-6, None).astype(np.float32)

    # Convert to dB (rasterio file stores dB; ice_detector.py converts back)
    hh_db = (10.0 * np.log10(hh_lin)).astype(np.float32)
    hv_db = (10.0 * np.log10(hv_lin)).astype(np.float32)

    # Debug arrays (verify ICE logic is embedded correctly)
    cpr = (hv_lin / hh_lin).astype(np.float32)
    dop = (np.abs(hh_lin - hv_lin) / (hh_lin + hv_lin)).astype(np.float32)

    # ── Optical (OHRC) — simulated reflectance ────────────────────────────────
    # Bright terrain (sunlit) = high reflectance; shadowed = low
    optical = np.where(shadow, 15, 180).astype(np.float32)
    # Add rim glint
    for (cy, cx, r0, _) in crater_params:
        r = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
        rim_mask = (r > r0 * 0.75) & (r < r0 * 1.05)
        optical = np.where(rim_mask, np.clip(optical + 60, 0, 255), optical)
    # Scatter boulder-like blobs (bright anomalies)
    flat_mask = (slope_deg <= 15.0) & (~shadow)
    flat_indices = np.argwhere(flat_mask)
    n_boulders = max(1, int(len(flat_indices) * 0.025))
    chosen = rng.choice(len(flat_indices), size=min(n_boulders, len(flat_indices)), replace=False)
    for idx in chosen:
        ri, ci = flat_indices[idx]
        for dr in range(-1, 2):
            for dc in range(-1, 2):
                nr, nc = ri + dr, ci + dc
                if 0 <= nr < rows and 0 <= nc < cols:
                    optical[nr, nc] = min(255, float(optical[nr, nc]) + 70)
    optical += rng.standard_normal((rows, cols)).astype(np.float32) * 5
    optical = np.clip(optical, 0, 255).astype(np.uint8)

    return {
        "dem_m": dem,
        "hh_db": hh_db,
        "hv_db": hv_db,
        "cpr": cpr,
        "dop": dop,
        "slope_deg": slope_deg,
        "shadow_mask": shadow,
        "optical_refl": optical,
    }


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def write_dfsar(arrays: dict, out_path: str, transform, crs) -> None:
    """Write 2-band (HH, HV) DFSAR GeoTIFF in dB scale."""
    with rasterio.open(
        out_path, "w",
        driver="GTiff",
        height=arrays["hh_db"].shape[0],
        width=arrays["hh_db"].shape[1],
        count=2,
        dtype="float32",
        crs=crs,
        transform=transform,
        compress="lzw",
    ) as dst:
        dst.write(arrays["hh_db"], 1)
        dst.write(arrays["hv_db"], 2)
        dst.update_tags(
            band1="HH_dB",
            band2="HV_dB",
            description="Synthetic DFSAR — CPR/DOP ice detection",
            cpr_threshold=str(CPR_THRESHOLD),
            dop_threshold=str(DOP_THRESHOLD),
            instrument="DFSAR_synthetic",
            mission="Chandrayaan-2_simulated",
        )
    print(f"  [DFSAR] Written: {out_path}")


def write_dem(arrays: dict, out_path: str, transform, crs) -> None:
    """Write 1-band DEM GeoTIFF (float32, metres)."""
    with rasterio.open(
        out_path, "w",
        driver="GTiff",
        height=arrays["dem_m"].shape[0],
        width=arrays["dem_m"].shape[1],
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=-9999.0,
        compress="lzw",
    ) as dst:
        dst.write(arrays["dem_m"], 1)
        dst.update_tags(
            description="Synthetic TMC-2 DEM — Gaussian crater bowl topography",
            units="metres",
            instrument="TMC2_synthetic",
            mission="Chandrayaan-2_simulated",
            resolution_m=str(CELL_SIZE_M),
        )
    print(f"  [DEM]   Written: {out_path}")


def write_optical(arrays: dict, out_path: str, transform, crs) -> None:
    """Write 1-band OHRC optical GeoTIFF (uint8 reflectance 0-255)."""
    with rasterio.open(
        out_path, "w",
        driver="GTiff",
        height=arrays["optical_refl"].shape[0],
        width=arrays["optical_refl"].shape[1],
        count=1,
        dtype="uint8",
        crs=crs,
        transform=transform,
        compress="lzw",
    ) as dst:
        dst.write(arrays["optical_refl"], 1)
        dst.update_tags(
            description="Synthetic OHRC optical — boulder detection",
            units="reflectance_0_255",
            instrument="OHRC_synthetic",
            mission="Chandrayaan-2_simulated",
        )
    print(f"  [OHRC]  Written: {out_path}")


def write_thermal(arrays: dict, out_path: str, transform, crs) -> None:
    """Write a synthetic surface temperature map (float32, Kelvin)."""
    shadow = arrays["shadow_mask"].astype(np.float32)
    # PSR floor ~25 K, sunlit rim ~380 K
    temp = np.where(shadow, 25.0, 280.0).astype(np.float32)
    rng = np.random.default_rng(99)
    temp += (rng.standard_normal(temp.shape) * 10).astype(np.float32)
    temp = np.clip(temp, 20, 400).astype(np.float32)
    with rasterio.open(
        out_path, "w",
        driver="GTiff",
        height=temp.shape[0],
        width=temp.shape[1],
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        compress="lzw",
    ) as dst:
        dst.write(temp, 1)
        dst.update_tags(
            description="Synthetic IIRS surface temperature — Kelvin",
            units="Kelvin",
            psr_temp_k="25",
            sunlit_temp_k="380",
            instrument="IIRS_synthetic",
            mission="Chandrayaan-2_simulated",
        )
    print(f"  [TEMP]  Written: {out_path}")


def print_validation(arrays: dict) -> None:
    """Print ice detection validation stats."""
    cpr = arrays["cpr"]
    dop = arrays["dop"]
    ice_mask = (cpr > CPR_THRESHOLD) & (dop < DOP_THRESHOLD)
    total_cells = cpr.size
    ice_cells = int(ice_mask.sum())
    total_vol = float(ice_cells * CELL_SIZE_M ** 2 * 5.0 * (cpr[ice_mask].mean() - 1.0) / 2.0) if ice_cells > 0 else 0.0

    print("\n  === Validation ===")
    print(f"  Grid: {cpr.shape[0]} × {cpr.shape[1]} cells @ {CELL_SIZE_M} m")
    print(f"  Ice cells    : {ice_cells} / {total_cells}  ({100*ice_cells/total_cells:.1f}%)")
    print(f"  CPR max      : {cpr.max():.3f}  (threshold > {CPR_THRESHOLD})")
    print(f"  DOP min      : {dop.min():.3f}  (threshold < {DOP_THRESHOLD})")
    print(f"  Ice vol ~est : {total_vol:,.0f} m³")
    print(f"  Shadow cells : {int(arrays['shadow_mask'].sum())} ({100*arrays['shadow_mask'].mean():.1f}%)")
    print(f"  Slope > 15°  : {int((arrays['slope_deg'] > 15).sum())} cells\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not _RASTERIO_OK:
        print("ERROR: rasterio not installed.  Run:  pip install rasterio")
        sys.exit(1)

    print("AstraNav-LRIS — Synthetic GeoTIFF Generator")
    print("============================================")
    print(f"Output dir : {_DATA_DIR}")
    print(f"Grid       : {ROWS} × {COLS} cells @ {CELL_SIZE_M} m")
    print(f"Region     : lat {ORIGIN_LAT} -> {ORIGIN_LAT + ROWS * CELL_SIZE_M / DEG_LAT_M:.4f}")
    print()

    _ensure_dirs()

    print("Generating synthetic arrays …")
    arrays = _build_synthetic_arrays(ROWS, COLS, seed=42)
    print("  Arrays generated.")

    transform = _moon_transform(ROWS, COLS, CELL_SIZE_M, ORIGIN_LAT, ORIGIN_LON)
    crs = _moon_crs()

    print("Writing GeoTIFFs …")
    write_dfsar(arrays,   os.path.join(_DATA_DIR, "sar",     "synthetic_dfsar.tif"), transform, crs)
    write_dem(arrays,     os.path.join(_DATA_DIR, "dem",     "synthetic_dem.tif"),   transform, crs)
    write_optical(arrays, os.path.join(_DATA_DIR, "optical", "synthetic_ohrc.tif"),  transform, crs)
    write_thermal(arrays, os.path.join(_DATA_DIR, "thermal", "synthetic_temp.tif"),  transform, crs)

    print_validation(arrays)
    print("Done.  All synthetic GeoTIFFs written successfully.")


if __name__ == "__main__":
    main()
