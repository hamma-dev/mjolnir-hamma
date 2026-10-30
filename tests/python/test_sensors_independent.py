"""Tests for sensors.py — sensor power control script.
INDEPENDENTLY WRITTEN SUITE (HAM-184). Kept as its own module, verbatim.

Written from the specification by an agent that never saw the
implementation, so these tests cannot have been shaped to fit it. Two
earlier attempts at this fix shipped regressions their own tests did not
catch -- in both cases the same author wrote the code and the tests that
judged it.

It does not trust the module under test to say what a drop-in means: it
carries its own oracle, effective_mode()/non_mode_lines(), implementing
brokkr's documented precedence directly (CLI `--mode` beats
`Environment=BROKKR_MODE=`; within a form systemd's last assignment
wins), so it cannot be fooled by the inverted-precedence bug it exists to
catch. Everything round-trips through a real file.

Measured: 48 failed / 222 passed against the pre-fix baseline 052ee1f;
269 passed against this implementation.

It used to also carry a verbatim copy of test_sensors.py's baseline tests
(about half its lines). The copy was removed in the 2026-10-30 red team:
it re-ran the same assertions, and the four copied tests whose fixtures
the independent spec had changed are kept below in
TestSpecVariantsOfSharedTests. Every parametrize list here is built from
this file's own fixture data, so none is emptied by the cut.
"""

import importlib.util
import io
import os
import pathlib
import re
import textwrap

import pytest
from unittest.mock import patch, MagicMock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "sensors.py"


