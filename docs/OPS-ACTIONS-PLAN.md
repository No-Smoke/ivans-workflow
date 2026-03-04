# IWO Ops Actions System — Implementation Plan

**Author:** Claude + Vanya  
**Date:** 2026-02-26  
**Status:** APPROVED — Ready for implementation  
**Estimated effort:** 3–4 sessions across 5 phases

---

## Problem Statement

IWO pipelines generate manual operational actions (migrations, secrets, DNS changes, browser verifications) embedded in handoff JSON. These are currently invisible unless you read raw handoff files. There is no tracking, no notification escalation, no completion workflow, and no historical record. With autonomous overnight runs (`a` + `D` enabled), critical infrastructure steps can be silently skipped.

A backlog scan of 17 specs found **25 unresolved action items** (9 critical, 5 warning, 11 informational) — some dating back weeks.

---

## Design: Ops Actions Register

Professional CI/CD systems solve this with **deployment runbooks** — structured, persistent, append-only registers of human-required actions with lifecycle tracking and notification escalation. Our implementation adapts this pattern to IWO's filesystem-based architecture.

### Core Concepts

- **Ops Action**: A discrete manual task required by the pipeline. Has a lifecycle: `pending` → `in_progress` → `completed` | `skipped`.
- **Priority**: `critical` (blocks deploy or breaks functionality), `warning` (post-deploy integration task), `info` (browser verification, visual check).
- **Auto-extraction**: The daemon parses handoff JSON on every pipeline transition, extracting new actions from `unresolvedIssues`, `deploymentInstructions`, and `nextAgent.action`.
- **Deduplication**: Actions are fingerprinted by (spec_id, normalized_text_hash) to prevent the echo problem visible in MONETIZATION-MVP's RESEND_API_KEY note.
- **Stale detection**: When a handoff's `unresolvedIssues` no longer contains a previously-extracted action, the action is flagged as `possibly_resolved` for human confirmation.

### Data Model

```json
{
  "id": "ops-20260225-001",
  "spec_id": "EBATT-015",
  "title": "Self-promote platform owner to super_admin",
  "description": "Run: UPDATE users SET role = 'super_admin' WHERE email = 'ivan@ethospower.org'",
  "category": "migration",
  "priority": "critical",
  "status": "pending",
  "verification_cmd": "npx wrangler d1 execute ebatt-users --remote --command \"SELECT role FROM users WHERE email='ivan@ethospower.org'\"",
  "created_at": "2026-02-25T03:55:00Z",
  "source_agent": "deployer",
  "source_sequence": 5,
  "fingerprint": "sha256:...",
  "resolved_at": null,
  "resolved_by": null,
  "notes": null,
  "auto_extracted": true,
  "stale_since": null
}
```

**Categories:** `migration`, `secret`, `dns`, `webhook`, `verification`, `config`, `email_infra`, `other`

### Storage

`docs/agent-comms/.ops-actions.json` — co-located with handoff data, version-controlled, no new dependencies. The daemon owns writes; TUI and Kanban read.

---

## Phase 1: Backlog Seeding + Data Model (Session 1, ~45 min)

**Goal:** Create the ops_actions.py module and seed it with the 25 existing backlog items.

**Deliverables:**
1. `iwo/ops_actions.py` — OpsAction dataclass (Pydantic model), OpsActionsRegister class with:
   - `load()` / `save()` — JSON file I/O
   - `add(action)` — with fingerprint dedup
   - `resolve(id, resolved_by, notes)` — status → completed
   - `skip(id, reason)` — status → skipped
   - `get_pending(priority_filter)` — filtered queries
   - `get_summary()` — counts by priority × status
   - `mark_stale(id)` — flag possibly_resolved
2. `scripts/seed-ops-backlog.py` — one-time script that runs the handoff scanner (from the extraction above) through dedup/classification and writes the initial `.ops-actions.json`
3. Unit tests for OpsActionsRegister CRUD and dedup logic

**Implementation notes:**
- Fingerprint = sha256 of `f"{spec_id}:{normalized_text}"` where normalized_text strips whitespace, lowercases, removes sequence-specific references
- Priority classification: keyword-based (migration/secret/DNS → critical; webhook/config → warning; verify/browser/visual → info) with manual override field
- Category classification: same keyword extraction, mapping to the 8 categories

---

## Phase 2: Auto-Extraction in Daemon (Session 1–2, ~30 min)

**Goal:** The daemon automatically creates Ops Actions when processing handoffs.

