#!/usr/bin/env python3
"""Print a Run Document compactly: the run summary, then one line per step."""
import json
import sys

d = json.load(open(sys.argv[1]))
print("run:", json.dumps(d.get("run", {})))
for i, s in enumerate(d.get("steps", []), start=1):
    step = str(s.get("step", ""))[:60]
    res = json.dumps(s.get("result"))
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    print(f"  {i}. {step}")
    print(f"     -> {res[:limit]}")
