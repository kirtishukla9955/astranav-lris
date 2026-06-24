import numpy as np
from typing import List, Tuple, Dict
from schemas import HazardMask

class CostGrid:
    def __init__(self, width: int, height: int, resolution_m: float = 1.0):
        self.width = width
        self.height = height
        self.resolution_m = resolution_m
        # Base cost is 1.0 per grid cell
        self.grid = np.ones((height, width), dtype=np.float32)
        # Shadow map tracks which cells are in permanent/current shadow
        self.shadow_map = np.zeros((height, width), dtype=bool)
        
    def apply_hazard_mask(self, mask: HazardMask, grid_x: int, grid_y: int):
        """
        Applies a hazard mask (slope and obstacle data) to a specific grid coordinate.
        This converts Member 1's hazard detection into routing costs.
        """
        if not (0 <= grid_x < self.width and 0 <= grid_y < self.height):
            return
            
        # Obstacles make the cell impassable (infinite cost)
        if mask.is_obstacle:
            self.grid[grid_y, grid_x] = np.inf
        else:
            # Slopes > 30 deg make it impassable for most rovers
            if mask.slope_deg > 30:
                self.grid[grid_y, grid_x] = np.inf
            else:
                # Add slope penalty (e.g., 1 + slope^2 / 100)
                # A 15 deg slope adds 2.25 cost multiplier
                slope_penalty = (mask.slope_deg ** 2) / 100.0
                self.grid[grid_y, grid_x] += slope_penalty
                
    def apply_shadow_map(self, shadow_data: np.ndarray):
        """
        Applies a boolean shadow map to track dark regions.
        shadow_data: boolean array of shape (height, width). True = in shadow.
        """
        assert shadow_data.shape == (self.height, self.width)
        self.shadow_map = shadow_data
        
    def get_traversal_cost(self, x: int, y: int) -> float:
        """
        Gets the base traversal cost. Shadow penalty is handled separately in pathfinder
        because we need to track cumulative dark-dwell time.
        """
        if not (0 <= x < self.width and 0 <= y < self.height):
            return np.inf
        return float(self.grid[y, x])
        
    def is_in_shadow(self, x: int, y: int) -> bool:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return False
        return bool(self.shadow_map[y, x])

def generate_mock_cost_grid(width: int = 100, height: int = 100) -> CostGrid:
    """
    MOCK DATA: Generates a mock cost grid with random hazards and shadows.
    Replace this with real map data ingestion in production.
    """
    grid = CostGrid(width, height)
    
    # Generate random shadow map (e.g., roughly 20% in shadow)
    shadow_map = np.random.rand(height, width) > 0.8
    grid.apply_shadow_map(shadow_map)
    
    # Generate random hazards
    for y in range(height):
        for x in range(width):
            # 5% chance of hard obstacle (boulder/wall)
            is_obs = np.random.rand() < 0.05
            # Random slope 0-35 degrees
            slope = np.random.rand() * 35.0
            
            mask = HazardMask(lat=0.0, lon=0.0, slope_deg=slope, is_obstacle=is_obs)
            grid.apply_hazard_mask(mask, x, y)
            
    return grid
