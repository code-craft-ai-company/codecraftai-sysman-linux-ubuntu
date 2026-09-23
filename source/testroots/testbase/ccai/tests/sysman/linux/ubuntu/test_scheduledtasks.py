"""
    Tests for :class:`UbuntuScheduledTasksManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    File I/O is exercised entirely through the runner (``cat`` / ``tee``
    / ``rm`` / ``test -e`` / ``mkdir -p``); no live filesystem is
    touched.  No live ``systemctl`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.scheduledtasks import (
    UbuntuScheduledTaskActionStatus,
    UbuntuScheduledTaskQueryError,
    UbuntuScheduledTasksManager,
    UbuntuScheduledTasksManagerConfig,
    UbuntuTaskOwner,
    UbuntuTaskSpec,
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

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _make_spec(
    *,
    owner: UbuntuTaskOwner = UbuntuTaskOwner.SYSTEM,
    enabled: bool = True,
) -> UbuntuTaskSpec:
    """
        Build a canonical :class:`UbuntuTaskSpec` used across tests.
    """
    rtnval = UbuntuTaskSpec(
        name="backup",
        command=["/usr/local/bin/backup.sh", "--dest", "/var/backups"],
        schedule="daily",
        working_dir="/var/lib/backup",
        user="root",
        env={"LANG": "C"},
        owner=owner,
        description="Nightly backup",
        enabled=enabled,
    )
    return rtnval


def _expected_service_body() -> str:
    """
        Build the canonical rendered ``backup.service`` body.
    """
    rtnval = (
        "[Unit]\n"
        "Description=Nightly backup\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/local/bin/backup.sh --dest /var/backups\n"
        "WorkingDirectory=/var/lib/backup\n"
        "User=root\n"
        "Environment=LANG=C\n"
    )
    return rtnval


def _expected_timer_body() -> str:
    """
        Build the canonical rendered ``backup.timer`` body.
    """
    rtnval = (
        "[Unit]\n"
        "Description=Nightly backup timer\n"
        "\n"
        "[Timer]\n"
        "OnCalendar=daily\n"
        "Persistent=true\n"
        "Unit=backup.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return rtnval


def _seed_create_misses(runner: _MockCommandRunner) -> None:
    """
        Seed the runner so create_task sees no existing files and a
        disabled timer.
    """
    runner.results[("cat", "/etc/systemd/system/backup.service")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.results[("cat", "/etc/systemd/system/backup.timer")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.results[("systemctl", "is-enabled", "backup.timer")] = RunResult(
        exit_code=1, stdout="disabled\n", stderr="",
    )
    return


# ----------------------------------------------------------------------
# create_task — system scope
# ----------------------------------------------------------------------


def test_create_task_writes_unit_files_and_enables():
    """
        create_task writes ``<name>.service`` and ``<name>.timer``,
        runs ``daemon-reload``, and ``enable --now`` for the timer.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_create_misses(runner=runner)

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(elevated=True),
    )
    result = manager.create_task(spec=_make_spec())

    testbase.assert_equal(result.status, UbuntuScheduledTaskActionStatus.SUCCESS)

    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 2)

    by_path = {c["args"][1]: c for c in tee_calls}
    service_call = by_path["/etc/systemd/system/backup.service"]
    timer_call = by_path["/etc/systemd/system/backup.timer"]
    testbase.assert_equal(service_call["stdin"], _expected_service_body())
    testbase.assert_equal(timer_call["stdin"], _expected_timer_body())
    testbase.assert_equal(service_call["elevated"], True)
    testbase.assert_equal(timer_call["elevated"], True)

    reload_calls = [
        c for c in runner.calls
        if c["args"] == ["systemctl", "daemon-reload"]
    ]
    testbase.assert_equal(len(reload_calls), 1)

    enable_calls = [
        c for c in runner.calls
        if c["args"] == ["systemctl", "enable", "--now", "backup.timer"]
    ]
    testbase.assert_equal(len(enable_calls), 1)
    testbase.assert_equal(enable_calls[0]["elevated"], True)
    return


