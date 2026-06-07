"""
    Tests for :class:`UbuntuConfigurationManager`.

    Tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    All file I/O (``cat`` / ``tee`` / ``cp``) is exercised entirely
    through the runner.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.configurationmanagement import (
    UbuntuConfigActionStatus,
    UbuntuConfigManagerConfig,
    UbuntuConfigQueryError,
    UbuntuConfigurationManager,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)

    def run(
        self,
        args: list,
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult:
        call_record = {
            "args": list(args),
            "elevated": elevated,
            "stdin": stdin,
            "env": env,
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _ok(stdout: str = "", stderr: str = "") -> RunResult:
    """
        Build a successful :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr=stderr)
    return rtnval


def _fail(
    stdout: str = "", stderr: str = "", exit_code: int = 1,
) -> RunResult:
    """
        Build a failure :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


# ----------------------------------------------------------------------
# read_setting / list_settings
# ----------------------------------------------------------------------


def test_read_setting_parses_shell_value():
    """
        read_setting returns the unquoted value parsed from a shell-style
        configuration file.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/default/grub")] = _ok(
        stdout=(
            "# top comment\n"
            'GRUB_TIMEOUT="5"\n'
            'GRUB_DEFAULT="0"\n'
        ),
    )

    manager = UbuntuConfigurationManager(
        runner=runner, config=UbuntuConfigManagerConfig(),
    )
    info = manager.read_setting(file="/etc/default/grub", key="GRUB_TIMEOUT")

    testbase.assert_equal(info.key, "GRUB_TIMEOUT")
    testbase.assert_equal(info.value, "5")
    testbase.assert_equal(info.file, "/etc/default/grub")
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


def test_read_setting_missing_source_raises():
    """
        read_setting raises UbuntuConfigQueryError when the file is
        absent.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/default/grub")] = _fail()

    manager = UbuntuConfigurationManager(
        runner=runner, config=UbuntuConfigManagerConfig(),
    )
    raised = False
    try:
        manager.read_setting(file="/etc/default/grub", key="X")
    except UbuntuConfigQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_read_setting_missing_key_raises():
    """
        read_setting raises UbuntuConfigQueryError when the key is not
        present.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/default/grub")] = _ok(
        stdout='GRUB_TIMEOUT="5"\n',
    )

    manager = UbuntuConfigurationManager(
        runner=runner, config=UbuntuConfigManagerConfig(),
    )
    raised = False
    try:
        manager.read_setting(file="/etc/default/grub", key="MISSING")
    except UbuntuConfigQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_list_settings_returns_dict():
    """
        list_settings returns every parsed entry as a dict.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/default/grub")] = _ok(
        stdout=(
            "# header\n"
            'GRUB_TIMEOUT="5"\n'
            'GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"\n'
            "\n"
        ),
    )

    manager = UbuntuConfigurationManager(
        runner=runner, config=UbuntuConfigManagerConfig(),
    )
    settings = manager.list_settings(file="/etc/default/grub")

    testbase.assert_equal(settings["GRUB_TIMEOUT"], "5")
    testbase.assert_equal(
        settings["GRUB_CMDLINE_LINUX_DEFAULT"], "quiet splash",
    )
    testbase.assert_equal(len(settings), 2)
    return


def test_list_settings_empty_when_file_missing():
    """
        list_settings returns an empty dict when the file is absent.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/default/grub")] = _fail()

    manager = UbuntuConfigurationManager(
        runner=runner, config=UbuntuConfigManagerConfig(),
    )
    settings = manager.list_settings(file="/etc/default/grub")

    testbase.assert_equal(settings, {})
    return


# ----------------------------------------------------------------------
# write_setting
# ----------------------------------------------------------------------


