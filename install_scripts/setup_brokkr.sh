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
sudo apt-get update
sudo apt-get dist-upgrade
sudo apt-get autoremove

# Start up the right python
source "/home/pi/$VENV_NAME"

brokkr configure-system hamma $INSTALL_PATH"mjolnir-hamma"

brokkr configure-unit $number --site-description "Deployed site description - unit.toml"

brokkr install-dependencies
sudo $INSTALL_PATH/$VENV_NAME"/bin/brokkr" install-all