def load_sensors():
    """Load sensors module from scripts/."""
    spec = importlib.util.spec_from_file_location(
        "sensors", str(SCRIPT_PATH),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def isolate_from_host(module, host_dir):
    """Point every probe of the live unit away from this machine.

    plan_mode() reads the unit file, other drop-ins and mode.toml, and asks
    systemd about the loaded unit. On a Pi, a systemd CI box or a dev box
    with brokkr installed those answers would leak into every test, so they
    are pinned here; tests that exercise a probe use the saved _real_*.
    """
    module._real_systemd_preflight = module.systemd_preflight
    module._real_systemctl_show_mode = module.systemctl_show_mode
    dropin_dir = host_dir / "brokkr-hamma-default.service.d"
    module.DROPIN_DIR = str(dropin_dir)
    module.DROPIN_PATH = str(dropin_dir / "mode.conf")
    module.DROPIN_TMP_PATH = str(dropin_dir / module.DROPIN_TMP_NAME)
    module.UNIT_FILE_PATH = str(host_dir / "absent.service")
    module.EXTRA_DROPIN_DIRS = [str(host_dir / "absent.d")]
    module.LOCAL_MODE_TOML = str(host_dir / "absent-mode.toml")
    module.systemd_preflight = lambda current: None
    module.systemctl_show_mode = lambda: None
    return module


@pytest.fixture
def sensors(tmp_path_factory):
    """Provide the sensors module, isolated from this machine's systemd."""
    return isolate_from_host(load_sensors(), tmp_path_factory.mktemp("host"))


# --- Shared tests whose fixtures the independent spec changed ---

ENVIRONMENT_DROPIN = "[Service]\nEnvironment=BROKKR_MODE={}\n"


class TestSpecVariantsOfSharedTests:
    """The copied baseline tests that DIFFER from test_sensors.py.

    The rest of the copy was identical and is gone; these four assert the
    independent spec's corrected expectations (a mode-free file is
    `default`; an empty mode FIELD is what is unparseable).
    """

    def _run(self, sensors, tmp_path, existing, sensor_on):
        p = tmp_path / "mode.conf"
        if existing is not None:
            p.write_text(existing)
        written = {}

        def fake_run_command(cmd, description, stdin_data=None):
            if cmd[:2] == ["sudo", "tee"]:
                written["content"] = stdin_data
            elif cmd[:3] == ["sudo", "rm", "-f"]:
                written["removed"] = True
            return 0

        with patch.object(sensors, "DROPIN_PATH", str(p)), \
             patch.object(sensors, "run_command", fake_run_command):
            rc = sensors.apply_mode(sensor_on=sensor_on)
        return rc, written

    def test_unparseable_returns_unknown(self, sensors):
        """Content we don't recognise must report unknown, never a guess.

        Fixture changed from "[Service]\\nRestart=always\\n" by the
        independent spec suite below: a file with no mode field at all is
        provably mode-free and resolves to `default` (see
        TestModeFreeFileIsDefault). What is genuinely unparseable is a
        mode FIELD whose value cannot be read -- here the field is
        present and empty.
        """
        mode, form = sensors.parse_mode(
            "[Service]\nEnvironment=BROKKR_MODE=\n")
        assert mode == sensors.MODE_UNKNOWN

    def test_empty_execstart_reset_line_ignored(self, sensors):
        """The bare 'ExecStart=' reset line must not parse as a mode.

        Expectation changed from MODE_UNKNOWN by the independent spec
        suite below: the reset line carries no `--mode`, so the file sets
        no mode, and a file that sets no mode is `default`. (That the
        reset line alone is a broken unit is a WRITE post-condition --
        see TestCollapseToDefault -- not a parse result.)
        """
        mode, _ = sensors.parse_mode("[Service]\nExecStart=\n")
        assert mode == sensors.MODE_DEFAULT

    def test_unknown_mode_refuses(self, sensors, tmp_path):
        """An unrecognised drop-in must be refused, not overwritten.

        Fixture changed from "[Service]\\nRestart=always\\n" for the same
        reason as TestParseMode.test_unparseable_returns_unknown: a file
        with no mode field is mode-free, not unparseable.
        """
        rc, w = self._run(
            sensors, tmp_path, "[Service]\nEnvironment=BROKKR_MODE=\n",
            sensor_on=False)
        assert rc != 0
        assert "content" not in w and "removed" not in w

    def test_dry_run_refusal_matches_the_real_run(self, sensors, tmp_path,
                                                  capsys):
        """A dry run that says REFUSE must refuse, not print a full sequence.

        The point of --dry-run is to sanity-check before doing it for real. On
        an unparseable drop-in the real path returns 1 and stops; the dry run
        printed REFUSE and then kept going, listing daemon-reload, the relay
        toggle and the start as if the run would complete, and returned 0.
        """
        config_file = tmp_path / "unit.toml"
        config_file.write_text("[relay]\npin = 17\nactive_high = false\n")
        dropin = tmp_path / "mode.conf"
        # An empty mode FIELD is unparseable. ("[Service]\nRestart=always\n",
        # the original fixture, is mode-free and now resolves to `default`.)
        dropin.write_text("[Service]\nEnvironment=BROKKR_MODE=\n")

        with patch.object(sensors, "DROPIN_PATH", str(dropin)):
            rc = sensors.run(["--off", "--dry-run"],
                             config_path=str(config_file))
        out = capsys.readouterr().out

        assert rc != 0, "dry run reported success for a case that hard-fails"
        assert "REFUSE" in out
        assert "daemon-reload" not in out
        assert "systemctl start" not in out


# =====================================================================
# HAM-184 / PR #101 -- independent spec suite
#
# Written from the spec alone, against the API as of 052ee1f, with no
# sight of the implementation. Two earlier attempts at this fix shipped
# regressions their own tests did not catch, so nothing below asserts a
# mechanism: every check is a property of the bytes on disk, the return
# code, or the recorded command log.
#
# THE FACT IT ALL TURNS ON. Brokkr resolves its mode CLI `--mode` > env
# `BROKKR_MODE` > `mode.toml` -- brokkr v0.4.0 brokkr/config/modehandler.py
# (config levels system < local < env < CLI, later wins); see also
# docs/sensors-usage.md.
# systemd drop-ins APPEND, so a drop-in's `ExecStart= --mode A` lands on
# the command line and therefore BEATS the same drop-in's
# `Environment=BROKKR_MODE=B`.
# =====================================================================

# A path that appears in no template anywhere in this repo. Content that
# was fabricated from a template instead of edited in place cannot carry
# it, so "the unit's own interpreter survived" is not satisfiable by
# accident.
CUSTOM_INTERPRETER = "/opt/custom/venv/bin/python3"

CUSTOM_EXECSTART_DROPIN = (
    "[Service]\n"
    "ExecStart=\n"
    "ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr "
    "--system hamma --mode {} start\n"
)


# --- The suite's own parser (never calls into sensors.py) -------------

ENV_ORACLE_RE = re.compile(
    r"^Environment\s*=\s*[\"']?BROKKR_MODE=(.*?)[\"']?$")
EXEC_ORACLE_RE = re.compile(r"^ExecStart\s*=")
MODE_FLAG_RE = re.compile(r"--mode\b(?:[=\s]+(\S+))?")


def _effective_lines(text):
    """Yield the [Service] lines systemd would act on: no blanks, no
    comments, no section headers, nothing from another section.

    systemd takes BOTH `#` and `;` as comment introducers.

    Parameters
    ----------
    text : str
        Raw drop-in contents.

    Yields
    ------
    str
        Each significant line, stripped.
    """
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped[0] in "#;":
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            # systemd matches section names case-sensitively and ignores
            # every assignment outside [Service] for these keys.
            section = stripped
            continue
        if section != "[Service]":
            continue
        yield stripped


def mode_directives(text):
    """Every mode assignment systemd would see, in file order.

    This is the test suite's own oracle. It deliberately does not call
    sensors.parse_mode, so a test cannot be fooled by the same parsing
    mistake it exists to catch.

    Parameters
    ----------
    text : str
        Raw drop-in contents.

    Returns
    -------
    list of tuple
        ``(form, value)`` pairs; form is "execstart" or "environment",
        value is the assigned token or None when the field is present
        but carries no readable value.
    """
    found = []
    for line in _effective_lines(text):
        env = ENV_ORACLE_RE.match(line)
        if env is not None:
            found.append(("environment", env.group(1).strip() or None))
            continue
        if EXEC_ORACLE_RE.match(line):
            for match in MODE_FLAG_RE.finditer(line):
                found.append(("execstart", match.group(1)))
    return found


def effective_mode(text):
    """The mode brokkr would run in, given this drop-in alone.

    CLI beats env (brokkr's precedence), and within one form systemd's
    LAST assignment wins.

    Parameters
    ----------
    text : str
        Raw drop-in contents.

    Returns
    -------
    str or None
        The resolved mode token, or None if the file sets no mode.
    """
    directives = mode_directives(text)
    execs = [value for form, value in directives if form == "execstart"]
    envs = [value for form, value in directives if form == "environment"]
    if execs:
        return execs[-1]
    if envs:
        return envs[-1]
    return None


def non_mode_lines(text):
    """Everything a mode rewrite is NOT allowed to change.

    Effective lines with the mode taken out: `Environment=BROKKR_MODE=`
    lines dropped entirely, `--mode <v>` excised from ExecStart lines,
    whitespace normalised. Two files that differ only in their mode
    produce equal lists, so comparing before against after is a
    "changed the mode and nothing else" check that does not have to
    enumerate what "else" might be.

    Parameters
    ----------
    text : str
        Raw drop-in contents.

    Returns
    -------
    list of str
    """
    kept = []
    for line in _effective_lines(text):
        if ENV_ORACLE_RE.match(line) is not None:
            continue
        if EXEC_ORACLE_RE.match(line):
            line = MODE_FLAG_RE.sub(" ", line)
        kept.append(" ".join(line.split()))
    return kept


def _argv(cmd):
    """Normalise a command to a list of str, shell string included."""
    if isinstance(cmd, str):
        return [cmd]
    return [str(part) for part in cmd]


class _Completed:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeSubprocess:
    """Stand-in for the subprocess module: records, never spawns.

    Every entry point is implemented, not just ``run``. A refusal that
    is supposed to happen before anything is touched must be checked
    against ALL the ways out of the process, or an implementation that
    reaches for ``check_call`` slips past the assertion.
    """

    DEVNULL = -3
    PIPE = -1
    STDOUT = -2

    def __init__(self, harness):
        self._harness = harness

    def run(self, cmd, **kwargs):
        return self._harness.dispatch(_argv(cmd), kwargs)

    def Popen(self, cmd, **kwargs):
        self._harness.record("Popen", _argv(cmd))
        return MagicMock(returncode=0)

    def call(self, cmd, **kwargs):  # noqa: F811 - subprocess.call
        self._harness.record("call", _argv(cmd))
        return 0

    def check_call(self, cmd, **kwargs):
        self._harness.record("check_call", _argv(cmd))
        return 0

    def check_output(self, cmd, **kwargs):
        self._harness.record("check_output", _argv(cmd))
        return ""


class DropinHarness:
    """A real mode.conf on disk, plus a fake for every way out.

    ``sudo tee``, ``sudo mv -f`` and ``sudo rm -f`` are applied to real files,
    so every assertion reads back bytes that actually landed and a
    rewrite that re-parses its own output wrongly is visible. A delete
    makes :meth:`read_back` return None rather than "" -- tests guard
    that explicitly so a deleted file fails with a message instead of
    passing vacuously or raising TypeError.

    Everything else -- systemctl, relay.py, ping, os.system -- is
    recorded and not run. ``calls`` is the whole record.

    Parameters
    ----------
    sensors : module
        The loaded sensors module to patch.
    tmp_path : pathlib.Path
        pytest tmp_path.
    content : str, optional
        Initial drop-in contents. Omit for "no drop-in".
    name : str, optional
        Subdirectory name, so one test can hold two harnesses.
    """

    def __init__(self, sensors, tmp_path, content=None, name="dropin"):
        self.sensors = sensors
        self.dir = tmp_path / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "mode.conf"
        self.telemetry = self.dir / "absent-telemetry"
        if content is not None:
            self.path.write_text(content)
        self.calls = []
        self._patches = []

    def read_back(self):
        """Return the drop-in's bytes, or None when it is not there."""
        if not self.path.is_file():
            return None
        return self.path.read_text()

    def ran(self, *tokens):
        """Recorded commands whose argv holds all of ``tokens``.

        Tokens match whole argv ELEMENTS, not substrings: ``ran("rm")``
        does not match a ``--rm`` flag. (This repo has shipped
        ``assert "abort" in cmd`` against an argv holding ``--abort``,
        which is always False and passes silently.)

        Parameters
        ----------
        *tokens : str
            argv elements that must all be present.

        Returns
        -------
        list of list of str
        """
        return [cmd for _via, cmd in self.calls
                if all(token in cmd for token in tokens)]

    def record(self, via, cmd):
        """Append a command to the log without running it."""
        self.calls.append((via, cmd))

    def dispatch(self, cmd, kwargs):
        """Log a command, then emulate only the file-touching ones."""
        self.record("run", cmd)
        if cmd[:2] == ["sudo", "tee"] and len(cmd) > 2:
            data = kwargs.get("input")
            if data is None:
                return _Completed(1, stderr="tee received no stdin")
            pathlib.Path(cmd[2]).write_text(data)
            return _Completed(0)
        if cmd[:3] == ["sudo", "mv", "-f"] and len(cmd) > 4:
            os.replace(cmd[3], cmd[4])
            return _Completed(0)
        if cmd[:3] == ["sudo", "rm", "-f"] and len(cmd) > 3:
            target = pathlib.Path(cmd[3])
            if target.exists():
                target.unlink()
            return _Completed(0)
        if cmd[:3] == ["sudo", "mkdir", "-p"] and len(cmd) > 3:
            pathlib.Path(cmd[3]).mkdir(parents=True, exist_ok=True)
            return _Completed(0)
        if cmd[:1] == ["ping"]:
            return _Completed(1)
        if "is-active" in cmd:
            return _Completed(0, stdout="active")
        return _Completed(0)

    def _os_system(self, command):
        self.record("os.system", _argv(command))
        return 0

    def _os_popen(self, command, *args, **kwargs):
        self.record("os.popen", _argv(command))
        return io.StringIO("")

    def __enter__(self):
        sensors = self.sensors
        self._patches = [
            patch.object(sensors, "DROPIN_PATH", str(self.path)),
            patch.object(sensors, "DROPIN_DIR", str(self.dir)),
            patch.object(sensors, "TELEMETRY_DIR", str(self.telemetry)),
            patch.object(sensors, "subprocess", _FakeSubprocess(self)),
            patch.object(sensors.os, "system", self._os_system),
            patch.object(sensors.os, "popen", self._os_popen),
        ]
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *exc_info):
        for item in reversed(self._patches):
            item.stop()
        return False


