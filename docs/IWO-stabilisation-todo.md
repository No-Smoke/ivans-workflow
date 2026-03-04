# IWO Stabilisation — To-Do List

**Created:** 2026-02-22
**Updated:** 2026-02-22 (post context-loss audit)
**Goal:** IWO processes specs end-to-end with HITL approval via n8n.

---

## Phase 1: Fix Handoff Flow — STATUS: COMPLETE

All four items verified against codebase as of commit `9137d50` (IWO) and `8c3b1f9` (eBatt).

- [x] **1.1 Unify handoff schema** — `/agent-handoff` command and `workflow-handoff` skill both produce Schema A (with `status.outcome`, `metadata`, `nextAgent`). IWO Pydantic parser accepts this format.
- [x] **1.2 Fix recovery routing** — `_recover_state()` now detects unrouted handoffs (target agent hasn't responded with higher sequence), removes idempotency key, and queues for activation. 24h age limit prevents stale dispatching. Code at daemon.py:1112-1146.
- [x] **1.3 Fix prompt-handoff.sh** — Now references `/agent-handoff` command and mentions IWO automatic detection.
- [x] **1.4 Update agent skills for IWO awareness** — All 6 SKILL.md files have "IWO Integration" paragraph. All reference automatic detection and routing.

## Phase 1.5: Dispatch Reliability (NEXT — blocks Phase 2)

Despite Phase 1 fixes, Reviewer failed to pick up Builder handoff #012 for EBATT-006A. Planner manually re-routed via handoff #013. The rich activation prompt (commit `fed1c67`) needs validation after IWO restart.

- [ ] **1.5.1 Test rich prompt delivery** — Start IWO + agents, trigger a handoff, verify the full rich prompt text arrives in the target agent's tmux pane intact (not truncated by tmux send-keys). If truncated, switch to writing a temp prompt file and sending `cat /path/to/prompt.txt` instead.
- [ ] **1.5.2 Validate canary probe matching** — Manually inspect Claude Code's visible prompt (last 10 lines of pane) and compare against the `idle_prompt_pattern` regex (`[❯>]\s*$`). If Claude Code's prompt doesn't match, update the pattern.
- [ ] **1.5.3 Check pipeline.is_agent_busy()** — During a live dispatch attempt, log the return value of `is_agent_busy()` for the target agent. If it returns True when the agent is idle, trace through the assignment logic to find why.
- [ ] **1.5.4 Verify TUI timer fires** — Add a debug log line inside `_process_pending_activations()` showing queue depth. Watch logs to confirm it fires every ~2s. If not firing, the Textual timer may be blocked.
- [ ] **1.5.5 End-to-end single transition test** — Start IWO + agents. Manually write a simple test handoff file targeting Builder. Watch IWO detect, canary, and dispatch. Builder should start executing within 60s.

**Debug procedure:** Use the decision tree in IWO-IWF-BUGFIX-PROMPT.md:
1. Did IWO see the file? (check `received_at` stamp)
2. Is LATEST.json correct? (`readlink`)
3. Was canary attempted? (TUI log)
4. Did canary pass? (log messages)
5. Did agent execute? (check tmux pane for rich prompt text)
6. Pipeline state? (`is_agent_busy()`, queue depth)

## Phase 2: Build n8n HITL Bridge (do after Phase 1.5 works)

- [ ] **2.1 Add webhook endpoints to IWO** — IWO sends POST to n8n webhook URL on events: `handoff_complete`, `human_input_needed`, `pipeline_error`, `pipeline_complete`. Payload includes spec ID, agent, outcome summary, and a callback URL.
  - File: new module `iwo/notifications.py` or extend existing webhook code
- [ ] **2.2 Build n8n approval workflow** — n8n workflow: webhook trigger → format message → send Telegram notification with approve/reject buttons → Wait node → on approval, POST back to IWO callback URL.
  - Location: n8n.ethospower.org
- [ ] **2.3 Add callback endpoint to IWO** — IWO receives approval/rejection via HTTP and activates or pauses the next agent accordingly.
  - File: extend IWO with lightweight HTTP server (aiohttp or FastAPI)

## Phase 3: Validation

- [ ] **3.1 Single spec end-to-end** — Run a low-stakes spec through full pipeline: Planner → Builder → Reviewer → Tester → Deployer → Docs, with IWO routing and n8n notifications at each transition.
- [ ] **3.2 Process 10 specs** — Before pursuing Agent 007 Layer 2/3, IWO must process 10 specs end-to-end with HITL approval.

---

**Priority order:** 1.5.5 (quick smoke test) → 1.5.1 → 1.5.2 → 1.5.3 → 1.5.4 → 2.1 → 2.2 → 2.3 → 3.1 → 3.2
