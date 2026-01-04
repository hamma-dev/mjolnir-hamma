#!/bin/bash
# Master installation script for HAMMA Pi
#
# This script orchestrates the full installation after bootstrap:
#   Phase 2: Network Setup (WiFi or Cellular)
#   Phase 3: Base Software (packages, brokkr)
#   Phase 4: Hardware (sensor connect, automount)
#   Phase 6: Additional Software (sindri, pyltg, hamma)
#
# Usage:
#   ./install.sh <sensor_number> --wifi [options]
#   ./install.sh <sensor_number> --cellular [options]
#
# Options:
#   --wifi          Use WiFi network path (UAH/NSSTC)
#   --cellular      Use Cellular network path (modem)
#   --dry-run       Show what would be done without executing
#   --skip-packages Skip system package installation
#   --skip-brokkr   Skip Brokkr installation
#   --skip-hardware Skip hardware setup
#   --skip-extras   Skip sindri/pyltg/hamma installation
#   -h, --help      Show this help message
#
# Prerequisites:
#   - Run bootstrap.sh first and reboot
#   - For WiFi: UAH certificate on USB drive
#   - For Cellular: SIM card installed in modem

set -e

# --- Get script directory ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Source common library ---
source "$SCRIPT_DIR/lib/common.sh"

# --- Parse arguments ---
SENSOR_NUM=""
NETWORK_PATH=""
DRY_RUN=false
SKIP_PACKAGES=false
SKIP_BROKKR=false
SKIP_HARDWARE=false
SKIP_EXTRAS=false

print_usage() {
    echo "Usage: $0 <sensor_number> --wifi|--cellular [options]"
    echo ""
    echo "Network modes (required, choose one):"
    echo "  --wifi          Use WiFi network (UAH/NSSTC)"
    echo "  --cellular      Use Cellular network (modem)"
    echo ""
    echo "Options:"
    echo "  --dry-run       Show what would be done without executing"
    echo "  --skip-packages Skip system package installation"
    echo "  --skip-brokkr   Skip Brokkr installation"
    echo "  --skip-hardware Skip hardware setup"
    echo "  --skip-extras   Skip sindri/pyltg/hamma installation"
    echo "  -h, --help      Show this help message"
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --wifi)
            NETWORK_PATH="wifi"
            shift
            ;;
        --cellular)
            NETWORK_PATH="cellular"
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --skip-packages)
            SKIP_PACKAGES=true
            shift
            ;;
        --skip-brokkr)
            SKIP_BROKKR=true
            shift
            ;;
        --skip-hardware)
            SKIP_HARDWARE=true
            shift
            ;;
        --skip-extras)
            SKIP_EXTRAS=true
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

if [[ -z "$NETWORK_PATH" ]]; then
    log_error "Network path is required: --wifi or --cellular"
    print_usage
    exit 1
fi

SENSOR_FORMATTED=$(format_sensor_num "$SENSOR_NUM")
HOSTNAME="mjolnir$SENSOR_FORMATTED"

# --- Initialize ---
init_common $(if [[ "$DRY_RUN" == "true" ]]; then echo "--dry-run"; fi)

# --- Display configuration ---
log_info "=== HAMMA Pi Installation ==="
log_info "Sensor: $HOSTNAME"
log_info "Network: $NETWORK_PATH"
log_info "Dry run: $DRY_RUN"
echo ""
log_info "Skip packages: $SKIP_PACKAGES"
log_info "Skip brokkr: $SKIP_BROKKR"
log_info "Skip hardware: $SKIP_HARDWARE"
log_info "Skip extras: $SKIP_EXTRAS"
echo ""

# --- Verify prerequisites ---
log_step "Verifying prerequisites..."

# Check if running from correct location
if [[ ! -f "$SCRIPT_DIR/lib/common.sh" ]]; then
    log_error "Cannot find lib/common.sh - are you running from the correct directory?"
    exit 1
fi

# Check if bootstrap was run (hostname should be set)
CURRENT_HOSTNAME=$(hostname)
if [[ "$CURRENT_HOSTNAME" != "$HOSTNAME" ]]; then
    log_warn "Current hostname ($CURRENT_HOSTNAME) doesn't match expected ($HOSTNAME)"
    log_warn "Did you run bootstrap.sh and reboot?"
    if [[ "$DRY_RUN" != "true" ]]; then
        read -p "Continue anyway? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
fi

log_success "Prerequisites OK"
echo ""

# ============================================================================
# PHASE 2: Network Setup
# ============================================================================
log_info "=== Phase 2: Network Setup ($NETWORK_PATH) ==="
echo ""

if [[ "$NETWORK_PATH" == "wifi" ]]; then
    # --- WiFi Path ---
    source "$SCRIPT_DIR/lib/network_wifi.sh"
    setup_wifi_network "$SENSOR_NUM"
else
    # --- Cellular Path ---
    source "$SCRIPT_DIR/lib/network_wwan.sh"
    setup_cellular_network "$SENSOR_NUM"
fi

echo ""

# ============================================================================
# PHASE 3: Base Software
# ============================================================================
log_info "=== Phase 3: Base Software ==="
echo ""

# --- System packages ---
if [[ "$SKIP_PACKAGES" != "true" ]]; then
    log_step "Installing system packages..."
    source "$SCRIPT_DIR/lib/software.sh"
    install_system_packages
else
    log_info "Skipping system packages (--skip-packages)"
fi

echo ""

# --- Brokkr ---
if [[ "$SKIP_BROKKR" != "true" ]]; then
    log_step "Setting up Brokkr..."
    source "$SCRIPT_DIR/lib/brokkr.sh"
    install_brokkr "$SENSOR_NUM"
    configure_brokkr "$SENSOR_NUM"
else
    log_info "Skipping Brokkr (--skip-brokkr)"
fi

echo ""

# ============================================================================
# PHASE 4: Hardware Setup
# ============================================================================
log_info "=== Phase 4: Hardware Setup ==="
echo ""

if [[ "$SKIP_HARDWARE" != "true" ]]; then
    source "$SCRIPT_DIR/lib/hardware.sh"
    setup_sensor_connection
    setup_automount
else
    log_info "Skipping hardware setup (--skip-hardware)"
fi

echo ""

# ============================================================================
# PHASE 6: Additional Software
# ============================================================================
log_info "=== Phase 6: Additional Software ==="
echo ""

if [[ "$SKIP_EXTRAS" != "true" ]]; then
    source "$SCRIPT_DIR/lib/software.sh"
    install_sindri "$SENSOR_NUM"
    install_pyltg
    install_hamma
else
    log_info "Skipping additional software (--skip-extras)"
fi

echo ""

# ============================================================================
# Finalize
# ============================================================================
if [[ "$DRY_RUN" == "true" ]]; then
    manifest_finalize
    echo ""
    log_info "Dry run complete. Manifest written to: $MANIFEST_FILE"
    echo ""
    log_info "To view manifest:"
    echo "  cat $MANIFEST_FILE | jq ."
else
    log_info "=== Installation Complete ==="
    echo ""
    log_info "To verify Brokkr:"
    echo "  source /home/pi/ltgenv"
    echo "  brokkr status"
    echo ""
    log_info "To start Brokkr service:"
    echo "  sudo systemctl start brokkr-hamma-default.service"
    echo ""

    if [[ "$NETWORK_PATH" == "wifi" ]]; then
        log_warn "REMINDER: Don't forget to:"
        echo "  1. Update private key password in /etc/wpa_supplicant/wpa_supplicant-wlan0.conf"
        echo "  2. Copy id_rsa.pub to server authorized_keys"
    fi
fi
