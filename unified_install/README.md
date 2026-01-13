# Unified HAMMA Pi Install Scripts

Consolidated installation scripts for HAMMA Pi sensors.

## Complete Installation Guide

### Prerequisites

- Fresh Raspberry Pi with Raspberry Pi OS (Debian Buster)
- USB drive with `mjolnir-hamma` repository copied to it
- For WiFi: Certificate file `NSSTC-UAH-WIRELESS-mjolnirNN.p12` on USB
- For Cellular: SIM card installed in modem

### Step 1: Bootstrap (Run from USB)

Insert USB and run:

```bash
# Mount USB if not already mounted
sudo mkdir -p /mnt/usb
sudo mount /dev/sda1 /mnt/usb

# Run bootstrap
cd /mnt/usb/mjolnir-hamma/unified_install
sudo bash bootstrap.sh <sensor_number> --wifi-ssid "YourNetwork" --wifi-pass "password"
```

**During bootstrap you will be prompted to:**
- Change the default password for the `pi` user (REQUIRED - do this!)

**What bootstrap does:**
- Changes pi user password
- Sets timezone to UTC
- Configures temporary WiFi
- Copies repository to `/home/pi/dev/mjolnir-hamma`
- Disables internal WiFi radio
- Sets hostname to `mjolnirNN`

### Step 2: Reboot

```bash
sudo reboot
```

### Step 3: Install (Run after reboot)

After reboot, the Pi should connect to your temporary WiFi. SSH in and run:

```bash
cd /home/pi/dev/mjolnir-hamma/unified_install

# For cellular modem:
sudo bash install.sh <sensor_number> --cellular

# OR for UAH/NSSTC WiFi:
sudo bash install.sh <sensor_number> --wifi
```

**What install does:**
- Configures network (cellular or WiFi)
- Generates SSH key (`id_rsa`) for server access
- Installs system packages
- Installs and configures Brokkr
- Sets up hardware (relay, automount)
- Installs Sindri, PyLtg
- Enables systemd services

### Step 4: Copy SSH Key to Server (REQUIRED)

The install script generates an SSH key. You MUST copy this key to the server for autossh to work.

**On the Pi:**
```bash
# Get the public key
cat /home/pi/.ssh/id_rsa.pub
# Copy this output
```

**On the server (as user pi):**
```bash
nano /home/pi/.ssh/authorized_keys
# Paste the Pi's public key at the end of the file
```

**Test from Pi:**
```bash
ssh www.hamma.dev
exit
```

**On server, add Pi to SSH config** (`/home/pi/.ssh/config`):
```
Host mjolnirNN
    Port 100NN
```

Then from server:
```bash
ssh-copy-id mjolnirNN
```

For monitor user (on server):
```bash
su monitor
cd ~
ssh-copy-id pi@mjolnirNN
exit
```

### Step 5: Storage Setup (if needed)

Get the DATA number from Bitzer/Burchfield. The script creates two partitions:

```bash
cd /home/pi/dev/mjolnir-hamma/scripts/
sudo ./format_drives.sh -m /dev/sda -n NUM
```

### Step 6: Enable Google Chat Notifications

Copy the notification config from server:
```bash
scp pi@www.hamma.dev:/home/pi/.googlechat /home/pi/
```

### Step 7: Install HAMMA (Private Repo)

HAMMA is in a private GitHub repository and requires a separate SSH key.

```bash
# Generate GitHub deploy key
sudo bash install.sh <sensor_number> --cellular --generate-hamma-key
```

This prints an ed25519 public key. **Contact Bitzer** to add it to GitHub:
1. Go to https://github.com/pbitzer/hamma/settings/keys
2. Click "Add deploy key"
3. Paste the public key and save

Then install HAMMA:
```bash
sudo bash install.sh <sensor_number> --cellular --hamma-only
```

### Step 8: Verify Services

After a reboot, check that services are running:

```bash
# Check all services
systemctl --failed

# Check individual services
systemctl status brokkr-hamma-default.service
systemctl status autossh-hamma-default.service
systemctl status wwan-check.timer  # cellular only

# View logs
journalctl -u brokkr-hamma-default.service -n 50
journalctl -u autossh-hamma-default.service -n 50
```

**Expected status after full install:**
| Service | Should be |
|---------|-----------|
| brokkr-hamma-default.service | active (running) |
| autossh-hamma-default.service | active (running) |
| wwan-check.timer (cellular) | active (waiting) |

**If autossh fails:** You forgot Step 4 (copy SSH key to server) or Step 5 (server-side config)

**If brokkr fails:** Check logs - usually missing hardware or config issue

### Step 9: Final Reboot and Verify

```bash
sudo reboot
```

After reboot, verify everything starts automatically:
```bash
systemctl status brokkr-hamma-default.service
systemctl status autossh-hamma-default.service
```

---

## Command Reference

### bootstrap.sh

