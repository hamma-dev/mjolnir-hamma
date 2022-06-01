#!/bin/bash
# Install apt packages

# First, get a fresh state
apt-get update -y
apt-get dist-upgrade -y
apt-get autoremove -y

# Note: should we just apt all these for install?
apt-get install imagemagick  	# Needed for sindri (I think)
apt-get install eject        	# Automount drives
apt-get install udisks2			# Automount drives

# These should already be installed on the image...
apt-get install python3-venv
apt-get install git
apt-get install build-essential python3-dev gfortran
apt-get install networkd-dispatcher
apt-get install modemmanager




