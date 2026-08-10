"""Load the *real* brokkr package (from a sibling checkout) inside tests.

Why this exists
---------------
`plugins/state_monitor.py` resolves the mj-side DATA drives by calling
`brokkr.utils.output.find_drives`.  Every existing test in this directory
`MagicMock`s `brokkr` into `sys.modules` before loading the plugin, so that
function can never run: the tests exercise a *test-local reimplementation* of
it instead.  Two of the bugs that motivated the drive checks live precisely in
the code that reimplementation skips -- the `.format()` of
`/media/{current_user}` and the `is_dir() / ismount()` filter.

This module imports brokkr for real, without installing it, so those paths are
actually executed.

How it works
------------
brokkr cannot simply be imported: `brokkr.utils.output` pulls in
`brokkr.config.main` at module scope, which *reads and writes* config files
under `~/.config/brokkr` at import time.  So the import is done with:

* ``HOME`` pointed at a throwaway directory, so those writes land in a temp
  dir and never touch the developer's real ``~/.config/brokkr``;
* ``BROKKR_SYSTEM_PATH`` pinned to this repo, because brokkr otherwise resolves
  its "system path" to the *current working directory* -- which silently makes
  every config value depend on where pytest was invoked from.  Pinning it also
  means the tests load this repo's **real** ``config/main.toml``, so a test can
  assert against the actual deployed ``drive_glob``.

Both environment variables are restored immediately after the import; brokkr
captures them at import time only (``brokkr.constants.CONFIG_PATH_XDG`` is a
module-level ``Path("~/.config").expanduser()``), so restoring is safe and
keeps the rest of the suite -- including tests that spawn subprocesses --
unaffected.

Availability
------------
``require()`` skips the module cleanly when no brokkr source can be found, so
a checkout without the sibling repo still runs green.  Set
``HAMMA_REQUIRE_REAL_BROKKR=1`` (CI should) to turn that skip into a hard
failure, so this coverage cannot quietly disappear.
"""

import atexit
import contextlib
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Modules we want the *real* implementation of.
_REAL_MODULES = (
    "brokkr",
    "brokkr.constants",
    "brokkr.utils",
    "brokkr.utils.misc",
    "brokkr.utils.output",
    "brokkr.config",
    "brokkr.config.main",
    "brokkr.config.metadata",
    "brokkr.config.unit",
    "brokkr.pipeline",
    "brokkr.pipeline.base",
)

#: Real if their optional deps are installed, stubbed otherwise.
#: `brokkr.pipeline.decode` needs `simpleeval`.
_OPTIONAL_MODULES = ("brokkr.pipeline.decode",)

#: Always stubbed.  `notifiers` is the alert transport, not the code under
#: test, and the name collides with an unrelated PyPI package -- importing
#: whatever happens to be installed reproduces the fleet's
#: "ImportError: cannot import name 'Notifier'" crash instead of testing
#: anything.  Stubbing is deliberate, not incidental.
_STUBBED_MODULES = ("notifiers",)

_state = {"loaded": False, "reason": None, "modules": {}}


def _find_brokkr_src():
    """Return the path to put on sys.path, or None if brokkr is unavailable.

    Order: explicit override, sibling checkout, already-installed brokkr.
    """
    override = os.environ.get("BROKKR_SRC")
    if override:
        if (Path(override) / "brokkr" / "__init__.py").is_file():
            return Path(override)
        raise RuntimeError(
            "BROKKR_SRC={!r} does not contain a brokkr package".format(override))

    # Walk up rather than assuming a fixed depth: feature work in this repo
    # happens in git worktrees (.claude/worktrees/<agent>/, .worktrees/<name>/),
    # where REPO_ROOT.parent is NOT the directory holding the sibling checkouts.
    for ancestor in [REPO_ROOT] + list(REPO_ROOT.parents):
        sibling = ancestor.parent / "brokkr" / "src"
        if (sibling / "brokkr" / "__init__.py").is_file():
            return sibling

    if importlib.util.find_spec("brokkr") is not None:
        return None   # installed; nothing to add to sys.path
    raise RuntimeError(
        "no brokkr source found (looked at $BROKKR_SRC, {}, and the "
        "installed packages)".format(sibling))


