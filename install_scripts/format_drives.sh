#!/bin/bash
# Format drives for HAMMA sensors

# Parse the options
if [[ $# -ne 4 ]]; then
    echo "Pass both -m (mount location) and -n (number label) options"
    exit 1
fi

while [[ -n $1 ]]; do
	case $1 in
		-m) 
			mount_point=$2
			shift
			;;
		-n)
			num_label=$2
			shift
			;;
		--)
			break
			;;
		
	esac
	shift
	
done

# We are going to format two partitions
mount_point1=$mount_point"1"
mount_point2=$mount_point"2"

# Get the lables
num_label1="DATA"$(printf "%.2d" "$num_label")
num_label2="DATA"$(printf "%.2d" "$(( $num_label+1))")

# Format!
parted --script --align optimal $mount_point mklabel gpt mkpart $num_label1 fat32 "0%" "50%" mkpart $num_label2 fat32 "50%" "100%" && sudo mkfs.vfat -n "$num_label1" $mount_point1 && sudo mkfs.vfat -n "$num_label2" $mount_point2

eject /dev/sda1
eject /dev/sda2

echo "Formatted $num_label1 and $num_label2 and ejected. It's safe to remove the drive."
