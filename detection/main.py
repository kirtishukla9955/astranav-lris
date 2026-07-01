"""
detection/main.py
-----------------
AstraNav-LRIS — Detection Service (Member 1)
Layer 1: Volumetric Ice Detection Core
Layer 2: Morphological Hazard Mapping

FastAPI application exposing two REST endpoints:

  GET /api/ice-layer/{region_id}
      → IceLayerResponse (GeoJSON FeatureCollection of ice-candidate polygons)
      Properties per feature: ice_id, cpr, dop, dielectric_constant,
                              depth_m, volume_m3, confidence, cell_size_m

  GET /api/hazard-layer/{region_id}
      → HazardLayerResponse (GeoJSON FeatureCollection of no-go polygons)
      Properties per feature: hazard_id, hazard_type, severity,
                              slope_deg?, max_boulder_diameter_m?

Science rules enforced in pipeline.py:
  ice candidate ↔ CPR > 1.0  AND  DOP < 0.13
  no-go zone    ↔ slope > 15°  OR  boulder diameter > 0.5 m
  dielectric ε  ∈ [2.5 (dust), 3.5 (ice mix)] → ice depth & volume

Run this service independently on port 8001:
  cd detection
  uvicorn main:app --reload --port 8001

The routing backend at port 8000 expects this URL pattern:
  MEMBER1_BASE_URL = "http://localhost:8001"   (backend/data/mock_fixtures.py)
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .cache import get_available_regions, get_ice_layer, get_hazard_layer, prewarm_all
from .models import (
    IceLayerResponse,
    HazardLayerResponse,
    DetectionServiceStatus,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, os.getenv("ASTRANAV_LOG_LEVEL", "INFO")),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("astranav.detection")


# ---------------------------------------------------------------------------
# Lifespan — prewarm all regions at startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Detection Service starting — pre-warming all regions…")
    # Run prewarm in a thread so we don't block the event loop
    await asyncio.get_event_loop().run_in_executor(None, prewarm_all)
    logger.info("Detection Service ready — %d regions cached", len(get_available_regions()))
    yield
    logger.info("Detection Service shutting down.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def create_detection_app() -> FastAPI:
    app = FastAPI(
        title="AstraNav-LRIS — Detection Service",
        description=(
            "**Team Aura++ · PS-08 · Bharatiya Antariksh Hackathon 2026**\n\n"
            "Member 1 service: geospatial raster processing for Chandrayaan-2 "
            "DFSAR (SAR) + OHRC (optical) + TMC-2 (DEM) data.\n\n"
            "## Endpoints\n\n"
            "| Endpoint | Returns |\n"
            "|---|---|\n"
            "| `GET /api/ice-layer/{region_id}` | GeoJSON FeatureCollection — ice-candidate polygons |\n"
            "| `GET /api/hazard-layer/{region_id}` | GeoJSON FeatureCollection — no-go / obstacle polygons |\n\n"
            "## Science rules\n\n"
            "- **Ice candidate**: CPR > 1.0 AND DOP < 0.13 (ISRO DFSAR specification)\n"
            "- **Dielectric model**: ε ≈ 2.5 (dust) → ε ≈ 3.5 (ice mixture) → depth 0–5 m\n"
            "- **No-go zone**: slope > 15° OR boulder diameter > 0.5 m\n"
            "- **Temperature**: doubly-shadowed crater floors ≈ 25 K\n\n"
            "> All data is **synthetic** (scientifically calibrated NumPy simulation). "
            "Swap `detection/cache.py::_get_rasters()` to ingest real GeoTIFF files."
        ),
        version="1.0.0",
        contact={"name": "Team Aura++ — Data & Detection"},
        license_info={"name": "MIT"},
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_tags=[
            {
                "name": "Detection",
                "description": "Ice-candidate and terrain-hazard GeoJSON layers.",
            },
            {
                "name": "Meta",
                "description": "Health, status, and region enumeration.",
            },
        ],
    )

    # ── CORS (allow the routing backend + frontend dev servers) ──────────────
    cors_origins_raw = os.getenv(
        "ASTRANAV_CORS_ORIGINS",
        "http://localhost:8000,http://localhost:5500,http://127.0.0.1:5500,"
        "http://localhost:3000,http://127.0.0.1:8000",
    )
    cors_origins = [o.strip() for o in cors_origins_raw.split(",") if o.strip()]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Health ───────────────────────────────────────────────────────────────

    @app.get("/health", tags=["Meta"], summary="Health check")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "AstraNav-LRIS Detection Service"}

    @app.get(
        "/api/status",
        tags=["Meta"],
        summary="Service status + science thresholds",
        response_model=DetectionServiceStatus,
    )
    async def status() -> DetectionServiceStatus:
        return DetectionServiceStatus(available_regions=get_available_regions())

    # ── Layer 1: Ice Detection ───────────────────────────────────────────────

    @app.get(
        "/api/ice-layer/{region_id}",
        tags=["Detection"],
        summary="Ice-candidate polygons (Layer 1)",
        response_model=IceLayerResponse,
        responses={
            200: {
                "description": "GeoJSON FeatureCollection of ice-candidate polygons",
                "content": {
                    "application/json": {
                        "example": {
                            "type": "FeatureCollection",
                            "region_id": "shackleton-east",
                            "features": [
                                {
                                    "type": "Feature",
                                    "geometry": {
                                        "type": "Polygon",
                                        "coordinates": [[[44.28, -89.51], [44.30, -89.51],
                                                         [44.30, -89.50], [44.28, -89.50],
                                                         [44.28, -89.51]]],
                                    },
                                    "properties": {
                                        "ice_id": "ICE-001",
                                        "cpr": 1.24,
                                        "dop": 0.09,
                                        "dielectric_constant": 3.37,
                                        "depth_m": 2.1,
                                        "volume_m3": 4200.0,
                                        "confidence": 0.87,
                                        "cell_size_m": 30.0,
                                    },
                                }
                            ],
                            "metadata": {},
                        }
                    }
                },
            },
            404: {"description": "Unknown region_id"},
        },
    )
    async def get_ice_layer_endpoint(region_id: str) -> IceLayerResponse:
        """
        Returns a GeoJSON FeatureCollection of ice-candidate polygons for the
        given region.

        **Ice detection criteria (ISRO-specified):**
        - Circular Polarization Ratio (CPR) > 1.0
        - Degree of Polarization (DOP) < 0.13

        **Volume estimation:**
        - Dielectric constant ε interpolated linearly: 2.5 (dust) → 3.5 (ice mix)
        - Depth = 5 m × (ε − 2.5) / (3.5 − 2.5)
        - Volume = depth × cell_area_m²
        """
        available = get_available_regions()
        if region_id not in available:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown region '{region_id}'. "
                       f"Available: {available}",
            )
        try:
            layer = get_ice_layer(region_id)
        except Exception as exc:
            logger.exception("Ice layer generation failed for %s", region_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        logger.info(
            "Ice layer served: region=%s features=%d total_vol=%.0f m³",
            region_id,
            len(layer.features),
            sum(f.properties.volume_m3 for f in layer.features),
        )
        return layer

    # ── Layer 2: Hazard Mapping ──────────────────────────────────────────────

    @app.get(
        "/api/hazard-layer/{region_id}",
        tags=["Detection"],
        summary="Terrain hazard polygons (Layer 2)",
        response_model=HazardLayerResponse,
        responses={
            200: {
                "description": "GeoJSON FeatureCollection of no-go and obstacle polygons",
                "content": {
                    "application/json": {
                        "example": {
                            "type": "FeatureCollection",
                            "region_id": "shackleton-east",
                            "features": [
                                {
                                    "type": "Feature",
                                    "geometry": {
                                        "type": "Polygon",
                                        "coordinates": [[[44.33, -89.52], [44.38, -89.52],
                                                         [44.38, -89.50], [44.33, -89.50],
                                                         [44.33, -89.52]]],
                                    },
                                    "properties": {
                                        "hazard_id": "HAZ-001",
                                        "hazard_type": "steep_slope",
                                        "severity": "no_go",
                                        "slope_deg": 22.4,
                                        "max_boulder_diameter_m": None,
                                    },
                                },
                                {
                                    "type": "Feature",
                                    "geometry": {
                                        "type": "Polygon",
                                        "coordinates": [[[44.46, -89.50], [44.49, -89.50],
                                                         [44.49, -89.49], [44.46, -89.49],
                                                         [44.46, -89.50]]],
                                    },
                                    "properties": {
                                        "hazard_id": "HAZ-002",
                                        "hazard_type": "boulder_field",
                                        "severity": "no_go",
                                        "slope_deg": None,
                                        "max_boulder_diameter_m": 1.2,
                                    },
                                },
                            ],
                            "metadata": {},
                        }
                    }
                },
            },
            404: {"description": "Unknown region_id"},
        },
    )
    async def get_hazard_layer_endpoint(region_id: str) -> HazardLayerResponse:
        """
        Returns a GeoJSON FeatureCollection of hazard polygons for the given region.

        **Hazard classification:**
        - **steep_slope** / **crater_wall**: slope > 15° (ISRO constraint)
        - **boulder_field**: obstacle diameter > 0.5 m (OHRC detection)

        All features carry `severity = "no_go"` — the routing backend sets
        traversal cost to ∞ for these cells (see `backend/pathfinder/grid.py`).
        """
        available = get_available_regions()
        if region_id not in available:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown region '{region_id}'. "
                       f"Available: {available}",
            )
        try:
            layer = get_hazard_layer(region_id)
        except Exception as exc:
            logger.exception("Hazard layer generation failed for %s", region_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        logger.info(
            "Hazard layer served: region=%s features=%d",
            region_id,
            len(layer.features),
        )
        return layer

    # ── All regions (convenience) ────────────────────────────────────────────

    @app.get(
        "/api/regions",
        tags=["Meta"],
        summary="List available region IDs",
    )
    async def list_regions() -> dict[str, list[str]]:
        return {"regions": get_available_regions()}

    return app


# ---------------------------------------------------------------------------
# ASGI entry-point
# ---------------------------------------------------------------------------

app = create_detection_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("detection.main:app", host="0.0.0.0", port=8001, reload=True)
