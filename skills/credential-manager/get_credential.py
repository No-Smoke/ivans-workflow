#!/usr/bin/env python3
"""
Bitwarden Credential Retriever
Retrieves credentials from Bitwarden Password Manager via `bw` CLI.
Master password is stored in GNOME Keyring for auto-unlock.

Usage:
    python3 get_credential.py <service>
    python3 get_credential.py <service> --field secret --quiet
    
    # As module
    from get_credential import get_credential
    creds = get_credential('anthropic')

Reliability notes:
    - Sets DBUS_SESSION_BUS_ADDRESS for non-interactive shells (MCP servers, cron)
    - Uses exact name matching via `bw list items` to avoid fuzzy match collisions
    - Passes master password via environment variable (--passwordenv) to avoid /proc exposure
"""

import subprocess
import json
import sys
import os
import argparse


# Service name to Bitwarden item name mapping
SERVICE_MAPPING = {
    # API Keys
    'anthropic': 'Anthropic-API-Key',
    'github': 'Github personal access token (classic)',
    'perplexity': 'Perplexity-API-Key',
    'exa': 'Exa-API-Key',
    'deepseek': 'DeepSeek API',
    'elevenlabs': 'Eleven Labs Voice Generator',
    'cloudflare': 'Cloudflare',
    'openai': 'OpenAI API Key for n8n',
    
    # Infrastructure
    'qdrant': 'Qdrant-API-Key',
    'neo4j': 'Neo4j-Password',
    'n8n': 'Claude-n8n-API-Key',
    'openproject': 'OpenProject-API-Key',
    'postiz': 'Postiz API Key - social.ethospower.org',
    'nextcloud': 'Nextcloud-Password',
    'plane': 'Plane-API-Key',
    'plane-webhook': 'Plane-Webhook-Secret',
    
    # Backup
    'borg': 'Borg-Backup-Passphrase',
    
    # VPS
    'flow-vps': 'VPS-flow-ethospower',
    'cloud-vps': 'cloud.ethospower.org SSH',
    
    # ERPNext
    'erpnext': 'ERPNext-Vanya',
    'erpnext-vanya': 'ERPNext-Vanya',
    'erpnext-admin': 'ERPNext-Admin',
    'erpnext-api': 'ERPNext-API',
    'erpnext-api-admin': 'ERPNext-API-Admin',
    'erpnext-db': 'ERPNext-DB-Root',
    
    # eBatt Platform
    'monitor-token': 'ebatt-platform-monitor-auth-token',
}

# Cache the session key within a single process lifetime
_cached_session = None


def _get_env_with_dbus():
    """Return an env dict with DBUS_SESSION_BUS_ADDRESS set.
    
    MCP servers and non-interactive shells lack the D-Bus session
    variable, which secret-tool requires. We inject it from the
    well-known socket path.
    """
    env = os.environ.copy()
    if 'DBUS_SESSION_BUS_ADDRESS' not in env:
        uid = os.getuid()
        dbus_path = f"/run/user/{uid}/bus"
        if os.path.exists(dbus_path):
            env['DBUS_SESSION_BUS_ADDRESS'] = f"unix:path={dbus_path}"
    return env


def _run_bw(args: list, session: str = None, env=None) -> subprocess.CompletedProcess:
    """Run a bw CLI command with proper environment."""
    if env is None:
        env = _get_env_with_dbus()
    cmd = ['bw'] + args
    if session:
        cmd.extend(['--session', session])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)


