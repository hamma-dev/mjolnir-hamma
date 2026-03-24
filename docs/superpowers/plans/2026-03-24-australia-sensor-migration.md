# Australia Sensor Migration Plan

> **For agentic workers:** This is an operational migration plan, NOT a code implementation plan. It involves live SSH sessions to remote sensors in Australia. Execute steps manually via SSH. Use checkbox syntax for tracking. Do NOT use subagent-driven-development — this requires a single continuous session with human oversight.

**Goal:** Migrate mj41, mj42, mj43 from `wwan_install` to `feature/daily-compression`, fixing DNS, carrier.d races, and wwan scripts.

**Architecture:** Switch git branch on each sensor, then run `install.sh` with `--skip-*` flags to re-deploy only network components. One sensor at a time with ~2 hour stability gap between each.

**Spec:** `docs/superpowers/specs/2026-03-24-australia-sensor-migration-design.md`

---

## Task 0: Push prerequisites to origin

The code changes are committed locally. Sensors need to fetch them.

- [ ] **Step 1: Push 0.3.x (DNS fix)**

```bash
cd /Users/bitzer/Documents/insync/uah_gdrive/programming/python/hamma_sensor_repos/mjolnir-hamma
git checkout 0.3.x
git push origin 0.3.x
```

- [ ] **Step 2: Push feature/daily-compression (installer fixes + DNS merge)**

```bash
git checkout feature/daily-compression
git push origin feature/daily-compression
```

- [ ] **Step 3: Verify remote has the commits**

```bash
git log --oneline origin/feature/daily-compression -5
```

Expected: should show `71a12e1` (fix(install): deploy 40-eth0.network and restart networkd) and `9913b24` (Merge branch '0.3.x').

- [ ] **Step 4: Verify deadman script is in the repo**

```bash
ls -la scripts/migration-deadman.sh
```

This script must be committed and pushed so sensors can access it after branch switch.

---

## Task 1: Migrate mj41

mj41 has a broken carrier.d symlink already, making it the lowest-risk sensor to start with. It also needs the `power_delim = 15` user config override.

### Pre-flight

- [ ] **Step 1: Snapshot current state**

```bash
ssh mjolnir41 "uptime && \
  systemctl is-active brokkr-hamma-default.service && \
  systemctl is-active autossh-hamma-default.service && \
  ip -4 addr show wwan0 | grep inet && \
  sudo crontab -l | grep connection_status && \
  hostname"
```

Record the output. Verify: services active, wwan0 has IP, kill switch present, hostname is `mjolnir41`.

- [ ] **Step 2: Verify tunnel from hamma.dev**

```bash
ssh monitor@hamma.dev "timeout 5 ssh -o ConnectTimeout=3 -o LogLevel=ERROR -p 10041 pi@127.0.0.1 hostname"
```

Expected: `mjolnir41`

**GATE: All checks must pass before proceeding.**

### Execute migration

- [ ] **Step 3: Copy and arm dead man's switch (30 minutes)**

The script doesn't exist on the sensor yet (it's on `feature/daily-compression`). SCP it first:

```bash
scp scripts/migration-deadman.sh mjolnir41:/tmp/migration-deadman.sh
ssh mjolnir41 "sudo bash /tmp/migration-deadman.sh arm 30"
```

Verify output shows "DEAD MAN'S SWITCH ARMED" and lists all backed-up files. The clock is now ticking — if we don't defuse within 30 minutes, all changes roll back and the Pi reboots.

- [ ] **Step 4: Fetch new branch (while timer still running)**

Fetch first, before stopping the timer — minimizes the window where the sensor has no automatic wwan0 recovery. The fetch is read-only and safe to run alongside the old scripts.

```bash
ssh mjolnir41 "cd /home/pi/dev/mjolnir-hamma && git fetch origin"
```

- [ ] **Step 5: Stop wwan-check timer**

```bash
ssh mjolnir41 "sudo systemctl stop wwan-check.timer"
```

From here until install.sh restarts the timer (Step 8), there is no automatic wwan0 recovery. Work quickly but carefully.

- [ ] **Step 6: Inspect and clean git working tree**

```bash
ssh mjolnir41 "cd /home/pi/dev/mjolnir-hamma && git status && git diff --stat"
```

Expected modifications: `config/main.toml`, possibly empty `install_scripts/` changes. Untracked: `plugins/compress_data.py`, `presets/compress_data.preset.toml`.

```bash
ssh mjolnir41 "cd /home/pi/dev/mjolnir-hamma && \
  git checkout -- . && \
  rm -f plugins/compress_data.py presets/compress_data.preset.toml"
```

