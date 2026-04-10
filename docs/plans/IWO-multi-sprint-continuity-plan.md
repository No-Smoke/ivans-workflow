# IWO Multi-Sprint Continuity Fix — Implementation Plan

**Date:** 2026-04-10
**Author:** Claude Desktop (interactive session) + Gemini 2.5 Pro (PAL-MCP consultation)
**Problem:** Multi-sprint specs lose continuity between sprints — EBATT-105 was started before EBATT-104 Sprint 2
**Recommendation:** Option D (deterministic daemon detection + explicit start-spec with sprint context)

---

## Problem Analysis

### Root Cause

When Sprint N of a multi-sprint spec completes, the Docs agent hands off to `"human"` (terminal target). The daemon's `_schedule_auto_continue` method fires and writes a generic `next-spec` directive. The `_handle_next_spec` handler dispatches the Planner with a broad "select the next logical spec" prompt. The Planner reads BUILD-PRIORITY.md and selects whatever looks like the next available work — which may be a different spec entirely if the current spec's sprint status is ambiguous.

The fundamental issue: **the auto-continue path treats multi-sprint spec completion identically to single-sprint spec completion.** There is no mechanism to detect remaining sprints and route back to the same spec.

### Concrete Failure Trace (EBATT-104)

1. EBATT-104 Sprint 1 pipeline completes → Docs agent writes `007-docs-*.json` with `nextAgent.target: "human"`
2. `daemon.py` line 1248: terminal target detected → `pipeline.mark_completed("EBATT-104")` → spec removed from active tracking
3. Line 1269: `_schedule_auto_continue("EBATT-104")` fires (outcome was "success", auto_continue enabled)
4. Line 1417: `_schedule_auto_continue` checks guards (no active pipelines, planner idle) → passes
5. Line 1450: writes `{"directive": "next-spec", "focus": "Auto-continue after EBATT-104 completed successfully. Select the next logical spec."}` to `.directives/`
6. `directives.py` line 278: `_handle_next_spec` builds generic Planner prompt with completed spec list and "select next spec" instructions
7. Planner dispatched → reads BUILD-PRIORITY.md → sees EBATT-104 as "Sprint 1 DEPLOYED" and EBATT-105 as "IN PIPELINE"
8. Planner interprets EBATT-104 as partially done but EBATT-105 as the active pipeline item → selects EBATT-105
9. **Result:** EBATT-104 Sprint 2 orphaned; EBATT-105 starts prematurely

### Why the Planner Gets It Wrong

The Planner's spec selection prompt (directives.py line 440) says: "Resume incomplete pipelines first — if a spec has handoffs but no docs-agent completion, resume it." But EBATT-104 Sprint 1 **does** have docs-agent completion. From the Planner's perspective, EBATT-104's pipeline is complete. The concept of "Sprint 2 still needed" is only encoded in BUILD-PRIORITY.md's natural-language status column ("Sprint 2 QUEUED"), which the LLM may or may not parse correctly, and in the implementation plan file, which the `next-spec` prompt doesn't reference.

---

## Current Flow Trace

### Terminal Target Handling (daemon.py lines 1248-1271)

```python
# 8.5 Terminal targets — pipeline complete, no activation needed
if target in ("human", "none"):
    self.pipeline.mark_completed(handoff.spec_id)   # removes from active tracking
    self._write_active_specs()
    self._notify(...)
    
    # Bug-fix completion handler (if applicable)
    if handoff.spec_id.startswith("BUG-FIX-"):
        self._handle_bug_completion(handoff)
    
    # Auto-continue: queue next-spec directive if enabled
    if (self.config.auto_continue_on_completion
            and handoff.status.outcome == "success"):
        self._schedule_auto_continue(handoff.spec_id)    # <-- THE PROBLEM
    return
```

### Auto-Continue Method (daemon.py lines 1417-1471)

```python
def _schedule_auto_continue(self, completed_spec_id: str):
    # Guards: no active pipelines, planner idle
    if self.pipeline.active_count > 0: return
    if planner not idle: return
    
    # Writes generic next-spec directive — NO SPRINT AWARENESS
    directive = {
        "directive": "next-spec",
        "focus": f"Auto-continue after {completed_spec_id} completed successfully. "
                 f"Select the next logical spec.",
        "auto_generated": True,
    }
    # Written to .directives/ via background thread
```

### Next-Spec Handler (directives.py lines 278-340)

