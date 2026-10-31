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
import glob
import io
import os
import re
import subprocess
import sys

# Local imports -- ags.py is stdlib-only, so its startup-file parser imports
# cleanly off-sensor. Reused rather than re-implemented so the two cannot drift.
#
# This file may run from either repo layout (mjolnir-hamma/server/, where
# ../scripts/ags.py is a sibling, or brokkr-vps-system, where it is not), so the
# parser is searched for rather than assumed.
#
# A MISSING parser is NOT silently tolerated. Without it, threshold_*_mv and
# gain_* quietly read "unknown" -- losing 4 of the 11 fields AND, because
# "unknown" differs from the previous value, firing a spurious change for every
# unit on the next run, polluting the very history this probe exists to produce.
# main() therefore refuses to probe unless the parser resolved.
AGS_FALLBACK_DIRS = [
    # the VPS checkout: present on the box the probe runs on
    "/home/monitor/dev/mjolnir-hamma/scripts",
]


def load_ags_parser(extra_path=None):
    """Find ags.parse_startup_state. Returns (parser_or_None, source_dir_or_None).

    Search order: explicit path, $FLEET_PROBE_AGS_PATH, sibling ../scripts, then
    the known VPS checkout.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    if extra_path:
        candidates.append(extra_path)
    if os.environ.get("FLEET_PROBE_AGS_PATH"):
        candidates.append(os.environ["FLEET_PROBE_AGS_PATH"])
    candidates.append(os.path.join(here, "..", "scripts"))
    candidates.extend(AGS_FALLBACK_DIRS)

    for directory in candidates:
        if not os.path.isfile(os.path.join(directory, "ags.py")):
            continue
        if directory not in sys.path:
            sys.path.insert(0, directory)
        try:
            from ags import parse_startup_state as parser
        except ImportError:                            # pragma: no cover
            continue
        return parser, directory
    return None, None


parse_startup_state, AGS_SOURCE = load_ags_parser()

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
UNIT_ROW = re.compile(r"^mjolnir\d{2}$")
UNKNOWN = "unknown"

# Fields read from the AGS startup file, which is only reachable while the front
# end is powered. When front_end flips they go blank (off) or populate (on) as a
# mechanical consequence, not an independent config change -- so the digest
# suppresses them on a power transition and reports the front_end flip alone.
# The snapshot still records their true values; only the human-facing digest
# collapses. A threshold/gain change WITHOUT a front_end change is a real
# adjustment and is always reported.
POWER_DERIVED_FIELDS = frozenset(
    {"threshold_1_mv", "threshold_2_mv", "gain_fast", "gain_slow"})

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


PENDING_UNCHECKED = "- [ ] "

# Bound on the pending block. The chat sender posts the digest as one message
# and an over-limit message is rejected outright -- the whole digest is lost,
# not just the overflow. Measured: 18 units x 3 items is ~6.2 kB against a
# 4 kB ceiling, which one array-wide recovery would reach.
PENDING_DIGEST_MAX = 20


def _is_pending_heading(line):
    """True for `## Pending` and the spellings people actually write.

    Heading drift is a documented problem in sensor-log, and the failure here
    is silent: a profile with `### Pending` or `## Pending actions` renders
    perfectly in Markdown and simply never produces a digest line. Accept the
    variants rather than quietly ignore somebody's deferred work.
    """
    stripped = line.strip()
    if not stripped.startswith("#"):
        return False
    return stripped.lstrip("#").strip().rstrip(":").strip().lower() == "pending"


def pending_items(repo, unit):
    """Deferred actions for `unit`, from its own sensor-log profile.

    Same reasoning as expected_offline(): read what a human already maintains
    rather than adding a place to record fleet state. The profile is what
    somebody opens before touching a unit, so the action lives beside the
    history that explains it -- and the originating ticket can be CLOSED,
    cited as background, instead of held open for months waiting on hardware.

    Only `- [ ] ` lines count, and only between the heading and the next one.
    A `- [x] ` line is done and stays in the file as the record that it was.
    HTML comments are skipped: both the template and the profiles carry their
    instructions in a comment INSIDE this block, complete with an example
    item, and broadcasting that example nightly would discredit the whole
    section. Do not "simplify" that away.
    """
    if not repo:
        return []
    # sorted(): glob order is os.scandir order, so a unit with a stale profile
    # left behind in another array directory (profiles do get moved -- mj05
    # hamma<->lab, mj07 camma->lab) would otherwise resolve differently on the
    # VPS than on a developer's box.
    matches = sorted(glob.glob(os.path.join(repo, "deployments", "*",
                                            "{}.md".format(unit))))
    if not matches:
        return []
    try:
        # encoding is explicit and errors are replaced: a single smart quote
        # pasted in from Word would otherwise raise UnicodeDecodeError, which
        # is a ValueError, escape this function, and kill the whole probe run
        # before the digest is built -- losing every unit's configuration
        # change for that day, not just this one line.
        with open(matches[0], encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    out = []
    inside = False
    in_comment = False
    for line in lines:
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if "<!--" in line and "-->" not in line:
            in_comment = True
            continue
        if _is_pending_heading(line):
            inside = True
            continue
        if inside and line.strip().startswith("#"):
            break
        if inside and line.startswith(PENDING_UNCHECKED):
            # An unfilled `- [ ]` is not an action, and must not become a
            # nightly digest line saying nothing.
            text = line[len(PENDING_UNCHECKED):].strip()
            if text:
                out.append(text)
    return out


def read_snapshot(path):
    """Existing snapshot as {unit: row}, or {} if there is not one yet."""
    if not os.path.isfile(path):
        return {}
    # Only real unit rows: a conflicted file (markers parse as rows) or a
    # hand-written CSV without a `unit` column must not invent units or crash
    # the run before the digest goes out.
    with open(path) as handle:
        return parse_snapshot(handle)


def parse_snapshot(lines):
    """{unit: row} from CSV text lines, keeping only real unit rows."""
    return {r["unit"]: r for r in csv.DictReader(lines)
            if UNIT_ROW.match(r.get("unit") or "")}


def render(rows):
    """Snapshot text. Sorted so the file is diff-stable."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for unit in sorted(rows):
        writer.writerow({f: rows[unit].get(f, UNKNOWN) for f in FIELDS})
    return buf.getvalue()


