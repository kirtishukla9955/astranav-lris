"""
4_features_back — Python package for AstraNav-LRIS Features A, C, and D.

This package adds the root project directory to sys.path so that the
feature modules (illumination.py, report.py, briefing.py) can continue
using the same bare imports they used when they lived in the project root
(e.g. `from cost_grid import CostGrid`).

This is intentional: the existing core modules (cost_grid, pathfinder, lmrs,
schemas, etc.) are not part of any package themselves — they live in the root
and are imported directly by the FastAPI app. Adding root to sys.path here
keeps ALL inter-module imports consistent without changing any existing file.
"""
import sys
import os

# Insert the project root (parent of this package directory) at the front of
# sys.path so that bare imports like `from cost_grid import ...` resolve
# correctly from anywhere inside this package.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
