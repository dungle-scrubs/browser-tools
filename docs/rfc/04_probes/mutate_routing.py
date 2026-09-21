"""Break each rule RFC-04's Routing section states. Every row must read CAUGHT.

Run serially. Two of these harnesses at once each restore the other's file.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TESTS = "tests/test_frame_routing.py tests/test_frame_manager.py tests/test_frame_session_discovery.py"

CASES = [
    (
        "key the context map by the id alone",
        "src/browser_tools/frame_manager.py",
        "            self._execution_contexts[(session_id, context_id)] = frame_id\n",
        "            self._execution_contexts[(None, context_id)] = frame_id\n",
    ),
    (
        "let a destroy clear whichever frame holds the id",
        "src/browser_tools/frame_manager.py",
        "        frame_id = self._execution_contexts.pop((session_id, context_id), None)\n",
        "        frame_id = next(\n"
        "            (v for k, v in list(self._execution_contexts.items()) if k[1] == context_id),\n"
        "            None,\n"
        "        )\n",
    ),
    (
        "let a destroy cross the session boundary",
        "src/browser_tools/frame_manager.py",
        "            and frame.frame_session_id == session_id\n        ):\n",
        "        ):\n",
    ),
    (
        "clear every session's contexts on one session's clear",
        "src/browser_tools/frame_manager.py",
        "        for key in [k for k in self._execution_contexts if k[0] == session_id]:\n"
        "            del self._execution_contexts[key]\n"
        "        for frame in self._frames.values():\n"
        "            if frame.frame_session_id == session_id:\n"
        "                frame.execution_context_id = None\n",
        "        self._execution_contexts.clear()\n"
        "        for frame in self._frames.values():\n"
        "            frame.execution_context_id = None\n",
    ),
    (
        "return the context id without its session",
        "src/browser_tools/frame_manager.py",
        "        return frame.frame_session_id, frame.execution_context_id\n",
        "        return None, frame.execution_context_id\n",
    ),
    (
        "send the frame-scoped read on the page session",
        "src/browser_tools/cdp_handler.py",
        "        frame_cdp = self.client_for_session(context[0]) if context else cdp\n",
        "        frame_cdp = cdp\n",
    ),
    (
        "ignore the session when building the client",
        "src/browser_tools/cdp_handler.py",
        "        if frame_session_id is None or self._cdp_client is None:\n",
        "        if True:\n",
    ),
    (
        "subscribe a child's Runtime events after the enable that replays them",
        "src/browser_tools/frame_sessions.py",
        '        self._subscribe_runtime(session.session_id)\n'
        '        await self._client.send(method="Runtime.enable", session_id=session.session_id)\n',
        '        await self._client.send(method="Runtime.enable", session_id=session.session_id)\n'
        "        self._subscribe_runtime(session.session_id)\n",
    ),
    (
        "file every child's contexts under the last session id",
        "src/browser_tools/frame_sessions.py",
        "            self._client.on(event, functools.partial(handler, session_id=session_id), session_id)\n",
        "            self._client.on(event, handler, session_id)\n",
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
        print(f"SKIP     {name:58}  anchor appears {original.count(old)} times")
        missed += 1
        continue
    path.write_text(original.replace(old, new))
    try:
        code, last = run()
    finally:
        path.write_text(original)
    if code == 0:
        missed += 1
    print(f"{'CAUGHT' if code else 'MISSED':8} {name:58}  {last}", flush=True)

code, last = run("tests/")
print(f"\nrestored, full suite: {last}")
sys.exit(1 if missed or code else 0)
