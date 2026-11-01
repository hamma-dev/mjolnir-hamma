# server/

Scripts that run on the HAMMA VPS (`hamma.dev`), not on the sensor Pis.

| Script | Purpose |
| ------ | ------- |
| `mjol_array.py` | Drive sensor power on/off across an array by SSHing into each Pi and running `sensors.py`. Also reports per-Pi status. |
| `webgen.py` | Regenerate the per-array status HTML pages served at `hamma.dev`. |
| `fleet_probe.py` | Measure each unit's configuration daily, write `state/fleet-state.csv` in a sensor-log clone when it changed, and post a digest. |
| `install.sh` | Symlink the above onto `PATH` (default `/usr/local/bin`). Idempotent. |

## Install

```bash
cd ~/dev/mjolnir-hamma
bash server/install.sh
```

After install, `mjol_array`, `webgen` and `fleet_probe` resolve from anywhere on `PATH`.

## Usage

End-user guide (commands, examples, troubleshooting):
**[mjol_array — Sensor On/Off from the VPS](https://hsvltg.atlassian.net/wiki/spaces/HAMMA/pages/497221633)** on Confluence.

For what `sensors.py` does on each Pi when `mjol_array` calls it:
**[sensors.py — Sensor Power Control](https://hsvltg.atlassian.net/wiki/spaces/HAMMA/pages/489914369)**.


## fleet_probe — the daily configuration sweep

Measures what each unit actually *is* and writes it to `state/fleet-state.csv`
in a sensor-log clone **only when it changed**. `git log` on that file is
therefore the list of every configuration change the fleet has had, and
log.hamma.dev renders it. Per unit it records front-end power, brokkr mode, AGS
threshold and gain, the five repo SHAs, and how each of brokkr, sindri, hamma
and notifiers is imported (`*_install`, below). The sweep is serial; for the
fleet today it takes under a minute.

It detects deliberate changes (`mjol_array --up/--down`, `sensors.py`) within a
day, by measurement, however they were made. It does not record *why*: put
that in a sensor-log issue, as before (HAM-189).

### Running it

On the VPS it runs from `monitor`'s crontab, by full path with the venv
interpreter (`fleet_probe` is not installed on `PATH` there):

```bash
10 6 * * * /home/monitor/dev/ltgenv/bin/python /home/monitor/dev/mjolnir-hamma/server/fleet_probe.py --repo /home/monitor/sensor-log --commit --notify >> /home/monitor/fleet_probe.log 2>&1
```

To try it by hand, as `monitor`, start with a dry run: it probes and prints the
snapshot and digest, and writes, commits and sends nothing.

```bash
/home/monitor/dev/ltgenv/bin/python /home/monitor/dev/mjolnir-hamma/server/fleet_probe.py --repo /home/monitor/sensor-log --dry-run
```

`-a hamma` or `-p 2 3` probes a subset; units it skips are left exactly as
they were. `--commit` and `--notify` are opt-in so a hand run cannot push or
page anyone by surprise.

`bash server/install.sh` also links `fleet_probe` onto `PATH`, but the default
target `/usr/local/bin` needs sudo; the cron line above does not depend on it.

### Prerequisites (a new host, or a rebuilt one)

- **Run it as the user that owns the clone.** On the VPS that is `monitor`:
  `/home/monitor/sensor-log` is `monitor`-owned and `pi` can neither write it
  nor run git in it.
- **The clone tracks `origin/main`** and has a git identity in its own config
  (`hamma fleet probe <noreply@hamma.dev>` on the VPS).
- **It can push without a prompt.** The VPS clone's remote is the ssh alias
  `github-sensor-log:hamma-dev/sensor-log.git`, which needs a deploy key with
  write access in that user's `~/.ssh`.
- **It can reach every unit without a prompt**, through its autossh tunnel
  (`ssh -p 100NN pi@localhost`, `BatchMode=yes`).
- **`--notify` needs `notifiers` in the interpreter that runs the probe.** It is
  in `/home/monitor/dev/ltgenv`, not in the system `python3`. With the wrong
  interpreter the probe still measures, commits and pushes, and only the digest
  fails (`digest FAILED to send: ModuleNotFoundError: No module named
  'notifiers'`). That partial success is easy to miss in a cron log.
- The digest key is read from `/home/pi/.googlechat` by default, which is
  world-readable, so `monitor` needs no copy of its own.
- **Before enabling cron on a new host, do a dry run and check that units
  answer.** A first `--commit` run that reaches nothing records every unit as
  `unreachable` as the baseline.

### When it cannot publish

The probe only ever **fast-forwards** the clone. It never rebases or merges on
its own, and never touches a clone that needs a person. When something is wrong
the digest **starts with a banner**, and each command it prints is on its own
`$` line, to be run as `monitor`:

- `*** THE FLEET RECORD IS NOT BEING PUBLISHED ***`: the day's snapshot is
  committed locally but not on GitHub, or the clone is blocked (mid-rebase,
  mid-merge, off `main`, an untracked file where GitHub adds one). Run the
  printed steps in order; the last one is the recovery:

  ```bash
  git -C /home/monitor/sensor-log pull --rebase origin main && git -C /home/monitor/sensor-log push origin HEAD:main
  ```

  If that stops on a conflict, back out with
  `git -C /home/monitor/sensor-log rebase --abort` and resolve by hand.
- `*** COULD NOT REFRESH THE SENSOR-LOG CLONE ***`: GitHub was unreachable but
  nothing is unpublished. Usually transient; the next run retries on its own.
- `*** FLEET RECORD PROBLEM ***`: something else, e.g. an unreadable snapshot
  file, which is rewritten from that day's probe.

Every run also writes `PUBLISH PROBLEM: …` lines and a final
`exit status N` to `/home/monitor/fleet_probe.log`, so the outcome is in the
log even when the digest could not be sent.

### What the digest shows

- Changes since the last snapshot, collapsed: a power flip is one `front_end`
  line, not the threshold/gain cascade it causes.
- **not capturing**: units whose front end is off.
- **install problems**: units where a package reads `broken`, `missing`,
  `unknown` or `timeout` today.
- **not at `0.4.x` head**: units not on the release branch head, grouped by
  branch, commit and dirty state, with a total. If GitHub cannot be read for
  the head, the section says so.
- Pending items from each probed unit's sensor-log profile.

### The `*_install` columns

How each package is imported, by the interpreter that runs it:

| Value | Meaning |
| --- | --- |
| `editable` | Imported from the git checkout; the SHA column is what runs. |
| `installed` | A `site-packages` copy; the SHA column is **not** what runs. |
| `broken` | Present but raises on import. |
| `missing` | Not importable. |
| `timeout` | The import did not finish within 10 s. |
| `unknown` | Any other failure, e.g. a missing interpreter. |

A unit not measured yet is blank. `timeout` on a unit that has a real value is
not stored as a change (it is still listed under install problems that day);
`unknown` is, because a lost interpreter is a real change.

The first run after these columns were added commits once as
`state: schema +brokkr_install,…`; that is the only commit a stable fleet makes
other than real changes.

### Caveats

- log.hamma.dev shows the snapshot's age, but a stable fleet legitimately goes
  weeks without a commit, so an old snapshot does **not** mean the probe has
  stopped. Check `exit status` in `/home/monitor/fleet_probe.log` instead.
- `/home/monitor/fleet_probe.log` is appended to and not rotated (about 60 KB
  from mid-August to November 2026).
