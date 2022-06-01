#!/bin/bash
# Install sindri and related software

if [[ $# -ne 1 ]]; then
    echo "Pass the sensor number"
    exit 1
fi

name="hamma"$1  # This will be used for the server push location

# We need to modify a file (for now). Set up the variables for that
LEKTOR_FILE="/home/pi/dev/sindri/src/sindri/website/mjolnir-website/mjolnir-website.lektorproject"
LEKTOR_KEY="target = rsync://pi@hamma.dev/var/www/hamma.dev/public_html/"

INSTALL_PATH="/home/pi/dev/"  # Trailing slash, please
VENV_NAME="sindrienv"


# Now, we're going to set up the environment

python3 -m venv $INSTALL_PATH$VENV_NAME

cp $INSTALL_PATH$VENV_NAME"/bin/activate" "/home/pi/$VENV_NAME"

source "/home/pi/$VENV_NAME"

# Note: not sure if this is needed
pip install --upgrade pip setuptools wheel

# Download the needed software
git -C $INSTALL_PATH clone --recursive "https://github.com/project-mjolnir/sindri.git"

# Now, we install
echo "********* Installing sindri. This might take a while..."
pip install -e $INSTALL_PATH"sindri"

# Get service installer. It should already be installed...
# TODO: test to see if it is
pip install -e $INSTALL_PATH"serviceinstaller"

# Modify a local file to reflect this sensor
sed -i "s%$LEKTOR_KEY.*%$LEKTOR_KEY$name%" $LEKTOR_FILE
