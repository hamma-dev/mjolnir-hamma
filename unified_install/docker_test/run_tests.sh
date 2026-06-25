#!/bin/bash
# Complete test of HAMMA install scripts in Docker
#
# This script:
# 1. Builds a Docker container simulating Raspberry Pi OS
# 2. Runs through the ORIGINAL install scripts
# 3. Runs through the UNIFIED install scripts
# 4. Compares results and reports errors

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMAGE_NAME="hamma-pi-test"
CONTAINER_NAME="hamma-test"

echo "=== HAMMA Install Scripts Test Suite ==="
echo "Repository: $REPO_DIR"
echo ""

# Build the Docker image
echo "[1/4] Building Docker image..."
docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"
echo ""

# Function to run a test in Docker
run_docker_test() {
    local test_name="$1"
    local test_script="$2"

    echo "=== Running: $test_name ==="

    # Remove old container if exists
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

    # Run container with repo mounted
    docker run -d --name "$CONTAINER_NAME" \
        -v "$REPO_DIR:/mnt/usb/mjolnir-hamma:ro" \
        "$IMAGE_NAME" \
        sleep infinity

    # Copy repo to simulate USB copy (bootstrap does this)
    docker exec "$CONTAINER_NAME" bash -c "cp -r /mnt/usb/mjolnir-hamma /home/pi/dev/"

    # Run the test script
    docker exec "$CONTAINER_NAME" bash -c "$test_script" || {
        echo "ERROR: $test_name failed"
        docker logs "$CONTAINER_NAME"
        return 1
    }

    # Cleanup
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

    echo ""
}

# Test 1: Original scripts - WWAN setup
echo "[2/4] Testing ORIGINAL scripts..."

run_docker_test "Original setup_wwan.sh (dry run analysis)" '
echo "=== Analyzing Original setup_wwan.sh ==="
cd /home/pi/dev/mjolnir-hamma/install_scripts

echo ""
echo "Checking for resolv.conf manipulation..."
grep -n "resolv" setup_wwan.sh || echo "  -> No resolv.conf manipulation (EXPECTED)"

echo ""
echo "Checking for systemd-resolved..."
grep -n "systemd-resolved" setup_wwan.sh || echo "  -> No systemd-resolved (EXPECTED)"

echo ""
echo "Checking required files exist..."
ls -la /home/pi/dev/mjolnir-hamma/files/20-wwan0.network || echo "MISSING: 20-wwan0.network"
ls -la /home/pi/dev/mjolnir-hamma/files/wwan-check.sh || echo "MISSING: wwan-check.sh"
ls -la /home/pi/dev/mjolnir-hamma/files/wwan-check.timer || echo "MISSING: wwan-check.timer"
ls -la /home/pi/dev/mjolnir-hamma/files/wwan-check.service || echo "MISSING: wwan-check.service"
ls -la /home/pi/dev/mjolnir-hamma/scripts/50_bring_wwan0_up.py || echo "MISSING: 50_bring_wwan0_up.py"

echo ""
echo "=== Original setup_wwan.sh Analysis Complete ==="
'

run_docker_test "Original setup_uah_wireless.sh (dry run analysis)" '
echo "=== Analyzing Original setup_uah_wireless.sh ==="
cd /home/pi/dev/mjolnir-hamma/install_scripts

echo ""
echo "Checking resolv.conf manipulation..."
grep -n "resolv" setup_uah_wireless.sh

echo ""
echo "Expected: Uses /run/systemd/resolve/resolv.conf (NOT stub)"
echo ""

echo "Checking required files exist..."
ls -la /home/pi/dev/mjolnir-hamma/files/10-wlan0.network || echo "MISSING: 10-wlan0.network"
ls -la /home/pi/dev/mjolnir-hamma/files/override.conf || echo "MISSING: override.conf"
ls -la /home/pi/dev/mjolnir-hamma/files/wpa_supplicant-wlan0.conf || echo "MISSING: wpa_supplicant-wlan0.conf"

echo ""
echo "=== Original setup_uah_wireless.sh Analysis Complete ==="
'

# Test 2: Unified scripts - Bootstrap dry run
echo "[3/4] Testing UNIFIED scripts..."

run_docker_test "Unified bootstrap.sh --dry-run" '
echo "=== Testing Unified bootstrap.sh ==="
cd /home/pi/dev/mjolnir-hamma/unified_install

echo ""
echo "Checking resolv.conf in bootstrap.sh..."
grep -n "resolv" bootstrap.sh

