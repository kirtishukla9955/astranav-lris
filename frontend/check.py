import re
html = open('index.html').read()
paths = re.findall(r'd="([^"]+)"', html)
for p in paths:
    # check if any coordinate pair is duplicated
    coords = re.findall(r'\b\d+,\d+\b', p)
    if len(coords) != len(set(coords)):
        print(f"DUPLICATE FOUND IN: {p}")
        print(f"Coords: {coords}")
