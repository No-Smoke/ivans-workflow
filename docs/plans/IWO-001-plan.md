# IWO-001 Implementation Plan — resolve-bugs Directive

## Overview

Add a `resolve-bugs` directive that fetches `status:approved` GitHub Issues from `ebatt-ai/ebatt`, wraps each as a `BUG-FIX-{N}` mini-spec, and routes them through the 6-agent pipeline (Planner → Builder → Reviewer → Tester → Deployer → Docs). Mirrors the `resolve-ops` pattern exactly: directive → gate → dispatch → completion loop.

---

## 1. Exact Code Changes Per File

### 1.1 `iwo/config.py`

**Add after the Ops Agent section (~line 194):**

```python
# ─── Bug Fix Pipeline ─────────────────────────────────────
bugs_enabled: bool = True
bugs_github_repo: str = field(default_factory=lambda: _env(
    "IWO_BUGS_GITHUB_REPO", "ebatt-ai/ebatt"
))
bugs_auto_approve_priorities: set[str] = field(
    default_factory=lambda: {"medium", "low"}
)
bugs_human_gate_priorities: set[str] = field(
    default_factory=lambda: {"critical", "high"}
)
bugs_max_per_run: int = 10
bugs_label_approved: str = "status:approved"
bugs_label_in_progress: str = "status:in-progress"
bugs_label_verify: str = "status:verify"
```

8 new fields, all with defaults. No breaking changes.

---

### 1.2 `iwo/directives.py`

**Change 1: Add to `DIRECTIVE_TYPES` frozenset (line 27):**
```python
"resolve-bugs",
```

**Change 2: Add state fields to `DirectiveProcessor.__init__` (after `_max_directive_retries`):**
```python
# Bug fix pipeline state
self._bug_queue: list[dict] = []
self._bug_gate_pending: Optional[tuple] = None  # (bug_dict, context_str)
self._bugs_processed_count: int = 0
self._bugs_github_token: Optional[str] = None
```

**Change 3: Add 5 new methods after the resolve-ops section (~line 912):**

1. `_handle_resolve_bugs(data: dict)` — Main handler. Validates config, fetches PAT, calls GitHub API, builds queue, dispatches first bug. ~60 lines. Pattern: mirrors `_handle_resolve_ops` structure.

2. `_fetch_approved_issues(token: str, repo: str, label: str) -> list[dict]` — HTTP GET to `api.github.com/repos/{repo}/issues` with label filter. Parses priority/category from labels. Returns sorted list. ~40 lines.

3. `_dispatch_bug_to_planner(bug: dict, context: str)` — Creates `BUG-FIX-{N}` directory in agent-comms, builds Planner prompt, writes prompt file, creates synthetic handoff, dispatches via `commander.activate_agent("planner", ...)`. Updates GitHub label to `status:in-progress`. ~50 lines. Pattern: mirrors `_handle_start_spec` dispatch.

4. `_build_bug_planner_prompt(bug: dict, context: str) -> str` — Builds the untrusted-input-framed Planner prompt per spec section 3.1. ~50 lines.

5. `approve_bug_gate()` — Called by TUI `B` key. Checks `_bug_gate_pending`, dispatches, clears state. ~15 lines. Pattern: mirrors `approve_ops_gate`.

6. `_advance_bug_queue()` — Pops next bug from `_bug_queue`, checks gate, dispatches or gates. Called by daemon completion handler. ~25 lines.

**Estimated: ~240 new lines in directives.py.**

---

### 1.3 `iwo/daemon.py`

**Change 1: Add step 15 in `process_handoff()` after step 14 (~line 1154):**

```python
# 15. Bug-fix pipeline completion
spec_id = handoff.spec_id
if spec_id.startswith("BUG-FIX-") and target in ("human", "none"):
    try:
        self._handle_bug_completion(handoff)
    except Exception as e:
        log.warning(f"Bug completion handler failed (non-fatal): {e}")
```

**Change 2: Add `_handle_bug_completion(handoff)` method (~30 lines):**
1. Extract issue number from `BUG-FIX-{N}` spec ID
2. Get GitHub PAT via credential-manager subprocess
3. GitHub API: remove `status:in-progress` label, add `status:verify` label
4. GitHub API: post summary comment on the issue
5. Send ntfy notification
6. Call `self.directive_processor._advance_bug_queue()`

