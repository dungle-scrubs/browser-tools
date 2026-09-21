#!/usr/bin/env bash
# RFC-04 Phase 2b: what stitching across Frame Sessions costs `snapshot`.
# Resolves its own instance BY NAME and refuses to run without one.
set -uo pipefail
cd "$(dirname "$0")/../../.." || exit 1
D=$(mktemp -d /tmp/bt-snapcost.XXXXXX)
BT=(uv run bt)
mkdir -p "$D/leaf" "$D/plain" "$D/wide"
printf '<!doctype html><title>leaf</title><body><button>B</button><input></body>' >"$D/leaf/index.html"
printf '<!doctype html><title>plain</title><body><button>B</button></body>' >"$D/plain/index.html"
{ echo '<!doctype html><title>wide</title><body>'
  for i in $(seq 1 10); do echo "<iframe src=\"http://localhost:8151/?a=$i\"></iframe>"; done
  echo '</body>'; } >"$D/wide/index.html"
python3 -m http.server 8151 --bind 127.0.0.1 -d "$D/leaf"  >/dev/null 2>&1 & P1=$!
python3 -m http.server 8153 --bind 127.0.0.1 -d "$D/plain" >/dev/null 2>&1 & P2=$!
python3 -m http.server 8154 --bind 127.0.0.1 -d "$D/wide"  >/dev/null 2>&1 & P3=$!
I=$("${BT[@]}" launch --headless | python3 -c 'import json,sys;print(json.load(sys.stdin)["name"])')
[ -z "$I" ] && { echo 'no instance; refusing to drive another browser'; exit 1; }
trap 'kill $P1 $P2 $P3 2>/dev/null; "${BT[@]}" stop "$I" >/dev/null 2>&1; rm -rf "$D"' EXIT

timeit() { local label="$1"; shift; local total=0 n=9
  for _ in $(seq 1 $n); do local t0=$EPOCHREALTIME; "$@" >/dev/null 2>&1; local t1=$EPOCHREALTIME
    total=$(python3 -c "print($total + ($t1 - $t0) * 1000)"); done
  python3 -c "print(f'  {\"$label\":44} {$total / $n:7.1f} ms')"; }

for page in "8153 no iframe" "8154 ten cross-origin iframes"; do
  set -- $page; port=$1; shift; label="$*"
  "${BT[@]}" "$I" Page.navigate "{\"url\":\"http://127.0.0.1:$port/\"}" >/dev/null
  "${BT[@]}" "$I" wait-idle >/dev/null 2>&1; sleep 1.5
  timeit "$label, snapshot" "${BT[@]}" "$I" snapshot
  timeit "$label, snapshot --frames all" "${BT[@]}" "$I" snapshot --frames all
  a=$("${BT[@]}" "$I" snapshot | grep -o 'button' | wc -l | tr -d ' ')
  b=$("${BT[@]}" "$I" snapshot --frames all | grep -o 'button' | wc -l | tr -d ' ')
  echo "    button nodes reachable: default $a, --frames all $b"
done
