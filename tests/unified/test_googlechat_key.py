"""
Tests for the HAM-118 Google Chat key-file check: check_googlechat_key() in
verify_deployment.sh, backed by scripts/verify_googlechat_channels.py.

The channel/method/key_file resolution and the key-file parsing are both
delegated to real parsers -- brokkr's own config renderer
(brokkr.config.main.CONFIG) and Python's configparser, matching exactly
how notifiers reads the file (configparser.ConfigParser(interpolation=
None), then config['channel'][channel]) -- rather than re-deriving either
in bash. A first version used a hand-written awk section/key scan and got
the file's own shape wrong (channel names are options under a single
[channel] table, not [section] headers), which FAILed every channel on
every unit.

These tests drive the real check_googlechat_key() (sourced with its
trailing `main "$@"` stripped, so sourcing never runs main()) against:
  - a "python_stub" standing in for /home/pi/dev/ltgenv/bin/python, which
    just adds a fake brokkr package to PYTHONPATH and execs the real
    python3 -- so configparser and the step-selection logic in
    verify_googlechat_channels.py run for real;
  - a "systemctl_stub" standing in for systemd, so mode detection
    (ExecStart --mode / Environment=BROKKR_MODE) can be driven per test;
  - two kinds of fake brokkr.config.main.CONFIG: a plain dict (for
    channel-matching/parsing cases, where the rendered result is what
    matters) and a TOML-backed one that actually parses system/local/mode
    TOML files with tomllib (for cases that exercise real TOML syntax and
    brokkr's system-then-local-then-mode-overlay precedence).
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_deployment.sh"
HELPER_SCRIPT = REPO_ROOT / "scripts" / "verify_googlechat_channels.py"

KEY_FILE_STATUS_ONLY = textwrap.dedent("""\
    # Webhook attributes for HAMMA Status bot

    [channel]

    # for status channel
    status = https://chat.googleapis.com/v1/spaces/AAA/messages?key=SECRET1
    # for HAMMA channel
    # hamma = https://chat.googleapis.com/v1/spaces/BBB/messages?key=SECRET2