- [ ] **Step 7: Switch to feature/daily-compression (local only, already fetched)**

```bash
ssh mjolnir41 "cd /home/pi/dev/mjolnir-hamma && \
  git checkout feature/daily-compression && \
  git pull"
```

Verify:

```bash
ssh mjolnir41 "cd /home/pi/dev/mjolnir-hamma && git branch --show-current && git status"
```

Expected: `feature/daily-compression`, clean working tree.

**GATE: Branch must be clean before running installer.**

- [ ] **Step 8: Run install.sh (network only)**

```bash
ssh mjolnir41 "sudo bash /home/pi/dev/mjolnir-hamma/unified_install/install.sh 41 \
  --cellular --apn telstra.extranet \
  --skip-packages --skip-brokkr --skip-hardware --skip-extras --skip-hamma"
```

Expected: Warning about branch mismatch ("On branch 'feature/daily-compression', not '0.3.x' - skipping pull") — this is harmless.

Watch output for errors. All 9 WWAN steps should complete with success messages.

- [ ] **Step 9: Verify network survived**

First check routing, then connectivity:

```bash
ssh mjolnir41 "ip route show default && \
  ip -4 addr show wwan0 | grep inet && \
  ping -c 3 8.8.8.8 && \
  ping -c 3 google.com"
```

**GATE: Default route must show wwan0. All pings must succeed. If DNS fails but IP ping works, the DNS fix didn't take effect — investigate. If default route is missing, run `sudo /usr/local/bin/wwan-check.sh` immediately.**

- [ ] **Step 10: Apply power_delim override**

```bash
ssh mjolnir41 "sudo -H -u pi mkdir -p /home/pi/.config/brokkr/hamma"
ssh mjolnir41 "printf '[steps.state_monitor.plugins.hamma_state_monitor.kwargs]\npower_delim = 15\n' | sudo -H -u pi tee /home/pi/.config/brokkr/hamma/main.toml > /dev/null"
```

Verify:

```bash
ssh mjolnir41 "cat /home/pi/.config/brokkr/hamma/main.toml"
```

- [ ] **Step 11: Restart brokkr**

```bash
ssh mjolnir41 "sudo systemctl restart brokkr-hamma-default.service"
```

Do NOT restart autossh.

### Post-migration verification

- [ ] **Step 12: Full verification checklist**

Uses `;` instead of `&&` so one check failing doesn't abort the rest:

```bash
ssh mjolnir41 "echo '=== Services ===' ; \
  systemctl is-active brokkr-hamma-default.service ; \
  systemctl is-active autossh-hamma-default.service ; \
  systemctl is-active wwan-check.timer ; \
  echo '=== Branch ===' ; \
  git -C /home/pi/dev/mjolnir-hamma branch --show-current ; \
  echo '=== carrier.d ===' ; \
  ls /etc/networkd-dispatcher/carrier.d/ 2>/dev/null || echo '(directory does not exist)' ; \
  echo '=== flock ===' ; \
  grep flock /usr/local/bin/wwan-check.sh ; \
  echo '=== APN ===' ; \
  grep '^APN\s*=' /usr/local/bin/50_bring_wwan0_up.py ; \
  echo '=== DNS fix ===' ; \
  grep '^DNS=' /etc/systemd/network/40-eth0.network || echo '(no DNS line = good)' ; \
  echo '=== 30-eth1 ===' ; \
  head -3 /etc/systemd/network/30-eth1.network ; \
  echo '=== Brokkr logs ===' ; \
  journalctl -u brokkr-hamma-default.service --since '2 min ago' -n 5 --no-pager"
```

Expected:
- Services: all `active`
- Branch: `feature/daily-compression`
- carrier.d: empty listing or directory does not exist
- flock: shows `flock -n -E 1`
- APN: `APN = "telstra.extranet"`
- DNS: `(no DNS line = good)`
- 30-eth1: shows `[Match]` / `Name=eth1` header
- Brokkr logs: no errors

- [ ] **Step 13: Verify tunnel from hamma.dev**

```bash
ssh monitor@hamma.dev "timeout 5 ssh -o ConnectTimeout=3 -o LogLevel=ERROR -p 10041 pi@127.0.0.1 hostname"
```

Expected: `mjolnir41`

- [ ] **Step 14: Defuse dead man's switch**

```bash
ssh mjolnir41 "sudo bash /tmp/migration-deadman.sh defuse"
```

