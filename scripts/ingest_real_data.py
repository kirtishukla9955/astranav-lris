"""
scripts/ingest_real_data.py
----------------------------
Real ISRO Chandrayaan-2 Data Ingestion Script
=============================================

DROP YOUR REAL DATA FILES HERE, then run this script.

Handles all formats the PRADAN portal provides:
  - CH2_SAR_...  (DFSAR — CEOS format OR GeoTIFF, L-band/S-band)
  - CH2_TMC2_... (TMC-2 DEM — GeoTIFF, float32, metres)
  - CH2_OHRC_... (OHRC optical — GeoTIFF or JPEG2000)
  - CH2_IIRS_... (IIRS thermal — GeoTIFF)

Usage
-----
1. Copy your CH2_*.* files (or folders) into the `real_input/` directory
   next to this script (create it if needed):

     astranav-lris-main/
       real_input/           <--- drop your files here
         CH2_SAR_MRS_...
         CH2_TMC2_DEM_...
         CH2_OHRC_NRT_...
       scripts/
         ingest_real_data.py   <--- this script

2. Run:
     python scripts/ingest_real_data.py

3. The script will:
   a) Auto-detect each file's instrument type from its name
   b) Inspect bands, scale, CRS, nodata
   c) Convert / calibrate as needed
   d) Write ready-to-use files to backend/data/{sar,dem,optical,thermal}/

Science:
  CPR > 1.0 AND DOP < 0.13  -> ice candidate (ISRO DFSAR specification)
  DFSAR HH, HV in LINEAR power (not dB) before CPR/DOP computation
  No-Go: slope > 15deg or boulder > 0.5 m
"""

from __future__ import annotations

import math
import os
import sys
import glob
from pathlib import Path

import numpy as np

try:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds
    _RASTERIO_OK = True
except ImportError:
    _RASTERIO_OK = False
    print("ERROR: rasterio not installed.  Run: pip install rasterio")
    sys.exit(1)

try:
    # pyrefly: ignore [missing-import]
    import pdr
    _PDR_OK = True
except ImportError:
    _PDR_OK = False

import zipfile
import tempfile
import shutil

# ---------------------------------------------------------------------------
# Directory layout
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).parent
_ROOT       = _SCRIPT_DIR.parent
_INPUT_DIR  = _ROOT / "real_input"
_DATA_DIR   = _ROOT / "backend" / "data"

SAR_OUT     = _DATA_DIR / "sar"     / "synthetic_dfsar.tif"
DEM_OUT     = _DATA_DIR / "dem"     / "synthetic_dem.tif"
OPTICAL_OUT = _DATA_DIR / "optical" / "synthetic_ohrc.tif"
THERMAL_OUT = _DATA_DIR / "thermal" / "synthetic_temp.tif"

# ---------------------------------------------------------------------------
# Science constants (IMMUTABLE)
# ---------------------------------------------------------------------------
CPR_THRESHOLD = 1.0
DOP_THRESHOLD = 0.13


# ===========================================================================
# STEP 1 — FILE SCANNER
# ===========================================================================

def scan_input_files() -> dict:
    """
    Walk real_input/ and classify files by instrument.
    Returns dict with keys: 'sar', 'dem', 'optical', 'thermal'
    each mapping to a list of matched Paths.
    """
    _INPUT_DIR.mkdir(exist_ok=True)

    # Collect all candidate files
    all_files = list(_INPUT_DIR.rglob("*"))
    all_files = [f for f in all_files if f.is_file()]

    found = {"sar": [], "dem": [], "optical": [], "thermal": []}

    for f in all_files:
        name = f.name.upper()

        # SAR / DFSAR files
        if any(k in name for k in ["SAR", "DFSAR", "CH2_SAR", "_SAR_"]):
            found["sar"].append(f)

        # TMC-2 DEM files
        elif any(k in name for k in ["TMC", "DEM", "DTM"]):
            found["dem"].append(f)

        # OHRC optical files
        elif any(k in name for k in ["OHRC", "OHR", "OPTICAL"]):
            found["optical"].append(f)

        # IIRS thermal files
        elif any(k in name for k in ["IIRS", "IIR", "THERMAL", "TEMP", "TEMPERATURE"]):
            found["thermal"].append(f)

        # Generic GeoTIFF fallback — inspect contents to classify
        elif f.suffix.lower() in (".tif", ".tiff", ".jp2", ".img", ".hdf", ".h5"):
            found["sar"].append(f)   # will be inspected/re-classified below

    return found


