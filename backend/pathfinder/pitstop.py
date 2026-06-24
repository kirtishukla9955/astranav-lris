"""
pathfinder/pitstop.py
---------------------
Post-processes a raw A* cell path into typed Waypoints with:

1. Running energy accounting (per-cell battery drain via the battery model)
2. Automatic solar pitstop insertion when the dark-dwell budget is about
   to be exceeded:
   - Uses BFS to find the nearest sunlit rim cell from the rover's current
     position on the path.
   - Recharges the simulated battery by ``solar_charge_rate_wh``.
   - Inserts a WaypointType.SOLAR_PITSTOP entry before the next transit cell.

3. All numeric outputs are rounded to keep JSON payloads clean.

Science rules enforced here:
- Battery can never go below 0 Wh (rover stalls — recorded in warnings).
- dark_dwell_wh resets to 0 whenever the rover enters a sunlit cell.
- Solar pitstops are placed at the actual nearest sunlit cell, NOT
  on the A* path itself (they may be a short detour).
"""

from __future__ import annotations

import math
from collections import deque
from typing import Optional

from .cost import CostConfig
from .grid import PolarGrid
from .types import RouteResult, Waypoint, WaypointType

# Default rover battery specs (can be overridden via API params)
DEFAULT_BATTERY_CAPACITY_WH: float = 200.0
DEFAULT_SOLAR_CHARGE_RATE_WH: float = 50.0   # Wh restored per pitstop event


# ---------------------------------------------------------------------------
# BFS: nearest sunlit cell
# ---------------------------------------------------------------------------

def _nearest_sunlit_cell(
    grid: PolarGrid,
    from_row: int,
    from_col: int,
) -> Optional[tuple[int, int]]:
    """
    BFS from (from_row, from_col) to find the closest non-hazard cell
    with solar_illumination > 0.

    Returns
    -------
    (row, col) of the nearest sunlit rim cell, or None if none reachable.
    """
    visited: set[tuple[int, int]] = set()
    queue: deque[tuple[int, int]] = deque()
    queue.append((from_row, from_col))
    visited.add((from_row, from_col))

    while queue:
        r, c = queue.popleft()
        cell = grid.get_cell(r, c)
        if not cell.is_hazard and cell.solar_illumination > 0.0:
            return (r, c)
        for nr, nc in grid.neighbors(r, c):
            if (nr, nc) not in visited:
                visited.add((nr, nc))
                queue.append((nr, nc))

    return None  # no reachable sunlit cell in the entire grid


# ---------------------------------------------------------------------------
# Main route builder
# ---------------------------------------------------------------------------