echo ""
echo "Running dry-run..."
chmod +x bootstrap.sh
./bootstrap.sh 1 --wifi-ssid TestNetwork --wifi-pass testpass --dry-run 2>&1 || true

echo ""
echo "=== Unified bootstrap.sh Test Complete ==="
'

run_docker_test "Unified install.sh --dry-run (cellular)" '
echo "=== Testing Unified install.sh (cellular path) ==="
cd /home/pi/dev/mjolnir-hamma/unified_install

echo ""
echo "Checking lib/network_wwan.sh for resolv.conf..."
grep -n "resolv" lib/network_wwan.sh || echo "  -> No resolv.conf manipulation (CORRECT)"

echo ""
echo "Checking lib/network_wifi.sh for resolv.conf..."
grep -n "resolv" lib/network_wifi.sh

echo ""
echo "Running dry-run..."
chmod +x install.sh
sudo ./install.sh 1 --apn vzwinternet --dry-run 2>&1 || true

echo ""
echo "=== Unified install.sh Test Complete ==="
'

# Test 3: Full simulation
echo "[4/4] Full install simulation..."

run_docker_test "Full unified install simulation" '
echo "=== Full Install Simulation ==="
cd /home/pi/dev/mjolnir-hamma/unified_install

# Set FILES_DIR since we are not running from USB
export FILES_DIR=/home/pi/dev/mjolnir-hamma/files
export SCRIPTS_DIR=/home/pi/dev/mjolnir-hamma/scripts

echo ""
echo "Step 1: Bootstrap (skip network-dependent parts)..."
chmod +x bootstrap.sh

# Manually run bootstrap steps that dont need network
echo "  - Setting timezone..."
sudo timedatectl set-timezone UTC 2>/dev/null || echo "    (timedatectl not available in container)"

echo "  - Setting hostname..."
echo "mjolnir01" | sudo tee /etc/hostname > /dev/null
sudo sed -i "s/127.0.1.1.*/127.0.1.1       mjolnir01/" /etc/hosts 2>/dev/null || true

echo "  - Checking config.txt..."
if ! grep -q "dtoverlay=disable-wifi" /boot/config.txt 2>/dev/null; then
    echo "dtoverlay=disable-wifi" | sudo tee -a /boot/config.txt > /dev/null
fi

echo ""
echo "Step 2: Network setup simulation..."
echo "  - Testing network_wwan.sh functions..."
source lib/common.sh
DRY_RUN=true
init_common --dry-run

# Check if network_wwan.sh sources properly
source lib/network_wwan.sh 2>&1 || echo "  ERROR sourcing network_wwan.sh"

echo "  - Verifying resolv.conf is NOT touched in WWAN path..."
if grep -q "stub-resolv" lib/network_wwan.sh; then
    echo "  ERROR: stub-resolv.conf found in network_wwan.sh!"
else
    echo "  OK: No stub-resolv.conf in network_wwan.sh"
fi

echo ""
echo "Step 3: Checking all required files..."
MISSING_FILES=""
check_file() {
    if [[ ! -f "$1" ]]; then
        echo "  MISSING: $1"
        MISSING_FILES="$MISSING_FILES $1"
    else
        echo "  OK: $1"
    fi
}

check_file "/home/pi/dev/mjolnir-hamma/files/20-wwan0.network"
check_file "/home/pi/dev/mjolnir-hamma/files/30-eth1.network"
check_file "/home/pi/dev/mjolnir-hamma/files/40-eth0.network"
check_file "/home/pi/dev/mjolnir-hamma/files/10-wlan0.network"
check_file "/home/pi/dev/mjolnir-hamma/files/wwan-check.sh"
check_file "/home/pi/dev/mjolnir-hamma/files/wwan-check.timer"
check_file "/home/pi/dev/mjolnir-hamma/files/wwan-check.service"
check_file "/home/pi/dev/mjolnir-hamma/scripts/50_bring_wwan0_up.py"
check_file "/home/pi/dev/mjolnir-hamma/files/override.conf"
check_file "/home/pi/dev/mjolnir-hamma/files/wpa_supplicant-wlan0.conf"

echo ""
if [[ -n "$MISSING_FILES" ]]; then
    echo "ERROR: Missing files detected!"
else
    echo "All required files present."
fi

echo ""
echo "Step 4: Syntax check all scripts..."
for script in bootstrap.sh install.sh lib/*.sh; do
    bash -n "$script" && echo "  OK: $script" || echo "  ERROR: $script has syntax errors"
done

echo ""
echo "=== Full Install Simulation Complete ==="
'

echo ""
echo "=== All Tests Complete ==="
echo ""
echo "Review output above for any errors or issues."
