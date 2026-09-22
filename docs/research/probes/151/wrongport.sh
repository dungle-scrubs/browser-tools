#!/bin/bash
# Observe what Lighthouse does when --port names a port with no browser on it.
cd /Users/kevin/dev/browser-tools/.scratch/retire-axi/work/151 || exit 1
echo "listeners on 9299 before:"
lsof -nP -iTCP:9299 -sTCP:LISTEN 2>/dev/null || echo "  none"
(
  i=0
  while [ "$i" -lt 40 ]; do
    ps -ax -o pid,command | grep -- '--remote-debugging-port=9299' | grep -v grep >> wrongport-procs.txt
    i=$((i+1))
    sleep 0.5
  done
) &
sampler=$!
npx -y lighthouse@latest https://example.com --port=9299 --output=json \
  --output-path=./reports/wrongport-example.json > lh-wrongport.stdout.txt 2> lh-wrongport.stderr.txt
echo "lighthouse exit=$?"
kill "$sampler" 2>/dev/null
wait "$sampler" 2>/dev/null
echo "listeners on 9299 after:"
lsof -nP -iTCP:9299 -sTCP:LISTEN 2>/dev/null || echo "  none"
echo "distinct chrome pids seen on 9299 during run:"
awk '{print $1}' wrongport-procs.txt 2>/dev/null | sort -u | tr '\n' ' '
echo
