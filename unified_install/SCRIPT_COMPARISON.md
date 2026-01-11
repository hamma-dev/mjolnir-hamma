# Script Comparison: Original vs Unified

This document maps every operation in the unified install scripts back to the original scripts. Operations not found in the original scripts are flagged as potential issues.

## Key Finding Summary

| Issue | Location | Original | Unified | Status |
|-------|----------|----------|---------|--------|
| resolv.conf symlink | bootstrap.sh:276 | `resolv.conf` | `stub-resolv.conf` | **BUG** |
| bootstrap.sh existence | N/A | Did NOT exist | Created | Expected (new feature) |
| install.sh existence | N/A | Did NOT exist | Created | Expected (new feature) |

## Detailed Comparison

### 1. bootstrap.sh (Unified)

The original repository had **NO bootstrap.sh**. This is a new script I created to handle the pre-network phase.

#### Step 0a: Clock Fix
- **Original**: Not present (Pis were manually configured)
- **Unified**: Sets clock from USB file timestamp if year < 2024
- **Status**: NEW FEATURE (addresses DNSSEC failures)

#### Step 0b: Buster EOL Repos
- **Original**: Not present
- **Unified**: sed deb.debian.org → archive.debian.org
- **Status**: NEW FEATURE (addresses Buster EOL)

#### Step 1: Password Change
- **Original**: Manual step per documentation
- **Unified**: Interactive passwd prompt
- **Status**: NEW FEATURE (convenience)

#### Step 2: Timezone
- **Original**: Manual step per documentation
- **Unified**: `timedatectl set-timezone UTC`
- **Status**: NEW FEATURE (convenience)

#### Step 3: Temp WiFi Setup
- **Original**: Not present (WiFi sites used setup_uah_wireless.sh after copy)
- **Unified**: Creates temp WiFi config for connectivity during install
- **Status**: NEW FEATURE

**⚠️ BUG HERE**: Line 276 uses wrong resolv.conf:
```bash
# CURRENT (BUGGY):
sudo ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf

# SHOULD BE (matching original setup_uah_wireless.sh lines 46-47):
sudo rm -f /etc/resolv.conf
sudo ln -s /run/systemd/resolve/resolv.conf /etc/
```

#### Step 4-7: USB mount, copy repo, disable WiFi, set hostname
- **Original**: Manual steps per documentation
- **Unified**: Scripted versions
- **Status**: NEW FEATURE (convenience)

---

### 2. install.sh (Unified)

The original repository had **NO install.sh**. This is a new driver script I created.

#### Network Setup
- Calls `lib/network_wwan.sh` or `lib/network_wifi.sh`
- These match the original scripts (see below)
- **Status**: CORRECT

---

### 3. network_wwan.sh (Unified lib)

Maps to: `install_scripts/setup_wwan.sh`

| Operation | Original setup_wwan.sh | Unified network_wwan.sh | Match? |
|-----------|------------------------|-------------------------|--------|
| Install packages | apt-get install udhcpc libqmi-utils | apt-get install modemmanager udhcpc libqmi-utils | UPDATED (modemmanager added per timer-based approach) |
| Copy wwan0.network | cp to /etc/systemd/network/ | cp to /etc/systemd/network/ | ✅ |
| Copy wwan-check scripts | N/A (old method) | cp to /usr/local/bin/ | UPDATED (timer-based approach) |
| Configure APN | N/A | sed in Python script | NEW (timer-based approach) |
| Install timer/service | N/A | systemctl enable wwan-check.timer | NEW (timer-based approach) |
| **Touch resolv.conf** | **NO** | **NO** | ✅ |
| Generate SSH key | N/A | ssh-keygen | NEW (moved from separate step) |

**Status**: CORRECT (matches timer-based cellular fixes)

---

### 4. network_wifi.sh (Unified lib)

Maps to: `install_scripts/setup_uah_wireless.sh`

| Operation | Original | Unified | Match? |
|-----------|----------|---------|--------|
| Copy certificate | cp from USB | cp from USB | ✅ |
| Copy wpa_supplicant override | cp override.conf | cp override.conf | ✅ |
| Copy network file | cp 10-wlan0.network | cp 10-wlan0.network | ✅ |
| Update hostname in network file | sed Hostname= | sed Hostname= | ✅ |
| Copy wpa_supplicant config | cp wpa_supplicant-wlan0.conf | cp wpa_supplicant-wlan0.conf | ✅ |
| Update private_key path | sed private_key= | sed private_key= | ✅ |
| **resolv.conf symlink** | `ln -s .../resolv.conf` | `ln -s .../resolv.conf` | ✅ |
| Enable services | systemctl enable | systemctl enable | ✅ |
| Generate SSH key | ssh-keygen | ssh-keygen | ✅ |

**Status**: CORRECT (matches original)

---

## Root Cause Analysis

The DNS/IPv6 issue occurred because:

1. **I created bootstrap.sh** which didn't exist before
2. **In bootstrap.sh**, I used `stub-resolv.conf` for temp WiFi
3. **The original setup_uah_wireless.sh** uses `resolv.conf` (not stub)
4. **The original setup_wwan.sh** doesn't touch resolv.conf at all

For **cellular sites using temp WiFi**:
1. Bootstrap sets up `stub-resolv.conf` (wrong)
2. Install.sh runs network_wwan.sh which doesn't touch resolv.conf
3. `stub-resolv.conf` persists and causes IPv6 preference issues

## The Fix

Change `unified_install/bootstrap.sh` line 276 from:
```bash
sudo ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
```

To:
```bash
sudo rm -f /etc/resolv.conf
sudo ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
```

This matches the original `setup_uah_wireless.sh` behavior.

## Why stub-resolv.conf is Wrong

- `stub-resolv.conf`: Points to `127.0.0.53` (systemd-resolved stub resolver)
  - The stub resolver runs locally and makes its own DNS queries
  - May prefer IPv6 AAAA records if available, even when IPv6 connectivity is broken
  - Causes "network unreachable" errors when trying to connect to IPv6 addresses

- `resolv.conf`: Contains actual upstream DNS servers
  - Direct connection to upstream DNS (e.g., 8.8.8.8)
  - No local proxy to introduce IPv6 preference issues
