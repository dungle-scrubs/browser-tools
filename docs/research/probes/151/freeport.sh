#!/bin/bash
# Observe what Lighthouse does when --port names a genuinely free port.
cd /Users/kevin/dev/browser-tools/.scratch/retire-axi/work/151 || exit 1
PORT=9391
: > freeport-procs.txt
echo "listeners on $PORT before:"
lsof -nP -iTCP:$PORT -sTCP:LISTEN 2>/dev/null || echo "  none"
(
  i=0
  while [ "$i" -lt 60 ]; do
    ps -ax -o pid,command | grep -- "--remote-debugging-port=$PORT" | grep -v grep >> freeport-procs.txt
    i=$((i+1))
    sleep 0.5
  done
) &
sampler=$!
npx -y lighthouse@latest https://example.com --port=$PORT --output=json \
  --output-path=./reports/freeport-example.json > lh-freeport.stdout.txt 2> lh-freeport.stderr.txt
echo "lighthouse exit=$?"
kill "$sampler" 2>/dev/null
wait "$sampler" 2>/dev/null
echo "listeners on $PORT after:"
lsof -nP -iTCP:$PORT -sTCP:LISTEN 2>/dev/null || echo "  none"
echo "chrome-launcher lines:"
grep "ChromeLauncher" lh-freeport.stderr.txt | head -5
echo "browser processes seen on $PORT during run:"
grep -c . freeport-procs.txt
sort -u freeport-procs.txt | grep -v Helper | cut -c1-260