def test_create_task_skipped_when_files_and_state_match():
    """
        create_task returns SKIPPED when the units already match and
        the timer is already enabled.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/systemd/system/backup.service")] = RunResult(
        exit_code=0, stdout=_expected_service_body(), stderr="",
    )
    runner.results[("cat", "/etc/systemd/system/backup.timer")] = RunResult(
        exit_code=0, stdout=_expected_timer_body(), stderr="",
    )
    runner.results[("systemctl", "is-enabled", "backup.timer")] = RunResult(
        exit_code=0, stdout="enabled\n", stderr="",
    )

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(),
    )
    result = manager.create_task(spec=_make_spec())

    testbase.assert_equal(result.status, UbuntuScheduledTaskActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


def test_create_task_dry_run_short_circuits():
    """
        Dry-run returns DRY_RUN and never invokes the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(dry_run=True),
    )
    result = manager.create_task(spec=_make_spec())

    testbase.assert_equal(result.status, UbuntuScheduledTaskActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command, ["tee", "/etc/systemd/system/backup.service"],
    )
    return


def test_create_task_failed_on_invalid_name():
    """
        create_task returns FAILED with no runner calls on an invalid
        task name.
    """
    runner = _MockCommandRunner()
    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(),
    )
    bad = UbuntuTaskSpec(
        name="bad name!",
        command=["/bin/true"],
        schedule="daily",
    )
    result = manager.create_task(spec=bad)

    testbase.assert_equal(result.status, UbuntuScheduledTaskActionStatus.FAILED)
    testbase.assert_equal(result.message, "invalid_task_name")
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_create_task_failed_when_enable_step_fails():
    """
        create_task returns FAILED with
        ``message="enable_now_failed"`` when ``enable --now`` reports a
        non-zero exit.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_create_misses(runner=runner)
    runner.results[
        ("systemctl", "enable", "--now", "backup.timer")
    ] = RunResult(
        exit_code=1, stdout="", stderr="Failed to enable",
    )

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(elevated=True),
    )
    result = manager.create_task(spec=_make_spec())

    testbase.assert_equal(result.status, UbuntuScheduledTaskActionStatus.FAILED)
    testbase.assert_equal(result.message, "enable_now_failed")
    testbase.assert_equal(result.stderr, "Failed to enable")
    return


# ----------------------------------------------------------------------
# create_task — user scope
# ----------------------------------------------------------------------


def test_create_task_user_scope_writes_under_user_home_and_passes_user_flag():
    """
        USER-scope writes go to ``/home/<user>/.config/systemd/user/``
        and the systemctl invocations carry ``--user``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    user_dir = "/home/alice/.config/systemd/user"
    runner.results[("cat", "{}/backup.service".format(user_dir))] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.results[("cat", "{}/backup.timer".format(user_dir))] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.results[
        ("systemctl", "--user", "is-enabled", "backup.timer")
    ] = RunResult(exit_code=1, stdout="disabled\n", stderr="")

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(
            target_user="alice",
        ),
    )
    result = manager.create_task(
        spec=_make_spec(owner=UbuntuTaskOwner.USER),
    )

    testbase.assert_equal(result.status, UbuntuScheduledTaskActionStatus.SUCCESS)

    mkdir_calls = [c for c in runner.calls if c["args"][0] == "mkdir"]
    testbase.assert_equal(len(mkdir_calls), 1)
    testbase.assert_equal(mkdir_calls[0]["args"], ["mkdir", "-p", user_dir])

    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    by_path = {c["args"][1]: c for c in tee_calls}
    testbase.assert_equal(
        sorted(by_path.keys()),
        sorted([
            "{}/backup.service".format(user_dir),
            "{}/backup.timer".format(user_dir),
        ]),
    )

    reload_calls = [
        c for c in runner.calls
        if c["args"] == ["systemctl", "--user", "daemon-reload"]
    ]
    testbase.assert_equal(len(reload_calls), 1)

    enable_calls = [
        c for c in runner.calls
        if c["args"] == [
            "systemctl", "--user", "enable", "--now", "backup.timer",
        ]
    ]
    testbase.assert_equal(len(enable_calls), 1)
    return


# ----------------------------------------------------------------------
# delete_task
# ----------------------------------------------------------------------