# ===========================================================================
# STEP 2 — FILE INSPECTOR
# ===========================================================================

def inspect_file(filepath: Path) -> dict:
    """
    Open a rasterio-readable file and return its metadata.
    Supports: GeoTIFF, JPEG2000, CEOS, HDF5 sub-datasets, ENVISAT N1.
    """
    result = {
        "path": str(filepath),
        "driver": None,
        "count": 0,
        "dtype": None,
        "crs": None,
        "shape": None,
        "nodata": None,
        "band_stats": [],   # [(min, max, mean) per band]
        "readable": False,
        "subdatasets": [],
        "error": None,
    }

    # Try direct open first
    try:
        with rasterio.open(str(filepath)) as src:
            result["driver"] = src.driver
            result["count"]  = src.count
            result["dtype"]  = src.dtypes[0]
            result["crs"]    = str(src.crs) if src.crs else "None"
            result["shape"]  = (src.height, src.width)
            result["nodata"] = src.nodata
            result["subdatasets"] = src.subdatasets
            result["readable"] = True

            # Sample band stats (read at 1:4 overview for speed)
            for band_idx in range(1, min(src.count + 1, 5)):
                try:
                    data = src.read(band_idx, out_shape=(
                        src.height // 4 or 1,
                        src.width  // 4 or 1
                    )).astype(np.float64)
                    nd = src.nodata
                    if nd is not None:
                        data = data[data != nd]
                    if data.size > 0:
                        result["band_stats"].append((
                            float(data.min()), float(data.max()), float(data.mean())
                        ))
                    else:
                        result["band_stats"].append((0, 0, 0))
                except Exception:
                    result["band_stats"].append(None)
                    
    except Exception as exc:
        # Fallback to pure python PDR library if rasterio doesn't understand the PDS4/binary file
        if _PDR_OK:
            try:
                data = pdr.read(str(filepath))
                if 'IMAGE' in data.keys():
                    img_array = data['IMAGE']
                elif 'UNCOMPRESSED_FILE' in data.keys():
                    img_array = data['UNCOMPRESSED_FILE']
                else:
                    valid_keys = [k for k in data.keys() if k.upper() != 'LABEL']
                    img_array = data[valid_keys[0]]
                
                img_array = np.array(img_array)
                
                # Check for (bands, height, width) or (height, width, bands)
                if len(img_array.shape) > 2:
                    # If last dimension is small, it's likely (H, W, Bands)
                    if img_array.shape[-1] <= 10:
                        img_array = np.moveaxis(img_array, -1, 0)
                
                # If it's a 2D array, wrap it in a band dimension so it's (1, H, W)
                if len(img_array.shape) == 2:
                    img_array = img_array[np.newaxis, :, :]
                    
                result["driver"] = "PDS4_PDR"
                result["count"]  = img_array.shape[0]
                result["dtype"]  = str(img_array.dtype)
                result["crs"]    = "None"
                result["shape"]  = (img_array.shape[1], img_array.shape[2])
                result["nodata"] = None
                result["readable"] = True
                result["_pdr_array"] = img_array  # stored as (bands, H, W)
                result["band_stats"].append((float(img_array.min()), float(img_array.max()), float(img_array.mean())))
                result["error"] = None
            except Exception as e_pdr:
                result["error"] = f"Rasterio error: {exc}. PDR error: {e_pdr}"
        else:
            result["error"] = f"{exc} (Consider installing 'pdr' to natively read ISRO .img files)"

    return result


def classify_sar_scale(info: dict) -> str:
    """
    Given band stats, determine if SAR data is in:
      'dB'           — values typically -30 to 0 dB
      'linear'       — values 0 to ~1 (backscatter coefficient)
      'amplitude'    — values 0 to ~10000 (raw amplitude)
      'complex'      — complex integers (SLC)
      'unknown'
    """
    if not info["band_stats"] or info["band_stats"][0] is None:
        return "unknown"

    vmin, vmax, vmean = info["band_stats"][0]

    if info["dtype"] in ("complex64", "complex128", "cint16", "cint32"):
        return "complex"
    if vmin < -100 and vmax < 10:
        return "dB"
    if 0 <= vmin and vmax <= 10.0:
        return "linear"
    if vmax > 100:
        return "amplitude"
    return "unknown"


# ===========================================================================
# STEP 3 — CONVERTERS
# ===========================================================================

