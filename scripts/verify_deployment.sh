#!/bin/bash
#
# verify_deployment.sh - Verify Pi is ready for production deployment
#
# Run this script AFTER install and reboot to verify everything works.
#
# Usage:
#   ./verify_deployment.sh           # Basic checks (before server setup)
#   ./verify_deployment.sh --full    # Full checks (after server setup)
#   ./verify_deployment.sh --help    # Show help
#
# Exit codes:
#   0 = All checks passed (ready for deployment)
#   1 = Some checks failed (not ready)
#

set -euo pipefail

# --- Configuration ---
SERVER_HOST="www.hamma.dev"
PING_TARGET="8.8.8.8"
PING_TIMEOUT=5

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# --- Counters ---
PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0
SKIP_COUNT=0

# --- Helper Functions ---

print_header() {
    echo ""
    echo -e "${BLUE}══════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}══════════════════════════════════════════════════════════════${NC}"
    echo ""
}

print_section() {
    echo ""
    echo -e "${BLUE}── $1 ──${NC}"
}

pass() {
    echo -e "  ${GREEN}✓ PASS${NC}: $1"
    ((PASS_COUNT++))
}

fail() {
    echo -e "  ${RED}✗ FAIL${NC}: $1"
    ((FAIL_COUNT++))
}

warn() {
    echo -e "  ${YELLOW}! WARN${NC}: $1"
    ((WARN_COUNT++))
}

skip() {
    echo -e "  ${YELLOW}○ SKIP${NC}: $1"
    ((SKIP_COUNT++))
}

info() {
    echo -e "  ${BLUE}ℹ${NC} $1"
}

# Check if a systemd service is active
check_service_active() {
    local service="$1"
    local description="$2"

    if systemctl is-active --quiet "$service" 2>/dev/null; then
        pass "$description"
        return 0
    else
        fail "$description"
        return 1
    fi
}

# Check if a systemd service is enabled
check_service_enabled() {
    local service="$1"
    local description="$2"

    local status
    status=$(systemctl is-enabled "$service" 2>/dev/null || echo "not-found")

    if [[ "$status" == "enabled" ]]; then
        pass "$description"
        return 0
    elif [[ "$status" == "not-found" ]]; then
        skip "$description (service not found)"
        return 2
    else
        fail "$description (status: $status)"
        return 1
    fi
}

# Check if file exists and is owned by expected user
check_file_ownership() {
    local filepath="$1"
    local expected_owner="$2"
    local description="$3"

    if [[ ! -e "$filepath" ]]; then
        fail "$description (file not found)"
        return 1
    fi

    local actual_owner
    actual_owner=$(stat -c %U "$filepath" 2>/dev/null)

    if [[ "$actual_owner" == "$expected_owner" ]]; then
        pass "$description"
        return 0
    else
        fail "$description (owned by $actual_owner, expected $expected_owner)"
        return 1
    fi
}

# --- Check Functions ---

check_network_connectivity() {
    print_section "Network Connectivity"

    # Check ping
    if ping -c 1 -W "$PING_TIMEOUT" "$PING_TARGET" > /dev/null 2>&1; then
        pass "Internet reachable (ping $PING_TARGET)"
    else
        fail "Cannot reach internet (ping $PING_TARGET failed)"
    fi

    # Check DNS
    if host google.com > /dev/null 2>&1; then
        pass "DNS resolution working"
    elif nslookup google.com > /dev/null 2>&1; then
        pass "DNS resolution working"
    else
        fail "DNS resolution failed"
    fi

    # Check which network interface is up
    if ip addr show wwan0 2>/dev/null | grep -q "inet "; then
        info "Network: Cellular (wwan0)"
    elif ip addr show wlan0 2>/dev/null | grep -q "inet "; then
        info "Network: WiFi (wlan0)"
    elif ip addr show eth0 2>/dev/null | grep -q "inet "; then
        info "Network: Ethernet (eth0)"
    else
        warn "Could not determine active network interface"
    fi
}

