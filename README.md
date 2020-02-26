# Mjolnir System Configuration Package for HAMMA

A system configuration package for Mjolnir, containing config data specific to the HAMMA lightning sensor network.



## Installation and Setup


### Fully-scripted clean install

1. On your machine with internet access, run brokkr_prepare.py: Gets latest version of system and brokkr and puts them in an output dir, e.g. SD card or flash drive
2. Eject, connect and mount flash drive, SD card, etc. to Pi.
3. Run bootstrap.py script in the system dir with sudo and follow the prompts


### Semi-automated clean install

1. Flash SD card with OS image
2. Do basic raspi-config/Fedora setup; change username, sudo timeout (?), load nano (?)
3. Create venv, install Brokkr and copy system config dir, NSSTC cert and private key file
4. Run ``brokkr configure-system <systempath>``
5. PHASE 1: Run ``brokkr install --phase 1`` to enable Internet
6. Update all packages to latest (``apt update && apt full-upgrade && apt autoremove``) and reinstall brokkr with all packages (``pip uninstall brokkr && pip install brokkr``)
7. PHASE 2: Run ``brokkr install --phase 2`` to install remaining items
8. Run ``brokkr setup-device`` which executes passwd, ssh-keygen, (brokkr setup-system: ssh-copy-id/test to proxy)
9. Create venv and install Sindri (script?)


### On-site setup

1. Run brokkr configure-unit <unit-number> <network-interface>
2. Run brokkr setup-unit: SSH-copy-id and test to sensor, hostname, redo autossh, enable brokkr service, test to proxy


### Phase 1 actions

* Enable: systemd-networkd
* Disable: networking, network (?), NetworkManager (?), dhcpcd, wpa_supplicant
* Files: networkd and wpa_supplicant config files
* Other: move interfaces to .bak


### Phase 2 actions

* Install: systemd-resolved, networkd-dispatcher, ModemManager
* Enable: systemd-timesyncd or htpdate
* Disable: hciuart
* Firewall: 8084 udp
* Files: Install other files in system
* Other: resolvconf.conf -> resolvconf=NO, disable audio (dtparam=audio=on) and bluetooth (dtoverlay=disable-bt) in config.txt
* Run builtin commands: autossh, brokkr service, udev, dialout


### Flashing the Pis

It should be very simple once I've prepared the image; you just use the open-source, cross-platform software balenaEtcher to flash it to each card or as many at once as you have card readers (I can do up to 4 at once with my setup).
Simply select the image, select the card and click Flash, and then just boot the Pi.

The remaining minimal setup will be automated by Brokkr commands (ensure the correct virtual environment is activated first with ``source ~/brokkrenv``):

1. After flashing the Pi, run brokkr setup-device to regenerate the Pi’s password, hash and ssh keys, and to register and test connectivity with the proxy server. You’ll need to enter the Pi’s current and desired password at the interactive prompt.
2. Once a specific unit number is assigned to a Pi, or on site, run brokkr configure-unit <unit-number> <network-interface> to set the unit number, connection mode, and other unit-specific details.
3. Finally, on site, once the final unit configuration is set (or after it is changed in the future), run brokkr setup-unit to automatically perform the necessary unit-specific steps, including registering the Pi with the sensor and testing the connection, setting the hostname, installing the autossh service with the correct target port number, testing connectivity with proxy, and enabling the autossh and brokkr services for autostart



## General Usage

Brokkr will set the time and start logging automatically upon boot and write the data into CSVs for each day, and it will restart automatically if it crashes or the power is cut, so you don't need to actually do anything to get it to work.
Its running as a Systemd service unit you can start, stop, enable, disable, and check the status of it via usual systemctl commands, though you shouldn't need to.

Once you're logged in, you can check the status of the monitoring client with systemctl status brokkr, and likewise enable/disable and start/stop it.
systemctl status brokkr will also show you the latest log output indicating any errors, warnings or other important information about the service, sensor and charge controller.
Full log output can be viewed with journalctl -u brokkr and is also logged to a text file under ``~/brokkr/log``.

The ping, charge controller and H&S data itself is stored in daily CSVs (though tuned to be nearly fixed width) under ~/data/monitoring, and can be easily viewed via cat, less, tail, etc. and pulled back individually or en masse via rsync (recommended), sftp or (legacy) scp.
Data logging will automatically recover on reboot or any other disruption (unplugged charge controller or sensor, etc), so no need to worry about that.

