from typing import List, Dict
from schemas import CompareRequest, CompareResponse, LMRSResponse
from lmrs import compute_lmrs
from cost_grid import CostGrid

def compare_sites(
    points: List[Dict[str, float]], 
    start_lat: float, 
    start_lon: float, 
    region_id: str, 
    grid: CostGrid
) -> CompareResponse:
    """
    Evaluates multiple sites and recommends the best one based on LMRS.
    """
    comparisons = []
    
    for pt in points:
        target_lat = pt.get("lat", 0.0)
        target_lon = pt.get("lon", 0.0)
        
        lmrs_resp = compute_lmrs(
            target_lat=target_lat,
            target_lon=target_lon,
            start_lat=start_lat,
            start_lon=start_lon,
            region_id=region_id,
            grid=grid
        )
        comparisons.append(lmrs_resp)
        
    # Find the best index (highest LMRS score)
    recommended_index = -1
    highest_score = -1.0
    
    for idx, resp in enumerate(comparisons):
        if resp.lmrs_score > highest_score:
            highest_score = resp.lmrs_score
            recommended_index = idx
            
    # Default to first if none found
    if recommended_index == -1 and comparisons:
        recommended_index = 0
            
    return CompareResponse(
        comparisons=comparisons,
        recommended_index=recommended_index
    )
