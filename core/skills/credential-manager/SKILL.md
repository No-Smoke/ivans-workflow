---
name: credential-manager
description: "Retrieve credentials from Bitwarden Password Manager CLI for the current project. Reads credential requirements from project-config.yaml. Supports auto-unlock via GNOME Keyring with manual fallback."
---

## Honesty Protocol

- Never inflate language: no "robust", "comprehensive", "seamless", "cutting-edge"
- State what was actually done, not what was intended
- If something failed or was skipped, say so explicitly
- Evidence over claims: link to files, show output, cite line numbers

# Credential Manager

Retrieve project credentials from Bitwarden CLI with a reliable three-strategy fallback.

## How It Works

This skill retrieves credentials defined in your project's `.claude/project-config.yaml` under the `credentials:` section. It does NOT hardcode any service names or Bitwarden entry names — those come from your project configuration.

### Credential Resolution Order

1. **BW_SESSION environment variable** — If set, use directly (fastest, most reliable)
2. **GNOME Keyring auto-unlock** — If master password stored in keyring, unlock automatically
3. **User prompt** — Ask user to run `bw unlock` and provide the session token

### Prerequisites

- Bitwarden CLI (`bw`) installed and logged in (`bw login`)
- For auto-unlock: master password stored in GNOME Keyring
  ```bash
  secret-tool store --label='Bitwarden' bitwarden master
  ```
- For manual mode: user runs `bw unlock` when prompted

---

## Usage

### Retrieve a credential by project alias

```bash
# The credential helper reads from project-config.yaml
.claude/scripts/credential-helper.sh get <alias>

# Examples (alias names come from YOUR project config):
.claude/scripts/credential-helper.sh get cloudflare    # → password + username
.claude/scripts/credential-helper.sh get github        # → password (PAT)
```

### Load all project credentials into environment

```bash
# Sets all env vars defined in project-config.yaml credentials section
source .claude/scripts/load-project-credentials.sh
```

### Check credential status

```bash
.claude/scripts/credential-helper.sh status
# Shows: Bitwarden status, which credentials are configured, which are loadable
```

### List configured credentials

```bash
.claude/scripts/credential-helper.sh list
# Shows credentials from project-config.yaml with their Bitwarden entry names
```

---

## Project Configuration

Credentials are defined in `.claude/project-config.yaml`:

```yaml
credentials:
  - alias: cloudflare
    bitwarden_entry: "MyProject-CloudFlare-API"
    env_vars:
      - name: CLOUDFLARE_API_TOKEN
        field: password
      - name: CLOUDFLARE_ACCOUNT_ID
        field: username

  - alias: github
    bitwarden_entry: "GitHub-PAT"
    env_vars:
      - name: GITHUB_TOKEN
        field: password
```

Each credential maps:
- `alias` → friendly name used in commands
- `bitwarden_entry` → exact item name in your Bitwarden vault
- `env_vars` → which fields to extract and what env var to set them as
- `field` → `password`, `username`, `url`, or `notes`

---

## When Auto-Unlock Fails

If you see: "Vault is locked and auto-unlock unavailable"

**Quick fix (this session only):**
```bash
export BW_SESSION=$(bw unlock --raw)
```
Then retry the credential operation.

**Permanent fix (store master password in keyring):**
```bash
secret-tool store --label='Bitwarden' bitwarden master
# Enter your Bitwarden master password when prompted
```

**Check status:**
```bash
bw status                                    # logged in? locked?
secret-tool lookup bitwarden master          # keyring has password?
echo $BW_SESSION | head -c 10               # session active?
```

---

## Security Notes

- Master password is NEVER passed as a CLI argument (visible in process list)
- When using auto-unlock, password is piped via stdin to `bw unlock`
- BW_SESSION tokens are ephemeral (expire when vault locks)
- Credential mappings in project-config.yaml contain entry NAMES only, never secrets
- The `.claude/settings.local.json` file (which may cache session info) is gitignored

---

## For the Launch Script

The tmux launch script calls `load-project-credentials.sh` during agent startup, so credentials are available to all agents automatically. If loading fails, the launch script will:

1. Print which credentials failed to load
2. Ask if you want to continue without them (some agents don't need all credentials)
3. Provide the `bw unlock` command to run manually

## Definition of Failure

You have FAILED if:
- You expose credentials in terminal output, logs, or commit history
- You proceed without credentials when the project config requires them
- You silently skip a credential that a downstream agent depends on
- You retry unlock more than 3 times without falling back to manual entry

**On failure:** Report which credentials could not be loaded and which agents are affected. Do not proceed with placeholder values.

---

**Version:** 1.0
**Framework:** Ivan's Workflow