def field_report(rows, field):
    """Group units by their value of one field, for a fleet consistency check.

    Answers "are all units on the same X" from the snapshot alone -- no probing.
    Units sharing a value are listed together; groups are ordered largest first
    so the consensus value leads and any outlier falls to the bottom and is
    obvious. Returns text.
    """
    groups = {}
    for unit in sorted(rows):
        value = rows[unit].get(field, UNKNOWN)
        value = value if str(value).strip() != "" else "(blank)"
        groups.setdefault(value, []).append(unit)

    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))
    total = len(rows)
    lines = ["{}: {} unit(s) in snapshot, {} distinct value(s)".format(
        field, total, len(groups))]
    lines.append("")
    for value, units in ordered:
        count = len(units)
        lines.append("  {:>2} {:<7}  {:<12}  {}".format(
            count, "unit" if count == 1 else "units", value,
            ", ".join(units)))
    lines.append("")
    lines.append("=> uniform" if len(groups) == 1
                 else "=> NOT uniform ({} distinct values)".format(len(groups)))
    return "\n".join(lines)


def diff(previous, current):
    """[(unit, field, old, new)] for every changed field.

    An unreachable unit is skipped entirely rather than diffed -- otherwise every
    outage would read as "everything about this unit changed".

    Two cascades are collapsed to the single event that caused them, because a
    digest nobody reads is worth nothing: a power transition (which drags the
    AGS-derived fields) and a unit coming back into view after never having been
    probed (which drags every field at once). Both report the front_end line
    alone.
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
        # A power transition drags the AGS-derived fields with it; report the
        # front_end flip alone rather than the six-line cascade it causes.
        front_changed = (str(old.get("front_end", ""))
                         != str(new.get("front_end", "")))
        # Same shape, different cause: a unit that was never successfully probed
        # is stored as unreachable with every field UNKNOWN (merge() only writes
        # that row when there is no prior one). The first time it answers, all
        # eleven fields go UNKNOWN -> value at once. That is ONE event -- it came
        # back into view -- not eleven configuration changes, and reporting it as
        # eleven buries the real changes in the same digest. Report the front_end
        # transition alone.
        came_back = (str(old.get("front_end", "")) == UNREACHABLE
                     and str(new.get("front_end", "")) != UNREACHABLE)
        for field in FIELDS[1:]:
            if str(old.get(field, "")) != str(new.get(field, "")):
                if front_changed and field in POWER_DERIVED_FIELDS:
                    continue
                # Suppress only the fields that were genuinely unknown. If a
                # field somehow carried a real old value and changed, that is a
                # real change and still deserves a line.
                if (came_back and field != "front_end"
                        and str(old.get(field, "")) == UNKNOWN):
                    continue
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
    # STANDING, not a transition -- see the rule at the top of this file.
    #
    # The first cut of this fired only when a unit went unreachable -> reachable.
    # That is dead code for most of the fleet: merge() never stores `unreachable`
    # for a unit that was reachable when it entered the snapshot, so a real
    # outage-and-return produces no change line at all. It also announced each
    # item exactly once, which is the failure the standing-section rule exists
    # to prevent -- a deferred action is unresolved-until-done by definition.
    #
    # Listed for every unit reachable THIS RUN, every day, until the box is
    # ticked. Hidden while a unit is unreachable: nobody can act on it, and the
    # ticket that recorded it is closed, so a permanent nag would train people
    # to skim past the section.
    pending_lines = []
    for unit in sorted(set(rows) - set(unreachable)):
        for item in pending_items(repo, unit):
            pending_lines.append("  {}  [ ] {}".format(unit, item))
    if pending_lines:
        lines.append("")
        lines.append("pending actions (edit the unit's profile to tick one off):")
        # A whole array returning at once can push the digest past the chat
        # message limit, and an over-long message is REJECTED, not truncated --
        # losing the entire digest, changes and all. Bound this section rather
        # than risk that; the full list is always in the profiles.
        lines.extend(pending_lines[:PENDING_DIGEST_MAX])
        if len(pending_lines) > PENDING_DIGEST_MAX:
            lines.append("  ... and {} more, see sensor-log/deployments/".format(
                len(pending_lines) - PENDING_DIGEST_MAX))
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
        # "Offline" is the default reason and repeating it for every unit is
        # noise; annotate only the distinctive ones (Retired, Shelf, Lab) so
        # they still stand out in the list.
        def _label(unit):
            reason = expected[unit]
            if reason.strip().lower() == "offline":
                return unit
            return "{} ({})".format(unit, reason)
        lines.append("")
        lines.append("expected offline: " + ", ".join(_label(u) for u in known))
    return "\n".join(lines)


# The snapshot clone is only ever published to this branch.
BRANCH = "main"

# Pasted by a human; both commands carry -C so it works from any directory.
RECOVER = ("git -C {repo} pull --rebase origin main"
           " && git -C {repo} push origin HEAD:main")


def _git(repo, *args, timeout=60):
    """Run git in `repo` with no stdin and no credential prompt (cron)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(("git", "-C", repo) + args, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, timeout=timeout, env=env)