**Change 3: Add `_github_api_request(method, url, token, data=None)` helper (~20 lines):**
Shared HTTP helper using `subprocess.run(["curl", ...])` or `httpx` (check what's available). Keeps GitHub API calls DRY between directives.py and daemon.py.

**Decision: Use `urllib.request` (stdlib)** — avoids adding httpx dependency for 3 API calls. The ops agent already shells out to `curl` in prompts, but Python-native is cleaner for daemon code.

**Estimated: ~80 new lines in daemon.py.**

---

### 1.4 `iwo/tui.py`

**Change 1: Add key bindings to `IWOApp.BINDINGS` (line 265):**
```python
Binding("B", "bug_approve", "Bug Approve", priority=True),
Binding("b", "resolve_bugs", "Resolve Bugs", priority=True),
```

**Change 2: Add `action_bug_approve()` method (~15 lines):**
Pattern: mirrors `action_ops_approve()`. Checks `directive_processor._bug_gate_pending`, calls `approve_bug_gate()`, logs to RichLog.

**Change 3: Add `action_resolve_bugs()` method (~20 lines):**
Writes a `resolve-bugs` directive JSON file to `.directives/` directory. Same as running the shell script from within the TUI.

**Change 4: Add bug gate row to SafetyPanel compose (~line 185):**
```python
yield Static("", id="safety-bug-gate", classes="safety-row")
```

**Change 5: Update `_update_safety()` to show bug gate status (~10 lines):**
```python
# Bug gate status
bug_gate = self.daemon.directive_processor._bug_gate_pending
if bug_gate:
    bug, _ = bug_gate
    self.query_one("#safety-bug-gate", Static).update(
        f" Bug gate: [bold magenta]PENDING (#{bug['number']})[/] — press 'B'"
    )
else:
    queue_len = len(self.daemon.directive_processor._bug_queue)
    if queue_len:
        self.query_one("#safety-bug-gate", Static).update(
            f" Bugs: [yellow]{queue_len} queued[/]"
        )
    else:
        self.query_one("#safety-bug-gate", Static).update(
            " Bug gate: [dim]—[/]"
        )
```

**Estimated: ~60 new lines in tui.py.**

---

### 1.5 `scripts/directive-resolve-bugs.sh` (NEW)

Shell script following exact pattern of `directive-resolve-ops.sh`:
- Loads `.env` for `IWO_PROJECT_ROOT`
- Writes directive JSON to `.directives/`
- Optional zenity dialog for filter selection
- notify-send confirmation

~45 lines.

---

### 1.6 `tests/test_resolve_bugs.py` (NEW)

Unit tests following `test_ops_agent.py` patterns:
- `test_fetch_approved_issues()` — Mock urllib response, verify filtering/sorting
- `test_priority_gate_critical_gated()` — Critical bugs are gated
- `test_priority_gate_medium_auto()` — Medium bugs auto-dispatch
- `test_planner_prompt_security_framing()` — Verify UNTRUSTED markers present
- `test_bug_completion_label_update()` — Mock GitHub API, verify label swap
- `test_queue_advancement()` — After completion, next bug dispatches
- `test_blocked_planner_handling()` — Planner returns blocked → bug skipped
- `test_empty_queue()` — No approved issues → notification, no dispatch
- `test_disabled_config()` — `bugs_enabled=False` → early return

~200 lines.

---

## 2. Execution Order

1. `iwo/config.py` — Config fields (no dependencies)
2. `iwo/directives.py` — Core handler + all methods
3. `iwo/daemon.py` — Completion detection hook
4. `iwo/tui.py` — Key bindings and safety panel
5. `scripts/directive-resolve-bugs.sh` — Shell script
6. `tests/test_resolve_bugs.py` — Unit tests

Each file verified with `python3 -c "import ast; ast.parse(open('path').read())"` after edit.

---

## 3. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Prompt injection via bug descriptions | HIGH | Five-layer defense from spec. Untrusted-input markers in every Planner prompt. Planner skill has honesty protocol. Reviewer provides second check. |
| GitHub PAT retrieval failure in headless env | MEDIUM | Use same credential-manager subprocess pattern as ops agent. Fail fast with clear error if token unavailable. |
| `urllib.request` SSL issues on some systems | LOW | Python 3.11+ has reliable SSL. Fallback: shell out to `curl` if needed. |
| Step 15 placement in process_handoff | LOW | Added after existing step 14. Non-fatal try/except wrapper. Only triggers for `BUG-FIX-*` spec IDs — zero impact on existing pipelines. |
| Concurrent EBATT-* spec + bug pipeline | MEDIUM | Spec addresses this in Q5 — PipelineManager handles multi-spec queuing. No new code needed. Bug specs queue behind active specs. |
| GitHub API rate limiting | LOW | PAT provides 5000 req/hr. A run of 10 bugs needs ~30 API calls. |
| TUI crash from new widget | LOW | All widget updates wrapped in try/except (existing pattern). |

---

## 4. Test Plan

### Unit Tests (automated, `tests/test_resolve_bugs.py`)
- GitHub issue fetch + filter + sort
- Priority gate (gated vs auto-dispatch)
- Planner prompt security framing
- Bug completion label updates
- Queue advancement
- Blocked Planner handling
- Empty queue handling
- Config disabled check

### Integration (manual, single pass)
- Drop `resolve-bugs` directive file
- Verify Planner dispatched with correct prompt
- Verify `BUG-FIX-{N}` directory created
- Verify GitHub label changes on completion

### E2E (manual, post-deploy)
- Create test GitHub Issue on `ebatt-ai/ebatt`
- Triage as `status:approved`
- Run full pipeline
- Verify label → `status:verify` and comment posted

---

## 5. Spec Feedback / Adjustments

### Agreed as-is
- Five-layer security model
- Single-sprint constraint
- Spec ID format `BUG-FIX-{N}`
- Human gate for critical/high, auto for medium/low
- Auto-advance after medium/low completion

### Minor adjustments proposed

1. **GitHub API approach:** Spec doesn't specify HTTP library. I'll use `urllib.request` (stdlib) rather than adding `httpx` as a dependency. Three API calls don't justify a new dependency.

2. **Token retrieval:** The spec says "retrieve GitHub PAT via credential-manager" once at start of `_handle_resolve_bugs`. I'll pass it through to sub-methods as a parameter (matching spec Q6), but also store it in `self._bugs_github_token` so the daemon's `_handle_bug_completion` can access it for label updates. The token is short-lived within a single directive run.

3. **StatusBar enhancement:** The spec requests `Bugs: 3 queued | #42 in-progress (Builder) | 2 verified`. The "in-progress (Builder)" and "verified" counts require additional tracking state that goes beyond Phase 1 scope. I'll implement the queue count and gate status in the SafetyPanel (consistent with ops gate display) and defer the detailed status bar enhancement to a follow-up.

4. **Desktop launcher file** (`~/.local/share/applications/iwo-resolve-bugs.desktop`): Deferred — not in the critical path and machine-specific. The shell script is sufficient for now.

5. **`_advance_bug_queue` gate on completion:** The spec Q4 recommends waiting for human confirmation after high/critical bug completion. I'll implement this: after a gated-priority bug completes, the next bug dispatches only after `B` key press (same gate mechanism). Medium/low completions auto-advance.

6. **Integration test file:** Spec mentions `tests/test_bugs_integration.py`. I'll fold integration-style tests into `test_resolve_bugs.py` to avoid file proliferation — the mock patterns are the same.

---

## 6. Dependency Analysis

### No new Python dependencies
- `urllib.request` — stdlib
- `json` — stdlib
- All other imports already in use

### Existing dependencies confirmed present
- `pydantic` — for Handoff model
- `watchdog` — for file detection
- `textual` — for TUI
- `pytest` — for tests

---

## 7. Files Summary

| File | Action | Est. Lines |
|------|--------|-----------|
| `iwo/config.py` | Modify | +10 |
| `iwo/directives.py` | Modify | +240 |
| `iwo/daemon.py` | Modify | +80 |
| `iwo/tui.py` | Modify | +60 |
| `scripts/directive-resolve-bugs.sh` | Create | ~45 |
| `tests/test_resolve_bugs.py` | Create | ~200 |
| **Total** | | **~635 lines** |
