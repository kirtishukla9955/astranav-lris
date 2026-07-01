"""
detection/models.py
-------------------
Pydantic v2 models for the Member 1 Detection Service.

Contract: these schemas are the EXACT shapes consumed by backend/data/mock_fixtures.py.
The downstream team swaps the mock with a live httpx call to this service's endpoints.

Endpoints this service exposes:
  GET /api/ice-layer/{region_id}    → IceLayerResponse   (GeoJSON FeatureCollection)
  GET /api/hazard-layer/{region_id} → HazardLayerResponse (GeoJSON FeatureCollection)

Science constants (ISRO-specified, immutable):
  CPR_THRESHOLD     = 1.0    (Circular Polarization Ratio — must exceed this for ice)
  DOP_THRESHOLD     = 0.13   (Degree of Polarization — must be below this for ice)
  DIELECTRIC_DUST   = 2.5    (pure lunar regolith)
  DIELECTRIC_ICE    = 3.5    (ice-laden regolith mixture)
  MAX_ICE_DEPTH_M   = 5.0    (maximum profiling depth in metres)
  SLOPE_NOGO_DEG    = 15.0   (slope threshold for hard no-go)
  BOULDER_NOGO_M    = 0.5    (minimum boulder diameter triggering obstacle flag)
"""

from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Science constants (exported so pipeline modules can import from one place)
# ---------------------------------------------------------------------------

CPR_THRESHOLD: float = 1.0
DOP_THRESHOLD: float = 0.13
DIELECTRIC_DUST: float = 2.5
DIELECTRIC_ICE: float = 3.5
MAX_ICE_DEPTH_M: float = 5.0
SLOPE_NOGO_DEG: float = 15.0
BOULDER_NOGO_M: float = 0.5


# ---------------------------------------------------------------------------
# Shared GeoJSON primitives
# ---------------------------------------------------------------------------

class PolygonGeometry(BaseModel):
    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[list[float]]] = Field(
        ...,
        description="GeoJSON Polygon rings — first ring is outer boundary. "
                    "Coordinate order: [longitude, latitude] (GeoJSON standard).",
    )


# ---------------------------------------------------------------------------
# Ice-layer feature
# ---------------------------------------------------------------------------

class IceFeatureProperties(BaseModel):
    """
    Properties attached to every ice-candidate polygon.

    Scientific provenance
    ---------------------
    ice_id        : stable unique ID referenceable across frames
    cpr           : computed Circular Polarization Ratio (must be > 1.0)
    dop           : computed Degree of Polarization (must be < 0.13)
    dielectric_constant : estimated ε (2.5=dust → 3.5=ice mixture)
    depth_m       : estimated ice depth, 0–5 m, from dielectric model
    volume_m3     : depth_m × cell_area_m² (per polygon)
    confidence    : composite quality score 0–1
    cell_size_m   : raster pixel size used for volume calculation
    """

    ice_id: str = Field(..., description="Stable ice polygon identifier, e.g. 'ICE-001'")
    cpr: float = Field(..., ge=CPR_THRESHOLD,
                       description=f"Circular Polarization Ratio (must be ≥ {CPR_THRESHOLD})")
    dop: float = Field(..., le=DOP_THRESHOLD,
                       description=f"Degree of Polarization (must be ≤ {DOP_THRESHOLD})")
    dielectric_constant: float = Field(..., ge=DIELECTRIC_DUST, le=4.5,
                                       description="Estimated dielectric constant ε")
    depth_m: float = Field(..., ge=0.0, le=MAX_ICE_DEPTH_M,
                           description="Estimated ice depth in metres (0–5 m profile)")
    volume_m3: float = Field(..., ge=0.0,
                             description="Estimated water-ice volume in cubic metres")
    confidence: float = Field(..., ge=0.0, le=1.0,
                              description="Detection confidence 0–1")
    cell_size_m: float = Field(default=30.0,
                               description="Raster cell size used for area → volume")


class IceFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: PolygonGeometry
    properties: IceFeatureProperties


class IceLayerResponse(BaseModel):
    """
    GeoJSON FeatureCollection of ice-candidate polygons.

    Downstream contract
    -------------------
    backend/data/mock_fixtures.py → fetch_ice_layer() returns this shape.
    backend/data/region_registry.py → build_grid_for_region() ingests properties.
    """

    type: Literal["FeatureCollection"] = "FeatureCollection"
    region_id: str
    features: list[IceFeature]
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Pipeline metadata: timestamp, raster paths, science thresholds used",
    )


# ---------------------------------------------------------------------------
# Hazard-layer feature
# ---------------------------------------------------------------------------

HazardType = Literal["steep_slope", "boulder_field", "crater_wall", "shadow_zone"]
Severity = Literal["no_go", "caution"]


class HazardFeatureProperties(BaseModel):
    """
    Properties attached to every hazard polygon.

    Severity rules
    --------------
    no_go   : slope > 15° or boulder > 0.5 m — router sets traversal cost = ∞
    caution : slope 10–15° — router adds a penalty multiplier but cell is passable
    """

    hazard_id: str = Field(..., description="Stable hazard polygon identifier")
    hazard_type: HazardType
    severity: Severity
    slope_deg: Optional[float] = Field(
        None, ge=0.0, le=90.0,
        description="Slope in degrees (present for steep_slope features)"
    )
    max_boulder_diameter_m: Optional[float] = Field(
        None, ge=0.0,
        description="Largest boulder diameter in metres (present for boulder_field features)"
    )


class HazardFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: PolygonGeometry
    properties: HazardFeatureProperties


class HazardLayerResponse(BaseModel):
    """
    GeoJSON FeatureCollection of hazard polygons.

    Downstream contract
    -------------------
    backend/data/mock_fixtures.py → fetch_hazard_layer() returns this shape.
    backend/data/region_registry.py → build_grid_for_region() marks hazard cells.
    """

    type: Literal["FeatureCollection"] = "FeatureCollection"
    region_id: str
    features: list[HazardFeature]
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Health / status response
# ---------------------------------------------------------------------------

class DetectionServiceStatus(BaseModel):
    service: str = "AstraNav-LRIS Detection Service (Member 1)"
    status: Literal["ok", "degraded"] = "ok"
    available_regions: list[str]
    science_thresholds: dict[str, float] = Field(
        default_factory=lambda: {
            "cpr_threshold": CPR_THRESHOLD,
            "dop_threshold": DOP_THRESHOLD,
            "dielectric_dust": DIELECTRIC_DUST,
            "dielectric_ice": DIELECTRIC_ICE,
            "max_ice_depth_m": MAX_ICE_DEPTH_M,
            "slope_nogo_deg": SLOPE_NOGO_DEG,
            "boulder_nogo_m": BOULDER_NOGO_M,
        }
    )