check_essential_services() {
    print_section "Essential Services"

    # Brokkr service
    check_service_active "brokkr-hamma-default.service" "Brokkr service running"
    check_service_enabled "brokkr-hamma-default.service" "Brokkr service enabled (starts on boot)"

    # Autossh service
    check_service_active "autossh-hamma-default.service" "Autossh service running"
    check_service_enabled "autossh-hamma-default.service" "Autossh service enabled (starts on boot)"
}

check_cellular_services() {
    print_section "Cellular Services"

    # Check if this is a cellular setup
    if [[ ! -f /etc/systemd/system/wwan-check.timer ]]; then
        skip "Not a cellular setup (wwan-check.timer not found)"
        return
    fi

    check_service_active "wwan-check.timer" "WWAN check timer running"
    check_service_enabled "wwan-check.timer" "WWAN check timer enabled"

    # Check modem status
    if command -v mmcli > /dev/null 2>&1; then
        if mmcli -L 2>/dev/null | grep -q "Modem"; then
            pass "Modem detected"

            # Check if connected
            local state
            state=$(mmcli -m 0 2>/dev/null | grep -i "state:" | head -1 | awk '{print $NF}' || echo "unknown")
            if [[ "$state" == "connected" ]]; then
                pass "Modem connected"
            else
                warn "Modem state: $state (expected: connected)"
            fi
        else
            warn "No modem detected (may be normal if modem is resetting)"
        fi
    else
        skip "mmcli not available"
    fi
}

check_wifi_services() {
    print_section "WiFi Services"

    # Check if this is a WiFi setup
    if [[ ! -f /etc/wpa_supplicant/wpa_supplicant-wlan0.conf ]]; then
        skip "Not a WiFi setup (wpa_supplicant config not found)"
        return
    fi

    check_service_active "wpa_supplicant@wlan0.service" "WPA supplicant running"
    check_service_enabled "wpa_supplicant@wlan0.service" "WPA supplicant enabled"
    check_service_active "systemd-networkd.service" "systemd-networkd running"
    check_service_enabled "systemd-networkd.service" "systemd-networkd enabled"
}

check_file_setup() {
    print_section "File Setup"

    # SSH keys
    check_file_ownership "/home/pi/.ssh/id_rsa" "pi" "SSH private key owned by pi"

    if [[ -f /home/pi/.ssh/id_rsa ]]; then
        local perms
        perms=$(stat -c %a /home/pi/.ssh/id_rsa)
        if [[ "$perms" == "600" ]]; then
            pass "SSH private key has correct permissions (600)"
        else
            fail "SSH private key has wrong permissions ($perms, expected 600)"
        fi
    fi

    # Brokkr venv
    check_file_ownership "/home/pi/dev/ltgenv" "pi" "Brokkr venv owned by pi"

    # Brokkr config
    check_file_ownership "/home/pi/.config/brokkr" "pi" "Brokkr config owned by pi"

    # Check for bad root artifacts
    if [[ -d /root/.config/brokkr ]]; then
        fail "Found /root/.config/brokkr (should not exist)"
    else
        pass "No root config artifacts"
    fi
}

check_brokkr_status() {
    print_section "Brokkr Status"

    if [[ ! -f /home/pi/dev/ltgenv/bin/activate ]]; then
        fail "Brokkr venv not found"
        return
    fi

    # Try to run brokkr status
    local status_output
    if status_output=$(sudo -u pi bash -c "source /home/pi/ltgenv && brokkr status" 2>&1); then
        pass "Brokkr status command works"

        # Check for critical errors (but allow N/A for disconnected hardware)
        if echo "$status_output" | grep -qi "error\|exception\|traceback"; then
            warn "Brokkr status shows errors (may be OK if hardware not connected)"
        else
            pass "Brokkr status shows no errors"
        fi
    else
        fail "Brokkr status command failed"
        info "Output: $status_output"
    fi
}

