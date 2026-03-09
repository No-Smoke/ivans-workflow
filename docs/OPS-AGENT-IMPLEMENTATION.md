# Ops Agent Implementation Plan

**Started:** 2026-03-08
**Rollback tag:** `pre-ops-agent-v1` (commit `4c491be`)
**Rollback command:** `git reset --hard pre-ops-agent-v1`
**Architecture doc:** `docs/ops-agent-architecture-prompt.md`

---

## Phase 1 — `resolve-ops` Directive Handler (CORE)

**Status:** COMPLETE
**Files:** `iwo/config.py`, `iwo/directives.py`

### Tasks

- [x] Create implementation tracking doc
- [x] Add ops agent config fields to `config.py`
- [x] Add `"resolve-ops"` to `DIRECTIVE_TYPES` in `directives.py`
- [x] Implement `_handle_resolve_ops()` handler
- [x] Build prompt generator that embeds skill content + register state
- [x] Implement tiered safety check (auto-approve vs human-gate by category)
- [x] Wire dispatch to Agent 007 via `commander.launch_agent_007()`
- [x] Create desktop launcher script (`scripts/directive-resolve-ops.sh`)
- [x] Add `_ops_gate_pending` attribute to daemon `__init__`
- [x] Integration test (4 scenarios: gate, critical-gate, auto-dispatch, prompt)
- [x] Test with manual directive file drop (live) — **PASS 2026-03-08 session 4**
- [x] Commit + push

### TUI Integration (added Phase 1.5)

- [x] Add `Binding("o", "ops_approve", "Ops Approve")` to TUI BINDINGS
- [x] Implement `action_ops_approve()` method in IWOApp
- [x] Add `#safety-ops-gate` Static widget to SafetyPanel compose
- [x] Add ops gate status update in `_update_safety()` — shows PENDING count + 'o' hint
- [x] Create `scripts/test-ops-agent.sh` for manual validation (directive/gate/auto/proactive tests)
- [x] Commit + push

### Design Notes

Directive format:
```json
{
    "directive": "resolve-ops",
    "filter": "critical|all|[action-ids]",
    "context": "optional guidance"
}
```

Safety tiers:
- Auto-approve: migration, verification, config, other (file placements)
- Human-gate: secret, dns, email_infra, webhook

Prompt includes: skill content (from SKILL.md), pending actions (from register),
safety classification, handoff target (planner).

---

## Phase 2 — Reactive Trigger (Planner blocked → auto-fire)

**Status:** COMPLETE
**Files:** `iwo/daemon.py`

### Tasks

- [x] Detect Planner `outcome: "blocked"` in `process_handoff()` step 8.5
- [x] Check `ops_register.pending_count() > 0`
- [x] Auto-write `resolve-ops` directive via `_schedule_resolve_ops()`
- [x] Guard: skip if ops gate already pending, or directive already queued
- [x] AST + syntax verification
- [ ] Test: simulate Planner blocked handoff (live) — deferred (requires spec in blocked state)
- [x] Commit + push

---

## Phase 3 — Proactive Trigger (critical ops age threshold)

**Status:** COMPLETE
**Files:** `iwo/daemon.py`, `iwo/config.py`

### Tasks

- [x] Add `_check_ops_proactive()` method to daemon
- [x] Call from main poll loop (every 60s)
- [x] Check if critical actions pending > threshold minutes
- [x] Guard: only fire if Agent 007 idle and no resolve-ops in progress
- [x] Syntax + AST verification
- [x] Commit + push

---

## Phase 4 — Ops Handoff Processing

**Status:** COMPLETE
**Files:** `iwo/daemon.py`

### Tasks

- [x] Add step 13 in `process_handoff()` — detect ops-agent handoffs
- [x] Implement `_handle_ops_completion()` — reload register, log summary, notify
- [x] Release Agent 007 tracking after ops completion
- [x] Route ops completion back to Planner (handled by normal routing — target=planner)
- [x] Syntax + AST verification
- [x] Commit + push

---

## Config Audit (Session 3, 2026-03-08)

**Status:** COMPLETE — no runtime bugs remaining

### Findings