def write_run_configs(tmp_path):
    """Write the unit.toml and main.toml that run() needs.

    main.toml has no [steps.state_monitor], so no real notifier is ever
    built; tests that care about notifications patch build_sender.

    Parameters
    ----------
    tmp_path : pathlib.Path

    Returns
    -------
    tuple of (str, str)
        Paths to unit.toml and main.toml.
    """
    unit = tmp_path / "unit.toml"
    unit.write_text(textwrap.dedent("""\
        number = 3
        site_description = "Bench"
        [relay]
        pin = 17
        active_high = true
    """))
    main = tmp_path / "main.toml"
    main.write_text("[steps]\n")
    return str(unit), str(main)


# --- Fixture catalogue -----------------------------------------------
#
# Field shapes, per docs/sensors-usage.md and the live fleet: mj04 and
# mj43 carry the Environment form; mj06 and mj50 the ExecStart form,
# single space, sticky axis. Modes are chosen so that no expected token
# is a substring of the token it replaces -- `nosensor` IS a substring
# of `nosensor_nochargecontroller`, so substring assertions on mode
# tokens are unfalsifiable and the suite compares resolved modes for
# equality instead.

DUPLICATE_FIXTURES = {
    # Both valid forms in one file. ExecStart wins per brokkr, so the
    # Environment line is dead weight that contradicts it.
    "dup-both-forms": (
        CUSTOM_EXECSTART_DROPIN.format("nochargecontroller")
        + "Environment=BROKKR_MODE=nosensor\n"),
    # systemd applies the LAST Environment= assignment; a first-match
    # parser reads the first.
    "dup-two-environment": (
        "[Service]\n"
        "Environment=BROKKR_MODE=nosensor\n"
        "Environment=BROKKR_MODE=nochargecontroller\n"),
    # Two --mode flags on one command line; brokkr's argparse takes the
    # last, a non-greedy regex takes the first.
    "dup-two-mode-flags": (
        "[Service]\n"
        "ExecStart=\n"
        "ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr --system hamma "
        "--mode nosensor --mode nochargecontroller start\n"),
}

