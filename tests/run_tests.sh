#!/bin/bash
# Master test runner for mjolnir-hamma
# Runs all test suites: shellcheck, pytest, and optionally Docker integration tests

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Options
RUN_SHELLCHECK=true
RUN_PYTEST=true
RUN_DOCKER=false
VERBOSE=false

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -s, --skip-shellcheck    Skip shellcheck tests"
    echo "  -p, --skip-pytest        Skip pytest tests"
    echo "  -d, --docker             Run Docker integration tests"
    echo "  -a, --all                Run all tests including Docker"
    echo "  -v, --verbose            Verbose output"
    echo "  -h, --help               Show this help"
    echo ""
    echo "Examples:"
    echo "  $0                       Run shellcheck and pytest"
    echo "  $0 -d                    Run shellcheck, pytest, and Docker tests"
    echo "  $0 --skip-pytest -d      Run shellcheck and Docker only"
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -s|--skip-shellcheck)
            RUN_SHELLCHECK=false
            shift
            ;;
        -p|--skip-pytest)
            RUN_PYTEST=false
            shift
            ;;
        -d|--docker)
            RUN_DOCKER=true
            shift
            ;;
        -a|--all)
            RUN_DOCKER=true
            shift
            ;;
        -v|--verbose)
            VERBOSE=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

# Track results
SHELLCHECK_RESULT=0
PYTEST_RESULT=0
DOCKER_RESULT=0

echo -e "${BLUE}======================================${NC}"
echo -e "${BLUE}  Mjolnir-HAMMA Test Suite${NC}"
echo -e "${BLUE}======================================${NC}"
echo ""

# --- Shellcheck Tests ---
if $RUN_SHELLCHECK; then
    echo -e "${YELLOW}Running Shellcheck validation...${NC}"
    echo ""

    # Check if shellcheck is installed
    if command -v shellcheck &> /dev/null; then
        # Find all shell scripts
        SCRIPTS=$(find "$REPO_ROOT/install_scripts" "$REPO_ROOT/files" -name "*.sh" 2>/dev/null || true)

        if [ -n "$SCRIPTS" ]; then
            SHELLCHECK_FAILED=0
            for script in $SCRIPTS; do
                if $VERBOSE; then
                    echo "Checking: $(basename "$script")"
                fi
                if ! shellcheck -e SC1091 -e SC2034 "$script" 2>/dev/null; then
                    echo -e "${RED}Failed: $(basename "$script")${NC}"
                    SHELLCHECK_FAILED=1
                fi
            done

            if [ $SHELLCHECK_FAILED -eq 0 ]; then
                echo -e "${GREEN}Shellcheck: All scripts passed${NC}"
            else
                echo -e "${RED}Shellcheck: Some scripts failed${NC}"
                SHELLCHECK_RESULT=1
            fi
        else
            echo "No shell scripts found"
        fi
    else
        echo -e "${YELLOW}Shellcheck not installed, skipping...${NC}"
        echo "Install with: brew install shellcheck (macOS) or apt install shellcheck (Linux)"
    fi
    echo ""
fi

# --- Pytest Tests ---
if $RUN_PYTEST; then
    echo -e "${YELLOW}Running Pytest tests...${NC}"
    echo ""

    # Check if pytest is available
    if command -v pytest &> /dev/null || python3 -m pytest --version &> /dev/null; then
        cd "$SCRIPT_DIR"

        PYTEST_ARGS="-v"
        if ! $VERBOSE; then
            PYTEST_ARGS="-q"
        fi

        if python3 -m pytest $PYTEST_ARGS --tb=short; then
            echo -e "${GREEN}Pytest: All tests passed${NC}"
        else
            echo -e "${RED}Pytest: Some tests failed${NC}"
            PYTEST_RESULT=1
        fi
    else
        echo -e "${YELLOW}Pytest not installed, skipping...${NC}"
        echo "Install with: pip install pytest"
        echo "Or: pip install -r $SCRIPT_DIR/requirements-test.txt"
    fi
    echo ""
fi

# --- Docker Integration Tests ---
if $RUN_DOCKER; then
    echo -e "${YELLOW}Running Docker integration tests...${NC}"
    echo ""

    # Check if Docker is available
    if command -v docker &> /dev/null; then
        cd "$SCRIPT_DIR/integration"

        # Create results directory
        mkdir -p results

        # Build and run
        echo "Building test container..."
        if docker build -t mjolnir-test . > /dev/null 2>&1; then
            echo "Running tests in container..."

            if docker run --rm \
                -v "$REPO_ROOT:/home/pi/dev/mjolnir-hamma:ro" \
                -v "$SCRIPT_DIR/integration/results:/test-results" \
                mjolnir-test \
                bash /home/pi/dev/mjolnir-hamma/tests/integration/test_in_container.sh; then
                echo -e "${GREEN}Docker tests: All tests passed${NC}"
            else
                echo -e "${RED}Docker tests: Some tests failed${NC}"
                DOCKER_RESULT=1
            fi

            # Show results if available
            if [ -f results/summary.txt ]; then
                echo ""
                echo "Container test summary:"
                cat results/summary.txt
            fi
        else
            echo -e "${RED}Failed to build Docker image${NC}"
            DOCKER_RESULT=1
        fi
    else
        echo -e "${YELLOW}Docker not installed, skipping...${NC}"
    fi
    echo ""
fi

# --- Summary ---
echo -e "${BLUE}======================================${NC}"
echo -e "${BLUE}  Test Summary${NC}"
echo -e "${BLUE}======================================${NC}"

TOTAL_RESULT=0

if $RUN_SHELLCHECK; then
    if [ $SHELLCHECK_RESULT -eq 0 ]; then
        echo -e "Shellcheck:  ${GREEN}PASSED${NC}"
    else
        echo -e "Shellcheck:  ${RED}FAILED${NC}"
        TOTAL_RESULT=1
    fi
fi

if $RUN_PYTEST; then
    if [ $PYTEST_RESULT -eq 0 ]; then
        echo -e "Pytest:      ${GREEN}PASSED${NC}"
    else
        echo -e "Pytest:      ${RED}FAILED${NC}"
        TOTAL_RESULT=1
    fi
fi

if $RUN_DOCKER; then
    if [ $DOCKER_RESULT -eq 0 ]; then
        echo -e "Docker:      ${GREEN}PASSED${NC}"
    else
        echo -e "Docker:      ${RED}FAILED${NC}"
        TOTAL_RESULT=1
    fi
fi

echo ""

if [ $TOTAL_RESULT -eq 0 ]; then
    echo -e "${GREEN}All tests passed!${NC}"
else
    echo -e "${RED}Some tests failed.${NC}"
fi

exit $TOTAL_RESULT
