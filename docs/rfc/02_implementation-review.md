# RFC-02 implementation review

Baseline: `1775e17fc3a30494d311210a6ac912bb7e56a4a8` plus working-tree changes.

Four independent reviews by `opus-5@claude`. Reviewers read live files while remaining gate tests were being completed; individual reports state their limits.



## Validation

- Python 3.13 surviving suite: **645 passed, no skips**. Includes real Capture
  completion by SIGINT, SIGTERM, duration and frame limit, and the native/core
  versus pinned Node parity gate.
- `ruff check`, `ruff format --check`, `pyright src/`, and `git diff --check` pass.
- Clean Python 3.13 wheel install runs launch/evaluate/snapshot/tool/stop, imports
  the optional driver without Camoufox, and reports missing-extra exit 3. The wheel
  includes `py.typed`, excludes removed modules/scripts, and requires only websockets.
- Screenshot coverage is 95%. The RFC's advisory 75% targets remain unmet for
  `core/attach` (30%), `core/registry` (48%), and `process_utils` (63%). Whole-module
  coverage includes untouched vendored attach/registry operations and platform-specific
  process branches; these remain coverage work beyond the exercised RFC seams.
- `pyright tests/` reports 953 errors, chiefly unannotated test fixtures and doubles.
  Its proposed CI gate is deferred as the RFC permits until the count reaches zero.
- The explicit Camoufox automation follow-up remains outside this RFC's implementation;
  the reserved driver and optional launch support remain available.

## Disposition

The implementation applies confirmed defects and scoped cleanup. The reports below
retain each reviewer's findings, including recommendations that conflict with the RFC.

| Axis / findings | Result |
| --- | --- |
| Reuse 1-5 | Shared runtime lifetime, target-flag registration and CLI validation, exact argv parsing, and private directory creation now have one owner. |
| Reuse 6 | Kept separate tool-name parsing: raw protocol and handler names have different grammars and preconnection validation. |
| Reuse 7 | Retained `cdp_constants`: RFC section 4 explicitly requires its direct screenshot imports. |
| Reuse 8 | Fixed frame/screencast target registration and stale comments. Kept small parser declarations, explicit test fixtures, and diagnostic-only process reader; broader API consolidation is outside this change. |
| Quality 1 | Removed duplicate CLI error prefix and obsolete transport descriptions. Retained the internal envelope shape to preserve surviving handler and optional-driver contracts. |
| Quality 2, 5, 6, 13, 15, 17 | Left larger interface/registry rewrites separate. Existing result keys and public signatures remain stable; instance-error conversion now reaches the shared mapper. |
| Quality 3, 7, 9, 11, 12, 18-25 | Consolidated target flags/checks and argv parsing, replaced function-name dispatch with explicit decorator policy, repaired error mapping and comments, reused the parser, removed redundant metadata defaults, released recorder state, documented the mandated second holder check, and formatted the tree. Independent test fixtures retain explicit setup. |
| Quality 4 | Kept the runtime seam required by existing handler/native consumers; removed no public runtime behavior merely for fewer aliases. |
| Quality 8, 10 | Retained diagnostic-only `read_process_command` and the screenshot constant imports required by the RFC; corrected stale docstrings. |
| Quality 14 | Kept `lifecycle.guide_text` as the RFC's manual owner, added the format option, and kept parser-verb coverage. |
| Quality 16 | Capture's non-overwrite policy remains scoped to Capture. Changing ordinary screenshot/PDF overwrite behavior would be a separate functionality decision. |
| Efficiency 1 | Retained both holder checks, recurring migration retries, and live-holder checks before collisions: all are explicit RFC requirements. Shared exact argv parsing avoids repeated source resolution inside argv loops. |
| Efficiency 2 | Retained conservative lifecycle locking through termination and registry mutation so migration cannot race a stopping profile. Acquisition remains bounded to ten seconds. |
| Efficiency 3 | Retained the specified resolve-target then runtime-construction seam and eager frame initialization. Lazy domain setup is a separate runtime design. |
| Efficiency 4 | Retained one-second page health checks. The vendored client exposes no public `connected` property, and a socket-only check misses target closure. |
| Efficiency 5-8 | Bounded Capture finalization; signals no longer stop its runtime mid-finalization; guarded loop teardown; awaited frame refresh; reused the CLI parser. |
| Efficiency 9-12 | Removed repeated source resolution; retained simple bounded lock polling and required mode enforcement. Navigation metadata is explicitly required even with no current navigation handler. Capture cases use isolated browsers to prevent state leaking across tests. |
| Fidelity 1-5 | Fixed unsafe-root deferral, missing CLI/argv/migration/capture regression gates, launch remedy propagation, startup timeout diagnostics, and stale docs. Supervisor process-group coverage was also completed during review. |
| Fidelity 6 | Interpret the removal gate as source/package-only, as the RFC's phase-1 gate says. A pinned Node baseline and test-only broker remain under `tests/parity`; the wheel and application source do not spawn Node. |

Additional validation found and fixed trailing-empty-argument loss on Linux,
startup-signal recorder cleanup, a zero-frame manifest, late transport errors
reported as success, and a stalled detach consuming socket-close grace. Pillow's
deprecated pixel iterator was replaced with its supported statistics API while
adding screenshot coverage. Pillow was added only to development dependencies.

## reuse

I read every cited file directly from disk (the `check_index_coverage` call was not permitted in this session, and the index predates these edits anyway), so all line numbers below are verified against the working tree at `1775e17` + uncommitted changes.

# Code-reuse review - browser-tools RFC-02 CLI ownership

## 1. `screencast_record` re-implements the runtime transport helper next to it - MEDIUM-HIGH

`src/browser_tools/curated.py:383-446` open-codes the whole CDP runtime lifecycle that `_cdp_handler_session` (`src/browser_tools/curated.py:80-98`) already owns: `target_slot` → `asyncio.run(resolve_page_target(...))` → `CDPRuntime(port, target_id)` → daemon thread → `wait_ready(HANDLER_CONNECT_TIMEOUT_SECONDS)` → `CDPHandler(runtime)` → `runtime.stop()` + `thread.join(timeout=6)`. Every line is a copy; only the signal handling and the `stopped.is_set()` gates are new.

The copy has already drifted. `_cdp_handler_session:92-94` converts the connect timeout into a `LifecycleError`; the copy at line 394 calls `runtime.wait_ready` bare. `cli_cdp_errors` (`src/browser_tools/one_shot.py:190-200`) catches `AmbiguousTargetError`, `TargetNotFoundError`, `NoPageError`, `InstanceNotFoundError`, `CDPError`, `ConnectionError` - not `TimeoutError` - and `cli.main:730-737` catches only `LifecycleError` and `PassthroughUsageError`. So `bt screencast record NAME --dir out` against an instance whose port accepts TCP but never completes the CDP handshake within 10s prints a Python traceback, while every other curated verb prints `error: CDP connection timed out`.

