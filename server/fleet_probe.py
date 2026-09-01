#!/usr/bin/env python3

"""Daily fleet configuration probe (HAM-189).

Measures each unit's CONFIGURATION, writes a snapshot only when it CHANGED, and
posts one digest a day.

WHY IT LOOKS LIKE THIS -- read before altering the shape.

  Write-if-changed. The snapshot is rewritten only when measured state differs
  from what is already committed. A stable fleet therefore produces no commits,
  and `git log` on the snapshot IS the list of every state change the fleet has
  had. That artifact did not exist for mj43: establishing when its front end came
  back on (2026-07-02) took real forensic work, and the field log was not
  corrected until 2026-07-28 -- 26 days, and only because someone was doing
  unrelated triage.

  CONFIGURATION, not TELEMETRY. Threshold in mV is a setting and belongs here;
  load current in amps is a measurement and does not. Anything that varies run to
  run would make the snapshot differ every run, which would commit every run and
  destroy the signal entirely. That is the whole reason for the distinction -- do
  not add a timestamp, a voltage, or an uptime to the snapshot.

  Unreachable is DATA, not failure. A unit that cannot be probed is reported as
  such and its previous row is preserved untouched. Dropping it would silently
  look like "no change", and a unit nobody can reach is itself worth knowing --
  mj02 was invisible for exactly this reason.

  The digest carries a standing section, not just changes. Reporting only
  transitions means a dark sensor is announced once and then invisible; the
  standing list keeps it visible every day until it is resolved.

Deliberately NOT here: this does not decide whether a change was expected. Once
the write-hook (mjol_array) updates the snapshot as part of making a change, a
logged change leaves no diff and only UNLOGGED changes surface. Until then, every
change surfaces.
"""

# Standard library imports
import argparse
import csv
import io
import os
import subprocess
import sys

# Local imports -- ags.py is stdlib-only, so its startup-file parser imports
# cleanly off-sensor. Reused rather than re-implemented so the two cannot drift.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
try:
    from ags import parse_startup_state
except ImportError:                                    # pragma: no cover
    parse_startup_state = None

HAMMA_SENSORS = list(range(1, 10))
PAMMA_SENSORS = [50, 51, 52, 53, 54, 56]
AUMMA_SENSORS = [41, 42, 43]
ARRAYS = {"hamma": HAMMA_SENSORS, "pamma": PAMMA_SENSORS, "aumma": AUMMA_SENSORS}

# Column order is the snapshot's schema. Appending is safe; reordering rewrites
# every row and produces one enormous meaningless diff.
FIELDS = [
    "unit", "front_end", "brokkr_mode",
    "threshold_1_mv", "threshold_2_mv", "gain_fast", "gain_slow",
    "mjolnir_hamma", "brokkr", "sindri", "hamma", "notifiers",
]

UNREACHABLE = "unreachable"
UNKNOWN = "unknown"

# One ssh per unit. Every lookup is `|| echo unknown` so a single missing piece
# degrades that field instead of losing the whole unit.
REMOTE = r'''
U=~/.config/brokkr/hamma/unit.toml
PIN=$(awk '/^\[relay\]/{f=1;next} /^\[/{f=0} f&&/pin/{print $3}' $U 2>/dev/null)
AH=$(awk '/^\[relay\]/{f=1;next} /^\[/{f=0} f&&/active_high/{print $3}' $U 2>/dev/null)
if [ -n "$PIN" ]; then
  LVL=$(raspi-gpio get $PIN 2>/dev/null | grep -o 'level=[01]' | cut -d= -f2)
  if [ -n "$LVL" ] && [ -n "$AH" ]; then
    if { [ "$LVL" = "0" ] && [ "$AH" = "true" ]; } || { [ "$LVL" = "1" ] && [ "$AH" = "false" ]; }
    then echo "front_end=on"; else echo "front_end=off"; fi
  else echo "front_end=unknown"; fi
else echo "front_end=no_relay"; fi
# Mode has FOUR placements (HAM-184) and reading only one of them is how mj05 was
# first reported as `default` when it actually runs `nochargecontroller`. Resolved
# here in brokkr's own precedence order: CLI arg beats env beats config file.
#   1. --mode on the running command line (drop-in ExecStart= or the unit's own)
#   2. BROKKR_MODE in the process environment (drop-in Environment=)
#   3. `mode = ` in the unit's local mode.toml
P=$(systemctl show brokkr-hamma-default.service -p MainPID --value 2>/dev/null)
M=$(sudo cat /proc/$P/cmdline 2>/dev/null | tr '\0' '\n' | grep -A1 '^--mode$' | tail -1)
if [ -z "$M" ] || [ "$M" = "--mode" ]; then
  M=$(sudo cat /proc/$P/environ 2>/dev/null | tr '\0' '\n' | grep '^BROKKR_MODE=' | cut -d= -f2)
fi
if [ -z "$M" ]; then
  M=$(awk -F'"' '/^ *mode *=/{print $2}' ~/.config/brokkr/hamma/mode.toml 2>/dev/null | head -1)
fi
echo "brokkr_mode=${M:-default}"
# Git SHAs, not package versions. The units run Python 3.7, so importlib.metadata
# does not exist; pkg_resources works for most but NOT sindri, which runs from its
# own sindrienv and is not installed in ltgenv. A SHA is uniform across all five,
# more precise than a version string, and independent of the interpreter.
for repo in mjolnir-hamma brokkr sindri notifiers hamma; do
  KEY=$(echo "$repo" | tr - _)
  echo "$KEY=$(git -C /home/pi/dev/$repo rev-parse --short HEAD 2>/dev/null || echo unknown)"
done
timeout 15 ssh -o ConnectTimeout=5 -o BatchMode=yes hamma cat /ags/scripts/startup 2>/dev/null \
  | sed 's/^/AGS:/' || true
'''


