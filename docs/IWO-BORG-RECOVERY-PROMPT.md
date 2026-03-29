# IWO Source Recovery from Borg Backup — Handoff Prompt

## Problem

The IWO (Ivan's Workflow Orchestrator) has regressed. 10 of 14 Python source files in `iwo/` are either **missing** or **older versions** compared to the advanced codebase that was running until ~March 8 evening. Two critical modules (`directives.py` and `ops_actions.py`) are completely absent. The remaining 8 files are older/smaller versions missing features like DirectiveProcessor, auto-continue, ops action integration, and expanded config.

**Root cause:** The `iwo/*.py` source files were never committed to git. They only existed as local working tree files. A combination of Nextcloud sync and `git checkout feature/headless-dispatch -- iwo/` (which restored old committed versions) destroyed the advanced source. The `.pyc` bytecode files in `__pycache__/` survived and prove the advanced versions existed.

**Evidence from .pyc analysis:**

```
FILES NEEDING RECOVERY:
  __init__.py    (disk=150b  → expected=211b,   diff=+61b)
  auditor.py     (disk=36242b → expected=36255b, diff=+13b)
  commander.py   (disk=3244b  → expected=19648b, diff=+16404b)
  config.py      (disk=4414b  → expected=6264b,  diff=+1850b)
  daemon.py      (disk=46912b → expected=74605b, diff=+27693b)
  directives.py  (MISSING,     expected=33477b)
  headless_commander.py (disk=17784b → expected=25509b, diff=+7725b)
  memory.py      (disk=19431b → expected=14641b, diff=-4790b)
  ops_actions.py (MISSING,     expected=12941b)
  tui.py         (disk=23858b → expected=24700b, diff=+842b)

FILES THAT MATCH (no recovery needed):
  metrics.py  (5885b)  — OK
  parser.py   (3935b)  — OK
  pipeline.py (13406b) — OK
  state.py    (934b)   — OK
```

**Key features in the advanced daemon.py (confirmed via bytecode inspection):**
- Imports: `directives.DirectiveProcessor`, `ops_actions.OpsAction`, `ops_actions.OpsActionsRegister`
- These modules implement: 8 directive types (start-spec, next-spec, resume, reconcile, status, pause, unpause, cancel-spec, resolve-ops), auto-continue between specs, ops action extraction/resolution, Agent 007 dispatch

**Compile timestamps of the advanced .pyc files (cpython-313):**
- daemon.py:           compiled 2026-03-08 10:01
- directives.py:       compiled 2026-03-08 13:55
- config.py:           compiled 2026-03-08 13:58
- ops_actions.py:      compiled 2026-02-26 08:53
- commander.py:        compiled 2026-02-21 19:56
- headless_commander:  compiled 2026-02-25 08:02
- tui.py:              compiled 2026-02-25 09:01

## Recovery Source

Borg backup repository at `/media/vanya/Junk-2/NUC9-Vorta-Borg-Backups`. Backups run every 6 hours via Vorta (Flatpak). The repo is encrypted — Vanya has the passphrase.

**Borg is NOT currently installed** in the system PATH. You need to install it first:
```bash
sudo apt install borgbackup -y
```

## Recovery Procedure

### Step 1 — Install borg and list archives

```bash
sudo apt install borgbackup -y
export BORG_REPO="/media/vanya/Junk-2/NUC9-Vorta-Borg-Backups"
borg list "$BORG_REPO" | tail -20
```
Vanya will enter the passphrase when prompted. You want the most recent archive from **March 8** (before March 9 morning when Nextcloud sync clobbered the files).

### Step 2 — Explore the archive to find the IWO source path

```bash
# Replace ARCHIVE_NAME with the chosen archive
borg list "$BORG_REPO::ARCHIVE_NAME" | grep "ivans-workflow-orchestrator/iwo/"
```

This will show the exact paths inside the backup. The source files should be at a path like:
`home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo/*.py`

### Step 3 — Extract IWO source to a temp directory

```bash
mkdir -p /tmp/iwo-recovery
cd /tmp/iwo-recovery
borg extract "$BORG_REPO::ARCHIVE_NAME" home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo/
```

### Step 4 — Verify recovered files match expected sizes

Run this verification script to confirm the recovered files match the .pyc bytecode expectations:

```python
python3 << 'PYEOF'
import marshal, struct, os

pycache = '/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo/__pycache__'
recovered = '/tmp/iwo-recovery/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo'

print("VERIFICATION — Recovered files vs .pyc expected sizes:")
all_match = True
for pyc in sorted(os.listdir(pycache)):
    if not pyc.endswith('.cpython-313.pyc'):
        continue
    base = pyc.replace('.cpython-313.pyc', '') + '.py'
    with open(os.path.join(pycache, pyc), 'rb') as f:
        f.read(12)
        expected = struct.unpack('<I', f.read(4))[0]
    
    recovered_path = os.path.join(recovered, base)
    if os.path.exists(recovered_path):
        actual = os.path.getsize(recovered_path)
        status = "MATCH" if actual == expected else f"MISMATCH (got={actual}, expected={expected})"
        if actual != expected:
            all_match = False
    else:
        status = "NOT IN BACKUP"
        all_match = False
    print(f"  {base}: {status}")

print(f"\nOverall: {'ALL MATCH — safe to restore' if all_match else 'MISMATCHES — check archive selection'}")
PYEOF
```

**If files don't match:** Try an older archive (March 7 or earlier). The compile timestamps show some files were last compiled Feb 21-26, so the advanced versions existed for weeks.

### Step 5 — Backup current files, then restore

```bash
# Backup current (regressed) versions just in case
cp -r /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo/ \
      /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo-regressed-backup/

# Copy recovered files over
cp /tmp/iwo-recovery/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo/*.py \
   /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo/

# Keep the schemas subdir (it wasn't affected)
```

### Step 6 — Special handling for config.py

The recovered config.py may have VPS IPs (74.50.49.35) instead of Ivan's local IPs. After restoring, verify and fix:

```bash
grep -n "qdrant_url\|neo4j_uri\|ollama_url" /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo/config.py
```

If it shows VPS IPs, update to:
- `qdrant_url`: `http://192.168.1.71:6333` (Ivan CT 201)
- `neo4j_uri`: `bolt://192.168.1.78:7687` (Ivan CT 202)  
- `ollama_url`: `http://192.168.1.76:11434` (Ivan CT 200)

### Step 7 — Test the restored IWO

```bash
cd /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator

# Test imports
.venv/bin/python -c "from iwo.directives import DirectiveProcessor; print('DirectiveProcessor OK')"
.venv/bin/python -c "from iwo.ops_actions import OpsActionsRegister; print('OpsActionsRegister OK')"
.venv/bin/python -c "from iwo.daemon import IWODaemon; print('Daemon OK')"
.venv/bin/python -c "from iwo.tui import main; print('TUI OK')"

# Check version string in daemon
grep -n "version" iwo/daemon.py | head -5
```

### Step 8 — CRITICAL: Commit ALL iwo/*.py to feature/headless-dispatch

This is the step that was never done before and caused all the trouble:

```bash
cd /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/.claude/worktrees/naughty-bohr

# Copy all restored .py files to the worktree
cp /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo/*.py iwo/

# Stage and commit
git add iwo/
git status
git diff --cached --stat

git commit -m "feat: commit ALL iwo source files including directives.py and ops_actions.py

Critical: these files were never committed and were lost when Nextcloud sync
overwrote the working tree. This commit preserves the full v2.9 feature set:
- directives.py: DirectiveProcessor with 8 directive types
- ops_actions.py: OpsAction/OpsActionsRegister for pipeline ops
- Updated daemon.py with directive/ops integration
- Updated commander.py, headless_commander.py, config.py, tui.py
- All advanced features: auto-continue, resolve-ops, Agent 007 dispatch"

# Push (switch to SSH, push, switch back)
git remote set-url origin git@github.com:No-Smoke/ivans-workflow.git
git push origin feature/headless-dispatch
git remote set-url origin https://github.com/No-Smoke/ivans-workflow.git
```

### Step 9 — Launch TUI and verify

Start the tmux session first (IWO needs agents running):
```bash
cd /home/vanya/Nextcloud/PROJECTS/ebatt-ai/ebatt
./scripts/boris-workflow/launch-tmux-agents-v5.sh
```

Then in a separate terminal:
```bash
cd /home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator
iwo-tui
```

**Expected:** TUI shows agents with status, PIPELINES populated, MEMORY indicators green (Qdrant/Neo4j/Ollama), SAFETY stats present, and advanced keybindings visible.

## Key Files Reference

| File | Path |
|------|------|
| IWO source | `/home/vanya/Nextcloud/PROJECTS/ivans-workflow-orchestrator/iwo/` |
| IWO __pycache__ | `.../iwo/__pycache__/` (DO NOT DELETE — contains proof of advanced versions) |
| Borg repo | `/media/vanya/Junk-2/NUC9-Vorta-Borg-Backups` |
| Git worktree (feature branch) | `.../ivans-workflow-orchestrator/.claude/worktrees/naughty-bohr` |
| Regressed backup | `.../ivans-workflow-orchestrator/iwo-regressed-backup/` (created in Step 5) |

## Memory Context

Retrieve session context with:
- `qdrant-new:semantic_search collection='project_memory_v2' query='IWO source recovery directives ops_actions pyc bytecode 2026-03-08'`
- `neo4j-memory-remote:search_memories query='IWO Daemon'`

**Project:** IWO (Ivan's Workflow Orchestrator)