Builds a Planner prompt with:
- List of completed spec directories (by scanning for docs-agent handoffs)
- List of available spec files
- Instructions to read BUILD-PRIORITY.md and DISPOSITION.md
- Selection criteria: follow phase order, resume incomplete pipelines, respect dependencies

**Critical gap:** The prompt tells the Planner to "resume incomplete pipelines" but defines incomplete as "has handoffs but no docs-agent completion." A multi-sprint spec that finished Sprint 1 **has** docs-agent completion — so the Planner considers it complete and moves on.

---

## Options Evaluation Matrix

| Criterion | A: Docs updates BUILD-PRIORITY | B: Daemon injects context into next-spec | C: Daemon issues start-spec | D: start-spec + sprint context |
|-----------|------|------|------|------|
| **Reliability** | Very Low — LLM editing markdown tables | Medium — deterministic detection, LLM hint may be ignored | Medium-High — bypasses selection, but Planner must infer sprint | Very High — deterministic detection + unambiguous prompt |
| **Code change scope** | Low — prompt change only | Medium — daemon plan parsing | Medium — daemon plan parsing | High — daemon parsing + directive extension + prompt builder |
| **Race condition risk** | High — concurrent table edits | Low — serial handoff processing | Low | Low |
| **Backward compat** | Risk — table corruption affects all specs | Good — falls through for single-sprint | Good — only triggers for multi-sprint | Excellent — self-contained, explicit fallback |
| **LLM dependency** | Double LLM (Docs writes, Planner reads) | Single LLM (Planner interprets hint) | Single LLM (Planner infers sprint) | Minimal LLM (Planner given explicit sprint number) |

### PAL-MCP Consultation Summary (Gemini 2.5 Pro, thinking_mode: high)

Gemini evaluated all four options and recommended **Option D** as the only approach that "fully addresses the root cause — the unreliability of using LLM interpretation for critical control flow." Key quotes from the analysis:

- Option A: "Unacceptable. The low reliability and high race condition risk make this option architecturally unsound."
- Option B: "Viable, but not ideal. It improves determinism but retains a critical dependency on LLM interpretation for control flow."
- Option C: "A strong approach. It correctly identifies that bypassing spec selection is key. Its primary weakness is the ambiguity of the task presented to the Planner."
- Option D: "The best architectural choice. It correctly treats the LLM as a powerful tool that requires precise, unambiguous instructions for critical tasks, rather than as a reliable decision-maker for control flow."

---

## Recommended Approach: Option D

### Architecture

```
Sprint N completes → Docs agent → "human" terminal
    │
    ▼
daemon._schedule_auto_continue(spec_id)
    │
    ├── _detect_remaining_sprints(spec_id)
    │       ├── Read docs/plans/{SPEC-ID}-implementation-plan.md
    │       ├── Parse ### Sprint|Phase N headers → total_sprints
    │       ├── Count completed sprint handoff chains → completed_sprints
    │       └── Return (next_sprint_number, total_sprints) or None
    │
    ├── IF remaining sprints detected:
    │       Write start-spec directive with sprintContext:
    │       {
    │           "directive": "start-spec",
    │           "specId": "EBATT-104",
    │           "sprintContext": {
    │               "sprintNumber": 2,
    │               "totalSprints": 2,
    │               "previousSprintOutcome": "success",
    │               "planPath": "docs/plans/EBATT-104-implementation-plan.md"
    │           }
    │       }
    │
    └── ELSE (single-sprint or no plan found):
            Write next-spec directive (existing behaviour, unchanged)
```

---

## Implementation Steps

### Step 1: Add `_detect_remaining_sprints()` to `daemon.py`

**File:** `/home/vanya/PROJECTS/ivans-workflow-orchestrator/iwo/daemon.py`
**Location:** New method on `IWODaemon` class, after `_schedule_auto_continue` (line ~1471)