def ssh_cmd(port):
    """Reach the unit through its autossh tunnel on the VPS."""
    return ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
            "pi@localhost", "-p", str(port)]


def probe_unit(number, timeout=90):
    """Return a row dict for one unit. Never raises; unreachable is a value."""
    row = {f: UNKNOWN for f in FIELDS}
    row["unit"] = "mjolnir{:02d}".format(number)
    try:
        result = subprocess.run(
            ssh_cmd(10000 + number) + ["bash -s"],
            input=REMOTE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return dict(row, front_end=UNREACHABLE)
    if result.returncode != 0:
        return dict(row, front_end=UNREACHABLE)

    ags_lines = []
    for line in result.stdout.splitlines():
        if line.startswith("AGS:"):
            ags_lines.append(line[4:])
        elif "=" in line:
            key, _, value = line.partition("=")
            if key in row:
                row[key] = value.strip() or UNKNOWN

    if ags_lines and parse_startup_state is not None:
        state = parse_startup_state("\n".join(ags_lines))
        row["threshold_1_mv"] = state.get("threshold_1_mv", UNKNOWN)
        row["threshold_2_mv"] = state.get("threshold_2_mv", UNKNOWN)
        row["gain_fast"] = state.get("gain_fast", UNKNOWN)
        row["gain_slow"] = state.get("gain_slow", UNKNOWN)
    return row


def expected_offline(repo):
    """Units the fleet table says should not be up, as {unit: reason}.

    Read from `inventory-manifest.csv`'s existing `status` column rather than a
    new file. sensor-log already has a documented sync problem between the README
    fleet table, the manifest and the per-unit profiles; adding a fourth place to
    record fleet state would make that worse. `status` is human-owned, already
    rendered on log.hamma.dev, and already carries `Retired`.

    Recognised: `Retired`, `Offline`, `Shelf`, `Lab` (case-insensitive). Anything
    else -- including blank and `OK` -- means the unit is expected to be up.
    """
    if not repo:
        return {}
    path = os.path.join(repo, "inventory-manifest.csv")
    if not os.path.isfile(path):
        return {}
    recognised = {"retired", "offline", "shelf", "lab"}
    out = {}
    try:
        with open(path) as handle:
            for row in csv.DictReader(handle):
                status = (row.get("status") or "").strip()
                if status.lower() in recognised:
                    out[row["unit"]] = status
    except (OSError, csv.Error):
        return {}
    return out


def read_snapshot(path):
    """Existing snapshot as {unit: row}, or {} if there is not one yet."""
    if not os.path.isfile(path):
        return {}
    with open(path) as handle:
        return {r["unit"]: r for r in csv.DictReader(handle)}


def render(rows):
    """Snapshot text. Sorted so the file is diff-stable."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for unit in sorted(rows):
        writer.writerow({f: rows[unit].get(f, UNKNOWN) for f in FIELDS})
    return buf.getvalue()


def diff(previous, current):
    """[(unit, field, old, new)] for every changed field.

    An unreachable unit is skipped entirely rather than diffed -- otherwise every
    outage would read as "everything about this unit changed".
    """
    changes = []
    for unit in sorted(current):
        new = current[unit]
        if new.get("front_end") == UNREACHABLE:
            continue
        old = previous.get(unit)
        if old is None:
            changes.append((unit, "*", "-", "first seen"))
            continue
        for field in FIELDS[1:]:
            if str(old.get(field, "")) != str(new.get(field, "")):
                changes.append((unit, field, old.get(field, ""), new.get(field, "")))
    return changes


def merge(previous, current):
    """Carry an unreachable unit's previous row forward untouched."""
    merged = dict(previous)
    for unit, row in current.items():
        if row.get("front_end") == UNREACHABLE and unit in previous:
            continue
        merged[unit] = row
    return merged


def not_capturing(rows):
    """Units that are not ingesting, for the standing section of the digest.

    Either the front end is off, or brokkr is in a nosensor mode. Both mean no
    data reaches the server, which is what an operator actually cares about.
    """
    out = []
    for unit in sorted(rows):
        row = rows[unit]
        if row.get("front_end") in (UNREACHABLE, "no_relay", UNKNOWN):
            continue
        if row.get("front_end") == "off" or "nosensor" in str(row.get("brokkr_mode", "")):
            out.append((unit, row.get("front_end"), row.get("brokkr_mode")))
    return out


def changed_since(repo, snapshot_rel, unit, current_state, limit=200):
    """Date the unit last entered its current state, from the snapshot's history.

    Walks commits touching the snapshot newest-first and returns the date of the
    oldest consecutive commit that still shows `current_state`. Free of any extra
    bookkeeping -- the git history already knows, which is the whole point of
    write-if-changed. Returns None if it cannot be determined.
    """
    try:
        log = subprocess.run(
            ["git", "-C", repo, "log", "--format=%H %ad", "--date=short",
             "-n", str(limit), "--", snapshot_rel],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=30)
        if log.returncode != 0:
            return None
        oldest_matching = None
        for line in log.stdout.splitlines():
            sha, _, date = line.partition(" ")
            show = subprocess.run(
                ["git", "-C", repo, "show", "{}:{}".format(sha, snapshot_rel)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=30)
            if show.returncode != 0:
                break
            rows = {r["unit"]: r for r in csv.DictReader(io.StringIO(show.stdout))}
            row = rows.get(unit)
            if row is None:
                break
            if (row.get("front_end"), row.get("brokkr_mode")) != current_state:
                break
            oldest_matching = date.strip()
        return oldest_matching
    except (OSError, subprocess.SubprocessError, csv.Error):
        return None


def digest(rows, changes, unreachable, baseline=False, repo=None,
           snapshot_rel=None, expected=None):
    probed = len(rows) - len(unreachable)
    if baseline:
        lines = ["fleet probe: baseline established, {} units".format(probed)]
    else:
        lines = ["fleet probe: {} units, {} change(s)".format(probed, len(changes))]
        if changes:
            lines.append("")
            for unit, field, old, new in changes:
                lines.append("  {}  {} {} -> {}".format(unit, field, old, new)
                             if field != "*" else "  {}  {}".format(unit, new))
    standing = not_capturing(rows)
    if standing:
        lines.append("")
        lines.append("not capturing:")
        for unit, front, mode in standing:
            since = None
            if repo and snapshot_rel:
                since = changed_since(repo, snapshot_rel, unit, (front, mode))
            lines.append("  {}  front_end={}, mode={}{}".format(
                unit, front, mode, "   since {}".format(since) if since else ""))
    # Split expected-offline out of the unreachable list. Lumping them together
    # trains people to skim past the line, which is how a genuinely dark unit
    # gets missed -- the whole reason this exists.
    expected = expected or {}
    unexpected = [u for u in unreachable if u not in expected]
    known = [u for u in unreachable if u in expected]
    if unexpected:
        lines.append("")
        lines.append("UNREACHABLE: " + ", ".join(unexpected))
    if known:
        lines.append("")
        lines.append("expected offline: " + ", ".join(
            "{} ({})".format(u, expected[u]) for u in known))
    return "\n".join(lines)


def commit_snapshot(repo, snapshot_rel, message):
    """Commit and push the snapshot. Returns (ok, detail)."""
    try:
        add = subprocess.run(["git", "-C", repo, "add", "--", snapshot_rel],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True, timeout=60)
        if add.returncode != 0:
            return False, add.stdout.strip()
        commit = subprocess.run(["git", "-C", repo, "commit", "-m", message],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, timeout=60)
        if commit.returncode != 0:
            return False, commit.stdout.strip()
        push = subprocess.run(["git", "-C", repo, "push", "origin", "HEAD"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=120)
        if push.returncode != 0:
            return False, push.stdout.strip()
        return True, "committed and pushed"
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)


def send_digest(text, key_file, channel):
    """Post the digest. Uses the sender DIRECTLY, not Notifier.

    Notifier.send() is a silent no-op when no sender could be constructed, so a
    misconfigured key file would look identical to a successful send. For an
    unattended job that is the difference between "quiet fleet" and "nobody has
    been told anything for a month", so let the failure surface instead.
    """
    from notifiers.google_chat import GoogleChatSender
    GoogleChatSender(key_file, channel=channel).send(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("-a", "--array", choices=sorted(ARRAYS),
                        help="probe one array; default is all")
    parser.add_argument("-p", "--ports", nargs="+", type=int,
                        help="probe specific units by number (mod 10000)")
    parser.add_argument("--repo",
                        help="path to a sensor-log clone; the snapshot is written "
                             "to <repo>/state/fleet-state.csv")
    parser.add_argument("--snapshot", default="fleet-state.csv",
                        help="snapshot path when --repo is not given")
    parser.add_argument("--commit", action="store_true",
                        help="commit and push the snapshot when it changed "
                             "(requires --repo)")
    parser.add_argument("--notify", action="store_true",
                        help="post the digest to chat")
    parser.add_argument("--channel", default="status",
                        help="chat channel for --notify (default: status)")
    parser.add_argument("--key-file", default="/home/pi/.googlechat",
                        help="notification key file for --notify")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the snapshot and digest; write, commit and "
                             "send nothing")
    args = parser.parse_args()

    # Commit and notify are opt-in rather than default-on, so a hand-run probe
    # cannot surprise anyone by pushing or paging. The cron line asks for them.
    if args.commit and not args.repo:
        parser.error("--commit requires --repo")
    snapshot_rel = os.path.join("state", "fleet-state.csv")
    snapshot = (os.path.join(args.repo, snapshot_rel) if args.repo
                else args.snapshot)

    if args.ports:
        numbers = args.ports
    elif args.array:
        numbers = ARRAYS[args.array]
    else:
        numbers = sorted(sum(ARRAYS.values(), []))

    current = {}
    for number in numbers:
        row = probe_unit(number)
        current[row["unit"]] = row
        print("probed {}: {}".format(row["unit"], row["front_end"]),
              file=sys.stderr)

    unreachable = sorted(u for u, r in current.items()
                         if r.get("front_end") == UNREACHABLE)
    previous = read_snapshot(snapshot)
    baseline = not previous
    changes = diff(previous, current)
    merged = merge(previous, current)
    text = render(merged)

    # "since" comes from the snapshot's own git history, so it is only available
    # once there is one to read.
    report = digest(merged, changes, unreachable, baseline=baseline,
                    repo=args.repo if not baseline else None,
                    snapshot_rel=snapshot_rel,
                    expected=expected_offline(args.repo))
    print(report)

    if args.dry_run:
        print("\n--- snapshot (not written) ---", file=sys.stderr)
        print(text, file=sys.stderr)
        return 0

    # Write-if-changed: an identical snapshot must leave no trace at all, so that
    # `git log` on it stays a clean list of real state changes.
    wrote = False
    if baseline or changes:
        directory = os.path.dirname(snapshot)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(snapshot, "w") as handle:
            handle.write(text)
        wrote = True

    status = 0
    if wrote and args.commit:
        summary = ("state: fleet baseline" if baseline else
                   "state: {}".format(", ".join(
                       sorted({u for u, _, _, _ in changes}))))
        ok, detail = commit_snapshot(args.repo, snapshot_rel, summary)
        print("commit: {}".format(detail), file=sys.stderr)
        if not ok:
            status = 1

    # Notify even when nothing changed -- the digest IS the liveness signal. A
    # silent probe and a dead probe must not look the same.
    if args.notify:
        try:
            send_digest(report, args.key_file, args.channel)
            print("digest sent to '{}'".format(args.channel), file=sys.stderr)
        except Exception as error:            # noqa: BLE001 - report, don't mask
            print("digest FAILED to send: {}: {}".format(
                type(error).__name__, error), file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
