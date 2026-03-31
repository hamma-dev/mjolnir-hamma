# mjolnir-hamma Development Reference

Repo-specific knowledge for developing in mjolnir-hamma. For operational sensor knowledge (deployment, troubleshooting, services), see `~/.claude/hamma-system.md`.

---

## Unified Install Architecture

### Two-Phase Design

**CRITICAL:** `bootstrap.sh` runs BEFORE network is available.

| Phase | Script | Network | Runs From |
|-------|--------|---------|-----------|
| 1 | `bootstrap.sh` | NO network | USB drive |
| 2 | `install.sh` | Network UP | Local repo |

**bootstrap.sh** (Phase 1, no network):
- Changes pi user password (prompts interactively)
- Sets timezone to UTC
- Fixes Buster EOL repos (archive.debian.org)
- Fixes clock if year < 2024 (uses file timestamp)
- Configures temp WiFi (not connected yet)
- Copies repo from USB to `/home/pi/dev/mjolnir-hamma`
- Removes macOS AppleDouble files (`._*`)
- Disables internal WiFi radio
- Sets hostname to `mjolnirNN`
- **Requires reboot after**

**install.sh** (Phase 2, network available):
- Configures production network (WiFi or cellular)
- Generates SSH key for server access (`id_rsa`)
- Installs system packages
- Clones/installs brokkr, sindri, pyltg, hamma
- Configures hardware (relay, automount)
- Enables systemd services

### Directory Structure

```
unified_install/
├── bootstrap.sh          # Phase 1: USB setup
├── install.sh            # Phase 2: Main installation
├── lib/
│   ├── common.sh         # Shared functions, logging, manifest
│   ├── brokkr.sh         # Brokkr installation and configuration
│   ├── hardware.sh       # Relay, automount setup
│   ├── network_wifi.sh   # WiFi/UAH network setup
│   ├── network_wwan.sh   # Cellular/WWAN network setup
│   └── software.sh       # System packages, sindri, pyltg, hamma
└── README.md             # User installation guide
```

### Clone Sources

| Repo | Source | Branch | Notes |
|------|--------|--------|-------|
| mjolnir-hamma | hamma-dev | `0.3.x` | Copied from USB in bootstrap |
| brokkr | hamma-dev | `0.4.x` | |
| serviceinstaller | hamma-dev | default | |
| sindri | hamma-dev | `0.3.x` | |
| notifiers | pbitzer | default | Personal fork |
| pyltg | pbitzer | default | Personal code |
| hamma | pbitzer | `0.3.x` | Private repo (uses ed25519 deploy key) |

---

## Testing Infrastructure

### Four Testing Layers

| Layer | Tool | What it Tests | Location |
|-------|------|---------------|----------|
| 1-Syntax | `bash -n`, shellcheck | Scripts parse without error | `tests/shell/test_shellcheck.py` |
| 2-Mock | pytest + `--dry-run` | Logic flow, manifest output | `tests/unified/test_*.py` |
| 3-Integration | Docker with systemd | Files created, ownership, services | `tests/integration/test_integration.py` |
| 4-Functional | Real Pi + verify script | Services run, network, tunnel | `scripts/verify_deployment.sh` |

### Running Tests

```bash
# Layer 1-2: Syntax and mock tests
pytest tests/shell tests/unified -v

# Layer 3: Integration tests (requires Docker)
cd tests/integration
./test-with-systemd.sh --full-test --cellular

# Layer 4: Production verification (on real Pi)
cd scripts
./verify_deployment.sh --wifi  # or --cellular
```

### Key Test Files

- `tests/shell/test_shellcheck.py` — Syntax validation (19 scripts)
- `tests/unified/test_script_execution.py` — Dry-run execution, manifest validation (35 tests)
- `tests/unified/test_behavior_comparison.py` — Comparison with original scripts (25 tests)
- `tests/unified/test_cellular_path.py` — Cellular path specifics (24 tests)
- `tests/unified/test_wifi_path.py` — WiFi path specifics (19 tests)
- `tests/unified/test_failure_scenarios.py` — Failure scenarios (24 tests)
- `tests/integration/test_integration.py` — Docker integration (35 tests)
- `scripts/verify_deployment.sh` — Production verification (20 checks)

**Total pytest tests: ~152** (117 Layer 2 + 35 Layer 3)

### Test Coverage by Component

| Component | L1 | L2 | L3 | L4 |
|-----------|----|----|----|----|
| bootstrap.sh | pass | 7 tests | N/A | N/A |
| install.sh | pass | 12 tests | 35 tests | verify script |
| network_wifi.sh | pass | 19 tests | 8 tests | verify script |
| network_wwan.sh | pass | 24 tests | 9 tests | verify script |
| brokkr.sh | pass | 12 tests | 6 tests | verify script |
| hardware.sh | pass | 4 tests | 4 tests | verify script |

### Docker Testing Notes

The integration tests use `jrei/systemd-debian:10` (Debian Buster with systemd). Buster is EOL, so the Dockerfile applies archive.debian.org fixes:
```dockerfile
RUN sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list && \
    sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list
```

### Untracked Test Helpers (in tests/integration/)

These scripts are not committed but useful for manual debugging:

**run-install-test.sh** — Automated install test with logging
```bash
./run-install-test.sh --cellular           # Test cellular install
./run-install-test.sh --wifi --dry-run     # Dry-run WiFi test
./run-install-test.sh --rebuild            # Force rebuild image
```

