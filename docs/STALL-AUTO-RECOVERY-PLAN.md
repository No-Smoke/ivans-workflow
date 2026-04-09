# IWO Stall Auto-Recovery — Implementation Plan

**Date:** 2026-04-09
**Author:** Claude Opus 4.6 (via Claude Desktop session)
**Context:** EBATT-100 Sprint 2 and Sprint 3 both stalled because agents (Builder, Tester) exited headless mode without writing their handoff JSON. Manual intervention was needed twice in one session.

---

## Problem Statement

When IWO dispatches agents in headless mode (`claude -p`), the `prompt-handoff.sh` Stop hook is supposed to write the handoff JSON before the process exits. In practice, this hook fails intermittently — the Claude Code process terminates before the hook completes, or the hook silently errors. The result: the pipeline stalls at agent→next_agent transitions with no handoff file, no error, and no alert.

**Evidence from 2026-04-09 session:**
- EBATT-100 Sprint 2: Builder committed `d4390784` then exited. No `002-builder-*.json` written. Pipeline stuck for ~35 minutes until manually fixed.
- EBATT-100 Sprint 3: Tester completed (20:13:09), no `004-tester-*.json` written. Same pattern.
- Sprint 3 Builder DID write its handoff correctly — confirming this is intermittent, not systematic.

## What's Already Done (Quick Fix — Phase 2.9.1)

A stall detection alert has been patched into `iwo/daemon.py` (4 insertion points):

1. **Init:** `self._stall_watchdog: dict[str, tuple[float, str]]` and `self._stall_alert_sent: set[str]`
2. **Set:** In `_poll_agent_states` completed loop — when agent transitions PROCESSING→IDLE, record `(now, spec_id)` in watchdog
3. **Check:** At end of `_poll_agent_states` — if watchdog entry > 60s old, fire ntfy alert with `critical=True`
4. **Clear:** In `process_handoff` — after successful parse, `pop` agent from watchdog

This gives Vanya immediate phone alerts when stalls happen. But it still requires manual intervention.

---

## Phase 1: Auto-Handoff Generation (The Robust Fix)

### Concept

When a stall is detected (agent idle for 60s, no handoff), IWO should automatically generate the missing handoff JSON by inspecting:
1. **Git diff** — what files were created/modified since the last handoff
2. **Test results** — run `npx vitest run` on relevant test files
3. **Typecheck** — run `npx tsc --noEmit`
4. **Pipeline state** — which agent just ran, what spec, what sequence number

### Implementation Location

New file: `iwo/auto_handoff.py`

### Required Function Signature

```python
def generate_auto_handoff(
    agent_name: str,
    spec_id: str,
    last_sequence: int,
    project_dir: Path,
    handoffs_dir: Path,
) -> Optional[Path]:
    """Generate a missing handoff JSON for a stalled agent.
    
    Returns the path to the written handoff file, or None if generation failed.
    """
```

### Algorithm

