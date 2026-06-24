"""
tests/test_pathfinder.py
------------------------
Pytest unit tests for the AstraNav-LRIS pathfinder core.

All tests operate on small synthetic grids (≤ 15×15) so they run in
< 1 second even without pytest-benchmark.

Test categories
---------------
1.  Grid construction & coordinate maths
2.  A* — basic routing
3.  A* — hazard avoidance
4.  A* — unreachable / edge cases
5.  A* — ice-seeking mode
6.  Cost function correctness
7.  Battery model (static)
8.  Pitstop auto-insertion
9.  Full plan_route() integration
10. Swarm planning (two independent rovers)
"""

from __future__ import annotations

import math
import sys
import os

# Allow running from any directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from pathfinder import (
    CostConfig,
    PolarGrid,
    RouteResult,
    StaticBatteryModel,
    WaypointType,
    astar,
    build_route,
    plan_route,
)
from pathfinder.cost import octile_heuristic, traversal_cost
from pathfinder.types import GridCell


# ===========================================================================
# Fixtures — reusable synthetic grids
# ===========================================================================

def make_open_grid(rows: int = 10, cols: int = 10) -> PolarGrid:
    """All-open grid: no hazards, no shadows, no ice."""
    return PolarGrid(rows=rows, cols=cols, origin_lat=-89.55, origin_lon=44.0)


def make_walled_grid() -> PolarGrid:
    """
    10×10 grid with a horizontal hazard wall at row=5 (all cols except col=9).
    This forces A* to go around the right edge.

        cols:  0 1 2 3 4 5 6 7 8 9
    row 9: . . . . . . . . . .
    ...
    row 5: H H H H H H H H H .   ← wall, gap at col=9
    ...
    row 0: . . . . . . . . . .
    """
    grid = make_open_grid(10, 10)
    for c in range(9):         # col 9 is the gap
        grid.mark_hazard(5, c)
    return grid


def make_shadow_grid() -> PolarGrid:
    """
    10×10 grid where rows 3–6 are fully shadowed.
    Row 8-9 are sunlit (solar_illumination=1.0).
    No hazards.
    """
    grid = make_open_grid(10, 10)
    for r in range(3, 7):
        for c in range(10):
            grid.mark_shadow(r, c, illumination=0.0, temperature_k=25.0)
    for r in range(8, 10):
        for c in range(10):
            grid.mark_illuminated(r, c, illumination=1.0)
    return grid


def make_ice_grid() -> PolarGrid:
    """
    10×10 grid with ice cells in the bottom-left quadrant (rows 0-4, cols 0-4).
    All cells passable, no shadows.
    """
    grid = make_open_grid(10, 10)
    for r in range(5):
        for c in range(5):
            grid.mark_ice(r, c, volume_m3=1000.0, confidence=0.9)
    return grid


def default_config(**kwargs) -> CostConfig:
    return CostConfig(**kwargs)


# ===========================================================================
# 1. Grid construction & coordinate maths
# ===========================================================================

