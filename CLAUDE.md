# Notes for Claude

## Project Status (as of 2026-02-18)

**COMPLETED:**
- Unified install scripts are working and tested on mjolnir06
- All 4 testing layers pass (syntax, mock, integration, functional)
- 20/20 verification checks pass on real Pi
- WiFi and cellular connectivity verified (including failover)
- HAMMA private repo installation via deploy key working
- Old `install_scripts/` directory deleted (recoverable from git history)

**KNOWN ISSUES:**
- DNS resolution bug on cellular sensors — `40-eth0.network` registers sensor IP as DNS server, and wwan0 DNS is never registered with systemd-resolved. Fix proposed (remove DNS from eth0, add resolvectl to wwan0 script) but not yet committed. See `~/.claude/hamma-system.md` under "DNS Resolution Failing on Cellular Sensors".
- Uncommitted changes may exist on working branches.

---

## Branching Convention

Feature branches should come off of the **current version branch** (currently `0.3.x`), NOT `master`.

Branches only get merged to `master` when we release a version.

```
feature-branch --> 0.3.x --> master (on release)
```

---

## Critical Rules - DO NOT Do These

### 1. NEVER use `sudo -u pi` without `-H` flag

```bash
# WRONG — HOME stays /root, files get wrong ownership
sudo -u pi git clone ...

# CORRECT — HOME=/home/pi
sudo -H -u pi git clone ...
```

### 2. NEVER put heredocs inside `bash -c` strings

```bash
# WRONG — heredoc parsed by outer shell
sudo -u pi bash -c "cat >> '$file' <<EOF
content
EOF"

# CORRECT — use tee
sudo -H -u pi tee -a "$file" > /dev/null <<EOF
content
EOF
```

### 3. NEVER use editable pip installs with sudo

```bash
# WRONG — permission issues
sudo -H -u pi pip install -e /home/pi/dev/brokkr

# CORRECT
sudo -H -u pi pip install /home/pi/dev/brokkr
```

### 4. NEVER forget `|| true` with bash arithmetic under `set -e`

```bash
# WRONG — exits when COUNT was 0
((COUNT++))

# CORRECT
((COUNT++)) || true
```

### 5. NEVER use `passwd` without username when running as root

```bash
# WRONG — changes root's password
passwd

# CORRECT
passwd pi
```

---

## Quick Reference

- **Repo development (install architecture, testing, dev gotchas):** [KNOWLEDGE.md](KNOWLEDGE.md)
- **Operational sensor knowledge (deployment, troubleshooting, services):** `~/.claude/hamma-system.md`