1. **DUPLICATE FIELD:** `agent_007_project_root` defined twice in `config.py` (line ~83 and ~101). Second shadows first. Same default value — cosmetic, not a crash risk. **Fix:** remove duplicate at line ~101.

2. **DEAD CODE:** `ops_agent_budget_usd` and `ops_agent_timeout_seconds` defined in `IWOConfig` but never referenced. `launch_agent_007()` uses `agent_007_budget_usd` directly. **Decision:** remove dead fields — ops agent intentionally shares Agent 007's budget/timeout.

3. **AuditorConfig is separate:** All 12 "missing" `self.config.*` references in `auditor.py` belong to `AuditorConfig` (its own dataclass). Not an `IWOConfig` gap.

4. **Future hooks (no action needed):** 10 fields defined but unused — `ops_actions_notify_*`, `agent_007_max_retries`, `agent_timeout_seconds`, `file_debounce_seconds`. Intentional placeholders for future work.

### Config Fix Tasks

- [x] Remove duplicate `agent_007_project_root` at line ~101
- [x] Remove dead `ops_agent_budget_usd` and `ops_agent_timeout_seconds`

### Test Script Bug (found session 3)

- [x] `scripts/test-ops-agent.sh` had `DIRECTIVES_DIR="$IWO_ROOT/.directives"` — wrong path. IWO watches `$EBATT_ROOT/docs/agent-comms/.directives`. Directives dropped to wrong path would be silently ignored. Fixed in commit `9e789ba`.

---

## Phase 5 — P0 Error Propagation + Skip-Archive Retry

**Status:** COMPLETE
**Date:** 2026-03-09
**Commit:** `43dd09c`
**Files:** `iwo/directives.py`, `tests/test_ops_agent.py`
**Source:** Peer review report (P0-1 + P0-2), build prompt at `docs/P0-ERROR-PROPAGATION-BUILDER-PROMPT.md`

### Problem

Two related bugs made dispatch failures invisible:

1. **P0-1 (Error Propagation):** `_handle_resolve_ops()` called `_dispatch_ops_agent()` but never checked its return value. `poll()` only set `failed = True` on exception. Result: dispatch failures silently archived as successes — `FAILED-` prefix never applied.

2. **P0-2 (Silent Directive Loss):** When dispatch failed, the directive was moved to `.processed/` and lost. At-most-once delivery. Ops work requires at-least-once.

### Changes

1. **`AgentDispatchError` exception** — new custom exception raised when dispatch returns `False`
2. **Retry tracking** — `_retry_counts: dict[str, int]` and `_max_directive_retries: int = 5` on `DirectiveProcessor.__init__`
3. **`poll()` restructured** — `AgentDispatchError` caught separately from generic `Exception`:
   - On `AgentDispatchError`: increment retry counter, leave directive in `.directives/` for next poll
   - After max retries (5): archive with `FAILED-` prefix
   - On other exceptions: archive immediately as `FAILED-`
   - On success: archive normally, clear retry counter
4. **`_handle_resolve_ops()` raises on failure** — both the auto-approved subset dispatch and the all-auto-approvable dispatch path now raise `AgentDispatchError` when dispatch returns `False`
5. **`approve_ops_gate()` notifies on failure** — dispatch failure after human gate approval sends notification instead of raising (interactive context, not from `poll()`)

### Pre-Existing Bugs Fixed

Two additional bugs discovered during testing:
- `OpsActionPriority.CRITICAL` → `"critical"` — `Literal` type aliases are not enums, don't support attribute access
- `a.priority.value` → `a.priority` — priority is a plain string, not an enum with `.value`

### Tests

`tests/test_ops_agent.py` — rewritten (previous file was broken: wrong imports, incomplete). 7 tests:
- **TestResolveOpsClassification** (3): filter=all, filter=critical, auto-approve bypass gate
- **TestErrorPropagation** (4): T21 dispatch failure raises AgentDispatchError, T18 archives as FAILED after 5 retries, T19 retries then succeeds, T20 retry counter resets on success

All 7 pass. AST check passes.

### Deviations from Plan

