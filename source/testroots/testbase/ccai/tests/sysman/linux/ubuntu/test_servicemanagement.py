"""
    Tests for :class:`UbuntuServiceManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call and
    returns pre-seeded :class:`RunResult` values keyed by argv.  No live
    ``systemctl`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.servicemanagement import (
    UbuntuServiceActionStatus,
    UbuntuServiceManager,
    UbuntuServiceManagerConfig,
    UbuntuServiceQueryError,
    UbuntuServiceScope,
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


def _is_active_result(state: str, exit_code: int = 0) -> RunResult:
    """
        Build a canonical ``systemctl is-active`` result.
    """
    rtnval = RunResult(
        exit_code=exit_code,
        stdout=state + "\n",
        stderr="",
    )
    return rtnval


def _show_result(
    *,
    load_state: str = "loaded",
    unit_file_state: str = "enabled",
    main_pid: int = 0,
    description: str = "",
    exit_code: int = 0,
) -> RunResult:
    """
        Build a canonical ``systemctl show --property=...`` result.
    """
    lines = [
        "LoadState={}".format(load_state),
        "UnitFileState={}".format(unit_file_state),
        "MainPID={}".format(main_pid),
        "Description={}".format(description),
    ]
    rtnval = RunResult(
        exit_code=exit_code,
        stdout="\n".join(lines) + "\n",
        stderr="",
    )
    return rtnval


def _seed_service_state(
    *,
    runner: _MockCommandRunner,
    name: str,
    scope: UbuntuServiceScope,
    active_state: str,
    enabled_state: str,
    main_pid: int = 0,
    description: str = "",
) -> None:
    """
        Seed ``is-active`` + ``show`` results for a single service under the
        given scope.
    """
    if scope == UbuntuServiceScope.USER:
        prefix = ("systemctl", "--user")
    else:
        prefix = ("systemctl",)

    is_active_exit = 0
    if active_state != "active":
        is_active_exit = 3
    runner.results[prefix + ("is-active", name)] = _is_active_result(
        state=active_state, exit_code=is_active_exit,
    )
    runner.results[
        prefix + ("show", name, "--property=LoadState,UnitFileState,MainPID,Description")
    ] = _show_result(
        unit_file_state=enabled_state,
        main_pid=main_pid,
        description=description,
    )
    return


def _success_runner_for(
    *,
    name: str,
    scope: UbuntuServiceScope,
    active_state: str,
    enabled_state: str,
) -> _MockCommandRunner:
    """
        Build a runner seeded with a service state and a default success
        ``RunResult`` for any un-seeded write command.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_service_state(
        runner=runner,
        name=name,
        scope=scope,
        active_state=active_state,
        enabled_state=enabled_state,
    )
    return runner


# ----------------------------------------------------------------------
# start_service
# ----------------------------------------------------------------------


def test_start_service_invokes_systemctl_start():
    """
        start_service on an inactive service dispatches ``systemctl start``.
    """
    runner = _success_runner_for(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
        active_state="inactive", enabled_state="enabled",
    )
    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    result = manager.start_service(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, UbuntuServiceActionStatus.SUCCESS)
    write_call = runner.calls[-1]
    testbase.assert_equal(write_call["args"], ["systemctl", "start", "nginx"])
    testbase.assert_equal(result.service_name, "nginx")
    testbase.assert_equal(result.scope, UbuntuServiceScope.SYSTEM)
    return