Alternative: install the two signal handlers around a `with _cdp_handler_session(port, target, url) as handler:` block, take `runtime = handler.runtime`, and keep only the recording loop, health check, and finalization in the body. The one thing lost is the cancellation check between target resolution and runtime construction, which the existing check after the `with` entry already covers.

## 2. `_profile_holder_reason` re-derives `--user-data-dir` argv scanning - MEDIUM

`src/browser_tools/lifecycle.py:176-187` walks argv looking for `--user-data-dir=VALUE` and the split `--user-data-dir VALUE` form, then compares `Path(value).resolve()` to the source. `src/browser_tools/process_utils.py:130-141` (`find_chrome_user_data_dir`) is the same loop, added in the same diff, and `process_utils.py:144-163` (`pid_holds_user_data_dir`) is exactly the comparison - and `lifecycle.py:72` already imports `pid_holds_user_data_dir`.

Beyond the twelve duplicated lines, the copy drops `.expanduser()`, so a Chrome started with `--user-data-dir=~/profiles/login` reads as "no holder" through `lifecycle` and as a holder through `process_utils`, and the migration then moves a live profile.

Alternative: replace lines 176-187 with `if pid_holds_user_data_dir(pid, source): return f"live holder PID {pid}"`. If the second `read_process_args` call matters, extract `user_data_dir_from_args(args: list[str]) -> Path | None` into `process_utils` and have `find_chrome_user_data_dir` call it too - one owner, two call sites.

## 3. The `--target`/`--url` exclusivity check is written six times - MEDIUM

`src/browser_tools/cli.py:421-422` (attach), `434-435` (wait), `449-450` (console-list), `462-463` (network-list), `494-495` (`_run_curated`), plus `src/browser_tools/curated.py:503-504` inside `screenshot` - all the same `if target is not None and url is not None: raise ... "cannot specify both --target and --url"`. `curated.screenshot` is the only curated function carrying its own copy, so the rule is enforced twice on that one path and once everywhere else.

