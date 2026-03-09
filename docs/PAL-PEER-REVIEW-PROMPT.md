# Multi-Model Peer Review: IWO Ops Agent Architecture

## Instructions for Reviewing Models

You are reviewing the architecture and recent bug fixes for the **Ops Agent subsystem** of IWO (Ivan's Workflow Orchestrator) — a Python daemon that orchestrates a 6-agent AI development pipeline via tmux panes and headless `claude -p` dispatch.

**Your task:** Provide a critical architectural review covering correctness, robustness, failure modes, and design quality. Be direct. Identify real problems. Suggest concrete improvements. Do not pad with praise.

**Output format:** Return a structured review with these sections:
1. **Architecture Assessment** — Is the overall design sound? Rate 1-10 with justification.
2. **Bug Fix Quality** — Are the three fixes correct and complete? Any edge cases missed?
3. **Failure Mode Analysis** — What can still go wrong? Identify the top 3 unaddressed failure modes.
4. **Security & Safety Concerns** — Any risks in the tiered approval model, file-based dispatch, or credential handling?
5. **Concurrency & Race Conditions** — File-based IPC via JSON directives + inotify — what races exist?
6. **Recommendations** — Prioritized list of improvements (P0 = do now, P1 = next sprint, P2 = backlog).

---

## System Context

IWO is a ~4,200-line Python daemon (Textual TUI + watchdog filesystem observer) that:
- Monitors a `docs/agent-comms/` directory for handoff JSON files written by AI agents
- Validates handoffs via Pydantic, routes them to the correct next agent
- Dispatches work by piping markdown prompts into `claude -p` (Anthropic's headless CLI) in tmux panes
- Tracks pipeline state: which specs are active, which agents are busy/idle/crashed
- Provides a TUI dashboard (Textual framework) with real-time status, key bindings for human gates

### Agent Architecture

```
tmux session "claude-agents" — 7 windows:
  Window 0: Planner   (claude -p --model opus)
  Window 1: Builder   (claude -p --model opus)
  Window 2: Reviewer  (claude -p --model opus)
  Window 3: Tester    (claude -p --model sonnet)
  Window 4: Deployer  (claude -p --model sonnet)
  Window 5: Docs      (claude -p --model sonnet)
  Window 6: Agent 007 (claude -p --model opus, supervisory/ops)
```

Pipeline flow: Planner → Builder → Reviewer → Tester → Deployer → Docs. Each agent writes a handoff JSON when done; IWO picks it up and dispatches the next agent.

**Agent 007** is a supervisory agent used for ops resolution. It runs in window 6 and is dispatched by the `resolve-ops` directive handler.

---

## The Ops Agent Subsystem (Under Review)

### Purpose
When the 6-agent pipeline produces infrastructure side effects (D1 migrations to run, R2 buckets to create, wrangler secrets to set, DNS records, browser verifications), they accumulate in an ops register (`.ops-actions.json`, ~85 actions). The ops agent automates resolution of these pending items.

### Directive Flow

```
Trigger sources:
  1. Manual: test-ops-agent.sh drops JSON into .directives/
  2. Reactive: Planner blocked/failed + pending ops → auto-queues resolve-ops
  3. Proactive: _check_ops_proactive() every 60s, fires when critical actions aged > threshold

DirectiveProcessor.poll() (every 2s)
  → picks up resolve-ops directive JSON
  → _handle_resolve_ops(data)
    → loads ops register, filters by mode (all/critical/auto-only)
    → classifies actions into auto-approve vs human-gate categories
    → if any gated: sets _ops_gate_pending, waits for TUI 'o' key
    → if all auto: dispatches immediately
  → _dispatch_ops_agent(actions, context)
    → _build_ops_agent_prompt(actions, context)  [reads SKILL.md, embeds register state]
    → writes prompt to logs/prompts/ops-agent-{ts}.md
    → commander.launch_agent_007(prompt_path)
      → cat prompt.md | claude -p ... | tee log
  → Agent 007 runs, writes handoff JSON when done
  → daemon step 13 detects handoff from agent-007, reloads register
```

### Tiered Safety Model

```python
# Categories that can be resolved without human approval
ops_auto_approve_categories = {"migration", "config", "other"}

# Categories requiring TUI 'o' key press before dispatch
ops_human_gate_categories = {"verification", "secret", "dns", "webhook", "email_infra"}
```

When a `resolve-ops` directive arrives:
- If ALL pending actions are in auto-approve categories → dispatch immediately
- If ANY are in human-gate categories → hold pending, show "press 'o' to approve" in TUI
- Auto-approvable subset can still dispatch immediately while gated ones wait

---

## Three Bugs Fixed (Commit e764d9f)

### Bug 1: Lazy Agent-007 Re-Discovery

**Problem:** `HeadlessCommander.__init__()` does NOT call `connect()` — that happens later in `daemon.setup()`. But if agent-007's tmux window (index 6) didn't exist when `connect()` ran, or if discovery failed silently, `launch_agent_007()` would permanently return `False` with no recovery path.

**Fix:** Added lazy re-discovery to `launch_agent_007()`:

```python
def launch_agent_007(self, prompt_file: Path) -> bool:
    agent = self._agents.get("agent-007")
    if not agent:
        log.info("[agent-007] Not in _agents, attempting re-discovery...")
        self._discover_agent_007()
        agent = self._agents.get("agent-007")
        if not agent:
            log.error(
                "[agent-007] Pane not found even after re-discovery. "
                f"Expected window index {self.config.agent_007_window} "
                f"in session '{self.config.tmux_session_name}'"
            )
            return False
    # ... proceed with dispatch
```

**Question for reviewers:** Is single-retry re-discovery sufficient, or should this be retried with backoff? What if the tmux session exists but window 6 was accidentally closed?

### Bug 2: File-Based Logging

**Problem:** All IWO log output went exclusively to the TUI's RichLog widget (in-memory Textual widget). No file was written. Post-mortem debugging was impossible.

**Fix:** Added `FileHandler` alongside existing `TUILogHandler`:

```python
# In tui.py on_mount():
file_handler = logging.FileHandler(log_dir / "iwo.log")
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-5s %(name)s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
root_logger.addHandler(file_handler)
wd_logger.addHandler(file_handler)  # watchdog logs too
```

**Question for reviewers:** Should the file handler use `RotatingFileHandler` to prevent unbounded log growth? What rotation size/count is appropriate for a daemon that runs 8-12 hours?

### Bug 3: Archive Failure Tracking

**Problem:** `poll()` archived directives in a `finally` block regardless of success or failure. Failed directives (e.g., Agent 007 not found) were indistinguishable from successful ones.

**Fix:** Track failure state and prefix archived filename:

```python
for path in directives:
    failed = False
    try:
        self._process_directive(path)
    except Exception as e:
        log.error(f"Directive processing failed for {path.name}: {e}")
        failed = True
    finally:
        self._archive(path, failed=failed)

def _archive(self, path: Path, failed: bool = False):
    ts = int(time.time())
    prefix = f"{ts}-FAILED-" if failed else f"{ts}-"
    dest = self.processed_dir / f"{prefix}{path.name}"
    shutil.move(str(path), str(dest))
```

**Question for reviewers:** `_dispatch_ops_agent` returns `False` on failure but doesn't raise — so `_handle_resolve_ops` doesn't propagate that failure to the `failed` tracking in `poll()`. Should `_handle_resolve_ops` raise on dispatch failure, or is silent failure acceptable here since the ops agent can be retried?

---

## Key Design Decisions to Review

### 1. File-Based IPC for Directives
Directives are JSON files dropped into `.directives/`, picked up by `poll()` every 2 seconds via directory listing (not inotify — handoffs use inotify but directives use polling). This is intentionally simple but has implications for atomicity and race conditions.

### 2. Single-Agent Ops Resolution
Only Agent 007 handles ops — there's no parallel resolution. If Agent 007 is busy, new resolve-ops directives queue (but get archived, so they're effectively lost). There's no retry mechanism for the queued directive.

### 3. Prompt-as-Stdin Pattern
The ops agent prompt is built as a markdown file containing: the ops-action-resolver skill content (~150 lines), the current register state, safety classifications, and resolution instructions. This is piped via stdin to `claude -p`. The prompt can reach 400+ lines.

### 4. Register Update Pattern
Agent 007 must update `.ops-actions.json` (a single JSON file with ~85 entries). The previous implementation failed because Claude Code agents don't have persistent in-memory state between tool calls — the skill was rewritten to use a single-execution Python heredoc that does read-modify-write atomically. But there's no file locking.

### 5. Proactive Trigger
`_check_ops_proactive()` runs every 60s and fires when any critical ops action has been pending longer than `ops_proactive_threshold_minutes` (default 30 min). This can fire while Agent 007 is already running from a manual trigger — the idle check prevents double-dispatch but the directive still gets archived as processed.

---

## Configuration Reference

```python
@dataclass
class IWOConfig:
    # Ops Agent
    ops_agent_enabled: bool = True
    ops_auto_approve_categories: set[str] = {"migration", "config", "other"}
    ops_human_gate_categories: set[str] = {"verification", "secret", "dns", "webhook", "email_infra"}
    ops_max_actions_per_run: int = 20
    ops_max_minutes_per_run: int = 10
    ops_proactive_threshold_minutes: int = 30
    ops_agent_budget_usd: float = 5.0
    
    # Agent 007
    agent_007_window: int = 6
    agent_007_max_retries: int = 3
    agent_007_timeout_seconds: int = 600
    agent_007_budget_usd: float = 5.0
    agent_007_project_root: Path = Path.home() / "Nextcloud/PROJECTS/ebatt-ai/ebatt"
    
    # Safety
    max_rejection_loops: int = 5
    max_handoffs_per_spec: int = 150
    human_gate_agents: set[str] = {"deployer"}
    auto_approve_safe_deploys: bool = True
    auto_deploy_all: bool = False
    auto_continue_on_completion: bool = False
```

---

## File Inventory

| File | Lines | Role |
|------|-------|------|
| `iwo/daemon.py` | 1,711 | Main daemon: watcher, routing, state, triggers |
| `iwo/directives.py` | 866 | Directive processing: 9 types including resolve-ops |
| `iwo/headless_commander.py` | 729 | tmux pane management, dispatch, idle detection |
| `iwo/tui.py` | 788 | Textual TUI dashboard, key bindings, logging |
| `iwo/config.py` | 150 | Dataclass configuration |
| `iwo/ops_actions.py` | ~400 | Ops register CRUD, fingerprint dedup, stale detection |
| `iwo/auditor.py` | ~900 | Historical: auditor + retry logic (pre-headless) |
| `iwo/commander.py` | ~500 | AgentPane class, legacy interactive dispatch |

---

## Specific Review Questions

1. **Directive loss:** If Agent 007 is busy when resolve-ops fires, the directive archives as "processed" but no work happens. The proactive trigger may re-fire in 60s, but that directive also archives. Is this acceptable, or should we implement a retry queue?

2. **Register atomicity:** `.ops-actions.json` is read-modify-written by Agent 007 (via Python heredoc in a bash shell). The daemon also reads the register for proactive trigger checks. No file locking exists. What's the realistic collision risk and what's the minimal fix?

3. **Category completeness:** The 8 categories (`migration`, `config`, `other`, `verification`, `secret`, `dns`, `webhook`, `email_infra`) were derived empirically from existing ops patterns. Are we missing categories? Is the auto/gate partition correct?

4. **Budget duplication:** `ops_agent_budget_usd` and `agent_007_budget_usd` are both `5.0` and both exist in config. `launch_agent_007` uses `agent_007_budget_usd`. `_dispatch_ops_agent` references `ops_agent_budget_usd` only in the notification message. Should one be removed?

5. **Model selection:** Agent 007 doesn't appear in `AGENT_MODEL_MAP`. The `launch_agent_007` method builds the `claude -p` command without `--model`. What model does it default to? (Answer: whatever `~/.bashrc` sets via `ANTHROPIC_MODEL`, which is currently `opus`.)

6. **Observability:** With file logging now added, what additional observability would you recommend? Structured logging (JSON)? Metrics export? Health endpoint?
