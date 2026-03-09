# Gap Analysis Prompt: IWO Ops Agent — Built vs Current vs Recommended

## Your Task

You have two documents attached to this conversation:

1. **OPS-AGENT-IMPLEMENTATION.md** — The build journal from 2026-03-08 documenting all 4 phases of the Ops Agent implementation, config audit findings, and live integration test results.

2. **OPS-AGENT-PEER-REVIEW-REPORT.md** — A multi-model peer review (Gemini 2.5 Pro, GPT-5.2, Gemini 3 Pro Preview) conducted on 2026-03-09, covering architecture assessment, bug fix quality, failure modes, security concerns, and prioritized recommendations.

Please produce a **gap analysis** comparing three states:

### State A: "What Was Built" (Implementation Doc)
- 4 phases completed: directive handler, reactive trigger, proactive trigger, handoff processing
- Config audit completed (3 fixes shipped)
- 17 dry-run tests passing
- Live integration test: PASS on IWO plumbing, PARTIAL on agent skill (register not updated)
- 3 bug fixes shipped in commit e764d9f

### State B: "What We Have Now" (Current Code)
- The implementation doc records what was built as of 2026-03-08
- The SKILL.md Step 5 was rewritten after the live test failure (single-execution Python heredoc pattern)
- The `ops_agent_budget_usd` and duplicate `agent_007_project_root` were removed per config audit
- Bug fixes e764d9f are merged

### State C: "What the Peer Review Recommends" (Review Report)
- P0 fixes: error propagation completion, skip-archive retry, atomic register writes, "other" category move
- P1 fixes: budget unification, model pinning, log rotation, re-discovery improvement, timeout enforcement
- P2 fixes: ops run ledger, rule-based gating, secret redaction, SQLite queue

## Requested Output

### 1. Delta Matrix
Create a table with columns: Issue | Built (State A) | Current (State B) | Recommended (State C) | Gap Status

For each issue, mark whether it's: DONE, PARTIALLY DONE, NOT STARTED, or REGRESSION.

### 2. Already-Addressed Items
List anything the implementation already handled that the peer review also flagged. These represent confirmed good decisions.

### 3. Critical Gaps (P0)
For each P0 recommendation, assess:
- What specifically needs to change in the code?
- Does it conflict with anything already built?
- Estimated implementation effort (lines of code, files touched)
- Risk of regression if implemented

### 4. Implementation Sequence
Propose an ordered implementation plan for the P0 + P1 items that minimizes risk. Consider dependencies between fixes (e.g., error propagation fix should precede skip-archive, since skip-archive depends on accurate failure detection).

### 5. Skill Defect Interaction
The implementation doc records a skill defect (Agent 007 didn't update the register). The peer review's P0-3 recommends atomic register writes. Analyze how these interact:
- Does P0-3 (atomic writes) solve or worsen the original skill defect?
- Is the rewritten SKILL.md Step 5 (Python heredoc) compatible with the `os.replace()` atomic pattern?
- What changes to the SKILL.md heredoc template are needed?

### 6. Test Coverage Assessment
The implementation has 17 dry-run tests. Which peer review recommendations are covered by existing tests, and which need new test scenarios?

## Format
Dense prose, minimal formatting. No emoji. Tables where they add clarity. Be direct about what's actually broken vs what's theoretical risk.
