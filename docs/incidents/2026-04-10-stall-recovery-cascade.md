# Incident Postmortem: Phantom Handoff Cascade from Stall Auto-Recovery

- **Date:** 2026-04-10 (NZST)
- **Duration:** ~17 minutes of active cascade (20:13–20:30), ~1 hour of prior stall investigation
- **Severity:** High — pipeline integrity corruption, sequence collisions, parallel agent execution on same spec
- **Status:** Contained. IWO + IWF shut down. Data integrity verified (git history clean). Phantom handoffs quarantined.
- **Author:** Vanya + Claude (Opus 4.6, Desktop workspace)
- **Spec affected:** EBATT-106 Sprint 2 (NSGA-II core)

## Summary

A Claude CLI stall during auto-continue dispatch of the EBATT-106 Sprint 2 Planner was recovered by manually authoring a handoff JSON. Sprint 2 then ran successfully end-to-end, committing and deploying working code (worker `77e8f6c1`). However, during Sprint 2 execution, IWO's Phase 2.9.2 stall auto-recovery feature erroneously synthesised phantom handoffs for agents that had already completed their work. These phantom handoffs cascaded through the pipeline creating sequence-number collisions, parallel agent execution on the same spec, and a premature Sprint 3 auto-continue dispatch. User intervened by killing all IWF agents and stopping IWO. No deployed code was corrupted; the damage was confined to the agent-comms handoff chain and pipeline state files.

## Timeline (NZST)

### Preamble: the original Planner stall
- **19:26:31** — Sprint 1 completes cleanly. `006-docs` written. Pipeline marked complete. Auto-continue (commit `28f8482`) fires a `start-spec` directive for EBATT-106 Sprint 2.
- **19:26:31** — IWO logs `Spec file not found for EBATT-106 — Planner will need to locate it`. Planner dispatched via headless `claude -p` anyway (PID 2067638).
- **19:26:32 → 20:26** — Planner process alive but zero stdout for 60 minutes. State `Sl+`, 0.8% CPU, waiting on I/O. Pane shows stale Sprint 1 stream-JSON output from the previous Planner run. Nine MCP child processes (perplexity, github, sequential-thinking, neo4j-cypher, neo4j-memory, tos-bridge, context7, desktop-commander, qdrant) all idle at 0% CPU. `ntfy` notifications repeatedly returning HTTP 429.
- **User diagnoses** via Claude Desktop: Planner hung before first tool call, likely at API connection / rate-limit / MCP initialization. Root cause of this specific stall not conclusively identified.

### Manual recovery and successful Sprint 2 execution
- **20:07** — User kills Planner process. IWF and IWO stopped.
- **20:08** — User + Claude manually author `007-planner-2026-04-10T08-07-42Z.json` pointing Builder at the existing implementation plan (`docs/plans/EBATT-106-implementation-plan.md`, which covered all 4 sprints from the Sprint 1 Planner run). Plan was saved to disk during the Sprint 1 Planner run and did not need regeneration.
- **20:10** — LATEST.json symlink updated to `007-planner`.
- **20:10** — IWO daemon and IWF tmux agents restarted.
- **20:10 → 20:14** — IWO detects `007-planner`, dispatches Builder. Builder runs, writes `008-builder` at **20:13:46**, commits `a5ae5102`. IWO dispatches Reviewer.
- **20:14:02** — Builder pane returns to shell. `_poll_agent_states` records PROCESSING→IDLE transition and **arms the stall watchdog for Builder** (`_stall_watchdog["builder"] = (20:14:02, "EBATT-106")`).
- **20:14:03** — Queue drain dispatches Builder again for an unrelated queued `EBATT-106 #7` — Builder runs briefly and exits at 20:15:42 with no handoff to write (nothing to do).
- **20:16:18** — `009-reviewer` written. Tester dispatched.
- **20:17:21** — `010-tester` written. Deployer dispatched.
- **20:17:42** — **First STALL WARNING fires** for Builder: `builder completed EBATT-106 120s ago, no handoff. Auto-recovery in 119s`. But Builder's real handoff (`008-builder`) had already been processed 3+ minutes earlier.
- **20:19:59** — Real deployment completes. Worker `77e8f6c1` live at ebatt.ai.
- **20:19:51** — **Auto-recovery fires** at 240s elapsed. `generate_auto_handoff` inspects git diff, runs tests (5505 pass), typecheck (clean), and writes phantom `012-builder-2026-04-10T08-19-42Z.json` marked `auto_generated: true`.