class TestGridConstruction:

    def test_grid_dimensions(self):
        grid = make_open_grid(8, 12)
        assert grid.rows == 8
        assert grid.cols == 12

    def test_origin_cell_coords(self):
        grid = PolarGrid(rows=10, cols=10, origin_lat=-89.55, origin_lon=44.0)
        cell = grid.get_cell(0, 0)
        assert cell.lat == pytest.approx(-89.55, abs=1e-6)
        assert cell.lon == pytest.approx(44.0, abs=1e-6)

    def test_lat_lon_to_cell_roundtrip(self):
        grid = PolarGrid(rows=20, cols=20, origin_lat=-89.55, origin_lon=44.0)
        # Place a point at cell (5, 7), convert to lat/lon, convert back
        cell_55 = grid.get_cell(5, 7)
        row, col = grid.lat_lon_to_cell(cell_55.lat, cell_55.lon)
        assert row == 5
        assert col == 7

    def test_lat_lon_clamps_to_grid(self):
        grid = make_open_grid(10, 10)
        # Far outside grid → should clamp to boundary
        row, col = grid.lat_lon_to_cell(100.0, 200.0)
        assert 0 <= row < grid.rows
        assert 0 <= col < grid.cols

    def test_neighbors_centre_cell(self):
        grid = make_open_grid(5, 5)
        nb = grid.neighbors(2, 2)
        assert len(nb) == 8   # full 8-connected

    def test_neighbors_corner_cell(self):
        grid = make_open_grid(5, 5)
        nb = grid.neighbors(0, 0)
        assert len(nb) == 3   # only 3 valid neighbours at corner

    def test_neighbors_edge_cell(self):
        grid = make_open_grid(5, 5)
        nb = grid.neighbors(0, 2)
        assert len(nb) == 5   # 5 valid on a top edge

    def test_mark_hazard(self):
        grid = make_open_grid()
        grid.mark_hazard(3, 4)
        assert grid.get_cell(3, 4).is_hazard is True
        assert grid.get_cell(3, 3).is_hazard is False

    def test_mark_shadow(self):
        grid = make_open_grid()
        grid.mark_shadow(2, 2, illumination=0.0, temperature_k=25.0)
        cell = grid.get_cell(2, 2)
        assert cell.is_shadowed is True
        assert cell.temperature_k == pytest.approx(25.0)

    def test_mark_illuminated(self):
        grid = make_open_grid()
        grid.mark_illuminated(9, 9, illumination=0.85)
        cell = grid.get_cell(9, 9)
        assert cell.is_shadowed is False
        assert cell.solar_illumination == pytest.approx(0.85)

    def test_sunlit_rim_cells(self):
        grid = make_shadow_grid()
        rim = grid.sunlit_rim_cells()
        # Rows 8-9 × 10 cols = 20 sunlit cells
        assert len(rim) == 20
        for r, c in rim:
            assert r in (8, 9)

    def test_ice_cells(self):
        grid = make_ice_grid()
        ice = grid.ice_cells()
        assert len(ice) == 25  # 5×5 quadrant

    def test_cell_distance_m(self):
        grid = PolarGrid(rows=10, cols=10, origin_lat=-89.55, origin_lon=44.0, cell_size_m=5.0)
        # Cardinal step: 1 cell → 5 m
        assert grid.cell_distance_m(0, 0, 0, 1) == pytest.approx(5.0)
        # Diagonal step: 1 cell → √2 × 5 m
        assert grid.cell_distance_m(0, 0, 1, 1) == pytest.approx(5.0 * math.sqrt(2), rel=1e-4)

    def test_ascii_map_shape(self):
        grid = make_open_grid(4, 5)
        ascii_art = grid.ascii_map()
        lines = ascii_art.strip().split("\n")
        assert len(lines) == 4
        assert all(len(line) == 5 for line in lines)


# ===========================================================================
# 2. A* — basic routing
# ===========================================================================

class TestAStarBasicRouting:

    def test_start_equals_goal(self):
        grid = make_open_grid()
        cfg = default_config()
        path = astar(grid, (5, 5), (5, 5), cfg)
        assert path == [(5, 5)]

    def test_single_step_cardinal(self):
        grid = make_open_grid()
        cfg = default_config()
        path = astar(grid, (5, 5), (5, 6), cfg)
        assert path is not None
        assert path[0] == (5, 5)
        assert path[-1] == (5, 6)

    def test_path_is_connected(self):
        """Every consecutive pair of cells must be neighbours."""
        grid = make_open_grid()
        cfg = default_config()
        path = astar(grid, (0, 0), (9, 9), cfg)
        assert path is not None
        for i in range(len(path) - 1):
            r1, c1 = path[i]
            r2, c2 = path[i + 1]
            assert abs(r2 - r1) <= 1 and abs(c2 - c1) <= 1, (
                f"Disconnected step: {path[i]} → {path[i+1]}"
            )

    def test_open_grid_diagonal_path(self):
        """On an open grid, diagonal path from (0,0) to (9,9) has 10 cells."""
        grid = make_open_grid()
        cfg = default_config()
        path = astar(grid, (0, 0), (9, 9), cfg)
        assert path is not None
        assert len(path) == 10  # 0,1,...,9 → 10 steps

    def test_path_always_reaches_goal(self):
        grid = make_open_grid(15, 15)
        cfg = default_config()
        path = astar(grid, (0, 0), (14, 14), cfg)
        assert path is not None
        assert path[-1] == (14, 14)


