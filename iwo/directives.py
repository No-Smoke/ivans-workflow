"""Directive processor — operator commands via filesystem.

Operators drop JSON directive files into docs/agent-comms/.directives/
to control IWO without interacting with the TUI or tmux directly.
Useful for desktop launcher right-click actions, cron jobs, or
external automation.

Directives are processed once and archived to .directives/.processed/.
"""

import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("iwo.directives")

# Valid directive types
DIRECTIVE_TYPES = frozenset((
    "start-spec",
    "next-spec",
    "resume",
    "reconcile",
    "status",
    "pause",
    "unpause",
    "cancel-spec",
))

# Pipeline agent order — used by resume to determine next agent
AGENT_ORDER = ["planner", "builder", "reviewer", "tester", "deployer", "docs"]


class DirectiveProcessor:
    """Processes operator directive files from .directives/ directory.

    Lifecycle:
        processor = DirectiveProcessor(config, daemon)
        processor.ensure_dirs()
        processor.poll()  # called from main loop or TUI timer
    """

    def __init__(self, config, daemon):
        self.config = config
        self.daemon = daemon
        self.directives_dir = config.handoffs_dir / ".directives"
        self.processed_dir = self.directives_dir / ".processed"

    def ensure_dirs(self):
        """Create directives directories if they don't exist."""
        self.directives_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)

    def poll(self):
        """Scan for new directive files and process them.

        Called periodically from the daemon's main loop.
        Files are processed in filename order (timestamp prefix ensures FIFO).
        """
        if not self.directives_dir.exists():
            return

        directives = sorted(
            f for f in self.directives_dir.iterdir()
            if f.is_file() and f.suffix == ".json"
        )

        for path in directives:
            try:
                self._process_directive(path)
            except Exception as e:
                log.error(f"Directive processing failed for {path.name}: {e}")
            finally:
                self._archive(path)

    def _process_directive(self, path: Path):
        """Parse and execute a single directive file."""
        raw = path.read_text()
        data = json.loads(raw)

        directive_type = data.get("directive")
        if not directive_type:
            log.warning(f"Directive missing 'directive' field: {path.name}")
            return

        if directive_type not in DIRECTIVE_TYPES:
            log.warning(f"Unknown directive type '{directive_type}': {path.name}")
            return

        log.info(f"Processing directive: {directive_type} from {path.name}")

        handler = getattr(self, f"_handle_{directive_type.replace('-', '_')}", None)
        if handler:
            handler(data)
        else:
            log.warning(f"No handler for directive type: {directive_type}")

    def _archive(self, path: Path):
        """Move processed directive to .processed/ with timestamp."""
        try:
            ts = int(time.time())
            dest = self.processed_dir / f"{ts}-{path.name}"
            shutil.move(str(path), str(dest))
            log.debug(f"Archived directive: {path.name} → {dest.name}")
        except Exception as e:
            log.warning(f"Failed to archive directive {path.name}: {e}")
            try:
                path.unlink()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Directive Handlers
    # ------------------------------------------------------------------

    def _handle_start_spec(self, data: dict):
        """Start a new spec by dispatching Planner with the spec content.

        Directive format:
        {
            "directive": "start-spec",
            "specId": "EBATT-011",
            "specFile": "optional/path/to/spec.md",
            "context": "Optional additional instructions"
        }
        """
        spec_id = data.get("specId")
        if not spec_id:
            log.error("start-spec directive missing 'specId'")
            self.daemon._notify("❌ start-spec failed: missing specId")
            return

        # Find the spec file
        spec_content = self._find_spec_content(spec_id, data.get("specFile"))
        context = data.get("context", "")

        # Create the spec's agent-comms directory
        spec_dir = self.config.handoffs_dir / spec_id
        spec_dir.mkdir(parents=True, exist_ok=True)

        # Update .current-spec
        current_spec_file = self.config.handoffs_dir / ".current-spec"
        current_spec_file.write_text(spec_id)

        # Build prompt for Planner
        prompt = f"""## New Spec Assignment: {spec_id}

You are the Planner agent. Read the specification below and create a detailed
implementation plan. When complete, write your handoff JSON to:
  docs/agent-comms/{spec_id}/

Follow the handoff schema at .claude/skills/workflow-handoff/HANDOFF-SCHEMA.md

### Specification

{spec_content if spec_content else f"Read the spec for {spec_id} from the ebatt-specs directory."}
"""
        if context:
            prompt += f"\n### Additional Context\n\n{context}\n"

        # Write prompt file
        prompt_dir = self.config.log_dir / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        prompt_path = prompt_dir / f"planner-start-{spec_id}-{ts}.md"
        prompt_path.write_text(prompt)

        # Dispatch to Planner via HeadlessCommander
        from .parser import Handoff, HandoffMetadata, HandoffStatus, NextAgent

        # Create a synthetic handoff to drive the dispatch
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
                action=f"Create implementation plan for {spec_id}",
                context=context or None,
            ),
        )

        success = self.daemon.commander.activate_agent(
            "planner", handoff=synthetic, handoff_path=prompt_path,
        )

        if success:
            from .state import AgentState
            self.daemon.agent_states["planner"] = AgentState.PROCESSING
            self.daemon.pipeline.assign_agent("planner", spec_id)
            self.daemon._notify(f"Started {spec_id} -- Planner dispatched")
            log.info(f"start-spec: dispatched Planner for {spec_id}")
        else:
            self.daemon._notify(f"start-spec failed: Planner not idle or dispatch error")
            log.error(f"start-spec: failed to dispatch Planner for {spec_id}")

    def _find_spec_content(self, spec_id: str, spec_file: Optional[str] = None) -> Optional[str]:
        """Locate and read spec content. Returns None if not found."""
        # Explicit path provided
        if spec_file:
            p = Path(spec_file)
            if p.exists():
                return p.read_text()

        # Search common spec locations
        spec_dirs = [
            self.config.project_root / "ebatt-specs",
            self.config.project_root.parent / "ebatt-specs",
            self.config.project_root.parent / "shared-unified" / "shared-specs" / "v2-schema-first",
        ]

        # Try exact match first, then prefix match
        spec_lower = spec_id.lower().replace("-", "")
        for d in spec_dirs:
            if not d.exists():
                continue
            for f in d.iterdir():
                if f.suffix == ".md" and spec_lower in f.name.lower().replace("-", ""):
                    try:
                        return f.read_text()
                    except Exception:
                        continue

        log.warning(f"Spec file not found for {spec_id} — Planner will need to locate it")
        return None

    def _handle_next_spec(self, data: dict):
        """Auto-select and plan the next logical spec.

        Scans completed handoff directories, reads tracking file and spec list,
        dispatches Planner with a deterministic prompt to select and plan the
        next spec. Planner MUST use its full skill to write the plan and handoff.

        Directive format:
        {
            "directive": "next-spec",
            "focus": "optional focus area, e.g. 'calculators' or 'shared infrastructure'",
            "context": "optional additional guidance"
        }
        """
        focus = data.get("focus", "")
        context = data.get("context", "")

        # Gather completed spec IDs from agent-comms directories
        completed = self._gather_completed_specs()

        # Gather available spec files
        ebatt_specs = self._list_spec_files("ebatt")
        shared_specs = self._list_spec_files("shared")

        # Build deterministic Planner prompt
        prompt = self._build_next_spec_prompt(completed, ebatt_specs, shared_specs, focus, context)

        # Write prompt file
        prompt_dir = self.config.log_dir / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        prompt_path = prompt_dir / f"planner-next-spec-{ts}.md"
        prompt_path.write_text(prompt)

        # Create synthetic handoff to drive dispatch
        from .parser import Handoff, HandoffMetadata, HandoffStatus, NextAgent

        synthetic = Handoff(
            metadata=HandoffMetadata(
                specId="NEXT-SPEC-SELECTION",
                agent="operator",
                timestamp=datetime.now(timezone.utc).isoformat(),
                sequence=0,
            ),
            status=HandoffStatus(outcome="success"),
            nextAgent=NextAgent(
                target="planner",
                action="Select next spec and create implementation plan",
                context=focus or None,
            ),
        )

        success = self.daemon.commander.activate_agent(
            "planner", handoff=synthetic, handoff_path=prompt_path,
        )

        if success:
            from .state import AgentState
            self.daemon.agent_states["planner"] = AgentState.PROCESSING
            self.daemon._notify(f"Planner dispatched — selecting next spec")
            log.info("next-spec: dispatched Planner for spec selection")
        else:
            self.daemon._notify("next-spec failed: Planner not idle or dispatch error")
            log.error("next-spec: failed to dispatch Planner")

    def _gather_completed_specs(self) -> list[str]:
        """Scan agent-comms directories for specs that have completed the pipeline."""
        completed = []
        comms_dir = self.config.handoffs_dir
        if not comms_dir.exists():
            return completed

        for d in sorted(comms_dir.iterdir()):
            if not d.is_dir():
                continue
            if d.name.startswith("."):
                continue
            # Check if docs agent completed (indicates full pipeline completion)
            handoffs = sorted(d.glob("*.json"))
            for h in handoffs:
                try:
                    data = json.loads(h.read_text())
                    agent = data.get("metadata", {}).get("agent", "")
                    if agent == "docs":
                        completed.append(d.name)
                        break
                except Exception:
                    continue

        return completed

    def _list_spec_files(self, spec_type: str) -> list[str]:
        """List available spec filenames."""
        projects_root = self.config.project_root.parent.parent  # .../PROJECTS/
        if spec_type == "ebatt":
            dirs = [
                self.config.project_root / "ebatt-specs",
                self.config.project_root.parent / "ebatt-specs",
            ]
        else:
            dirs = [
                projects_root / "shared-unified" / "shared-specs" / "v2-schema-first",
                self.config.project_root.parent / "shared-unified" / "shared-specs" / "v2-schema-first",
            ]

        specs = []
        for d in dirs:
            if not d.exists():
                continue
            for f in sorted(d.iterdir()):
                if f.suffix == ".md" and f.name not in ("README.md",):
                    specs.append(f.name)
            if specs:
                break  # found specs in first directory that has them
        return specs

    def _build_next_spec_prompt(
        self,
        completed: list[str],
        ebatt_specs: list[str],
        shared_specs: list[str],
        focus: str,
        context: str,
    ) -> str:
        """Build the deterministic Planner prompt for next-spec selection."""
        prompt = """## MANDATORY INSTRUCTIONS — READ YOUR SKILL FIRST

You are the Planner agent. Before doing ANYTHING else, execute these two commands:

```bash
cat .claude/skills/boris-planner-agent/SKILL.md
cat .claude/skills/workflow-handoff/HANDOFF-SCHEMA.md
```

You MUST read both files completely. Your plan and handoff MUST follow the formats
defined in those files exactly. This is non-negotiable.

---

## Task: Select and Plan the Next Spec

You must select the next logical specification to implement and create a full
implementation plan with Builder handoff. Follow this exact sequence:

### Step 1: Understand What's Been Completed

These spec directories have completed the full pipeline (docs agent finished):

"""
        if completed:
            for s in completed:
                prompt += f"- {s}\n"
        else:
            prompt += "- (none completed yet)\n"

        prompt += """
### Step 2: Review Available Specs

**eBatt specs available:**
"""
        for s in ebatt_specs:
            prompt += f"- {s}\n"

        if shared_specs:
            prompt += "\n**Shared specs available:**\n"
            for s in shared_specs:
                prompt += f"- {s}\n"

        prompt += """
### Step 3: Check the Tracking File

```bash
cat ebatt-specs/TRACKING-v2.1.md
```

Review the tracking file for completion status, dependencies, and priority order.

### Step 4: Check Current State

```bash
cat docs/agent-comms/.current-spec 2>/dev/null
ls docs/agent-comms/*/LATEST.json 2>/dev/null
```

Identify any specs that were started but not completed (partial pipelines).

### Step 5: Select the Next Spec

Apply these selection criteria IN ORDER:
1. **Resume incomplete pipelines first** — if a spec has handoffs but no docs-agent completion, resume it
2. **Respect dependency chains** — don't start a spec whose prerequisites aren't done
3. **Prioritise foundation specs** — shared infrastructure, schema, core platform over features
4. **Prefer lower-numbered specs** — they were sequenced intentionally
"""
        if focus:
            prompt += f"5. **Focus area requested:** {focus}\n"

        prompt += """
### Step 6: Read the Selected Spec

```bash
cat <path-to-selected-spec.md>
```

Read the FULL spec. Do not summarise from memory.

### Step 7: Create the Implementation Plan

Follow your SKILL.md exactly. Write the plan to `docs/plans/{SPEC-ID}-implementation-plan.md`.

Required sections (from your skill):
- Overview
- Key Findings from Spec
- Implementation Phases (numbered, with dependencies)
- Fallback Phases (what if primary fails)
- Files to Create (full paths)
- Files to Modify (with justification)
- Risks and Unknowns (with severity)
- Test Requirements
- User-Facing Success Criteria
- Definition of Failure
- Estimated Effort (ranges, not points)

### Step 8: Write the Handoff JSON

```bash
cat .claude/skills/workflow-handoff/HANDOFF-SCHEMA.md
```

Re-read the handoff schema IMMEDIATELY before writing. Then write to:
`docs/agent-comms/{SPEC-ID}/001-planner-{timestamp}.json`

The handoff MUST have these as OBJECTS (not strings):
- `metadata` — with specId, agent, timestamp, sequence
- `status` — with outcome, planComplete, unresolvedQuestions, unverifiedAssumptions
- `plan` — with documentPath, phaseCount, estimatedEffort, highestRisk
- `nextAgent` — with target ("builder"), action, context, userFacingGoal

### Step 9: Update .current-spec

```bash
echo "{SPEC-ID}" > docs/agent-comms/.current-spec
```

### Step 10: Print Completion Signal

Print the completion signal defined in your SKILL.md:
```
PLANNER STATUS: COMPLETE
SPEC: {spec-id} — {title}
PLAN: docs/plans/{SPEC-ID}-implementation-plan.md
...
```

---

## CRITICAL REMINDERS

- You MUST read your SKILL.md and HANDOFF-SCHEMA.md before writing anything
- You MUST read the full spec file, not work from memory
- Your handoff JSON MUST pass IWO Pydantic validation or the Builder will never start
- If no suitable next spec exists, write outcome "blocked" and explain why
- Be honest about risks and effort — no optimistic estimates
"""
        if context:
            prompt += f"\n## Additional Context from Operator\n\n{context}\n"

        return prompt

    def _handle_resume(self, data: dict):
        """Resume an interrupted pipeline by re-dispatching the stalled agent.

        Directive format:
        {
            "directive": "resume",
            "specId": "EBATT-010"
        }
        """
        spec_id = data.get("specId")
        if not spec_id:
            log.error("resume directive missing 'specId'")
            self.daemon._notify("❌ resume failed: missing specId")
            return

        # Find LATEST.json for this spec
        spec_dir = self.config.handoffs_dir / spec_id
        latest = spec_dir / "LATEST.json"

        if not latest.exists():
            self.daemon._notify(f"❌ resume failed: no handoffs found for {spec_id}")
            return

        # Parse the latest handoff to determine where the pipeline stalled
        try:
            latest_target = latest.resolve() if latest.is_symlink() else latest
            raw = latest_target.read_text()
            handoff_data = json.loads(raw)

            from .parser import Handoff
            handoff = Handoff.model_validate(handoff_data)

            next_agent = handoff.nextAgent.target
            log.info(
                f"resume: {spec_id} last completed by {handoff.metadata.agent}, "
                f"next agent should be {next_agent}"
            )

            # Try to dispatch the next agent
            success = self.daemon.commander.activate_agent(
                next_agent, handoff=handoff, handoff_path=latest_target,
            )

            if success:
                from .state import AgentState
                self.daemon.agent_states[next_agent] = AgentState.PROCESSING
                self.daemon.pipeline.assign_agent(next_agent, spec_id)
                self.daemon._notify(f"▶️ Resumed {spec_id} — {next_agent} dispatched")
                log.info(f"resume: dispatched {next_agent} for {spec_id}")
            else:
                self.daemon._notify(
                    f"❌ resume failed: {next_agent} not idle or dispatch error"
                )

        except Exception as e:
            log.error(f"resume: failed to parse latest handoff for {spec_id}: {e}")
            self.daemon._notify(f"❌ resume failed for {spec_id}: {e}")

    def _handle_reconcile(self, data: dict):
        """Trigger filesystem reconciliation.

        Directive format:
        { "directive": "reconcile" }
        """
        log.info("reconcile: triggered by directive")
        self.daemon._reconcile_filesystem()
        self.daemon._notify("🔄 Reconciliation completed")

    def _handle_status(self, data: dict):
        """Write pipeline status to a file and send notification.

        Directive format:
        { "directive": "status" }
        """
        from .state import AgentState

        lines = [f"IWO Status Report — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"]
        lines.append("")

        # Agent states
        lines.append("AGENTS:")
        for name in AGENT_ORDER:
            state = self.daemon.agent_states.get(name, AgentState.UNKNOWN)
            lines.append(f"  {name:12s} {state.value}")
        lines.append("")

        # Active pipelines
        active = [
            (sid, info) for sid, info in self.daemon.pipeline.specs.items()
            if info.get("status") == "active"
        ]
        lines.append(f"ACTIVE PIPELINES: {len(active)}")
        for sid, info in active:
            agent = info.get("current_agent", "—")
            count = info.get("handoff_count", 0)
            lines.append(f"  {sid}: agent={agent}, handoffs={count}")
        lines.append("")

        # Queued
        queued = len(self.daemon._pending_activations)
        lines.append(f"QUEUED: {queued}")

        # Write to file
        status_path = self.config.handoffs_dir / ".directives" / ".last-status.txt"
        status_text = "\n".join(lines)
        status_path.write_text(status_text)

        # Send summary notification
        summary = f"Agents: {len([s for s in self.daemon.agent_states.values() if s == AgentState.IDLE])} idle | Active: {len(active)} | Queued: {queued}"
        self.daemon._notify(f"📊 {summary}")
        log.info(f"status: wrote to {status_path}")

    def _handle_pause(self, data: dict):
        """Pause IWO dispatch (agents finish current work but no new dispatch).

        Directive format:
        { "directive": "pause" }
        """
        self.daemon._paused = True
        self.daemon._notify("⏸️ IWO paused — no new dispatches")
        log.info("pause: IWO dispatch paused by directive")

    def _handle_unpause(self, data: dict):
        """Resume IWO dispatch after pause.

        Directive format:
        { "directive": "unpause" }
        """
        self.daemon._paused = False
        self.daemon._notify("▶️ IWO unpaused — dispatch resumed")
        log.info("unpause: IWO dispatch resumed by directive")

    def _handle_cancel_spec(self, data: dict):
        """Cancel an active spec pipeline.

        Directive format:
        {
            "directive": "cancel-spec",
            "specId": "EBATT-011"
        }
        """
        spec_id = data.get("specId")
        if not spec_id:
            log.error("cancel-spec directive missing 'specId'")
            return

        # Remove from pipeline tracking
        if hasattr(self.daemon, 'pipeline') and spec_id in self.daemon.pipeline.specs:
            self.daemon.pipeline.specs[spec_id]["status"] = "cancelled"
            self.daemon.pipeline.specs[spec_id]["current_agent"] = None
            self.daemon._notify(f"🛑 Cancelled pipeline for {spec_id}")
            log.info(f"cancel-spec: cancelled {spec_id}")
        else:
            self.daemon._notify(f"⚠️ {spec_id} not found in active pipelines")
            log.warning(f"cancel-spec: {spec_id} not in pipeline")
