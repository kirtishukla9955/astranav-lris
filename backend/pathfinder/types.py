"""
pathfinder/types.py
-------------------
Pure-Python domain types for the AstraNav-LRIS pathfinder.
NO FastAPI, NO Pydantic imports — this module must remain framework-agnostic
so the A* core can be swapped for a Rust/Numba kernel without touching the
API layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class WaypointType(str, Enum):
    """Semantic classification of a waypoint on the rover route."""
    TRANSIT = "transit"
    SOLAR_PITSTOP = "solar_pitstop"


class RoverStatus(str, Enum):
    """Live telemetry status broadcasted over WebSocket."""
    MOVING = "moving"
    CHARGING = "charging"
    ARRIVED = "arrived"
    STALLED = "stalled"


# ---------------------------------------------------------------------------
# Grid Primitives
# ---------------------------------------------------------------------------

@dataclass
class GridCell:
    """
    Represents a single 5 m × 5 m cell on the rover planning grid.

    Domain rules encoded here:
    - ``is_hazard=True``  → hard impassable wall (slope >15° or boulder >0.5 m)
    - ``is_shadowed=True`` → dark zone; NOT impassable but carries heavy cost
    - ``solar_illumination`` in [0, 1]; >0 means cell is a valid pitstop candidate
    - ``temperature_k`` drives heater-energy cost in the predictive battery model;
      doubly-shadowed craters sit at ~25 K
    """
    row: int
    col: int
    lat: float
    lon: float

    # Traversal
    base_traversal_cost: float = 1.0
    is_hazard: bool = False           # Member 1: slope>15° or obstacle>0.5m → True

    # Illumination / thermal
    is_shadowed: bool = False
    solar_illumination: float = 0.0   # 0=fully dark, 1=fully lit
    temperature_k: float = 200.0      # ~200 K lit, ~25 K doubly shadowed
    slope_deg: float = 0.0

    # Ice data (from Member 1's ice-layer)
    ice_volume_m3: float = 0.0
    ice_confidence: float = 0.0       # 0–1


# ---------------------------------------------------------------------------
# Route Outputs
# ---------------------------------------------------------------------------

@dataclass
class Waypoint:
    """
    A single step in a planned rover route.
    Matches the JSON schema agreed in Step 1.
    """
    lat: float
    lon: float
    type: WaypointType
    cumulative_distance_m: float
    cumulative_energy_wh: float
    battery_pct_remaining: float      # 0–100
    is_shadowed: bool
    solar_illumination: float         # 0–1


@dataclass
class RouteResult:
    """
    Complete output of the pathfinder for one rover mission.
    ``route_found=False`` signals an unroutable request (start/end in hazard,
    or grid fully blocked) — NOT an exception; callers must check this flag.
    """
    route_found: bool
    waypoints: list[Waypoint]
    total_distance_m: float
    total_energy_wh: float
    total_pitstops: int
    total_waypoints: int
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Swarm Support
# ---------------------------------------------------------------------------

@dataclass
class RoverConfig:
    """Per-rover mission parameters for swarm planning."""
    rover_id: str
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    initial_battery_pct: float = 100.0
    ice_seeking: bool = False


@dataclass
class SwarmPlanResult:
    """Aggregated result of planning routes for N rovers simultaneously."""
    region_id: str
    plans: list[tuple[str, RouteResult]]   # (rover_id, RouteResult)
    warnings: list[str] = field(default_factory=list)