check_server_connection() {
    print_section "Server Connection"

    if [[ ! -f /home/pi/.ssh/id_rsa ]]; then
        skip "SSH key not found - cannot test server connection"
        return
    fi

    # Test SSH to server
    info "Testing SSH connection to $SERVER_HOST..."

    if sudo -u pi ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$SERVER_HOST" "echo 'connected'" 2>/dev/null | grep -q "connected"; then
        pass "SSH to server works"
    else
        fail "SSH to server failed"
        echo ""
        echo -e "  ${YELLOW}Server setup may be needed:${NC}"
        echo "  1. Copy this public key to the server:"
        echo ""
        echo -e "     ${BLUE}$(cat /home/pi/.ssh/id_rsa.pub 2>/dev/null || echo '[key not found]')${NC}"
        echo ""
        echo "  2. On server, add to /home/pi/.ssh/authorized_keys"
        echo "  3. On server, add to /home/pi/.ssh/config:"
        echo "     Host $(hostname)"
        echo "         Port 100XX  # Replace XX with sensor number"
        echo ""
    fi
}

print_summary() {
    print_header "DEPLOYMENT VERIFICATION SUMMARY"

    echo -e "  ${GREEN}Passed${NC}:  $PASS_COUNT"
    echo -e "  ${RED}Failed${NC}:  $FAIL_COUNT"
    echo -e "  ${YELLOW}Warnings${NC}: $WARN_COUNT"
    echo -e "  ${YELLOW}Skipped${NC}: $SKIP_COUNT"
    echo ""

    if [[ $FAIL_COUNT -eq 0 ]]; then
        echo -e "  ${GREEN}╔═══════════════════════════════════════╗${NC}"
        echo -e "  ${GREEN}║     READY FOR DEPLOYMENT  ✓           ║${NC}"
        echo -e "  ${GREEN}╚═══════════════════════════════════════╝${NC}"
        echo ""
        return 0
    else
        echo -e "  ${RED}╔═══════════════════════════════════════╗${NC}"
        echo -e "  ${RED}║     NOT READY - $FAIL_COUNT FAILURE(S)            ║${NC}"
        echo -e "  ${RED}╚═══════════════════════════════════════╝${NC}"
        echo ""
        echo "  Review failures above before deploying."
        echo ""
        return 1
    fi
}

print_ssh_key() {
    print_section "SSH Public Key (for server setup)"

    if [[ -f /home/pi/.ssh/id_rsa.pub ]]; then
        echo ""
        echo "  Copy this key to the server's /home/pi/.ssh/authorized_keys:"
        echo ""
        echo -e "  ${BLUE}$(cat /home/pi/.ssh/id_rsa.pub)${NC}"
        echo ""
    else
        echo ""
        echo -e "  ${RED}SSH public key not found at /home/pi/.ssh/id_rsa.pub${NC}"
        echo ""
    fi
}

show_help() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Verify that a HAMMA Pi is ready for production deployment."
    echo ""
    echo "Options:"
    echo "  --full        Run full checks including server connection test"
    echo "  --show-key    Just print the SSH public key (for server setup)"
    echo "  --help        Show this help message"
    echo ""
    echo "Workflow:"
    echo "  1. Run install, then reboot the Pi"
    echo "  2. Run: $0"
    echo "  3. Copy SSH key to server (see output)"
    echo "  4. Run: $0 --full"
    echo "  5. If all checks pass, Pi is ready for deployment"
    echo ""
}

# --- Main ---

main() {
    local full_check=false
    local show_key_only=false

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            --full|-f)
                full_check=true
                shift
                ;;
            --show-key|-k)
                show_key_only=true
                shift
                ;;
            --help|-h)
                show_help
                exit 0
                ;;
            *)
                echo "Unknown option: $1"
                show_help
                exit 1
                ;;
        esac
    done

    # Just show key and exit
    if $show_key_only; then
        print_ssh_key
        exit 0
    fi

    # Print header
    print_header "HAMMA Pi Deployment Verification"
    echo "  Hostname: $(hostname)"
    echo "  Date:     $(date)"
    echo "  Mode:     $(if $full_check; then echo 'Full (with server check)'; else echo 'Basic'; fi)"

    # Run checks
    check_network_connectivity
    check_essential_services
    check_cellular_services
    check_wifi_services
    check_file_setup
    check_brokkr_status

    if $full_check; then
        check_server_connection
    else
        print_section "Server Connection"
        skip "Use --full to test server connection"
        echo ""
        print_ssh_key
    fi

    # Print summary and exit with appropriate code
    print_summary
}

main "$@"
