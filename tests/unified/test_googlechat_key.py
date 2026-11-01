"""
Tests for the HAM-118 Google Chat key-file check in verify_deployment.sh.

A missing or empty /home/pi/.googlechat, or a configured channel with no
matching section in it, disables that step's alerts with no error --
notifiers logs once and send() becomes a permanent no-op. These tests
exercise check_googlechat_key() directly by sourcing verify_deployment.sh
(guarded so sourcing does not run main()) and overriding its key-file and
config-path lookups via environment variables.
"""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "verify_deployment.sh"

BASE_TOML_BOTH_STATUS = textwrap.dedent("""\
    [steps.state_monitor]
    channel = "status"

    [steps.noise_diag]
    channel = "status"
""")


def _stat_stub(tmp_path):
    """Build a `stat` shim that supports GNU `stat -c %U|%a FILE`.

    verify_deployment.sh (and this check) use GNU stat throughout, since it
    only ever runs on Raspbian. macOS ships BSD stat with no -c option, so
    tests prepend this shim to PATH rather than changing the check's
    production behavior.
    """
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
            print(pwd.getpwuid(s.st_uid).pw_name)
        elif fmt == "%a":
            print(oct(st.S_IMODE(s.st_mode))[2:])
    """))
    stub.chmod(0o755)
    return str(bin_dir)


def _sourceable_script(tmp_path):
    """Copy verify_deployment.sh with its trailing `main "$@"` call stripped.

    Same technique as TestVerifyDeploymentBehaviour in test_log_bounds.py:
    sourcing the real file would otherwise invoke main() and run every
    check against the live host.
    """
    script = VERIFY_SCRIPT.read_text()
    assert script.rstrip().endswith('main "$@"')
    copy = tmp_path / "verify_deployment.sh"
    copy.write_text(script.rstrip()[:-len('main "$@"')])
    return copy


def _run_check(tmp_path, key_file=None, key_file_exists=True, key_file_mode=0o644,
                base_toml=BASE_TOML_BOTH_STATUS, local_toml=None):
    """Source verify_deployment.sh and run check_googlechat_key() in isolation."""
    key_path = tmp_path / "googlechat"
    if key_file_exists:
        key_path.write_text(key_file if key_file is not None else "")
        key_path.chmod(key_file_mode)

    base_path = tmp_path / "main.toml"
    base_path.write_text(base_toml)

    # A local_toml of None means "no per-unit override file at all", which
    # must be handled the same as an override that sets nothing.
    local_path = tmp_path / "local_main.toml"
    if local_toml is not None:
        local_path.write_text(local_toml)

    sourceable = _sourceable_script(tmp_path)

    env = os.environ.copy()
    env["GOOGLECHAT_KEY_FILE"] = str(key_path)
    env["GOOGLECHAT_BASE_TOML"] = str(base_path)
    env["GOOGLECHAT_LOCAL_TOML"] = str(local_path)
    env["PATH"] = "{}:{}".format(_stat_stub(tmp_path), env["PATH"])

    result = subprocess.run(
        ["bash", "-c", 'source "$1"; check_googlechat_key', "_", str(sourceable)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    return result


class TestMissingOrEmptyKeyFile:
    def test_missing_file_fails(self, tmp_path):
        result = _run_check(tmp_path, key_file_exists=False)
        assert "FAIL" in result.stdout
        assert "not found" in result.stdout

    def test_empty_file_fails(self, tmp_path):
        result = _run_check(tmp_path, key_file="")
        assert "FAIL" in result.stdout
        assert "empty" in result.stdout


class TestChannelResolution:
    def test_configured_channel_absent_fails(self, tmp_path):
        """Key file has sections, but not the one state_monitor/noise_diag use."""
        result = _run_check(
            tmp_path,
            key_file=textwrap.dedent("""\
                [other]
                default = https://chat.googleapis.com/v1/spaces/AAA/messages?key=BBB
            """),
        )
        assert result.stdout.count("FAIL") == 2, result.stdout
        assert "'status'" in result.stdout
        assert "not found in key file" in result.stdout

    def test_configured_channel_present_passes(self, tmp_path):
        result = _run_check(
            tmp_path,
            key_file=textwrap.dedent("""\
                [status]
                default = https://chat.googleapis.com/v1/spaces/AAA/messages?key=BBB
            """),
        )
        assert "FAIL" not in result.stdout
        assert result.stdout.count("PASS") >= 2

    def test_per_unit_override_channel_present_passes(self, tmp_path):
        """Override moves state_monitor to a different channel; key file has it."""
        result = _run_check(
            tmp_path,
            key_file=textwrap.dedent("""\
                [aumma_status]
                default = https://chat.googleapis.com/v1/spaces/AAA/messages?key=BBB
                [status]
                default = https://chat.googleapis.com/v1/spaces/CCC/messages?key=DDD
            """),
            local_toml=textwrap.dedent("""\
                [steps.state_monitor]
                channel = "aumma_status"
            """),
        )
        assert "FAIL" not in result.stdout
        assert "'aumma_status'" in result.stdout
        # noise_diag has no override, so it still resolves to the base channel.
        assert "'status'" in result.stdout

    def test_per_unit_override_channel_absent_fails(self, tmp_path):
        """Override moves state_monitor to a channel the key file lacks."""
        result = _run_check(
            tmp_path,
            key_file=textwrap.dedent("""\
                [status]
                default = https://chat.googleapis.com/v1/spaces/AAA/messages?key=BBB
            """),
            local_toml=textwrap.dedent("""\
                [steps.state_monitor]
                channel = "panama_status"
            """),
        )
        assert "FAIL" in result.stdout
        assert "'panama_status'" in result.stdout
        assert "not found in key file" in result.stdout
        # noise_diag used the base channel and found it.
        assert "'status'" in result.stdout


class TestPermissions:
    def test_group_world_readable_warns_not_fails(self, tmp_path):
        """Every unit today is pi:pi 644 -- this must WARN, never FAIL."""
        result = _run_check(
            tmp_path,
            key_file=textwrap.dedent("""\
                [status]
                default = https://chat.googleapis.com/v1/spaces/AAA/messages?key=BBB
            """),
            key_file_mode=0o644,
        )
        assert "WARN" in result.stdout
        assert "group/world readable" in result.stdout
        assert "FAIL" not in result.stdout

    def test_owner_only_permissions_pass(self, tmp_path):
        result = _run_check(
            tmp_path,
            key_file=textwrap.dedent("""\
                [status]
                default = https://chat.googleapis.com/v1/spaces/AAA/messages?key=BBB
            """),
            key_file_mode=0o600,
        )
        assert "Google Chat key file permissions (600) restrict read access" in result.stdout


class TestNeverPrintsSecrets:
    def test_webhook_url_never_in_output(self, tmp_path):
        webhook = "https://chat.googleapis.com/v1/spaces/AAAAA/messages?key=SECRETVALUE"
        result = _run_check(
            tmp_path,
            key_file="[status]\ndefault = {}\n".format(webhook),
        )
        assert webhook not in result.stdout
        assert "SECRETVALUE" not in result.stdout
        assert "chat.googleapis.com" not in result.stdout
