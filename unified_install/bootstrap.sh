#!/bin/bash
# Bootstrap script for HAMMA Pi initial setup
#
# This script handles Phase 1 from the "Pi Setup [Working]" documentation:
#   1. Change default password
#   2. Set timezone to UTC
#   3. Setup temporary WiFi for connectivity (external USB antenna)
#   4. Mount USB drive
#   5. Copy repository from USB to /home/pi/dev/
#   6. Disable internal WiFi radio
#   7. Set hostname (mjolnirNN format)
#
# Usage:
#   ./bootstrap.sh <sensor_number> --wifi-ssid "NetworkName" [options]
#   ./bootstrap.sh <sensor_number> --no-wifi [options]
#
# Options:
#   --wifi-ssid SSID   WiFi network name (will prompt for password)
#   --wifi-pass PASS   WiFi password (optional, will prompt if not given)
#   --no-wifi          Skip temp WiFi setup (rare - for pre-configured networks)
#   --dry-run          Show what would be done without executing
#
# After running, reboot is required before proceeding.

set -e

# --- Get script directory ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Source common library ---
source "$SCRIPT_DIR/lib/common.sh"

# --- Parse arguments ---
SENSOR_NUM=""
DRY_RUN=false
WIFI_SSID=""
WIFI_PASS=""
NO_WIFI=false

print_usage() {
    echo "Usage: $0 <sensor_number> --wifi-ssid SSID [options]"
    echo ""
    echo "Arguments:"
    echo "  sensor_number       The sensor number (1-99)"
    echo ""
    echo "Options:"
    echo "  --wifi-ssid SSID    WiFi network name (required unless --no-wifi)"
    echo "  --wifi-pass PASS    WiFi password (will prompt if not given)"
    echo "  --no-wifi           Skip temp WiFi setup"
    echo "  --dry-run           Show what would be done without executing"
    echo "  -h, --help          Show this help"
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --wifi-ssid)
            WIFI_SSID="$2"
            shift 2
            ;;
        --wifi-pass)
            WIFI_PASS="$2"
            shift 2
            ;;
        --no-wifi)
            NO_WIFI=true
            shift
            ;;
        -h|--help)
            print_usage
            exit 0
            ;;
        -*)
            log_error "Unknown option: $1"
            print_usage
            exit 1
            ;;
        *)
            if [[ -z "$SENSOR_NUM" ]]; then
                SENSOR_NUM="$1"
            else
                log_error "Unexpected argument: $1"
                print_usage
                exit 1
            fi
            shift
            ;;
    esac
done

# --- Validate arguments ---
if ! validate_sensor_num "$SENSOR_NUM"; then
    print_usage
    exit 1
fi

# Require either --wifi-ssid or --no-wifi
if [[ -z "$WIFI_SSID" && "$NO_WIFI" != "true" ]]; then
    log_error "Either --wifi-ssid or --no-wifi is required"
    print_usage
    exit 1
fi

SENSOR_FORMATTED=$(format_sensor_num "$SENSOR_NUM")
HOSTNAME="mjolnir$SENSOR_FORMATTED"

# --- Initialize ---
init_common $(if [[ "$DRY_RUN" == "true" ]]; then echo "--dry-run"; fi)

log_info "=== HAMMA Pi Bootstrap ==="
log_info "Sensor: $HOSTNAME"
log_info "Dry run: $DRY_RUN"
if [[ -n "$WIFI_SSID" ]]; then
    log_info "Temp WiFi: $WIFI_SSID"
else
    log_info "Temp WiFi: (skipped)"
fi
echo ""

# --- Configuration ---
USB_MOUNT="/mnt/usb"
INSTALL_PATH="/home/pi/dev"
REPO_NAME="mjolnir-hamma"
FILES_PATH="$SCRIPT_DIR/../files"
CONFIG_FILE="/boot/config.txt"
# On newer Raspberry Pi OS, it might be /boot/firmware/config.txt
if [[ -f "/boot/firmware/config.txt" ]]; then
    CONFIG_FILE="/boot/firmware/config.txt"
fi

