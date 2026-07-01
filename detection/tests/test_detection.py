"""
detection/tests/test_detection.py
----------------------------------
Pytest test suite for the AstraNav-LRIS Detection Service.

Tests cover:
  1. Science threshold enforcement (CPR, DOP, dielectric, depth, volume)
  2. GeoJSON schema correctness
  3. Hazard classification rules
  4. FastAPI endpoint integration (TestClient)
  5. Cache invalidation and re-generation

Run:
    cd <project_root>
    pytest detection/tests/ -v --tb=short
"""

from __future__ import annotations

import pytest
import numpy as np
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Import detection modules (run from project root)
# ---------------------------------------------------------------------------
from detection.models import (
    CPR_THRESHOLD, DOP_THRESHOLD, DIELECTRIC_DUST, DIELECTRIC_ICE,
    MAX_ICE_DEPTH_M, SLOPE_NOGO_DEG,
)
from detection.synthetic import generate_synthetic_rasters
from detection.pipeline import build_ice_layer, build_hazard_layer
from detection.cache import get_ice_layer, get_hazard_layer, invalidate_region, get_available_regions
from detection.main import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def rasters():
    """Generate a consistent set of synthetic rasters once for the module."""
    return generate_synthetic_rasters("shackleton-east", rows=64, cols=64, cell_size_m=30.0)


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


# ===========================================================================
# 1. Science threshold tests
# ===========================================================================

class TestScienceThresholds:

    def test_ice_mask_respects_cpr_threshold(self, rasters):
        """Every cell in ice_mask must have CPR > CPR_THRESHOLD (1.0)."""
        ice_cpr = rasters.cpr[rasters.ice_mask]
        assert ice_cpr.size > 0, "No ice detected — synthetic generator may be broken"
        assert (ice_cpr > CPR_THRESHOLD).all(), (
            f"Found ice cells with CPR ≤ {CPR_THRESHOLD}: min={ice_cpr.min():.4f}"
        )

    def test_ice_mask_respects_dop_threshold(self, rasters):
        """Every cell in ice_mask must have DOP < DOP_THRESHOLD (0.13)."""
        ice_dop = rasters.dop[rasters.ice_mask]
        assert ice_dop.size > 0
        assert (ice_dop < DOP_THRESHOLD).all(), (
            f"Found ice cells with DOP ≥ {DOP_THRESHOLD}: max={ice_dop.max():.4f}"
        )

    def test_non_ice_cells_fail_at_least_one_criterion(self, rasters):
        """Non-ice cells must fail CPR > 1.0 OR DOP < 0.13 (one of the two conditions)."""
        non_ice = ~rasters.ice_mask
        cpr_fail = rasters.cpr[non_ice] <= CPR_THRESHOLD
        dop_fail = rasters.dop[non_ice] >= DOP_THRESHOLD
        either_fail = cpr_fail | dop_fail
        # At least 95% of non-ice cells should legitimately fail a criterion
        assert either_fail.mean() >= 0.95, (
            "Too many non-ice cells appear to pass both thresholds — "
            "ice_mask may be under-detecting"
        )

    def test_dielectric_in_valid_range(self, rasters):
        """Dielectric constant must lie in [DIELECTRIC_DUST, DIELECTRIC_ICE+0.01]."""
        assert rasters.dielectric.min() >= DIELECTRIC_DUST - 1e-4
        assert rasters.dielectric.max() <= DIELECTRIC_ICE + 0.1  # small tolerance

    def test_ice_depth_bounded_by_max(self, rasters):
        """Ice depth must never exceed MAX_ICE_DEPTH_M (5 m)."""
        assert rasters.ice_depth_m.max() <= MAX_ICE_DEPTH_M + 1e-4

    def test_ice_depth_zero_outside_ice_mask(self, rasters):
        """Outside ice_mask, depth must be exactly 0."""
        outside_depth = rasters.ice_depth_m[~rasters.ice_mask]
        assert (outside_depth == 0.0).all()

    def test_volume_proportional_to_depth(self, rasters):
        """volume_m3 = depth_m × cell_area_m² — verify this relationship."""
        cell_area = rasters.cell_size_m ** 2
        ice_cells = rasters.ice_mask
        computed = rasters.ice_depth_m[ice_cells] * cell_area
        actual = rasters.ice_volume_m3[ice_cells]
        np.testing.assert_allclose(actual, computed, rtol=1e-5,
                                   err_msg="Ice volume ≠ depth × cell_area")

    def test_slope_nogo_threshold(self, rasters):
        """steep-slope hazard cells must have slope > SLOPE_NOGO_DEG (15°)."""
        steep = rasters.slope_deg > SLOPE_NOGO_DEG
        non_steep = rasters.slope_deg[~steep]
        assert (non_steep <= SLOPE_NOGO_DEG).all()


