#!/usr/bin/env bash
# Stand up the two-origin page, then run the auto-attach probe against it.
set -uo pipefail
REPO=/Users/kevin/dev/browser-tools
D=$(mktemp -d /tmp/bt-rfc04.XXXXXX)
BT=(uv run bt)
cd "$REPO" || exit 1
P1=8127; P2=8128

mkdir -p "$D/parent" "$D/child"
cat >"$D/child/index.html" <<'HTML'
<!doctype html><title>child-origin</title>
<body><h1>child</h1><input id="card" placeholder="card number">
<script>localStorage.setItem("childkey", "childvalue")</script></body>
HTML
cat >"$D/parent/index.html" <<HTML
<!doctype html><title>parent-origin</title>
<body><h1>parent</h1>
<iframe src="http://localhost:$P2/" width="300" height="120"></iframe>
</body>
HTML

python3 -m http.server "$P1" --bind 127.0.0.1 -d "$D/parent" >/dev/null 2>&1 & S1=$!
python3 -m http.server "$P2" --bind 127.0.0.1 -d "$D/child" >/dev/null 2>&1 & S2=$!

INSTANCE=$("${BT[@]}" launch --headless | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')
trap 'kill $S1 $S2 2>/dev/null; "${BT[@]}" stop "$INSTANCE" >/dev/null 2>&1; rm -rf "$D"' EXIT
PORT=$("${BT[@]}" status | python3 -c "
import json,sys
for row in json.load(sys.stdin):
    if row['name'] == '$INSTANCE': print(row['port']); break
")
echo "instance: $INSTANCE  port: $PORT"
[ -z "$PORT" ] && { echo "no port; refusing to probe someone else's browser"; exit 1; }

"${BT[@]}" "$INSTANCE" Page.navigate "{\"url\": \"http://127.0.0.1:$P1/\"}" >/dev/null
"${BT[@]}" "$INSTANCE" wait-idle >/dev/null 2>&1
sleep 1

WS=$(curl -s "http://127.0.0.1:$PORT/json/version" | python3 -c 'import json,sys; print(json.load(sys.stdin)["webSocketDebuggerUrl"])')
echo "browser ws: $WS"
uv run python "$REPO/.scratch/rfc-04/${PROBE:-probe_autoattach.py}" "$WS" "http://127.0.0.1:$P1/"