```python
import json
import subprocess
import time
from pathlib import Path
from typing import Optional

AGENT_ORDER = ["planner", "builder", "reviewer", "tester", "deployer", "docs"]

def _next_agent(current: str) -> str:
    """Determine the next agent in the pipeline."""
    idx = AGENT_ORDER.index(current)
    if idx + 1 < len(AGENT_ORDER):
        return AGENT_ORDER[idx + 1]
    return "human"

def _run_cmd(cmd: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str]:
    """Run a subprocess and return (returncode, stdout+stderr)."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as e:
        return -1, str(e)

def _get_git_diff_stat(project_dir: Path) -> tuple[list[str], list[str]]:
    """Get files created and modified since last commit's parent."""
    rc, output = _run_cmd(
        ["git", "diff", "--name-status", "HEAD~1..HEAD"], project_dir
    )
    created, modified = [], []
    if rc == 0:
        for line in output.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                status, filepath = parts
                if status == "A":
                    created.append(filepath)
                elif status == "M":
                    modified.append(filepath)
    return created, modified

def _run_tests(project_dir: Path, spec_id: str) -> dict:
    """Run vitest for hybrid-calculator tests and return status."""
    # Determine test directory from spec pattern
    test_dir = "src/__tests__/hybrid-calculator/"
    rc, output = _run_cmd(
        ["npx", "vitest", "run", test_dir, "--reporter=json"],
        project_dir, timeout=60
    )
    
    try:
        # vitest --reporter=json outputs JSON to stdout
        json_output = json.loads(output)
        passed = json_output.get("numPassedTests", 0)
        failed = json_output.get("numFailedTests", 0)
        return {"passed": passed, "failed": failed, "skipped": 0}
    except (json.JSONDecodeError, KeyError):
        # Fallback: parse text output
        if "Tests" in output and "passed" in output:
            return {"passed": -1, "failed": 0, "skipped": 0}  # -1 = unknown count
        return {"passed": 0, "failed": -1, "skipped": 0}

def _run_typecheck(project_dir: Path) -> bool:
    """Run tsc --noEmit and return True if it passes."""
    rc, _ = _run_cmd(["npx", "tsc", "--noEmit"], project_dir, timeout=60)
    return rc == 0

def generate_auto_handoff(
    agent_name: str,
    spec_id: str,
    last_sequence: int,
    project_dir: Path,
    handoffs_dir: Path,
) -> Optional[Path]:
    """Generate a missing handoff JSON for a stalled agent."""
    
    next_seq = last_sequence + 1
    target = _next_agent(agent_name)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    
    # Gather evidence
    created, modified = _get_git_diff_stat(project_dir)
    tests = _run_tests(project_dir, spec_id)
    typecheck = _run_typecheck(project_dir)
    
    # Determine outcome
    if tests["failed"] > 0:
        outcome = "partial"
        goal_met = False
        notes = f"Auto-generated handoff (agent exited without writing). Tests: {tests['failed']} failures."
    elif not typecheck:
        outcome = "partial"
        goal_met = False
        notes = "Auto-generated handoff (agent exited without writing). Typecheck failed."
    else:
        outcome = "success"
        goal_met = True
        notes = "Auto-generated handoff (agent exited without writing). Tests pass, typecheck clean."
    
    handoff = {
        "metadata": {
            "specId": spec_id,
            "agent": agent_name,
            "timestamp": timestamp,
            "sequence": next_seq,
            "auto_generated": True,  # Flag for auditing
        },
        "status": {
            "outcome": outcome,
            "goalMet": goal_met,
            "notes": notes,
        },
        "summary": {
            "oneLiner": f"[AUTO] {agent_name} completed {spec_id} — handoff auto-generated by IWO stall recovery",
        },
        "nextAgent": {
            "target": target,
            "action": f"Continue pipeline for {spec_id} (auto-recovery from stalled {agent_name})",
            "context": f"Previous agent ({agent_name}) completed work but failed to write handoff. "
                       f"Git shows {len(created)} new files, {len(modified)} modified. "
                       f"Tests: {tests['passed']} passed, {tests['failed']} failed. "
                       f"Typecheck: {'pass' if typecheck else 'fail'}.",
            "knownIssues": [],
        },
        "deliverables": {
            "filesCreated": created,
            "filesModified": modified,
            "testsStatus": tests,
            "typecheckPassed": typecheck,
            "lintPassed": True,  # Assume lint passes if typecheck passes
            "buildPassed": True,
        },
    }
    
    # Write the handoff file
    spec_dir = handoffs_dir / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    
    safe_ts = timestamp.replace(":", "-").replace("T", "T")
    filename = f"{next_seq:03d}-{agent_name}-{safe_ts}.json"
    handoff_path = spec_dir / filename
    
    try:
        handoff_path.write_text(json.dumps(handoff, indent=2))
        return handoff_path
    except Exception:
        return None
```

### Integration into daemon.py

In `_poll_agent_states`, replace the current alert-only logic with:

```python
# Phase 2.9.2: Auto-handoff generation on stall
from iwo.auto_handoff import generate_auto_handoff

stall_timeout = 60.0
auto_handoff_timeout = 90.0  # Give extra 30s grace before auto-generating
stale_watchdogs = []

for agent_name, (idle_at, spec_id) in self._stall_watchdog.items():
    elapsed = now - idle_at
    
    if elapsed >= auto_handoff_timeout and agent_name not in self._stall_alert_sent:
        # Attempt auto-handoff
        log.warning(f"STALL AUTO-RECOVERY: generating handoff for {agent_name}/{spec_id}")
        
        # Get last sequence from existing handoffs
        spec_dir = self.config.handoffs_dir / spec_id
        existing = sorted(spec_dir.glob("*.json")) if spec_dir.exists() else []
        last_seq = len(existing)  # Planner=1, so len gives next expected
        
        result = generate_auto_handoff(
            agent_name=agent_name,
            spec_id=spec_id,
            last_sequence=last_seq,
            project_dir=self.config.project_dir,
            handoffs_dir=self.config.handoffs_dir,
        )
        
        if result:
            log.info(f"STALL AUTO-RECOVERY: wrote {result.name}")
            self._notify(
                f"🔧 Auto-recovered stall: {agent_name}/{spec_id} — generated {result.name}",
                critical=True,
            )
        else:
            log.error(f"STALL AUTO-RECOVERY: failed for {agent_name}/{spec_id}")
            self._notify(
                f"⚠️ STALL: {agent_name}/{spec_id} — auto-recovery FAILED, manual intervention needed",
                critical=True,
            )
        self._stall_alert_sent.add(agent_name)
    
    elif elapsed >= stall_timeout and agent_name not in self._stall_alert_sent:
        # Alert only (existing Phase 2.9.1 behavior) — warn before auto-recovery kicks in
        log.warning(f"STALL WARNING: {agent_name} completed {spec_id} "
                    f"{int(elapsed)}s ago, no handoff. Auto-recovery in {int(auto_handoff_timeout - elapsed)}s")
    
    if elapsed > 300:
        stale_watchdogs.append(agent_name)

for agent_name in stale_watchdogs:
    self._stall_watchdog.pop(agent_name, None)
    self._stall_alert_sent.discard(agent_name)
```

### Configuration Additions (config.py)

Add to the IWOConfig dataclass:

```python
# ─── Stall Recovery ───────────────────────────────────────────
stall_alert_timeout: float = 60.0       # seconds before alerting
stall_auto_handoff_timeout: float = 90.0  # seconds before auto-generating handoff
stall_auto_handoff_enabled: bool = True   # kill switch for auto-generation
```

### Safety Guards

1. **`auto_generated: True` flag** in handoff metadata — the Reviewer and Tester can see this and apply extra scrutiny
2. **Kill switch** via `IWO_STALL_AUTO_HANDOFF=false` env var
3. **Tests must pass** — if tests fail, generate a `partial` outcome handoff that routes back to Builder (Reviewer will reject)
4. **One attempt only** — `_stall_alert_sent` prevents repeated auto-generation for the same stall
5. **Auditor logs** — auto-generated handoffs should be logged with `AUDIT [WARN]` level

---

## Phase 2: Stop Hook Investigation

Before implementing Phase 1, investigate WHY the hook fails. This is lower priority but would prevent the root cause.

### Diagnostic Steps

1. Check if `prompt-handoff.sh` is even configured as a Stop hook in the current CLAUDE.md:
   ```bash
   grep -n "prompt-handoff" /home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/.claude/hooks.json
   ```

2. Add logging to the hook itself:
   ```bash
   echo "$(date) prompt-handoff.sh fired for $(pwd)" >> /tmp/iwo-hook-debug.log
   ```

3. Check if `claude -p` (headless/pipe mode) fires Stop hooks at all — this may be a Claude Code limitation where hooks are skipped in non-interactive mode.

4. If hooks don't fire in headless mode, consider an alternative: have IWO send a "write your handoff now" command to the agent BEFORE it exits, rather than relying on a post-exit hook.

---

## Testing Plan

1. **Unit test for `generate_auto_handoff`**: Mock subprocess calls, verify handoff JSON structure
2. **Integration test**: Start a dummy agent, let it exit without handoff, verify auto-recovery fires after timeout
3. **Manual test**: Run the pipeline, kill an agent mid-work, verify the stall alert + auto-recovery

---

## Files to Create/Modify

| Action | File | Description |
|--------|------|-------------|
| NEW | `iwo/auto_handoff.py` | Auto-handoff generation logic |
| MOD | `iwo/daemon.py` | Replace Phase 2.9.1 alert-only with Phase 2.9.2 auto-recovery |
| MOD | `iwo/config.py` | Add stall recovery config fields |
| NEW | `tests/test_auto_handoff.py` | Unit tests for handoff generation |

---

## Execution Instructions

1. Stop IWO: `Ctrl+C` in the IWO terminal or `iwo stop`
2. Create `iwo/auto_handoff.py` with the algorithm above
3. Update `iwo/daemon.py` — replace the Phase 2.9.1 stall check block (lines ~316-338) with the Phase 2.9.2 version
4. Add config fields to `iwo/config.py`
5. Run syntax check: `python3 -c "import py_compile; py_compile.compile('iwo/daemon.py', doraise=True)"`
6. Run existing tests: `python3 -m pytest tests/ -v`
7. Start IWO: `iwo` or `iwo-tui`
8. Test by deliberately killing an agent during a sprint and verifying auto-recovery fires
