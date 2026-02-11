# Troubleshooting

Common issues and solutions for Ivan's Workflow.

## Agent Not Starting

**Symptom:** Agent window shows shell prompt instead of Claude Code.

**Causes & fixes:**

1. **Claude Code CLI not installed:** Run `claude --version`. If not found, install from [docs.anthropic.com/claude-code](https://docs.anthropic.com/claude-code).

2. **Wrong project directory:** The launch script reads `.claude/project-config.yaml` from the project. Ensure you're pointing to the right directory.

3. **tmux session conflict:** Kill the existing session first:
   ```bash
   tmux kill-session -t ivan-your-project
   # Or use the kill flag:
   ./scripts/launch-tmux-agents.sh --kill
   ```

4. **Staggered startup too fast:** If agents fail to initialize, increase the sleep delay in the launch script (default: 2-3 seconds between agents).

## Hooks Failing

**Symptom:** "Hook failed" errors when writing or editing files.

**Causes & fixes:**

1. **Hook not executable:** Fix permissions:
   ```bash
   chmod +x .claude/hooks/*.sh .claude/hooks/*.mjs
   ```

2. **Missing jq:** The builder-guard hook uses jq. Install: `sudo apt install jq`

3. **Wrong paths in hooks.json:** Check `.claude/hooks.json` — paths should be relative to project root.

4. **Hook blocking legitimate operations:** If `builder-guard.sh` is blocking something it shouldn't, check which rule is triggering and either update the hook or adjust your approach.

## Credential Errors

### "Vault is locked and auto-unlock unavailable"

The three-strategy fallback failed. Fix options (try in order):

1. **Set BW_SESSION directly:**
   ```bash
   export BW_SESSION=$(bw unlock --raw)
   ```

2. **Store master password in GNOME Keyring:**
   ```bash
   secret-tool store --label='Bitwarden' bitwarden master
   ```

3. **Check Bitwarden login:**
   ```bash
   bw status   # Should show "locked" or "unlocked", not "unauthenticated"
   bw login    # If unauthenticated
   ```

### "Bitwarden entry not found"

The entry name in `project-config.yaml` doesn't match your vault:
```bash
# List your vault items
bw list items --search "MyProject" | jq '.[].name'

# Compare with config
grep bitwarden_entry .claude/project-config.yaml
```

### "DBUS_SESSION_BUS_ADDRESS not set"

Common in SSH sessions and tmux spawned from cron. The credential helper tries to fix this automatically, but if it fails:
```bash
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
```

### Credentials not available to agents

The launch script sources `load-project-credentials.sh` before launching agents. If credentials aren't available:
1. Check the launch script output for errors during credential loading
2. Verify credentials work manually: `.claude/scripts/credential-helper.sh status`
3. Ensure `.claude/scripts/credential-helper.sh` is executable

## tmux Session Management

### Can't attach to session

```bash
# List all sessions
tmux list-sessions

# Attach to the right one
tmux attach -t ivan-your-project
```

### Session exists but agents are dead

```bash
# Check agent health
./scripts/monitor-agents.sh ivan-your-project

# Kill and restart
./scripts/launch-tmux-agents.sh --kill
./scripts/launch-tmux-agents.sh /path/to/project
```

### Multiple project sessions

```bash
# Each project gets a unique session name
tmux list-sessions
# ivan-project-a: ...
# ivan-project-b: ...

# Switch between them
tmux switch-client -t ivan-project-a
```

## Context Rot Recovery

**Symptom:** Agent responses become confused, repetitive, or lose track of the task.

**Fixes:**

1. **Use /compact-and-continue:** Saves state, compacts context, restores state.

2. **Manual handoff:** Use `/chat-completion` in the current agent, then start fresh with the handoff document.

3. **Kill and restart the agent:** Switch to the window, exit Claude Code, relaunch. The handoff files in `docs/agent-comms/` preserve state.

## Handoff Problems

### Handoff file not found

Check the handoff directory exists and has the right task ID:
```bash
ls -la docs/agent-comms/
# Should have folders named by task ID
```

### Agent can't find previous handoff

Ensure the agent reads from `{config:paths.handoffs}` (configured in project-config.yaml), not a hardcoded path.

### Wrong agent sequence

The standard sequence is: Planner → Builder → Reviewer → Tester → Deployer → Docs. If an agent is trying to hand off to the wrong next agent, check the handoff document's `next_agent` field.

## Installer Issues

### "Symlink target doesn't exist"

The framework directory may have moved. Re-run the installer:
```bash
~/projects/ivans-workflow/install.sh /path/to/project
```

### Installer overwrites my customizations

The installer is designed to be idempotent — it skips files that exist and aren't symlinks to the framework. If a file was overwritten, it was likely still a symlink. Convert it to a local copy first:
```bash
# Convert symlink to local copy
cp --remove-destination "$(readlink .claude/rules/project-stack.md)" .claude/rules/project-stack.md
```

### Install in a project without git

The installer doesn't require git, but some hooks (like classify-risk.sh) use git commands. Initialize git first:
```bash
git init
```

## Performance Issues

### Agents are slow to respond

1. Check if hooks are adding latency — temporarily disable Stop hooks in hooks.json
2. Ensure you're using the right model tier — haiku subagents should be fast
3. Check network connectivity to Anthropic API

### Ralph loop runs too many iterations

The ralph-loop command has a max of 50 iterations and stops if the same error repeats 10 times. If it's looping excessively, there may be a fundamental issue that auto-fix can't resolve. Stop it (Ctrl+C) and investigate manually.
