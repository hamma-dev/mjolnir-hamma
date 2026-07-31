#!/bin/bash
# Shared /var/log size-bound operations (HAM-113)
#
# SINGLE implementation of the three defences that keep a runaway log source
# (brokkr science_ingest spamming a dead AGS, HAM-112, measured at 3 GB/hr)
# from filling the SD card:
#   1. journald SystemMaxUse cap (drop-in)
#   2. maxsize on the rsyslog logrotate stanzas
#   3. hourly logrotate run so maxsize is enforced within the hour
#
# Used by BOTH callers, so the logic exists in exactly one place:
#   - configure_log_bounds() in unified_install/lib/hardware.sh  (install time)
#   - scripts/apply_log_bounds.sh                                (fleet remediation)
#
# Deliberately free of any dependency on the installer framework -- no
# common.sh logging, no $DRY_RUN, no manifest_add -- so the standalone script
# can source it directly. Callers supply their own logging, dry-run handling
# and manifest bookkeeping.
#
# Each apply function echoes ONE result token and returns 0 unless noted:
#   APPLIED         the change was made
#   ALREADY         already in place; idempotent no-op
#   MISSING_SRC     the shipped asset is absent          (returns 1)
#   MISSING_TARGET  the file to be modified is absent    (returns 1)

# Resolve files/ the same way the other libs do, so this works whether sourced
# by the installer (which exports FILES_DIR) or by the standalone script.
LOG_BOUNDS_FILES_DIR="${FILES_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../files" && pwd 2>/dev/null || echo "/home/pi/dev/mjolnir-hamma/files")}"

LOG_BOUNDS_JOURNALD_DIR="/etc/systemd/journald.conf.d"
LOG_BOUNDS_JOURNALD_DST="$LOG_BOUNDS_JOURNALD_DIR/00-sensor-bounds.conf"
LOG_BOUNDS_JOURNALD_SRC="journald-sensor-bounds.conf"

LOG_BOUNDS_RSYSLOG_LR="/etc/logrotate.d/rsyslog"
# Backup MUST live outside /etc/logrotate.d/ -- logrotate reads every file in
# that directory, so a backup kept there triggers "duplicate log entry" errors
# for every log path it covers. Regression found during the mj05 E2E.
LOG_BOUNDS_RSYSLOG_BAK="/var/backups/logrotate-rsyslog.mjolnir-orig"

LOG_BOUNDS_CRON_SRC="logrotate-hourly.sh"
LOG_BOUNDS_CRON_DST="/etc/cron.hourly/mjolnir-logrotate"

LOG_BOUNDS_MAXSIZE="100M"
LOG_BOUNDS_JOURNAL_MAX="500M"

# --- apply -------------------------------------------------------------------

log_bounds_apply_journald() {
    local src="$LOG_BOUNDS_FILES_DIR/$LOG_BOUNDS_JOURNALD_SRC"
    if [[ ! -f "$src" ]]; then
        echo "MISSING_SRC"
        return 1
    fi
    sudo mkdir -p "$LOG_BOUNDS_JOURNALD_DIR"
    sudo cp "$src" "$LOG_BOUNDS_JOURNALD_DST"
    sudo chmod 0644 "$LOG_BOUNDS_JOURNALD_DST"
    sudo systemctl restart systemd-journald
    echo "APPLIED"
}

log_bounds_apply_rsyslog() {
    if [[ ! -f "$LOG_BOUNDS_RSYSLOG_LR" ]]; then
        echo "MISSING_TARGET"
        return 1
    fi
    if sudo grep -q "maxsize" "$LOG_BOUNDS_RSYSLOG_LR"; then
        echo "ALREADY"
        return 0
    fi
    sudo mkdir -p "$(dirname "$LOG_BOUNDS_RSYSLOG_BAK")"
    sudo cp -a "$LOG_BOUNDS_RSYSLOG_LR" "$LOG_BOUNDS_RSYSLOG_BAK"
    sudo sed -i "/^{/a\\    maxsize $LOG_BOUNDS_MAXSIZE" "$LOG_BOUNDS_RSYSLOG_LR"
    echo "APPLIED"
}

log_bounds_apply_cron() {
    local src="$LOG_BOUNDS_FILES_DIR/$LOG_BOUNDS_CRON_SRC"
    if [[ ! -f "$src" ]]; then
        echo "MISSING_SRC"
        return 1
    fi
    sudo cp "$src" "$LOG_BOUNDS_CRON_DST"
    sudo chmod 0755 "$LOG_BOUNDS_CRON_DST"
    echo "APPLIED"
}

# Reclaim any overage that accumulated before the bounds went on. Safe to call
# when already within bounds; not needed on a fresh install.
log_bounds_reclaim() {
    sudo journalctl --vacuum-size="$LOG_BOUNDS_JOURNAL_MAX" >/dev/null 2>&1 || true
    sudo /usr/sbin/logrotate /etc/logrotate.conf || true
}

# --- status (read-only) ------------------------------------------------------

log_bounds_status_journald() {
    [[ -f "$LOG_BOUNDS_JOURNALD_DST" ]] && echo "PRESENT" || echo "MISSING"
}

log_bounds_status_rsyslog() {
    grep -q "maxsize" "$LOG_BOUNDS_RSYSLOG_LR" 2>/dev/null && echo "PRESENT" || echo "MISSING"
}

log_bounds_status_cron() {
    [[ -f "$LOG_BOUNDS_CRON_DST" ]] && echo "PRESENT" || echo "MISSING"
}

# Returns 0 only when all three defences are in place. Intended for
# verify_deployment.sh and for the --check path of the remediation script.
log_bounds_all_present() {
    [[ "$(log_bounds_status_journald)" == "PRESENT" ]] &&
    [[ "$(log_bounds_status_rsyslog)" == "PRESENT" ]] &&
    [[ "$(log_bounds_status_cron)" == "PRESENT" ]]
}
