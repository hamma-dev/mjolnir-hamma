#!/bin/bash
# Bootstrap script for HAMMA Pi setup
#
# This script runs ONCE from USB to:
#   1. Change the default password
#   2. Clone mjolnir-hamma repository to /home/pi/dev/
#   3. Setup temporary WiFi for install (external USB antenna)
#   4. Set the hostname to mjolnirNN
#   5. Disable internal WiFi radio
#   6. Set timezone to UTC
#   7. Set permissions and prompt for reboot
#
# After reboot, all further setup runs from the cloned repository.
# The temp WiFi persists across reboot so install.sh has connectivity.
#
# Usage:
#   ./bootstrap.sh -n <sensor_number> --wifi <SSID>
#   ./bootstrap.sh -n <sensor_number> --no-wifi
#   ./bootstrap.sh <sensor_number> --wifi <SSID>    # positional (backwards compat)
#
# Examples:
#   ./bootstrap.sh -n 42 --wifi MyNetwork
#   ./bootstrap.sh 42 --wifi "Home WiFi"
#   ./bootstrap.sh -n 42 --no-wifi                  # rare: skip temp WiFi

set -e

# --- Configuration ---
INSTALL_PATH="/home/pi/dev"
USB_REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FILES_PATH="$USB_REPO_PATH/files"

# --- Parse Arguments ---
SENSOR_NUM=""
WIFI_SSID=""
NO_WIFI=false

usage() {
    echo "Usage: $0 -n <sensor_number> --wifi <SSID>"
    echo "       $0 -n <sensor_number> --no-wifi"
    echo "       $0 <sensor_number> --wifi <SSID>"
    echo ""
    echo "Options:"
    echo "  -n, --sensor-num NUM   Sensor number (e.g., 42)"
    echo "  --wifi SSID            WiFi network for install (prompts for password)"
    echo "  --no-wifi              Skip temp WiFi setup (rare)"
    echo ""
    echo "Examples:"
    echo "  $0 -n 42 --wifi MyNetwork"
    echo "  $0 42 --wifi \"Home WiFi\""
    exit 1
}

while [[ $# -gt 0 ]]; do
    case $1 in
        -n|--sensor-num)
            SENSOR_NUM="$2"
            shift 2
            ;;
        --wifi)
            WIFI_SSID="$2"
            shift 2
            ;;
        --no-wifi)
            NO_WIFI=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        -*)
            echo "Unknown option: $1"
            usage
            ;;
        *)
            # Positional argument = sensor number (backwards compat)
            if [[ -z "$SENSOR_NUM" ]]; then
                SENSOR_NUM="$1"
            else
                echo "Unknown argument: $1"
                usage
            fi
            shift
            ;;
    esac
done

# --- Validate Arguments ---
if [[ -z "$SENSOR_NUM" ]]; then
    echo "Error: Sensor number is required"
    usage
fi

if [[ -z "$WIFI_SSID" && "$NO_WIFI" == "false" ]]; then
    echo "Error: --wifi SSID is required (or use --no-wifi to skip)"
    usage
fi

if [[ -n "$WIFI_SSID" && "$NO_WIFI" == "true" ]]; then
    echo "Error: Cannot use both --wifi and --no-wifi"
    usage
fi

SENSOR_NUM_PADDED=$(printf "%.2d" "$SENSOR_NUM")
NEW_HOSTNAME="mjolnir${SENSOR_NUM_PADDED}"

echo "=== HAMMA Pi Bootstrap ==="
echo "Sensor Number: $SENSOR_NUM ($NEW_HOSTNAME)"
echo "Source: $USB_REPO_PATH"
echo "Destination: $INSTALL_PATH/mjolnir-hamma"
if [[ -n "$WIFI_SSID" ]]; then
    echo "Temp WiFi: $WIFI_SSID"
else
    echo "Temp WiFi: (skipped)"
fi
echo ""

# --- Fix system clock if obviously wrong ---
# Pi has no RTC; if clock is years off, DNSSEC fails and NTP can't sync.
# Use USB file timestamp as approximate current time.
CURRENT_YEAR=$(date +%Y)
if [[ "$CURRENT_YEAR" -lt 2024 ]]; then
    echo "[Pre] System clock is wrong ($CURRENT_YEAR), fixing..."
    # Get modification time of this script (recently copied to USB)
    USB_TIME=$(stat -c %Y "${BASH_SOURCE[0]}" 2>/dev/null || stat -f %m "${BASH_SOURCE[0]}")
    sudo date -s "@$USB_TIME"
    echo "  Clock set to approximately: $(date)"
    echo ""
fi

# --- Step 1: Change password ---
echo "[1/7] Password setup..."
read -p "  Change default password now? (Y/n): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    passwd
    echo "  Password changed"
else
    echo "  Skipping password change (remember to change it later!)"
fi
echo ""

# --- Step 2: Create dev directory and clone/copy repository ---
echo "[2/7] Setting up mjolnir-hamma repository..."
mkdir -p "$INSTALL_PATH"

if [[ -d "$INSTALL_PATH/mjolnir-hamma" ]]; then
    echo "  WARNING: $INSTALL_PATH/mjolnir-hamma already exists"
    read -p "  Overwrite? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf "$INSTALL_PATH/mjolnir-hamma"
    else
        echo "  Skipping repository copy"
    fi
fi

