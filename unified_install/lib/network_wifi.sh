#!/bin/bash
# WiFi network setup for HAMMA Pi (UAH/NSSTC)
#
# This script sets up WiFi connectivity using WPA-EAP with certificate.
# Based on original setup_uah_wireless.sh
#
# CRITICAL: This script generates id_rsa via ssh-keygen for server SSH access!
# This was the key bug in the failed unification attempt.
#
# Requirements:
#   - UAH certificate file (NSSTC-UAH-WIRELESS-mjolnirNN.p12) on USB
#   - common.sh must be sourced first
#
# Functions:
#   setup_wifi_network <sensor_number>

# --- Configuration ---
USB_PATH="${USB_PATH:-/mnt/usb}"
CERT_PATH="/home/pi/.nsstc"
FILES_DIR="${FILES_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../files" && pwd 2>/dev/null || echo "/home/pi/dev/mjolnir-hamma/files")}"

# --- Main Setup Function ---
setup_wifi_network() {
    local sensor_num="$1"

    if ! validate_sensor_num "$sensor_num"; then
        log_error "Invalid sensor number: $sensor_num"
        return 1
    fi

    local sensor_formatted=$(format_sensor_num "$sensor_num")
    local hostname="mjolnir$sensor_formatted"
    local cert_name="NSSTC-UAH-WIRELESS-$hostname.p12"

    log_step "Setting up WiFi network for $hostname..."

    # --- Step 1: Copy certificate ---
    log_step "[WiFi 1/7] Setting up certificate..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "mkdir -p $CERT_PATH"
        log_dry_run "cp $USB_PATH/$cert_name $CERT_PATH/"
        manifest_add "mkdir" "path" "$CERT_PATH"
        manifest_add "copy" "src" "$USB_PATH/$cert_name" "dst" "$CERT_PATH/$cert_name"
    else
        # Create certificate directory
        mkdir -p "$CERT_PATH"

        # Copy certificate from USB
        if [[ -f "$USB_PATH/$cert_name" ]]; then
            cp "$USB_PATH/$cert_name" "$CERT_PATH/"
            log_success "Copied certificate to $CERT_PATH"
        else
            log_warn "Certificate not found at $USB_PATH/$cert_name"
            log_warn "You will need to copy it manually"
        fi
    fi

    # --- Step 2: Copy wpa_supplicant override ---
    log_step "[WiFi 2/7] Setting up wpa_supplicant override..."

    local override_dir="/etc/systemd/system/wpa_supplicant@wlan0.service.d"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "mkdir -p $override_dir"
        log_dry_run "cp $FILES_DIR/override.conf $override_dir/"
        log_dry_run "chmod 0755 $override_dir"
        log_dry_run "chmod 0644 $override_dir/override.conf"
        manifest_add "mkdir" "path" "$override_dir" "sudo" "true"
        manifest_add "copy" "src" "$FILES_DIR/override.conf" "dst" "$override_dir/override.conf" "sudo" "true"
        manifest_add "chmod" "path" "$override_dir" "mode" "0755" "sudo" "true"
        manifest_add "chmod" "path" "$override_dir/override.conf" "mode" "0644" "sudo" "true"
    else
        sudo mkdir -p "$override_dir"
        sudo cp "$FILES_DIR/override.conf" "$override_dir/"
        sudo chmod 0755 "$override_dir"
        sudo chmod 0644 "$override_dir/override.conf"
        log_success "Installed wpa_supplicant override"
    fi

    # --- Step 3: Copy network file ---
    log_step "[WiFi 3/7] Setting up network file..."

    local network_file="/etc/systemd/network/10-wlan0.network"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "cp $FILES_DIR/10-wlan0.network $network_file"
        log_dry_run "sed 'Hostname=$hostname' in $network_file"
        log_dry_run "chmod 0644 $network_file"
        manifest_add "copy" "src" "$FILES_DIR/10-wlan0.network" "dst" "$network_file" "sudo" "true"
        manifest_add "sed" "path" "$network_file" "pattern" "Hostname=.*" "replacement" "Hostname=$hostname"
        manifest_add "chmod" "path" "$network_file" "mode" "0644" "sudo" "true"
    else
        sudo cp "$FILES_DIR/10-wlan0.network" "$network_file"
        sudo sed -i "s/^Hostname=.*/Hostname=$hostname/" "$network_file"
        sudo chmod 0644 "$network_file"
        log_success "Installed 10-wlan0.network"
    fi

    # --- Step 4: Copy wpa_supplicant config ---
    log_step "[WiFi 4/7] Setting up wpa_supplicant config..."

    local wpa_config="/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "cp $FILES_DIR/wpa_supplicant-wlan0.conf $wpa_config"
        log_dry_run "sed private_key path to $CERT_PATH/$cert_name"
        manifest_add "copy" "src" "$FILES_DIR/wpa_supplicant-wlan0.conf" "dst" "$wpa_config" "sudo" "true"
        manifest_add "sed" "path" "$wpa_config" "pattern" "private_key=.*" "replacement" "private_key=\"$CERT_PATH/$cert_name\""
    else
        sudo cp "$FILES_DIR/wpa_supplicant-wlan0.conf" "$wpa_config"
        sudo sed -i "s%private_key=.*%private_key=\"$CERT_PATH/$cert_name\"%" "$wpa_config"
        log_success "Installed wpa_supplicant-wlan0.conf"
        log_warn "Don't forget to update private_key_passwd in $wpa_config!"
    fi

    # --- Step 5: Setup resolv.conf ---
    log_step "[WiFi 5/7] Setting up DNS resolution..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "rm -f /etc/resolv.conf"
        log_dry_run "ln -s /run/systemd/resolve/resolv.conf /etc/resolv.conf"
        manifest_add "remove" "path" "/etc/resolv.conf" "sudo" "true"
        manifest_add "symlink" "target" "/run/systemd/resolve/resolv.conf" "link" "/etc/resolv.conf" "sudo" "true"
    else
        sudo rm -f /etc/resolv.conf
        sudo ln -s /run/systemd/resolve/resolv.conf /etc/resolv.conf
        log_success "Linked resolv.conf to systemd-resolved"
    fi

    # --- Step 6: Enable services ---
    log_step "[WiFi 6/7] Enabling services..."

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "systemctl daemon-reload"
        log_dry_run "systemctl enable wpa_supplicant@wlan0.service"
        log_dry_run "systemctl enable systemd-networkd.service"
        log_dry_run "systemctl restart systemd-networkd.service"
        log_dry_run "systemctl restart wpa_supplicant@wlan0.service"
        manifest_add "command" "cmd" "systemctl daemon-reload" "sudo" "true"
        manifest_add "systemctl" "action" "enable" "service" "wpa_supplicant@wlan0.service"
        manifest_add "systemctl" "action" "enable" "service" "systemd-networkd.service"
        manifest_add "systemctl" "action" "restart" "service" "systemd-networkd.service"
        manifest_add "systemctl" "action" "restart" "service" "wpa_supplicant@wlan0.service"
    else
        sudo systemctl daemon-reload
        sudo systemctl enable wpa_supplicant@wlan0.service
        sudo systemctl enable systemd-networkd.service
        sudo systemctl restart systemd-networkd.service || true
        sudo systemctl restart wpa_supplicant@wlan0.service || true
        log_success "Services enabled and restarted"
    fi

    # --- Step 7: Generate SSH key (CRITICAL!) ---
    # This is THE critical step that was missed in the failed unification attempt.
    # WiFi path MUST generate id_rsa for server SSH access.
    # See setup_uah_wireless.sh line 54
    log_step "[WiFi 7/7] Generating SSH key for server access (CRITICAL)..."

    local ssh_dir="/home/pi/.ssh"
    local id_rsa="$ssh_dir/id_rsa"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "mkdir -p $ssh_dir"
        log_dry_run "ssh-keygen -t rsa -f $id_rsa -N ''"
        manifest_add "mkdir" "path" "$ssh_dir" "mode" "0700"
        manifest_add "ssh-keygen" "keytype" "rsa" "keyfile" "$id_rsa"
    else
        # Create .ssh directory if needed
        mkdir -p "$ssh_dir"
        chmod 700 "$ssh_dir"

        # Generate RSA key if it doesn't exist
        if [[ -f "$id_rsa" ]]; then
            log_warn "SSH key already exists at $id_rsa"
            log_info "Skipping key generation"
        else
            # Generate key without passphrase for automated access
            ssh-keygen -t rsa -f "$id_rsa" -N "" -q
            log_success "Generated SSH key at $id_rsa"
        fi

        # Show public key for copying to server
        if [[ -f "$id_rsa.pub" ]]; then
            echo ""
            log_info "Public key (copy to server authorized_keys):"
            cat "$id_rsa.pub"
            echo ""
        fi
    fi

    log_success "WiFi network setup complete!"
    echo ""
    log_info "Next steps:"
    echo "  1. Update private_key_passwd in /etc/wpa_supplicant/wpa_supplicant-wlan0.conf"
    echo "  2. Reboot to apply network changes"
    echo "  3. Copy id_rsa.pub to server authorized_keys"
}
