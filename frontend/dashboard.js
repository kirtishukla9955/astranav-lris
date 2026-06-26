/* ===================================================================
   ASTRANAV-LRIS — MISSION CONTROL DASHBOARD LOGIC
   Sections:
     0) Starfield backdrop
     1) Seeded RNG + region generation (grid data model)
     2) Rendering pipeline
     3) A* pathfinder + pitstop insertion + energy/battery model
     4) Interaction: modes, clicks, hover
     5) LMRS scoring + panel
     6) Compare mode
     7) Swarm mode
     8) Mission Copilot
     9) Top bar clock + boot
=================================================================== */

/* ---------------------------------------------------------------
   0) STARFIELD BACKDROP (same lightweight treatment as the landing page)
---------------------------------------------------------------- */
(function starfield(){
  const canvas = document.getElementById('bg-stars');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let stars = [], w, h;
  function resize(){
    w = canvas.width = window.innerWidth;
    h = canvas.height = window.innerHeight;
    const count = Math.floor((w * h) / 11000);
    stars = Array.from({ length: count }, () => ({
      x: Math.random() * w, y: Math.random() * h,
      r: Math.random() * 1 + 0.2,
      a: Math.random() * 0.5 + 0.15,
      sp: Math.random() * 0.015 + 0.004,
      ph: Math.random() * Math.PI * 2,
    }));
  }
  let t = 0;
  function draw(){
    t++;
    ctx.clearRect(0, 0, w, h);
    for (const s of stars) {
      const a = s.a + Math.sin(t * s.sp + s.ph) * 0.2;
      ctx.beginPath();
      ctx.fillStyle = `rgba(220,235,240,${Math.max(0, a)})`;
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      ctx.fill();
    }
    requestAnimationFrame(draw);
  }
  window.addEventListener('resize', resize);
  resize(); draw();
})();

