# IWO Fix Changelog

All bug fixes and corrections from project inception through v2.9.0.
Ordered chronologically (newest first). Feature commits are excluded — see `git log` for the full history.

---

## Fix 12: Headless dispatch missing --model flag (all agents defaulting to Haiku)
**Date:** 2026-02-25 | **Version:** v2.9.0 | **File:** `iwo/headless_commander.py` (691 lines)

**Root cause:** `_dispatch_headless()` built the `claude -p` command without `--model`, and no model was configured in `~/.claude/settings.json`. Claude CLI defaults to the cheapest available model (Haiku 4.5), so every headless dispatch — Planner, Builder, Reviewer, Tester, Deployer, Docs — was running on Haiku instead of Opus/Sonnet.

**Impact:** All pipeline quality degraded. Planner producing shallow plans, Builder writing lower-quality code, Reviewer missing issues. Agent 007 diagnostic reasoning ineffective. This affected every headless dispatch since the v2.9.0 migration to `claude -p`.

**Fix:** Added `AGENT_MODEL_MAP` mapping agent roles to models. Planner/Builder/Reviewer use `opus` for quality-critical reasoning. Tester/Deployer/Docs use `sonnet` for speed on mechanical tasks. `--model {model}` injected into the `claude -p` command string.

---

## Feature: Directive system — desktop launcher pipeline control
**Date:** 2026-02-25 | **Files:** `iwo/directives.py` (new, 388 lines), `daemon.py`, desktop launchers

**Summary:** Operator commands via filesystem JSON files. Desktop launcher right-click menus write directive JSONs to `docs/agent-comms/.directives/`, IWO polls every 2 seconds via `DirectiveProcessor`, executes typed handler, archives to `.processed/`.

**Directive types:** start-spec, next-spec, resume, reconcile, status, pause, unpause, cancel-spec

**Desktop launchers updated:**
- `boris-workflow.desktop` (IWF): 6 actions — Kill Session, Plan Next Spec, Start Spec, Resume Spec, View Git Log, Load Credentials
- `iwo.desktop` (IWO): 11 actions — Run Headless, Plan Next Spec, Start Spec, Resume Spec, Reconcile, Status Report, Pause, Unpause, Cancel Spec, Stop IWO, View Logs

**Daemon integration (4 touchpoints):** import, watchdog exclusion for `.directives/`, DirectiveProcessor init, `ensure_dirs()` at startup, `poll()` in run_loop every 2 seconds.

**Fix:** Desktop launcher filenames changed from `%s` to `%s%N` (nanosecond precision) to prevent collisions on rapid actions.

---

## Fix 11: Strip interactive dispatch — headless only
**Date:** 2026-02-25 | **Files:** `iwo/headless_commander.py`, `scripts/boris-workflow/launch-tmux-agents-v5.sh`

**Symptom:** IWO still injecting C-u + `/workflow-next` into agent panes via send-keys while agents were actively working. The "dual-mode" HeadlessCommander always hit the interactive path because agents launched as interactive Claude Code sessions (`pane_current_command = "claude"/"node"`), never reaching the headless dispatch path.

**Root cause:** Claude Code built HeadlessCommander with dual-mode dispatch instead of replacing interactive dispatch entirely. Since the launch script started `claude --permission-mode bypassPermissions` in each pane, `pane_current_command` was always in `CLAUDE_COMMANDS`, routing every dispatch to `_dispatch_interactive()` — the same broken send-keys injection.

**Fix (Option B — clean removal):**
- Removed `_dispatch_interactive()`, `_is_interactive_prompt()`, all retry/delay constants
- Removed `CLAUDE_COMMANDS`, `_ANSI_ESCAPE_RE`, `_PROMPT_CHARS`, `_CHROME_INDICATORS`
- Simplified `is_agent_idle()` — only checks `pane_current_command ∈ IDLE_SHELLS`
- Simplified `activate_agent()` — always calls `_dispatch_headless()`, no mode switching
- Simplified `_is_agent_complete()` — only checks shell idle
- Updated launch script to v5.6: all 7 panes start as idle bash shells, no Claude Code launched
- Launch time reduced from 3-6 minutes to ~10 seconds

**Impact:** headless_commander.py 870→677 lines, launch script 277→152 lines. Interactive dispatch is permanently eliminated. IWO can only dispatch via `claude -p` into idle bash panes.

---

## Fix 10: TUI keyboard shortcuts swallowed by focused widget
**Commit:** `6761321` | **Date:** 2026-02-23 | **Files:** `iwo/tui.py`

**Symptom:** Pressing Q (quit), D (deploy), R (reconcile), or P (pause) did nothing when the RichLog panel had focus — which it always did after scrolling.

**Root cause:** Textual's `RichLog` widget captured single-character keypresses before they reached app-level `Binding()` definitions. The bindings lacked `priority=True`, so focused widgets took precedence.

**Fix:** Added `priority=True` to all four `Binding()` definitions so they fire at the App level regardless of widget focus.

---

