---

## Removal Checklist: firstmate Footprint on This Machine

### Reframing Finding

Firstmate's footprint is **surprisingly small** on this machine. No state directories were created under `$XDG_STATE_HOME/firstmate`, `.tasks-axi`, or any other expected location. The primary footprint consists of:
- Two Go binaries in `~/.local/bin/` (treehouse, no-mistakes)
- Three global npm packages installed via mise (`tasks-axi`, `quota-axi`, `chrome-devtools-axi`)
- No shell hooks, no tmux bindings, no launchd services, no config entries

The ticket clone itself (`/Users/kevin/dev/firstmate`) is the source of truth for this inventory and stays.

---

### Removal Checklist

| Path / Entry | What It Is | Evidence | Verdict | Standing |
|---|---|---|---|---|
| `~/.local/bin/treehouse` | Go binary (Mach-O executable) installed by firstmate via `curl ... treehouse/install.sh \| sh` | File exists; `fm-bootstrap.sh:872` documents install command as `curl -fsSL https://kunchenguid.github.io/treehouse/install.sh \| sh` | **Remove** | firstmate-only; no external usage found |
| `~/.local/bin/no-mistakes` | Go binary (Mach-O executable) installed by firstmate via `curl ... no-mistakes/install.sh \| sh` | File exists; `fm-bootstrap.sh:873` documents install command as `curl -fsSL https://raw.githubusercontent.com/kunchenguid/no-mistakes/main/docs/install.sh \| sh` | **Remove** | firstmate-only; no external usage found |
| `tasks-axi` (global npm) | CLI task queue backend, installed via `npm install -g tasks-axi` | Present in `/Users/kevin/.local/share/mise/installs/node/lts/lib/node_modules/tasks-axi/`; `fm-bootstrap.sh:874` documents install | **Remove** | No usage found outside firstmate in `.agents/skills/`, `.claude/skills/`, `dev/skills/`, or `dev/dotfiles/` |
| `quota-axi` (global npm) | Quota window reporter for LLM providers, installed via `npm install -g quota-axi` | Present in `/Users/kevin/.local/share/mise/installs/node/lts/lib/node_modules/quota-axi/`; `fm-bootstrap.sh:874` documents install | **Remove** | Notchbar config (`dotfiles/notchbar/.config/notchbar/config-pro.toml:124`) mentions it only in a comment explaining why `CLAUDE_CONFIG_DIR` must be set; does not invoke it at runtime |
| `chrome-devtools-axi` (global npm) | Browser automation CLI, installed via `npm install -g chrome-devtools-axi` | Present in `/Users/kevin/.local/share/mise/installs/node/lts/lib/node_modules/chrome-devtools-axi/`; `fm-bootstrap.sh:873` documents install | **Keep** | Used outside firstmate (ticket body explicitly states: "being uninstalled with chrome-devtools-axi" — it replaces firstmate, not the other way around) |
| `gh-axi` (global npm) | GitHub CLI wrapper, installed via `npm install -g gh-axi` | Present in `/Users/kevin/.local/share/mise/installs/node/lts/lib/node_modules/gh-axi/`; `fm-bootstrap.sh:873` documents install | **Keep** | Used outside firstmate (ticket body explicitly states: "`gh-axi` and `lavish-axi` are used elsewhere and stay") |
| `lavish-axi` (global npm) | Visual decision/reporting CLI, installed via `npm install -g lavish-axi` | Present in `/Users/kevin/.local/share/mise/installs/node/lts/lib/node_modules/lavish-axi/`; `fm-bootstrap.sh:873` documents install | **Keep** | Used outside firstmate (ticket body explicitly states: "`gh-axi` and `lavish-axi` are used elsewhere and stay") |
| `/Users/kevin/dev/firstmate` (clone) | Firstmate repository itself | Source of truth for this inventory; ticket says "The clone itself stays" | **Keep** | N/A — source of truth for the uninstall investigation |
| `~/.local/state/firstmate` | Expected firstmate state directory | **Does not exist** — observed absence | N/A | Clean |
| `~/.local/share/firstmate` | Expected firstmate data directory | **Does not exist** — observed absence | N/A | Clean |
| `~/.config/firstmate` | Expected firstmate config directory | **Does not exist** — observed absence | N/A | Clean |
| `~/.tasks-axi` | Expected tasks-axi data directory | **Does not exist** — observed absence | N/A | Clean |
| `~/.kimi-code` | Expected Kimi Code integration directory | **Does not exist** — observed absence | N/A | Clean |
| `~/.grok` | Expected Grok integration directory | **Does not exist** — observed absence | N/A | Clean (firstmate creates per-worktree hooks in `.grok/hooks/` but this directory was never created on this machine) |
| `~/.toolbox` | Expected Cursor Toolbox integration directory | **Does not exist** — observed absence | N/A | Clean |
| `~/.claude/settings.json` | Claude Code settings | No firstmate/fm-/tasks-axi/quota-axi/treehouse/no-mistakes references — observed absence | N/A | Clean |
| `~/.codex/hooks.json` | Codex hooks config | No firstmate references — observed absence | N/A | Clean |
| `~/.codex/config.toml` | Codex config | One project name reference to "find-kun-s-firstmate-github-repo" — not tooling, just a project label | N/A | Clean |
| `~/.config/tmux/` (tmux.conf, scripts/) | Tmux config and plugins | No firstmate/fm-/tasks-axi/quota-axi/treehouse/no-mistakes references — observed absence | N/A | Clean |
| `~/.zshrc` / `~/.zshenv` | Shell config | No firstmate/fm-/tasks-axi/quota-axi/treehouse/no-mistakes references — observed absence | N/A | Clean |
| `~/.config/launchdawg/services.yml` | Launchd service registry | No firstmate/fm- references — observed absence (confirmed by ticket body) | N/A | Clean |
| `~/.cursor/hooks.json` / `~/.cursor/agents/` | Cursor hooks and agents | No firstmate/fm-/tasks-axi/quota-axi references — observed absence | N/A | Clean |
| `~/.agents/skills/` (full directory) | Agent skill library (~100+ skills) | No firstmate/fm-/tasks-axi/quota-axi/treehouse/no-mistakes references — observed absence | N/A | Clean |
| `~/.claude/skills/` | Claude-compatible skills (symlink to `.agents/skills`) | No firstmate references — observed absence | N/A | Clean |
| `~/dev/skills/` | Public installer-facing skills repo | No firstmate/fm-/tasks-axi/quota-axi/treehouse/no-mistakes references — observed absence | N/A | Clean |
| `~/dev/dotfiles/` (full tree) | Dotfile repository | Only notchbar config comment about quota-axi environment setup — no actual invocation or firstmate references | N/A | Clean (comment is documentation, not a dependency) |