The primary git repo containing the code, Readme, Release Guide, Changelog, etc. can be viewed under ~/dev/brokkr (as well as on my Github), and the brokkr module itself is a fully PyPI-ready package installed under the venv in that directory.
Once activated (``source ~/brokkrenv``) you can import it into any Python interpreter, or run the main entry point by simply typing brokkr and the desired subcommand (of which there are numerous, mostly for installing/configuring various features), or ``--help`` for detailed help, ``--version`` for version, etc.

All config files, including for both the main functionality as well as the logging system, are stored in clean but human-readable TOML format (similar to INI, but more modern) under the ~/.config/brokkr directory per the XDG spec, in a specific hierarchy to make it relatively easy to configure them remotely, all at once, with a much higher level interface running on the server or individual user laptops that can communicate with it.
To edit the local config for testing, use the files labeled ``*_local.toml``.


### SSHing into the Pi and sensor

Locally, first time:
1. Place the SSH config file (see appendix) in your ~/.ssh/ directory
2. If you don't already have a SSH private key, run ``ssh-keygen`` to generate one
3. Plug into the Ethernet jack to the right of my workstation labeled "Not Ethernet, Roof Platform"
4. On your machine’s Ethernet interface, set a static IP of 192.168.1.NNN/24(where NNN > 1, <255 and /= 2 or 102)
5. Execute ``ssh-copy-id mjolnir`` and enter the Pi's password when prompted to register your key with the Pi.
6. SSH into the Pi with ``ssh mjolnir``

Locally, subsequent times:
1. Plug into the Ethernet jack to the right of my workstation labeled "Not Ethernet, Roof Platform"
2. On your machine’s Ethernet interface, set a static IP of 192.168.1.NNN/24 (where NNN > 1, <255 and /= 2 or 102)
3. SSH into the Pi with ``ssh mjolnir``

Remotely, first time:
1. Place the SSH config file in your ~/.ssh/ directory
2. If you don't already have a SSH private key, run ``ssh-keygen`` to generate one
3. Connect to the UAH network, VPN or other trusted IP
4. Execute ``ssh-copy-id proxy`` and enter the proxy account's password when prompted to register your key with the proxy server.
5. Execute ``ssh-copy-id mjolnir0`` and enter the Pi's password when prompted to register your key with the Pi.
6. Execute ``ssh-copy-id hamma0`` and enter the sensor's password to register your key with the sensor.

Remotely, subsequent times:
1. Connect to the UAH network, VPN or other trusted IP
2. Run ``ssh mjolnir0``

Then, you can do any of the following:
* Browse the files (in ~/data/monitoring/)
* Manage data logging (with systemctl)
* SSH into the sensor itself (ssh hamma)
* Run Brokkr commands (source ~/brokkerenv, then brokkr --help for a list)


### Getting Current Status Data

After activating the appropriate virtual environment with ``source ~/brokkerenv``, run the new ``brokkr status`` command to get a snapshot of the monitoring data, and the ``brokkr monitor`` command to get a pretty-printed display of all the main monitoring variables, updated in real time (1s) as you watch (it'll be prettier once I finish the common config implementation so it can add units, conversions and nicer names, but that'll come later).

Retrieving logged data from the Pi:
1. Do steps 1 and 2 above (no need to actually SSH)
2. From your machine, SCP one, specific or all the files as follows (. for the second arg will store them in the current directory; you can specify another dir instead as desired):

