import re

with open('frontend/dashboard.js', 'r', encoding='utf-8') as f:
    js = f.read()

# Add modeOptimize to the tab switcher
if 'modeOptimize' not in js.split('document.getElementById(\'swarmControls\').hidden = mode !== \'swarm\';')[1][:200]:
    js = js.replace(
        "document.getElementById('swarmControls').hidden = mode !== 'swarm';",
        "document.getElementById('swarmControls').hidden = mode !== 'swarm';\n      const mo = document.getElementById('modeOptimize');\n      if(mo) mo.hidden = mode !== 'optimize';"
    )

with open('frontend/dashboard.js', 'w', encoding='utf-8') as f:
    f.write(js)
print("JS fixed")
