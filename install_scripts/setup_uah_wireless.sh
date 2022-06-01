#!/bin/bash
# Set wireless for NSSTC/SWIRLL

# Make sure we have an argument

if [[ $# -ne 1 ]]; then
    echo "Pass the sensor number"
    exit 1
fi

USB_PATH="/mnt/usb/"  # Trailing slash, please
CERT_PATH="/home/pi/.nsstc/"

name="mjolnir"$(printf "%.2d" "$1")


# First, copy over the certificate:
cp $USB_PATH"NSSTC-UAH-WIRELESS-$name.p12" $CERT_PATH
 
# Copy over the override file
sudo cp $USB_PATH"mjolnir-hamma/files/override.conf" /etc/systemd/system/wpa_supplicant@wlan0.service.d/

# Change some permissions
sudo chmod 0755 /etc/systemd/system/wpa_supplicant@wlan0.service.d/
sudo chmod 0644 /etc/systemd/system/wpa_supplicant@wlan0.service.d/override.conf

# Copy over network file
sudo cp $USB_PATH"mjolnir-hamma/files/10-wlan0.network" /etc/systemd/network/

# Change the hostname
sudo sed -i "s%^Hostname=.*%Hostname=$name%" /etc/systemd/network/10-wlan0.network

# Set the permissions
sudo chmod 0644 /etc/systemd/network/10-wlan0.network

# Now, the config file
sudo cp $USB_PATH"/mjolnir-hamma/files/wpa_supplicant-wlan0.conf" /etc/wpa_supplicant/

# Update the config file with the correct path to the certificate
sudo sed -i "s%private_key=.*%private_key=\""$CERT_PATH"NSSTC-UAH-WIRELESS-$name.p12"\""%" "/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"

echo "***** Don't forget to update private key password at /etc/wpa_supplicant/wpa_supplicant-wlan0.conf!"


# Enable daemons and make sure resolv.conf is linked properly
sudo rm -f /etc/resolv.conf
sudo ln -s /run/systemd/resolve/resolv.conf /etc/
sudo systemctl daemon-reload
sudo systemctl enable wpa_supplicant@wlan0.service
sudo systemctl enable systemd-networkd.service
sudo systemctl restart systemd-networkd.service
sudo systemctl restart wpa_supplicant@wlan0.service

ssh-keygen