def test_delete_task_disables_removes_and_reloads():
    """
        delete_task disables the timer, deletes both unit files, and
        runs ``daemon-reload``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[
        ("test", "-e", "/etc/systemd/system/backup.service")
    ] = RunResult(exit_code=0, stdout="", stderr="")
    runner.results[
        ("test", "-e", "/etc/systemd/system/backup.timer")
    ] = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(elevated=True),
    )
    result = manager.delete_task(name="backup", owner=UbuntuTaskOwner.SYSTEM)

    testbase.assert_equal(result.status, UbuntuScheduledTaskActionStatus.SUCCESS)

    disable_calls = [
        c for c in runner.calls
        if c["args"] == [
            "systemctl", "disable", "--now", "backup.timer",
        ]
    ]
    testbase.assert_equal(len(disable_calls), 1)

    rm_calls = [c for c in runner.calls if c["args"][0] == "rm"]
    testbase.assert_equal(len(rm_calls), 1)
    testbase.assert_equal(
        rm_calls[0]["args"],
        [
            "rm", "-f",
            "/etc/systemd/system/backup.timer",
            "/etc/systemd/system/backup.service",
        ],
    )

    reload_calls = [
        c for c in runner.calls
        if c["args"] == ["systemctl", "daemon-reload"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    return


def test_delete_task_skipped_when_files_absent():
    """
        delete_task returns SKIPPED when neither unit file exists.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("test", "-e", "/etc/systemd/system/backup.service")
    ] = RunResult(exit_code=1, stdout="", stderr="")
    runner.results[
        ("test", "-e", "/etc/systemd/system/backup.timer")
    ] = RunResult(exit_code=1, stdout="", stderr="")

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(),
    )
    result = manager.delete_task(name="backup", owner=UbuntuTaskOwner.SYSTEM)

    testbase.assert_equal(result.status, UbuntuScheduledTaskActionStatus.SKIPPED)
    rm_calls = [c for c in runner.calls if c["args"][0] == "rm"]
    testbase.assert_equal(rm_calls, [])
    return


# ----------------------------------------------------------------------
# enable_task / disable_task
# ----------------------------------------------------------------------


def test_enable_task_skipped_when_already_enabled():
    """
        enable_task returns SKIPPED when ``is-enabled`` already
        reports ``enabled``.
    """
    runner = _MockCommandRunner()
    runner.results[("systemctl", "is-enabled", "backup.timer")] = RunResult(
        exit_code=0, stdout="enabled\n", stderr="",
    )

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(),
    )
    result = manager.enable_task(name="backup", owner=UbuntuTaskOwner.SYSTEM)

    testbase.assert_equal(result.status, UbuntuScheduledTaskActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemctl", "enable"]
        or (c["args"][:3] == ["systemctl", "--user", "enable"])
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_enable_task_dispatches_systemctl_enable_now():
    """
        enable_task dispatches ``systemctl enable --now <name>.timer``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("systemctl", "is-enabled", "backup.timer")] = RunResult(
        exit_code=1, stdout="disabled\n", stderr="",
    )

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(elevated=True),
    )
    result = manager.enable_task(name="backup", owner=UbuntuTaskOwner.SYSTEM)

    testbase.assert_equal(result.status, UbuntuScheduledTaskActionStatus.SUCCESS)
    enable_calls = [
        c for c in runner.calls
        if c["args"] == [
            "systemctl", "enable", "--now", "backup.timer",
        ]
    ]
    testbase.assert_equal(len(enable_calls), 1)
    testbase.assert_equal(enable_calls[0]["elevated"], True)
    return


def test_disable_task_skipped_when_already_disabled():
    """
        disable_task returns SKIPPED when ``is-enabled`` reports a
        non-enabled state.
    """
    runner = _MockCommandRunner()
    runner.results[("systemctl", "is-enabled", "backup.timer")] = RunResult(
        exit_code=1, stdout="disabled\n", stderr="",
    )

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(),
    )
    result = manager.disable_task(
        name="backup", owner=UbuntuTaskOwner.SYSTEM,
    )

    testbase.assert_equal(result.status, UbuntuScheduledTaskActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemctl", "disable"]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# run_task_now
# ----------------------------------------------------------------------


def test_run_task_now_dispatches_systemctl_start_service():
    """
        run_task_now dispatches ``systemctl start <name>.service`` with
        no idempotency guard.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(elevated=True),
    )
    result = manager.run_task_now(
        name="backup", owner=UbuntuTaskOwner.SYSTEM,
    )

    testbase.assert_equal(result.status, UbuntuScheduledTaskActionStatus.SUCCESS)
    start_calls = [
        c for c in runner.calls
        if c["args"] == ["systemctl", "start", "backup.service"]
    ]
    testbase.assert_equal(len(start_calls), 1)
    testbase.assert_equal(start_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# get_task / list_tasks
# ----------------------------------------------------------------------


def test_get_task_populates_fields_from_systemctl_show():
    """
        get_task folds the parsed timer + service show output into a
        :class:`UbuntuTaskInfo`.
    """
    runner = _MockCommandRunner()
    timer_show = (
        "LoadState=loaded\n"
        "ActiveState=active\n"
        "UnitFileState=enabled\n"
        "NextElapseUSecRealtime=Mon 2026-04-27 02:00:00 UTC\n"
        "LastTriggerUSec=Sun 2026-04-26 02:00:00 UTC\n"
        "OnCalendar=daily\n"
        "ExecStart=\n"
    )
    service_show = (
        "LoadState=loaded\n"
        "ExecStart={ path=/usr/local/bin/backup.sh ; argv=...; }\n"
    )
    runner.results[(
        "systemctl", "show", "backup.timer",
        "--property=LoadState,ActiveState,UnitFileState,"
        "NextElapseUSecRealtime,LastTriggerUSec,OnCalendar,ExecStart",
    )] = RunResult(exit_code=0, stdout=timer_show, stderr="")
    runner.results[(
        "systemctl", "show", "backup.service",
        "--property=LoadState,ActiveState,UnitFileState,"
        "NextElapseUSecRealtime,LastTriggerUSec,OnCalendar,ExecStart",
    )] = RunResult(exit_code=0, stdout=service_show, stderr="")

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(elevated=True),
    )
    info = manager.get_task(name="backup", owner=UbuntuTaskOwner.SYSTEM)

    testbase.assert_equal(info is not None, True)
    testbase.assert_equal(info.enabled, True)
    testbase.assert_equal(info.active, True)
    testbase.assert_equal(info.schedule, "daily")
    testbase.assert_equal(info.next_run, "Mon 2026-04-27 02:00:00 UTC")
    testbase.assert_equal(info.last_run, "Sun 2026-04-26 02:00:00 UTC")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_get_task_returns_none_on_load_state_not_found():
    """
        get_task returns ``None`` when the timer's LoadState is
        ``not-found``.
    """
    runner = _MockCommandRunner()
    timer_show = (
        "LoadState=not-found\n"
        "ActiveState=inactive\n"
        "UnitFileState=\n"
    )
    runner.results[(
        "systemctl", "show", "ghost.timer",
        "--property=LoadState,ActiveState,UnitFileState,"
        "NextElapseUSecRealtime,LastTriggerUSec,OnCalendar,ExecStart",
    )] = RunResult(exit_code=0, stdout=timer_show, stderr="")

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(),
    )
    info = manager.get_task(name="ghost", owner=UbuntuTaskOwner.SYSTEM)

    testbase.assert_equal(info is None, True)
    return


