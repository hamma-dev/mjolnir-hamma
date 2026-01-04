#!/bin/bash
# Hardware setup for HAMMA Pi
#
# Based on original setup_sensor_connect.sh and enable_automount.sh
#
# This script sets up:
#   1. SSH config for sensor connection
#   2. Network configuration for eth0/eth1 (sensor interfaces)
#   3. Automount rules for USB drives via polkit
#
# Requirements:
#   - common.sh must be sourced first
#
# Functions:
#   setup_sensor_connection
#   setup_automount

# --- Configuration ---
FILES_DIR="${FILES_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../files" && pwd 2>/dev/null || echo "/home/pi/dev/mjolnir-hamma/files")}"
NETWORK_PATH="/etc/systemd/network"
POLKIT_PATH="/etc/polkit-1/localauthority/50-local.d"

# --- Setup Sensor Connection ---
# Copies SSH config and eth network files
setup_sensor_connection() {
    log_step "Setting up sensor connection..."

    local ssh_dir="/home/pi/.ssh"
    local ssh_config="$ssh_dir/config"

    # --- Step 1: Copy SSH config ---
    log_step "[Hardware 1/2] Copying SSH config..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "mkdir -p $ssh_dir"
        log_dry_run "cp $FILES_DIR/config $ssh_config"
        log_dry_run "chmod 600 $ssh_config"
        manifest_add "mkdir" "path" "$ssh_dir" "mode" "0700"
        manifest_add "copy" "src" "$FILES_DIR/config" "dst" "$ssh_config"
        manifest_add "chmod" "path" "$ssh_config" "mode" "0600"
    else
        # Create .ssh directory if needed
        mkdir -p "$ssh_dir"
        chmod 700 "$ssh_dir"

        # Copy SSH config (connection settings for sensor and proxy)
        if [[ -f "$FILES_DIR/config" ]]; then
            cp "$FILES_DIR/config" "$ssh_config"
            chmod 600 "$ssh_config"
            log_success "SSH config installed"
        else
            log_warn "SSH config file not found at $FILES_DIR/config"
        fi
    fi

    # --- Step 2: Copy ethernet network files ---
    log_step "[Hardware 2/2] Copying ethernet network files..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "cp $FILES_DIR/*eth*network $NETWORK_PATH/"
        for f in "$FILES_DIR"/*eth*network; do
            local basename=$(basename "$f")
            manifest_add "copy" "src" "$f" "dst" "$NETWORK_PATH/$basename" "sudo" "true"
        done
    else
        # Copy eth0 and eth1 network configuration
        for f in "$FILES_DIR"/*eth*network; do
            if [[ -f "$f" ]]; then
                local basename=$(basename "$f")
                sudo cp "$f" "$NETWORK_PATH/"
                log_info "  Copied $basename"
            fi
        done
        log_success "Ethernet network files installed"
    fi

    log_success "Sensor connection setup complete!"
    echo ""
    log_info "SSH aliases configured:"
    echo "  - ssh proxy  (proxy.nsstc.uah.edu as mjolnir)"
    echo "  - ssh hamma  (10.10.10.1 as root)"
}

# --- Setup Automount ---
# Enables automatic mounting of USB drives via polkit rules
setup_automount() {
    log_step "Setting up automount..."

    local mount_file="mount-udisks.pkla"

    # --- Step 1: Create polkit directory if needed ---
    log_step "[Automount 1/2] Setting up polkit directory..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "mkdir -p $POLKIT_PATH"
        manifest_add "mkdir" "path" "$POLKIT_PATH" "sudo" "true"
    else
        if [[ ! -d "$POLKIT_PATH" ]]; then
            sudo mkdir -p "$POLKIT_PATH"
            log_info "Created polkit directory"
        else
            log_info "Polkit directory already exists"
        fi
    fi

    # --- Step 2: Copy mount rules ---
    log_step "[Automount 2/2] Installing mount rules..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "cp $FILES_DIR/$mount_file $POLKIT_PATH/"
        log_dry_run "chown root $POLKIT_PATH/$mount_file"
        log_dry_run "chmod 700 $POLKIT_PATH/$mount_file"
        manifest_add "copy" "src" "$FILES_DIR/$mount_file" "dst" "$POLKIT_PATH/$mount_file" "sudo" "true"
        manifest_add "chown" "path" "$POLKIT_PATH/$mount_file" "owner" "root" "sudo" "true"
        manifest_add "chmod" "path" "$POLKIT_PATH/$mount_file" "mode" "0700" "sudo" "true"
    else
        if [[ -f "$FILES_DIR/$mount_file" ]]; then
            sudo cp "$FILES_DIR/$mount_file" "$POLKIT_PATH/"
            sudo chown root "$POLKIT_PATH/$mount_file"
            sudo chmod 700 "$POLKIT_PATH/$mount_file"
            log_success "Mount rules installed"
        else
            log_warn "Mount rules file not found at $FILES_DIR/$mount_file"
        fi
    fi

    log_success "Automount setup complete!"
    echo ""
    log_info "Users in 'pi' group can now mount/unmount drives using udisksctl"
}