def build_route(
    grid: PolarGrid,
    cell_path: list[tuple[int, int]],
    config: CostConfig,
    initial_battery_pct: float = 100.0,
    dark_budget_wh: float = 80.0,
    battery_capacity_wh: float = DEFAULT_BATTERY_CAPACITY_WH,
    solar_charge_rate_wh: float = DEFAULT_SOLAR_CHARGE_RATE_WH,
) -> RouteResult:
    """
    Convert a raw cell-path (from astar()) into a typed RouteResult with
    running energy accounting and auto-inserted solar pitstops.

    Parameters
    ----------
    grid               : the same PolarGrid used for path-finding.
    cell_path          : ordered list of (row, col) from astar().
    config             : CostConfig including battery_model.
    initial_battery_pct: rover's charge at mission start (0–100).
    dark_budget_wh     : max Wh that can be spent in shadow before a
                         forced pitstop is triggered.
    battery_capacity_wh: total rover battery capacity in Wh.
    solar_charge_rate_wh: Wh restored during one pitstop event.

    Returns
    -------
    RouteResult with waypoints, totals, and any warnings.
    """
    if not cell_path:
        return RouteResult(
            route_found=False,
            waypoints=[],
            total_distance_m=0.0,
            total_energy_wh=0.0,
            total_pitstops=0,
            total_waypoints=0,
            warnings=["empty_path_received"],
        )

    waypoints: list[Waypoint] = []
    warnings: list[str] = []

    # Running accumulators
    battery_wh: float = (initial_battery_pct / 100.0) * battery_capacity_wh
    cumulative_energy_wh: float = 0.0
    cumulative_distance_m: float = 0.0
    dark_dwell_wh: float = 0.0   # resets on reaching a sunlit cell
    pitstop_count: int = 0
    cell_size = grid.cell_size_m

    # ── Emit the start cell as the first waypoint ────────────────────────────
    start_cell = grid.get_cell(*cell_path[0])
    waypoints.append(
        _make_waypoint(
            cell=start_cell,
            wtype=WaypointType.TRANSIT,
            cum_dist=0.0,
            cum_energy=0.0,
            battery_pct=(battery_wh / battery_capacity_wh) * 100.0,
        )
    )

    # ── Walk every subsequent cell ───────────────────────────────────────────
    for i in range(1, len(cell_path)):
        prev_row, prev_col = cell_path[i - 1]
        curr_row, curr_col = cell_path[i]
        curr_cell = grid.get_cell(curr_row, curr_col)

        # Step distance (cardinal vs diagonal)
        dx = abs(curr_col - prev_col)
        dy = abs(curr_row - prev_row)
        step_dist_m = cell_size * math.sqrt(dx * dx + dy * dy)

        # Energy drain for this step
        battery_pct_now = (battery_wh / battery_capacity_wh) * 100.0
        drain_wh = config.battery_model.predict_drain_wh(
            curr_cell, step_dist_m, battery_pct_now
        )

        # ── Dark budget check ────────────────────────────────────────────────
        # If this cell is shadowed, would entering it exceed the dark budget?
        if curr_cell.is_shadowed:
            projected_dark_dwell = dark_dwell_wh + drain_wh
            if projected_dark_dwell > dark_budget_wh:
                # Find nearest sunlit cell from current rover position
                sunlit_pos = _nearest_sunlit_cell(grid, prev_row, prev_col)
                if sunlit_pos is not None:
                    sun_cell = grid.get_cell(*sunlit_pos)
                    recharged = min(solar_charge_rate_wh, battery_capacity_wh - battery_wh)
                    battery_wh += recharged
                    dark_dwell_wh = 0.0
                    pitstop_count += 1
                    battery_pct_after_charge = (battery_wh / battery_capacity_wh) * 100.0
                    waypoints.append(
                        _make_waypoint(
                            cell=sun_cell,
                            wtype=WaypointType.SOLAR_PITSTOP,
                            cum_dist=round(cumulative_distance_m, 2),
                            cum_energy=round(cumulative_energy_wh, 3),
                            battery_pct=round(battery_pct_after_charge, 2),
                        )
                    )
                else:
                    warnings.append(
                        f"dark_budget_exceeded_at_step_{i}_no_sunlit_cell_found"
                    )

        # Reset dark dwell counter on sunlit cells
        if not curr_cell.is_shadowed:
            dark_dwell_wh = 0.0
        else:
            dark_dwell_wh += drain_wh

        # Apply battery drain (floor at 0 — rover cannot go negative)
        battery_wh = max(0.0, battery_wh - drain_wh)
        if battery_wh == 0.0 and drain_wh > 0.0:
            warnings.append(f"battery_depleted_at_step_{i}")

        cumulative_distance_m += step_dist_m
        cumulative_energy_wh += drain_wh

        battery_pct_remaining = (battery_wh / battery_capacity_wh) * 100.0

        waypoints.append(
            _make_waypoint(
                cell=curr_cell,
                wtype=WaypointType.TRANSIT,
                cum_dist=round(cumulative_distance_m, 2),
                cum_energy=round(cumulative_energy_wh, 3),
                battery_pct=round(battery_pct_remaining, 2),
            )
        )

    return RouteResult(
        route_found=True,
        waypoints=waypoints,
        total_distance_m=round(cumulative_distance_m, 2),
        total_energy_wh=round(cumulative_energy_wh, 3),
        total_pitstops=pitstop_count,
        total_waypoints=len(waypoints),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _make_waypoint(
    cell: "PolarGrid",   # actually GridCell — avoid circular import type hint
    wtype: WaypointType,
    cum_dist: float,
    cum_energy: float,
    battery_pct: float,
) -> Waypoint:
    return Waypoint(
        lat=cell.lat,
        lon=cell.lon,
        type=wtype,
        cumulative_distance_m=cum_dist,
        cumulative_energy_wh=cum_energy,
        battery_pct_remaining=round(battery_pct, 2),
        is_shadowed=cell.is_shadowed,
        solar_illumination=cell.solar_illumination,
    )
