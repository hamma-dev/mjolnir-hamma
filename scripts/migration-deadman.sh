#!/bin/bash
# Migration Dead Man's Switch
#
# Usage:
#   sudo bash migration-deadman.sh arm [minutes]   # Arm the switch (default: 30 min)
#   sudo bash migration-deadman.sh defuse           # Defuse after successful migration
#   sudo bash migration-deadman.sh status            # Check timer status
#
# How it works:
#   ARM: Backs up all files that install.sh will modify, records the current
#        git branch, then creates a systemd timer that fires after N minutes.
#        If not defused, the timer triggers a restore of all backed-up files
#        and reboots the Pi.
#
#   DEFUSE: Stops and removes the timer, cleans up backup files.
#
#   The systemd timer survives reboots. If the Pi reboots for any reason
#   (including the kill switch cron), the timer re-arms on boot and fires
#   N minutes after the reboot. This ensures rollback happens even if the
#   Pi is caught in a reboot loop.
#
# What gets backed up (everything install.sh --cellular modifies):
#   /etc/systemd/network/40-eth0.network
#   /etc/systemd/network/20-wwan0.network
#   /etc/systemd/network/30-eth1.network
#   /usr/local/bin/50_bring_wwan0_up.py
#   /usr/local/bin/wwan-check.sh
#   /etc/systemd/system/wwan-check.timer
#   /etc/systemd/system/wwan-check.service
#   /etc/networkd-dispatcher/carrier.d/  (symlinks, may not exist)
#   /etc/networkd-dispatcher/degraded.d/ (symlinks, may not exist)
#   /etc/systemd/system/networkd-dispatcher.service.d/ (may not exist)
#
# Limitation: OnActiveSec resets on each reboot. If the Pi is in a rapid
# reboot loop (uptime < timer duration), the rollback never fires. This is
# acceptable because the only automated reboot is the kill switch cron
# (every 4 hours), and the default 30-minute timer fires well within that.

# Require root
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (sudo)"
    exit 1
fi

BACKUP_DIR="/var/lib/migration-backup"
BACKUP_TAR="$BACKUP_DIR/pre-migration.tar"
BACKUP_BRANCH="$BACKUP_DIR/git-branch"
ROLLBACK_SCRIPT="$BACKUP_DIR/rollback.sh"
TIMER_UNIT="deadman-rollback.timer"
SERVICE_UNIT="deadman-rollback.service"

# Files that install.sh overwrites
BACKUP_FILES=(
    /etc/systemd/network/40-eth0.network
    /etc/systemd/network/20-wwan0.network
    /etc/systemd/network/30-eth1.network
    /usr/local/bin/50_bring_wwan0_up.py
    /usr/local/bin/wwan-check.sh
    /etc/systemd/system/wwan-check.timer
    /etc/systemd/system/wwan-check.service
)

# Paths that install.sh removes (may or may not exist)
BACKUP_REMOVABLE=(
    /etc/networkd-dispatcher/carrier.d/50_bring_wwan0_up.py
    /etc/networkd-dispatcher/degraded.d/50_bring_wwan0_up.py
    /etc/systemd/system/networkd-dispatcher.service.d
)

cleanup_partial_arm() {
    echo "INTERRUPTED -- cleaning up partial arm state..."
    systemctl stop "$TIMER_UNIT" 2>/dev/null || true
    systemctl disable "$TIMER_UNIT" 2>/dev/null || true
    rm -f "/etc/systemd/system/$TIMER_UNIT" "/etc/systemd/system/$SERVICE_UNIT"
    systemctl daemon-reload 2>/dev/null || true
    rm -rf "$BACKUP_DIR"
    echo "Cleaned up. Dead man's switch was NOT armed."
    exit 130
}

