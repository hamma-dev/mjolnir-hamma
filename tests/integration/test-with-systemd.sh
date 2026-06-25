#!/bin/bash
# Test install scripts in a Docker container WITH systemd
# This allows testing systemctl commands that would fail in regular containers
#
# Usage:
#   ./test-with-systemd.sh                    # Start interactive container
#   ./test-with-systemd.sh --rebuild          # Rebuild image first
#   ./test-with-systemd.sh --run-install      # Run install.sh automatically
#   ./test-with-systemd.sh --stop             # Stop and remove container

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
CONTAINER_NAME="mjolnir-systemd-test"
IMAGE_NAME="mjolnir-systemd-test:latest"
RESULTS_DIR="$SCRIPT_DIR/results"

# Parse args
ACTION="interactive"
REBUILD=false
NETWORK_MODE="cellular"
SENSOR_NUM="99"

while [[ $# -gt 0 ]]; do
    case $1 in
        --rebuild|-r)
            REBUILD=true
            shift
            ;;
        --run-install)
            ACTION="install"
            shift
            ;;
        --verify)
            ACTION="verify"
            shift
            ;;
        --full-test)
            ACTION="full"
            shift
            ;;
        --stop)
            ACTION="stop"
            shift
            ;;
        --cellular)
            NETWORK_MODE="cellular"
            shift
            ;;
        --wifi)
            NETWORK_MODE="wifi"
            shift
            ;;
        --sensor)
            SENSOR_NUM="$2"
            shift 2
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --rebuild, -r     Force rebuild the Docker image"
            echo "  --run-install     Run install.sh inside the container"
            echo "  --verify          Run integration tests (pytest) to verify install"
            echo "  --full-test       Run install followed by verification tests"
            echo "  --stop            Stop and remove the container"
            echo "  --cellular        Use cellular network mode (default)"
            echo "  --wifi            Use WiFi network mode"
            echo "  --sensor NUM      Sensor number (default: 99)"
            echo ""
            echo "Examples:"
            echo "  $0                    # Start container, drop into shell"
            echo "  $0 --run-install      # Run full install test"
            echo "  $0 --verify           # Run pytest integration tests"
            echo "  $0 --full-test        # Install + verify (recommended)"
            echo "  $0 --stop             # Clean up container"
            echo ""
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Handle stop action
if [[ "$ACTION" == "stop" ]]; then
    echo "Stopping container..."
    docker stop "$CONTAINER_NAME" 2>/dev/null || true
    docker rm "$CONTAINER_NAME" 2>/dev/null || true
    echo "Container stopped and removed."
    exit 0
fi

# Build if needed or requested
if $REBUILD || ! docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^${IMAGE_NAME}$"; then
    echo "Building systemd-enabled Docker image..."
    docker build -f "$SCRIPT_DIR/Dockerfile.systemd" -t "$IMAGE_NAME" "$SCRIPT_DIR"
fi

# Check if container is already running
if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "Container already running."
else
    # Check if container exists but stopped
    if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        echo "Starting existing container..."
        docker start "$CONTAINER_NAME"
    else
        echo "Creating new systemd container..."
        # Create results directory
        mkdir -p "$RESULTS_DIR"

        # Start container in background with systemd
        # --cgroupns=host is required for systemd on Docker Desktop
        docker run -d \
            --privileged \
            --cgroupns=host \
            --name "$CONTAINER_NAME" \
            --hostname "mjolnir${SENSOR_NUM}" \
            -v "$REPO_ROOT:/home/pi/dev/mjolnir-hamma" \
            -v "$RESULTS_DIR:/test-results" \
            -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
            -e "HOME=/home/pi" \
            -e "MJOLNIR_TESTING=1" \
            "$IMAGE_NAME"

        # Wait for systemd to initialize
        echo "Waiting for systemd to start..."
        sleep 3
    fi
fi