def test_start_service_skipped_when_already_active():
    """
        start_service returns SKIPPED when the service is already active.
    """
    runner = _success_runner_for(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
        active_state="active", enabled_state="enabled",
    )
    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    result = manager.start_service(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, UbuntuServiceActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls if c["args"][:2] == ["systemctl", "start"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_start_service_dry_run_short_circuits():
    """
        Dry-run short-circuits start_service without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuServiceManager(
        runner=runner,
        config=UbuntuServiceManagerConfig(dry_run=True),
    )
    result = manager.start_service(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, UbuntuServiceActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(result.command, ["systemctl", "start", "nginx"])
    return


# ----------------------------------------------------------------------
# stop_service
# ----------------------------------------------------------------------


def test_stop_service_invokes_systemctl_stop():
    """
        stop_service on an active service dispatches ``systemctl stop``.
    """
    runner = _success_runner_for(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
        active_state="active", enabled_state="enabled",
    )
    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    result = manager.stop_service(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, UbuntuServiceActionStatus.SUCCESS)
    write_call = runner.calls[-1]
    testbase.assert_equal(write_call["args"], ["systemctl", "stop", "nginx"])
    return


def test_stop_service_skipped_when_already_inactive():
    """
        stop_service on an inactive service returns SKIPPED.
    """
    runner = _success_runner_for(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
        active_state="inactive", enabled_state="enabled",
    )
    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    result = manager.stop_service(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, UbuntuServiceActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls if c["args"][:2] == ["systemctl", "stop"]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# restart_service / reload_service
# ----------------------------------------------------------------------


def test_restart_service_unconditional():
    """
        restart_service always dispatches, even when the service is active.
    """
    runner = _success_runner_for(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
        active_state="active", enabled_state="enabled",
    )
    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    result = manager.restart_service(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, UbuntuServiceActionStatus.SUCCESS)
    restart_calls = [
        c for c in runner.calls if c["args"][:2] == ["systemctl", "restart"]
    ]
    testbase.assert_equal(len(restart_calls), 1)
    testbase.assert_equal(
        restart_calls[0]["args"], ["systemctl", "restart", "nginx"],
    )
    return


def test_reload_service_refuses_when_inactive():
    """
        reload_service returns FAILED without invoking ``systemctl reload``
        when the service is not currently active.
    """
    runner = _success_runner_for(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
        active_state="inactive", enabled_state="enabled",
    )
    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    result = manager.reload_service(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, UbuntuServiceActionStatus.FAILED)
    testbase.assert_equal(
        result.message, "service is not active; cannot reload",
    )
    reload_calls = [
        c for c in runner.calls if c["args"][:2] == ["systemctl", "reload"]
    ]
    testbase.assert_equal(reload_calls, [])
    return


# ----------------------------------------------------------------------
# enable_service / disable_service / mask_service / unmask_service
# ----------------------------------------------------------------------


def test_enable_service_invokes_systemctl_enable():
    """
        enable_service on a disabled service dispatches ``systemctl enable``.
    """
    runner = _success_runner_for(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
        active_state="active", enabled_state="disabled",
    )
    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    result = manager.enable_service(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, UbuntuServiceActionStatus.SUCCESS)
    testbase.assert_equal(
        runner.calls[-1]["args"], ["systemctl", "enable", "nginx"],
    )
    return


def test_enable_service_skipped_when_enabled():
    """
        enable_service returns SKIPPED when the service is already enabled.
    """
    runner = _success_runner_for(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
        active_state="active", enabled_state="enabled",
    )
    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    result = manager.enable_service(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, UbuntuServiceActionStatus.SKIPPED)
    enable_calls = [
        c for c in runner.calls if c["args"][:2] == ["systemctl", "enable"]
    ]
    testbase.assert_equal(enable_calls, [])
    return


def test_disable_service_invokes_systemctl_disable():
    """
        disable_service on an enabled service dispatches ``systemctl disable``.
    """
    runner = _success_runner_for(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
        active_state="active", enabled_state="enabled",
    )
    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    result = manager.disable_service(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, UbuntuServiceActionStatus.SUCCESS)
    testbase.assert_equal(
        runner.calls[-1]["args"], ["systemctl", "disable", "nginx"],
    )
    return


def test_mask_service_invokes_systemctl_mask():
    """
        mask_service on a non-masked service dispatches ``systemctl mask``.
    """
    runner = _success_runner_for(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
        active_state="active", enabled_state="enabled",
    )
    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    result = manager.mask_service(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, UbuntuServiceActionStatus.SUCCESS)
    testbase.assert_equal(
        runner.calls[-1]["args"], ["systemctl", "mask", "nginx"],
    )
    return


def test_unmask_service_invokes_systemctl_unmask():
    """
        unmask_service on a masked service dispatches ``systemctl unmask``.
    """
    runner = _success_runner_for(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
        active_state="inactive", enabled_state="masked",
    )
    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    result = manager.unmask_service(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, UbuntuServiceActionStatus.SUCCESS)
    testbase.assert_equal(
        runner.calls[-1]["args"], ["systemctl", "unmask", "nginx"],
    )
    return


# ----------------------------------------------------------------------
# get_service_status / list_services
# ----------------------------------------------------------------------


def test_get_service_status_parses_show_output():
    """
        get_service_status parses ``is-active`` + ``show`` into a
        :class:`UbuntuServiceStatus`.
    """
    runner = _MockCommandRunner()
    runner.results[("systemctl", "is-active", "nginx")] = _is_active_result(
        state="active", exit_code=0,
    )
    runner.results[
        ("systemctl", "show", "nginx",
         "--property=LoadState,UnitFileState,MainPID,Description")
    ] = _show_result(
        load_state="loaded",
        unit_file_state="enabled",
        main_pid=1234,
        description="A high performance web server",
    )

    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    status = manager.get_service_status(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    testbase.assert_equal(status.active_state, "active")
    testbase.assert_equal(status.enabled_state, "enabled")
    testbase.assert_equal(status.main_pid, 1234)
    testbase.assert_equal(status.description, "A high performance web server")
    testbase.assert_equal(status.service_name, "nginx")
    testbase.assert_equal(status.scope, UbuntuServiceScope.SYSTEM)
    return


def test_list_services_parses_list_units_output():
    """
        list_services parses ``list-units`` output into
        :class:`UbuntuServiceInfo` rows.
    """
    list_units_stdout = (
        "cron.service        loaded active     running Regular background program processing daemon\n"
        "ssh.service         loaded active     running OpenBSD Secure Shell server\n"
        "nginx.service       loaded failed     failed  A high performance web server\n"
        "apparmor.service    loaded active     exited  Load AppArmor profiles\n"
    )
    runner = _MockCommandRunner()
    runner.results[(
        "systemctl", "list-units", "--type=service", "--all",
        "--no-legend", "--plain",
    )] = RunResult(
        exit_code=0, stdout=list_units_stdout, stderr="",
    )

    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    listed = manager.list_services(scope=UbuntuServiceScope.SYSTEM)

    testbase.assert_equal(len(listed), 4)

    first = listed[0]
    testbase.assert_equal(first.service_name, "cron")
    testbase.assert_equal(first.active_state, "active")
    testbase.assert_equal(
        first.description,
        "Regular background program processing daemon",
    )
    testbase.assert_equal(first.scope, UbuntuServiceScope.SYSTEM)
    testbase.assert_equal(first.enabled_state, "")

    third = listed[2]
    testbase.assert_equal(third.service_name, "nginx")
    testbase.assert_equal(third.active_state, "failed")
    return


# ----------------------------------------------------------------------
# reload_daemon
# ----------------------------------------------------------------------


def test_reload_daemon_dispatches_systemctl_daemon_reload():
    """
        reload_daemon dispatches ``systemctl daemon-reload`` unconditionally.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = UbuntuServiceManager(
        runner=runner,
        config=UbuntuServiceManagerConfig(elevated=True),
    )
    result = manager.reload_daemon(scope=UbuntuServiceScope.SYSTEM)
    testbase.assert_equal(result.status, UbuntuServiceActionStatus.SUCCESS)
    testbase.assert_equal(
        runner.calls[-1]["args"], ["systemctl", "daemon-reload"],
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Scope + elevation
# ----------------------------------------------------------------------


def test_user_scope_adds_user_flag():
    """
        USER scope inserts ``--user`` into every constructed argv.
    """
    runner = _success_runner_for(
        name="pipewire", scope=UbuntuServiceScope.USER,
        active_state="inactive", enabled_state="enabled",
    )
    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    result = manager.start_service(
        name="pipewire", scope=UbuntuServiceScope.USER,
    )
    testbase.assert_equal(result.status, UbuntuServiceActionStatus.SUCCESS)
    testbase.assert_equal(
        runner.calls[-1]["args"],
        ["systemctl", "--user", "start", "pipewire"],
    )
    return


def test_write_with_elevated_config_passes_elevated_true():
    """
        System-scope writes pass ``elevated=True`` when the config is
        configured to elevate.
    """
    runner = _success_runner_for(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
        active_state="inactive", enabled_state="enabled",
    )
    manager = UbuntuServiceManager(
        runner=runner,
        config=UbuntuServiceManagerConfig(elevated=True),
    )
    manager.start_service(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    write_call = runner.calls[-1]
    testbase.assert_equal(
        write_call["args"], ["systemctl", "start", "nginx"],
    )
    testbase.assert_equal(write_call["elevated"], True)
    return


def test_read_never_elevates():
    """
        Reads and idempotency-probe commands never elevate, even when the
        config is configured to elevate writes.
    """
    runner = _success_runner_for(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
        active_state="inactive", enabled_state="enabled",
    )
    manager = UbuntuServiceManager(
        runner=runner,
        config=UbuntuServiceManagerConfig(elevated=True),
    )
    # Probe via read verb.
    manager.get_service_status(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    # Write verb, whose idempotency probe must also not elevate.
    manager.start_service(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    elevated_probe_calls = [
        c for c in runner.calls
        if (c["args"][:2] == ["systemctl", "is-active"]
            or c["args"][:2] == ["systemctl", "show"])
        and c["elevated"]
    ]
    testbase.assert_equal(elevated_probe_calls, [])

    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemctl", "start"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_user_scope_write_never_elevates():
    """
        USER-scope writes never pass ``elevated=True``, even when the config
        is configured to elevate system writes.
    """
    runner = _success_runner_for(
        name="pipewire", scope=UbuntuServiceScope.USER,
        active_state="inactive", enabled_state="enabled",
    )
    manager = UbuntuServiceManager(
        runner=runner,
        config=UbuntuServiceManagerConfig(elevated=True),
    )
    manager.start_service(
        name="pipewire", scope=UbuntuServiceScope.USER,
    )
    write_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["systemctl", "--user", "start"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], False)
    return


# ----------------------------------------------------------------------
# Failure surfaces
# ----------------------------------------------------------------------


def test_failed_command_returns_failed_result_with_stderr():
    """
        A non-zero exit code from the write maps to FAILED with the
        captured stderr / exit_code / command.
    """
    runner = _success_runner_for(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
        active_state="inactive", enabled_state="enabled",
    )
    runner.results[("systemctl", "start", "nginx")] = RunResult(
        exit_code=1,
        stdout="",
        stderr="Failed to start nginx: Access denied",
    )
    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    result = manager.start_service(
        name="nginx", scope=UbuntuServiceScope.SYSTEM,
    )
    testbase.assert_equal(result.status, UbuntuServiceActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(
        result.stderr, "Failed to start nginx: Access denied",
    )
    testbase.assert_equal(result.command, ["systemctl", "start", "nginx"])
    return


def test_list_services_raises_on_error():
    """
        list_services raises :class:`UbuntuServiceQueryError` on non-zero
        exit.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "systemctl", "list-units", "--type=service", "--all",
        "--no-legend", "--plain",
    )] = RunResult(
        exit_code=1, stdout="", stderr="boom",
    )
    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    raised = False
    try:
        manager.list_services(scope=UbuntuServiceScope.SYSTEM)
    except UbuntuServiceQueryError:
        raised = True
    testbase.assert_true(raised)
    return


def test_invalid_scope_raises_value_error():
    """
        A non-:class:`UbuntuServiceScope` value raises ``ValueError`` before
        the runner is invoked.
    """
    runner = _MockCommandRunner()
    manager = UbuntuServiceManager(
        runner=runner, config=UbuntuServiceManagerConfig(),
    )
    raised = False
    try:
        # Intentional type abuse to exercise the scope guard.
        manager.start_service(name="nginx", scope="system")
    except ValueError:
        raised = True
    testbase.assert_true(raised)
    testbase.assert_equal(len(runner.calls), 0)
    return
