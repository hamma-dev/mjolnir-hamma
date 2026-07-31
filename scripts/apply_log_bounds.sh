#!/usr/bin/env bash
#
# apply_log_bounds.sh (HAM-113)
#
# Apply the /var/log size bounds to an ALREADY-DEPLOYED sensor, without a full
# reinstall. Idempotent -- safe to run repeatedly. Run ON the sensor (needs
# sudo).
#
# The work itself lives in unified_install/lib/log_bounds.sh, the single shared
# implementation used by BOTH this script and configure_log_bounds() in
# unified_install/lib/hardware.sh. This script adds only the two things the
# installer does not need: --check reporting, and reclaiming overage that
# accumulated before the bounds went on.
#
# Usage:
#   sudo bash scripts/apply_log_bounds.sh            # apply the bounds
#   sudo bash scripts/apply_log_bounds.sh --check    # report status, no changes
#
# Applies:
#   1. journald SystemMaxUse cap (drop-in)
#   2. maxsize 100M on the rsyslog logrotate stanzas (backup under /var/backups)
#   3. hourly logrotate run so maxsize is enforced within the hour
# and reclaims any current overage (journal vacuum + one logrotate pass).

set -euo pipefail

CHECK_ONLY=false
if [[ "${1:-}" == "--check" ]]; then
    CHECK_ONLY=true
elif [[ -n "${1:-}" ]]; then
    echo "Unknown argument: $1" >&2
    echo "Usage: $0 [--check]" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../unified_install/lib/log_bounds.sh
source "$SCRIPT_DIR/../unified_install/lib/log_bounds.sh"

log() { echo "[apply-log-bounds] $*"; }

log "Current /var/log usage:"
du -sh /var/log 2>/dev/null || true
df -h / | awk 'NR==1 || /\/$/ {print}'

if [[ "$CHECK_ONLY" == "true" ]]; then
    log "Status (--check, no changes made):"
    log "  journald cap:      $(log_bounds_status_journald)"
    log "  rsyslog maxsize:   $(log_bounds_status_rsyslog)"
    log "  hourly logrotate:  $(log_bounds_status_cron)"
    if log_bounds_all_present; then
        log "All bounds present."
    else
        log "One or more bounds MISSING -- re-run without --check to apply."
    fi
    exit 0
fi

# --- 1. journald cap ---
result="$(log_bounds_apply_journald || true)"
case "$result" in
    APPLIED)     log "journald cap applied and journald restarted" ;;
    MISSING_SRC) log "WARN: journald bounds file not found in $LOG_BOUNDS_FILES_DIR; skipping" ;;
    *)           log "WARN: unexpected journald result: $result" ;;
esac

# --- 2. rsyslog logrotate maxsize (idempotent, with one-time backup) ---
result="$(log_bounds_apply_rsyslog || true)"
case "$result" in
    APPLIED)        log "added 'maxsize $LOG_BOUNDS_MAXSIZE' to $LOG_BOUNDS_RSYSLOG_LR (backup at $LOG_BOUNDS_RSYSLOG_BAK)" ;;
    ALREADY)        log "rsyslog logrotate already has a maxsize cap; leaving as-is" ;;
    MISSING_TARGET) log "WARN: $LOG_BOUNDS_RSYSLOG_LR not found; skipping rsyslog cap" ;;
    *)              log "WARN: unexpected rsyslog result: $result" ;;
esac

# --- 3. hourly logrotate ---
result="$(log_bounds_apply_cron || true)"
case "$result" in
    APPLIED)     log "hourly logrotate installed at $LOG_BOUNDS_CRON_DST" ;;
    MISSING_SRC) log "WARN: hourly logrotate file not found in $LOG_BOUNDS_FILES_DIR; skipping" ;;
    *)           log "WARN: unexpected cron result: $result" ;;
esac

# --- 4. reclaim any current overage now ---
log_bounds_reclaim

log "Done. New /var/log usage:"
du -sh /var/log 2>/dev/null || true
