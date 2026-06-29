with open('frontend/dashboard.css', 'a', encoding='utf-8') as f:
    f.write('\n/* Fix dropdown options visibility */\n')
    f.write('.region-select option {\n  background-color: #0a0d14;\n  color: #e5ebf0;\n}\n')

print("CSS fixed")
