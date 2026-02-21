"""IWO Daemon — Phase 2.4 'Operational Robustness'.

Watches for handoff files, validates them, checks agent state via
state machine, sends canary probe, and routes to next agent.

Phase 2.4.1 additions:
- Automatic crash recovery (respawn-pane + re-launch Claude Code)
- Max 3 respawn attempts with 30s cooldown per agent
- Crash events logged to Neo4j for pattern analysis
- Permanently crashed agents escalate to human notification

Phase 2.3 additions:
- Multi-spec pipeline tracking (PipelineManager)
- Per-agent handoff queuing with rejection-first priority
- All spec dirs scanned (not just .current-spec)
- .active-specs.json for external visibility
- Agent assignment tracking (who's working on what)

Phase 1 foundations:
- Agent state machine (IDLE/PROCESSING/STUCK/WAITING_HUMAN/CRASHED)
- Canary probe before command injection
- Pre-activation state validation
- 30-second filesystem reconciliation
- Pipe-pane archival logging
- Pane tag-based discovery

Design: Three-model consensus (Claude Opus 4.6 + GPT-5.2 + Gemini 3 Pro).
Phase 2.3–2.4 design: Claude Opus 4.6 interactive session, 2026-02-19.
"""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from pydantic import ValidationError
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent

from .config import IWOConfig
from .parser import Handoff
from .commander import TmuxCommander
from .state import AgentState, AgentStateMachine
from .memory import IWOMemory
from .pipeline import PipelineManager
from .metrics import MetricsCollector
from .auditor import Auditor, AuditorConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("iwo.daemon")


class HandoffTracker:
    """Tracks processed handoffs to prevent duplicates (GPT-5.2's idempotency key).

    Phase 2.7: Supports supersede — if a newer file arrives with the same
    idempotency key, it replaces the original (e.g., Reviewer redoes work).
    """

    def __init__(self):
        self._processed: set[str] = set()
        self._processed_paths: dict[str, Path] = {}  # key → path for supersede check
        self._spec_handoff_counts: dict[str, int] = {}
        self._rejection_counts: dict[str, int] = {}  # key: "spec:agent_pair"

    def already_processed(self, handoff: Handoff, path: Optional[Path] = None) -> bool:
        """Check if handoff was already processed.

        If path is provided and a previous file with the same key exists,
        allow supersede if the new file is different (newer version from
        the same agent at the same sequence).
        """
        key = handoff.idempotency_key
        if key not in self._processed:
            return False

        # Same key exists — check for supersede
        if path and key in self._processed_paths:
            prev_path = self._processed_paths[key]
            if path != prev_path:
                log.info(
                    f"Supersede: {key} has newer file {path.name} "
                    f"(replacing {prev_path.name})"
                )
                # Allow re-processing — caller will process the newer version
                self._processed.discard(key)
                return False

        return True

    def mark_processed(self, handoff: Handoff, path: Optional[Path] = None):
        self._processed.add(handoff.idempotency_key)
        if path:
            self._processed_paths[handoff.idempotency_key] = path
        spec = handoff.spec_id
        self._spec_handoff_counts[spec] = self._spec_handoff_counts.get(spec, 0) + 1

    def check_rejection_loop(self, handoff: Handoff, max_loops: int) -> bool:
        """Returns True if rejection loop threshold exceeded."""
        if not handoff.is_rejection:
            return False
        key = f"{handoff.spec_id}:{handoff.source_agent}->{handoff.target_agent}"
        self._rejection_counts[key] = self._rejection_counts.get(key, 0) + 1
        count = self._rejection_counts[key]
        if count >= max_loops:
            log.warning(f"Rejection loop threshold ({max_loops}) hit: {key} ({count} times)")
            return True
        return False

    def check_handoff_limit(self, handoff: Handoff, max_handoffs: int) -> bool:
        """Returns True if handoff count limit exceeded for this spec."""
        count = self._spec_handoff_counts.get(handoff.spec_id, 0)
        return count >= max_handoffs


class HandoffHandler(FileSystemEventHandler):
    """Watchdog handler for new handoff JSON files."""

    def __init__(self, daemon: "IWODaemon"):
        self.daemon = daemon

    def on_created(self, event: FileCreatedEvent):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix != ".json":
            return
        if path.name == "LATEST.json":
            return
        if path.name.endswith(".tmp"):
            return
        if path.name.startswith("007-"):
            log.debug(f"Skipping 007 file: {path.name}")
            return
        # Ignore audit trail files (written by auditor, not handoffs)
        if ".audit" in path.parts:
            log.debug(f"Skipping audit file: {path.name}")
            return
        log.info(f"New handoff detected: {path.name}")
        time.sleep(self.daemon.config.file_debounce_seconds)
        self.daemon.process_handoff(path)