```python
def _detect_remaining_sprints(self, spec_id: str) -> Optional[dict]:
    """Detect if a spec has remaining sprints by parsing its implementation plan.
    
    Returns dict with sprint info if more sprints remain, None otherwise.
    Format: {"next_sprint": int, "total_sprints": int, "plan_path": str}
    """
    import re
    
    # Locate implementation plan
    plan_patterns = [
        self.config.project_root / "docs" / "plans" / f"{spec_id}-implementation-plan.md",
        self.config.project_root / "docs" / "plans" / f"{spec_id.lower()}-implementation-plan.md",
    ]
    
    plan_path = None
    for p in plan_patterns:
        if p.exists():
            plan_path = p
            break
    
    if not plan_path:
        log.info(f"No implementation plan found for {spec_id} — treating as single-sprint")
        return None
    
    # Parse sprint/phase headers
    try:
        content = plan_path.read_text()
    except Exception as e:
        log.warning(f"Failed to read plan for {spec_id}: {e}")
        return None
    
    # Match both "### Sprint N" and "### Phase N (Sprint N)" patterns
    sprint_pattern = re.compile(
        r'^###\s+(?:Sprint\s+(\d+)|Phase\s+\d+\s*\(?\s*Sprint\s+(\d+)\s*\)?)',
        re.MULTILINE | re.IGNORECASE
    )
    matches = sprint_pattern.findall(content)
    
    if not matches:
        log.info(f"No sprint headers found in plan for {spec_id}")
        return None
    
    # Extract sprint numbers (either group 1 or group 2 will match)
    sprint_numbers = sorted(set(
        int(m[0] or m[1]) for m in matches
    ))
    total_sprints = max(sprint_numbers)
    
    if total_sprints <= 1:
        return None  # Single-sprint spec
```

    
    # Count completed sprint pipelines from handoff history
    # Each full pipeline run produces a docs-agent handoff with "Sprint N" in context
    comms_dir = self.config.handoffs_dir / spec_id
    if not comms_dir.exists():
        log.warning(f"No agent-comms directory for {spec_id}")
        return None
    
    # Count distinct completed pipeline runs (each ends with a docs-agent handoff)
    completed_runs = 0
    for h in sorted(comms_dir.glob("*-docs-*.json")):
        try:
            data = json.loads(h.read_text())
            target = data.get("nextAgent", {}).get("target", "")
            outcome = data.get("status", {}).get("outcome", "")
            if target in ("human", "none") and outcome == "success":
                completed_runs += 1
        except Exception:
            continue
    
    if completed_runs >= total_sprints:
        log.info(f"{spec_id}: all {total_sprints} sprints completed")
        return None
    
    next_sprint = completed_runs + 1
    log.info(
        f"{spec_id}: sprint {completed_runs}/{total_sprints} completed, "
        f"next sprint: {next_sprint}"
    )
    
    return {
        "next_sprint": next_sprint,
        "total_sprints": total_sprints,
        "plan_path": str(plan_path.relative_to(self.config.project_root)),
    }
```

### Step 2: Modify `_schedule_auto_continue()` in `daemon.py`

**File:** `/home/vanya/PROJECTS/ivans-workflow-orchestrator/iwo/daemon.py`
**Location:** `_schedule_auto_continue` method (line 1417), inside `_write_directive` inner function

Replace the directive construction block (lines 1453-1460) with:

```python
def _write_directive():
    time.sleep(self.config.auto_continue_delay_seconds)
    try:
        directives_dir = self.config.handoffs_dir / ".directives"
        directives_dir.mkdir(parents=True, exist_ok=True)
        ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts_ns = time.time_ns()
        
        # NEW: Check for remaining sprints before deciding directive type
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
                "focus": (
                    f"Auto-continue after {completed_spec_id} completed "
                    f"successfully. Select the next logical spec."
                ),
                "timestamp": ts_iso,
                "auto_generated": True,
            }
        
        directive_path = directives_dir / filename
        directive_path.write_text(json.dumps(directive))
        log.info(
            f"Auto-continue: queued {directive['directive']} directive "
            f"→ {directive_path.name}"
        )
        self._notify(
            f"🔄 Auto-continue: {directive['directive']} queued "
            f"after {completed_spec_id}"
        )
    except Exception as e:
        log.error(f"Auto-continue failed to write directive: {e}")
```

### Step 3: Modify `_handle_start_spec()` in `directives.py`

**File:** `/home/vanya/PROJECTS/ivans-workflow-orchestrator/iwo/directives.py`
**Location:** `_handle_start_spec` method (line ~158)

The current method builds a generic prompt. We need to detect `sprintContext` in the directive data and build a sprint-specific prompt instead.

**Current signature and early logic (unchanged):**
```python
def _handle_start_spec(self, data: dict):
    spec_id = data.get("specId")
    if not spec_id:
        log.error("start-spec directive missing 'specId'")
        return
```

**Add after spec_id extraction:**
```python
    # Check for sprint continuation context
    sprint_ctx = data.get("sprintContext")
    
    if sprint_ctx:
        # Multi-sprint continuation — build sprint-specific prompt
        self._handle_sprint_continuation(spec_id, sprint_ctx, data)
        return
    
    # ... existing start-spec logic continues unchanged ...