**Deliverables:**
1. `daemon.py` modification: after `process_handoff()` succeeds, call `_extract_ops_actions(handoff_data)` which:
   - Scans `status.unresolvedIssues` for manual-action patterns
   - Scans `deploymentInstructions.{preDeploySteps,postDeploySteps,manualSteps}`
   - Scans `nextAgent.action` when target is "human"
   - Checks infra flags (noNewMigrations=false → migration action)
   - Deduplicates against existing register
   - Adds new actions with auto_extracted=true
2. Stale detection: when processing a new handoff for a spec, compare its unresolvedIssues against existing pending actions for that spec. If an action's source text no longer appears, set `stale_since` timestamp.
3. Integration test: process a mock handoff, verify ops actions created

**Pattern matching (refined from backlog scan):**
```python
CRITICAL_PATTERNS = [
    r'must\s+(run|create|configure|set|apply|execute)',
    r'wrangler\s+(secret|d1\s+migrations)',
    r'not\s+yet\s+(set|configured|created|applied)',
    r'human\s+(must|task|action)',
    r'\bDNS\b.*\b(CNAME|A\s+record|MX)\b',
    r'secrets?\s+not\s+(set|configured)',
]
VERIFICATION_PATTERNS = [
    r'(human|user)\s+must\s+verify',
    r'browser[\s-]verif(y|ication)',
    r'visual(ly)?\s+verif',
    r'end-to-end.*requires?\s+(auth|browser|session)',
]
```

---

## Phase 3: Notification Integration (Session 2, ~45 min)

**Goal:** Critical and warning ops actions trigger phone notifications (ntfy) and desktop notifications with the specific action details.

**Deliverables:**
1. `iwo/notifications.py` enhancement — new method `notify_ops_action(action: OpsAction)`:
   - `critical` → ntfy priority=5 (urgent/red), title="⛔ OPS ACTION REQUIRED", body includes spec_id + title + description
   - `warning` → ntfy priority=4 (high/orange), title="⚠ OPS ACTION", same body
   - `info` → no push notification, log only
   - Desktop `notify-send` with urgency=critical for critical actions
2. Notification on creation: when `_extract_ops_actions()` adds a new critical/warning action, `notify_ops_action()` fires immediately
3. Daily digest: optional scheduled summary of all pending actions (configurable in config.py, default=disabled)
4. Deploy gate enhancement: when `auto_deploy_all=False` and the gate fires, the notification now includes the LIST of pending critical ops actions for that spec, not just "press d"

**ntfy integration:**
- Existing: IWO already has `_notify()` using ntfy.sh topic
- Enhancement: use ntfy tags for visual indicators: `warning` tag → orange, `rotating_light` tag → red
- Action buttons in ntfy: "View Details" deep-link to kanban /ops page (when implemented)

**Config additions:**
```python
ops_actions_notify_critical: bool = True
ops_actions_notify_warning: bool = True  
ops_actions_daily_digest: bool = False
ops_actions_daily_digest_hour: int = 8  # NZ morning
```

---

## Phase 4: Dashboard + TUI Integration (Session 2–3, ~60 min)

**Goal:** Ops Actions visible in Kanban dashboard and manageable from TUI.

### 4a: Kanban Dashboard

1. **Banner**: Top of kanban page, persistent banner:
   - Red background: "{N} critical ops actions pending" (if any critical)
   - Orange background: "{N} warning ops actions pending" (if no critical but warnings exist)  
   - Green: "All ops actions resolved" (if none pending)
2. **Ops Actions page** (`/ops` route): Full register view:
   - Grouped: Pending (critical first, then warning, then info) → Completed → Skipped
   - Each entry shows: spec_id, title, description, verification_cmd (copyable), created_at, age
   - Stale entries highlighted with "possibly resolved — verify and close" badge
3. **Button on main kanban**: "Ops Actions ({count})" linking to /ops page

### 4b: TUI Integration

1. **'o' key**: Opens Ops Actions panel showing summary counts and pending items
2. **Mark complete**: Navigate to action, press Enter, confirm with 'y', optionally add notes
3. **Mark skipped**: Press 's' on an action, provide reason
4. **Status line**: Bottom of TUI shows "OPS: {critical}⛔ {warning}⚠ {info}ℹ" when pending items exist

### 4c: CLI Interface

1. `iwo ops list [--status pending|completed|skipped] [--priority critical|warning|info] [--spec SPEC-ID]`
2. `iwo ops complete <id> [--notes "reason"]`
3. `iwo ops skip <id> --reason "reason"`
4. `iwo ops report` — formatted summary (same as the backlog report above)
5. `iwo ops seed` — re-run backlog scanner (idempotent via fingerprint dedup)

