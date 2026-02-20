# Agent 007: Pipeline Supervisor — Implementation Plan

**Version:** 1.0
**Created:** 2026-02-20
**Author:** Vanya + Claude Opus 4.6 (interactive session)
**Project:** Ivan's Workflow Orchestrator (IWO)

---

## Vision

An autonomous pipeline supervisor that keeps IWF/IWO running overnight with minimal human intervention. Three layers of increasing intelligence: deterministic health checks, AI-powered diagnosis with bounded retry authority, and (future) self-healing infrastructure modification.

## Architecture Overview

```
IWO Daemon (existing)
  ├── Auditor Module (Layer 1 — deterministic Python)
  │   ├── Post-handoff invariant checks
  │   ├── Timer-based liveness checks (every 5 min)
  │   ├── Writes diagnostics to docs/agent-comms/.audit/
  │   └── Sends webhook to n8n on anomaly detection
  │
  ├── Agent 007 (Layer 2 — Claude Code in tmux pane 7)
  │   ├── Triggered by Auditor when anomaly requires AI judgment
  │   ├── Reads handoff chain + IWO logs + audit diagnostics
  │   ├── CAN: retry failed steps, restart stalled agents, skip non-critical failures
  │   ├── CANNOT: modify IWO or IWF source code (hardcoded safety rail)
  │   └── Reports diagnosis + actions to Matrix via n8n
  │
  └── n8n Notification Workflow
      ├── Webhook receiver (from IWO auditor + Agent 007)
      ├── Matrix node → Element room (mobile push)
      ├── Severity-based message routing
      └── Optional: SmythOS webhook for dashboard visualization

SmythOS (Layer 3 — external, future)
  ├── Visual dashboard showing pipeline state
  ├── Historical metrics and trend analysis
  ├── AI diagnostician for infrastructure bugs
  └── Can roll back its own changes (detached from IWF/IWO)
```

## Notification Tiers

| Severity | Matrix Behavior | Examples |
|----------|----------------|----------|
| 🟢 Info | Silent message | Sprint started, agent completed handoff, pipeline progressing |
| 🟡 Warning | Normal notification | Agent working >45min, timestamp anomaly, non-critical skip |
| 🔴 Critical | Highlight/ping | Pipeline stalled after retry, deploy failure, rejection loop |
| 💀 Fatal | Ping + repeated | IWO daemon crash, Agent 007 exhausted retries, infrastructure bug detected |

---

## Phase 1: IWO Auditor Module (`iwo/auditor.py`)

**Goal:** Deterministic health monitoring integrated into the IWO daemon poll loop.
**Risk:** Zero — read-only checks with notification output.
**Estimated effort:** 1-2 sessions.

### 1.1 Check Catalogue

| Check | Trigger | Threshold | Action |
|-------|---------|-----------|--------|
| Agent liveness | After activation | No handoff within 30 min | Warning → n8n webhook |
| Agent timeout | Timer (5 min poll) | Agent working >60 min | Critical → n8n webhook |
| Pipeline consistency | After each handoff | Status doesn't match latest handoff | Auto-fix + log |
| Sequence continuity | After each handoff | Gap or unexpected duplicate | Warning → log |
| Timestamp sanity | After each handoff | `received_at` vs `metadata.timestamp` drift >1h | Warning → log |
| Stale assignment | Timer (5 min poll) | Agent assigned to completed pipeline | Auto-release + log |
| Queue inflation | Timer (5 min poll) | Queue depth >5 for any agent | Warning → n8n webhook |
| Daemon heartbeat | Timer (60s) | Write heartbeat file, external monitor checks | Fatal if stale |

### 1.2 Diagnostic Output Schema

```json
{
  "timestamp": "2026-02-20T03:18:04Z",
  "check": "agent_liveness",
  "severity": "warning",
  "spec_id": "AI-INFRASTRUCTURE",
  "details": {
    "agent": "deployer",
    "activated_at": "2026-02-20T03:00:00Z",
    "minutes_elapsed": 18,
    "threshold_minutes": 30,
    "state_machine_state": "ACTIVE",
    "tmux_pane_responsive": true
  },
  "action_taken": null,
  "recommended_action": "monitor — approaching timeout threshold"
}
```

### 1.3 Integration Points

- **IWO daemon:** Call `auditor.post_handoff_checks(handoff)` after step 9 in `process_handoff()`.
- **IWO poll loop:** Call `auditor.periodic_checks()` every poll cycle.
- **n8n webhook:** POST to `http://localhost:5678/webhook/iwo-audit` with diagnostic JSON.
- **Filesystem:** Write to `docs/agent-comms/.audit/{timestamp}-{check}.json` for history.
- **Heartbeat:** Write `docs/agent-comms/.audit/heartbeat.json` every 60s with daemon PID and timestamp.

