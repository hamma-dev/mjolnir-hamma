#!/bin/bash
# Setup connection from Pi to sensor

# The path to put where the files can be found
FILES_PATH="/home/pi/dev/mjolnir-hamma/files/"
# The path to where the network files should go
NETWORK_PATH="/etc/systemd/network/"

sudo cp $FILES_PATH"config" "/home/pi/.ssh/"
sudo cp "$FILES_PATH"*eth*network $NETWORK_PATH