def test_list_tasks_parses_list_timers_rows():
    """
        list_tasks parses ``systemctl list-timers`` rows.
    """
    runner = _MockCommandRunner()
    payload = (
        "Mon 2026-04-27 02:00:00 UTC 23h Sun 2026-04-26 02:00:00 UTC 1h"
        "    backup.timer    backup.service\n"
        "n/a n/a n/a n/a once.timer once.service\n"
    )
    runner.results[(
        "systemctl", "list-timers", "--all", "--no-legend",
    )] = RunResult(exit_code=0, stdout=payload, stderr="")

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(elevated=True),
    )
    tasks = manager.list_tasks(owner=UbuntuTaskOwner.SYSTEM)

    testbase.assert_equal(len(tasks), 2)
    by_name = {info.name: info for info in tasks}
    testbase.assert_equal("backup" in by_name, True)
    testbase.assert_equal("once" in by_name, True)
    testbase.assert_equal(by_name["once"].next_run is None, True)
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_list_tasks_raises_on_failure_with_stderr():
    """
        list_tasks raises :class:`UbuntuScheduledTaskQueryError` when
        the runner reports a non-zero exit and stderr.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "systemctl", "list-timers", "--all", "--no-legend",
    )] = RunResult(
        exit_code=1, stdout="", stderr="Permission denied",
    )

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(),
    )
    raised = False
    try:
        manager.list_tasks(owner=UbuntuTaskOwner.SYSTEM)
    except UbuntuScheduledTaskQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# validate_schedule
# ----------------------------------------------------------------------


def test_validate_schedule_accepts_shorthands():
    """
        validate_schedule accepts known shorthands.
    """
    runner = _MockCommandRunner()
    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(),
    )
    for value in ("daily", "hourly", "weekly", "monthly", "yearly"):
        testbase.assert_equal(manager.validate_schedule(schedule=value), True)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_validate_schedule_accepts_oncalendar_and_cron():
    """
        validate_schedule accepts OnCalendar and 5-field cron forms.
    """
    runner = _MockCommandRunner()
    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(),
    )
    testbase.assert_equal(
        manager.validate_schedule(schedule="*-*-* 02:00:00"), True,
    )
    testbase.assert_equal(
        manager.validate_schedule(schedule="Mon..Fri 09:00"), True,
    )
    testbase.assert_equal(
        manager.validate_schedule(schedule="0 2 * * *"), True,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_validate_schedule_rejects_garbage():
    """
        validate_schedule rejects empty / nonsense inputs.
    """
    runner = _MockCommandRunner()
    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(),
    )
    testbase.assert_equal(manager.validate_schedule(schedule=""), False)
    testbase.assert_equal(
        manager.validate_schedule(schedule="not-a-thing"), False,
    )
    testbase.assert_equal(
        manager.validate_schedule(schedule="@@@"), False,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# Cross-cutting: dry-run, elevation, error surfaces
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_every_write():
    """
        Dry-run mode returns DRY_RUN for every write verb without
        invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(dry_run=True),
    )

    create = manager.create_task(spec=_make_spec())
    delete = manager.delete_task(
        name="backup", owner=UbuntuTaskOwner.SYSTEM,
    )
    enable = manager.enable_task(
        name="backup", owner=UbuntuTaskOwner.SYSTEM,
    )
    disable = manager.disable_task(
        name="backup", owner=UbuntuTaskOwner.SYSTEM,
    )
    run_now = manager.run_task_now(
        name="backup", owner=UbuntuTaskOwner.SYSTEM,
    )

    for r in (create, delete, enable, disable, run_now):
        testbase.assert_equal(r.status, UbuntuScheduledTaskActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_reads_never_pass_elevated_true():
    """
        Read-only verbs never pass ``elevated=True`` regardless of
        config.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "systemctl", "list-timers", "--all", "--no-legend",
    )] = RunResult(exit_code=0, stdout="", stderr="")
    runner.results[(
        "systemctl", "show", "backup.timer",
        "--property=LoadState,ActiveState,UnitFileState,"
        "NextElapseUSecRealtime,LastTriggerUSec,OnCalendar,ExecStart",
    )] = RunResult(
        exit_code=0,
        stdout="LoadState=not-found\n",
        stderr="",
    )

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(elevated=True),
    )
    manager.list_tasks(owner=UbuntuTaskOwner.SYSTEM)
    manager.get_task(name="backup", owner=UbuntuTaskOwner.SYSTEM)

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_writes_pass_elevated_true_when_configured():
    """
        Every system-scope write verb passes ``elevated=True`` when the
        config is configured to elevate.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("systemctl", "is-enabled", "backup.timer")] = RunResult(
        exit_code=1, stdout="disabled\n", stderr="",
    )
    runner.results[
        ("test", "-e", "/etc/systemd/system/backup.timer")
    ] = RunResult(exit_code=0, stdout="", stderr="")
    runner.results[
        ("test", "-e", "/etc/systemd/system/backup.service")
    ] = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(elevated=True),
    )
    manager.enable_task(name="backup", owner=UbuntuTaskOwner.SYSTEM)
    manager.run_task_now(name="backup", owner=UbuntuTaskOwner.SYSTEM)
    manager.delete_task(name="backup", owner=UbuntuTaskOwner.SYSTEM)

    write_heads = (
        ["systemctl", "enable"],
        ["systemctl", "start"],
        ["systemctl", "disable"],
        ["systemctl", "daemon-reload"],
        ["rm", "-f"],
    )
    for call in runner.calls:
        head_args = call["args"]
        for head in write_heads:
            if head_args[: len(head)] == head:
                testbase.assert_equal(call["elevated"], True)
                break
    return