---

## Phase 5: Smart Deploy Gate (Session 3–4, ~30 min)

**Goal:** Replace binary auto_deploy_all with tiered deployment intelligence.

**New config:**
```python
deploy_gate_mode: Literal["manual", "safe_only", "smart", "all"] = "safe_only"
```

**Modes:**
- `manual`: All deploys gated (current auto_approve_safe_deploys=False behavior)
- `safe_only`: Auto-approve safe deploys, gate unsafe (current default)
- `smart`: Auto-approve safe deploys. For unsafe deploys, check ops actions register:
  - If no critical ops actions pending for this spec → auto-approve with warning notification
  - If critical ops actions pending → gate + notify with the specific actions list
- `all`: Bypass all gates (current auto_deploy_all=True, retained for emergency/trusted-spec use)

**`smart` mode rationale:** An unsafe deploy with "new D1 migration" is only dangerous if the migration hasn't been run. If the ops action "run migration 0003" has been marked complete, the deploy is safe to proceed. This gives overnight autonomy while preserving safety for genuinely unresolved infrastructure changes.

**Implementation:**
1. daemon.py `_check_deploy_gate()` refactored to consult ops_actions register
2. Notification includes pending actions list with descriptions
3. TUI 'D' key cycles through modes: manual → safe_only → smart → all → manual

---

## File Changes Summary

| File | Phase | Changes |
|------|-------|---------|
| `iwo/ops_actions.py` | 1 | New module — OpsAction model, OpsActionsRegister class |
| `scripts/seed-ops-backlog.py` | 1 | New — one-time backlog scanner |
| `iwo/daemon.py` | 2, 5 | `_extract_ops_actions()`, deploy gate refactor |
| `iwo/notifications.py` | 3 | `notify_ops_action()`, priority-based ntfy |
| `iwo/config.py` | 3, 5 | New config fields |
| `iwo/tui.py` | 4b, 5 | 'o' key panel, status line, 'D' mode cycling |
| `iwo/cli.py` | 4c | New `ops` subcommand group |
| `tools/kanban-dashboard.py` | 4a | Banner, /ops page, button |
| `tests/test_ops_actions.py` | 1, 2 | Unit + integration tests |
| `docs/ARCHITECTURE.md` | All | Updated architecture docs |
| `docs/IWO-TUI-Manual.md` | 4 | New keybindings documented |
| `docs/CHANGELOG-FIXES.md` | All | Feature entries |

---

## Handoff Instructions for Fresh Chat

**Context to provide:**

> I'm implementing the IWO Ops Actions System per the plan at:
> `/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/docs/OPS-ACTIONS-PLAN.md`
>
> Read that plan first. The existing codebase is at:
> `/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/`
>
> Key files to understand before starting:
> - `iwo/daemon.py` — pipeline orchestration, `process_handoff()`, `_notify()`, deploy gate
> - `iwo/config.py` — Pydantic settings model
> - `iwo/tui.py` — Textual TUI with keybindings
> - `tools/kanban-dashboard.py` — standalone Flask/HTML dashboard
> - `docs/agent-comms/.ops-actions.json` — will be created by Phase 1
>
> Start with Phase 1: create `iwo/ops_actions.py` and `scripts/seed-ops-backlog.py`. 
> The backlog of 25 items is documented in the plan. The handoff scanner 
> pattern-matching regex is in Phase 2. Seed the register, then proceed to Phase 2.
>
> ntfy topic for notifications: check `iwo/config.py` for existing ntfy_topic field.
> Kanban runs on localhost:8787.

**Per-session scoping:**
- Session 1: Phases 1 + 2 (data model, seeding, auto-extraction)
- Session 2: Phases 3 + 4a (notifications, kanban dashboard)
- Session 3: Phases 4b + 4c + 5 (TUI, CLI, smart deploy gate)

---

## Success Criteria

1. `iwo ops report` shows all 25 backlog items with correct priority classification
2. Processing a new handoff with unresolvedIssues auto-creates ops actions (no duplicates)
3. Marking an action complete removes it from pending counts
4. Critical action creation triggers ntfy push with red/urgent priority
5. Kanban shows red/orange banner with pending count
6. `/ops` page renders full register with grouping and status
7. `smart` deploy mode gates only when relevant critical ops actions are unresolved
8. Stale detection flags actions whose source text disappears from newer handoffs