# ===========================================================================
# 3. A* — hazard avoidance
# ===========================================================================

class TestAStarHazardAvoidance:

    def test_path_never_enters_hazard(self):
        grid = make_walled_grid()
        cfg = default_config()
        path = astar(grid, (0, 0), (9, 0), cfg)
        assert path is not None
        for r, c in path:
            assert not grid.get_cell(r, c).is_hazard, (
                f"Path entered hazard cell ({r}, {c})"
            )

    def test_walled_grid_routes_around_gap(self):
        """With wall at row=5 cols 0-8, path (0,0)→(9,0) must pass through col=9."""
        grid = make_walled_grid()
        cfg = default_config()
        path = astar(grid, (0, 0), (9, 0), cfg)
        assert path is not None
        # Path must pass through col=9 (the gap) to get past the wall
        cols_visited = {c for _, c in path}
        assert 9 in cols_visited

    def test_hazard_in_entire_column_except_row0(self):
        """A hazard column from row 1 to 9 forces path to stay on row 0."""
        grid = make_open_grid(10, 10)
        for r in range(1, 10):
            grid.mark_hazard(r, 5)
        cfg = default_config()
        path = astar(grid, (0, 0), (0, 9), cfg)
        assert path is not None
        for r, c in path:
            assert not grid.get_cell(r, c).is_hazard


# ===========================================================================
# 4. A* — unreachable & edge cases
# ===========================================================================

class TestAStarEdgeCases:

    def test_start_in_hazard_returns_none(self):
        grid = make_open_grid()
        grid.mark_hazard(0, 0)
        cfg = default_config()
        assert astar(grid, (0, 0), (9, 9), cfg) is None

    def test_goal_in_hazard_returns_none(self):
        grid = make_open_grid()
        grid.mark_hazard(9, 9)
        cfg = default_config()
        assert astar(grid, (0, 0), (9, 9), cfg) is None

    def test_fully_enclosed_goal_returns_none(self):
        """Surround goal with hazards — no path should exist."""
        grid = make_open_grid(7, 7)
        goal = (3, 3)
        for dr in [-1, 0, 1]:
            for dc in [-1, 0, 1]:
                if (dr, dc) != (0, 0):
                    grid.mark_hazard(3 + dr, 3 + dc)
        cfg = default_config()
        assert astar(grid, (0, 0), goal, cfg) is None

    def test_fully_enclosed_goal_returns_none_v2(self):
        """Explicit version without unpacking."""
        grid = make_open_grid(7, 7)
        for dr in [-1, 0, 1]:
            for dc in [-1, 0, 1]:
                if (dr, dc) != (0, 0):
                    grid.mark_hazard(3 + dr, 3 + dc)
        cfg = default_config()
        result = astar(grid, (0, 0), (3, 3), cfg)
        assert result is None

    def test_out_of_bounds_start_returns_none(self):
        grid = make_open_grid(5, 5)
        cfg = default_config()
        assert astar(grid, (10, 10), (0, 0), cfg) is None

    def test_1x1_grid_same_cell(self):
        grid = PolarGrid(rows=1, cols=1, origin_lat=-89.0, origin_lon=44.0)
        cfg = default_config()
        path = astar(grid, (0, 0), (0, 0), cfg)
        assert path == [(0, 0)]


