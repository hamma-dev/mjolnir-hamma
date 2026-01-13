#!/bin/bash
# Cellular/WWAN network setup for HAMMA Pi
#
# This script sets up cellular connectivity using a timer-based approach
# as documented in Confluence "Cellular Fixes" (Page 361332739).
#
# Key architecture:
#   - dhcpcd is disabled (conflicts with systemd-networkd)
#   - wwan0 is set to Unmanaged=yes in systemd-networkd
#   - Timer runs every 5 minutes to check/establish connection
#   - flock wrapper prevents concurrent connection attempts
#   - Python script handles all connection logic including zombie detection
#
# NOTE: SSH key (id_rsa) is generated for server access - same as WiFi path
#
# Requirements:
#   - common.sh must be sourced first
#   - SIM card installed in modem
#
# Functions:
#   setup_cellular_network <sensor_number> [--apn APN]

# --- Configuration ---
FILES_DIR="${FILES_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../files" && pwd 2>/dev/null || echo "/home/pi/dev/mjolnir-hamma/files")}"
SCRIPTS_DIR="${SCRIPTS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" && pwd 2>/dev/null || echo "/home/pi/dev/mjolnir-hamma/scripts")}"
NETWORK_PATH="/etc/systemd/network"
SYSTEMD_PATH="/etc/systemd/system"
BIN_PATH="/usr/local/bin"

# Default APN (T-Mobile)
DEFAULT_APN="h2g2"