UNPARSEABLE_FIXTURES = {
    # brokkr's argparse takes `--mode x`, not `--mode=x`; the shape is
    # close enough to be a plausible hand-edit and must not be guessed.
    "bad-mode-equals": (
        "[Service]\n"
        "ExecStart=\n"
        "ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr --system hamma "
        "--mode=nosensor start\n"),
    # A systemd line continuation: the mode is on a physical line that
    # does not itself begin with ExecStart=.
    "bad-backslash-continuation": (
        "[Service]\n"
        "ExecStart=\n"
        "ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr \\\n"
        "    --system hamma --mode nosensor start\n"),
    # Field present, value empty.
    "bad-environment-no-value": "[Service]\nEnvironment=BROKKR_MODE=\n",
    # systemd has no trailing comments, so the value is literally
    # "nosensor # old" -- not a mode, and not safely strippable.
    "bad-environment-trailing-comment": (
        "[Service]\nEnvironment=BROKKR_MODE=nosensor # old\n"),
}

# config/mode.toml really does define these three alongside the four
# compositions. They parse cleanly but decompose to neither axis, so an
# --on would compute `default` and discard whatever the operator set.
UNMODELED_FIXTURES = {
    "unmodeled-test-execstart": CUSTOM_EXECSTART_DROPIN.format("test"),
    "unmodeled-realtime-environment": ENVIRONMENT_DROPIN.format("realtime"),
    "unmodeled-sindri02x-execstart": (
        CUSTOM_EXECSTART_DROPIN.format("sindri02x")),
}

REFUSE_FIXTURES = {}
REFUSE_FIXTURES.update(DUPLICATE_FIXTURES)
REFUSE_FIXTURES.update(UNPARSEABLE_FIXTURES)
REFUSE_FIXTURES.update(UNMODELED_FIXTURES)

# (content, sensor_on, expected resolved mode after the write)
WRITE_FIXTURES = {
    "write-execstart-form": (
        CUSTOM_EXECSTART_DROPIN.format("nochargecontroller"), False,
        "nosensor_nochargecontroller"),
    "write-environment-form": (
        ENVIRONMENT_DROPIN.format("nochargecontroller"), False,
        "nosensor_nochargecontroller"),
    "write-mode-free-file": (
        "[Service]\nRestart=always\nTimeoutStopSec=90\n", False,
        "nosensor"),
}

COLLAPSE_FIXTURES = {
    "collapse-execstart-only": CUSTOM_EXECSTART_DROPIN.format("nosensor"),
    "collapse-execstart-with-neighbours": (
        CUSTOM_EXECSTART_DROPIN.format("nosensor")
        + "Restart=always\nTimeoutStopSec=90\n"),
    "collapse-environment-only": ENVIRONMENT_DROPIN.format("nosensor"),
    "collapse-environment-with-neighbours": (
        "[Service]\n"
        "Environment=BROKKR_MODE=nosensor\n"
        "Restart=always\nTimeoutStopSec=90\n"),
}

# Everything above, for the properties that must hold across the whole
# input space rather than for one shape.
ALL_FIXTURES = {}
ALL_FIXTURES.update(REFUSE_FIXTURES)
ALL_FIXTURES.update(COLLAPSE_FIXTURES)
ALL_FIXTURES.update(
    {label: spec[0] for label, spec in WRITE_FIXTURES.items()})


class TestTheSuitesOwnOracle:
    """The oracle the rest of the suite leans on, checked on its own.

    If `effective_mode` were wrong, every assertion built on it would be
    wrong in the same direction and nothing would show it.
    """

    def test_execstart_beats_environment(self):
        """Both forms present: the ExecStart mode is the effective one."""
        content = DUPLICATE_FIXTURES["dup-both-forms"]
        assert effective_mode(content) == "nochargecontroller"

    def test_last_environment_assignment_wins(self):
        """Repeated Environment=: systemd applies the last one."""
        content = DUPLICATE_FIXTURES["dup-two-environment"]
        assert effective_mode(content) == "nochargecontroller"

    def test_last_mode_flag_on_a_line_wins(self):
        """Repeated --mode on one ExecStart: argparse takes the last."""
        content = DUPLICATE_FIXTURES["dup-two-mode-flags"]
        assert effective_mode(content) == "nochargecontroller"

    def test_comments_hold_no_directives(self):
        """`#` and `;` lines are comments, so they assign nothing."""
        content = (
            "[Service]\n"
            "# Environment=BROKKR_MODE=nosensor\n"
            "; ExecStart=/x -m brokkr --mode nochargecontroller\n"
            "Restart=always\n")
        assert mode_directives(content) == []
        assert effective_mode(content) is None

    def test_mode_free_file_has_no_effective_mode(self):
        """A file with neither field resolves to no mode at all."""
        assert effective_mode(
            "[Service]\nRestart=always\n") is None

    def test_bare_reset_line_assigns_no_mode(self):
        """`ExecStart=` with no replacement carries no --mode."""
        assert mode_directives("[Service]\nExecStart=\n") == []

    def test_non_mode_lines_ignores_only_the_mode(self):
        """Two files differing only in mode have equal non_mode_lines.

        And a file differing in a neighbour does NOT -- the second
        assertion is what makes the first falsifiable.
        """
        one = CUSTOM_EXECSTART_DROPIN.format("nosensor")
        two = CUSTOM_EXECSTART_DROPIN.format("nochargecontroller")
        assert non_mode_lines(one) == non_mode_lines(two)
        assert non_mode_lines(one) != non_mode_lines(
            one + "Restart=always\n")

    def test_counts_the_mode_token_whatever_separates_it(self):
        """`--mode x`, `--mode  x`, `--mode\\tx`, `--mode=x` all count."""
        for gap in (" ", "  ", "\t", " \t ", "="):
            line = ("[Service]\nExecStart=/x -m brokkr --mode"
                    + gap + "nosensor start\n")
            assert effective_mode(line) == "nosensor", repr(gap)


