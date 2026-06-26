# AstraNav-LRIS — Frontend (Team Aura++)

A complete, two-part frontend for **AstraNav-LRIS**, built for ISRO's
Bharatiya Antariksh Hackathon 2026 (Problem Statement 08).

This is a static build — plain HTML/CSS/JS, no framework, no build step.
Everything runs by opening a file or serving the folder; nothing needs `npm install`.

## The two pages

### 1. `index.html` — the landing / marketing page
What a judge, mentor, or recruiter sees first. A cinematic hero built around
a real WebGL 3D Moon, the mission pipeline, the 9 features, the tech stack,
and the team — all on the obsidian/cyan/amber theme.

### 2. `dashboard.html` — Mission Control (the actual product)
A working, interactive simulation of the real AstraNav-LRIS tool, covering
**all 9 planned features** against a procedurally generated (but
deterministic, per-region) lunar south-pole terrain grid:

| # | Feature | Where it lives in the dashboard |
|---|---|---|
| 1 | Volumetric Ice Detection (CPR/DOP → m³ down to 5m) | "Ice Detection" layer + LMRS panel's ice volume/depth |
| 2 | Terrain Hazard Masking (slope > 15°, obstacles > 0.5m) | "Hazard Mask" layer (red no-go zones) |
| 3 | Shadow-Hopping Pathfinder (A* + solar pitstops) | **Plan Route** mode — click a start and end point |
| 4 | Lunar Mining Readiness Score (LMRS) | Right-hand slide-out panel on any map click |
| 5 | Multi-Site Comparison | **Compare Sites** mode — click up to 3 points |
| 6 | Multi-Rover Swarm View | **Swarm View** mode — two rovers, two live routes |
| 7 | Mission Copilot Q&A | Floating chat button, bottom-right |
| 8 | Predictive Battery Model | "Predicted Battery" sparkline in the route rail |
| 9 | Detection Confidence Overlay | "Confidence Overlay" layer toggle |

The pathfinding is a **real A\*** search over a weighted grid (hazard cells
are impassable, shadowed cells carry a heavy darkness-cost penalty, and the
planner auto-inserts solar-charging waypoints when a route's dark-dwell time
would exceed a battery budget) — not a decorative animation. The LMRS,
energy estimates, and comparison table are all computed from that same
underlying grid, so the numbers are internally consistent with what you see
on the map.

The Mission Copilot is intentionally **fully offline** (template logic over
the real grid/route data, no API calls) — this is a deliberate hackathon
choice: a live demo should never go dark because the venue's wifi did.

## What's inside

```
index.html         landing page markup
dashboard.html      mission control app markup
style.css           shared design system (landing) — tokens, nav, buttons, reveals
dashboard.css       mission control layout (rail, map stage, panels, drawers)
script.js           landing page behavior: starfield, 3D moon, reveals, counters
dashboard.js        mission control logic: terrain gen, A*, LMRS, swarm, copilot
assets/three.min.js Three.js r128, bundled locally — no CDN dependency
assets/moon_albedo.jpg, moon_bump.jpg   real lunar photographic textures
```

## Running it locally

No install needed.

```bash
python3 -m http.server 8080
# then open http://localhost:8080
```

Or just double-click `index.html` / `dashboard.html` directly — everything
except the Google Fonts link works fully offline.

## Deploying

Static site, zero config, deploy anywhere:
- **Vercel** — drag the folder in, or `vercel deploy` from this directory.
- **GitHub Pages** — push and enable Pages.
- **Netlify** — drag-and-drop onto the dashboard.

Both pages are already cross-linked: the landing page's "Launch Dashboard"
buttons point to `dashboard.html`, and the dashboard's "← Back to Site"
points back to `index.html`. No path changes needed after deploying.

## What to customize before sharing

1. **Repo link** — search `index.html` for `target="_blank"` and point
   "View Repo" / "Open Repository" at your real GitHub URL.
2. **Team section** — `index.html`'s team cards currently list roles only;
   add real names if you want.
3. **Real data** — the dashboard's terrain (craters, ice, hazards) is
   procedurally generated per region with a fixed seed, so it's consistent
   across reloads but is **not** real Chandrayaan-2 data. When Member 1 and
   Member 2's actual backends are ready, the cleanest integration point is
   `generateRegion()` in `dashboard.js` — swap its output for a real API
   response in the same `{ cells, craters, landing }` shape and the rest of
   the dashboard (routing, LMRS, swarm, copilot) keeps working unchanged.

## Design notes

- **Theme**: obsidian black + electric cyan (cold/ice) + warm amber
  (solar/energy) — the product's whole hook is that duality, so the palette
  carries it everywhere, including the route-vs-pitstop color coding.
- **The hero Moon** uses real photographic lunar textures (not procedural),
  tilted so the south pole — the entire subject of the product — faces the
  camera directly, with a glowing latitude ring marking the surveyed PSR
  region. The ring is geometrically positioned just outside the sphere's
  surface at that latitude so it's never occluded by the moon mesh itself.
- **Route line styling**: a dark outline + dashed white "planned" segment +
  solid glowing "traveled" segment, specifically so the path stays legible
  whether it crosses the ice layer, the hazard layer, or bare terrain.
- **Unreachable sites are shown honestly** — if a crater floor has no
  passable corridor through its hazard rim from the landing zone, the LMRS
  panel and comparison table say so explicitly rather than faking a number.
- Respects `prefers-reduced-motion` on the landing page.
- Mobile: the landing page is fully responsive; the dashboard (a real
  GIS-style tool) collapses its side rail into a slide-in drawer via the
  ☰ icon in the top bar on narrow screens.