/* ---------------------------------------------------------------
   1) SEEDED RNG + REGION GENERATION
---------------------------------------------------------------- */
function mulberry32(seed) {
  return function () {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const GRID_COLS = 46, GRID_ROWS = 28;
const DARK_PENALTY = 4.2;
const BASE_WH_PER_CELL = 2.1;
const SHADOW_WH_EXTRA = 5.4;
const DARK_BUDGET_CELLS = 5;

const REGION_SEEDS = { shackleton: 1337, faustini: 5021, degerlache: 8842 };
const REGION_LABELS = {
  shackleton: 'Shackleton Rim · 89.9°S',
  faustini: 'Faustini Crater · 87.3°S',
  degerlache: 'de Gerlache Rim · 88.5°S',
};

let region = null; // current region data
let currentRegionKey = 'shackleton';

function generateRegion(key) {
  const rng = mulberry32(REGION_SEEDS[key]);
  const cols = GRID_COLS, rows = GRID_ROWS;
  const cells = [];
  for (let y = 0; y < rows; y++) {
    const row = [];
    for (let x = 0; x < cols; x++) {
      row.push({ x, y, elevation: 0.5, shadow: false, hazard: false, boulder: false, ice: null, craterId: -1 });
    }
    cells.push(row);
  }

  // Place craters
  const craterCount = 5 + Math.floor(rng() * 3);
  const craters = [];
  for (let i = 0; i < craterCount; i++) {
    const cx = 4 + rng() * (cols - 8);
    const cy = 4 + rng() * (rows - 8);
    const r = 3 + rng() * 4.2;
    craters.push({ cx, cy, r, id: i });
  }

  for (let y = 0; y < rows; y++) {
    for (let x = 0; x < cols; x++) {
      const cell = cells[y][x];
      for (const c of craters) {
        const d = Math.hypot(x - c.cx, y - c.cy);
        if (d < c.r * 0.78) {
          cell.shadow = true;
          cell.craterId = c.id;
          cell.elevation = 0.18 + (d / c.r) * 0.12;
        } else if (d < c.r) {
          cell.hazard = true; // rim slope > 15°
          cell.craterId = c.id;
          cell.elevation = 0.75 + rng() * 0.15;
        }
      }
    }
  }

  // Scatter boulder hazards outside craters
  const boulderCount = Math.floor(cols * rows * 0.035);
  for (let i = 0; i < boulderCount; i++) {
    const x = Math.floor(rng() * cols), y = Math.floor(rng() * rows);
    const cell = cells[y][x];
    if (!cell.shadow && !cell.hazard) { cell.boulder = true; cell.hazard = true; }
  }

  // Carve 1-2 "safe corridor" gaps through each crater's rim.
  // Real crater rims aren't a uniform wall — slope varies around the
  // circumference, and ISRO's own site-selection material references
  // lower-slope corridors (e.g. on Shackleton's rim) for exactly this
  // reason. Without this, every ice floor would be a sealed, unreachable
  // island, which would make routing/LMRS meaningless for most of the map.
  for (const c of craters) {
    const gapCount = c.r > 5 ? 2 : 1;
    const gapAngles = Array.from({ length: gapCount }, () => rng() * Math.PI * 2);
    const gapWidth = 0.5; // radians (~28°)

    for (let y = 0; y < rows; y++) {
      for (let x = 0; x < cols; x++) {
        const cell = cells[y][x];
        if (cell.craterId !== c.id || !cell.hazard) continue;
        const angle = Math.atan2(y - c.cy, x - c.cx);
        const inGap = gapAngles.some(ga => {
          let diff = Math.abs(angle - ga);
          if (diff > Math.PI) diff = Math.PI * 2 - diff;
          return diff < gapWidth / 2;
        });
        if (inGap) {
          cell.hazard = false;
          cell.boulder = false;
          cell.corridor = true;
          cell.elevation = 0.5 + rng() * 0.08; // gentle slope, not a cliff
        }
      }
    }
  }

  // Ice candidates within shadowed crater floors
  for (const c of craters) {
    for (let y = 0; y < rows; y++) {
      for (let x = 0; x < cols; x++) {
        const cell = cells[y][x];
        if (cell.craterId === c.id && cell.shadow && rng() < 0.55) {
          const distFactor = 1 - Math.hypot(x - c.cx, y - c.cy) / (c.r * 0.78);
          cell.ice = {
            volume_m3: Math.round((800 + rng() * 5200) * (0.5 + distFactor * 0.6)),
            depth_m: +(1 + rng() * 4).toFixed(1),
            confidence: Math.min(0.97, Math.max(0.42, 0.5 + distFactor * 0.4 + (rng() - 0.5) * 0.15)),
          };
        }
      }
    }
  }

  // Landing zone (fixed reference point, top-left margin, always safe)
  const landing = { x: 2, y: 2 };
  cells[landing.y][landing.x].shadow = false;
  cells[landing.y][landing.x].hazard = false;

  return { key, cells, craters, cols, rows, landing, label: REGION_LABELS[key] };
}

function cellAt(gx, gy) {
  if (gx < 0 || gy < 0 || gx >= region.cols || gy >= region.rows) return null;
  return region.cells[gy][gx];
}

/* ---------------------------------------------------------------
   2) RENDERING PIPELINE
---------------------------------------------------------------- */
const canvas = document.getElementById('mapCanvas');
const ctx = canvas.getContext('2d');
let cellW, cellH, dpr;
let terrainNoise = null; // cached per-cell brightness noise for the background

function resizeCanvas() {
  const rect = canvas.parentElement.getBoundingClientRect();
  dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  cellW = rect.width / GRID_COLS;
  cellH = rect.height / GRID_ROWS;
  render();
}
window.addEventListener('resize', resizeCanvas);

function buildTerrainNoise() {
  const rng = mulberry32(REGION_SEEDS[currentRegionKey] + 99);
  terrainNoise = [];
  for (let y = 0; y < region.rows; y++) {
    const row = [];
    for (let x = 0; x < region.cols; x++) row.push(rng());
    terrainNoise.push(row);
  }
}

const layers = { ice: true, hazard: true, confidence: false, route: true };

function render() {
  const W = canvas.width / dpr, H = canvas.height / dpr;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#050608';
  ctx.fillRect(0, 0, W, H);

  // --- base terrain shading ---
  for (let y = 0; y < region.rows; y++) {
    for (let x = 0; x < region.cols; x++) {
      const cell = region.cells[y][x];
      const n = terrainNoise[y][x];
      let base = 26 + cell.elevation * 60 + (n - 0.5) * 14;
      base = Math.max(8, Math.min(110, base));
      ctx.fillStyle = `rgb(${base},${base + 2},${base + 4})`;
      ctx.fillRect(x * cellW, y * cellH, cellW + 0.5, cellH + 0.5);
    }
  }

  // subtle grid graticule
  ctx.strokeStyle = 'rgba(255,255,255,0.035)';
  ctx.lineWidth = 1;
  for (let x = 0; x <= region.cols; x += 4) {
    ctx.beginPath(); ctx.moveTo(x * cellW, 0); ctx.lineTo(x * cellW, H); ctx.stroke();
  }
  for (let y = 0; y <= region.rows; y += 4) {
    ctx.beginPath(); ctx.moveTo(0, y * cellH); ctx.lineTo(W, y * cellH); ctx.stroke();
  }

  // --- confidence overlay (soft blobs, drawn under crisp ice cells) ---
  if (layers.confidence) {
    for (let y = 0; y < region.rows; y++) {
      for (let x = 0; x < region.cols; x++) {
        const cell = region.cells[y][x];
        if (!cell.ice) continue;
        const px = x * cellW + cellW / 2, py = y * cellH + cellH / 2;
        const r = Math.max(cellW, cellH) * 1.8;
        const g = ctx.createRadialGradient(px, py, 0, px, py, r);
        const op = cell.ice.confidence * 0.45;
        g.addColorStop(0, `rgba(157,127,232,${op})`);
        g.addColorStop(1, 'rgba(157,127,232,0)');
        ctx.fillStyle = g;
        ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2); ctx.fill();
      }
    }
  }

  // --- hazard layer ---
  if (layers.hazard) {
    for (let y = 0; y < region.rows; y++) {
      for (let x = 0; x < region.cols; x++) {
        const cell = region.cells[y][x];
        if (!cell.hazard) continue;
        ctx.fillStyle = cell.boulder ? 'rgba(255,107,94,0.5)' : 'rgba(255,107,94,0.22)';
        ctx.fillRect(x * cellW, y * cellH, cellW + 0.5, cellH + 0.5);
        if (!cell.boulder) {
          ctx.strokeStyle = 'rgba(255,107,94,0.35)';
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(x * cellW, (y + 1) * cellH);
          ctx.lineTo((x + 1) * cellW, y * cellH);
          ctx.stroke();
        }
      }
    }
  }

  // --- ice layer ---
  if (layers.ice) {
    for (let y = 0; y < region.rows; y++) {
      for (let x = 0; x < region.cols; x++) {
        const cell = region.cells[y][x];
        if (!cell.ice) continue;
        const alpha = 0.22 + cell.ice.confidence * 0.35;
        ctx.fillStyle = `rgba(63,231,236,${alpha})`;
        ctx.fillRect(x * cellW + 1, y * cellH + 1, cellW - 1.5, cellH - 1.5);
      }
    }
  }

  // --- landing zone marker ---
  drawFlag(region.landing.x, region.landing.y, '#e8e8e6', 'LZ');

  // --- compare pins ---
  compareSet.forEach((c, i) => drawPin(c.x, c.y, i + 1));

  // --- route layer ---
  if (layers.route) {
    if (activeRoute) drawRoute(activeRoute, '#3fe7ec', routeProgress);
    if (mode === 'swarm') {
      if (swarm.a.route) drawRoute(swarm.a.route, '#3fe7ec', swarm.a.progress);
      if (swarm.b.route) drawRoute(swarm.b.route, '#ffb255', swarm.b.progress);
    }
  }

  // --- selection markers for route mode ---
  if (routeStart) drawFlag(routeStart.x, routeStart.y, '#3fe7ec', 'A');
  if (routeEnd) drawFlag(routeEnd.x, routeEnd.y, '#ff6b5e', 'B');

  // --- hover highlight ---
  if (hoverCell) {
    ctx.strokeStyle = 'rgba(255,255,255,0.5)';
    ctx.lineWidth = 1.4;
    ctx.strokeRect(hoverCell.x * cellW, hoverCell.y * cellH, cellW, cellH);
  }
}

function cellCenter(gx, gy) { return [gx * cellW + cellW / 2, gy * cellH + cellH / 2]; }

function drawFlag(gx, gy, color, label) {
  const [px, py] = cellCenter(gx, gy);
  ctx.beginPath(); ctx.arc(px, py, 6, 0, Math.PI * 2);
  ctx.fillStyle = color; ctx.fill();
  ctx.strokeStyle = 'rgba(0,0,0,0.5)'; ctx.lineWidth = 1.5; ctx.stroke();
  ctx.fillStyle = '#04050a';
  ctx.font = '700 9px IBM Plex Mono, monospace';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(label, px, py + 0.5);
}

function drawPin(gx, gy, num) {
  const [px, py] = cellCenter(gx, gy);
  ctx.beginPath(); ctx.arc(px, py, 8, 0, Math.PI * 2);
  ctx.fillStyle = 'rgba(230,230,228,0.95)'; ctx.fill();
  ctx.strokeStyle = 'rgba(0,0,0,0.4)'; ctx.lineWidth = 1; ctx.stroke();
  ctx.fillStyle = '#0c0c0c';
  ctx.font = '700 10px IBM Plex Mono, monospace';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(String(num), px, py + 0.5);
}

function drawRoute(routeData, color, progress) {
  const pts = routeData.path.map(p => cellCenter(p.x, p.y));
  if (pts.length < 2) return;
  const upto = Math.max(1, Math.floor(pts.length * progress));

  // dark outline underneath the full path so it reads clearly over bright
  // ice cells, hazard tint, or confidence blobs alike
  ctx.save();
  ctx.strokeStyle = 'rgba(2,3,6,0.85)';
  ctx.lineWidth = 5.4; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
  ctx.stroke();
  ctx.restore();

  // un-traveled portion: dashed, neutral white so it reads as "planned" not "live"
  ctx.save();
  ctx.strokeStyle = 'rgba(235,242,245,0.85)';
  ctx.lineWidth = 1.8; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  ctx.setLineDash([5, 5]);
  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.restore();

  // traveled portion: solid glowing color
  ctx.save();
  ctx.shadowColor = color; ctx.shadowBlur = 10;
  ctx.strokeStyle = color; ctx.lineWidth = 3; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  for (let i = 1; i < upto; i++) ctx.lineTo(pts[i][0], pts[i][1]);
  ctx.stroke();
  ctx.restore();

  // pitstop markers
  routeData.pitstopIndices.forEach(idx => {
    const [px, py] = pts[Math.min(idx, pts.length - 1)];
    ctx.beginPath(); ctx.arc(px, py, 5.5, 0, Math.PI * 2);
    ctx.fillStyle = '#ffe28a'; ctx.fill();
    ctx.strokeStyle = '#04050a'; ctx.lineWidth = 1.2; ctx.stroke();
  });

  // moving rover marker
  const ri = Math.min(upto, pts.length - 1);
  const [rx, ry] = pts[ri];
  ctx.beginPath(); ctx.arc(rx, ry, 6.5, 0, Math.PI * 2);
  ctx.fillStyle = '#04050a'; ctx.fill();
  ctx.beginPath(); ctx.arc(rx, ry, 5.5, 0, Math.PI * 2);
  ctx.fillStyle = color; ctx.shadowColor = color; ctx.shadowBlur = 14;
  ctx.fill();
}


/* ---------------------------------------------------------------
   3) A* PATHFINDER + PITSTOP INSERTION + ENERGY MODEL
---------------------------------------------------------------- */
function cellCost(cell) {
  if (cell.hazard) return Infinity;
  return 1 + (cell.shadow ? DARK_PENALTY : 0);
}

function astar(start, end) {
  const cols = region.cols, rows = region.rows;
  const key = (x, y) => y * cols + x;
  const open = new Map();
  const gScore = new Map();
  const fScore = new Map();
  const came = new Map();
  const h = (x, y) => Math.hypot(x - end.x, y - end.y);

  gScore.set(key(start.x, start.y), 0);
  fScore.set(key(start.x, start.y), h(start.x, start.y));
  open.set(key(start.x, start.y), start);

  const dirs = [[1,0],[-1,0],[0,1],[0,-1],[1,1],[1,-1],[-1,1],[-1,-1]];

  let guard = 0;
  while (open.size > 0 && guard++ < 20000) {
    let curKey = null, curNode = null, bestF = Infinity;
    for (const [k, node] of open) {
      const f = fScore.get(k) ?? Infinity;
      if (f < bestF) { bestF = f; curKey = k; curNode = node; }
    }
    if (curNode.x === end.x && curNode.y === end.y) {
      const path = [curNode];
      let ck = curKey;
      while (came.has(ck)) {
        const prev = came.get(ck);
        path.unshift(prev.node);
        ck = key(prev.node.x, prev.node.y);
      }
      return path;
    }
    open.delete(curKey);

    for (const [dx, dy] of dirs) {
      const nx = curNode.x + dx, ny = curNode.y + dy;
      const ncell = cellAt(nx, ny);
      if (!ncell) continue;
      const stepCost = cellCost(ncell) * (dx !== 0 && dy !== 0 ? 1.41 : 1);
      if (!isFinite(stepCost)) continue;
      const tentative = (gScore.get(curKey) ?? Infinity) + stepCost;
      const nk = key(nx, ny);
      if (tentative < (gScore.get(nk) ?? Infinity)) {
        came.set(nk, { node: curNode });
        gScore.set(nk, tentative);
        fScore.set(nk, tentative + h(nx, ny));
        open.set(nk, { x: nx, y: ny });
      }
    }
  }
  return null; // no path found
}

function nearestSunlitNeighbor(gx, gy) {
  let best = null, bestD = Infinity;
  for (let r = 1; r <= 4; r++) {
    for (let dy = -r; dy <= r; dy++) {
      for (let dx = -r; dx <= r; dx++) {
        const c = cellAt(gx + dx, gy + dy);
        if (c && !c.hazard && !c.shadow) {
          const d = Math.hypot(dx, dy);
          if (d < bestD) { bestD = d; best = c; }
        }
      }
    }
    if (best) return best;
  }
  return null;
}

function buildRoute(start, end) {
  const rawPath = astar(start, end);
  if (!rawPath) return null;

  const path = rawPath.map(p => ({ x: p.x, y: p.y }));
  const pitstopIndices = [];
  let darkRun = 0;

  for (let i = 0; i < path.length; i++) {
    const c = cellAt(path[i].x, path[i].y);
    if (c.shadow) {
      darkRun++;
      if (darkRun === DARK_BUDGET_CELLS) {
        const sun = nearestSunlitNeighbor(path[i].x, path[i].y);
        if (sun) {
          path.splice(i + 1, 0, { x: sun.x, y: sun.y, pitstop: true });
          pitstopIndices.push(i + 1);
          i++;
        }
        darkRun = 0;
      }
    } else {
      darkRun = 0;
    }
  }

  // energy + battery telemetry per step
  const energySteps = [];
  let cumWh = 0, battery = 100;
  const CAPACITY_WH = 420;
  for (let i = 0; i < path.length; i++) {
    const c = cellAt(path[i].x, path[i].y);
    const stepWh = path[i].pitstop ? -38 : BASE_WH_PER_CELL + (c.shadow ? SHADOW_WH_EXTRA : 0);
    cumWh = Math.max(0, cumWh + stepWh);
    battery = Math.max(2, Math.min(100, 100 - (cumWh / CAPACITY_WH) * 100));
    energySteps.push({ wh: cumWh, battery, shadow: c.shadow });
  }

  const darkCells = path.filter(p => cellAt(p.x, p.y).shadow).length;
  const distanceM = (path.length - 1) * 140;

  return {
    path, pitstopIndices, energySteps,
    totalWh: Math.round(cumWh),
    distanceM,
    darkCells,
    darkMinutes: Math.round(darkCells * 1.8),
  };
}

/* ---------------------------------------------------------------
   4) INTERACTION: MODES, CLICKS, HOVER
---------------------------------------------------------------- */
let mode = 'route';
let routeStart = null, routeEnd = null;
let activeRoute = null, routeProgress = 0, playing = false, playRAF = null;
let hoverCell = null;
let compareSet = [];
let lastLmrs = null, lastLmrsCell = null;
const swarm = { a: { route: null, progress: 0 }, b: { route: null, progress: 0 }, playing: false };

function gridFromEvent(e) {
  const rect = canvas.getBoundingClientRect();
  const x = e.clientX - rect.left, y = e.clientY - rect.top;
  const gx = Math.floor(x / cellW), gy = Math.floor(y / cellH);
  return { x: gx, y: gy, px: x, py: y };
}

canvas.addEventListener('mousemove', (e) => {
  const g = gridFromEvent(e);
  if (g.x < 0 || g.y < 0 || g.x >= region.cols || g.y >= region.rows) { hoverCell = null; }
  else hoverCell = g;
  updateCursorReadout(g);
  render();
});
canvas.addEventListener('mouseleave', () => { hoverCell = null; render(); });

canvas.addEventListener('click', (e) => {
  const g = gridFromEvent(e);
  if (g.x < 0 || g.y < 0 || g.x >= region.cols || g.y >= region.rows) return;
  const cell = cellAt(g.x, g.y);
  if (cell.hazard && mode === 'route' && !routeStart) {
    flashHint('Cannot start in a hazard zone — pick a safer point.');
    return;
  }

  if (mode === 'route') {
    if (!routeStart) { routeStart = { x: g.x, y: g.y }; setModeHint('Now click an end point.'); }
    else if (!routeEnd) {
      routeEnd = { x: g.x, y: g.y };
      computeAndShowRoute();
    } else {
      routeStart = { x: g.x, y: g.y }; routeEnd = null; activeRoute = null;
      stopPlayback(); resetRouteStats();
      setModeHint('Now click an end point.');
    }
  } else if (mode === 'compare') {
    if (compareSet.length >= 3) { flashHint('Comparison set is full (3 max). Clear to add more.'); }
    else { compareSet.push({ x: g.x, y: g.y }); renderCompareList(); }
  }

  openLmrsFor(g.x, g.y);
  render();
});

function updateCursorReadout(g) {
  const el = document.getElementById('cursorReadout');
  if (!el) return;
  if (g.x < 0 || g.y < 0 || g.x >= region.cols || g.y >= region.rows) { el.textContent = 'LAT — · LON —'; return; }
  const lat = -(89.9 - g.y * 0.02).toFixed(3);
  const lon = (g.x * 0.035 - 0.7).toFixed(3);
  el.textContent = `LAT ${lat}°S · LON ${lon}°`;
}

function setModeHint(text) { const el = document.getElementById('modeHint'); if (el) el.textContent = text; }
function flashHint(text) {
  setModeHint(text);
  setTimeout(() => { if (mode === 'route') setModeHint(routeStart ? 'Now click an end point.' : 'Click a start point, then an end point, on the map.'); }, 2200);
}

document.querySelectorAll('.mode-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.mode-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    mode = btn.dataset.mode;
    document.getElementById('routeControls').hidden = mode !== 'route';
    document.getElementById('compareControls').hidden = mode !== 'compare';
    document.getElementById('swarmControls').hidden = mode !== 'swarm';
    if (mode === 'route') setModeHint(routeStart ? (routeEnd ? '' : 'Now click an end point.') : 'Click a start point, then an end point, on the map.');
    if (mode === 'compare') setModeHint('Click up to 3 points on the map to shortlist candidate sites.');
    if (mode === 'swarm') { setModeHint('Two rovers are auto-routed to nearby ice sites. Press Play.'); ensureSwarmRoutes(); }
    if (window.innerWidth <= 760) { railEl.classList.remove('open'); railBackdrop.classList.remove('open'); }
    render();
  });
});

