#!/bin/bash
# Install HAMMA and related software

# Parse options
do_key=false
while [[ -n $1 ]]; do
	case $1 in
		-k) 
			do_key=true
			shift
			;;

		--)
			break
			;;
		
	esac
	shift
	
done

# Install the SSH key needed to download the private repo
if $do_key;
	then 

	ssh-keygen -f /home/pi/.ssh/id_ed25519 -t ed25519 -C "bitzerp@uah.edu" -N ''

	config_file="/home/pi/.ssh/config"
	sudo tee -a $config_file > /dev/null <<EOT
	
Host github-hamma
   HostName github.com 
   AddKeysToAgent yes 
   PreferredAuthentications publickey 
   IdentityFile /home/pi/.ssh/id_ed25519
EOT


else
	INSTALL_PATH="/home/pi/dev/"  # Trailing slash, please
	VENV_NAME="ltgenv"
	
	# TODO: make sure environment exists
	source "/home/pi/$VENV_NAME"
	
	# Download the needed software
	git -C $INSTALL_PATH clone git@github-hamma:pbitzer/hamma.git
	
	# Now, we install
	pip install -e $INSTALL_PATH"hamma"
	pip install future  # This seems to be a stealth requirement for lmfit, used by hamma
fi


