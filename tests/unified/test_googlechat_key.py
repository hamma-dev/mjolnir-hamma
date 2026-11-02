"""
Tests for the HAM-118 Google Chat key-file check: check_googlechat_key() in
verify_deployment.sh, backed by scripts/verify_googlechat_channels.py.

Round 3: the previous version imported brokkr.config.main to ask brokkr
for the "effective" rendered config. That import can WRITE a missing
local config file back to disk (FileConfigLevel writes its defaults on
first read) -- as root, that means new root-owned files under
/home/pi/.config/brokkr, or an entirely new /root/.config/brokkr.
verify_deployment.sh must never write anything, so the helper no longer
imports brokkr at all: it scans config/main.toml, the local override, and
every mode.toml table read-only with tomli, and checks every channel it
finds -- anywhere, not just the currently effective one -- against the
key file with configparser, the same way notifiers reads it. No fakes of
brokkr are needed any more.

These tests drive the real check_googlechat_key() (sourced with its
trailing `main "$@"` stripped, so sourcing never runs main()) with every
path overridable through environment variables that are unset (and so
inert) in production:
    GOOGLECHAT_PYTHON             -- interpreter (default /home/pi/dev/ltgenv/bin/python)
    GOOGLECHAT_HELPER             -- helper script path
    GOOGLECHAT_DEFAULT_KEY_FILE   -- default key file (default /home/pi/.googlechat)
    GOOGLECHAT_SYSTEM_MAIN_TOML   -- config/main.toml
    GOOGLECHAT_SYSTEM_MODE_TOML   -- config/mode.toml
    GOOGLECHAT_LOCAL_MAIN_TOML    -- ~/.config/brokkr/hamma/main.toml
    GOOGLECHAT_LOCAL_MODE_TOML    -- ~/.config/brokkr/hamma/mode.toml
    GOOGLECHAT_PI_HOME            -- what '~' expands to (never root's home)
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


# --- Stand-ins for the sensor environment --------------------------------

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


def _python_interpreter(tmp_path, missing=False, no_tomli=False):
    """Stand-in for /home/pi/dev/ltgenv/bin/python.

    no_tomli builds a launcher that poisons sys.modules for 'tomli' and
    'tomllib' *before* running the target script, so "tomli unavailable"
    can be exercised deterministically regardless of what the real test
    interpreter (which always has at least tomllib, Python >= 3.11) ships.
    """
    if missing:
        return str(tmp_path / "no-such-python")
    if not no_tomli:
        return sys.executable
    stub = tmp_path / "python_no_tomli"
    stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys, runpy
        sys.modules["tomli"] = None
        sys.modules["tomllib"] = None
        target = sys.argv[1]
        sys.argv = sys.argv[1:]
        runpy.run_path(target, run_name="__main__")
    """))
    stub.chmod(0o755)
    return str(stub)


def _sourceable_script(tmp_path, source_path=None):
    """Copy verify_deployment.sh (or `source_path`) with its trailing
    `main "$@"` call stripped, so sourcing it never runs main() against
    the live host."""
    script = (source_path or VERIFY_SCRIPT).read_text()
    assert script.rstrip().endswith('main "$@"')
    copy = tmp_path / "verify_deployment.sh"
    copy.write_text(script.rstrip()[:-len('main "$@"')])
    return copy


