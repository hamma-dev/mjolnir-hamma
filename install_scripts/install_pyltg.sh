#!/bin/bash
# Install Pyltg and related software

INSTALL_PATH="/home/pi/dev/"  # Trailing slash, please
VENV_NAME="ltgenv"

# TODO: make sure environment exists
source "/home/pi/$VENV_NAME"

# Before we begin installing, make sure we have up to date copies
# pip install --upgrade pip setuptools wheel  # NOTE: This should already be done

# Before installing PyLtg, we need some dependencies that don't get
# installed via normal means (partly, mostly because we use pip)

sudo apt-get update
sudo apt-get --yes install libhdf5-dev
sudo apt-get --yes install libnetcdf-dev
sudo apt-get --yes install proj-bin libproj-dev libgeos-dev

# There’s a conflict between the latest cartopy (v0.20), which is needed for pyltg, 
# and the geos available on Raspberry OS. Cartopy needs a newer version of geos than 
# we can get via apt, so we have to manually install an older version of cartopy 
# before pyltg.
pip install cartopy==0.19.0.post1

# Download the needed software
git -C $INSTALL_PATH clone "https://github.com/pbitzer/pyltg"

# Now, we install
pip install -e $INSTALL_PATH"pyltg"

