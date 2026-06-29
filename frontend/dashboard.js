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

const HOST_NAME = window.location.hostname || '127.0.0.1';
const BACKEND_BASE_URL = window.localStorage.getItem('ASTRANAV_API_URL') || `http://${HOST_NAME}:8000`;
const BACKEND_WS_URL = window.localStorage.getItem('ASTRANAV_WS_URL') || `ws://${HOST_NAME}:8000`;
window.BACKEND_BASE_URL = BACKEND_BASE_URL;
window.BACKEND_WS_URL = BACKEND_WS_URL;

// --- ONLINE/OFFLINE PWA STATUS ---
function updateNetworkStatus() {
  const statusEl = document.getElementById('networkStatus');
  if (!statusEl) return;
  if (navigator.onLine) {
    statusEl.textContent = 'ONLINE';
    statusEl.style.color = 'var(--cyan)';
  } else {
    statusEl.textContent = 'OFFLINE (CACHED)';
    statusEl.style.color = 'var(--slate-dim)';
  }
}
window.addEventListener('online', updateNetworkStatus);
window.addEventListener('offline', updateNetworkStatus);
updateNetworkStatus();


let GRID_COLS = 46, GRID_ROWS = 28;
const DARK_PENALTY = 4.2;
const BASE_WH_PER_CELL = 2.1;
const SHADOW_WH_EXTRA = 5.4;
const DARK_BUDGET_CELLS = 5;

