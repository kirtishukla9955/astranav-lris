"""
data/mock_fixtures.py
---------------------
Simulates the responses from Member 1's two endpoints:

  GET /api/ice-layer/{region_id}    → GeoJSON ice-candidate polygons
  GET /api/hazard-layer/{region_id} → GeoJSON no-go + obstacle polygons

HOW TO SWAP TO LIVE HTTP CALLS:
  Replace ``_fetch_ice_layer`` and ``_fetch_hazard_layer`` bodies with a
  single httpx.get() call — one-line change per function — and delete the
  MOCK_DATA dict below. Everything else (region loading, grid population)
  is unchanged.

Example swap:
    # BEFORE (mock):
    return MOCK_DATA["ice"][region_id]

    # AFTER (live):
    import httpx
    resp = httpx.get(f"{MEMBER1_BASE_URL}/api/ice-layer/{region_id}", timeout=10)
    resp.raise_for_status()
    return resp.json()
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Member 1 base URL (set to live URL when their service is up)
# ---------------------------------------------------------------------------
MEMBER1_BASE_URL = "http://localhost:8001"   # placeholder


# ---------------------------------------------------------------------------
# Mock GeoJSON payloads — realistic south-polar coordinates
# ---------------------------------------------------------------------------

MOCK_DATA: dict[str, dict[str, Any]] = {
    "ice": {
        "shackleton-east": {
            "type": "FeatureCollection",
            "region_id": "shackleton-east",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [44.28, -89.51], [44.30, -89.51],
                            [44.30, -89.50], [44.28, -89.50],
                            [44.28, -89.51],
                        ]],
                    },
                    "properties": {
                        "ice_id": "ICE-001",
                        "volume_m3": 4200.0,
                        "depth_m": 2.1,
                        "confidence": 0.87,
                        "cpr": 1.24,
                        "dop": 0.09,
                        "dielectric_constant": 3.5,
                    },
                },
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [44.40, -89.49], [44.43, -89.49],
                            [44.43, -89.48], [44.40, -89.48],
                            [44.40, -89.49],
                        ]],
                    },
                    "properties": {
                        "ice_id": "ICE-002",
                        "volume_m3": 1800.0,
                        "depth_m": 3.8,
                        "confidence": 0.63,
                        "cpr": 1.08,
                        "dop": 0.11,
                        "dielectric_constant": 3.2,
                    },
                },
            ],
        },
        "haworth": {
            "type": "FeatureCollection",
            "region_id": "haworth",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [0.10, -87.80], [0.14, -87.80],
                            [0.14, -87.78], [0.10, -87.78],
                            [0.10, -87.80],
                        ]],
                    },
                    "properties": {
                        "ice_id": "ICE-HAW-001",
                        "volume_m3": 9800.0,
                        "depth_m": 1.4,
                        "confidence": 0.91,
                        "cpr": 1.35,
                        "dop": 0.07,
                        "dielectric_constant": 3.7,
                    },
                },
            ],
        },
    },
    "hazard": {
        "shackleton-east": {
            "type": "FeatureCollection",
            "region_id": "shackleton-east",
            "features": [
                {
                    # Steep slope band — no-go zone
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [44.33, -89.52], [44.38, -89.52],
                            [44.38, -89.50], [44.33, -89.50],
                            [44.33, -89.52],
                        ]],
                    },
                    "properties": {
                        "hazard_id": "HAZ-001",
                        "hazard_type": "steep_slope",
                        "slope_deg": 22.4,
                        "severity": "no_go",
                    },
                },
                {
                    # Boulder field — collision hazard
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [44.46, -89.50], [44.49, -89.50],
                            [44.49, -89.49], [44.46, -89.49],
                            [44.46, -89.50],
                        ]],
                    },
                    "properties": {
                        "hazard_id": "HAZ-002",
                        "hazard_type": "boulder_field",
                        "max_boulder_diameter_m": 1.2,
                        "severity": "no_go",
                    },
                },
            ],
        },
        "haworth": {
            "type": "FeatureCollection",
            "region_id": "haworth",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [0.08, -87.82], [0.12, -87.82],
                            [0.12, -87.80], [0.08, -87.80],
                            [0.08, -87.82],
                        ]],
                    },
                    "properties": {
                        "hazard_id": "HAZ-HAW-001",
                        "hazard_type": "steep_slope",
                        "slope_deg": 18.1,
                        "severity": "no_go",
                    },
                },
            ],
        },
    },
}


# ---------------------------------------------------------------------------
# Fetch functions (swap body → live httpx call to go live)
# ---------------------------------------------------------------------------

def fetch_ice_layer(region_id: str) -> dict[str, Any]:
    """
    Return the ice-layer GeoJSON for a region.

    MOCK: reads from in-process MOCK_DATA dict.
    LIVE: replace body with:
        import httpx
        r = httpx.get(f"{MEMBER1_BASE_URL}/api/ice-layer/{region_id}", timeout=10)
        r.raise_for_status()
        return r.json()
    """
    data = MOCK_DATA["ice"].get(region_id)
    if data is None:
        raise KeyError(f"No ice-layer mock for region '{region_id}'")
    return data


def fetch_hazard_layer(region_id: str) -> dict[str, Any]:
    """
    Return the hazard-layer GeoJSON for a region.

    MOCK: reads from in-process MOCK_DATA dict.
    LIVE: replace body with:
        import httpx
        r = httpx.get(f"{MEMBER1_BASE_URL}/api/hazard-layer/{region_id}", timeout=10)
        r.raise_for_status()
        return r.json()
    """
    data = MOCK_DATA["hazard"].get(region_id)
    if data is None:
        raise KeyError(f"No hazard-layer mock for region '{region_id}'")
    return data


def list_regions() -> list[str]:
    """Return all region IDs that have both ice and hazard mock data."""
    return list(set(MOCK_DATA["ice"].keys()) & set(MOCK_DATA["hazard"].keys()))
