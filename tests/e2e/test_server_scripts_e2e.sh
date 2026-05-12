#!/usr/bin/env bash
set -e

# E2E tests for server-side scripts.
# Run with: HAMMA_E2E=1 bash tests/e2e/test_server_scripts_e2e.sh
#
# VPS-side tests require SSH access to monitor@hamma.dev.
# Sensor-side tests require SSH access to mj00 (lab sensor) via tunnel.

if [[ "${HAMMA_E2E}" != "1" ]]; then
    echo "Skipping E2E tests (set HAMMA_E2E=1 to run)"
    exit 0
fi

PASS=0
FAIL=0
VPS_PYTHON="/home/monitor/dev/ltgenv/bin/python"

run_test() {
    local name="$1"
    shift
    echo -n "  $name ... "
    if "$@" > /dev/null 2>&1; then
        echo "PASS"
        ((PASS++)) || true
    else
        echo "FAIL"
        ((FAIL++)) || true
    fi
}

echo "=== VPS-side E2E tests ==="

# Test: webgen produces valid HTML for aumma (smallest array)
run_test "webgen -a aumma produces HTML" \
    ssh monitor@hamma.dev \
    "cd ~/dev/mjolnir-hamma && $VPS_PYTHON server/webgen.py -a aumma"

# Test: mjol_array --status returns output for aumma
run_test "mjol_array --status -a aumma" \
    ssh monitor@hamma.dev \
    "cd ~/dev/mjolnir-hamma && $VPS_PYTHON server/mjol_array.py --status -a aumma"

echo ""
echo "=== Sensor-side E2E tests ==="

# Test: mjol_array --down on mj00 (lab sensor)
# This actually powers down the sensor -- only run on mj00!
run_test "mjol_array --down mj00" \
    ssh monitor@hamma.dev \
    "cd ~/dev/mjolnir-hamma && $VPS_PYTHON server/mjol_array.py --down -p 0"

# Give sensors.py time to complete all steps
sleep 10

# Test: mjol_array --up on mj00 (restore)
run_test "mjol_array --up mj00" \
    ssh monitor@hamma.dev \
    "cd ~/dev/mjolnir-hamma && $VPS_PYTHON server/mjol_array.py --up -p 0"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ "$FAIL" -eq 0 ]]
