# IWO Implementation Plan — Phases 2.2–3.0

**Created:** 2026-02-19 | **Status:** Active
**Context:** Following completion of Phase 2.1 (memory integration). All items below are prioritized improvements discovered during the Phase 1–2.1 build sessions.

---

## Phase 2.2: Agent Intelligence (High Priority)

These items directly improve workflow quality by making agents smarter.

### 2.2.1 — Update workflow-next to query tos-bridge

**Effort:** Small (30 min) | **Impact:** High
**Files:** `ebatt/.claude/commands/workflow-next.md`

Add a step before agents begin work: query tos-bridge for patterns relevant to their role and the current spec. Example: Reviewer queries "common review findings for D1 schemas" before starting review.

```markdown
## Step 0: Load Context
Before beginning work, query the pattern library:
- Run: tos-bridge:search_with_graph collection="ebatt_pattern_library" query="[your role] patterns for [spec topic]"
- Review returned patterns and apply relevant ones to your work
```

**Acceptance criteria:** Agent logs show pattern query before starting work. Reviewer cites relevant past patterns in review findings.

### 2.2.2 — Enrich handoff parser with deliverables

**Effort:** Small (30 min) | **Impact:** Medium
**Files:** `iwo/parser.py`, `iwo/memory.py`

The Pydantic `Handoff` model currently only models `metadata`, `status`, and `nextAgent`. Real handoffs also include `deliverables` (files created/modified, test results) and `evidence`. Adding these as optional fields lets the memory module capture richer data.

```python
class Deliverables(BaseModel):
    filesCreated: list[str] = []
    filesModified: list[str] = []
    testsStatus: Optional[dict] = None
    typecheckPassed: Optional[bool] = None

class Handoff(BaseModel):
    # ... existing fields ...
    deliverables: Optional[Deliverables] = None
    evidence: Optional[dict] = None
```

**Acceptance criteria:** Memory summaries include test counts and file lists. Neo4j HandoffEvent nodes have deliverables metadata.

### 2.2.3 — Pattern library dimension migration

**Effort:** Medium (2 hr) | **Impact:** Medium
**Files:** New migration script

The `ebatt_pattern_library` collection uses 384-dim embeddings (legacy). tos-bridge uses 1024-dim (mxbai-embed-large). This means tos-bridge can write to Neo4j Pattern nodes but cannot properly search the Qdrant collection. Options:

- **Option A:** Create `ebatt_patterns_v2` (1024-dim), re-embed all 56 patterns via Ollama, update tos-bridge default collection. Keep 384-dim as archive.
- **Option B:** Keep 384-dim, add a 384-dim embedding option to tos-bridge search. More complex.

**Recommended:** Option A. One-time migration script:
```python
# Read all 56 points from ebatt_pattern_library (384-dim)
# Re-embed text via Ollama mxbai-embed-large
# Write to ebatt_patterns_v2 (1024-dim)
# Update tos-bridge default collection
```

**Acceptance criteria:** tos-bridge:search_with_graph returns results from the 1024-dim collection. Old 384-dim collection preserved as backup.

---

## Phase 2.3: Multi-Spec Pipeline (High Priority)

### 2.3.1 — Parallel spec support

**Effort:** Large (4 hr) | **Impact:** High
**Files:** `iwo/daemon.py`, `iwo/config.py`, `iwo/tui.py`

Currently IWO tracks one active spec via `.current-spec`. For parallel operation (e.g., Planner starts EBATT-023 while Reviewer finishes EBATT-022):

- Route handoffs by spec ID, not just "next agent"
- Track per-spec pipeline state (which agent is active for which spec)
- TUI: show multiple active pipelines with per-spec status
- Safety rails: per-spec rejection counts and handoff limits

**Key change:** The `HandoffTracker` already tracks per-spec counts. The activation logic in `process_handoff()` needs to check if the target agent is already working on a different spec and queue accordingly.

**Acceptance criteria:** Two specs can progress through the pipeline simultaneously without interference. TUI shows both pipelines.

---

## Phase 2.4: Operational Robustness (Medium Priority)

### 2.4.1 — Agent crash recovery

**Effort:** Medium (2 hr) | **Impact:** Medium
**Files:** `iwo/commander.py`, `iwo/daemon.py`

When state machine detects CRASHED (pane process exited), IWO currently only notifies. Add:

1. Attempt to respawn a shell in the crashed pane (`tmux respawn-pane`)
2. Re-inject the agent's role initialization command
3. If respawn fails 3 times, mark as permanently crashed and notify human
4. Log crash events to memory for pattern analysis ("which agents crash most?")

**Acceptance criteria:** Agent crash → automatic respawn → agent resumes at idle prompt within 30s. Repeated crashes escalate to human notification.

