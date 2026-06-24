import heapq
import math
import uuid
from typing import List, Tuple, Optional
from schemas import Waypoint, RouteResponse
from cost_grid import CostGrid
from battery_model import predict_energy_wh

# Default rover speed in meters per second
ROVER_SPEED_MPS = 0.05 
# 30 minutes in seconds
MAX_DARK_DWELL_TIME_S = 30 * 60 

def heuristic(a: Tuple[int, int], b: Tuple[int, int]) -> float:
    # Euclidean distance
    return math.sqrt((a[0] - b[0])**2 + (a[1] - b[1])**2)

def a_star_search(grid: CostGrid, start: Tuple[int, int], goal: Tuple[int, int]) -> Optional[List[Tuple[int, int]]]:
    # Check if start or goal is impassable
    if grid.get_traversal_cost(start[0], start[1]) == float('inf') or grid.get_traversal_cost(goal[0], goal[1]) == float('inf'):
        return None

    frontier = []
    heapq.heappush(frontier, (0, start))
    
    came_from = {}
    cost_so_far = {}
    
    came_from[start] = None
    cost_so_far[start] = 0.0
    
    while frontier:
        _, current = heapq.heappop(frontier)
        
        if current == goal:
            break
            
        for dx, dy in [(0, 1), (1, 0), (0, -1), (-1, 0), (1, 1), (-1, 1), (1, -1), (-1, -1)]:
            next_node = (current[0] + dx, current[1] + dy)
            if not (0 <= next_node[0] < grid.width and 0 <= next_node[1] < grid.height):
                continue
                
            step_cost = grid.get_traversal_cost(next_node[0], next_node[1])
            if step_cost == float('inf'):
                continue
                
            # Distance is 1 for cardinal, 1.414 for diagonal
            dist = math.sqrt(dx**2 + dy**2)
            
            # Shadow penalty as required (25K additional cost multiplier)
            is_shadow = grid.is_in_shadow(next_node[0], next_node[1])
            if is_shadow:
                step_cost *= 25000.0 # Heavy darkness penalty
                
            new_cost = cost_so_far[current] + step_cost * dist
            
            if next_node not in cost_so_far or new_cost < cost_so_far[next_node]:
                cost_so_far[next_node] = new_cost
                priority = new_cost + heuristic(next_node, goal)
                heapq.heappush(frontier, (priority, next_node))
                came_from[next_node] = current
                
    if goal not in came_from:
        return None
        
    # Reconstruct path
    path = []
    current = goal
    while current is not None:
        path.append(current)
        current = came_from[current]
    path.reverse()
    return path

def find_nearest_sunlit_cell(grid: CostGrid, x: int, y: int) -> Tuple[int, int]:
    # BFS to find nearest sunlit cell
    queue = [(x, y)]
    visited = set([(x, y)])
    
    while queue:
        curr_x, curr_y = queue.pop(0)
        if not grid.is_in_shadow(curr_x, curr_y) and grid.get_traversal_cost(curr_x, curr_y) != float('inf'):
            return (curr_x, curr_y)
            
        for dx, dy in [(0, 1), (1, 0), (0, -1), (-1, 0)]:
            nx, ny = curr_x + dx, curr_y + dy
            if 0 <= nx < grid.width and 0 <= ny < grid.height and (nx, ny) not in visited:
                visited.add((nx, ny))
                queue.append((nx, ny))
    return (x, y) # Fallback

def build_route(grid: CostGrid, start: Tuple[int, int], goal: Tuple[int, int], region_id: str) -> Optional[RouteResponse]:
    path = a_star_search(grid, start, goal)
    if not path:
        return None
        
    # Process path for pitstops and battery usage
    final_path = []
    i = 0
    dark_dwell_time = 0.0
    
    while i < len(path):
        current = path[i]
        final_path.append((current, "transit"))
        
        is_shadow = grid.is_in_shadow(current[0], current[1])
        
        if i > 0:
            prev = path[i-1]
            dist_m = heuristic(prev, current) * grid.resolution_m
            
            # Update dark dwell time
            if is_shadow:
                dark_dwell_time += dist_m / ROVER_SPEED_MPS
            else:
                dark_dwell_time = 0.0
                
            # Auto-insert Solar Charging Pitstop
            if dark_dwell_time > MAX_DARK_DWELL_TIME_S:
                pitstop = find_nearest_sunlit_cell(grid, current[0], current[1])
                final_path.append((pitstop, "solar_pitstop"))
                dark_dwell_time = 0.0 # reset after charging
                
                # Recalculate route from pitstop to goal
                new_path = a_star_search(grid, pitstop, goal)
                if new_path:
                    # Replace remaining path
                    path = path[:i+1] + new_path
                
        i += 1

    # Convert final_path to waypoints
    waypoints = []
    cum_dist = 0.0
    cum_energy = 0.0
    
    for idx, (node, wpt_type) in enumerate(final_path):
        is_shadow = grid.is_in_shadow(node[0], node[1])
        temp_k = 50.0 if is_shadow else 300.0
        
        # approximate slope from traversal cost
        cost = grid.get_traversal_cost(node[0], node[1])
        slope_deg = math.sqrt(max(0, (cost - 1.0) * 100.0))
        
        dist_m = 0.0
        if idx > 0:
            prev_node = final_path[idx-1][0]
            dist_m = heuristic(prev_node, node) * grid.resolution_m
            cum_dist += dist_m
            
            # Predict energy using Feature 8 model
            energy_wh = predict_energy_wh(temp_k, is_shadow, slope_deg, ROVER_SPEED_MPS, dist_m)
            cum_energy += energy_wh
            
        wpt = Waypoint(
            lat=node[1] * 0.0001, # Mock lat/lon conversion based on grid coords
            lon=node[0] * 0.0001,
            type=wpt_type,
            cumulative_distance_m=cum_dist,
            cumulative_energy_wh=cum_energy,
            is_in_shadow=is_shadow,
            confidence=0.9 # Placeholder, will be populated by Feature 9
        )
        waypoints.append(wpt)
        
    return RouteResponse(
        route_id=str(uuid.uuid4()),
        region_id=region_id,
        waypoints=waypoints,
        total_distance_m=cum_dist,
        total_energy_wh=cum_energy
    )
