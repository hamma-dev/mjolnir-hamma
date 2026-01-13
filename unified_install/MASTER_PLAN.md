# HAMMA Pi Unified Install - Master Plan

**Created:** 2026-01-13
**Purpose:** Consolidate knowledge from multiple development sessions and define a testing strategy for the unified install scripts.

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Architecture Overview](#architecture-overview)
3. [Sources of Truth](#sources-of-truth)
4. [Current Script Status](#current-script-status)
5. [Bugs Found and Fixes Applied](#bugs-found-and-fixes-applied)
6. [Known Remaining Issues](#known-remaining-issues)
7. [Testing Strategy](#testing-strategy)
8. [Test Coverage Matrix](#test-coverage-matrix)
9. [Functional Test Requirements](#functional-test-requirements)
10. [Next Steps](#next-steps)

---

## Executive Summary

### Goal
Simplify the HAMMA Raspberry Pi installation process by consolidating multiple individual scripts into a unified, testable installation system.

### Current State
- **Unified scripts created:** `bootstrap.sh` + `install.sh` with library modules
- **Layer 1 (Syntax):** ✅ All 19 scripts pass `bash -n` validation
- **Layer 2 (Mock):** ✅ 117 pytest tests pass (dry-run execution, manifest validation)
- **Layer 3 (Integration):** ✅ 35 tests pass (Docker with systemd, file ownership verified)
- **Layer 4 (Functional):** ✅ `verify_deployment.sh` created and tested on mjolnir06

### Critical Gap
> "I don't just want install without error, we need to know if it works"

The testing infrastructure validates syntax and structure but does NOT verify:
- Services actually start and run
- SSH tunnels connect to server
- Brokkr collects data
- Cellular modem maintains connection
- Automount works when drives plugged in

---

## Architecture Overview

### Two-Phase Installation

```
┌─────────────────────────────────────────────────────────────────┐
│                     PHASE 1: BOOTSTRAP                          │
│                   (runs from USB, NO network)                   │
├─────────────────────────────────────────────────────────────────┤
│  • Password change                                              │
│  • Timezone → UTC                                               │
│  • Temp WiFi config (for install phase)                         │
│  • Copy repo from USB → /home/pi/dev/mjolnir-hamma              │
│  • Disable internal WiFi radio                                  │
│  • Set hostname → mjolnirNN                                     │
│  • Buster EOL repo fix (archive.debian.org)                     │
│  • Clock fix (if year < 2024, use file timestamp)               │
│  • REBOOT REQUIRED                                              │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                     PHASE 2: INSTALL                            │
│              (runs after reboot, network available)             │
├─────────────────────────────────────────────────────────────────┤
│  1. Network setup (--wifi OR --cellular)                        │
│     • WiFi: wpa_supplicant + certificate                        │
│     • Cellular: timer-based wwan-check approach                 │
│     • SSH key generation (id_rsa for server)                    │
│  2. Update repo from GitHub (now that network is up)            │
│  3. System packages (apt-get install)                           │
│  4. Brokkr installation and configuration                       │
│  5. Hardware setup (sensor connect, automount)                  │
│  6. Extras (sindri, pyltg, hamma - optional)                    │
└─────────────────────────────────────────────────────────────────┘
```

### File Structure

```
unified_install/
├── bootstrap.sh          # Phase 1 entry point
├── install.sh            # Phase 2 entry point
├── lib/
│   ├── common.sh         # Shared functions, logging, manifest output
│   ├── network_wifi.sh   # UAH/NSSTC WiFi setup
│   ├── network_wwan.sh   # Cellular modem setup (timer-based)
│   ├── brokkr.sh         # Brokkr installation and configuration
│   ├── hardware.sh       # Sensor connection, USB automount
│   └── software.sh       # System packages, sindri, pyltg, hamma
├── README.md             # User documentation
└── MASTER_PLAN.md        # This file
```

### Clone Sources

| Repo | GitHub Org | Branch | Notes |
|------|------------|--------|-------|
| mjolnir-hamma | hamma-dev | 0.3.x | Copied from USB in bootstrap, updated in install |
| brokkr | hamma-dev | 0.4.x | |
| serviceinstaller | hamma-dev | default | |
| sindri | hamma-dev | 0.3.x | |
| notifiers | pbitzer | default | Personal fork |
| pyltg | pbitzer | default | Personal code |
| hamma | pbitzer | 0.3.x | Private repo, requires deploy key |

---

## Sources of Truth

### Primary Documentation

| Source | Location | Status |
|--------|----------|--------|
| Confluence "Pi Setup [Working]" | [Page 126681092](https://hsvltg.atlassian.net/wiki/spaces/HAMMA/pages/126681092) | Original manual process |
| Confluence "Cellular Fixes" | [Page 361332739](https://hsvltg.atlassian.net/wiki/spaces/HAMMA/pages/361332739) | Timer-based cellular approach |
| Confluence "MjolnirPi Setup" | [Page 428802050](https://hsvltg.atlassian.net/wiki/spaces/HAMMA/pages/428802050) | Unified process (Dec 2024) |
| unified_install/README.md | This repo | Current unified instructions |

### Original Scripts (install_scripts/)

The unified scripts were created by consolidating these original scripts:

| Original Script | Unified Equivalent | Status |
|-----------------|-------------------|--------|
| `update_host.sh` | `bootstrap.sh` | ✅ Replaced |
| `disable_wifi_radio.sh` | `bootstrap.sh` | ✅ Replaced |
| `setup_uah_wireless.sh` | `lib/network_wifi.sh` | ✅ Replaced |
| `setup_wwan.sh` | `lib/network_wwan.sh` | ✅ Replaced + enhanced |
| `install_packages.sh` | `lib/software.sh` | ✅ Replaced |
| `install_brokkr.sh` + `setup_brokkr.sh` | `lib/brokkr.sh` | ✅ Replaced |
| `setup_sensor_connect.sh` + `enable_automount.sh` | `lib/hardware.sh` | ✅ Replaced |
| `install_sindri.sh` | `lib/software.sh` | ✅ Replaced |
| `install_pyltg.sh` | `lib/software.sh` | ✅ Replaced |
| `install_hamma.sh` | `lib/software.sh` | ✅ Replaced |

**Key Enhancement:** Unified scripts generate SSH keys (id_rsa) in BOTH wifi and cellular paths. Original `setup_wwan.sh` did NOT generate SSH keys (assumed WiFi ran first).

---

## Current Script Status

### Test Results by Script

| Script | Syntax (L1) | Mock/Dry-run (L2) | Integration (L3) | Functional (L4) |
|--------|-------------|-------------------|------------------|-----------------|
| `bootstrap.sh` | ✅ Pass | ✅ 7 tests | ✅ N/A (phase 1) | ✅ N/A |
| `install.sh` | ✅ Pass | ✅ 12 tests | ✅ 35 tests | ✅ verify script |
| `lib/common.sh` | ✅ Pass | ✅ (implicit) | N/A | N/A |
| `lib/network_wifi.sh` | ✅ Pass | ✅ 19 tests | ✅ 8 tests | ✅ verify script |
| `lib/network_wwan.sh` | ✅ Pass | ✅ 24 tests | ✅ 9 tests | ✅ verify script |
| `lib/brokkr.sh` | ✅ Pass | ✅ 12 tests | ✅ 6 tests | ✅ verify script |
| `lib/hardware.sh` | ✅ Pass | ✅ 4 tests | ✅ 4 tests | ✅ verify script |
| `lib/software.sh` | ✅ Pass | ✅ (implicit) | N/A | N/A |

### Git Status

**Branch:** `feature/unified-install`

**Recent commits:**
- `8b39afd` - Fix verify_deployment.sh to not exit on first failure
- `a40f935` - Add verify_deployment.sh for production Pi verification
- `680c6ef` - Fix Layer 3 integration tests for actual venv location
- `40b02f7` - Add Layer 3 integration tests for Docker with systemd
- `28f2202` - Update MASTER_PLAN.md with accurate test coverage status
- `2b8a64b` - Add unified_install scripts to shellcheck/syntax tests
- `992abc4` - Fix critical permission bugs and add MASTER_PLAN.md

---

## Bugs Found and Fixes Applied

### Critical Fixes (Applied)

| Bug | Root Cause | Fix | Files Changed |
|-----|------------|-----|---------------|
| SSH keys owned by root | `ssh-keygen` ran as root via sudo | Wrap in `sudo -H -u pi bash -c "..."` | network_wifi.sh, network_wwan.sh |
| Brokkr config in /root/.config | `sudo -u pi` doesn't set HOME | Use `sudo -H -u pi` (the -H flag) + set XDG_CONFIG_HOME ⚠️ | brokkr.sh (12 occurrences), software.sh (14 occurrences) |

> **⚠️ INVESTIGATE:** The `XDG_CONFIG_HOME=/home/pi/.config` workaround in brokkr.sh configure-system shouldn't have been necessary. The `-H` flag should set HOME correctly, and brokkr should respect that. Need to investigate why brokkr still looks in /root/.config even with correct HOME. Possible causes: brokkr bug, SUDO_USER interference, or something else in the environment.
| Editable installs broken | `pip install -e` as root creates bad .pth files | Changed to non-editable: `pip install '$path'` | brokkr.sh, software.sh |
| FILES_DIR wrong path | Default pointed to empty unified_install/files/ | Changed default to `../../files` (repo's files dir) | network_wifi.sh, network_wwan.sh, hardware.sh |
| DNS issues (IPv6 preference) | Used `stub-resolv.conf` instead of `resolv.conf` | Changed to `/run/systemd/resolve/resolv.conf` | bootstrap.sh line 276 |
| Password change changed root | `passwd` without username changes current user | Changed to `passwd pi` | bootstrap.sh |
| Bootstrap skipping file copy | Existing directory not refreshed | Delete and recopy: `rm -rf` then `cp -r` | bootstrap.sh lines 370-380 |
| Buster EOL repos | deb.debian.org returns 404 | sed to archive.debian.org | bootstrap.sh, Dockerfile |
| Cartopy version | Python 3.7 incompatible with latest | Version detection: 3.7→0.19.0.post1, 3.8+→0.21.1 | software.sh |
| pyltg missing setuptools | setup.py doesn't import setuptools | Patch to insert `import setuptools` after clone | software.sh |
| macOS AppleDouble files | `._*` files on USB cause unicode errors | `find -name '._*' -delete` after copy | bootstrap.sh |
| Clone branch missing | Repos cloned without branch specification | Added branch parameter to clone_or_update | brokkr.sh, software.sh |

### Known Workarounds Required

| Issue | Workaround |
|-------|------------|
| Scripts modified but not deployed to Pi | Re-run bootstrap or manually copy lib/*.sh files |
| `/root/.config/brokkr` exists from failed run | `sudo rm -rf /root/.config/brokkr` before install |
| Need to set env vars when running | `FILES_DIR=../files SCRIPTS_DIR=../scripts sudo bash install.sh ...` |

---

## Known Remaining Issues

### High Priority

| Issue | Impact | Status |
|-------|--------|--------|
| bootstrap.sh not tested in Docker | Could have syntax errors we don't catch | Needs test |
| Services not verified to start | "Install without error" ≠ "works" | Needs functional test |
| No end-to-end test | Can't verify full workflow works | Needs test design |
| Confluence still references install_scripts/ | Users may use wrong scripts | Needs doc update |

### Medium Priority

| Issue | Impact | Status |
|-------|--------|--------|
| Two script locations (install_scripts/ vs unified_install/) | Confusion about which to use | Needs cleanup/decision |
| No test for password change | Bug escaped testing | Needs test |
| autossh not verified | Tunnel might not connect | Needs functional test |

### Low Priority / Future

| Issue | Impact | Status |
|-------|--------|--------|
| sindri testing incomplete | May have integration issues | Deferred |
| Base image modifications | Could pre-install more packages | Future goal |
| Auto-mount USB in bootstrap | Currently manual mount | Enhancement |

---

## Testing Strategy

### Testing Layers

```
┌────────────────────────────────────────────────────────────────┐
│                    LAYER 1: SYNTAX/STRUCTURE                   │
│                         (Docker-based)                         │
├────────────────────────────────────────────────────────────────┤
│  • Shell scripts parse without error                           │
│  • Python syntax valid                                         │
│  • Required files exist                                        │
│  • Functions defined before use                                │
│  Tool: Docker + shellcheck + python -m py_compile              │
└────────────────────────────────────────────────────────────────┘
                              ↓
┌────────────────────────────────────────────────────────────────┐
│                    LAYER 2: MOCK EXECUTION                     │
│                    (Docker with dry-run)                       │
├────────────────────────────────────────────────────────────────┤
│  • Scripts run through full flow in dry-run mode               │
│  • All code paths exercised                                    │
│  • Manifest output generated and validated                     │
│  • No actual system changes made                               │
│  Tool: Docker + --dry-run flag                                 │
└────────────────────────────────────────────────────────────────┘
                              ↓
┌────────────────────────────────────────────────────────────────┐
│                    LAYER 3: INTEGRATION                        │
│                  (Docker with systemd)                         │
├────────────────────────────────────────────────────────────────┤
│  • Scripts make actual changes in container                    │
│  • Services installed and enabled                              │
│  • File permissions correct                                    │
│  • Config files generated correctly                            │
│  Tool: Docker (jrei/systemd-debian:10) + actual execution      │
└────────────────────────────────────────────────────────────────┘
                              ↓
┌────────────────────────────────────────────────────────────────┐
│                    LAYER 4: FUNCTIONAL                         │
│                      (Real Pi hardware)                        │
├────────────────────────────────────────────────────────────────┤
│  • Services START and STAY running                             │
│  • Network connectivity works                                  │
│  • SSH tunnel connects to server                               │
│  • Brokkr collects data                                        │
│  • Hardware (relay, sensor, drives) works                      │
│  Tool: Real Raspberry Pi + verification scripts                │
└────────────────────────────────────────────────────────────────┘
```

### Docker Test Infrastructure

**Current files:**
- `tests/integration/Dockerfile` - Basic Debian Buster image
- `tests/integration/Dockerfile.systemd` - Image with systemd support
- `tests/integration/test-with-systemd.sh` - Runner script

**Buster EOL fix in Dockerfile:**
```dockerfile
RUN sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list && \
    sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list && \
    sed -i '/buster-updates/d' /etc/apt/sources.list
```

### Test Environments

| Environment | Use Case | Limitations |
|-------------|----------|-------------|
| Docker (basic) | Syntax checking, pytest | No systemd, no network config |
| Docker (systemd) | Service installation | No real hardware, limited network |
| Real Pi (dry-run) | Full path validation | No actual changes, can't verify services work |
| Real Pi (live) | Full functional test | Requires physical hardware, time-consuming |

---

## Test Coverage Matrix

### Summary by Layer

| Layer | Description | Test File(s) | Status |
|-------|-------------|--------------|--------|
| 1-Syntax | bash -n, shellcheck, shebang | `tests/shell/test_shellcheck.py` | ✅ 19/19 scripts pass |
| 2-Mock | --dry-run execution, manifest validation | `tests/unified/test_script_execution.py` | ✅ 35 tests pass |
| 2-Mock | Behavior comparison with originals | `tests/unified/test_behavior_comparison.py` | ✅ 25 tests pass |
| 2-Mock | Cellular path specifics | `tests/unified/test_cellular_path.py` | ✅ 24 tests pass |
| 2-Mock | WiFi path specifics | `tests/unified/test_wifi_path.py` | ✅ 19 tests pass |
| 2-Mock | Failure scenarios | `tests/unified/test_failure_scenarios.py` | ✅ 24 tests pass |
| 3-Integration | Docker with systemd | `tests/integration/test_integration.py` | ✅ 35 pass, 5 skip (WiFi-only) |
| 4-Functional | Real Pi hardware | `scripts/verify_deployment.sh` | ✅ Tested on mjolnir06 |

**Total pytest tests: 152 passing** (117 Layer 2 + 35 Layer 3)

### Bootstrap.sh Tests

| Test | Layer | Status | Test Location |
|------|-------|--------|---------------|
| Script parses (`bash -n`) | 1-Syntax | ✅ Done | `test_shellcheck.py::test_bash_syntax_valid` |
| Dry-run completes | 2-Mock | ✅ Done | `test_script_execution.py::TestBootstrapExecution` |
| Manifest has timezone | 2-Mock | ✅ Done | `test_bootstrap_manifest_has_timezone` |
| Manifest has temp WiFi | 2-Mock | ✅ Done | `test_bootstrap_manifest_has_temp_wifi` |
| Manifest has hostname | 2-Mock | ✅ Done | `test_bootstrap_manifest_has_hostname` |
| Manifest disables WiFi | 2-Mock | ✅ Done | `test_bootstrap_manifest_disables_internal_wifi` |
| Manifest copies repo | 2-Mock | ✅ Done | `test_bootstrap_manifest_copies_repo` |
| Files actually created | 3-Integration | ❌ Missing | Docker with systemd |
| Password prompt works | 4-Functional | ❌ Missing | Manual test on Pi |

### Install.sh Tests (Cellular Path)

| Test | Layer | Status | Test Location |
|------|-------|--------|---------------|
| Script parses | 1-Syntax | ✅ Done | `test_shellcheck.py::test_bash_syntax_valid` |
| Dry-run completes | 2-Mock | ✅ Done | `test_script_execution.py` |
| SSH key in manifest | 2-Mock | ✅ Done | `test_cellular_path_generates_ssh_key` |
| dhcpcd disabled in manifest | 2-Mock | ✅ Done | `test_cellular_path_disables_dhcpcd` |
| Timer in manifest | 2-Mock | ✅ Done | `test_cellular_path_installs_timer` |
| Python script in manifest | 2-Mock | ✅ Done | `test_cellular_path_installs_python_script` |
| APN configurable | 2-Mock | ✅ Done | `test_apn_argument_supported` |
| Files actually created | 3-Integration | ❌ Missing | Docker with systemd |
| Modem connects | 4-Functional | ❌ Missing | Real Pi with modem |
| Internet reachable | 4-Functional | ❌ Missing | Real Pi |

### Install.sh Tests (WiFi Path)

| Test | Layer | Status | Test Location |
|------|-------|--------|---------------|
| Script parses | 1-Syntax | ✅ Done | `test_shellcheck.py::test_bash_syntax_valid` |
| Dry-run completes | 2-Mock | ✅ Done | `test_script_execution.py` |
| SSH key in manifest | 2-Mock | ✅ Done | `test_wifi_path_generates_ssh_key` |
| Certificate in manifest | 2-Mock | ✅ Done | `test_wifi_path_copies_certificate` |
| wpa_supplicant in manifest | 2-Mock | ✅ Done | `test_manifest_contains_wpa_supplicant_config` |
| Services enabled in manifest | 2-Mock | ✅ Done | `test_wifi_path_enables_services` |
| Files actually created | 3-Integration | ❌ Missing | Docker with systemd |
| WiFi connects | 4-Functional | ❌ Missing | Real Pi with WiFi |

### Brokkr Tests

| Test | Layer | Status | Test Location |
|------|-------|--------|---------------|
| Venv creation in manifest | 2-Mock | ✅ Done | `test_behavior_comparison.py` |
| Git clones in manifest | 2-Mock | ✅ Done | `test_behavior_comparison.py` |
| Pip installs in manifest | 2-Mock | ✅ Done | `test_behavior_comparison.py` |
| Configure commands in manifest | 2-Mock | ✅ Done | `test_behavior_comparison.py` |
| Venv actually created | 3-Integration | ❌ Missing | Docker with systemd |
| Venv owned by pi | 3-Integration | ❌ Missing | Docker with systemd |
| Config owned by pi | 3-Integration | ❌ Missing | Docker with systemd |
| Service starts | 4-Functional | ❌ Missing | Real Pi |
| Data collected | 4-Functional | ❌ Missing | Real Pi with sensor |

### Hardware Tests

| Test | Layer | Status | Test Location |
|------|-------|--------|---------------|
| SSH config in manifest | 2-Mock | ✅ Done | `test_behavior_comparison.py` |
| Network files in manifest | 2-Mock | ✅ Done | `test_behavior_comparison.py` |
| Polkit rules in manifest | 2-Mock | ✅ Done | `test_behavior_comparison.py` |
| Files actually created | 3-Integration | ❌ Missing | Docker with systemd |
| Sensor SSH works | 4-Functional | ❌ Missing | Real Pi with sensor |
| USB automounts | 4-Functional | ❌ Missing | Real Pi with drive |

### Server Connection Tests

| Test | Layer | Status | Test Location |
|------|-------|--------|---------------|
| autossh service in manifest | 2-Mock | ✅ Done | (implicit in brokkr install) |
| Service actually installed | 3-Integration | ❌ Missing | Docker with systemd |
| Tunnel connects | 4-Functional | ❌ Missing | Real Pi + server |
| Reverse tunnel works | 4-Functional | ❌ Missing | Real Pi + server |

---

## Functional Test Requirements

### Minimum Viable Functional Test

A Pi is considered "successfully installed" when ALL of these pass:

```bash
#!/bin/bash
# functional_test.sh - Run on Pi after install

PASS=0
FAIL=0

test_result() {
    if [ $1 -eq 0 ]; then
        echo "✅ PASS: $2"
        ((PASS++))
    else
        echo "❌ FAIL: $2"
        ((FAIL++))
    fi
}

# 1. Basic connectivity
ping -c 1 8.8.8.8 > /dev/null 2>&1
test_result $? "Internet connectivity (ping 8.8.8.8)"

# 2. DNS resolution
nslookup google.com > /dev/null 2>&1
test_result $? "DNS resolution (nslookup google.com)"

# 3. SSH key exists and owned by pi
[ -f /home/pi/.ssh/id_rsa ] && [ "$(stat -c %U /home/pi/.ssh/id_rsa)" = "pi" ]
test_result $? "SSH key exists and owned by pi"

# 4. Brokkr venv exists and owned by pi
[ -d /home/pi/ltgenv ] && [ "$(stat -c %U /home/pi/ltgenv)" = "pi" ]
test_result $? "Brokkr venv exists and owned by pi"

# 5. Brokkr config exists and owned by pi
[ -d /home/pi/.config/brokkr ] && [ "$(stat -c %U /home/pi/.config/brokkr)" = "pi" ]
test_result $? "Brokkr config exists and owned by pi"

# 6. Brokkr service running
systemctl is-active --quiet brokkr-hamma-default.service
test_result $? "Brokkr service running"

# 7. autossh service running
systemctl is-active --quiet autossh-hamma-default.service
test_result $? "autossh service running"

# 8. Server SSH works (requires key to be added to server)
ssh -o BatchMode=yes -o ConnectTimeout=5 www.hamma.dev exit 2>/dev/null
test_result $? "SSH to server works"

# 9. Cellular: wwan-check timer active (skip if WiFi)
if [ -f /etc/systemd/system/wwan-check.timer ]; then
    systemctl is-active --quiet wwan-check.timer
    test_result $? "wwan-check timer active (cellular)"
fi

# Summary
echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed"
echo "========================================"

[ $FAIL -eq 0 ] && exit 0 || exit 1
```

### Extended Functional Tests (Hardware Required)

| Test | Requirements | Command |
|------|--------------|---------|
| Sensor connection | Powered sensor connected | `ssh hamma echo "connected"` |
| USB automount | USB drive with DATA label | Plug drive, check `/media/pi/DATA*` |
| Relay control | Relay board connected | `brokkr status` shows relay state |
| Data logging | Brokkr running + sensor | Check `/home/pi/data/` for new files |

---

## Next Steps

### Completed ✅

1. ~~**Commit modified files**~~ - Done (commits 992abc4, 2b8a64b)
2. ~~**Layer 1 tests:** Add `bash -n` parsing tests for all scripts~~ - Done (19 scripts)
3. ~~**Layer 2 tests:** Ensure `--dry-run` coverage for all paths~~ - Done (117 tests)
4. ~~**Test bootstrap.sh in Docker**~~ - Done (via test_script_execution.py)
5. ~~**Layer 3: Docker integration tests**~~ - Done (35 tests in test_integration.py)
6. ~~**Verify file ownership**~~ - Done (tests verify pi owns venv, config, SSH keys)
7. ~~**Verify service installation**~~ - Done (tests verify systemd unit files)
8. ~~**Layer 4: Create verify_deployment.sh**~~ - Done (tested on mjolnir06: 18 pass, 1 fail)

### Immediate

1. **Install HAMMA on mjolnir06** - The only failure in verify script is brokkr service not running (HAMMA not installed)
2. **Add shellcheck to CI** - Catch lint issues automatically (currently skipped locally)
3. **Run verify script after HAMMA install** - Should get 0 failures

### Short Term

1. **Test full install on fresh Pi** - Start from clean image, run bootstrap → install → verify
2. **Document test Pi setup** - How to prepare a Pi for testing

### Medium Term (Documentation)

1. **Update Confluence** - Reference unified_install, not install_scripts
2. **Deprecate install_scripts** - Clear guidance on which scripts to use
3. **Add troubleshooting section** - Common failures and fixes

### Long Term (Base Image)

1. **Pre-install cellular packages** - modemmanager, udhcpc, libqmi-utils
2. **Pre-install Python packages** - Reduce install time
3. **Pre-configure systemd-resolved** - Avoid DNS issues
4. **Document image creation** - Reproducible base image

### Investigate

1. **XDG_CONFIG_HOME workaround** - Why is this needed with `sudo -H -u pi`? (see Bugs section)

### Documentation TODO

1. **Add wwan-check troubleshooting** - Document how to force wwan-check to run outside the 5-minute timer interval (e.g., `sudo systemctl start wwan-check.service` or `sudo /usr/local/bin/wwan-check.sh`)

---

## Reference: Test Pi Information

| Property | Value |
|----------|-------|
| Hostname | mjolnir06 |
| Access | `ssh mjolnir06` (via tunnel on port 10006) |
| OS | Debian Buster (10) |
| Current state | WiFi + cellular, near-complete install |
| Missing | HAMMA package not installed |

### Verify Deployment Results (mjolnir06, 2026-01-13)

```
Passed:  18
Failed:  1   (brokkr service not running - HAMMA not installed)
Warnings: 2  (modem disabled, brokkr shows errors without hardware)
Skipped: 1  (server connection - use --full flag)
```

### Commands for Testing

```bash
# SSH to test Pi (via tunnel)
ssh mjolnir06

# Copy verify script to Pi
scp scripts/verify_deployment.sh mjolnir06:/tmp/

# Run verification (basic)
ssh mjolnir06 "/tmp/verify_deployment.sh"

# Run verification (with server connection test)
ssh mjolnir06 "/tmp/verify_deployment.sh --full"

# Docker test (from local machine)
cd tests/integration
./test-with-systemd.sh --full-test --cellular
```

---

## Appendix: Session History

| Date | Focus | Key Outcomes |
|------|-------|--------------|
| 2026-01-03 | Initial unification | POSTMORTEM: Failed due to insufficient testing |
| 2026-01-11 | Debug on sensor 6 | Fixed sudo -H, editable installs, SSH key ownership |
| 2026-01-13 (session 1) | SSH troubleshooting, repo consolidation | Feature branches created, clone URLs updated |
| 2026-01-13 (session 2) | Docker testing, dry-run on Pi | Scripts mostly working, testing gaps identified |
| 2026-01-13 (session 3) | Layer 3 + 4 testing | Created test_integration.py (35 tests), verify_deployment.sh tested on mjolnir06 |