### 1.4 Deliverables

- [x] `iwo/auditor.py` — Auditor class with all checks (616 lines)
- [x] Integration into `daemon.py` (post-handoff + poll loop hooks, +28 lines)
- [x] n8n webhook workflow (IWO Audit Event Receiver, ID: `mSKU23aEzwqPJSZy`, active)
- [x] ntfy mobile notifications (topic: `iwo-vanya-8v8-audit`, all 4 severity levels tested)
- [x] Unit tests for each check (29 tests, `tests/test_auditor.py`, 559 lines)
- [x] Documentation update to TECHNICAL-DOCUMENTATION.md (Phase 2.8)
- [ ] End-to-end test: run IWO daemon with auditor enabled, trigger real check (not manual curl)

---

## Phase 2: n8n → Mobile Notification Pipeline (ntfy)

**Goal:** Route audit events and Agent 007 reports to mobile with priority-based alerting.
**Risk:** Low — notification infrastructure, no code modification authority.
**Estimated effort:** 1 session.
**Prerequisite:** Phase 1 complete.
**Status:** ✅ COMPLETE (2026-02-21)

### 2.0 Design Decision: ntfy over Matrix

The original plan called for Matrix (self-hosted Synapse + Element room + bot account). During implementation, ntfy was chosen instead for the following reasons:

| Factor | Matrix | ntfy |
|--------|--------|------|
| Infrastructure | Synapse homeserver + bot account + Element room | Zero — uses ntfy.sh public server |
| Setup time | 2-4 hours (server, DNS, bot, room) | 5 minutes (app install + topic subscribe) |
| Mobile push | Via Element app + push gateway | Native ntfy app with instant delivery |
| Priority routing | Room notification rules (limited) | Built-in priority levels (1-5) with DND bypass |
| Battery | Element background service | 0-1% per 17h with instant delivery |
| Multi-user | Better (room-based, history, threads) | Adequate (topic subscription, no history) |

**Decision:** ntfy for Phase 2. Matrix can be added in Phase 3 or later if multi-user monitoring is needed. The n8n workflow is designed so adding a Matrix node alongside ntfy is trivial.

### 2.1 n8n Workflow: IWO Audit Event Receiver

- **Workflow ID:** `mSKU23aEzwqPJSZy`
- **Status:** Active
- **Webhook URL:** `https://n8n.ethospower.org/webhook/iwo-audit`
- **Method:** POST with JSON body

**Architecture (7 nodes):**
```
Webhook (POST /iwo-audit, responseMode: responseNode, typeVersion 2.1)
  → Switch "Route by Severity" (typeVersion 3.2, routes on $json.body.event.severity)
    → [info]     → HTTP Request to ntfy (priority: low, tag: information_source)     → Respond OK
    → [warning]  → HTTP Request to ntfy (priority: default, tag: warning)            → Respond OK
    → [critical] → HTTP Request to ntfy (priority: high, tag: rotating_light)        → Respond OK
    → [fatal]    → HTTP Request to ntfy (priority: urgent, tag: skull)               → Respond OK
```

**ntfy Configuration:**
- Topic: `iwo-vanya-8v8-audit`
- URL: `https://ntfy.sh/iwo-vanya-8v8-audit`
- All HTTP Request nodes: typeVersion 4.4, method POST, contentType raw/text/plain
- Headers: Priority, Tags, Title (expression: `{{ "IWO [SEVERITY]: " + $json.body.event.check }}`)
- Body: `$json.body.event.details.message + "\n\nAction: " + $json.body.event.recommended_action`
- Respond OK: typeVersion 1.1, respondWith json, returns `{status, severity, ts}`

**Severity → ntfy Priority Mapping:**

| Severity | ntfy Priority | Tag Emoji | Mobile Behavior |
|----------|---------------|-----------|-----------------|
| info | low (2) | ℹ️ information_source | Silent, badge only |
| warning | default (3) | ⚠️ warning | Normal notification sound |
| critical | high (4) | 🚨 rotating_light | Loud notification, LED |
| fatal | urgent (5) | 💀 skull | Alarm, bypasses DND |

### 2.2 n8n MCP Tooling

- **MCP server:** `mcp-n8n-builder` (spences10/mcp-n8n-builder)
- **Zod v3/v4 compatibility patch:** The package was written for Zod v3 but `npx` resolves Zod v4.3.6. Patched `schemas.js` to import from `'zod/v3'` (v4 ships a v3 compat shim).
- **Wrapper script:** `/home/vanya/scripts/mcp-n8n-builder-wrapper.sh` — auto-applies Zod patch before launch, survives npx cache clears.
- **Claude Desktop config:** Updated to use wrapper script instead of direct `npx`.
- **N8N_HOST:** Must be `https://n8n.ethospower.org` (NOT `.../api/v1` — the MCP appends `/api/v1` itself).
- **Capabilities confirmed working:** list_workflows, activate_workflow, deactivate_workflow, delete_workflow, list_executions, get_execution. `create_workflow` works after Zod patch. For complex workflows, direct `curl` to the n8n API is a reliable fallback.