```

### Step 4: Add `_handle_sprint_continuation()` to `directives.py`

**File:** `/home/vanya/PROJECTS/ivans-workflow-orchestrator/iwo/directives.py`
**Location:** New method on `DirectiveProcessor`, after `_handle_start_spec`

```python
def _handle_sprint_continuation(self, spec_id: str, sprint_ctx: dict, data: dict):
    """Handle multi-sprint continuation by dispatching Planner with sprint-specific prompt.
    
    Unlike generic start-spec, this tells the Planner exactly which sprint to plan
    and provides context from the previous sprint's completion.
    """
    sprint_num = sprint_ctx.get("next_sprint", 2)
    total = sprint_ctx.get("total_sprints", "unknown")
    plan_path = sprint_ctx.get("plan_path", "")
    
    # Read spec content
    spec_content = self._find_spec_content(spec_id)
    spec_block = ""
    if spec_content:
        # Truncate to first 3000 chars to avoid prompt bloat
        spec_block = f"\n\n## Spec Content (truncated)\n\n{spec_content[:3000]}\n"
    
    # Read previous sprint's LATEST.json for context
    latest_path = self.config.handoffs_dir / spec_id / "LATEST.json"
    prev_sprint_context = ""
    if latest_path.exists():
        try:
            latest_data = json.loads(latest_path.read_text())
            ctx = latest_data.get("nextAgent", {}).get("context", "")
            open_items = latest_data.get("nextAgent", {}).get("openItems", [])
            unresolved = latest_data.get("status", {}).get("unresolvedIssues", [])
            prev_sprint_context = f"""
## Previous Sprint Completion Context

**Sprint {sprint_num - 1} outcome:** {latest_data.get('status', {}).get('outcome', 'unknown')}
**Context from Docs agent:** {ctx}
**Open items carried forward:**
"""
            for item in open_items:
                prev_sprint_context += f"- {item}\n"
            if unresolved:
                prev_sprint_context += "\n**Unresolved issues:**\n"
                for issue in unresolved:
                    prev_sprint_context += f"- {issue}\n"
        except Exception as e:
            log.warning(f"Failed to read LATEST.json for {spec_id}: {e}")
    
    prompt = f"""## MANDATORY INSTRUCTIONS — READ YOUR SKILL FIRST

You are the Planner agent. Before doing ANYTHING else, execute these two commands:

```bash
cat .claude/skills/boris-planner-agent/SKILL.md
cat .claude/skills/workflow-handoff/HANDOFF-SCHEMA.md
```

You MUST read both files completely. This is non-negotiable.

---

## Task: Plan Sprint {sprint_num} of {spec_id}

**THIS IS A MULTI-SPRINT CONTINUATION.** You are NOT selecting a new spec.
You are planning Sprint {sprint_num} of {total} for {spec_id}.

The previous sprint (Sprint {sprint_num - 1}) completed successfully and the
pipeline is automatically continuing to the next sprint.

### Step 1: Read the Implementation Plan

```bash
cat {plan_path}
```

Read the FULL plan. Find the section for Sprint {sprint_num} (look for
"### Sprint {sprint_num}" or "### Phase {sprint_num}"). This defines your scope.

### Step 2: Read the Previous Sprint's Handoff Chain

```bash
ls docs/agent-comms/{spec_id}/
cat docs/agent-comms/{spec_id}/LATEST.json
```

Understand what was built in Sprint {sprint_num - 1} and what open items
were carried forward.

{prev_sprint_context}

### Step 3: Read the Spec

```bash
cat <spec-file-path>
```

Read the original spec for full context.
{spec_block}
"""
    prompt += f"""
### Step 4: Create the Sprint {sprint_num} Plan

Write the implementation plan for Sprint {sprint_num} ONLY to:
`docs/plans/{spec_id}-implementation-plan.md`

If the plan file already exists with Sprint {sprint_num} details, update it
with any adjustments based on Sprint {sprint_num - 1}'s outcomes. If Sprint
{sprint_num} scope needs changes based on what was learned, document the
changes and rationale.

### Step 5: Write the Handoff JSON

Re-read the handoff schema:
```bash
cat .claude/skills/workflow-handoff/HANDOFF-SCHEMA.md
```

Write handoff to: `docs/agent-comms/{spec_id}/{{sequence}}-planner-{{timestamp}}.json`

CRITICAL: The sequence number must continue from the existing handoff chain.
Check `ls docs/agent-comms/{spec_id}/` and use the next number.

### Step 6: Update .current-spec

```bash
echo "{spec_id}" > docs/agent-comms/.current-spec
```

### Step 7: Print Completion Signal

```
PLANNER STATUS: COMPLETE
SPEC: {spec_id} — Sprint {sprint_num} of {total}
PLAN: docs/plans/{spec_id}-implementation-plan.md
```

## CRITICAL REMINDERS

- You are planning Sprint {sprint_num}, NOT Sprint 1. Do not re-plan work already done.
- The implementation plan already exists with sprint decomposition — read it.
- Open items from Sprint {sprint_num - 1} should be addressed in this sprint if applicable.
- Handoff sequence must continue from existing chain (do not restart at 001).
- Be honest about risks and effort — no optimistic estimates.
"""

    # Write prompt file
    prompt_dir = self.config.log_dir / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    prompt_path = prompt_dir / f"planner-sprint-continue-{spec_id}-s{sprint_num}-{ts}.md"
    prompt_path.write_text(prompt)

    # Create synthetic handoff
    from .parser import Handoff, HandoffMetadata, HandoffStatus, NextAgent

    synthetic = Handoff(
        metadata=HandoffMetadata(
            specId=spec_id,
            agent="operator",
            timestamp=datetime.now(timezone.utc).isoformat(),
            sequence=0,
        ),
        status=HandoffStatus(outcome="success"),
        nextAgent=NextAgent(
            target="planner",
            action=f"Plan Sprint {sprint_num} of {total} for {spec_id}",
            context=f"Multi-sprint continuation. Previous sprint completed successfully.",
        ),
    )

    success = self.daemon.commander.activate_agent(
        "planner", handoff=synthetic, handoff_path=prompt_path,
    )

    if success:
        from .state import AgentState
        self.daemon.agent_states["planner"] = AgentState.PROCESSING
        self.daemon.pipeline.assign_agent("planner", spec_id)
        self.daemon._notify(
            f"Sprint continuation: {spec_id} Sprint {sprint_num}/{total} — Planner dispatched"
        )
        log.info(f"sprint-continue: dispatched Planner for {spec_id} Sprint {sprint_num}")
    else:
        self.daemon._notify(f"Sprint continuation failed: Planner not idle or dispatch error")
        log.error(f"sprint-continue: failed to dispatch Planner for {spec_id}")