class TestHarnessRoundTrip:
    """The harness itself, so no later assertion is vacuous.

    A fake that silently failed to write, or that returned "" for a
    deleted file, would make half this suite pass for free.
    """

    def test_tee_lands_on_disk_and_is_readable(self, sensors, tmp_path):
        """A `sudo tee` is visible to the next read of the real file."""
        harness = DropinHarness(sensors, tmp_path, "old\n")
        with harness:
            rc = sensors.write_dropin("new\n", "nosensor")
        assert rc == 0
        assert harness.read_back() == "new\n"

    def test_rm_makes_read_back_none_not_empty(self, sensors, tmp_path):
        """A delete reads back as None, so `is not None` can catch it."""
        harness = DropinHarness(sensors, tmp_path, "old\n")
        with harness:
            # remove_dropin() lost its (existing, form) params: the
            # delete decision is now proven upstream by plan_mode().
            rc = sensors.remove_dropin()
        assert rc == 0
        assert harness.read_back() is None

    def test_every_route_out_of_process_is_recorded(
            self, sensors, tmp_path):
        """run/Popen/call/check_call/check_output/os.system all log."""
        harness = DropinHarness(sensors, tmp_path)
        with harness:
            sensors.subprocess.run(["a"])
            sensors.subprocess.Popen(["b"])
            sensors.subprocess.call(["c"])
            sensors.subprocess.check_call(["d"])
            sensors.subprocess.check_output(["e"])
            sensors.os.system("f")
        assert [via for via, _cmd in harness.calls] == [
            "run", "Popen", "call", "check_call", "check_output",
            "os.system"]

    def test_a_real_service_command_is_logged_not_run(
            self, sensors, tmp_path):
        """stop_brokkr is captured; `ran` matches whole argv elements."""
        harness = DropinHarness(sensors, tmp_path)
        with harness:
            rc = sensors.stop_brokkr()
        assert rc == 0
        assert harness.ran("systemctl", "stop") != []
        assert harness.ran("systemctl", "start") == []


# --- Spec clause 1: precedence ---------------------------------------

class TestModePrecedence:
    """Resolution follows brokkr's precedence, not the file's order.

    brokkr: CLI `--mode` > `BROKKR_MODE` > mode.toml. Drop-ins append,
    so a drop-in's ExecStart `--mode` reaches the command line and beats
    the same file's `Environment=BROKKR_MODE=`.
    """

    def test_execstart_mode_beats_environment_mode(self, sensors):
        """parse_mode returns the ExecStart mode and the execstart form.

        A = nochargecontroller, B = nosensor. Neither token contains the
        other, so "A is reported" and "B is not" fail in opposite
        directions and neither can pass by accident.
        """
        content = DUPLICATE_FIXTURES["dup-both-forms"]
        mode, form = sensors.parse_mode(content)
        assert mode == "nochargecontroller"
        assert form == "execstart"

    def test_status_never_names_the_overridden_environment_mode(
            self, sensors, tmp_path):
        """--status does not report the losing Environment mode.

        Weaker than the clause above on purpose, and independent of how
        the fix chooses to represent a conflict: this holds whether the
        reported mode is `nochargecontroller` or some refusal marker,
        and fails only if `nosensor` -- the value the command line
        overrides -- is reported as the unit's mode.
        """
        content = DUPLICATE_FIXTURES["dup-both-forms"]
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            out = sensors.sensor_status(
                {"pin": 17, "active_high": False})
        match = re.search(r"^Brokkr mode: (\S+)$", out, re.MULTILINE)
        assert match is not None, (
            "status printed no 'Brokkr mode:' line:\n" + out)
        assert match.group(1) != "nosensor", (
            "status reports the Environment mode that the ExecStart "
            "command line overrides")


# --- Spec clause 2: a mode set twice is refused ----------------------

class TestDuplicateModeDirectivesRefused:
    """A mode named in more than one place is refused, not reconciled.

    Whichever one the script picks, it is guessing at which the operator
    meant, and a wrong guess rewrites the other one out of existence.
    """

    @pytest.mark.parametrize("sensor_on", [True, False])
    @pytest.mark.parametrize("label", sorted(DUPLICATE_FIXTURES))
    def test_apply_mode_refuses_and_leaves_the_bytes_alone(
            self, sensors, tmp_path, label, sensor_on):
        """apply_mode returns nonzero; the file is byte-identical.

        Both directions: a duplicate is equally unreadable to --on and
        to --off.
        """
        content = DUPLICATE_FIXTURES[label]
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=sensor_on)
        after = harness.read_back()
        assert rc != 0, "a duplicated mode was reconciled, not refused"
        assert after is not None, "the drop-in was deleted"
        assert after == content, "the drop-in was rewritten"
        assert harness.ran("tee") == []
        assert harness.ran("rm") == []


# --- Spec clause 3: an unparseable mode token is refused -------------

class TestUnparseableModeTokenRefused:
    """A mode field whose value cannot be read is refused.

    Distinct from clause 4: here the FIELD is present, so the file is
    plainly trying to set a mode and we cannot tell which.
    """

    @pytest.mark.parametrize("sensor_on", [True, False])
    @pytest.mark.parametrize("label", sorted(UNPARSEABLE_FIXTURES))
    def test_apply_mode_refuses_and_leaves_the_bytes_alone(
            self, sensors, tmp_path, label, sensor_on):
        """apply_mode returns nonzero; the file is byte-identical."""
        content = UNPARSEABLE_FIXTURES[label]
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=sensor_on)
        after = harness.read_back()
        assert rc != 0, "an unreadable mode token was overwritten"
        assert after is not None, "the drop-in was deleted"
        assert after == content, "the drop-in was rewritten"


# --- Spec clause 4: a mode-free file is default ----------------------

