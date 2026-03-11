import re

with open('src/application/use_cases/execute_trade.py', 'r', encoding='utf-8') as f:
    text = f.read()

counter = [0]
def repl(m):
    counter[0] += 1
    return f'raise RuntimeError(\"RETURN_FALSE_TRIGGERED at {counter[0]}\")'

text = re.sub(r'return False', repl, text)

with open('src/application/use_cases/execute_trade.py', 'w', encoding='utf-8') as f:
    f.write(text)
