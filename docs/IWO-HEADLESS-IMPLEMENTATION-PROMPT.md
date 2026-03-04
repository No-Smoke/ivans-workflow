# IWO Headless Dispatch Migration — Implementation Prompt

## Context

You are working on Ivan's Workflow Orchestrator (IWO), a Python daemon (~1200 lines in daemon.py) that automates handoffs between 6 Claude Code agents in tmux. The current dispatch mechanism — injecting text into interactive Claude Code sessions via tmux send-keys — has proven unreliable despite 9+ bug fixes. Not one full pipeline run has completed without manual intervention.

A detailed migration plan has been written and approved. Read it first:

```
cat /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/docs/IWO-HEADLESS-DISPATCH-PLAN.md
```

The plan replaces send-keys dispatch with headless `claude -p` process invocations — the documented, Anthropic-recommended approach for automating Claude Code. Each agent tmux pane starts as idle bash. When IWO detects a handoff, it launches `claude -p` in the target pane. When the process exits, the pane returns to idle.

## Your Task

Implement the plan in order: Phase 0 → Phase 1 → Phase 2 → Phase 3 → Phase 4.

**Do not skip Phase 0.** The validation spike must succeed before you touch IWO's production code. If any Phase 0 test fails, stop and report findings — the plan may need adjustment.

## Key Repository Paths

- **IWO repo:** `/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/`
- **eBatt repo (agent home):** `/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/`
- **IWO daemon:** `iwo/daemon.py` (1197 lines — target: under 700)
- **IWO commander:** `iwo/commander.py` (486 lines — target: under 150, or replaced)
- **IWO state machine:** `iwo/state.py` (195 lines — target: under 30)
- **IWO config:** `iwo/config.py` (148 lines)
- **IWO pipeline:** `iwo/pipeline.py` (341 lines — keep as-is)
- **Agent skills:** `$EBATT_ROOT/.claude/skills/{agent-name}/SKILL.md`
- **Agent handoffs:** `$EBATT_ROOT/docs/agent-comms/{SPEC-ID}/`
- **CLAUDE.md:** `$EBATT_ROOT/CLAUDE.md` (loaded automatically by claude -p)
- **Existing tests:** `tests/`
- **Migration plan:** `docs/IWO-HEADLESS-DISPATCH-PLAN.md`

## Critical Technical Details

### Claude Code Headless Invocation
```bash
# Basic headless execution (loads CLAUDE.md from cwd automatically)
claude -p "Your prompt here" --output-format json

# With role injection (appends to default system prompt, preserving CLAUDE.md)
claude -p "Your prompt" --append-system-prompt-file ./path/to/SKILL.md --output-format json

# Session resumption for multi-turn work
session_id=$(claude -p "First task" --output-format json | jq -r '.session_id')
claude --resume "$session_id" -p "Continue" --output-format json

# Full agent launch command pattern
cd /home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt && \
claude -p "$(cat /tmp/iwo-prompt-builder-12.md)" \
  --output-format stream-json \
  --permission-mode bypassPermissions \
  --append-system-prompt-file .claude/skills/builder/SKILL.md \
  2>&1 | tee /path/to/logs/agent-builder-12.log
```

### Idle Detection (Replaces Canary Probes)
```python
# Deterministic — no regex, no timing, no false positives
import libtmux
pane = ...  # libtmux.Pane
current_command = pane.pane_current_command
is_idle = current_command in ("bash", "zsh", "sh", "fish")
```

### Prompt File Structure
Write structured prompt files rather than inlining prompts in shell commands:
```markdown
# Agent Activation: Builder

## Handoff Details
- **Spec:** EBATT-006A
- **Sequence:** #012
- **From:** planner
- **Handoff file:** docs/agent-comms/EBATT-006A/012-planner-to-builder.json

## Your Task
{action from handoff.nextAgent.action}

## Instructions
1. Read the handoff file above for full context
2. Execute /workflow-next to activate your role  
3. Do your work following your SKILL.md guidelines
4. When finished, write your handoff JSON to docs/agent-comms/EBATT-006A/
5. Follow the handoff naming convention: {next_sequence}-{your_role}-to-{next_agent}.json
```

## Rules

1. **Read the migration plan first** — it has detailed specifications for each phase.
2. **Phase 0 must pass before proceeding.** Create `tests/spike_headless.py` and run all 6 validation tests.
3. **Use Desktop Commander for all file operations** under `/home/vanya/`.
4. **Run tests after every code change:** `python3 -m pytest tests/ -v`
5. **Run syntax checks:** `python3 -m py_compile iwo/{file}.py`
6. **Git branch:** Work on `feature/headless-dispatch`. Commit after each phase with detailed messages.
7. **Preserve all IWF pipeline logic** — PipelineManager, HandoffTracker, deploy gates, rejection loops, webhook notifications, memory integration, auditor. Only the dispatch mechanism changes.
8. **Do not delete the old commander.py** — rename to `commander_legacy.py` for rollback safety.
9. **Honesty Protocol:** Report exact test results. If something doesn't work, say so. No claims without verification.

## What Success Looks Like

After implementation, start IWO and the agents. Give the Planner a simple spec to work on. Watch IWO detect the Planner's handoff, launch `claude -p` in the Builder's pane, and see the Builder complete its work and write its own handoff — all without manual intervention. Then watch the chain continue through Reviewer → Tester → Deployer → Docs.

Each handoff transition should complete within 30 seconds. The TUI should show agents cycling between IDLE and RUNNING states cleanly. No stuck queues, no false PROCESSING, no canary failures, no silent command drops.

## Phase 0 Quick Reference

Create `tests/spike_headless.py` with these tests:

1. `test_basic_invocation()` — `claude -p "echo test" --output-format json` from eBatt directory. Verify JSON output with session_id.
2. `test_skill_injection()` — `claude -p "What is your role?" --append-system-prompt-file .claude/skills/builder/SKILL.md --output-format json`. Verify role awareness.
3. `test_session_resume()` — Start session, capture session_id, resume with context query. Verify continuity.
4. `test_tmux_pane_launch()` — Launch claude -p in a test tmux pane. Verify process starts and exits.
5. `test_idle_detection()` — After process exits, verify pane_current_command shows "bash".
6. `test_error_handling()` — Invalid invocation. Document exit codes and stderr.

Run each test, document results, then proceed to Phase 1 if all pass.
