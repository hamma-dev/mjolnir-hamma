#!/bin/bash
# Master install script for HAMMA Pi
#
# This driver script orchestrates the installation process by calling
# worker scripts in the correct order. Run this AFTER bootstrap.sh and reboot.
#
# IMPORTANT: For cellular sites, the base image must have modemmanager pre-installed.
# For WiFi sites, connect to WiFi manually first if needed before running this script.
#
# Usage:
#   sudo ./install.sh [OPTIONS]
#
# Options:
#   -n, --sensor-num NUM    Sensor number (required)
#   -a, --apn APN           APN for cellular (default: h2g2)
#   --wifi                  Setup WiFi instead of cellular
#   --skip-packages         Skip package installation
#   --skip-network          Skip network setup
#   --skip-brokkr           Skip Brokkr installation
#   --skip-hardware         Skip hardware setup (sensor connect, automount)
#   --skip-sindri           Skip Sindri installation
#   --skip-pyltg            Skip PyLtg installation
#   --skip-hamma            Skip HAMMA installation
#   --only STEP             Run only specified step (see below)
#   -h, --help              Show this help
#
# Valid --only steps: network, packages, brokkr, hardware, sindri, pyltg, hamma
#
# Examples:
#   sudo ./install.sh -n 42                    # Full install, cellular, sensor 42
#   sudo ./install.sh -n 42 --wifi             # Full install, WiFi, sensor 42
#   sudo ./install.sh -n 42 --apn vzwinternet  # Verizon cellular
#   sudo ./install.sh --only network           # Just setup network
#   sudo ./install.sh -n 42 --only brokkr      # Just setup Brokkr
#
# NOTE: The hamma repo is private and requires SSH key setup.
#       Run install_hamma.sh -k first to generate key, add to GitHub, then run full install.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Default Configuration ---
SENSOR_NUM=""
APN="h2g2"
USE_WIFI=false
SKIP_PACKAGES=false
SKIP_NETWORK=false
SKIP_BROKKR=false
SKIP_HARDWARE=false
SKIP_SINDRI=false
SKIP_PYLTG=false
SKIP_HAMMA=false
ONLY_STEP=""

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# --- Functions ---
log_step() {
    echo -e "${BLUE}==>${NC} $1"
}

log_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

usage() {
    head -35 "$0" | tail -30
    exit 0
}

check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "This script must be run as root (use sudo)"
        exit 1
    fi
}

# --- Parse Arguments ---
while [[ $# -gt 0 ]]; do
    case $1 in
        -n|--sensor-num)
            SENSOR_NUM="$2"
            shift 2
            ;;
        -a|--apn)
            APN="$2"
            shift 2
            ;;
        --wifi)
            USE_WIFI=true
            shift
            ;;
        --skip-packages)
            SKIP_PACKAGES=true
            shift
            ;;
        --skip-network)
            SKIP_NETWORK=true
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
        --skip-sindri)
            SKIP_SINDRI=true
            shift
            ;;
        --skip-pyltg)
            SKIP_PYLTG=true
            shift
            ;;
        --skip-hamma)
            SKIP_HAMMA=true
            shift
            ;;
        --only)
            ONLY_STEP="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            log_error "Unknown option: $1"
            usage
            ;;
    esac
done

# --- Validation ---
check_root

if [[ -n "$ONLY_STEP" ]]; then
    # Only mode - run single step
    case $ONLY_STEP in
        network|packages|brokkr|hardware|sindri|pyltg|hamma)
            ;;
        *)
            log_error "Invalid --only step: $ONLY_STEP"
            echo "Valid steps: network, packages, brokkr, hardware, sindri, pyltg, hamma"
            exit 1
            ;;
    esac
fi

# Check sensor number for steps that need it
if [[ -z "$SENSOR_NUM" ]]; then
    # Steps that don't need sensor number: packages, hardware, pyltg, hamma
    if [[ "$ONLY_STEP" != "packages" && "$ONLY_STEP" != "hardware" && \
          "$ONLY_STEP" != "pyltg" && "$ONLY_STEP" != "hamma" ]]; then
        log_error "Sensor number required. Use -n <number>"
        exit 1
    fi
fi

# --- Main Installation ---
echo ""
echo -e "${BLUE}======================================${NC}"
echo -e "${BLUE}  HAMMA Pi Installation${NC}"
echo -e "${BLUE}======================================${NC}"
echo ""
if [[ -n "$SENSOR_NUM" ]]; then
    echo "Sensor Number: $SENSOR_NUM"
fi
if [[ "$USE_WIFI" == "true" ]]; then
    echo "Network: WiFi"
else
    echo "Network: Cellular (APN: $APN)"
fi
echo ""

# --- Step 1: Network Setup (FIRST - uses pre-installed packages) ---
wait_for_connectivity() {
    local max_attempts=30
    local attempt=1
    log_step "Waiting for network connectivity..."
    while [[ $attempt -le $max_attempts ]]; do
        if ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; then
            log_success "Network connectivity established"
            return 0
        fi
        echo "  Attempt $attempt/$max_attempts - waiting..."
        sleep 5
        ((attempt++))
    done
    log_error "Failed to establish network connectivity after $max_attempts attempts"
    return 1
}

run_network() {
    if [[ "$USE_WIFI" == "true" ]]; then
        log_step "Setting up WiFi..."
        bash "$SCRIPT_DIR/setup_uah_wireless.sh" "$SENSOR_NUM"
        log_success "WiFi configured"
    else
        log_step "Setting up cellular (WWAN)..."
        bash "$SCRIPT_DIR/setup_wwan.sh" --apn "$APN"
        log_success "Cellular configured"

        # Trigger connection now (don't wait for timer)
        log_step "Establishing cellular connection..."
        bash /usr/local/bin/wwan-check.sh || true
    fi

    # Wait for actual connectivity before proceeding
    wait_for_connectivity
}