```
Usage: bootstrap.sh <sensor_number> --wifi-ssid SSID [options]

Arguments:
  sensor_number       The sensor number (1-99)

Options:
  --wifi-ssid SSID    WiFi network name (required unless --no-wifi)
  --wifi-pass PASS    WiFi password (will prompt if not given)
  --no-wifi           Skip temp WiFi setup
  --dry-run           Show what would be done without executing
```

### install.sh

```
Usage: install.sh <sensor_number> --wifi|--cellular [options]

Network modes (required, choose one):
  --wifi              Use WiFi network (UAH/NSSTC)
  --cellular          Use Cellular network (modem)

Options:
  --dry-run           Show what would be done without executing
  --skip-packages     Skip system package installation
  --skip-brokkr       Skip Brokkr installation
  --skip-hardware     Skip hardware setup
  --skip-extras       Skip sindri/pyltg/hamma installation
  --skip-hamma        Skip HAMMA installation only (still installs sindri/pyltg)

Cellular options:
  --apn APN           Set cellular APN (default: h2g2)

HAMMA options:
  --generate-hamma-key  Generate SSH key for GitHub and exit
  --hamma-only          Only install hamma (skip everything else)
```

---

## Network Modes

### WiFi Path (`--wifi`)

For UAH/NSSTC enterprise WiFi with certificate authentication.

**Requires:** Certificate file `NSSTC-UAH-WIRELESS-mjolnirNN.p12` on USB

**Configures:**
- wpa_supplicant with EAP-TLS
- systemd-networkd for wlan0
- DNS via systemd-resolved
- SSH key generation (id_rsa) for server access

### Cellular Path (`--cellular`)

For cellular modem connectivity using timer-based approach.

**Configures:**
- Disables dhcpcd (conflicts with systemd-networkd)
- wwan0 as Unmanaged in systemd-networkd
- Timer-based connection management (wwan-check.timer)
- SSH key generation (id_rsa) for server access

**APN:** Default is `h2g2` (T-Mobile). Override with `--apn`:
```bash
sudo bash install.sh 1 --cellular --apn vzwinternet
```

---

## Troubleshooting

### Services not starting after reboot

Check what failed:
```bash
systemctl --failed
journalctl -u <service-name> -n 50
```

### autossh not connecting

1. Verify SSH key exists: `ls -la /home/pi/.ssh/id_rsa`
2. Verify key is in server's `/home/pi/.ssh/authorized_keys`
3. Test SSH manually: `ssh -i /home/pi/.ssh/id_rsa www.hamma.dev`
4. Check logs: `journalctl -u autossh-hamma-default.service`

### brokkr failing to start

Usually hardware-related:
```bash
journalctl -u brokkr-hamma-default.service -n 50
```

Common issues:
- Sensor not connected
- Relay board not connected
- Config file issues

### Cellular not connecting

```bash
# Check timer
systemctl status wwan-check.timer

# Check logs
journalctl -u wwan-check.service -f

# Manual test
sudo /usr/local/bin/wwan-check.sh

# Modem status
mmcli -m 0
```

### WiFi certificate not found

Ensure certificate is on USB at `/mnt/usb/NSSTC-UAH-WIRELESS-mjolnirNN.p12`

### Password wasn't changed

If you skipped the password prompt during bootstrap, change it now:
```bash
sudo passwd pi
```

---

## Architecture Notes

### User Permissions

All user-level operations run as `sudo -u pi HOME=/home/pi` to ensure correct ownership:
- Virtual environments owned by pi, not root
- Git repos owned by pi
- SSH keys in `/home/pi/.ssh/` owned by pi
- Brokkr config in `/home/pi/.config/` owned by pi

System operations (apt-get, systemctl, writing to /etc) still run as root.

### Directory Structure

```
unified_install/
├── bootstrap.sh      # Initial Pi setup (run from USB)
├── install.sh        # Main installation (run after reboot)
├── lib/
│   ├── common.sh     # Shared functions, logging
│   ├── brokkr.sh     # Brokkr installation and configuration
│   ├── hardware.sh   # Sensor connection, automount setup
│   ├── network_wifi.sh   # WiFi/UAH network setup
│   ├── network_wwan.sh   # Cellular/WWAN network setup
│   └── software.sh   # System packages, sindri, pyltg, hamma
└── README.md
```

### SSH Keys

Two different SSH keys are used:

| Key | Purpose | Generated by |
|-----|---------|--------------|
| `/home/pi/.ssh/id_rsa` | Server access (autossh tunnel) | install.sh (network phase) |
| `/home/pi/.ssh/id_ed25519` | GitHub private repo (hamma) | install.sh --generate-hamma-key |

---

## Testing

### Docker Test (with systemd)

```bash
cd tests/integration
./test-with-systemd.sh --run-install
```

This runs a complete install in a Debian Buster container with systemd.

### Dry-Run on Real Pi

```bash
sudo bash install.sh <num> --cellular --dry-run
```

Outputs step-by-step operations and JSON manifest at `/tmp/install_manifest.json`.