['layerIce','layerHazard','layerConfidence','layerRoute'].forEach(id => {
  document.getElementById(id).addEventListener('change', (e) => {
    const map = { layerIce: 'ice', layerHazard: 'hazard', layerConfidence: 'confidence', layerRoute: 'route' };
    layers[map[id]] = e.target.checked;
    render();
  });
});

// Mobile rail toggle — the layers/mode/playback panel slides in as a drawer
// on narrow viewports since there's no room for a persistent sidebar.
const railEl = document.querySelector('.rail');
const railBackdrop = document.getElementById('railBackdrop');
document.getElementById('railToggle').addEventListener('click', () => {
  railEl.classList.add('open');
  railBackdrop.classList.add('open');
});
railBackdrop.addEventListener('click', () => {
  railEl.classList.remove('open');
  railBackdrop.classList.remove('open');
});

document.getElementById('regionSelect').addEventListener('change', (e) => {
  loadRegion(e.target.value);
});

/* ---- route compute / playback ---- */
function computeAndShowRoute() {
  const r = buildRoute(routeStart, routeEnd);
  if (!r) { flashHint('No safe path found — hazard zones block this route.'); routeEnd = null; return; }
  activeRoute = r; routeProgress = 0;
  document.getElementById('statDistance').textContent = `${(r.distanceM / 1000).toFixed(2)} km`;
  document.getElementById('statPitstops').textContent = r.pitstopIndices.length;
  document.getElementById('statEnergy').textContent = `${r.totalWh} Wh`;
  document.getElementById('statDark').textContent = `${r.darkMinutes} min`;
  document.getElementById('scrubber').value = 0;
  drawBatteryChart(r.energySteps);
  setModeHint('Route planned. Use playback controls to simulate the traverse.');
  render();
}
function resetRouteStats() {
  ['statDistance','statPitstops','statEnergy','statDark'].forEach(id => document.getElementById(id).textContent = '—');
  drawBatteryChart([]);
}