# --- Step 0a: Fix system clock if wrong ---
# Pi has no RTC; if clock is years off, DNSSEC fails and NTP can't sync.
# Use USB file timestamp as approximate current time.
CURRENT_YEAR=$(date +%Y)
if [[ "$CURRENT_YEAR" -lt 2024 ]]; then
    log_step "[0/7] Fixing system clock (was $CURRENT_YEAR)..."
    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "Set system clock from USB file timestamp"
        manifest_add "command" "cmd" "date -s @<usb_timestamp>" "sudo" "true"
    else
        # Get modification time of this script (recently copied to USB)
        if [[ "$(uname)" == "Linux" ]]; then
            USB_TIME=$(stat -c %Y "${BASH_SOURCE[0]}" 2>/dev/null)
        else
            USB_TIME=$(stat -f %m "${BASH_SOURCE[0]}" 2>/dev/null)
        fi
        if [[ -n "$USB_TIME" ]]; then
            sudo date -s "@$USB_TIME"
            log_success "Clock set to approximately: $(date)"
        else
            log_warn "Could not get USB timestamp, clock may be wrong"
        fi
    fi
    echo ""
fi

# --- Step 0b: Fix Buster EOL repos ---
# Debian Buster is EOL; deb.debian.org no longer serves it.
# This must happen before any apt commands.
if grep -q "deb.debian.org" /etc/apt/sources.list 2>/dev/null; then
    log_step "[0/7] Fixing EOL Debian Buster repositories..."
    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "sed sources.list: deb.debian.org -> archive.debian.org"
        manifest_add "sed" "path" "/etc/apt/sources.list" "pattern" "deb.debian.org" "replacement" "archive.debian.org"
    else
        # Replace all occurrences of deb.debian.org with archive.debian.org
        sudo sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list
        # Remove buster-updates (no longer exists)
        sudo sed -i '/buster-updates/d' /etc/apt/sources.list
        log_success "Repositories updated to archive.debian.org"
    fi
    echo ""
fi

# --- Step 1: Change default password ---
log_step "[1/7] Password setup..."

if [[ "$DRY_RUN" == "true" ]]; then
    log_dry_run "passwd (interactive)"
    manifest_add "command" "cmd" "passwd" "interactive" "true"
else
    read -p "  Change default password now? (Y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        passwd
        log_success "Password changed"
    else
        log_warn "Skipping password change (remember to change it later!)"
    fi
fi
echo ""

# --- Step 2: Set timezone to UTC ---
log_step "[2/7] Setting timezone to UTC..."

if [[ "$DRY_RUN" == "true" ]]; then
    log_dry_run "timedatectl set-timezone UTC"
    manifest_add "command" "cmd" "timedatectl set-timezone UTC" "sudo" "true"
else
    sudo timedatectl set-timezone UTC
    log_success "Timezone set to UTC"
fi

# --- Step 3: Setup temporary WiFi ---
log_step "[3/7] Setting up temporary WiFi..."

if [[ -n "$WIFI_SSID" ]]; then
    # Prompt for password if not provided
    if [[ -z "$WIFI_PASS" && "$DRY_RUN" != "true" ]]; then
        read -s -p "  Enter WiFi password for '$WIFI_SSID': " WIFI_PASS
        echo ""
        if [[ -z "$WIFI_PASS" ]]; then
            log_error "Password cannot be empty"
            exit 1
        fi
    fi

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "rfkill unblock wifi"
        log_dry_run "Create wpa_supplicant config with SSID: $WIFI_SSID"
        log_dry_run "Copy 10-wlan0.network"
        log_dry_run "Enable wpa_supplicant@wlan0.service"
        log_dry_run "Enable systemd-networkd"
        manifest_add "command" "cmd" "rfkill unblock wifi" "sudo" "true"
        manifest_add "write" "path" "/etc/wpa_supplicant/wpa_supplicant-wlan0.conf" "contains" "ssid=\"$WIFI_SSID\""
        manifest_add "copy" "src" "10-wlan0.network" "dst" "/etc/systemd/network/10-wlan0.network"
        manifest_add "symlink" "target" "/run/systemd/resolve/resolv.conf" "link" "/etc/resolv.conf"
        manifest_add "systemctl" "action" "enable" "service" "systemd-networkd"
        manifest_add "systemctl" "action" "enable" "service" "systemd-resolved"
        manifest_add "systemctl" "action" "enable" "service" "wpa_supplicant@wlan0.service"
    else
        # Unblock WiFi radio
        sudo rfkill unblock wifi

        # Create wpa_supplicant config
        sudo tee /etc/wpa_supplicant/wpa_supplicant-wlan0.conf > /dev/null << EOF
ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev
update_config=1
country=US

