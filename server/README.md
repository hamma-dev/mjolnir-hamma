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

Measures what each unit actually *is* — front-end power, brokkr mode, AGS
threshold and gain, and the five repo SHAs — and writes it to
`state/fleet-state.csv` in a sensor-log clone **only when it changed**. A stable
fleet therefore produces no commits, and `git log` on that file is the list of
every configuration change the fleet has had.

It must run as a user who can write the clone. On the VPS that is `monitor`:
`/home/monitor/sensor-log` is `monitor`-owned and is not writable by `pi`.

```bash
# dry run first -- probes, prints the snapshot and digest, writes nothing
fleet_probe --repo /home/monitor/sensor-log --dry-run

# the scheduled form: one line in monitor's crontab
10 6 * * * cd /home/monitor/dev/mjolnir-hamma && /home/monitor/dev/ltgenv/bin/python server/fleet_probe.py --repo /home/monitor/sensor-log --commit --notify >> /home/monitor/fleet_probe.log 2>&1
```

**Use the venv interpreter, not `/usr/bin/python3`.** `--notify` imports
`notifiers.google_chat`, which is installed in `/home/monitor/dev/ltgenv` and
not in the system python. With the wrong interpreter the probe still measures,
commits and pushes correctly and only the digest fails, with
`digest FAILED to send: ModuleNotFoundError: No module named 'notifiers'` --
a partial success that is easy to miss in a cron log. Verified on the VPS
2026-09-27.

The digest reads its key from `/home/pi/.googlechat` by default, which is
world-readable, so `monitor` can send without a copy of its own.

`--commit` and `--notify` are opt-in so a hand-run probe cannot surprise anyone
by pushing or paging; the cron line asks for them.

**Why it needs a schedule.** Nothing else records a deliberate power change. An
operator powering a front end down leaves no trace in the field log — that is
HAM-189, and it has cost real time twice: mj43 sat misrecorded for 26 days, and
mj02's 2026-09-23 shutdown was found two days later only by noticing that
`noise_diag` had stopped. The probe catches those by measurement, regardless of
how the change was made, which no hook on `mjol_array` can do.

`sensor-log`'s `build_site.py` renders `front_end`, `brokkr_mode` and the
snapshot's age on log.hamma.dev. If the probe stops running the site says so
rather than presenting stale measurements as current.