document.getElementById('playBtn').addEventListener('click', () => {
  if (!activeRoute) return;
  playing = !playing;
  document.getElementById('playIcon').innerHTML = playing
    ? '<rect x="6" y="5" width="4" height="14" fill="currentColor"/><rect x="14" y="5" width="4" height="14" fill="currentColor"/>'
    : '<path d="M7 5v14l11-7L7 5Z" fill="currentColor"/>';
  if (playing) tickPlayback();
});
document.getElementById('resetBtn').addEventListener('click', () => {
  routeStart = null; routeEnd = null; activeRoute = null; routeProgress = 0;
  stopPlayback(); resetRouteStats();
  setModeHint('Click a start point, then an end point, on the map.');
  render();
});
document.getElementById('scrubber').addEventListener('input', (e) => {
  routeProgress = Number(e.target.value) / 100;
  render();
});
function stopPlayback() {
  playing = false;
  if (playRAF) cancelAnimationFrame(playRAF);
  document.getElementById('playIcon').innerHTML = '<path d="M7 5v14l11-7L7 5Z" fill="currentColor"/>';
}
function tickPlayback() {
  if (!playing) return;
  routeProgress += 0.0035;
  if (routeProgress >= 1) { routeProgress = 1; playing = false; stopPlayback(); }
  document.getElementById('scrubber').value = Math.round(routeProgress * 100);
  render();
  if (playing) playRAF = requestAnimationFrame(tickPlayback);
}