def test_write_setting_writes_via_tee():
    """
        write_setting dispatches a ``tee`` write with the rendered
        content on stdin.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("cat", "/etc/default/grub")] = _ok(
        stdout='GRUB_TIMEOUT="5"\n',
    )

    manager = UbuntuConfigurationManager(
        runner=runner,
        config=UbuntuConfigManagerConfig(elevated=True),
    )
    result = manager.write_setting(
        file="/etc/default/grub", key="GRUB_TIMEOUT", value="10",
    )

    testbase.assert_equal(result.status, UbuntuConfigActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"], ["tee", "/etc/default/grub"],
    )
    testbase.assert_equal(tee_calls[0]["stdin"], 'GRUB_TIMEOUT="10"\n')
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    testbase.assert_equal(result.file, "/etc/default/grub")
    testbase.assert_equal(result.key, "GRUB_TIMEOUT")
    return


def test_write_setting_creates_missing_file():
    """
        write_setting creates the file with a single line when missing.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("cat", "/etc/default/new")] = _fail()

    manager = UbuntuConfigurationManager(
        runner=runner,
        config=UbuntuConfigManagerConfig(elevated=True),
    )
    result = manager.write_setting(
        file="/etc/default/new", key="X", value="1",
    )

    testbase.assert_equal(result.status, UbuntuConfigActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["stdin"], 'X="1"\n')
    return


def test_write_setting_skipped_when_value_matches():
    """
        write_setting returns SKIPPED when the file already contains the
        exact desired pair.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/default/grub")] = _ok(
        stdout='GRUB_TIMEOUT="10"\n',
    )

    manager = UbuntuConfigurationManager(
        runner=runner,
        config=UbuntuConfigManagerConfig(elevated=True),
    )
    result = manager.write_setting(
        file="/etc/default/grub", key="GRUB_TIMEOUT", value="10",
    )

    testbase.assert_equal(result.status, UbuntuConfigActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


def test_write_setting_dry_run_short_circuits():
    """
        Dry-run short-circuits write_setting without invoking writes.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/default/grub")] = _fail()

    manager = UbuntuConfigurationManager(
        runner=runner,
        config=UbuntuConfigManagerConfig(dry_run=True, elevated=True),
    )
    result = manager.write_setting(
        file="/etc/default/grub", key="X", value="1",
    )

    testbase.assert_equal(result.status, UbuntuConfigActionStatus.DRY_RUN)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    testbase.assert_equal(result.command, ["tee", "/etc/default/grub"])
    return


# ----------------------------------------------------------------------
# remove_setting
# ----------------------------------------------------------------------