def _run_check(tmp_path, python_missing=False, no_tomli=False,
                helper_override=None, key_file=None, key_file_mode=0o644,
                default_key_file=None, system_main_toml=None,
                system_mode_toml=None, local_main_toml=None,
                local_mode_toml=None, fake_stat_owner=None, extra_shell=""):
    sourceable = _sourceable_script(tmp_path)

    key_path = tmp_path / "googlechat"
    if key_file is not None:
        key_path.write_bytes(
            key_file if isinstance(key_file, bytes) else key_file.encode("utf-8"))
        key_path.chmod(key_file_mode)

    def _maybe_write(name, content):
        if content is None:
            return str(tmp_path / ("no-such-" + name))
        path = tmp_path / name
        path.write_text(content)
        return str(path)

    env = os.environ.copy()
    env["PATH"] = "{}:{}".format(_stat_stub(tmp_path), env["PATH"])
    env["GOOGLECHAT_PYTHON"] = _python_interpreter(
        tmp_path, missing=python_missing, no_tomli=no_tomli)
    env["GOOGLECHAT_HELPER"] = (
        helper_override if helper_override is not None else str(HELPER_SCRIPT))
    env["GOOGLECHAT_DEFAULT_KEY_FILE"] = default_key_file or str(key_path)
    env["GOOGLECHAT_PI_HOME"] = str(tmp_path)
    env["GOOGLECHAT_SYSTEM_MAIN_TOML"] = _maybe_write("system_main.toml", system_main_toml)
    env["GOOGLECHAT_SYSTEM_MODE_TOML"] = _maybe_write("system_mode.toml", system_mode_toml)
    env["GOOGLECHAT_LOCAL_MAIN_TOML"] = _maybe_write("local_main.toml", local_main_toml)
    env["GOOGLECHAT_LOCAL_MODE_TOML"] = _maybe_write("local_mode.toml", local_mode_toml)
    if fake_stat_owner:
        env["FAKE_STAT_OWNER"] = fake_stat_owner

    script = 'source "$1"; check_googlechat_key' + (
        ("; " + extra_shell) if extra_shell else "")
    result = subprocess.run(
        ["bash", "-c", script, "_", str(sourceable)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    return result


def _step_toml(channel, method='"gchat"', key_file=None, step="state_monitor",
               table="steps"):
    kf_line = 'key_file = "{}"\n'.format(key_file) if key_file else ""
    method_line = "method = {}\n".format(method) if method is not None else ""
    return textwrap.dedent("""\
        [{table}.{step}]
        {method_line}channel = "{channel}"
        {kf_line}
    """).format(table=table, step=step, method_line=method_line,
                channel=channel, kf_line=kf_line)


# --- Key file: missing, empty, no [channel] section, OSError, encoding --

class TestKeyFileChecks:
    def test_missing_file_fails(self, tmp_path):
        result = _run_check(tmp_path, default_key_file=str(tmp_path / "nope"))
        assert "FAIL" in result.stdout
        assert "is missing" in result.stdout

    def test_empty_file_fails(self, tmp_path):
        result = _run_check(tmp_path, key_file="")
        assert "FAIL" in result.stdout
        assert "is empty" in result.stdout

    def test_no_channel_section_fails(self, tmp_path):
        result = _run_check(tmp_path, key_file="[other]\nkey = https://x/\n")
        assert "FAIL" in result.stdout
        assert "no [channel] section" in result.stdout

    def test_unreadable_file_fails_no_crash(self, tmp_path):
        """A mode-000 file denies read access to everyone, including the
        owner, as long as the test is not itself running as root."""
        if os.geteuid() == 0:
            import pytest
            pytest.skip("chmod 000 does not deny root")
        key_path = tmp_path / "googlechat"
        key_path.write_text("[channel]\nstatus = https://x/\n")
        key_path.chmod(0o000)
        result = _run_check(
            tmp_path, default_key_file=str(key_path), key_file=None)
        assert "FAIL" in result.stdout
        # Specifically the "could not be opened" reason, not merely *some*
        # FAIL -- configparser.read() silently drops an unreadable file,
        # so without this check the generic "no [channel] section" FAIL
        # would fire instead and mask the real cause.
        assert "could not be opened" in result.stdout
        assert "SECRET" not in result.stdout
        assert "SECRET" not in result.stderr
        key_path.chmod(0o644)  # so tmp_path cleanup can remove it

    def test_non_utf8_file_fails_no_abort(self, tmp_path):
        key_path = tmp_path / "googlechat"
        key_path.write_bytes(b"[channel]\nstatus = \xff\xfe not valid utf-8\n")
        result = _run_check(tmp_path, default_key_file=str(key_path), key_file=None)
        assert "FAIL" in result.stdout
        assert result.returncode == 0
        assert "could not be decoded" in result.stdout

    def test_duplicate_key_fails(self, tmp_path):
        """configparser's default strict=True rejects a repeated option in
        the same section (DuplicateOptionError)."""
        key_file = "[channel]\nstatus = https://x/\nstatus = https://y/\n"
        result = _run_check(tmp_path, key_file=key_file)
        assert "FAIL" in result.stdout
        assert "failed to parse" in result.stdout

    def test_duplicate_section_fails(self, tmp_path):
        key_file = "[channel]\nstatus = https://x/\n\n[channel]\nother = https://y/\n"
        result = _run_check(tmp_path, key_file=key_file)
        assert "FAIL" in result.stdout
        assert "failed to parse" in result.stdout

    def test_step_without_a_channel_key_is_ignored(self, tmp_path):
        """A step with no channel key at all (e.g. hamma_plot) must never
        be reported, and scanning a later channel-bearing step must still
        complete -- a buggy "channel is None" skip doesn't just miss
        hamma_plot silently, it crashes on it (configparser's option
        lookup calls .lower() on the key), aborting the rest of the scan."""
        toml = textwrap.dedent("""\
            [steps.hamma_plot]
            method = "gchat"
            output_path = "trigger_plots"

            [steps.state_monitor]
            method = "gchat"
            channel = "status"
        """)
        result = _run_check(
            tmp_path, key_file=KEY_FILE_STATUS_ONLY, system_main_toml=toml)
        assert "hamma_plot" not in result.stdout
        assert "None" not in result.stdout
        assert "crashed unexpectedly" not in result.stdout
        assert "'status'" in result.stdout
        assert "PASS" in result.stdout

    def test_run_and_report_survives_an_unexpected_crash_in_main(self):
        """The helper must always exit 0 and report via its own output,
        even if main() raises something nothing else anticipated -- not
        just the specific error types caught inside main()'s own logic.
        Imports the module directly and monkeypatches main() itself to
        raise, so this doesn't depend on finding a real bug to trigger
        the outermost catch-all (run_and_report, called from __main__)."""
        sys.path.insert(0, str(HELPER_SCRIPT.parent))
        try:
            import verify_googlechat_channels as helper_module
            import importlib
            importlib.reload(helper_module)

            def _boom():
                raise RuntimeError("nothing anticipated this")

            original_main = helper_module.main
            helper_module.main = _boom
            try:
                import io
                import contextlib
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    helper_module.run_and_report()  # must not raise
                output = buf.getvalue()
            finally:
                helper_module.main = original_main
        finally:
            sys.path.remove(str(HELPER_SCRIPT.parent))

        assert "FAIL" in output
        assert "crashed unexpectedly" in output
        assert "RuntimeError" in output

    def test_helper_stderr_is_discarded_not_merged_into_parsed_output(self, tmp_path):
        """If the helper ever writes to stderr, that must never reach the
        parsed PASS/FAIL/SKIP stream -- production redirects it to
        /dev/null rather than merging it (2>/dev/null, not 2>&1)."""
        noisy = tmp_path / "noisy_helper.py"
        noisy.write_text(
            "import sys\n"
            "print('SOMETHING ON STDERR', file=sys.stderr)\n"
            "print('PASS ok')\n"
        )
        result = _run_check(tmp_path, helper_override=str(noisy))
        assert "SOMETHING ON STDERR" not in result.stdout
        assert "unexpected helper output" not in result.stdout
        assert result.stdout.count("FAIL") == 0
        assert "ok" in result.stdout


# --- Channel scanning: substrings, sections, case, delimiters -----------

class TestChannelMatching:
    def test_configured_channel_absent_fails(self, tmp_path):
        result = _run_check(
            tmp_path, key_file="[channel]\nother = https://x/\n",
            system_main_toml=_step_toml("status"))
        assert "FAIL" in result.stdout
        assert "'status'" in result.stdout
        assert "not found" in result.stdout

    def test_configured_channel_present_passes(self, tmp_path):
        result = _run_check(
            tmp_path, key_file=KEY_FILE_STATUS_ONLY,
            system_main_toml=_step_toml("status"))
        assert "FAIL" not in result.stdout
        assert "'status'" in result.stdout
        assert "PASS" in result.stdout

    def test_missing_channel_key_fails(self, tmp_path):
        """[channel] exists, but not this specific option."""
        result = _run_check(
            tmp_path, key_file="[channel]\nstatus = https://x/\n",
            system_main_toml=_step_toml("aumma_status"))
        assert "FAIL" in result.stdout
        assert "'aumma_status'" in result.stdout

    def test_substring_channel_not_satisfied_by_longer_key(self, tmp_path):
        result = _run_check(
            tmp_path, key_file="[channel]\naumma_status = https://x/\n",
            system_main_toml=_step_toml("status"))
        assert "FAIL" in result.stdout
        assert "'status'" in result.stdout

    def test_substring_channel_not_satisfied_by_shorter_key(self, tmp_path):
        result = _run_check(
            tmp_path, key_file="[channel]\nstatus = https://x/\n",
            system_main_toml=_step_toml("aumma_status"))
        assert "FAIL" in result.stdout
        assert "'aumma_status'" in result.stdout

    def test_uppercase_key_passes(self, tmp_path):
        result = _run_check(
            tmp_path, key_file="[channel]\nSTATUS = https://x/\n",
            system_main_toml=_step_toml("status"))
        assert "FAIL" not in result.stdout
        assert "PASS" in result.stdout

    def test_empty_value_fails(self, tmp_path):
        result = _run_check(
            tmp_path, key_file="[channel]\nstatus =\n",
            system_main_toml=_step_toml("status"))
        assert "FAIL" in result.stdout

    def test_malformed_toml_fails_naming_file(self, tmp_path):
        bad_toml = "steps.state_monitor.channel = [unterminated\n"
        result = _run_check(
            tmp_path, key_file=KEY_FILE_STATUS_ONLY, system_main_toml=bad_toml)
        assert "FAIL" in result.stdout
        assert "failed to parse" in result.stdout
        assert "system_main.toml" in result.stdout

    def test_channel_only_in_mode_toml(self, tmp_path):
        mode_toml = textwrap.dedent("""\
            [panama]
                [panama.main.steps.state_monitor]
                method = "gchat"
                channel = "panama_status"
        """)
        result = _run_check(
            tmp_path, key_file="[channel]\npanama_status = https://x/\n",
            system_mode_toml=mode_toml)
        assert "FAIL" not in result.stdout
        assert "'panama_status'" in result.stdout
        assert "mode.toml" in result.stdout
        assert "panama.main.steps.state_monitor" in result.stdout

    def test_channel_only_in_local_main_toml(self, tmp_path):
        result = _run_check(
            tmp_path, key_file="[channel]\naumma_status = https://x/\n",
            local_main_toml=_step_toml("aumma_status", step="noise_diag"))
        assert "FAIL" not in result.stdout
        assert "'aumma_status'" in result.stdout
        assert "local override" in result.stdout

    def test_non_gchat_method_is_not_checked(self, tmp_path):
        """A slack step's channel must never be reported at all, even
        though it isn't in the key file -- it is simply out of scope."""
        result = _run_check(
            tmp_path, key_file="[channel]\nstatus = https://x/\n",
            system_main_toml=_step_toml("not_a_real_channel", method='"slack"'))
        assert "not_a_real_channel" not in result.stdout

    def test_absent_method_defaults_to_gchat(self, tmp_path):
        """config/main.toml declares every notifier step's method
        explicitly, but a step with channel set and no method key must
        still be checked (gchat is the implicit default)."""
        result = _run_check(
            tmp_path, key_file="[channel]\nstatus = https://x/\n",
            system_main_toml=_step_toml("status", method=None))
        assert "FAIL" not in result.stdout
        assert "PASS" in result.stdout

    def test_step_own_key_file_is_used_not_the_default(self, tmp_path):
        """A step naming its own key_file must be checked against THAT
        file, not silently against the default -- the default key file
        here deliberately lacks the channel, and the step's own key file
        deliberately has it, so a fallback-to-default bug fails this."""
        custom_key_file = tmp_path / "custom.googlechat"
        custom_key_file.write_text("[channel]\nstatus = https://x/\n")
        default_key_file = tmp_path / "default.googlechat"
        default_key_file.write_text("[channel]\nother = https://x/\n")
        result = _run_check(
            tmp_path, key_file=None, default_key_file=str(default_key_file),
            system_main_toml=_step_toml("status", key_file=str(custom_key_file)))
        assert "'status'" in result.stdout
        # The step's own key_file must pass; a fallback-to-default bug
        # would instead report channel 'status' as FAILed.
        fail_lines = [l for l in result.stdout.splitlines() if "FAIL" in l]
        assert not any("'status'" in l for l in fail_lines), fail_lines
        pass_lines = [l for l in result.stdout.splitlines() if "PASS" in l]
        assert any("'status'" in l for l in pass_lines), result.stdout


# --- Helper robustness: crash, empty/unexpected output, missing deps ----

class TestHelperRobustness:
    def test_missing_interpreter_skips(self, tmp_path):
        result = _run_check(tmp_path, python_missing=True)
        assert "SKIP" in result.stdout
        assert "ltgenv python not found" in result.stdout
        assert "FAIL" not in result.stdout

    def test_missing_tomli_skips_channel_scan_but_still_checks_default_file(
            self, tmp_path):
        result = _run_check(
            tmp_path, key_file=KEY_FILE_STATUS_ONLY, no_tomli=True,
            system_main_toml=_step_toml("status"))
        assert "SKIP" in result.stdout
        assert "tomli not available" in result.stdout
        # The default key file is still checked -- that part needs no TOML.
        assert "is readable and has a [channel] section" in result.stdout

    def test_helper_exit_nonzero_is_a_fail_not_a_skip_or_abort(self, tmp_path):
        crashy = tmp_path / "crashy_helper.py"
        crashy.write_text("import sys; sys.exit(1)\n")
        result = _run_check(
            tmp_path, helper_override=str(crashy),
            extra_shell="echo REACHED_AFTER_CHECK")
        assert "FAIL" in result.stdout
        assert "crashed" in result.stdout
        assert "SKIP" not in result.stdout
        assert "REACHED_AFTER_CHECK" in result.stdout, (
            "a crashing helper must not abort the rest of verify_deployment.sh")

    def test_helper_empty_output_is_a_fail(self, tmp_path):
        quiet = tmp_path / "quiet_helper.py"
        quiet.write_text("")
        result = _run_check(
            tmp_path, helper_override=str(quiet),
            extra_shell="echo REACHED_AFTER_CHECK")
        assert "FAIL" in result.stdout
        assert "no output" in result.stdout
        assert "REACHED_AFTER_CHECK" in result.stdout

    def test_helper_unexpected_output_is_a_fail(self, tmp_path):
        weird = tmp_path / "weird_helper.py"
        weird.write_text("print('MAYBE a status line')\n")
        result = _run_check(
            tmp_path, helper_override=str(weird),
            extra_shell="echo REACHED_AFTER_CHECK")
        assert "FAIL" in result.stdout
        assert "unexpected helper output" in result.stdout
        assert "REACHED_AFTER_CHECK" in result.stdout

    def test_missing_helper_file_skips(self, tmp_path):
        result = _run_check(tmp_path, helper_override=str(tmp_path / "nope.py"))
        assert "SKIP" in result.stdout
        assert "verify_googlechat_channels.py not found" in result.stdout
        assert "FAIL" not in result.stdout


# --- Permissions: informational at 644/640, WARN only for a non-pi owner -

class TestPermissions:
    def test_644_is_informational_not_warn(self, tmp_path):
        result = _run_check(
            tmp_path, key_file=KEY_FILE_STATUS_ONLY, key_file_mode=0o644,
            fake_stat_owner="pi")
        assert "WARN" not in result.stdout
        assert "FAIL" not in result.stdout
        assert "expected across the fleet" in result.stdout

    def test_640_is_informational_not_warn(self, tmp_path):
        result = _run_check(
            tmp_path, key_file=KEY_FILE_STATUS_ONLY, key_file_mode=0o640,
            fake_stat_owner="pi")
        assert "WARN" not in result.stdout
        assert "expected across the fleet" in result.stdout

    def test_owner_only_permissions_pass(self, tmp_path):
        result = _run_check(
            tmp_path, key_file=KEY_FILE_STATUS_ONLY, key_file_mode=0o600,
            fake_stat_owner="pi")
        assert "permissions (600) restrict read access" in result.stdout
        assert "WARN" not in result.stdout

    def test_non_pi_owner_warns(self, tmp_path):
        result = _run_check(
            tmp_path, key_file=KEY_FILE_STATUS_ONLY, key_file_mode=0o644,
            fake_stat_owner="root")
        assert "WARN" in result.stdout
        assert "not owned by pi" in result.stdout


# --- Never print webhook values, in stdout OR stderr ---------------------

class TestNeverPrintsSecrets:
    def test_webhook_url_never_leaks_through_the_shell_check(self, tmp_path):
        key_file = textwrap.dedent("""\
            [channel]
            status = https://chat.googleapis.com/v1/spaces/AAA/messages?key=SECRETVALUE
        """)
        result = _run_check(
            tmp_path, key_file=key_file, system_main_toml=_step_toml("status"))
        assert "SECRETVALUE" not in result.stdout
        assert "SECRETVALUE" not in result.stderr
        assert "chat.googleapis.com" not in result.stdout
        assert "chat.googleapis.com" not in result.stderr

    def test_malformed_file_exception_text_never_leaks(self, tmp_path):
        """ParsingError's own str() embeds the offending source line --
        the helper must only ever print the exception's type name. Call
        the helper directly (not through bash) so stdout and stderr are
        captured as two genuinely separate streams, not merged."""
        key_path = tmp_path / "googlechat"
        key_path.write_text(
            "status_with_SECRETVALUE_in_the_bad_line = https://x/\n[channel]\n")
        env = os.environ.copy()
        env["GOOGLECHAT_DEFAULT_KEY_FILE"] = str(key_path)
        env["GOOGLECHAT_SYSTEM_MAIN_TOML"] = str(tmp_path / "no-such.toml")
        result = subprocess.run(
            [sys.executable, str(HELPER_SCRIPT)],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert result.returncode == 0
        assert "SECRETVALUE" not in result.stdout
        assert "SECRETVALUE" not in result.stderr
        assert "FAIL" in result.stdout


# --- main() wiring: a commented-out call must not satisfy this -----------

class TestMainWiring:
    def test_main_invokes_check_googlechat_key(self):
        body = VERIFY_SCRIPT.read_text()
        main_body = body[body.index("main() {"):]
        lines = [
            line.strip() for line in main_body.splitlines()
            if not line.strip().startswith("#")
        ]
        assert "check_googlechat_key" in lines, (
            "main() must have an actual, uncommented call to "
            "check_googlechat_key, not merely mention its name")

    def test_a_commented_out_call_would_not_satisfy_this_test(self):
        """Guards the guard: confirms the comment-stripping above actually
        does something, by checking it against a deliberately-commented
        fixture that mentions the function name only in a comment."""
        fixture = "main() {\n    # check_googlechat_key\n    check_log_bounds\n}\n"
        lines = [
            line.strip() for line in fixture.splitlines()
            if not line.strip().startswith("#")
        ]
        assert "check_googlechat_key" not in lines


# --- Symlinked deployment (mj51, mj54 run via /usr/local/bin symlink) ---

class TestSymlinkInvocation:
    def test_helper_found_through_a_symlink_to_the_real_script(self, tmp_path):
        real_dir = tmp_path / "realdir"
        link_dir = tmp_path / "linkdir"
        real_dir.mkdir()
        link_dir.mkdir()

        stripped = _sourceable_script(real_dir)
        (real_dir / "verify_googlechat_channels.py").write_text(
            HELPER_SCRIPT.read_text())
        (real_dir / "verify_googlechat_channels.py").chmod(0o755)

        symlink = link_dir / "verify_deployment.sh"
        symlink.symlink_to(stripped)

        key_path = tmp_path / "googlechat"
        key_path.write_text(KEY_FILE_STATUS_ONLY)

        env = os.environ.copy()
        env["PATH"] = "{}:{}".format(_stat_stub(tmp_path), env["PATH"])
        env["GOOGLECHAT_PYTHON"] = sys.executable
        # No GOOGLECHAT_HELPER override -- it must be found next to the
        # *real* script via readlink -f, not the symlink's own directory.
        env["GOOGLECHAT_DEFAULT_KEY_FILE"] = str(key_path)
        env["GOOGLECHAT_SYSTEM_MAIN_TOML"] = str(tmp_path / "no-such.toml")
        env["GOOGLECHAT_SYSTEM_MODE_TOML"] = str(tmp_path / "no-such2.toml")
        env["GOOGLECHAT_LOCAL_MAIN_TOML"] = str(tmp_path / "no-such3.toml")
        env["GOOGLECHAT_LOCAL_MODE_TOML"] = str(tmp_path / "no-such4.toml")

        result = subprocess.run(
            ["bash", "-c", 'source "$1"; check_googlechat_key', "_", str(symlink)],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert "verify_googlechat_channels.py not found" not in result.stdout
        assert "is readable and has a [channel] section" in result.stdout