function drawBatteryChart(steps) {
  const c = document.getElementById('batteryChart');
  const cctx = c.getContext('2d');
  const dpr2 = Math.min(window.devicePixelRatio || 1, 2);
  const w = c.clientWidth || 240, h = 70;
  c.width = w * dpr2; c.height = h * dpr2;
  cctx.setTransform(dpr2, 0, 0, dpr2, 0, 0);
  cctx.clearRect(0, 0, w, h);
  if (!steps.length) { return; }
  cctx.strokeStyle = 'rgba(255,255,255,0.08)';
  for (let i = 1; i < 4; i++) { cctx.beginPath(); cctx.moveTo(0, (h / 4) * i); cctx.lineTo(w, (h / 4) * i); cctx.stroke(); }

  cctx.beginPath();
  steps.forEach((s, i) => {
    const x = (i / (steps.length - 1)) * w;
    const y = h - (s.battery / 100) * h;
    i === 0 ? cctx.moveTo(x, y) : cctx.lineTo(x, y);
  });
  cctx.strokeStyle = '#3fe7ec';
  cctx.lineWidth = 1.8;
  cctx.shadowColor = 'rgba(63,231,236,0.6)'; cctx.shadowBlur = 5;
  cctx.stroke();
  cctx.shadowBlur = 0;

  steps.forEach((s, i) => {
    if (!s.shadow) return;
    const x = (i / (steps.length - 1)) * w;
    cctx.fillStyle = 'rgba(63,231,236,0.06)';
    cctx.fillRect(x, 0, w / steps.length + 1, h);
  });
}