arm() {
    local minutes="${1:-30}"

    # Validate minutes is a positive integer
    if ! [[ "$minutes" =~ ^[0-9]+$ ]] || [[ "$minutes" -eq 0 ]]; then
        echo "ERROR: minutes must be a positive integer, got: '$minutes'"
        exit 1
    fi

    if systemctl is-active "$TIMER_UNIT" &>/dev/null; then
        echo "ERROR: Dead man's switch is already armed!"
        echo "Run 'sudo bash migration-deadman.sh defuse' first, or check status."
        exit 1
    fi

    # Clean up on interrupt (Ctrl-C, SSH drop, SIGTERM)
    trap cleanup_partial_arm INT TERM HUP

    echo "=== Arming dead man's switch ($minutes minutes) ==="
    echo

    # Warn if backup directory already exists from a previous run
    if [[ -d "$BACKUP_DIR" ]]; then
        echo "WARNING: Backup directory already exists from a previous run."
        echo "Previous backup will be overwritten."
        echo
    fi

    # Create backup directory
    mkdir -p "$BACKUP_DIR" || { echo "ERROR: Cannot create $BACKUP_DIR"; exit 1; }

    # --- Save current git branch ---
    local current_branch
    current_branch=$(sudo -H -u pi git -C /home/pi/dev/mjolnir-hamma rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    echo "$current_branch" > "$BACKUP_BRANCH"
    echo "Saved git branch: $current_branch"

    # --- Backup files that will be overwritten ---
    echo "Backing up files..."
    local files_to_tar=()
    for f in "${BACKUP_FILES[@]}"; do
        if [[ -e "$f" ]]; then
            files_to_tar+=("$f")
            echo "  + $f"
        else
            echo "  - $f (not found, skipping)"
        fi
    done

    # Also backup removable paths (symlinks, directories)
    for f in "${BACKUP_REMOVABLE[@]}"; do
        if [[ -e "$f" || -L "$f" ]]; then
            files_to_tar+=("$f")
            echo "  + $f (removable)"
        else
            echo "  - $f (not found, skipping)"
        fi
    done

    if [[ ${#files_to_tar[@]} -eq 0 ]]; then
        echo "ERROR: No files found to backup. Something is wrong."
        exit 1
    fi

    # Create tar (--absolute-names preserves leading /; symlinks stored as-is)
    if ! tar cf "$BACKUP_TAR" --absolute-names "${files_to_tar[@]}"; then
        echo "ERROR: Failed to create backup tar. Aborting."
        rm -rf "$BACKUP_DIR"
        exit 1
    fi
    echo "Backup saved to $BACKUP_TAR ($(du -h "$BACKUP_TAR" | cut -f1))"

    # --- Create the rollback script ---
    cat > "$ROLLBACK_SCRIPT" <<'ROLLBACK_EOF'
#!/bin/bash
# Auto-generated rollback script -- DO NOT EDIT
# This runs if the dead man's switch fires.
# No set -e: every command must continue even if prior ones fail.

BACKUP_DIR="/var/lib/migration-backup"
LOG_TAG="deadman-rollback"

logger -t "$LOG_TAG" "DEAD MAN'S SWITCH FIRED -- ROLLING BACK"

# Restore backed-up files
if [[ -f "$BACKUP_DIR/pre-migration.tar" ]]; then
    logger -t "$LOG_TAG" "Restoring files from backup..."
    tar xf "$BACKUP_DIR/pre-migration.tar" --absolute-names
    tar_rc=$?
    if [[ $tar_rc -eq 0 ]]; then
        logger -t "$LOG_TAG" "Files restored successfully"
    else
        logger -t "$LOG_TAG" "ERROR: tar restore failed (exit $tar_rc), continuing anyway"
    fi
else
    logger -t "$LOG_TAG" "ERROR: No backup tar found at $BACKUP_DIR/pre-migration.tar"
fi

# Once files are restored, ignore SIGTERM so defuse cannot interrupt us
# mid-rollback -- we must finish and reboot to reach a consistent state
trap '' TERM

# Restore git branch (force checkout -- working tree may be dirty)
if [[ -f "$BACKUP_DIR/git-branch" ]]; then
    saved_branch=$(cat "$BACKUP_DIR/git-branch")
    logger -t "$LOG_TAG" "Restoring git branch: $saved_branch"
    runuser -u pi -- git -C /home/pi/dev/mjolnir-hamma checkout -f "$saved_branch" 2>/dev/null || true
fi

# Reload systemd (timer/service files may have been restored)
systemctl daemon-reload || true

# Restart networkd to apply restored network configs
# Safe for wwan0: it is Unmanaged=yes, only eth0/eth1 are affected
systemctl restart systemd-networkd || true

# Restart wwan timer (restored version)
systemctl restart wwan-check.timer 2>/dev/null || true

# Remove the dead man's switch units (they didn't exist before arming,
# so they are not in the backup tar and must be explicitly cleaned up)
systemctl stop deadman-rollback.timer 2>/dev/null || true
systemctl disable deadman-rollback.timer 2>/dev/null || true
rm -f /etc/systemd/system/deadman-rollback.timer
rm -f /etc/systemd/system/deadman-rollback.service
systemctl daemon-reload || true

logger -t "$LOG_TAG" "Rollback complete. Rebooting in 10 seconds..."
sleep 10
reboot
ROLLBACK_EOF
    chmod +x "$ROLLBACK_SCRIPT"

    # --- Create systemd service (runs the rollback) ---
    cat > "/etc/systemd/system/$SERVICE_UNIT" <<EOF
[Unit]
Description=Migration Dead Man's Switch - Rollback Service

[Service]
Type=oneshot
ExecStart=$ROLLBACK_SCRIPT
EOF

    # --- Create systemd timer ---
    # OnActiveSec fires N seconds after the timer is started.
    # If the system reboots and the timer is enabled, it re-starts on boot
    # and fires N seconds after boot — giving us another window to defuse.
    local seconds=$((minutes * 60))
    cat > "/etc/systemd/system/$TIMER_UNIT" <<EOF
[Unit]
Description=Migration Dead Man's Switch - ${minutes}min Timer

[Timer]
OnActiveSec=${seconds}

[Install]
WantedBy=timers.target
EOF

    # Enable (survives reboot) and start
    systemctl daemon-reload || { echo "ERROR: daemon-reload failed"; exit 1; }
    systemctl enable "$TIMER_UNIT" || { echo "ERROR: failed to enable timer"; exit 1; }
    systemctl start "$TIMER_UNIT" || { echo "ERROR: failed to start timer"; exit 1; }

    # Belt-and-suspenders: verify timer is actually running
    if ! systemctl is-active "$TIMER_UNIT" &>/dev/null; then
        echo "ERROR: Timer failed to start despite no errors. Aborting."
        cleanup_partial_arm
    fi

    # Disarm the interrupt trap -- we are now in a consistent armed state
    trap - INT TERM HUP

    echo
    echo "========================================="
    echo " DEAD MAN'S SWITCH ARMED"
    echo " Fires in: $minutes minutes"
    echo " To defuse: sudo bash migration-deadman.sh defuse"
    echo " To check:  sudo bash migration-deadman.sh status"
    echo "========================================="
    echo
    echo "If not defused, ALL changes will be rolled back and the Pi will reboot."
}

defuse() {
    echo "=== Defusing dead man's switch ==="

    # Check if the rollback service is already running (timer just fired)
    if systemctl is-active "$SERVICE_UNIT" &>/dev/null; then
        echo "WARNING: Rollback service is ALREADY RUNNING!"
        echo "The rollback may have already restored files. Attempting to stop it..."
        # The rollback script traps SIGTERM after file restore, so this may
        # not stop it if it has passed the point of no return.
        systemctl kill "$SERVICE_UNIT" 2>/dev/null || true
        echo "Check system state carefully -- a reboot may be imminent."
    fi

    if ! systemctl is-active "$TIMER_UNIT" &>/dev/null; then
        echo "Timer is not active. Cleaning up any partial state..."
    fi

    # Stop timer and service
    systemctl stop "$SERVICE_UNIT" 2>/dev/null || true
    systemctl stop "$TIMER_UNIT" 2>/dev/null || true
    systemctl disable "$TIMER_UNIT" 2>/dev/null || true

    # Remove systemd units
    rm -f "/etc/systemd/system/$TIMER_UNIT"
    rm -f "/etc/systemd/system/$SERVICE_UNIT"
    systemctl daemon-reload

    # Clean up backup (optional — keep for safety)
    echo
    echo "Timer stopped and removed."
    echo "Backup files preserved at $BACKUP_DIR (manual cleanup: sudo rm -rf $BACKUP_DIR)"
    echo
    echo "========================================="
    echo " DEAD MAN'S SWITCH DEFUSED"
    echo "========================================="
}

status() {
    echo "=== Dead Man's Switch Status ==="
    echo

    if systemctl is-active "$TIMER_UNIT" &>/dev/null; then
        echo "Status: ARMED"
        echo
        systemctl status "$TIMER_UNIT" --no-pager 2>/dev/null
        echo
        # Show time remaining
        local next_fire
        next_fire=$(systemctl show "$TIMER_UNIT" --property=NextElapseUSecMonotonic --value 2>/dev/null)
        if [[ -n "$next_fire" && "$next_fire" != "0" ]]; then
            echo "Timer details:"
            systemctl list-timers "$TIMER_UNIT" --no-pager 2>/dev/null
        fi
    else
        echo "Status: NOT ARMED"
    fi

    echo
    if [[ -d "$BACKUP_DIR" ]]; then
        echo "Backup exists at $BACKUP_DIR"
        if [[ -f "$BACKUP_BRANCH" ]]; then
            echo "  Saved branch: $(cat "$BACKUP_BRANCH")"
        fi
        if [[ -f "$BACKUP_TAR" ]]; then
            echo "  Backup size: $(du -h "$BACKUP_TAR" | cut -f1)"
        fi
    else
        echo "No backup directory found."
    fi
}

# --- Main ---
case "${1:-}" in
    arm)
        arm "${2:-30}"
        ;;
    defuse)
        defuse
        ;;
    status)
        status
        ;;
    *)
        echo "Usage: sudo bash migration-deadman.sh {arm [minutes]|defuse|status}"
        echo
        echo "  arm [N]   Backup current state and arm timer (default: 30 min)"
        echo "  defuse    Stop timer and clean up"
        echo "  status    Show current timer status"
        exit 1
        ;;
esac