""")


# --- Stand-ins for the sensor environment ------------------------------

def _stat_stub(tmp_path):
    """GNU `stat -c %U|%a FILE` shim; optionally overrides the reported
    owner via FAKE_STAT_OWNER so ownership-mismatch tests don't need real
    chown/root. verify_deployment.sh only ever runs on Raspbian (GNU
    coreutils); macOS ships BSD stat with no -c option."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "stat"
    stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys, os, stat as st, pwd
        args = sys.argv[1:]
        fmt, path = args[0], args[1]
        if fmt == "-c":
            fmt, path = args[1], args[2]
        s = os.stat(path)
        if fmt == "%U":
            print(os.environ.get("FAKE_STAT_OWNER")
                  or pwd.getpwuid(s.st_uid).pw_name)
        elif fmt == "%a":
            print(oct(st.S_IMODE(s.st_mode))[2:])
    """))
    stub.chmod(0o755)
    return str(bin_dir)


def _systemctl_stub(tmp_path):
    """`systemctl show <unit> --property=ExecStart|Environment` shim,
    driven entirely by FAKE_SYSTEMCTL_EXECSTART / FAKE_SYSTEMCTL_ENV."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "systemctl"
    stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys, os
        prop = None
        for a in sys.argv[1:]:
            if a.startswith("--property="):
                prop = a.split("=", 1)[1]
        if prop == "ExecStart":
            print("ExecStart=" + os.environ.get("FAKE_SYSTEMCTL_EXECSTART", ""))
        elif prop == "Environment":
            print("Environment=" + os.environ.get("FAKE_SYSTEMCTL_ENV", ""))
        else:
            print("")
    """))
    stub.chmod(0o755)
    return str(bin_dir)


def _python_stub(tmp_path, pythonpath, missing=False):
    """Stand-in for /home/pi/dev/ltgenv/bin/python: adds `pythonpath` to
    PYTHONPATH and execs the real python3, so the helper script's use of
    configparser (and the fake brokkr package) runs for real."""
    if missing:
        return str(tmp_path / "no-such-python")
    stub = tmp_path / "python_stub"
    stub.write_text(textwrap.dedent("""\
        #!/bin/bash
        export PYTHONPATH="{}"
        exec "{}" "$@"
    """).format(pythonpath, sys.executable))
    stub.chmod(0o755)
    return str(stub)


def _write_fake_brokkr_dict(tmp_path, config):
    """A fake brokkr.config.main.CONFIG that is just the given dict --
    for tests where what matters is the *rendered* result, not how TOML
    sources combine to produce it."""
    pkg_root = tmp_path / "fakebrokkr_dict"
    config_dir = pkg_root / "brokkr" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (pkg_root / "brokkr" / "__init__.py").write_text("")
    (config_dir / "__init__.py").write_text("")
    (config_dir / "main.py").write_text(
        "CONFIG = " + repr(config) + "\n")
    return str(pkg_root)


def _write_fake_brokkr_broken(tmp_path):
    """A brokkr package whose config.main raises on import, simulating a
    real import failure (missing dependency, broken venv, ...)."""
    pkg_root = tmp_path / "fakebrokkr_broken"
    config_dir = pkg_root / "brokkr" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (pkg_root / "brokkr" / "__init__.py").write_text("")
    (config_dir / "__init__.py").write_text("")
    (config_dir / "main.py").write_text(
        "raise ImportError('no module named notifiers')\n")
    return str(pkg_root)


def _write_fake_brokkr_toml(tmp_path, system_toml=None, local_toml=None,
                            mode_toml=None):
    """A fake brokkr.config.main that actually parses real TOML files with
    tomllib and merges them system -> local -> mode-overlay, same order
    and same later-wins deep-merge semantics as brokkr.config.base's
    update_dict_recursive / ConfigHandlerFactory(overlays=...). Reads
    paths from FAKE_BROKKR_SYSTEM_TOML / FAKE_BROKKR_LOCAL_TOML /
    FAKE_BROKKR_MODE_TOML and the mode name from BROKKR_MODE, exactly as
    brokkr reads BROKKR_MODE via its EnvVarsConfigLevel."""
    pkg_root = tmp_path / "fakebrokkr_toml"
    config_dir = pkg_root / "brokkr" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (pkg_root / "brokkr" / "__init__.py").write_text("")
    (config_dir / "__init__.py").write_text("")
    (config_dir / "main.py").write_text(textwrap.dedent("""\
        import os
        try:
            import tomllib as _toml
        except ImportError:
            import tomli as _toml

        def _deep_merge(base, overlay):
            result = dict(base)
            for key, value in overlay.items():
                base_value = result.get(key)
                if isinstance(base_value, dict) and isinstance(value, dict):
                    result[key] = _deep_merge(base_value, value)
                else:
                    result[key] = value
            return result

        def _load(path):
            if not path or not os.path.isfile(path):
                return {}
            with open(path, "rb") as f:
                return _toml.load(f)

        _system = _load(os.environ.get("FAKE_BROKKR_SYSTEM_TOML"))
        _local = _load(os.environ.get("FAKE_BROKKR_LOCAL_TOML"))
        _mode_data = _load(os.environ.get("FAKE_BROKKR_MODE_TOML"))
        _mode_name = os.environ.get("BROKKR_MODE") or "default"
        _overlay = _mode_data.get(_mode_name, {}).get("main", {})

        CONFIG = _deep_merge(_deep_merge(_system, _local), _overlay)
    """))
    if system_toml is not None:
        (tmp_path / "system_main.toml").write_text(system_toml)
    if local_toml is not None:
        (tmp_path / "local_main.toml").write_text(local_toml)
    if mode_toml is not None:
        (tmp_path / "mode.toml").write_text(mode_toml)
    return str(pkg_root)


def _sourceable_script(tmp_path):
    """Copy verify_deployment.sh with its trailing `main "$@"` call
    stripped, so sourcing it never runs main() against the live host."""
    script = VERIFY_SCRIPT.read_text()
    assert script.rstrip().endswith('main "$@"')
    copy = tmp_path / "verify_deployment.sh"
    copy.write_text(script.rstrip()[:-len('main "$@"')])
    return copy


def _run_check(tmp_path, pythonpath=None, python_missing=False,
               helper_missing=False, key_file=None, key_file_mode=0o644,
               fake_stat_owner=None, execstart_mode="", env_mode="",
               extra_env=None):
    sourceable = _sourceable_script(tmp_path)

    key_path = tmp_path / "googlechat"
    if key_file is not None:
        key_path.write_text(key_file)
        key_path.chmod(key_file_mode)

    env = os.environ.copy()
    env["PATH"] = "{}:{}:{}".format(
        _stat_stub(tmp_path), _systemctl_stub(tmp_path), env["PATH"])
    env["GOOGLECHAT_PYTHON"] = _python_stub(
        tmp_path, pythonpath or "", missing=python_missing)
    env["GOOGLECHAT_HELPER"] = (
        str(tmp_path / "no-such-helper.py") if helper_missing
        else str(HELPER_SCRIPT))
    env["GOOGLECHAT_KEY_FILE"] = str(key_path)
    env["FAKE_SYSTEMCTL_EXECSTART"] = execstart_mode
    env["FAKE_SYSTEMCTL_ENV"] = env_mode
    if fake_stat_owner:
        env["FAKE_STAT_OWNER"] = fake_stat_owner
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        ["bash", "-c", 'source "$1"; check_googlechat_key', "_", str(sourceable)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    return result


def _run_with_dict_config(tmp_path, config, **kwargs):
    pythonpath = _write_fake_brokkr_dict(tmp_path, config)
    kwargs.setdefault("key_file", KEY_FILE_STATUS_ONLY)
    return _run_check(tmp_path, pythonpath=pythonpath, **kwargs)


def _both_steps(tmp_path, channel="status"):
    """Both steps enabled, gchat, same channel, key_file pointed at the
    exact path _run_check() writes `key_file=` content to."""
    key_file = str(tmp_path / "googlechat")
    return {
        "steps": {
            "state_monitor": {
                "method": "gchat", "channel": channel,
                "key_file": key_file,
            },
            "noise_diag": {
                "method": "gchat", "channel": channel,
                "key_file": key_file,
            },
        }
    }


# --- Interpreter / helper availability ----------------------------------

class TestInterpreterAndHelperAvailability:
    def test_missing_interpreter_skips(self, tmp_path):
        result = _run_check(tmp_path, python_missing=True, key_file=KEY_FILE_STATUS_ONLY)
        assert "SKIP" in result.stdout
        assert "ltgenv python not found" in result.stdout
        assert "FAIL" not in result.stdout

    def test_missing_helper_skips(self, tmp_path):
        result = _run_check(
            tmp_path, pythonpath=_write_fake_brokkr_dict(tmp_path, _both_steps(tmp_path)),
            helper_missing=True, key_file=KEY_FILE_STATUS_ONLY)
        assert "SKIP" in result.stdout
        assert "verify_googlechat_channels.py not found" in result.stdout
        assert "FAIL" not in result.stdout

    def test_brokkr_import_failure_skips_not_fails(self, tmp_path):
        pythonpath = _write_fake_brokkr_broken(tmp_path)
        result = _run_check(tmp_path, pythonpath=pythonpath, key_file=KEY_FILE_STATUS_ONLY)
        assert "FAIL" not in result.stdout
        assert result.stdout.count("SKIP") == 2
        assert "brokkr import failed" in result.stdout


# --- Key file existence (tightened: must not match "non-empty") ---------

class TestKeyFileExistence:
    def _config_for(self, key_path):
        return {
            "steps": {
                "state_monitor": {"method": "gchat", "channel": "status",
                                   "key_file": str(key_path)},
                "noise_diag": {"_enabled": False},
            }
        }

    def test_missing_file_fails(self, tmp_path):
        missing_path = tmp_path / "does-not-exist"
        pythonpath = _write_fake_brokkr_dict(tmp_path, self._config_for(missing_path))
        result = _run_check(tmp_path, pythonpath=pythonpath)
        assert "FAIL" in result.stdout
        assert "key file not found" in result.stdout

    def test_empty_file_fails(self, tmp_path):
        key_path = tmp_path / "googlechat"
        key_path.write_text("")
        pythonpath = _write_fake_brokkr_dict(tmp_path, self._config_for(key_path))
        result = _run_check(tmp_path, pythonpath=pythonpath)
        # Tightened: must not be satisfiable by the substring "non-empty".
        assert "key file is empty" in result.stdout
        assert "non-empty" not in result.stdout


# --- Channel matching against the real key-file parser -------------------

class TestChannelMatching:
    def _run(self, tmp_path, key_file, channel="status"):
        return _run_with_dict_config(tmp_path, _both_steps(tmp_path, channel), key_file=key_file)

    def test_configured_channel_absent_fails(self, tmp_path):
        result = self._run(tmp_path, "[channel]\nother = https://x/\n")
        assert result.stdout.count("FAIL") == 2
        assert "'status'" in result.stdout
        assert "not found" in result.stdout

    def test_configured_channel_present_passes(self, tmp_path):
        result = self._run(tmp_path, KEY_FILE_STATUS_ONLY)
        assert "FAIL" not in result.stdout
        assert result.stdout.count("PASS") == 2

    def test_commented_channel_does_not_count(self, tmp_path):
        result = self._run(tmp_path, KEY_FILE_STATUS_ONLY, channel="hamma")
        assert result.stdout.count("FAIL") == 2
        assert "'hamma'" in result.stdout

    def test_substring_channel_not_satisfied_by_longer_key(self, tmp_path):
        """channel 'status' must not match a key file holding only
        'aumma_status' (channel as substring of the key)."""
        result = self._run(tmp_path, "[channel]\naumma_status = https://x/\n",
                            channel="status")
        assert result.stdout.count("FAIL") == 2
        assert "'status'" in result.stdout

    def test_substring_channel_not_satisfied_by_shorter_key(self, tmp_path):
        """channel 'aumma_status' must not match a key file holding only
        'status' (key as substring of the channel)."""
        result = self._run(tmp_path, "[channel]\nstatus = https://x/\n",
                            channel="aumma_status")
        assert result.stdout.count("FAIL") == 2
        assert "'aumma_status'" in result.stdout

    def test_channel_only_in_a_different_section_fails(self, tmp_path):
        key_file = textwrap.dedent("""\
            [channel]
            other = https://x/

            [extra]
            status = https://y/
        """)
        result = self._run(tmp_path, key_file)
        assert result.stdout.count("FAIL") == 2

    def test_duplicate_key_fails(self, tmp_path):
        key_file = "[channel]\nstatus = https://x/\nstatus = https://y/\n"
        result = self._run(tmp_path, key_file)
        assert result.stdout.count("FAIL") == 2
        assert "failed to parse" in result.stdout
        assert "DuplicateOptionError" in result.stdout

    def test_duplicate_section_fails(self, tmp_path):
        key_file = "[channel]\nstatus = https://x/\n\n[channel]\nother = https://y/\n"
        result = self._run(tmp_path, key_file)
        assert result.stdout.count("FAIL") == 2
        assert "DuplicateSectionError" in result.stdout

    def test_malformed_syntax_fails(self, tmp_path):
        """A value line before any section header is a parsing error."""
        key_file = "status = https://x/\n[channel]\nstatus = https://y/\n"
        result = self._run(tmp_path, key_file)
        assert result.stdout.count("FAIL") == 2
        assert "failed to parse" in result.stdout

    def test_indented_continuation_does_not_create_a_key(self, tmp_path):
        """An indented line is a continuation of the previous value in
        configparser, not a new option -- 'other' never becomes a key."""
        key_file = "[channel]\nstatus = https://x/\n    other = https://y/\n"
        result = self._run(tmp_path, key_file, channel="other")
        assert result.stdout.count("FAIL") == 2
        assert "'other'" in result.stdout

    def test_empty_value_fails(self, tmp_path):
        key_file = "[channel]\nstatus =\n"
        result = self._run(tmp_path, key_file)
        assert result.stdout.count("FAIL") == 2

    def test_uppercase_key_passes(self, tmp_path):
        key_file = "[channel]\nSTATUS = https://x/\n"
        result = self._run(tmp_path, key_file)
        assert "FAIL" not in result.stdout
        assert result.stdout.count("PASS") == 2

    def test_colon_delimiter_passes(self, tmp_path):
        key_file = "[channel]\nstatus: https://x/\n"
        result = self._run(tmp_path, key_file)
        assert "FAIL" not in result.stdout
        assert result.stdout.count("PASS") == 2

    def test_crlf_passes(self, tmp_path):
        key_path = tmp_path / "googlechat_crlf"
        key_path.write_bytes(b"[channel]\r\nstatus = https://x/\r\n")
        pythonpath = _write_fake_brokkr_dict(tmp_path, self._config_with_path(key_path))
        result = _run_check(tmp_path, pythonpath=pythonpath)
        assert "FAIL" not in result.stdout
        assert result.stdout.count("PASS") == 2

    @staticmethod
    def _config_with_path(key_path):
        return {
            "steps": {
                "state_monitor": {"method": "gchat", "channel": "status",
                                   "key_file": str(key_path)},
                "noise_diag": {"method": "gchat", "channel": "status",
                               "key_file": str(key_path)},
            }
        }


# --- Steps are evaluated independently ------------------------------------

class TestPerStepIndependence:
    def test_one_step_fails_while_the_other_passes(self, tmp_path):
        key_file = "[channel]\nstatus = https://x/\n"
        config = {
            "steps": {
                "state_monitor": {"method": "gchat", "channel": "aumma_status",
                                   "key_file": "PLACEHOLDER"},
                "noise_diag": {"method": "gchat", "channel": "status",
                               "key_file": "PLACEHOLDER"},
            }
        }
        key_path = tmp_path / "googlechat"
        key_path.write_text(key_file)
        config["steps"]["state_monitor"]["key_file"] = str(key_path)
        config["steps"]["noise_diag"]["key_file"] = str(key_path)
        pythonpath = _write_fake_brokkr_dict(tmp_path, config)
        result = _run_check(tmp_path, pythonpath=pythonpath)
        assert result.stdout.count("FAIL") == 1
        assert result.stdout.count("PASS") == 1
        # FAIL quotes the channel (repr); PASS does not -- distinguish by
        # step name rather than by quoting, since "status" is itself a
        # substring of "aumma_status".
        assert "state_monitor: channel 'aumma_status' not found" in result.stdout
        assert "noise_diag: status" in result.stdout

    def test_disabled_step_is_skipped_other_still_checked(self, tmp_path):
        key_file = "[channel]\nstatus = https://x/\n"
        key_path = tmp_path / "googlechat"
        key_path.write_text(key_file)
        config = {
            "steps": {
                "state_monitor": {"_enabled": False},
                "noise_diag": {"method": "gchat", "channel": "status",
                               "key_file": str(key_path)},
            }
        }
        pythonpath = _write_fake_brokkr_dict(tmp_path, config)
        result = _run_check(tmp_path, pythonpath=pythonpath)
        assert "FAIL" not in result.stdout
        assert "SKIP" in result.stdout
        assert "disabled" in result.stdout
        assert result.stdout.count("PASS") == 1

    def test_non_gchat_method_is_skipped(self, tmp_path):
        config = {
            "steps": {
                "state_monitor": {"method": "slack", "channel": "status",
                                   "key_file": "/nonexistent"},
                "noise_diag": {"_enabled": False},
            }
        }
        pythonpath = _write_fake_brokkr_dict(tmp_path, config)
        result = _run_check(tmp_path, pythonpath=pythonpath)
        assert "FAIL" not in result.stdout
        assert result.stdout.count("SKIP") == 2
        assert "not gchat" in result.stdout

    def test_step_absent_from_rendered_config_is_skipped(self, tmp_path):
        pythonpath = _write_fake_brokkr_dict(tmp_path, {"steps": {}})
        result = _run_check(tmp_path, pythonpath=pythonpath)
        assert "FAIL" not in result.stdout
        assert result.stdout.count("SKIP") == 2
        assert "absent" in result.stdout


# --- Real TOML parsing + brokkr's system/local/mode precedence ----------

class TestRealTomlRendering:
    def _run(self, tmp_path, system_toml=None, local_toml=None, mode_toml=None,
              execstart_mode="", env_mode="", key_file=KEY_FILE_STATUS_ONLY):
        pythonpath = _write_fake_brokkr_toml(
            tmp_path, system_toml=system_toml, local_toml=local_toml,
            mode_toml=mode_toml)
        extra_env = {
            "FAKE_BROKKR_SYSTEM_TOML": str(tmp_path / "system_main.toml")
            if system_toml is not None else "",
            "FAKE_BROKKR_LOCAL_TOML": str(tmp_path / "local_main.toml")
            if local_toml is not None else "",
            "FAKE_BROKKR_MODE_TOML": str(tmp_path / "mode.toml")
            if mode_toml is not None else "",
        }
        # _run_check() is the sole writer of tmp_path/"googlechat" -- a
        # caller that needs multiple live channels passes its own
        # key_file= content rather than pre-writing the path itself, so it
        # is never silently clobbered by the default here.
        return _run_check(
            tmp_path, pythonpath=pythonpath, key_file=key_file,
            execstart_mode=execstart_mode, env_mode=env_mode, extra_env=extra_env)

    def test_section_header_with_trailing_comment(self, tmp_path):
        key_path = tmp_path / "googlechat"
        system_toml = textwrap.dedent("""\
            [steps.state_monitor] # the status alert step
            method = "gchat"
            channel = "status"
            key_file = "{}"

            [steps.noise_diag]
            _enabled = false
        """).format(key_path)
        result = self._run(tmp_path, system_toml=system_toml)
        assert "FAIL" not in result.stdout
        assert result.stdout.count("PASS") == 1

    def test_single_quoted_value(self, tmp_path):
        key_path = tmp_path / "googlechat"
        system_toml = textwrap.dedent("""\
            [steps.state_monitor]
            method = 'gchat'
            channel = 'status'
            key_file = '{}'

            [steps.noise_diag]
            _enabled = false
        """).format(key_path)
        result = self._run(tmp_path, system_toml=system_toml)
        assert "FAIL" not in result.stdout
        assert result.stdout.count("PASS") == 1

    def test_dotted_key_syntax(self, tmp_path):
        key_path = tmp_path / "googlechat"
        system_toml = (
            'steps.state_monitor.method = "gchat"\n'
            'steps.state_monitor.channel = "status"\n'
            'steps.state_monitor.key_file = "{}"\n'
            'steps.noise_diag._enabled = false\n'
        ).format(key_path)
        result = self._run(tmp_path, system_toml=system_toml)
        assert "FAIL" not in result.stdout
        assert result.stdout.count("PASS") == 1

    def test_channel_in_unrelated_table_is_not_picked_up(self, tmp_path):
        key_path = tmp_path / "googlechat"
        system_toml = textwrap.dedent("""\
            [other_thing]
            channel = "wrong"

            [steps.state_monitor]
            method = "gchat"
            channel = "status"
            key_file = "{}"

            [steps.noise_diag]
            _enabled = false

            [yet_another]
            channel = "also_wrong"
        """).format(key_path)
        result = self._run(tmp_path, system_toml=system_toml)
        assert "FAIL" not in result.stdout
        assert result.stdout.count("PASS") == 1

    def test_local_override_changes_noise_diag_channel(self, tmp_path):
        key_path = tmp_path / "googlechat"
        key_file = textwrap.dedent("""\
            [channel]
            status = https://x/
            aumma_status = https://y/
        """)
        system_toml = textwrap.dedent("""\
            [steps.state_monitor]
            method = "gchat"
            channel = "status"
            key_file = "{0}"

            [steps.noise_diag]
            method = "gchat"
            channel = "status"
            key_file = "{0}"
        """).format(key_path)
        local_toml = textwrap.dedent("""\
            [steps.noise_diag]
            channel = "aumma_status"
        """)
        result = self._run(tmp_path, system_toml=system_toml, local_toml=local_toml,
                            key_file=key_file)
        assert "FAIL" not in result.stdout
        assert result.stdout.count("PASS") == 2
        # Both PASS -- no repr() quoting on that path. Distinguish by step.
        assert "noise_diag: aumma_status" in result.stdout
        assert "state_monitor: status" in result.stdout

    def test_mode_overlay_changes_channel(self, tmp_path):
        key_path = tmp_path / "googlechat"
        key_file = textwrap.dedent("""\
            [channel]
            status = https://x/
            panama_status = https://y/
        """)
        system_toml = textwrap.dedent("""\
            [steps.state_monitor]
            method = "gchat"
            channel = "status"
            key_file = "{0}"

            [steps.noise_diag]
            method = "gchat"
            channel = "status"
            key_file = "{0}"
        """).format(key_path)
        mode_toml = textwrap.dedent("""\
            [panama]
                [panama.main.steps.state_monitor]
                channel = "panama_status"
        """)
        result = self._run(
            tmp_path, system_toml=system_toml, mode_toml=mode_toml,
            env_mode="BROKKR_MODE=panama", key_file=key_file)
        assert "FAIL" not in result.stdout
        assert result.stdout.count("PASS") == 2
        # Both PASS -- no repr() quoting on that path. Distinguish by step.
        assert "state_monitor: panama_status" in result.stdout
        assert "noise_diag: status" in result.stdout

    def test_mode_overlay_disables_a_step(self, tmp_path):
        key_path = tmp_path / "googlechat"
        system_toml = textwrap.dedent("""\
            [steps.state_monitor]
            method = "gchat"
            channel = "status"
            key_file = "{0}"

            [steps.noise_diag]
            method = "gchat"
            channel = "status"
            key_file = "{0}"
        """).format(key_path)
        mode_toml = textwrap.dedent("""\
            [test]
                [test.main.steps.state_monitor]
                _enabled = false
        """)
        result = self._run(
            tmp_path, system_toml=system_toml, mode_toml=mode_toml,
            execstart_mode="/home/pi/dev/ltgenv/bin/brokkr --mode test start")
        assert "FAIL" not in result.stdout
        assert result.stdout.count("SKIP") == 1
        assert "disabled" in result.stdout
        assert result.stdout.count("PASS") == 1


# --- Permissions: informational at 644, WARN only for a non-pi owner ----

class TestPermissions:
    def test_group_world_readable_is_informational_not_warn(self, tmp_path):
        result = _run_with_dict_config(tmp_path, _both_steps_with_keyfile(tmp_path),
                                        key_file_mode=0o644, fake_stat_owner="pi")
        assert "WARN" not in result.stdout
        assert "FAIL" not in result.stdout
        assert "expected across the fleet" in result.stdout

    def test_owner_only_permissions_pass(self, tmp_path):
        result = _run_with_dict_config(tmp_path, _both_steps_with_keyfile(tmp_path),
                                        key_file_mode=0o600, fake_stat_owner="pi")
        assert "permissions (600) restrict read access" in result.stdout
        assert "WARN" not in result.stdout

    def test_non_pi_owner_warns(self, tmp_path):
        result = _run_with_dict_config(
            tmp_path, _both_steps_with_keyfile(tmp_path), key_file_mode=0o644,
            fake_stat_owner="root")
        assert "WARN" in result.stdout
        assert "not owned by pi" in result.stdout


def _both_steps_with_keyfile(tmp_path):
    key_path = tmp_path / "googlechat"
    return {
        "steps": {
            "state_monitor": {"method": "gchat", "channel": "status",
                               "key_file": str(key_path)},
            "noise_diag": {"_enabled": False},
        }
    }


# --- Never print webhook values, in stdout OR stderr ---------------------

class TestNeverPrintsSecrets:
    def test_webhook_url_never_leaks(self, tmp_path):
        key_file = textwrap.dedent("""\
            [channel]
            status = https://chat.googleapis.com/v1/spaces/AAA/messages?key=SECRETVALUE
            aumma_status = https://chat.googleapis.com/v1/spaces/BBB/messages?key=SECRETVALUE2
        """)
        result = _run_with_dict_config(tmp_path, _both_steps(tmp_path), key_file=key_file)
        for stream in (result.stdout, result.stderr):
            assert "SECRETVALUE" not in stream
            assert "chat.googleapis.com" not in stream

    def test_malformed_file_does_not_leak_source_line_in_exception(self, tmp_path):
        """ParsingError's own message embeds the offending source line --
        the helper must only ever print the exception's type name."""
        key_file = "garbage_before_any_header_with_SECRETVALUE_in_it\n[channel]\n"
        result = _run_with_dict_config(tmp_path, _both_steps(tmp_path), key_file=key_file)
        for stream in (result.stdout, result.stderr):
            assert "SECRETVALUE" not in stream


# --- main() wiring --------------------------------------------------------

class TestMainWiring:
    def test_main_invokes_check_googlechat_key(self):
        body = VERIFY_SCRIPT.read_text()
        main_body = body[body.index("main() {"):]
        assert "check_googlechat_key" in main_body, \
            "main() must call check_googlechat_key"