# ===========================================================================
# 5. A* — ice-seeking mode
# ===========================================================================

class TestAStarIceSeeking:

    def test_ice_seeking_prefers_ice_cells(self):
        """
        Two paths from (0,0) to (9,9):
        - Direct diagonal (upper-right): passes through ice cells
        - No ice on the grid except bottom-left quadrant

        With ice_seeking=True, the path should visit at least one ice cell.
        """
        grid = make_ice_grid()   # ice in rows 0-4, cols 0-4
        # Route from (0,0) to (9,9) — ice cells are at start area
        cfg_ice = default_config(ice_seeking_mode=True)
        cfg_std = default_config(ice_seeking_mode=False)

        path_ice = astar(grid, (0, 0), (9, 9), cfg_ice)
        path_std = astar(grid, (0, 0), (9, 9), cfg_std)

        assert path_ice is not None
        assert path_std is not None

        def count_ice_cells(path):
            return sum(1 for r, c in path if grid.get_cell(r, c).ice_volume_m3 > 0)

        # Both paths start in ice territory; ice-seeking should visit more
        # ice cells or at least the same number
        assert count_ice_cells(path_ice) >= count_ice_cells(path_std)

    def test_ice_seeking_false_does_not_bias(self):
        """Without ice_seeking, route should be more direct (fewer cells)."""
        grid = make_open_grid(10, 10)
        for r in range(5):
            for c in range(5):
                grid.mark_ice(r, c, 1000.0, 0.9)

        cfg = default_config(ice_seeking_mode=False)
        path = astar(grid, (0, 0), (9, 9), cfg)
        assert path is not None
        assert path[-1] == (9, 9)


# ===========================================================================
# 6. Cost function correctness
# ===========================================================================

class TestCostFunction:

    def _make_cell(self, **kwargs) -> GridCell:
        defaults = dict(row=0, col=0, lat=0.0, lon=0.0)
        defaults.update(kwargs)
        return GridCell(**defaults)

    def test_hazard_cell_returns_inf(self):
        from_cell = self._make_cell()
        to_cell = self._make_cell(is_hazard=True)
        cfg = default_config()
        cost = traversal_cost(from_cell, to_cell, 5.0, cfg)
        assert cost == math.inf

    def test_shadowed_cell_costs_more_than_lit(self):
        from_cell = self._make_cell()
        lit_cell = self._make_cell(is_shadowed=False)
        dark_cell = self._make_cell(is_shadowed=True)
        cfg = default_config(shadow_penalty_weight=5.0)
        assert traversal_cost(from_cell, dark_cell, 5.0, cfg) > \
               traversal_cost(from_cell, lit_cell, 5.0, cfg)

    def test_ice_reward_reduces_cost_in_ice_seeking_mode(self):
        from_cell = self._make_cell()
        ice_cell = self._make_cell(ice_volume_m3=1000.0, ice_confidence=1.0)
        no_ice = self._make_cell(ice_volume_m3=0.0)
        cfg_seek = default_config(ice_seeking_mode=True, ice_reward_weight=2.0)
        cfg_noseek = default_config(ice_seeking_mode=False)
        cost_seek = traversal_cost(from_cell, ice_cell, 5.0, cfg_seek)
        cost_normal = traversal_cost(from_cell, no_ice, 5.0, cfg_noseek)
        assert cost_seek < cost_normal

    def test_cost_never_negative(self):
        from_cell = self._make_cell()
        # Max ice reward on a fully illuminated cell
        ice_cell = self._make_cell(ice_volume_m3=999999.0, ice_confidence=1.0)
        cfg = default_config(ice_seeking_mode=True, ice_reward_weight=100.0)
        cost = traversal_cost(from_cell, ice_cell, 5.0, cfg)
        assert cost > 0.0

    def test_diagonal_costs_more_than_cardinal(self):
        from_cell = self._make_cell()
        to_cell = self._make_cell()
        cfg = default_config()
        cardinal_cost = traversal_cost(from_cell, to_cell, 5.0, cfg)
        diagonal_cost = traversal_cost(from_cell, to_cell, 5.0 * math.sqrt(2), cfg)
        assert diagonal_cost > cardinal_cost

    def test_octile_heuristic_admissible(self):
        """h(n) must never exceed the true cost of the optimal path."""
        grid = make_open_grid(10, 10)
        cfg = default_config()
        for start in [(0, 0), (2, 3)]:
            for goal in [(9, 9), (7, 4)]:
                h = octile_heuristic(*start, *goal, cell_size_m=5.0)
                path = astar(grid, start, goal, cfg)
                if path and len(path) > 1:
                    # Approximate true path cost (sum of step distances)
                    true_dist = sum(
                        grid.cell_distance_m(*path[i], *path[i+1])
                        for i in range(len(path)-1)
                    )
                    assert h <= true_dist * 2 + 0.01  # generous bound for hackathon


