#!/bin/bash
# Install brokkr and related software

INSTALL_PATH="/home/pi/dev/"  # Trailing slash, please
VENV_NAME="ltgenv"

# Set up the environment
python3 -m venv $INSTALL_PATH$VENV_NAME
cp $INSTALL_PATH$VENV_NAME"/bin/activate" "/home/pi/$VENV_NAME"

source "/home/pi/$VENV_NAME"

# Before we begin installing, make sure we have up to date copies
pip install --upgrade pip setuptools wheel

# Download the needed software
git -C $INSTALL_PATH clone "https://github.com/project-mjolnir/brokkr.git"
git -C $INSTALL_PATH clone --recursive "https://github.com/hamma-dev/mjolnir-hamma"
git -C $INSTALL_PATH clone "https://github.com/project-mjolnir/serviceinstaller.git"
git -C $INSTALL_PATH clone "https://github.com/pbitzer/notifiers.git"

# Now, we install
pip install -e $INSTALL_PATH"brokkr"
pip install -e $INSTALL_PATH"serviceinstaller"
pip install -e $INSTALL_PATH"notifiers"
# Note: we don't install mjolnir-hamma

# Install packages for controlling the GPIO pins
pip install gpiozero
pip install RPi.GPIO
