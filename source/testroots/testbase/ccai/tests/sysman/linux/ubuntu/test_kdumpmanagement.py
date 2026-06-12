"""
    Tests for :class:`UbuntuKdumpManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by
    argv.  No live ``kdump-config`` / ``systemctl`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.kdumpmanagement import (
    UbuntuKdumpActionStatus,
    UbuntuKdumpManager,
    UbuntuKdumpManagerConfig,
    UbuntuKdumpQueryError,
    UbuntuKdumpTargetKind,
    UbuntuKdumpTargetSpec,
)


_DEFAULTS_PATH = "/etc/default/kdump-tools"
_CRASH_DIR = "/var/crash"
_SERVICE_NAME = "kdump-tools.service"


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv
        tuple.
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
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(
            exit_code=0,
            stdout="",
            stderr="",
        )
        return rtnval


def _ok(stdout: str = "") -> RunResult:
    """
        Build an exit-code-zero :class:`RunResult`.
    """
    rtnval = RunResult(
        exit_code=0,
        stdout=stdout,
        stderr="",
    )
    return rtnval


def _fail(stderr: str = "boom", exit_code: int = 1) -> RunResult:
    """
        Build a non-zero :class:`RunResult`.
    """
    rtnval = RunResult(
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
    )
    return rtnval


def _seed_service_state(
    *,
    runner: _MockCommandRunner,
    active: str,
    enabled: str,
) -> None:
    """
        Pre-seed ``systemctl is-active`` and ``systemctl is-enabled``
        results.
    """
    is_active_key = ("systemctl", "is-active", _SERVICE_NAME)
    is_enabled_key = ("systemctl", "is-enabled", _SERVICE_NAME)

    if active == "active":
        active_result = _ok(stdout=active + "\n")
    else:
        active_result = RunResult(
            exit_code=3,
            stdout=active + "\n",
            stderr="",
        )
    if enabled == "enabled":
        enabled_result = _ok(stdout=enabled + "\n")
    else:
        enabled_result = RunResult(
            exit_code=1,
            stdout=enabled + "\n",
            stderr="",
        )

    runner.results[is_active_key] = active_result
    runner.results[is_enabled_key] = enabled_result
    return


def _seed_defaults(
    *,
    runner: _MockCommandRunner,
    body: str,
) -> None:
    """
        Pre-seed a ``cat /etc/default/kdump-tools`` result.
    """
    key = ("cat", _DEFAULTS_PATH)
    runner.results[key] = _ok(stdout=body)
    return


def _make_manager(
    *,
    runner: _MockCommandRunner,
    elevated: bool = False,
    dry_run: bool = False,
) -> UbuntuKdumpManager:
    """
        Construct a manager wired to ``runner`` with default paths.
    """
    config = UbuntuKdumpManagerConfig(
        dry_run=dry_run,
        elevated=elevated,
        kdump_defaults=_DEFAULTS_PATH,
        crash_dir=_CRASH_DIR,
        service_name=_SERVICE_NAME,
    )
    rtnval = UbuntuKdumpManager(runner=runner, config=config)
    return rtnval


# ----------------------------------------------------------------------
# enable_kdump
# ----------------------------------------------------------------------


def test_enable_kdump_invokes_systemctl():
    """
        ``enable_kdump`` runs ``systemctl enable --now kdump-tools.service``
        after rewriting the defaults file.
    """
    runner = _MockCommandRunner()
    _seed_defaults(runner=runner, body="USE_KDUMP=0\n")
    _seed_service_state(runner=runner, active="inactive", enabled="disabled")
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.enable_kdump()

    testbase.assert_equal(result.status, UbuntuKdumpActionStatus.SUCCESS)
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        ["systemctl", "enable", "--now", _SERVICE_NAME],
    )
    tee_calls = [
        call for call in runner.calls
        if call["args"][:1] == ["tee"]
        and call["args"][1] == _DEFAULTS_PATH
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_true("USE_KDUMP=" in tee_calls[0]["stdin"])
    return


def test_enable_kdump_skipped_when_enabled():
    """
        ``enable_kdump`` returns ``SKIPPED`` when both the defaults flag
        and the systemd unit are already on.
    """
    runner = _MockCommandRunner()
    _seed_defaults(runner=runner, body='USE_KDUMP=1\nKDUMP_COREDIR="/var/crash"\n')
    _seed_service_state(runner=runner, active="active", enabled="enabled")
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.enable_kdump()

    testbase.assert_equal(result.status, UbuntuKdumpActionStatus.SKIPPED)
    write_calls = [
        call for call in runner.calls
        if call["args"][:2] == ["systemctl", "enable"]
        or call["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_enable_kdump_dry_run_short_circuits():
    """
        ``enable_kdump`` does not invoke the runner under ``dry_run=True``.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, dry_run=True, elevated=True)

    result = manager.enable_kdump()

    testbase.assert_equal(result.status, UbuntuKdumpActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command,
        ["systemctl", "enable", "--now", _SERVICE_NAME],
    )
    return


