# Mjolnir-Hamma Changelog


## Version 0.5.0 (2026-11-01)

35 PRs merged to `0.4.x` since 0.4.0. Changes are grouped by area, most important first; dates are when each landed on `0.4.x`.

**Noise diagnostics**
* **Slow-channel noise tracked alongside fast** — `noise_diag` adds `slow_offset`, `slow_noise`, `slow_vpp`, `slow_snr`; existing CSVs are upgraded in place. (2026-10-30 #110)
* **noise_diag CSVs pushed to the VPS** on a dedicated, jittered, skip-if-unchanged rsync pipeline (`hamma.dev:/home/pi/noise_diag`). (2026-10-30 #113)
* NoiseDiag uses the public `hamma.diagnostic_data`. (2026-09-03 #67)
* Noise alert uses a time-based 30-min sustain to stop false alarms. (2026-07-08 #75)
* Website: fast-channel noise floor + DC offset panel and gauges; gauges report NA when noise data is missing or stale. (2026-07-02 #68, 2026-10-30 #116)

**Alerting and data-loss detection (state_monitor)**
* **No page for a sensor that is deliberately off** (science ingest disabled). (2026-10-29 #102, HAM-114)
* Alert when the relay and brokkr's mode disagree. (2026-08-12 #87, HAM-182)
* Alert when brokkr's drive target disagrees with the labelled partitions (silent science-data loss). (2026-08-12 #88)
* Capacity and drive alert wording: split by urgency, drop overclaims. (2026-08-14 #95, #96)

**Fleet operations**
* **Daily fleet configuration probe** (`server/fleet_probe.py`): fleet-consistency digest, collapsed reachability, pending actions surfaced the day a unit returns, refresh-before-publish with a banner on failure, install-mode and release-drift columns, installable with scheduling docs. (2026-09-03 #97; #99, #104, #107, #109, #114, #106 through 2026-11-01; HAM-189, HAM-205)
* **Server-side threshold & gain control** (`mjol_array` + `ags.py`). (2026-07-08 #70)
* `sensors.py`: `nochargecontroller` stays set across `--on/--off`. (2026-10-30 #101, HAM-184)
* VPS `array_status` plugin tracked in the repo; bounded ssh calls that wedged it; `mjol_array` status no longer needs numpy. (2026-09-01 #94, 2026-08-14 #92, 2026-07-09 #77)
* Website: PAMMA and AUMMA overview pages, array links in the top nav. (2026-11-01 #117)

**Disk, logs and scrub**
* **Bound `/var/log`** so log spam cannot fill the SD card; pin persistent journald storage and flag a RAM-only journal; `verify_deployment.sh` reports on the bounds. (2026-07-31 #73, 2026-10-30 #115, #105; HAM-113, HAM-216, HAM-181)
* Scrub resilience: silent scrub failure made visible and self-healing; `--audit-loss` reconciliation gate. (2026-07-15 #82, 2026-10-04 #103, HAM-164)
* `verify_deployment.sh` detects the AGS writing to SD instead of the data stick. (2026-09-10 #100, HAM-166)

**Install and config**
* Pin `gpiozero<2.0` on Python 3.7. (2026-08-06 #83, HAM-84)
* Bootstrap guards the paths passed to `rm -rf` / `find -delete`. (2026-08-12 #90)
* `daily_compression` disabled by default; compression docs no longer quote a fixed ratio. (2026-07-01 #71, 2026-10-30 #111)
* Docs: fleet deployment plan for threshold/gain control. (2026-08-07 #76)

**Deploy note:** requires the `hamma` package **0.3.0** (unchanged from 0.4.0; provides `diagnostic_data`), brokkr ≥ 0.4.0, sindri 0.3.x. Restart `brokkr-hamma-default` after updating; the first record after a restart gets no noise_diag row.


## Version 0.4.0 (2026-06-25)

First tagged release since 0.3.0, consolidating a large backlog of sensor-software, tooling, and reliability work. Changes are ordered by importance; dates are when each landed on `0.3.x`.

* **DNS fix for cellular sensors** — remove bogus eth0/eth1 DNS and deploy the corrected `40-eth0.network` at install; cellular units could not resolve DNS without it. (2026-03-25 #40, 2026-03-31 #44)
* **Real-time noise-diagnostic plugin** (`NoiseDiag`) — throttled fast-channel noise-floor sampling on the `realtime` pipeline, rolling CSV, edge-triggered Google Chat alerts, and a pre-trigger (under 50 ms) skip guard. (2026-06-24 #64)
* **AGS data scrubber** — scan, recover, and purge corrupted / bad-GPS trigger data, with operational deployment. (2026-04-17 #49, 2026-04-24 #50)
* **Daily compression plugin** for HAMMA trigger data (`.hmc`), quiet-hours gated, originals preserved. (2026-03-31 #43)
* **Single-command sensor power control** (`sensors.py`) with on/off/failure notifications. (2026-05-11 #55, 2026-06-24 #60)
* **`hamma_download` tool** plus a dedicated `datasync` user for rsync-based data pulls. (2026-03-31 #45)
* **On-demand sensor noise check** (`scripts/hamma_noise.py`) — per-Pi noise report versus the AGS threshold. (2026-06-25 #52)
* **`nochargecontroller` mode** via a new NullInput plugin, for units without a charge controller. (2026-04-13 #48)
* **Server-side scripts brought into the repo** (`mjol_array.py`, `webgen.py`) plus an `install.sh` to put them on PATH. (2026-05-12 #56, 2026-05-25 #58)
* **Trigger sensors from the VPS** — `mjol_array.py --trigger` over the autossh tunnel. (2026-06-24 #63)
* **state_monitor edge-check fix** — use `>=` on the previous value so threshold-boundary drops correctly fire. (2026-06-24 #61)
* **`mjol_array` import fix** — defer pandas/numpy so control commands work without them; surface remote `sensors.py` output. (2026-05-25 #57)
* **Google Chat notifier**, later refactored into a shared `notifiers.Notifier` transport. (2022-07-07 #23, 2026-06-24 #64)
* **Cellular / WWAN connectivity** — install, settings, "patient" ping, and connectivity fixes. (2022-11 to 2025-06: #26, #28, #31, #32)

**Deploy note:** requires the `hamma` package **0.3.0** (`python_requires >=3.6`; resolves HAM-96). Install hamma from the `0.3.0` tag. Verified against brokkr 0.4.0 and sindri 0.3.1.dev0.


## Version 0.1.0a3 (2020-03-20)

Test deployment release with the following changes:

* Comprehensively overhaul main config format and add presets for pipeline arch
* Add message of the day to files
* Make numerous fixes, improvements and cleanup for preliminary test deploy


## Version 0.1.0a2 (2020-03-09)

Development release with the following changes:

* Greatly simplify configuration structure to fewer and simpler files
* Support more units and a per-system config path



## Version 0.1.0a1 (2020-03-07)

Initial development release:

* Full set of Brokkr configuration files
* Set of files to install on client device w/o paths
* System metadata with full set of information
* Basic Readme, gitignore and structure