## Fix 9: Interactive Claude sessions not detected as idle (Bug 6)
**Commit:** `7486561` | **Date:** 2026-02-23 | **Files:** `iwo/headless_commander.py`, `tests/`

**Symptom:** Daemon never dispatched handoffs to any agent. All 6 agents reported as "busy" despite sitting at idle `>` prompts.

**Root cause:** `is_agent_idle()` only checked `pane_current_command in IDLE_SHELLS` (bash/zsh/sh/fish). All agents ran interactive Claude Code sessions where `pane_current_command = "claude"` — not in `IDLE_SHELLS`, so always returned `False`.

**Fix:** Dual-mode idle detection and dispatch:
- `is_agent_idle()`: also detects interactive `>` prompt via `_is_interactive_prompt()` (scans last 5 visible lines)
- `activate_agent()`: routes to `_dispatch_interactive()` (Ctrl+U + `/workflow-next`) for Claude sessions, or `_dispatch_headless()` (cat prompt | claude -p) for bash panes
- `_is_agent_complete()`: bypasses `_active_agents` filter so `check_completions()` can detect when interactive agents finish

---

## Fix 8: 007- prefix filter blocking sequence-7 handoffs
**Commit:** `7e33580` | **Date:** 2026-02-23 | **Files:** `iwo/daemon.py`

**Symptom:** Handoffs at sequence #7 (e.g. `007-planner-2026-02-22T23-50.json`) were silently dropped. Pipeline stalled after 6 successful handoffs.

**Root cause:** The daemon filtered all files starting with `"007-"` to skip Agent 007 audit diagnostics, but this prefix collides with zero-padded sequence numbers. The `.audit/` directory filter already handles Agent 007's files, making the prefix filter redundant and harmful.

**Fix:** Removed `007-` prefix filter from all 3 locations: watchdog handler, reconciliation, and startup recovery.

---

## Fix 7: Rich activation prompt replaces bare /workflow-next
**Commit:** `fed1c67` | **Date:** 2026-02-21 | **Files:** `iwo/daemon.py`, `iwo/commander.py`

**Symptom:** Agents received `/workflow-next` command but produced no output and returned to prompt. Pipeline dispatch appeared successful but agents didn't start working.

**Root cause:** Claude Code silently ignored the `/workflow-next` slash command under context pressure (47.2k CLAUDE.md + slash command processing unreliable).

**Fix:** `activate_agent()` now builds a rich natural language prompt inlining handoff path, spec ID, sequence, source agent, and action summary. Gives the model explicit context without relying on slash command machinery. Falls back to bare `/workflow-next` if no handoff context available.

---

## Fix 6: Noisy canary-failure notifications
**Commit:** `8ec14cf` | **Date:** 2026-02-21 | **Files:** `iwo/daemon.py`

**Symptom:** Phone buzzed with "canary failure" alerts every few seconds during normal agent initialization.

**Root cause:** Canary probe failures during the 10-15s Claude Code startup window triggered immediate notifications. These are expected and transient.

**Fix:** Suppress canary-failure notifications until agent has been stuck for 2+ minutes. Only then escalate to human alert.

---

## Fix 5: Queue retry deadlock — state machine blocking canary retry
**Commit:** `2c89c71` | **Date:** 2026-02-21 | **Files:** `iwo/daemon.py`

**Symptom:** Handoffs sat in queue indefinitely after initial dispatch failure. Agent was idle but queue never retried.

**Root cause:** When canary probe failed on initial dispatch (agent still loading), the handoff was queued. But `_process_pending_activations()` skipped the canary retry if the state machine showed PROCESSING — creating a permanent deadlock. The canary is the ground truth, not the state machine.

**Fix:** After a handoff has been queued for 30+ seconds, always try the canary probe regardless of state machine state. Shorter-queued items still respect the state machine hint.

---

## Fix 4: False IDLE during active processing
**Commit:** `09a59bd` | **Date:** 2026-02-21 | **Files:** `iwo/state.py`

**Symptom:** State machine occasionally detected IDLE while agent was actively generating output. Caused premature dispatch attempts.

**Root cause:** `_check_idle_prompt()` scanned all 30 captured lines. The `❯` character could appear in interface chrome (status bar) above active output. No check for spinner/processing indicators.

**Fix:** Tightened idle prompt detection:
- Check only bottom 5 non-empty lines (not all 30)
- Reject if braille spinner chars or `Thinking...` visible
- Prevents false IDLE when `❯` appears in chrome above output

---

## Fix 3: Three auto-dispatch bugs (state detection, LATEST.json, stale pipelines)
**Commit:** `c1cf05a` | **Date:** 2026-02-21 | **Files:** `iwo/state.py`, `iwo/daemon.py`, `iwo/pipeline.py`, `iwo/config.py`

### Bug 3a: State machine stuck in PROCESSING
**Symptom:** All agents show PROCESSING after startup, never transition to IDLE.
**Root cause:** State machine reset stability timers on cosmetic redraws (status bar, token counter) even when idle prompt was visible.
**Fix:** Don't reset timers when prompt is visible. Fast IDLE path: 2s stability when prompt detected (vs full timeout).

