# Unified Install - Development Notes

This document summarizes bugs, gotchas, and lessons learned during development of the unified install scripts. This is a developer reference - for user troubleshooting, see README.md.

**Last Updated:** 2026-01-25

---

## Critical Bugs Fixed

### 1. Files Owned by Root Instead of Pi

**Problem:** Running `sudo ./install.sh` caused all user files to be owned by root:
- SSH keys in `/home/pi/.ssh/`
- Virtual environment at `/home/pi/dev/ltgenv/`
- Brokkr config at `/home/pi/.config/brokkr/`
- Git repos in `/home/pi/dev/`

**Root Cause:** `sudo -u pi` changes the user but does NOT change `$HOME`. It stays as `/root`.

**Fix:** Use `sudo -H -u pi` (the `-H` flag sets HOME to the target user's home directory):
```bash
# Wrong - HOME stays /root
sudo -u pi git clone ...

# Correct - HOME becomes /home/pi
sudo -H -u pi git clone ...
```

**Files Changed:** All lib/*.sh files, 26+ occurrences fixed.

---

### 2. SSH Config Permission Denied (Heredoc Issue)

**Problem:** When generating HAMMA SSH key, got error:
```
bash: /home/pi/.ssh/config: Permission denied
```

**Root Cause:** Nested heredoc inside `sudo -u pi bash -c "..."` was parsed by the outer (root) shell:
```bash
# Bug: The <<EOT is parsed by root shell, not the inner bash
sudo -H -u pi bash -c "cat >> '$ssh_config' <<EOT
Host github-hamma
...
EOT"
```

**Fix:** Use `tee` instead of nested heredoc:
```bash
sudo -H -u pi tee -a "$ssh_config" > /dev/null <<EOT
Host github-hamma
...
EOT
```

**File Changed:** `lib/software.sh`

---

### 3. Bash Arithmetic with `set -e`

**Problem:** Scripts using `set -e` (exit on error) would exit unexpectedly on the first success.

**Root Cause:** `((COUNT++))` returns exit code 1 when COUNT is 0, because the expression evaluates to 0 (falsy):
```bash
COUNT=0
((COUNT++))  # Returns exit code 1! Script exits with set -e
```

**Fix:** Add `|| true` to arithmetic operations:
```bash
((COUNT++)) || true
```

**File Changed:** `scripts/verify_deployment.sh`

---

### 4. Editable Pip Installs as Root

**Problem:** `pip install -e .` (editable installs) created broken `.pth` files when run as root, even with `sudo -u pi`.

**Root Cause:** Editable installs write to the source directory. When run as root, permissions get mixed up.

**Fix:** Use non-editable installs:
```bash
# Wrong
sudo -H -u pi pip install -e /home/pi/dev/brokkr

# Correct
sudo -H -u pi pip install /home/pi/dev/brokkr
```

**Files Changed:** `lib/brokkr.sh`, `lib/software.sh`

---

### 5. XDG_CONFIG_HOME Workaround

**Problem:** Even with `sudo -H -u pi`, brokkr's `configure-system` wrote to `/root/.config/brokkr/`.

**Workaround:** Explicitly set XDG_CONFIG_HOME:
```bash
sudo -H -u pi XDG_CONFIG_HOME=/home/pi/.config brokkr configure-system ...
```

**Investigation Needed:** This shouldn't be necessary with `-H` flag. Possible causes:
- Brokkr bug
- SUDO_USER environment variable interference
- Something else in the environment

**File Changed:** `lib/brokkr.sh`

---

### 6. DNS Resolution (IPv6 Preference)

**Problem:** DNS resolution failed or was very slow after WiFi setup.

**Root Cause:** Used `stub-resolv.conf` which points to `127.0.0.53`. This can prefer IPv6 even when broken.

**Fix:** Use `/run/systemd/resolve/resolv.conf` which contains actual upstream DNS servers:
```bash
ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
```

**File Changed:** `bootstrap.sh`

---

### 7. FILES_DIR Default Path

**Problem:** Scripts couldn't find certificate files and other resources.

**Root Cause:** Default `FILES_DIR` pointed to `unified_install/files/` which was empty. The actual files are in the repo's `files/` directory.

**Fix:** Changed default to `../../files` (relative to lib/ scripts) or use absolute path:
```bash
FILES_DIR="${FILES_DIR:-$(dirname "$SCRIPT_DIR")/files}"
```

**Files Changed:** `lib/network_wifi.sh`, `lib/network_wwan.sh`, `lib/hardware.sh`

---

### 8. macOS AppleDouble Files

**Problem:** Copying repo from USB (on macOS) caused unicode errors in brokkr.

**Root Cause:** macOS creates `._*` (AppleDouble) files for extended attributes. These contain binary data that confuses Python.

**Fix:** Delete AppleDouble files after copying:
```bash
find /home/pi/dev/mjolnir-hamma -name '._*' -delete
```

**File Changed:** `bootstrap.sh`

---

### 9. Debian Buster EOL

**Problem:** `apt-get update` failed with 404 errors.

**Root Cause:** Debian Buster reached end-of-life. Packages moved from `deb.debian.org` to `archive.debian.org`.

**Fix:** Update sources.list:
```bash
sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list
sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list
sed -i '/buster-updates/d' /etc/apt/sources.list
```

**Files Changed:** `bootstrap.sh`, `tests/integration/Dockerfile.systemd`

---

### 10. Password Change Changed Root

**Problem:** `passwd` command in bootstrap changed root's password, not pi's.

**Root Cause:** `passwd` without a username changes the current user (root when running with sudo).

**Fix:** Explicitly specify username:
```bash
passwd pi
```

**File Changed:** `bootstrap.sh`

---

### 11. SSH Config Owned by Root (hardware.sh)

**Problem:** After install, `/home/pi/.ssh/config` was owned by root:root, causing later operations (like adding github-hamma entry) to fail with permission denied.

**Root Cause:** `setup_sensor_connection()` in `lib/hardware.sh` created the .ssh directory and copied the config file as root, not as the pi user:
```bash
# Bug: runs as root
mkdir -p "$ssh_dir"
cp "$FILES_DIR/config" "$ssh_config"
```

**Fix:** Use `sudo -H -u pi` for all operations on pi's files:
```bash
# Correct: runs as pi user
sudo -H -u pi mkdir -p "$ssh_dir"
sudo -H -u pi cp "$FILES_DIR/config" "$ssh_config"
```

**File Changed:** `lib/hardware.sh`

---

## Testing Infrastructure

### Four Testing Layers

| Layer | Tool | What it Tests |
|-------|------|---------------|
| 1-Syntax | `bash -n`, shellcheck | Scripts parse without error |
| 2-Mock | pytest + `--dry-run` | Logic flow, manifest output |
| 3-Integration | Docker with systemd | Files created, ownership, services installed |
| 4-Functional | Real Pi + `verify_deployment.sh` | Services run, network works, tunnel connects |

### Key Test Files

- `tests/shell/test_shellcheck.py` - Syntax validation (19 scripts)
- `tests/unified/test_*.py` - Mock/dry-run tests (117 tests)
- `tests/integration/test_integration.py` - Docker integration (35 tests)
- `scripts/verify_deployment.sh` - Production verification (20 checks)

### Docker Testing

```bash
# Run full test (install + verify)
cd tests/integration
./test-with-systemd.sh --full-test --cellular

# Just run verification
./test-with-systemd.sh --verify
```

---

## Gotchas and Tips

### sudo -H vs sudo -u

Always use `-H` when you need the target user's home directory:
```bash
sudo -H -u pi ...  # HOME=/home/pi
sudo -u pi ...     # HOME=/root (wrong!)
```

### Heredocs in sudo bash -c

Don't put heredocs inside `bash -c` strings. Use `tee` instead:
```bash
# Wrong
sudo -u pi bash -c "cat >> file <<EOF
content
EOF"

# Correct
sudo -u pi tee -a file > /dev/null <<EOF
content
EOF
```

### Testing SSH Keys

Always check ownership after generating:
```bash
ls -la /home/pi/.ssh/
# Should show: pi pi, not root root
```

### Checking for Root Artifacts

After install, verify no root-owned configs:
```bash
ls -la /root/.config/  # Should NOT have brokkr/
ls -la /home/pi/.config/brokkr/  # Should be owned by pi
```

### WWAN Timer

The wwan-check timer runs every 5 minutes. To force immediate check:
```bash
sudo systemctl start wwan-check.service
```

---

## Version History

| Date | Changes |
|------|---------|
| 2026-01-13 | Initial unified scripts, all critical bugs fixed, tested on mjolnir06 |
| 2026-01-11 | Debug session on sensor 6 - found sudo/ownership issues |
| 2026-01-03 | First attempt at unification - failed due to insufficient testing |
