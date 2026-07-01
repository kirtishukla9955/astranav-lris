"""
scripts/convert_tmc_nca.py
--------------------------
Converts a CH2_TMC_NCA PDS4 product (ISRO Chandrayaan-2 TMC-2 Nadir Camera Albedo)
from its native .img + .xml + .csv format into a georeferenced GeoTIFF.

What CH2_TMC_NCA IS:
  - TMC-2 Nadir Camera Albedo image
  - 2D panchromatic optical image (visible 500-800 nm)
  - 16-bit unsigned integers (DN = Digital Numbers, calibrated reflectance)
  - Resolution: ~5.73 m/pixel at 100 km altitude

What CH2_TMC_NCA IS NOT:
  - NOT a DEM / DTM / elevation model
  - CANNOT be used for slope computation or shadow modelling
  - Values are brightness counts, not metres above reference

What you need for DEM (slope + shadow):
  Download from PRADAN: CH2_TMC_OPD_*  (Ortho Product DEM)
                     or CH2_TMC_DTM_*  (Digital Terrain Model)
  These contain float32 elevation values in metres.

ALSO IMPORTANT — Region Coverage:
  This file covers lat 11-18 deg (Equatorial region).
  The pipeline needs lat -70 to -90 deg (Lunar South Polar Region).
  For ice detection, download data specifically for the south polar area.

Usage:
  python scripts/convert_tmc_nca.py <path_to_zip_or_img>

Output:
  backend/data/optical/tmc_nca_optical.tif   (GeoTIFF, uint8, georeferenced)
"""

from __future__ import annotations

import os
import sys
import re
import zipfile
import tempfile
from pathlib import Path

import numpy as np

try:
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.crs import CRS
    _RASTERIO_OK = True
except ImportError:
    print("ERROR: pip install rasterio")
    sys.exit(1)

_ROOT      = Path(__file__).parent.parent
_OUT_PATH  = _ROOT / "backend" / "data" / "optical" / "tmc_nca_optical.tif"


# ---------------------------------------------------------------------------
# PDS4 XML parser
# ---------------------------------------------------------------------------

def parse_pds4_xml(xml_text: str) -> dict:
    """Extract key metadata from ISRO PDS4 XML label."""
    def _find(tag, text):
        m = re.search(fr'<{tag}[^>]*>(.*?)</{tag}>', text, re.IGNORECASE | re.DOTALL)
        return m.group(1).strip() if m else None

    def _find_all(tag, text):
        return [m.strip() for m in re.findall(fr'<{tag}[^>]*>(.*?)</{tag}>', text, re.IGNORECASE | re.DOTALL)]

    # Image dimensions
    axes_elements = _find_all('elements', xml_text)
    lines   = int(axes_elements[0]) if len(axes_elements) > 0 else None
    samples = int(axes_elements[1]) if len(axes_elements) > 1 else None

    # Corner coordinates (Refined_Corner_Coordinates)
    ul_lat = _find('isda:upper_left_latitude', xml_text)
    ul_lon = _find('isda:upper_left_longitude', xml_text)
    lr_lat = _find('isda:lower_right_latitude', xml_text)
    lr_lon = _find('isda:lower_right_longitude', xml_text)

    return {
        "lines":   lines,
        "samples": samples,
        "dtype":   _find('data_type', xml_text) or "UnsignedLSB2",
        "product": _find('product_class', xml_text),
        "pixel_res_m": float(_find('isda:pixel_resolution', xml_text) or 5.73),
        "area":        _find('isda:area', xml_text),
        "ul_lat": float(ul_lat) if ul_lat else None,
        "ul_lon": float(ul_lon) if ul_lon else None,
        "lr_lat": float(lr_lat) if lr_lat else None,
        "lr_lon": float(lr_lon) if lr_lon else None,
    }


# ---------------------------------------------------------------------------
# PDS4 .img reader (raw binary, UnsignedLSB2 = uint16 little-endian)
# ---------------------------------------------------------------------------