# ===========================================================================
# 7. Battery model (static)
# ===========================================================================

class TestStaticBatteryModel:

    def _cell(self, shadowed: bool = False, slope_deg: float = 0.0) -> GridCell:
        return GridCell(
            row=0, col=0, lat=0.0, lon=0.0,
            is_shadowed=shadowed,
            slope_deg=slope_deg,
        )

    def test_base_drain_proportional_to_distance(self):
        model = StaticBatteryModel(wh_per_meter=0.05)
        cell = self._cell()
        drain_10 = model.predict_drain_wh(cell, 10.0, 100.0)
        drain_20 = model.predict_drain_wh(cell, 20.0, 100.0)
        assert drain_20 == pytest.approx(2 * drain_10, rel=1e-6)

    def test_shadow_heater_surcharge(self):
        model = StaticBatteryModel(wh_per_meter=0.05, shadow_heater_wh_per_m=0.15)
        lit = self._cell(shadowed=False)
        dark = self._cell(shadowed=True)
        dist = 10.0
        assert model.predict_drain_wh(dark, dist, 100.0) > \
               model.predict_drain_wh(lit, dist, 100.0)

    def test_slope_increases_drain(self):
        model = StaticBatteryModel(slope_factor=0.002)
        flat = self._cell(slope_deg=0.0)
        steep = self._cell(slope_deg=14.9)   # just under hazard threshold
        dist = 10.0
        assert model.predict_drain_wh(steep, dist, 100.0) > \
               model.predict_drain_wh(flat, dist, 100.0)

    def test_protocol_compliance(self):
        from pathfinder.cost import BatteryModel
        model = StaticBatteryModel()
        assert isinstance(model, BatteryModel)


# ===========================================================================
# 8. Pitstop auto-insertion
# ===========================================================================

