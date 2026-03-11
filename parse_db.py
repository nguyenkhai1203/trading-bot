import re

with open('test_out.txt', 'r', encoding='utf-8', errors='replace') as f:
    text = f.read()

match = re.search(r'ALL TRADES IN DB:\s*(\[.*?\])', text, re.DOTALL)
if match:
    import ast
    try:
        trades = ast.literal_eval(match.group(1))
        for t in trades:
            print(f"ID: {t.get('id')} STATUS: {t.get('status')!r} PRO: {t.get('profile_id')} KEY: {t.get('pos_key')}")
    except Exception as e:
        print('Could not eval:', e, match.group(1)[:100])
else:
    print('NOT FOUND')