def read_img_raw(img_path: str, lines: int, samples: int) -> np.ndarray:
    """Read a PDS4 raw binary .img file as uint16 array."""
    expected = lines * samples * 2   # 2 bytes per uint16
    actual   = os.path.getsize(img_path)
    if actual != expected:
        print(f"  WARNING: Expected {expected:,} bytes, got {actual:,}. Adjusting lines.")
        lines = actual // (samples * 2)

    arr = np.fromfile(img_path, dtype="<u2")   # little-endian uint16
    arr = arr[:lines * samples].reshape(lines, samples)
    return arr


# ---------------------------------------------------------------------------
# Main converter
# ---------------------------------------------------------------------------

def convert(source: str) -> str:
    """
    Convert CH2_TMC_NCA product (ZIP or extracted folder) to GeoTIFF.

    Returns path to output GeoTIFF.
    """
    source = Path(source)
    tmpdir = None

    # ── Extract ZIP if needed ──────────────────────────────────────────────
    if source.suffix.lower() == ".zip":
        print(f"Extracting {source.name} …")
        tmpdir = tempfile.mkdtemp(prefix="tmc_nca_")
        with zipfile.ZipFile(str(source)) as z:
            z.extractall(tmpdir)
        work_dir = Path(tmpdir)
    else:
        work_dir = source if source.is_dir() else source.parent

    # ── Find .img and .xml ─────────────────────────────────────────────────
    img_files = list(work_dir.rglob("*_img_*.img"))
    xml_files = list(work_dir.rglob("*_img_*.xml"))

    if not img_files:
        raise FileNotFoundError(f"No .img file found under {work_dir}")
    if not xml_files:
        raise FileNotFoundError(f"No .xml file found under {work_dir}")

    img_path = img_files[0]
    xml_path = xml_files[0]
    print(f"  .img : {img_path.name}  ({img_path.stat().st_size:,} bytes)")
    print(f"  .xml : {xml_path.name}")

    # ── Parse metadata ─────────────────────────────────────────────────────
    xml_text = xml_path.read_text(encoding="utf-8", errors="ignore")
    meta = parse_pds4_xml(xml_text)

    print(f"\n  Product metadata:")
    print(f"    Lines    : {meta['lines']}")
    print(f"    Samples  : {meta['samples']}")
    print(f"    Dtype    : {meta['dtype']}  (16-bit optical DN, NOT metres)")
    print(f"    Pixel res: {meta['pixel_res_m']} m/px")
    print(f"    Area     : {meta['area']}")
    print(f"    UL corner: lat={meta['ul_lat']}, lon={meta['ul_lon']}")
    print(f"    LR corner: lat={meta['lr_lat']}, lon={meta['lr_lon']}")

    # ── Region coverage warning ────────────────────────────────────────────
    if meta['ul_lat'] is not None and meta['ul_lat'] > -60:
        print(f"\n  [!] REGION WARNING:")
        print(f"      This image covers lat {meta['lr_lat']:.1f} to {meta['ul_lat']:.1f} deg")
        print(f"      Pipeline needs lat -70 to -90 deg (Lunar South Polar Region)")
        print(f"      Ice detection will NOT work on equatorial/mid-lat imagery.")
        print(f"      Download south-polar data from PRADAN for the ice pipeline.\n")

    # ── Read raw image ─────────────────────────────────────────────────────
    print("  Reading .img binary …")
    arr_uint16 = read_img_raw(str(img_path), meta['lines'], meta['samples'])
    print(f"  Shape: {arr_uint16.shape}  min={arr_uint16.min()}  max={arr_uint16.max()}")

    # Normalise to uint8 for GeoTIFF compatibility (2nd-98th percentile stretch)
    p2, p98 = np.percentile(arr_uint16, [2, 98])
    arr_norm = np.clip((arr_uint16.astype(np.float32) - p2) / (p98 - p2 + 1e-6) * 255, 0, 255)
    arr_uint8 = arr_norm.astype(np.uint8)

    # ── Build geotransform from corner coords ──────────────────────────────
    if all(v is not None for v in [meta['ul_lat'], meta['ul_lon'], meta['lr_lat'], meta['lr_lon']]):
        # from_bounds(west, south, east, north, width, height)
        west  = min(meta['ul_lon'], meta['lr_lon'])
        east  = max(meta['ul_lon'], meta['lr_lon'])
        south = min(meta['ul_lat'], meta['lr_lat'])
        north = max(meta['ul_lat'], meta['lr_lat'])
        transform = from_bounds(west, south, east, north, meta['samples'], meta['lines'])
        try:
            crs = CRS.from_epsg(104903)   # ESRI Moon 2000
        except Exception:
            crs = CRS.from_proj4("+proj=latlong +a=1737400 +b=1737400 +no_defs")
    else:
        from rasterio.transform import from_origin
        transform = from_origin(0, 90, meta['pixel_res_m'] / 111320, meta['pixel_res_m'] / 111320)
        crs = None
        print("  WARNING: No corner coords found — output will not be georeferenced.")

    # ── Write GeoTIFF ──────────────────────────────────────────────────────
    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        str(_OUT_PATH), "w",
        driver="GTiff", dtype="uint8", count=1,
        height=meta['lines'], width=meta['samples'],
        crs=crs, transform=transform, compress="lzw",
    ) as dst:
        dst.write(arr_uint8, 1)
        dst.update_tags(
            source=img_path.name,
            product="CH2_TMC_NCA",
            type="Nadir_Camera_Albedo",
            IS_DEM="False",
            NOTE="Optical image only. NOT elevation data. Use for visual/crater inspection.",
            pixel_res_m=str(meta['pixel_res_m']),
            region=str(meta['area']),
        )

    print(f"\n  Output: {_OUT_PATH}")
    print(f"  Size  : {meta['lines']} x {meta['samples']} px @ {meta['pixel_res_m']} m/px")

    # ── Cleanup ────────────────────────────────────────────────────────────
    if tmpdir:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    return str(_OUT_PATH)


