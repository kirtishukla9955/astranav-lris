"""
pathfinder/__init__.py
----------------------
Public surface of the pathfinder package.

Importers should use this module rather than importing sub-modules directly,
so the internal structure can be reorganised without breaking callers.

Usage
-----
    from pathfinder import plan_route, PolarGrid, CostConfig, RouteResult
"""

from .astar import astar
from .cost import (
    BatteryModel,
    CostConfig,
    StaticBatteryModel,
    octile_heuristic,
    traversal_cost,
)
from .grid import PolarGrid
from .pitstop import build_route
from .types import (
    GridCell,
    RouteResult,
    RoverConfig,
    RoverStatus,
    SwarmPlanResult,
    Waypoint,
    WaypointType,
)


def plan_route(
    grid: PolarGrid,
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    config: CostConfig,
    initial_battery_pct: float = 100.0,
    dark_budget_wh: float = 80.0,
    battery_capacity_wh: float = 200.0,
    solar_charge_rate_wh: float = 50.0,
) -> RouteResult:
    """
    High-level entry point: lat/lon → RouteResult.

    Orchestrates:
    1. Coordinate → cell conversion
    2. A* search
    3. Pitstop post-processing

    Returns RouteResult with route_found=False if no path exists.
    """
    start_cell = grid.lat_lon_to_cell(start_lat, start_lon)
    goal_cell = grid.lat_lon_to_cell(end_lat, end_lon)

    cell_path = astar(grid, start_cell, goal_cell, config)

    if cell_path is None:
        return RouteResult(
            route_found=False,
            waypoints=[],
            total_distance_m=0.0,
            total_energy_wh=0.0,
            total_pitstops=0,
            total_waypoints=0,
            warnings=["no_path_found_start_or_end_in_hazard_or_grid_blocked"],
        )

    return build_route(
        grid=grid,
        cell_path=cell_path,
        config=config,
        initial_battery_pct=initial_battery_pct,
        dark_budget_wh=dark_budget_wh,
        battery_capacity_wh=battery_capacity_wh,
        solar_charge_rate_wh=solar_charge_rate_wh,
    )


__all__ = [
    # High-level
    "plan_route",
    # Grid
    "PolarGrid",
    "GridCell",
    # Cost
    "CostConfig",
    "BatteryModel",
    "StaticBatteryModel",
    "traversal_cost",
    "octile_heuristic",
    # Algorithm
    "astar",
    "build_route",
    # Types
    "Waypoint",
    "WaypointType",
    "RouteResult",
    "RoverConfig",
    "RoverStatus",
    "SwarmPlanResult",
]
