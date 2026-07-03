#!/bin/sh
# Managed by mjolnir-hamma (HAM-113) -> /etc/cron.hourly/mjolnir-logrotate
#
# Run logrotate hourly so a log that exceeds `maxsize` (set on the rsyslog
# stanza) is rotated within the hour instead of waiting for the daily run.
# This is what bounds /var/log under sustained log spam.
#
# Safe/idempotent: `daily`/`weekly` stanzas still rotate on their normal
# cadence; `maxsize` only forces an extra rotation when a log has grown past
# the cap since the last run.
/usr/sbin/logrotate /etc/logrotate.conf
exit 0
