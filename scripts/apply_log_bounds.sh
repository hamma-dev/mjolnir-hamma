#!/usr/bin/env bash
#
# apply_log_bounds.sh (HAM-113)
#
# Apply the /var/log size bounds to an ALREADY-DEPLOYED sensor, without a full
# reinstall. Idempotent -- safe to run repeatedly. Run ON the sensor (needs
# sudo). This is the fleet-remediation counterpart to configure_log_bounds() in
# unified_install/lib/hardware.sh.
#
# Usage:
#   sudo bash scripts/apply_log_bounds.sh            # apply the bounds
#   sudo bash scripts/apply_log_bounds.sh --check    # report status, no changes
#
# Applies:
#   1. journald SystemMaxUse cap (drop-in)
#   2. maxsize 100M on the rsyslog logrotate stanzas (backup at .mjolnir-orig)
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
FILES_DIR="$(cd "$SCRIPT_DIR/../files" && pwd)"

JOURNALD_DST="/etc/systemd/journald.conf.d/00-sensor-bounds.conf"
RSYSLOG_LR="/etc/logrotate.d/rsyslog"
# Backup MUST live outside /etc/logrotate.d/ -- logrotate reads every file in
# that dir, so a backup there causes "duplicate log entry" errors.
RSYSLOG_BAK="/var/backups/logrotate-rsyslog.mjolnir-orig"
CRON_DST="/etc/cron.hourly/mjolnir-logrotate"

log() { echo "[apply-log-bounds] $*"; }

log "Current /var/log usage:"
du -sh /var/log 2>/dev/null || true
df -h / | awk 'NR==1 || /\/$/ {print}'

if [[ "$CHECK_ONLY" == "true" ]]; then
    log "Status (--check, no changes made):"
    if [[ -f "$JOURNALD_DST" ]]; then log "  journald cap:      PRESENT"; else log "  journald cap:      MISSING"; fi
    if grep -q "maxsize" "$RSYSLOG_LR" 2>/dev/null; then log "  rsyslog maxsize:   PRESENT"; else log "  rsyslog maxsize:   MISSING"; fi
    if [[ -f "$CRON_DST" ]]; then log "  hourly logrotate:  PRESENT"; else log "  hourly logrotate:  MISSING"; fi
    exit 0
fi

# --- 1. journald cap ---
sudo mkdir -p "$(dirname "$JOURNALD_DST")"
sudo cp "$FILES_DIR/journald-sensor-bounds.conf" "$JOURNALD_DST"
sudo chmod 0644 "$JOURNALD_DST"
sudo systemctl restart systemd-journald
log "journald cap applied and journald restarted"

# --- 2. rsyslog logrotate maxsize (idempotent, with one-time backup) ---
if [[ -f "$RSYSLOG_LR" ]]; then
    if grep -q "maxsize" "$RSYSLOG_LR"; then
        log "rsyslog logrotate already has a maxsize cap; leaving as-is"
    else
        sudo mkdir -p "$(dirname "$RSYSLOG_BAK")"
        sudo cp -a "$RSYSLOG_LR" "$RSYSLOG_BAK"
        sudo sed -i '/^{/a\    maxsize 100M' "$RSYSLOG_LR"
        log "added 'maxsize 100M' to $RSYSLOG_LR (backup at $RSYSLOG_BAK)"
    fi
else
    log "WARN: $RSYSLOG_LR not found; skipping rsyslog cap"
fi

# --- 3. hourly logrotate ---
sudo cp "$FILES_DIR/logrotate-hourly.sh" "$CRON_DST"
sudo chmod 0755 "$CRON_DST"
log "hourly logrotate installed at $CRON_DST"

# --- 4. reclaim any current overage now ---
sudo journalctl --vacuum-size=500M >/dev/null 2>&1 || true
sudo /usr/sbin/logrotate /etc/logrotate.conf || true

log "Done. New /var/log usage:"
du -sh /var/log 2>/dev/null || true
