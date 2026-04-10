# Multi-Sprint Continuity Fix — Claude Code Implementation Prompt

## Context

Read the implementation plan first:
```bash
cat docs/plans/IWO-multi-sprint-continuity-plan.md
```

## Problem

IWO's `_schedule_auto_continue()` always writes a generic `next-spec` directive when a spec completes. For multi-sprint specs, this causes the Planner to select a different spec instead of continuing to the next sprint. EBATT-105 was incorrectly started before EBATT-104 Sprint 2.

## What to Implement (Option D — approved)

Three changes across two files in `/home/vanya/PROJECTS/ivans-workflow-orchestrator/iwo/`:

### 1. New method: `_detect_remaining_sprints()` in `daemon.py`

Add after `_schedule_auto_continue` (line ~1471). This method:
- Reads `docs/plans/{SPEC-ID}-implementation-plan.md` from `self.config.project_root`
- Parses `### Sprint N` and `### Phase N (Sprint N)` headers via regex:
  ```python
  re.compile(r'^###\s+(?:Sprint\s+(\d+)|Phase\s+\d+\s*\(?\s*Sprint\s+(\d+)\s*\)?)', re.MULTILINE | re.IGNORECASE)
  ```
- Counts completed sprint pipelines by scanning `{spec_id}/*-docs-*.json` files in `self.config.handoffs_dir` for those with `nextAgent.target in ("human", "none")` and `status.outcome == "success"`
- Returns `{"next_sprint": N, "total_sprints": M, "plan_path": "relative/path"}` if more sprints remain, `None` otherwise
- Falls through gracefully (returns `None`) if: no plan file, no sprint headers, plan unreadable, all sprints complete

### 2. Modify `_schedule_auto_continue()` in `daemon.py`

In the `_write_directive` inner function (line ~1450), replace the directive construction with:

```python
# Check for remaining sprints before deciding directive type
sprint_info = self._detect_remaining_sprints(completed_spec_id)

if sprint_info:
    # Multi-sprint continuation: issue start-spec for same specId
    filename = f"{ts_ns}-auto-continue-sprint.json"
    directive = {
        "directive": "start-spec",
        "specId": completed_spec_id,
        "sprintContext": sprint_info,
        "timestamp": ts_iso,
        "auto_generated": True,
    }
    log.info(
        f"Auto-continue: sprint continuation for {completed_spec_id} "
        f"Sprint {sprint_info['next_sprint']}/{sprint_info['total_sprints']}"
    )
else:
    # Single-sprint or final sprint: existing behaviour
    filename = f"{ts_ns}-auto-next-spec.json"
    directive = {
        "directive": "next-spec",
        "focus": f"Auto-continue after {completed_spec_id} completed successfully. Select the next logical spec.",
        "timestamp": ts_iso,
        "auto_generated": True,
    }
```

Keep everything else in `_write_directive` unchanged (the `directive_path` write, logging, notification, exception handling).

### 3. New method + routing: `_handle_sprint_continuation()` in `directives.py`

**3a.** In `_handle_start_spec()`, add routing after `spec_id` extraction:
```python
sprint_ctx = data.get("sprintContext")
if sprint_ctx:
    self._handle_sprint_continuation(spec_id, sprint_ctx, data)
    return
```

**3b.** New `_handle_sprint_continuation(self, spec_id, sprint_ctx, data)` method that:
- Extracts `sprint_num`, `total`, `plan_path` from `sprint_ctx`
- Reads previous sprint's LATEST.json for context (outcome, openItems, unresolvedIssues)
- Optionally reads spec content via `self._find_spec_content(spec_id)` (truncate to 3000 chars)
- Builds a sprint-specific Planner prompt that:
  - Tells the Planner to read its SKILL.md and HANDOFF-SCHEMA.md
  - States clearly: "THIS IS A MULTI-SPRINT CONTINUATION. You are planning Sprint N of M for SPEC-ID."
  - Instructs Planner to read the existing implementation plan at `plan_path`
  - Provides previous sprint context (outcome, open items, unresolved issues)
  - Instructs Planner to continue the handoff sequence number (not restart at 001)
  - Reminds: "Do not re-plan work already done"
- Writes prompt to `logs/prompts/planner-sprint-continue-{spec_id}-s{N}-{ts}.md`
- Creates synthetic Handoff with `specId=spec_id` (not "NEXT-SPEC-SELECTION")
- Dispatches Planner via `self.daemon.commander.activate_agent()`
- On success: sets planner state to PROCESSING, assigns agent to spec, notifies

The full prompt template and pseudocode are in the implementation plan at `docs/plans/IWO-multi-sprint-continuity-plan.md`.

## Constraints

- Do NOT change `_handle_next_spec` — that path remains for single-sprint auto-continue
- Single-sprint specs must continue to work identically (backward compat)
- All new code goes in `iwo/daemon.py` and `iwo/directives.py` only
- Use `import re` at module level in daemon.py if not already imported (it is — line 5)
- The `json` import is already available in both files
- Test by checking: `python -c "from iwo.daemon import IWODaemon; print('OK')"`

## Verification

After implementing, verify:
1. `python -c "from iwo.daemon import IWODaemon; print('imports OK')"` passes
2. `python -c "from iwo.directives import DirectiveProcessor; print('imports OK')"` passes
3. Read the implementation plan's test scenarios section for edge cases
4. Commit with message: `feat(iwo): multi-sprint continuity — detect remaining sprints and issue start-spec continuation`
5. Push to `feature/headless-dispatch` branch
