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

# Prevent concurrent execution (two arms, arm+defuse, etc.)
LOCK_FILE="/var/run/migration-deadman.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "ERROR: Another instance of migration-deadman.sh is already running."
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

CLEANUP_IN_PROGRESS=0
cleanup_partial_arm() {
    # Re-entry guard: if cleanup triggers another signal, don't recurse
    if [[ "$CLEANUP_IN_PROGRESS" -eq 1 ]]; then return; fi
    CLEANUP_IN_PROGRESS=1
    echo "INTERRUPTED -- cleaning up partial arm state..."
    logger -t "deadman-arm" "Arm interrupted, cleaning up partial state"
    systemctl stop "$TIMER_UNIT" 2>/dev/null || true
    systemctl disable "$TIMER_UNIT" 2>/dev/null || true
    rm -f "/etc/systemd/system/$TIMER_UNIT" "/etc/systemd/system/$SERVICE_UNIT"
    systemctl daemon-reload 2>/dev/null || true
    rm -rf "$BACKUP_DIR"
    echo "Cleaned up. Dead man's switch was NOT armed."
    exit "${1:-130}"
}

arm() {
    local minutes="${1:-30}"

    # Validate minutes is a positive integer, capped at 1440 (24 hours)
    # Regex rejects leading zeros (avoids bash octal interpretation) and zero
    if ! [[ "$minutes" =~ ^[1-9][0-9]{0,3}$ ]]; then
        echo "ERROR: minutes must be a positive integer (1-1440), got: '$minutes'"
        exit 1
    fi
    if [[ "$minutes" -gt 1440 ]]; then
        echo "ERROR: minutes must be <= 1440 (24 hours), got: '$minutes'"
        exit 1
    fi

    if systemctl is-active "$TIMER_UNIT" &>/dev/null; then
        echo "ERROR: Dead man's switch is already armed!"
        echo "Run 'sudo bash migration-deadman.sh defuse' first, or check status."
        exit 1
    fi

    # Check if rollback service is currently running (flock does NOT protect against
    # this — the rollback is a separate process launched by systemd, not this script).
    # Without this check, arm() could overwrite BACKUP_DIR while rollback reads from it.
    local svc_state
    svc_state=$(systemctl show "$SERVICE_UNIT" --property=ActiveState 2>/dev/null || echo "unknown")
    svc_state="${svc_state#ActiveState=}"
    if [[ "$svc_state" == "activating" || "$svc_state" == "deactivating" ]]; then
        echo "ERROR: Rollback service is currently running (state: $svc_state)!"
        echo "Wait for the rollback to complete and the system to reboot."
        exit 1
    fi

    # Secondary guard: unit files on disk mean we armed previously, even if the
    # timer isn't active yet (e.g., early boot before timers.target is reached).
    if [[ -f "/etc/systemd/system/$TIMER_UNIT" ]] && [[ -f "/etc/systemd/system/$SERVICE_UNIT" ]]; then
        echo "ERROR: Dead man's switch unit files exist but timer is not active."
        echo "The timer may still be starting. Run 'defuse' to clean up first."
        exit 1
    fi

    # Clean up on interrupt (Ctrl-C, SSH drop, SIGTERM)
    trap 'cleanup_partial_arm 130' INT
    trap 'cleanup_partial_arm 143' TERM
    trap 'cleanup_partial_arm 129' HUP

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

    # Copy this script to backup dir so it survives if original location changes
    cp "${BASH_SOURCE[0]:-$0}" "$BACKUP_DIR/migration-deadman.sh" 2>/dev/null || true

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
        rm -rf "$BACKUP_DIR"
        exit 1
    fi

    # Create tar (--absolute-names preserves leading /; symlinks stored as-is)
    if ! tar cf "$BACKUP_TAR" --absolute-names "${files_to_tar[@]}"; then
        echo "ERROR: Failed to create backup tar. Aborting."
        rm -rf "$BACKUP_DIR"
        exit 1
    fi
    sync
    # Verify tar is valid and non-empty
    if [[ ! -s "$BACKUP_TAR" ]] || ! tar tf "$BACKUP_TAR" --absolute-names >/dev/null 2>&1; then
        echo "ERROR: Backup tar is empty or corrupt. Aborting."
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

# Ignore signals so defuse (systemctl stop) cannot kill us.
# Once rollback begins, it MUST complete and reboot to reach a consistent state.
# PIPE: prevents dead syslog socket from killing us via logger.
trap '' TERM INT HUP PIPE

BACKUP_DIR="/var/lib/migration-backup"
LOG_TAG="deadman-rollback"
ROLLBACK_LOG="/var/log/deadman-rollback.log"

# Log to both syslog and a persistent file (survives backup dir deletion)
# Timeout on logger: if journald is stuck (disk full), logger blocks forever
# and the rollback never reaches reboot.
log() {
    timeout 5 logger -t "$LOG_TAG" "$1" 2>/dev/null || true
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$ROLLBACK_LOG" 2>/dev/null || true
}

log "DEAD MAN'S SWITCH FIRED -- ROLLING BACK"

# Check if filesystem is writable. If read-only (most common SD card failure),
# skip restore (can't write files anyway) and skip backup dir deletion
# (can't delete). Just reboot — on next boot ConditionPathExists passes again,
# but we track attempt count to break infinite loops.
ATTEMPT_FILE="$BACKUP_DIR/.rollback-attempts"
RO_FS=0
# Test writability of both /var/lib (backup dir) and /etc (restore targets).
# Selective sector wear can make one writable while the other is read-only.
if ! touch /var/lib/.rw-test 2>/dev/null || ! touch /etc/.rw-test 2>/dev/null; then
    rm -f /var/lib/.rw-test /etc/.rw-test 2>/dev/null
    # Try to remount rw — RO may be a transient condition (journaling recovery)
    log "Filesystem is read-only, attempting remount rw..."
    mount -o remount,rw / 2>/dev/null || true
    if ! touch /var/lib/.rw-test2 2>/dev/null || ! touch /etc/.rw-test2 2>/dev/null; then
        rm -f /var/lib/.rw-test2 /etc/.rw-test2 2>/dev/null
        # Truly RO: can't restore files, can't increment attempt counter, can't
        # delete rollback.sh to break ConditionPathExists. Jump straight to reboot
        # with a long delay to throttle the loop (otherwise reboots every ~30s).
        log "ERROR: Filesystem is read-only even after remount! Sleeping 5min then rebooting."
        sync
        sleep 300
        reboot || reboot -f || echo b > /proc/sysrq-trigger
        exit 0
    fi
    rm -f /var/lib/.rw-test2 /etc/.rw-test2
    log "Remount succeeded, continuing with rollback"
    RO_FS=0
else
    rm -f /var/lib/.rw-test /etc/.rw-test
    # Track rollback attempts to break infinite reboot loops
    attempts=0
    if [[ -f "$ATTEMPT_FILE" ]]; then
        attempts=$(cat "$ATTEMPT_FILE" 2>/dev/null || echo "0")
        # Sanitize: if corrupted to non-numeric, reset to 0
        [[ "$attempts" =~ ^[0-9]+$ ]] || attempts=0
    fi
    attempts=$((attempts + 1))
    # Atomic write: mv is atomic on ext4, prevents corruption on power loss
    echo "$attempts" > "$ATTEMPT_FILE.tmp" 2>/dev/null && \
        mv "$ATTEMPT_FILE.tmp" "$ATTEMPT_FILE" 2>/dev/null || true
    if [[ $attempts -gt 3 ]]; then
        log "ERROR: Rollback attempted $attempts times. Giving up to prevent infinite loop."
        # Delete backup dir to break the ConditionPathExists cycle
        rm -rf "$BACKUP_DIR"
        log "Backup dir deleted. Rebooting to whatever state we have."
        sync
        sleep 5
        reboot || reboot -f || echo b > /proc/sysrq-trigger
        exit 0
    fi
    log "Rollback attempt $attempts of 3"
fi

# Restore backed-up files (skip if filesystem is read-only)
if [[ $RO_FS -eq 0 ]] && [[ -f "$BACKUP_DIR/pre-migration.tar" ]]; then
    # Verify tar integrity before extracting (corrupt tar -> skip restore, still reboot)
    if ! tar tf "$BACKUP_DIR/pre-migration.tar" --absolute-names >/dev/null 2>&1; then
        log "ERROR: Backup tar is corrupt! Skipping restore, rebooting anyway"
    else
        log "Restoring files from backup tar..."
        # --unlink-first: required so tar can replace symlinks/dirs with files
        # (or vice versa). Tradeoff: power loss between unlink and write loses
        # the file entirely. Acceptable because file-type mismatch is more likely
        # than power loss during the ~0.1s extraction window.
        tar xf "$BACKUP_DIR/pre-migration.tar" --absolute-names --unlink-first
        tar_rc=$?
        sync  # Flush restored files to disk immediately
        if [[ $tar_rc -eq 0 ]]; then
            log "Files restored successfully"
        else
            log "ERROR: tar restore failed (exit $tar_rc), continuing anyway"
        fi
    fi
elif [[ $RO_FS -eq 0 ]]; then
    log "ERROR: No backup tar found at $BACKUP_DIR/pre-migration.tar"
fi

# Restore git branch (force checkout -- working tree may be dirty)
# Timeout prevents hanging on NFS/hook issues
if [[ $RO_FS -eq 0 ]] && [[ -f "$BACKUP_DIR/git-branch" ]]; then
    saved_branch=$(cat "$BACKUP_DIR/git-branch")
    if [[ -n "$saved_branch" && "$saved_branch" != "unknown" && "$saved_branch" != "HEAD" ]]; then
        log "Restoring git branch: $saved_branch"
        timeout 60 sudo -H -u pi git -C /home/pi/dev/mjolnir-hamma checkout -f "$saved_branch" || \
            log "WARNING: git checkout failed or timed out"
    fi
fi

# Reload systemd (timer/service files may have been restored)
systemctl daemon-reload || true

# Restart networkd to apply restored network configs
# Safe for wwan0: it is Unmanaged=yes, only eth0/eth1 are affected
# Timeout prevents a hung networkd from blocking rollback+reboot
timeout 30 systemctl restart systemd-networkd || true

# Restart wwan timer (restored version)
systemctl restart wwan-check.timer 2>/dev/null || true

# Remove the dead man's switch units (they didn't exist before arming,
# so they are not in the backup tar and must be explicitly cleaned up).
systemctl stop deadman-rollback.timer 2>/dev/null || true
systemctl disable deadman-rollback.timer 2>/dev/null || true
rm -f /etc/systemd/system/deadman-rollback.timer
rm -f /etc/systemd/system/deadman-rollback.service
systemctl daemon-reload 2>/dev/null || true

# Delete the rollback script FIRST — this is what ConditionPathExists checks.
# Even if rm -rf is interrupted by power loss, the condition fails on next boot.
rm -f "$BACKUP_DIR/rollback.sh"
rm -rf "$BACKUP_DIR"

log "Rollback complete. Rebooting in 10 seconds..."
sync
sleep 10
reboot || {
    log "ERROR: reboot failed, trying forced reboot"
    reboot -f || {
        log "ERROR: forced reboot failed, trying sysrq"
        echo b > /proc/sysrq-trigger
    }
}
# If all reboot methods failed, exit non-zero so systemd marks service as failed
exit 1
ROLLBACK_EOF
    chmod +x "$ROLLBACK_SCRIPT" || { echo "ERROR: chmod +x failed on rollback script"; cleanup_partial_arm 1; }

    # Verify the rollback script was fully written (heredoc fails silently on full disk)
    if [[ ! -s "$ROLLBACK_SCRIPT" ]] || ! grep -q 'sysrq-trigger' "$ROLLBACK_SCRIPT"; then
        echo "ERROR: Rollback script is empty or truncated"; cleanup_partial_arm 1
    fi

    # --- Create systemd service (runs the rollback) ---
    cat > "/etc/systemd/system/$SERVICE_UNIT" <<EOF
[Unit]
Description=Migration Dead Man's Switch - Rollback Service
ConditionPathExists=$ROLLBACK_SCRIPT

[Service]
Type=oneshot
ExecStart=$ROLLBACK_SCRIPT
# Oneshot services spend their entire life in "activating" (start phase).
# Both timeouts must be infinity to prevent systemd from killing the rollback.
TimeoutStartSec=infinity
TimeoutStopSec=infinity
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

    # Verify unit files were written (heredocs fail silently on full disk)
    if [[ ! -s "/etc/systemd/system/$SERVICE_UNIT" ]]; then
        echo "ERROR: Service unit file is empty or missing"; cleanup_partial_arm 1
    fi
    if [[ ! -s "/etc/systemd/system/$TIMER_UNIT" ]]; then
        echo "ERROR: Timer unit file is empty or missing"; cleanup_partial_arm 1
    fi

    # Enable (survives reboot) and start
    # Use cleanup_partial_arm on failure to remove orphan unit files
    systemctl daemon-reload || { echo "ERROR: daemon-reload failed"; cleanup_partial_arm 1; }
    systemctl enable "$TIMER_UNIT" || { echo "ERROR: failed to enable timer"; cleanup_partial_arm 1; }
    systemctl start "$TIMER_UNIT" || { echo "ERROR: failed to start timer"; cleanup_partial_arm 1; }

    # Belt-and-suspenders: verify timer is actually running
    if ! systemctl is-active "$TIMER_UNIT" &>/dev/null; then
        echo "ERROR: Timer failed to start despite no errors. Aborting."
        cleanup_partial_arm 1
    fi

    # Disarm the interrupt trap -- we are now in a consistent armed state
    trap - INT TERM HUP

    logger -t "deadman-arm" "Dead man's switch armed for $minutes minutes"

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

    # Check if the rollback service is already running (timer just fired).
    # Type=oneshot services are in "activating" state during execution,
    # never "active" (is-active returns false). Check ActiveState directly.
    local svc_state
    svc_state=$(systemctl show "$SERVICE_UNIT" --property=ActiveState 2>/dev/null || echo "unknown")
    # Strip "ActiveState=" prefix (more portable than --value)
    svc_state="${svc_state#ActiveState=}"
    if [[ "$svc_state" == "activating" || "$svc_state" == "deactivating" ]]; then
        echo "ERROR: Rollback service is ALREADY RUNNING (state: $svc_state)!"
        echo "It is too late to defuse. The system will reboot shortly."
        echo "Wait for reboot, then assess the state."
        echo "After reboot the rollback will have completed. Re-arm if needed."
        logger -t "deadman-arm" "Defuse attempted but rollback already in progress (state: $svc_state)"
        exit 1
    fi

    if ! systemctl is-active "$TIMER_UNIT" &>/dev/null; then
        echo "Timer is not active. Cleaning up any partial state..."
    fi

    # Stop timer FIRST to prevent it from re-triggering the service between stops
    systemctl stop "$TIMER_UNIT" 2>/dev/null || true
    systemctl disable "$TIMER_UNIT" 2>/dev/null || true

    # Stop the service. Timeout protects against TOCTOU race: if the timer fired
    # between our check above and the timer stop, the rollback script's
    # trap '' TERM + TimeoutStopSec=infinity would block forever.
    timeout 10 systemctl stop "$SERVICE_UNIT" 2>/dev/null || true

    # Re-check: if the rollback started during our stop attempt, warn the user
    # instead of printing a misleading "DEFUSED" message.
    svc_state=$(systemctl show "$SERVICE_UNIT" --property=ActiveState 2>/dev/null || echo "unknown")
    svc_state="${svc_state#ActiveState=}"
    if [[ "$svc_state" == "activating" || "$svc_state" == "deactivating" ]]; then
        echo "WARNING: Rollback service started during defuse (state: $svc_state)!"
        echo "The system will reboot shortly. Wait for reboot, then assess state."
        logger -t "deadman-arm" "Rollback started during defuse, system will reboot"
        exit 1
    fi

    # Remove systemd units
    rm -f "/etc/systemd/system/$TIMER_UNIT"
    rm -f "/etc/systemd/system/$SERVICE_UNIT"
    systemctl daemon-reload

    logger -t "deadman-arm" "Dead man's switch defused"

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
        systemctl list-timers "$TIMER_UNIT" --no-pager 2>/dev/null
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