# What an in-progress operation leaves in .git, what to call it, and how a
# human backs out of it. Checked in order: `git am` also uses rebase-apply/.
IN_PROGRESS = [
    ("rebase-apply/applying", "a `git am`", "am --abort"),
    ("rebase-merge", "a rebase", "rebase --abort"),
    ("rebase-apply", "a rebase", "rebase --abort"),
    ("MERGE_HEAD", "a merge", "merge --abort"),
    ("CHERRY_PICK_HEAD", "a cherry-pick", "cherry-pick --abort"),
    ("REVERT_HEAD", "a revert", "revert --abort"),
    ("sequencer", "a cherry-pick/revert sequence", "cherry-pick --quit"),
    ("BISECT_START", "a bisect", "bisect reset"),
]


def clone_blocker(repo):
    """(reason, fix steps) if the clone must not be written, committed or
    pushed; None if it is safe.

    A run into a half-finished operation used to commit inside it (a revert
    was even pushed); a run on another branch or a detached HEAD pushed the
    wrong history. All of these need a human, and each has its own way out.
    """
    def git_step(*args):
        return "git -C {} {}".format(repo, " ".join(args))

    try:
        git_dir = _git(repo, "rev-parse", "--absolute-git-dir")
        if git_dir.returncode != 0:
            return ("not a usable git clone ({})".format(
                _brief(git_dir.stdout)), [])
        git_dir = git_dir.stdout.strip()
        for marker, name, abort in IN_PROGRESS:
            if os.path.exists(os.path.join(git_dir, marker)):
                return ("in the middle of {}".format(name),
                        [git_step(abort), git_step("checkout", BRANCH)])
        unmerged = _git(repo, "diff", "--name-only", "--diff-filter=U")
        if unmerged.stdout.strip():
            return ("holding unresolved conflicts ({})".format(
                        ", ".join(unmerged.stdout.split())),
                    [git_step("reset", "--merge"),
                     git_step("checkout", BRANCH)])
        branch = _git(repo, "symbolic-ref", "-q", "--short", "HEAD")
        if branch.stdout.strip() != BRANCH:
            where = branch.stdout.strip() or "a detached HEAD"
            return ("not on {} (on {})".format(BRANCH, where),
                    [git_step("checkout", BRANCH)])
    except (OSError, subprocess.SubprocessError) as error:
        return "not inspectable ({})".format(error), []
    return None