### 2.3 Mobile Setup

- ntfy app installed on phone (Android/iOS)
- Subscribed to topic `iwo-vanya-8v8-audit`
- Instant delivery enabled (bypasses Android Doze mode for immediate delivery)
- All 4 severity levels tested and verified (2026-02-21)

### 2.4 Test Command

```bash
curl -X POST https://n8n.ethospower.org/webhook/iwo-audit \
  -H "Content-Type: application/json" \
  -d '{
    "event": {
      "timestamp": "2026-02-21T08:00:00Z",
      "check": "test_connectivity",
      "severity": "info",
      "spec_id": null,
      "details": {"message": "IWO Auditor webhook connectivity test"},
      "action_taken": "none",
      "recommended_action": "none"
    },
    "daemon_version": "2.8.0"
  }'
```

### 2.5 Deliverables

- [x] n8n workflow created and activated (ID: `mSKU23aEzwqPJSZy`)
- [x] Severity-based routing to ntfy with priority mapping
- [x] Message formatting with check name, details, and recommended action
- [x] ntfy mobile app configured with instant delivery
- [x] All 4 severity levels tested end-to-end (webhook → ntfy → phone)
- [x] n8n MCP tooling working (Zod patch, wrapper script)
- [x] Credentials stored in Bitwarden (n8n API key)

---

## Phase 3: Agent 007 — AI Pipeline Supervisor

**Goal:** Claude Code agent in tmux pane 7 that diagnoses and retries stalled pipelines.
**Risk:** Medium — can restart agents but CANNOT modify IWO/IWF code.
**Estimated effort:** 2-3 sessions.
**Prerequisite:** Phase 1 and Phase 2 complete.

### 3.1 Trigger Mechanism