/* ---------------------------------------------------------------
   5) LMRS SCORING + PANEL
---------------------------------------------------------------- */
function findNearestIce(gx, gy) {
  let best = null, bestD = Infinity;
  for (let y = 0; y < region.rows; y++) {
    for (let x = 0; x < region.cols; x++) {
      const c = region.cells[y][x];
      if (!c.ice) continue;
      const d = Math.hypot(x - gx, y - gy);
      if (d < bestD) { bestD = d; best = c; }
    }
  }
  return best ? { cell: best, dist: bestD } : null;
}

function computeLMRS(gx, gy) {
  const cell = cellAt(gx, gy);
  if (!cell) return null;
  const nearestIce = cell.ice ? { cell, dist: 0 } : findNearestIce(gx, gy);

  const ice = nearestIce ? nearestIce.cell.ice : { volume_m3: 0, depth_m: 0, confidence: 0.3 };
  const dist = nearestIce ? nearestIce.dist : 99;

  const RAI = Math.max(4, Math.min(100, Math.round(100 - dist * 3.2 + ice.volume_m3 / 130 + ice.confidence * 18)));
  const CommVisibility = Math.max(5, Math.min(100, Math.round((cell.shadow ? 38 : 78) + (Math.random() * 10 - 5))));
  const pathToLanding = buildRoute(region.landing, { x: gx, y: gy });
  const reachable = !!pathToLanding;
  const energyWh = reachable ? pathToLanding.totalWh : null;
  const ThermalScore = reachable
    ? Math.max(4, Math.min(100, Math.round(100 - (cell.shadow ? 28 : 6) - energyWh / 9)))
    : 2;
  const LMRS = reachable ? Math.round((RAI + CommVisibility + ThermalScore) / 3) : Math.round(RAI * 0.15);

  return { LMRS, RAI, CommVisibility, ThermalScore, ice, dist, energyWh, reachable, shadow: cell.shadow, cell: { x: gx, y: gy } };
}

function openLmrsFor(gx, gy) {
  const result = computeLMRS(gx, gy);
  if (!result) return;
  lastLmrs = result; lastLmrsCell = { x: gx, y: gy };
  const panel = document.getElementById('lmrsPanel');
  panel.classList.add('open');

  document.getElementById('lmrsScoreNum').textContent = result.LMRS;
  const ring = document.getElementById('lmrsRingFg');
  const circumference = 327;
  ring.style.strokeDashoffset = circumference - (result.LMRS / 100) * circumference;

  const lat = -(89.9 - gy * 0.02).toFixed(3);
  const lon = (gx * 0.035 - 0.7).toFixed(3);
  document.getElementById('lmrsCoord').textContent = `${lat}°S, ${lon}°  ·  grid (${gx}, ${gy})`;

  document.getElementById('barRAI').style.width = result.RAI + '%';
  document.getElementById('valRAI').textContent = result.RAI;
  document.getElementById('barComm').style.width = result.CommVisibility + '%';
  document.getElementById('valComm').textContent = result.CommVisibility;
  document.getElementById('barThermal').style.width = result.ThermalScore + '%';
  document.getElementById('valThermal').textContent = result.ThermalScore;

  document.getElementById('detIceVol').textContent = result.dist === 0 ? `${result.ice.volume_m3.toLocaleString()} m³` : `${result.ice.volume_m3.toLocaleString()} m³ (${Math.round(result.dist * 140)} m away)`;
  document.getElementById('detIceDepth').textContent = `${result.ice.depth_m} m`;
  document.getElementById('detConfidence').textContent = `${Math.round(result.ice.confidence * 100)}%`;
  document.getElementById('detEnergy').textContent = result.reachable ? `${result.energyWh} Wh` : 'No safe path found';
  document.getElementById('detShadow').textContent = result.shadow ? 'Permanently shadowed (25 K)' : 'Sunlit';
}

document.getElementById('closeLmrs').addEventListener('click', () => {
  document.getElementById('lmrsPanel').classList.remove('open');
});
document.getElementById('addToCompareBtn').addEventListener('click', () => {
  if (!lastLmrsCell) return;
  if (compareSet.length >= 3) { alert('Comparison set is full (3 max).'); return; }
  compareSet.push(lastLmrsCell);
  renderCompareList();
  render();
});

