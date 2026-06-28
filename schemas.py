from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Literal

# ==========================================
# MOCK INPUT SCHEMAS (From Member 1)
# ==========================================

class IceLayerData(BaseModel):
    # MOCK DATA - Replace with real Member 1 integration
    lat: float
    lon: float
    ice_volume_m3: float = Field(..., description="Estimated ice volume in cubic meters")
    ice_depth_m: float = Field(..., description="Estimated depth of ice in meters")
    confidence: float = Field(..., ge=0, le=1, description="Detection confidence (0-1)")

class HazardMask(BaseModel):
    # MOCK DATA - Replace with real Member 1 integration
    lat: float
    lon: float
    slope_deg: float = Field(..., description="Terrain slope in degrees")
    is_obstacle: bool = Field(..., description="True if a boulder/crater wall is present")

# ==========================================
# FEATURE 3: ROUTING SCHEMAS
# ==========================================

class Waypoint(BaseModel):
    lat: float
    lon: float
    type: Literal["transit", "solar_pitstop"]
    cumulative_distance_m: float
    cumulative_energy_wh: float
    is_in_shadow: bool
    confidence: float = Field(..., ge=0, le=1)

class RouteResponse(BaseModel):
    route_id: str
    region_id: str
    waypoints: List[Waypoint]
    total_distance_m: float
    total_energy_wh: float

# ==========================================
# FEATURE 4 & 5: LMRS & COMPARISON SCHEMAS
# ==========================================

class ResourceAccessibilityIndex(BaseModel):
    ice_volume_m3: float
    ice_depth_m: float
    extraction_difficulty_score: float

class CommVisibility(BaseModel):
    earth_line_of_sight: bool
    signal_strength_pct: float

class ThermalRisk(BaseModel):
    total_energy_wh: float
    shadow_exposure_time_min: float
    thermal_risk_score: float

class LMRSResponse(BaseModel):
    lat: float
    lon: float
    region_id: str
    lmrs_score: float = Field(..., description="Overall Lunar Mining Readiness Score (0-100)")
    rai: ResourceAccessibilityIndex
    comm_visibility: CommVisibility
    thermal_risk: ThermalRisk
    confidence: float = Field(..., ge=0, le=1)

class CompareRequest(BaseModel):
    points: List[Dict[str, float]] # List of {"lat": x, "lon": y}

class CompareResponse(BaseModel):
    comparisons: List[LMRSResponse]
    recommended_index: int

# ==========================================
# FEATURE 6: SWARM SCHEMAS
# ==========================================

class SwarmRouteRequest(BaseModel):
    region_id: str
    start_points: List[Dict[str, float]] # [{"lat": x, "lon": y}, ...]
    target_point: Dict[str, float]

class SwarmRouteResponse(BaseModel):
    region_id: str
    routes: Dict[str, RouteResponse] # rover_id -> RouteResponse

# ==========================================
# FEATURE 7: COPILOT SCHEMAS
# ==========================================

class ExplainRequestContext(BaseModel):
    route_id: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None

class ExplainRequest(BaseModel):
    question: str
    context: ExplainRequestContext

class ExplainResponse(BaseModel):
    answer: str

# ==========================================
# FEATURE 9: CONFIDENCE SCHEMAS
# ==========================================

class RouteConfidenceResponse(BaseModel):
    region_id: str
    grid_confidence: List[Dict[str, float]] # List of {"lat": x, "lon": y, "confidence": c}

# ==========================================
# FEATURE A: ILLUMINATION TIMELAPSE SCHEMAS
# ==========================================

class IlluminationCell(BaseModel):
    lat: float
    lon: float
    illumination_pct: float = Field(..., ge=0, le=100)
    is_pitstop_eligible: bool

class IlluminationFrame(BaseModel):
    timestep: int
    sun_angle_deg: float  # 0-360 degrees azimuth of the simulated sun

    cells: List[IlluminationCell]

class IlluminationTimelapseResponse(BaseModel):
    region_id: str
    duration_hours: float
    num_frames: int
    frames: List[IlluminationFrame]

# ==========================================
# FEATURE B: MISSION SNAPSHOT SCHEMAS
# ==========================================

class MissionSnapshotResponse(BaseModel):
    region_id: str
    snapshot_timestamp: str          # ISO-8601 UTC string
    ice_layer: IceLayerData          # MOCK DATA - swap with Member 1 real data
    hazard_summary: Dict[str, float] # slope_mean, slope_max, obstacle_pct, shadow_pct
    route: RouteResponse
    lmrs: LMRSResponse
    route_confidence: RouteConfidenceResponse

# ==========================================
# FEATURE C: MISSION REPORT SCHEMAS
# ==========================================

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
    ice_layer: IceLayerData    # MOCK DATA - swap with Member 1 real data
    hazard_summary: HazardSummary
    waypoints: List[Waypoint]
    total_distance_m: float
    total_energy_wh: float

# ==========================================
# FEATURE D: MISSION BRIEFING SCHEMAS
# ==========================================

class MissionBriefingResponse(BaseModel):
    lat: float
    lon: float
    region_id: str
    briefing_text: str
    generated_by: Literal["llm", "fallback_template"]
