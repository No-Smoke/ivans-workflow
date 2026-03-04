# IWO Headless Dispatch Migration Plan

**Version:** 1.1 | **Date:** 2026-02-25
**Author:** Claude Opus 4.6 + Gemini 2.5 Pro (multi-model consensus)
**Status:** IMPLEMENTED — headless_commander.py and launch script updated
**Relates to:** IWO v2.9.0, Ivan's Workflow v5.6

---

## 1. Problem Statement

IWO has accumulated 9+ bug fixes over two days, all addressing symptoms of the same root cause: **dispatching work to Claude Code agents by injecting text into interactive TUI sessions via tmux send-keys is inherently unreliable**. The canary probe system, state machine, and queue retry logic are all workarounds for this fundamental mismatch. Despite extensive debugging, not one full pipeline run (Planner → Builder → Reviewer → Tester → Deployer → Docs) has completed without manual intervention.

The interactive dispatch mechanism was chosen early in IWO's design because agents were envisioned as long-running sessions with loaded CLAUDE.md context. However, Claude Code's headless mode (`claude -p`) automatically loads CLAUDE.md from the working directory and supports session resumption via `--resume`. The interactive-mode assumption is no longer necessary.

## 2. Proposed Solution

Replace interactive send-keys dispatch with headless `claude -p` process invocations. Each agent tmux pane operates as an idle bash shell between tasks. When IWO detects a handoff targeting an agent, it launches a fresh `claude -p` process in that pane with the handoff context. When the process exits, the pane returns to idle bash.

**Key insight:** Agent 007 in the current IWO already uses this pattern successfully — "Pane starts as idle bash. IWO launches Claude Code with structured prompt. 007 runs, reports, exits. Pane returns to idle." This migration extends the 007 pattern to all six pipeline agents.

## 3. Architecture: Before and After

### Current (Broken)
```
Watchdog → parse/validate → canary probe (send Enter, regex prompt) →
pane.send_keys(rich_prompt) → hope Claude Code picks it up →
queue/retry if it doesn't → state machine tracks IDLE/PROCESSING/STUCK
```

### Proposed (Headless)
```
Watchdog → parse/validate → check pane is idle (process == bash) →
write prompt file → launch `claude -p` in pane → monitor PID →
process exits → pane returns to idle bash → repeat
```

## 4. Implementation Phases

### Phase 0: Validation Spike (1-2 hours)

**Goal:** Prove the headless mechanics work before touching IWO.

Create a standalone script `tests/spike_headless.py`:

1. **Test basic invocation:** From the eBatt project directory, run `claude -p "Read CLAUDE.md and report your role" --output-format json`. Confirm it loads CLAUDE.md automatically. Capture `session_id` from JSON output.

2. **Test role injection via --append-system-prompt-file:** Run with `--append-system-prompt-file .claude/skills/builder/SKILL.md` (or equivalent agent skill file path). Confirm the agent adopts its role.

3. **Test session resumption:** Resume with `claude --resume $SESSION_ID -p "What was your previous task?"`. Confirm context is preserved.

4. **Test tmux pane launch:** In a tmux pane, launch `claude -p "..." --output-format stream-json 2>&1 | tee /tmp/agent-test.log`. Confirm process starts, runs, and exits cleanly. Confirm pane returns to bash prompt.

5. **Test idle detection:** After the process exits, run `tmux list-panes -F '#{pane_current_command}'` and confirm it shows `bash` (not `claude`).

6. **Test error handling:** Run with an invalid prompt or simulate a failure. Document exit codes and stderr content for different failure types.

**Success criteria:** All six tests pass. Document exact command syntax, exit codes, and timing.

### Phase 1: New HeadlessCommander Class (4-6 hours)

**Goal:** Build the replacement dispatch layer without modifying existing code.

Create `iwo/headless_commander.py` (~150 lines):

