# IWO Ops Agent — Multi-Model Peer Review Report

**Date:** 2026-03-09
**Reviewed by:** Gemini 2.5 Pro (neutral architect), GPT-5.2 (skeptical SRE), Gemini 3 Pro Preview (pragmatic defender)
**Orchestrated by:** Claude Opus via PAL MCP consensus workflow
**Commit under review:** e764d9f (3 bug fixes) + all Phase 1–4 implementation
**Input document:** `docs/PAL-PEER-REVIEW-PROMPT.md`

---

## Executive Summary

Three models independently reviewed the Ops Agent subsystem architecture, the three bug fixes in commit e764d9f, and the overall design decisions. The consensus architecture score is **6/10** (range: 4–7 across models). The design is sound for a solo-developer daemon — the tiered safety model, three-source trigger architecture, and prompt-as-stdin pattern are well-conceived. The problems are all in implementation details and are fixable without rearchitecting.

**Four issues reached unanimous agreement (3/3 models):**
1. Bug 3's error propagation is broken — dispatch failures are silently archived as successes
2. Register atomicity is a real race condition — Agent 007 writes while daemon reads
3. The "other" category in auto-approve is a security footgun — default-allow for uncategorized actions
4. Directive loss when Agent 007 is busy — silent archival of unexecuted work

---

## 1. Architecture Assessment

| Model | Score | Key Rationale |
|-------|-------|---------------|
| Gemini 2.5 Pro | 4/10 | File-based polling + single agent + unlocked state = architecturally unsound for critical automation |
| GPT-5.2 | 6/10 | Clear pipeline + human gating + pragmatic tmux automation; weak queue semantics + concurrency controls |
| Gemini 3 Pro | ~7/10 | Unconventional but brilliant for solo dev; tmux + headless CLI bypasses unnecessary Celery/Airflow complexity |

**Consensus: 6/10.** The divergence reflects different evaluation contexts. Gemini 2.5 Pro evaluated against distributed systems first principles. GPT-5.2 balanced operational risks against pragmatic constraints. Gemini 3 Pro correctly emphasized that this is a solo-developer workflow tool, not an enterprise job runner.

---

## 2. Bug Fix Quality

### Bug 1: Lazy Agent-007 Re-Discovery — 7/10

All models agreed this is an acceptable tactical patch. The single-retry re-discovery correctly solves the immediate problem of Agent 007 not being available at daemon startup.

**Remaining risk:** If tmux is restarted and window 6 is recreated after the single re-discovery attempt, the commander never re-discovers and dispatch keeps failing. GPT-5.2 flagged this as a "3am failure mode."

**Recommendation:** Change from "discover once on first miss" to "always attempt re-discovery if agent not found in `_agents`." Still single-attempt per launch call, but handles window recreation.

### Bug 2: File-Based Logging — 8/10

All models agreed this was a fundamental and well-executed improvement. Non-negotiable for any daemon.

**Remaining risks:**
- No log rotation — will fill disk during long sessions (GPT-5.2, Gemini 2.5)
- No crash-flush guarantee (GPT-5.2)
- No run IDs or prompt paths in log entries for correlation (GPT-5.2)

**Recommendation:** Replace `FileHandler` with `RotatingFileHandler(maxBytes=10*1024*1024, backupCount=5)` — 10MB per file, 5 rotations, 50MB total cap. Sufficient for 8–12 hour daemon sessions.

### Bug 3: Archive Failure Tracking — 5/10 (BROKEN)

All three models independently identified this fix as incomplete. The root cause:

1. `_dispatch_ops_agent()` returns `False` on failure but does NOT raise an exception
2. `_handle_resolve_ops()` calls `_dispatch_ops_agent()` but does not check the return value
3. `poll()` only sets `failed = True` when an exception is caught
4. Therefore: dispatch failures are silently archived as successes, and the `FAILED-` prefix is never applied

