#!/usr/bin/env python3
"""Report which insight-relevant event names a captured trace carries."""
import json
import sys
from collections import Counter

d = json.load(open(sys.argv[1]))
evs = d["traceEvents"] if isinstance(d, dict) else d
names = Counter(e.get("name", "") for e in evs)
print(f"total events: {len(evs)}   distinct names: {len(names)}")

probes = [
    "largestContentfulPaint::Candidate",
    "LayoutShift",
    "navigationStart",
    "firstContentfulPaint",
    "ResourceSendRequest",
    "ResourceReceiveResponse",
    "RunTask",
    "EventTimingr",
    "EventTiming",
    "ParseHTML",
    "TracingStartedInBrowser",
    "Profile",
    "ProfileChunk",
]
print("\nprobe                                 count")
for p in probes:
    print(f"  {p:<36} {names.get(p, 0)}")

print("\ntop 12 names:")
for n, c in names.most_common(12):
    print(f"  {c:>6}  {n}")
