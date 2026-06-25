# Mjolnir-Hamma Changelog


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