**test-install-interactive.sh** — Interactive debugging environment
```bash
./test-install-interactive.sh --rebuild    # Rebuild and enter shell
# Inside container:
cd /home/pi/dev/mjolnir-hamma/unified_install
sudo bash install.sh 99 --cellular --dry-run
```

---

## Development Gotchas

### 1. `sudo -H` is mandatory with `sudo -u pi`

`sudo -u pi` does NOT change `$HOME` — it stays `/root`. All user files end up owned by root.

```bash
# WRONG
sudo -u pi git clone ...      # HOME stays /root!

# CORRECT
sudo -H -u pi git clone ...   # HOME=/home/pi
```

This was the single biggest source of bugs (26+ occurrences fixed across lib/*.sh files).

### 2. No heredocs inside `bash -c` strings

The heredoc is parsed by the OUTER (root) shell, not the inner one.

```bash
# WRONG — heredoc parsed by root shell, permission denied
sudo -u pi bash -c "cat >> '$file' <<EOF
content
EOF"

# CORRECT — tee runs as root, writes to file
sudo -H -u pi tee -a "$file" > /dev/null <<EOF
content
EOF
```

### 3. Bash arithmetic + `set -e` = surprise exit

`((COUNT++))` returns exit code 1 when COUNT is 0 (expression evaluates to 0 = falsy).

```bash
# WRONG — exits script when COUNT was 0
((COUNT++))

# CORRECT
((COUNT++)) || true
```

### 4. No editable pip installs with sudo

Editable installs (`-e`) write to the source directory. When run as root, permissions get mixed up.

```bash
# WRONG
sudo -H -u pi pip install -e /home/pi/dev/brokkr

# CORRECT
sudo -H -u pi pip install /home/pi/dev/brokkr
```

### 5. `passwd` without username changes current user

When running as root (via sudo), bare `passwd` changes root's password.

```bash
# WRONG — changes root's password
passwd

# CORRECT
passwd pi
```

### 6. XDG_CONFIG_HOME workaround for brokkr

Even with `sudo -H -u pi`, brokkr's `configure-system` may write to `/root/.config/brokkr/`. Workaround: explicitly set XDG_CONFIG_HOME:

```bash
sudo -H -u pi XDG_CONFIG_HOME=/home/pi/.config brokkr configure-system ...
```

Root cause not fully investigated (possible brokkr bug or SUDO_USER interference).

### 7. FILES_DIR must be set correctly

`FILES_DIR` defaults to `unified_install/files/` which is intentionally empty. Scripts need it set to the repo's `files/` directory:

```bash
FILES_DIR=/home/pi/dev/mjolnir-hamma/files \
SCRIPTS_DIR=/home/pi/dev/mjolnir-hamma/scripts \
sudo bash install.sh 1 --cellular
```

---

## Script Reference (lib/*.sh)

| Script | Role |
|--------|------|
| `common.sh` | Shared functions: logging (`log_info`, `log_warn`, `log_error`), manifest output, dry-run support |
| `brokkr.sh` | Creates ltgenv virtualenv, clones/installs brokkr, runs `configure-system`, installs systemd service |
| `hardware.sh` | Sets up sensor SSH connection (eth0 at 10.10.10.2), USB automount rules, relay control |
| `network_wifi.sh` | UAH/NSSTC WiFi: copies certificate, wpa_supplicant config, generates SSH key |
| `network_wwan.sh` | Cellular: installs modemmanager/udhcpc, copies wwan-check timer/script, configures APN, generates SSH key |
| `software.sh` | Installs system packages (apt-get), sindri, pyltg, hamma (private repo via deploy key) |

### Key Enhancement Over Original Scripts

The unified scripts generate SSH keys (`id_rsa`) in BOTH wifi and cellular paths. The original `setup_wwan.sh` did NOT generate SSH keys (assumed WiFi ran first).

---

## Historical Fixed Issues

Numbered list for reference (all fixed in current code):

1. **Files owned by root** — `sudo -u pi` without `-H`; fixed with `sudo -H -u pi`
2. **SSH config permission denied** — heredoc in `bash -c`; fixed with `tee`
3. **Bash arithmetic with `set -e`** — `((COUNT++))` returns 1 when COUNT=0; fixed with `|| true`
4. **Editable pip installs** — `-e` installs as root cause permission issues; use non-editable
5. **XDG_CONFIG_HOME workaround** — brokkr writes to /root/.config; explicitly set XDG_CONFIG_HOME
6. **DNS resolution (IPv6)** — `stub-resolv.conf` prefers broken IPv6; use `resolv.conf`
7. **FILES_DIR default** — pointed to empty directory; changed to `../../files`
8. **macOS AppleDouble files** — `._*` files cause unicode errors; delete after copying
9. **Debian Buster EOL** — apt repos return 404; use archive.debian.org
10. **Password change changed root** — `passwd` without username; specify `passwd pi`
11. **SSH config owned by root (hardware.sh)** — `setup_sensor_connection()` ran as root; fixed with `sudo -H -u pi`

---

## Post-Install Manual Steps

After `install.sh` completes, these steps must be run manually:

- **datasync user**: Run `scripts/setup_datasync.sh --key /path/to/id_rsa.pub <sensor_numbers>` to create the `datasync` user for remote data download via `hamma_download.py`. This is not part of the automated install.

---

## Future Improvements

- **Auto-mount USB in bootstrap**: Currently user must manually mount USB
- **Add Dockerfile.systemd to git**: Currently not tracked
- **Fix DNS bug on cellular sensors**: See `~/.claude/hamma-system.md` under "DNS Resolution Failing on Cellular Sensors"