if [[ ! -d "$INSTALL_PATH/mjolnir-hamma" ]]; then
    # Copy from USB (preserves any local modifications on the USB)
    cp -r "$USB_REPO_PATH" "$INSTALL_PATH/mjolnir-hamma"
    # Remove macOS metadata files (AppleDouble) that cause issues with brokkr
    find "$INSTALL_PATH/mjolnir-hamma" -name '._*' -delete 2>/dev/null || true
    find "$INSTALL_PATH/mjolnir-hamma" -name '.DS_Store' -delete 2>/dev/null || true
    echo "  Repository copied to $INSTALL_PATH/mjolnir-hamma"
fi
echo ""

# --- Step 3: Setup temporary WiFi ---
echo "[3/7] Setting up temporary WiFi..."
if [[ -n "$WIFI_SSID" ]]; then
    # Prompt for password (hidden input)
    read -s -p "  Enter WiFi password for '$WIFI_SSID': " WIFI_PASSWORD
    echo ""

    if [[ -z "$WIFI_PASSWORD" ]]; then
        echo "  Error: Password cannot be empty"
        exit 1
    fi

    # Copy base wpa_supplicant config
    sudo cp "$FILES_PATH/wpa_supplicant-wlan0.conf" /etc/wpa_supplicant/wpa_supplicant-wlan0.conf

    # Append temp network with higher priority
    sudo tee -a /etc/wpa_supplicant/wpa_supplicant-wlan0.conf > /dev/null << EOF

# Temporary network for install (added by bootstrap.sh)
network={
    ssid="$WIFI_SSID"
    psk="$WIFI_PASSWORD"
    priority=10
}
EOF
    echo "  WiFi config created"

    # Copy networkd config for wlan0
    sudo cp "$FILES_PATH/10-wlan0.network" /etc/systemd/network/
    # Update hostname in network config
    sudo sed -i "s/mjolnirNN/$NEW_HOSTNAME/" /etc/systemd/network/10-wlan0.network
    echo "  Network config installed"

    # Enable and start networking services
    sudo systemctl enable systemd-networkd
    sudo systemctl enable systemd-resolved
    sudo systemctl start systemd-resolved
    sudo ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
    sudo systemctl enable wpa_supplicant@wlan0.service
    sudo systemctl start wpa_supplicant@wlan0.service || true
    sudo systemctl restart systemd-networkd

    # Wait for connectivity
    echo "  Waiting for WiFi connection..."
    ATTEMPTS=0
    MAX_ATTEMPTS=30
    while [[ $ATTEMPTS -lt $MAX_ATTEMPTS ]]; do
        if ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; then
            echo "  WiFi connected!"
            break
        fi
        ATTEMPTS=$((ATTEMPTS + 1))
        echo "    Attempt $ATTEMPTS/$MAX_ATTEMPTS..."
        sleep 2
    done

    if [[ $ATTEMPTS -ge $MAX_ATTEMPTS ]]; then
        echo "  WARNING: Could not verify WiFi connectivity"
        echo "  Check SSID/password and USB antenna connection"
        read -p "  Continue anyway? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
else
    echo "  Skipping (--no-wifi specified)"
fi
echo ""

# --- Step 4: Set hostname ---
echo "[4/7] Setting hostname to $NEW_HOSTNAME..."
echo "$NEW_HOSTNAME" | sudo tee /etc/hostname > /dev/null
sudo sed -i "s/127.0.1.1.*/127.0.1.1       $NEW_HOSTNAME/" /etc/hosts
echo "  Hostname set"
echo ""

# --- Step 5: Disable internal WiFi radio ---
echo "[5/7] Disabling internal WiFi radio..."
if ! grep -q "dtoverlay=disable-wifi" /boot/config.txt; then
    echo "dtoverlay=disable-wifi" | sudo tee -a /boot/config.txt > /dev/null
    echo "  Internal WiFi will be disabled after reboot"
else
    echo "  Internal WiFi already disabled"
fi
echo ""

# --- Step 6: Set timezone ---
echo "[6/7] Setting timezone to UTC..."
sudo timedatectl set-timezone UTC
echo "  Timezone set to UTC"
echo ""

# --- Step 7: Set permissions on install scripts ---
echo "[7/7] Setting script permissions..."
chmod +x "$INSTALL_PATH/mjolnir-hamma/install_scripts/"*.sh
chmod +x "$INSTALL_PATH/mjolnir-hamma/scripts/"*.sh 2>/dev/null || true
chmod +x "$INSTALL_PATH/mjolnir-hamma/scripts/"*.py 2>/dev/null || true
echo "  Scripts are executable"

# --- Summary ---
echo ""
echo "=== Bootstrap Complete ==="
echo ""
echo "Repository installed to: $INSTALL_PATH/mjolnir-hamma"
echo "Hostname set to: $NEW_HOSTNAME"
if [[ -n "$WIFI_SSID" ]]; then
    echo "Temp WiFi configured: $WIFI_SSID"
fi
echo ""
echo "Next steps:"
echo "  1. Reboot (temp WiFi will reconnect automatically)"
echo "  2. After reboot, run the install script:"
echo ""
echo "     cd $INSTALL_PATH/mjolnir-hamma/install_scripts"
echo "     sudo ./install.sh -n $SENSOR_NUM"
echo ""
echo "  3. After install completes, remove USB WiFi antenna"
echo ""
read -p "Reboot now? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    sudo reboot
fi