def get_master_password() -> str | None:
    """Get Bitwarden master password from GNOME Keyring."""
    try:
        env = _get_env_with_dbus()
        result = subprocess.run(
            ['secret-tool', 'lookup', 'bitwarden', 'master'],
            capture_output=True, text=True, timeout=5, env=env
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def ensure_unlocked() -> str:
    """Ensure vault is unlocked and return session key.
    
    Strategy:
    1. Use cached session if valid
    2. Use BW_SESSION env var if valid
    3. Unlock with master password from GNOME Keyring
    
    Returns: session key string
    Raises: RuntimeError if unlock fails
    """
    global _cached_session
    env = _get_env_with_dbus()
    
    # Try cached session first
    for candidate in [_cached_session, os.environ.get('BW_SESSION', '')]:
        if candidate:
            result = _run_bw(['status', '--session', candidate], env=env)
            if result.returncode == 0:
                status = json.loads(result.stdout)
                if status.get('status') == 'unlocked':
                    _cached_session = candidate
                    return candidate
    
    # Check vault status
    result = _run_bw(['status'], env=env)
    if result.returncode != 0:
        raise RuntimeError(f"bw status failed: {result.stderr}")
    status = json.loads(result.stdout)
    
    if status.get('status') == 'unauthenticated':
        raise RuntimeError("Not logged into Bitwarden. Run: bw login")
    
    # Vault is locked or unlocked-without-session — either way we need to unlock
    master_pw = get_master_password()
    if not master_pw:
        raise RuntimeError(
            "Vault is locked and no master password in GNOME Keyring.\n"
            "Either:\n"
            "1. Set BW_SESSION env var: export BW_SESSION=$(bw unlock --raw)\n"
            "2. Store master password: secret-tool store --label='Bitwarden' bitwarden master"
        )
    
    result = subprocess.run(
        ['bw', 'unlock', '--raw', '--passwordenv', 'BW_MASTER_PW'],
        capture_output=True, text=True, timeout=30,
        env={**env, 'BW_MASTER_PW': master_pw}
    )
    if result.returncode != 0 or len(result.stdout.strip()) < 50:
        raise RuntimeError(
            f"Unlock failed: {result.stderr or result.stdout[:200]}\n"
            f"Try manually: bw unlock --raw"
        )
    
    session = result.stdout.strip()
    _cached_session = session
    return session


def _find_item_exact(item_name: str, session: str) -> dict | None:
    """Find a Bitwarden item by exact name match.
    
    Uses `bw list items --search` then filters for exact name equality.
    This avoids the fuzzy matching of `bw get item` which fails when
    entries share name prefixes (e.g. Anthropic-API-Key vs 
    Anthropic-API-Key-SmythOS).
    """
    result = _run_bw(['list', 'items', '--search', item_name], session=session)
    if result.returncode != 0:
        return None
    
    items = json.loads(result.stdout)
    # Filter for exact name match (case-sensitive)
    exact = [i for i in items if i.get('name') == item_name and not i.get('deletedDate')]
    
    if not exact:
        return None
    
    # If multiple exact matches exist (true duplicates), take the most recently revised
    if len(exact) > 1:
        exact.sort(key=lambda i: i.get('revisionDate', ''), reverse=True)
    
    return exact[0]


def get_credential(service: str) -> dict:
    """
    Retrieve credential from Bitwarden.
    
    Args:
        service: Service name (e.g., 'anthropic', 'github', 'qdrant')
                 OR exact Bitwarden entry name if no alias exists
    
    Returns:
        dict with keys: secret, url, username, service, label
    """
    item_name = SERVICE_MAPPING.get(service.lower(), service)
    session = ensure_unlocked()
    
    item = _find_item_exact(item_name, session)
    
    if item is None:
        if service.lower() in SERVICE_MAPPING:
            raise RuntimeError(
                f"No exact match for '{item_name}' in Bitwarden.\n"
                f"Check the entry exists with exactly that name."
            )
        else:
            raise RuntimeError(
                f"No entry found for '{service}'\n"
                f"Known aliases: {', '.join(sorted(SERVICE_MAPPING.keys()))}\n"
                f"Or use exact Bitwarden entry name"
            )
    
    login = item.get('login', {})
    
    # Determine secret value based on item type
    item_type = item.get('type', 1)
    if item_type == 2:
        # Secure Note — check custom fields first (most reliable for tokens/keys),
        # then fall back to notes text
        fields = item.get('fields', [])
        if fields:
            # Use the first custom field's value (type 1 = hidden, type 0 = text)
            secret = fields[0].get('value', '')
        else:
            secret = item.get('notes', '')
    else:
        # Login item — secret is in login.password
        secret = login.get('password', '')
    
    # Build fields dict for callers that need specific custom fields
    custom_fields = {}
    for f in item.get('fields', []):
        custom_fields[f.get('name', '')] = f.get('value', '')
    
    return {
        'secret': secret,
        'url': login.get('uris', [{}])[0].get('uri', '') if login.get('uris') else '',
        'username': login.get('username', ''),
        'service': service,
        'label': item.get('name', item_name),
        'fields': custom_fields,
    }


def mask_secret(secret: str, visible: int = 4) -> str:
    """Mask a secret, showing only first few characters."""
    if len(secret) <= visible:
        return '*' * len(secret)
    return secret[:visible] + '*' * (len(secret) - visible)


def main():
    parser = argparse.ArgumentParser(description='Retrieve credentials from Bitwarden')
    parser.add_argument('service', nargs='?', help='Service name')
    parser.add_argument('--field', choices=['secret', 'url', 'username', 'label'])
    parser.add_argument('--quiet', '-q', action='store_true', help='Value only')
    parser.add_argument('--list', '-l', action='store_true', help='List services')
    
    args = parser.parse_args()
    
    if args.list:
        print("Available services:")
        for svc, item in sorted(SERVICE_MAPPING.items()):
            print(f"  {svc:15} → {item}")
        return 0
    
    if not args.service:
        parser.print_help()
        return 1
    
    try:
        creds = get_credential(args.service)
        
        if args.field:
            value = creds.get(args.field, '')
            if args.quiet:
                print(value)
            elif args.field == 'secret':
                print(f"{args.field}: {mask_secret(value)}")
            else:
                print(f"{args.field}: {value}")
        else:
            if args.quiet:
                print(creds['secret'])
            else:
                print(f"Service:  {creds['service']}")
                print(f"Label:    {creds['label']}")
                print(f"Username: {creds['username']}")
                print(f"URL:      {creds['url']}")
                print(f"Secret:   {mask_secret(creds['secret'])}")
        return 0
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
