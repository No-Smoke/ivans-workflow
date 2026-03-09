# Reconstruct IWO Ops Agent Integration (March 3-8 Delta)

## Context

IWO (Ivan's Workflow Orchestrator) source files were recovered from a Borg backup dated March 3, but the ops agent integration code developed March 3-8 was lost. The TUI is running and functional with directives, ops_actions, and all base features. What's missing is the **resolve-ops directive handler** and its integration into the daemon, directives processor, config, and TUI.

The `.pyc` bytecode files in `iwo/__pycache__/` contain the compiled March 8 versions — these are the ground truth for method signatures and structure. A `pycdc` decompiler is built at `/tmp/pycdc/pycdc` but can only partially decompile Python 3.13.

## What Exists (Working)

- `iwo/directives.py` (657 lines) — DirectiveProcessor with 8 directive types: start-spec, next-spec, resume, reconcile, status, pause, unpause, cancel-spec
- `iwo/ops_actions.py` (12,941 bytes) — OpsAction dataclass, OpsActionsRegister class (CRUD, fingerprint dedup, stale detection)
- `iwo/daemon.py` (67,781 bytes) — Full daemon with ops extraction (`_extract_ops_actions`, `_is_ops_action`), agent dispatch, handoff processing
- `iwo/config.py` (5,673 bytes) — Base config with agent_007_* fields
- `iwo/tui.py` (26,384 bytes) — TUI with agents, pipelines, memory, safety, metrics panels. Has `d Deploy Approve`, `D Auto-Deploy`, `a Auto-Continue`, `r Reconcile`, `p Pause`

All imports work. TUI launches and shows all green indicators.

## What's Missing (The March 3-8 Delta)

From bytecode analysis of the advanced `.pyc` files, these specific methods/features need to be added:

### 1. directives.py — Add `resolve-ops` as 9th directive type

**Expected final size:** 33,477 bytes (current: 23,048, delta: ~10,429 bytes)

Add to `DIRECTIVE_TYPES` frozenset: `"resolve-ops"`

Add these methods to `DirectiveProcessor`:

```
def _handle_resolve_ops(self, data)
    # Uses: ops_agent_enabled, ops_register, ops_max_actions_per_run,
    #       ops_auto_approve_categories, ops_human_gate_categories,
    #       _ops_gate_pending, _dispatch_ops_agent
    
def approve_ops_gate(self)
    # Uses: _ops_gate_pending, _dispatch_ops_agent
    # Called by TUI when operator presses 'o' key
    
def _dispatch_ops_agent(self, actions, context)
    # Uses: _build_ops_agent_prompt, launch_agent_007
    # Writes prompt to file, dispatches Agent 007 via headless_commander
    
def _build_ops_agent_prompt(self, actions, context)
    # Uses: ops_auto_approve_categories, ops_human_gate_categories, ops_max_minutes_per_run
    # Builds markdown prompt containing:
    #   - The ops-action-resolver skill content (read from /home/vanya/Nextcloud/skills/personal/custom/ops-action-resolver/SKILL.md)
    #   - Current register state (pending actions filtered by category)
    #   - Resolution instructions
```

**Tiered safety model:**
- `ops_auto_approve_categories`: categories like `migration`, `config`, `other` that can be resolved without human approval
- `ops_human_gate_categories`: categories like `verification`, `secret`, `dns` that need operator 'o' key approval
- When a resolve-ops directive arrives, check if all pending actions are auto-approvable. If yes, dispatch immediately. If any need human gate, set `_ops_gate_pending` and wait for 'o' key.

### 2. daemon.py — Add reactive/proactive triggers and completion handler

**Expected final size:** 74,605 bytes (current: 67,781, delta: ~6,824 bytes)

Add to `IWODaemon.__init__`:
- `self._ops_gate_pending = None` — holds pending ops dispatch data when waiting for human approval
- Import and instantiate `DirectiveProcessor` and `OpsActionsRegister` (may already be there — verify)

Add these methods to `IWODaemon`:

```
def _schedule_resolve_ops(self, context)
    # Uses: ops_agent_enabled, _ops_gate_pending
    # Creates a resolve-ops directive programmatically (not from file)
    # Called reactively when Planner is blocked or proactively on timer
    
def _check_ops_proactive(self)
    # Uses: ops_agent_enabled, _ops_gate_pending, ops_proactive_threshold_minutes,
    #       ops_register, _schedule_resolve_ops
    # Called every 60s in run_loop
    # Fires when critical ops actions have been pending > threshold minutes
    
def _handle_ops_completion(self, handoff)
    # Uses: ops_register
    # Called in process_handoff step 13 when handoff comes from Agent 007
    # Reloads register, logs summary of resolved/skipped/failed actions
```

Modify `process_handoff`:
- After existing processing, add step 13: if handoff is from Agent 007 (ops agent), call `_handle_ops_completion`
- After existing step where Planner blocks/fails, call `_schedule_resolve_ops` reactively

Modify `run_loop`:
- Add periodic call to `_check_ops_proactive()` every 60 seconds

Modify `setup`:
- Initialize `directive_processor` if not already done
- Check for `.directives/` directory

### 3. config.py — Add ops agent config fields

**Expected final size:** 6,264 bytes (current: 5,673, delta: ~591 bytes)

Add to `IWOConfig` dataclass:

```python
# --- Ops Agent (resolve-ops via Agent 007) ---
ops_agent_enabled: bool = True
ops_auto_approve_categories: set[str] = field(default_factory=lambda: {"migration", "config", "other"})
ops_human_gate_categories: set[str] = field(default_factory=lambda: {"verification", "secret", "dns", "webhook", "email_infra"})
ops_max_actions_per_run: int = 20
ops_max_minutes_per_run: int = 10
ops_proactive_threshold_minutes: int = 30
ops_agent_budget_usd: float = 5.0
```

### 4. tui.py — Add 'o' key binding for ops gate approval

**Expected final size:** 24,700 bytes (current: 26,384 — current is actually LARGER, but missing the 'o' binding)

Add to `IWOApp.BINDINGS`:
```python
Binding("o", "ops_approve", "Ops Approve"),
```

Add method:
```python
def action_ops_approve(self) -> None:
    """Approve pending ops agent dispatch (human gate)."""
    if self.daemon and self.daemon.directive_processor:
        self.daemon.directive_processor.approve_ops_gate()
```

Add to `_update_safety` method:
- Show ops gate status: "Ops gate: PENDING (press 'o')" or "Ops gate: —"

## Implementation Notes

### The ops-action-resolver skill path
The prompt built by `_build_ops_agent_prompt` should read and embed the content of:
`/home/vanya/Nextcloud/skills/personal/custom/ops-action-resolver/SKILL.md`

This skill was recently updated (Step 5 rewritten to use Python heredoc pattern for register updates). The prompt should tell Agent 007 to follow this skill.

### Agent 007 dispatch mechanism
`headless_commander.launch_agent_007(prompt_file)` already exists and works. It:
1. Writes the prompt to a temp `.md` file in `logs/prompts/`
2. Sends `claude -p <prompt_file>` to tmux pane 6 (Agent 007's window)
3. Sets Agent 007 as active

### Directive file format for resolve-ops
```json
{
    "directive": "resolve-ops",
    "filter": "all",
    "context": "Manual trigger from desktop launcher"
}
```
Optional fields: `filter` (all/critical/category:xxx), `context` (human-readable reason)

### Test script
A `scripts/test-ops-agent.sh` was referenced but never created. Consider creating it:
```bash
#!/bin/bash
# Drop a resolve-ops directive for testing
TIMESTAMP=$(date +%s)
DIR="/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/docs/agent-comms/.directives"
cat > "$DIR/resolve-ops-test-$TIMESTAMP.json" << EOF
{
    "directive": "resolve-ops",
    "filter": "all",
    "context": "Manual test from test-ops-agent.sh"
}
EOF
echo "Dropped resolve-ops directive: $DIR/resolve-ops-test-$TIMESTAMP.json"
```

## Verification

After implementing, test:
1. `from iwo.directives import DirectiveProcessor` — should import without error
2. `grep -c "resolve-ops" iwo/directives.py` — should be > 0
3. `grep -c "_check_ops_proactive\|_schedule_resolve_ops\|_handle_ops_completion" iwo/daemon.py` — should be > 0
4. `grep -c "ops_agent_enabled\|ops_auto_approve" iwo/config.py` — should be > 0
5. Drop a test directive and verify Agent 007 dispatches
6. Commit ALL changes to `feature/headless-dispatch` via the naughty-bohr worktree

## Bytecode Reference

The `.pyc` files at `iwo/__pycache__/*.cpython-313.pyc` are the compiled March 8 versions. Use `pycdc` (built at `/tmp/pycdc/pycdc`) for partial decompilation:
```bash
/tmp/pycdc/pycdc iwo/__pycache__/directives.cpython-313.pyc
/tmp/pycdc/pycdc iwo/__pycache__/daemon.cpython-313.pyc
```
These give imports, constants, class names, and variable declarations but NOT function bodies (Python 3.13 MAKE_FUNCTION opcode unsupported).

For detailed method signatures and internal names, use:
```python
python3 -c "
import marshal, types
with open('iwo/__pycache__/directives.cpython-313.pyc','rb') as f:
    f.read(16); code = marshal.load(f)
for c in code.co_consts:
    if isinstance(c, types.CodeType):
        for m in c.co_consts:
            if isinstance(m, types.CodeType):
                print(f'{c.co_name}.{m.co_name}: names={list(m.co_names)[:15]}, vars={list(m.co_varnames)[:10]}')
"
```

## Key Files

| File | Path |
|------|------|
| directives.py (to modify) | `/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo/directives.py` |
| daemon.py (to modify) | `.../iwo/daemon.py` |
| config.py (to modify) | `.../iwo/config.py` |
| tui.py (to modify) | `.../iwo/tui.py` |
| ops_actions.py (reference) | `.../iwo/ops_actions.py` |
| headless_commander.py (reference) | `.../iwo/headless_commander.py` |
| ops-action-resolver SKILL.md | `/home/vanya/Nextcloud/skills/personal/custom/ops-action-resolver/SKILL.md` |
| OPS-AGENT-IMPLEMENTATION.md | `.../docs/OPS-AGENT-IMPLEMENTATION.md` |
| Git worktree for commits | `.../ivans-workflow-orchestrator/.claude/worktrees/naughty-bohr` |
| .pyc bytecode (ground truth) | `.../iwo/__pycache__/*.cpython-313.pyc` |

## Git Workflow

After implementing, commit via the worktree:
```bash
cd /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/.claude/worktrees/naughty-bohr
cp /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo/{directives,daemon,config,tui}.py iwo/
git add iwo/
git commit -m "feat: reconstruct resolve-ops directive handler and ops agent integration"
git remote set-url origin git@github.com:No-Smoke/ivans-workflow.git
git push origin feature/headless-dispatch
git remote set-url origin https://github.com/No-Smoke/ivans-workflow.git
```

**Project:** IWO (Ivan's Workflow Orchestrator)