1. Test file is `test_ops_agent.py` not `test_ops_agent_e2e.py` (plan referenced wrong filename)
2. Existing test file was broken and required full rewrite — plan assumed 17 working existing tests
3. Fixed 2 pre-existing Literal/string bugs not in plan scope (discovered during testing, necessary for tests to pass)

---

## Verification Checklist

- [x] Manual `resolve-ops` directive dispatches Ops Agent correctly (T04, T07 dry-run; live session 4)
- [x] Auto-approve categories bypass human gate (T04 dry-run)
- [x] Human-gated categories pause for approval (T05, T06 dry-run; live session 4 — 'o' key)
- [x] Planner blocked → reactive trigger fires (T10 dry-run — directive file creation)
- [x] Proactive trigger fires after threshold (T11 dry-run — aged action detection)
- [x] Ops Agent handoff routes back to Planner (T14 dry-run; live session 4 — received_at stamped)
- [ ] Register is updated correctly after resolution — **FAIL live session 4** (skill defect, not IWO bug)
- [x] Pipeline pauses during ops run (sequential) (T04 dry-run — 007 state=PROCESSING)
- [x] Rollback to `pre-ops-agent-v1` works cleanly (T16 — tag verified)

- [x] Dispatch failure raises AgentDispatchError (T21 — commit `43dd09c`)
- [x] Failed dispatch retries up to 5 times, then archives as FAILED- (T18 — commit `43dd09c`)
- [x] Retry succeeds on second attempt, archives normally (T19 — commit `43dd09c`)
- [x] Retry counter resets after successful archive (T20 — commit `43dd09c`)

**Test harness:** `tests/test_ops_agent.py` — 7 tests, all passing (commit `43dd09c`)
**Live tmux testing:** Session 4, 2026-03-08. See below.

---

## Live Integration Test (Session 4, 2026-03-08)

**Status:** PASS (IWO plumbing) / PARTIAL (agent skill)

### Test: `test-ops-agent.sh directive` (filter: all)

| Step | Expected | Actual | Result |
|------|----------|--------|--------|
| Directive pickup | File consumed, moved to `.processed/` | Moved to `.processed/1772943381-*.json` (109 bytes) | **PASS** |
| Safety classification | Gated categories detected (secret, dns, webhook) | Gate activated, no auto-dispatch | **PASS** |
| Human gate (TUI) | TUI shows PENDING, waits for 'o' | Required 'o' keypress to proceed | **PASS** |
| Prompt generation | Skill + register + context embedded | `ops-agent-1772943447.md` (428 lines) | **PASS** |
| Agent 007 dispatch | Command sent to tmux window 6 | Agent started, 16 turns, 154s runtime | **PASS** |
| Agent work quality | Register updated, handoff written | Handoff correct, register NOT updated (see below) | **PARTIAL** |
| Handoff detection (Phase 4) | `received_at` stamped by daemon | Stamped at `2026-03-08T04:19:59Z` | **PASS** |
| Handoff routing | `nextAgent.target: "planner"` | Correct | **PASS** |

### Agent Skill Defect

Agent 007 correctly classified all 5 actions, resolved ops-seed-016 (CSV verification — confirmed buttons relocated to /batteries), and wrote a valid handoff. However, the register file was NOT updated — the agent rewrote the file (changing `updated_at`) but did not modify any individual action's `status`, `resolved_at`, `resolved_by`, or `notes` fields. All 5 actions remained `pending`.

**Root cause:** The `ops-action-resolver` SKILL.md Step 5 instructs "Desktop Commander `read_file` → mutate in memory → `write_file`" but this is too vague for Claude Code, which doesn't have persistent in-memory state between tool calls. The agent needs an explicit Python script pattern that loads JSON, modifies specific fields, and writes back atomically.

**Fix required:** Update SKILL.md Step 5 with an explicit Python update script template. Retest the pipeline after skill update.

### Handoff Output

```json
{
  "metadata": {"specId": "OPS-RUN-20260308-0417", "agent": "ops-agent", "sequence": 1},
  "status": {"outcome": "partial", "notes": "1 resolved, 4 skipped..."},
  "nextAgent": {"target": "planner", "action": "Re-evaluate build priority..."}
}
```

### Cost

$2.43 USD (Opus, 252K cache creation + 1.4M cache read + 5.5K output tokens)
