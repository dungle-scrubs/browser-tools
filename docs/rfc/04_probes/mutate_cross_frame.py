"""Break each rule RFC-04 Phase 2b states. Every row must read CAUGHT.

Run serially. Two of these harnesses at once each restore the other's file.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TESTS = (
    "tests/test_cross_frame_snapshot.py tests/test_native_snapshot.py "
    "tests/test_native_interaction.py tests/test_frame_routing.py"
)

CASES = [
    (
        "mint every node under the build's token",
        "src/browser_tools/native_snapshot.py",
        '            uid = mint(backend, str(raw.get(NODE_DOC_TOKEN_KEY) or doc_token))\n',
        "            uid = mint(backend, doc_token)\n",
    ),
    (
        "stamp every node with the top document",
        "src/browser_tools/native_snapshot.py",
        "        take(child_nodes, child.doc_token)\n",
        "        take(child_nodes, top_doc_token)\n",
    ),
    (
        "find the owner by backend id alone",
        "src/browser_tools/native_snapshot.py",
        "        owner = by_owner.get((child.owner_doc_token, child.owner_backend_node_id))\n",
        "        owner = next(\n"
        "            (n for (_t, b), n in by_owner.items() if b == child.owner_backend_node_id),\n"
        "            None,\n"
        "        )\n",
    ),
    (
        "truncate the loader id instead of hashing an identity",
        "src/browser_tools/native_snapshot.py",
        '    digest = hashlib.sha256(f"{frame_id}\\x00{loader_id}".encode()).hexdigest()\n'
        "    return digest[:DOC_TOKEN_CHARS].upper()\n",
        "    return str(loader_id)[:DOC_TOKEN_CHARS].upper()\n",
    ),
    (
        "leave the frame id out of the token",
        "src/browser_tools/native_snapshot.py",
        '    digest = hashlib.sha256(f"{frame_id}\\x00{loader_id}".encode()).hexdigest()\n',
        '    digest = hashlib.sha256(f"{loader_id}".encode()).hexdigest()\n',
    ),
    (
        "check a uid against the main frame only",
        "src/browser_tools/native_snapshot.py",
        "    def walk(node: dict[str, Any]) -> None:\n"
        "        frame = node.get(\"frame\", {})\n"
        "        if frame:\n"
        "            tokens.add(doc_token_from_frame(frame))\n"
        "        for child in node.get(\"childFrames\", []) or []:\n"
        "            walk(child)\n",
        "    def walk(node: dict[str, Any]) -> None:\n"
        "        frame = node.get(\"frame\", {})\n"
        "        if frame:\n"
        "            tokens.add(doc_token_from_frame(frame))\n",
    ),
    (
        "read every frame's tree on the page session",
        "src/browser_tools/cdp_handler.py",
        "                child_send = self.client_for_session(document.session_id).send\n",
        "                child_send = top_send\n",
    ),
    (
        "ask the child session for its own owner node",
        "src/browser_tools/cdp_handler.py",
        "                owner_send = self.client_for_session(document.parent_session_id).send\n",
        "                owner_send = self.client_for_session(document.session_id).send\n",
    ),
    (
        "send an interaction to the page session whatever the uid says",
        "src/browser_tools/cdp_handler.py",
        "            send = self._send_for_uid(uid, send)\n",
        "",
    ),
    (
        "blame navigation for a uid the missing flag hid",
        "src/browser_tools/cdp_handler.py",
        "                return make_error(await self._uid_failure(uid, exc))\n",
        "                return make_error(str(exc))\n",
    ),
    (
        "name the flag even when the flag is already on",
        "src/browser_tools/cdp_handler.py",
        "        if self._rt.frame_sessions is not None or \"previous document\" not in message:\n",
        "        if False:\n",
    ),
    (
        "take the cross-session path on a page with no out-of-process frame",
        "src/browser_tools/cdp_handler.py",
        "        if not any(document.session_id for document in documents):\n"
        "            return []\n",
        "",
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
