#!/bin/bash
# Install and configure Brokkr for HAMMA Pi
#
# This script:
#   1. Creates Python virtual environment
#   2. Clones required repositories (brokkr, serviceinstaller, notifiers)
#   3. Installs packages into the venv
#   4. Configures Brokkr for the sensor
#   5. Installs Brokkr systemd services
#
# Usage:
#   ./setup_brokkr.sh <sensor_number>
#
# Note: This script should be run as the pi user (not root).
#       The install.sh driver handles this with sudo -u pi.

set -e

# Ensure HOME is set correctly (when run via sudo -u pi, HOME might still be /root)
export HOME=/home/pi

# Brokkr uses SUDO_USER to find config dir. When run via "sudo -u pi" from root,
# SUDO_USER=root, causing brokkr to look in /root/.config instead of /home/pi/.config.
# Unset it so brokkr uses HOME instead.
unset SUDO_USER

# Clean up any stale root config from previous failed runs
if [[ -d /root/.config/brokkr ]]; then
    sudo rm -rf /root/.config/brokkr
fi

# --- Check arguments ---
if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <sensor_number>"
    exit 1
fi

SENSOR_NUM=$(printf "%.2d" "$1")

# --- Configuration ---
INSTALL_PATH="/home/pi/dev"
VENV_NAME="ltgenv"
VENV_PATH="$INSTALL_PATH/$VENV_NAME"

echo "=== Brokkr Setup ==="
echo "Sensor: mjolnir$SENSOR_NUM"
echo ""

# --- Step 1: Create virtual environment ---
echo "[1/6] Creating Python virtual environment..."
if [[ ! -d "$VENV_PATH" ]]; then
    python3 -m venv "$VENV_PATH"
    ln -sf "$VENV_PATH/bin/activate" "/home/pi/$VENV_NAME"
    echo "  Created $VENV_PATH"
else
    echo "  Virtual environment already exists"
fi

# Activate the environment
source "$VENV_PATH/bin/activate"

# --- Step 2: Upgrade pip ---
echo "[2/6] Upgrading pip and setuptools..."
pip install --upgrade pip setuptools wheel

# --- Step 3: Clone repositories ---
echo "[3/6] Cloning/updating repositories..."

clone_or_update() {
    local repo_url=$1
    local repo_name=$2
    local repo_path="$INSTALL_PATH/$repo_name"

    if [[ -d "$repo_path" ]]; then
        echo "  $repo_name: already exists (run 'git pull' manually to update)"
    else
        echo "  $repo_name: cloning..."
        git -C "$INSTALL_PATH" clone "$repo_url"
    fi
}

clone_or_update "https://github.com/project-mjolnir/brokkr.git" "brokkr"
clone_or_update "https://github.com/project-mjolnir/serviceinstaller.git" "serviceinstaller"
clone_or_update "https://github.com/pbitzer/notifiers.git" "notifiers"

# Note: mjolnir-hamma should already exist from bootstrap.sh

# --- Step 4: Install Python packages ---
echo "[4/6] Installing Python packages..."
pip install -e "$INSTALL_PATH/brokkr"
pip install -e "$INSTALL_PATH/serviceinstaller"
pip install -e "$INSTALL_PATH/notifiers"

# GPIO packages for relay control
pip install gpiozero RPi.GPIO

echo "  Packages installed"

# --- Step 5: Configure Brokkr ---
echo "[5/6] Configuring Brokkr..."

brokkr configure-system hamma "$INSTALL_PATH/mjolnir-hamma"
brokkr configure-unit "$SENSOR_NUM" --site-description "Deployed site description - unit.toml"
brokkr install-dependencies

echo "  Brokkr configured for sensor $SENSOR_NUM"

# --- Step 6: Install services ---
echo "[6/6] Installing Brokkr services..."

# This needs elevated privileges, so use sudo
# Preserve HOME so brokkr reads pi user's config, not /root/.config
sudo HOME=/home/pi "$VENV_PATH/bin/brokkr" install-all

echo "  Services installed"

# --- Summary ---
echo ""
echo "=== Brokkr Setup Complete ==="
echo ""
echo "To verify:"
echo "  source /home/pi/$VENV_NAME"
echo "  brokkr status"
echo ""
echo "To start service:"
echo "  sudo systemctl start brokkr-hamma-default.service"
echo ""
