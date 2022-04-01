#!/bin/bash
# Setup brokkr and related software

if [[ $# -ne 1 ]]; then
    echo "Pass the sensor number"
    exit 1
fi

number=$(printf "%.2d" "$1")

INSTALL_PATH="/home/pi/dev/"  # Trailing slash, please
VENV_NAME="ltgenv"

# Make sure everything is updated
sudo apt update
sudo apt full-upgrade
sudo apt autoremove

# Start up the right python
source "/home/pi/$VENV_NAME"

brokkr configure-system hamma $INSTALL_PATH"mjolnir-hamma"

brokkr configure-unit $number

brokkr install-dependencies
sudo $INSTALL_PATH/$VENV_NAME"/bin/brokkr" install-all