/* ---------------------------------------------------------------
   6) COMPARE MODE
---------------------------------------------------------------- */
function renderCompareList() {
  const list = document.getElementById('compareList');
  list.innerHTML = '';
  compareSet.forEach((c, i) => {
    const r = computeLMRS(c.x, c.y);
    const div = document.createElement('div');
    div.className = 'compare-item';
    div.innerHTML = `<span>Site ${i + 1} (${c.x},${c.y})</span><b>${r.LMRS}</b>`;
    list.appendChild(div);
  });
  if (compareSet.length >= 2) renderCompareDrawer();
}
document.getElementById('clearCompare').addEventListener('click', () => {
  compareSet = []; renderCompareList();
  document.getElementById('compareDrawer').hidden = true;
  render();
});
document.getElementById('closeCompareDrawer').addEventListener('click', () => {
  document.getElementById('compareDrawer').hidden = true;
});
function renderCompareDrawer() {
  const drawer = document.getElementById('compareDrawer');
  drawer.hidden = false;
  const results = compareSet.map((c, i) => ({ i, c, r: computeLMRS(c.x, c.y) }));
  const bestIdx = results.reduce((best, cur) => cur.r.LMRS > results[best].r.LMRS ? cur.i : best, 0);
  const table = document.getElementById('compareTable');
  table.innerHTML = `
    <thead><tr>
      <th>Site</th><th>LMRS</th><th>RAI</th><th>Comm. Visibility</th><th>Thermal Risk</th>
      <th>Ice Volume</th><th>Confidence</th><th>Energy (Wh)</th><th></th>
    </tr></thead>
    <tbody>
      ${results.map(({ i, r }) => `
        <tr class="${i === bestIdx ? 'best' : ''}">
          <td>Site ${i + 1}</td>
          <td>${r.LMRS}</td>
          <td>${r.RAI}</td>
          <td>${r.CommVisibility}</td>
          <td>${r.ThermalScore}</td>
          <td>${r.ice.volume_m3.toLocaleString()} m³</td>
          <td>${Math.round(r.ice.confidence * 100)}%</td>
          <td>${r.reachable ? r.energyWh : '<span class="tag-unreachable">Unreachable</span>'}</td>
          <td>${i === bestIdx ? '<span class="tag-best">★ Recommended</span>' : ''}</td>
        </tr>`).join('')}
    </tbody>`;
}

/* ---------------------------------------------------------------
   7) SWARM MODE
---------------------------------------------------------------- */
function pickIceTarget(rng) {
  const candidates = [];
  for (let y = 0; y < region.rows; y++) for (let x = 0; x < region.cols; x++) if (region.cells[y][x].ice) candidates.push({ x, y });
  if (!candidates.length) return { x: region.cols - 3, y: region.rows - 3 };
  return candidates[Math.floor(rng() * candidates.length)];
}
function findSafeStart(candidates) {
  for (const c of candidates) {
    const cell = cellAt(c.x, c.y);
    if (cell && !cell.hazard) return c;
  }
  return region.landing; // guaranteed safe fallback
}


function ensureSwarmRoutes(forceNew) {
  if (swarm.a.route && swarm.b.route && !forceNew) return;
  const rng = mulberry32(Date.now() % 100000);
  const startB = findSafeStart([
    { x: region.cols - 3, y: 2 }, { x: region.cols - 3, y: region.rows - 3 },
    { x: 2, y: region.rows - 3 }, { x: Math.floor(region.cols / 2), y: 1 },
  ]);

  // retry target selection a bounded number of times so the swarm demo
  // never silently shows a stalled rover with no route
  let routeA = null, routeB = null, targetA = null, targetB = null, tries = 0;
  while ((!routeA || !routeB) && tries++ < 25) {
    targetA = pickIceTarget(rng);
    targetB = pickIceTarget(rng);
    if (targetA.x === targetB.x && targetA.y === targetB.y) continue;
    routeA = buildRoute(region.landing, targetA);
    routeB = buildRoute(startB, targetB);
  }

  swarm.a.route = routeA;
  swarm.b.route = routeB;
  swarm.a.progress = 0; swarm.b.progress = 0;
  document.getElementById('swarmABattery').textContent = '100%';
  document.getElementById('swarmBBattery').textContent = '100%';
  render();
}
document.getElementById('rerollSwarm').addEventListener('click', () => ensureSwarmRoutes(true));
document.getElementById('swarmPlayBtn').addEventListener('click', (e) => {
  swarm.playing = !swarm.playing;
  e.target.textContent = swarm.playing ? '⏸ Pause Swarm' : '▶ Play Swarm';
  if (swarm.playing) tickSwarm();
});
function tickSwarm() {
  if (!swarm.playing) return;
  let allDone = true;
  ['a', 'b'].forEach(k => {
    const s = swarm[k];
    if (!s.route) return;
    if (s.progress < 1) { s.progress = Math.min(1, s.progress + 0.003); allDone = false; }
    const idx = Math.min(Math.floor(s.progress * (s.route.energySteps.length - 1)), s.route.energySteps.length - 1);
    const batt = Math.round(s.route.energySteps[idx]?.battery ?? 100);
    document.getElementById(k === 'a' ? 'swarmABattery' : 'swarmBBattery').textContent = batt + '%';
  });
  render();
  if (!allDone) requestAnimationFrame(tickSwarm);
  else { swarm.playing = false; document.getElementById('swarmPlayBtn').textContent = '▶ Play Swarm'; }
}

/* ---------------------------------------------------------------
   8) MISSION COPILOT (template-grounded, fully offline)
---------------------------------------------------------------- */
const copilotFab = document.getElementById('copilotFab');
const copilotPanel = document.getElementById('copilotPanel');
copilotFab.addEventListener('click', () => { copilotPanel.hidden = !copilotPanel.hidden; });
document.getElementById('closeCopilot').addEventListener('click', () => { copilotPanel.hidden = true; });

function pushMessage(text, who) {
  const wrap = document.getElementById('copilotMessages');
  const div = document.createElement('div');
  div.className = `msg msg-${who}`;
  div.textContent = text;
  wrap.appendChild(div);
  wrap.scrollTop = wrap.scrollHeight;
}

