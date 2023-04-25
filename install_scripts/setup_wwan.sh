#!/bin/bash
# Set cell modem access (WWAN) 

# Path to the files
FILES_PATH="/home/pi/dev/mjolnir-hamma/files/"

# Paths to where the files should go
DEGRADED_PATH="/etc/networkd-dispatcher/degraded.d/"
NETWORK_PATH="/etc/systemd/network/"

WWAN_UP_FILE="50_bring_wwan0_up.py"
WWAN_CONFIG_FILE="20-wwan0.network"

# Install needed packages
apt-get install udhcpc libqmi-utils

cp $FILES_PATH$WWAN_UP_FILE $DEGRADED_PATH
cp $FILES_PATH$WWAN_CONFIG_FILE $NETWORK_PATH

# Set proper permissions
chgrp root $DEGRADED_PATH$WWAN_UP_FILE
chown root $DEGRADED_PATH$WWAN_UP_FILE
chmod a+x $DEGRADED_PATH$WWAN_UP_FILE
