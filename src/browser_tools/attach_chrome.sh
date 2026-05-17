#!/usr/bin/env bash
# attach-chrome.sh — Launch Chrome with remote debugging for browser-tools attach
#
# Usage:
#   ./attach-chrome.sh [--port PORT] [--profile PROFILE] [URL]
#
# Examples:
#   ./attach-chrome.sh                          # Default port 9222
#   ./attach-chrome.sh --port 9333              # Custom port
#   ./attach-chrome.sh --profile dev            # Named profile
#   ./attach-chrome.sh https://myapp.localhost   # Open specific URL

set -euo pipefail

PORT=9222
PROFILE=""
URL=""
CHROME_CANARY="/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"
CHROME_STABLE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)
            PORT="$2"
            shift 2
            ;;
        --profile)
            PROFILE="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [--port PORT] [--profile PROFILE] [URL]"
            echo ""
            echo "Launch Chrome with remote debugging enabled for browser-tools."
            echo ""
            echo "Options:"
            echo "  --port PORT      Remote debugging port (default: 9222)"
            echo "  --profile NAME   Named profile for persistent sessions"
            echo "  URL              URL to open on launch"
            echo ""
            echo "Then in your agent, use:"
            echo '  attach_browser(endpoint="http://127.0.0.1:PORT")'
            exit 0
            ;;
        *)
            URL="$1"
            shift
            ;;
    esac
done

# Find Chrome executable
CHROME=""
if [[ -x "$CHROME_CANARY" ]]; then
    CHROME="$CHROME_CANARY"
elif [[ -x "$CHROME_STABLE" ]]; then
    CHROME="$CHROME_STABLE"
elif command -v google-chrome &>/dev/null; then
    CHROME="google-chrome"
elif command -v chromium &>/dev/null; then
    CHROME="chromium"
else
    echo "Error: Chrome not found. Install Chrome or Chrome Canary." >&2
    exit 1
fi

# Build user-data-dir path
CACHE_DIR="$HOME/.cache/tool-proxy/browser-tools"
if [[ -n "$PROFILE" ]]; then
    USER_DATA_DIR="$CACHE_DIR/profiles/$PROFILE"
else
    USER_DATA_DIR="$CACHE_DIR/profiles/attach-$PORT"
fi

mkdir -p "$USER_DATA_DIR"
chmod 700 "$USER_DATA_DIR"

echo "Launching Chrome with remote debugging on port $PORT..."
echo "  Executable: $CHROME"
echo "  Profile:    $USER_DATA_DIR"

ARGS=(
    "--remote-debugging-port=$PORT"
    "--user-data-dir=$USER_DATA_DIR"
    "--no-first-run"
    "--no-default-browser-check"
    "--disable-sync"
)

if [[ -n "$URL" ]]; then
    ARGS+=("$URL")
fi

# Detach Chrome into a new session so it survives if our parent shell is
# killed (e.g. an agent's Bash tool call hits its timeout). nohup alone is
# not enough on macOS: when the shell's process group is signaled, the
# child dies with it. Python's start_new_session=True calls os.setsid()
# in the child before exec, putting Chrome in its own session/process group.
python3 - "$CHROME" "${ARGS[@]}" <<'PY'
import subprocess, sys
subprocess.Popen(
    sys.argv[1:],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
PY

# Wait for the remote debugging endpoint to become reachable so the caller
# can attach immediately on return.
for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
        echo ""
        echo "Chrome ready on http://127.0.0.1:$PORT"
        echo "Connect with: attach_browser(endpoint=\"http://127.0.0.1:$PORT\")"
        exit 0
    fi
    sleep 0.5
done

echo "Error: Chrome did not become ready on port $PORT within 30s." >&2
echo "If another Chrome is already using $USER_DATA_DIR, kill it first or use --profile NAME." >&2
exit 1
