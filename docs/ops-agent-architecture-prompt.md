# Architecture Consultation: Adding an Ops Agent to a 6-Agent AI Development Pipeline

## Problem Statement

I run a 6-agent AI development pipeline (Planner → Builder → Reviewer → Tester → Deployer → Docs) orchestrated by a Python daemon called IWO (Ivan's Workflow Orchestrator). Each agent is a Claude Code instance running in its own tmux window pane. The daemon monitors the filesystem for handoff JSON files, validates them with Pydantic, and routes work to the next agent.

The pipeline builds software specs end-to-end: the Planner reads a spec and creates an implementation plan, the Builder writes code, the Reviewer checks it, the Tester tests it, the Deployer deploys it, and the Docs agent updates documentation.

**The gap I just hit:** The pipeline is driven by a `next-spec` directive system that selects the next specification to build. But my strategic roadmap says "Phase 1: stabilise and verify what's deployed" before building new features. Phase 1 work consists entirely of ops tasks: apply D1 database migrations to production, verify deployed features in a browser, fix known issues. When the Planner received the `next-spec` directive, it correctly identified that Phase 1 items are not spec-shaped work, declared itself "blocked", and stopped the pipeline.

**The root cause:** The pipeline can only do spec-driven work. It has no mechanism to execute operational tasks — migrations, verifications, infrastructure commands, browser checks — that fall between "spec-driven feature development" and "manual human tasks."

## What Already Exists

### Ops Actions Register
I have a structured JSON register (`docs/agent-comms/.ops-actions.json`) that accumulates ops tasks. Every time the Deployer agent deploys code, the daemon extracts human-required tasks from the handoff (migrations to apply, secrets to set, browser verifications needed) and adds them to this register with deduplication, priority classification (critical/warning/info), and category classification (migration/secret/dns/verification/config/etc.).

### Ops Action Resolver Skill
I have a Claude Desktop skill (instructions file) called `ops-action-resolver` that can:
1. Read the register and classify pending items
2. Present a resolution plan for human approval
3. Execute automatable items: `wrangler d1 migrations apply`, `wrangler r2 bucket create`, `wrangler secret put` (with credential lookup from Bitwarden), file placements
4. Verify after execution (run verification commands, check tables exist, etc.)
5. Use Playwright browser automation for browser verification tasks
6. Update the register with results (backup → mutate → write → verify)
7. Handle stale items (verify-and-close vs re-execute)
8. Feed browser observation results back into the pipeline as directives

### IWO Daemon Integration
The daemon already has:
- `ops_actions.py` module with Pydantic models, fingerprint dedup, priority/category classification
- `_extract_ops_actions()` called after every handoff
- A 7th tmux window (index 6) designated "Agent 007" for ad-hoc work, with config: `agent_007_window: int = 6`, `agent_007_max_retries: int = 3`, `agent_007_timeout_seconds: int = 600`
- Directive processor that handles 8 directive types (start-spec, next-spec, resume, reconcile, status, pause, unpause, cancel-spec)
- HeadlessCommander that can dispatch prompts to any agent pane
- Deploy gate system (human approval for deploys)

## What I Want to Build

An "Ops Agent" — a 7th (or repurposed Agent 007) agent in the pipeline that the daemon can dispatch to resolve ops actions autonomously. When the Planner declares "blocked — only ops work remains", or when critical ops actions accumulate past a threshold, IWO would dispatch the Ops Agent with the ops-action-resolver skill content as its instructions.

## Design Questions I Need Help With

1. **Integration point:** Should this be a new directive type (`resolve-ops`), an extension of `next-spec` (Planner detects ops-only phases and routes to Ops Agent instead of Builder), or a parallel track that runs independently of the spec pipeline?

2. **Trigger conditions:** When should the Ops Agent activate? Options:
   - When Planner returns `outcome: "blocked"` with ops-only items remaining
   - When critical ops actions exceed a threshold (e.g., 3+ critical pending)
   - On a periodic schedule (e.g., after every N spec completions)
   - When BUILD-PRIORITY.md Phase 1 items are all ops-type
   - Manual directive only
   - Some combination

3. **Safety model:** The existing skill requires human approval before executing (Step 3: "Present plan, wait for confirmation"). In an autonomous pipeline context, what approval model makes sense?
   - Full human gate (like deploy gate — press a key to approve)
   - Auto-approve safe categories (migrations, file placements) but gate dangerous ones (secrets, DNS)
   - Dry-run first pass, human review, then execute
   - Trust the verification step as the safety net (execute, verify, roll back on failure)

4. **Handoff protocol:** After the Ops Agent resolves actions, what should its handoff look like? Should it hand back to Planner (to re-evaluate whether spec work is now unblocked)? Should it produce a different handoff schema than the standard spec-pipeline handoff?

5. **Scope boundary:** Should the Ops Agent ONLY resolve items from the register, or should it also handle the "Phase 1 verification" tasks from BUILD-PRIORITY.md that aren't in the register (like "verify offline mode end-to-end")? If the latter, it needs to understand BUILD-PRIORITY.md and create ops actions for items that don't exist in the register yet.

6. **Concurrency:** Should the Ops Agent run in parallel with the spec pipeline (resolving ops while Builder is coding), or sequentially (spec pipeline pauses, ops runs, then spec resumes)?

## Constraints

- The pipeline runs on a local Ubuntu machine with Claude Code agents in tmux
- Each agent is a separate Claude Code process with its own context window (~190K tokens)
- Agents communicate ONLY via filesystem (handoff JSON files) — no direct inter-process communication
- The daemon polls the filesystem every 1.5 seconds for new handoffs
- Browser automation is available via Playwright MCP but requires a debug Chrome instance running
- Wrangler CLI is available for Cloudflare operations
- Bitwarden CLI is available for credential retrieval (requires session unlock)
- The ops register JSON is the single source of truth for ops state
- Agent 007 (window index 6) already exists in tmux but is used for ad-hoc work

## What I'm Looking For

Architectural recommendations on the integration design. I'm a solo founder — I need the simplest correct solution, not the most sophisticated one. The existing 6-agent pipeline is battle-tested with 75+ completed specs. I want to extend it, not rewrite it.


---

## Consensus from Multi-Model Review (Gemini 2.5 Pro + GPT-5.2)

Both models were consulted via PAL-MCP on 2026-03-08. They converged on all six questions:

### Agreed Decisions

1. **Integration point:** New first-class directive `resolve-ops`. Do not overload `next-spec`. Reuse Agent 007 (tmux window 6) as the Ops Agent — formalise it, stop calling it ad-hoc.

2. **Trigger conditions:** Three triggers:
   - **Reactive:** When Planner returns `outcome: "blocked"` citing ops work → daemon auto-fires `resolve-ops`
   - **Proactive:** When critical ops actions ≥ 1 pending for > N minutes → daemon fires `resolve-ops`
   - **Manual:** `resolve-ops` directive always available as operator command

3. **Safety model:** Category-based tiered approval reusing deploy gate:
   - **Auto-approve:** migrations, verifications, file placements
   - **Human-gate:** secrets, DNS, unrecognised commands
   - Ops Agent writes plan first → daemon checks plan against category allowlist → auto-approves if all safe, pauses for human approval if any gated item exists
   - Timebox: max 10 actions or 15 minutes per run

4. **Handoff protocol:** Standard Pydantic envelope with ops-specific payload (resolved/skipped/failed items, register mutation metadata). After ops completion, route back to **Planner** to re-evaluate. Closed loop: Planner blocks → Ops resolves → Planner re-evaluates.

5. **Scope boundary:** v1: Ops Agent only resolves items from `.ops-actions.json` register. Planner is responsible for translating BUILD-PRIORITY.md items into register entries. v2: allow Planner to auto-seed register from BUILD-PRIORITY.md.

6. **Concurrency:** Sequential. Pipeline pauses while ops runs. No parallel execution in v1.

### Implementation Notes from This Session

- The existing `ops-action-resolver` skill (245 lines) at `/home/vanya/Nextcloud/skills/personal/custom/ops-action-resolver/SKILL.md` contains the complete execution logic — read register, classify, plan, execute, verify, update register. This becomes the Ops Agent's prompt/instructions.
- `ops_actions.py` (327 lines) already has the Pydantic models, fingerprint dedup, classification logic, and register CRUD.
- Agent 007 config already exists in `config.py`: window index 6, 10-min timeout, 3 retries, $5 budget.
- The daemon already has `_extract_ops_actions()` that runs after every handoff.
- `.ops-actions.json` is effectively single-writer (daemon writes via `ops_actions.py`), though the skill also writes during resolution sessions. Need single-writer semantics.
- Playwright MCP runs from same machine/user context. Debug Chrome on localhost:9222.

### What Triggered This

The Planner received a `next-spec` directive with BUILD-PRIORITY.md guidance saying "Phase 1 stabilisation first." It correctly read the file, correctly identified Phase 1 items as ops/verification tasks (not spec-shaped), declared `outcome: "blocked"`, and stopped. The pipeline has no mechanism to execute ops work autonomously — this is the gap the Ops Agent closes.