def _load():
    """Import the real brokkr modules once, with HOME/system path sandboxed."""
    if _state["loaded"] or _state["reason"]:
        return

    try:
        src = _find_brokkr_src()
    except RuntimeError as exc:
        _state["reason"] = str(exc)
        return

    if "brokkr.constants" in sys.modules:
        # Something already imported brokkr with the real HOME; its config
        # paths are already fixed and we cannot retroactively sandbox them.
        _state["reason"] = (
            "brokkr was already imported before the sandbox could be set up")
        return

    if src is not None:
        sys.path.insert(0, str(src))

    sandbox_home = tempfile.mkdtemp(prefix="brokkr_test_home_")
    atexit.register(shutil.rmtree, sandbox_home, True)

    saved = {key: os.environ.get(key)
             for key in ("HOME", "BROKKR_SYSTEM_PATH", "BROKKR_SYSTEM",
                         "SUDO_USER")}
    os.environ["HOME"] = sandbox_home
    os.environ["BROKKR_SYSTEM_PATH"] = str(REPO_ROOT)
    os.environ.pop("BROKKR_SYSTEM", None)
    os.environ.pop("SUDO_USER", None)   # brokkr resolves "~" through this
    try:
        modules = {name: importlib.import_module(name)
                   for name in _REAL_MODULES}
    except Exception as exc:                       # pragma: no cover
        _state["reason"] = "importing real brokkr failed: {}: {}".format(
            type(exc).__name__, exc)
        return
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    for name in _OPTIONAL_MODULES:
        try:
            modules[name] = importlib.import_module(name)
        except Exception:
            modules[name] = MagicMock()
    for name in _STUBBED_MODULES:
        modules[name] = MagicMock()

    _state["modules"] = modules
    _state["loaded"] = True


def require():
    """Return the real brokkr module map, or skip/fail if it is unavailable."""
    _load()
    if _state["loaded"]:
        return _state["modules"]
    message = "real brokkr unavailable: {}".format(_state["reason"])
    if os.environ.get("HAMMA_REQUIRE_REAL_BROKKR") == "1":
        raise RuntimeError(message)
    pytest.skip(message, allow_module_level=True)


def load_plugin(plugin_name="state_monitor", output_step=None):
    """Load a repo plugin with the *real* brokkr bound to its module globals.

    The plugin does ``import brokkr.utils.output`` at module scope, so whatever
    is in ``sys.modules`` at exec time is what its global ``brokkr`` name
    resolves to for the life of the module.  That is why loading has to happen
    against the real package rather than patching afterwards.

    Parameters
    ----------
    plugin_name : str
        Stem of the file in ``plugins/``.
    output_step : type, optional
        Replacement for ``brokkr.pipeline.base.OutputStep``.  Defaults to the
        real class; pass a stub if a test wants a trivial constructor.
    """
    modules = require()
    path = REPO_ROOT / "plugins" / "{}.py".format(plugin_name)

    injected = dict(modules)
    if output_step is not None:
        base = MagicMock()
        base.OutputStep = output_step
        injected["brokkr.pipeline.base"] = base

    saved = {name: sys.modules.get(name) for name in injected}
    sys.modules.update(injected)
    try:
        spec = importlib.util.spec_from_file_location(
            "_real_{}".format(plugin_name), str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    return module


@contextlib.contextmanager
def mountpoints(paths):
    """Make `paths` report as mountpoints to `os.path.ismount`.

    Creating a real mount needs privileges that a test runner does not
    reliably have, and `os.path.ismount` is a pure predicate over the
    filesystem -- `find_drives` does not care *how* the answer is produced.
    Everything else in the path stays real: real directories, real
    `Path.glob`, real `Path.is_dir`, real `str.format`.  Those are where the
    bugs were; the privilege boundary is not.

    (A genuinely real mount is possible on macOS via `hdiutil attach
    -mountpoint`, and on Linux CI via `sudo mount -t tmpfs`.  See
    `real_mountpoint` below -- it is opt-in because it is slow and
    platform-specific.)
    """
    wanted = {os.path.realpath(str(path)) for path in paths}
    real_ismount = os.path.ismount

    def ismount(path):
        return os.path.realpath(str(path)) in wanted or real_ismount(path)

    saved = os.path.ismount
    os.path.ismount = ismount
    try:
        yield
    finally:
        os.path.ismount = saved


@contextlib.contextmanager
def real_mountpoint(path, size_mb=8):
    """Mount a real (tiny) filesystem at `path`. Opt-in; needs tooling.

    macOS: rootless via `hdiutil`.  Linux: needs passwordless sudo, which
    GitHub Actions runners provide.  Skips if neither is available.
    """
    import subprocess

    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        image = Path(tempfile.mkdtemp(prefix="hamma_vol_")) / "vol.dmg"
        subprocess.run(
            ["hdiutil", "create", "-size", "{}m".format(size_mb), "-fs", "HFS+",
             "-volname", path.name, "-quiet", str(image)], check=True)
        subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-quiet",
             "-mountpoint", str(path), str(image)], check=True)
        try:
            yield path
        finally:
            subprocess.run(["hdiutil", "detach", "-quiet", str(path)],
                           check=False)
            shutil.rmtree(image.parent, ignore_errors=True)
    elif sys.platform.startswith("linux"):
        if shutil.which("sudo") is None:
            pytest.skip("real mounts on Linux need sudo")
        rc = subprocess.run(
            ["sudo", "-n", "mount", "-t", "tmpfs", "-o",
             "size={}m".format(size_mb), "tmpfs", str(path)]).returncode
        if rc != 0:
            pytest.skip("passwordless sudo mount unavailable")
        try:
            yield path
        finally:
            subprocess.run(["sudo", "-n", "umount", str(path)], check=False)
    else:                                            # pragma: no cover
        pytest.skip("no real-mount support on {}".format(sys.platform))
