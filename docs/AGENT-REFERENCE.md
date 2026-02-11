# Agent Reference

Complete reference for all agents and subagents in Ivan's Workflow.

## Main Agents (tmux Windows)

These agents run as persistent Claude Code sessions in tmux windows.

| Window | Agent | Mode | Purpose |
|--------|-------|------|---------|
| 0 | 🔵 Planner | Plan | Architecture, design, task breakdown |
| 1 | 🟢 Builder | Auto-accept | Implementation execution |
| 2 | 🟡 Reviewer | Interactive | Code review, quality checks |
| 3 | 🟣 Tester | Interactive | Test execution, verification |
| 4 | 🔴 Deployer | Interactive | Deployment and PR creation (optional) |
| 5 | 🟠 Docs | Interactive | Documentation updates (optional) |

### 🔵 Planner

**Skill:** `core/skills/planner-agent/SKILL.md`
**Mode:** Plan mode (architecture only, no code execution)

The Planner receives tasks or spec IDs, analyzes requirements, checks schema and existing patterns, and produces a phased implementation plan. It writes a structured handoff document for the Builder.

**Does:** Analyze specs, check schema, create implementation plans, identify risks, break down tasks
**Does NOT:** Write implementation code, run tests, review code, deploy, modify any source files

### 🟢 Builder

**Skill:** `core/skills/builder-agent/SKILL.md`
**Mode:** Auto-accept (or interactive, configurable)

The Builder reads the Planner's handoff and implements code phase by phase. It runs tests after each phase and commits incrementally using `/test-and-commit`. Invokes `@schema-guardian` for schema changes.

**Does:** Write code, run tests, commit incrementally, invoke subagents for specialized tasks
**Does NOT:** Plan architecture, review own code, deploy, modify schema directly

### 🟡 Reviewer

**Skill:** `core/skills/reviewer-agent/SKILL.md`
**Mode:** Interactive

The Reviewer reads the Builder's handoff and git diff, runs automated pre-review checks, then performs focused manual review against spec acceptance criteria. Uses a two-pass protocol: automated checks first, then targeted human-like review.

**Does:** Review code, verify spec compliance, check patterns, flag issues, verify quality gates
**Does NOT:** Write code, fix issues (sends back to Builder), deploy, run full test suites

### 🟣 Tester

**Skill:** `core/skills/tester-agent/SKILL.md`
**Mode:** Interactive

The Tester runs the full test suite, integration tests, and verifies all quality gates pass. Can invoke subagents for specialized testing (integration, performance, deployment).

**Does:** Run tests, verify builds, invoke deployment subagents, verify quality gates
**Does NOT:** Write production code, review code, plan architecture

### 🔴 Deployer (Optional — Agent 5)

**Skill:** `core/skills/deployer-agent/SKILL.md`
**Mode:** Interactive

The Deployer runs the deploy command from project-config.yaml, creates PRs via `@pr-architect`, and verifies deployment health.

**Does:** Deploy, create PRs, verify deployment health, rollback if needed
**Does NOT:** Write code, review, plan

### 🟠 Docs (Optional — Agent 6)

**Skill:** `core/skills/docs-agent/SKILL.md`
**Mode:** Interactive

The Docs agent updates project documentation — CLAUDE.md, changelogs, spec docs, API documentation.

**Does:** Update documentation, generate changelogs, maintain CLAUDE.md
**Does NOT:** Write source code, run tests, deploy

---

## Subagents

Subagents are invoked by main agents using `@agent-name` syntax. They run within the invoking agent's context.

| Subagent | Model | Invoked By | Purpose |
|----------|-------|-----------|---------|
| @code-simplifier | inherit | Builder, Reviewer | Reduce complexity without changing behavior |
| @verify-app | haiku | Tester, Deployer | Quick health check: compile, start, respond |
| @ux-reviewer | inherit | Reviewer | UI/UX usability assessment |
| @schema-guardian | sonnet | Builder | Safe schema changes with backup/rollback |
| @integration-tester | sonnet | Tester | Integration/E2E tests against running server |
| @pr-architect | haiku | Deployer, Tester | Create well-structured PRs with risk assessment |
| @perf-monitor | haiku | Tester, Deployer | Bundle size, cold start, response latency checks |
| @docs-agent | sonnet | Any | Documentation updates (when Docs isn't a full agent) |

### @code-simplifier

Reduces code complexity without changing behavior. Checks tests pass first, then: flatten conditionals → extract duplication → improve naming → remove dead code → verify tests still pass.

**Safety:** Max 15 files, 10 passes, 500-line diff, 5-minute timeout
**Escalation:** Same pattern fails 3x, >15 files affected, public API change needed

### @verify-app

Fast verification that the application compiles, starts, and responds. Runs typecheck → starts dev server → curls health endpoints → checks for errors → stops server.

**Model:** haiku (fast, cheap)
**Safety:** Read-only — cannot modify any files

### @ux-reviewer

Reviews UI/UX decisions for usability, accessibility, and consistency. Read-only — produces a findings report with severity ratings.

**Trigger:** Only for specs tagged as UI/frontend work

### @schema-guardian

Validates schema changes safely. Creates backup → applies change → regenerates types → typechecks → runs affected tests → reports or rolls back on failure.

**Scope:** Only active when `schema.enabled: true` in project config
**Safety:** Can only write to schema/ and generated/ directories

### @integration-tester

Runs integration and E2E tests against a running dev server. Starts server, runs test suite, tests key endpoints, checks database connectivity.

### @pr-architect

Creates well-structured PRs with risk assessment. Classifies changes as CRITICAL/HIGH/MEDIUM/LOW based on what files were touched. Generates PR body from template.

**Risk classification:**
- CRITICAL: schema, auth, payment, deployment config
- HIGH: new API handlers, database migrations
- MEDIUM: new source files, test changes
- LOW: documentation, comments, formatting

### @perf-monitor

Checks bundle size, cold start time, and API response latency. Compares against configurable thresholds. Reports pass/warn/block.

---

## Workflow Sequence

```
  Task/Spec
      │
      ▼
  🔵 Planner ──────► Implementation Plan
      │
      ▼
  🟢 Builder ──────► Code + Tests + Commits
      │
      ▼
  🟡 Reviewer ─────► Approval or Rejection
      │                    │
      │ (approved)         │ (issues found)
      ▼                    ▼
  🟣 Tester          Back to Builder
      │
      ▼
  🔴 Deployer ─────► PR + Deploy (optional)
      │
      ▼
  🟠 Docs ──────────► Documentation (optional)
```
