"""
ml/__init__.py
--------------
Machine-learning battery-drain model for AstraNav-LRIS.

Exports
-------
MLBatteryModel   — scikit-learn wrapper that implements pathfinder.cost.BatteryModel
load_ml_model    — loads the pickled model (or returns None on failure)
FEATURE_NAMES    — canonical ordered list of input features
"""

from .battery_model import FEATURE_NAMES, MLBatteryModel, load_ml_model

__all__ = ["MLBatteryModel", "load_ml_model", "FEATURE_NAMES"]
