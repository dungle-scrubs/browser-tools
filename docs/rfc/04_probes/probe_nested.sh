#!/usr/bin/env bash
# A embeds B (cross-origin) embeds C (cross-origin again), plus a same-origin
# frame inside B, so one page carries every case at once.
set -uo pipefail
REPO=/Users/kevin/dev/browser-tools
D=$(mktemp -d /tmp/bt-rfc04n.XXXXXX)
BT=(uv run bt)
cd "$REPO" || exit 1
PA=8131; PB=8132; PC=8133

mkdir -p "$D/a" "$D/b" "$D/c"
cat >"$D/c/index.html" <<'HTML'
<!doctype html><title>C</title><body><h1>C</h1><input id="cvv"></body>
HTML
cat >"$D/b/same.html" <<'HTML'
<!doctype html><title>B-same</title><body><h1>B same-origin child</h1></body>
HTML
cat >"$D/b/index.html" <<HTML
<!doctype html><title>B</title><body><h1>B</h1>
<iframe src="http://[::1]:$PC/" width="200" height="60"></iframe>
<iframe src="same.html" width="200" height="60"></iframe>
</body>
HTML
cat >"$D/a/index.html" <<HTML
<!doctype html><title>A</title><body><h1>A</h1>
<iframe src="http://localhost:$PB/" width="400" height="200"></iframe>
</body>
HTML

python3 -m http.server "$PA" --bind 127.0.0.1 -d "$D/a" >/dev/null 2>&1 & S1=$!
python3 -m http.server "$PB" --bind 127.0.0.1 -d "$D/b" >/dev/null 2>&1 & S2=$!
python3 -c "
import http.server, socketserver, sys, os
os.chdir(sys.argv[1])
class S(socketserver.TCPServer): address_family = __import__('socket').AF_INET6
S(('::1', int(sys.argv[2])), http.server.SimpleHTTPRequestHandler).serve_forever()
" "$D/c" "$PC" >/dev/null 2>&1 & S3=$!

INSTANCE=$("${BT[@]}" launch --headless | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')
trap 'kill $S1 $S2 $S3 2>/dev/null; "${BT[@]}" stop "$INSTANCE" >/dev/null 2>&1; rm -rf "$D"' EXIT
PORT=$("${BT[@]}" status | python3 -c "
import json,sys
for row in json.load(sys.stdin):
    if row['name'] == '$INSTANCE': print(row['port']); break
")
echo "instance: $INSTANCE  port: $PORT"
[ -z "$PORT" ] && { echo "no port; refusing to probe someone else's browser"; exit 1; }
"${BT[@]}" "$INSTANCE" Page.navigate "{\"url\": \"http://127.0.0.1:$PA/\"}" >/dev/null
"${BT[@]}" "$INSTANCE" wait-idle >/dev/null 2>&1
sleep 1.5
WS=$(curl -s "http://127.0.0.1:$PORT/json/version" | python3 -c 'import json,sys; print(json.load(sys.stdin)["webSocketDebuggerUrl"])')
uv run python "$REPO/.scratch/rfc-04/probe_nesting.py" "$WS" "http://127.0.0.1:$PA/"