### 2.4.2 — Post-deploy health check

**Effort:** Medium (2 hr) | **Impact:** Medium
**Files:** `iwo/daemon.py`, `iwo/config.py`

After deploy gate approval and deployer completes:

1. IWO waits for deployer's handoff (success/failure)
2. If success, hit the production URL(s) with a health check
3. Verify HTTP 200 and expected response content
4. If health check fails, notify with rollback instructions

```python
health_check_urls: list[str] = ["https://ebatt.ai/api/health"]
health_check_timeout: int = 10
```

**Acceptance criteria:** Deploy success → automatic health check → green notification or rollback warning.

### 2.4.3 — Memory health indicator in TUI

**Effort:** Small (1 hr) | **Impact:** Low
**Files:** `iwo/tui.py`, `iwo/memory.py`

Add a status line to TUI showing memory system health:
- 🟢 Qdrant + Neo4j + Ollama all connected
- 🟡 Partial (e.g., Ollama down, Neo4j OK)
- 🔴 All memory systems unavailable

Periodic health check every 60s.

**Acceptance criteria:** TUI shows memory health status. Color changes within 60s of service up/down.

---

## Phase 2.5: Metrics & Observability (Medium Priority)

### 2.5.1 — Pipeline metrics dashboard

**Effort:** Medium (3 hr) | **Impact:** Medium
**Files:** New `iwo/metrics.py`, `iwo/tui.py`

Now that memory stores every handoff with timing, add a TUI panel or command showing:

- Average cycle time per agent (Builder: 45min, Reviewer: 12min, etc.)
- Rejection rate per agent pair
- Specs completed per day/week
- Current pipeline bottleneck identification
- Time-to-completion estimates based on historical data

Data source: Neo4j HandoffEvent nodes via Cypher aggregation queries.

**Acceptance criteria:** `iwo metrics` command or TUI panel shows real pipeline performance data.

### 2.5.2 — Webhook/notification integration

**Effort:** Small (1 hr) | **Impact:** Medium
**Files:** `iwo/daemon.py`, `iwo/config.py`

Add optional webhook/n8n notification alongside desktop notify-send:

```python
notification_webhook_url: Optional[str] = None  # e.g., n8n webhook URL
notification_channels: list[str] = ["desktop"]  # "desktop", "webhook", "both"
```

**Acceptance criteria:** IWO events appear in n8n workflow for mobile notifications.

---

## Phase 3.0: Advanced Features (Lower Priority)

### 3.0.1 — Multi-project support

**Effort:** Large (6 hr) | **Impact:** High (future)

Support orchestrating workflows for multiple projects (eBatt, EthosPower) from a single IWO instance. Each project has its own:
- Agent mapping (different tmux sessions or window ranges)
- Handoff directory
- Safety rail thresholds
- Memory collection

### 3.0.2 — Agent performance profiling

**Effort:** Medium (3 hr) | **Impact:** Medium

Use pipeline history to identify:
- Agents that consistently take longest
- Specs that always trigger rejection loops
- File patterns that correlate with review failures
- Optimal agent ordering for different spec types

### 3.0.3 — Credential rotation via Bitwarden

**Effort:** Small (1 hr) | **Impact:** Low

Move hardcoded Qdrant/Neo4j credentials from config.py to environment variables loaded from Bitwarden CLI, matching the Boris workflow launch script pattern.

### 3.0.4 — Self-healing Ollama

**Effort:** Small (1 hr) | **Impact:** Low

If Ollama is unreachable, attempt to start it (`systemctl --user start ollama` or `ollama serve`). Retry memory operations after Ollama recovers.

---

## Implementation Order (Recommended)

```
Week 1:  2.2.1 (workflow-next tos-bridge) → 2.2.2 (parser enrichment)
Week 2:  2.2.3 (pattern migration) → 2.4.3 (memory health TUI)
Week 3:  2.3.1 (multi-spec pipeline)
Week 4:  2.4.1 (crash recovery) → 2.4.2 (health check)
Week 5:  2.5.1 (metrics) → 2.5.2 (webhooks)
Future:  Phase 3.0 items as needed
```

## Dependencies

| Phase | Requires |
|-------|----------|
| 2.2.1 | tos-bridge connected to Claude Code ✅ (done 2026-02-19) |
| 2.2.2 | None |
| 2.2.3 | Ollama running with mxbai-embed-large |
| 2.3.1 | Phase 2.1 complete ✅ |
| 2.4.1 | tmux respawn-pane capability |
| 2.4.2 | Production URLs configured |
| 2.5.1 | Neo4j HandoffEvent data (accumulates over time) |
| 2.5.2 | n8n webhook URL configured |
