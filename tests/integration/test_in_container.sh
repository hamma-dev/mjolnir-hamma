#!/bin/bash
# Integration tests that run inside the Docker container
# These test actual script execution in a Pi-like environment

set -e

SCRIPT_DIR="/home/pi/dev/mjolnir-hamma/install_scripts"
FILES_DIR="/home/pi/dev/mjolnir-hamma/files"
SCRIPTS_DIR="/home/pi/dev/mjolnir-hamma/scripts"
RESULTS_DIR="/test-results"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test counter
TESTS_PASSED=0
TESTS_FAILED=0

log_pass() {
    echo -e "${GREEN}[PASS]${NC} $1"
    ((TESTS_PASSED++))
}

log_fail() {
    echo -e "${RED}[FAIL]${NC} $1"
    ((TESTS_FAILED++))
}

log_skip() {
    echo -e "${YELLOW}[SKIP]${NC} $1"
}

log_info() {
    echo -e "[INFO] $1"
}

# --- Test Functions ---

test_bootstrap_structure() {
    log_info "Testing bootstrap.sh structure..."

    SCRIPT="$SCRIPT_DIR/bootstrap.sh"
    if [ ! -f "$SCRIPT" ]; then
        log_fail "bootstrap.sh not found"
        return
    fi

    # Check for hostname setting
    if grep -q "hostname" "$SCRIPT" && grep -q "mjolnir" "$SCRIPT"; then
        log_pass "bootstrap.sh: has hostname setting"
    else
        log_fail "bootstrap.sh: missing hostname setting"
    fi

    # Check for wifi disable
    if grep -q "disable-wifi" "$SCRIPT"; then
        log_pass "bootstrap.sh: has wifi disable"
    else
        log_fail "bootstrap.sh: missing wifi disable"
    fi

    # Check syntax
    if bash -n "$SCRIPT"; then
        log_pass "bootstrap.sh: syntax valid"
    else
        log_fail "bootstrap.sh: syntax errors"
    fi
}

test_install_driver() {
    log_info "Testing install.sh driver..."

    SCRIPT="$SCRIPT_DIR/install.sh"
    if [ ! -f "$SCRIPT" ]; then
        log_fail "install.sh not found"
        return
    fi

    # Check help works
    if bash "$SCRIPT" --help 2>&1 | grep -q "Usage"; then
        log_pass "install.sh: help works"
    else
        log_fail "install.sh: help failed"
    fi

    # Check syntax
    if bash -n "$SCRIPT"; then
        log_pass "install.sh: syntax valid"
    else
        log_fail "install.sh: syntax errors"
    fi
}

