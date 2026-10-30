# Reflection input packet: recent 3, browser-tools registry-lock session

## Sessions
1. Active pi session 01a0c7f4 (cwd /Users/kevin/dev/browser-tools, this conversation):
   Chrome crash dialogs + wrong-instance launches; diagnosed orphaned live browser
   (registry {} while PID alive); re-registered it; added registry_lock() flock to
   lifecycle.py; proved crash loop external (abort@RegisterApplication from
   puppeteer/playwright + codex drivers); added GUIDE.txt placement note; comment-sicko
   review via subagent (9 findings, all addressed); fixed self-deadlock (re-entrant
   lock); opened PR #145; reran flaky py3.14 CI; squash-merged 0342f4c.
2. claude 987a6c16 (same cwd, 2026-09-21): exported to sessions/claude-987a6c16.jsonl
   (38MB). Precedes this work; assess for recurring patterns only.
3. muse 01a0c1a0 (same cwd, 2026-09-21, 1.9MB): export returned a limits/capabilities
   envelope, not transcript content. Coverage gap: unusable as evidence.

## Code and evidence sources (read as needed)
- /Users/kevin/dev/browser-tools/src/browser_tools/lifecycle.py (registry_lock, launch/stop/cleanup)
- /Users/kevin/dev/browser-tools/src/browser_tools/core/registry.py (verbatim vendored; _save_registry atomic via temp-rename)
- /Users/kevin/dev/browser-tools/src/browser_tools/GUIDE.txt (NEVER TAKE THE SCREEN)
- /Users/kevin/dev/browser-tools/src/browser_tools/core/supervisor.py
- /Users/kevin/dev/browser-tools/tests/test_cli_lifecycle.py
- /Users/kevin/dev/browser-tools/.scratch/chrome-orphan-crash/ledger.md and crash-verdict.md (local only)

## Skills actually used
- dev-mode (bug-fix playbook), diagnose-bugs, tdd, browser-tools, aerospace
- engineering principles: fix-root-causes, prove-it-works, test-behavior-not-implementation
- no-comments (comment-sicko review), choose-model, delegate (brief-gate, walk), technical-writing

## Catalog visible
/Users/kevin/.agents/skills/: dev-mode, diagnose-bugs, tdd, browser-tools, aerospace,
no-comments, choose-model, delegate, technical-writing, reflect, hcn, talk-normal, and others.

## Working-tree state
main at 0342f4c (merged #145). Only untracked .reflect/ and .scratch/ remain.
Validation: 40 lifecycle tests pass; 254 across lifecycle suites; pyright clean;
ruff clean; full suite 1499 passed + 1 pre-existing packaging failure on clean main.
CI py3.14 first run failed 295 async/event-loop errors unrelated to the sync-only diff;
rerun green (flake, recorded in conversation).

## Privacy
Normal. No secret material carried (a .zsh_history read was refused by the secret-file gate).
