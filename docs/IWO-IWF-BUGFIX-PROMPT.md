# IWO/IWF Bug-Fixing Session Prompt

Copy everything below the line into a fresh chat in the eBatt.ai Claude Project.

---

## Task: Debug and fix an IWO/IWF issue

IWO (Ivan's Workflow Orchestrator) is a Python daemon that orchestrates 6 Claude Code agents in tmux via headless `claude -p` dispatch. IWF (Ivan's Workflow) is the agent framework. I need you to diagnose and fix a specific issue.

**The bug:** [DESCRIBE THE SYMPTOM HERE — e.g. "Builder dispatched twice for the same spec" or "Reviewer handoff not detected" or "TUI shows IDLE but agent is running"]

### System Overview

- **IWO:** Python daemon (~3,300 lines across 12 modules), headless dispatch via `claude -p`
- **IWF:** 6 agents (Planner→Builder→Reviewer→Tester→Deployer→Docs) in tmux panes
- **Dispatch model:** All panes start as idle bash. IWO detects handoff JSON via inotify, validates with Pydantic, checks pane is idle (pane_current_command + child process check via pgrep), launches `claude -p --model {opus|sonnet}` with handoff context piped to stdin.
- **Agent models:** Planner/Builder/Reviewer=Opus, Tester/Deployer/Docs=Sonnet (AGENT_MODEL_MAP)

### Repos and Key Paths

| Location | Purpose |
|----------|---------|
| `/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/` | IWO daemon, TUI, tools |
| `/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/` | eBatt project (agents, handoffs, specs) |
| `iwo/headless_commander.py` (~719 lines) | Dispatch, idle detection, completion checking |
| `iwo/daemon.py` | Watchdog handler, poll loop, state derivation |
| `iwo/parser.py` | Pydantic handoff validation |
| `iwo/pipeline.py` | PipelineManager, multi-spec tracking |
| `iwo/directives.py` | Desktop launcher directive processing |
| `iwo/tui.py` | Textual TUI dashboard |
| `iwo/config.py` | IWOConfig defaults |
| `docs/ARCHITECTURE.md` | Full architecture guide |
| `docs/CHANGELOG-FIXES.md` | All bug fixes chronologically (read this FIRST) |
| `docs/IWO-TUI-Manual.md` | TUI controls and safety rails |
| `ebatt/docs/agent-comms/{SPEC-ID}/` | Handoff JSON files per spec |
| `ebatt/.claude/skills/boris-{role}-agent/SKILL.md` | Agent role definitions |
| `ebatt/.claude/skills/workflow-handoff/HANDOFF-SCHEMA.md` | Handoff JSON schema |

### Debugging Methodology


**ALWAYS read these files before proposing any fix:**

1. `docs/CHANGELOG-FIXES.md` — all prior fixes with root causes. Do not re-introduce fixed bugs.
2. `docs/ARCHITECTURE.md` — system architecture and component diagram.
3. The specific module(s) implicated by the symptom.

**Diagnosis steps:**

1. **Did IWO see the file?** Check `received_at` field in the handoff JSON. Present = watchdog fired.
2. **Is LATEST.json correct?** Compare symlink target vs highest-numbered JSON in the spec directory.
3. **Is the target pane idle?** Run: `tmux list-windows -t claude-agents -F '#{window_index}: #{window_name} | #{pane_current_command}'` and check for child processes: `pgrep -P $(tmux display -t claude-agents:{N} -p '#{pane_pid}')`.
4. **Was dispatch attempted?** Check IWO TUI log panel or `logs/agent-{name}-{seq}.log` existence.
5. **Did the agent run?** Check `ps aux | grep 'claude -p'` for active processes.
6. **Pipeline state?** Read `docs/agent-comms/.active-specs.json` for pipeline tracking state.

### Known Fixed Bugs (Do NOT Re-Introduce)

Fixes 1-13 are documented in `docs/CHANGELOG-FIXES.md`. Key ones:

- **Fix 11:** Stripped interactive dispatch — headless only. No send-keys, no canary probes.
- **Fix 12:** Added `--model` flag and AGENT_MODEL_MAP. Without it, all agents default to Haiku.
- **Fix 13:** Idle detection now checks child processes via `pgrep -P $pane_pid`. Without this, `pane_current_command` reports "bash" while `claude -p` runs as a child, causing double-dispatch and premature completion detection.

### Rules

- Use **Desktop Commander** for ALL file operations under `/home/vanya/`. Never use container tools (view, bash_tool, str_replace) for local files.
- Read CHANGELOG-FIXES.md and ARCHITECTURE.md BEFORE proposing changes.
- Run `python3 -m py_compile iwo/{file}.py` after any code change.
- Run `python3 -m pytest tests/ -v` if tests exist for the affected module.
- Commit with detailed message explaining root cause and fix, following the pattern in CHANGELOG-FIXES.md.
- **Honesty Protocol:** Report exact evidence. No claims without verification. If you cannot reproduce the bug, say so.
- After fixing, verify with `get_file_info` that the file was written (size > 0, mtime updated).
- Query `qdrant-new:semantic_search collection='project_memory_v2'` for related context if the bug involves a recurring pattern.

### Reproduction

1. Start IWF: Click "Ivan's Workflow" desktop launcher (or `cd ebatt && ./scripts/boris-workflow/launch-tmux-agents-v5.sh`)
2. Start IWO: Click "IWO" desktop launcher (or `cd ivans-workflow-orchestrator && source .venv/bin/activate && python -m iwo.tui`)
3. Trigger work: "Plan Next Spec" desktop action, or manually dispatch via directive
4. Monitor: IWO TUI dashboard, Kanban at http://localhost:8787, and `docs/agent-comms/{SPEC}/` for new handoff files

### Success Criteria

The specific bug described above is fixed, verified by evidence, committed with a descriptive message, and does not regress any of the 13 prior fixes.