```python
class HeadlessCommander:
    """Dispatch layer using headless claude -p invocations.
    
    Replaces TmuxCommander's send-keys approach with process-based dispatch.
    Each agent pane starts as idle bash. Dispatch launches claude -p in the pane.
    Process exit returns pane to idle.
    """
    
    def __init__(self, config: IWOConfig):
        self.config = config
        self.session: Optional[libtmux.Session] = None
        self.agents: dict[str, AgentPane] = {}
        self._active_pids: dict[str, int] = {}  # agent_name → PID
        self._session_ids: dict[str, str] = {}  # spec:agent → session_id
    
    def discover_agents(self) -> dict[str, AgentPane]:
        """Discover agent panes via @iwo-agent tags (same as current)."""
        # Reuse existing pane discovery logic
        ...
    
    def is_agent_idle(self, agent_name: str) -> bool:
        """Check if agent pane is idle (running bash, not claude).
        
        This replaces the entire canary probe + state machine + idle pattern
        regex system with a single deterministic check.
        """
        pane = self.agents.get(agent_name)
        if not pane:
            return False
        current_cmd = pane.pane.pane_current_command
        return current_cmd in ("bash", "zsh", "sh", "fish")
    
    def activate_agent(self, agent_name: str, handoff: 'Handoff', 
                       handoff_path: Path) -> bool:
        """Launch headless claude -p in the agent's tmux pane.
        
        1. Verify pane is idle
        2. Build prompt file with handoff context + agent role
        3. Launch claude -p process in pane
        4. Record PID for monitoring
        """
        if not self.is_agent_idle(agent_name):
            log.warning(f"{agent_name} pane not idle, cannot dispatch")
            return False
        
        # Build prompt file
        prompt_path = self._build_prompt_file(agent_name, handoff, handoff_path)
        
        # Build command
        skill_file = self._get_skill_path(agent_name)
        cmd_parts = [
            f"cd {self.config.project_root}",
            "&&",
            "claude", "-p",
            f'"$(cat {prompt_path})"',
            "--output-format", "stream-json",
            "--permission-mode", "bypassPermissions",
        ]
        if skill_file and skill_file.exists():
            cmd_parts.extend([
                "--append-system-prompt-file", str(skill_file)
            ])
        
        # Add session resumption if continuing same spec
        spec_key = f"{handoff.spec_id}:{agent_name}"
        if spec_key in self._session_ids:
            cmd_parts.extend([
                "--resume", self._session_ids[spec_key]
            ])
        
        cmd_parts.extend([
            "2>&1",
            "|", "tee",
            f"{self.config.log_dir}/agent-{agent_name}-{handoff.sequence}.log"
        ])
        
        cmd = " ".join(cmd_parts)
        pane = self.agents[agent_name]
        return pane.send_command(cmd)
    
    def _build_prompt_file(self, agent_name: str, handoff: 'Handoff',
                           handoff_path: Path) -> Path:
        """Write a structured prompt file for the agent."""
        ...
    
    def _get_skill_path(self, agent_name: str) -> Optional[Path]:
        """Get the SKILL.md path for an agent role."""
        ...
    
    def check_completions(self) -> list[str]:
        """Check which agents have completed (pane returned to bash).
        
        Called periodically by daemon. Returns list of agent names
        that just finished.
        """
        completed = []
        for name in list(self._active_pids.keys()):
            if self.is_agent_idle(name):
                completed.append(name)
                del self._active_pids[name]
                # Parse session_id from log output for future resumption
                self._extract_session_id(name)
        return completed
```

**Key design decisions:**
- **Prompt file approach:** Write the handoff context to a temp file rather than inlining in the command. This avoids shell escaping issues with long prompts and special characters.
- **--append-system-prompt-file for roles:** This preserves Claude Code's default system prompt (which loads CLAUDE.md) while adding the agent-specific SKILL.md as additional context. Do NOT use --system-prompt-file, which replaces the default.
- **Session ID tracking:** Optional. For the first iteration, fresh sessions per handoff are fine. Session resumption is an optimisation for Phase 2.
- **Completion detection:** Poll `pane_current_command` — when it returns to "bash", the agent is done. No canary probes needed.

