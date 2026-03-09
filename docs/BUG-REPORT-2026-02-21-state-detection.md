# IWO Bug Report: Agent State Detection Issues

**Filed:** 2026-02-21
**Severity:** Medium (functional workaround exists, but degrades IWO auto-dispatch)
**Affects:** IWO v2.8.2, state machine in `iwo/commander.py`

---

## Bug 1: Agents stuck in PROCESSING when actually IDLE

### Symptoms
- All 6 pipeline agents show PROCESSING in TUI after startup
- Agents are actually sitting at idle Claude Code prompts
- State cycles `stuck → processing` repeatedly in logs
- IWO does not auto-dispatch handoffs because it thinks agents are busy

### Root Cause
The state machine tracks output activity independently from the canary probe. When agents initialize Claude Code on startup, they emit output (loading CLAUDE.md, skills, rules), setting state to PROCESSING. The state machine then keeps detecting pane activity (cursor blinks, prompt redraws) and resets the idle timer, preventing transition to IDLE.

The canary probe (which checks for a visible prompt) would correctly identify these agents as idle, but the state machine doesn't incorporate canary results.

### Fix Required
The state machine should incorporate canary probe results: if the canary detects a clean prompt, transition to IDLE regardless of recent output activity. The canary is the ground truth; raw output activity is a heuristic.

### Workaround
Manually send `/workflow-next` to agents. IWO dispatch is bypassed but the pipeline works.

---

## Bug 4: Pipeline agents pause for permission prompts (FIXED)

### Symptoms
- Both Planner and Builder paused early in their work to request folder access permissions
- Agents could not run unattended — required human intervention to approve access

### Root Cause
The launch script (`scripts/boris-workflow/launch-tmux-agents-v5.sh` in eBatt repo) started Claude Code with bare `claude` command, which runs in interactive permission mode by default.

### Fix Applied
Added `--dangerously-skip-permissions` flag to all 6 pipeline agent launch commands. Agent 007 already had this flag in `commander.py`. Commit `220b4ae` in eBatt repo (v5.5.1).

---

## Bug 2: LATEST.json symlink not updated by Planner

### Symptoms
- Planner wrote handoff 019-planner-*.json but LATEST.json still pointed to 018-docs-*.json
- IWO never detected the new handoff
- Builder sat idle for 1+ hour
- No phone notification (007 not triggered — no agent was assigned to the pipeline)

### Root Cause
The Planner agent wrote its handoff JSON file but did not update the LATEST.json symlink. The `/workflow-next` hook either didn't fire or didn't complete the symlink update. IWO relies on LATEST.json to determine pipeline state.

### Fix Required
Two-part fix:
1. **Defensive:** Add a "dangling handoff" detector to the auditor — compare highest-numbered JSON file against LATEST.json target for active pipelines. If they diverge, emit a WARNING.
2. **Root cause:** Investigate why the Planner's post-handoff hook didn't update the symlink. Check `.claude/hooks.json` for the handoff hook configuration.

### Workaround
Manually fix symlink: `ln -sf 019-planner-*.json LATEST.json` then press `r` in TUI.

---

## Bug 3: Stale pipeline assignments blocking dispatch

### Symptoms
- Builder assigned to SERVER-SIDE-API (stale, from previous session)
- AI-INFRASTRUCTURE handoff queued but never dispatched
- Queue depth of 13, mostly stale work

### Root Cause
IWO scans existing handoff directories on startup and reconstructs pipeline state, but it has no way to distinguish "active work in progress" from "leftover from a previous session that was never cleaned up." Stale pipelines hold agent assignments, preventing new work from being dispatched.

### Fix Required
Add a staleness threshold — if a pipeline's last_handoff_at is older than N hours (configurable, e.g. 4h), auto-mark it as `stale` and release the agent assignment. Or add a TUI command to bulk-clear stale pipelines.

### Workaround
Manually send `/workflow-next` to the target agent.


---

## Resolution Summary (2026-02-21, commits c1cf05a → 849413c)

### Bug 1: RESOLVED via Option A (canary-based dispatch)
State machine no longer controls dispatch decisions. Handoff routing now uses
direct canary probe (send Enter, wait for prompt) as the definitive idle check.
State machine remains for dashboard display and auditor alerts only.
Additionally, `_check_idle_prompt()` tightened: checks bottom 5 lines, rejects
if spinner characters visible.

### Bug 2: RESOLVED via HandoffHandler symlink ownership
IWO now updates LATEST.json symlink whenever a new handoff file arrives.
Agents no longer need to manage the symlink. Commit c1cf05a.

### Bug 3: RESOLVED via Option B (session-based staleness)
On daemon startup, any handoff file with mtime predating the current session
is marked stale — no agent assignment. Replaces unreliable hours-based threshold
that failed when file mtimes were touched during reconciliation. Commit 849413c.

### Bug 4: RESOLVED (commit 220b4ae, then 68e2e1d)
`--permission-mode bypassPermissions` in all launch commands.

### Bugs 5-7: RESOLVED (commits 05f3970, cc028ec, b11ab20)
Info-level webhooks, audit file filtering, permissions refinement.

### Architectural Decision
Based on multi-model consensus (Gemini 2.5 Pro, GPT 5.2):
- **Option A (dispatch on file arrival):** Canary probe is the definitive idle
  test. Bypasses unreliable TUI screen-scraping state machine for routing.
- **Option B (session timestamp):** Daemon start time partitions current vs stale
  work. No need for UUID embedding in handoff files.
- **Option C (done files):** Deferred. Would require SKILL.md changes across all
  6 agents and doesn't eliminate need for canary probe.