def test_failed_write_surfaces_stderr_in_result():
    """
        A non-zero exit on a write maps to FAILED with the captured
        stderr / exit_code / command.
    """
    runner = _MockCommandRunner()
    runner.results[("systemctl", "is-enabled", "backup.timer")] = RunResult(
        exit_code=1, stdout="disabled\n", stderr="",
    )
    runner.results[
        ("systemctl", "enable", "--now", "backup.timer")
    ] = RunResult(
        exit_code=1, stdout="", stderr="Failed to enable timer",
    )

    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(elevated=True),
    )
    result = manager.enable_task(
        name="backup", owner=UbuntuTaskOwner.SYSTEM,
    )

    testbase.assert_equal(result.status, UbuntuScheduledTaskActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "Failed to enable timer")
    return


def test_user_owner_without_target_user_raises_on_write():
    """
        USER-owner writes without ``target_user`` configured raise
        ``ValueError`` from the path resolution.
    """
    runner = _MockCommandRunner()
    manager = UbuntuScheduledTasksManager(
        runner=runner,
        config=UbuntuScheduledTasksManagerConfig(),
    )
    raised = False
    try:
        manager.create_task(spec=_make_spec(owner=UbuntuTaskOwner.USER))
    except ValueError:
        raised = True
    testbase.assert_equal(raised, True)
    return
