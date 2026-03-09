#!/usr/bin/env python3
"""
Bitwarden Credential Store
Stores/updates credentials in Bitwarden via `bw` CLI.

Usage:
    python3 store_credential.py "Entry-Name" --username API --password-prompt --url https://api.example.com
"""

import subprocess
import json
import sys
import os
import argparse
import getpass
import base64

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from get_credential import ensure_unlocked


def store_credential(name: str, username: str, password: str, url: str = '', notes: str = '') -> bool:
    """Create a new credential in Bitwarden."""
    session = ensure_unlocked()
    
    # Get template
    result = subprocess.run(
        ['bw', 'get', 'template', 'item', '--session', session],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get template: {result.stderr}")
    
    item = json.loads(result.stdout)
    item['name'] = name
    item['type'] = 1  # Login
    item['login'] = {
        'username': username,
        'password': password,
        'uris': [{'uri': url, 'match': None}] if url else []
    }
    if notes:
        item['notes'] = notes
    
    encoded = base64.b64encode(json.dumps(item).encode()).decode()
    
    result = subprocess.run(
        ['bw', 'create', 'item', encoded, '--session', session],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create: {result.stderr}")
    
    # Sync to cloud
    subprocess.run(['bw', 'sync', '--session', session], capture_output=True)
    return True


def update_credential(name: str, password: str = None, username: str = None, url: str = None) -> bool:
    """Update existing credential."""
    session = ensure_unlocked()
    
    # Get existing item
    result = subprocess.run(
        ['bw', 'get', 'item', name, '--session', session],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Item not found: {name}")
    
    item = json.loads(result.stdout)
    item_id = item['id']
    
    if password:
        item['login']['password'] = password
    if username:
        item['login']['username'] = username
    if url:
        item['login']['uris'] = [{'uri': url, 'match': None}]
    
    encoded = base64.b64encode(json.dumps(item).encode()).decode()
    
    result = subprocess.run(
        ['bw', 'edit', 'item', item_id, encoded, '--session', session],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to update: {result.stderr}")
    
    subprocess.run(['bw', 'sync', '--session', session], capture_output=True)
    return True


def main():
    parser = argparse.ArgumentParser(description='Store credentials in Bitwarden')
    parser.add_argument('name', help='Entry name')
    parser.add_argument('--username', '-u', default='API')
    parser.add_argument('--password', '-p', help='Password (use --password-prompt for secure input)')
    parser.add_argument('--password-prompt', action='store_true')
    parser.add_argument('--url')
    parser.add_argument('--notes')
    parser.add_argument('--update', action='store_true', help='Update existing')
    
    args = parser.parse_args()
    
    if args.password_prompt:
        password = getpass.getpass("Password/API key: ")
    elif args.password:
        password = args.password
    else:
        print("Error: --password or --password-prompt required", file=sys.stderr)
        return 1
    
    try:
        if args.update:
            update_credential(args.name, password=password, username=args.username, url=args.url)
            print(f"✓ Updated: {args.name}")
        else:
            store_credential(args.name, args.username, password, args.url, args.notes)
            print(f"✓ Created: {args.name}")
        return 0
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
