# Australia Sensor Migration Design

## Goal

Migrate mj41, mj42, mj43 from `wwan_install` branch to `feature/daily-compression`, fixing DNS resolution, carrier.d race conditions, and wwan bringup scripts.

## Context

Three HAMMA sensors deployed in Australia on Telstra cellular (APN: `telstra.extranet`). They are accessible only via reverse SSH tunnels through hamma.dev. Physical access requires international travel. A cron-based kill switch (`connection_status.py`) reboots the Pi every 4 hours if TCP to hamma.dev:80 fails — this is the safety net.

### Current State (wwan_install branch)

| Component | State |
|-----------|-------|
| Git branch | `wwan_install` (62 commits behind target) |
| carrier.d | Symlinks present (mj41 broken, mj42/43 active) |
| wwan-check.sh | No flock locking (timer + carrier.d can race) |
| 50_bring_wwan0_up.py | Hand-rewritten 2.7–3.6KB scripts in /usr/local/bin |
| APN | telstra.extranet (in custom scripts) |
| DNS | `DNS=10.10.10.1` in 40-eth0.network (sensor IP, not DNS server) |
| Compression | Untracked old plugin version |
| Kill switch | Active (root crontab, every 4h) |
| mj41 special | `power_delim = 15` (default is 20) |

### Target State (feature/daily-compression branch)

| Component | State |
|-----------|-------|
| Git branch | `feature/daily-compression` (includes DNS fix from 0.3.x) |
| carrier.d | Empty (cleaned by installer step 3) |
| wwan-check.sh | flock-protected |
| 50_bring_wwan0_up.py | 19.8KB repo version with retry/zombie detection |
| APN | telstra.extranet (sed by installer step 6) |
| DNS | Removed from 40-eth0.network (fix/eth0-dns merged) |
| 40-eth0.network | Deployed by install.sh Phase 2 common section |
| systemd-networkd | Restarted after network config changes |
| Compression | In-branch, current version |
| Kill switch | Left alone |
| mj41 special | `power_delim = 15` in user config override |

## Prerequisites (already completed)

1. `fix/eth0-dns` merged into `0.3.x`, then `0.3.x` merged into `feature/daily-compression`
2. `install.sh` updated to deploy `40-eth0.network` in Phase 2 common section
3. `network_wwan.sh` updated to restart `systemd-networkd` after config changes
4. All tests pass (117 passed)

## Approach: Hybrid (branch switch + selective install.sh)

Switch the git branch manually, then run `install.sh` with `--skip-*` flags to only re-do network setup. This uses the installer's proven carrier.d cleanup, script deployment, APN configuration, and timer setup while skipping everything that's already working (packages, brokkr, hardware, extras).

### Sensor Order

1. **mj41** first — carrier.d already broken, lowest risk
2. **mj42** second — ~2 hour wait after mj41
3. **mj43** last — ~2 hour wait after mj42

## Per-Sensor Procedure

### Step 1: Pre-flight snapshot

Record current state before touching anything:

```bash
# From local machine, via tunnel
ssh mjolnirNN "uptime && \
  systemctl is-active brokkr-hamma-default.service && \
  systemctl is-active autossh-hamma-default.service && \
  ip -4 addr show wwan0 | grep inet && \
  sudo crontab -l | grep connection_status"
```

Verify from hamma.dev that the tunnel is live:

```bash
ssh monitor@hamma.dev "timeout 5 ssh -o ConnectTimeout=3 -o LogLevel=ERROR -p 100NN pi@127.0.0.1 hostname"
```

Also verify hostname matches expected: `hostname` should return `mjolnirNN`.

**Gate: Do not proceed unless all checks pass.**

### Step 2: Stop wwan-check timer

Prevent the timer from executing a partially-written script during the install:

```bash
sudo systemctl stop wwan-check.timer
```

The timer will be re-enabled by install.sh step 8.

### Step 3: Switch git branch

```bash
cd /home/pi/dev/mjolnir-hamma

# Inspect local modifications
git status
git diff config/main.toml  # Should only be compression config (already in new branch)

# Discard local modifications (all superseded by new branch)
git checkout -- .

# Remove untracked files that conflict with tracked files on new branch
# (compress_data.py and preset are tracked on feature/daily-compression)
rm -f plugins/compress_data.py
rm -f presets/compress_data.preset.toml

# Switch branch
git fetch origin
git checkout feature/daily-compression
git pull
```

**Gate: Verify `git status` shows clean working tree on `feature/daily-compression`.**

### Step 4: Run install.sh (network only)

```bash
sudo bash unified_install/install.sh NN --cellular --apn telstra.extranet \
  --skip-packages --skip-brokkr --skip-hardware --skip-extras --skip-hamma
```

**Expected warning:** install.sh will report a branch mismatch ("On branch 'feature/daily-compression', not '0.3.x' - skipping pull"). This is harmless — it skips the pull, which is what we want since we already pulled in Step 3.