### Bug 3b: LATEST.json symlink not updated
**Symptom:** Planner wrote new handoff but LATEST.json pointed to old file. Builder never saw it.
**Root cause:** Agents were expected to manage LATEST.json symlink but didn't always do so.
**Fix:** IWO `HandoffHandler.on_created()` now updates LATEST.json symlink on every new numbered JSON file. IWO is the authority, not agents.

### Bug 3c: Stale pipeline assignments blocking dispatch
**Symptom:** After restart, old pipeline assignments prevented new handoffs from being dispatched.
**Root cause:** Pipeline assignments from previous sessions persisted with no staleness check.
**Fix:** Added `stale_pipeline_hours` config (default 4h). `release_stale_pipelines()` runs every poll cycle. Startup recovery skips agent assignment for old pipelines.

---

## Fix 2: Audit files parsed as handoffs (Bug 5)
**Commit:** `b11ab20` | **Date:** 2026-02-21 | **Files:** `iwo/daemon.py`

**Symptom:** "Invalid handoff structure" desktop notifications on every audit event.

**Root cause:** `.audit/` directory files written by auditor for info-level webhook events were picked up by the watchdog file handler and parsed as handoff JSON.

**Fix:** Filter `.audit/` directory files from `HandoffHandler` before parsing.

---

## Fix 1b: Agent 007 permissions prompt
**Commit:** `cc028ec` | **Date:** 2026-02-21 | **Files:** `iwo/commander.py`

**Symptom:** Agent 007 paused for confirmation when activated by daemon.

**Root cause:** Missing `--permission-mode bypassPermissions` flag on Agent 007 launch command.

**Fix:** Added `--permission-mode bypassPermissions` to match launch script v5.5.2.

---

## Fix 1a: Missing Python packages in venv
**Commit:** `4122088` | **Date:** 2026-02-19 | **Files:** `requirements.txt`

**Symptom:** Memory health dots permanently red (Qdrant, Neo4j unreachable).

**Root cause:** IWO runs in `.venv` which was missing `qdrant-client`, `neo4j`, and `httpx`. These were installed in system Python only. `initialize()` caught `ImportError`, set clients to `None`, `health_check` showed permanent red.

**Fix:** Installed packages in venv, froze `requirements.txt`.

---

## Fix 1: Health check reconnection on transient startup failure
**Commit:** `6e13577` | **Date:** 2026-02-19 | **Files:** `iwo/memory.py`

**Symptom:** Memory stayed disabled for entire session after transient startup failure, even though services became reachable seconds later.

**Root cause:** `_init_attempted` flag prevented retry after `initialize()` failure. A transient network issue at startup meant permanent red dots.

**Fix:** `health_check()` now attempts lightweight reconnection if clients are `None`. On success, sets client and updates `_available` flag. Polled every 60s by TUI, so recovery happens within one minute.

---

## Fix 0: Path migration
**Commit:** `95e6986` | **Date:** 2026-02-18 | **Files:** `iwo/config.py`, `iwo/daemon.py`

**Symptom:** IWO couldn't find handoff directories after repo rename.

**Root cause:** Hardcoded paths referenced old repository name.

**Fix:** Updated all paths to `ivans-workflow-orchestrator`.

---

## Summary

| # | Fix | Commit | Impact |
|---|-----|--------|--------|
| 10 | TUI keybindings swallowed by widget focus | `6761321` | TUI unusable without mouse |
| 9 | Interactive Claude sessions seen as busy | `7486561` | **All dispatch blocked** |
| 8 | 007- prefix filter drops sequence-7 handoffs | `7e33580` | Pipeline stalls at seq 7 |
| 7 | Bare /workflow-next silently ignored | `fed1c67` | Agents don't start working |
| 6 | Noisy canary-failure notifications | `8ec14cf` | Phone notification storm |
| 5 | Queue retry deadlock (state machine blocks canary) | `2c89c71` | **Queued handoffs stuck forever** |
| 4 | False IDLE during active processing | `09a59bd` | Premature dispatch attempts |
| 3a | State machine stuck in PROCESSING | `c1cf05a` | **All dispatch blocked** |
| 3b | LATEST.json symlink not updated | `c1cf05a` | Handoffs invisible to agents |
| 3c | Stale pipeline assignments | `c1cf05a` | Dispatch blocked after restart |
| 2 | .audit/ files parsed as handoffs | `b11ab20` | Spam notifications |
| 1b | Agent 007 missing permissions flag | `cc028ec` | 007 pauses for human input |
| 1a | Missing venv packages | `4122088` | Memory permanently offline |
| 1 | Health check no reconnection | `6e13577` | Memory permanently offline |
| 0 | Hardcoded repo paths | `95e6986` | Nothing found at startup |

**Critical dispatch blockers fixed:** 3 (Fixes 9, 5, 3a)
**Total fixes:** 15 across 13 commits
