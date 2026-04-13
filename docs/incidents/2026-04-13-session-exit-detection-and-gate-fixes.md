# 2026-04-13: Session-Exit Detection, Deploy Gate, and Pause Fixes

## Incident Summary

Multiple pipeline stalls on EBATT-109 Sprint 3 and Sprint 4 caused by Builder agent exiting without writing handoff JSON. Deploy gate (`Shift+D`) failed to prevent unauthorized Deployer dispatch. Pause (`p`) did not actually stop dispatches.

## Root Causes

### 1. No fast-path handoff detection on agent session exit

When an agent exited (PROCESSING→IDLE), IWO armed a stall watchdog but waited 240s before acting. With `stall_auto_handoff_enabled=False` (disabled after 2026-04-10 cascade incident), the pipeline simply HALTed at 240s requiring manual synthetic handoff injection. Two manual interventions were needed in one session.

### 2. `auto_handoff.py` bugs (3 bugs)

- **Hardcoded test directory** (line 69): `_run_tests()` targeted `src/__tests__/hybrid-calculator/` instead of the full test suite. Synthetic handoffs reported misleading test counts.
- **stdout+stderr concatenation** (lines 33-40, 75-77): `_run_cmd()` returned `stdout+stderr` as one string. When vitest wrote progress to stderr, JSON parsing of `--reporter=json` output failed.
- **No downstream-progress check**: `generate_auto_handoff()` could create redundant/duplicate handoffs even when the pipeline had already progressed past the stalled agent.

### 3. Deploy gate had a hidden auto-approve path

`Shift+D` toggled `auto_deploy_all` but NOT `auto_approve_safe_deploys` (default: `True`). When a Tester handoff included `deploymentInstructions` with `noNewMigrations/noNewSecrets/noNewWranglerVars` all true, `_should_auto_approve_deploy()` bypassed the gate. User pressed `Shift+D` twice and the Deployer was dispatched both times.

### 4. Pause (`p`) only stopped TUI polling, not dispatches

`_paused` was a TUI-only flag. The inotify `HandoffHandler` called `daemon.process_handoff()` directly — no pause check. Pressing `p` before an agent finished was ineffective at preventing the next dispatch.

## Fixes Applied

### Fix 1: Targeted session-exit detection (`daemon.py`)

Added `_session_exit_checks` mechanism — when an agent transitions PROCESSING→IDLE with a pipeline assignment, a 15-second grace period starts. After 15s, if no handoff file appeared for that agent:
- Checks `has_downstream_handoff()` to avoid redundancy
- Calls `generate_auto_handoff()` immediately
- Clears the stall watchdog (no need to wait 240s)
- Falls through to stall watchdog as backup if generation fails

This is a separate mechanism from the general auto-recovery (`stall_auto_handoff_enabled` stays `False`). It only fires on the specific PROCESSING→IDLE transition.

### Fix 2: `auto_handoff.py` bug fixes

- `_run_cmd()` now returns `(rc, stdout, stderr)` 3-tuple instead of `(rc, stdout+stderr)`
- `_run_tests()` runs full vitest suite (no hardcoded directory), parses JSON from stdout only, timeout 120s
- Added `has_downstream_handoff()` guard — checks if downstream agent already wrote a handoff before generating a synthetic one
- All callers updated for 3-tuple return

### Fix 3: Deploy gate closes both auto-approve paths (`tui.py`)

`Shift+D` now toggles both `auto_deploy_all` AND `auto_approve_safe_deploys`:
- Gate OPEN: both enabled
- Gate CLOSED: both disabled — all deploys require manual `d` approval
- TUI safety display updated to show `OPEN`, `SEMI` (safe auto-approve on), or `CLOSED` with pending count

### Fix 4: Pause actually stops all dispatches (`daemon.py` + `tui.py`)

- `daemon._paused` synced from TUI `_paused` on toggle
- `process_handoff()` checks `_paused` at entry — queues to `_pause_pending` list
- On unpause, `drain_pause_queue()` processes queued handoffs in FIFO order
- No handoffs are dropped — just deferred

## Agent Skill Updates (same session)

All 6 Boris workflow agent skills updated:
- Added EXIT GATE (MANDATORY) sections requiring handoff file before agent exit
- Replaced broken `HANDOFF-SCHEMA.md` references with inline IWO Pydantic Schema
- Fixed incorrect commands (`npm run typecheck` → `npx tsc --noEmit`, `npm run build` → `npx opennextjs-cloudflare build`)
- Added `summary.oneLiner` to handoff examples (IWO TUI display)
- Added pre-write validation checklists
- Docs agent: added multi-sprint check (Step 7) and `.current-spec` update logic

Skills location: `/home/vanya/Nextcloud/skills/shared/boris-workflow/`

## Validation

- PAL-MCP consensus (GPT-5.2 + Gemini-2.5-Pro) validated the session-exit detection approach
- All 188 IWO tests pass after changes
- Pre-existing test import failure in `test_dispatch_retry.py` (unrelated)

## Files Changed

| File | Changes |
|------|---------|
| `iwo/auto_handoff.py` | 3-tuple `_run_cmd`, full test suite, `has_downstream_handoff()` guard |
| `iwo/daemon.py` | `_session_exit_checks` mechanism, `_pause_pending` queue, `drain_pause_queue()`, pause gate in `process_handoff()` |
| `iwo/tui.py` | `Shift+D` toggles both flags, pause syncs to daemon, deploy gate display shows real state |
| `tests/test_auto_handoff.py` | Updated for 3-tuple `_run_cmd` return |
| 6x `boris-*-agent/SKILL.md` | EXIT GATE, inline schema, command fixes |

## Risk Assessment

- Session-exit detection fires at 15s with downstream-progress guard — low cascade risk
- General auto-recovery remains disabled — no change to 2026-04-10 fix
- Pause queue is unbounded but handoffs are small and infrequent — not a memory concern
