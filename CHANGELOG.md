# Changelog

All notable changes to Ivan's Workflow are documented in this file.

## [3.0.0] — 2026-03-17

### Changed

- **Environment-driven configuration**: All hardcoded paths and service URLs in `iwo/config.py` replaced with `IWO_*` environment variables loaded from `.env` file. Zero `/home/vanya` or `192.168.x.x` references remain in source code.
- **Portable across machines**: Fresh clone defaults to current working directory for project root, bundled skills, memory disabled, notifications disabled. Progressive configuration via `.env`.
- **Version bump to 3.0.0**: Breaking change — `config.py` dataclass fields now read from environment instead of hardcoded defaults. Existing installations must create a `.env` file.

### Added

- `.env.example` — full documentation of all `IWO_*` environment variables with safe placeholder values.
- Built-in `.env` parser in `config.py` (works without `python-dotenv` installed).
- `python-dotenv` as a dependency (preferred loader, falls back to built-in parser).
- `IWO_SKILLS_DIR` config field — bundled skills at `{repo}/skills`, overridable for custom locations.
- Auto-source `.env` in all shell scripts (`directive-next-spec.sh`, `directive-resolve-ops.sh`, `kanban-dashboard-start.sh`, `test-ops-agent.sh`).
- `EnvironmentFile` directive in `iwo.service` for systemd.
- Graceful memory auto-disable when no Qdrant/Neo4j endpoints configured.

### Fixed

- `directives.py`: 3 hardcoded skill paths replaced with `self.config.skills_dir` references.
- `tools/kanban-dashboard.py`: Added `.env` loading to `get_project_root()`, changed fallback from hardcoded path to `Path.cwd()`.
- `scripts/setup-new-machine.sh`: Desktop launchers now use shell variable expansion at install time instead of hardcoded paths.
- `skills/credential-manager/scripts/bw-auto-unlock.py`: Replaced hardcoded path in error message with generic `secret-tool` instruction.

## [2.9.0] — 2026-02-25

### Changed

- **Headless dispatch**: Replaced interactive `send-keys` dispatch with headless `claude -p` process invocations. Each agent pane is an idle bash shell between tasks. Eliminates canary probes, state machine from dispatch path, and queue retry hacks.
- **State model simplified**: 5-state machine (IDLE/PROCESSING/STUCK/WAITING_HUMAN/CRASHED) reduced to 3 states (IDLE/RUNNING/ERROR) derived from `pane_current_command`.
- **Agent model tiering**: `AGENT_MODEL_MAP` assigns planner/builder/reviewer to Opus, tester/deployer/docs to Sonnet.

### Added

- `iwo/headless_commander.py` — new dispatch layer using `claude -p` with `--output-format stream-json`.
- `iwo/directives.py` — DirectiveProcessor: polls `.directives/` every 2s for 9 JSON directive types (start-spec, next-spec, resume, reconcile, status, pause, unpause, cancel-spec, resolve-ops).
- `iwo/ops_actions.py` — Ops Actions register: auto-extraction from handoffs, fingerprint dedup, priority classification, stale detection.
- Desktop launcher scripts for directive injection (`scripts/directive-next-spec.sh`, `scripts/directive-resolve-ops.sh`).
- `scripts/setup-new-machine.sh` — full machine setup script (venv, desktop launchers, verification).

## [1.0.2] — 2026-02-11

### Added

- **Stack installation test suite** (`tests/test-install-stacks.sh`): Automated tests covering 6 stack combinations (cloudflare-workers/hono/typescript, node/express/typescript, node/express/javascript, python/fastapi/python, cloudflare-workers/nextjs/typescript, deno/none/typescript). Validates LANG_EXT derivation, SOURCE_DIR substitution, template variable replacement, generated-by headers, and file non-emptiness across all paths.
- **Design decisions documentation** added to `docs/TROUBLESHOOTING.md` explaining intentional choices: `set -uo` vs `set -euo` in hooks, example paths in agent definitions, and all-runtimes-included in project-stack.md.

### Fixed

- Added `set -uo pipefail` to `core/scripts/load-project-credentials.sh` — was the only script without error mode flags.

## [1.0.1] — 2026-02-11

### Fixed

**Template substitution bugs in `install.sh`:**

- Added `LANG_EXT` variable derivation from `LANGUAGE` (typescript→ts, javascript→js, python→py). Previously, `project-stack.md` contained a literal `${LANG_EXT}` glob pattern that matched nothing.
- Added `SOURCE_DIR` substitution to the `project-stack.md` sed block — was defined but never substituted into the template.
- Added 7 missing variable substitutions to the `CLAUDE.md` sed block: `COVERAGE_THRESHOLD`, `DEPLOY_CMD`, `DEV_CMD`, `TEST_RUNNER`, `LINTER`, `FORMATTER`, `HANDOFFS_DIR`. These all appeared as literal `${...}` placeholders in the generated file.
- Changed `project-domain.md` generation from `cp` to `sed` processing, so `${PROJECT_NAME}` is now substituted.

**Hook scripts:**

- Added `set -uo pipefail` to `core/hooks/prompt-handoff.sh`. This was the only hook script without error mode flags. Uses `-uo` rather than `-euo` because Stop hooks should not abort on individual check failures.

**Agent files:**

- Added `# Generated by Ivan's Workflow` header to all 8 agent definition files in `core/agents/`. These files previously started directly with YAML frontmatter `---`, inconsistent with the framework's header convention.

**Skill files:**

- Added Honesty Protocol section to `credential-manager` and `spec-checklist` skills. All other skills had this section; these two were missed during initial implementation.
- Added Definition of Failure section to `spec-checklist` and `workflow-handoff` skills (identified by audit).
- Added Definition of Failure section to `credential-manager` skill (identified during fix implementation — not in the original audit but was the only skill missing this section).

**Repository hygiene:**

- Committed `docs/REVIEW-PROMPT.md` which was previously untracked.

### Verification

All fixes verified via end-to-end install test: `install.sh` now generates `CLAUDE.md`, `project-stack.md`, and `project-domain.md` with zero literal `${...}` placeholders across all supported stack configurations.

## [1.0.0] — 2026-02-10

### Added

- Initial release of Ivan's Workflow framework
- 6 main agents: Planner, Builder, Reviewer, Tester, Deployer, Docs
- 8 subagents: code-simplifier, verify-app, ux-reviewer, schema-guardian, integration-tester, pr-architect, perf-monitor, docs-agent
- 9 slash commands including /ralph-loop, /test-and-commit, /workflow-start
- 6 hooks: builder-guard, verify-work, pre-review-checks, classify-risk, metrics-logger, prompt-handoff
- 9 skills with Honesty Protocol and Definition of Failure sections
- Interactive installer with symlink/copy modes
- Bitwarden CLI credential management with three-strategy fallback
- tmux-based multi-agent launch and monitoring scripts
- Support for CloudFlare Workers, Node.js, Deno, and Python runtimes
