import re

# Patch dashboard.html
with open('frontend/dashboard.html', 'r', encoding='utf-8') as f:
    html = f.read()

# Add script tag
if 'optimizer.js' not in html:
    html = html.replace('<script src="dashboard.js"></script>', '<script src="optimizer.js"></script>\n<script src="dashboard.js"></script>')

# Add mode tab
if 'data-mode="optimize"' not in html:
    html = html.replace('<button class="mode-tab" data-mode="swarm">Swarm View</button>',
                        '<button class="mode-tab" data-mode="swarm">Swarm View</button>\n      <button class="mode-tab" data-mode="optimize">Budget Optimizer</button>')

# Add Optimizer Panel
optimizer_html = """
    <!-- BUDGET OPTIMIZER MODE -->
    <div id="modeOptimize" class="mode-panel" hidden>
      <div class="rail-block">
        <h4 class="rail-title">Mission Energy Budget</h4>
        <div style="display:flex; justify-content:space-between; margin-bottom:5px;">
          <span class="mono" style="font-size:0.75rem; color:var(--slate);">Budget (Wh)</span>
          <span class="mono" id="budgetReadout" style="font-size:0.8rem; color:var(--cyan); font-weight:bold;">150 Wh</span>
        </div>
        <input type="range" id="budgetSlider" min="20" max="500" value="150" class="scrubber" style="margin-bottom:15px;">
        <button class="btn btn-primary" id="runOptimizerBtn" style="width:100%; margin-bottom: 10px;">Find Best Site</button>
      </div>

      <div class="rail-block" id="optResultsBlock" hidden>
        <h4 class="rail-title">Optimization Results</h4>
        
        <div id="optRecommended" class="swarm-card best" style="border:1px solid var(--cyan); background:rgba(255,255,255,0.05); margin-bottom:15px;">
          <!-- Populated by JS -->
        </div>

        <h4 class="rail-title" style="margin-top:10px; font-size:0.7rem;">Runners-Up</h4>
        <div id="optRunnersUp">
          <!-- Populated by JS -->
        </div>
      </div>
      
      <div class="rail-block" id="optErrorBlock" hidden>
        <div style="padding:10px; border:1px solid var(--hairline); border-radius:4px; font-family:var(--font-mono); font-size:0.75rem; color:var(--slate);">
          <span id="optErrorText"></span>
        </div>
      </div>
    </div>
"""

if 'id="modeOptimize"' not in html:
    html = html.replace('<!-- SWARM VIEW MODE -->', optimizer_html + '\n    <!-- SWARM VIEW MODE -->')

with open('frontend/dashboard.html', 'w', encoding='utf-8') as f:
    f.write(html)

# Patch dashboard.js
with open('frontend/dashboard.js', 'r', encoding='utf-8') as f:
    js = f.read()

# Register the tab in the loop (it might just automatically work if the loop checks all `.mode-tab`, let's verify how mode tabs work)
# dashboard.js: document.querySelectorAll('.mode-tab').forEach(t => ...)
# So we just need to add the logic for when `activeMode === 'optimize'`.

# Let's add the wiring logic inside the DOMContentLoaded block or at the bottom.
wiring_js = """
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
          <div class="swarm-card" style="opacity:0.7; margin-bottom:6px; padding:8px;">
            <div style="display:flex; justify-content:space-between;">
              <b>Grid ${ru.result.cell.x}x${ru.result.cell.y}</b>
              <span style="color:${color}; font-size:0.7rem;">${ru.reason}</span>
            </div>
            <div style="font-family:var(--font-mono); font-size:0.7rem; color:var(--slate); margin-top:4px;">
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
        optErrorText.innerHTML = `No reachable site fits a <b>${budget} Wh</b> budget.<br>Try raising it to at least <b>${results.minBudget} Wh</b>.`;
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
"""

if 'BUDGET OPTIMIZER INTEGRATION' not in js:
    js += '\n' + wiring_js

# We also need to draw the pin for the optimized site. The render loop already calls `drawPin()` for `lastLmrsCell`!
# Because I set `lastLmrsCell = { x: r.cell.x, y: r.cell.y }` and `activeRoute = buildRoute()`, the existing `render()` loop will automatically draw the route and the pin.

with open('frontend/dashboard.js', 'w', encoding='utf-8') as f:
    f.write(js)

print("Patch applied successfully.")