def test_remove_setting_skipped_when_file_missing():
    """
        remove_setting returns SKIPPED when the file does not exist.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/default/grub")] = _fail()

    manager = UbuntuConfigurationManager(
        runner=runner, config=UbuntuConfigManagerConfig(),
    )
    result = manager.remove_setting(
        file="/etc/default/grub", key="GRUB_TIMEOUT",
    )

    testbase.assert_equal(result.status, UbuntuConfigActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


def test_remove_setting_skipped_when_key_absent():
    """
        remove_setting returns SKIPPED when the key is not present.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/default/grub")] = _ok(
        stdout='OTHER="value"\n',
    )

    manager = UbuntuConfigurationManager(
        runner=runner, config=UbuntuConfigManagerConfig(),
    )
    result = manager.remove_setting(
        file="/etc/default/grub", key="GRUB_TIMEOUT",
    )

    testbase.assert_equal(result.status, UbuntuConfigActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


def test_remove_setting_rewrites_file():
    """
        remove_setting rewrites the file via tee with the matching line
        removed.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("cat", "/etc/default/grub")] = _ok(
        stdout='GRUB_TIMEOUT="5"\nKEEP="value"\n',
    )

    manager = UbuntuConfigurationManager(
        runner=runner,
        config=UbuntuConfigManagerConfig(elevated=True),
    )
    result = manager.remove_setting(
        file="/etc/default/grub", key="GRUB_TIMEOUT",
    )

    testbase.assert_equal(result.status, UbuntuConfigActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["stdin"], 'KEEP="value"\n')
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# reload_configuration
# ----------------------------------------------------------------------


def test_reload_configuration_invokes_systemctl():
    """
        reload_configuration dispatches ``systemctl reload <service>`` and
        elevates.
    """
    runner = _MockCommandRunner(default_result=_ok())

    manager = UbuntuConfigurationManager(
        runner=runner,
        config=UbuntuConfigManagerConfig(elevated=True),
    )
    result = manager.reload_configuration(service="nginx")

    testbase.assert_equal(result.status, UbuntuConfigActionStatus.SUCCESS)
    testbase.assert_equal(
        runner.calls[-1]["args"], ["systemctl", "reload", "nginx"],
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    return


# ----------------------------------------------------------------------
# backup_configuration / restore_configuration
# ----------------------------------------------------------------------


def test_backup_configuration_dispatches_cp():
    """
        backup_configuration dispatches ``cp -p <file> <file>.<ts>.bak``
        and populates ``backup_path`` on success.
    """
    runner = _MockCommandRunner(default_result=_ok())

    manager = UbuntuConfigurationManager(
        runner=runner,
        config=UbuntuConfigManagerConfig(elevated=True),
    )
    result = manager.backup_configuration(file="/etc/default/grub")

    testbase.assert_equal(result.status, UbuntuConfigActionStatus.SUCCESS)
    cp_calls = [c for c in runner.calls if c["args"][0] == "cp"]
    testbase.assert_equal(len(cp_calls), 1)
    testbase.assert_equal(cp_calls[0]["args"][:3], ["cp", "-p", "/etc/default/grub"])
    testbase.assert_equal(cp_calls[0]["elevated"], True)
    testbase.assert_equal(result.backup_path is not None, True)
    testbase.assert_equal(
        result.backup_path.startswith("/etc/default/grub."), True,
    )
    testbase.assert_equal(result.backup_path.endswith(".bak"), True)
    return


def test_backup_configuration_dry_run_short_circuits():
    """
        Dry-run short-circuits backup_configuration without invoking
        writes.
    """
    runner = _MockCommandRunner()

    manager = UbuntuConfigurationManager(
        runner=runner,
        config=UbuntuConfigManagerConfig(dry_run=True, elevated=True),
    )
    result = manager.backup_configuration(file="/etc/default/grub")

    testbase.assert_equal(result.status, UbuntuConfigActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    return


def test_restore_configuration_dispatches_cp():
    """
        restore_configuration dispatches ``cp -p <backup> <target>`` and
        elevates.
    """
    runner = _MockCommandRunner(default_result=_ok())

    manager = UbuntuConfigurationManager(
        runner=runner,
        config=UbuntuConfigManagerConfig(elevated=True),
    )
    result = manager.restore_configuration(
        backup_path="/etc/default/grub.20260101T000000Z.bak",
        target_path="/etc/default/grub",
    )

    testbase.assert_equal(result.status, UbuntuConfigActionStatus.SUCCESS)
    testbase.assert_equal(
        runner.calls[-1]["args"],
        [
            "cp", "-p",
            "/etc/default/grub.20260101T000000Z.bak",
            "/etc/default/grub",
        ],
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    testbase.assert_equal(result.file, "/etc/default/grub")
    testbase.assert_equal(
        result.backup_path, "/etc/default/grub.20260101T000000Z.bak",
    )
    return


# ----------------------------------------------------------------------
# Reads, elevation, error surfaces
# ----------------------------------------------------------------------


def test_reads_never_elevate():
    """
        list_settings and read_setting never pass elevated=True even
        when the config is configured to elevate.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/default/grub")] = _ok(
        stdout='GRUB_TIMEOUT="5"\n',
    )

    manager = UbuntuConfigurationManager(
        runner=runner,
        config=UbuntuConfigManagerConfig(elevated=True),
    )
    manager.list_settings(file="/etc/default/grub")
    manager.read_setting(file="/etc/default/grub", key="GRUB_TIMEOUT")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_runner_surfaces_stderr_on_write_setting():
    """
        A non-zero exit on the tee write maps to FAILED with the captured
        stderr / exit_code on the result.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/default/grub")] = _fail()
    runner.results[
        ("tee", "/etc/default/grub")
    ] = _fail(stderr="tee: permission denied")

    manager = UbuntuConfigurationManager(
        runner=runner,
        config=UbuntuConfigManagerConfig(elevated=True),
    )
    result = manager.write_setting(
        file="/etc/default/grub", key="X", value="1",
    )

    testbase.assert_equal(result.status, UbuntuConfigActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "tee: permission denied")
    return