class TestModeFreeFileIsDefault:
    """A file that provably sets no mode is `default`, not `unknown`.

    Refusing here would strand any unit whose mode.conf holds only
    unrelated directives -- a file on/off has every right to add a mode
    to.
    """

    def test_directive_free_file_resolves_to_default(self, sensors):
        """No mode field anywhere: the mode is default."""
        content = "[Service]\nRestart=always\nTimeoutStopSec=90\n"
        assert mode_directives(content) == []
        mode, _form = sensors.parse_mode(content)
        assert mode == sensors.MODE_DEFAULT

    def test_hash_and_semicolon_comments_are_not_directives(
            self, sensors):
        """A mode named only inside a `#` or `;` comment does not count.

        systemd takes both characters as comment introducers, and the
        `;` half is the one a `#`-only parser gets wrong.
        """
        content = (
            "[Service]\n"
            "# Environment=BROKKR_MODE=nosensor (set 2026-01-01)\n"
            "; ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr "
            "--system hamma --mode nochargecontroller start\n"
            "Restart=always\n")
        assert mode_directives(content) == []
        mode, _form = sensors.parse_mode(content)
        assert mode == sensors.MODE_DEFAULT

    def test_mode_free_file_gains_a_mode_and_keeps_its_directives(
            self, sensors, tmp_path):
        """--off writes nosensor into it without losing a neighbour."""
        content = "[Service]\nRestart=always\nTimeoutStopSec=90\n"
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=False)
        after = harness.read_back()
        assert rc == 0, "a mode-free drop-in was refused"
        assert after is not None, "the drop-in was deleted"
        assert effective_mode(after) == "nosensor"
        assert non_mode_lines(after) == non_mode_lines(content)


# --- Spec clause 5: a write changes the mode and nothing else --------

class TestWriteChangesTheModeAndNothingElse:
    """What else is in mode.conf has to come out the other side.

    mode.conf is the shared filename for every override and is
    hand-edited in the field, so a rewrite that reconstructs the file
    from a template silently replaces whatever was there.
    """

    def test_execstart_write_keeps_the_units_own_interpreter(
            self, sensors, tmp_path):
        """--off retargets the mode; path, --system and neighbours stay.

        The interpreter is a path no template in this repo contains, so
        a rewrite that fabricated the line instead of editing it cannot
        pass.
        """
        content = (
            CUSTOM_EXECSTART_DROPIN.format("nochargecontroller")
            + "Restart=always\nTimeoutStopSec=90\n")
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=False)
        after = harness.read_back()
        assert rc == 0
        assert after is not None, "the drop-in was deleted"
        assert effective_mode(after) == "nosensor_nochargecontroller"
        assert CUSTOM_INTERPRETER in after
        assert "--system hamma" in after
        assert "Restart=always" in after
        assert "TimeoutStopSec=90" in after
        assert non_mode_lines(after) == non_mode_lines(content)

    def test_environment_write_keeps_coresident_directives(
            self, sensors, tmp_path):
        """--off on the Environment form keeps the other directives."""
        content = (
            "[Service]\n"
            "Environment=BROKKR_MODE=nochargecontroller\n"
            "Restart=always\n"
            "TimeoutStopSec=90\n")
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=False)
        after = harness.read_back()
        assert rc == 0
        assert after is not None, "the drop-in was deleted"
        assert effective_mode(after) == "nosensor_nochargecontroller"
        assert "Restart=always" in after
        assert "TimeoutStopSec=90" in after
        assert non_mode_lines(after) == non_mode_lines(content)

    def test_environment_write_keeps_a_mode_free_execstart_override(
            self, sensors, tmp_path):
        """The mode is in Environment=; a mode-free ExecStart survives.

        Only one mode directive, so this is a write, not a refusal. If
        the ExecStart override goes, the unit falls back to the parent
        unit's command line -- a different interpreter, and a different
        place for the mode to come from.
        """
        content = (
            "[Service]\n"
            "ExecStart=\n"
            "ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr "
            "--system hamma start\n"
            "Environment=BROKKR_MODE=nochargecontroller\n")
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=False)
        after = harness.read_back()
        assert rc == 0
        assert after is not None, "the drop-in was deleted"
        assert effective_mode(after) == "nosensor_nochargecontroller"
        assert CUSTOM_INTERPRETER in after
        assert non_mode_lines(after) == non_mode_lines(content)


# --- Spec clause 6: whitespace before the mode value -----------------

class TestWhitespaceBeforeTheModeValue:
    """systemd and argparse accept any whitespace run; so must we.

    A rewrite that looks for the literal "--mode <old>" finds nothing
    when the file uses two spaces or a tab, and then writes the file
    back unchanged while reporting success -- the worst outcome
    available, because the operator is told the mode changed.
    """

    WHITESPACE = [" ", "  ", "\t", " \t "]

    @pytest.mark.parametrize("gap", WHITESPACE,
                             ids=["space", "two-spaces", "tab", "mixed"])
    def test_retarget_tolerates_any_whitespace(
            self, sensors, tmp_path, gap):
        """--off reaches nosensor_nochargecontroller for every gap."""
        content = (
            "[Service]\n"
            "ExecStart=\n"
            "ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr "
            "--system hamma --mode" + gap + "nochargecontroller start\n")
        assert effective_mode(content) == "nochargecontroller"
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=False)
        after = harness.read_back()
        assert rc == 0
        assert after is not None, "the drop-in was deleted"
        assert effective_mode(after) == "nosensor_nochargecontroller", (
            "mode not retargeted with {!r} before the value".format(gap))
        assert CUSTOM_INTERPRETER in after

    @pytest.mark.parametrize("gap", WHITESPACE,
                             ids=["space", "two-spaces", "tab", "mixed"])
    def test_collapse_tolerates_any_whitespace(
            self, sensors, tmp_path, gap):
        """--on leaves no mode set, for every gap."""
        content = (
            "[Service]\n"
            "ExecStart=\n"
            "ExecStart=" + CUSTOM_INTERPRETER + " -m brokkr "
            "--system hamma --mode" + gap + "nosensor start\n")
        assert effective_mode(content) == "nosensor"
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=True)
        after = harness.read_back()
        assert rc == 0
        if after is not None:
            assert mode_directives(after) == [], (
                "a mode survived the collapse with {!r} before the "
                "value".format(gap))


# --- Spec clause 7: exactly one mode directive after a write ---------

