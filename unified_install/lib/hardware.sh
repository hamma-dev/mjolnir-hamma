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
FILES_DIR="${FILES_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../files" && pwd 2>/dev/null || echo "/home/pi/dev/mjolnir-hamma/files")}"
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
        # Create .ssh directory if needed (as pi user to ensure correct ownership)
        # Fix ownership first in case a prior run created it as root
        chown -R pi:pi "$ssh_dir" 2>/dev/null || true
        sudo -H -u pi mkdir -p "$ssh_dir"
        sudo -H -u pi chmod 700 "$ssh_dir"

        # Copy SSH config (connection settings for sensor and proxy)
        if [[ -f "$FILES_DIR/config" ]]; then
            sudo -H -u pi cp "$FILES_DIR/config" "$ssh_config"
            sudo -H -u pi chmod 600 "$ssh_config"
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

# --- Configure Log-Size Bounds (HAM-113) ---
# Bounds /var/log so a runaway log source (brokkr science_ingest spamming when
# the AGS is down, HAM-112) cannot fill the SD card. Three defenses:
#   1. journald SystemMaxUse cap (drop-in)
#   2. maxsize on the rsyslog logrotate stanzas
#   3. hourly logrotate run so maxsize is enforced within the hour
configure_log_bounds() {
    log_step "Configuring log-size bounds (HAM-113)..."

    local journald_dir="/etc/systemd/journald.conf.d"
    local journald_src="journald-sensor-bounds.conf"
    local journald_dst="$journald_dir/00-sensor-bounds.conf"
    local rsyslog_lr="/etc/logrotate.d/rsyslog"
    # Backup MUST live outside /etc/logrotate.d/ -- logrotate reads every file
    # in that dir, so a backup there causes "duplicate log entry" errors.
    local rsyslog_bak="/var/backups/logrotate-rsyslog.mjolnir-orig"
    local cron_src="logrotate-hourly.sh"
    local cron_dst="/etc/cron.hourly/mjolnir-logrotate"

    # --- Step 1: Cap systemd-journald disk use ---
    log_step "[Log bounds 1/3] Capping systemd-journald..."
    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "mkdir -p $journald_dir"
        log_dry_run "cp $FILES_DIR/$journald_src $journald_dst"
        log_dry_run "systemctl restart systemd-journald"
        manifest_add "mkdir" "path" "$journald_dir" "sudo" "true"
        manifest_add "copy" "src" "$FILES_DIR/$journald_src" "dst" "$journald_dst" "sudo" "true"
        manifest_add "service" "action" "restart" "unit" "systemd-journald" "sudo" "true"
    else
        if [[ -f "$FILES_DIR/$journald_src" ]]; then
            sudo mkdir -p "$journald_dir"
            sudo cp "$FILES_DIR/$journald_src" "$journald_dst"
            sudo chmod 0644 "$journald_dst"
            sudo systemctl restart systemd-journald
            log_success "journald disk use capped ($journald_dst)"
        else
            log_warn "journald bounds file not found at $FILES_DIR/$journald_src"
        fi
    fi

    # --- Step 2: Add maxsize cap to the rsyslog logrotate stanzas ---
    log_step "[Log bounds 2/3] Adding maxsize cap to $rsyslog_lr..."
    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "inject 'maxsize 100M' into $rsyslog_lr (idempotent; backup at $rsyslog_bak)"
        manifest_add "modify" "path" "$rsyslog_lr" "change" "add maxsize 100M" "sudo" "true"
    else
        if [[ -f "$rsyslog_lr" ]]; then
            if sudo grep -q "maxsize" "$rsyslog_lr"; then
                log_info "rsyslog logrotate already has a maxsize cap; leaving as-is"
            else
                sudo mkdir -p "$(dirname "$rsyslog_bak")"
                sudo cp -a "$rsyslog_lr" "$rsyslog_bak"
                sudo sed -i '/^{/a\    maxsize 100M' "$rsyslog_lr"
                log_success "Added 'maxsize 100M' to $rsyslog_lr (backup at $rsyslog_bak)"
            fi
        else
            log_warn "rsyslog logrotate config not found at $rsyslog_lr"
        fi
    fi

    # --- Step 3: Run logrotate hourly so maxsize is enforced within the hour ---
    log_step "[Log bounds 3/3] Installing hourly logrotate job..."
    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "cp $FILES_DIR/$cron_src $cron_dst"
        log_dry_run "chmod 0755 $cron_dst"
        manifest_add "copy" "src" "$FILES_DIR/$cron_src" "dst" "$cron_dst" "sudo" "true"
        manifest_add "chmod" "path" "$cron_dst" "mode" "0755" "sudo" "true"
    else
        if [[ -f "$FILES_DIR/$cron_src" ]]; then
            sudo cp "$FILES_DIR/$cron_src" "$cron_dst"
            sudo chmod 0755 "$cron_dst"
            log_success "Hourly logrotate job installed at $cron_dst"
        else
            log_warn "Hourly logrotate file not found at $FILES_DIR/$cron_src"
        fi
    fi

    log_success "Log-size bounds configured!"
    echo ""
    log_info "  - journald: SystemMaxUse capped via $journald_dst"
    log_info "  - rsyslog:  maxsize 100M per log, rotated hourly"
    log_info "  - Keeps /var/log well under 2 GB even under sustained log spam"
}
