#!/bin/bash
# Enable automount of drives for Pi

# The path to put the mount rules in
POLKIT_PATH="/etc/polkit-1/localauthority/50-local.d/"
# The path to the file that contains the mount rules
MOUNT_PATH="/home/pi/dev/mjolnir-hamma/files/"
# The file name with the mount rules
MOUNT_FILE="mount-udisks.pkla"

# Check to see if directory exists
if [[ ! -d $POLKIT_PATH ]]
	then mkdir $POLKIT_PATH
fi

cp $MOUNT_PATH$MOUNT_FILE $POLKIT_PATH 

chown root $POLKIT_PATH$MOUNT_FILE
chmod 700 $POLKIT_PATH$MOUNT_FILE

