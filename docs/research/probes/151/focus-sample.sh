#!/bin/bash
# Sample the frontmost application pid every 0.3s for N iterations.
# Usage: focus-sample.sh <iterations> <outfile>
n="$1"
out="$2"
: > "$out"
i=0
while [ "$i" -lt "$n" ]; do
  asn=$(lsappinfo front 2>/dev/null)
  pid=$(lsappinfo info -only pid "$asn" 2>/dev/null | grep -o 'pid = [0-9]*' | grep -o '[0-9]*')
  name=$(lsappinfo info -only name "$asn" 2>/dev/null | head -1)
  printf '%s %s %s\n' "$(date +%H:%M:%S)" "$pid" "$name" >> "$out"
  i=$((i+1))
  sleep 0.3
done
