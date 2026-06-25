#!/bin/bash
# Docker-based test runner for unified install scripts
#
# This script runs all tests for the unified install scripts
# either locally or in a Docker container simulating a Pi.
#
# Usage:
#   ./run_unified_tests.sh [options]
#
# Options:
#   --docker      Run tests in Docker container
#   --local       Run tests locally (default)
#   --verbose     Show verbose output
#   --quick       Run only quick tests
#   --help        Show this help

set -e

# --- Configuration ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DOCKER_IMAGE="mjolnir-pi-test"
TESTS_DIR="$SCRIPT_DIR/unified"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# --- Parse arguments ---
USE_DOCKER=false
VERBOSE=""
QUICK=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --docker)
            USE_DOCKER=true
            shift
            ;;
        --local)
            USE_DOCKER=false
            shift
            ;;
        --verbose|-v)
            VERBOSE="-v"
            shift
            ;;
        --quick|-q)
            QUICK=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [--docker|--local] [--verbose] [--quick]"
            echo ""
            echo "Options:"
            echo "  --docker    Run tests in Docker container"
            echo "  --local     Run tests locally (default)"
            echo "  --verbose   Show verbose test output"
            echo "  --quick     Run only quick tests"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# --- Functions ---
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[PASS]${NC} $1"
}

log_error() {
    echo -e "${RED}[FAIL]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# --- Check prerequisites ---
check_prerequisites() {
    log_info "Checking prerequisites..."

    # Check Python
    if ! command -v python3 &> /dev/null; then
        log_error "Python 3 is required but not found"
        exit 1
    fi

    # Check pytest
    if ! python3 -c "import pytest" 2>/dev/null; then
        log_warn "pytest not found, installing..."
        pip3 install pytest
    fi

    # Check unified_install directory exists
    if [[ ! -d "$REPO_ROOT/unified_install" ]]; then
        log_error "unified_install directory not found"
        exit 1
    fi

    # Check test files exist
    if [[ ! -d "$TESTS_DIR" ]]; then
        log_error "tests/unified directory not found"
        exit 1
    fi

    log_success "Prerequisites OK"
}

# --- Build Docker image ---
build_docker() {
    log_info "Building Docker image..."

    docker build \
        -f "$SCRIPT_DIR/docker/Dockerfile.pi-test" \
        -t "$DOCKER_IMAGE" \
        "$REPO_ROOT"

    log_success "Docker image built: $DOCKER_IMAGE"
}

# --- Run tests in Docker ---
run_docker_tests() {
    log_info "Running tests in Docker container..."

    # Build if needed
    if ! docker image inspect "$DOCKER_IMAGE" &>/dev/null; then
        build_docker
    fi

    # Run tests
    docker run --rm \
        -v "$REPO_ROOT:/home/pi/dev/mjolnir-hamma" \
        -w "/home/pi/dev/mjolnir-hamma" \
        "$DOCKER_IMAGE" \
        python3 -m pytest tests/unified/ $VERBOSE

    return $?
}

# --- Run tests locally ---
run_local_tests() {
    log_info "Running tests locally..."

    cd "$REPO_ROOT"

    # Set up Python path
    export PYTHONPATH="$REPO_ROOT/tests/fixtures:$PYTHONPATH"

    # Determine which tests to run
    local test_args=""
    if [[ "$QUICK" == "true" ]]; then
        test_args="-m 'not slow'"
    fi

    # Run pytest
    python3 -m pytest "$TESTS_DIR" $VERBOSE $test_args

    return $?
}

# --- Run dry-run test ---
run_dry_run_test() {
    log_info "Running dry-run manifest test..."

    local bootstrap="$REPO_ROOT/unified_install/bootstrap.sh"
    local install="$REPO_ROOT/unified_install/install.sh"
    local manifest="/tmp/test_manifest.json"

    # Test bootstrap.sh --dry-run
    if [[ -f "$bootstrap" ]]; then
        log_info "Testing bootstrap.sh --dry-run..."
        MANIFEST_FILE="$manifest" DRY_RUN=true bash "$bootstrap" 1 --dry-run 2>&1 || {
            log_warn "bootstrap.sh dry-run returned non-zero (may be expected)"
        }

        if [[ -f "$manifest" ]]; then
            log_success "bootstrap.sh produced manifest"
        fi
    fi

    # Clean up
    rm -f "$manifest"
}

# --- Main ---
main() {
    echo ""
    echo "========================================"
    echo "  HAMMA Pi Unified Install Test Runner"
    echo "========================================"
    echo ""

    check_prerequisites

    # Run the appropriate test mode
    local exit_code=0

    if [[ "$USE_DOCKER" == "true" ]]; then
        run_docker_tests || exit_code=$?
    else
        run_local_tests || exit_code=$?
    fi

    # Summary
    echo ""
    echo "========================================"
    if [[ $exit_code -eq 0 ]]; then
        log_success "All tests passed!"
    else
        log_error "Some tests failed (exit code: $exit_code)"
    fi
    echo "========================================"

    exit $exit_code
}

main "$@"