# Temporary network for install (added by bootstrap.sh)
network={
    ssid="$WIFI_SSID"
    psk="$WIFI_PASS"
    priority=10
}
EOF
        log_info "  WiFi config created"

        # Copy networkd config for wlan0
        if [[ -f "$FILES_PATH/10-wlan0.network" ]]; then
            sudo cp "$FILES_PATH/10-wlan0.network" /etc/systemd/network/
            sudo sed -i "s/mjolnirNN/$HOSTNAME/" /etc/systemd/network/10-wlan0.network
        else
            # Create minimal config if file not found
            sudo tee /etc/systemd/network/10-wlan0.network > /dev/null << EOF
[Match]
Name=wlan0

[Network]
DHCP=ipv4

[DHCP]
RouteMetric=10
EOF
        fi
        log_info "  Network config installed"

        # Enable and start networking services
        sudo systemctl enable systemd-networkd
        sudo systemctl enable systemd-resolved
        sudo systemctl start systemd-resolved || true
        sudo rm -f /etc/resolv.conf
        sudo ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
        sudo systemctl enable wpa_supplicant@wlan0.service
        sudo systemctl start wpa_supplicant@wlan0.service || true
        sudo systemctl restart systemd-networkd || true

        # Wait for connectivity
        log_info "  Waiting for WiFi connection..."
        ATTEMPTS=0
        MAX_ATTEMPTS=30
        while [[ $ATTEMPTS -lt $MAX_ATTEMPTS ]]; do
            if ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; then
                log_success "WiFi connected!"
                break
            fi
            ATTEMPTS=$((ATTEMPTS + 1))
            echo "    Attempt $ATTEMPTS/$MAX_ATTEMPTS..."
            sleep 2
        done

        if [[ $ATTEMPTS -ge $MAX_ATTEMPTS ]]; then
            log_warn "Could not verify WiFi connectivity"
            log_warn "Check SSID/password and USB antenna connection"
            read -p "  Continue anyway? (y/N): " -n 1 -r
            echo
            if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                exit 1
            fi
        fi
    fi
else
    log_info "Skipping temp WiFi (--no-wifi specified)"
    if [[ "$DRY_RUN" == "true" ]]; then
        manifest_add "skip" "step" "temp_wifi" "reason" "no-wifi flag"
    fi
fi

# --- Step 3: Mount USB if needed ---
log_step "[4/7] Checking USB mount..."

if [[ "$DRY_RUN" == "true" ]]; then
    log_dry_run "Check/mount USB at $USB_MOUNT"
    manifest_add "check" "type" "usb_mount" "path" "$USB_MOUNT"
else
    # Check if USB is mounted
    if ! mountpoint -q "$USB_MOUNT" 2>/dev/null; then
        # Try to create mount point and mount
        sudo mkdir -p "$USB_MOUNT"

        # Look for USB device
        USB_DEVICE=""
        for dev in /dev/sda1 /dev/sdb1; do
            if [[ -b "$dev" ]]; then
                USB_DEVICE="$dev"
                break
            fi
        done

        if [[ -z "$USB_DEVICE" ]]; then
            log_warn "No USB device found at /dev/sda1 or /dev/sdb1"
            log_info "If running from USB, this is normal - continuing..."
        else
            sudo mount "$USB_DEVICE" "$USB_MOUNT"
            log_success "Mounted $USB_DEVICE at $USB_MOUNT"
        fi
    else
        log_success "USB already mounted at $USB_MOUNT"
    fi