This executes:
- **Phase 2 common:** Copies `40-eth0.network` (with DNS fix) to `/etc/systemd/network/`
- **WWAN 1/9:** Package check (already installed, skips)
- **WWAN 2/9:** Disables dhcpcd and old wwan-connect (already done, harmless)
- **WWAN 3/9:** Removes carrier.d and degraded.d symlinks
- **WWAN 4/9:** Copies `20-wwan0.network` and `30-eth1.network`, restarts systemd-networkd
- **WWAN 5/9:** Copies repo `50_bring_wwan0_up.py` and `wwan-check.sh` (with flock) to `/usr/local/bin/`
- **WWAN 6/9:** Seds APN to `telstra.extranet` in the Python script
- **WWAN 7/9:** Copies timer/service unit files, daemon-reload, enables timer
- **WWAN 8/9:** Starts wwan-check.timer
- **WWAN 9/9:** SSH key check (already exists, skips)

**Does NOT touch:** autossh, brokkr service, SSH keys, hardware config, Python packages.

### Step 5: Verify network is still alive

```bash
# On sensor
ip -4 addr show wwan0
ping -c 3 8.8.8.8
ping -c 3 google.com   # DNS resolution test
```

**Gate: If ping fails, the kill switch will reboot within 4 hours. But check immediately.**

### Step 6: Apply per-unit config (mj41 only)

```bash
# Create user config override directory if needed
sudo -H -u pi mkdir -p /home/pi/.config/brokkr/hamma

# Write power_delim override
sudo -H -u pi tee /home/pi/.config/brokkr/hamma/main.toml > /dev/null <<'EOF'
[steps.state_monitor.plugins.hamma_state_monitor.kwargs]
power_delim = 15
EOF
```

### Step 7: Restart brokkr

```bash
sudo systemctl restart brokkr-hamma-default.service
```

Do NOT restart autossh — the tunnel is our lifeline.

### Step 8: Post-migration verification

```bash
# Service status
systemctl is-active brokkr-hamma-default.service
systemctl is-active autossh-hamma-default.service
systemctl is-active wwan-check.timer

# Tunnel check from hamma.dev
ssh monitor@hamma.dev "timeout 5 ssh -o ConnectTimeout=3 -o LogLevel=ERROR -p 100NN pi@127.0.0.1 hostname"

# DNS resolution
ping -c 1 google.com

# Git branch
git -C /home/pi/dev/mjolnir-hamma branch --show-current

# Verify carrier.d is clean
ls /etc/networkd-dispatcher/carrier.d/

# Verify flock in wwan-check.sh
grep flock /usr/local/bin/wwan-check.sh

# Verify APN
grep "^APN" /usr/local/bin/50_bring_wwan0_up.py

# Verify DNS fix
grep DNS /etc/systemd/network/40-eth0.network  # Should return nothing

# Check brokkr logs for errors
journalctl -u brokkr-hamma-default.service --since "5 min ago" -n 10
```

Wait 15 minutes, then re-check tunnel and services. Wait ~2 hours before proceeding to next sensor.

## Rollback Plan

If connectivity is lost after any step:

1. **Kill switch reboots within 4 hours** if TCP to hamma.dev:80 fails
2. On reboot, `wwan-check.timer` fires and reconnects cellular
3. Autossh config is unchanged — tunnel should re-establish
4. If tunnel doesn't come back after reboot, wait for next kill switch cycle (4h)
5. **Worst case:** Contact someone with physical access

If brokkr has issues but connectivity is fine:

```bash
# Revert to previous branch (only affects brokkr config, not deployed scripts)
cd /home/pi/dev/mjolnir-hamma
git checkout wwan_install
sudo systemctl restart brokkr-hamma-default.service
```

Note: Branch revert only affects brokkr's configuration (presets/plugins read from the repo). The deployed network scripts in `/usr/local/bin/` and systemd timer units remain as-installed by `install.sh` — this is fine since the new scripts are strictly better.

## Not In Scope

- Removing kill switch cron (separate concern, tracked in HAM-75)
- Persistent journal (SD card wear concern)
- ttyAMA0 modbus noise (HAM-76)
- mj00 stability (monitoring post HAM-75 fix)

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Cellular connection drops during install | Low | High | Kill switch reboots; timer reconnects |
| SSH tunnel dies | Low | High | autossh not touched; kill switch reboots |
| New wwan script fails to connect | Low | Medium | Same proven script running on mj03; APN sed tested |
| systemd-networkd restart drops wwan0 | Very low | High | wwan0 is Unmanaged=yes; only eth0/eth1 affected |
| git checkout fails (conflicts) | Low | None | Pre-cleaned; can resolve interactively |
| Brokkr fails with new config | Low | Low | Science data missed but connectivity fine; revert branch |
