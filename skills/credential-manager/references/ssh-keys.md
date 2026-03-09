# SSH Key Management - Detailed Guide

## Table of Contents
- Retrieving SSH Keys (Python & CLI)
- Storing New SSH Keys
- Best Practices
- Troubleshooting

---

## Retrieving SSH Keys

### Method 1: CLI Extraction
```bash
export BW_SESSION=$(secret-tool lookup bitwarden master | bw unlock --raw)

# Extract ethospower.org SSH key
bw list items --session "$BW_SESSION" | \
  jq -r '.[] | select(.name == "ethospower.org SSH Private Key") | .notes' | \
  grep -A 8 "BEGIN OPENSSH PRIVATE KEY" > /tmp/ethospower_key

chmod 600 /tmp/ethospower_key

# Connect
ssh -i /tmp/ethospower_key -p 355 -o IdentitiesOnly=yes vanya@ethospower.org
```

### Method 2: Python Function
```python
import subprocess, json, re, os

def get_ssh_key(key_name):
    """Extract SSH private key from Bitwarden secure note"""
    master = subprocess.run(['secret-tool', 'lookup', 'bitwarden', 'master'], 
                          capture_output=True, text=True, check=True).stdout.strip()
    session = subprocess.run(['bw', 'unlock', '--raw'], 
                           input=master, capture_output=True, text=True, check=True).stdout.strip()
    
    result = subprocess.run(['bw', 'list', 'items', '--session', session],
                          capture_output=True, text=True, check=True)
    items = json.loads(result.stdout)
    
    for item in items:
        if item.get('name') == key_name and item.get('type') == 2:
            notes = item.get('notes', '')
            match = re.search(r'(-----BEGIN OPENSSH PRIVATE KEY-----.*?-----END OPENSSH PRIVATE KEY-----)', 
                            notes, re.DOTALL)
            if match:
                return match.group(1)
    return None

# Usage
key = get_ssh_key('ethospower.org SSH Private Key')
with open('/tmp/ssh_key', 'w') as f:
    f.write(key)
os.chmod('/tmp/ssh_key', 0o600)
```

---

## Storing New SSH Keys

```bash
export BW_SESSION=$(secret-tool lookup bitwarden master | bw unlock --raw)

PRIVATE_KEY=$(cat ~/.ssh/id_ed25519)
PUBLIC_KEY=$(cat ~/.ssh/id_ed25519.pub)
FINGERPRINT=$(ssh-keygen -lf ~/.ssh/id_ed25519 | awk '{print $2}')

cat > /tmp/ssh_note.json <<EOF
{
  "type": 2,
  "name": "hostname SSH Private Key",
  "secureNote": {"type": 0},
  "notes": "HOST: hostname\nPORT: 22\nUSER: username\n\nPRIVATE KEY:\n${PRIVATE_KEY}\n\nPUBLIC KEY:\n${PUBLIC_KEY}\n\nFINGERPRINT:\n${FINGERPRINT}"
}
EOF

bw create item "$(cat /tmp/ssh_note.json | bw encode)" --session "$BW_SESSION"
```

---

## Best Practices

**1. Remove passphrases before storing**
```bash
ssh-keygen -p -P 'old-passphrase' -N '' -f /path/to/key
```

**2. Always set proper permissions**
```bash
chmod 600 /tmp/ssh_key
```

**3. Use IdentitiesOnly to prevent auth failures**
```bash
ssh -i /tmp/key -o IdentitiesOnly=yes user@host
```

**4. Secure delete after use**
```bash
shred -u /tmp/ssh_key
```

---

## Troubleshooting

### "Too many authentication failures"
**Cause:** SSH tries multiple keys before yours  
**Fix:** Add `-o IdentitiesOnly=yes`

### "Permission denied (publickey)"
**Check:**
1. Key in server's `~/.ssh/authorized_keys`
2. File permissions: `chmod 600 /tmp/key`
3. Key validity: `ssh-keygen -y -f /tmp/key`

### "Load key: error in libcrypto"
**Cause:** Key still encrypted with passphrase  
**Fix:** `ssh-keygen -p -P 'passphrase' -N '' -f key`

### Verbose debugging
```bash
ssh -vvv -i /tmp/key user@host
# Look for "Offering public key" and authentication attempts
```

### Verify key matches server
```bash
# Extract public key from private key
ssh-keygen -y -f /tmp/key

# Compare with server
ssh user@host "cat ~/.ssh/authorized_keys"
```