fi

# --- Step 4: Copy repository from USB ---
log_step "[5/7] Copying repository to $INSTALL_PATH..."

if [[ "$DRY_RUN" == "true" ]]; then
    log_dry_run "mkdir -p $INSTALL_PATH"
    log_dry_run "cp -r $USB_MOUNT/$REPO_NAME $INSTALL_PATH/"
    manifest_add "mkdir" "path" "$INSTALL_PATH"
    manifest_add "copy" "src" "$USB_MOUNT/$REPO_NAME" "dst" "$INSTALL_PATH/$REPO_NAME" "recursive" "true"
else
    # Create install directory if needed
    mkdir -p "$INSTALL_PATH"

    # Determine source path (USB or current script location)
    REPO_SOURCE=""
    if [[ -d "$USB_MOUNT/$REPO_NAME" ]]; then
        REPO_SOURCE="$USB_MOUNT/$REPO_NAME"
    elif [[ -d "$SCRIPT_DIR/../" ]] && [[ -f "$SCRIPT_DIR/../install_scripts/setup_brokkr.sh" ]]; then
        # We're running from within the repo already
        REPO_SOURCE="$(cd "$SCRIPT_DIR/.." && pwd)"
    else
        log_error "Cannot find $REPO_NAME on USB or in script location"
        exit 1
    fi

    if [[ -d "$INSTALL_PATH/$REPO_NAME" ]]; then
        log_warn "$REPO_NAME already exists at $INSTALL_PATH"
        log_info "Skipping copy (use 'git pull' to update)"
    else
        cp -r "$REPO_SOURCE" "$INSTALL_PATH/"
        log_success "Copied $REPO_NAME to $INSTALL_PATH"
    fi
fi

# --- Step 5: Disable internal WiFi radio ---
log_step "[6/7] Disabling internal WiFi radio..."

if [[ "$DRY_RUN" == "true" ]]; then
    log_dry_run "Append 'dtoverlay=disable-wifi' to $CONFIG_FILE"
    manifest_add "append" "path" "$CONFIG_FILE" "content" "dtoverlay=disable-wifi"
else
    # Check if already disabled
    if grep -q "^dtoverlay=disable-wifi" "$CONFIG_FILE" 2>/dev/null; then
        log_warn "Internal WiFi already disabled"
    else
        echo "dtoverlay=disable-wifi" | sudo tee -a "$CONFIG_FILE" > /dev/null
        log_success "Internal WiFi disabled in $CONFIG_FILE"
    fi
fi

# --- Step 6: Set hostname ---
log_step "[7/7] Setting hostname to $HOSTNAME..."

if [[ "$DRY_RUN" == "true" ]]; then
    log_dry_run "Write '$HOSTNAME' to /etc/hostname"
    log_dry_run "Update /etc/hosts with $HOSTNAME"
    manifest_add "write" "path" "/etc/hostname" "content" "$HOSTNAME"
    manifest_add "sed" "path" "/etc/hosts" "pattern" "127.0.1.1.*" "replacement" "127.0.1.1       $HOSTNAME"
else
    # Update /etc/hostname
    echo "$HOSTNAME" | sudo tee /etc/hostname > /dev/null

    # Update /etc/hosts
    sudo sed -i "s/127.0.1.1.*/127.0.1.1       $HOSTNAME/" /etc/hosts

    log_success "Hostname set to $HOSTNAME"
fi

# --- Finalize ---
if [[ "$DRY_RUN" == "true" ]]; then
    manifest_finalize
fi

echo ""
log_info "=== Bootstrap Complete ==="
echo ""

if [[ "$DRY_RUN" != "true" ]]; then
    log_info "Next steps:"
    echo "  1. Reboot the Pi: sudo reboot"
    echo "  2. After reboot, run install.sh for network and software setup"
    echo ""
    if [[ -n "$WIFI_SSID" ]]; then
        log_info "Temp WiFi '$WIFI_SSID' will reconnect after reboot"
    fi
    log_warn "IMPORTANT: A reboot is required before continuing!"
fi
