"""
pathfinder/fault_injector.py
----------------------------
Autonomous Contingency Planner wrappers and helpers.
Implements dynamic anomaly decorators for PolarGrid and BatteryModel.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple
from pathfinder.types import GridCell, Waypoint
from pathfinder.cost import BatteryModel
from pathfinder.grid import PolarGrid
from scoring.lmrs_scorer import compute_comm_visibility

class FaultyBatteryModel:
    """
    Wraps an existing BatteryModel to simulate motor actuator degradation
    and extreme PSR thermal load.
    """
    def __init__(
        self,
        base_model: BatteryModel,
        multiplier: float = 1.0,
        thermal_scale: float = 1.0
    ) -> None:
        self.base_model = base_model
        self.multiplier = multiplier
        self.thermal_scale = thermal_scale

    def predict_drain_wh(
        self,
        cell: GridCell,
        distance_m: float,
        prior_battery_pct: float
    ) -> float:
        # Get baseline prediction
        drain = self.base_model.predict_drain_wh(cell, distance_m, prior_battery_pct)
        
        # Apply thermal scaling inside shadows (heaters drawing more power)
        if self.thermal_scale != 1.0 and cell.is_shadowed:
            if hasattr(self.base_model, 'shadow_heater_wh_per_m') and hasattr(self.base_model, 'wh_per_meter'):
                # Handle StaticBatteryModel directly
                base = self.base_model.wh_per_meter * distance_m
                heater = self.base_model.shadow_heater_wh_per_m * distance_m * self.thermal_scale
                slope_extra = self.base_model.slope_factor * cell.slope_deg * distance_m
                drain = base + heater + slope_extra
            else:
                # Apply heuristic scaling to ML or other models
                drain = drain * self.thermal_scale
                
        return max(0.001, drain * self.multiplier)


class FaultyGrid:
    """
    Wraps PolarGrid to dynamically modify cell states during A* search,
    simulating obstacles, sensor degradation (terrain blindness), and solar unavailabilities.
    """
    def __init__(
        self,
        base_grid: PolarGrid,
        obstacle_cell: Optional[Tuple[int, int]] = None,
        obstacle_radius: float = 0.0,
        slope_multiplier: float = 1.0,
        shadow_is_hazard: bool = False,
        unavailable_pitstop: Optional[Tuple[int, int]] = None
    ) -> None:
        self.base_grid = base_grid
        self.obstacle_cell = obstacle_cell
        self.obstacle_radius = obstacle_radius
        self.slope_multiplier = slope_multiplier
        self.shadow_is_hazard = shadow_is_hazard
        self.unavailable_pitstop = unavailable_pitstop

    def __getattr__(self, name):
        return getattr(self.base_grid, name)

    def get_cell(self, row: int, col: int) -> GridCell:
        cell = self.base_grid.get_cell(row, col)
        
        is_hazard = cell.is_hazard
        slope_deg = cell.slope_deg
        is_shadowed = cell.is_shadowed
        solar_illumination = cell.solar_illumination
        
        modified = False
        
        # Newly detected obstacle:
        if self.obstacle_cell is not None:
            orow, ocol = self.obstacle_cell
            if math.hypot(row - orow, col - ocol) <= self.obstacle_radius:
                is_hazard = True
                modified = True
                
        # Solar charging site unavailable:
        if self.unavailable_pitstop is not None:
            prow, pcol = self.unavailable_pitstop
            if row == prow and col == pcol:
                is_hazard = True
                solar_illumination = 0.0
                modified = True
                
        # Sensor degradation:
        if self.slope_multiplier != 1.0:
            slope_deg = min(15.0, cell.slope_deg * self.slope_multiplier)
            modified = True
            
        if self.shadow_is_hazard and cell.is_shadowed:
            is_hazard = True
            modified = True
            
        if modified:
            return GridCell(
                row=cell.row,
                col=cell.col,
                lat=cell.lat,
                lon=cell.lon,
                base_traversal_cost=cell.base_traversal_cost,
                is_hazard=is_hazard,
                is_shadowed=is_shadowed,
                solar_illumination=solar_illumination,
                temperature_k=cell.temperature_k,
                slope_deg=slope_deg,
                ice_volume_m3=cell.ice_volume_m3,
                ice_confidence=cell.ice_confidence
            )
            
        return cell


def find_nearest_comm_cell(
    grid: PolarGrid,
    start_row: int,
    start_col: int
) -> Optional[Tuple[int, int]]:
    """
    Finds the nearest non-hazard cell with clear Line-of-Sight visibility
    to re-establish comm link (defined as los_fraction >= 0.8).
    """
    best_cell = None
    best_dist = math.inf
    
    for r in range(grid.rows):
        for c in range(grid.cols):
            cell = grid.get_cell(r, c)
            if cell.is_hazard:
                continue
            # Query line of sight visibility
            vis = compute_comm_visibility(cell.lat, cell.lon, grid)
            if vis.los_fraction >= 0.8:
                dist = math.hypot(r - start_row, c - start_col)
                if dist < best_dist:
                    best_dist = dist
                    best_cell = (r, c)
                    
    return best_cell