# ----------------------------------------------------------------------
# disable_kdump
# ----------------------------------------------------------------------


def test_disable_kdump_invokes_systemctl():
    """
        ``disable_kdump`` runs ``systemctl disable --now kdump-tools.service``
        after rewriting the defaults file.
    """
    runner = _MockCommandRunner()
    _seed_defaults(runner=runner, body="USE_KDUMP=1\n")
    _seed_service_state(runner=runner, active="active", enabled="enabled")
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.disable_kdump()

    testbase.assert_equal(result.status, UbuntuKdumpActionStatus.SUCCESS)
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        ["systemctl", "disable", "--now", _SERVICE_NAME],
    )
    return


# ----------------------------------------------------------------------
# configure_kdump_target
# ----------------------------------------------------------------------


def test_configure_kdump_target_invokes_tee():
    """
        ``configure_kdump_target`` rewrites the defaults file and
        restarts the service.
    """
    runner = _MockCommandRunner()
    _seed_defaults(runner=runner, body='KDUMP_COREDIR="/var/crash"\n')
    manager = _make_manager(runner=runner, elevated=True)

    spec = UbuntuKdumpTargetSpec(
        target_kind=UbuntuKdumpTargetKind.LOCAL_PATH,
        path="/var/dumps/crash",
    )
    result = manager.configure_kdump_target(spec=spec)

    testbase.assert_equal(result.status, UbuntuKdumpActionStatus.SUCCESS)
    tee_calls = [
        call for call in runner.calls
        if call["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_true("/var/dumps/crash" in tee_calls[0]["stdin"])
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        ["systemctl", "restart", _SERVICE_NAME],
    )
    return


def test_configure_kdump_target_skipped_when_already_set():
    """
        ``configure_kdump_target`` returns ``SKIPPED`` when the rendered
        defaults file matches the on-disk body.
    """
    runner = _MockCommandRunner()
    _seed_defaults(runner=runner, body='KDUMP_COREDIR="/var/crash"\n')
    manager = _make_manager(runner=runner, elevated=True)

    spec = UbuntuKdumpTargetSpec(
        target_kind=UbuntuKdumpTargetKind.LOCAL_PATH,
        path="/var/crash",
    )
    result = manager.configure_kdump_target(spec=spec)

    testbase.assert_equal(result.status, UbuntuKdumpActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# get_kdump_status
# ----------------------------------------------------------------------


def test_get_kdump_status_parses_kdump_config_output():
    """
        ``get_kdump_status`` combines defaults-file parse, systemd
        probes, and ``kdump-config show`` into a single status object.
    """
    runner = _MockCommandRunner()
    defaults_body = (
        'USE_KDUMP=1\n'
        'KDUMP_COREDIR="/var/crash"\n'
    )
    _seed_defaults(runner=runner, body=defaults_body)
    _seed_service_state(runner=runner, active="active", enabled="enabled")

    show_output = (
        "DUMP_MODE:        kdump\n"
        "USE_KDUMP:        1\n"
        "KDUMP_COREDIR:    /var/crash\n"
        "kdump kernel:     /var/lib/kdump/vmlinuz\n"
        "current state:    ready to kdump\n"
    )
    runner.results[("kdump-config", "show")] = _ok(stdout=show_output)
    manager = _make_manager(runner=runner)

    status = manager.get_kdump_status()

    testbase.assert_true(status.enabled)
    testbase.assert_true(status.running)
    testbase.assert_equal(status.target_kind, UbuntuKdumpTargetKind.LOCAL_PATH)
    testbase.assert_equal(status.target_path, "/var/crash")
    testbase.assert_equal(status.kernel, "/var/lib/kdump/vmlinuz")
    return


# ----------------------------------------------------------------------
# list_recent_dumps
# ----------------------------------------------------------------------


def test_list_recent_dumps_parses_directory_listing():
    """
        ``list_recent_dumps`` returns one info per surviving ``ls -1t``
        entry.
    """
    runner = _MockCommandRunner()
    listing = (
        "202604240931\n"
        "202604220702\n"
        "README\n"
    )
    runner.results[("ls", "-1t", _CRASH_DIR)] = _ok(stdout=listing)
    manager = _make_manager(runner=runner)

    result = manager.list_recent_dumps()

    testbase.assert_equal(len(result), 3)
    ids = [item.id for item in result]
    testbase.assert_true("202604240931" in ids)
    testbase.assert_true("202604220702" in ids)
    first = result[0]
    testbase.assert_equal(first.path, "/var/crash/202604240931")
    return


# ----------------------------------------------------------------------
# clear_old_dumps
# ----------------------------------------------------------------------


def test_clear_old_dumps_invokes_find_with_mtime():
    """
        ``clear_old_dumps`` invokes ``find ... -mtime +N -exec rm``.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.clear_old_dumps(age_days=30)

    testbase.assert_equal(result.status, UbuntuKdumpActionStatus.SUCCESS)
    testbase.assert_equal(len(runner.calls), 1)
    argv = runner.calls[0]["args"]
    testbase.assert_equal(argv[0], "find")
    testbase.assert_true("-mtime" in argv)
    testbase.assert_true("+30" in argv)
    testbase.assert_true("-rf" in argv)
    return


# ----------------------------------------------------------------------
# get_dump_info
# ----------------------------------------------------------------------


def test_get_dump_info_parses_dmesg_output():
    """
        ``get_dump_info`` surfaces kernel + panic message + size from
        the parsed dmesg files.
    """
    runner = _MockCommandRunner()
    dump_id = "202604240931"
    primary = "/var/crash/{}/vmcore-dmesg.txt".format(dump_id)
    fallback = "/var/crash/{}/dmesg.{}".format(dump_id, dump_id)
    full_path = "/var/crash/{}".format(dump_id)

    primary_text = (
        "[    0.000000] Linux version 6.5.0-1010-generic ...\n"
        "[    0.000000] Command line: BOOT_IMAGE=/vmlinuz ...\n"
        "[ 4567.890123] Kernel panic - not syncing: Fatal exception\n"
    )
    runner.results[("cat", primary)] = _ok(stdout=primary_text)
    runner.results[("cat", fallback)] = _fail(
        stderr="cat: no such file",
        exit_code=1,
    )
    runner.results[("stat", "-c", "%y;%s", full_path)] = _ok(
        stdout="2026-04-24 09:31:00.000000000 +0000;81920\n",
    )
    manager = _make_manager(runner=runner)

    info = manager.get_dump_info(dump_id=dump_id)

    testbase.assert_equal(info.id, dump_id)
    testbase.assert_equal(info.path, full_path)
    testbase.assert_true(info.kernel is not None)
    testbase.assert_true("Linux version" in info.kernel)
    testbase.assert_true(info.panic_message is not None)
    testbase.assert_true("Kernel panic" in info.panic_message)
    testbase.assert_equal(info.size_bytes, 81920)
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write verbs forward ``elevated=True`` to the runner.
    """
    runner = _MockCommandRunner()
    _seed_defaults(runner=runner, body="USE_KDUMP=0\n")
    _seed_service_state(runner=runner, active="inactive", enabled="disabled")
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.enable_kdump()

    testbase.assert_equal(result.status, UbuntuKdumpActionStatus.SUCCESS)
    write_calls = [
        call for call in runner.calls
        if call["args"][:1] == ["tee"]
        or call["args"][:2] == ["systemctl", "enable"]
    ]
    testbase.assert_equal(len(write_calls), 2)
    for call in write_calls:
        testbase.assert_true(call["elevated"])
    return


def test_reads_never_elevate():
    """
        Read verbs always pass ``elevated=False`` regardless of config.
    """
    runner = _MockCommandRunner()
    _seed_defaults(runner=runner, body='USE_KDUMP=1\nKDUMP_COREDIR="/var/crash"\n')
    _seed_service_state(runner=runner, active="active", enabled="enabled")
    runner.results[("kdump-config", "show")] = _ok(stdout="")
    manager = _make_manager(runner=runner, elevated=True)

    manager.get_kdump_status()

    for call in runner.calls:
        testbase.assert_false(call["elevated"])
    return


# ----------------------------------------------------------------------
# Failure surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A non-zero ``systemctl`` exit produces a ``FAILED`` result with
        captured stderr.
    """
    runner = _MockCommandRunner()
    _seed_defaults(runner=runner, body="USE_KDUMP=0\n")
    _seed_service_state(runner=runner, active="inactive", enabled="disabled")
    runner.results[
        ("systemctl", "enable", "--now", _SERVICE_NAME)
    ] = _fail(stderr="Failed to enable unit: Access denied")
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.enable_kdump()

    testbase.assert_equal(result.status, UbuntuKdumpActionStatus.FAILED)
    testbase.assert_true("Access denied" in (result.stderr or ""))
    testbase.assert_equal(result.exit_code, 1)
    return


def test_list_recent_dumps_raises_on_unexpected_failure():
    """
        ``list_recent_dumps`` raises :class:`UbuntuKdumpQueryError` on
        an unexpected ``ls`` failure.
    """
    runner = _MockCommandRunner()
    runner.results[("ls", "-1t", _CRASH_DIR)] = _fail(
        stderr="ls: permission denied",
        exit_code=2,
    )
    manager = _make_manager(runner=runner)

    raised = False
    try:
        manager.list_recent_dumps()
    except UbuntuKdumpQueryError:
        raised = True
    testbase.assert_true(raised)
    return