Verify output shows "DEAD MAN'S SWITCH DEFUSED". Backup files are preserved at `/var/lib/migration-backup` for manual cleanup later.

If `/tmp/migration-deadman.sh` is missing (e.g., after a reboot), defuse manually:
```bash
ssh mjolnir41 "sudo systemctl stop deadman-rollback.timer && \
  sudo systemctl disable deadman-rollback.timer && \
  sudo rm -f /etc/systemd/system/deadman-rollback.timer /etc/systemd/system/deadman-rollback.service && \
  sudo systemctl daemon-reload"
```

- [ ] **Step 15: Wait 15 minutes, recheck**

```bash
ssh mjolnir41 "systemctl is-active brokkr-hamma-default.service && \
  systemctl is-active autossh-hamma-default.service && \
  ping -c 1 google.com"
```

**GATE: Wait ~2 hours before starting mj42. Monitor tunnel periodically.**

---

## Task 2: Migrate mj42

Same procedure as mj41 (including dead man's switch), minus the power_delim override.

### Pre-flight

- [ ] **Step 1: Snapshot current state**

```bash
ssh mjolnir42 "uptime && \
  systemctl is-active brokkr-hamma-default.service && \
  systemctl is-active autossh-hamma-default.service && \
  ip -4 addr show wwan0 | grep inet && \
  sudo crontab -l | grep connection_status && \
  hostname"
```

- [ ] **Step 2: Verify tunnel from hamma.dev**

```bash
ssh monitor@hamma.dev "timeout 5 ssh -o ConnectTimeout=3 -o LogLevel=ERROR -p 10042 pi@127.0.0.1 hostname"
```

**GATE: All checks must pass.**

### Execute migration

- [ ] **Step 3: Copy and arm dead man's switch (30 minutes)**

```bash
scp scripts/migration-deadman.sh mjolnir42:/tmp/migration-deadman.sh
ssh mjolnir42 "sudo bash /tmp/migration-deadman.sh arm 30"
```

Verify output shows "DEAD MAN'S SWITCH ARMED" and lists all backed-up files.

- [ ] **Step 4: Fetch new branch (while timer still running)**

```bash
ssh mjolnir42 "cd /home/pi/dev/mjolnir-hamma && git fetch origin"
```

- [ ] **Step 5: Stop wwan-check timer**

```bash
ssh mjolnir42 "sudo systemctl stop wwan-check.timer"
```

- [ ] **Step 6: Inspect and clean git working tree**

```bash
ssh mjolnir42 "cd /home/pi/dev/mjolnir-hamma && git status && git diff --stat"
```

```bash
ssh mjolnir42 "cd /home/pi/dev/mjolnir-hamma && \
  git checkout -- . && \
  rm -f plugins/compress_data.py presets/compress_data.preset.toml"
```

- [ ] **Step 7: Switch to feature/daily-compression (local only, already fetched)**

```bash
ssh mjolnir42 "cd /home/pi/dev/mjolnir-hamma && \
  git checkout feature/daily-compression && \
  git pull"
```

Verify:

```bash
ssh mjolnir42 "cd /home/pi/dev/mjolnir-hamma && git branch --show-current && git status"
```

**GATE: Clean working tree on correct branch.**

- [ ] **Step 8: Run install.sh (network only)**

```bash
ssh mjolnir42 "sudo bash /home/pi/dev/mjolnir-hamma/unified_install/install.sh 42 \
  --cellular --apn telstra.extranet \
  --skip-packages --skip-brokkr --skip-hardware --skip-extras --skip-hamma"
```

- [ ] **Step 9: Verify network survived**

```bash
ssh mjolnir42 "ip route show default && \
  ip -4 addr show wwan0 | grep inet && \
  ping -c 3 8.8.8.8 && \
  ping -c 3 google.com"
```

**GATE: Default route must show wwan0. All pings must succeed.**

- [ ] **Step 10: Restart brokkr**

```bash
ssh mjolnir42 "sudo systemctl restart brokkr-hamma-default.service"
```

### Post-migration verification

- [ ] **Step 11: Full verification checklist**

```bash
ssh mjolnir42 "echo '=== Services ===' ; \
  systemctl is-active brokkr-hamma-default.service ; \
  systemctl is-active autossh-hamma-default.service ; \
  systemctl is-active wwan-check.timer ; \
  echo '=== Branch ===' ; \
  git -C /home/pi/dev/mjolnir-hamma branch --show-current ; \
  echo '=== carrier.d ===' ; \
  ls /etc/networkd-dispatcher/carrier.d/ 2>/dev/null || echo '(directory does not exist)' ; \
  echo '=== flock ===' ; \
  grep flock /usr/local/bin/wwan-check.sh ; \
  echo '=== APN ===' ; \
  grep '^APN\s*=' /usr/local/bin/50_bring_wwan0_up.py ; \
  echo '=== DNS fix ===' ; \
  grep '^DNS=' /etc/systemd/network/40-eth0.network || echo '(no DNS line = good)' ; \
  echo '=== 30-eth1 ===' ; \
  head -3 /etc/systemd/network/30-eth1.network ; \
  echo '=== Brokkr logs ===' ; \
  journalctl -u brokkr-hamma-default.service --since '2 min ago' -n 5 --no-pager"
```

- [ ] **Step 12: Verify tunnel from hamma.dev**

```bash
ssh monitor@hamma.dev "timeout 5 ssh -o ConnectTimeout=3 -o LogLevel=ERROR -p 10042 pi@127.0.0.1 hostname"
```

- [ ] **Step 13: Defuse dead man's switch**

```bash
ssh mjolnir42 "sudo bash /tmp/migration-deadman.sh defuse"
```

Verify output shows "DEAD MAN'S SWITCH DEFUSED".

- [ ] **Step 14: Wait 15 minutes, recheck**

```bash
ssh mjolnir42 "systemctl is-active brokkr-hamma-default.service && \
  systemctl is-active autossh-hamma-default.service && \
  ping -c 1 google.com"
```

**GATE: Wait ~2 hours before starting mj43.**

---

## Task 3: Migrate mj43

Same procedure as mj42.

### Pre-flight

- [ ] **Step 1: Snapshot current state**

```bash
ssh mjolnir43 "uptime && \
  systemctl is-active brokkr-hamma-default.service && \
  systemctl is-active autossh-hamma-default.service && \
  ip -4 addr show wwan0 | grep inet && \
  sudo crontab -l | grep connection_status && \
  hostname"
```

- [ ] **Step 2: Verify tunnel from hamma.dev**

```bash
ssh monitor@hamma.dev "timeout 5 ssh -o ConnectTimeout=3 -o LogLevel=ERROR -p 10043 pi@127.0.0.1 hostname"
```

**GATE: All checks must pass.**

### Execute migration

- [ ] **Step 3: Copy and arm dead man's switch (30 minutes)**

```bash
scp scripts/migration-deadman.sh mjolnir43:/tmp/migration-deadman.sh
ssh mjolnir43 "sudo bash /tmp/migration-deadman.sh arm 30"
```

Verify output shows "DEAD MAN'S SWITCH ARMED" and lists all backed-up files.

- [ ] **Step 4: Fetch new branch (while timer still running)**

```bash
ssh mjolnir43 "cd /home/pi/dev/mjolnir-hamma && git fetch origin"
```

- [ ] **Step 5: Stop wwan-check timer**

```bash
ssh mjolnir43 "sudo systemctl stop wwan-check.timer"
```

- [ ] **Step 6: Inspect and clean git working tree**

```bash
ssh mjolnir43 "cd /home/pi/dev/mjolnir-hamma && git status && git diff --stat"
```

```bash
ssh mjolnir43 "cd /home/pi/dev/mjolnir-hamma && \
  git checkout -- . && \
  rm -f plugins/compress_data.py presets/compress_data.preset.toml"
```

- [ ] **Step 7: Switch to feature/daily-compression (local only, already fetched)**

```bash
ssh mjolnir43 "cd /home/pi/dev/mjolnir-hamma && \
  git checkout feature/daily-compression && \
  git pull"
```

Verify:

```bash
ssh mjolnir43 "cd /home/pi/dev/mjolnir-hamma && git branch --show-current && git status"
```

**GATE: Clean working tree on correct branch.**

- [ ] **Step 8: Run install.sh (network only)**

```bash
ssh mjolnir43 "sudo bash /home/pi/dev/mjolnir-hamma/unified_install/install.sh 43 \
  --cellular --apn telstra.extranet \
  --skip-packages --skip-brokkr --skip-hardware --skip-extras --skip-hamma"
```

- [ ] **Step 9: Verify network survived**

```bash
ssh mjolnir43 "ip route show default && \
  ip -4 addr show wwan0 | grep inet && \
  ping -c 3 8.8.8.8 && \
  ping -c 3 google.com"
```

**GATE: Default route must show wwan0. All pings must succeed.**

- [ ] **Step 10: Restart brokkr**

```bash
ssh mjolnir43 "sudo systemctl restart brokkr-hamma-default.service"
```

### Post-migration verification

- [ ] **Step 11: Full verification checklist**

```bash
ssh mjolnir43 "echo '=== Services ===' ; \
  systemctl is-active brokkr-hamma-default.service ; \
  systemctl is-active autossh-hamma-default.service ; \
  systemctl is-active wwan-check.timer ; \
  echo '=== Branch ===' ; \
  git -C /home/pi/dev/mjolnir-hamma branch --show-current ; \
  echo '=== carrier.d ===' ; \
  ls /etc/networkd-dispatcher/carrier.d/ 2>/dev/null || echo '(directory does not exist)' ; \
  echo '=== flock ===' ; \
  grep flock /usr/local/bin/wwan-check.sh ; \
  echo '=== APN ===' ; \
  grep '^APN\s*=' /usr/local/bin/50_bring_wwan0_up.py ; \
  echo '=== DNS fix ===' ; \
  grep '^DNS=' /etc/systemd/network/40-eth0.network || echo '(no DNS line = good)' ; \
  echo '=== 30-eth1 ===' ; \
  head -3 /etc/systemd/network/30-eth1.network ; \
  echo '=== Brokkr logs ===' ; \
  journalctl -u brokkr-hamma-default.service --since '2 min ago' -n 5 --no-pager"
```

- [ ] **Step 12: Verify tunnel from hamma.dev**

```bash
ssh monitor@hamma.dev "timeout 5 ssh -o ConnectTimeout=3 -o LogLevel=ERROR -p 10043 pi@127.0.0.1 hostname"
```

- [ ] **Step 13: Defuse dead man's switch**

```bash
ssh mjolnir43 "sudo bash /tmp/migration-deadman.sh defuse"
```

Verify output shows "DEAD MAN'S SWITCH DEFUSED".

- [ ] **Step 14: Wait 15 minutes, recheck**

```bash
ssh mjolnir43 "systemctl is-active brokkr-hamma-default.service && \
  systemctl is-active autossh-hamma-default.service && \
  ping -c 1 google.com"
```

---

## Task 4: Post-migration wrap-up

After all three sensors are stable (next day):

- [ ] **Step 1: Final health check on all three**

```bash
for s in 41 42 43; do
  echo "=== mjolnir$s ==="
  ssh mjolnir$s "systemctl is-active brokkr-hamma-default.service && \
    systemctl is-active autossh-hamma-default.service && \
    git -C /home/pi/dev/mjolnir-hamma branch --show-current && \
    ping -c 1 -W 5 google.com 2>&1 | tail -1"
  echo ""
done
```

- [ ] **Step 2: Verify tunnels from hamma.dev**

```bash
ssh monitor@hamma.dev 'for port in 10041 10042 10043; do
  result=$(timeout 5 ssh -o ConnectTimeout=3 -o LogLevel=ERROR -p $port pi@127.0.0.1 hostname 2>&1)
  echo "Port $port: ${result:-TIMEOUT/FAIL}"
done'
```

- [ ] **Step 3: Update Jira**

Add a comment to any relevant Jira ticket noting the migration is complete.

- [ ] **Step 4: Delete wwan_install branch (if desired)**

Only after confirming all sensors are stable for 24+ hours:

```bash
git push origin --delete wwan_install
```

---

## Rollback Reference

**Primary: Dead man's switch (automatic)**
- Armed at start of each sensor's migration with a 30-minute timer
- If not defused, restores ALL backed-up files (network configs, wwan scripts, systemd units), reverts git branch, restarts networkd and wwan timer, then reboots
- Survives reboots (systemd timer with `WantedBy=timers.target`)
- Limitation: `OnActiveSec` resets on each reboot. If Pi is in a rapid reboot loop (uptime < 30 min), the timer never fires. Acceptable because kill switch reboots every 4 hours.

**Secondary: Kill switch (existing)**
1. Kill switch reboots Pi within 4 hours if TCP to hamma.dev:80 fails
2. wwan-check.timer reconnects cellular on boot
3. Autossh re-establishes tunnel (config unchanged)
4. Wait up to 8 hours (two kill switch cycles) before escalating

**If brokkr fails but connectivity is fine:**
```bash
ssh mjolnirNN "cd /home/pi/dev/mjolnir-hamma && git checkout wwan_install && \
  sudo systemctl restart brokkr-hamma-default.service"
```
This only reverts brokkr config. Deployed scripts in `/usr/local/bin/` and systemd units stay as-installed (which is fine — they're strictly better).
