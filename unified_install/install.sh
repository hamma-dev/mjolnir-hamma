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
SKIP_HAMMA=false
CELLULAR_APN=""
GENERATE_HAMMA_KEY=false
HAMMA_ONLY=false

print_usage() {
    echo "Usage: $0 <sensor_number> --wifi|--cellular [options]"
    echo ""
    echo "Network modes (required, choose one):"
    echo "  --wifi              Use WiFi network (UAH/NSSTC)"
    echo "  --cellular          Use Cellular network (modem)"
    echo ""
    echo "Options:"
    echo "  --dry-run           Show what would be done without executing"
    echo "  --skip-packages     Skip system package installation"
    echo "  --skip-brokkr       Skip Brokkr installation"
    echo "  --skip-hardware     Skip hardware setup"
    echo "  --skip-extras       Skip sindri/pyltg/hamma installation"
    echo "  --skip-hamma        Skip HAMMA installation (requires SSH key for private repo)"
    echo "  -h, --help          Show this help message"
    echo ""
    echo "Cellular options:"
    echo "  --apn APN           Set cellular APN (default: h2g2)"
    echo ""
    echo "HAMMA private repo options:"
    echo "  --generate-hamma-key  Generate SSH key for GitHub and exit"
    echo "  --hamma-only          Only install hamma (skip everything else)"
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
        --skip-hamma)
            SKIP_HAMMA=true
            shift
            ;;
        --apn)
            CELLULAR_APN="$2"
            shift 2
            ;;
        --generate-hamma-key)
            GENERATE_HAMMA_KEY=true
            shift
            ;;
        --hamma-only)
            HAMMA_ONLY=true
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

# Network path not required for --generate-hamma-key or --hamma-only
if [[ -z "$NETWORK_PATH" && "$GENERATE_HAMMA_KEY" != "true" && "$HAMMA_ONLY" != "true" ]]; then
    log_error "Network path is required: --wifi or --cellular"
    print_usage
    exit 1
fi

SENSOR_FORMATTED=$(format_sensor_num "$SENSOR_NUM")
HOSTNAME="mjolnir$SENSOR_FORMATTED"

# --- Initialize ---
init_common $(if [[ "$DRY_RUN" == "true" ]]; then echo "--dry-run"; fi)

# ============================================================================
# Special mode: --generate-hamma-key (generate key and exit)
# ============================================================================
if [[ "$GENERATE_HAMMA_KEY" == "true" ]]; then
    log_info "=== Generate HAMMA SSH Key ==="
    echo ""
    source "$SCRIPT_DIR/lib/software.sh"
    install_hamma --generate-key
    echo ""
    log_info "Next steps:"
    echo "  1. Add the public key above to GitHub as a deploy key:"
    echo "     https://github.com/pbitzer/hamma/settings/keys"
    echo "  2. Run: sudo bash install.sh $SENSOR_NUM --hamma-only"
    exit 0
fi

# ============================================================================
# Special mode: --hamma-only (install just hamma)
# ============================================================================
if [[ "$HAMMA_ONLY" == "true" ]]; then
    log_info "=== Install HAMMA Only ==="
    echo ""
    source "$SCRIPT_DIR/lib/software.sh"
    install_hamma
    log_success "HAMMA installation complete!"
    exit 0
fi

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
if [[ -n "$CELLULAR_APN" ]]; then
    log_info "Cellular APN: $CELLULAR_APN"
fi
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
# Update mjolnir-hamma repository
# ============================================================================
log_step "Updating mjolnir-hamma repository..."

REPO_PATH="/home/pi/dev/mjolnir-hamma"
REPO_URL="https://github.com/hamma-dev/mjolnir-hamma.git"
# TODO: Update this branch when releasing or changing version branches
REPO_BRANCH="0.3.x"

if [[ "$DRY_RUN" == "true" ]]; then
    log_dry_run "git -C $REPO_PATH remote set-url origin $REPO_URL"
    log_dry_run "git -C $REPO_PATH pull (if on $REPO_BRANCH)"
    manifest_add "command" "cmd" "git remote set-url origin $REPO_URL" "cwd" "$REPO_PATH"
    manifest_add "command" "cmd" "git pull (if on $REPO_BRANCH)" "cwd" "$REPO_PATH"
else
    if [[ -d "$REPO_PATH/.git" ]]; then
        # Set remote to hamma-dev (in case USB copy had different origin)
        git -C "$REPO_PATH" remote set-url origin "$REPO_URL" 2>/dev/null || \
            git -C "$REPO_PATH" remote add origin "$REPO_URL"

        # Only pull if already on the expected branch (don't switch branches mid-install)
        CURRENT_BRANCH=$(git -C "$REPO_PATH" rev-parse --abbrev-ref HEAD 2>/dev/null)
        if [[ "$CURRENT_BRANCH" == "$REPO_BRANCH" ]]; then
            git -C "$REPO_PATH" fetch origin
            if git -C "$REPO_PATH" pull; then
                log_success "Repository updated from GitHub (branch: $REPO_BRANCH)"
            else
                log_warn "Could not pull latest (continuing with current version)"
            fi
        else
            log_warn "On branch '$CURRENT_BRANCH', not '$REPO_BRANCH' - skipping pull to avoid switching branches"
        fi
    else
        log_warn "Repository not found at $REPO_PATH - was bootstrap.sh run?"
    fi
fi

echo ""

# ============================================================================
# PHASE 2: Network Setup
# ============================================================================
log_info "=== Phase 2: Network Setup ($NETWORK_PATH) ==="
echo ""

# --- Common: eth0 sensor interface config (applies to all network modes) ---
log_step "Installing eth0 network configuration..."
FILES_DIR="${FILES_DIR:-$REPO_ROOT/files}"
NETWORK_DIR="/etc/systemd/network"

if [[ "$DRY_RUN" == "true" ]]; then
    log_dry_run "cp $FILES_DIR/40-eth0.network $NETWORK_DIR/"
    manifest_add "copy" "src" "$FILES_DIR/40-eth0.network" "dst" "$NETWORK_DIR/40-eth0.network" "sudo" "true"
else
    sudo cp "$FILES_DIR/40-eth0.network" "$NETWORK_DIR/"
    log_success "eth0 network config installed"
fi

echo ""

if [[ "$NETWORK_PATH" == "wifi" ]]; then
    # --- WiFi Path ---
    source "$SCRIPT_DIR/lib/network_wifi.sh"
    setup_wifi_network "$SENSOR_NUM"
else
    # --- Cellular Path ---
    source "$SCRIPT_DIR/lib/network_wwan.sh"
    if [[ -n "$CELLULAR_APN" ]]; then
        setup_cellular_network "$SENSOR_NUM" --apn "$CELLULAR_APN"
    else
        setup_cellular_network "$SENSOR_NUM"
    fi
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
    if [[ "$SKIP_HAMMA" != "true" ]]; then
        install_hamma
    else
        log_info "Skipping HAMMA installation (--skip-hamma)"
    fi
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
