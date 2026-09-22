# Findings: how parallel sessions in one directory each get their own bt instance

Ticket: [#153](https://github.com/dungle-scrubs/browser-tools/issues/153),
under map [#147](https://github.com/dungle-scrubs/browser-tools/issues/147).
Researched 2026-09-22 against `browser-tools` at commit `aca6037`, installed
version `browser-tools 0.8.0`.

Every claim below is marked **documented** (the owning source says it),
**observed** (run here, transcript in section 3), or **unverified**.

## 1. The reframing findings

**The ticket's premise is wrong. A second `bt launch` in the same directory
succeeds.** It does not fail and it names no holder. The registry appends a
`-NN` suffix, so the second instance is `<dir>-02`. Observed: `153-01` then
`153-02`, ports 9226 and 9227, each with its own user-data-dir, each driven
independently (section 3.1). Documented in the same terms by RFC-01: "name
derived from the working directory ..., `-NN` suffix on collision"
(`docs/rfc/01_merge-chrome-agent-core-into-browser-tools.rfc.md:223`, and
again at line 100).

The refusal the ticket quotes is real, but it is a **profile** refusal, not a
directory refusal. It lives inside `if profile is not None`
(`src/browser_tools/lifecycle.py:1005-1010`) and fires only when a live
instance holds the named profile. The wording that produced the
misreading is in the manual and the README: "rather than opening a second
browser on the same directory"
(`src/browser_tools/GUIDE.txt:591`, `README.md:176`). "Directory" there means
the profile's user-data-dir, not the working directory. Nothing in the manual,
`CONTEXT.md` or the README says a directory can hold more than one instance;
that silence is what left the question open.

**The sample does not show the need the ticket assumed either.** Of 20
distinct `CHROME_DEVTOOLS_AXI_SESSION` names sampled from real transcripts,
**0 are parallel workers in one repository** and **0 are isolation from the
user's own Chrome**. 17 are one caller-named browser per task, re-addressed on
every invocation. 3 attach on purpose to a browser the caller did not launch
(`CHROME_DEVTOOLS_AXI_AUTO_CONNECT=1` or `CHROME_DEVTOOLS_AXI_BROWSER_URL`),
where the session name separates only the bridge process. Counts and lines in
section 4.

So the need `CHROME_DEVTOOLS_AXI_SESSION` actually served is **continuity of a
caller-chosen name across many invocations**, not concurrency. bt's positional
`[INSTANCE]` already serves that shape. The one thing bt cannot do is let the
caller **choose** the name (section 5).

## 2. How the name is derived, and what the options are

### 2.1 Derivation (documented in code, observed live)

| Step | Where | What it does |
|---|---|---|
| Base name | `src/browser_tools/core/registry.py:192-208` (`_derive_base_name`) | `os.path.basename(working_dir)`, lowercased, spaces to hyphens, stripped to `[a-z0-9.-]`, repeated hyphens collapsed, leading and trailing `-` and `.` stripped; empty falls back to `chrome`. |
| Uniqueness | `src/browser_tools/core/registry.py:211-218` (`_derive_unique_name`) | Appends the lowest free `-NN` suffix: `base-01`, `base-02`, and upward. |
| Applied | `src/browser_tools/core/registry.py:267-268`, inside `register()` | Name assigned at registration, after the browser is already running. |
| Working dir | `src/browser_tools/lifecycle.py:1039` passes `working_dir=os.getcwd()`; `src/browser_tools/core/launcher.py:242-253` registers with it | The launching process's cwd, nothing else. |

Two consequences follow from the derivation, both observed:

- **The caller cannot choose or predict the name.** `launch` takes no name
  argument (`src/browser_tools/cli.py:145-178`), and the manual says so:
  "launch takes no positional argument: the name is assigned by the registry"
  (`src/browser_tools/GUIDE.txt:68-72`). The suffix depends on registry state
  at launch time, which includes every other agent's instances.
- **The name is recycled.** Stopping `153-01` and relaunching from the same
  directory produced `153-01` again, pointing at a different process and a
  different user-data-dir (section 3.5).

A third consequence: the base name is the directory **basename**, so two
different directories with the same basename share one `-NN` sequence.
Observed: `.../work/153` and `.../work/153/wt/153` produced `153-01` through
`153-04` in one sequence (section 3.4). A worktree per task, each worktree
named after the repository, collapses into one sequence and the name no longer
says which worktree it belongs to.

### 2.2 What a second `launch` in the same directory does

It succeeds, unless `--profile` names a profile a live instance holds. No
other exclusivity check runs at launch. An unprofiled Chrome launch gets a
throwaway user-data-dir from `tempfile.mkdtemp`
(`src/browser_tools/core/launcher.py:146-151`), so two of them never contend
for a directory. Ports are auto-allocated and skip both live registry entries
and any listening socket (`src/browser_tools/core/registry.py:221-242`).

### 2.3 Every option available today for two sessions in one directory

| Option | Yields two live instances? | Cost / caveat | Standing |
|---|---|---|---|
| Nothing. Launch twice. | Yes: `<dir>-01`, `<dir>-02` | The caller must read `name` out of the launch JSON. It cannot be agreed ahead of time. | observed (3.1) |
| A different `--profile` per worker | Yes | Also gives login persistence and exclusivity. Adds a durable profile directory per worker, which `cleanup` never reaps on age (`README.md:179-181`). | observed (3.2) |
| A subdirectory per worker | Yes: `worker-a-01` | The worker must `cd` there. The basename must be unique, or it rejoins the shared `-NN` sequence. | observed (3.4) |
| `--port PORT` at launch, then `--endpoint http://127.0.0.1:PORT` on every verb | Yes | The port is the caller-chosen handle. `--endpoint` never reads or writes the registry (`src/browser_tools/lifecycle.py:906-934`), so `stop` still needs the registry name. | observed (3.3, 3.4) |
| `BROWSER_TOOLS_REGISTRY=<path>` per worker | Yes, and `[INSTANCE]` becomes omittable again | Undocumented in the manual. Breaks profile exclusivity across registries: the holder check reads one registry (`src/browser_tools/lifecycle.py:467-482`), so it does not see the other's holder. Observed to fall through to Chrome's own process singleton and fail with a raw Chrome error naming no holder (3.6). The code comment calls this var "used by the CLI front and tests" (`src/browser_tools/lifecycle.py:82-85`). | observed (3.6) |
| An env var naming the default instance | Not available | No such variable exists. The only environment variables the package reads are `BROWSER_TOOLS_REGISTRY`, `BROWSER_TOOLS_PROFILES_DIR`, `XDG_DATA_HOME`, `BROWSER_TOOLS_LEGACY_PROFILES_DIR` and `BROWSER_TOOLS_ALLOW_REMOTE_ENDPOINT`. | documented (grep of `os.environ` over `src/browser_tools/**.py`) |

With more than one instance registered, every verb must name one:
`resolve_single_instance` raises rather than guess
(`src/browser_tools/lifecycle.py:888-904`), which RFC-01 makes normative
(`docs/rfc/01_...rfc.md:182`). Observed error, with seven instances live from
several agents at once:

```
error: Multiple instances are running; name one explicitly.
Available: 148-01, 151-01, 153-01, 153-02, 153-03, graybox-01, reviewsion-extensions-01
```

## 3. Live confirmation transcript

Run from `/Users/kevin/dev/browser-tools/.scratch/retire-axi/work/153` with
`browser-tools 0.8.0`. Every instance launched here was stopped afterwards
(section 3.7).

### 3.1 Two unprofiled launches in one directory

```
$ bt launch --headless
{"name": "153-01", "port": 9226, "pid": 11257, "profile": null,
 "user_data_dir": "/tmp/chrome-agent/session-aph_ychq"}          exit=0

$ bt launch --headless          # same directory, no flags
{"name": "153-02", "port": 9227, "pid": 12799, "profile": null,
 "user_data_dir": "/tmp/chrome-agent/session-8dfe7n66"}          exit=0
```

No refusal. Driven independently:

```
$ bt 153-01 Page.navigate '{"url": "file://.../one.html"}'   exit=0
$ bt 153-02 Page.navigate '{"url": "file://.../two.html"}'   exit=0
$ bt status 153-01   ->  port 9226, title "PAGE-ONE"
$ bt status 153-02   ->  port 9227, title "PAGE-TWO"
```

### 3.2 The profile refusal, which is the one the ticket quotes

```
$ bt launch --headless --profile bt-153-probe
{"name": "153-03", "port": 9230, "profile": "bt-153-probe",
 "user_data_dir": "/Users/kevin/.local/share/browser-tools/profiles/bt-153-probe"}   exit=0

$ bt launch --headless --profile bt-153-probe          # same directory, same profile
error: Profile 'bt-153-probe' is already held by live instance '153-03'. Stop it first, or launch a different profile.
                                                                                     exit=1
```

The same directory had already produced `153-01`, `153-02` and `153-03`
without complaint. Only the repeated profile is refused.

### 3.3 `--endpoint` against a browser bt launched

```
$ bt snapshot --endpoint http://127.0.0.1:9226   ->  RootWebArea "PAGE-ONE"   exit=0
$ bt snapshot --endpoint http://127.0.0.1:9227   ->  RootWebArea "PAGE-TWO"   exit=0
```

`--endpoint` is a verb flag, not a leading flag: `bt --endpoint URL snapshot`
is exit 2.

### 3.4 Subdirectory, explicit port, and basename collision

```
$ cd worker-a && bt launch --headless --port 9299
{"name": "worker-a-01", "port": 9299, ...}                       exit=0

$ cd wt/153 && bt launch --headless        # different directory, same basename "153"
{"name": "153-04", "port": 9225, ...}                            exit=0
```

`153-04` continues the sequence started in a different directory.

### 3.5 The name is recycled

```
$ bt stop 153-01          {"stopped": true, "message": "Stopped 153-01"}
$ bt launch --headless    {"name": "153-01", "port": 9226, "pid": 6826,
                           "user_data_dir": "/tmp/chrome-agent/session-afhww6pe"}
```

Same name, different process, different user-data-dir. A name held in a
worker's script does not stay bound to that worker's browser.

### 3.6 Per-worker registry, and where it breaks

```
$ BROWSER_TOOLS_REGISTRY=$PWD/regA/registry.json bt launch --headless
{"name": "153-01", "port": 9231, ...}                            exit=0

$ BROWSER_TOOLS_REGISTRY=$PWD/regA/registry.json bt snapshot     # no instance named
{"snapshot": "[uid=...] RootWebArea \"New Tab\" ..."}            exit=0
```

Port allocation stays safe across registries because it probes for a listener
as well as reading the registry: registry A picked 9231 while 9222, 9225,
9226, 9227 and 9230 were held by instances it could not see. Profile
exclusivity does not stay safe:

```
$ BROWSER_TOOLS_REGISTRY=$PWD/regB/registry.json bt launch --headless --profile bt-153-probe
error: Launch failed: Chrome exited immediately with code 21. stderr:
  ERROR:chrome/browser/process_singleton_posix.cc:347] Failed to create
  .../profiles/bt-153-probe/SingletonLock: File exists (17)
  ...Failed to create a ProcessSingleton for your profile directory...   exit=1
```

The bt-level check never fired, because `find_profile_holder` read registry B
and the holder was in the default registry. Chrome's own singleton stopped it.
The `launch` docstring names exactly this degradation: "Chrome's own singleton
may stop the second browser from becoming usable, but that is not the
specified behaviour and it names no holder"
(`src/browser_tools/lifecycle.py:1297-1298`).

### 3.7 Cleanup, and instances that were not mine

Stopped: `153-01`, `153-02`, `153-03`, `153-04`, `worker-a-01`, and the
registry-A instance. Deleted the throwaway profile `bt-153-probe`. `bt
cleanup` was never run. The user's `reviewsion-extensions-01` (profile
`shopify-admin`) was live before and after and was never touched.

Three instances that were not mine left the registry during the run:
`graybox-01` (profile `graybox-proof-2`, port 9225), `148-01` and `151-01`,
the last two belonging to sibling research agents working #148 and #151.
I ran no `stop` and no `cleanup` against them. `bt launch` calls
`cleanup_sessions()` unconditionally (`src/browser_tools/core/launcher.py:133`),
which removes registry entries whose browser fails the liveness check, so a
launch of mine may have removed their already-dead entries. It cannot stop a
live browser: `cleanup` removes only entries that fail liveness, and
`allocate_port` skips any port with a listener. My `153-04` was given port
9225, which means nothing was listening on 9225 at that moment, so the browser
`graybox-01` named was already gone. What ended those three browsers is
**unverified**.

## 4. What the transcripts actually did with `CHROME_DEVTOOLS_AXI_SESSION`

### 4.1 Method and privacy handling

Sources: `~/.claude/projects/**/*.jsonl` (12 files containing the string) and
`~/.codex/sessions/**` (21 files). No transcript was read whole. For each
occurrence of `CHROME_DEVTOOLS_AXI_SESSION=` a window of at most 200
characters around the match was extracted and classified from that alone.

- occurrences extracted: **3291**
- dropped unread as credential-looking: **3**
- distinct windows: 2053
- form: **2379** inline prefixes on the command itself
  (`CHROME_DEVTOOLS_AXI_SESSION=x chrome-devtools-axi <verb>`), **123**
  `export` statements, 747 neither (prose, help text, quoted documentation).

Two files were excluded from the classification as self-contamination: this
research session's own transcript and its subagent transcript, both under
`~/.claude/projects/-Users-kevin-dev-browser-tools/d8b0d62d-.../`.

`worker-1` (320 occurrences across 20 files) and `worker-2` (8) were also
excluded from the 20: they are documentation echoes, not uses. Both come from
`chrome-devtools-axi --help` ("e.g. CHROME_DEVTOOLS_AXI_SESSION=worker-1") and
from a skill or README quoted into transcripts. They appear in files from six
different repositories, including this one, wherever an agent printed the
help.

The 20 classified are the 20 highest-occurrence real names.

### 4.2 Classification

| Class | Count | Names |
|---|---|---|
| Parallel workers in one repository | **0** | none observed |
| Isolation from the user's own Chrome | **0** | none observed; 3 names do the opposite |
| A named long-lived session reused across turns | **17** | trevor-activity-host, graybox-proof, trevor-proof-fixed, graybox-ui, trevor-flow-explainer, recover1, trevor-proof-map, graybox-guided, trevor-child-narrow, trevor-research, graybox-tab-check, trevor-explainer-review, trevor-flow-live-verify, pi011, proto-review, pi011verify, pr159 |
| Other: a named bridge over a browser the caller did not launch | **3** | graybox-e2e, pi039, graybox-owner |

Representative windows, truncated as extracted.

**Named long-lived session reused across turns (17).**

```
CHROME_DEVTOOLS_AXI_SESSION=trevor-proof-fixed CHROME_DEVTOOLS_AXI_HEADED=1 chrome-devtools-axi open http://127.0.0.1:17471

CHROME_DEVTOOLS_AXI_HEADED=1 CHROME_DEVTOOLS_AXI_SESSION=graybox-ui chrome-devtools-axi open http://127.0.0.1:17471 && CHROME_DEVTOOLS_AXI_SESSION=graybox-ui chrome-devtools-axi resize 1440 900 && CHROME_DEVTOOLS_AXI_SESSION=graybox-ui chrome-devtoo

CHROME_DEVTOOLS_AXI_SESSION=pi011 chrome-devtools-axi open http://127.0.0.1:5173 && CHROME_DEVTOOLS_AXI_SESSION=pi011 chrome-devtools-axi resize 1440 1000 && CHROME_DEVTOOLS_AXI_SESSION=pi011 chrome-devtools-axi sn
```

**Other: a named bridge over a browser the caller did not launch (3).**

```
CHROME_DEVTOOLS_AXI_SESSION=graybox-e2e CHROME_DEVTOOLS_AXI_BROWSER_URL=http://127.0.0.1:9225 chrome-devtools-axi eval 'JSON.strin

CHROME_DEVTOOLS_AXI_AUTO_CONNECT=1 CHROME_DEVTOOLS_AXI_SESSION=pi039 chrome-devtools-axi start

CHROME_DEVTOOLS_AXI_SESSION=graybox-owner CHROME_DEVTOOLS_AXI_AUTO_CONNECT=1 chrome-devtools-axi pages
```

**Parallel workers in one repository: nothing observed, documentation only.**
The pattern exists in the documentation and nowhere in a real invocation:

```
e.g. CHROME_DEVTOOLS_AXI_SESSION=worker-1   [from chrome-devtools-axi --help]

SESSION` - one per agent session, worktree, or test worker:  ```sh CHROME_DEVTOOLS_AXI_SESSION=worker-1 chrome-devtools-axi open https://example.com CHROME_DEVTOOLS_AXI_SESSION=worker-2 chrome-dev

CHROME_DEVTOOLS_AXI_SESSION=worker-1 chrome-devtools-axi open ...  *   CHROME_DEVTOOLS_AXI_SESSION=worker-2 chrome-devtools-axi open ...  * A session only isolates the bridge itself; the connectio
```

**Isolation from the user's own Chrome: nothing observed, and three
counter-examples.** `graybox-owner`, `pi039` and `userchrome` set
`CHROME_DEVTOOLS_AXI_AUTO_CONNECT=1`, which attaches to the user's running
Chrome on purpose. axi's own help says the session name does not isolate the
browser: "Each session name gets its own bridge process, port ..., and on-disk
state ... **Connection mode and profile are unchanged**"
(`chrome-devtools-axi --help`, environment section). The documentation quoted
above says the same: "A session only isolates the bridge itself."

### 4.3 What the shape of the uses shows

- **One agent session, many named browsers, one directory.** One codex
  transcript for `/Users/kevin/dev/trevor` carries eight distinct session
  names (trevor-activity-host, trevor-child-narrow, trevor-explainer-review,
  trevor-flow-explainer, trevor-flow-live-verify, trevor-proof-fixed,
  trevor-proof-map, trevor-research). Another, for `/Users/kevin/dev/graybox`,
  carries seven. One name per sub-task, not one name per worker. At least one
  command opens two of them in a row, so two browsers were live at once from
  one directory, driven by one agent.
- **Names are shared across agents, not used to keep agents apart.**
  `graybox-proof` and `recover1` each appear in two transcripts: a codex
  session and a Claude Code subagent under `~/.claude/projects/-Users-kevin-dev/`.
  The second agent addressed the browser the first had named. That is handoff,
  the opposite of isolation.
- **The name is restated on every call.** 2379 inline prefixes against 123
  exports. The variable was used as an address, not as ambient configuration.

## 5. The gap

There is a gap, and it is narrower than the ticket supposed.

**bt already covers concurrency.** Two agents in one directory get two
instances with no flag, no subdirectory and no profile. This should go to the
manual: `GUIDE.txt` and `CONTEXT.md` are silent on the `-NN` suffix, and the
one sentence that touches the subject reads the other way (section 1).

**bt does not cover naming.** The caller cannot choose the instance name, and
the derived name is not a stable handle. Three concrete failures follow, all
observed:

1. **A name cannot be agreed before the browser exists.** A worker that is
   handed its handle up front, or that must publish it to another process,
   has nothing to publish until `launch` returns. With
   `CHROME_DEVTOOLS_AXI_SESSION` the caller picks the name first and the
   bridge is created on demand.
2. **A name is recycled.** Stop `foo-01` and the next launch from that
   directory takes `foo-01` (3.5). A second worker can be handed the first
   worker's name.
3. **The name does not identify the directory.** Same-basename directories
   share one sequence (3.4), so a worktree-per-task layout produces
   `repo-01 .. repo-04` with nothing saying which worktree each belongs to.

The counterweight, and the reason this may not need code: `bt launch` prints
`name` in its JSON, so a worker that captures its own launch output already
has a stable handle for its whole life. 17 of the 20 sampled uses are exactly
that shape, and they would work today.

### Options for the grilling ticket, not decisions

| Option | What it is | Cost to keep |
|---|---|---|
| **A. Documentation only** | Add to `GUIDE.txt`: a second launch in one directory succeeds and is named `<dir>-NN`; capture `name` from the launch JSON and use it for the rest of the task; a stopped name is recycled. Add the `-NN` rule to `CONTEXT.md`'s Instance noun. | One manual section. No code, no new surface. Leaves failures 1 and 2 unaddressed. |
| **B. `bt launch --name NAME`** | Caller-chosen instance name, validated against the existing instance-name character set, refused when a live instance already holds it, in the shape profile exclusivity already has (`lifecycle.py:1005-1010`). Derived name stays the default. | One flag, one uniqueness check inside the registry lock, one manual entry, one more way to name a thing. Closes 1, 2 and 3. |
| **C. `BROWSER_TOOLS_INSTANCE` env var** | Default instance for verbs that omit `[INSTANCE]`, matching axi's `export` form. | One env var, and a new precedence rule against the positional and against `--endpoint`. The sample argues against it: 2379 inline prefixes to 123 exports. Closes none of 1, 2 or 3 on its own. |
| **D. Document `BROWSER_TOOLS_REGISTRY` as the per-worker recipe** | Per-worker registry file, which also restores the omittable `[INSTANCE]`. | Zero code, but it would promote a test hook to a contract and it silently weakens profile exclusivity across registries (3.6). Not recommended without fixing that first. |

Option B is the one that matches what the sample shows people doing, and it
subsumes A's recipe. Option A alone is defensible if the fit check decides a
caller-chosen name widens the surface for a need 17 of 20 uses already meet by
capturing the launch JSON. That call belongs to the grilling ticket.

## 6. Left open

- **What ended `graybox-01`, `148-01` and `151-01`** during the run
  (section 3.7). Nothing I ran can stop a live browser, and the evidence says
  the one on port 9225 was already dead when its entry left the registry, but
  the cause itself is unverified.
- **Profile exclusivity across registries** (3.6) is a defect this research
  found while probing option D, not part of the ticket's question. It has no
  ticket. `find_profile_holder` reads one registry; the per-profile launch
  lock (`lifecycle.py:1285-1330`) is registry-independent but is held only for
  the duration of a launch, not for the life of the instance. Whether this
  matters depends on whether `BROWSER_TOOLS_REGISTRY` stays a test hook.
- **Camoufox was not tested.** Every live check used `--engine chrome`. The
  name derivation is engine-independent (`register` is called from both
  paths), but a second Camoufox launch in one directory was not run.
- **The sample is one machine's transcripts.** 0 parallel-worker uses is a
  statement about these 33 files, not about the pattern's usefulness. The
  pattern is recommended by axi's own help and by a skill document, so agents
  may have been about to adopt it.
- **Whether the two-instance recipe belongs in `bt guide` at all** is a fit
  check, not a research finding. The manual is the agent's only documentation,
  and every section added is upkeep.
