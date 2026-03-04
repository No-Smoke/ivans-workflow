# IWO-n8n Architecture Decision

**Date:** 2026-02-22
**Decision:** Do NOT migrate IWO to n8n. Use n8n as HITL notification layer alongside IWO.
**Qdrant ID:** b25145b2-9cfb-4c76-a71a-eab67f689fad

---

## Context

After several days of debugging IWO handoff flows between the six Ivan's Workflow agents, the question arose whether IWO (and its planned Agent 007 supervisor) should be replaced by n8n, which already runs on the VPS at n8n.ethospower.org and has an AI agent node, SSH execution, and human-in-the-loop capabilities.

## Decision

**Keep IWO as the tmux orchestrator. Add n8n as a webhook-based notification and approval sidecar.**

### Rationale

**Architecture mismatch is fundamental.** n8n's AI agent nodes are LangChain-based internal agents — they run LLMs inside n8n's runtime. IWO orchestrates external CLI processes: Claude Code instances in tmux panes with persistent terminal state, filesystem access, and long-running sessions. n8n has no concept of tmux session monitoring, terminal state detection, canary probes, or persistent state machines. Its SSH node fires single commands but cannot maintain interactive terminals or detect when a Claude Code agent finishes by monitoring stdout patterns.

This is the same conclusion reached during the SmythOS evaluation: IWO and n8n/SmythOS solve fundamentally different problems.

**Boris Cherny's own practice confirms the model.** Even Cherny, who created Claude Code and shipped 300+ PRs in December 2025, does NOT run fully autonomous agent loops for important work. He uses slash commands, specifications, and verification strategies with human approval. Fully autonomous loops "require near-perfect specifications and are best suited for lower-stakes projects."

### Where n8n adds genuine value

n8n excels at the exact capability IWO currently lacks: rich mobile notifications with interactive approval. The planned integration:

1. IWO fires webhook to n8n on key events (handoff complete, human input needed, error, pipeline complete)
2. n8n sends rich Telegram notification with handoff summary, outcome, and approve/reject buttons
3. n8n Wait node pauses until human taps approve on phone
4. n8n fires webhook back to IWO API endpoint, IWO activates next agent

This gives Vanya the "walk away, get notified, tap approve, walk away again" workflow without replacing IWO's tmux orchestration.

## IWO Remaining Bugs (Stabilisation)

Four bugs remain before IWO can process specs end-to-end:

1. **Schema mismatch** — Generic handoff tools (`/agent-handoff`, `workflow-handoff` skill) produce Schema B (missing `status.outcome`). IWO's Pydantic parser silently rejects these. Fix: update generic tools to produce Schema A, or make parser accept both.

2. **Recovery swallows handoffs** — `_recover_state()` marks handoff files as "processed" even when they were never routed to the target agent. Fix: during recovery, if the latest handoff targets an agent that hasn't produced a subsequent handoff, treat it as "pending" not "processed."

3. **Agent IWO-awareness** — Agent skills and slash commands still say "Switch to Reviewer window (Ctrl+b 2) and run /workflow-next." Agents don't know that writing a handoff file triggers automatic IWO routing. Fix: update all 6 agent skill files to reference IWO and explain that handoff files trigger automatic routing.

4. **prompt-handoff.sh** — References nonexistent `/workflow-handoff` command. Fix: point to `/agent-handoff` (after fixing its schema) or remove in favour of role-specific skills.

## Deferred Work

- **Agent 007 Layer 2/3** (AI diagnostician, SmythOS integration) — defer until IWO has processed 10+ specs end-to-end without intervention.
- **Full autonomy** — not a goal for production platform work. HITL with fast notification is the correct model.

## Action Plan

See `IWO-stabilisation-todo.md` in this directory.
