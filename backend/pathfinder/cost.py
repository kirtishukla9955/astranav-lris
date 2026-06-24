"""
pathfinder/cost.py
------------------
Cost function and battery drain models for the AstraNav-LRIS pathfinder.

Design rules (from the science spec):
- Hazard cells → math.inf (hard wall; never passable)
- Shadowed cells → heavy cost addition, NOT infinity (rover can traverse,
  just expensive: battery + heater draw at ~25 K)
- Sunlit rim cells → cheap / free; valid pitstop candidates
- Ice cells → optional negative bias when ice_seeking_mode=True

Battery model is hidden behind the ``BatteryModel`` Protocol so the static
constant model and the future scikit-learn ML model are hot-swappable
without changing any calling code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .types import GridCell


# ---------------------------------------------------------------------------
# Battery Model Protocol  (Structural subtyping — duck-typed)
# ---------------------------------------------------------------------------

@runtime_checkable
class BatteryModel(Protocol):
    """
    Anything that implements this predict_drain_wh signature is a valid
    battery model — both StaticBatteryModel and the ML model (Feature 5).
    """

    def predict_drain_wh(
        self,
        cell: GridCell,
        distance_m: float,
        prior_battery_pct: float,
    ) -> float:
        """Return energy consumed (Wh) traversing ``cell`` over ``distance_m``."""
        ...


# ---------------------------------------------------------------------------
# Static (constant) battery model — default / fallback
# ---------------------------------------------------------------------------

@dataclass
class StaticBatteryModel:
    """
    Deterministic energy model using hand-tuned domain constants.
    Serves as the fallback when ``use_predictive_battery=False`` or when
    the ML model pickle fails to load.

    Domain constants
    ----------------
    wh_per_meter          : drive energy on flat, lit terrain  (~50 Wh/km)
    shadow_heater_wh_per_m: extra thermal-heater draw in dark zones;
                            at ~25 K the rover must run resistive heaters
                            continuously — modelled as a fixed surcharge.
    slope_factor          : extra energy per degree of slope per metre
    """

    wh_per_meter: float = 0.05              # baseline: ~50 Wh / km
    shadow_heater_wh_per_m: float = 0.15    # heater surcharge in dark zones
    slope_factor: float = 0.002             # Wh / (m · degree_slope)

    def predict_drain_wh(
        self,
        cell: GridCell,
        distance_m: float,
        prior_battery_pct: float,           # unused in static model; kept for API compat
    ) -> float:
        base = self.wh_per_meter * distance_m
        heater = self.shadow_heater_wh_per_m * distance_m if cell.is_shadowed else 0.0
        slope_extra = self.slope_factor * cell.slope_deg * distance_m
        return base + heater + slope_extra


# ---------------------------------------------------------------------------
# Cost configuration
# ---------------------------------------------------------------------------

@dataclass
class CostConfig:
    """
    All tunable parameters for the traversal cost function.
    Passed by value into astar() so different API requests can use different
    configs without shared mutable state.

    Default weights are chosen so:
    - A fully shadowed path costs ~6× a fully lit path
    - Ice reward can shave at most 50% of the shadow penalty off a cell
    """

    shadow_penalty_weight: float = 5.0
    """Additional cost added per shadowed cell (on top of base_traversal_cost)."""

    ice_reward_weight: float = 2.0
    """Max bonus subtracted for high-confidence ice cells in ice_seeking_mode."""

    ice_seeking_mode: bool = False
    """If True, route engine biases toward high-volume, high-confidence ice cells."""

    battery_model: BatteryModel = field(default_factory=StaticBatteryModel)
    """Pluggable energy model — swap to MLBatteryModel at runtime."""


# ---------------------------------------------------------------------------
# Traversal cost function  (called once per neighbour expansion in A*)
# ---------------------------------------------------------------------------

def traversal_cost(
    from_cell: GridCell,
    to_cell: GridCell,
    distance_m: float,
    config: CostConfig,
) -> float:
    """
    Compute the cost of moving from ``from_cell`` to ``to_cell``.

    Returns
    -------
    float
        Positive cost, or ``math.inf`` if ``to_cell`` is a hazard.

    Notes
    -----
    Cost formula::

        cost = (base_traversal_cost
                + shadow_penalty  [if shadowed]
                - ice_reward      [if ice_seeking and ice present])
               × distance_normaliser
    """
    if to_cell.is_hazard:
        return math.inf   # hard wall — A* will never expand through this

    cost = to_cell.base_traversal_cost

    # ── Shadow penalty ──────────────────────────────────────────────────────
    # Heavy but finite: rover CAN enter dark zones, just expensive.
    # Doubly-shadowed craters sit at ~25 K; heater draw is modelled in battery.
    if to_cell.is_shadowed:
        cost += config.shadow_penalty_weight

    # ── Ice reward ──────────────────────────────────────────────────────────
    # Bias toward high-confidence ice cells when ice_seeking_mode is on.
    # Reward is capped at half the shadow penalty so it never dominates routing.
    if config.ice_seeking_mode and to_cell.ice_volume_m3 > 0.0:
        ice_bonus = config.ice_reward_weight * to_cell.ice_confidence
        ice_bonus = min(ice_bonus, config.shadow_penalty_weight * 0.5)
        cost -= ice_bonus

    # ── Distance normaliser ─────────────────────────────────────────────────
    # Scale by actual step distance so diagonal steps cost proportionally more.
    # Dividing by cell_size_m (5.0) keeps the base cost ≈ 1.0 per cardinal step.
    cost *= distance_m / 5.0

    # Floor at a small positive epsilon — prevents negative costs if ice reward
    # somehow exceeds the base cost on a lit ice cell.
    return max(0.01, cost)


# ---------------------------------------------------------------------------
# Heuristic  (must be admissible — never overestimates true cost)
# ---------------------------------------------------------------------------

def octile_heuristic(
    row_a: int,
    col_a: int,
    row_b: int,
    col_b: int,
    cell_size_m: float = 5.0,
) -> float:
    """
    Octile distance heuristic for an 8-connected grid.

    Admissible because it assumes every cell has base_traversal_cost=1.0
    and no shadow penalties — always ≤ real cost.

    Formula::

        h = cell_size * (dx + dy + (sqrt(2) - 2) * min(dx, dy))
    """
    dx = abs(col_a - col_b)
    dy = abs(row_a - row_b)
    return cell_size_m * (dx + dy + (math.sqrt(2) - 2.0) * min(dx, dy))
