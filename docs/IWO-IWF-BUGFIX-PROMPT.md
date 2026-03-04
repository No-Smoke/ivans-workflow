# IWO/IWF Bug-Fixing Session Prompt

Copy everything below the line into a fresh chat in the eBatt.ai Claude Project.

---

## Task: Debug and fix IWO auto-dispatch — agents not picking up handoffs

IWO (Ivan's Workflow Orchestrator) is failing to reliably dispatch handoffs to agents. Despite 8 bugs fixed in the previous session, the system still stalls. I need you to diagnose and fix the remaining issues.

### System Overview

**IWO** is a Python daemon (~4,800 lines across 10 modules) that orchestrates 6 Claude Code agents in tmux. When one agent writes a handoff JSON file, IWO detects it via inotify watchdog and dispatches the next agent.

**IWF** (Ivan's Workflow) is the agent framework — 6 agents (Planner, Builder, Reviewer, Tester, Deployer, Docs) running in tmux windows, each with a SKILL.md defining their role.

**Repos:**
- IWO: `/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/` (commit `2962915`)
- eBatt (agents + handoffs): `/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/` (commit `8c3b1f9`)

### Current Architecture (v2.8.5)

Dispatch has three layers:
1. **Direct dispatch:** `HandoffHandler.on_created()` → `process_handoff()` → canary probe on target → rich activation prompt
2. **Queue retry:** If canary fails, handoff queued. `_process_pending_activations()` retries every ~2s. After 30s queue age, ignores state machine hint. After 2min, sends notification.
3. **State machine (display only):** IDLE/PROCESSING/STUCK/CRASHED for TUI dashboard. NOT used for dispatch decisions.

### Key Files to Read First

Read these files using Desktop Commander before proposing any changes:

1. `docs/ARCHITECTURE.md` — full architecture guide with troubleshooting decision tree
2. `docs/BUG-REPORT-2026-02-21-state-detection.md` — 8 bugs found and fixed, with root causes
3. `iwo/daemon.py` (1172 lines) — HandoffHandler, process_handoff(), _process_pending_activations(), _activate_for_handoff()
4. `iwo/commander.py` (485 lines) — activate_agent() with rich prompt, send_canary_and_wait(), AgentPane
5. `iwo/state.py` (195 lines) — AgentStateMachine, _check_idle_prompt()
6. `iwo/pipeline.py` (341 lines) — PipelineManager, queue/dequeue, agent assignments, staleness
7. `iwo/config.py` (148 lines) — IWOConfig defaults
8. `tests/test_bug_fixes.py` — existing tests for bugs 1-3

Also read the IWF agent activation flow:
- `.claude/commands/workflow-next.md` (in eBatt repo) — the slash command agents are supposed to execute
- `.claude/hooks.json` (in eBatt repo) — agent hooks configuration

### Debugging Decision Tree

Follow this to diagnose where dispatch fails:

**Step 1: Did IWO see the file?** Check `received_at` in the handoff JSON. Present = watchdog fired. Absent = detection failed.

**Step 2: Is LATEST.json correct?** `readlink` the symlink vs highest-numbered JSON file.

**Step 3: Was canary attempted?** TUI log panel shows "Canary probe on {agent}..."

**Step 4: Did canary pass?** "Canary passed" = dispatch attempted. "Canary failed" = queued for retry.

**Step 5: Did agent execute?** Check tmux pane for the rich prompt text. If visible but no execution = Claude Code ignored it.

**Step 6: Pipeline state?** Check `pipeline.is_agent_busy()`, `pipeline.queue_depth()`, pipeline status.

### Known Fixed Bugs (Do Not Re-Fix)

1. State machine stuck PROCESSING → canary is now the dispatch gate (c1cf05a, 09a59bd)
2. LATEST.json symlink stale → IWO owns symlink updates (c1cf05a)
3. Stale pipeline assignments → session-timestamp staleness (849413c)
4. Permission prompts → `--permission-mode bypassPermissions` (220b4ae, 68e2e1d)
5. Missing webhook notifications → INFO-severity audit events (05f3970)
6. Permission mode refinement → exact skill paths (cc028ec)
7. Audit file filtering → .audit/ excluded from HandoffHandler (b11ab20)
8. /workflow-next silently ignored → rich activation prompt (fed1c67)
9. Queue retry deadlock → 30s threshold ignores state machine (2c89c71)

### What's Still Failing

Despite all fixes, the Reviewer agent did not pick up Builder handoff #012 for EBATT-006A. The rich activation prompt was committed (fed1c67) but needs IWO restart to take effect. After restart, if dispatch still fails, investigate:

- Whether `send_command()` (tmux send-keys) actually delivers the full rich prompt text reliably (long strings may be truncated or garbled by tmux)
- Whether the canary probe's "bottom 5 lines" check matches what Claude Code actually shows
- Whether `pipeline.is_agent_busy()` is returning True when it shouldn't (blocking queue drain)
- Whether the queue `peek_queue()` returns items with correct `queued_at` timestamps
- Whether the Textual TUI timer for `_poll_states` is actually calling `_process_pending_activations()`

### Rules

- Use Desktop Commander for ALL file operations under `/home/vanya/`
- Read the ARCHITECTURE.md troubleshooting section before proposing changes
- Query `qdrant-new:semantic_search collection='project_memory_v2' query='IWO dispatch canary queue retry'` for additional context
- Query `neo4j-memory-remote:search_memories query='IWO Dispatch System Option A'` for architecture decisions
- Run `python3 -m pytest tests/ -v` after any code change
- All syntax must pass `python3 -m py_compile iwo/{file}.py`
- Commit with detailed messages explaining root cause and fix
- Push to `origin main` after each fix
- Follow the Honesty Protocol: report exact evidence, no claims without verification

### Reproduction Steps

1. Start IWO: `cd /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator && source .venv/bin/activate && python -m iwo.tui`
2. Start agents: `cd /home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt && ./scripts/boris-workflow/launch-tmux-agents-v5.sh`
3. On the Planner pane: `/workflow-start` or `/workflow-next`
4. Watch IWO TUI — when Planner writes handoff, does the Builder pick it up?
5. When Builder writes handoff, does the Reviewer pick it up?
6. Check for stuck queues, false PROCESSING states, silent command failures

### Success Criteria

IWO reliably dispatches handoffs through at least one full pipeline cycle: Planner → Builder → Reviewer → Tester → Deployer → Docs, with no manual intervention. Each transition should complete within 60 seconds of the previous agent writing its handoff.