# ---------------------------------------------------------------------------
# Summary banner
# ---------------------------------------------------------------------------

def print_summary():
    print("=" * 60)
    print("  CH2_TMC_NCA Converter — AstraNav-LRIS")
    print("=" * 60)
    print()
    print("  THIS FILE:   CH2_TMC_NCA = Nadir Camera Albedo (IMAGE)")
    print("  CAN DO:      Optical feature mapping, crater rim mapping")
    print("  CANNOT DO:   Slope computation, shadow modelling (NOT a DEM)")
    print()
    print("  FOR DEM (slope/shadow), download from PRADAN:")
    print("  +--------------------------------------------------+")
    print("  |  Product: CH2_TMC_OPD_*  (Ortho Product DEM)    |")
    print("  |        or CH2_TMC_DTM_*  (Digital Terrain Model) |")
    print("  |  Region : Latitude -70 to -90 deg (South Polar)  |")
    print("  +--------------------------------------------------+")
    print()


if __name__ == "__main__":
    print_summary()
    if len(sys.argv) < 2:
        # Default: look for the known filename in project root
        default = _ROOT / "ch2_tmc_nca_20260608T2253417834_d_img_d18.zip"
        if default.exists():
            source = str(default)
            print(f"  Auto-detected: {default.name}")
        else:
            print(f"Usage: python scripts/convert_tmc_nca.py <zip_or_folder>")
            sys.exit(0)
    else:
        source = sys.argv[1]

    print(f"  Converting: {Path(source).name}")
    print()
    out = convert(source)
    print()
    print("  Done. The GeoTIFF is saved as an OPTICAL image.")
    print("  It can be used for crater/feature mapping, NOT slope computation.")
