import re

with open('frontend/dashboard.html', 'r', encoding='utf-8') as f:
    html = f.read()

# Fix contingencyPanel inline style
html = html.replace(
    '<aside class="lmrs-panel contingency-theme" id="contingencyPanel" style="border-left: 1px solid #ff6b5e; background: rgba(15, 6, 6, 0.95); z-index: 61;">',
    '<aside class="lmrs-panel contingency-theme" id="contingencyPanel" style="border-left: 1px solid #ff6b5e; background: rgba(15, 6, 6, 0.95); z-index: 61; position: absolute; right: 0; top: 0; bottom: 0; width: 340px;">'
)

# Insert modeOptimize if not present
optimizer_html = """
    <!-- BUDGET OPTIMIZER MODE -->
    <div id="modeOptimize" class="rail-block" hidden>
      <h4 class="rail-title">Mission Energy Budget</h4>
      <div style="display:flex; justify-content:space-between; margin-bottom:5px;">
        <span class="mono" style="font-size:0.75rem; color:var(--slate);">Budget (Wh)</span>
        <span class="mono" id="budgetReadout" style="font-size:0.8rem; color:var(--cyan); font-weight:bold;">150 Wh</span>
      </div>
      <input type="range" id="budgetSlider" min="20" max="500" value="150" class="scrubber" style="margin-bottom:15px; width: 100%;">
      <button class="btn btn-primary icon-btn-wide" id="runOptimizerBtn" style="margin-top: 0; margin-bottom: 10px;">Find Best Site</button>

      <div id="optResultsBlock" hidden>
        <h4 class="rail-title">Optimization Results</h4>
        <div id="optRecommended" class="swarm-card best" style="border:1px solid var(--cyan); background:rgba(255,255,255,0.05); margin-bottom:15px; display: block;">
          <!-- Populated by JS -->
        </div>
        <h4 class="rail-title" style="margin-top:10px; font-size:0.7rem;">Runners-Up</h4>
        <div id="optRunnersUp">
          <!-- Populated by JS -->
        </div>
      </div>
      
      <div id="optErrorBlock" hidden>
        <div style="padding:10px; border:1px solid var(--hairline); border-radius:4px; font-family:var(--font-mono); font-size:0.75rem; color:var(--slate);">
          <span id="optErrorText"></span>
        </div>
      </div>
    </div>
"""

if 'id="modeOptimize"' not in html:
    html = html.replace('    <div class="rail-block" id="swarmControls" hidden>', optimizer_html + '\n    <div class="rail-block" id="swarmControls" hidden>')

with open('frontend/dashboard.html', 'w', encoding='utf-8') as f:
    f.write(html)
print("Layout fixed")