Gemini 3 Pro described it as "currently broken." GPT-5.2 called it a "reliability bug."

**Required fix:** Either (a) have `_handle_resolve_ops` raise an `AgentDispatchError` when `_dispatch_ops_agent` returns `False`, or (b) explicitly check `if not success:` and set the failed flag before returning.

---

## 3. Failure Mode Analysis (Top 3, Unanimous)

### FM-1: Silent Directive Loss (Highest Risk)

When Agent 007 is busy and a `resolve-ops` directive arrives, the directive is archived as "processed" even though no work happened. The proactive trigger may re-fire in 60 seconds, but that directive also gets archived. Work orders are silently dropped.

GPT-5.2 framed this as an **at-most-once vs at-least-once** semantics problem. The current design provides at-most-once delivery with silent drops. Ops work requires at-least-once.

**Models diverged on the fix complexity:**
- Gemini 2.5 Pro: Full spool directory pattern (`incoming/`, `processing/`, `done/`, `failed/`)
- GPT-5.2: Durable state machine with retry and backoff
- Gemini 3 Pro: **Just skip the archive on failure** — leave the directive in `.directives/` and let the 2s poll re-pick it up naturally. Zero new infrastructure.

**Recommended fix:** Gemini 3 Pro's approach. Check `_dispatch_ops_agent()` return value. If `False`, do not archive. The existing 2s poll becomes the retry mechanism. Add a max-retry counter or TTL to prevent infinite retries on permanently broken directives.

### FM-2: Register Corruption via Race Condition

`.ops-actions.json` is the single source of truth for ops state. Agent 007 modifies it via a Python heredoc (full rewrite). The daemon reads it every 60 seconds for proactive checks. No file locking exists.

With Agent 007 running for minutes and the daemon reading every 60s, overlap is near-certain. The daemon will eventually read a partially-written JSON file, get a parse error, and either crash or make incorrect proactive trigger decisions.

**Models diverged on the fix:**
- Gemini 2.5 Pro: `filelock` library with lock files
- GPT-5.2: `fcntl` advisory locks + optimistic concurrency versioning (`register_version` field)
- Gemini 3 Pro: No locking needed. Write to `.ops-actions.json.tmp`, then `os.replace()` (POSIX atomic rename).

**Recommended fix:** Gemini 3 Pro's approach. The atomic rename pattern is correct for a single-writer scenario. Agent 007 is the only writer; the daemon only reads. `os.replace()` guarantees the daemon never sees a partial write. Also wrap daemon's register reads in `try/except json.JSONDecodeError` with retry-after-1s fallback.

### FM-3: Stuck/Unknown Agent State

GPT-5.2 uniquely flagged this: if Agent 007 or the `claude -p` process hangs, proactive triggers keep firing and getting archived while the system appears active but makes no progress.

The `agent_007_timeout_seconds: 600` exists in config but enforcement is unclear. Without a heartbeat or lease mechanism, a hung agent is indistinguishable from a slow-running one.

**Recommended fix (P1):** Log agent start time and last observed output. If no handoff received within `agent_007_timeout_seconds`, mark agent as timed-out, release the busy lock, and allow the next directive to dispatch.

---

## 4. Security & Safety Concerns

### "Other" Category Auto-Approve (CRITICAL — Unanimous)