### Phase 2: Daemon Integration (3-4 hours)

**Goal:** Wire HeadlessCommander into daemon.py, replacing the send-keys dispatch path.

**Changes to daemon.py:**

1. **Replace TmuxCommander with HeadlessCommander** in `IWODaemon.__init__()`.

2. **Simplify `process_handoff()` routing (step 9):** Remove the canary probe block. Replace with:
   ```python
   if self.commander.is_agent_idle(target):
       self._activate_for_handoff(target, handoff, path)
   else:
       self.pipeline.enqueue(handoff, path)
   ```

3. **Simplify `_activate_for_handoff()`:** Remove state machine mark_command_sent(). The agent is either idle (bash) or running (claude process). No intermediate states.

4. **Replace `_process_pending_activations()` queue retry:** Remove the 30-second threshold hack, the canary probe retry, the state machine hint override. Replace with a simple idle check:
   ```python
   for name, (handoff, path) in self.pipeline.peek_all_queues():
       if self.commander.is_agent_idle(name):
           self._activate_for_handoff(name, handoff, path)
   ```

5. **Add completion polling:** In the main poll loop, call `self.commander.check_completions()` and release pipeline assignments for completed agents.

6. **Simplify state for TUI dashboard:** Replace the 5-state machine (IDLE/PROCESSING/STUCK/WAITING_HUMAN/CRASHED) with 3 states derived from pane_current_command:
   - IDLE: process is bash
   - RUNNING: process is claude
   - ERROR: pane is dead or process is something unexpected

### Phase 3: Cleanup (2-3 hours)

**Goal:** Remove dead code and simplify.

**Files to modify:**
- `iwo/state.py` — Gut the AgentStateMachine. Replace with a simple 3-state enum derived from pane command. Remove idle_prompt_pattern regex, _check_idle_prompt(), all the prompt-matching logic. (~195 lines → ~30 lines)
- `iwo/commander.py` — Remove send_canary_and_wait(), the idle_pattern matching, the rich prompt construction for send-keys. Keep pane discovery, pipe-pane logging, and Agent 007 launch. Rename to `commander_legacy.py` or merge remaining functionality into headless_commander.py. (~486 lines → ~150 lines)
- `iwo/daemon.py` — Remove all references to the old state machine hints, the 30-second reconciliation of prompt state, the canary fallback logic. (~1197 lines → ~600 lines estimated)
- `iwo/config.py` — Remove: canary_string, canary_timeout_seconds, canary_poll_interval_seconds, idle_prompt_pattern. Add: prompt_file_dir, agent_skill_paths dict.

**Files to delete (or archive):**
- None deleted outright — use git to track the removal.

### Phase 4: Testing and Hardening (2-3 hours)

**New tests:**
- `tests/test_headless_commander.py` — Unit tests for is_agent_idle(), prompt file generation, completion detection.
- `tests/test_headless_integration.py` — Integration test: create a mock handoff file, verify HeadlessCommander launches claude -p correctly in a test tmux pane.
- `tests/test_full_pipeline.py` — End-to-end: Planner → Builder handoff with real claude -p invocation in a test tmux session.

