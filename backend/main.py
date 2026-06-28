"""
main.py
-------
AstraNav-LRIS — Routing & Intelligence Backend
Team Aura++ | PS-08 | Bharatiya Antariksh Hackathon 2026

FastAPI application entry-point.

Startup sequence
----------------
1. Initialise GridCache in app.state.
2. Pre-warm grids for all registered regions (background task).
3. Mount all routers.
4. Expose OpenAPI docs at /docs (Swagger) and /redoc.

Run locally
-----------
    uvicorn main:app --reload --port 8000

Environment variables
---------------------
  ASTRANAV_LOG_LEVEL   : DEBUG / INFO / WARNING  (default INFO)
  ASTRANAV_CORS_ORIGINS: comma-separated origins  (default http://localhost:3000)
  ANTHROPIC_API_KEY    : required for the copilot endpoint (Step 6)
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routes import lmrs as lmrs_router
from api.routes import route_plan as route_router
from api.routes import telemetry as telemetry_router
from api.routes import battery_model as battery_router
from api.routes import copilot as copilot_router
from api.routes import fault_replan as fault_replan_router
from core.grid_cache import GridCache
from data.region_registry import REGION_REGISTRY
from ml.battery_model import load_ml_model

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, os.getenv("ASTRANAV_LOG_LEVEL", "INFO")),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("astranav.main")


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Startup: create the GridCache and pre-warm all registered regions.
    Shutdown: nothing needed (in-memory cache is GC'd automatically).
    """
    logger.info("AstraNav-LRIS backend starting…")

    app.state.grid_cache = GridCache()

    # Pre-warm grids for all regions in the background so the first API
    # request isn't slow.  Failures are logged but don't abort startup.
    async def _prewarm() -> None:
        for region_id in REGION_REGISTRY:
            try:
                await app.state.grid_cache.get(region_id)
                logger.info("Pre-warmed grid: %s", region_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to pre-warm grid '%s': %s", region_id, exc)

    asyncio.create_task(_prewarm())

    # Load ML battery model (Step 5).  Returns None if pickle not found—
    # the API transparently falls back to StaticBatteryModel in that case.
    app.state.ml_battery_model = load_ml_model()
    if app.state.ml_battery_model is not None:
        logger.info(
            "ML battery model loaded: %s",
            app.state.ml_battery_model.model_type,
        )
    else:
        logger.info(
            "ML battery model not available; run `python -m ml.train_battery_model` "
            "to generate the pickle and restart."
        )

    logger.info("AstraNav-LRIS backend ready.")
    yield
    logger.info("AstraNav-LRIS backend shutting down.")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(
        title="AstraNav-LRIS — Routing & Intelligence API",
        description=(
            "**Team Aura++ · PS-08 · Bharatiya Antariksh Hackathon 2026**\n\n"
            "Dual-purpose API for lunar rover routing and ice-mining site scoring:\n\n"
            "- **Shadow-Hopping Pathfinder** (A*) with automatic solar pitstop insertion\n"
            "- **Lunar Mining Readiness Score (LMRS)** — 0–100 composite site score\n"
            "- **Multi-Site Comparison** — rank and recommend candidate landing sites\n"
            "- **Multi-Rover Swarm Planning** — independent routes for up to 2 rovers\n"
            "- **Live Telemetry** — WebSocket stream (see `/ws/telemetry/{region_id}`)\n"
            "- **Predictive Battery Model** — scikit-learn energy estimator (Feature 5)\n"
            "- **Chat Copilot** — data-grounded Q&A via Claude API (Feature 6)\n\n"
            "> Science rules: CPR > 1.0 AND DOP < 0.13 → ice candidate; "
            "slope > 15° or boulder > 0.5 m → hard hazard wall; "
            "shadowed crater floor ≈ 25 K → heavy cost, not impassable."
        ),
        version="1.0.0",
        contact={
            "name": "Team Aura++ — Routing & Intelligence",
            "url": "https://github.com/team-auraplus/astranav-lris",
        },
        license_info={"name": "MIT"},
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_tags=[
            {
                "name": "Routing",
                "description": "A* path planning, swarm coordination, and solar pitstop logic.",
            },
            {
                "name": "LMRS",
                "description": "Lunar Mining Readiness Score — site scoring and comparison.",
            },
            {
                "name": "Telemetry",
                "description": "WebSocket live telemetry stream for rover animation.",
            },
            {
                "name": "Battery Model",
                "description": "ML-based battery drain predictor metadata.",
            },
            {
                "name": "Copilot",
                "description": "Data-grounded chat assistant backed by Claude API.",
            },
        ],
    )

    # ── CORS ─────────────────────────────────────────────────────────────────
    cors_origins_raw = os.getenv("ASTRANAV_CORS_ORIGINS", "http://localhost:5500,http://127.0.0.1:5500,http://localhost:8080,http://127.0.0.1:8080,http://localhost:3000")
    cors_origins = [o.strip() for o in cors_origins_raw.split(",") if o.strip()]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(route_router.router)
    app.include_router(lmrs_router.router)
    app.include_router(telemetry_router.router)   # Step 4: WebSocket telemetry
    app.include_router(battery_router.router)      # Step 5: ML battery model info
    app.include_router(copilot_router.router)
    app.include_router(fault_replan_router.router)      # Step 6: Chat copilot

    # ── Health / meta endpoints ───────────────────────────────────────────────

    @app.get("/health", tags=["Meta"], summary="Health check")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "AstraNav-LRIS Routing & Intelligence"}

    @app.get("/api/regions", tags=["Meta"], summary="List available regions")
    async def list_regions() -> dict[str, object]:
        """Return all region IDs known to the registry, with display names."""
        from data.region_registry import REGION_REGISTRY
        return {
            "regions": [
                {
                    "region_id": cfg.region_id,
                    "display_name": cfg.display_name,
                    "grid_size": f"{cfg.rows}×{cfg.cols}",
                    "cell_size_m": cfg.cell_size_m,
                    "lander_lat": cfg.lander_lat,
                    "lander_lon": cfg.lander_lon,
                }
                for cfg in REGION_REGISTRY.values()
            ]
        }

    @app.get("/api/regions/{region_id}/grid", tags=["Meta"], summary="Get region grid data")
    async def get_region_grid(region_id: str, request: Request) -> dict[str, object]:
        from data.region_registry import get_region_config
        try:
            cfg = get_region_config(region_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        
        cache = request.app.state.grid_cache
        try:
            grid = await cache.get(region_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        cells_data = []
        for r in range(grid.rows):
            for c in range(grid.cols):
                cell = grid.get_cell(r, c)
                cells_data.append({
                    "row": cell.row,
                    "col": cell.col,
                    "lat": cell.lat,
                    "lon": cell.lon,
                    "is_hazard": cell.is_hazard,
                    "is_shadowed": cell.is_shadowed,
                    "temperature_k": cell.temperature_k,
                    "ice_volume_m3": cell.ice_volume_m3,
                    "ice_confidence": cell.ice_confidence,
                })

        return {
            "region_id": region_id,
            "rows": grid.rows,
            "cols": grid.cols,
            "cells": cells_data
        }

    @app.get("/api/cache/status", tags=["Meta"], summary="Grid cache status")
    async def cache_status(request: Request) -> dict[str, object]:
        cache: GridCache = request.app.state.grid_cache
        return {
            "cached_regions": cache.cached_regions(),
            "total_cached": len(cache.cached_regions()),
        }

    return app


# ---------------------------------------------------------------------------
# WSGI / ASGI entry-point
# ---------------------------------------------------------------------------

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