function answerCopilot(qRaw) {
  const q = qRaw.toLowerCase();
  if (q.includes('hazard') || q.includes('danger') || q.includes('slope') || q.includes('corridor')) {
    const hazardCount = region.cells.flat().filter(c => c.hazard).length;
    if (lastLmrs) {
      return `Based on the hazard mask, this region has ${hazardCount} flagged cells (slope > 15° or boulders > 0.5 m). Your last selected point at (${lastLmrs.cell.x}, ${lastLmrs.cell.y}) is ${lastLmrs.shadow ? 'inside a shadowed crater' : 'on sunlit terrain'} — the pathfinder routes around any adjacent no-go cells automatically.`;
    }
    return `This region has ${hazardCount} hazard-flagged cells from the slope (>15°) and boulder (>0.5 m) constraints. Click a point or plan a route and I can be more specific.`;
  }
  if (q.includes('lmrs') || q.includes('score') || q.includes('readiness')) {
    if (lastLmrs) {
      return `The last selected site scored ${lastLmrs.LMRS}/100 on the LMRS. Resource Accessibility is ${lastLmrs.RAI} (driven by ${lastLmrs.ice.volume_m3.toLocaleString()} m³ of estimated ice ${lastLmrs.dist === 0 ? 'directly at this point' : `~${Math.round(lastLmrs.dist * 140)} m away`}), Communication Visibility is ${lastLmrs.CommVisibility}, and Thermal Risk scores ${lastLmrs.ThermalScore}${lastLmrs.reachable ? ` given an estimated ${lastLmrs.energyWh} Wh to reach it` : ' — the pathfinder could not find any safe corridor to this point at all, which is why its score is low'}.`;
    }
    return `Click any point on the map and I'll break down its LMRS score — it combines Resource Accessibility, Communication Visibility, and Thermal Risk into one 0–100 number.`;
  }
  if (q.includes('battery') || q.includes('energy') || q.includes('power') || q.includes('wh')) {
    if (activeRoute) {
      return `The current route is estimated to cost ${activeRoute.totalWh} Wh over ${(activeRoute.distanceM / 1000).toFixed(2)} km, with ${activeRoute.darkCells} cells inside permanent shadow (~${activeRoute.darkMinutes} minutes of dark dwell). The predictive battery model inserted ${activeRoute.pitstopIndices.length} solar charging pitstop(s) to keep it survivable.`;
    }
    return `Plan a route in "Plan Route" mode and I'll give you the predicted Wh cost and dark-dwell time for it.`;
  }
  if (q.includes('confidence') || q.includes('certain') || q.includes('noisy') || q.includes('noise')) {
    const iceCells = region.cells.flat().filter(c => c.ice);
    const avg = iceCells.length ? Math.round(100 * iceCells.reduce((a, c) => a + c.ice.confidence, 0) / iceCells.length) : 0;
    return `Detection confidence across the ${iceCells.length} ice-candidate cells in this region averages ${avg}%. Real CPR/DOP radar signals are noisy, so the Confidence Overlay layer (left panel) shows this as a translucent heat layer — brighter purple means a more reliable detection, not necessarily more ice.`;
  }
  if (q.includes('route') || q.includes('path') || q.includes('pitstop')) {
    if (activeRoute) {
      return `This route runs ${(activeRoute.distanceM / 1000).toFixed(2)} km with ${activeRoute.pitstopIndices.length} solar charging pitstop(s) automatically inserted whenever dark-dwell would exceed the safety budget. Total predicted cost: ${activeRoute.totalWh} Wh.`;
    }
    return `No route is planned yet — switch to "Plan Route" mode and click a start, then an end point.`;
  }
  if (q.includes('ice') || q.includes('volume') || q.includes('depth')) {
    if (lastLmrs) {
      return `Nearest ice candidate to your last selection holds an estimated ${lastLmrs.ice.volume_m3.toLocaleString()} m³ at ${lastLmrs.ice.depth_m} m depth, with ${Math.round(lastLmrs.ice.confidence * 100)}% detection confidence — derived from CPR > 1.0 and DOP < 0.13 thresholds on the (simulated) DFSAR signal.`;
    }
    return `Click any point on the ice layer and I'll tell you its estimated volume, depth, and detection confidence.`;
  }
  return `I can answer questions about hazards, the LMRS score, route energy cost, ice volume/depth, or detection confidence for whatever you've selected on the map — try one of the suggested questions below.`;
}

document.getElementById('copilotForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const input = document.getElementById('copilotInput');
  const val = input.value.trim();
  if (!val) return;
  pushMessage(val, 'user');
  input.value = '';
  setTimeout(() => pushMessage(answerCopilot(val), 'bot'), 380);
});
document.querySelectorAll('.chip-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    pushMessage(btn.textContent, 'user');
    setTimeout(() => pushMessage(answerCopilot(btn.textContent), 'bot'), 380);
  });
});

/* ---------------------------------------------------------------
   9) TOP BAR CLOCK + BOOT
---------------------------------------------------------------- */
let missionSeconds = 0;
setInterval(() => {
  missionSeconds++;
  const h = String(Math.floor(missionSeconds / 3600)).padStart(2, '0');
  const m = String(Math.floor((missionSeconds % 3600) / 60)).padStart(2, '0');
  const s = String(missionSeconds % 60).padStart(2, '0');
  const el = document.getElementById('clockReadout');
  if (el) el.textContent = `T+${h}:${m}:${s}`;
}, 1000);

function loadRegion(key) {
  currentRegionKey = key;
  region = generateRegion(key);
  buildTerrainNoise();
  routeStart = null; routeEnd = null; activeRoute = null; routeProgress = 0;
  compareSet = []; swarm.a.route = null; swarm.b.route = null;
  document.getElementById('lmrsPanel').classList.remove('open');
  document.getElementById('compareDrawer').hidden = true;
  resetRouteStats();
  stopPlayback();
  if (mode === 'swarm') ensureSwarmRoutes();
  resizeCanvas();
}

loadRegion('shackleton');