def convert_sar_to_pipeline_format(
    filepath: Path,
    info: dict,
    hh_band_idx: int = 1,
    hv_band_idx: int = 2,
) -> bool:
    """
    Convert real DFSAR file to the 2-band dB GeoTIFF the pipeline expects.

    Handles:
      - GeoTIFF with HH=band1, HV=band2 in dB  -> copy directly
      - GeoTIFF with HH=band1, HV=band2 in linear -> convert to dB
      - GeoTIFF with HH=band1, HV=band2 in amplitude -> amplitude^2 -> dB
      - Complex SLC -> take magnitude squared -> dB
      - Single-band file (HH only) -> set HV = HH * 0.3 (rough non-ice estimate)

    Returns True if successful.
    """
    SAR_OUT.parent.mkdir(parents=True, exist_ok=True)
    scale = classify_sar_scale(info)
    count = info["count"]

    print(f"  SAR scale detected: {scale}, bands: {count}")

    # Handle HDF5 subdatasets — pick first two
    src_path = str(filepath)
    if info["subdatasets"]:
        print(f"  HDF5/NetCDF subdatasets found: {len(info['subdatasets'])}")
        # Use first two subdatasets as HH, HV
        subs = info["subdatasets"]
        if len(subs) >= 2:
            src_hh_path = subs[0][0]
            src_hv_path = subs[1][0]
        else:
            src_hh_path = subs[0][0]
            src_hv_path = subs[0][0]
        return _convert_two_source_paths(src_hh_path, src_hv_path, scale)

    # 1. PDR Fallback
    if info.get("driver") == "PDS4_PDR":
        src_array = info["_pdr_array"]  # shape is (bands, height, width)
        crs = None
        transform = rasterio.transform.from_origin(0, src_array.shape[1], 1, 1)
        
        def _read_mag_squared_pdr(band_idx: int) -> np.ndarray:
            raw = src_array[band_idx - 1].astype(np.float64)
            if scale == "dB":
                lin = 10.0 ** (raw / 10.0)
            elif scale == "linear":
                lin = raw
            elif scale == "amplitude":
                lin = raw ** 2
            elif scale == "complex":
                lin = np.abs(raw) ** 2
            else:
                lin = np.abs(raw) ** 2
            lin = np.where(np.isfinite(lin) & (lin > 0), lin, 1e-9)
            return lin.astype(np.float32)

        hh_lin = _read_mag_squared_pdr(hh_band_idx)
        if count >= 2:
            hv_lin = _read_mag_squared_pdr(hv_band_idx)
        else:
            print(f"  WARNING: Only 1 band found. Estimating HV = HH * 0.3 (non-ice assumption).")
            hv_lin = hh_lin * 0.3

        hh_db = (10.0 * np.log10(np.maximum(hh_lin, 1e-9))).astype(np.float32)
        hv_db = (10.0 * np.log10(np.maximum(hv_lin, 1e-9))).astype(np.float32)
        _validate_sar_bands(hh_lin, hv_lin)

        out_profile = {
            "driver": "GTiff", "dtype": "float32", "count": 2,
            "height": src_array.shape[1], "width": src_array.shape[2],
            "crs": crs, "transform": transform,
            "compress": "lzw", "nodata": None,
        }
        with rasterio.open(str(SAR_OUT), "w", **out_profile) as dst:
            dst.write(hh_db, 1)
            dst.write(hv_db, 2)
            dst.update_tags(band1="HH_dB", band2="HV_dB", source=str(filepath.name), instrument="CH2_DFSAR", scale_in=scale)

        print(f"  SAR -> {SAR_OUT}")
        return True

    # 2. Rasterio normal flow
    with rasterio.open(src_path) as src:
        profile = src.profile.copy()
        transform = src.transform
        crs = src.crs

        def _read_mag_squared(band_i: int) -> np.ndarray:
            """Read band and return linear-power (magnitude squared)."""
            raw = src.read(band_i).astype(np.float64)
            nd = src.nodata
            if nd is not None:
                raw[raw == nd] = np.nan

            if scale == "dB":
                lin = 10.0 ** (raw / 10.0)
            elif scale == "linear":
                lin = raw
            elif scale == "amplitude":
                lin = raw ** 2
            elif scale == "complex":
                lin = np.abs(raw) ** 2
            else:
                # Unknown: assume amplitude
                lin = np.abs(raw) ** 2

            lin = np.where(np.isfinite(lin) & (lin > 0), lin, 1e-9)
            return lin.astype(np.float32)

        hh_lin = _read_mag_squared(hh_band_idx)
        if count >= 2:
            hv_lin = _read_mag_squared(hv_band_idx)
        else:
            print(f"  WARNING: Only 1 band found. Estimating HV = HH * 0.3 (non-ice assumption).")
            hv_lin = hh_lin * 0.3

        # Convert to dB for storage
        hh_db = (10.0 * np.log10(np.maximum(hh_lin, 1e-9))).astype(np.float32)
        hv_db = (10.0 * np.log10(np.maximum(hv_lin, 1e-9))).astype(np.float32)

        # Quick validation
        _validate_sar_bands(hh_lin, hv_lin)

        # Write output
        out_profile = {
            "driver": "GTiff",
            "dtype": "float32",
            "count": 2,
            "height": src.height,
            "width":  src.width,
            "crs":    crs,
            "transform": transform,
            "compress": "lzw",
            "nodata": None,
        }
        with rasterio.open(str(SAR_OUT), "w", **out_profile) as dst:
            dst.write(hh_db, 1)
            dst.write(hv_db, 2)
            dst.update_tags(
                band1="HH_dB",
                band2="HV_dB",
                source=str(filepath.name),
                instrument="CH2_DFSAR",
                scale_in=scale,
            )

    print(f"  SAR -> {SAR_OUT}")
    return True


