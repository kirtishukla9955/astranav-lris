"""
pathfinder/astar.py
-------------------
Pure A* path-finding over a PolarGrid.

⚠️  ZERO FastAPI imports.  Zero Pydantic imports.
This file is the hot-swap boundary: replace it with a Rust extension module
or a Numba-jitted function and the rest of the codebase is untouched.

Public API
----------
astar(grid, start, goal, config) -> list[tuple[int,int]] | None

Algorithm
---------
Standard A* with a binary heap (heapq).  Tie-breaking via a monotone counter
prevents heapq from comparing GridCell objects on equal f-scores.

8-connected movement:
  Cardinal  steps cost  1.0 × distance_m / cell_size
  Diagonal  steps cost  √2 × distance_m / cell_size (naturally handled by
                        the Euclidean distance passed to traversal_cost)

Stretch upgrade path:
  Replace this module with D* Lite (dynamic re-planning) for the live demo
  obstacle-avoidance feature without changing astar()'s signature.
"""

from __future__ import annotations

import heapq
import math
from typing import Optional

from .cost import CostConfig, octile_heuristic, traversal_cost
from .grid import PolarGrid


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def astar(
    grid: PolarGrid,
    start: tuple[int, int],
    goal: tuple[int, int],
    config: CostConfig,
) -> Optional[list[tuple[int, int]]]:
    """
    Find the lowest-cost path from ``start`` to ``goal`` on ``grid``.

    Parameters
    ----------
    grid   : PolarGrid — the crater region cell map.
    start  : (row, col) of the rover's current position.
    goal   : (row, col) of the mission target.
    config : CostConfig — weights, mode flags, and battery model.

    Returns
    -------
    list[tuple[int, int]]
        Ordered cell path from start (inclusive) to goal (inclusive),
        or ``None`` if:
        - start or goal is inside a hazard polygon, OR
        - the grid is fully blocked (no path exists).
    """
    # ── Pre-flight checks ────────────────────────────────────────────────────
    if not grid.in_bounds(*start) or not grid.in_bounds(*goal):
        return None

    if grid.get_cell(*start).is_hazard:
        return None

    if grid.get_cell(*goal).is_hazard:
        return None

    if start == goal:
        return [start]

    # ── Data structures ──────────────────────────────────────────────────────
    # open_set entries: (f_score, tie_counter, (row, col))
    # The tie_counter avoids heapq comparing tuples on equal f-scores.
    _counter = 0
    open_heap: list[tuple[float, int, tuple[int, int]]] = []
    heapq.heappush(open_heap, (0.0, _counter, start))

    # came_from[node] = node we came from (None for start)
    came_from: dict[tuple[int, int], Optional[tuple[int, int]]] = {start: None}

    # g_score[node] = exact lowest cost from start to node
    g_score: dict[tuple[int, int], float] = {start: 0.0}

    cell_size = grid.cell_size_m
    goal_row, goal_col = goal

    # ── Main loop ────────────────────────────────────────────────────────────
    while open_heap:
        _, _, current = heapq.heappop(open_heap)
        curr_row, curr_col = current

        # Goal reached — reconstruct and return
        if current == goal:
            return _reconstruct_path(came_from, goal)

        curr_cell = grid.get_cell(curr_row, curr_col)
        current_g = g_score[current]

        for nb_row, nb_col in grid.neighbors(curr_row, curr_col):
            nb_cell = grid.get_cell(nb_row, nb_col)

            # Hard block — skip immediately (avoids even computing cost)
            if nb_cell.is_hazard:
                continue

            # Euclidean step distance (cardinal = cell_size, diagonal = √2 * cell_size)
            dx = abs(nb_col - curr_col)
            dy = abs(nb_row - curr_row)
            dist_m = cell_size * math.sqrt(dx * dx + dy * dy)

            step_cost = traversal_cost(curr_cell, nb_cell, dist_m, config)
            tentative_g = current_g + step_cost

            neighbor = (nb_row, nb_col)
            if tentative_g < g_score.get(neighbor, math.inf):
                came_from[neighbor] = current
                g_score[neighbor] = tentative_g
                h = octile_heuristic(nb_row, nb_col, goal_row, goal_col, cell_size)
                f_score = tentative_g + h
                _counter += 1
                heapq.heappush(open_heap, (f_score, _counter, neighbor))

    # Exhausted open set — no path exists
    return None


# ---------------------------------------------------------------------------
# Path reconstruction
# ---------------------------------------------------------------------------

def _reconstruct_path(
    came_from: dict[tuple[int, int], Optional[tuple[int, int]]],
    goal: tuple[int, int],
) -> list[tuple[int, int]]:
    """Walk came_from backwards from goal to start, then reverse."""
    path: list[tuple[int, int]] = []
    node: Optional[tuple[int, int]] = goal
    while node is not None:
        path.append(node)
        node = came_from[node]
    path.reverse()
    return path
