#!/bin/bash
# Install apt packages

# First, get a fresh state
apt update -y
apt full-upgrade -y
apt autoremove -y

# Note: should we just apt all these for install?
apt-get install imagemagick  	# Needed for sindri (I think)
apt-get install eject        	# Automount drives
apt-get install udisks2			# Automount drives

# These should already be installed on the image...
apt-get install python3-venv
apt install git
apt install build-essential python3-dev gfortran
apt install networkd-dispatcher
apt install modemmanager




