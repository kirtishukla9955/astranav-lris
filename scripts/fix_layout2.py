import re

with open('frontend/dashboard.html', 'r', encoding='utf-8') as f:
    html = f.read()

# Fix contingencyPanel inline style using regex to catch newlines
html = re.sub(
    r'id="contingencyPanel"\s+style="',
    'id="contingencyPanel" style="position: absolute; right: 0; top: 0; bottom: 0; width: 340px; ',
    html
)

with open('frontend/dashboard.html', 'w', encoding='utf-8') as f:
    f.write(html)
print("Layout fixed 2")
