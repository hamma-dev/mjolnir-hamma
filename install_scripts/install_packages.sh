#!/bin/bash
# Install apt packages for HAMMA Pi

set -e

# --- Fix Buster EOL repos (deb.debian.org no longer serves Buster) ---
if grep -q "deb.debian.org" /etc/apt/sources.list 2>/dev/null; then
    echo "Fixing EOL Debian Buster repositories..."
    sed -i 's|deb.debian.org/debian |archive.debian.org/debian |g' /etc/apt/sources.list
    sed -i 's|deb.debian.org/debian-security |archive.debian.org/debian-security |g' /etc/apt/sources.list
    # Remove buster-updates (no longer exists in archive)
    sed -i '/buster-updates/d' /etc/apt/sources.list
    echo "  Repositories updated to archive.debian.org"
fi

# First, get a fresh state
apt-get update -y
apt-get dist-upgrade -y
apt-get autoremove -y

# Core utilities
apt-get install -y imagemagick    # Needed for sindri
c          # Automount drives
apt-get install -y udisks2        # Automount drives

# Development tools (should already be on image, but ensure present)
apt-get install -y python3-venv
apt-get install -y git
apt-get install -y build-essential python3-dev gfortran

# Networking
apt-get install -y networkd-dispatcher
apt-get install -y modemmanager   # Cell modem management

# WWAN/Cellular connectivity (required for setup_wwan.sh)
apt-get install -y udhcpc         # Lightweight DHCP client for wwan0
apt-get install -y libqmi-utils   # QMI modem utilities