class TestExactlyOneModeDirective:
    """After a write the file holds one mode assignment, no more.

    Two is the quiet failure: a first-match parser reads back the one it
    wrote and reports success, while systemd applies the other.
    """

    @pytest.mark.parametrize("label", sorted(WRITE_FIXTURES))
    def test_one_directive_naming_the_target(
            self, sensors, tmp_path, label):
        """Exactly one assignment, and it names the target mode."""
        content, sensor_on, expected = WRITE_FIXTURES[label]
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=sensor_on)
        after = harness.read_back()
        assert rc == 0
        assert after is not None, "the drop-in was deleted"
        assert len(mode_directives(after)) == 1, (
            "{} mode assignments in:\n{}".format(
                len(mode_directives(after)), after))
        assert effective_mode(after) == expected


# --- Spec clause 8: a collapse to default leaves no mode -------------

class TestCollapseToDefault:
    """--on to default removes every mode form, or the file.

    "Valid" has one extra condition: a drop-in whose only surviving
    ExecStart is the bare reset line clears brokkr's command line, and
    the unit will not start at all. That is worse than a wrong mode.
    """

    @pytest.mark.parametrize("label", sorted(COLLAPSE_FIXTURES))
    def test_no_mode_survives_and_no_lone_reset_is_left(
            self, sensors, tmp_path, label):
        """Afterwards: absent, or present, mode-free and startable."""
        content = COLLAPSE_FIXTURES[label]
        assert effective_mode(content) == "nosensor"
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=True)
        after = harness.read_back()
        assert rc == 0
        if after is None:
            assert harness.ran("rm") != [], (
                "the drop-in vanished but no rm was issued")
            return
        assert mode_directives(after) == [], (
            "a mode is still set:\n" + after)
        exec_lines = [line for line in _effective_lines(after)
                      if EXEC_ORACLE_RE.match(line)]
        if exec_lines:
            assert any(line.split("=", 1)[1].strip()
                       for line in exec_lines), (
                "only a bare ExecStart= reset survived, which clears "
                "brokkr's command line:\n" + after)


# --- Spec clause 9: a collapse preserves, or refuses -----------------

class TestCollapsePreservesRatherThanDeletes:
    """Deleting the file is never how a post-condition gets satisfied.

    The design review for power-state reconciliation named "could delete
    a custom mode.conf" as a real hazard, and a rejected attempt at this
    fix did exactly that when its own verification step failed.
    """

    @pytest.mark.parametrize("label", [
        "collapse-execstart-with-neighbours",
        "collapse-environment-with-neighbours",
    ])
    def test_neighbours_survive_and_nothing_is_deleted(
            self, sensors, tmp_path, label):
        """Collapse keeps the neighbours; no rm -f is issued."""
        content = COLLAPSE_FIXTURES[label]
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=True)
        after = harness.read_back()
        assert rc == 0
        assert harness.ran("rm") == [], (
            "the drop-in was deleted rather than rewritten")
        assert after is not None, "the drop-in is gone"
        assert "Restart=always" in after
        assert "TimeoutStopSec=90" in after
        assert mode_directives(after) == []

    def test_a_refused_collapse_changes_nothing(
            self, sensors, tmp_path):
        """--on on a file whose collapse cannot be proven correct.

        Stripping the Environment line here would leave a file that
        still sets a mode from its ExecStart, so there is no correct
        single-directive result to write. The outcome must be: refuse,
        and the bytes stay.
        """
        content = DUPLICATE_FIXTURES["dup-both-forms"]
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=True)
        after = harness.read_back()
        assert rc != 0, "a collapse with no correct result went ahead"
        assert after is not None, "the drop-in was deleted"
        assert after == content


# --- Spec clause 10: the sticky axis survives both transitions -------

class TestStickyAxisSurvivesBothTransitions:
    """nochargecontroller is hardware, not power state.

    mj06 and mj50 carry it in the ExecStart form; mj04 and mj43 use the
    Environment form. Dropping it starts a unit polling a charge
    controller that is not connected.
    """

    def test_execstart_form_round_trip(self, sensors, tmp_path):
        """mj06/mj50 shape: off then on, sticky axis at every step."""
        content = CUSTOM_EXECSTART_DROPIN.format("nochargecontroller")
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc_off = sensors.apply_mode(sensor_on=False)
            after_off = harness.read_back()
            rc_on = sensors.apply_mode(sensor_on=True)
            after_on = harness.read_back()
        assert (rc_off, rc_on) == (0, 0)
        assert after_off is not None, "--off deleted the drop-in"
        assert effective_mode(after_off) == "nosensor_nochargecontroller"
        assert after_on is not None, "--on deleted the drop-in"
        assert effective_mode(after_on) == "nochargecontroller"
        assert CUSTOM_INTERPRETER in after_on
        assert non_mode_lines(after_on) == non_mode_lines(content)

    def test_environment_form_round_trip(self, sensors, tmp_path):
        """mj04/mj43 shape: off then on, sticky axis at every step."""
        content = ENVIRONMENT_DROPIN.format("nochargecontroller")
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc_off = sensors.apply_mode(sensor_on=False)
            after_off = harness.read_back()
            rc_on = sensors.apply_mode(sensor_on=True)
            after_on = harness.read_back()
        assert (rc_off, rc_on) == (0, 0)
        assert after_off is not None, "--off deleted the drop-in"
        assert effective_mode(after_off) == "nosensor_nochargecontroller"
        assert after_on is not None, "--on deleted the drop-in"
        assert effective_mode(after_on) == "nochargecontroller"


# --- Spec clause 11: refuse before touching anything -----------------

