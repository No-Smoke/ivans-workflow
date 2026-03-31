# IWO resolve-bugs — Cheat Sheet

## How to Trigger

| Method | Command / Action |
|--------|-----------------|
| **TUI** | Press **`b`** — drops a directive with filter=all, max=10 |
| **Shell script** | `scripts/directive-resolve-bugs.sh [filter] [max]` |
| **Manual directive** | Drop JSON into `docs/agent-comms/.directives/` |
| **Desktop launcher** | Right-click menu → "IWO: Resolve Bugs" (zenity filter picker) |

## Directive JSON Format

```json
{
    "directive": "resolve-bugs",
    "filter": "all",
    "max_bugs": 10,
    "context": ""
}
```

**Filter options:** `all`, `critical`, `high`, `approved-only`

## Pipeline Flow

```
GitHub Issues (status:approved) → IWO fetches & sorts →
  [Gate if critical/high] → Planner → Builder → Reviewer →
  Tester → Deployer → Docs → label→status:verify → next bug
```

## TUI Key Bindings

| Key | Action |
|-----|--------|
| **`b`** | Queue a new resolve-bugs directive |
| **`B`** | Approve a gated bug (critical/high priority) |

## Human Gate Rules

| Priority | Behavior |
|----------|----------|
| **critical / high** | Pauses — press **`B`** to approve |
| **medium / low** | Auto-dispatches to Planner immediately |

## What Happens on Completion (per bug)

- GitHub label changes: `status:in-progress` → **`status:verify`**
- Summary comment posted on the GitHub Issue
- ntfy notification sent to you
- Next bug in queue auto-advances (or gates if critical/high)

## Prerequisites

- Bugs must be triaged to **`status:approved`** on GitHub **before** IWO sees them
- GitHub PAT available via credential-manager (`bw`)
- `claude-agents` tmux session running
- `bugs_enabled: True` in config (default)

## Spec ID Format

`BUG-FIX-{issue_number}` — e.g., `BUG-FIX-42`

## Single-Sprint Constraint

- Each bug = one pipeline pass (no multi-sprint)
- If Planner decides it's too complex → `outcome: blocked` → recommends creating a proper `EBATT-*` spec instead

## Config Fields (iwo/config.py)

```
bugs_enabled           = True
bugs_github_repo       = "ebatt-ai/ebatt"
bugs_max_per_run       = 10
bugs_label_approved    = "status:approved"
bugs_label_in_progress = "status:in-progress"
bugs_label_verify      = "status:verify"
bugs_auto_approve_priorities = {"medium", "low"}
bugs_human_gate_priorities   = {"critical", "high"}
```

## Troubleshooting

- **"No approved issues found"** → Check GitHub for `status:approved` labels
- **Bug stuck at gate** → Press `B` in TUI to approve
- **Planner not idle** → Another spec is using the Planner; bug queues until it's free
- **PAT retrieval failed** → Run `bw unlock` or check credential-manager
