#!/bin/bash
# E2E test for sensors.py — runs on a real sensor.
# WARNING: This toggles real hardware (relay, brokkr service).
#
# Prerequisites:
#   - Run on a Pi with sensors.py deployed
#   - [relay] section configured in ~/.config/brokkr/hamma/unit.toml
#   - HAMMA_E2E=1 environment variable set
#
# Usage:
#   HAMMA_E2E=1 bash tests/e2e/test_sensors_e2e.sh

set -e

if [ "$HAMMA_E2E" != "1" ]; then
    echo "Skipping E2E test (set HAMMA_E2E=1 to run)"
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SENSORS="$SCRIPT_DIR/scripts/sensors.py"
DROPIN="/etc/systemd/system/brokkr-hamma-default.service.d/mode.conf"
PASS=0
FAIL=0

check() {
    local desc="$1"
    shift
    if "$@" > /dev/null 2>&1; then
        echo "[PASS] $desc"
        ((PASS++)) || true
    else
        echo "[FAIL] $desc"
        ((FAIL++)) || true
    fi
}

check_not() {
    local desc="$1"
    shift
    if ! "$@" > /dev/null 2>&1; then
        echo "[PASS] $desc"
        ((PASS++)) || true
    else
        echo "[FAIL] $desc"
        ((FAIL++)) || true
    fi
}

echo "=== E2E Test: sensors.py ==="
echo ""

# Create timestamp marker for telemetry check
touch /tmp/e2e_start_marker

# Record initial state
echo "--- Initial state ---"
$SENSORS --status
echo ""

# Test 1: Turn sensor off
echo "--- Test 1: sensors.py --off ---"
$SENSORS --off
check "Drop-in exists" test -f "$DROPIN"
check "Brokkr is active" systemctl is-active brokkr-hamma-default.service
# Wait for sensor to power down
sleep 3
check_not "Sensor unreachable" ping -c 1 -W 2 10.10.10.1
echo ""

# Test 2: Idempotency — off again
echo "--- Test 2: sensors.py --off (idempotent) ---"
$SENSORS --off
check "Drop-in still exists" test -f "$DROPIN"
check "Brokkr still active" systemctl is-active brokkr-hamma-default.service
echo ""

# Test 3: Turn sensor on
echo "--- Test 3: sensors.py --on ---"
$SENSORS --on
check_not "Drop-in removed" test -f "$DROPIN"
check "Brokkr is active" systemctl is-active brokkr-hamma-default.service
# Wait for sensor to power up
sleep 5
check "Sensor reachable" ping -c 1 -W 2 10.10.10.1
echo ""

# Test 4: Idempotency — on again
echo "--- Test 4: sensors.py --on (idempotent) ---"
$SENSORS --on
check_not "Drop-in still absent" test -f "$DROPIN"
check "Brokkr still active" systemctl is-active brokkr-hamma-default.service
echo ""

# Test 5: Verify telemetry CSV archive
echo "--- Test 5: Telemetry CSV archive ---"
TELEMETRY_DIR="$HOME/brokkr/hamma/telemetry"
if [ -d "$TELEMETRY_DIR" ]; then
    BAK_COUNT=$(find "$TELEMETRY_DIR" -name '*.csv.bak*' -newer /tmp/e2e_start_marker 2>/dev/null | wc -l)
    check "Telemetry CSV archived (.bak exists)" test "$BAK_COUNT" -gt 0
else
    echo "[SKIP] Telemetry directory not found"
fi
echo ""

# Summary
echo "=== Results: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