### The cascade
- **20:19:51** — IWO detects phantom `012-builder`, updates LATEST symlink (bypassing the real chain's `011-deployer`), dispatches Reviewer for seq 12.
- **20:20:46** — Real `011-deployer` arrives. IWO detects it (still honouring watchdog events), dispatches Docs for seq 11.
- **20:20:54** — Docs and Reviewer now running in parallel on EBATT-106 (different "chains").
- **20:21:25** — Phantom Reviewer completes (it correctly identifies the work is already done and writes a no-op `013-reviewer` noting "Sprint 2 already deployed as worker 77e8f6c1"). Tester dispatched for seq 13.
- **20:22:52** — Phantom Tester writes `014-tester` similarly noting duplicate pipeline.
- **20:23:09** — Real Docs agent writes **`014-docs`** — a legitimate Docs closure of the real chain but stamped with seq 14 because the phantom cascade had advanced the sequence counter. `workflowComplete: true`.
- **20:23:11** — IWO marks pipeline complete → auto-continue fires **for Sprint 3**.
- **20:23:22** — `start-spec EBATT-106` directive written. Planner dispatched for Sprint 3 via headless mode.
- **20:23:32** — Phantom Tester's `014-tester` (written at 20:22:52) finally picked up — IWO treats it as fresh since `already_processed` keys on `spec+agent+seq` (different agent from `014-docs`). Dispatches Deployer for seq 14.
- **20:23:34** — Now running simultaneously: Planner (Sprint 3), Deployer (phantom Sprint 2 chain), and Docs from seconds earlier.
- **20:25:00** — Phantom Deployer writes `015-deployer` (empty notes, re-deploying nothing).
- **20:29:40** — Auto-recovery fires again for Planner/Docs: phantom `018-planner` and `019-docs` written (both `auto_generated: true`).
- **20:30** — User observes Planner + Deployer simultaneously active, reports to Claude Desktop.
- **20:31** — User kills all IWF agents and stops IWO.

### Cleanup
- **20:32** — Claude verifies git state: real commits intact (`a5ae5102 feat + 45927761 docs + 0e0f60c5 docs`). Real deployment `77e8f6c1` live. No corruption to code or production.
- **20:32** — Phantom handoffs moved to `docs/agent-comms/EBATT-106/.phantom/`:
  - `012-builder-2026-04-10T08-19-42Z.json` (auto-generated)
  - `013-reviewer-2026-04-10T08-21-25Z.json` (phantom-chain response)
  - `014-tester-2026-04-10T08-22-52Z.json` (phantom-chain response)
  - `015-deployer-2026-04-10T08-45-00Z.json` (phantom re-deploy)
  - `018-planner-2026-04-10T08-29-40Z.json` (auto-generated Sprint 3)
  - `019-docs-2026-04-10T08-29-50Z.json` (auto-generated)
- **20:32** — `014-docs` (real chain) renamed to `012-docs` to restore sequential numbering.
- **20:32** — LATEST.json symlink restored to `012-docs-2026-04-10T08-30-00Z.json` (real Sprint 2 closure).

## Final state

- **Real handoff chain (post-cleanup):** 001-planner → 003-builder → 004-reviewer → 004-tester → 005-deployer → 006-docs (Sprint 1) → 007-planner → 008-builder → 009-reviewer → 010-tester → 011-deployer → 012-docs (Sprint 2)
- **Git history:** Clean. Three commits land Sprint 2 (`a5ae5102 feat` + `45927761 docs` + `0e0f60c5 docs`). No phantom commits.
- **Production:** Worker `77e8f6c1` live at ebatt.ai. 5505 tests passing. Sprint 2 (NSGA-II core: non-dominated sort, crowding distance, tournament selection) deployed correctly.
- **Pipeline state:** `.active-specs.json` shows EBATT-106 `status=completed`, `current_agent=null`, `handoff_count=19` (cosmetic artifact of cascade).
- **IWO daemon:** Stopped. IWF tmux session torn down.

## Root causes

### RC1 (PRIMARY — the bug that caused the cascade): Stall-watchdog arming race

Introduced by commit `dc10ae1` (Phase 2.9.2 auto-handoff generation).

In `iwo/daemon.py:_poll_agent_states`, when an agent transitions PROCESSING→IDLE the stall watchdog is armed:

```python
if prev == AgentState.PROCESSING:
    spec_id = self.pipeline.agent_current_spec(name)
    if spec_id:
        self._stall_watchdog[name] = (now, spec_id)
```

And in `process_handoff` (line 1130), the watchdog is cleared when a handoff is processed:

```python
self._stall_watchdog.pop(handoff.source_agent, None)
```

**The race:** Claude CLI has a shutdown delay of roughly 15 seconds between the agent writing its handoff JSON and the pane returning to shell. The real sequence during Sprint 2 Builder was:

1. `20:13:46` — Builder writes `008-builder-*.json`. IWO's watchdog observer fires immediately, `process_handoff` runs, `_stall_watchdog.pop("builder", ...)` runs — but Builder is not yet in the watchdog because the pane is still showing PROCESSING.
2. `20:13:47` — Handoff #8 dispatched to Reviewer. Pipeline state advances.
3. `20:14:02` — Builder's pane finally returns to shell (~15s after handoff write). `check_completions` detects the transition. `_poll_agent_states` sees `PROCESSING → IDLE` and **arms the watchdog for a spec_id that Builder has already completed**.
4. Nothing ever clears the watchdog again because Builder has no more work to do and no more handoffs to write.
5. `20:17:42` — 120s elapsed: stall warning logged.
6. `20:19:51` — 240s elapsed: `generate_auto_handoff` fires → phantom `012-builder` written.

The `pop()` guard was written assuming handoff arrival happens *after* the IDLE transition. In reality, the filesystem watcher (watchdog/inotify) fires before the pane state poller observes the shell-return. The two code paths race, and the watchdog arming happens last, so the clear is a no-op.

### RC2 (COMPOUNDING): Auto-recovery doesn't validate downstream progress

`iwo/auto_handoff.py:generate_auto_handoff` only inspects `spec_dir` to count existing files (for sequence numbering). It never checks: *has a downstream agent already written a handoff past the supposedly-stalled one?* At the time phantom-012 was generated, the spec dir already contained `009-reviewer`, `010-tester`, and `011-deployer`. Obviously Builder's work had been consumed downstream, but auto_handoff had no way to know.

The only existing guard (RC-adjacent commit `8e402ec`) is a hard-coded set of phantom spec IDs: `{"unknown", "NEXT-SPEC-SELECTION", "QUEUE-EXHAUSTED"}`. It doesn't protect real specs.

### RC3 (DESIGN): IWO tolerates sequence-number collisions across agents

`HandoffTracker.already_processed` keys on `handoff.idempotency_key`, which combines spec + source_agent + sequence. Two handoffs with seq 14 from different agents (Tester vs Docs) are treated as independent events and both get processed. There is no concept of "is this handoff on the current live chain?"

When phantom-012-builder was dispatched to Reviewer (seq 12) while real-011-deployer was still being processed by Docs (seq 11), IWO saw no conflict because the two chains had non-overlapping idempotency keys.

### RC4 (SEPARATE BUG): `start-spec` sprint-continuation doesn't resolve spec file path

Commit `28f8482` added `_detect_remaining_sprints` which locates the implementation plan at `docs/plans/{spec}-implementation-plan.md` (inside `project_root`). But the actual spec file lives at `ebatt-specs/v2-schema-first/EBATT-106.md` — a *sibling* repo outside `project_root`.

The directive processor dispatches Planner with `specId` only, logging `Spec file not found for EBATT-106 — Planner will need to locate it`. Planner must then rely on the skill prompt to find the spec, which adds retrieval complexity and is likely where the original 19:26 Planner stall happened. This is independent of RC1–RC3 and fails differently.

### RC5 (UNKNOWN UPSTREAM): Claude CLI stall at dispatch

The original 19:26 Planner stall — 60 minutes with zero stdout, 0.8% CPU, nine idle MCP children, PID `Sl+` state — has no conclusive root cause. Hypotheses:
- **Anthropic API rate limiting** during dispatch (`ntfy` was returning persistent HTTP 429s throughout, suggesting broader upstream pressure).
- **MCP initialization deadlock** at Claude CLI startup (all nine MCP children had spawned but none were active, suggesting the parent was blocked waiting on initialization completion for one of them).
- **Nextcloud sync interference** while the Claude CLI attempted to read project files (`project_root` is under `~/Nextcloud/PROJECTS/ebatt-ai/ebatt/`, not moved outside sync as IWO itself was).
- **Spec file not found warning** triggered a retrieval loop that hung on some MCP call.

This is the stall that Phase 2.9.2 was *designed to recover from* — so RC1 is the tragic irony: the bug fix for the rare real stall (RC5) introduced a far more frequent corruption path (RC1) that cascades on every normal agent shutdown.

## Contributing factors

- **No CI gate on IWO changes.** Auto-recovery was merged without a test covering the PROCESSING→IDLE-after-handoff-processed timing race. `tests/test_auto_handoff.py` contains 7 unit tests of `generate_auto_handoff` in isolation but no integration test of the daemon's stall watchdog under realistic timing.
- **Feature-flag kill switch exists but wasn't used.** `IWO_STALL_AUTO_HANDOFF` env var was designed in from the start (`dc10ae1` config section) but defaulted to `true`. For a new and risky recovery path, default-off with opt-in would have been safer.
- **Multi-sprint continuity (commit `28f8482`) landed same day as the auto-recovery feature.** Two new dispatch paths activated simultaneously made it hard to isolate which was responsible when things broke.
- **`ntfy` 429 errors persistent throughout.** Not a direct cause but a signal of broader upstream pressure that may have contributed to RC5.
- **The phantom guard in `8e402ec`** was written reactively after a phantom-'unknown' incident and only added a hard-coded blocklist — not a structural check.

## What saved us

- **Git is the source of truth, not agent-comms.** The real commits landed cleanly and production deployed correctly before the cascade began. The damage was contained to state files, not code or production.
- **Phantom agents were cautious.** The phantom Reviewer and Tester wrote honest no-op responses noting "Sprint 2 already deployed" rather than re-running the work, which would have duplicated commits or re-deployed.
- **User intervened quickly.** Approximately 10 minutes from first observed anomaly to killing all agents. If the cascade had run unattended longer, auto-continue might have advanced to Sprint 3 with phantom state.
- **Manual handoff recovery pattern was known.** From prior incidents documented in memory: "Builder commits code but sometimes exits without writing handoff JSON → pipeline stuck; manual handoff write unblocks it." This exact pattern unblocked the original Planner stall before the cascade.
- **LATEST.json was a symlink.** Allowed clean replacement during cleanup without corrupting file content.

## What went wrong in response

- **Claude Sonnet (running in the ebatt IWO pane) misdiagnosed the initial state.** It reported Sprint 2 as never having run because it confused the Sprint 1 Planner's pane scrollback (stream-JSON output from 07:05) with a fresh Sprint 2 run. A quick filesystem check would have revealed `nextAgent.target: "human"` on `006-docs` and a clean workflowComplete state. Diagnostic procedure should always start with the filesystem, not the tmux pane.
- **Cleanup happened before postmortem.** Claude Desktop moved phantoms to `.phantom/` before writing this document. Fresh context preserved, but ideally the state should be frozen (read-only) until after the incident is fully written up.

## Blast radius

- **Code:** No corruption. 3 real commits on main, 0 phantom commits.
- **Production:** No corruption. Worker `77e8f6c1` deployed correctly. `ebatt.ai` returning HTTP 200 throughout.
- **Tests:** 5505 passing. No regression.
- **Agent-comms:** 6 phantom handoff files (quarantined). Sequence numbering compressed (`014-docs` → `012-docs`) to remove gap.
- **Pipeline state:** `.active-specs.json` has cosmetic inflated `handoff_count: 19` (real count: 12). No functional impact.
- **Memory systems:** Qdrant and Neo4j received phantom handoffs #12–#14 via `iwo.memory` storage calls. These are recoverable but will need pruning to avoid polluting future `project_memory_v2` searches.
- **User time:** ~90 minutes (19:26 original stall → 20:50 incident close).
- **Compute cost:** Original Sprint 2 Planner stall: $0 (no API calls completed). Phantom Reviewer/Tester/Docs/Deployer/Planner re-runs: unknown, likely $3–6 total wasted (3 phantom Opus/Sonnet dispatches).

## Lessons

1. **File-watcher-driven state and poll-driven state must be synchronized.** IWO mixes watchdog (filesystem events) and 2-second polling. When these two systems both mutate `_stall_watchdog`, ordering is not deterministic. Needs either a single source of truth or explicit locks.
2. **Any auto-generator that writes to the canonical chain must check downstream state first.** "Generate a handoff" is a dangerous operation when there's no verification that the handoff is actually needed.
3. **New recovery paths default to off.** `IWO_STALL_AUTO_HANDOFF_ENABLED=false` should have been the default for the first N days after `dc10ae1`. Observability (Phase 2.9.1 warnings) should precede action (Phase 2.9.2 writes) by longer than one commit.
4. **Integration tests > unit tests for orchestration bugs.** The race is invisible in `test_auto_handoff.py` because it tests the generator in isolation. It's only visible when daemon timing is modelled.
5. **Spec file locations must be resolved by the directive processor, not deferred to the Planner agent.** Asking the Planner to "locate the spec" under load is a reliability hazard.
6. **Diagnose from the filesystem, not the tmux pane.** Pane scrollback is cheap but misleading. `LATEST.json`, `.current-spec`, `.active-specs.json`, and agent-comms file mtimes are authoritative.
7. **Commit kill-switches AND defaults-off for every new recovery mechanism.** Both are free; together they provide two layers of safety.

## Action items (see: STALL-RECOVERY-FIX-PLAN.md)

1. Disable `stall_auto_handoff_enabled` by default, revert or patch `dc10ae1`.
2. Add integration test reproducing the RC1 timing race.
3. Add downstream-progress check to `generate_auto_handoff`.
4. Add current-chain validation to `HandoffTracker`.
5. Resolve spec file path in `DirectiveProcessor`, not in Planner.
6. Investigate RC5 (original Claude CLI stall) — likely separate workstream.
7. Prune memory systems of phantom handoffs.
8. Add incident checklist to `chat-completion` skill.