def _brief(output):
    """The meaningful line(s) of git output, for a chat bullet."""
    noise = ("hint:", "From ", " * branch", "To ")
    lines = [line.strip() for line in (output or "").splitlines()
             if line.strip() and not line.startswith(noise)
             and "FETCH_HEAD" not in line]
    return " / ".join(lines[-2:]) or "(no output)"


def pull_snapshot(repo):
    """Fetch origin/main and fast-forward to it. Returns (ok, detail, fetched).

    `--ff-only`, deliberately, not `--rebase` (decided 2026-10-31): nothing
    watches this clone, so history is never rewritten unattended. A refusal
    means local commits are stacked up from an earlier failed push, which a
    human resolves with RECOVER. Fetch and merge are separate so the caller
    knows whether origin/main is current (a refusal) or stale (no network).
    """
    try:
        fetch = _git(repo, "fetch", "origin", BRANCH, timeout=120)
        if fetch.returncode != 0:
            return False, "fetch failed: " + _brief(fetch.stdout), False
        merge = _git(repo, "merge", "--ff-only", "origin/" + BRANCH)
        if merge.returncode != 0:
            return False, "not a fast-forward: " + _brief(merge.stdout), True
        return True, _brief(merge.stdout), True
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error), False


def snapshot_uncommitted(repo, snapshot_rel):
    """True if the snapshot differs from HEAD (or is new).

    Committing is decided by this, not by the day's diff: a failed commit, or a
    hand run without --commit, leaves the new state in the working tree, and
    the next run then sees no change against it and would never commit it.
    """
    status = _git(repo, "status", "--porcelain", "--", snapshot_rel)
    return status.returncode == 0 and bool(status.stdout.strip())


def snapshot_untracked(repo, snapshot_rel):
    """Why git would never publish the snapshot file, or None.

    A gitignored or skip-worktree snapshot reads as clean to `git status`, so
    it would be written every day and never committed, with exit 0.
    """
    listed = _git(repo, "ls-files", "-v", "--", snapshot_rel)
    if listed.returncode != 0 or not listed.stdout.strip():
        return "the snapshot is not tracked by git (gitignored?)"
    if listed.stdout.startswith("S"):
        return "the snapshot is marked skip-worktree, so git ignores changes"
    return None


