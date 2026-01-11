# Unified HAMMA Pi Install Scripts

Consolidated installation scripts for HAMMA Pi sensors, replacing the collection of individual setup scripts.

## Quick Start

```bash
# On fresh Pi with USB mounted at /mnt/usb:

# 1. Bootstrap (sets hostname, copies repo, disables internal WiFi)
sudo bash bootstrap.sh <sensor_number> --wifi-ssid "YourNetwork" --wifi-pass "password"

# 2. Reboot
sudo reboot

# 3. Install (choose --wifi OR --cellular)
sudo bash install.sh <sensor_number> --wifi      # For UAH/NSSTC WiFi
sudo bash install.sh <sensor_number> --cellular  # For cellular modem
```

## Scripts

### bootstrap.sh

Initial Pi setup before network is configured.

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

**What it does:**
- Sets timezone to UTC
- Configures temporary WiFi (for initial connectivity)
- Mounts USB and copies repository to `/home/pi/dev/`
- Disables internal WiFi radio (for external dongle)
- Sets hostname to `mjolnirNN`

### install.sh

Main installation after bootstrap and reboot.

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

Cellular options:
  --apn APN           Set cellular APN (default: h2g2)

HAMMA private repo options:
  --generate-hamma-key  Generate SSH key for GitHub and exit
  --hamma-only          Only install hamma (skip everything else)
```

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
- flock wrapper to prevent concurrent connections
- SSH key generation (id_rsa) for server access

**APN:** Default is `h2g2` (T-Mobile). Override with `--apn`:
```bash
sudo bash install.sh 1 --cellular --apn vzwinternet
```

## HAMMA Private Repository

The `hamma` package is in a private GitHub repository and requires SSH key authentication.

### Two-Step Installation

**Step 1: Generate SSH key**
```bash
sudo bash install.sh <sensor_number> --generate-hamma-key
```
This generates an ed25519 key and prints the public key.

**Step 2: Add key to GitHub**
1. Copy the public key output
2. Go to https://github.com/pbitzer/hamma/settings/keys
3. Click "Add deploy key"
4. Paste the public key and save

**Step 3: Install hamma**
```bash
sudo bash install.sh <sensor_number> --hamma-only
```

### During Full Installation

If running the full install and the SSH key is already configured:
- hamma will be installed automatically as part of the extras phase

If the key is not configured:
- The hamma install step will fail
- Run `--generate-hamma-key`, add to GitHub, then `--hamma-only`

## Dry-Run Mode

Use `--dry-run` to see what would be done without making changes:

```bash
sudo bash install.sh 1 --wifi --dry-run
```

This outputs:
- Step-by-step description of operations
- JSON manifest at `/tmp/install_manifest.json`

View the manifest:
```bash
cat /tmp/install_manifest.json | python3 -m json.tool
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FILES_DIR` | repo's `files/` directory | Source for config files |
| `SCRIPTS_DIR` | repo's `scripts/` directory | Source for Python scripts |
| `USB_PATH` | `/mnt/usb` | USB mount point |

The scripts automatically detect the repo's files and scripts directories. No manual configuration is typically needed.

## Directory Structure

```
unified_install/
├── bootstrap.sh      # Initial Pi setup
├── install.sh        # Main installation driver
├── lib/
│   ├── common.sh     # Shared functions, logging, manifest
│   ├── brokkr.sh     # Brokkr installation and configuration
│   ├── hardware.sh   # Sensor connection, automount setup
│   ├── network_wifi.sh   # WiFi/UAH network setup
│   ├── network_wwan.sh   # Cellular/WWAN network setup
│   └── software.sh   # System packages, sindri, pyltg, hamma
├── files/            # (empty - uses repo's files/ directory)
└── README.md
```

## Testing

### Docker Tests

```bash
# Build test image
docker build -t pi-test -f tests/docker/Dockerfile.pi-test .

# Run pytest
docker run --rm -v "$(pwd):/home/pi/dev/mjolnir-hamma" \
  -e FILES_DIR=files -e SCRIPTS_DIR=scripts \
  pi-test python3 -m pytest tests/unified/ -v
```

### Dry-Run on Real Pi

```bash
# SSH to Pi and run dry-run
ssh pi@<pi-ip>
cd /home/pi/dev/mjolnir-hamma/unified_install
FILES_DIR=../files SCRIPTS_DIR=../scripts \
  sudo bash install.sh <num> --cellular --dry-run
```

## Comparison to Original Scripts

The unified scripts replace these individual scripts:

| Original Script | Unified Equivalent |
|-----------------|-------------------|
| `update_host.sh` | `bootstrap.sh` |
| `disable_wifi_radio.sh` | `bootstrap.sh` |
| `setup_uah_wireless.sh` | `install.sh --wifi` |
| `setup_wwan.sh` | `install.sh --cellular` |
| `install_brokkr.sh` + `setup_brokkr.sh` | `install.sh` (brokkr phase) |
| `setup_sensor_connect.sh` + `enable_automount.sh` | `install.sh` (hardware phase) |
| `install_sindri.sh`, `install_pyltg.sh`, `install_hamma.sh` | `install.sh` (extras phase) |

Archived originals are in `install_scripts/archive/`.

## Troubleshooting

### "Files not found" errors

Set `FILES_DIR` to the repo's files directory:
```bash
export FILES_DIR=/home/pi/dev/mjolnir-hamma/files
export SCRIPTS_DIR=/home/pi/dev/mjolnir-hamma/scripts
```

### WiFi certificate not found

Ensure the certificate is on the USB at `/mnt/usb/NSSTC-UAH-WIRELESS-mjolnirNN.p12`

### Cellular not connecting

Check timer status:
```bash
systemctl status wwan-check.timer
journalctl -u wwan-check.service -f
```

Manual connection test:
```bash
sudo /usr/local/bin/wwan-check.sh
```

Modem status:
```bash
mmcli -m 0
```
