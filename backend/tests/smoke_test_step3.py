"""Smoke-test all three Step 3 endpoints."""
import urllib.request, json, urllib.parse, sys

base = "http://localhost:8000"
ok = True

def check(label, url, method="GET", body=None):
    global ok
    try:
        if body:
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"}, method=method)
        else:
            req = urllib.request.Request(url, method=method)
        r = urllib.request.urlopen(req, timeout=30)
        data = json.loads(r.read())
        print(f"  PASS  {label}")
        return data
    except Exception as e:
        print(f"  FAIL  {label}: {e}")
        ok = False
        return None

print("\n=== AstraNav-LRIS Smoke Tests ===\n")

# Health
check("GET /health", f"{base}/health")

# Regions list
check("GET /api/regions", f"{base}/api/regions")

# Route — with tight dark budget to force pitstops
params = urllib.parse.urlencode({
    "start_lat": -89.55, "start_lon": 44.0,
    "end_lat": -89.537, "end_lon": 44.1,
    "region_id": "shackleton-east",
    "dark_budget_wh": 10.0,
})
route = check("GET /api/route", f"{base}/api/route?{params}")
if route:
    assert route["route_found"], "route_found should be True"
    assert route["total_pitstops"] >= 1, "Should have at least 1 pitstop with tight budget"
    assert route["waypoints"][0]["cumulative_distance_m"] == 0.0
    print(f"         pitstops={route['total_pitstops']}  dist={route['total_distance_m']}m  energy={route['total_energy_wh']}Wh")

# LMRS single point
params = urllib.parse.urlencode({"lat": -89.51, "lon": 44.29, "region_id": "shackleton-east"})
lmrs = check("GET /api/lmrs", f"{base}/api/lmrs?{params}")
if lmrs:
    assert 0 <= lmrs["lmrs_score"] <= 100
    assert lmrs["rai"]["ice_volume_m3"] > 0, "Ice should be detected near ICE-001 polygon"
    print(f"         lmrs={lmrs['lmrs_score']}  rai={lmrs['rai']['score']}  comm={lmrs['comm_visibility']['score']}  thermal={lmrs['thermal_risk']['score']}")

# LMRS compare
compare_body = {
    "region_id": "shackleton-east",
    "points": [
        {"lat": -89.51,  "lon": 44.29, "label": "Site-Alpha"},
        {"lat": -89.499, "lon": 44.51, "label": "Site-Beta"},
        {"lat": -89.521, "lon": 44.10, "label": "Site-Gamma"},
    ],
    "weights": {"rai": 0.45, "comm_visibility": 0.25, "thermal_risk": 0.30},
}
cmp = check("POST /api/lmrs/compare", f"{base}/api/lmrs/compare", "POST", compare_body)
if cmp:
    assert cmp["recommended"] in ["Site-Alpha", "Site-Beta", "Site-Gamma"]
    assert len(cmp["results"]) == 3
    scores = [r["lmrs_score"] for r in cmp["results"]]
    assert scores == sorted(scores, reverse=True), "Results should be sorted descending"
    print(f"         winner='{cmp['recommended']}'  scores={scores}")

# Swarm plan
swarm_body = {
    "region_id": "shackleton-east",
    "rovers": [
        {"rover_id": "rover-alpha", "start_lat": -89.55, "start_lon": 44.0,
         "end_lat": -89.537, "end_lon": 44.1, "initial_battery_pct": 100.0},
        {"rover_id": "rover-beta",  "start_lat": -89.548, "start_lon": 44.05,
         "end_lat": -89.535, "end_lon": 44.15, "initial_battery_pct": 85.0, "ice_seeking": True},
    ],
    "dark_budget_wh": 15.0,
}
swarm = check("POST /api/swarm/plan", f"{base}/api/swarm/plan", "POST", swarm_body)
if swarm:
    assert swarm["total_rovers"] == 2
    assert swarm["collision_avoidance"] == "not_implemented"
    for plan in swarm["plans"]:
        print(f"         rover={plan['rover_id']}  found={plan['route_found']}  dist={plan['total_distance_m']}m")

print()
print("ALL TESTS PASSED" if ok else "SOME TESTS FAILED")
sys.exit(0 if ok else 1)