def commit_summary(repo, snapshot_rel):
    """Commit message naming the units actually in the uncommitted change.

    Today's diff is not enough: a change left in the file by an earlier run is
    not a change against that file.
    """
    if _git(repo, "cat-file", "-e", "HEAD:" + snapshot_rel).returncode != 0:
        return "state: fleet baseline"
    patch = _git(repo, "diff", "-U0", "HEAD", "--", snapshot_rel)
    units = set()
    for line in patch.stdout.splitlines():
        if line[:1] in "+-" and line[:3] not in ("+++", "---"):
            unit = line[1:].split(",", 1)[0]
            if UNIT_ROW.match(unit):
                units.add(unit)
    if not units:
        return "state: snapshot left uncommitted by an earlier run"
    return "state: {}".format(", ".join(sorted(units)))


def published_snapshot(repo, snapshot_rel):
    """The snapshot as origin/main has it (GitHub), or {}."""
    shown = _git(repo, "show", "origin/{}:{}".format(BRANCH, snapshot_rel))
    if shown.returncode != 0:
        return {}
    return parse_snapshot(io.StringIO(shown.stdout))


def unpushed(repo):
    """Commits on HEAD that origin/main (as last fetched) lacks, or None."""
    try:
        count = _git(repo, "rev-list", "--count",
                     "origin/{}..HEAD".format(BRANCH))
        return int(count.stdout.strip()) if count.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def push_snapshot(repo):
    """Push every local commit origin/main lacks. Returns (ok, detail).

    Run whether or not today committed anything: after a transient push
    failure, a quiet day wrote nothing and so pushed nothing, leaving the
    commit stranded with exit 0 and no banner.
    """
    try:
        ahead = unpushed(repo)
        if ahead == 0:
            return True, "nothing to push"
        push = _git(repo, "push", "origin", "HEAD:" + BRANCH, timeout=120)
        if push.returncode != 0:
            return False, _brief(push.stdout) or "git push failed"
        return True, ("pushed" if ahead is None
                      else "pushed {} commit(s)".format(ahead))
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)


def commit_snapshot(repo, snapshot_rel, message, push=True):
    """Commit the snapshot (and only it) and, if `push`, push.

    Returns (ok, detail). With push=True, ok is False both when the commit
    failed and when it succeeded but the push did not -- callers that must
    tell those apart pass push=False and call push_snapshot() themselves.
    """
    try:
        add = _git(repo, "add", "--", snapshot_rel)
        if add.returncode != 0:
            return False, _brief(add.stdout)
        # Path-scoped: anything else already staged in the clone stays out.
        commit = _git(repo, "commit", "-q", "-m", message, "--", snapshot_rel)
        if commit.returncode != 0:
            return False, _brief(commit.stdout)
        if not push:
            return True, "committed locally; NOT pushed"
        ok, detail = push_snapshot(repo)
        return ok, ("committed and " + detail) if ok else detail
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)