**Hardening:**
- Timeout per agent invocation (configurable, default 10 minutes). If claude -p hasn't exited, send SIGTERM to the process.
- Log file rotation (don't let agent logs grow unbounded).
- Graceful shutdown: on IWO exit, send SIGTERM to all active claude processes.

## 5. What Gets Preserved (Ivan's Workflow Enhancements)

Everything that makes IWF superior to vanilla Boris Cherny Workflow is preserved:

- **Structured JSON handoff protocol** — unchanged, filesystem-based
- **Pipeline tracking** — PipelineManager with multi-spec support
- **Quality gates** — ESLint, Vitest, pre-commit hooks all work the same
- **Deploy approval gate** — human gate for infrastructure changes
- **Rejection loops** — Reviewer → Builder rejection routing unchanged
- **Memory integration** — Qdrant + Neo4j storage of handoffs
- **Webhook notifications** — ntfy/n8n alerts unchanged
- **Auditor** — simplified but retained for anomaly detection
- **TUI dashboard** — updated to show simpler 3-state model
- **Agent 007** — already uses headless pattern, no changes needed
- **Handoff tracker** — idempotency, supersede, rejection loop detection all unchanged
- **Safety rails** — handoff limits, rejection loop limits unchanged

## 6. What Gets Deleted

All interactive-mode workarounds:

- AgentStateMachine 5-state model (IDLE/PROCESSING/STUCK/WAITING_HUMAN/CRASHED)
- Canary probe system (send_canary_and_wait, idle pattern regex)
- Queue retry with 30-second threshold hack
- State machine hint overrides
- Rich prompt construction for send-keys injection
- _check_idle_prompt() and all prompt-matching logic
- Crash recovery/respawn logic (panes don't crash — claude -p exits cleanly)
- Filesystem reconciliation of prompt state (30-second poller)

## 7. Risk Assessment

**Low risk:**
- `claude -p` is the documented, Anthropic-recommended automation interface
- Agent 007 already proves this pattern works within IWO
- CLAUDE.md is automatically loaded — no context loss
- Deterministic idle detection (pane_current_command) vs probabilistic (regex prompt matching)

**Medium risk:**
- Session resumption (`--resume`) may have edge cases — mitigate by making it optional (fresh sessions work fine, just more token usage)
- Long prompts via `$(cat file)` may hit shell argument limits — mitigate by using `claude -p --input-file` if available, or piping stdin

**Mitigations:**
- Phase 0 validation spike catches all unknowns before any production code changes
- Git branching: all work on `feature/headless-dispatch` branch
- Rollback: old commander.py preserved in git history

## 8. Estimated Timeline

| Phase | Effort | Dependency |
|-------|--------|------------|
| Phase 0: Validation Spike | 1-2 hours | None |
| Phase 1: HeadlessCommander | 4-6 hours | Phase 0 success |
| Phase 2: Daemon Integration | 3-4 hours | Phase 1 complete |
| Phase 3: Cleanup | 2-3 hours | Phase 2 tested |
| Phase 4: Testing | 2-3 hours | Phase 3 complete |
| **Total** | **12-18 hours** | |

## 9. Success Criteria

1. One full pipeline run (Planner → Builder → Reviewer → Tester → Deployer → Docs) completes without manual intervention.
2. Each handoff transition completes within 30 seconds of the previous agent writing its handoff file (down from the current "never completes" baseline).
3. No canary probe, state machine, or queue retry code remains in the dispatch path.
4. daemon.py is under 700 lines (down from 1197).
5. All existing tests pass plus new headless-specific tests.

## 10. Decision Record

**ADR: Switch IWO dispatch from interactive send-keys to headless claude -p**

- **Status:** Accepted
- **Context:** 9+ bug fixes addressing interactive dispatch reliability, zero successful full pipeline runs
- **Decision:** Replace tmux send-keys text injection with headless `claude -p` process invocations
- **Consequences:** Eliminates canary/state-machine/queue-retry complexity. Adds dependency on claude CLI headless mode stability. Reduces daemon.py by ~50%.
- **Alternatives considered:** (1) Continue patching interactive dispatch — rejected, diminishing returns. (2) Claude Code Agent Teams — rejected for now, experimental with known limitations, wrong coordination pattern for sequential pipeline. (3) Revert to vanilla Boris Cherny manual workflow — rejected, IWF enhancements are valuable, only the dispatch mechanism is broken.