# Create results directory
mkdir -p "$RESULTS_DIR"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
LOG_FILE="$RESULTS_DIR/systemd-test-${NETWORK_MODE}-${TIMESTAMP}.log"

echo ""
echo "=========================================="
echo "  Mjolnir Systemd Test Environment"
echo "=========================================="
echo ""
echo "Container: $CONTAINER_NAME"
echo "Repository mounted at: /home/pi/dev/mjolnir-hamma"
echo ""

# Function to run install
run_install() {
    echo "Running install.sh..."
    echo "Mode: $NETWORK_MODE"
    echo "Sensor: $SENSOR_NUM"
    echo "Log: $LOG_FILE"
    echo ""

    {
        echo "========================================"
        echo "Systemd Install Test Log"
        echo "Date: $(date)"
        echo "Mode: $NETWORK_MODE"
        echo "Sensor: $SENSOR_NUM"
        echo "========================================"
        echo ""
    } > "$LOG_FILE"

    # Run install script (use -i only if TTY available)
    # Note: --skip-hamma is used because HAMMA is a private repo requiring SSH keys
    if [ -t 0 ]; then
        docker exec -it "$CONTAINER_NAME" \
            bash -c "cd /home/pi/dev/mjolnir-hamma/unified_install && sudo bash install.sh $SENSOR_NUM --$NETWORK_MODE --skip-hamma" 2>&1 | tee -a "$LOG_FILE"
    else
        docker exec "$CONTAINER_NAME" \
            bash -c "cd /home/pi/dev/mjolnir-hamma/unified_install && sudo bash install.sh $SENSOR_NUM --$NETWORK_MODE --skip-hamma" 2>&1 | tee -a "$LOG_FILE"
    fi

    echo ""
    echo "Log saved to: $LOG_FILE"
}

# Function to run verification tests
run_verify() {
    echo "Running integration tests..."
    echo ""

    VERIFY_LOG="$RESULTS_DIR/verify-${TIMESTAMP}.log"

    # Install pytest if not available
    docker exec "$CONTAINER_NAME" bash -c "pip3 install pytest --quiet 2>/dev/null || true"

    # Run integration tests
    docker exec "$CONTAINER_NAME" \
        bash -c "cd /home/pi/dev/mjolnir-hamma && python3 -m pytest tests/integration/test_integration.py -v" 2>&1 | tee "$VERIFY_LOG"

    RESULT=$?
    echo ""
    echo "Verification log saved to: $VERIFY_LOG"

    return $RESULT
}

# Handle different actions
case "$ACTION" in
    install)
        run_install
        ;;
    verify)
        run_verify
        ;;
    full)
        echo "=========================================="
        echo "  FULL TEST: Install + Verify"
        echo "=========================================="
        echo ""
        run_install
        echo ""
        echo "=========================================="
        echo "  Verification Phase"
        echo "=========================================="
        echo ""
        run_verify
        ;;
    interactive)
        echo "To test install:"
        echo "  cd /home/pi/dev/mjolnir-hamma/unified_install"
        echo "  sudo bash install.sh $SENSOR_NUM --$NETWORK_MODE"
        echo ""
        echo "To run verification tests:"
        echo "  python3 -m pytest /home/pi/dev/mjolnir-hamma/tests/integration/test_integration.py -v"
        echo ""
        echo "To check systemd status:"
        echo "  systemctl status"
        echo ""
        echo "To stop container later:"
        echo "  $0 --stop"
        echo ""
        echo "=========================================="
        echo ""

        # Drop into interactive shell
        if [ -t 0 ]; then
            docker exec -it -u pi -w /home/pi/dev/mjolnir-hamma "$CONTAINER_NAME" bash
        else
            echo "Not running in TTY - cannot start interactive shell"
            echo "Run manually: docker exec -it -u pi -w /home/pi/dev/mjolnir-hamma $CONTAINER_NAME bash"
        fi
        ;;
esac
