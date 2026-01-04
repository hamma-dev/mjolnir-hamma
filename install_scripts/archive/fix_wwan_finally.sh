#!/bin/bash

# Script to apply the final cellular connection fixes, assuming starting
# state is similar to the 'HAMMA-Cellular Fixes-231025-211449.pdf' document.

# --- Configuration ---
SCRIPT_DIR="$( cd "$( dirname "\${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PYTHON_SCRIPT_SRC="\$SCRIPT_DIR/50_bring_wwan0_up.py"
WRAPPER_SCRIPT_SRC="\$SCRIPT_DIR/wwan-check.sh"

PYTHON_SCRIPT_DEST="/usr/local/bin/50_bring_wwan0_up.py"
WRAPPER_SCRIPT_DEST="/usr/local/bin/wwan-check.sh"

TIMER_FILE_DEST="/etc/systemd/system/wwan-check.timer"
SERVICE_FILE_DEST="/etc/systemd/system/wwan-check.service"

# Exit on any error
set -e

echo "Applying cellular connection fixes..."

# --- 1. Stop and Disable Conflicting/Obsolete Services ---
echo "Disabling conflicting services (wwan-connect, dhcpcd)..."
sudo systemctl disable --now wwan-connect.service || echo "wwan-connect.service not found or already disabled."
sudo systemctl disable --now dhcpcd.service || echo "dhcpcd.service not found or already disabled."

# --- 2. Remove Obsolete networkd-dispatcher Config ---
echo "Removing obsolete networkd-dispatcher configuration..."
sudo rm -f /etc/networkd-dispatcher/carrier.d/50_bring_wwan0_up.py || echo "networkd-dispatcher carrier link not found."
sudo rm -f /etc/networkd-dispatcher/degraded.d/50_bring_wwan0_up.py || echo "networkd-dispatcher degraded link not found."
sudo rm -rf /etc/systemd/system/networkd-dispatcher.service.d/ || echo "networkd-dispatcher override not found."

# --- 3. Copy Updated Scripts ---
echo "Copying updated scripts..."
if [ ! -f "\$PYTHON_SCRIPT_SRC" ]; then
    echo "ERROR: Source file \$PYTHON_SCRIPT_SRC not found!"
    exit 1
fi
if [ ! -f "\$WRAPPER_SCRIPT_SRC" ]; then
    echo "ERROR: Source file \$WRAPPER_SCRIPT_SRC not found!"
    exit 1
fi
sudo cp "\$PYTHON_SCRIPT_SRC" "\$PYTHON_SCRIPT_DEST"
sudo cp "\$WRAPPER_SCRIPT_SRC" "\$WRAPPER_SCRIPT_DEST"
sudo chmod +x "\$PYTHON_SCRIPT_DEST"
sudo chmod +x "\$WRAPPER_SCRIPT_DEST"

# --- 4. Create/Overwrite systemd Timer and Service Files ---
echo "Creating/overwriting systemd timer and service files..."

# Timer file content
sudo tee "\$TIMER_FILE_DEST" > /dev/null << EOF
[Unit]
Description=Run WWAN connectivity check periodically

[Timer]
# Run 30 seconds after boot
OnBootSec=30s
# Run every 5 minutes thereafter
OnUnitActiveSec=5min
AccuracySec=1s

[Install]
WantedBy=timers.target
EOF

# Service file content
sudo tee "\$SERVICE_FILE_DEST" > /dev/null << EOF
[Unit]
Description=WWAN Connectivity Check Service (via flock wrapper)
# Dependencies to ensure ModemManager is ready
Requires=ModemManager.service
After=ModemManager.service

[Service]
Type=oneshot
# Execute the flock wrapper script
ExecStart=$WRAPPER_SCRIPT_DEST
EOF

# --- 5. Verify networkd Config (Informational) ---
# Ensure wwan0 is unmanaged. This script doesn't change it but warns if missing.
NETWORKD_WWAN_CONF="/etc/systemd/network/20-wwan0.network"
if [ -f "\$NETWORKD_WWAN_CONF" ]; then
    if grep -q "Unmanaged=yes" "\$NETWORKD_WWAN_CONF"; then
        echo "Verified Unmanaged=yes in \$NETWORKD_WWAN_CONF."
    else
        echo "WARNING: Unmanaged=yes not found in \$NETWORKD_WWAN_CONF. This script relies on wwan0 being unmanaged."
    fi
else
    echo "WARNING: \$NETWORKD_WWAN_CONF not found. Ensure wwan0 is configured as Unmanaged=yes."
fi

# --- 6. Reload systemd and Enable Timer ---
echo "Reloading systemd daemon and enabling timer..."
sudo systemctl daemon-reload
sudo systemctl enable --now "\$(basename \$TIMER_FILE_DEST)" # Enable and start the timer

echo "Cellular fixes applied successfully."
echo "It is recommended to reboot the system."

exit 0
