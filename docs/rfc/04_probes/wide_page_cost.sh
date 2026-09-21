#!/usr/bin/env bash
# RFC-04 Open Question 4: what --frames all costs on a wide page.
#
# Serves a page holding twenty sibling cross-origin iframes from two loopback
# origins, then times `frames list` with and against the flag. Also reports
# how many of the twenty each run actually lists, which is what caught the
# readiness race: the count was 0, 8, 12 or 20 from one identical run to the
# next.
#
# Resolves its own instance BY NAME. An earlier version took the first row of
# `bt status` and attached to a browser this script did not launch.
set -uo pipefail
cd "$(dirname "$0")/../../.." || exit 1

D=$(mktemp -d /tmp/bt-wide.XXXXXX)
BT=(uv run bt)
mkdir -p "$D/wide" "$D/child" "$D/plain"
echo '<!doctype html><title>ad</title><body>ad</body>' >"$D/child/index.html"
echo '<!doctype html><title>plain</title><body>plain</body>' >"$D/plain/index.html"
{
  echo '<!doctype html><title>wide</title><body><h1>wide</h1>'
  for i in $(seq 1 20); do
    echo "<iframe src=\"http://localhost:8128/?ad=$i\" width=60 height=40></iframe>"
  done
  echo '</body>'
} >"$D/wide/index.html"

python3 -m http.server 8128 --bind 127.0.0.1 -d "$D/child" >/dev/null 2>&1 & S2=$!
python3 -m http.server 8129 --bind 127.0.0.1 -d "$D/plain" >/dev/null 2>&1 & S3=$!
python3 -m http.server 8130 --bind 127.0.0.1 -d "$D/wide" >/dev/null 2>&1 & S4=$!

I=$("${BT[@]}" launch --headless | python3 -c 'import json,sys;print(json.load(sys.stdin)["name"])')
[ -z "$I" ] && { echo 'no instance; refusing to drive someone else browser'; exit 1; }
trap 'kill $S2 $S3 $S4 2>/dev/null; "${BT[@]}" stop "$I" >/dev/null 2>&1; rm -rf "$D"' EXIT

timeit() {
  local label="$1"; shift
  local total=0 n=9
  for _ in $(seq 1 $n); do
    local t0=$EPOCHREALTIME
    "$@" >/dev/null 2>&1
    local t1=$EPOCHREALTIME
    total=$(python3 -c "print($total + ($t1 - $t0) * 1000)")
  done
  python3 -c "print(f'  {\"$label\":46} {$total / $n:7.1f} ms')"
}

for page in "8129 no iframe at all" "8130 twenty Out-of-Process Frames"; do
  set -- $page
  port=$1; shift
  label="$*"
  "${BT[@]}" "$I" Page.navigate "{\"url\":\"http://127.0.0.1:$port/\"}" >/dev/null
  "${BT[@]}" "$I" wait-idle >/dev/null 2>&1
  sleep 1.5
  timeit "$label, --frames page" "${BT[@]}" "$I" frames list
  timeit "$label, --frames all" "${BT[@]}" "$I" frames list --frames all
  for r in $(seq 1 10); do
    printf '    run %2d listed %s out-of-process\n' "$r" \
      "$("${BT[@]}" "$I" frames list --frames all | grep -o 'out-of-process' | wc -l | tr -d ' ')"
  done
done
