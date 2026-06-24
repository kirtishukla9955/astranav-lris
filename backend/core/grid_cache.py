"""
core/grid_cache.py
------------------
Thread-safe (asyncio-safe) in-memory cache for PolarGrid instances.

Building a grid (rasterising Member 1's GeoJSON into cells) takes ~10–50 ms
for a 40×40 grid.  We cache the result per region_id so repeated API calls
for the same region are O(1) lookups rather than rebuilding each time.

Cache invalidation:
  - Call ``invalidate(region_id)`` to force a rebuild (e.g., after Member 1
    pushes updated ice/hazard data).
  - Call ``invalidate_all()`` at startup if you want a fresh slate.

Thread safety:
  Uses asyncio.Lock so concurrent async requests for the same region_id
  don't each trigger a separate grid build.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from data.region_registry import build_grid_for_region, get_region_config
from pathfinder import PolarGrid

logger = logging.getLogger(__name__)


class GridCache:
    """Singleton-ish cache stored in app.state.grid_cache."""

    def __init__(self) -> None:
        self._cache: dict[str, PolarGrid] = {}
        self._lock = asyncio.Lock()

    async def get(self, region_id: str) -> PolarGrid:
        """
        Return the cached grid for ``region_id``, building it on first access.

        Raises
        ------
        KeyError
            If ``region_id`` is not in the region registry.
        """
        # Fast path — no lock needed if already cached
        if region_id in self._cache:
            return self._cache[region_id]

        # Slow path — acquire lock so only one coroutine builds the grid
        async with self._lock:
            # Double-check after acquiring the lock
            if region_id in self._cache:
                return self._cache[region_id]

            # Validate region exists (raises KeyError if not)
            cfg = get_region_config(region_id)

            logger.info("Building grid for region '%s' (%d×%d cells)…",
                        region_id, cfg.rows, cfg.cols)

            # build_grid_for_region is synchronous (NumPy/dict ops).
            # Run in a thread executor to avoid blocking the event loop.
            loop = asyncio.get_running_loop()
            grid = await loop.run_in_executor(
                None, build_grid_for_region, region_id
            )
            self._cache[region_id] = grid
            logger.info("Grid for '%s' ready — %d×%d cells cached.",
                        region_id, cfg.rows, cfg.cols)
            return grid

    def invalidate(self, region_id: str) -> None:
        """Remove a single region from the cache."""
        self._cache.pop(region_id, None)
        logger.info("Cache invalidated for region '%s'.", region_id)

    def invalidate_all(self) -> None:
        """Clear the entire cache."""
        self._cache.clear()
        logger.info("Grid cache cleared.")

    def cached_regions(self) -> list[str]:
        return list(self._cache.keys())
