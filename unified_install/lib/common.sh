#!/bin/bash
# Common functions for HAMMA Pi unified install scripts
#
# This file provides:
#   - Logging functions with color output
#   - Manifest generation for --dry-run mode
#   - Validation helpers
#   - System operation wrappers that respect DRY_RUN
#
# Usage:
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
#   init_common [--dry-run]

# --- Configuration ---
MANIFEST_FILE="${MANIFEST_FILE:-/tmp/install_manifest.json}"
DRY_RUN="${DRY_RUN:-false}"
MANIFEST_STARTED=false

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# --- Logging Functions ---
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_step() {
    echo -e "${CYAN}==>${NC} $1"
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

log_dry_run() {
    echo -e "${YELLOW}[DRY-RUN]${NC} Would: $1"
}

# --- Manifest Functions ---

# Initialize the manifest file (call once at start)
manifest_init() {
    if [[ "$DRY_RUN" == "true" ]]; then
        echo '{"operations": [' > "$MANIFEST_FILE"
        MANIFEST_STARTED=true
        log_info "Dry-run mode: manifest will be written to $MANIFEST_FILE"
    fi
}

# Add an operation to the manifest
# Usage: manifest_add "type" "key1" "value1" "key2" "value2" ...
manifest_add() {
    if [[ "$DRY_RUN" != "true" ]]; then
        return 0
    fi

    local op_type="$1"
    shift

    # Build JSON object
    local json='{'
    json+='"type": "'"$op_type"'"'

    while [[ $# -ge 2 ]]; do
        local key="$1"
        local value="$2"
        # Escape special characters in value
        value="${value//\\/\\\\}"
        value="${value//\"/\\\"}"
        json+=', "'"$key"'": "'"$value"'"'
        shift 2
    done

    json+='}'

    # Append to manifest with proper comma handling
    if [[ -f "$MANIFEST_FILE" ]]; then
        local content
        content=$(cat "$MANIFEST_FILE")
        # Check if we need a comma (not first entry)
        if [[ "$content" == *'[' && "$content" != *'{' ]]; then
            # First entry, no comma needed
            echo "$content" > "$MANIFEST_FILE"
            echo "  $json" >> "$MANIFEST_FILE"
        else
            # Not first entry, add comma to previous line
            # Remove trailing newlines, add comma, then add new entry
            echo "${content}" | sed '$ s/$/,/' > "$MANIFEST_FILE"
            echo "  $json" >> "$MANIFEST_FILE"
        fi
    fi
}

# Finalize the manifest (call at end)
manifest_finalize() {
    if [[ "$DRY_RUN" == "true" && -f "$MANIFEST_FILE" ]]; then
        echo ']}' >> "$MANIFEST_FILE"
        log_success "Manifest written to: $MANIFEST_FILE"
    fi
}

# --- System Operation Wrappers ---
# These respect DRY_RUN mode

# Copy a file
# Usage: safe_cp source dest [mode]
safe_cp() {
    local src="$1"
    local dst="$2"
    local mode="${3:-}"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "cp $src $dst"
        manifest_add "copy" "src" "$src" "dst" "$dst"
        if [[ -n "$mode" ]]; then
            manifest_add "chmod" "path" "$dst" "mode" "$mode"
        fi
        return 0
    fi

    cp "$src" "$dst"
    if [[ -n "$mode" ]]; then
        chmod "$mode" "$dst"
    fi
}

# Copy with sudo
safe_sudo_cp() {
    local src="$1"
    local dst="$2"
    local mode="${3:-}"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "sudo cp $src $dst"
        manifest_add "copy" "src" "$src" "dst" "$dst" "sudo" "true"
        if [[ -n "$mode" ]]; then
            manifest_add "chmod" "path" "$dst" "mode" "$mode" "sudo" "true"
        fi
        return 0
    fi

    sudo cp "$src" "$dst"
    if [[ -n "$mode" ]]; then
        sudo chmod "$mode" "$dst"
    fi
}

# Run a command
# Usage: safe_run command [args...]
safe_run() {
    local cmd="$*"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "$cmd"
        manifest_add "command" "cmd" "$cmd"
        return 0
    fi

    "$@"
}

# Run a command with sudo
safe_sudo() {
    local cmd="$*"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "sudo $cmd"
        manifest_add "command" "cmd" "$cmd" "sudo" "true"
        return 0
    fi

    sudo "$@"
}

# Enable a systemd service
safe_systemctl_enable() {
    local service="$1"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "systemctl enable $service"
        manifest_add "systemctl" "action" "enable" "service" "$service"
        return 0
    fi

    sudo systemctl enable "$service"
}

# Disable a systemd service
safe_systemctl_disable() {
    local service="$1"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "systemctl disable $service"
        manifest_add "systemctl" "action" "disable" "service" "$service"
        return 0
    fi

    sudo systemctl disable "$service" 2>/dev/null || true
}

# Start a systemd service
safe_systemctl_start() {
    local service="$1"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "systemctl start $service"
        manifest_add "systemctl" "action" "start" "service" "$service"
        return 0
    fi

    sudo systemctl start "$service"
}

# Restart a systemd service
safe_systemctl_restart() {
    local service="$1"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "systemctl restart $service"
        manifest_add "systemctl" "action" "restart" "service" "$service"
        return 0
    fi

    sudo systemctl restart "$service" || true
}

# Run ssh-keygen (CRITICAL for WiFi path)
# Usage: safe_ssh_keygen [keytype] [keyfile]
safe_ssh_keygen() {
    local keytype="${1:-rsa}"
    local keyfile="${2:-}"

    if [[ "$DRY_RUN" == "true" ]]; then
        if [[ -n "$keyfile" ]]; then
            log_dry_run "ssh-keygen -t $keytype -f $keyfile"
            manifest_add "ssh-keygen" "keytype" "$keytype" "keyfile" "$keyfile"
        else
            log_dry_run "ssh-keygen -t $keytype"
            manifest_add "ssh-keygen" "keytype" "$keytype"
        fi
        return 0
    fi

    if [[ -n "$keyfile" ]]; then
        ssh-keygen -t "$keytype" -f "$keyfile" -N ""
    else
        ssh-keygen -t "$keytype"
    fi
}

# Create a directory
safe_mkdir() {
    local dir="$1"
    local mode="${2:-}"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "mkdir -p $dir"
        manifest_add "mkdir" "path" "$dir"
        return 0
    fi

    mkdir -p "$dir"
    if [[ -n "$mode" ]]; then
        chmod "$mode" "$dir"
    fi
}

# Write content to a file
# Usage: safe_write_file filepath content
safe_write_file() {
    local filepath="$1"
    local content="$2"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "Write to $filepath"
        manifest_add "write" "path" "$filepath" "content_length" "${#content}"
        return 0
    fi

    echo "$content" > "$filepath"
}

# Append content to a file
safe_append_file() {
    local filepath="$1"
    local content="$2"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "Append to $filepath"
        manifest_add "append" "path" "$filepath" "content_length" "${#content}"
        return 0
    fi

    echo "$content" >> "$filepath"
}

# Create a symlink
safe_ln() {
    local target="$1"
    local link="$2"

    if [[ "$DRY_RUN" == "true" ]]; then
        log_dry_run "ln -sf $target $link"
        manifest_add "symlink" "target" "$target" "link" "$link"
        return 0
    fi

    ln -sf "$target" "$link"
}

# Git clone
safe_git_clone() {
    local repo="$1"
    local dest="$2"
    local recursive="${3:-false}"

    if [[ "$DRY_RUN" == "true" ]]; then
        if [[ "$recursive" == "true" ]]; then
            log_dry_run "git clone --recursive $repo $dest"
            manifest_add "git_clone" "repo" "$repo" "dest" "$dest" "recursive" "true"
        else
            log_dry_run "git clone $repo $dest"
            manifest_add "git_clone" "repo" "$repo" "dest" "$dest"
        fi
        return 0
    fi

    if [[ "$recursive" == "true" ]]; then
        git clone --recursive "$repo" "$dest"
    else
        git clone "$repo" "$dest"
    fi
}

# Pip install
safe_pip_install() {
    local package="$1"
    local editable="${2:-false}"

    if [[ "$DRY_RUN" == "true" ]]; then
        if [[ "$editable" == "true" ]]; then
            log_dry_run "pip install -e $package"
            manifest_add "pip_install" "package" "$package" "editable" "true"
        else
            log_dry_run "pip install $package"
            manifest_add "pip_install" "package" "$package"
        fi
        return 0
    fi

    if [[ "$editable" == "true" ]]; then
        pip install -e "$package"
    else
        pip install "$package"
    fi
}

# --- Validation Helpers ---

# Check if running as root
require_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "This script must be run as root (use sudo)"
        exit 1
    fi
}

# Check if NOT running as root (for user scripts)
require_user() {
    if [[ $EUID -eq 0 ]]; then
        log_error "This script should NOT be run as root"
        exit 1
    fi
}

# Validate sensor number
validate_sensor_num() {
    local num="$1"

    if [[ -z "$num" ]]; then
        log_error "Sensor number is required"
        return 1
    fi

    if ! [[ "$num" =~ ^[0-9]+$ ]]; then
        log_error "Sensor number must be a positive integer"
        return 1
    fi

    return 0
}

# Format sensor number with zero-padding
format_sensor_num() {
    local num="$1"
    printf "%.2d" "$num"
}

# --- Initialization ---

# Initialize common library
# Usage: init_common [--dry-run]
init_common() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            *)
                shift
                ;;
        esac
    done

    if [[ "$DRY_RUN" == "true" ]]; then
        manifest_init
    fi
}

# Export functions for use in sourced scripts
export -f log_info log_step log_success log_warn log_error log_dry_run
export -f manifest_init manifest_add manifest_finalize
export -f safe_cp safe_sudo_cp safe_run safe_sudo
export -f safe_systemctl_enable safe_systemctl_disable safe_systemctl_start safe_systemctl_restart
export -f safe_ssh_keygen safe_mkdir safe_write_file safe_append_file safe_ln
export -f safe_git_clone safe_pip_install
export -f require_root require_user validate_sensor_num format_sensor_num init_common
export DRY_RUN MANIFEST_FILE