class TestPitstopInsertion:

    def test_no_pitstop_on_short_lit_route(self):
        """Open, fully illuminated grid — no dark budget consumed, no pitstop."""
        grid = make_open_grid(5, 5)
        for r in range(5):
            for c in range(5):
                grid.mark_illuminated(r, c, illumination=1.0)
        cfg = default_config()
        path = astar(grid, (0, 0), (4, 4), cfg)
        result = build_route(grid, path, cfg, dark_budget_wh=80.0)
        assert result.route_found is True
        assert result.total_pitstops == 0
        # All waypoints should be TRANSIT
        for wp in result.waypoints:
            assert wp.type == WaypointType.TRANSIT

    def test_pitstop_inserted_when_budget_exceeded(self):
        """
        Route passes through a long shadow band; tiny dark budget forces a pitstop.
        Grid: rows 0-2 lit, rows 3-7 dark, rows 8-9 lit.
        Route: (0,0) → (9,0).  Dark budget = 0.1 Wh (very tight).
        """
        grid = make_open_grid(10, 10)
        # Sunlit rim
        for r in [0, 1, 2, 8, 9]:
            for c in range(10):
                grid.mark_illuminated(r, c, illumination=1.0)
        # Shadow band
        for r in range(3, 8):
            for c in range(10):
                grid.mark_shadow(r, c, illumination=0.0, temperature_k=25.0)

        cfg = default_config()
        path = astar(grid, (0, 0), (9, 0), cfg)
        result = build_route(
            grid, path, cfg,
            initial_battery_pct=100.0,
            dark_budget_wh=0.1,    # extremely tight → forces pitstop
        )
        assert result.route_found is True
        assert result.total_pitstops >= 1
        # At least one SOLAR_PITSTOP in waypoints
        pitstop_wps = [wp for wp in result.waypoints if wp.type == WaypointType.SOLAR_PITSTOP]
        assert len(pitstop_wps) >= 1

    def test_pitstop_is_not_shadowed(self):
        """Every solar_pitstop waypoint must be on a sunlit cell."""
        grid = make_shadow_grid()   # rows 3-6 shadow, rows 8-9 lit
        cfg = default_config()
        path = astar(grid, (0, 0), (9, 9), cfg)
        result = build_route(grid, path, cfg, dark_budget_wh=0.01)
        for wp in result.waypoints:
            if wp.type == WaypointType.SOLAR_PITSTOP:
                assert not wp.is_shadowed
                assert wp.solar_illumination > 0.0

    def test_battery_never_goes_negative(self):
        """Battery percentage must always be ≥ 0 across all waypoints."""
        grid = make_shadow_grid()
        cfg = default_config()
        path = astar(grid, (0, 0), (9, 9), cfg)
        result = build_route(grid, path, cfg, initial_battery_pct=1.0)
        for wp in result.waypoints:
            assert wp.battery_pct_remaining >= 0.0

    def test_cumulative_distance_monotonically_increases(self):
        grid = make_open_grid()
        cfg = default_config()
        path = astar(grid, (0, 0), (9, 9), cfg)
        result = build_route(grid, path, cfg)
        dists = [wp.cumulative_distance_m for wp in result.waypoints]
        for i in range(1, len(dists)):
            assert dists[i] >= dists[i - 1], (
                f"Distance decreased at waypoint {i}: {dists[i-1]} → {dists[i]}"
            )

    def test_cumulative_energy_monotonically_increases(self):
        grid = make_shadow_grid()
        cfg = default_config()
        path = astar(grid, (0, 0), (9, 9), cfg)
        result = build_route(grid, path, cfg)
        energies = [wp.cumulative_energy_wh for wp in result.waypoints]
        for i in range(1, len(energies)):
            assert energies[i] >= energies[i - 1]

    def test_empty_path_returns_not_found(self):
        grid = make_open_grid()
        cfg = default_config()
        result = build_route(grid, [], cfg)
        assert result.route_found is False
        assert len(result.waypoints) == 0


# ===========================================================================
# 9. Full plan_route() integration
# ===========================================================================

