"""
api/models.py
-------------
Pydantic v2 request/response models for the AstraNav-LRIS REST API.

All models are derived from the pure-Python types in pathfinder/types.py —
the API layer wraps them; it does NOT import algorithm internals directly.

Naming convention:
  *Request  → inbound body (POST)
  *Response → outbound body (all methods)
  *Out      → nested response fragment
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional
from datetime import datetime

from pydantic import BaseModel, Field, model_validator


# ===========================================================================
# Shared fragments
# ===========================================================================

class GeoPointOut(BaseModel):
    lat: float = Field(..., description="Latitude in degrees (south pole ≈ −90)")
    lon: float = Field(..., description="Longitude in degrees")


class WaypointOut(BaseModel):
    lat: float
    lon: float
    type: Literal["transit", "solar_pitstop"]
    cumulative_distance_m: float = Field(..., ge=0)
    cumulative_energy_wh: float = Field(..., ge=0)
    battery_pct_remaining: float = Field(..., ge=0, le=100)
    is_shadowed: bool
    solar_illumination: float = Field(..., ge=0, le=1)

    model_config = {"json_schema_extra": {"example": {
        "lat": -89.5123, "lon": 44.2891, "type": "transit",
        "cumulative_distance_m": 142.7, "cumulative_energy_wh": 18.4,
        "battery_pct_remaining": 76.3, "is_shadowed": True,
        "solar_illumination": 0.0,
    }}}


# ===========================================================================
# /api/route — Shadow-Hopping Pathfinder
# ===========================================================================

class RouteResponse(BaseModel):
    region_id: str
    start: GeoPointOut
    end: GeoPointOut
    route_found: bool
    total_distance_m: float
    total_energy_wh: float
    total_pitstops: int
    total_waypoints: int
    ice_seeking_mode: bool
    use_predictive_battery: bool
    waypoints: list[WaypointOut]
    warnings: list[str] = Field(default_factory=list)


# ===========================================================================
# /api/lmrs — Lunar Mining Readiness Score
# ===========================================================================

class RAIBreakdownOut(BaseModel):
    """Resource Accessibility Index sub-score."""
    score: float = Field(..., ge=0, le=100)
    ice_volume_m3: float
    ice_depth_m: float
    confidence: float = Field(..., ge=0, le=1)
    extraction_difficulty: float = Field(..., ge=0, le=1,
        description="0=trivial, 1=extremely difficult")
    nearest_ice_distance_m: float = Field(..., ge=0,
        description="Distance from query point to nearest ice candidate")


class CommVisibilityOut(BaseModel):
    """Communication line-of-sight sub-score."""
    score: float = Field(..., ge=0, le=100)
    los_fraction: float = Field(..., ge=0, le=1,
        description="Fraction of time line-of-sight to Earth/orbiter is clear")
    occlusion_reason: Optional[str] = Field(None,
        description="Human-readable occlusion cause, or null if clear")


class ThermalRiskOut(BaseModel):
    """Thermal/energy risk sub-score (higher = safer)."""
    score: float = Field(..., ge=0, le=100,
        description="Inverted & normalised: 100 = minimal energy cost (safest)")
    energy_cost_wh: float = Field(..., ge=0,
        description="Wh needed to reach this point from the reference lander")
    mean_temperature_k: float = Field(...,
        description="Estimated temperature at this cell in Kelvin")
    dark_dwell_fraction: float = Field(..., ge=0, le=1,
        description="Fraction of route waypoints that are permanently shadowed")


class WeightsOut(BaseModel):
    rai: float
    comm_visibility: float
    thermal_risk: float


class LMRSResponse(BaseModel):
    region_id: str
    lat: float
    lon: float
    lmrs_score: float = Field(..., ge=0, le=100)
    weights_used: WeightsOut
    rai: RAIBreakdownOut
    comm_visibility: CommVisibilityOut
    thermal_risk: ThermalRiskOut
    data_freshness: dict[str, str] = Field(default_factory=dict,
        description="ISO timestamps of last ice/hazard layer fetch")


# ===========================================================================
# /api/lmrs/compare — Multi-Site Comparison
# ===========================================================================

class ComparePointIn(BaseModel):
    lat: float
    lon: float
    label: str = Field(..., min_length=1, max_length=64)


class LMRSWithLabel(LMRSResponse):
    label: str


class LMRSCompareRequest(BaseModel):
    region_id: str
    points: Annotated[list[ComparePointIn], Field(min_length=2, max_length=5)]
    weights: Optional[WeightsOut] = None
    use_predictive_battery: bool = False

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> "LMRSCompareRequest":
        if self.weights is not None:
            total = self.weights.rai + self.weights.comm_visibility + self.weights.thermal_risk
            if abs(total - 1.0) > 0.01:
                raise ValueError(
                    f"weights must sum to 1.0 (got {total:.3f}). "
                    "Adjust rai + comm_visibility + thermal_risk."
                )
        return self


class LMRSCompareResponse(BaseModel):
    region_id: str
    recommended: str = Field(..., description="Label of the site with the highest LMRS score")
    recommended_score: float = Field(..., ge=0, le=100)
    results: list[LMRSWithLabel] = Field(
        ..., description="Full LMRS breakdowns sorted descending by lmrs_score"
    )


# ===========================================================================
# /api/swarm/plan — Multi-Rover Swarm Planning
# ===========================================================================

class RoverPlanRequest(BaseModel):
    rover_id: str = Field(..., min_length=1, max_length=32)
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    initial_battery_pct: float = Field(default=100.0, ge=0, le=100)
    ice_seeking: bool = False


class SwarmPlanRequest(BaseModel):
    region_id: str
    rovers: Annotated[list[RoverPlanRequest], Field(min_length=1, max_length=2,
        description="Cap at 2 rovers for demo stability")]
    dark_budget_wh: float = Field(default=80.0, gt=0)
    shadow_penalty_weight: float = Field(default=5.0, gt=0)
    use_predictive_battery: bool = False


class RoverRouteOut(BaseModel):
    rover_id: str
    route_found: bool
    total_distance_m: float
    total_energy_wh: float
    total_pitstops: int
    total_waypoints: int
    waypoints: list[WaypointOut]
    warnings: list[str] = Field(default_factory=list)


class SwarmPlanResponse(BaseModel):
    region_id: str
    total_rovers: int
    plans: list[RoverRouteOut]
    collision_avoidance: Literal["not_implemented"] = "not_implemented"
    warnings: list[str] = Field(default_factory=list)


# ===========================================================================
# /ws/telemetry/{region_id} — Live Rover Telemetry (WebSocket)
# ===========================================================================

class TelemetryMessage(BaseModel):
    """Single telemetry frame broadcast over the WebSocket stream."""
    rover_id: str = Field(..., description="Unique rover identifier")
    region_id: str
    lat: float = Field(..., description="Current rover latitude")
    lon: float = Field(..., description="Current rover longitude")
    battery_pct: float = Field(..., ge=0, le=100, description="Battery charge 0–100")
    is_shadowed: bool
    solar_illumination: float = Field(..., ge=0, le=1)
    status: Literal["moving", "charging", "arrived", "stalled"]
    waypoint_index: int = Field(..., ge=0, description="Index into the rover's waypoint list")
    total_waypoints: int
    cumulative_distance_m: float = Field(..., ge=0)
    cumulative_energy_wh: float = Field(..., ge=0)
    timestamp: str = Field(..., description="ISO-8601 UTC timestamp")


# ===========================================================================
# /api/battery-model/info — Predictive Battery Model Metadata
# ===========================================================================

class FeatureImportanceOut(BaseModel):
    feature: str
    importance: float = Field(..., ge=0, le=1)


class BatteryModelInfoResponse(BaseModel):
    model_type: str = Field(..., description="scikit-learn estimator class name")
    feature_names: list[str]
    feature_importances: Optional[list[FeatureImportanceOut]] = Field(
        None, description="Available for tree-based models; null for linear models"
    )
    training_samples: int
    use_predictive_battery_flag: bool = Field(
        ..., description="Whether the ML model is currently active (feature flag)"
    )
    caveat: str = Field(
        ...,
        description="Plain-text disclaimer for judges — model trained on synthetic data",
    )


# ===========================================================================
# /api/copilot/ask — Data-Grounded Chat Copilot
# ===========================================================================

class CopilotContextPoint(BaseModel):
    lat: float
    lon: float


class CopilotRequest(BaseModel):
    region_id: str
    question: str = Field(..., min_length=3, max_length=2000)
    context_point: Optional[CopilotContextPoint] = Field(
        None, description="Optional map coordinate to ground the answer on"
    )


class CopilotResponse(BaseModel):
    answer: str
    data_sources_used: list[str] = Field(
        ...,
        description="List of data sources the answer was grounded on (e.g. 'ice-layer', 'lmrs@(-89.5,44.2)')",
    )
    region_id: str
    context_point: Optional[CopilotContextPoint] = None
    model_used: str = Field(
        ..., description="LLM model name, or 'fallback' if LLM was unavailable"
    )


# ===========================================================================
# Error shapes (documented in OpenAPI)
# ===========================================================================

class ErrorDetail(BaseModel):
    detail: str
    reason: Optional[str] = None


# ===========================================================================
# Teammate Integrated Features
# ===========================================================================

class IceLayerData(BaseModel):
    lat: float
    lon: float
    ice_volume_m3: float = Field(..., description="Estimated ice volume in cubic meters")
    ice_depth_m: float = Field(..., description="Estimated depth of ice in meters")
    confidence: float = Field(..., ge=0, le=1, description="Detection confidence (0-1)")

class RouteConfidenceResponse(BaseModel):
    region_id: str
    grid_confidence: list[dict[str, float]] # List of {"lat": x, "lon": y, "confidence": c}

class IlluminationCell(BaseModel):
    lat: float
    lon: float
    illumination_pct: float = Field(..., ge=0, le=100)
    is_pitstop_eligible: bool

class IlluminationFrame(BaseModel):
    timestep: int
    sun_angle_deg: float
    cells: list[IlluminationCell]

class IlluminationTimelapseResponse(BaseModel):
    region_id: str
    duration_hours: float
    num_frames: int
    frames: list[IlluminationFrame]

class MissionSnapshotResponse(BaseModel):
    region_id: str
    snapshot_timestamp: str          # ISO-8601 UTC string
    ice_layer: IceLayerData          # MOCK DATA
    hazard_summary: dict[str, float] # slope_mean, slope_max, obstacle_pct, shadow_pct
    route: RouteResponse
    lmrs: LMRSResponse
    route_confidence: RouteConfidenceResponse

class HazardSummary(BaseModel):
    slope_mean_deg: float
    slope_max_deg: float
    obstacle_cell_pct: float   # percentage of sampled cells that are impassable
    shadow_cell_pct: float     # percentage of sampled cells in permanent shadow

class MissionReportData(BaseModel):
    region_id: str
    generated_at: str          # ISO-8601 UTC string
    lat: float
    lon: float
    lmrs: LMRSResponse
    ice_layer: IceLayerData
    hazard_summary: HazardSummary
    waypoints: list[WaypointOut]
    total_distance_m: float
    total_energy_wh: float

class MissionBriefingResponse(BaseModel):
    lat: float
    lon: float
    region_id: str
    briefing_text: str
    briefing: str
    generated_by: Literal["llm", "fallback_template"]

class MissionBriefingRequest(BaseModel):
    lmrs: LMRSResponse
    route: Optional[RouteResponse] = None