Alternative: one `_require_single_target(args) -> tuple[str | None, str | None]` in `cli.py` called at the top of `_run`, returning the pair the branches then pass down; delete the copy in `curated.screenshot`. (An argparse `add_mutually_exclusive_group()` inside `_add_target_flags` also works and gets it for free on future verbs, but it changes the message text and routes through argparse's own exit rather than `PassthroughUsageError`.)

## 4. `_add_target_flags` exists but five parsers still hand-add the pair - MEDIUM

`src/browser_tools/cli.py:187-200` was added this diff to attach `--target`/`--url` to curated leaves. The same two `add_argument` calls are still hand-written at `cli.py:116-119` (attach), `134-135` (wait), `143-148` (console-list), `163-168` (network-list), and `304-307` (screenshot).

The screenshot copy is pure dead weight: `screenshot` is curated, so `_add_target_flags` runs on it, and the guard at line 198 makes the helper skip a parser that already has `--target`. The observable result is inconsistent help text for the same flag - "Select the page target (index or id)" on five verbs, "Select a page by index or id" on the rest.

Alternative: delete `cli.py:304-307`, and call `_add_target_flags(verb)` for `attach`, `wait`, `console-list`, and `network-list` in the same loop at `cli.py:179-182` (they are already all in `sub.choices`; the loop just needs to stop excluding them, or a second explicit call per verb).

## 5. Private-directory creation is duplicated verbatim across two modules - MEDIUM

`src/browser_tools/screencast.py:190-196` and `src/browser_tools/lifecycle.py:200-206` are the same six lines - walk parents while neither `exists()` nor `is_symlink()`, then `mkdir(mode=0o700, exist_ok=True)` in reverse - with only the loop variable renamed (`parent` vs `current`). Both were added in this diff. A future fix to the symlink-race handling has to land in both, and `screencast` has no equivalent of `_check_profile_directory` (`lifecycle.py:193-196`), so the two already disagree about verifying the final directory's ownership and group/other write bits.

Alternative: one `make_private_dirs(path: Path) -> None` (natural home: `process_utils`, which is already the shared low-level module both import from) and let `lifecycle._private_directory` be that call plus `_check_profile_directory`.

## 6. `curated.tool` re-derives the instance-name disambiguation rule - LOW-MEDIUM

`src/browser_tools/curated.py:538-546` decides whether the first positional is an instance with `any(item.name == remaining[0] for item in lifecycle.read_instances(registry_path))`. That rule - "a bare leading token is an instance name if the registry knows it" - already has two expressions: `passthrough.is_passthrough_head` (`src/browser_tools/passthrough.py:62-65`) and `passthrough.resolve_passthrough_args` (`passthrough.py:87-94`), both of which build `known = {inst.name for inst in lifecycle.read_instances(...)}`. A third spelling means a linear scan instead of a set, and the "instance named but nothing after it" error that `resolve_passthrough_args:91-92` raises has no counterpart here (`bt tool my-instance` with a registered `my-instance` falls through to "Unknown handler tool 'my-instance'").

Alternative: add `lifecycle.instance_names(registry_path) -> frozenset[str]` and have all three call it, or reuse `resolve_passthrough_args`' head-splitting shape for the `[INSTANCE] NAME [JSON]` line.

## 7. `cdp_constants` is now an alias module nobody reads through - LOW

`src/browser_tools/cdp_constants.py:3-8` re-exports five constants from `screenshot_utils`. Grepping the tree, no module imports any of those five from `cdp_constants`: `cdp_handler.py:31` and `curated.py:40-44` both go straight to `screenshot_utils`. The module's only live export is `REQUEST_TIMEOUT_SECONDS` (`cdp_handler.py:19`), and a stale doc reference to the module survives at `cdp_handler.py:431`.

Alternative: move `REQUEST_TIMEOUT_SECONDS` to `core/protocol.py` or `cdp_handler` itself and delete `cdp_constants.py`.

## 8. Smaller reuse items - LOW

- `src/browser_tools/one_shot.py:196` and `:200` both do `str(exc).replace("chrome-agent launch", "bt launch")`; the `CDPError` branch at `:198` omits it, which is the drift the duplication invites. One `_cli_message(exc) -> str` covers all three.
- `_run_curated` computes `target`/`url` at `src/browser_tools/cli.py:492-493`, then `_run_frames` and `_run_screencast` re-derive them with `getattr(args, "target", None)` six more times (`cli.py:619-620, 630-631, 640-641, 662-663, 673-674`). Pass the pair down instead.
- `instance` positional with `nargs="?", metavar="INSTANCE", help="Instance (omit if only one)"` is repeated 16 times in `cli.py` (lines 122-329). Given `_add_target_flags` now exists for the sibling slot, an `_add_instance_arg(parser)` is the matching helper.
- `src/browser_tools/process_utils.py:65-85` (`read_process_command`) has no caller left in `src` or `tests` after `read_process_args` landed - including the `-ww` fix this diff applied to it. Delete it, or route `find_chrome_user_data_dir`'s fallback through it.
- Tests: the fake-holder subprocess idiom `subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", f"--user-data-dir={dir}"])` is written twice, at `tests/test_process_args.py:11-13` and `tests/test_profile_migration.py:93-95`; and the `AsyncMock(return_value=SimpleNamespace(name=..., port=9222, pid=123, ...))` launcher double is copied verbatim at `tests/test_profile_migration.py:55-65` and `:82-91`. Both belong in `tests/conftest.py` as fixtures. `tests/test_tool_verb.py:9,24` and `tests/test_guide.py` also assert the literal `2` where `cli.EXIT_USAGE` exists and is used by `tests/test_daemonless_cli.py:111`.

## Not findings

`target_slot` (`one_shot.py:52-64`) consolidating the three prior copies in `events`, `passthrough`, and `list_verbs`; `_js_string` replacing `{selector!r}`; `_resolve_page_target` extracted out of `one_shot_page_session`; `TOOLS` driving `CDPHandler._handlers` instead of the parallel `_CDP_HANDLERS` table - these all move the diff the right way.

## quality

I've read the diff scope end to end. Here is the quality-and-cohesion report.

---

# browser-tools RFC-02 review - quality and cohesion

Base: HEAD `1775e17` + working tree + listed untracked tests. Standards loaded: `codebase-design`, `standards-python`. Graph index predates these edits, so every line cited below was read directly from the working tree.

## High

**1. `mcp_response` still frames JSON-RPC for a transport the diff deleted; the user sees `error: Error: ...`**
`src/browser_tools/mcp_response.py:9-14, 64, 74-78`

The module docstring names two carriers: the daemon socket (`DaemonClient` ↔ `mcp_daemon`) and the wrapper return (`browser_tools_session` → tool-proxy). The diff deletes `mcp_daemon.py`, `daemon_client.py`, `mcp_broker.py`, `mcp_session.py`. Nothing remains that reads a socket frame. Yet `make_text`/`make_error` still emit `{"jsonrpc": "2.0", "result": {...}, "id": 0}` at ~80 call sites in `cdp_handler.py` and `screencast.py`, and the placeholder `id` that "the daemon overwrites" is never overwritten by anything.

The concrete consequence is a visible defect. `make_error` prefixes `"Error: "` (`mcp_response.py:76`). `curated._envelope_text` returns that text verbatim (`curated.py:108-112`), `_tool_or_raise` raises `LifecycleError(text)` (`curated.py:117-119`), and `cli.main` prints `f"error: {exc}"` (`cli.py:733`). `bt tool get_text '{}'` therefore prints `error: Error: selector is required`. No test asserts on the composed message - `grep "Error: "` over `tests/test_curated_verbs.py`, `test_tool_verb.py`, `test_cli_lifecycle.py` returns nothing.

Also dead after the deletions: `text_response`, `error_response`, `append_text` have no callers in `src/` or `tests/`.

**2. Instance → port resolution is re-derived in four modules**
`curated.py:59-72`, `events.py:236-239`, `list_verbs.py:135-137`, `passthrough.py:178-181`

`curated._resolve_port` is the named owner of "omitted instance resolves via `resolve_single_instance`, then `core_registry.lookup` for the port". The other three re-implement the same two lines inline, and they diverge: `curated` wraps `InstanceNotFoundError` into `LifecycleError` itself, while `events`/`list_verbs`/`passthrough` rely on `@cli_cdp_errors` to catch it. Two mechanisms for one rule, so a change to instance resolution has four edit sites.

**3. The `--target`/`--url` mutual-exclusion rule is enforced in six places**
`cli.py:421-422, 434-435, 449-450, 462-463, 494-495`, `passthrough.py:149-150`, `curated.py:503-504`

Six hand-written copies of `if target is not None and url is not None: raise ...`, five with the same message string. argparse has `add_mutually_exclusive_group` for exactly this, and `_add_target_flags` (`cli.py:187-200`) is already the single place the pair is registered. `curated.screenshot` is the only curated verb that also re-checks it (`curated.py:503`) even though `_run_curated` checked it two frames up - an asymmetry with no stated reason.

**4. `CDPRuntime` exposes four names for one thing**
`cdp_handler.py:236-262`

```python
available  -> self.connected
connected  -> self._connected
client     -> self if self._cdp_client is not None else None
cdp_or_error() -> (self, None) | (None, make_error(...))
```

`client` returns the runtime itself, so `self.runtime.client.connected` (`cdp_handler.py:594, 662, 1353`) is `self.runtime.connected` by a longer route, and `client is not None` tests `_cdp_client`, not connectedness - two different predicates behind one property. Callers pick arbitrarily: `_handle_get_frame_storage` uses `runtime.client` + `.connected` (line 661-663), every other handler uses `cdp_or_error()`, `curated.screencast_record` uses `runtime.available` (line 380). `available` has one caller and is a pure alias.

## Medium

**5. Ten curated verbs repeat one four-line body and one four-parameter clump**
`curated.py:136-347`

`snapshot`, `wait_idle`, `wait_stable`, `frames_list`, `frames_select`, `frames_reset`, `storage_get`, `click`, `fill`, `detect` all take the same keyword-only `(instance, target, url, registry_path)` clump and all have the same body: resolve port → open `_cdp_handler_session` → one `_tool_or_raise` → wrap in a one-key dict. The wrapping key is inconsistent for no reason a caller can predict: `{"snapshot":…}`, `{"frames":…}`, `{"selected":…}`, `{"storage":…}`, `{"result":…}`. `_tool_or_raise` (115-120) and `_native_or_raise` (123-128) are byte-identical apart from `call_tool` vs `call_native`.

**6. Raw registry file I/O is spread across six call sites in `lifecycle.py`**
`lifecycle.py:350-357, 378-380, 456-464, 472-475, 841-846, 995-1004`

Six separate uses of `core_registry._resolve_path` / `_load_registry` / `_save_registry`, each carrying its own `# pyright: ignore[reportPrivateUsage]`. `registry_is_parseable` bypasses `_load_registry` entirely and opens the file with `json.load` itself (456-464), so "how the registry is read" has two implementations. One private wrapper (`_read_raw` / `_write_raw`) would make the vendoring-adaptation seam a single place instead of a pattern repeated six times.

**7. `_locked_profile_operation` dispatches on the wrapped function's name**
`lifecycle.py:243-254`

```python
if fn.__name__ == "launch":
```

A generic decorator that special-cases one function by string comparison. Renaming `launch`, or wrapping it in anything that changes `__name__`, silently converts "skip the lock when no profile is named" into "always take the lock". The policy ("only profile-bound launches need the lifecycle lock") belongs in `launch`, or the decorator should take the predicate as a parameter.

**8. Dead code and dangling references left in `process_utils.py`**
`process_utils.py:65-85, 185`

`read_process_command` has no caller in `src/` or `tests/`. `terminate_process_and_wait`'s docstring says "Unlike :func:`terminate_process` (fire-and-forget SIGTERM)…" - `terminate_process` was removed by this diff (`grep -rn "terminate_process\b"` matches only that docstring).

**9. `--user-data-dir` argv parsing exists twice, in two shapes**
`process_utils.py:125-141` vs `lifecycle.py:176-187`

`find_chrome_user_data_dir` walks argv for `--user-data-dir=X` and `--user-data-dir X` and returns an expanded, resolved `Path`. `_profile_holder_reason` inlines the same walk with a nested conditional expression and compares `Path(value).resolve()` without `expanduser()`. A `--user-data-dir ~/…` argv is matched by one and missed by the other.

**10. `cdp_constants.py` is a re-export shim nobody uses**
`cdp_constants.py:3-9`

Five `SCREENSHOT_*` names re-exported from `screenshot_utils`. `grep -rn cdp_constants src tests` shows one importer: `cdp_handler.py:19`, importing only `REQUEST_TIMEOUT_SECONDS`. `curated.py:40-44` and `cdp_handler.py:31` import the screenshot constants from `screenshot_utils` directly. Deletion test: removing the re-export block costs nothing.

**11. `_dispatch_tool`'s docstring documents symbols that do not exist**
`cdp_handler.py:565-579`

> The handler table (``_CDP_HANDLERS``) is the single binding of tool name to method; it is parity-checked against ``tool_registry.CDP_TOOLS`` at import…

Neither `_CDP_HANDLERS` nor `CDP_TOOLS` exists (`grep` finds them only in this docstring, in the RFC, and as a local alias in `tests/test_new_tools.py:752`). The real binding is `self._handlers` built from `TOOLS` in `__init__` (`cdp_handler.py:465-467`), and there is no import-time parity check - a registry entry naming a missing method now fails at construction with `AttributeError`, which is the opposite of what the docstring promises. `tests/test_new_tools.py:746-768` also still calls `TOOLS` a "frozenset" in comments; it is a dict.

**12. `events.run_attach` hand-rolls the mapping `cli_cdp_errors` already owns**
`events.py:110-148`

`run_attach` is the one verb in `events`/`list_verbs`/`curated`/`passthrough` that is not decorated with `@cli_cdp_errors`; instead it catches `InstanceNotFoundError`, `AmbiguousTargetError`, `TargetNotFoundError`, `NoPageError`, `ConnectionError` by hand - the same set, minus `CDPError`, and without the `"chrome-agent launch"` → `"bt launch"` rewrite the decorator applies (`one_shot.py:196, 200`). So an unreachable browser under `bt attach` prints the vendored program name; under every other verb it prints `bt launch`.

It also re-raises `UsageError("attach requires at least one +Domain.event subscription")` at line 126-127, which `resolve_attach_args` already raises at line 100-101.

**13. `curated.tool` re-implements the instance-vs-name disambiguation `passthrough` owns**
`curated.py:538-546`

```python
if (len(remaining) > 1 and remaining[0] not in TOOLS
    and (remaining[1] in TOOLS
         or any(item.name == remaining[0] for item in lifecycle.read_instances(registry_path)))):
```

`passthrough.is_passthrough_head` (`passthrough.py:54-65`) is the named owner of "is this leading token an instance name". This is a fourth spelling of the registry-name lookup, alongside `passthrough.py:62, 87, 119`. Each of those recomputes `{inst.name for inst in lifecycle.read_instances(...)}` from disk; a single passthrough invocation reads the registry file twice (`main` → `is_passthrough_head`, then `_run_passthrough` → `resolve_passthrough_args`).

**14. The 100-line CLI manual lives inside the lifecycle module and duplicates argparse**
`lifecycle.py:1015-1116`

`_GUIDE` restates every verb, flag, and default that `cli.build_parser` declares - a second source of truth in a module whose stated job (docstring, lines 1-39) is registry schema, liveness, and profile exclusivity. It has already drifted: the guide's `screenshot [INSTANCE] [--path FILE]` omits `--target`/`--url`, which `cli.py:304-307` defines; `screencast record` in the guide omits `--format`, which `cli.py:317-319` defines. `tests/test_guide.py` is 10 lines and does not gate this.

**15. Two layered exception wrappers for one CDP failure**
`cdp_handler.py:46-126`

`_safe_cdp_send` wraps `CDPError`/`ConnectionError` into `ToolInvocationError`; `_cdp_call` immediately unwraps it into `CdpToolError`. `_safe_cdp_send` is called directly from exactly one place (`_handle_get_frame_storage`, lines 677, 698, 719), which is also the only place that catches `ToolInvocationError`. Both `_safe_cdp_send` and `_cdp_call` also carry a redundant `if params is not None` branch (lines 96-98, 119-121) - `params` already defaults to `None` on the callee.

**16. Two artifact-writing policies inside one feature family**
`screencast.py:32-37` vs `cdp_handler.py:938-942, 1015-1019` and `curated.py:516-518`

`screencast._write_private` opens with `O_EXCL | O_NOFOLLOW`, mode `0o600`, relative to a held directory fd - and `tests/test_capture.py:30-32, 91` asserts both the no-overwrite and the `0o600` behaviour. `_handle_export_pdf`, `_handle_screenshot_element`, and `curated.screenshot` write the same class of artifact with `Path(...).write_bytes(...)`: follows symlinks, truncates an existing file, umask-default permissions. Three copies of `b64decode → resolve → mkdir(parents) → write_bytes`, none of them the hardened one.

**17. `_handle_get_frame_storage` is three copies of one block**
`cdp_handler.py:658-738`

Cookies, localStorage, and sessionStorage each get their own try/`except ToolInvocationError`/`except Exception` ladder with the label spelled inline three times. The localStorage and sessionStorage blocks (696-733) differ only in the identifier inside the JS string. A stray blank line sits inside the third `except` at line 732.

## Low

**18. Orphaned comments left where their constants were removed**
`cli.py:40-45` (a `#:` doc-comment about "Verbs argparse owns directly" now floating above `build_parser`, describing `_KNOWN_VERBS`, which is defined at line 748), `cli.py:481-482` (`#: Curated verbs dispatched through browser_tools.curated` above `_run_curated`, describing `_CURATED_COMMANDS`), `curated.py:50-52` (a `#:` comment for constants that are imported at line 26-32, pointing at private handler methods `cdp_handler._handle_wait_idle`).

**19. `cli._registered_commands()` builds a second parser at import time through argparse internals**
`cli.py:740-748`

Module import constructs a full `ArgumentParser`, walks `parser._actions`, and filters `argparse._SubParsersAction.choices` to derive `_KNOWN_VERBS`/`_CURATED_COMMANDS`. `main()` then calls `build_parser()` again, so every invocation builds the parser twice. The verb set and the curated-verb set are facts `_add_curated_verbs` already knows at declaration time (`cli.py:177-182` even computes the delta) and could return.

**20. `argparse` internals reached into for flag placement**
`cli.py:189-198` - `parser._actions`, `argparse._SubParsersAction`, `parser._option_string_actions`, each with a pyright suppression. Combined with the manual `--target`/`--url` on `screenshot` (`cli.py:304-307`), which `_add_target_flags` would have added anyway and which its `"--target" not in parser._option_string_actions` guard exists to tolerate.

**21. `tool_registry.py` restates a default 16 times**
`tool_registry.py:14-31` - `requires_capture=False` is written on every entry except the two screencast tools, and it is already the dataclass default (line 10). The two entries that matter are buried in the repetition.

**22. `ScreencastRecorder.stop` duplicates `_unsubscribe`, and never releases the client**
`screencast.py:99-102, 168-171` - `stop` sets `self._active = False` and calls `cdp.off(...)` inline, which is exactly `_unsubscribe(cdp)`. `self._cdp` (set at line 126) is never cleared on stop, so the recorder keeps the CDP client alive for the rest of the runtime's life.

**23. `_migrate_profiles` calls `_profile_holder_reason` twice with no explanation**
`lifecycle.py:278-289` - the holder check runs, then the destination-collision check, then the identical holder check again. If the second call is a deliberate TOCTOU re-narrowing, nothing says so; as written it reads as a copy-paste. `_profile_holder_reason` shells out to `ps` and inspects every user process, so the duplicate is not cheap.

**24. `test_capture.py` will not pass the newly-added `ruff format --check` CI step**
`tests/test_capture.py:9-11, 51, 53`

`.github/workflows/ci.yml` gains `uv run ruff format --check src/ tests/` in this diff, and `[tool.ruff.lint] select` includes `"I"`. `import struct` sits after `import sys` (lines 10-11), which isort orders the other way. Lines 51 and 53 are each ~104 characters against `line-length = 100`, and both are reflowable expressions rather than unsplittable literals. I could not run the linter in this session (command not approved), so this is from reading, not from a ruff run.

**25. `test_profile_migration.py` inconsistent `raising=False`**
`tests/test_profile_migration.py:53` uses `monkeypatch.setattr(lifecycle, "LEGACY_PROFILES_ROOT", legacy, raising=False)` while lines 80 and 117 patch the same existing attribute without it. `raising=False` on an attribute that exists disables the typo guard for no gain.

---

## What is fine

The large modules are cohesive, not sprawling: `cdp_handler.CDPRuntime` (thread + loop + connection + frame manager + capture) is one owner with a small interface, matching the `CDPRuntime` term in `CONTEXT.md`. `one_shot.py` is a genuine deep seam - four byte-identical copies collapsed to one context manager plus one decorator, and the module docstring records the no-page-error decision rather than leaving it implicit. `list_verbs.collect_on_session` and `events.wait_on_session` correctly share the SUBSCRIBE-FIRST discipline without sharing an artificial base class. The `ElementEvalResult` / `eval_on_element` extraction (`cdp_handler.py:129-212`) removes three incompatible not-found conventions and is the cleanest single change in the diff. `interstitial.detect_with_retry` dropping the `non_retryable` double-count is a real fix.

## Suggested order

Findings 1 and 4 first - 1 is user-visible today and 4 is what makes the handler code ambiguous to read. Then 2, 3, 5, 6 as one pass: they are the same shape (a rule with several owners) and collapsing them shrinks `curated.py` and `cli.py` materially. 8, 10, 11, 18, 21 are mechanical deletions that can ride along. 14 needs a decision (generate the guide from the parser, or move `_GUIDE` to its own module and gate the drift) rather than a mechanical fix.

## efficiency

# browser-tools RFC-02 review: efficiency

Scope: working tree against `1775e17` plus the untracked implementation tests. The pinned `/tmp/browser-tools-review.diff` was outside this session's read sandbox, so I reconstructed the same diff with `git diff 1775e17fc3a30494d311210a6ac912bb7e56a4a8` over tracked files and read the untracked tests directly. Findings are ordered most severe first.

---

## 1. HIGH - profile migration sweeps every user process twice per legacy profile, on every named launch, indefinitely

`src/browser_tools/lifecycle.py:278` and `src/browser_tools/lifecycle.py:287`

`_profile_holder_reason` (`lifecycle.py:140-190`) runs `ps -U <uid>` and then calls `read_process_args(pid)` for every non-zombie PID the user owns. On macOS `read_process_args` (`process_utils.py:88-120`) builds a fresh `ctypes.CDLL(None, use_errno=True)` per PID (`process_utils.py:98`), issues two `sysctl(KERN_PROCARGS2)` calls, and allocates the kernel-reported buffer, capped at 8 MB per process.

`_migrate_profiles` calls that function twice per source directory: `lifecycle.py:278` and again at `lifecycle.py:287`. Between them sits only the destination collision check at `lifecycle.py:281`, which is pure `stat` work under the same held lock. Nothing between the two calls can change holder state, so the first sweep buys no additional safety over the second. RFC-02 (line 553) requires the recheck immediately before the move; it does not require the earlier one.

Two compounding problems:

- The collision check runs *after* the first sweep. A leftover directory whose destination already exists pays the most expensive check in the function before being skipped, on every launch, forever.
- `launch` calls `_migrate_profiles` whenever `--profile` is given (`lifecycle.py:602`), and the only early exit is `LEGACY_PROFILES_ROOT.exists()` (`lifecycle.py:258`). One permanently deferred entry (a collision, a live holder, an unsafe mode, a symlink) keeps `/tmp/browser-tools-profiles` alive, so every future profiled launch repeats the whole sweep. This is not a one-time migration cost.

Cost: with N legacy directories and P user processes, 2N sweeps of P processes per launch. On a desktop running Chrome, P is in the hundreds and Chrome's renderer argv lines are long.

The new test fixture corroborates the size of this: `tests/test_profile_migration.py:15-27` exists specifically to "Bound the OS inventory to this test's real processes, not desktop apps."

Concrete fixes: move the collision check ahead of the first holder check; build the set of held `--user-data-dir` values with one sweep for all sources instead of one sweep per source; keep only the RFC-mandated pre-rename recheck.

## 2. HIGH - `stop` and `cleanup` take the global profile lock unconditionally, and can hold it longer than the lock's own acquisition timeout

`src/browser_tools/lifecycle.py:243-254`, `:932`, `:974`

`_locked_profile_operation` exempts only `launch` without a profile. Every `stop` and every `cleanup` takes the exclusive lock, whether or not a profile is involved. RFC-02 line 538 scopes it narrower: "Named stop and cleanup operations MUST use the same lock **when changing profile state**."

The hold can outlast the wait. `PROFILE_LOCK_TIMEOUT` is `10.0` (`lifecycle.py:210`). On the managed path, `stop` reaches `_terminate_verified` (`lifecycle.py:864`) and `terminate_process_and_wait(pid, timeout=5.0)` (`process_utils.py:207-221`), which polls up to 5 s after SIGTERM and up to another 5 s after SIGKILL, on top of `_try_cdp_browser_close` (2 s HTTP plus a `Browser.close` round trip). That is over 10 s under the lock. The profile-less path goes to `core/registry.py:407`, which adds its own 5 s wait loop (`core/registry.py:526-532`) plus the CDP close.

Failure scenario: `bt stop` on an unrelated ephemeral instance whose Chrome ignores SIGTERM. A concurrent `bt launch --profile work` exits 1 with "Timed out waiting for profile lifecycle lock", despite nothing touching that profile.

Fix: gate the lock on whether the operation actually changes profile state (`ext.profile is not None`, or a cleanup pass that touches profile dirs), and take the lock around registry mutation rather than around process termination.

## 3. MEDIUM - every curated verb opens two browser connections and enables two CDP domains it usually does not need

`src/browser_tools/curated.py:85-89`, `src/browser_tools/cdp_handler.py:307`, `:328-330`

`_cdp_handler_session` resolves the target first: `asyncio.run(resolve_page_target(port, spec, by))` (`one_shot.py:91-94`) does `GET /json/version`, a WebSocket connect, `Target.getTargets`, and a close. It then constructs `CDPRuntime`, whose `_main` does a *second* `GET /json/version` and a second WebSocket connect to the same browser endpoint (`cdp_handler.py:307`) before attaching.

`_main` then always sends `Page.enable`, `Runtime.enable`, and `Page.getFrameTree` (`cdp_handler.py:328-330`), regardless of the verb. `wait-idle`, `wait-stable`, `bt tool get_text`, `ax_find`, and `export_pdf` never read frame state. `Runtime.enable` also makes the browser start reporting execution contexts and console output for the session's lifetime.

So `bt tool get_text` costs two HTTP requests, two WebSocket handshakes, and seven protocol messages around one useful call. Compare `one_shot_page_session` (`one_shot.py:117-121`), which the passthrough, `wait`, `screenshot`, and list verbs already use: one HTTP request, one socket, `Target.getTargets` plus `attachToTarget`.

The HTTP cost doubles again in practice because `get_ws_url` targets `http://localhost:{port}` (`core/cdp_client.py:206`) while Chrome binds `127.0.0.1`, so each call may pay a failed `::1` attempt first. That resolver behaviour is vendored and unchanged, but the new code doubles how many times it is paid.

Fix: resolve the page on the runtime's own connection, and make the frame-domain setup conditional on the tools that consume it.

## 4. MEDIUM - capture health check issues a CDP round trip per second against the page being recorded

`src/browser_tools/curated.py:406-424`

The record loop sends `Page.getFrameTree` once per second (`curated.py:419`) for the entire recording, purely to notice a dead transport. The information is already available locally: `CDPClient._recv_loop` sets `_connected = False` on socket close and fails all pending futures (`core/cdp_client.py:163-173`), and the runtime thread exits, so `thread.is_alive()` and the client's `connected` flag both answer the question with no I/O.

On a frame-heavy page, `Page.getFrameTree` serializes the whole tree on the browser main thread. That is the same thread whose latency shows up as dropped or late frames in the screencast being captured.

Fix: watch `runtime._cdp_client.connected` (or expose it) instead of probing, and drop the probe entirely.

## 5. MEDIUM - capture finalization waits on a future with no timeout, and can hang forever

`src/browser_tools/curated.py:431-434`

The finalization schedules `screencast.stop` onto `runtime.loop` and calls `future.result()` with no deadline. The health probe eleven lines above uses `future.result(timeout=5)` (`curated.py:421`), so the omission is inconsistent within the same function.

Failure scenario: the transport drops mid-recording, the loop breaks with `failure` set, and the operator presses Ctrl-C again. `request_stop` (`curated.py:378-381`) sees `not runtime.available` and calls `runtime.stop()`. The runtime loop shuts down between the `loop is None` check at `curated.py:428-430` and the `run_coroutine_threadsafe` at `:431`. The coroutine is never run, the future never resolves, and the CLI blocks forever with the buffered frames unwritten - the exact state the foreground-capture design exists to avoid.

Fix: bound the wait, and treat expiry as "frames not written" with a diagnostic naming the output directory.

## 6. LOW - `CDPRuntime.stop()` races the runtime's own loop teardown

`src/browser_tools/cdp_handler.py:354-365` against `:297-303`

`stop()` reads `self._loop`, checks `not loop.is_closed()`, then calls `loop.call_soon_threadsafe(request_stop)`. `run()`'s `finally` closes the loop and sets `self._loop = None` (`cdp_handler.py:302-303`). When the runtime exits on its own - a connect failure, for example - a `stop()` racing that teardown can call into a just-closed loop and raise `RuntimeError: Event loop is closed`.

That exception escapes from `_cdp_handler_session`'s `finally` (`curated.py:96-98`), replacing whatever error the caller was actually reporting and skipping the `thread.join`. Same exposure from the signal handler at `curated.py:378-381`.

Fix: suppress `RuntimeError` around the `call_soon_threadsafe`, or hold the loop reference under a lock shared with `run()`'s teardown.

## 7. LOW - `frames list` can schedule a refresh onto a loop that is about to close, and reports work that never happens

`src/browser_tools/cdp_handler.py:595`

When the frame manager is empty, the handler fires `asyncio.ensure_future(self._refresh_frame_tree())` and returns "No frames available. Refreshing frame tree...". Under the daemon that was coherent: a later call saw the refreshed tree. Under one-shot CLI ownership, `_cdp_handler_session` calls `runtime.stop()` as soon as the tool returns (`curated.py:96-98`), so the task is destroyed pending, and there is no second call to benefit. The user gets a message describing work that will not complete.

Fix: `await` the refresh inline and return the result, or drop the branch - `_main` already populates the tree at `cdp_handler.py:330`.

## 8. LOW - the argparse tree is built twice per run, and once needlessly on the passthrough path

`src/browser_tools/cli.py:748` and `:723`

`_registered_commands()` builds the entire parser at import time solely to derive `_KNOWN_VERBS` and `_CURATED_COMMANDS`. `main()` then builds it again at `cli.py:723`. Every invocation constructs the full subparser tree twice, including the `_add_target_flags` recursion over `frames` and `screencast` children.

On the raw-protocol path (`bt Page.navigate '{...}'`, handled at `cli.py:709-719`) the import-time parser is the only one built and its entire use is one set-membership test, then the process returns without parsing.

Fix: build once and memoize, or derive the two name sets from the verb tables without instantiating a parser.

## 9. LOW - invariant `resolve()` and `CDLL()` calls inside hot loops

`src/browser_tools/lifecycle.py:186`, `:147`, `src/browser_tools/process_utils.py:98`

- `lifecycle.py:186` evaluates `source.resolve()` once per argv element of every scanned process. `source` is fixed for the whole call. With a few hundred processes at tens of arguments each, that is tens of thousands of `realpath` syscalls per sweep, and finding 1 says the sweep runs `2N` times per launch.
- `lifecycle.py:147` recomputes `source.resolve()` once per registry entry for the same reason.
- `process_utils.py:98` constructs `ctypes.CDLL(None, use_errno=True)` on every `read_process_args` call, so once per PID scanned. Hoist it to a module-level handle.

Also in this area: `_profile_holder_reason` reads and parses the registry file twice per call - `registry_is_parseable` (`lifecycle.py:449-464`) and then `read_instances` (`lifecycle.py:376-380`).

## 10. LOW - `_profile_lock` busy-polls, and repeats a permission change it just validated

`src/browser_tools/lifecycle.py:229-237`, `:216-217`

The acquisition loop retries `flock(LOCK_EX | LOCK_NB)` with `time.sleep(0.05)`, up to 200 wakeups across the 10 s window, and adds up to 50 ms of latency to an uncontended-but-just-released lock. A blocking `LOCK_EX` under a `signal.setitimer` deadline, or a blocking acquire in a worker thread with a timed join, removes the poll.

Separately, `lifecycle.py:216-217` calls `_private_directory(directory)` - which ends in `_check_profile_directory`, verifying the mode - and then unconditionally `directory.chmod(0o700)`. The chmod repeats work the check just confirmed, on every locked operation.

## 11. LOW - `TOOLS` no longer marks any tool as navigation, so the post-navigation detection branch is unreachable

`src/browser_tools/tool_registry.py` (new `TOOLS` table), `src/browser_tools/curated.py:568-569`

The rewritten `HandlerTool` table declares `navigation: bool = False` and no entry sets it True. `curated.tool` still tests `if TOOLS[name].navigation:` and calls `run_post_navigation_detection()`. That call can never fire. Either the flag is vestigial and should be removed along with the branch, or a navigation-capable tool is missing from the table and interstitial detection silently stopped running after `bt tool` calls.

## 12. LOW - the capture integration test starts a real browser four times for four end conditions

`tests/test_capture.py:35-97`

`test_capture_cli_writes_frames_after_a_second_cli_drives_page` is parametrized over `sigint`, `sigterm`, `duration`, `frame-limit`. Each case runs a full `bt launch --headless`, a `screencast record` subprocess, a `Runtime.evaluate` passthrough invocation, and a `bt stop`, with a 40 s subprocess timeout and a 15 s `communicate` timeout. The four cases differ only in how recording ends. A module-scoped launched instance would cut three full Chrome starts and stops out of every CI run.

---

**Not flagged, checked and clean:** screencast frame buffering is bounded and correctly stops acking at `max_frames` (`screencast.py:68-90`), which gives real CDP flow control rather than unbounded growth. `wait_on_session`'s unbounded queue (`events.py:177`) drains continuously in the same loop, so it does not accumulate. `CDPClient.off` removes by callback identity regardless of session (`core/cdp_client.py:110-118`), so `CDPRuntime.off`'s missing `session_id` is not a handler leak.

## fidelity

I reviewed the change against RFC-02 v3. Note first: **the working tree moved during the review** - `tests/test_capture_failures.py`, `tests/parity/node_broker.py` appeared and `tests/test_profile_migration.py` and `src/browser_tools/screencast.py` were rewritten between my first and second passes. I could not read `/tmp/browser-tools-review.diff` (sandbox blocks reads outside the repo), so I reviewed `git diff HEAD` plus the untracked files as they stood at ~11:05. Findings below are pinned to that state.

Overall the diff is a faithful implementation. Sections 1, 2 (except one path), 3, 4, 5, 7, 8, 9, 11 and the Migration Strategy are met as written; I verified each MUST individually. Six divergences follow, ranked.

---

## 1. Medium - an unsafe legacy or destination profile root aborts every named launch instead of deferring migration

`src/browser_tools/lifecycle.py:261-262`

```python
_check_profile_directory(LEGACY_PROFILES_ROOT)   # raises LifecycleError
_private_directory(destination_root)             # raises LifecycleError via _check_profile_directory
```

Both calls sit **outside** the per-candidate `try: … except OSError` at `lifecycle.py:273-296`, so a `LifecycleError` propagates out of `_migrate_profiles` into `launch` (`lifecycle.py:602`) and fails the whole command.

Migration Strategy says "A failed safety check MUST defer migration" and "An unrelated deferred profile MUST NOT block an otherwise safe requested launch." Deferral means report and continue; only the *requested* profile may fail the launch (E017).

Failure scenario: `/tmp/browser-tools-profiles` exists and is group- or other-writable, is a symlink, or is owned by another user - all plausible for a directory in world-writable `/tmp` that a previous version created under a permissive umask, and trivially forced by any other local user creating it first. `bt launch --profile work` then exits 1 with "unsafe profile directory" **permanently**, even when `~/.cache/browser-tools/profiles/work` already exists and nothing needs to be migrated. There is no override short of deleting the legacy path or setting `BROWSER_TOOLS_PROFILES_DIR`.

`tests/test_profile_migration.py:111-123` pins this behavior, but its scenario is the *requested* profile living under the unsafe root, where exit 1 is correct. The unrelated-profile case is what diverges.

## 2. Medium - several tests the RFC names as phase gates are absent

`tests/test_capture_failures.py` (added mid-review) closes almost all of the Capture-failure list, and the rewritten `tests/test_profile_migration.py` closes collision, uncertain inspection, symlink, cross-filesystem rename, second-check holder, env-override bypass, and reserved-container retention. What remains missing against the RFC's explicit gate lists:

**Phase 4 gate** ("error-remedy, process-group, Preferences preservation, and JS literal tests pass") - only Preferences preservation exists (`tests/test_core_launcher.py:71-85`). There is no test for:
- the `bt launch` error remedy (§10 first bullet) - no test asserts `chrome-agent launch` is rewritten;
- `spawn_supervisor(start_new_session=True)` (`src/browser_tools/core/supervisor.py:545`) - `grep start_new_session tests/` returns nothing;
- the `_js_string` JS-literal helper (`src/browser_tools/cdp_handler.py:41-43`, five call sites) - no test.

**Phase 2 gate** - "concurrent-launch" and "interrupted-migration" tests are absent. `tests/test_profile_migration.py:126-142` tests lock *timeout* (one holder blocks), not two cooperating launches serializing; nothing covers crash-after-rename recovery. The RFC's "Migration defers live **registered** holders" is also untested - only the unregistered-holder branch (`lifecycle.py:158-189`) is exercised; the registry branch at `lifecycle.py:144-150` has no test.

**§3 Testing Strategy** - "a recorded-vector unit test" for `read_process_args` is missing. `tests/test_process_args.py` has only the live-process test, so the darwin `KERN_PROCARGS2` byte-vector parser (`process_utils.py:98-120`) is never exercised against a fixed input, and the Linux branch is never exercised on macOS.

**§5/§6 Testing Strategy** - `tests/test_cli_targets.py:24-27` only asserts the parser *accepts* `--target`/`--url`; no test asserts a curated verb honors them or that the no-flag multi-page case produces `AmbiguousTargetError` (exit 1). `resolve_page_target` parity with `passthrough` is untested. For `tool`: unknown name and malformed arguments are covered (`tests/test_tool_verb.py`), but the error-envelope exit 1, arguments-parsed-and-forwarded, and relative-`path`-against-cwd cases are not.

Related: `tests/test_profile_migration.py:15-27`'s `process_inventory` fixture replaces the real `ps` inventory ("Bound the OS inventory to this test's real processes, not desktop apps"). Every migration test therefore runs the holder scan against 1-2 synthetic PIDs. The behavior of `_profile_holder_reason` against a real desktop's process list - where a single un-inspectable process yields "uncertain holder" and blocks the requested launch - is never exercised.

## 3. Low-Medium - the `InstanceNotFoundError` remedy rewrite is unreachable on most verbs

`src/browser_tools/one_shot.py:190-196` implements §10's MUST, but every caller catches `InstanceNotFoundError` first and wraps it in `LifecycleError`, which the decorator does not touch:

- `src/browser_tools/curated.py:69-71` (all curated verbs and `tool`)
- `src/browser_tools/events.py:143-144` (`attach`)
- `src/browser_tools/passthrough.py:233-234` (`help`)

Failure scenario: with an empty registry, `bt snapshot ghost` prints `error: Instance 'ghost' not found. No instances registered. Launch one with: chrome-agent launch` - the vendored program name the requirement exists to remove. Only the raw protocol line (`passthrough.send:181`, uncaught) gets the rewrite.

Same shape for `ConnectionError`: `events.run_attach` re-raises it verbatim at `events.py:147-148` rather than going through `cli_cdp_errors`, so `bt attach +Page.loadEventFired` against a dead port also reports `chrome-agent launch`.

## 4. Low - a Capture connect timeout escapes as an uncaught `TimeoutError`

`src/browser_tools/curated.py:394`

```python
runtime.wait_ready(HANDLER_CONNECT_TIMEOUT_SECONDS)
```

`CDPRuntime.wait_ready` raises `TimeoutError` (`cdp_handler.py:266-267`) when the browser accepts the socket but never completes attach. `cli_cdp_errors` does not catch `TimeoutError` and neither does `cli.main`, so the process dies with a traceback. `_cdp_handler_session:91-94` maps exactly this to `LifecycleError`; the Capture path does not.

§6 requires "A failed connection or start MUST clean up and exit 1", and E018 requires the diagnostic to report the output directory. Cleanup does happen (the `finally` at `curated.py:440-446` runs), and Python's default exit status for an uncaught exception is 1, so the exit code is accidentally right - but the user gets a stack trace instead of the specified diagnostic. `tests/test_capture_failures.py` covers `connect-failure` (an immediate `ConnectionError`, which *is* mapped), not the timeout.

## 5. Low - docstrings naming symbols and modules this RFC deleted

§10's last bullet requires these not to survive.

- `src/browser_tools/cdp_handler.py:568-569`: "The handler table (`_CDP_HANDLERS`) … parity-checked against `tool_registry.CDP_TOOLS` at import". Both names were removed by §7; the code now builds `self._handlers` from `TOOLS` at `cdp_handler.py:465-467`.
- `tests/test_daemonless_cli.py:1-46`: the whole file's premise is "prove the MCP front is optional". Ten of the twelve `FORBIDDEN_MODULES` entries name deleted modules, so `test_cli_import_pulls_in_no_daemon_stack` and `test_top_level_package_import_is_daemon_free` now assert something that cannot fail.
- `src/browser_tools/camoufox_runner.py:16-17`: "The in-process `CamoufoxSession` MCP tools are untouched."

## 6. Low - the parity suite still spawns `npx chrome-devtools-mcp@latest`

`tests/parity/parity_engines.py:482`, plus the newly added untracked `tests/parity/node_broker.py`.

§1 says "No module MAY spawn `npx`", and the abstract claims this RFC "completes [RFC-01's] Phase 4 'Node dependency deleted'". The phase-1 gate narrows the check to "no `npx` in source", which this satisfies, so the two statements disagree about the test tree. Flagging the ambiguity rather than asserting a violation: if the intent was source-only, the gate is met; if it was the whole repo, the Node dependency survives in the parity gate, which `README.md:156` and `CONTRIBUTING.md` tell contributors to run.

---

## Informational

- `tool_registry.TOOLS` has no entry with `navigation=True` (all eighteen default to `False`). This is correct - the only navigation-flagged tools in the pre-RFC table were `navigate_page` and `new_page`, both removed with the Node surface - but it makes `curated.py:568-569`'s post-navigation-detection branch unreachable, and the `navigation` field §7 mandates is currently inert.
- `lifecycle.guide_text` (`lifecycle.py:1089`) documents `screencast record [INSTANCE] --dir DIR [--duration SECONDS] [--max-frames COUNT]`, omitting the `[--format FORMAT]` the §6 command signature specifies and the parser accepts.
- `src/browser_tools/py.typed` exists but is untracked. §10's "MUST exist" and the phase-4 gate "the built wheel contains `py.typed`" hold in this working tree only; the file will not reach a clone until it is added.
- `tests/test_curated_verbs.py` dropped `test_screencast_start_passes_opts` and `test_screencast_stop_passes_dir` without replacements at that layer. The behavior is now covered end-to-end by `tests/test_capture_failures.py`, so this is coverage relocation rather than loss.