class IWODaemon:
    """Main orchestrator daemon — Phase 1.0."""

    def __init__(self, config: Optional[IWOConfig] = None):
        self.config = config or IWOConfig()
        self.commander = TmuxCommander(self.config)
        self.tracker = HandoffTracker()
        self.observer: Optional[Observer] = None

        # Phase 1: state machines per agent
        self.state_machines: dict[str, AgentStateMachine] = {}

        # Pending activations: handoffs waiting for target agent to become IDLE
        self._pending_activations: list[tuple[Handoff, Path]] = []

        # Phase 2: handoff history for dashboard display (most recent first)
        self.handoff_history: list[Handoff] = []
        self._max_history: int = 50

        # Startup timestamp
        self._started_at: float = time.time()

        # Phase 2.1: Memory integration
        self.memory: Optional[IWOMemory] = None

        # Phase 2.3: Multi-spec pipeline manager
        self.pipeline = PipelineManager(max_concurrent=self.config.max_concurrent_specs)

        # Phase 2.4.1: Crash recovery tracking
        self._respawn_attempts: dict[str, int] = {}  # agent_name → attempt count
        self._respawn_cooldown: dict[str, float] = {}  # agent_name → last attempt time

        # Phase 2.5.1: Metrics collector (initialized after memory)
        self.metrics: Optional[MetricsCollector] = None

        # Phase 3.0: Auditor module (Agent 007 Phase 1)
        self.auditor: Optional[Auditor] = None

    def _init_state_machines(self):
        """Create state machines for all discovered agents."""
        self.state_machines.clear()
        for name, agent_pane in self.commander.agents.items():
            self.state_machines[name] = AgentStateMachine(agent_pane, self.config)
            log.info(f"State machine initialized for {name}")

    def _poll_agent_states(self):
        """Poll all agent state machines. Called every ~2s from main loop."""
        for name, sm in self.state_machines.items():
            prev = sm.state
            current = sm.poll()
            # Log only transitions (poll() already logs them, but we handle side effects here)
            if current == AgentState.WAITING_HUMAN and prev != AgentState.WAITING_HUMAN:
                self._notify(
                    f"🙋 {name} needs human input — check tmux",
                    critical=True,
                )
            elif current == AgentState.STUCK and prev != AgentState.STUCK:
                self._notify(
                    f"⏳ {name} appears stuck (no output for {self.config.stuck_timeout_seconds}s)",
                    critical=True,
                )
            elif current == AgentState.CRASHED and prev != AgentState.CRASHED:
                self._notify(
                    f"💀 {name} has crashed — attempting recovery",
                    critical=True,
                )
                self._attempt_respawn(name)

        # Check if any pending activations can proceed
        self._process_pending_activations()

    def _attempt_respawn(self, agent_name: str):
        """Attempt to respawn a crashed agent. Max 3 attempts with 30s cooldown.

        Phase 2.4.1: Auto-recovery for crashed panes.
        """
        max_attempts = self.config.max_respawn_attempts
        cooldown = self.config.respawn_cooldown_seconds
        now = time.time()

        # Check cooldown
        last_attempt = self._respawn_cooldown.get(agent_name, 0)
        if now - last_attempt < cooldown:
            remaining = int(cooldown - (now - last_attempt))
            log.info(f"Respawn: {agent_name} in cooldown ({remaining}s remaining)")
            return

        # Check attempt count
        attempts = self._respawn_attempts.get(agent_name, 0)
        if attempts >= max_attempts:
            msg = (
                f"💀 {agent_name} permanently crashed — "
                f"exhausted {max_attempts} respawn attempts. Manual intervention required."
            )
            log.error(msg)
            self._notify(msg, critical=True)
            return

        # Attempt respawn
        self._respawn_attempts[agent_name] = attempts + 1
        self._respawn_cooldown[agent_name] = now
        attempt_num = attempts + 1

        log.info(f"Respawn: attempting {agent_name} (attempt {attempt_num}/{max_attempts})")
        self._notify(f"🔄 Respawning {agent_name} (attempt {attempt_num}/{max_attempts})")

        success = self.commander.respawn_agent(agent_name)

        if success:
            # Reset state machine — agent is fresh, will be detected as IDLE on next poll
            sm = self.state_machines.get(agent_name)
            if sm:
                sm.state = AgentState.UNKNOWN
                sm._output_stable_since = 0.0
                sm._cursor_stable_since = 0.0
                sm._last_output_hash = None
                sm._last_cursor = None

            self._notify(f"✅ {agent_name} respawned successfully (attempt {attempt_num})")
            log.info(f"Respawn: {agent_name} recovered on attempt {attempt_num}")

            # Log crash event to memory
            if self.memory:
                try:
                    self._log_crash_event(agent_name, attempt_num, recovered=True)
                except Exception as e:
                    log.warning(f"Memory: crash event logging failed: {e}")
        else:
            self._notify(
                f"❌ {agent_name} respawn failed (attempt {attempt_num}/{max_attempts})",
                critical=True,
            )
            log.warning(f"Respawn: {agent_name} failed on attempt {attempt_num}")

            if self.memory:
                try:
                    self._log_crash_event(agent_name, attempt_num, recovered=False)
                except Exception as e:
                    log.warning(f"Memory: crash event logging failed: {e}")

    def _log_crash_event(self, agent_name: str, attempt: int, recovered: bool):
        """Store crash event to memory for pattern analysis."""
        if not self.memory or not self.memory._neo4j_driver:
            return

        try:
            with self.memory._neo4j_driver.session() as session:
                session.run(
                    """
                    CREATE (c:CrashEvent {
                        agent: $agent,
                        timestamp: datetime(),
                        attempt: $attempt,
                        recovered: $recovered,
                        active_spec: $spec
                    })
                    """,
                    agent=agent_name,
                    attempt=attempt,
                    recovered=recovered,
                    spec=self.pipeline.agent_current_spec(agent_name) or "none",
                )
            log.info(f"Memory: crash event logged for {agent_name}")
        except Exception as e:
            log.warning(f"Memory: crash event log failed: {e}")

    def _run_post_deploy_health_check(self, handoff: Handoff):
        """Hit production URLs after a successful deploy to verify health.

        Phase 2.4.2: Runs after deployer reports success. Checks each URL
        for expected HTTP status within timeout. Non-blocking — failures
        notify but don't halt the pipeline.
        """
        import urllib.request
        import urllib.error

        spec_id = handoff.spec_id
        delay = self.config.health_check_delay
        timeout = self.config.health_check_timeout
        expected = self.config.health_check_expected_status

        log.info(f"Post-deploy health check: {spec_id} — waiting {delay}s for propagation")
        self._notify(f"🏥 Running post-deploy health check for {spec_id}...")
        time.sleep(delay)

        results: list[tuple[str, bool, str]] = []  # (url, passed, detail)

        for url in self.config.health_check_urls:
            try:
                req = urllib.request.Request(url, method="GET")
                req.add_header("User-Agent", "IWO-HealthCheck/2.4")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status = resp.status
                    if status == expected:
                        results.append((url, True, f"HTTP {status}"))
                        log.info(f"Health check PASS: {url} → HTTP {status}")
                    else:
                        results.append((url, False, f"HTTP {status} (expected {expected})"))
                        log.warning(f"Health check FAIL: {url} → HTTP {status}")
            except urllib.error.HTTPError as e:
                results.append((url, False, f"HTTP {e.code}"))
                log.warning(f"Health check FAIL: {url} → HTTP {e.code}")
            except urllib.error.URLError as e:
                results.append((url, False, f"Connection error: {e.reason}"))
                log.warning(f"Health check FAIL: {url} → {e.reason}")
            except Exception as e:
                results.append((url, False, f"Error: {e}"))
                log.warning(f"Health check FAIL: {url} → {e}")

        # Summarize
        passed = sum(1 for _, ok, _ in results if ok)
        total = len(results)

        if passed == total:
            msg = f"✅ Post-deploy health check PASSED for {spec_id} ({passed}/{total} URLs)"
            log.info(msg)
            self._notify(msg)
        else:
            failed_details = [
                f"  {url}: {detail}" for url, ok, detail in results if not ok
            ]
            msg = (
                f"⚠️ Post-deploy health check FAILED for {spec_id} "
                f"({passed}/{total} passed)\n" + "\n".join(failed_details)
            )
            log.error(msg)
            self._notify(
                f"⚠️ HEALTH CHECK FAILED: {spec_id} — {total - passed} URL(s) down. "
                f"Consider rollback.",
                critical=True,
            )

        # Log to memory
        if self.memory and self.memory._neo4j_driver:
            try:
                with self.memory._neo4j_driver.session() as session:
                    session.run(
                        """
                        CREATE (h:HealthCheck {
                            spec_id: $spec_id,
                            timestamp: datetime(),
                            passed: $passed,
                            total: $total,
                            all_passed: $all_passed,
                            details: $details
                        })
                        """,
                        spec_id=spec_id,
                        passed=passed,
                        total=total,
                        all_passed=passed == total,
                        details=json.dumps(
                            [{u: d} for u, _, d in results]
                        ),
                    )
            except Exception as e:
                log.warning(f"Memory: health check logging failed: {e}")

    def _process_pending_activations(self):
        """Try to activate agents for queued handoffs whose target is now IDLE.

        Phase 2.3: Uses PipelineManager queue with rejection-first priority.
        Also checks the legacy _pending_activations list for backward compat.
        """
        # Legacy pending list (Phase 1 — migrate items to pipeline queue)
        if self._pending_activations:
            for handoff, path in self._pending_activations:
                self.pipeline.enqueue(handoff, path)
            self._pending_activations.clear()
            log.info("Migrated legacy pending activations to pipeline queue")

        # Check each agent's queue
        for name, sm in self.state_machines.items():
            if sm.state != AgentState.IDLE:
                continue
            if self.pipeline.is_agent_busy(name):
                continue  # Agent has an assignment but state shows IDLE — timing gap

            queued = self.pipeline.dequeue(name)
            if queued:
                self._activate_for_handoff(name, queued.handoff, queued.path)

    def _activate_for_handoff(self, agent: str, handoff: Handoff, path: Path):
        """Send activation command to an agent and update pipeline tracking."""
        log.info(f"Activating {agent} for {handoff.spec_id} #{handoff.sequence}")
        success = self.commander.activate_agent(agent)
        if success:
            sm = self.state_machines.get(agent)
            if sm:
                sm.mark_command_sent()
            self.pipeline.assign_agent(agent, handoff.spec_id)
            self._notify(f"✅ Activated {agent} for {handoff.spec_id} (#{handoff.sequence})")
            # Emit INFO audit event for phone notification of successful handoffs
            if self.auditor:
                from iwo.auditor import AuditEvent, Severity
                self.auditor._emit(AuditEvent(
                    timestamp=self.auditor._now_iso(),
                    check="handoff_success",
                    severity=Severity.INFO,
                    spec_id=handoff.spec_id,
                    details={
                        "agent": agent,
                        "sequence": handoff.sequence,
                        "message": f"✅ {agent} activated for {handoff.spec_id} (#{handoff.sequence})",
                    },
                    action_taken=f"activated_{agent}",
                    recommended_action=None,
                ))
        else:
            # Re-queue on failure — will retry next poll cycle
            self.pipeline.enqueue(handoff, path)
            self._notify(f"❌ Failed to activate {agent}, re-queued", critical=True)

    def _should_auto_approve_deploy(self, path: Path, handoff: Handoff) -> bool:
        """Check if a deploy handoff can bypass the human gate.

        Auto-approves when the handoff explicitly declares no infrastructure
        changes (no new migrations, secrets, or wrangler vars). If the flags
        are absent or any flag indicates changes, requires human approval.

        Reads the raw JSON because these fields are in deploymentInstructions
        which is not part of the Pydantic Handoff model.

        Industry best practice: auto-deploy for low-risk (code-only) changes,
        require human approval for high-risk (infra/DB/secrets) changes.
        """
        if not self.config.auto_approve_safe_deploys:
            return False  # Feature disabled — always gate

        try:
            with open(path) as f:
                raw = json.load(f)
        except Exception:
            log.warning("Auto-approve: cannot read raw handoff, requiring manual approval")
            return False

        # Check deploymentInstructions block (used by Planner/Tester handoffs)
        deploy_info = raw.get("deploymentInstructions", {})

        no_migrations = deploy_info.get("noNewMigrations", False)
        no_secrets = deploy_info.get("noNewSecrets", False)
        no_vars = deploy_info.get("noNewWranglerVars", False)

        # All three must be explicitly True for auto-approval
        if no_migrations and no_secrets and no_vars:
            log.info(
                f"Auto-approve check: migrations={no_migrations}, "
                f"secrets={no_secrets}, vars={no_vars} → SAFE"
            )
            return True

        # Log which flags are missing or false
        flags = {
            "noNewMigrations": no_migrations,
            "noNewSecrets": no_secrets,
            "noNewWranglerVars": no_vars,
        }
        missing = [k for k, v in flags.items() if not v]
        log.info(
            f"Auto-approve check: UNSAFE — missing or false: {missing}"
        )
        return False

    def process_handoff(self, path: Path):
        """Parse, validate, and route a handoff file.

        Phase 2.3: Pipeline-aware routing with per-agent queuing and
        rejection-first priority.
        """
        # 1. Parse and validate
        try:
            with open(path) as f:
                data = json.load(f)
            handoff = Handoff(**data)
        except json.JSONDecodeError as e:
            log.error(f"Invalid JSON in {path.name}: {e}")
            self._notify(f"Invalid JSON: {path.name}", critical=True)
            return
        except ValidationError as e:
            log.error(f"Handoff validation failed for {path.name}: {e}")
            self._notify(f"Invalid handoff structure: {path.name}", critical=True)
            return

        log.info(
            f"Handoff #{handoff.sequence}: "
            f"{handoff.source_agent} → {handoff.target_agent} "
            f"[{handoff.status.outcome}] ({handoff.spec_id})"
        )

        # 2. Idempotency check (with supersede support for same-sequence redos)
        if self.tracker.already_processed(handoff, path):
            log.info(f"Already processed {handoff.idempotency_key}, skipping")
            return

        # 3. Safety rails
        if self.tracker.check_handoff_limit(handoff, self.config.max_handoffs_per_spec):
            msg = f"HALT: {handoff.spec_id} exceeded {self.config.max_handoffs_per_spec} handoffs"
            log.error(msg)
            self._notify(msg, critical=True)
            self.pipeline.mark_halted(handoff.spec_id, "handoff limit exceeded")
            return

        if self.tracker.check_rejection_loop(handoff, self.config.max_rejection_loops):
            msg = f"HALT: Rejection loop detected in {handoff.spec_id}"
            log.error(msg)
            self._notify(msg, critical=True)
            self.pipeline.mark_halted(handoff.spec_id, "rejection loop")
            return

        # 4. Mark processed and record in history
        self.tracker.mark_processed(handoff, path)
        self.handoff_history.insert(0, handoff)
        if len(self.handoff_history) > self._max_history:
            self.handoff_history.pop()

        # 5. Pipeline bookkeeping: update spec pipeline, release source agent
        self.pipeline.record_handoff(handoff)

        # 5.1 Store to memory (best-effort, non-blocking)
        if self.memory:
            try:
                proc_start = time.monotonic()
                self.memory.store_handoff(handoff, processing_time_ms=0)
                proc_ms = (time.monotonic() - proc_start) * 1000
                log.info(f"Memory: stored handoff in {proc_ms:.0f}ms")
            except Exception as e:
                log.warning(f"Memory: store failed (non-fatal): {e}")

        # 6. Update LATEST.json symlink
        self._update_latest(path, handoff)

        # 6.1 Stamp canonical received_at time (agents fabricate timestamps)
        try:
            with open(path) as f:
                raw = json.load(f)
            raw.setdefault("metadata", {})["received_at"] = (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            )
            with open(path, "w") as f:
                json.dump(raw, f, indent=2)
        except Exception as e:
            log.warning(f"Could not stamp received_at on {path.name}: {e}")

        # 7. Write .active-specs.json for external visibility
        self._write_active_specs()

        # 7.1 Post-deploy health check (Phase 2.4.2)
        if (handoff.source_agent == "deployer"
                and handoff.status.outcome == "success"
                and self.config.health_check_urls):
            self._run_post_deploy_health_check(handoff)

        # 8. Human gate check (conditional: auto-approve if no infrastructure changes)
        target = handoff.target_agent
        if target in self.config.human_gate_agents:
            if self._should_auto_approve_deploy(path, handoff):
                log.info(
                    f"Deploy auto-approved for {handoff.spec_id}: "
                    f"no migrations, secrets, or wrangler vars"
                )
                self._notify(
                    f"✅ AUTO-DEPLOY: {handoff.spec_id} → {target} "
                    f"(no infra changes, bypassing gate)"
                )
                # Fall through to step 9 routing instead of returning
            else:
                msg = (
                    f"🚦 DEPLOY GATE: {handoff.spec_id} ready for {target}. "
                    f"Infrastructure changes detected — manual approval required. "
                    f"Action: {handoff.nextAgent.action[:100]}"
                )
                log.info(msg)
                self._notify(msg, critical=True)
                return

        # 8.5 Terminal targets — pipeline complete, no activation needed
        if target in ("human", "none"):
            self.pipeline.mark_completed(handoff.spec_id)
            self._write_active_specs()
            self._notify(
                f"🏁 {handoff.spec_id} → {target} (pipeline complete, "
                f"{handoff.source_agent} was final agent)"
            )
            log.info(
                f"Pipeline complete: {handoff.spec_id} → {target} "
                f"(terminal target, no activation)"
            )
            return

        # 9. Route to target agent (pipeline-aware)
        sm = self.state_machines.get(target)
        if not sm:
            log.warning(f"No state machine for {target}, activating directly")
            time.sleep(2)
            self._activate_for_handoff(target, handoff, path)
            return

        agent_busy_with = self.pipeline.agent_current_spec(target)
        target_idle = sm.state == AgentState.IDLE

        if target_idle and not agent_busy_with:
            # Agent is idle and free — activate immediately
            log.info(f"Target {target} is IDLE and free — activating now")
            time.sleep(1)
            self._activate_for_handoff(target, handoff, path)

        elif target_idle and agent_busy_with and agent_busy_with != handoff.spec_id:
            # Agent shows IDLE but pipeline thinks it's on another spec.
            # This likely means the agent finished but we haven't seen the
            # handoff yet (timing). Release and activate.
            log.info(
                f"Target {target} is IDLE but assigned to {agent_busy_with} — "
                f"releasing stale assignment, activating for {handoff.spec_id}"
            )
            self.pipeline.release_agent(target)
            time.sleep(1)
            self._activate_for_handoff(target, handoff, path)

        elif agent_busy_with == handoff.spec_id:
            # Same spec — could be a re-injection or timing overlap. Activate.
            log.info(f"Target {target} already assigned to {handoff.spec_id} — activating")
            time.sleep(1)
            self._activate_for_handoff(target, handoff, path)

        elif sm.state in (AgentState.PROCESSING, AgentState.UNKNOWN):
            # Agent is busy (likely with another spec) — queue
            reason = f"agent is {sm.state.value}"
            if agent_busy_with:
                reason += f" on {agent_busy_with}"
            log.info(f"Target {target} busy ({reason}) — queuing {handoff.spec_id}")
            self.pipeline.enqueue(handoff, path)
            self._notify(
                f"⏸️ Queued {handoff.spec_id} → {target} ({reason})"
            )

        elif sm.state in (AgentState.STUCK, AgentState.WAITING_HUMAN, AgentState.CRASHED):
            # Agent in bad state — queue and notify
            msg = (
                f"⚠️ Target {target} is {sm.state.value} — "
                f"queuing {handoff.spec_id}"
            )
            log.warning(msg)
            self._notify(msg, critical=True)
            self.pipeline.enqueue(handoff, path)

        else:
            # Fallback: queue it
            log.info(f"Target {target}: unexpected state, queuing {handoff.spec_id}")
            self.pipeline.enqueue(handoff, path)

        # 10. After processing, check if the released source agent has queued work
        source = handoff.source_agent
        source_sm = self.state_machines.get(source)
        if source_sm and source_sm.state == AgentState.IDLE:
            if not self.pipeline.is_agent_busy(source):
                queued = self.pipeline.dequeue(source)
                if queued:
                    log.info(f"Queue drain: {source} freed from {handoff.spec_id}, activating queued work")
                    self._activate_for_handoff(source, queued.handoff, queued.path)

        # 11. Auditor: post-handoff invariant checks (best-effort)
        if self.auditor:
            try:
                self.auditor.post_handoff_checks(handoff)
            except Exception as e:
                log.warning(f"Auditor post-handoff check failed (non-fatal): {e}")

    def _reconcile_filesystem(self):
        """Periodic scan to catch missed inotify events. Called every 30s.

        Phase 2.3: Scans ALL spec subdirectories, not just .current-spec.
        """
        if not self.config.handoffs_dir.exists():
            return

        # Scan all subdirectories that look like spec dirs
        for spec_dir in sorted(self.config.handoffs_dir.iterdir()):
            if not spec_dir.is_dir():
                continue
            if spec_dir.name.startswith("."):
                continue  # Skip .current-spec etc.

            json_files = sorted(spec_dir.glob("*.json"))
            json_files = [
                f for f in json_files
                if f.name != "LATEST.json"
                and not f.name.endswith(".tmp")
                and not f.name.startswith("007-")
            ]

            missed = 0
            for f in json_files:
                try:
                    with open(f) as fh:
                        data = json.load(fh)
                    handoff = Handoff(**data)
                    if not self.tracker.already_processed(handoff):
                        log.info(f"Reconciliation: found missed handoff {f.name}")
                        self.process_handoff(f)
                        missed += 1
                except Exception:
                    pass

            if missed > 0:
                log.info(f"Reconciliation: processed {missed} missed handoff(s) for {spec_dir.name}")

    def _update_latest(self, handoff_path: Path, handoff: Handoff):
        """Update LATEST.json as a symlink to the current handoff."""
        spec_dir = handoff_path.parent
        latest = spec_dir / "LATEST.json"
        try:
            if latest.exists() or latest.is_symlink():
                latest.unlink()
            latest.symlink_to(handoff_path.name)
            log.info(f"LATEST.json → {handoff_path.name}")
        except Exception as e:
            log.warning(f"Failed to update LATEST.json: {e}")

    def _write_active_specs(self):
        """Write .active-specs.json for external visibility (TUI, other tools).

        Also maintains .current-spec for backward compatibility (set to most recent active spec).
        """
        try:
            specs_file = self.config.handoffs_dir / ".active-specs.json"
            state = self.pipeline.to_dict()
            with open(specs_file, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.warning(f"Failed to write .active-specs.json: {e}")

        # Backward compat: .current-spec = most recently active spec
        try:
            active = [
                p for p in self.pipeline.all_pipelines if p.status == "active"
            ]
            if active:
                current_spec_file = self.config.handoffs_dir / ".current-spec"
                current_spec_file.write_text(active[0].spec_id)
        except Exception:
            pass

    def _notify(self, message: str, critical: bool = False):
        """Send notification via configured channels (desktop, webhook, or both)."""
        channels = self.config.notification_channels

        if "desktop" in channels:
            self._notify_desktop(message, critical)

        if "webhook" in channels:
            self._notify_webhook(message, critical)

    def _notify_desktop(self, message: str, critical: bool = False):
        """Send desktop notification via notify-send."""
        urgency = "critical" if critical else "normal"
        try:
            subprocess.run(
                ["notify-send", "-u", urgency, "-a", "IWO", "Ivan's Workflow", message],
                timeout=5,
                capture_output=True,
            )
        except Exception as e:
            log.warning(f"notify-send failed: {e}")

    def _notify_webhook(self, message: str, critical: bool = False):
        """Send notification via webhook (e.g., n8n) as JSON POST."""
        url = self.config.notification_webhook_url
        if not url:
            log.debug("Webhook notification skipped: no URL configured")
            return

        import json
        import time as _time
        from urllib.request import Request, urlopen
        from urllib.error import URLError

        # Build context-rich payload for n8n processing
        active_specs = [p.spec_id for p in self.pipeline.get_active()]
        payload = json.dumps({
            "source": "iwo",
            "message": message,
            "critical": critical,
            "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "active_specs": active_specs,
            "version": "2.5.2",
        }).encode("utf-8")

        req = Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=self.config.notification_webhook_timeout) as resp:
                log.debug(f"Webhook notification sent: {resp.status}")
        except URLError as e:
            log.warning(f"Webhook notification failed: {e}")
        except Exception as e:
            log.warning(f"Webhook notification error: {e}")

    def setup(self) -> bool:
        """Initialize daemon: connect to tmux, set up state machines, recover state, start watcher.

        Returns True if setup succeeded. Called by both headless start() and TUI mode.
        Does NOT enter the main loop — call run_loop() for headless or let TUI drive polling.
        """
        log.info("=" * 60)
        log.info("IWO — Ivan's Workflow Orchestrator v1.0")
        log.info("Phase 1: State Machine + Canary Probes + Tag Discovery")
        log.info("=" * 60)

        # 1. Connect to tmux and discover agents (with tag-based discovery)
        if not self.commander.connect():
            log.error("Cannot connect to tmux session. Is the workflow running?")
            return False

        # 2. Set up agent environments (pipe-pane archival)
        self.commander.setup_agent_environments()

        # 3. Initialize state machines for all discovered agents
        self._init_state_machines()

        # 4. Scan for current state (stateless recovery)
        self._recover_state()

        # 5. Start filesystem watcher
        handler = HandoffHandler(self)
        self.observer = Observer()
        watch_path = str(self.config.handoffs_dir)
        self.observer.schedule(handler, watch_path, recursive=True)
        self.observer.start()
        log.info(f"Watching: {watch_path}")

        # 6. Initialize memory integration (best-effort)
        if self.config.enable_memory:
            self.memory = IWOMemory(self.config)
            if self.memory.initialize():
                log.info("Memory integration active")
            else:
                log.warning("Memory integration unavailable — continuing without persistence")

        # 7. Initialize metrics collector (uses memory's Neo4j connection)
        self.metrics = MetricsCollector(self.memory)
        log.info("Metrics collector initialized")

        # 8. Initialize auditor module (Agent 007 Phase 1)
        try:
            self.auditor = Auditor(self, AuditorConfig(
                webhook_url=self.config.notification_webhook_url,
            ))
            log.info("Auditor module initialized")

            # Check if Agent 007 is already running on startup
            if not self.commander.check_agent_007_idle():
                self.auditor._007_active = True
                log.info("Agent 007 appears active on startup — marking as running")
        except Exception as e:
            log.warning(f"Auditor initialization failed (non-fatal): {e}")
            self.auditor = None

        self._notify("IWO v1.0 started — state machine active")
        return True

    def run_loop(self):
        """Headless main loop: state polling + reconciliation on tick counters.

        For TUI mode, the Textual app drives polling via set_interval instead.
        """
        poll_every = max(1, int(self.config.state_poll_interval_seconds))
        recon_every = self.config.reconciliation_interval_seconds
        tick = 0

        try:
            while True:
                time.sleep(1)
                tick += 1

                if tick % poll_every == 0:
                    self._poll_agent_states()

                if tick % recon_every == 0:
                    self._reconcile_filesystem()

                # Auditor periodic checks (self-throttles to 5-min intervals)
                if self.auditor:
                    try:
                        self.auditor.periodic_checks()
                        completion = self.auditor.check_007_completion()
                        if completion:
                            log.info(f"Agent 007 completed: {completion.get('outcome')}")
                    except Exception as e:
                        log.warning(f"Auditor check failed (non-fatal): {e}")

        except KeyboardInterrupt:
            log.info("Shutting down...")
            self.observer.stop()
        self.observer.join()
        log.info("IWO stopped.")

    def shutdown(self):
        """Clean shutdown — stop observer, close memory, and log."""
        if self.observer:
            self.observer.stop()
            self.observer.join()
        if self.memory:
            self.memory.close()
        log.info("IWO stopped.")

    def start(self):
        """Start the daemon: connect to tmux, init state machines, watch for handoffs."""
        if not self.setup():
            sys.exit(1)
        self.run_loop()

    def _recover_state(self):
        """Scan filesystem to reconstruct state after restart.

        Phase 2.3: Scans ALL spec directories and rebuilds pipeline state.
        """
        if not self.config.handoffs_dir.exists():
            log.info("No handoffs directory found")
            return

        total_specs = 0
        total_handoffs = 0

        for spec_dir in sorted(self.config.handoffs_dir.iterdir()):
            if not spec_dir.is_dir():
                continue
            if spec_dir.name.startswith("."):
                continue

            spec_id = spec_dir.name
            json_files = sorted(spec_dir.glob("*.json"))
            json_files = [
                f for f in json_files
                if f.name != "LATEST.json"
                and not f.name.endswith(".tmp")
                and not f.name.startswith("007-")
            ]

            if not json_files:
                continue

            handoff_pairs: list[tuple[Handoff, Path]] = []
            for f in json_files:
                try:
                    with open(f) as fh:
                        data = json.load(fh)
                    handoff = Handoff(**data)
                    self.tracker.mark_processed(handoff, f)
                    handoff_pairs.append((handoff, f))
                except Exception:
                    pass

            handoffs = [h for h, _ in handoff_pairs]

            if handoffs:
                self.pipeline.recover_from_handoffs(spec_id, handoffs)
                total_specs += 1
                total_handoffs += len(handoffs)

                # Check if pipeline reached a terminal state
                latest_handoff, latest_path = handoff_pairs[-1]
                target = latest_handoff.target_agent

                if target in ("human", "none"):
                    # Pipeline is complete — mark it and skip unrouted check
                    self.pipeline.mark_completed(spec_id)
                    log.info(
                        f"Recovery: {spec_id} pipeline complete "
                        f"(target={target})"
                    )
                    continue

                # Phase 2.6: Detect unrouted handoffs — the latest handoff
                # for this spec may never have been dispatched to the target
                # agent (e.g., if IWO restarted after the file was written
                # but before routing occurred). Check if the target agent
                # has produced a subsequent handoff; if not, queue it.
                #
                # Note: we already checked if the LATEST handoff targets
                # human/none above (marks pipeline complete). For multi-sprint
                # specs, earlier sprints may have targeted human but the
                # current sprint is active — that's fine, only the latest
                # handoff matters for unrouted detection.

                target_responded = any(
                    h.source_agent == target
                    and h.sequence > latest_handoff.sequence
                    for h in handoffs
                )
                if not target_responded:
                    # Only queue if the handoff file is recent (last 24h).
                    # Old unrouted handoffs from abandoned specs should not
                    # be force-dispatched on every restart.
                    file_age_hours = (
                        time.time() - latest_path.stat().st_mtime
                    ) / 3600
                    if file_age_hours > 24:
                        log.info(
                            f"Recovery: {spec_id} has unrouted handoff "
                            f"#{latest_handoff.sequence} but file is "
                            f"{file_age_hours:.0f}h old — skipping"
                        )
                        continue

                    # Remove from tracker so process_handoff won't skip it
                    self.tracker._processed.discard(
                        latest_handoff.idempotency_key
                    )
                    self._pending_activations.append(
                        (latest_handoff, latest_path)
                    )
                    log.info(
                        f"Recovery: {spec_id} has UNROUTED handoff "
                        f"#{latest_handoff.sequence} "
                        f"{latest_handoff.source_agent}→{target} "
                        f"— queuing for activation"
                    )

        log.info(
            f"Recovery: {total_specs} spec(s), {total_handoffs} handoff(s) recovered"
        )
        if self._pending_activations:
            log.info(
                f"Recovery: {len(self._pending_activations)} unrouted "
                f"handoff(s) queued for activation"
            )
        self._write_active_specs()


def main():
    """Entry point."""
    config = IWOConfig()
    if root := os.environ.get("IWO_PROJECT_ROOT"):
        config.project_root = Path(root)
        config.handoffs_dir = config.project_root / "docs" / "agent-comms"

    daemon = IWODaemon(config)
    daemon.start()


if __name__ == "__main__":
    main()
