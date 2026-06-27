"""
api/routes/telemetry.py
-----------------------
WebSocket live telemetry stream for AstraNav-LRIS rover animation.

Endpoint
--------
WS /ws/telemetry/{region_id}?rover_id=<id>&start_lat=&start_lon=&end_lat=&end_lon=&...

Connection lifecycle
--------------------
1. Client upgrades to WebSocket and sends optional JSON config frame.
2. Server plans the rover route using plan_route() with the given params.
3. Server streams TelemetryMessage JSON frames at `tick_interval_s` seconds
   per waypoint, simulating real-time rover traversal.
4. Solar pitstop waypoints are broadcast with status="charging" and a
   configurable dwell pause before resuming.
5. When the rover reaches the goal, a final status="arrived" frame is sent
   and the connection is closed gracefully.

Multi-rover (swarm) support
----------------------------
The same endpoint accepts a `rover_id` query param.  To simulate N rovers,
the frontend opens N parallel WebSocket connections, each with a different
rover_id. The server routes each connection independently — no collision
avoidance (noted in telemetry message metadata).

Alternative: the frontend can pass a list of rover configs via the POST
/api/swarm/plan endpoint first to get all waypoints, then replay them
through WebSocket connections.

Error handling
--------------
- Invalid region_id → immediate close with code 1008.
- No route found → single "stalled" frame then close.
- Client disconnect → loop breaks cleanly (no unhandled exception).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.websockets import WebSocketState

from api.models import TelemetryMessage
from core.grid_cache import GridCache
from data.region_registry import get_region_config
from pathfinder import CostConfig, StaticBatteryModel, plan_route
from pathfinder.types import WaypointType

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Telemetry"])


# ---------------------------------------------------------------------------
# Helper: get GridCache from app state
# ---------------------------------------------------------------------------

def _get_cache(websocket: WebSocket) -> GridCache:
    return websocket.app.state.grid_cache


# ---------------------------------------------------------------------------
# Helper: choose battery model
# ---------------------------------------------------------------------------

def _build_battery_model(use_ml: bool, app) -> object:
    """
    Return MLBatteryModel if use_ml=True and model is loaded, else StaticBatteryModel.
    Falls back to Static silently — we never crash the telemetry stream on ML errors.
    """
    if use_ml and hasattr(app.state, "ml_battery_model") and app.state.ml_battery_model is not None:
        return app.state.ml_battery_model
    return StaticBatteryModel()


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws/telemetry/{region_id}")
async def telemetry_stream(
    websocket: WebSocket,
    region_id: str,
    rover_id: Annotated[str, Query(description="Unique rover identifier")] = "rover-1",
    start_lat: Annotated[float, Query(description="Start latitude")] = -89.55,
    start_lon: Annotated[float, Query(description="Start longitude")] = 44.0,
    end_lat: Annotated[float, Query(description="Goal latitude")] = -89.50,
    end_lon: Annotated[float, Query(description="Goal longitude")] = 44.35,
    initial_battery_pct: Annotated[float, Query(ge=0, le=100)] = 100.0,
    dark_budget_wh: Annotated[float, Query(gt=0)] = 80.0,
    shadow_penalty_weight: Annotated[float, Query(gt=0)] = 5.0,
    ice_seeking: Annotated[bool, Query()] = False,
    use_predictive_battery: Annotated[bool, Query()] = False,
    tick_interval_s: Annotated[float, Query(gt=0, le=10,
        description="Seconds between telemetry frames (controls animation speed)")] = 0.5,
    pitstop_dwell_s: Annotated[float, Query(gt=0, le=30,
        description="Extra seconds spent at each solar pitstop")] = 2.0,
) -> None:

    await websocket.accept()
    logger.info(
        "WS telemetry connected: region=%s rover=%s start=(%.4f,%.4f) end=(%.4f,%.4f)",
        region_id, rover_id, start_lat, start_lon, end_lat, end_lon,
    )

    # ── Validate region ──────────────────────────────────────────────────────
    try:
        get_region_config(region_id)
    except KeyError:
        await _send_and_close(
            websocket,
            rover_id=rover_id,
            region_id=region_id,
            status="stalled",
            error_msg=f"Unknown region_id: {region_id}",
        )
        return

    # ── Build grid + route ───────────────────────────────────────────────────
    cache: GridCache = _get_cache(websocket)
    try:
        grid = await cache.get(region_id)
    except Exception as exc:  # noqa: BLE001
        await _send_and_close(
            websocket, rover_id=rover_id, region_id=region_id,
            status="stalled", error_msg=f"Grid load failed: {exc}",
        )
        return

    battery_model = _build_battery_model(use_predictive_battery, websocket.app)
    config = CostConfig(
        ice_seeking_mode=ice_seeking,
        shadow_penalty_weight=shadow_penalty_weight,
        battery_model=battery_model,
    )

    route_result = plan_route(
        grid=grid,
        start_lat=start_lat,
        start_lon=start_lon,
        end_lat=end_lat,
        end_lon=end_lon,
        config=config,
        initial_battery_pct=initial_battery_pct,
        dark_budget_wh=dark_budget_wh,
    )

    if not route_result.route_found:
        await _send_and_close(
            websocket, rover_id=rover_id, region_id=region_id,
            status="stalled", error_msg="no_route_found",
        )
        return

    waypoints = route_result.waypoints
    total = len(waypoints)
    logger.info(
        "WS telemetry: rover=%s route=%d waypoints, %d pitstops",
        rover_id, total, route_result.total_pitstops,
    )

    # ── Stream waypoints ─────────────────────────────────────────────────────
    try:
        for idx, wp in enumerate(waypoints):
            if websocket.client_state != WebSocketState.CONNECTED:
                break

            is_last = idx == total - 1
            is_pitstop = wp.type == WaypointType.SOLAR_PITSTOP

            status: str
            if is_last:
                status = "arrived"
            elif is_pitstop:
                status = "charging"
            else:
                status = "moving"

            frame = TelemetryMessage(
                rover_id=rover_id,
                region_id=region_id,
                lat=wp.lat,
                lon=wp.lon,
                battery_pct=wp.battery_pct_remaining,
                is_shadowed=wp.is_shadowed,
                solar_illumination=wp.solar_illumination,
                status=status,
                waypoint_index=idx,
                total_waypoints=total,
                cumulative_distance_m=wp.cumulative_distance_m,
                cumulative_energy_wh=wp.cumulative_energy_wh,
                timestamp=_utc_now(),
            )

            # Inject the strict nested structure required by the integration phase
            payload = frame.model_dump()
            payload["timestamp"] = int(datetime.now(timezone.utc).timestamp() * 1000)
            payload["rover"] = {
                "lat": wp.lat,
                "lon": wp.lon,
                "battery": wp.battery_pct_remaining,
                "status": status,
            }

            await websocket.send_json(payload)

            # Pause at pitstop cells for `pitstop_dwell_s` extra seconds
            dwell = tick_interval_s + (pitstop_dwell_s if is_pitstop else 0.0)
            await asyncio.sleep(dwell)

        # Final grace period then close
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1000)

    except WebSocketDisconnect:
        logger.info("WS telemetry: rover=%s client disconnected cleanly.", rover_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("WS telemetry error (rover=%s): %s", rover_id, exc)
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close(code=1011)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _send_and_close(
    websocket: WebSocket,
    rover_id: str,
    region_id: str,
    status: str,
    error_msg: str = "",
) -> None:
    """Send a single 'stalled' or error frame and close the connection."""
    try:
        frame = {
            "rover_id": rover_id,
            "region_id": region_id,
            "lat": 0.0,
            "lon": 0.0,
            "battery_pct": 0.0,
            "is_shadowed": False,
            "solar_illumination": 0.0,
            "status": status,
            "waypoint_index": 0,
            "total_waypoints": 0,
            "cumulative_distance_m": 0.0,
            "cumulative_energy_wh": 0.0,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            "rover": {
                "lat": 0.0,
                "lon": 0.0,
                "battery": 0.0,
                "status": status,
            },
            "error": error_msg,
        }
        await websocket.send_json(frame)
        await websocket.close(code=1008 if "Unknown" in error_msg else 1011)
    except Exception:  # noqa: BLE001
        pass