class TestRefusalHappensBeforeAnythingIsTouched:
    """sensors.py is the emergency power tool: fail empty-handed -- except
    that a refused --off still cuts sensor power, and only that.

    A refusal after brokkr is stopped, or after the relay has moved,
    leaves the unit down with its mode still wrong -- the operator ran
    one command and got a half-transition they now have to unpick by
    hand, over cellular.
    """

    @pytest.mark.parametrize("flag", ["--on", "--off"])
    @pytest.mark.parametrize("label", sorted(REFUSE_FIXTURES))
    def test_no_command_runs_and_the_file_is_untouched(
            self, sensors, tmp_path, label, flag):
        """run() refuses with an empty command log and the same bytes.

        Not "no relay command": NO command, by any route out of the
        process (subprocess run/Popen/call/check_call/check_output, or
        os.system), since stopping a service is as destructive here as
        moving the relay.
        """
        content = REFUSE_FIXTURES[label]
        unit, main = write_run_configs(tmp_path)
        harness = DropinHarness(sensors, tmp_path, content)
        sender = MagicMock()
        with harness, patch.object(sensors, "build_sender",
                                   return_value=sender):
            rc = sensors.run([flag], config_path=unit,
                             main_toml_path=main)
        assert rc != 0, "{} was not refused".format(label)
        if flag == "--on":
            assert harness.calls == [], (
                "commands ran before the refusal: {}".format(harness.calls))
        else:
            # A refused --off still cuts sensor power -- that one relay
            # command and nothing else (PB, 2026-10-30).
            assert len(harness.calls) == 1, harness.calls
            via, cmd = harness.calls[0]
            assert cmd[0].endswith("relay.py") and "--pin" in cmd, cmd
        assert harness.read_back() == content, (
            "the drop-in changed despite the refusal")


# --- Spec clause 12: a refusal still notifies ------------------------

class TestRefusalReachesTheNotifier:
    """Nobody is watching the terminal on the VPS.

    The notification is how a refused on/off becomes visible at all, so
    a refusal that returns early past the notifier is a silent no-op as
    far as the team is concerned.
    """

    @pytest.mark.parametrize("label", sorted(REFUSE_FIXTURES))
    def test_a_refused_off_sends_one_failure_message(
            self, sensors, tmp_path, label):
        """One send(), and the message says the turn-off failed."""
        content = REFUSE_FIXTURES[label]
        unit, main = write_run_configs(tmp_path)
        harness = DropinHarness(sensors, tmp_path, content)
        sender = MagicMock()
        with harness, patch.object(sensors, "build_sender",
                                   return_value=sender):
            rc = sensors.run(["--off"], config_path=unit,
                             main_toml_path=main)
        assert rc != 0, "{} was not refused".format(label)
        assert sender.send.call_count == 1, (
            "a refusal must still be announced; send() called {} "
            "times".format(sender.send.call_count))
        message = sender.send.call_args[0][0]
        assert "FAILED" in message
        assert "Mjolnir03" in message
        # Why it failed, not just that it did: run()'s own pre-flight puts
        # the refusal summary in the message.
        assert "REFUSED: " in message, message


# --- Spec clause 13: --dry-run refuses what the real run refuses -----

class TestDryRunMatchesTheRealRun:
    """A dry run exists to be trusted before the real one.

    Printing a sequence that completes, for a case that hard-fails,
    makes the check worse than not running it.
    """

    @pytest.mark.parametrize("flag", ["--on", "--off"])
    @pytest.mark.parametrize("label", sorted(REFUSE_FIXTURES))
    def test_dry_run_refuses_and_stops(
            self, sensors, tmp_path, label, flag, capsys):
        """Nonzero, and the tail of a successful run is not printed."""
        content = REFUSE_FIXTURES[label]
        unit, main = write_run_configs(tmp_path)
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.run([flag, "--dry-run"], config_path=unit,
                             main_toml_path=main)
        out = capsys.readouterr().out
        assert rc != 0, (
            "dry run reported success for a refused case: " + label)
        assert "daemon-reload" not in out, (
            "dry run printed a sequence that completes:\n" + out)
        assert "systemctl start" not in out, (
            "dry run printed a sequence that completes:\n" + out)
        assert harness.read_back() == content

    @pytest.mark.parametrize("flag", ["--on", "--off"])
    @pytest.mark.parametrize("label", sorted(ALL_FIXTURES))
    def test_dry_run_and_real_run_agree_on_every_fixture(
            self, sensors, tmp_path, label, flag):
        """The two paths refuse the same inputs, fixture by fixture.

        Catches drift in either direction: a dry run that waves through
        what the real run rejects, and a dry run that refuses what the
        real run would have done.
        """
        content = ALL_FIXTURES[label]
        unit, main = write_run_configs(tmp_path)
        dry_harness = DropinHarness(sensors, tmp_path, content,
                                    name="dry")
        with dry_harness:
            dry_rc = sensors.run([flag, "--dry-run"], config_path=unit,
                                 main_toml_path=main)
        real_harness = DropinHarness(sensors, tmp_path, content,
                                     name="real")
        sender = MagicMock()
        with real_harness, patch.object(sensors, "build_sender",
                                        return_value=sender):
            real_rc = sensors.run([flag], config_path=unit,
                                  main_toml_path=main)
        assert (dry_rc != 0) == (real_rc != 0), (
            "{} {}: dry-run rc={}, real rc={}".format(
                label, flag, dry_rc, real_rc))


# --- Spec clause 14: the mode.toml presets are refused ---------------

class TestUnmodeledPresetModesRefused:
    """`test`, `realtime` and `sindri02x` are outside the two axes.

    All three are real presets in config/mode.toml. They parse cleanly,
    so an unknown-mode check never fires, but they decompose to neither
    axis -- so --on computes `default` and throws the preset away.
    """

    @pytest.mark.parametrize("sensor_on", [True, False])
    @pytest.mark.parametrize("mode", ["test", "realtime", "sindri02x"])
    @pytest.mark.parametrize("form", ["execstart", "environment"])
    def test_refused_in_both_forms_and_both_directions(
            self, sensors, tmp_path, form, mode, sensor_on):
        """apply_mode returns nonzero and the file is byte-identical."""
        content = (CUSTOM_EXECSTART_DROPIN.format(mode)
                   if form == "execstart"
                   else ENVIRONMENT_DROPIN.format(mode))
        harness = DropinHarness(sensors, tmp_path, content)
        with harness:
            rc = sensors.apply_mode(sensor_on=sensor_on)
        after = harness.read_back()
        assert rc != 0, "{} {} was rewritten".format(mode, form)
        assert after is not None, "the drop-in was deleted"
        assert after == content
