import json
import sys

for path in sys.argv[1:]:
    with open(path) as fh:
        lhr = json.load(fh)
    print('===', path)
    print('lighthouseVersion:', lhr.get('lighthouseVersion'))
    print('requestedUrl:', lhr.get('requestedUrl'), '->', lhr.get('finalDisplayedUrl'))
    env = lhr.get('environment', {})
    print('hostUserAgent:', env.get('hostUserAgent'))
    print('networkUserAgent:', env.get('networkUserAgent'))
    print('benchmarkIndex:', env.get('benchmarkIndex'))
    print('gatherMode:', lhr.get('gatherMode'))
    print('runtimeError:', lhr.get('runtimeError'))
    print('runWarnings:', lhr.get('runWarnings'))
    cats = {k: v.get('score') for k, v in lhr.get('categories', {}).items()}
    print('categories:', cats)
    print('audit count:', len(lhr.get('audits', {})))
    print()
