# P0-1 + P0-2: Error Propagation & Skip-Archive Retry

**Priority:** P0 — fix before next ops agent dispatch
**Target repo:** IWO — `/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/`
**Files to edit:**
  - `/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo/directives.py`
**Tests to edit:**
  - `/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/tests/test_ops_agent_e2e.py`
**Context:** Peer review report at `/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/docs/OPS-AGENT-PEER-REVIEW-REPORT.md`, gap analysis conducted 2026-03-09

**IMPORTANT:** This task modifies IWO, not eBatt. All file paths are absolute. Do NOT look for these files relative to the eBatt project root.

---

## Problem Statement

Two related bugs in `DirectiveProcessor` (in `/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo/directives.py`) make dispatch failures invisible:

**Bug P0-1 (Error Propagation):** `_handle_resolve_ops()` calls `_dispatch_ops_agent()` which returns `False` on failure (agent busy, pane not found). But `_handle_resolve_ops()` never checks this return value and doesn't raise an exception. The `poll()` method only sets `failed = True` when an exception is caught. Result: dispatch failures are silently archived as successes — the `FAILED-` prefix is never applied.

**Bug P0-2 (Silent Directive Loss):** When dispatch fails, the directive is moved to `.processed/` and lost. This is at-most-once delivery. Ops work requires at-least-once. The directive should stay in `.directives/` for the next 2s poll to retry, with a max-retry counter to prevent infinite loops.

---

## Implementation Plan

### Step 1: Define AgentDispatchError

In `/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo/directives.py`, after the imports (after `log = logging.getLogger("iwo.directives")`), add:

```python
class AgentDispatchError(Exception):
    """Raised when Agent 007 dispatch fails (busy, not found, etc.)."""
    pass
```

### Step 2: Raise on dispatch failure in _handle_resolve_ops

In the same file, at the end of `_handle_resolve_ops()`, the non-gated path calls `self._dispatch_ops_agent(actions, context)` without checking the return value. Change it to:

```python
success = self._dispatch_ops_agent(actions, context)
if not success:
    raise AgentDispatchError("Ops agent dispatch failed — agent busy or not found")
```

Also do the same for the auto-approved subset dispatch inside the gated branch. If the auto subset dispatch fails, that's a dispatch error too — raise `AgentDispatchError`.

And in `approve_ops_gate()`, wrap the dispatch call:

```python
success = self._dispatch_ops_agent(actions, context + " [human-approved]")
if not success:
    self.daemon._notify("❌ Ops dispatch failed after gate approval")
    # Don't raise here — gate approval is interactive, not from poll()
```

### Step 3: Restructure poll() for skip-archive retry

The current `poll()` in the same file uses `try/except/finally` with unconditional archive. Replace it with:

```python
def poll(self):
    if not self.directives_dir.exists():
        return

    directives = sorted(
        f for f in self.directives_dir.iterdir()
        if f.is_file() and f.suffix == ".json"
    )

    for path in directives:
        failed = False
        retry = False
        try:
            self._process_directive(path)
        except AgentDispatchError as e:
            # Dispatch failed — leave directive for retry
            log.warning(f"Dispatch failed for {path.name}: {e}")
            retry = True
            attempts = self._retry_counts.get(path.name, 0) + 1
            self._retry_counts[path.name] = attempts
            if attempts >= self._max_directive_retries:
                log.error(f"Directive {path.name} exceeded {self._max_directive_retries} retries, archiving as FAILED")
                retry = False
                failed = True
        except Exception as e:
            log.error(f"Directive processing failed for {path.name}: {e}")
            failed = True

        if not retry:
            self._archive(path, failed=failed)
            self._retry_counts.pop(path.name, None)
```

### Step 4: Add retry tracking to __init__

In `DirectiveProcessor.__init__()` in the same file, add these two instance attributes:

```python
self._retry_counts: dict[str, int] = {}
self._max_directive_retries: int = 5
```

### Step 5: Add test scenarios

Add these to `/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/tests/test_ops_agent_e2e.py`:

**T18 — Dispatch failure archives as FAILED:** Mock `_dispatch_ops_agent` to return `False`. Drop a `resolve-ops` directive. Assert: directive archived with `FAILED-` prefix after 5 retries (5 calls to `poll()`).

**T19 — Dispatch failure retries:** Mock `_dispatch_ops_agent` to return `False` once, then `True`. Drop a directive. First poll: directive stays in `.directives/`. Second poll: dispatch succeeds, directive archived normally (no `FAILED-` prefix).

**T20 — Retry counter resets on success:** Same as T19 but verify `processor._retry_counts` is empty after successful archive.

**T21 — AgentDispatchError propagation:** Call `_handle_resolve_ops()` with a mocked `_dispatch_ops_agent` returning `False`. Assert `AgentDispatchError` is raised.

### Step 6: Verify

1. Run ALL existing tests: `cd /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator && python3 -m pytest tests/test_ops_agent_e2e.py -v`
2. All 17 existing tests must still pass
3. New tests T18–T21 must pass
4. AST check: `python3 -c "import ast; ast.parse(open('/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo/directives.py').read()); print('OK')"`

---

## What NOT to change

- Do NOT modify `_dispatch_ops_agent()` itself — it already returns `bool` correctly
- Do NOT modify `_archive()` — it works fine
- Do NOT touch the gating logic — it's correct
- Do NOT modify `_build_ops_agent_prompt()` — it's correct
- Do NOT touch `config.py` — P0-4 ("other" category) and dead field cleanup already shipped separately
- Do NOT touch `ops_actions.py` — P0-3b (JSONDecodeError retry) already shipped separately
- Do NOT modify `SKILL.md` — P0-3a (atomic writes) already shipped separately

## Dependencies

P0-4 (move "other" to human-gate) and P0-3 (atomic register writes) were already shipped in a separate commit. This work has no dependencies on those changes and no conflicts.
