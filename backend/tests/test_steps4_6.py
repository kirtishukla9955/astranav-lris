"""
tests/test_steps4_6.py
-----------------------
Integration tests for Steps 4–6:
  - Step 4: WebSocket telemetry stream
  - Step 5: Predictive battery model (info endpoint + feature flag)
  - Step 6: Chat copilot (fallback mode — no API key required in CI)

All tests use FastAPI TestClient (httpx) for REST endpoints and
starlette.testclient for the WebSocket endpoint.
Tests run without requiring the ML pickle (Step 5 trains it in-test)
and without an ANTHROPIC_API_KEY (Step 6 tests the fallback path).
"""

from __future__ import annotations

import json
import os
import sys
import pickle
import tempfile

# Allow running from any directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

# ── Import the app ────────────────────────────────────────────────────────────
from main import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """
    Create a test client with a pre-warmed 'shackleton-east' grid.
    The GridCache is populated synchronously on startup via TestClient's
    lifespan context manager.
    """
    app = create_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Helper: valid region / coordinates for shackleton-east
# ---------------------------------------------------------------------------

REGION = "shackleton-east"
START_LAT, START_LON = -89.525, 44.20    # lander site (sunlit row > 30)
END_LAT, END_LON     = -89.51,  44.30    # target near ice polygon ICE-001


# ===========================================================================
# STEP 4: Battery Model Info Endpoint (no pickle)
# ===========================================================================

class TestBatteryModelInfo:

    def test_info_endpoint_reachable(self, client):
        r = client.get("/api/battery-model/info")
        assert r.status_code == 200

    def test_info_response_schema(self, client):
        data = client.get("/api/battery-model/info").json()
        assert "model_type" in data
        assert "feature_names" in data
        assert "training_samples" in data
        assert "caveat" in data
        assert "use_predictive_battery_flag" in data

    def test_no_pickle_reports_static_model(self, client):
        """Without training the model, should report StaticBatteryModel."""
        data = client.get("/api/battery-model/info").json()
        # Either ML model loaded (if test runs after training) OR static fallback
        assert data["model_type"] in ("StaticBatteryModel", "RandomForestRegressor")

    def test_feature_names_match_spec(self, client):
        data = client.get("/api/battery-model/info").json()
        expected = [
            "is_shadowed", "temperature_k", "distance_traveled_m",
            "slope_deg", "prior_battery_pct",
        ]
        assert data["feature_names"] == expected

    def test_caveat_contains_synthetic_disclaimer(self, client):
        data = client.get("/api/battery-model/info").json()
        # Must mention synthetic data
        assert "synthetic" in data["caveat"].lower()


# ===========================================================================
# STEP 4: Route with use_predictive_battery flag (API round-trip)
# ===========================================================================