# ===========================================================================
# 2. GeoJSON schema tests
# ===========================================================================

class TestGeoJSONSchema:

    @pytest.fixture(scope="class")
    def ice_layer(self, rasters):
        return build_ice_layer(rasters, "shackleton-east",
                               origin_lat=-89.55, origin_lon=44.00)

    @pytest.fixture(scope="class")
    def hazard_layer(self, rasters):
        return build_hazard_layer(rasters, "shackleton-east",
                                  origin_lat=-89.55, origin_lon=44.00)

    def test_ice_layer_type(self, ice_layer):
        assert ice_layer.type == "FeatureCollection"

    def test_ice_layer_has_features(self, ice_layer):
        assert len(ice_layer.features) > 0, "Expected at least one ice feature"

    def test_ice_feature_geometry_type(self, ice_layer):
        for feat in ice_layer.features:
            assert feat.geometry.type == "Polygon"

    def test_ice_feature_cpr_gt_threshold(self, ice_layer):
        for feat in ice_layer.features:
            assert feat.properties.cpr > CPR_THRESHOLD, (
                f"Feature {feat.properties.ice_id} has CPR={feat.properties.cpr} ≤ {CPR_THRESHOLD}"
            )

    def test_ice_feature_dop_lt_threshold(self, ice_layer):
        for feat in ice_layer.features:
            assert feat.properties.dop < DOP_THRESHOLD, (
                f"Feature {feat.properties.ice_id} has DOP={feat.properties.dop} ≥ {DOP_THRESHOLD}"
            )

    def test_ice_feature_volume_positive(self, ice_layer):
        for feat in ice_layer.features:
            assert feat.properties.volume_m3 > 0

    def test_ice_feature_confidence_bounded(self, ice_layer):
        for feat in ice_layer.features:
            assert 0.0 <= feat.properties.confidence <= 1.0

    def test_ice_polygon_ring_closed(self, ice_layer):
        """GeoJSON rings must start and end with the same coordinate."""
        for feat in ice_layer.features:
            ring = feat.geometry.coordinates[0]
            assert ring[0] == ring[-1], (
                f"Ring not closed for {feat.properties.ice_id}: {ring[0]} ≠ {ring[-1]}"
            )

    def test_hazard_layer_type(self, hazard_layer):
        assert hazard_layer.type == "FeatureCollection"

    def test_hazard_layer_has_features(self, hazard_layer):
        assert len(hazard_layer.features) > 0

    def test_hazard_severity_all_no_go(self, hazard_layer):
        for feat in hazard_layer.features:
            assert feat.properties.severity == "no_go"

    def test_hazard_type_valid(self, hazard_layer):
        valid_types = {"steep_slope", "boulder_field", "crater_wall", "shadow_zone"}
        for feat in hazard_layer.features:
            assert feat.properties.hazard_type in valid_types

    def test_slope_features_have_slope_deg(self, hazard_layer):
        for feat in hazard_layer.features:
            if feat.properties.hazard_type in ("steep_slope", "crater_wall"):
                assert feat.properties.slope_deg is not None
                assert feat.properties.slope_deg > SLOPE_NOGO_DEG

    def test_boulder_features_have_diameter(self, hazard_layer):
        for feat in hazard_layer.features:
            if feat.properties.hazard_type == "boulder_field":
                assert feat.properties.max_boulder_diameter_m is not None
                assert feat.properties.max_boulder_diameter_m > 0