if [[ "$ONLY_STEP" == "network" ]] || [[ -z "$ONLY_STEP" && "$SKIP_NETWORK" == "false" ]]; then
    run_network
fi

# --- Step 2: Install Packages (needs network) ---
run_packages() {
    log_step "Installing system packages..."
    bash "$SCRIPT_DIR/install_packages.sh"
    log_success "Packages installed"
}

if [[ "$ONLY_STEP" == "packages" ]] || [[ -z "$ONLY_STEP" && "$SKIP_PACKAGES" == "false" ]]; then
    run_packages
fi

# --- Step 3: Brokkr Setup (needs network for git clone) ---
run_brokkr() {
    log_step "Installing and configuring Brokkr..."

    # Run as pi user for venv creation
    sudo -u pi bash "$SCRIPT_DIR/setup_brokkr.sh" "$SENSOR_NUM"

    log_success "Brokkr installed and configured"

    # Start brokkr service
    log_step "Starting Brokkr service..."
    systemctl start brokkr-hamma-default.service || log_warn "Could not start Brokkr service"
}

if [[ "$ONLY_STEP" == "brokkr" ]] || [[ -z "$ONLY_STEP" && "$SKIP_BROKKR" == "false" ]]; then
    run_brokkr
fi

# --- Step 4: Hardware Setup (no network needed) ---
run_hardware() {
    log_step "Setting up hardware (sensor connection, automount)..."
    bash "$SCRIPT_DIR/setup_hardware.sh"
    log_success "Hardware configured"
}

if [[ "$ONLY_STEP" == "hardware" ]] || [[ -z "$ONLY_STEP" && "$SKIP_HARDWARE" == "false" ]]; then
    run_hardware
fi

# --- Step 5: Sindri Setup (needs network for git clone) ---
run_sindri() {
    log_step "Installing Sindri..."
    sudo -u pi bash "$SCRIPT_DIR/install_sindri.sh" "$SENSOR_NUM"
    log_success "Sindri installed"
}

if [[ "$ONLY_STEP" == "sindri" ]] || [[ -z "$ONLY_STEP" && "$SKIP_SINDRI" == "false" ]]; then
    run_sindri
fi

# --- Step 6: PyLtg Setup (needs network for git clone) ---
run_pyltg() {
    log_step "Installing PyLtg..."
    sudo -u pi bash "$SCRIPT_DIR/install_pyltg.sh"
    log_success "PyLtg installed"
}

if [[ "$ONLY_STEP" == "pyltg" ]] || [[ -z "$ONLY_STEP" && "$SKIP_PYLTG" == "false" ]]; then
    run_pyltg
fi

# --- Step 7: HAMMA Setup (needs SSH key for private repo) ---
run_hamma() {
    log_step "Installing HAMMA..."
    # Check if SSH key exists for github-hamma
    if [[ ! -f /home/pi/.ssh/id_ed25519 ]]; then
        log_warn "SSH key not found. Generating key for GitHub access..."
        sudo -u pi bash "$SCRIPT_DIR/install_hamma.sh" -k
        echo ""
        log_warn "SSH key generated. You must add this public key to GitHub before continuing:"
        cat /home/pi/.ssh/id_ed25519.pub
        echo ""
        log_error "Add the key above to GitHub (deploy key or account), then re-run with --only hamma"
        return 1
    fi
    sudo -u pi bash "$SCRIPT_DIR/install_hamma.sh"
    log_success "HAMMA installed"
}

if [[ "$ONLY_STEP" == "hamma" ]] || [[ -z "$ONLY_STEP" && "$SKIP_HAMMA" == "false" ]]; then
    run_hamma
fi

# --- Summary ---
echo ""
echo -e "${GREEN}======================================${NC}"
echo -e "${GREEN}  Installation Complete${NC}"
echo -e "${GREEN}======================================${NC}"
echo ""
echo "Completed steps:"
[[ "$SKIP_NETWORK" == "false" || "$ONLY_STEP" == "network" ]] && echo "  - Network ($([[ "$USE_WIFI" == "true" ]] && echo "WiFi" || echo "Cellular"))"
[[ "$SKIP_PACKAGES" == "false" || "$ONLY_STEP" == "packages" ]] && echo "  - System packages"
[[ "$SKIP_BROKKR" == "false" || "$ONLY_STEP" == "brokkr" ]] && echo "  - Brokkr"
[[ "$SKIP_HARDWARE" == "false" || "$ONLY_STEP" == "hardware" ]] && echo "  - Hardware"
[[ "$SKIP_SINDRI" == "false" || "$ONLY_STEP" == "sindri" ]] && echo "  - Sindri"
[[ "$SKIP_PYLTG" == "false" || "$ONLY_STEP" == "pyltg" ]] && echo "  - PyLtg"
[[ "$SKIP_HAMMA" == "false" || "$ONLY_STEP" == "hamma" ]] && echo "  - HAMMA"
echo ""
echo "Remaining manual steps:"
echo "  1. Format drives: ../scripts/format_drives.sh -m /dev/sda -n NUM"
echo "  2. Server connection (see Confluence: MjolnirPi Setup)"
echo ""
echo "Verify with:"
echo "  source /home/pi/ltgenv && brokkr status"
echo ""