const REGION_SEEDS = {
  shackleton: 1337,
  faustini: 5021,
  degerlache: 8842,
  'shackleton-east': 1337,
  haworth: 5021
};
const REGION_LABELS = {
  shackleton: 'Shackleton Rim · 89.9°S',
  faustini: 'Faustini Crater · 87.3°S',
  degerlache: 'de Gerlache Rim · 88.5°S',
  'shackleton-east': 'Shackleton Crater — Eastern Rim',
  haworth: 'Haworth Crater'
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
        if (cell.craterId === c.id && cell.shadow) {
          // Simulate radar signals:
          const cpr = 0.5 + rng() * 1.0; // Circular Polarization Ratio
          const dop = rng() * 0.25; // Degree of Polarization
          const dielectricConstant = 2.0 + rng() * 2.0; // Lunar dust (2.5) to Ice mix (3.5+)
          
          if (cpr > 1.0 && dop < 0.13 && dielectricConstant > 2.5) {
            const distFactor = 1 - Math.hypot(x - c.cx, y - c.cy) / (c.r * 0.78);
            // Volume directly based on dielectric shift from 2.5
            const baseVol = 800 + rng() * 5200;
            const shiftMultiplier = Math.min(2.0, Math.max(0.1, (dielectricConstant - 2.5) * 2));
            cell.ice = {
              volume_m3: Math.round(baseVol * shiftMultiplier * (0.5 + distFactor * 0.6)),
              depth_m: +(1 + rng() * 4).toFixed(1),
              confidence: Math.min(0.97, Math.max(0.42, 0.5 + distFactor * 0.4 + (rng() - 0.5) * 0.15)),
              cpr: cpr.toFixed(2),
              dop: dop.toFixed(2),
              dielectric: dielectricConstant.toFixed(2)
            };
          }
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


// --- SOLAR TIME-LAPSE ---
let sunAngle = 0;
let timeLapseActive = false;
let sunPlaying = false;
let sunSpeed = 1;
let sunRAF = null;

function tickSun() {
  if (!sunPlaying) return;
  sunAngle = (sunAngle + 0.2 * sunSpeed) % 360;
  const scrubber = document.getElementById('sunScrubber');
  if (scrubber) scrubber.value = sunAngle;
  timeLapseActive = true;
  updateIllumination();
  render();
  sunRAF = requestAnimationFrame(tickSun);
}

function updateIllumination() {
  const sunRad = sunAngle * Math.PI / 180;
  const sx = Math.cos(sunRad);
  const sy = Math.sin(sunRad);

  for (let y = 0; y < region.rows; y++) {
    for (let x = 0; x < region.cols; x++) {
      const cell = region.cells[y][x];
      cell.liveShadow = false;
      if (cell.craterId !== -1) {
        const c = region.craters.find(cr => cr.id === cell.craterId);
        if (c) {
          const dx = x - c.cx;
          const dy = y - c.cy;
          const dist = Math.hypot(dx, dy);
          if (dist < c.r) {
            const dot = (dx * sx + dy * sy) / (dist || 1);
            if (dot > -0.2) cell.liveShadow = true;
          }
        }
      }
    }
  }
  
  const pitstopStatus = document.getElementById('pitstopStatus');
  if (pitstopStatus) {
    if (activeRoute && activeRoute.pitstopIndices.length > 0) {
      let anyOpen = false;
      activeRoute.pitstopIndices.forEach(idx => {
        const p = activeRoute.path[Math.min(idx, activeRoute.path.length - 1)];
        const c = cellAt(p.x, p.y);
        if (c && !c.liveShadow) anyOpen = true;
      });
      pitstopStatus.textContent = anyOpen ? 'OPEN' : 'CLOSED';
      pitstopStatus.style.color = anyOpen ? '#ffe28a' : '#ff6b5e';
    } else {
      pitstopStatus.textContent = 'N/A';
      pitstopStatus.style.color = 'var(--slate)';
    }
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const sunScrubber = document.getElementById('sunScrubber');
  const sunPlayBtn = document.getElementById('sunPlayBtn');
  const sunSpeedBtn = document.getElementById('sunSpeedBtn');
  const sunPlayIcon = document.getElementById('sunPlayIcon');
  
  if (sunScrubber) {
    sunScrubber.addEventListener('input', (e) => {
      sunAngle = Number(e.target.value);
      timeLapseActive = true;
      updateIllumination();
      render();
    });
  }
  
  if (sunPlayBtn) {
    sunPlayBtn.addEventListener('click', () => {
      sunPlaying = !sunPlaying;
      if (sunPlayIcon) sunPlayIcon.innerHTML = sunPlaying ? '<rect x="6" y="5" width="4" height="14" fill="currentColor"/><rect x="14" y="5" width="4" height="14" fill="currentColor"/>' : '<path d="M7 5v14l11-7L7 5Z" fill="currentColor"/>';
      if (sunPlaying) tickSun();
      else cancelAnimationFrame(sunRAF);
    });
  }
  
  if (sunSpeedBtn) {
    sunSpeedBtn.addEventListener('click', () => {
      if (sunSpeed === 1) sunSpeed = 4;
      else if (sunSpeed === 4) sunSpeed = 10;
      else sunSpeed = 1;
      if (sunSpeedBtn) sunSpeedBtn.textContent = sunSpeed + 'x';
    });
  }
});

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
      if (timeLapseActive && cell.liveShadow) base *= 0.3;
      else if (!timeLapseActive && cell.shadow) base *= 0.3;
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
  
  const cell = cellAt(g.x, g.y);
  if (cell && cell.lat !== undefined && cell.lon !== undefined) {
    const latAbs = Math.abs(cell.lat).toFixed(4);
    const lonAbs = Math.abs(cell.lon).toFixed(4);
    const latDir = cell.lat < 0 ? 'S' : 'N';
    const lonDir = cell.lon < 0 ? 'W' : 'E';
    el.textContent = `LAT ${latAbs}°${latDir} · LON ${lonAbs}°${lonDir}`;
  } else {
    const lat = -(89.9 - g.y * 0.02).toFixed(3);
    const lon = (g.x * 0.035 - 0.7).toFixed(3);
    el.textContent = `LAT ${lat}°S · LON ${lon}°`;
  }
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
      const mo = document.getElementById('modeOptimize');
      if(mo) mo.hidden = mode !== 'optimize';
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

document.getElementById('routeIceSeeking').addEventListener('change', () => {
  if (routeStart && routeEnd) computeAndShowRoute();
});
document.getElementById('routePredictiveBattery').addEventListener('change', () => {
  if (routeStart && routeEnd) computeAndShowRoute();
  if (mode === 'swarm') ensureSwarmRoutes(true);
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
function latLonToGrid(lat, lon) {
  let bestX = 0, bestY = 0, bestDist = Infinity;
  for (let y = 0; y < region.rows; y++) {
    for (let x = 0; x < region.cols; x++) {
      const cell = region.cells[y][x];
      const d = Math.hypot(cell.lat - lat, cell.lon - lon);
      if (d < bestDist) {
        bestDist = d;
        bestX = x;
        bestY = y;
      }
    }
  }
  return { x: bestX, y: bestY };
}

function mapBackendRouteToFrontend(backendRoute) {
  const path = [];
  const pitstopIndices = [];
  const energySteps = [];
  let darkCells = 0;

  backendRoute.waypoints.forEach((wp, idx) => {
    const gridPt = latLonToGrid(wp.lat, wp.lon);
    if (path.length === 0 || path[path.length - 1].x !== gridPt.x || path[path.length - 1].y !== gridPt.y || wp.type === 'solar_pitstop') {
      path.push({ x: gridPt.x, y: gridPt.y, pitstop: wp.type === 'solar_pitstop' });
    }
    
    if (wp.type === 'solar_pitstop') {
      pitstopIndices.push(path.length - 1);
    }
    
    energySteps.push({
      wh: wp.cumulative_energy_wh,
      battery: wp.battery_pct_remaining,
      shadow: wp.is_shadowed
    });

    if (wp.is_shadowed) {
      darkCells++;
    }
  });

  return {
    path,
    pitstopIndices,
    energySteps,
    totalWh: Math.round(backendRoute.total_energy_wh),
    distanceM: backendRoute.total_distance_m,
    darkCells,
    darkMinutes: Math.round(darkCells * 1.8),
  };
}

async function computeAndShowRoute() {
  const startCell = cellAt(routeStart.x, routeStart.y);
  const endCell = cellAt(routeEnd.x, routeEnd.y);
  
  let success = false;
  
  if (startCell && endCell && startCell.lat !== undefined && startCell.lon !== undefined) {
    try {
      const isSeeking = document.getElementById('routeIceSeeking')?.checked || false;
      const isPredictive = document.getElementById('routePredictiveBattery')?.checked || false;
      
      const url = `${BACKEND_BASE_URL}/api/route?` + new URLSearchParams({
        start_lat: startCell.lat,
        start_lon: startCell.lon,
        end_lat: endCell.lat,
        end_lon: endCell.lon,
        region_id: currentRegionKey,
        ice_seeking: isSeeking,
        use_predictive_battery: isPredictive,
        initial_battery_pct: 100.0,
        dark_budget_wh: 80.0,
        shadow_penalty_weight: 5.0
      });
      
      console.log("Planning route via backend API:", url);
      const res = await fetch(url);
      if (!res.ok) {
        const errJson = await res.json().catch(() => ({}));
        throw new Error(errJson.detail?.reason || `HTTP status ${res.status}`);
      }
      
      const data = await res.json();
      const mappedRoute = mapBackendRouteToFrontend(data);
      activeRoute = mappedRoute;
      routeProgress = 0;
      success = true;
      
      updateRouteStatsUI(mappedRoute);
      setModeHint('Route planned via backend. Use playback controls to simulate the traverse.');
      render();
    } catch (err) {
      console.warn("Backend routing failed, falling back to local simulation:", err);
      flashHint(`Backend Route Error: ${err.message}. Falling back to simulation.`);
    }
  }
  
  if (!success) {
    const r = buildRoute(routeStart, routeEnd);
    if (!r) { flashHint('No safe path found — hazard zones block this route.'); routeEnd = null; return; }
    activeRoute = r; routeProgress = 0;
    updateRouteStatsUI(r);
    setModeHint('Route planned via local simulation. Use playback controls.');
    render();
  }
}

function updateRouteStatsUI(r) {
  document.getElementById('statDistance').textContent = `${(r.distanceM / 1000).toFixed(2)} km`;
  document.getElementById('statPitstops').textContent = r.pitstopIndices.length;
  document.getElementById('statEnergy').textContent = `${r.totalWh} Wh`;
  document.getElementById('statDark').textContent = `${r.darkMinutes} min`;
  document.getElementById('scrubber').value = 0;
  drawBatteryChart(r.energySteps);
}

function resetRouteStats() {
  ['statDistance','statPitstops','statEnergy','statDark'].forEach(id => document.getElementById(id).textContent = '—');
  drawBatteryChart([]);
}

let telemetryWS = null;

function startWSPlayback() {
  if (telemetryWS) {
    telemetryWS.close();
  }
  
  const startCell = cellAt(routeStart.x, routeStart.y);
  const endCell = cellAt(routeEnd.x, routeEnd.y);
  if (!startCell || !endCell) return;

  const isSeeking = document.getElementById('routeIceSeeking')?.checked || false;
  const isPredictive = document.getElementById('routePredictiveBattery')?.checked || false;
  
  const wsUrl = `${BACKEND_WS_URL}/ws/telemetry/${currentRegionKey}?` + new URLSearchParams({
    rover_id: 'rover-1',
    start_lat: startCell.lat,
    start_lon: startCell.lon,
    end_lat: endCell.lat,
    end_lon: endCell.lon,
    use_predictive_battery: isPredictive,
    ice_seeking: isSeeking,
    tick_interval_s: 0.2,
    dark_budget_wh: 80.0,
    shadow_penalty_weight: 5.0,
    initial_battery_pct: 100.0
  });

  console.log("Connecting to WebSocket Telemetry:", wsUrl);
  telemetryWS = new WebSocket(wsUrl);

  playing = true;
  document.getElementById('playIcon').innerHTML = '<rect x="6" y="5" width="4" height="14" fill="currentColor"/><rect x="14" y="5" width="4" height="14" fill="currentColor"/>';

  telemetryWS.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      console.log("Telemetry Frame received:", data);
      
      const lat = data.rover.lat;
      const lon = data.rover.lon;
      const battery = data.rover.battery;
      const status = data.rover.status;
      
      const gridPt = latLonToGrid(lat, lon);
      
      if (data.total_waypoints) {
        routeProgress = data.waypoint_index / (data.total_waypoints - 1);
        document.getElementById('scrubber').value = Math.round(routeProgress * 100);
      }
      
      setModeHint(`Rover: ${status.toUpperCase()} | Charge: ${Math.round(battery)}%`);
      
      if (data.cumulative_distance_m !== undefined) {
        document.getElementById('statDistance').textContent = `${(data.cumulative_distance_m / 1000).toFixed(2)} km`;
      }
      if (data.cumulative_energy_wh !== undefined) {
        document.getElementById('statEnergy').textContent = `${Math.round(data.cumulative_energy_wh)} Wh`;
      }
      
      render();

      if (status === 'arrived') {
        stopPlayback();
        setModeHint('Rover arrived safely at destination.');
      } else if (status === 'stalled') {
        stopPlayback();
        setModeHint('Rover stalled: path blocked or battery depleted.');
      }
    } catch (e) {
      console.error("Error processing WS telemetry message:", e);
    }
  };

  telemetryWS.onerror = (err) => {
    console.error("WebSocket telemetry error:", err);
    fallbackToLocalPlayback();
  };

  telemetryWS.onclose = () => {
    console.log("Telemetry WebSocket closed.");
    playing = false;
    document.getElementById('playIcon').innerHTML = '<path d="M7 5v14l11-7L7 5Z" fill="currentColor"/>';
  };
}

function fallbackToLocalPlayback() {
  console.log("Falling back to local playback simulation.");
  if (telemetryWS) {
    telemetryWS.close();
    telemetryWS = null;
  }
  playing = true;
  document.getElementById('playIcon').innerHTML = '<rect x="6" y="5" width="4" height="14" fill="currentColor"/><rect x="14" y="5" width="4" height="14" fill="currentColor"/>';
  tickPlayback();
}

document.getElementById('playBtn').addEventListener('click', () => {
  if (!activeRoute) return;
  if (playing) {
    stopPlayback();
  } else {
    const firstCell = region.cells[0][0];
    if (firstCell && firstCell.lat !== undefined && firstCell.lon !== undefined) {
      startWSPlayback();
    } else {
      playing = true;
      document.getElementById('playIcon').innerHTML = '<rect x="6" y="5" width="4" height="14" fill="currentColor"/><rect x="14" y="5" width="4" height="14" fill="currentColor"/>';
      tickPlayback();
    }
  }
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
  if (telemetryWS) {
    telemetryWS.close();
    telemetryWS = null;
  }
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

async function openLmrsFor(gx, gy) {
  lastLmrsCell = { x: gx, y: gy };
  
  // Instant local calculation as preview & fallback
  const localResult = computeLMRS(gx, gy);
  if (localResult) {
    lastLmrs = localResult;
    updateLmrsUI(localResult);
  }
  
  const panel = document.getElementById('lmrsPanel');
  panel.classList.add('open');
  
  const cell = cellAt(gx, gy);
  if (cell && cell.lat !== undefined && cell.lon !== undefined) {
    try {
      const isPredictive = document.getElementById('routePredictiveBattery')?.checked || false;
      const url = `${BACKEND_BASE_URL}/api/lmrs?lat=${cell.lat}&lon=${cell.lon}&region_id=${currentRegionKey}&use_predictive_battery=${isPredictive}`;
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP status ${res.status}`);
      const data = await res.json();
      
      const backendResult = {
        LMRS: Math.round(data.lmrs_score),
        RAI: Math.round(data.rai.score),
        CommVisibility: Math.round(data.comm_visibility.score),
        ThermalScore: Math.round(data.thermal_risk.score),
        ice: {
          volume_m3: data.rai.ice_volume_m3,
          depth_m: data.rai.ice_depth_m,
          confidence: data.rai.confidence
        },
        dist: data.rai.nearest_ice_distance_m / 140,
        energyWh: data.thermal_risk.energy_cost_wh,
        reachable: data.thermal_risk.score > 2,
        shadow: data.thermal_risk.dark_dwell_fraction > 0,
        cell: { x: gx, y: gy }
      };
      
      lastLmrs = backendResult;
      updateLmrsUI(backendResult);
    } catch (err) {
      console.warn("Failed to fetch LMRS from backend, using local simulation values instead:", err);
    }
  }
}

function updateLmrsUI(result) {
  document.getElementById('lmrsScoreNum').textContent = result.LMRS;
  const ring = document.getElementById('lmrsRingFg');
  const circumference = 327;
  ring.style.strokeDashoffset = circumference - (result.LMRS / 100) * circumference;

  const cell = cellAt(result.cell.x, result.cell.y);
  if (cell && cell.lat !== undefined && cell.lon !== undefined) {
    document.getElementById('lmrsCoord').textContent = `${cell.lat.toFixed(4)}°S, ${cell.lon.toFixed(4)}°  ·  grid (${result.cell.x}, ${result.cell.y})`;
  } else {
    const lat = -(89.9 - result.cell.y * 0.02).toFixed(3);
    const lon = (result.cell.x * 0.035 - 0.7).toFixed(3);
    document.getElementById('lmrsCoord').textContent = `${lat}°S, ${lon}°  ·  grid (${result.cell.x}, ${result.cell.y})`;
  }

  document.getElementById('barRAI').style.width = result.RAI + '%';
  document.getElementById('valRAI').textContent = result.RAI;
  document.getElementById('barComm').style.width = result.CommVisibility + '%';
  document.getElementById('valComm').textContent = result.CommVisibility;
  document.getElementById('barThermal').style.width = result.ThermalScore + '%';
  document.getElementById('valThermal').textContent = result.ThermalScore;

  if (result.dist === 0) {
    document.getElementById('detIceVol').textContent = `${result.ice.volume_m3.toLocaleString()} m³`;
  } else {
    const distLabel = result.dist >= 99 ? 'far' : `${Math.round(result.dist * 140)} m away`;
    document.getElementById('detIceVol').textContent = `${result.ice.volume_m3.toLocaleString()} m³ (${distLabel})`;
  }
  document.getElementById('detIceDepth').textContent = `${result.ice.depth_m} m`;
  document.getElementById('detConfidence').textContent = `${Math.round(result.ice.confidence * 100)}%`;
  document.getElementById('detEnergy').textContent = result.reachable ? `${Math.round(result.energyWh)} Wh` : 'No safe path found';
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


// --- EXPORT REPORT & AI BRIEFING ---
document.addEventListener('DOMContentLoaded', () => {
  const exportReportBtn = document.getElementById('exportReportBtn');
  const exportMenu = document.getElementById('exportMenu');
  const exportPdfBtn = document.getElementById('exportPdfBtn');
  const exportCsvBtn = document.getElementById('exportCsvBtn');
  const briefingBtn = document.getElementById('briefingBtn');
  const briefingPanel = document.getElementById('briefingPanel');
  const briefingContent = document.getElementById('briefingContent');
  const copyBriefingBtn = document.getElementById('copyBriefingBtn');

  if (exportReportBtn) {
    exportReportBtn.addEventListener('click', () => {
      exportMenu.hidden = !exportMenu.hidden;
    });
  }

  document.addEventListener('click', (e) => {
    if (exportReportBtn && !exportReportBtn.contains(e.target) && exportMenu && !exportMenu.contains(e.target)) {
      exportMenu.hidden = true;
    }
  });

  if (exportCsvBtn) {
    exportCsvBtn.addEventListener('click', () => {
      exportMenu.hidden = true;
      if (!lastLmrs) return;
      
      let csv = "Site Coordinates,LMRS Score,RAI,Comm Visibility,Thermal Score,Ice Volume (m3),Ice Depth (m),Confidence,Energy to Reach (Wh),Shadow Status\n";
      csv += `"${document.getElementById('lmrsCoord').textContent}",${lastLmrs.LMRS},${lastLmrs.RAI},${lastLmrs.CommVisibility},${lastLmrs.ThermalScore},${lastLmrs.ice.volume_m3},${lastLmrs.ice.depth_m},${lastLmrs.ice.confidence},${lastLmrs.reachable ? lastLmrs.energyWh : 'N/A'},${lastLmrs.shadow}\n`;
      
      if (activeRoute) {
        csv += "\nWaypoint,X,Y,Pitstop\n";
        activeRoute.path.forEach((p, i) => {
          csv += `${i},${p.x},${p.y},${p.pitstop ? 'Yes' : 'No'}\n`;
        });
      }
      
      const blob = new Blob([csv], { type: 'text/csv' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `AstraNav-LRIS_Site_${lastLmrs.cell.x}_${lastLmrs.cell.y}_${new Date().toISOString().split('T')[0]}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    });
  }

  if (exportPdfBtn) {
    exportPdfBtn.addEventListener('click', () => {
      exportMenu.hidden = true;
      if (!lastLmrs || !window.jspdf) return;
      
      const { jsPDF } = window.jspdf;
      const doc = new jsPDF();
      
      doc.setFont("helvetica", "bold");
      doc.setFontSize(22);
      doc.text("AstraNav-LRIS - Mission Site Report", 20, 25);
      
      doc.setFontSize(10);
      doc.setFont("helvetica", "normal");
      doc.text(`Generated: ${new Date().toLocaleString()}`, 20, 32);
      doc.text(`Region: ${REGION_LABELS[currentRegionKey] || currentRegionKey}`, 20, 38);
      
      doc.line(20, 42, 190, 42);
      
      doc.setFontSize(14);
      doc.setFont("helvetica", "bold");
      doc.text("Site Details", 20, 52);
      doc.setFontSize(11);
      doc.setFont("helvetica", "normal");
      doc.text(`Coordinates: ${document.getElementById('lmrsCoord').textContent}`, 20, 60);
      doc.text(`LMRS Score: ${lastLmrs.LMRS}/100`, 20, 67);
      doc.text(`Resource Accessibility: ${lastLmrs.RAI}`, 20, 74);
      doc.text(`Communication Visibility: ${lastLmrs.CommVisibility}`, 20, 81);
      doc.text(`Thermal Risk Score: ${lastLmrs.ThermalScore}`, 20, 88);
      
      doc.setFontSize(14);
      doc.setFont("helvetica", "bold");
      doc.text("Resource Estimation", 20, 103);
      doc.setFontSize(11);
      doc.setFont("helvetica", "normal");
      doc.text(`Ice Volume: ${lastLmrs.ice.volume_m3.toLocaleString()} m3`, 20, 111);
      doc.text(`Ice Depth: ${lastLmrs.ice.depth_m} m`, 20, 118);
      doc.text(`Detection Confidence: ${Math.round(lastLmrs.ice.confidence * 100)}%`, 20, 125);
      doc.text(`Hazard Status: ${lastLmrs.shadow ? 'Permanently Shadowed (25 K)' : 'Sunlit'}`, 20, 132);
      
      if (activeRoute) {
        doc.setFontSize(14);
        doc.setFont("helvetica", "bold");
        doc.text("Route Plan", 20, 147);
        doc.setFontSize(11);
        doc.setFont("helvetica", "normal");
        doc.text(`Distance: ${(activeRoute.distanceM / 1000).toFixed(2)} km`, 20, 155);
        doc.text(`Total Energy Cost: ${activeRoute.totalWh} Wh`, 20, 162);
        doc.text(`Dark Dwell: ${activeRoute.darkMinutes} mins`, 20, 169);
        doc.text(`Solar Pitstops Required: ${activeRoute.pitstopIndices.length}`, 20, 176);
      }
      
      doc.save(`AstraNav-LRIS_Site_${lastLmrs.cell.x}_${lastLmrs.cell.y}_${new Date().toISOString().split('T')[0]}.pdf`);
    });
  }

  function mockMissionBriefing(data) {
    return `The selected site at ${document.getElementById('lmrsCoord').textContent} presents an LMRS score of ${data.LMRS}/100. It offers an estimated ${data.ice.volume_m3.toLocaleString()} m³ of water-ice at ${data.ice.depth_m}m depth. ${data.reachable ? 'A safe traverse route has been successfully plotted, costing approximately ' + activeRoute.totalWh + ' Wh.' : 'Currently, no safe route can be charted without exceeding thermal or hazard safety margins.'} Proceed with caution based on these parameters.`;
  }

  if (briefingBtn) {
    briefingBtn.addEventListener('click', async () => {
      briefingPanel.hidden = !briefingPanel.hidden;
      if (briefingPanel.hidden) return;
      
      if (!lastLmrs) {
        briefingContent.textContent = "No site selected. Please click on the map.";
        return;
      }
      
      briefingContent.textContent = "Generating briefing...";
      
      try {
        const payload = { lmrs: lastLmrs, route: activeRoute };
        const res = await fetch(`${BACKEND_BASE_URL}/api/mission-briefing`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (!res.ok) throw new Error("API not available");
        const data = await res.json();
        briefingContent.innerHTML = data.briefing + "<br><br><span style='color:var(--slate-dim);font-size:0.65rem;'>(online - generated by AI)</span>";
      } catch (err) {
        const fallbackText = mockMissionBriefing(lastLmrs);
        briefingContent.innerHTML = fallbackText + "<br><br><span style='color:var(--slate-dim);font-size:0.65rem;'>(offline - generated locally)</span>";
      }
    });
  }

  if (copyBriefingBtn) {
    copyBriefingBtn.addEventListener('click', () => {
      if (briefingContent.textContent) {
        navigator.clipboard.writeText(briefingContent.innerText);
        const old = copyBriefingBtn.innerHTML;
        copyBriefingBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none"><path d="M20 6L9 17l-5-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
        setTimeout(() => copyBriefingBtn.innerHTML = old, 1500);
      }
    });
  }
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
async function renderCompareDrawer() {
  const drawer = document.getElementById('compareDrawer');
  drawer.hidden = false;
  
  // Instant local simulation preview
  const localResults = compareSet.map((c, i) => ({ i, c, r: computeLMRS(c.x, c.y) }));
  const localBestIdx = localResults.reduce((best, cur) => cur.r.LMRS > localResults[best].r.LMRS ? cur.i : best, 0);
  updateCompareTableUI(localResults, localBestIdx);
  
  const points = compareSet.map((c, i) => {
    const cell = cellAt(c.x, c.y);
    return {
      lat: cell.lat !== undefined ? cell.lat : -(89.9 - c.y * 0.02),
      lon: cell.lon !== undefined ? cell.lon : (c.x * 0.035 - 0.7),
      label: `Site ${i + 1}`
    };
  });
  
  try {
    const isPredictive = document.getElementById('routePredictiveBattery')?.checked || false;
    const body = {
      region_id: currentRegionKey,
      points: points,
      weights: null,
      use_predictive_battery: isPredictive
    };
    
    console.log("Sending comparison request to backend:", body);
    const res = await fetch(`${BACKEND_BASE_URL}/api/lmrs/compare`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    
    if (!res.ok) throw new Error(`HTTP status ${res.status}`);
    const data = await res.json();
    
    const results = data.results.map((resItem) => {
      const index = parseInt(resItem.label.split(' ')[1]) - 1;
      const pt = compareSet[index];
      return {
        i: index,
        c: pt,
        r: {
          LMRS: Math.round(resItem.lmrs_score),
          RAI: Math.round(resItem.rai.score),
          CommVisibility: Math.round(resItem.comm_visibility.score),
          ThermalScore: Math.round(resItem.thermal_risk.score),
          ice: {
            volume_m3: resItem.rai.ice_volume_m3,
            depth_m: resItem.rai.ice_depth_m,
            confidence: resItem.rai.confidence
          },
          reachable: resItem.thermal_risk.score > 2,
          energyWh: resItem.thermal_risk.energy_cost_wh
        }
      };
    });
    
    results.sort((a, b) => a.i - b.i);
    const bestIdx = parseInt(data.recommended.split(' ')[1]) - 1;
    updateCompareTableUI(results, bestIdx);
  } catch (err) {
    console.warn("Backend comparison failed, using local simulation values:", err);
  }
}

function updateCompareTableUI(results, bestIdx) {
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
          <td>${r.reachable ? Math.round(r.energyWh) : '<span class="tag-unreachable">Unreachable</span>'}</td>
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


async function ensureSwarmRoutes(forceNew) {
  if (swarm.a.route && swarm.b.route && !forceNew) return;
  
  const localRng = mulberry32(Date.now() % 100000);
  const startB = findSafeStart([
    { x: region.cols - 3, y: 2 }, { x: region.cols - 3, y: region.rows - 3 },
    { x: 2, y: region.rows - 3 }, { x: Math.floor(region.cols / 2), y: 1 },
  ]);
  
  let success = false;
  const cellLZ = cellAt(region.landing.x, region.landing.y);
  const cellStartB = cellAt(startB.x, startB.y);
  
  if (cellLZ && cellLZ.lat !== undefined && cellLZ.lon !== undefined && cellStartB) {
    try {
      const isPredictive = document.getElementById('routePredictiveBattery')?.checked || false;
      const targetA = pickIceTarget(localRng);
      const targetB = pickIceTarget(localRng);
      const cellTargetA = cellAt(targetA.x, targetA.y);
      const cellTargetB = cellAt(targetB.x, targetB.y);
      
      const body = {
        region_id: currentRegionKey,
        rovers: [
          {
            rover_id: 'rover-a',
            start_lat: cellLZ.lat,
            start_lon: cellLZ.lon,
            end_lat: cellTargetA.lat,
            end_lon: cellTargetA.lon,
            initial_battery_pct: 100.0,
            ice_seeking: false
          },
          {
            rover_id: 'rover-b',
            start_lat: cellStartB.lat !== undefined ? cellStartB.lat : -(89.9 - startB.y * 0.02),
            start_lon: cellStartB.lon !== undefined ? cellStartB.lon : (startB.x * 0.035 - 0.7),
            end_lat: cellTargetB.lat,
            end_lon: cellTargetB.lon,
            initial_battery_pct: 100.0,
            ice_seeking: true
          }
        ],
        dark_budget_wh: 80.0,
        shadow_penalty_weight: 5.0,
        use_predictive_battery: isPredictive
      };
      
      console.log("Planning swarm via backend:", body);
      const res = await fetch(`${BACKEND_BASE_URL}/api/swarm/plan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      
      if (!res.ok) throw new Error(`HTTP status ${res.status}`);
      const data = await res.json();
      
      const planA = data.plans.find(p => p.rover_id === 'rover-a');
      const planB = data.plans.find(p => p.rover_id === 'rover-b');
      
      if (planA && planA.route_found && planB && planB.route_found) {
        swarm.a.route = mapBackendRouteToFrontend(planA);
        swarm.b.route = mapBackendRouteToFrontend(planB);
        swarm.a.progress = 0; swarm.b.progress = 0;
        document.getElementById('swarmABattery').textContent = '100%';
        document.getElementById('swarmBBattery').textContent = '100%';
        render();
        success = true;
        console.log("Swarm routing completed successfully via backend.");
      }
    } catch (err) {
      console.warn("Backend swarm planning failed, using local simulation:", err);
    }
  }
  
  if (!success) {
    let routeA = null, routeB = null, targetA = null, targetB = null, tries = 0;
    while ((!routeA || !routeB) && tries++ < 25) {
      targetA = pickIceTarget(localRng);
      targetB = pickIceTarget(localRng);
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

async function askCopilot(question) {
  let success = false;
  let contextPoint = null;
  
  if (lastLmrsCell) {
    const cell = cellAt(lastLmrsCell.x, lastLmrsCell.y);
    if (cell && cell.lat !== undefined && cell.lon !== undefined) {
      contextPoint = { lat: cell.lat, lon: cell.lon };
    }
  }
  
  try {
    const body = {
      region_id: currentRegionKey,
      question: question,
      context_point: contextPoint
    };
    
    console.log("Sending copilot question to backend:", body);
    const res = await fetch(`${BACKEND_BASE_URL}/api/copilot/ask`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    
    if (!res.ok) throw new Error(`HTTP status ${res.status}`);
    const data = await res.json();
    
    pushMessage(data.answer, 'bot');
    success = true;
  } catch (err) {
    console.warn("Backend copilot query failed, using offline rule templates:", err);
  }
  
  if (!success) {
    setTimeout(() => pushMessage(answerCopilot(question), 'bot'), 380);
  }
}

document.getElementById('copilotForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const input = document.getElementById('copilotInput');
  const val = input.value.trim();
  if (!val) return;
  pushMessage(val, 'user');
  input.value = '';
  askCopilot(val);
});
document.querySelectorAll('.chip-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    pushMessage(btn.textContent, 'user');
    askCopilot(btn.textContent);
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

async function populateRegions() {
  // Purposefully disabled to preserve the 3 static HTML dropdown options
  // (shackleton, faustini, degerlache) exactly as they were in Harshita's commit.
  const select = document.getElementById('regionSelect');
  currentRegionKey = select.value;
}

async function loadRegion(key) {
  currentRegionKey = key;
  let data = null;
  // Disabled backend fetch to preserve the local procedural grid generation
  // exactly as it looked in Harshita's commit (46x28).


  if (data) {
    GRID_ROWS = data.rows;
    GRID_COLS = data.cols;
    
    const cells = Array.from({ length: data.rows }, () => []);
    data.cells.forEach(c => {
      let elevation = 0.5;
      if (c.is_hazard) elevation = 0.8;
      else if (c.is_shadowed) elevation = 0.2;
      
      cells[c.row][c.col] = {
        x: c.col,
        y: c.row,
        lat: c.lat,
        lon: c.lon,
        elevation: elevation,
        shadow: c.is_shadowed,
        hazard: c.is_hazard,
        boulder: c.is_hazard,
        craterId: c.crater_id !== undefined ? c.crater_id : -1,
        ice: c.ice_volume_m3 > 0 ? {
          volume_m3: c.ice_volume_m3,
          depth_m: (1 + c.ice_confidence * 4).toFixed(1),
          confidence: c.ice_confidence
        } : null
      };
    });
    
    region = {
      key: key,
      cells: cells,
      craters: [],
      cols: data.cols,
      rows: data.rows,
      landing: { x: 0, y: 0 },
      label: REGION_LABELS[key] || key
    };
    
    let LZ = null;
    for (let r = 0; r < data.rows; r++) {
      for (let c = 0; c < data.cols; c++) {
        if (!cells[r][c].hazard && !cells[r][c].shadow) {
          LZ = { x: c, y: r };
          break;
        }
      }
      if (LZ) break;
    }
    region.landing = LZ || { x: 0, y: 0 };
    console.log(`Region ${key} grid loaded successfully. LZ at:`, region.landing);
  } else {
    console.warn(`No cache for ${key}, falling back to local simulation generation`);
    GRID_ROWS = 28;
    GRID_COLS = 46;
    region = generateRegion(key);
  }
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

async function boot() {
  await populateRegions();
  await loadRegion(currentRegionKey);
}

boot();


// --- BUDGET OPTIMIZER INTEGRATION ---
document.addEventListener('DOMContentLoaded', () => {
  const budgetSlider = document.getElementById('budgetSlider');
  const budgetReadout = document.getElementById('budgetReadout');
  const runOptimizerBtn = document.getElementById('runOptimizerBtn');
  const optResultsBlock = document.getElementById('optResultsBlock');
  const optErrorBlock = document.getElementById('optErrorBlock');
  const optRecommended = document.getElementById('optRecommended');
  const optRunnersUp = document.getElementById('optRunnersUp');
  const optErrorText = document.getElementById('optErrorText');
  
  let optDebounce = null;

  function runOptimizer() {
    if (!region) return;
    const budget = Number(budgetSlider.value);
    budgetReadout.textContent = budget + ' Wh';
    
    const results = findOptimalSite(region, budget);
    
    if (results.recommended) {
      optErrorBlock.hidden = true;
      optResultsBlock.hidden = false;
      
      const r = results.recommended;
      optRecommended.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:flex-start;">
          <b>Grid ${r.result.cell.x}x${r.result.cell.y}</b>
          <span class="tag-best" style="color:var(--cyan); font-weight:bold; font-size:0.75rem;">★ Recommended</span>
        </div>
        <div style="margin-top:6px; font-family:var(--font-mono); font-size:0.75rem;">
          LMRS: <span style="color:var(--ice);">${r.result.LMRS}</span> | Yield: ${r.result.ice.volume_m3.toLocaleString()} m³<br>
          Cost: ${Math.round(r.result.energyWh)} Wh | Eff: ${r.efficiency.toFixed(2)} m³/Wh
        </div>
        <div style="margin-top:8px; font-size:0.75rem; color:var(--ice); border-top:1px solid var(--hairline); padding-top:6px; line-height:1.4;">
          ${r.explanation}
        </div>
      `;
      
      let html = '';
      results.runnersUp.forEach(ru => {
        const isRej = !ru.efficiency;
        const color = isRej ? 'var(--slate)' : 'var(--slate-dim)';
        const effText = ru.efficiency ? `${ru.efficiency.toFixed(2)} m³/Wh` : 'N/A';
        html += `
          <div class="swarm-card" style="opacity:0.7; margin-bottom:6px; padding:8px; display:block;">
            <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:4px;">
              <b>Grid ${ru.result.cell.x}x${ru.result.cell.y}</b>
              <span style="color:${color}; font-size:0.7rem; text-align:right; max-width:55%; line-height:1.2;">${ru.reason}</span>
            </div>
            <div style="font-family:var(--font-mono); font-size:0.7rem; color:var(--slate);">
              Ice: ${ru.result.ice.volume_m3.toLocaleString()} m³ | Cost: ${Math.round(ru.result.energyWh)} Wh
            </div>
          </div>
        `;
      });
      optRunnersUp.innerHTML = html;
      
      // Map Integration
      lastLmrs = r.result;
      lastLmrsCell = { x: r.cell.x, y: r.cell.y };
      activeRoute = buildRoute(region.landing, r.cell);
      // updateLmrsUI(r.result); // Update the LMRS panel implicitly
      
      // Update the pin coordinates
      document.getElementById('lmrsCoord').textContent = `${r.cell.x}x${r.cell.y}`;
      
    } else {
      optResultsBlock.hidden = true;
      optErrorBlock.hidden = false;
      if (results.minBudget) {
        optErrorText.innerHTML = `No reachable site fits a <b>${budget} Wh</b> budget.<br>Try raising it to at least <b>${results.minBudget} Wh</b>.<div style="margin-top:12px;"><button onclick="document.getElementById('budgetSlider').value=${results.minBudget}; document.getElementById('budgetSlider').dispatchEvent(new Event('input'));" style="padding:6px 12px; font-size:0.75rem; border-radius:4px; border:none; background:var(--cyan); color:var(--panel); cursor:pointer; font-weight:bold;">Auto-Adjust to ${results.minBudget} Wh</button></div>`;
      } else {
        optErrorText.textContent = `No reachable sites found in this region.`;
      }
      activeRoute = null;
    }
    
    render();
  }

  if (budgetSlider) {
    budgetSlider.addEventListener('input', () => {
      budgetReadout.textContent = budgetSlider.value + ' Wh';
      clearTimeout(optDebounce);
      optDebounce = setTimeout(runOptimizer, 150);
    });
  }
  
  if (runOptimizerBtn) {
    runOptimizerBtn.addEventListener('click', runOptimizer);
  }
});
