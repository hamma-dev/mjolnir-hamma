#!/bin/bash
# Turn off the radio for the Pi

CONFIG_FILE="/boot/config.txt"

sudo tee -a $CONFIG_FILE <<< "dtoverlay=disable-wifi" > /dev/null
