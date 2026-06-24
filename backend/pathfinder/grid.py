"""
pathfinder/grid.py
------------------
PolarGrid — the 2-D cell map the A* algorithm navigates.

Design notes:
- Cells are 5 m × 5 m by default (``cell_size_m``), down-sampled from raw
  OHRC/DFSAR rasters.  Change the default once Member 1 confirms raster specs.
- lat/lon ↔ (row, col) conversion uses a simple linear approximation suited
  to small crater patches (~1–5 km across) at the south pole.  For a full
  polar-cap DEM, replace with a proper polar-stereographic projection.
- All numpy arrays are kept as optional metadata layers (shadow mask, ice
  volume) so they can be swapped in from GeoTIFF rasters without restructuring
  the cells list.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

from .types import GridCell

# Lunar mean radius in metres (IAU 2015)
LUNAR_RADIUS_M: float = 1_737_400.0
# 1 degree of latitude on the Moon in metres
DEG_LAT_M: float = (math.pi / 180.0) * LUNAR_RADIUS_M  # ≈ 30 328 m/°


class PolarGrid:
    """
    2-D grid representing a crater region for A* path-finding.

    Coordinate system
    -----------------
    ``origin_lat / origin_lon`` is the (row=0, col=0) corner.
    Rows increase northward (lat increasing), cols increase eastward.
    Latitude is typically negative (south pole region ~ −89 to −90°).

    Usage
    -----
    >>> grid = PolarGrid(rows=20, cols=20, origin_lat=-89.55, origin_lon=44.0)
    >>> grid.mark_hazard(5, 5)
    >>> row, col = grid.lat_lon_to_cell(-89.54, 44.05)
    >>> neighbors = grid.neighbors(row, col)
    """

    def __init__(
        self,
        rows: int,
        cols: int,
        origin_lat: float,
        origin_lon: float,
        cell_size_m: float = 5.0,
    ) -> None:
        self.rows = rows
        self.cols = cols
        self.origin_lat = origin_lat
        self.origin_lon = origin_lon
        self.cell_size_m = cell_size_m

        # Degree-per-cell along each axis
        self._dlat = cell_size_m / DEG_LAT_M
        # Longitude degree per metre at this latitude (cos-corrected)
        _cos_lat = math.cos(math.radians(abs(origin_lat)))
        self._dlon = cell_size_m / (DEG_LAT_M * _cos_lat) if _cos_lat > 1e-9 else cell_size_m / DEG_LAT_M

        # Build flat cell store
        self._cells: list[list[GridCell]] = [
            [
                GridCell(
                    row=r,
                    col=c,
                    lat=self._row_to_lat(r),
                    lon=self._col_to_lon(c),
                )
                for c in range(cols)
            ]
            for r in range(rows)
        ]

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def _row_to_lat(self, row: int) -> float:
        return self.origin_lat + row * self._dlat

    def _col_to_lon(self, col: int) -> float:
        return self.origin_lon + col * self._dlon

    def lat_lon_to_cell(self, lat: float, lon: float) -> tuple[int, int]:
        """Map a (lat, lon) coordinate to the nearest (row, col) in the grid."""
        row = round((lat - self.origin_lat) / self._dlat)
        col = round((lon - self.origin_lon) / self._dlon)
        row = max(0, min(self.rows - 1, row))
        col = max(0, min(self.cols - 1, col))
        return row, col

    def cell_distance_m(
        self, r1: int, c1: int, r2: int, c2: int
    ) -> float:
        """Euclidean distance between two cells in metres."""
        dx = (c2 - c1) * self.cell_size_m
        dy = (r2 - r1) * self.cell_size_m
        return math.hypot(dx, dy)

    # ------------------------------------------------------------------
    # Cell accessors
    # ------------------------------------------------------------------

    def get_cell(self, row: int, col: int) -> GridCell:
        return self._cells[row][col]

    def in_bounds(self, row: int, col: int) -> bool:
        return 0 <= row < self.rows and 0 <= col < self.cols

    def neighbors(self, row: int, col: int) -> list[tuple[int, int]]:
        """8-connected neighbours (diagonal movement allowed)."""
        candidates = [
            (row - 1, col - 1), (row - 1, col), (row - 1, col + 1),
            (row,     col - 1),                  (row,     col + 1),
            (row + 1, col - 1), (row + 1, col), (row + 1, col + 1),
        ]
        return [(r, c) for r, c in candidates if self.in_bounds(r, c)]

    # ------------------------------------------------------------------
    # Bulk-marking helpers (called by region loader / mock fixtures)
    # ------------------------------------------------------------------

    def mark_hazard(self, row: int, col: int) -> None:
        """Hard-block a cell (slope >15° or boulder >0.5 m)."""
        if self.in_bounds(row, col):
            self._cells[row][col].is_hazard = True

    def mark_shadow(
        self, row: int, col: int, illumination: float = 0.0, temperature_k: float = 25.0
    ) -> None:
        """Mark a cell as permanently shadowed with optional solar illumination."""
        if self.in_bounds(row, col):
            cell = self._cells[row][col]
            cell.is_shadowed = True
            cell.solar_illumination = max(0.0, min(1.0, illumination))
            cell.temperature_k = temperature_k

    def mark_illuminated(
        self, row: int, col: int, illumination: float = 1.0, temperature_k: float = 200.0
    ) -> None:
        """Mark a cell as sunlit rim cell — valid solar pitstop candidate."""
        if self.in_bounds(row, col):
            cell = self._cells[row][col]
            cell.is_shadowed = False
            cell.solar_illumination = max(0.0, min(1.0, illumination))
            cell.temperature_k = temperature_k

    def mark_ice(
        self, row: int, col: int, volume_m3: float, confidence: float
    ) -> None:
        """Inject Member 1 ice-layer data into a cell."""
        if self.in_bounds(row, col):
            cell = self._cells[row][col]
            cell.ice_volume_m3 = max(0.0, volume_m3)
            cell.ice_confidence = max(0.0, min(1.0, confidence))

    def mark_slope(self, row: int, col: int, slope_deg: float) -> None:
        if self.in_bounds(row, col):
            self._cells[row][col].slope_deg = slope_deg

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def sunlit_rim_cells(self) -> list[tuple[int, int]]:
        """All non-hazard cells with solar_illumination > 0 — pitstop candidates."""
        return [
            (r, c)
            for r in range(self.rows)
            for c in range(self.cols)
            if not self._cells[r][c].is_hazard
            and self._cells[r][c].solar_illumination > 0.0
        ]

    def ice_cells(self) -> list[tuple[int, int]]:
        """All cells carrying non-zero ice volume."""
        return [
            (r, c)
            for r in range(self.rows)
            for c in range(self.cols)
            if self._cells[r][c].ice_volume_m3 > 0.0
        ]

    # ------------------------------------------------------------------
    # Bulk numpy layer ingestion (for raster pipeline)
    # ------------------------------------------------------------------

    def apply_shadow_mask(self, mask: np.ndarray) -> None:
        """
        Apply a boolean numpy array (rows × cols) as the shadow mask.
        True = shadowed.  Used by the rasterio ingestion layer.
        """
        assert mask.shape == (self.rows, self.cols), (
            f"Shadow mask shape {mask.shape} ≠ grid ({self.rows}, {self.cols})"
        )
        for r in range(self.rows):
            for c in range(self.cols):
                if mask[r, c]:
                    self.mark_shadow(r, c)

    def apply_hazard_mask(self, mask: np.ndarray) -> None:
        """Apply a boolean numpy array as the hazard mask (slope/obstacles)."""
        assert mask.shape == (self.rows, self.cols)
        for r in range(self.rows):
            for c in range(self.cols):
                if mask[r, c]:
                    self.mark_hazard(r, c)

    def apply_illumination_layer(self, layer: np.ndarray) -> None:
        """
        Apply a float32 numpy array (0–1) as solar illumination.
        Cells with illumination > 0 are automatically un-shadowed.
        """
        assert layer.shape == (self.rows, self.cols)
        for r in range(self.rows):
            for c in range(self.cols):
                val = float(layer[r, c])
                if val > 0.0:
                    self.mark_illuminated(r, c, illumination=val)

    def apply_ice_volume_layer(
        self, volume_layer: np.ndarray, confidence_layer: np.ndarray
    ) -> None:
        """Apply ice volume (m³) and confidence (0–1) numpy arrays."""
        assert volume_layer.shape == confidence_layer.shape == (self.rows, self.cols)
        for r in range(self.rows):
            for c in range(self.cols):
                if volume_layer[r, c] > 0:
                    self.mark_ice(r, c, float(volume_layer[r, c]), float(confidence_layer[r, c]))

    # ------------------------------------------------------------------
    # Debug / diagnostics
    # ------------------------------------------------------------------

    def ascii_map(self) -> str:
        """
        Return a small ASCII art representation for debugging.
        H=hazard, *=shadowed, i=ice, S=sunlit, .=normal
        """
        lines = []
        for r in range(self.rows - 1, -1, -1):  # top row first
            row_chars = []
            for c in range(self.cols):
                cell = self._cells[r][c]
                if cell.is_hazard:
                    row_chars.append("H")
                elif cell.ice_volume_m3 > 0:
                    row_chars.append("i")
                elif cell.solar_illumination > 0:
                    row_chars.append("S")
                elif cell.is_shadowed:
                    row_chars.append("*")
                else:
                    row_chars.append(".")
            lines.append("".join(row_chars))
        return "\n".join(lines)