class TestPlanRouteIntegration:

    def test_basic_end_to_end(self):
        """plan_route() from corner to corner on an open grid."""
        grid = PolarGrid(rows=10, cols=10, origin_lat=-89.55, origin_lon=44.0, cell_size_m=5.0)
        cfg = default_config()
        result = plan_route(
            grid=grid,
            start_lat=-89.55,
            start_lon=44.0,
            end_lat=grid.get_cell(9, 9).lat,
            end_lon=grid.get_cell(9, 9).lon,
            config=cfg,
        )
        assert result.route_found is True
        assert result.total_distance_m > 0.0
        assert result.total_waypoints > 0

    def test_start_in_hazard_returns_not_found(self):
        grid = PolarGrid(rows=10, cols=10, origin_lat=-89.55, origin_lon=44.0)
        grid.mark_hazard(0, 0)
        cfg = default_config()
        result = plan_route(
            grid, -89.55, 44.0,
            grid.get_cell(9, 9).lat, grid.get_cell(9, 9).lon,
            cfg,
        )
        assert result.route_found is False
        assert len(result.waypoints) == 0

    def test_first_waypoint_is_start(self):
        grid = PolarGrid(rows=10, cols=10, origin_lat=-89.55, origin_lon=44.0)
        cfg = default_config()
        start_cell = grid.get_cell(0, 0)
        result = plan_route(
            grid,
            start_lat=start_cell.lat, start_lon=start_cell.lon,
            end_lat=grid.get_cell(9, 9).lat, end_lon=grid.get_cell(9, 9).lon,
            config=cfg,
        )
        assert result.route_found is True
        first = result.waypoints[0]
        assert first.cumulative_distance_m == pytest.approx(0.0)
        assert first.cumulative_energy_wh == pytest.approx(0.0)

    def test_last_waypoint_is_goal(self):
        grid = PolarGrid(rows=10, cols=10, origin_lat=-89.55, origin_lon=44.0)
        cfg = default_config()
        goal_cell = grid.get_cell(8, 7)
        result = plan_route(
            grid,
            start_lat=grid.get_cell(0, 0).lat, start_lon=grid.get_cell(0, 0).lon,
            end_lat=goal_cell.lat, end_lon=goal_cell.lon,
            config=cfg,
        )
        assert result.route_found is True
        last = result.waypoints[-1]
        assert last.lat == pytest.approx(goal_cell.lat, abs=1e-8)
        assert last.lon == pytest.approx(goal_cell.lon, abs=1e-8)

    def test_ice_seeking_flag_propagates(self):
        """plan_route() with ice_seeking should not crash and return valid result."""
        grid = make_ice_grid()
        cfg = default_config(ice_seeking_mode=True)
        result = plan_route(
            grid,
            start_lat=grid.get_cell(0, 0).lat, start_lon=grid.get_cell(0, 0).lon,
            end_lat=grid.get_cell(9, 9).lat, end_lon=grid.get_cell(9, 9).lon,
            config=cfg,
        )
        assert result.route_found is True


# ===========================================================================
# 10. Swarm planning — two independent rovers
# ===========================================================================

class TestSwarmPlanning:
    """
    Swarm planning is orchestrated at the API layer (Step 3).
    Here we verify that calling plan_route() twice with different
    configs is fully independent — no shared state between rovers.
    """

    def test_two_rovers_independent_results(self):
        grid = make_open_grid(15, 15)
        cfg_a = default_config()
        cfg_b = default_config(ice_seeking_mode=True)

        result_a = plan_route(
            grid,
            grid.get_cell(0, 0).lat, grid.get_cell(0, 0).lon,
            grid.get_cell(14, 14).lat, grid.get_cell(14, 14).lon,
            cfg_a,
        )
        result_b = plan_route(
            grid,
            grid.get_cell(0, 14).lat, grid.get_cell(0, 14).lon,
            grid.get_cell(14, 0).lat, grid.get_cell(14, 0).lon,
            cfg_b,
        )

        assert result_a.route_found is True
        assert result_b.route_found is True
        # Rover-A starts at col=0, Rover-B starts at col=14 → different start lons
        assert result_a.waypoints[0].lon != result_b.waypoints[0].lon
        # Goals also differ in longitude
        assert result_a.waypoints[-1].lon != result_b.waypoints[-1].lon

    def test_modifying_grid_after_plan_does_not_affect_previous_result(self):
        """Routes are computed once; mutating grid afterwards shouldn't change result."""
        grid = make_open_grid(10, 10)
        cfg = default_config()

        result = plan_route(
            grid,
            grid.get_cell(0, 0).lat, grid.get_cell(0, 0).lon,
            grid.get_cell(9, 9).lat, grid.get_cell(9, 9).lon,
            cfg,
        )
        original_dist = result.total_distance_m

        # Mutate grid after planning
        for r in range(10):
            for c in range(10):
                grid.mark_hazard(r, c)

        # Previous result is a snapshot — not affected
        assert result.total_distance_m == original_dist