def _convert_two_source_paths(hh_path: str, hv_path: str, scale: str) -> bool:
    """Convert HH and HV from separate rasterio-readable paths."""
    def _load(path):
        with rasterio.open(path) as src:
            raw = src.read(1).astype(np.float64)
            transform = src.transform
            crs = src.crs
            shape = (src.height, src.width)
        return raw, transform, crs, shape

    hh_raw, transform, crs, shape = _load(hh_path)
    hv_raw, _, _, _ = _load(hv_path)

    if scale in ("amplitude", "unknown"):
        hh_lin = hh_raw ** 2
        hv_lin = hv_raw ** 2
    elif scale == "dB":
        hh_lin = 10.0 ** (hh_raw / 10.0)
        hv_lin = 10.0 ** (hv_raw / 10.0)
    else:
        hh_lin = hh_raw
        hv_lin = hv_raw

    hh_lin = np.where(hh_lin > 0, hh_lin, 1e-9).astype(np.float32)
    hv_lin = np.where(hv_lin > 0, hv_lin, 1e-9).astype(np.float32)
    _validate_sar_bands(hh_lin, hv_lin)

    hh_db = (10.0 * np.log10(hh_lin)).astype(np.float32)
    hv_db = (10.0 * np.log10(hv_lin)).astype(np.float32)

    SAR_OUT.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(str(SAR_OUT), "w", driver="GTiff", dtype="float32",
                       count=2, height=shape[0], width=shape[1],
                       crs=crs, transform=transform, compress="lzw") as dst:
        dst.write(hh_db, 1)
        dst.write(hv_db, 2)
    print(f"  SAR (2-source) -> {SAR_OUT}")
    return True


def _validate_sar_bands(hh: np.ndarray, hv: np.ndarray) -> None:
    """Print quick CPR/DOP stats so user can confirm ice signatures."""
    cpr = np.where(hh > 0, hv / hh, 0.0)
    dop = np.where((hh + hv) > 0, np.abs(hh - hv) / (hh + hv), 0.0)
    ice = (cpr > CPR_THRESHOLD) & (dop < DOP_THRESHOLD)

    print(f"\n  --- Band Validation ---")
    print(f"  HH  range  : [{hh.min():.4f}, {hh.max():.4f}]  mean={hh.mean():.4f}")
    print(f"  HV  range  : [{hv.min():.4f}, {hv.max():.4f}]  mean={hv.mean():.4f}")
    print(f"  CPR range  : [{cpr.min():.3f}, {cpr.max():.3f}]  (threshold > {CPR_THRESHOLD})")
    print(f"  DOP range  : [{dop.min():.3f}, {dop.max():.3f}]  (threshold < {DOP_THRESHOLD})")
    print(f"  Ice pixels : {int(ice.sum())} / {ice.size}  ({100*ice.mean():.2f}%)")
    if ice.sum() == 0:
        print("  WARNING: No ice pixels detected with current thresholds.")
        print("           Check that HH/HV bands are correct and data covers a PSR.")


