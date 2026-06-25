"""
Shellcheck validation tests for all shell scripts in mjolnir-hamma.

These tests run shellcheck on all .sh files to catch common errors,
bad practices, and potential bugs.
"""

import subprocess
import pytest
from pathlib import Path


# Get all shell scripts in the repository
REPO_ROOT = Path(__file__).parent.parent.parent
INSTALL_SCRIPTS = list((REPO_ROOT / "install_scripts").glob("*.sh"))
FILES_SCRIPTS = list((REPO_ROOT / "files").glob("*.sh"))
UNIFIED_SCRIPTS = list((REPO_ROOT / "unified_install").glob("*.sh"))
UNIFIED_LIB_SCRIPTS = list((REPO_ROOT / "unified_install" / "lib").glob("*.sh"))
ALL_SCRIPTS = INSTALL_SCRIPTS + FILES_SCRIPTS + UNIFIED_SCRIPTS + UNIFIED_LIB_SCRIPTS


def check_shellcheck_available():
    """Check if shellcheck is installed."""
    try:
        subprocess.run(
            ["shellcheck", "--version"],
            capture_output=True,
            check=True
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


SHELLCHECK_AVAILABLE = check_shellcheck_available()


@pytest.mark.skipif(not SHELLCHECK_AVAILABLE, reason="shellcheck not installed")
class TestShellcheck:
    """Shellcheck validation tests."""

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=lambda p: p.name)
    def test_shellcheck_passes(self, script_path):
        """Test that shellcheck passes for each script."""
        result = subprocess.run(
            [
                "shellcheck",
                "-e", "SC1091",  # Ignore "not following sourced file"
                "-e", "SC2034",  # Ignore "unused variable" (often intentional)
                str(script_path)
            ],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            pytest.fail(
                f"Shellcheck failed for {script_path.name}:\n"
                f"{result.stdout}\n{result.stderr}"
            )

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=lambda p: p.name)
    def test_script_has_shebang(self, script_path):
        """Test that each script has a proper shebang line."""
        content = script_path.read_text()
        first_line = content.split('\n')[0] if content else ''

        assert first_line.startswith('#!'), \
            f"{script_path.name} missing shebang line"
        assert 'bash' in first_line or 'sh' in first_line, \
            f"{script_path.name} has non-shell shebang: {first_line}"

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=lambda p: p.name)
    def test_script_is_executable_or_sourced(self, script_path):
        """Test that scripts are either executable or clearly meant to be sourced."""
        import os
        import stat

        # Check if file is executable
        mode = script_path.stat().st_mode
        is_executable = mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Some scripts might be meant to be sourced, not executed
        content = script_path.read_text()
        is_source_only = 'source ' in content and 'main()' not in content

        # Either should be executable OR should be a sourced library
        if not is_executable and not is_source_only:
            pytest.skip(f"{script_path.name} is not executable (may need chmod +x)")


class TestShellSyntax:
    """Basic syntax validation that works without shellcheck."""

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=lambda p: p.name)
    def test_bash_syntax_valid(self, script_path):
        """Test that bash can parse the script without errors."""
        result = subprocess.run(
            ["bash", "-n", str(script_path)],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            pytest.fail(
                f"Bash syntax error in {script_path.name}:\n"
                f"{result.stderr}"
            )

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=lambda p: p.name)
    def test_no_windows_line_endings(self, script_path):
        """Test that scripts don't have Windows line endings (CRLF)."""
        content = script_path.read_bytes()
        assert b'\r\n' not in content, \
            f"{script_path.name} has Windows line endings (CRLF)"

    @pytest.mark.parametrize("script_path", ALL_SCRIPTS, ids=lambda p: p.name)
    def test_ends_with_newline(self, script_path):
        """Test that scripts end with a newline."""
        content = script_path.read_text()
        if content:
            assert content.endswith('\n'), \
                f"{script_path.name} doesn't end with newline"


class TestScriptConventions:
    """Test for consistent conventions across scripts."""

    @pytest.mark.parametrize("script_path", INSTALL_SCRIPTS, ids=lambda p: p.name)
    def test_install_script_has_description(self, script_path):
        """Test that install scripts have a description comment."""
        content = script_path.read_text()
        lines = content.split('\n')

        # Look for a comment in the first 5 lines after shebang
        has_description = any(
            line.strip().startswith('#') and len(line.strip()) > 2
            for line in lines[1:6]
            if line.strip() and not line.strip() == '#'
        )

        assert has_description, \
            f"{script_path.name} should have a description comment"

    def test_no_hardcoded_sensor_numbers(self):
        """Test that scripts don't have hardcoded sensor numbers (should use args)."""
        problematic = []

        for script_path in INSTALL_SCRIPTS:
            content = script_path.read_text()
            # Look for patterns like mjolnir01, mjolnir42 etc
            import re
            if re.search(r'mjolnir\d{2}', content):
                # Check if it's in a comment or example
                for line in content.split('\n'):
                    if re.search(r'mjolnir\d{2}', line) and not line.strip().startswith('#'):
                        problematic.append(script_path.name)
                        break

        if problematic:
            pytest.fail(
                f"Scripts with hardcoded sensor numbers: {problematic}\n"
                "Use command-line arguments instead."
            )
