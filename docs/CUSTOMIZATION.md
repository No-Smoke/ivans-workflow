# Customization Guide

Ivan's Workflow is designed to be extended. Everything project-specific lives in `.claude/` inside your project — the framework core stays clean and updatable.

## Adding Project-Specific Agents

Create custom subagents in `.claude/agents/`:

```bash
# Create a new agent definition
cat > .claude/agents/my-custom-agent.md << 'EOF'
---
name: my-custom-agent
description: "One-line description of when to invoke this agent"
model: inherit
allowed-tools:
  - Read
  - Grep
  - Glob
denied-tools:
  - Write
  - Edit
  - Bash
---

# My Custom Agent

## Purpose
What this agent does and why it exists.

## When to Use
- Trigger condition 1
- Trigger condition 2

## What It Does
1. Step one
2. Step two
3. Step three

## Hard Boundaries (DO NOT)
- Never do X
- Never do Y

## Safety Limits
- Max files: 10
- Timeout: 5 minutes

## Escalation Criteria
- When to stop and ask a human

## Definition of Done
- [ ] Criterion 1
- [ ] Criterion 2

## Output Format
Return a structured summary of findings.
EOF
```

Custom agents placed in `.claude/agents/` are automatically available to Claude Code — no configuration needed.

## Customizing Rules

### Stack Rules

`.claude/rules/project-stack.md` is generated from your tech stack answers. You can edit it freely — the installer won't overwrite existing customized files on re-run.

### Domain Rules

`.claude/rules/project-domain.md` is the most important customization point. Add:

- Domain terminology and definitions
- Business logic constraints
- Naming conventions specific to your project
- API design patterns
- Error message templates
- Security requirements

### Adding More Rule Files

Create any `.md` file in `.claude/rules/` — Claude Code loads all of them via glob matching. Use frontmatter to control scope:

```markdown
---
globs: ["src/api/**", "src/handlers/**"]
---

# API Handler Rules

Rules that only apply to API handler files.
```

## Overriding Hooks

### Disable a Hook

Edit `.claude/hooks.json` — remove or comment out entries you don't want.

### Add a Custom Hook

Add to `.claude/hooks.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": ".claude/hooks/my-custom-guard.sh"
          }
        ]
      }
    ]
  }
}
```

Write your hook script in `.claude/hooks/`. PreToolUse hooks receive tool input as JSON on stdin. Return exit code 2 with deny JSON to block the operation.

### Hook Types

| Hook Point | When | Blocking? |
|-----------|------|-----------|
| PreToolUse | Before file writes/edits | Yes (exit 2 blocks) |
| PostToolUse | After file writes/edits | No |
| Stop | When agent completes a response | Configurable |

## Configuring Credentials

### Add a New Credential

Edit `.claude/project-config.yaml`:

```yaml
credentials:
  - alias: my-api
    bitwarden_entry: "MyProject-API-Key"
    env_vars:
      - name: MY_API_KEY
        field: password
      - name: MY_API_ACCOUNT
        field: username
```

Then test: `.claude/scripts/credential-helper.sh get my-api`

### Credential Fields

| Field | Bitwarden Source |
|-------|-----------------|
| `password` | Login password |
| `username` | Login username |
| `url` | First URI |
| `notes` | Secure notes |

## Configuring MCP Servers

MCP servers are configured per-agent using `claude mcp add`. Since agents run in separate tmux windows, each can have different MCP configurations.

To add MCP servers that all agents share, configure them at the project level:

```bash
cd /path/to/your/project
claude mcp add qdrant -- npx -y @smithery/cli@latest run qdrant-mcp
claude mcp add github -- npx -y @modelcontextprotocol/server-github
```

## Creating Project-Specific Skills

Create skills in `.claude/skills/`:

```bash
mkdir -p .claude/skills/my-skill
cat > .claude/skills/my-skill/SKILL.md << 'EOF'
---
name: my-skill
description: "What this skill teaches Claude to do"
---

# My Skill

Instructions for Claude on how to perform a specific task...
EOF
```

### Required Skill Sections

All skills should include these two sections for consistency with the framework's quality standards:

**Honesty Protocol** — prevents inflated language and ensures factual reporting:

```markdown
## Honesty Protocol

- Never inflate language: no "robust", "comprehensive", "seamless", "cutting-edge"
- State what was actually done, not what was intended
- If something failed or was skipped, say so explicitly
- Evidence over claims: link to files, show output, cite line numbers
```

**Definition of Failure** — makes failure conditions explicit so the agent knows when to stop and report rather than guessing:

```markdown
## Definition of Failure

You have FAILED if:
- [Condition specific to this skill]
- [Another condition]

**On failure:** [What the agent should do instead of guessing]
```

These sections are not optional — the framework audit checks for their presence. Omitting them will be flagged as a compliance gap.

## Updating the Framework

When Ivan's Workflow gets updates:

```bash
cd ~/projects/ivans-workflow
git pull

# Re-run installer to update symlinks (won't overwrite your customizations)
~/projects/ivans-workflow/install.sh /path/to/your/project
```

If you used symlink mode (default), core files update automatically when you `git pull`. If you used copy mode, re-run the installer to get updates.

## Multi-Project Setup

Ivan's Workflow supports multiple projects simultaneously:

```bash
# Each project gets its own tmux session
~/projects/ivans-workflow/scripts/launch-tmux-agents.sh /path/to/project-a
# Session: ivan-project-a

~/projects/ivans-workflow/scripts/launch-tmux-agents.sh /path/to/project-b
# Session: ivan-project-b

# Switch between sessions
tmux switch-client -t ivan-project-a
tmux switch-client -t ivan-project-b
```