```

### Step 5: Update `_handle_start_spec()` in `directives.py` to Route Sprint Context

**File:** `/home/vanya/PROJECTS/ivans-workflow-orchestrator/iwo/directives.py`
**Location:** `_handle_start_spec` method, after `spec_id` extraction

Add the sprint context check as shown in Step 3. The existing `_handle_start_spec` logic remains unchanged for normal (non-sprint-continuation) start-spec directives.

### Summary of Changed Files

| File | Changes | Lines Added (est.) |
|------|---------|-------------------|
| `iwo/daemon.py` | New `_detect_remaining_sprints()` method | ~60 |
| `iwo/daemon.py` | Modified `_schedule_auto_continue()` inner function | ~20 (net change) |
| `iwo/directives.py` | New `_handle_sprint_continuation()` method | ~120 |
| `iwo/directives.py` | Modified `_handle_start_spec()` — 4-line routing check | ~4 |

**Total:** ~204 new/modified lines across 2 files.

---

## Test Scenarios

### 1. Single-Sprint Spec (Backward Compatibility)

**Setup:** Spec with no implementation plan file, or plan with only `### Sprint 1`.
**Expected:** `_detect_remaining_sprints()` returns `None`. `_schedule_auto_continue` writes `next-spec` directive. Existing behaviour unchanged.
**Verification:** Run single-sprint spec through pipeline. Confirm `next-spec` directive written to `.directives/`. Confirm Planner selects a different spec.

### 2. Multi-Sprint Spec — Sprint 1 → Sprint 2

**Setup:** EBATT-104 with 2-sprint implementation plan. Sprint 1 completes successfully.
**Expected:** `_detect_remaining_sprints("EBATT-104")` returns `{"next_sprint": 2, "total_sprints": 2, "plan_path": "docs/plans/EBATT-104-implementation-plan.md"}`. `_schedule_auto_continue` writes `start-spec` directive with `sprintContext`. Planner dispatched with sprint-specific prompt for Sprint 2.
**Verification:** Tail `iwo.log` for "sprint continuation" log entry. Confirm directive file is `auto-continue-sprint.json` not `auto-next-spec.json`. Confirm Planner prompt contains "Plan Sprint 2 of EBATT-104".

