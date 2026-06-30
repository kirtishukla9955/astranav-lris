import re
html = open('index.html').read()
from bs4 import BeautifulSoup
soup = BeautifulSoup(html, 'html.parser')
paths = soup.find_all('path')
for i, p in enumerate(paths):
    print(f"Path {i}: class='{p.get('class', [''])[0]}' d='{p.get('d', '')}'")