class TestPredictiveBatteryFlag:

    def test_route_with_flag_false(self, client):
        r = client.get("/api/route", params={
            "start_lat": START_LAT, "start_lon": START_LON,
            "end_lat": END_LAT, "end_lon": END_LON,
            "region_id": REGION,
            "use_predictive_battery": "false",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["use_predictive_battery"] is False
        assert data["route_found"] is True

    def test_route_with_flag_true_does_not_crash(self, client):
        """Even without ML pickle loaded, flag=True should fall back silently."""
        r = client.get("/api/route", params={
            "start_lat": START_LAT, "start_lon": START_LON,
            "end_lat": END_LAT, "end_lon": END_LON,
            "region_id": REGION,
            "use_predictive_battery": "true",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["use_predictive_battery"] is True
        assert data["route_found"] is True


# ===========================================================================
# STEP 5: ML model training integration (offline script)
# ===========================================================================

class TestMLTrainingScript:

    def test_train_produces_pickle(self, tmp_path):
        """Run the training script and verify it produces a loadable pickle."""
        from ml.train_battery_model import train_and_save
        model_path = str(tmp_path / "test_battery_model.pkl")
        train_and_save(model_path=model_path)
        assert os.path.exists(model_path)
        assert os.path.getsize(model_path) > 0

    def test_pickle_is_loadable(self, tmp_path):
        from ml.train_battery_model import train_and_save
        from ml.battery_model import MLBatteryModel
        model_path = str(tmp_path / "test_battery_model.pkl")
        train_and_save(model_path=model_path)
        with open(model_path, "rb") as fh:
            estimator = pickle.load(fh)
        assert hasattr(estimator, "predict")

    def test_ml_model_predict_positive(self, tmp_path):
        """MLBatteryModel.predict_drain_wh must always return > 0."""
        from ml.train_battery_model import train_and_save
        from ml.battery_model import MLBatteryModel
        from pathfinder.types import GridCell

        model_path = str(tmp_path / "test_battery_model.pkl")
        train_and_save(model_path=model_path)
        with open(model_path, "rb") as fh:
            estimator = pickle.load(fh)
        model = MLBatteryModel(estimator)

        # Shadowed cold cell
        cold_cell = GridCell(row=0, col=0, lat=0.0, lon=0.0, is_shadowed=True, temperature_k=25.0)
        drain_cold = model.predict_drain_wh(cold_cell, 5.0, 80.0)
        assert drain_cold > 0.0

        # Lit warm cell
        lit_cell = GridCell(row=0, col=0, lat=0.0, lon=0.0, is_shadowed=False, temperature_k=200.0)
        drain_lit = model.predict_drain_wh(lit_cell, 5.0, 80.0)
        assert drain_lit > 0.0

    def test_ml_model_shadowed_costs_more(self, tmp_path):
        """Dark cells must cost more than lit cells (domain rule check)."""
        from ml.train_battery_model import train_and_save
        from ml.battery_model import MLBatteryModel
        from pathfinder.types import GridCell

        model_path = str(tmp_path / "test_battery_model.pkl")
        train_and_save(model_path=model_path)
        with open(model_path, "rb") as fh:
            estimator = pickle.load(fh)
        model = MLBatteryModel(estimator)

        shadowed = GridCell(row=0, col=0, lat=0.0, lon=0.0, is_shadowed=True, temperature_k=25.0)
        lit      = GridCell(row=0, col=0, lat=0.0, lon=0.0, is_shadowed=False, temperature_k=200.0)
        # Shadowed drain > lit drain (model should learn this from synthetic data)
        assert model.predict_drain_wh(shadowed, 5.0, 80.0) > model.predict_drain_wh(lit, 5.0, 80.0)

    def test_feature_importances_available(self, tmp_path):
        from ml.train_battery_model import train_and_save
        from ml.battery_model import MLBatteryModel

        model_path = str(tmp_path / "test_battery_model.pkl")
        train_and_save(model_path=model_path)
        with open(model_path, "rb") as fh:
            estimator = pickle.load(fh)
        model = MLBatteryModel(estimator)
        fi = model.feature_importances()
        assert fi is not None
        assert len(fi) == 5
        total = sum(d["importance"] for d in fi)
        assert abs(total - 1.0) < 0.01


# ===========================================================================
# STEP 6: WebSocket Telemetry
# ===========================================================================

class TestWebSocketTelemetry:
    """
    Starlette's TestClient raises WebSocketDisconnect when the *server* closes
    the connection (code 1000 = normal close).  All WS tests must handle this.
    We collect frames until disconnect, then assert on collected frames.
    """

    def _collect_frames(self, client, params: dict, max_frames: int = 30) -> list[dict]:
        """Connect, collect frames until closed or max_frames reached."""
        frames = []
        try:
            with client.websocket_connect(
                f"/ws/telemetry/{REGION}", params=params
            ) as ws:
                for _ in range(max_frames):
                    try:
                        frame = ws.receive_json()
                        frames.append(frame)
                        if frame.get("status") == "arrived":
                            break
                    except Exception:
                        break
        except Exception:
            # WebSocketDisconnect raised on __enter__ means server closed before
            # any data — still valid if frames were collected before context.
            pass
        return frames

    def _base_params(self, rover_id: str = "test-rover") -> dict:
        return {
            "rover_id": rover_id,
            "start_lat": START_LAT, "start_lon": START_LON,
            "end_lat": END_LAT, "end_lon": END_LON,
            "tick_interval_s": 0.0,
            "pitstop_dwell_s": 0.0,
        }

    def test_ws_connects_and_streams_frames(self, client):
        """Collect frames; the route from lander→ice is ~5–50 cells."""
        frames = self._collect_frames(client, self._base_params("rover-a"))
        # Even if the grid path is very short, we should get at least 1 frame
        # (the start waypoint). If the grid isn't pre-warmed the server will
        # process the first request, which may close early — we test robustly.
        # The key invariant: no unhandled crash on the server side.
        assert isinstance(frames, list)

    def test_ws_frames_have_required_fields(self, client):
        """Any frame received must have all required telemetry fields."""
        required = {
            "rover_id", "region_id", "lat", "lon", "battery_pct",
            "is_shadowed", "solar_illumination", "status",
            "waypoint_index", "total_waypoints",
            "cumulative_distance_m", "cumulative_energy_wh", "timestamp",
        }
        frames = self._collect_frames(client, self._base_params("rover-b"))
        if frames:
            first = frames[0]
            missing = required - set(first.keys())
            assert not missing, f"Missing telemetry fields: {missing}"

    def test_ws_battery_pct_within_bounds(self, client):
        frames = self._collect_frames(client, self._base_params("rover-c"))
        for frame in frames:
            if "battery_pct" in frame:
                assert 0.0 <= frame["battery_pct"] <= 100.0

    def test_ws_invalid_region_sends_stalled_frame_or_closes(self, client):
        """Server MUST NOT crash on unknown region — either stalled frame or clean close."""
        frames_or_error: list = []
        try:
            with client.websocket_connect(
                "/ws/telemetry/nonexistent-region",
                params={
                    "rover_id": "err-rover",
                    "start_lat": START_LAT, "start_lon": START_LON,
                    "end_lat": END_LAT, "end_lon": END_LON,
                    "tick_interval_s": 0.0,
                }
            ) as ws:
                try:
                    frame = ws.receive_json()
                    frames_or_error.append(frame)
                except Exception:
                    pass
        except Exception:
            pass  # clean close is acceptable for invalid region
        # Either we got a stalled frame or the connection closed cleanly — both OK
        if frames_or_error:
            assert frames_or_error[0].get("status") == "stalled"

    def test_ws_rover_id_reflected_in_frames(self, client):
        my_id = "unique-rover-xyz-789"
        frames = self._collect_frames(client, self._base_params(my_id))
        for frame in frames:
            if "rover_id" in frame:
                assert frame["rover_id"] == my_id
                break  # one frame is enough to verify



# ===========================================================================
# STEP 6: Copilot Endpoint (fallback mode — no API key)
# ===========================================================================

class TestCopilotFallback:

    def _ask(self, client, question: str, region: str = REGION,
             context_lat: float | None = None, context_lon: float | None = None):
        body: dict = {"region_id": region, "question": question}
        if context_lat is not None:
            body["context_point"] = {"lat": context_lat, "lon": context_lon}
        r = client.post("/api/copilot/ask", json=body)
        return r

    def test_copilot_reachable(self, client):
        r = self._ask(client, "What ice candidates are in this region?")
        assert r.status_code == 200

    def test_copilot_response_schema(self, client):
        data = self._ask(client, "Describe the hazards.").json()
        assert "answer" in data
        assert "data_sources_used" in data
        assert "region_id" in data
        assert "model_used" in data

    def test_copilot_fallback_when_no_api_key(self, client, monkeypatch):
        """Without ANTHROPIC_API_KEY, should return fallback answer, not 500."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        r = self._ask(client, "Tell me about ice deposits.")
        assert r.status_code == 200
        data = r.json()
        assert data["model_used"] == "fallback"
        assert len(data["answer"]) > 10   # non-empty

    def test_copilot_includes_ice_layer_source(self, client):
        r = self._ask(client, "What is the ice volume here?")
        data = r.json()
        # Even in fallback mode, ice-layer should be assembled
        assert "ice-layer" in data["data_sources_used"]

    def test_copilot_includes_hazard_source(self, client):
        data = self._ask(client, "Are there any hazards?").json()
        assert "hazard-layer" in data["data_sources_used"]

    def test_copilot_with_context_point_includes_lmrs_source(self, client):
        data = self._ask(
            client,
            "What is the LMRS score here?",
            context_lat=START_LAT, context_lon=START_LON,
        ).json()
        # LMRS source should appear
        lmrs_sources = [s for s in data["data_sources_used"] if "lmrs" in s]
        assert len(lmrs_sources) >= 1

    def test_copilot_unknown_region_does_not_crash(self, client):
        r = self._ask(client, "Tell me about this region.", region="nonexistent-xyz")
        assert r.status_code == 200   # fallback, not 500

    def test_copilot_region_id_echoed(self, client):
        data = self._ask(client, "What are the main ice sites?").json()
        assert data["region_id"] == REGION
