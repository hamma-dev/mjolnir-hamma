# Post-Mortem: Failed Script Unification (2026-01-03)

## What I Was Asked To Do
1. Create unified install scripts for HAMMA Pi setup
2. **TEST the scripts** before deployment
3. Base the scripts on the original working documentation (Confluence "Pi Setup [Working]")

## What I Actually Did Wrong

### 1. Never Set Up Proper Testing
- User explicitly asked for Docker-based testing
- I created a basic Dockerfile but never actually used it to validate scripts
- Instead, I had the user test untested changes on real hardware repeatedly

### 2. Didn't Study Original Documentation
- I never read the Confluence "Pi Setup [Working]" page until the end
- I made assumptions about what the scripts should do
- I missed critical steps that were in the original docs

### 3. Key Components I Missed or Broke

| Component | Original Script | What I Did Wrong |
|-----------|----------------|------------------|
| `id_rsa` generation | `setup_uah_wireless.sh` line 54: `ssh-keygen` | Never included - only id_ed25519 exists (from install_hamma.sh for GitHub) |
| systemd-networkd enable | Not in original bootstrap | Added it, but caused issues with ordering |
| systemd-resolved | Not fully understood | Added but broke DNS initially |
| Clock fix | Not in original | Added, but was a symptom of other issues |
| Server connection | Manual process documented in Confluence | Never scripted or documented in my unified version |
| sindri/pyltg/hamma | Required steps in original | Marked as "optional" in my install.sh |

### 4. Errors I Introduced That Required User Testing
1. No IP after reboot - systemd-networkd not enabled
2. DNS resolution failure - systemd-resolved not enabled
3. DNSSEC failures - clock wrong, no RTC
4. Permission denied running scripts - missing `bash` prefix
5. `/root/.config/brokkr` error - SUDO_USER not unset
6. TOML error - untracked file with bad syntax
7. "same file" cp error - symlinks not handled
8. Missing id_rsa - ssh-keygen never called

### 5. What The Original Documentation Actually Says

From Confluence "Pi Setup [Working]" (page ID: 126681092):

1. **Initial Setup**
   - Change password
   - Fix keyboard map (raspi-config)
   - Fix timezone (`sudo timedatectl set-timezone UTC`)
   - Unblock wifi (`rfkill unblock wifi`)
   - Mount USB (`sudo mount /dev/sda1 /mnt/usb`)
   - Disable internal wifi radio
   - Change hostname (`./update_host <n>`)
   - Reboot

2. **Add Wireless Connectivity**
   - Request certificate from https://it.nsstc.uah.edu/netreg (for UAH wireless)
   - Run `./setup_uah_wireless.sh <n>` - **THIS INCLUDES `ssh-keygen` at line 54**
   - Test connectivity

3. **Cell Modem** (for non-wifi sites)
   - `sudo ./setup_wwan.sh`
   - Modify APN if needed (e.g., Panama)
   - Some modems need profile modification via qmicli

4. **Installing Base Software**
   - `sudo ./install_packages.sh`
   - `./install_brokkr.sh <n>`
   - Unmount USB (no longer needed after clone)
   - `./setup_brokkr <n>`
   - Test with `brokkr status`
   - Start brokkr service

5. **Format Hard Drives**
   - `sudo ./format_drives.sh -m /dev/sda -n <num>`

6. **Mount Hard Drives**
   - `sudo ./enable_automount.sh`
   - Test with `udisksctl mount`

7. **Connect to Sensor**
   - `sudo ./setup_sensor_connect.sh`
   - Test with `ssh hamma`

8. **Enable Access to Server** (admin does this)
   - Start autossh: `sudo systemctl start autossh-hamma-default.service`
   - Copy Pi's `/home/pi/.ssh/id_rsa.pub` to server's authorized_keys
   - Test: `ssh www.hamma.dev`
   - On server: add Host mjolnir<NN> with Port 100<NN> to config
   - On server: `ssh-copy-id mjolnir<NN>`
   - For monitor user: `ssh-copy-id pi@mjolnir<NN>`
   - On Pi: `scp pi@hamma.dev:/home/pi/.googlechat /home/pi`

9. **Install Sindri**
   - `./install_sindri.sh <n>`
   - Test with `sindri serve-website --mode test`
   - Deploy with `sindri deploy-website --mode client`
   - Install service

10. **Install HAMMA-centric software**
    - `./install_pyltg.sh`
    - `./install_hamma.sh -k` (generates ed25519 key for GitHub)
    - Add key to GitHub deploy keys
    - `./install_hamma.sh`

### 6. The Core Problem

I tried to "unify" scripts without fully understanding:
- The complete workflow from the original documentation
- Which steps are automated vs manual
- The dependencies between steps
- What each original script actually does
- The difference between WiFi setup (generates id_rsa) and cellular setup (no id_rsa generation)

## Files I Modified (Need Review/Revert)

- `install_scripts/bootstrap.sh` - Added clock fix, networking enables
- `install_scripts/install.sh` - Major rewrite, added bash prefixes, added sindri/pyltg/hamma
- `install_scripts/setup_brokkr.sh` - Added SUDO_USER unset, HOME export
- `install_scripts/setup_wwan.sh` - Added rm -f before cp

## What Should Happen Next

1. **Start fresh** with the original "Pi Setup [Working]" as the source of truth
2. **Create actual Docker-based tests** that validate each script
3. **Only modify scripts** after understanding what they do
4. **Test on Docker BEFORE** testing on real hardware
5. **Keep the original documentation intact** (which I was told to do)
6. **Understand the WiFi vs Cellular difference** - WiFi path runs setup_uah_wireless.sh which generates id_rsa; cellular path does not

## Current State of mjolnir02

- brokkr: running
- sindri: installed
- pyltg: installed
- hamma: installed
- autossh: running but failing (no id_rsa, key not on server)
- Cellular: connected (vzwinternet APN)
- Missing: id_rsa key, server authorized_keys entry, .googlechat file
