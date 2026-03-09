#!/usr/bin/env python3
"""Auto-unlock Bitwarden CLI using GNOME Keyring stored password"""

import secretstorage
import subprocess
import sys
import os

def unlock_bitwarden():
    """Unlock Bitwarden vault and return session token"""
    
    # Check if already unlocked
    result = subprocess.run(['bw', 'unlock', '--check'], 
                          capture_output=True, text=True)
    if result.returncode == 0:
        # Already unlocked, get existing session
        session = os.environ.get('BW_SESSION')
        if session:
            return session
    
    # Retrieve master password from GNOME Keyring
    try:
        conn = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(conn)
        items = list(collection.search_items({'service': 'bitwarden', 'username': 'master'}))
        
        if not items:
            print("Error: Bitwarden master password not found in GNOME Keyring", file=sys.stderr)
            print("Run: python3 /home/vanya/scripts/store-bw-password.py", file=sys.stderr)
            return None
        
        password = items[0].get_secret().decode('utf-8')
        conn.close()
    except Exception as e:
        print(f"Error accessing GNOME Keyring: {e}", file=sys.stderr)
        return None
    
    # Unlock Bitwarden
    result = subprocess.run(['bw', 'unlock', '--raw'],
                          input=password,
                          capture_output=True,
                          text=True)
    
    if result.returncode != 0:
        print(f"Error unlocking Bitwarden: {result.stderr}", file=sys.stderr)
        return None
    
    session_token = result.stdout.strip()
    return session_token

def get_credential(service_name):
    """Get credential from Bitwarden by service name"""
    
    session = unlock_bitwarden()
    if not session:
        return None
    
    env = os.environ.copy()
    env['BW_SESSION'] = session
    
    # Search for item
    result = subprocess.run(['bw', 'list', 'items', '--search', service_name],
                          env=env,
                          capture_output=True,
                          text=True)
    
    if result.returncode != 0:
        print(f"Error searching Bitwarden: {result.stderr}", file=sys.stderr)
        return None
    
    import json
    items = json.loads(result.stdout)
    
    if not items:
        print(f"No credential found for: {service_name}", file=sys.stderr)
        return None
    
    return items[0]

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: bw-auto-unlock.py <service_name>")
        sys.exit(1)
    
    service = sys.argv[1]
    cred = get_credential(service)
    
    if cred:
        print(f"Name: {cred.get('name')}")
        if cred.get('login'):
            print(f"Username: {cred['login'].get('username', 'N/A')}")
            print(f"Password: {cred['login'].get('password', 'N/A')}")
        if cred.get('notes'):
            print(f"Notes: {cred['notes']}")