def convert_dem_to_pipeline_format(filepath: Path, info: dict) -> bool:
    """
    Copy / reproject TMC-2 DEM to expected path.
    Handles: float32 already (ideal), int16 (convert), nodata masking.
    """
    DEM_OUT.parent.mkdir(parents=True, exist_ok=True)
    
    if info.get("driver") == "PDS4_PDR":
        dem = info["_pdr_array"][0].astype(np.float32)
        nd = None
        crs = None
        transform = rasterio.transform.from_origin(0, dem.shape[0], 1, 1)
    else:
        with rasterio.open(str(filepath)) as src:
            dem = src.read(1).astype(np.float32)
            nd  = src.nodata
            crs = src.crs
            transform = src.transform
            
    if nd is not None:
        dem[dem == nd] = np.nan
    # Fill NaN with interpolated mean
    if np.isnan(dem).any():
        dem = np.where(np.isnan(dem), np.nanmean(dem), dem)

    out_profile = {
        "driver": "GTiff", "dtype": "float32", "count": 1,
        "height": dem.shape[0], "width": dem.shape[1],
        "crs": crs, "transform": transform,
        "compress": "lzw", "nodata": -9999.0,
    }
    with rasterio.open(str(DEM_OUT), "w", **out_profile) as dst:
            dst.write(dem, 1)
            dst.update_tags(
                description="TMC-2 DEM (real data)",
                source=str(filepath.name),
                units="metres",
            )
    print(f"  DEM  -> {DEM_OUT}")
    print(f"  DEM  stats: min={float(np.nanmin(dem)):.1f} m  max={float(np.nanmax(dem)):.1f} m")
    return True


def convert_optical_to_pipeline_format(filepath: Path, info: dict) -> bool:
    """Copy OHRC optical to expected path (uint8, single band)."""
    OPTICAL_OUT.parent.mkdir(parents=True, exist_ok=True)
    
    if info.get("driver") == "PDS4_PDR":
        img = info["_pdr_array"][0].astype(np.float32)
        nd = None
        crs = None
        transform = rasterio.transform.from_origin(0, img.shape[0], 1, 1)
    else:
        with rasterio.open(str(filepath)) as src:
            if src.count >= 3:
                # RGB: take green channel (band 2) as luminance proxy
                img = src.read(2).astype(np.float32)
            else:
                img = src.read(1).astype(np.float32)
            nd = src.nodata
            crs = src.crs
            transform = src.transform

    if nd is not None:
        img[img == nd] = np.nan

    # Normalise to 0-255
    vmin, vmax = np.nanpercentile(img, 2), np.nanpercentile(img, 98)
    if vmax > vmin:
        img = np.clip((img - vmin) / (vmax - vmin) * 255, 0, 255)
    img = np.nan_to_num(img, nan=0).astype(np.uint8)

    out_profile = {
        "driver": "GTiff", "dtype": "uint8", "count": 1,
        "height": img.shape[0], "width": img.shape[1],
        "crs": crs, "transform": transform,
        "compress": "lzw",
    }
    with rasterio.open(str(OPTICAL_OUT), "w", **out_profile) as dst:
            dst.write(img, 1)
            dst.update_tags(source=str(filepath.name), instrument="CH2_OHRC")

    print(f"  OHRC -> {OPTICAL_OUT}")
    return True


def convert_thermal_to_pipeline_format(filepath: Path, info: dict) -> bool:
    """Copy IIRS thermal to expected path (float32, Kelvin)."""
    THERMAL_OUT.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(str(filepath)) as src:
        temp = src.read(1).astype(np.float32)
        nd = src.nodata
        if nd is not None:
            temp[temp == nd] = np.nan

        out_profile = {
            "driver": "GTiff", "dtype": "float32", "count": 1,
            "height": src.height, "width": src.width,
            "crs": src.crs, "transform": src.transform,
            "compress": "lzw",
        }
        with rasterio.open(str(THERMAL_OUT), "w", **out_profile) as dst:
            dst.write(temp, 1)
            dst.update_tags(source=str(filepath.name), units="Kelvin")

    print(f"  THERMAL -> {THERMAL_OUT}")
    return True


# ===========================================================================
# STEP 4 — MAIN ORCHESTRATOR
# ===========================================================================

def print_header():
    print("=" * 65)
    print("  AstraNav-LRIS — Real ISRO Data Ingestion")
    print("  CH2 / Chandrayaan-2 DFSAR + TMC-2 + OHRC + IIRS")
    print("=" * 65)
    print(f"  Input dir  : {_INPUT_DIR}")
    print(f"  Output dir : {_DATA_DIR}")
    print()