---

### Per-Tool "Used Elsewhere" Verdict

**Scope covered:** `.agents/skills/` (all files, excluding `tasks/` and `*.jsonl`), `.claude/skills/`, `dev/skills/`, `dev/dotfiles/` (full tree), notchbar config, launchdawg services, shell configs, tmux config, Codex/Claude/Grok/Cursor integration points.

| Tool | Used Outside Firstmate? | Evidence |
|---|---|---|
| `tasks-axi` | **No** | Zero matches in any non-firstmate location searched |
| `quota-axi` | **No** (notchbar mentions it only in a comment) | `dotfiles/notchbar/.config/notchbar/config-pro.toml:124` — comment explaining env var setup, no runtime invocation |
| `treehouse` | **No** | Zero matches in any non-firstmate location searched |
| `no-mistakes` | **No** | Zero matches in any non-firstmate location searched |
| `chrome-devtools-axi` | **Yes** — replaces firstmate as the browser automation tool | Ticket body: "being uninstalled with chrome-devtools-axi" |
| `gh-axi` | **Yes** — used by other tools/workflows | Ticket body: "used elsewhere and stay" |
| `lavish-axi` | **Yes** — used by other tools/workflows | Ticket body: "used elsewhere and stay" |

---

### Shared-State Warnings

1. **`~/.local/bin/`** — Both `treehouse` and `no-mistakes` are Go binaries in a shared PATH directory. Removing them is safe; no other tool on this machine uses these names.
2. **Global npm packages** — `tasks-axi` and `quota-axi` are installed under the mise-managed node prefix (`~/.local/share/mise/installs/node/lts/lib/node_modules/`). Removing them via `npm uninstall -g` is safe; no other tool depends on them.
3. **Notchbar comment** — The notchbar config file contains a one-line comment mentioning `quota-axi` in the context of explaining why `CLAUDE_CONFIG_DIR` must be set. This is documentation, not a runtime dependency. It can be left as-is or cleaned up separately.

---

### What Could Not Be Established Without a Shell

1. **Crontab entries** — `fm-bootstrap.sh` references crontab in its help text but no actual crontab writes were found. A shell `crontab -l` would confirm absence definitively.
2. **Launchd plist files** — No launchdawg services reference firstmate, but a shell check of `~/Library/LaunchAgents/` and `/Library/LaunchAgents/` for firstmate-plist files would confirm absence.
3. **Active tmux bindings** — The tmux config was searched but a shell `tmux list-keys` would confirm no firstmate bindings are loaded in any running session.
4. **npm global bin symlinks** — The mise node_modules directory was listed but a shell `ls -la ~/.local/share/mise/installs/node/lts/bin/ | grep -E 'tasks-axi|quota-axi'` would confirm the exact bin symlinks.
5. **File sizes and permissions** — The Go binaries were confirmed as Mach-O executables but their exact sizes, permissions, and whether they are currently in use would require a shell.

---

## Addendum by the charting session (shell checks the worker could not run)

All observed on 2026-09-22.

- `crontab -l`: no crontab for this user.
- `~/Library/LaunchAgents/`: contains `com.kunchenguid.no-mistakes.daemon.b6c2ac14.plist`, RunAtLoad and KeepAlive true, ProgramArguments `/Users/kevin/.no-mistakes/bin/no-mistakes daemon run --root /Users/kevin/.no-mistakes`. Not present in `~/.config/launchdawg/services.yml`.
- Running processes: pids 6176 (`no-mistakes daemon run`) and 6227 (`no-mistakes daemon log-sink`).
- `~/.no-mistakes/`: 26 MB; `bin/`, `config.yaml`, `state.sqlite`, `logs/`, `repos/` (empty), `servers/` (empty), `socket`, `daemon.lock`, `daemon.pid`.
- `~/.local/bin/no-mistakes` is a symlink to `~/.no-mistakes/bin/no-mistakes`; `~/.local/bin/treehouse` is a 12,914,626-byte binary; `~/.treehouse/` holds one file, `update-check.json`.
- `tmux list-keys`: no firstmate or `fm-` binding.
- mise node bin symlinks present: `chrome-devtools-axi`, `quota-axi`, `tasks-axi`.
- NotchBar: `~/dev/notchbar/plugins/quota/README.md:3` says the plugin is "backed by a pinned local quota-axi" and accepts only 0.1.49; the global install is 0.1.42. Whether the plugin resolves its own copy is not verified here.
- Only project config for no-mistakes: `~/dev/firstmate/.no-mistakes.yaml`.
- Correction to the worker's table: `chrome-devtools-axi` is removed by the map's retirement task, not kept.