Agent 007 is NOT a pipeline agent (doesn't receive handoffs in the normal chain).
Triggered by the Auditor module when anomalies exceed retry-safe thresholds:

```python
# In auditor.py
if check.severity >= CRITICAL and check.is_retry_safe:
    self.trigger_agent_007(check)
```

IWO activates pane 7 with a structured prompt containing the diagnostic context.

### 3.2 Authority Boundaries (Safety Rails)

**Agent 007 CAN:**
- Read all handoff files, IWO logs, .active-specs.json, audit diagnostics
- Send `/workflow-next` to a stalled agent (restart the current step)
- Write a "retry" handoff that re-routes to the same agent
- Skip a non-critical failure and advance the pipeline (e.g., skip docs agent if deployer succeeded)
- Write diagnostic reports to `docs/agent-comms/.audit/007-{timestamp}.json`
- Send notifications via n8n webhook

**Agent 007 CANNOT:**
- Modify any file in `iwo/` directory (IWO source code)
- Modify any file in `.claude/` directory (IWF configuration)
- Modify `CLAUDE.md` or any agent skill/command files
- Write handoffs that target the Planner for "fix IWO" work
- Spawn new pipelines or create new specs
- Override human gate decisions
- Run more than 3 retry attempts for the same failure

### 3.3 CLAUDE.md Rules for Agent 007

```markdown
# Agent 007 — Pipeline Supervisor

You are the pipeline health supervisor. Your job is to diagnose stalls
and retry safe operations. You have STRICT boundaries:

## FORBIDDEN (hard rules, no exceptions)
- NEVER modify files in iwo/ or .claude/ directories
- NEVER write handoffs targeting planner for infrastructure fixes
- NEVER override human gate decisions
- NEVER retry more than 3 times for the same failure
- NEVER spawn new pipelines or specs

## PERMITTED
- Read any file in docs/agent-comms/
- Read IWO logs and state files
- Send /workflow-next to restart a stalled agent
- Write retry handoffs (same agent, same step)
- Write diagnostic reports to docs/agent-comms/.audit/
- Send webhooks to n8n for notifications
- Skip non-critical pipeline steps (docs only)

## ESCALATION
If you cannot resolve the issue within 3 retries, or if the issue
requires code changes, STOP and send a CRITICAL notification via
n8n webhook with your full diagnosis. Do not attempt to fix it.
```

### 3.4 Decision Tree

```
Auditor triggers 007 with diagnostic context
  │
  ├── Agent stall (no handoff after activation)?
  │   ├── Check tmux pane — is agent responsive?
  │   │   ├── Pane idle at prompt → Send /workflow-next (retry)
  │   │   ├── Pane showing error → Read error, diagnose
  │   │   │   ├── Transient (network, timeout) → Retry
  │   │   │   ├── Deterministic (code bug) → Escalate to human
  │   │   │   └── Configuration (missing env var) → Escalate to human
  │   │   └── Pane not found → Escalate to human (tmux issue)
  │   └── Max retries reached → Escalate to human
  │
  ├── Pipeline status inconsistency?
  │   ├── Completed but active handoff → Reactivate pipeline
  │   ├── Active but no recent handoffs → Check for stall
  │   └── Halted → Report reason, escalate to human
  │
  ├── Rejection loop?
  │   ├── Count < threshold → Allow to continue
  │   └── Count >= threshold → Halt pipeline, escalate
  │
  └── Unknown anomaly → Full diagnostic report, escalate to human
```

### 3.5 Deliverables

- [ ] Agent 007 CLAUDE.md with safety rails
- [ ] Agent 007 skill file for diagnosis workflow
- [ ] IWO integration: tmux pane 7 setup in launch script
- [ ] Auditor → 007 trigger mechanism in daemon.py
- [ ] Retry handoff schema (distinct from normal handoffs)
- [ ] 007 diagnostic report schema
- [ ] Integration tests: simulate stall → 007 retries → pipeline resumes
- [ ] Integration tests: simulate code bug → 007 escalates correctly
- [ ] Documentation update

---

## Phase 4: SmythOS Dashboard Integration (Future)

**Goal:** Visual monitoring dashboard and potential Layer 3 self-healing.
**Risk:** Low for dashboard, high for self-healing.
**Estimated effort:** TBD — depends on SmythOS evaluation.
**Prerequisite:** Phases 1-3 stable.

### 4.1 Dashboard Features (Low Risk)

- Real-time pipeline visualization (agent states, handoff chain, queue depths)
- Historical metrics (sprint durations, failure rates, 007 intervention frequency)
- Audit event log with filtering and search
- Spec progress timeline

### 4.2 SmythOS Integration Points

- IWO auditor → SmythOS webhook (same events as n8n, additional endpoint)
- SmythOS reads `.active-specs.json` for pipeline state
- SmythOS reads `docs/agent-comms/.audit/` for historical diagnostics
- SmythOS → n8n webhook for escalation actions

### 4.3 Layer 3: Self-Healing (High Risk, Deferred)

**Prerequisites before attempting:**
- Comprehensive IWO test suite (unit + integration)
- Git-based rollback mechanism (auto-commit before changes)
- Isolated staging environment for testing fixes
- SmythOS running externally so it can undo its own damage

**Approach if/when attempted:**
- SmythOS diagnoses infrastructure bug via Claude Opus 4.6
- Proposes fix as a git branch (never direct to main)
- Runs IWO test suite against the branch
- If tests pass: notifies human for approval
- If tests fail: discards branch, escalates with full context

### 4.4 Deliverables

- [ ] SmythOS evaluation complete (from research prompt written earlier today)
- [ ] Dashboard webhook integration
- [ ] Pipeline state visualization
- [ ] Decision on Layer 3 scope and timeline

---

## Implementation Order

| Phase | Description | Sessions | Prerequisites | Risk | Status |
|-------|-------------|----------|---------------|------|--------|
| 1 | Auditor module | 1-2 | None | Zero | ✅ ~95% (E2E test remaining) |
| 2 | n8n → ntfy notifications | 1 | Phase 1 | Low | ✅ Complete |
| 3 | Agent 007 (AI supervisor) | 2-3 | Phases 1+2 | Medium | Not started |
| 4 | SmythOS dashboard | TBD | Phases 1-3 stable | Low-High | Future |

## Design Principles

1. **Each layer works independently.** The auditor provides value without 007. Notifications work without SmythOS. Failure of a higher layer doesn't break lower layers.
2. **Deterministic before intelligent.** Python checks catch 80% of issues. AI handles the remaining 20%. Never use AI where a conditional will do.
3. **Bounded autonomy.** 007 can retry but not redesign. SmythOS can propose but not merge. Humans approve infrastructure changes.
4. **Observable everything.** Every check, every decision, every retry is logged and notified. No silent failures.
5. **Fail safe, not fail fast.** If 007 can't fix it, it stops trying and asks for help. Three retries max. Never make things worse.

---

## Related Documents

- `docs/TECHNICAL-DOCUMENTATION.md` — IWO technical reference (v2.7.1)
- `docs/ARCHITECTURE.md` — IWO architecture overview
- `/home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt/docs/prompts/smythos-vs-iwo-research-prompt.md` — SmythOS evaluation prompt
- `docs/BORIS-WORKFLOW-MANUAL.md` — IWF agent reference (Ivan's Workflow)