def main():
    print_header()

    if not _INPUT_DIR.exists() or not any(_INPUT_DIR.iterdir()):
        print(f"NOTICE: No files found in {_INPUT_DIR}")
        print()
        print("  To use real data:")
        print("  1. Create the folder:  real_input/")
        print("  2. Copy your CH2_*.* files into it")
        print("  3. Re-run this script")
        print()
        print("  Alternatively the pipeline runs fine with the")
        print("  synthetic GeoTIFFs already generated.")
        return

    found = scan_input_files()
    total = sum(len(v) for v in found.values())
    print(f"Files detected: {total}")
    for kind, files in found.items():
        for f in files:
            print(f"  [{kind.upper():8s}] {f.name}")
    print()

    # ── Process SAR ──────────────────────────────────────────────────────────
    if found["sar"]:
        sar_file = max(found["sar"], key=lambda f: f.stat().st_size)
        
        # If it's a zip file, try extracting it to a temporary directory
        temp_dir = None
        if sar_file.suffix.lower() == ".zip":
            print(f"[SAR] Extracting {sar_file.name} ...")
            temp_dir = tempfile.mkdtemp()
            with zipfile.ZipFile(sar_file, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            extracted_files = list(Path(temp_dir).rglob("*"))
            # Find the largest .img or .tif file in the extraction
            img_files = [f for f in extracted_files if f.suffix.lower() in (".img", ".tif", ".tiff")]
            if img_files:
                sar_file = max(img_files, key=lambda f: f.stat().st_size)
            
        print(f"[SAR] Inspecting: {sar_file.name}")
        info = inspect_file(sar_file)
        if not info["readable"]:
            print(f"  ERROR: Cannot open {sar_file.name}: {info['error']}")
            print("  Make sure 'pdr' is installed or convert with GDAL first.")
        else:
            print(f"  Driver: {info['driver']}")
            print(f"  Shape : {info['shape']}")
            print(f"  Bands : {info['count']}  dtype={info['dtype']}")
            print(f"  CRS   : {info['crs']}")
            for i, bs in enumerate(info["band_stats"]):
                if bs:
                    print(f"  Band {i+1}: min={bs[0]:.3f}  max={bs[1]:.3f}  mean={bs[2]:.3f}")
            ok = convert_sar_to_pipeline_format(sar_file, info)
            print(f"  Result: {'SUCCESS' if ok else 'FAILED'}")
            
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
    else:
        print("[SAR] No SAR files found — keeping existing synthetic_dfsar.tif")
    print()

    # ── Process DEM ──────────────────────────────────────────────────────────
    if found["dem"]:
        dem_file = max(found["dem"], key=lambda f: f.stat().st_size)
        print(f"[DEM] Inspecting: {dem_file.name}")
        info = inspect_file(dem_file)
        if info["readable"]:
            print(f"  Shape: {info['shape']}  dtype={info['dtype']}")
            ok = convert_dem_to_pipeline_format(dem_file, info)
            print(f"  Result: {'SUCCESS' if ok else 'FAILED'}")
        else:
            print(f"  ERROR: {info['error']}")
    else:
        print("[DEM] No DEM files found — keeping existing synthetic_dem.tif")
    print()

    # ── Process OHRC ─────────────────────────────────────────────────────────
    if found["optical"]:
        opt_file = max(found["optical"], key=lambda f: f.stat().st_size)
        print(f"[OHRC] Inspecting: {opt_file.name}")
        info = inspect_file(opt_file)
        if info["readable"]:
            print(f"  Shape: {info['shape']}  dtype={info['dtype']}")
            ok = convert_optical_to_pipeline_format(opt_file, info)
            print(f"  Result: {'SUCCESS' if ok else 'FAILED'}")
        else:
            print(f"  ERROR: {info['error']}")
    else:
        print("[OHRC] No optical files found — keeping existing synthetic_ohrc.tif")
    print()

    # ── Process IIRS ─────────────────────────────────────────────────────────
    if found["thermal"]:
        therm_file = max(found["thermal"], key=lambda f: f.stat().st_size)
        print(f"[IIRS] Inspecting: {therm_file.name}")
        info = inspect_file(therm_file)
        if info["readable"]:
            ok = convert_thermal_to_pipeline_format(therm_file, info)
            print(f"  Result: {'SUCCESS' if ok else 'FAILED'}")
        else:
            print(f"  ERROR: {info['error']}")
    else:
        print("[IIRS] No thermal files found — keeping existing synthetic_temp.tif")
    print()

    print("=" * 65)
    print("  Done. Reset the data_loader cache to pick up new files:")
    print("  Restart uvicorn OR call /api/health to trigger reload.")
    print("=" * 65)


if __name__ == "__main__":
    main()