All three models flagged `"other"` in `ops_auto_approve_categories` as a security footgun. If the LLM hallucinates a destructive action and categorizes it as "other" (because it doesn't match `migration` or `config`), it bypasses human gating and executes immediately.

This is a **default-allow policy for uncategorized actions** — the opposite of defense-in-depth.

**Required fix:** Move `"other"` from `ops_auto_approve_categories` to `ops_human_gate_categories`. One-line change in `config.py`. Unrecognized actions must fail safe.

### Secrets in Prompts/Logs (GPT-5.2)

Ops prompts embed the full register state, which may contain sensitive values (wrangler secrets, API keys as action descriptions). These prompts are written to `logs/prompts/` as markdown files. If the register includes secret material, it persists in plaintext on disk.

**Recommended fix (P2):** Add a redaction pass to `_build_ops_agent_prompt()` that strips or masks values matching common secret patterns before writing prompt files.

### Model Reproducibility (All Models)

Agent 007 is not in `AGENT_MODEL_MAP`. The `launch_agent_007()` method builds the `claude -p` command without `--model`, defaulting to whatever `ANTHROPIC_MODEL` is set in the environment. This means:
- Different hosts/users get different models
- CLI default model upgrades change behavior silently

**Required fix (P1):** Explicitly pass `--model opus` in `launch_agent_007()` or add `agent-007` to `AGENT_MODEL_MAP`.

---

## 5. Concurrency & Race Conditions

The primary concurrency flaw is the unprotected read-modify-write cycle on `.ops-actions.json` (see FM-2 above).

A secondary race exists in the trigger system: proactive and reactive triggers can fire simultaneously. The current "is Agent 007 busy?" check causes one trigger's directive to be silently dropped (archived without execution). With the skip-archive fix (FM-1), this becomes self-correcting — the un-dispatched directive stays in `.directives/` for the next poll.

The directive polling (2s interval) vs handoff watching (inotify) asymmetry is acceptable — directives are low-frequency events where 2s latency is irrelevant.

---

## 6. Consolidated Recommendations

### P0 — Fix Before Next Ops Agent Dispatch

| # | Fix | Files | Effort |
|---|-----|-------|--------|
| P0-1 | **Complete Bug 3 error propagation.** In `_handle_resolve_ops`, check return value of `_dispatch_ops_agent()`. If `False`, raise `AgentDispatchError` or set failed flag. The `FAILED-` archive prefix must apply to dispatch failures. | `directives.py` | ~10 lines |
| P0-2 | **Skip archive on dispatch failure.** In `poll()`, if dispatch returns `False` (agent busy or discovery failed), do NOT move directive to `.processed/`. Leave it in `.directives/` for the 2s poll to retry. Add max-retry counter (e.g., 5 attempts) or TTL to prevent infinite loops. | `directives.py` | ~15 lines |
| P0-3 | **Atomic register writes.** Update the ops-action-resolver skill's Python heredoc to write to `.ops-actions.json.tmp` then `os.replace('.ops-actions.json.tmp', '.ops-actions.json')`. Wrap daemon register reads in `try/except json.JSONDecodeError` with retry-after-1s. | `SKILL.md` + `daemon.py` | ~10 lines |
| P0-4 | **Move "other" to human-gate.** One-line config change. Unrecognized action categories must fail safe. | `config.py` | 1 line |

### P1 — Next Sprint

| # | Fix | Files | Effort |
|---|-----|-------|--------|
| P1-1 | **Unify budget config.** Delete `ops_agent_budget_usd` (already dead per config audit). Use `agent_007_budget_usd` everywhere. | `config.py`, `directives.py` | ~5 lines |
| P1-2 | **Pin Agent 007 model.** Pass `--model opus` explicitly in `launch_agent_007()` or add `agent-007` to `AGENT_MODEL_MAP`. | `headless_commander.py` or `config.py` | ~3 lines |
| P1-3 | **RotatingFileHandler.** Replace `FileHandler` with `RotatingFileHandler(maxBytes=10*1024*1024, backupCount=5)`. | `tui.py` | ~3 lines |
| P1-4 | **Always re-discover Agent 007.** Change lazy re-discovery from "once on first miss" to "every launch attempt if not found." | `headless_commander.py` | ~5 lines |
| P1-5 | **Agent timeout enforcement.** Track agent start time; if no handoff within `agent_007_timeout_seconds`, mark as timed-out and release busy lock. | `daemon.py` | ~20 lines |

### P2 — Backlog

| # | Fix | Notes |
|---|-----|-------|
| P2-1 | **Ops run ledger.** Append-only `logs/ops-runs.jsonl` with run_id, directive_id, action fingerprints, outcome, timestamps. | Cheap to implement, invaluable for post-mortems |
| P2-2 | **Rule-based gating.** Replace category-only auto/gate partition with risk scoring considering blast radius and reversibility. | Longer-term safety improvement |
| P2-3 | **Secret redaction in prompts/logs.** Add redaction pass to `_build_ops_agent_prompt()` for common secret patterns. | Security hardening |
| P2-4 | **SQLite directive queue.** Replace filesystem IPC with embedded SQLite for directives + state transitions. | Only needed if directive volume or reliability requirements increase |

---

## 7. Disagreement Analysis

The most instructive divergence was on **fix complexity**. The three models represent a spectrum from "build it right" to "fix what's broken":

**Gemini 2.5 Pro (Architect)** recommended enterprise-grade solutions: spool directory patterns, filelock library, default-deny safety policies, and structured logging. These are architecturally correct but introduce significant implementation and maintenance overhead for a solo developer.

**GPT-5.2 (SRE)** recommended operationally robust solutions: durable state machines, optimistic concurrency versioning, Prometheus metrics, health endpoints, and agent heartbeat/lease patterns. These are the right answers for a production SRE team but over-invest in infrastructure for the current use case.

**Gemini 3 Pro (Pragmatist)** recommended minimal fixes using existing mechanisms: skip-archive retry via the natural 2s poll, atomic rename without locking, YAGNI on observability. These are correct for the current operating context and can be implemented in hours rather than days.

**Synthesis:** Start with Gemini 3 Pro's minimal fixes. They solve the same problems with dramatically less code. Bank the enterprise-grade solutions from the other models as the evolution path if the system grows beyond single-machine, single-operator use.

---

## 8. Answers to Specific Review Questions

**Q1 (Directive loss):** Not acceptable. Fix with skip-archive pattern (P0-2).

**Q2 (Register atomicity):** Realistic collision risk. Fix with atomic rename — no locking needed for single-writer architecture (P0-3).

**Q3 (Category completeness):** Categories adequate for current eBatt ops. Partition is wrong — move "other" to human-gate (P0-4). GPT-5.2 noted missing categories for future consideration: `iam/permissions`, `tls/cert`, `firewall/waf`, `billing/quota`. Not needed now.

**Q4 (Budget duplication):** Remove `ops_agent_budget_usd` — already identified as dead code in the config audit (P1-1).

**Q5 (Model selection):** Not acceptable to rely on env var. Pin explicitly (P1-2).

**Q6 (Observability):** `RotatingFileHandler` now (P1-3), ops run ledger later (P2-1). Skip Prometheus, structured logging, and health endpoints — YAGNI for solo dev.

---

## 9. Previously Known Issues (Config Audit Cross-Reference)

The implementation doc's Config Audit (Session 3) already identified and fixed:
- Duplicate `agent_007_project_root` — removed
- Dead `ops_agent_budget_usd` and `ops_agent_timeout_seconds` — removed
- Test script wrong path — fixed

The peer review independently re-discovered the budget duplication issue, confirming the config audit's findings. The "other" category safety concern and error propagation gap were **new findings** not previously identified.

---

## 10. Relationship to Known Skill Defect

The implementation doc records that the live integration test (Session 4) exposed a skill defect: Agent 007 wrote a valid handoff but did not update the register. Root cause was SKILL.md Step 5 being too vague for Claude Code's non-persistent tool-call model.

This is orthogonal to the peer review findings. The skill was already rewritten to use a single-execution Python heredoc. The peer review's P0-3 (atomic register writes) addresses the *next layer* — ensuring the heredoc pattern writes atomically even when it works correctly.

---

*Generated from PAL MCP multi-model consensus workflow (Gemini 2.5 Pro + GPT-5.2 + Gemini 3 Pro Preview). Confidence: high (8–9/10 across all models).*
