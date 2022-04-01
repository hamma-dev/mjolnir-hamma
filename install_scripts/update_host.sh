#!/bin/bash
# Update host name for HAMMA Pi

# Make sure we have an argument

if [[ $# -ne 1 ]]; then
    echo "Pass the sensor number"
    exit 1
fi

name="mjolnir"$(printf "%.2d" "$1")

# (Re)Write the hostname file
file_hostname="/etc/hostname"

sudo tee $file_hostname <<< $name > /dev/null

# Now, the hosts file. A little more tricky, since we only modify one line.
# Fun with sed!

file_hosts="/etc/hosts"

sudo sed -i "s%127.0.1.1.*%127.0.1.1       $name%" "$file_hosts"
