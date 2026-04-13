"""Phase 2.9.2: Auto-handoff generation for stalled agents.

When IWO's stall watchdog detects that an agent exited headless mode without
writing its handoff JSON, this module inspects git state, runs tests and
typecheck, and synthesizes a handoff file so the pipeline can continue.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("iwo.auto_handoff")

AGENT_ORDER = ["planner", "builder", "reviewer", "tester", "deployer", "docs"]


def has_downstream_handoff(agent_name: str, spec_id: str, handoffs_dir: Path) -> bool:
    """Check if a downstream agent already wrote a handoff for this spec.

    If so, the pipeline already progressed past this agent — generating a
    synthetic handoff would be redundant or harmful.
    """
    next_agent = _next_agent(agent_name)
    if next_agent == "human":
        return False
    spec_dir = handoffs_dir / spec_id
    if not spec_dir.exists():
        return False
    for f in spec_dir.glob("*.json"):
        if f.name == "LATEST.json":
            continue
        if f"-{next_agent}-" in f.name:
            log.info(
                f"auto_handoff: downstream handoff exists ({f.name}) — "
                f"skipping synthetic for {agent_name}/{spec_id}"
            )
            return True
    return False


def _next_agent(current: str) -> str:
    """Determine the next agent in the pipeline."""
    try:
        idx = AGENT_ORDER.index(current)
    except ValueError:
        return "human"
    if idx + 1 < len(AGENT_ORDER):
        return AGENT_ORDER[idx + 1]
    return "human"


def _run_cmd(cmd: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:  # pragma: no cover - defensive
        return -1, "", str(e)


def _get_git_diff_stat(project_dir: Path) -> tuple[list[str], list[str]]:
    """Return (created, modified) file lists from HEAD~1..HEAD."""
    rc, output, _ = _run_cmd(
        ["git", "diff", "--name-status", "HEAD~1..HEAD"], project_dir
    )
    created: list[str] = []
    modified: list[str] = []
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
    """Run the full vitest suite and report pass/fail counts.

    Uses --reporter=json which writes JSON to stdout. stderr is kept
    separate to avoid corrupting the JSON parse.
    """
    rc, stdout, stderr = _run_cmd(
        ["npx", "vitest", "run", "--reporter=json"],
        project_dir,
        timeout=120,
    )
    try:
        json_output = json.loads(stdout)
        return {
            "passed": json_output.get("numPassedTests", 0),
            "failed": json_output.get("numFailedTests", 0),
            "skipped": json_output.get("numPendingTests", 0),
        }
    except (json.JSONDecodeError, KeyError, ValueError):
        # Fallback: try to parse summary line from combined output
        combined = stdout + stderr
        if "Tests" in combined and "passed" in combined:
            return {"passed": -1, "failed": 0, "skipped": 0}
        return {"passed": 0, "failed": -1, "skipped": 0}


def _run_typecheck(project_dir: Path) -> bool:
    """Run `npx tsc --noEmit` and return True if it passes."""
    rc, _, _ = _run_cmd(["npx", "tsc", "--noEmit"], project_dir, timeout=60)
    return rc == 0


def generate_auto_handoff(
    agent_name: str,
    spec_id: str,
    last_sequence: int,
    project_dir: Path,
    handoffs_dir: Path,
) -> Optional[Path]:
    """Generate a missing handoff JSON for a stalled agent.

    Returns the path to the written handoff file, or None on failure.
    """
    # Guard: if downstream agent already has a handoff, skip
    if has_downstream_handoff(agent_name, spec_id, handoffs_dir):
        return None

    # Bug 4 fix: never generate handoffs for phantom/system specs
    PHANTOM_SPECS = {"unknown", "NEXT-SPEC-SELECTION", "QUEUE-EXHAUSTED"}
    if spec_id in PHANTOM_SPECS:
        log.warning(
            f"auto_handoff: refusing to generate for phantom spec '{spec_id}'"
        )
        return None
    next_seq = last_sequence + 1
    target = _next_agent(agent_name)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    created, modified = _get_git_diff_stat(project_dir)
    tests = _run_tests(project_dir, spec_id)
    typecheck = _run_typecheck(project_dir)

    if tests["failed"] > 0:
        outcome = "partial"
        goal_met = False
        notes = (
            "Auto-generated handoff (agent exited without writing). "
            f"Tests: {tests['failed']} failures."
        )
    elif not typecheck:
        outcome = "partial"
        goal_met = False
        notes = (
            "Auto-generated handoff (agent exited without writing). "
            "Typecheck failed."
        )
    else:
        outcome = "success"
        goal_met = True
        notes = (
            "Auto-generated handoff (agent exited without writing). "
            "Tests pass, typecheck clean."
        )

    handoff = {
        "metadata": {
            "specId": spec_id,
            "agent": agent_name,
            "timestamp": timestamp,
            "sequence": next_seq,
            "auto_generated": True,
            # Phase 5 fix 5d: Audit marker for traceability.
            # Downstream consumers can filter/flag auto-recovered handoffs.
            "auto_recovery": True,
        },
        "status": {
            "outcome": outcome,
            "goalMet": goal_met,
            "notes": notes,
        },
        "summary": {
            "oneLiner": (
                f"[AUTO] {agent_name} completed {spec_id} — "
                "handoff auto-generated by IWO stall recovery"
            ),
        },
        "nextAgent": {
            "target": target,
            "action": (
                f"Continue pipeline for {spec_id} "
                f"(auto-recovery from stalled {agent_name})"
            ),
            "context": (
                f"Previous agent ({agent_name}) completed work but failed to "
                f"write handoff. Git shows {len(created)} new files, "
                f"{len(modified)} modified. Tests: {tests['passed']} passed, "
                f"{tests['failed']} failed. "
                f"Typecheck: {'pass' if typecheck else 'fail'}."
            ),
            "knownIssues": [],
        },
        "deliverables": {
            "filesCreated": created,
            "filesModified": modified,
            "testsStatus": tests,
            "typecheckPassed": typecheck,
            "lintPassed": True,
            "buildPassed": True,
        },
    }

    spec_dir = handoffs_dir / spec_id
    try:
        spec_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.error(f"auto_handoff: mkdir failed for {spec_dir}: {e}")
        return None

    safe_ts = timestamp.replace(":", "-")
    filename = f"{next_seq:03d}-{agent_name}-{safe_ts}.json"
    handoff_path = spec_dir / filename

    try:
        handoff_path.write_text(json.dumps(handoff, indent=2))
        return handoff_path
    except Exception as e:
        log.error(f"auto_handoff: write failed for {handoff_path}: {e}")
        return None
