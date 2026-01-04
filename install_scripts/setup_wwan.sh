#!/bin/bash
# Setup cellular modem (WWAN) connectivity for HAMMA Pi
#
# This script configures the WWAN connection using a timer-based approach
# instead of the old networkd-dispatcher method. Key changes from the
# original approach (documented in Confluence "Cellular Fixes"):
#
#   1. dhcpcd is disabled (conflicts with systemd-networkd)
#   2. wwan0 is set to Unmanaged=yes in systemd-networkd
#   3. Connection management moved from networkd-dispatcher to a systemd timer
#   4. A wrapper script uses flock to prevent concurrent connection attempts
#   5. The Python script handles all connection logic including zombie detection
#
# Usage:
#   sudo ./setup_wwan.sh [--apn APN_NAME]
#
# Options:
#   --apn APN_NAME    Set the APN (default: h2g2 for T-Mobile)
#                     Common APNs:
#                       h2g2 - T-Mobile
#                       vzwinternet - Verizon
#                       apn01.cwpanama.com.pa - Panama

set -e

# --- Configuration ---
FILES_PATH="/home/pi/dev/mjolnir-hamma/files"
SCRIPTS_PATH="/home/pi/dev/mjolnir-hamma/scripts"
NETWORK_PATH="/etc/systemd/network"
SYSTEMD_PATH="/etc/systemd/system"
BIN_PATH="/usr/local/bin"

# Default APN (can be overridden with --apn)
APN="h2g2"

# --- Parse Arguments ---
while [[ $# -gt 0 ]]; do
    case $1 in
        --apn)
            APN="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: sudo $0 [--apn APN_NAME]"
            echo ""
            echo "Options:"
            echo "  --apn APN_NAME    Set the APN (default: h2g2)"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# --- Check root ---
if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (use sudo)"
    exit 1
fi

echo "=== WWAN Setup Script ==="
echo "APN: $APN"
echo ""

# --- Step 1: Check/Install required packages ---
echo "[1/8] Checking required packages..."

# Check if packages are already installed (modified image should have these)
PACKAGES_NEEDED=""
dpkg -l modemmanager >/dev/null 2>&1 || PACKAGES_NEEDED="$PACKAGES_NEEDED modemmanager"
dpkg -l udhcpc >/dev/null 2>&1 || PACKAGES_NEEDED="$PACKAGES_NEEDED udhcpc"
dpkg -l libqmi-utils >/dev/null 2>&1 || PACKAGES_NEEDED="$PACKAGES_NEEDED libqmi-utils"

if [[ -n "$PACKAGES_NEEDED" ]]; then
    echo "  Installing missing packages:$PACKAGES_NEEDED"
    apt-get update -y
    apt-get install -y $PACKAGES_NEEDED
else
    echo "  All required packages already installed"
fi

# --- Step 2: Stop and disable conflicting services ---
echo "[2/8] Disabling conflicting services..."
systemctl stop dhcpcd.service 2>/dev/null || true
systemctl disable dhcpcd.service 2>/dev/null || true
echo "  - dhcpcd.service disabled"

# Disable old wwan-connect service if it exists
systemctl stop wwan-connect.service 2>/dev/null || true
systemctl disable wwan-connect.service 2>/dev/null || true
echo "  - wwan-connect.service disabled (if present)"

# --- Step 3: Clean up old networkd-dispatcher scripts ---
echo "[3/8] Removing old networkd-dispatcher scripts..."
rm -f /etc/networkd-dispatcher/carrier.d/50_bring_wwan0_up.py 2>/dev/null || true
rm -f /etc/networkd-dispatcher/degraded.d/50_bring_wwan0_up.py 2>/dev/null || true
rm -rf /etc/systemd/system/networkd-dispatcher.service.d/ 2>/dev/null || true
echo "  - Old dispatcher scripts removed"

# --- Step 4: Copy network configuration files ---
echo "[4/8] Installing network configuration files..."
cp "$FILES_PATH/20-wwan0.network" "$NETWORK_PATH/"
cp "$FILES_PATH/30-eth1.network" "$NETWORK_PATH/"
echo "  - Network configs installed"

# --- Step 5: Copy WWAN scripts to /usr/local/bin ---
echo "[5/8] Installing WWAN management scripts..."
# Remove existing files/symlinks first to avoid "same file" errors
rm -f "$BIN_PATH/50_bring_wwan0_up.py" "$BIN_PATH/wwan-check.sh"
cp "$SCRIPTS_PATH/50_bring_wwan0_up.py" "$BIN_PATH/"
cp "$FILES_PATH/wwan-check.sh" "$BIN_PATH/"
chmod +x "$BIN_PATH/50_bring_wwan0_up.py"
chmod +x "$BIN_PATH/wwan-check.sh"
echo "  - Scripts installed to $BIN_PATH"

# --- Step 6: Configure APN in the Python script ---
echo "[6/8] Configuring APN..."
sed -i "s/^APN = .*/APN = \"$APN\"  # Your APN/" "$BIN_PATH/50_bring_wwan0_up.py"
echo "  - APN set to: $APN"

# --- Step 7: Install systemd timer and service ---
echo "[7/8] Installing systemd timer and service..."
cp "$FILES_PATH/wwan-check.timer" "$SYSTEMD_PATH/"
cp "$FILES_PATH/wwan-check.service" "$SYSTEMD_PATH/"
systemctl daemon-reload
systemctl enable wwan-check.timer
echo "  - wwan-check.timer enabled"

# --- Step 8: Start the timer ---
echo "[8/8] Starting WWAN check timer..."
systemctl start wwan-check.timer
echo "  - Timer started"

# --- Summary ---
echo ""
echo "=== WWAN Setup Complete ==="
echo ""
echo "Configuration summary:"
echo "  - Network config: $NETWORK_PATH/20-wwan0.network"
echo "  - Connection script: $BIN_PATH/50_bring_wwan0_up.py"
echo "  - Wrapper script: $BIN_PATH/wwan-check.sh"
echo "  - Timer: wwan-check.timer (runs every 5 minutes)"
echo "  - APN: $APN"
echo ""
echo "Useful commands:"
echo "  - Check timer status: systemctl status wwan-check.timer"
echo "  - Check service logs: journalctl -t wwan-connect-all -f"
echo "  - Manual connection test: /usr/local/bin/wwan-check.sh"
echo "  - Modem status: mmcli -m 0"
echo ""
echo "A reboot is recommended to ensure all changes take effect."
