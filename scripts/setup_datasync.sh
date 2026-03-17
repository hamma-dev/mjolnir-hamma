#!/bin/bash
# Setup the datasync user on remote HAMMA sensors.
#
# Creates the datasync user, configures SSH key access, and sets
# permissions so hamma_download.py can pull data via rsync.
#
# Usage:
#   ./setup_datasync.sh --key /path/to/id_rsa.pub 5 7 8
#   ./setup_datasync.sh --key ~/.ssh/id_rsa.pub --dry-run 5

# Source common library for logging and validation
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../unified_install/lib/common.sh"

# --- Constants ---
JUMP_HOST="pi@hamma.dev"
SENSOR_USER="pi"
SENSOR_HOST="localhost"
PORT_OFFSET=10000

# --- Globals ---
KEY_FILE=""
DRY_RUN=false
SENSORS=()
SUCCEEDED=()
FAILED=()

# --- Functions ---

usage() {
    cat <<EOF
Usage: $(basename "$0") --key PUBKEY_FILE [--dry-run] SENSOR_NUM [SENSOR_NUM ...]

Setup the datasync user on remote HAMMA sensors.

Arguments:
  --key PATH    Path to the public key file to install (required)
  --dry-run     Print commands without executing
  SENSOR_NUM    One or more sensor numbers (e.g., 5 7 8)

Examples:
  $(basename "$0") --key ~/.ssh/id_rsa.pub 5 7 8
  $(basename "$0") --key ~/Documents/rhome/.ssh/id_rsa.pub --dry-run 5
EOF
    exit 1
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --key)
                KEY_FILE="$2"
                shift 2
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --help|-h)
                usage
                ;;
            *)
                SENSORS+=("$1")
                shift
                ;;
        esac
    done

    # Validate --key
    if [[ -z "$KEY_FILE" ]]; then
        log_error "--key is required"
        usage
    fi
    if [[ ! -f "$KEY_FILE" ]]; then
        log_error "Key file not found: $KEY_FILE"
        exit 1
    fi

    # Validate key file looks like a public key
    local first_word
    first_word=$(awk '{print $1; exit}' "$KEY_FILE")
    case "$first_word" in
        ssh-rsa|ssh-ed25519|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521)
            ;;
        *)
            log_error "File does not look like a public key (starts with '$first_word'): $KEY_FILE"
            exit 1
            ;;
    esac

    # Validate sensor numbers
    if [[ ${#SENSORS[@]} -eq 0 ]]; then
        log_error "At least one sensor number is required"
        usage
    fi
    for num in "${SENSORS[@]}"; do
        validate_sensor_num "$num" || exit 1
    done
}

# Run a command on a remote sensor via SSH tunnel
# Usage: sensor_ssh SENSOR_NUM COMMAND
sensor_ssh() {
    local sensor_num="$1"
    local command="$2"
    local port=$((PORT_OFFSET + sensor_num))

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "ssh -J ${JUMP_HOST} -p ${port} ${SENSOR_USER}@${SENSOR_HOST} '${command}'"
        return 0
    fi

    ssh -J "${JUMP_HOST}" -p "${port}" "${SENSOR_USER}@${SENSOR_HOST}" "${command}"
}

# Setup datasync on a single sensor
# Returns 0 on success, 1 on failure
setup_sensor() {
    local sensor_num="$1"
    local formatted
    formatted=$(format_sensor_num "$sensor_num")
    local pubkey
    pubkey=$(cat "$KEY_FILE")

    log_step "Setting up datasync on mjolnir${formatted} (sensor ${sensor_num})"

    # Step 1: Check if datasync user already exists
    if sensor_ssh "$sensor_num" "id datasync" >/dev/null 2>&1; then
        log_info "User datasync already exists on sensor ${sensor_num}, skipping creation"
    else
        log_info "Creating datasync user"
        if ! sensor_ssh "$sensor_num" "sudo useradd -m -s /bin/bash datasync"; then
            log_error "Failed to create datasync user on sensor ${sensor_num}"
            return 1
        fi
    fi

    # Step 2: Add to pi group
    log_info "Adding datasync to pi group"
    if ! sensor_ssh "$sensor_num" "sudo usermod -a -G pi datasync"; then
        log_error "Failed to add datasync to pi group on sensor ${sensor_num}"
        return 1
    fi

    # Step 3: Create .ssh directory
    log_info "Creating .ssh directory"
    if ! sensor_ssh "$sensor_num" "sudo -H mkdir -p /home/datasync/.ssh && sudo -H chmod 700 /home/datasync/.ssh"; then
        log_error "Failed to create .ssh directory on sensor ${sensor_num}"
        return 1
    fi

    # Step 4: Write authorized_keys
    log_info "Installing public key"
    if ! sensor_ssh "$sensor_num" "echo '${pubkey}' | sudo tee /home/datasync/.ssh/authorized_keys > /dev/null && sudo chmod 600 /home/datasync/.ssh/authorized_keys"; then
        log_error "Failed to install public key on sensor ${sensor_num}"
        return 1
    fi

    # Step 5: Fix ownership
    log_info "Fixing .ssh ownership"
    if ! sensor_ssh "$sensor_num" "sudo -H chown -R datasync:datasync /home/datasync/.ssh"; then
        log_error "Failed to fix .ssh ownership on sensor ${sensor_num}"
        return 1
    fi

    # Step 6: Set media permissions
    log_info "Setting /media/pi/ permissions"
    if ! sensor_ssh "$sensor_num" "sudo chmod o+rx /media/pi/"; then
        log_error "Failed to set /media/pi/ permissions on sensor ${sensor_num}"
        return 1
    fi

    log_success "datasync setup complete on mjolnir${formatted}"
    return 0
}

# --- Main ---

parse_args "$@"

log_info "Setting up datasync on ${#SENSORS[@]} sensor(s)"
if [[ "$DRY_RUN" == "true" ]]; then
    log_warn "DRY RUN — no changes will be made"
fi

for sensor_num in "${SENSORS[@]}"; do
    if setup_sensor "$sensor_num"; then
        SUCCEEDED+=("$sensor_num")
    else
        FAILED+=("$sensor_num")
    fi
    echo ""
done

# --- Summary ---
echo "=============================="
if [[ ${#SUCCEEDED[@]} -gt 0 ]]; then
    log_success "Succeeded: ${SUCCEEDED[*]}"
fi
if [[ ${#FAILED[@]} -gt 0 ]]; then
    log_error "Failed: ${FAILED[*]}"
fi

# Print verification commands
if [[ ${#SUCCEEDED[@]} -gt 0 && "$DRY_RUN" != "true" ]]; then
    echo ""
    echo "Verification commands (run from matrix):"
    for sensor_num in "${SUCCEEDED[@]}"; do
        local_port=$((PORT_OFFSET + sensor_num))
        echo "  ssh -J monitor@hamma.dev -p ${local_port} datasync@localhost 'ls /media/pi/'"
    done
fi

# Exit with failure if any sensor failed
if [[ ${#FAILED[@]} -gt 0 ]]; then
    exit 1
fi
exit 0
