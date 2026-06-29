import re

with open('frontend/dashboard.js', 'r', encoding='utf-8') as f:
    js = f.read()

# Fix swarm-card inline style for runners up
js = js.replace(
    'class="swarm-card" style="opacity:0.7; margin-bottom:6px; padding:8px;"',
    'class="swarm-card" style="opacity:0.7; margin-bottom:6px; padding:8px; display:block;"'
)

# Also fix mÂ³ weird encoding while we're at it (from the PowerShell output I saw it as mA3)
js = js.replace('mA3', 'm³')
js = js.replace('mÂ³', 'm³')

with open('frontend/dashboard.js', 'w', encoding='utf-8') as f:
    f.write(js)
print("Runners up CSS fixed")
