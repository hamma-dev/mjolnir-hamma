#!/bin/bash
# Setup hardware connections for HAMMA Pi
#
# This script configures:
#   1. Pi-to-Sensor SSH connection (eth1 network)
#   2. Automount for USB drives via polkit
#
# Usage:
#   sudo ./setup_hardware.sh

set -e

# --- Configuration ---
FILES_PATH="/home/pi/dev/mjolnir-hamma/files"
NETWORK_PATH="/etc/systemd/network"
POLKIT_PATH="/etc/polkit-1/localauthority/50-local.d"
SSH_PATH="/home/pi/.ssh"

echo "=== Hardware Setup ==="
echo ""

# --- Check root ---
if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (use sudo)"
    exit 1
fi

# --- Step 1: Setup sensor connection ---
echo "[1/3] Setting up Pi-to-Sensor connection..."

# Copy SSH config for connecting to sensor
mkdir -p "$SSH_PATH"
cp "$FILES_PATH/config" "$SSH_PATH/"
chown pi:pi "$SSH_PATH/config"
chmod 600 "$SSH_PATH/config"
echo "  SSH config installed"

# Copy network files for eth0 (external) and eth1 (sensor)
cp "$FILES_PATH/40-eth0.network" "$NETWORK_PATH/"
cp "$FILES_PATH/30-eth1.network" "$NETWORK_PATH/"
echo "  Network configs installed"

# --- Step 2: Setup automount ---
echo "[2/3] Setting up USB drive automount..."

# Create polkit directory if needed
mkdir -p "$POLKIT_PATH"

# Copy mount rules
if [[ -f "$FILES_PATH/mount-udisks.pkla" ]]; then
    cp "$FILES_PATH/mount-udisks.pkla" "$POLKIT_PATH/"
    chown root:root "$POLKIT_PATH/mount-udisks.pkla"
    chmod 644 "$POLKIT_PATH/mount-udisks.pkla"
    echo "  Polkit rules installed"
else
    echo "  WARNING: mount-udisks.pkla not found, skipping automount setup"
fi

# --- Step 3: Restart networkd ---
echo "[3/3] Restarting systemd-networkd..."
systemctl restart systemd-networkd || echo "  Note: networkd restart may require reboot"

# --- Summary ---
echo ""
echo "=== Hardware Setup Complete ==="
echo ""
echo "Sensor connection:"
echo "  - SSH config: $SSH_PATH/config"
echo "  - Sensor IP: 192.168.1.1 (eth1)"
echo "  - Test with: ssh hamma (when sensor connected)"
echo ""
echo "USB automount:"
echo "  - Drives mount automatically to /media/pi/"
echo "  - Test with: udisksctl mount --no-user-interaction -b /dev/disk/by-label/DATAXX"
echo ""