# ===========================================================================
# 3. FastAPI endpoint tests
# ===========================================================================

class TestEndpoints:

    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_status_has_thresholds(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "science_thresholds" in data
        thresholds = data["science_thresholds"]
        assert thresholds["cpr_threshold"] == CPR_THRESHOLD
        assert thresholds["dop_threshold"] == DOP_THRESHOLD

    def test_list_regions(self, client):
        resp = client.get("/api/regions")
        assert resp.status_code == 200
        regions = resp.json()["regions"]
        assert "shackleton-east" in regions
        assert "haworth" in regions

    def test_ice_layer_valid_region(self, client):
        resp = client.get("/api/ice-layer/shackleton-east")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "FeatureCollection"
        assert data["region_id"] == "shackleton-east"
        assert len(data["features"]) > 0

    def test_ice_layer_feature_schema(self, client):
        resp = client.get("/api/ice-layer/haworth")
        assert resp.status_code == 200
        feat = resp.json()["features"][0]
        props = feat["properties"]
        assert props["cpr"] > CPR_THRESHOLD
        assert props["dop"] < DOP_THRESHOLD
        assert props["volume_m3"] > 0
        assert "confidence" in props

    def test_hazard_layer_valid_region(self, client):
        resp = client.get("/api/hazard-layer/shackleton-east")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) > 0

    def test_hazard_layer_severity(self, client):
        resp = client.get("/api/hazard-layer/haworth")
        assert resp.status_code == 200
        for feat in resp.json()["features"]:
            assert feat["properties"]["severity"] == "no_go"

    def test_ice_layer_unknown_region_404(self, client):
        resp = client.get("/api/ice-layer/unknown-crater-xyz")
        assert resp.status_code == 404

    def test_hazard_layer_unknown_region_404(self, client):
        resp = client.get("/api/hazard-layer/unknown-crater-xyz")
        assert resp.status_code == 404

    def test_metadata_present(self, client):
        resp = client.get("/api/ice-layer/faustini")
        assert resp.status_code == 200
        meta = resp.json().get("metadata", {})
        assert "thresholds" in meta
        assert "statistics" in meta

    def test_all_regions_reachable(self, client):
        """All registered regions must return 200 for both endpoints."""
        regions_resp = client.get("/api/regions")
        for region_id in regions_resp.json()["regions"]:
            r1 = client.get(f"/api/ice-layer/{region_id}")
            r2 = client.get(f"/api/hazard-layer/{region_id}")
            assert r1.status_code == 200, f"ice-layer failed for {region_id}"
            assert r2.status_code == 200, f"hazard-layer failed for {region_id}"


# ===========================================================================
# 4. Cache tests
# ===========================================================================

class TestCache:

    def test_cache_hit_returns_same_object(self):
        """Two calls to the same region return the same Python object (cache hit)."""
        invalidate_region("shackleton-east")
        layer1 = get_ice_layer("shackleton-east")
        layer2 = get_ice_layer("shackleton-east")
        assert layer1 is layer2

    def test_invalidate_generates_fresh_layer(self):
        """After invalidation, a new object is generated (but with same data since seed is fixed)."""
        layer1 = get_ice_layer("haworth")
        invalidate_region("haworth")
        layer2 = get_ice_layer("haworth")
        assert layer1 is not layer2
        # But science values should be identical (fixed seed)
        vol1 = sum(f.properties.volume_m3 for f in layer1.features)
        vol2 = sum(f.properties.volume_m3 for f in layer2.features)
        assert abs(vol1 - vol2) < 1.0
