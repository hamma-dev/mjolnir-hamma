"""Validate the periodic-scrub systemd artifacts (§3.3) and their consistency
with state_monitor's lock file and config/main.toml's scrub_command.

These are static/structural checks -- the units aren't run here, but drift
between the timer's command and what state_monitor spawns (different lock,
different args) would silently break the shared-lock / shared-heartbeat model,
so we pin those invariants.
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
FILES = REPO / "files"


def _read(name):
    return (FILES / name).read_text()


class TestScrubWrapper:
    def test_bash_syntax_valid(self):
        rc = subprocess.run(
            ["bash", "-n", str(FILES / "hamma-scrub.sh")]).returncode
        assert rc == 0

    def test_uses_nonblocking_flock_on_shared_lock(self):
        s = _read("hamma-scrub.sh")
        assert "flock -n" in s               # non-blocking: skip, don't queue
        assert "/tmp/hamma_scrub.lock" in s  # the shared lock

    def test_runs_scrub_with_recover_purge_since_auto(self):
        s = _read("hamma-scrub.sh")
        assert "hamma_scrub.py" in s
        for flag in ("--recover", "--purge", "--since auto"):
            assert flag in s


class TestServiceUnit:
    def test_oneshot_as_pi_running_the_wrapper(self):
        s = _read("hamma-scrub.service")
        assert "Type=oneshot" in s
        assert "User=pi" in s
        assert "ExecStart=/usr/local/bin/hamma-scrub.sh" in s

    def test_ordered_after_brokkr_but_does_not_want_it(self):
        s = _read("hamma-scrub.service")
        assert "After=brokkr-hamma-default.service" in s
        # Wants= would resurrect a deliberately-stopped brokkr every 15 min.
        assert "Wants=brokkr" not in s

    def test_start_limiting_disabled(self):
        # §3.2's periodic SIGKILLs must not be able to trip systemd's start
        # limit and silently disable the timer backstop.
        assert "StartLimitIntervalSec=0" in _read("hamma-scrub.service")

    def test_no_misleading_io_priority_knob(self):
        # ionice on the mj-pi governs nothing on the AGS Pi (separate host, SSH)
        # and mq-deadline ignores it anyway -- don't imply a mitigation that
        # does nothing.
        assert "IOSchedulingClass" not in _read("hamma-scrub.service")


class TestTimerUnit:
    def test_periodic_and_installed_to_timers_target(self):
        s = _read("hamma-scrub.timer")
        assert "OnUnitActiveSec=" in s
        assert "WantedBy=timers.target" in s


class TestConsistencyWithMonitorAndConfig:
    def test_lock_matches_state_monitor(self):
        sm = (REPO / "plugins" / "state_monitor.py").read_text()
        m = re.search(r'SCRUB_LOCK_FILE\s*=\s*"([^"]+)"', sm)
        assert m, "SCRUB_LOCK_FILE not found in state_monitor.py"
        assert m.group(1) in _read("hamma-scrub.sh")

    def test_scrub_args_match_main_toml(self):
        toml = (REPO / "config" / "main.toml").read_text()
        m = re.search(r'scrub_command\s*=\s*"([^"]+)"', toml)
        assert m, "scrub_command not found in main.toml"
        wrapper = _read("hamma-scrub.sh")
        for token in ("hamma_scrub.py", "--recover", "--purge", "--since auto"):
            assert token in m.group(1), "main.toml scrub_command missing " + token
            assert token in wrapper, "wrapper missing " + token


class TestInstallWiring:
    def test_brokkr_install_copies_and_enables_timer(self):
        s = (REPO / "unified_install" / "lib" / "brokkr.sh").read_text()
        for f in ("hamma-scrub.sh", "hamma-scrub.service", "hamma-scrub.timer"):
            assert f in s, "brokkr.sh does not install " + f
        assert re.search(r"enable\b.*hamma-scrub\.timer", s), \
            "brokkr.sh does not enable the timer"
