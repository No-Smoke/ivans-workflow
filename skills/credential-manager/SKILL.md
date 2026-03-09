---
name: credential-manager
description: Credential management using Bitwarden Password Manager CLI (`bw`). Cloud-synced across all devices. Auto-unlocks using GNOME Keyring. Use this skill whenever you need an API key, password, token, or SSH credential — whether the user explicitly asks for credentials OR when another task requires authentication (deploying to VPS, connecting to databases, configuring MCP servers, calling external APIs). Also use when storing new credentials, looking up database passwords, or managing secrets programmatically. If a workflow step fails due to missing authentication, check this skill first.
---

# Credential Manager - Bitwarden CLI

## Overview

Programmatic credential access via Bitwarden CLI with GNOME Keyring auto-unlock.
Works reliably from MCP servers, cron jobs, and other non-interactive shells
by injecting DBUS_SESSION_BUS_ADDRESS automatically. Uses exact name matching
to avoid Bitwarden CLI's fuzzy search collisions. Passes master password via
environment variable (`--passwordenv`) to avoid `/proc` exposure.

## Quick Start

Retrieve a credential (Python import — preferred):
```python
import sys
sys.path.insert(0, '/home/vanya/Nextcloud/skills/personal/custom/credential-manager')
from get_credential import get_credential

creds = get_credential('anthropic')
api_key = creds['secret']
```

Retrieve a credential (command line):
```bash
python3 ~/Nextcloud/skills/personal/custom/credential-manager/get_credential.py anthropic
python3 ~/Nextcloud/skills/personal/custom/credential-manager/get_credential.py anthropic --field secret --quiet
```

## Return Format

`get_credential()` returns a dict with these keys:

| Key | Contents |
|-----|----------|
| `secret` | The password, API key, or token (for Secure Notes: first custom field value, or notes text) |
| `username` | Login username (if any) |
| `url` | Associated URL (if any) |
| `service` | The alias you requested |
| `label` | The actual Bitwarden entry name |
| `fields` | Dict of all custom fields `{name: value}` (empty dict if none) |

## Error Recovery

If `get_credential()` raises `RuntimeError`, follow this sequence:

1. **Try the command-line fallback** with Desktop Commander:
   ```bash
   export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus"
   export BW_SESSION=$(secret-tool lookup bitwarden master | xargs -I{} bw unlock {} --raw)
   bw get item "Exact-Entry-Name" --session "$BW_SESSION" | jq -r '.login.password'
   ```
2. **If that also fails**, ask the user to unlock manually:
   "Could you run `bw unlock` in a terminal and paste the session key?"
3. **If the vault is unauthenticated** (not just locked), the user needs to `bw login` interactively — this cannot be automated.

## Service Mapping

| Alias | Bitwarden Entry |
|-------|-----------------|
| anthropic | Anthropic-API-Key |
| github | Github personal access token (classic) |
| perplexity | Perplexity-API-Key |
| deepseek | DeepSeek API |
| openai | OpenAI API Key for n8n |
| cloudflare | Cloudflare |
| elevenlabs | Eleven Labs Voice Generator |
| exa | Exa-API-Key |
| qdrant | Qdrant-API-Key |
| neo4j | Neo4j-Password |
| n8n | Claude-n8n-API-Key |
| openproject | OpenProject-API-Key |
| nextcloud | Nextcloud-Password |
| plane | Plane-API-Key |
| postiz | Postiz API Key - social.ethospower.org |
| borg | Borg-Backup-Passphrase |
| flow-vps | VPS-flow-ethospower |
| cloud-vps | cloud.ethospower.org SSH |
| erpnext | ERPNext-Vanya |
| erpnext-admin | ERPNext-Admin |
| erpnext-api | ERPNext-API |
| erpnext-api-admin | ERPNext-API-Admin |
| erpnext-db | ERPNext-DB-Root |
| monitor-token | ebatt-platform-monitor-auth-token |

## Storing Credentials

```bash
cd /home/vanya/Nextcloud/skills/personal/custom/credential-manager
python3 store_credential.py "Service-Name" --username user --password-prompt --url https://example.com
```

## Troubleshooting

If retrieval fails, check in this order:

1. **Vault locked**: `bw status` should show `"status":"locked"` or `"status":"unlocked"`. If `unauthenticated`, run `bw login`.
2. **GNOME Keyring password missing**: `DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus" secret-tool lookup bitwarden master` should return the master password. If empty, store it: `secret-tool store --label='Bitwarden' bitwarden master`.
3. **Name mismatch**: Run `python3 get_credential.py --list` and compare aliases to actual Bitwarden entries. Use `bw list items --search "name"` to find the real entry name.
4. **Stale vault cache**: Run `bw sync --session "$SESSION"` to force a sync.

## SSH Keys

SSH private keys are stored in Bitwarden Secure Notes. For detailed extraction
workflows (Python and CLI), see [references/ssh-keys.md](references/ssh-keys.md).

Note: The SSH reference file's `secret-tool | bw unlock` pipeline also needs
`DBUS_SESSION_BUS_ADDRESS` set first when running from non-interactive shells.

---

**Version:** 4.1  
**Updated:** 2026-03-05