### 3. Multi-Sprint Spec — Final Sprint Completion

**Setup:** EBATT-104 Sprint 2 of 2 completes. Both docs-agent handoffs exist in agent-comms.
**Expected:** `_detect_remaining_sprints("EBATT-104")` finds 2 docs-agent completions = total_sprints (2). Returns `None`. Falls through to `next-spec` directive.
**Verification:** Confirm `next-spec` directive written after final sprint. Planner selects next spec in BUILD-PRIORITY.md.

### 4. No Implementation Plan File

**Setup:** Spec dispatched via `start-spec` with no `docs/plans/{SPEC-ID}-implementation-plan.md`.
**Expected:** `_detect_remaining_sprints()` returns `None` (plan not found). Falls through to `next-spec`. Logged as "No implementation plan found — treating as single-sprint".
**Verification:** Confirm graceful fallback in `iwo.log`.

### 5. Implementation Plan with Phase/Sprint Header Variants

**Setup:** Plan using `### Phase 1 (Sprint 1):` format (like EBATT-104) and plan using `### Sprint 1:` format (like EBATT-103).
**Expected:** Regex `r'^###\s+(?:Sprint\s+(\d+)|Phase\s+\d+\s*\(?\s*Sprint\s+(\d+)\s*\)?)'` matches both patterns. Sprint numbers extracted correctly.
**Verification:** Unit test with both header formats.

### 6. Spec with Failed Sprint

**Setup:** Sprint 1 completed with `outcome: "failed"`.
**Expected:** `_schedule_auto_continue` is NOT called (line 1268 checks `outcome == "success"`). No auto-continue at all — operator must manually intervene.
**Verification:** Confirm no directive written on failed outcome.

### 7. Race Condition: Two Specs Completing Simultaneously

**Setup:** Two specs complete within the auto_continue_delay window.
**Expected:** Each `_schedule_auto_continue` call runs in its own thread. The active_count guard (line 1430) should prevent the second auto-continue from firing if the first already re-activated the pipeline. If both pass the guard simultaneously, the Planner will receive two directives — but `_handle_start_spec` will find the Planner busy on the second call and log a dispatch failure.
**Verification:** This is the existing race window from the `next-spec` path and is not made worse by this change.

### 8. Handoff Chain with Rejection Loops

**Setup:** Sprint 1 had Reviewer rejections, resulting in multiple Builder cycles. More than 6 handoff files exist, but only one docs-agent completion.
**Expected:** `_detect_remaining_sprints` counts only `*-docs-*.json` files with `target: "human"` and `outcome: "success"`. Rejection loops don't inflate the completed sprint count.
**Verification:** Create test agent-comms directory with rejection handoffs. Confirm `completed_runs == 1`.

---

## Edge Cases & Mitigations

**Plan file created after Sprint 1 starts:** If the Planner doesn't write the plan until Sprint 1 is in progress, the plan file will exist by the time Sprint 1 completes (the Planner always writes plans before handing off to Builder). No issue.

**Plan file has non-standard sprint numbering (e.g., starts at 0):** The regex extracts actual numbers. `max(sprint_numbers)` gives total. `completed_runs + 1` gives next sprint. If the plan uses 0-based numbering, `total_sprints` will be off by one. **Mitigation:** All existing plans use 1-based numbering. Add a log warning if sprint numbers don't start at 1.

**Spec with sprint count changed mid-execution:** If the plan is updated between Sprint 1 and Sprint 2 (e.g., a sprint is added or removed), the daemon re-reads the plan fresh each time. It will pick up the updated count. No stale cache issue.

**Plan file deleted between sprints:** `_detect_remaining_sprints` returns `None`, falls through to `next-spec`. The Planner will need to handle this naturally. Logged as warning.

---

## Implementation Order

1. Write `_detect_remaining_sprints()` in `daemon.py` — pure function, testable in isolation
2. Write `_handle_sprint_continuation()` in `directives.py` — prompt construction
3. Modify `_handle_start_spec()` to add 4-line routing check
4. Modify `_schedule_auto_continue()` to call detection and branch
5. Manual integration test: run EBATT-104 Sprint 2 (the original failure case)
6. Verify backward compat: run a single-sprint spec and confirm `next-spec` behaviour