test_setup_hardware() {
    log_info "Testing setup_hardware.sh..."

    # Clear destination directories
    sudo rm -f /home/pi/.ssh/config
    sudo rm -f /etc/systemd/network/*eth*.network

    # Run the script
    if sudo bash "$SCRIPT_DIR/setup_hardware.sh"; then
        # Check SSH config
        if [ -f /home/pi/.ssh/config ]; then
            log_pass "setup_hardware.sh: SSH config copied"
        else
            log_fail "setup_hardware.sh: SSH config not found"
        fi

        # Check network files
        if ls /etc/systemd/network/*eth*.network >/dev/null 2>&1; then
            log_pass "setup_hardware.sh: network files copied"
        else
            log_fail "setup_hardware.sh: network files not found"
        fi
    else
        log_fail "setup_hardware.sh: script failed to run"
    fi
}

test_wwan_files_exist() {
    log_info "Testing WWAN files exist..."

    if [ -f "$SCRIPTS_DIR/50_bring_wwan0_up.py" ]; then
        log_pass "50_bring_wwan0_up.py exists (in scripts/)"
    else
        log_fail "50_bring_wwan0_up.py not found in scripts/"
    fi

    if [ -f "$FILES_DIR/wwan-check.sh" ]; then
        log_pass "wwan-check.sh exists"
    else
        log_fail "wwan-check.sh not found"
    fi

    if [ -f "$FILES_DIR/20-wwan0.network" ]; then
        log_pass "20-wwan0.network exists"
    else
        log_fail "20-wwan0.network not found"
    fi
}

test_wwan_python_syntax() {
    log_info "Testing WWAN Python script syntax..."

    if python3 -m py_compile "$SCRIPTS_DIR/50_bring_wwan0_up.py"; then
        log_pass "50_bring_wwan0_up.py: Python syntax valid"
    else
        log_fail "50_bring_wwan0_up.py: Python syntax errors"
    fi
}

test_setup_wwan_structure() {
    log_info "Testing setup_wwan.sh structure..."

    SCRIPT="$SCRIPT_DIR/setup_wwan.sh"
    if [ ! -f "$SCRIPT" ]; then
        log_fail "setup_wwan.sh not found"
        return
    fi

    # Check it uses timer-based approach
    if grep -q "wwan-check.timer" "$SCRIPT"; then
        log_pass "setup_wwan.sh: uses timer-based approach"
    else
        log_fail "setup_wwan.sh: missing timer-based approach"
    fi

    # Check syntax
    if bash -n "$SCRIPT"; then
        log_pass "setup_wwan.sh: bash syntax valid"
    else
        log_fail "setup_wwan.sh: bash syntax errors"
    fi
}

test_network_file_formats() {
    log_info "Testing network file formats..."

    # Check 20-wwan0.network
    if grep -q "\[Match\]" "$FILES_DIR/20-wwan0.network" && \
       grep -q "Name=wwan0" "$FILES_DIR/20-wwan0.network"; then
        log_pass "20-wwan0.network: format valid"
    else
        log_fail "20-wwan0.network: format invalid"
    fi

    # Check 30-eth1.network
    if grep -q "\[Match\]" "$FILES_DIR/30-eth1.network" && \
       grep -q "Name=eth1" "$FILES_DIR/30-eth1.network"; then
        log_pass "30-eth1.network: format valid"
    else
        log_fail "30-eth1.network: format invalid"
    fi
}

test_wwan_check_script() {
    log_info "Testing wwan-check.sh..."

    # Check it uses flock
    if grep -q "flock" "$FILES_DIR/wwan-check.sh"; then
        log_pass "wwan-check.sh: uses flock"
    else
        log_fail "wwan-check.sh: missing flock"
    fi

    # Check shebang
    if head -1 "$FILES_DIR/wwan-check.sh" | grep -q "^#!"; then
        log_pass "wwan-check.sh: has shebang"
    else
        log_fail "wwan-check.sh: missing shebang"
    fi
}

# --- Main ---

main() {
    echo "========================================"
    echo "Mjolnir-HAMMA Integration Tests"
    echo "========================================"
    echo ""

    # Run all tests
    test_bootstrap_structure
    test_install_driver
    test_wwan_files_exist
    test_wwan_python_syntax
    test_network_file_formats
    test_wwan_check_script
    test_setup_wwan_structure
    test_setup_hardware

    # Summary
    echo ""
    echo "========================================"
    echo "Test Results"
    echo "========================================"
    echo -e "${GREEN}Passed: $TESTS_PASSED${NC}"
    echo -e "${RED}Failed: $TESTS_FAILED${NC}"
    echo ""

    # Write results to file
    if [ -d "$RESULTS_DIR" ]; then
        echo "passed=$TESTS_PASSED" > "$RESULTS_DIR/summary.txt"
        echo "failed=$TESTS_FAILED" >> "$RESULTS_DIR/summary.txt"
        echo "date=$(date -Iseconds)" >> "$RESULTS_DIR/summary.txt"
    fi

    # Exit with failure if any tests failed
    if [ $TESTS_FAILED -gt 0 ]; then
        exit 1
    fi
}

main "$@"
