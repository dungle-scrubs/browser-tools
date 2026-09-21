"""Break each rule the discovery fix states. Every row must read CAUGHT.

Run serially. Two of these harnesses running at once each restore the
other's file and the result is meaningless.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TESTS = "tests/test_frame_session_discovery.py tests/test_cross_origin_frames.py"

CASES = [
    (
        "start the invocation before setup finishes",
        "src/browser_tools/cdp_handler.py",
        "        return self._cdp_client is not None and self._cdp_client.connected "
        "and self._ready.is_set()\n",
        "        return self._cdp_client is not None and self._cdp_client.connected\n",
    ),
    (
        "call the runtime ready before Frame Sessions start",
        "src/browser_tools/cdp_handler.py",
        "            if self._all_frames:\n"
        "                await self._start_frame_sessions(cdp, session_id)\n",
        "            self._ready.set()\n"
        "            if self._all_frames:\n"
        "                await self._start_frame_sessions(cdp, session_id)\n",
    ),
    (
        "record the unreachable row before the drop that clears it",
        "src/browser_tools/frame_sessions.py",
        "        url = session.url\n        self.drop(session.session_id)\n"
        "        self._unreachable[session.target_id] = url\n",
        "        self._unreachable[session.target_id] = session.url\n"
        "        self.drop(session.session_id)\n",
    ),
    (
        "splice the held trees in one pass",
        "src/browser_tools/frame_sessions.py",
        "            if not spliced:\n                return\n",
        "            return\n",
    ),
    (
        "let a frame past the depth bound stay",
        "src/browser_tools/frame_sessions.py",
        "        if session.depth <= MAX_DEPTH:\n            return False\n",
        "        if True:\n            return False\n",
    ),
    (
        "take a second session for a target already held",
        "src/browser_tools/frame_sessions.py",
        "        if session_id in self._sessions or self._has_target(target_id):\n",
        "        if session_id in self._sessions:\n",
    ),
    (
        "drive this runtime's own page target as a frame",
        "src/browser_tools/frame_sessions.py",
        '        return info.get("targetId") != self._page_target_id\n',
        "        return True\n",
    ),
    (
        "count depth from the frame instead of from the root",
        "src/browser_tools/frame_manager.py",
        "            current = self._frames.get(parent_id)\n            depth += 1\n",
        "            current = None\n            depth += 1\n",
    ),
]


def run(tests=TESTS):
    try:
        proc = subprocess.run(
            ["uv", "run", "pytest", *tests.split(), "-q", "--no-header"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return 1, "(the mutant did not terminate within 600s)"
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode, lines[-1] if lines else "(no output)"


missed = 0
for name, rel, old, new in CASES:
    path = ROOT / rel
    original = path.read_text()
    if original.count(old) != 1:
        print(f"SKIP     {name:52}  anchor appears {original.count(old)} times")
        missed += 1
        continue
    path.write_text(original.replace(old, new))
    try:
        code, last = run()
    finally:
        path.write_text(original)
    if code == 0:
        missed += 1
    print(f"{'CAUGHT' if code else 'MISSED':8} {name:52}  {last}", flush=True)

code, last = run("tests/")
print(f"\nrestored, full suite: {last}")
sys.exit(1 if missed or code else 0)