# --- Main Setup Function ---
setup_cellular_network() {
    local sensor_num="$1"
    shift

    # Parse additional arguments
    local apn="$DEFAULT_APN"
    while [[ $# -gt 0 ]]; do
        case $1 in
            --apn)
                apn="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done

    if ! validate_sensor_num "$sensor_num"; then
        log_error "Invalid sensor number: $sensor_num"
        return 1
    fi

    local sensor_formatted=$(format_sensor_num "$sensor_num")
    local hostname="mjolnir$sensor_formatted"

    log_step "Setting up Cellular network for $hostname..."
    log_info "APN: $apn"
    echo ""

    # Check for root (most steps require sudo)
    if [[ "$DRY_RUN" != "true" && $EUID -ne 0 ]]; then
        log_warn "This script should be run with sudo for full functionality"
    fi

    # --- Step 1: Check/Install required packages ---
    log_step "[WWAN 1/9] Checking required packages..."

    local packages_needed=""
    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "Check for modemmanager, udhcpc, libqmi-utils"
        manifest_add "check" "type" "package" "packages" "modemmanager udhcpc libqmi-utils"
    else
        dpkg -l modemmanager >/dev/null 2>&1 || packages_needed="$packages_needed modemmanager"
        dpkg -l udhcpc >/dev/null 2>&1 || packages_needed="$packages_needed udhcpc"
        dpkg -l libqmi-utils >/dev/null 2>&1 || packages_needed="$packages_needed libqmi-utils"

        if [[ -n "$packages_needed" ]]; then
            log_info "Installing missing packages:$packages_needed"
            sudo apt-get update -y
            sudo apt-get install -y $packages_needed
            log_success "Packages installed"
        else
            log_success "All required packages already installed"
        fi
    fi

    # --- Step 2: Disable conflicting services ---
    log_step "[WWAN 2/9] Disabling conflicting services..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "systemctl stop/disable dhcpcd.service"
        log_dry_run "systemctl stop/disable wwan-connect.service"
        manifest_add "systemctl" "action" "disable" "service" "dhcpcd.service"
        manifest_add "systemctl" "action" "disable" "service" "wwan-connect.service"
    else
        sudo systemctl stop dhcpcd.service 2>/dev/null || true
        sudo systemctl disable dhcpcd.service 2>/dev/null || true
        log_info "  - dhcpcd.service disabled"

        sudo systemctl stop wwan-connect.service 2>/dev/null || true
        sudo systemctl disable wwan-connect.service 2>/dev/null || true
        log_info "  - wwan-connect.service disabled (if present)"
        log_success "Conflicting services disabled"
    fi

    # --- Step 3: Clean up old networkd-dispatcher scripts ---
    log_step "[WWAN 3/9] Removing old networkd-dispatcher scripts..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "Remove old dispatcher scripts from carrier.d and degraded.d"
        manifest_add "remove" "path" "/etc/networkd-dispatcher/carrier.d/50_bring_wwan0_up.py"
        manifest_add "remove" "path" "/etc/networkd-dispatcher/degraded.d/50_bring_wwan0_up.py"
        manifest_add "remove" "path" "/etc/systemd/system/networkd-dispatcher.service.d" "recursive" "true"
    else
        sudo rm -f /etc/networkd-dispatcher/carrier.d/50_bring_wwan0_up.py 2>/dev/null || true
        sudo rm -f /etc/networkd-dispatcher/degraded.d/50_bring_wwan0_up.py 2>/dev/null || true
        sudo rm -rf /etc/systemd/system/networkd-dispatcher.service.d/ 2>/dev/null || true
        log_success "Old dispatcher scripts removed"
    fi

    # --- Step 4: Copy network configuration files ---
    log_step "[WWAN 4/9] Installing network configuration files..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "cp $FILES_DIR/20-wwan0.network $NETWORK_PATH/"
        log_dry_run "cp $FILES_DIR/30-eth1.network $NETWORK_PATH/"
        manifest_add "copy" "src" "$FILES_DIR/20-wwan0.network" "dst" "$NETWORK_PATH/20-wwan0.network" "sudo" "true"
        manifest_add "copy" "src" "$FILES_DIR/30-eth1.network" "dst" "$NETWORK_PATH/30-eth1.network" "sudo" "true"
    else
        sudo cp "$FILES_DIR/20-wwan0.network" "$NETWORK_PATH/"
        sudo cp "$FILES_DIR/30-eth1.network" "$NETWORK_PATH/"
        log_success "Network configs installed"
    fi

    # --- Step 5: Copy WWAN scripts to /usr/local/bin ---
    log_step "[WWAN 5/9] Installing WWAN management scripts..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "cp $SCRIPTS_DIR/50_bring_wwan0_up.py $BIN_PATH/"
        log_dry_run "cp $FILES_DIR/wwan-check.sh $BIN_PATH/"
        log_dry_run "chmod +x both scripts"
        manifest_add "copy" "src" "$SCRIPTS_DIR/50_bring_wwan0_up.py" "dst" "$BIN_PATH/50_bring_wwan0_up.py" "sudo" "true"
        manifest_add "copy" "src" "$FILES_DIR/wwan-check.sh" "dst" "$BIN_PATH/wwan-check.sh" "sudo" "true"
        manifest_add "chmod" "path" "$BIN_PATH/50_bring_wwan0_up.py" "mode" "+x" "sudo" "true"
        manifest_add "chmod" "path" "$BIN_PATH/wwan-check.sh" "mode" "+x" "sudo" "true"
    else
        # Remove existing files to avoid "same file" errors
        sudo rm -f "$BIN_PATH/50_bring_wwan0_up.py" "$BIN_PATH/wwan-check.sh"
        sudo cp "$SCRIPTS_DIR/50_bring_wwan0_up.py" "$BIN_PATH/"
        sudo cp "$FILES_DIR/wwan-check.sh" "$BIN_PATH/"
        sudo chmod +x "$BIN_PATH/50_bring_wwan0_up.py"
        sudo chmod +x "$BIN_PATH/wwan-check.sh"
        log_success "Scripts installed to $BIN_PATH"
    fi

    # --- Step 6: Configure APN in the Python script ---
    log_step "[WWAN 6/9] Configuring APN..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "sed APN = \"$apn\" in $BIN_PATH/50_bring_wwan0_up.py"
        manifest_add "sed" "path" "$BIN_PATH/50_bring_wwan0_up.py" "pattern" "^APN = .*" "replacement" "APN = \"$apn\""
    else
        sudo sed -i "s/^APN = .*/APN = \"$apn\"  # Your APN/" "$BIN_PATH/50_bring_wwan0_up.py"
        log_success "APN set to: $apn"
    fi

    # --- Step 7: Install systemd timer and service ---
    log_step "[WWAN 7/9] Installing systemd timer and service..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "cp wwan-check.timer and wwan-check.service to $SYSTEMD_PATH/"
        log_dry_run "systemctl daemon-reload"
        log_dry_run "systemctl enable wwan-check.timer"
        manifest_add "copy" "src" "$FILES_DIR/wwan-check.timer" "dst" "$SYSTEMD_PATH/wwan-check.timer" "sudo" "true"
        manifest_add "copy" "src" "$FILES_DIR/wwan-check.service" "dst" "$SYSTEMD_PATH/wwan-check.service" "sudo" "true"
        manifest_add "command" "cmd" "systemctl daemon-reload" "sudo" "true"
        manifest_add "systemctl" "action" "enable" "service" "wwan-check.timer"
    else
        sudo cp "$FILES_DIR/wwan-check.timer" "$SYSTEMD_PATH/"
        sudo cp "$FILES_DIR/wwan-check.service" "$SYSTEMD_PATH/"
        sudo systemctl daemon-reload
        sudo systemctl enable wwan-check.timer
        log_success "wwan-check.timer enabled"
    fi

    # --- Step 8: Start the timer ---
    log_step "[WWAN 8/9] Starting WWAN check timer..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "systemctl start wwan-check.timer"
        manifest_add "systemctl" "action" "start" "service" "wwan-check.timer"
    else
        sudo systemctl start wwan-check.timer
        log_success "Timer started"
    fi

    # --- Step 9: Generate SSH key for server access ---
    # SSH key is needed for server access regardless of connection method
    log_step "[WWAN 9/9] Generating SSH key for server access..."

    local ssh_dir="/home/pi/.ssh"
    local id_rsa="$ssh_dir/id_rsa"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "mkdir -p $ssh_dir"
        log_dry_run "ssh-keygen -t rsa -f $id_rsa -N ''"
        manifest_add "mkdir" "path" "$ssh_dir" "mode" "0700"
        manifest_add "ssh-keygen" "keytype" "rsa" "keyfile" "$id_rsa"
    else
        # Run as pi user to ensure correct ownership
        sudo -H -u pi bash -c "
            mkdir -p '$ssh_dir'
            chmod 700 '$ssh_dir'

            # Generate RSA key if it doesn't exist
            if [[ -f '$id_rsa' ]]; then
                echo 'SSH key already exists'
            else
                ssh-keygen -t rsa -f '$id_rsa' -N '' -q
            fi
        "

        if [[ -f "$id_rsa" ]]; then
            if [[ -f "$id_rsa.pub" ]]; then
                log_success "SSH key ready at $id_rsa"
                echo ""
                log_info "Public key (copy to server authorized_keys):"
                cat "$id_rsa.pub"
                echo ""
            fi
        else
            log_warn "SSH key already exists at $id_rsa"
            log_info "Skipping key generation"
        fi
    fi

    log_success "Cellular network setup complete!"
    echo ""
    log_info "Configuration summary:"
    echo "  - Network config: $NETWORK_PATH/20-wwan0.network"
    echo "  - Connection script: $BIN_PATH/50_bring_wwan0_up.py"
    echo "  - Wrapper script: $BIN_PATH/wwan-check.sh"
    echo "  - Timer: wwan-check.timer (runs every 5 minutes)"
    echo "  - APN: $apn"
    echo ""
    log_info "Useful commands:"
    echo "  - Check timer status: systemctl status wwan-check.timer"
    echo "  - Check service logs: journalctl -t wwan-connect-all -f"
    echo "  - Manual connection test: /usr/local/bin/wwan-check.sh"
    echo "  - Modem status: mmcli -m 0"
    echo ""
    log_info "A reboot is recommended to ensure all changes take effect."
}