def publish_banner(repo, problems, blocker, fetched, ahead, commit_failed):
    """The text put in front of the digest when publishing went wrong.

    Every command a human is told to run is on its own `$ ` line, and each one
    has been run by the tests in the state it is printed for.
    """
    recover = RECOVER.format(repo=repo)
    if blocker or ahead != 0:
        lines = ["*** THE FLEET RECORD IS NOT BEING PUBLISHED ***"]
    else:
        lines = ["*** COULD NOT REFRESH THE SENSOR-LOG CLONE ***"]
    lines += ["  - {}".format(p) for p in problems]
    if ahead is None and not blocker:
        lines.append("  Local commits not on GitHub: unknown (no origin/{} "
                     "ref)".format(BRANCH))
    elif ahead:
        lines.append("  Local commits not on GitHub: {}{}".format(
            ahead, "" if fetched else " (as of the last successful fetch)"))
    if blocker:
        reason, steps = blocker
        lines.append("  Fix by hand, as monitor, in this order:")
        lines += ["    $ {}".format(step) for step in steps + [recover]]
        return "\n".join(lines) + "\n\n"
    if commit_failed:
        lines.append("  The commit failed; the next run retries it.")
        lock = os.path.join(repo, ".git", "index.lock")
        if os.path.exists(lock):
            lines.append("  A stale lock blocks git. If no git command is "
                         "running, as monitor:")
            lines.append("    $ rm {}".format(lock))
    if ahead != 0:
        if not fetched:
            lines.append("  GitHub was unreachable; the next run retries on "
                         "its own. If it keeps failing:")
        lines.append("  Recover (as monitor): {}".format(recover))
        lines.append("  If that stops on a conflict: git -C {} rebase --abort,"
                     " then resolve by hand.".format(repo))
    else:
        lines.append("  Usually transient: the next run retries on its own. "
                     "If it repeats, check monitor can reach GitHub:")
        lines.append("    $ git -C {} fetch origin".format(repo))
    return "\n".join(lines) + "\n\n"


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
                        help="fast-forward the clone from origin/main, commit "
                             "the snapshot when it changed, and push. Never "
                             "rebases: if the pull is refused the day is "
                             "committed locally only, the digest says so, and "
                             "the exit status is 1 (requires --repo)")
    parser.add_argument("--notify", action="store_true",
                        help="post the digest to chat")
    parser.add_argument("--channel", default="status",
                        help="chat channel for --notify (default: status)")
    parser.add_argument("--key-file", default="/home/pi/.googlechat",
                        help="notification key file for --notify")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the snapshot and digest; write, commit and "
                             "send nothing")
    parser.add_argument("--field", choices=FIELDS[1:], metavar="FIELD",
                        help="report one field's value across the snapshot, "
                             "grouped by value (a fleet consistency check); "
                             "reads the snapshot only, does NOT probe. "
                             "Choices: " + ", ".join(FIELDS[1:]))
    parser.add_argument("--ags-path",
                        help="directory containing ags.py (for the threshold/gain "
                             "parser). Overrides the search path; also settable "
                             "via $FLEET_PROBE_AGS_PATH")
    parser.add_argument("--allow-missing-ags", action="store_true",
                        help="probe even if ags.py cannot be found. Thresholds "
                             "and gains will read 'unknown' for every unit -- "
                             "expect a spurious change for each. Off by default "
                             "so a missing parser fails loudly instead.")
    args = parser.parse_args()

    # Commit and notify are opt-in rather than default-on, so a hand-run probe
    # cannot surprise anyone by pushing or paging. The cron line asks for them.
    if args.commit and not args.repo:
        parser.error("--commit requires --repo")
    snapshot_rel = os.path.join("state", "fleet-state.csv")
    snapshot = (os.path.join(args.repo, snapshot_rel) if args.repo
                else args.snapshot)

    # --field is a read-only report over the existing snapshot: no probing, no
    # fleet contact, no write. It short-circuits before anything reaches out.
    if args.field:
        rows = read_snapshot(snapshot)
        if not rows:
            print("no snapshot to report on at {}".format(snapshot),
                  file=sys.stderr)
            return 1
        if args.array:                       # narrow to one array if asked
            wanted = {"mjolnir{:02d}".format(n) for n in ARRAYS[args.array]}
            rows = {u: r for u, r in rows.items() if u in wanted}
            if not rows:
                print("snapshot has no {} units".format(args.array),
                      file=sys.stderr)
                return 1
        print(field_report(rows, args.field))
        return 0

    # Probing needs the AGS parser. Refuse rather than silently emit "unknown"
    # thresholds/gains, which would also fire a bogus change for every unit.
    # (--field above does not probe, so it is deliberately exempt.)
    global parse_startup_state, AGS_SOURCE
    if args.ags_path or parse_startup_state is None:
        found, source = load_ags_parser(args.ags_path)
        if found is not None:
            parse_startup_state, AGS_SOURCE = found, source
    if parse_startup_state is None and not args.allow_missing_ags:
        print("ERROR: cannot find ags.py, so threshold/gain cannot be read.\n"
              "       Probing now would record 'unknown' for those 4 fields on\n"
              "       every unit and register a spurious change for each.\n"
              "       Pass --ags-path DIR, set $FLEET_PROBE_AGS_PATH, or use\n"
              "       --allow-missing-ags to override deliberately.",
              file=sys.stderr)
        return 1

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
    # Refresh the clone BEFORE reading the baseline, so the diff is against
    # what is actually published. After probing rather than before, to keep the
    # window between pull and push short -- the sweep is serial and can take
    # ~27 minutes. A failure here is recorded, not raised: the digest still has
    # to go out, because a wedged clone and a dead probe must not look alike.
    repo_problems = []
    blocker, pulled, fetched = None, False, False
    if args.commit and not args.dry_run:
        blocker = clone_blocker(args.repo)
        if blocker:
            repo_problems.append("the sensor-log clone is {}; nothing was "
                                 "written or committed".format(blocker[0]))
        else:
            pulled, detail, fetched = pull_snapshot(args.repo)
            print("pull: {}".format(detail), file=sys.stderr)
            if not pulled:
                repo_problems.append(
                    "could not refresh the sensor-log clone: {}".format(detail))

    # A blocked clone's working file is not the record (it may hold conflict
    # markers or another branch's state), so diff against what GitHub has.
    previous = (published_snapshot(args.repo, snapshot_rel) if blocker
                else read_snapshot(snapshot))
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
    #
    # When the pull was refused, still write and commit -- locally only. The
    # probe is the only writer of state/, so its own last commit is the right
    # baseline; skipping the write would drop the day from history. The stacked
    # commits publish when a human runs RECOVER. A blocked clone (mid-rebase,
    # off main) is not touched at all.
    status = 1 if repo_problems else 0
    if not blocker and (baseline or changes):
        directory = os.path.dirname(snapshot)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(snapshot, "w") as handle:
            handle.write(text)

    ahead, commit_failed = None, False
    if args.commit and not blocker:
        if snapshot_uncommitted(args.repo, snapshot_rel):
            summary = commit_summary(args.repo, snapshot_rel)
            ok, detail = commit_snapshot(args.repo, snapshot_rel, summary,
                                         push=False)
            print("commit: {}".format(detail), file=sys.stderr)
            if not ok:
                repo_problems.append(
                    "snapshot commit FAILED: {}".format(detail))
                commit_failed = True
                status = 1
        if os.path.exists(snapshot):
            untracked = snapshot_untracked(args.repo, snapshot_rel)
            if untracked:
                repo_problems.append(untracked)
                status = 1
        if pulled:
            ok, detail = push_snapshot(args.repo)
            print("push: {}".format(detail), file=sys.stderr)
            if not ok:
                repo_problems.append(
                    "snapshot push FAILED: {}".format(detail))
                status = 1
        ahead = unpushed(args.repo)

    for problem in repo_problems:
        print("PUBLISH PROBLEM: {}".format(problem), file=sys.stderr)

    # Notify even when nothing changed -- the digest IS the liveness signal. A
    # silent probe and a dead probe must not look the same.
    if args.notify:
        outgoing = report
        if repo_problems:
            # Say it where humans actually read, and first. This failure was
            # silent for a month because it went only to a cron log while the
            # digest looked completely normal.
            outgoing = publish_banner(args.repo, repo_problems, blocker,
                                      fetched, ahead, commit_failed) + report
        try:
            send_digest(outgoing, args.key_file, args.channel)
            print("digest sent to '{}'".format(args.channel), file=sys.stderr)
        except Exception as error:            # noqa: BLE001 - report, don't mask
            print("digest FAILED to send: {}: {}".format(
                type(error).__name__, error), file=sys.stderr)
            status = 1
    # Cron's log drops $?, so say how the run ended.
    print("exit status {}".format(status), file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