* Downlink all the files: ``scp mjolnir0:data/monitoring/* .``
* Downlink a specific file: ``scp mjolnir0:data/monitoring/hamma15_2019-MM-DD.csv .`` (replace date with desired one)
* Downlink all files matching a pattern using the * wildcard: ``scp mjolnir0:data/monitoring/hamma15_2019-08-*.csv .``

Retrieving HAMMA data from the sensor:
I've set it up so you can pull data from the lab without having to pull the drive or take the sensor offline.
So long as the link is working at gigabit speeds, it should only take about 10 seconds per HAMMA file:
1. Do steps 1 and 2 as before
2. From _your machine_ run ``scp hamma0:/home/data/HAMMA_FILENAME.bin .``, e.g. to get all trigger files started in a specific hour, you can do ``scp hamma0:/home/data/hamma15_2019-MM-DD_HH*.bin .`` replacing ``MM-DD HH`` with the desired hour.
3. Enter the Pi's password, then the sensor's password when prompted.


### Hard drive notes

Picking an appropriate universal format for the >2 TB HDDs so they'd work reliably on Linux, Windows and Mac, and figuring out the best way to get them and the logging flash drive all automount to the right places was non-trivial.
I seriously considered ext4, NTFS, UFS and exFAT and looking into probably a dozen different possible options for automounting (complicated by the need to work headless and with arbitrary UUIDs, labels and mount points).

For now I've settled on FAT32 w/larger cluster sizes (which will need to either be done on non-Windows platforms or with a third party tool), and just doing the mounting in the Python code initialization.
We’ll need to name the drives consistently so the software can reliably find them and not dump data into any arbitrary mass storage device that may be inserted; to match the current ones they can be named DATANN, where NN 00-99.

Important note: Given drive selection only occurs on initialization (to avoid unacceptable overhead) and to avoid hardcoding udev rules, as well as for write safety anyway, the Brokkr client service should be cleanly stopped before swapping drives (or the Pi safety powered off).



## Working with the HAMMA sensor


### Sending sensor control commands

You can use the ags.py script to send commands to an attached sensor on any machine. It is implemented in pure Python, and thus will run on any system including Windows and Mac with no extra work (so you can use it if you connect your own machine directly to the sensor); it also fixes the timeout issue with netcat, is less to type and remember and has a cleaner API.

Simply run the following:

```bash
~/ags.py  # Print the sensor's commands, equivalent to ~/ags.py help
~/ags.py <command>  # Send a command with no args
~/ags.py "<command> <args>"  # Send a command with args
```

As usual, ~/ags.py --help will get help on the script itself.

The legacy method using netcat is as follows (you’ll need to be on a *nix system with Netcat installed; the above only requires Python):

```bash
echo "<command> <args>" | nc 10.10.10.1 8082
```

Then you’ll have to Ctrl-C to exit.



## Working with the Charge Controller

All charge controller functionality can be viewed and managed through the ``~/sunsaver.py`` script.

To see what you can do, simply run:

```bash
~/sunsaver.py --help
```


### Downloading Data from Charge Controller

Now that the Pi logs data, this shouldn’t be necessary for any sensors with it attached since the log is far more comprehensive, and can be accessed right on the hamma website.

However, if you do need to pull the charge controller’s own log, you can do so right from the Pi or any machine with Brokkr installed. Simply run any of the following:

```bash
~/sunsaver.py log --help  # Get help on sunsaver log command
~/sunsaver.py log  # Print formatted log output to your terminal
~/sunsaver.py log --output-path path/to/output.csv  # Write data to a CSV
```

You can easily pull back the CSV your machine with SFTP, rsync or scp.


### Commanding the charge controller load on and off

IMPORTANT NOTE: If the Pi is connected directly to the charge controller load outputs with no redundant power supply, if you shut off load power, you’ll need physical access to restore it.

You can use the ``~/sunsaver.py`` script to control load power, among other things.

On the Pi (or any machine with the script connected to the MPPT), just run any of the following:

```bash
~/sunsaver.py power --help  # Get help on sunsaver load command
~/sunsaver.py power  # Check current status
~/sunsaver.py power off  # Or on
```

The status LED on the charge controller will be solid red just like a LVD, but the fact that you've manually disabled it will show up explicitly in the data shown on the website, with a load state of 5 (disconnect) instead of 3 (LVD), so its clear it is intentional and not some unexpected problem.

For more information on the various fault, alarm and status indications shown, please consult the Sunsaver Modbus documentation available in the Google Drive.


### Programming the charge controller

IMPORTANT NOTE: If the Pi is connected directly to the charge controller load outputs with no redundant power supply, as soon as you change any EEPROM values (0xE0XX), it will shut the load off and require a site visit to restore, as the Pi will not be able to send the reset command.

You can use the ~/sunsaver.py script to program the charge controller’s EEPROM, as well as send commands to the coils and read RAM, ROM and coil values.

On the Pi (or any machine with the script connected to the MPPT), run any of the following:

```bash
~/sunsaver.py {register, coil} --help  # Get help on register/coil command
~/sunsaver.py {register, coil} <address>  # Get the current value of a register/coil by its hex address, e.g. 0x0011
~/sunsaver.py {register, coil} <address> <value>  # Set a register/coil to a (integer or bool) value, e.g. 3440 (LVD of 10.5 V)
~/sunsaver.py reset  # Reset the MPPT to clear faults and load new values
```

If EEPROM (any value in the 0xE0XX range) is changed, the charge controller battery level lights will blink in sequance, the error light will blink on and off, and both charging and the load will shut off.
You’ll need to issue the reset command to clear this fault, restart the MPPT and load the new values from EEPROM into RAM.

For addresses, conversions, and extensive information on the various RAM and ROM registers and read and write coils, consult the Sunsaver Modbus documentation on the Google Drive.
