"""
    Ubuntu scheduled-tasks feature.

    Implements the cross-OS scheduled-tasks contract for Ubuntu using
    systemd timers (primary) plus a small companion ``.service`` unit
    file per task.  Cron drop-ins are not handled by this manager — the
    base contract is satisfied by the timer pair, and a future
    ``UbuntuCronTaskManager`` companion can layer cron-specific
    behaviour without overlapping this module.

    File I/O policy:
        Every read / write / delete is performed through the injected
        :class:`ICommandRunner` (``cat`` / ``tee`` / ``rm`` / ``test
        -e`` / ``mkdir -p``).  This module does NOT consume an
        :class:`IFileOperator`.

    Owner-scope mapping:
        ``UbuntuTaskOwner.SYSTEM`` writes to ``/etc/systemd/system/``
        and dispatches plain ``systemctl ...``.
        ``UbuntuTaskOwner.USER`` writes to
        ``/home/<target_user>/.config/systemd/user/`` and dispatches
        ``systemctl --user ...``.

    Elevation policy:
        SYSTEM writes elevate when ``config.elevated`` is True.  USER
        writes currently honour ``config.elevated`` as well; the spec
        calls for suppressing elevation when the running user equals
        ``target_user``, but we do not yet have a reliable
        running-user probe that works under both local and SSH
        execution.  The simplification is documented at the call site
        and tracked as a follow-up in
        ``specs/3-scheduled-tasks/tasks.md``.
"""

import os
import re

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_SYSTEMCTL = "systemctl"
_USER_FLAG = "--user"

_DEFAULT_SYSTEM_UNIT_DIR = "/etc/systemd/system"
_DEFAULT_USER_UNIT_DIR_TEMPLATE = "/home/{user}/.config/systemd/user"

_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_]*$")

_SHOW_PROPERTIES = (
    "ActiveState,UnitFileState,NextElapseUSecRealtime,"
    "LastTriggerUSec,OnCalendar,ExecStart"
)


UbuntuScheduledTaskActionStatus = ActionStatus


class UbuntuTaskOwner(Enum):
    """
        Owner-scope selector for scheduled-task verbs.
    """
    SYSTEM = "system"
    USER   = "user"


@dataclass
class UbuntuScheduledTasksManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuScheduledTasksManager`.
        Inherits ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param system_unit_dir: Directory holding system-scope unit
                                files.  Defaults to
                                ``/etc/systemd/system``.
        :param user_unit_dir_template: Format string used to build the
                                       user-scope unit directory.  The
                                       sole substitution token is
                                       ``{user}``.  Defaults to
                                       ``"/home/{user}/.config/systemd/user"``.
        :param target_user: Username for user-scope operations.  Required
                            when any verb is invoked with
                            ``UbuntuTaskOwner.USER``; ignored for
                            system-scope verbs.
    """
    system_unit_dir: str = _DEFAULT_SYSTEM_UNIT_DIR
    user_unit_dir_template: str = _DEFAULT_USER_UNIT_DIR_TEMPLATE
    target_user: Optional[str] = None


@dataclass
class UbuntuScheduledTaskActionResult(FeatureActionResult):
    """
        Result of a scheduled-task write operation.

        :param name: The task name.
        :param owner: The owner scope targeted.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    owner: Optional[UbuntuTaskOwner] = None
    message: Optional[str] = None


@dataclass
class UbuntuTaskSpec:
    """
        Specification for a scheduled task.

        :param name: Task name.  Constrained to alphanumeric plus
                     hyphen and underscore by ``_VALID_NAME``.
        :param command: Argument vector executed by the service unit.
                        Rendered verbatim into ``ExecStart``.
        :param schedule: ``OnCalendar`` expression (e.g.
                         ``"daily"``, ``"*-*-* 02:00:00"``).
        :param working_dir: Optional ``WorkingDirectory`` for the
                            service unit.
        :param user: Optional ``User`` for the service unit.  Mostly
                     relevant for system-scope tasks; for user-scope
                     tasks the systemd user instance already runs as
                     the right user.
        :param env: Optional ``Environment=KEY=VALUE`` lines.
        :param owner: Owner scope selector.
        :param description: Optional ``Description=`` for the units.
        :param enabled: When True (the default), :meth:`create_task`
                        also enables and starts the timer.
    """
    name: str
    command: list[str]
    schedule: str
    working_dir: Optional[str] = None
    user: Optional[str] = None
    env: Optional[dict[str, str]] = None
    owner: UbuntuTaskOwner = UbuntuTaskOwner.SYSTEM
    description: Optional[str] = None
    enabled: bool = True


@dataclass
class UbuntuTaskInfo:
    """
        Current state of a scheduled task.

        :param name: Task name.
        :param owner: Owner scope queried.
        :param enabled: True when the timer's UnitFileState is
                        ``"enabled"``.
        :param active: True when the timer's ActiveState is
                       ``"active"``.
        :param next_run: Next-run timestamp string, or ``None`` when
                         not reported.
        :param last_run: Last-run timestamp string, or ``None`` when
                         not reported.
        :param schedule: Parsed ``OnCalendar`` string from the timer
                         unit, or ``None`` when not reported.
        :param exec_start: Parsed ``ExecStart`` string from the
                           service unit, or ``None`` when not
                           reported.
        :param readable: ``False`` when the task entry exists but
                         could not be parsed.
    """
    name: str
    owner: UbuntuTaskOwner
    enabled: bool = False
    active: bool = False
    next_run: Optional[str] = None
    last_run: Optional[str] = None
    schedule: Optional[str] = None
    exec_start: Optional[str] = None
    readable: bool = True


class UbuntuScheduledTaskQueryError(Exception):
    """
        Raised when a scheduled-task read fails for a reason other than
        the target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Scheduled-task query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuScheduledTasksManager(BaseFeatureManager):
    """
        Ubuntu systemd-timer scheduled-task manager.  Every verb is
        dispatched through the injected :class:`ICommandRunner`.
    """

    result_class = UbuntuScheduledTaskActionResult
    status_class = UbuntuScheduledTaskActionStatus
    feature_name = "scheduled-tasks"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def create_task(
        self,
        spec: UbuntuTaskSpec,
    ) -> UbuntuScheduledTaskActionResult:
        """
            Create or update a scheduled task.

            Renders the service + timer unit files, writes them via
            ``tee``, runs ``systemctl daemon-reload``, and (when
            ``spec.enabled`` is True) ``systemctl enable --now
            <name>.timer``.

            Idempotent: when both unit files already match the rendered
            content byte-for-byte and the timer is already in the
            requested enabled state, the verb returns ``SKIPPED``.

            :param spec: The :class:`UbuntuTaskSpec`.

            :returns: The :class:`UbuntuScheduledTaskActionResult`.
        """
        try:
            _ensure_valid_name(name=spec.name)
        except ValueError as exc:
            failed = self._build_result(
                status=UbuntuScheduledTaskActionStatus.FAILED,
                name=spec.name,
                owner=spec.owner,
                command=[],
                message="invalid_task_name",
                stderr=str(exc),
            )
            return failed

        unit_dir = self._unit_dir(owner=spec.owner)
        service_path = "{}/{}.service".format(unit_dir, spec.name)
        timer_path = "{}/{}.timer".format(unit_dir, spec.name)
        service_body = _render_service_unit(spec=spec)
        timer_body = _render_timer_unit(spec=spec)

        write_command = ["tee", service_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuScheduledTaskActionStatus.DRY_RUN,
                name=spec.name,
                owner=spec.owner,
                command=write_command,
            )
            return dry

        existing_service = self._read_file(path=service_path)
        existing_timer = self._read_file(path=timer_path)
        files_match = (
            existing_service is not None
            and existing_service == service_body
            and existing_timer is not None
            and existing_timer == timer_body
        )

        timer_unit = "{}.timer".format(spec.name)
        is_enabled = self._is_unit_enabled(
            unit=timer_unit, owner=spec.owner,
        )
        enabled_match = (spec.enabled == is_enabled)

        if files_match and enabled_match:
            skipped = self._build_result(
                status=UbuntuScheduledTaskActionStatus.SKIPPED,
                name=spec.name,
                owner=spec.owner,
                command=write_command,
                message="task already in desired state",
            )
            return skipped

        # Ensure the unit directory exists for user-scope writes.  The
        # system unit directory is always present on a working Ubuntu
        # install, so we only ``mkdir -p`` for the user case.
        if spec.owner == UbuntuTaskOwner.USER:
            mkdir_command = ["mkdir", "-p", unit_dir]
            mkdir_run = self._runner.run(
                list(mkdir_command),
                elevated=self._config.elevated,
            )
            if mkdir_run.exit_code != 0:
                failed = UbuntuScheduledTaskActionResult(
                    status=UbuntuScheduledTaskActionStatus.FAILED,
                    detail=spec.name,
                    exit_code=mkdir_run.exit_code,
                    stdout=mkdir_run.stdout,
                    stderr=mkdir_run.stderr,
                    command=list(mkdir_command),
                    name=spec.name,
                    owner=spec.owner,
                    message="user_unit_dir_create_failed",
                )
                return failed

        if existing_service != service_body:
            service_run = self._runner.run(
                ["tee", service_path],
                elevated=self._config.elevated,
                stdin=service_body,
            )
            if service_run.exit_code != 0:
                failed = UbuntuScheduledTaskActionResult(
                    status=UbuntuScheduledTaskActionStatus.FAILED,
                    detail=spec.name,
                    exit_code=service_run.exit_code,
                    stdout=service_run.stdout,
                    stderr=service_run.stderr,
                    command=["tee", service_path],
                    name=spec.name,
                    owner=spec.owner,
                    message="service_unit_write_failed",
                )
                return failed

        if existing_timer != timer_body:
            timer_run = self._runner.run(
                ["tee", timer_path],
                elevated=self._config.elevated,
                stdin=timer_body,
            )
            if timer_run.exit_code != 0:
                failed = UbuntuScheduledTaskActionResult(
                    status=UbuntuScheduledTaskActionStatus.FAILED,
                    detail=spec.name,
                    exit_code=timer_run.exit_code,
                    stdout=timer_run.stdout,
                    stderr=timer_run.stderr,
                    command=["tee", timer_path],
                    name=spec.name,
                    owner=spec.owner,
                    message="timer_unit_write_failed",
                )
                return failed

        # daemon-reload after the writes.
        reload_command = self._systemctl_argv(
            owner=spec.owner, args=["daemon-reload"],
        )
        reload_run = self._runner.run(
            list(reload_command),
            elevated=self._config.elevated,
        )
        if reload_run.exit_code != 0:
            failed = UbuntuScheduledTaskActionResult(
                status=UbuntuScheduledTaskActionStatus.FAILED,
                detail=spec.name,
                exit_code=reload_run.exit_code,
                stdout=reload_run.stdout,
                stderr=reload_run.stderr,
                command=list(reload_command),
                name=spec.name,
                owner=spec.owner,
                message="daemon_reload_failed",
            )
            return failed

        if spec.enabled:
            enable_command = self._systemctl_argv(
                owner=spec.owner, args=["enable", "--now", timer_unit],
            )
            enable_run = self._runner.run(
                list(enable_command),
                elevated=self._config.elevated,
            )
            if enable_run.exit_code != 0:
                failed = UbuntuScheduledTaskActionResult(
                    status=UbuntuScheduledTaskActionStatus.FAILED,
                    detail=spec.name,
                    exit_code=enable_run.exit_code,
                    stdout=enable_run.stdout,
                    stderr=enable_run.stderr,
                    command=list(enable_command),
                    name=spec.name,
                    owner=spec.owner,
                    message="enable_now_failed",
                )
                return failed
            last_run = enable_run
            last_command = enable_command
        else:
            last_run = reload_run
            last_command = reload_command

        success = UbuntuScheduledTaskActionResult(
            status=UbuntuScheduledTaskActionStatus.SUCCESS,
            detail=spec.name,
            exit_code=last_run.exit_code,
            stdout=last_run.stdout,
            stderr=last_run.stderr,
            command=list(last_command),
            name=spec.name,
            owner=spec.owner,
        )
        return success

    def delete_task(
        self,
        name: str,
        owner: UbuntuTaskOwner,
    ) -> UbuntuScheduledTaskActionResult:
        """
            Delete a scheduled task.

            Disables the timer (``systemctl disable --now <name>.timer``),
            deletes the unit files, and runs ``systemctl daemon-reload``.

            Idempotent: when neither unit file exists, the verb returns
            ``SKIPPED`` without invoking the runner.

            :param name: Task name.
            :param owner: Owner scope.

            :returns: The :class:`UbuntuScheduledTaskActionResult`.
        """
        try:
            _ensure_valid_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=UbuntuScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="invalid_task_name",
                stderr=str(exc),
            )
            return failed

        unit_dir = self._unit_dir(owner=owner)
        service_path = "{}/{}.service".format(unit_dir, name)
        timer_path = "{}/{}.timer".format(unit_dir, name)
        timer_unit = "{}.timer".format(name)

        disable_command = self._systemctl_argv(
            owner=owner, args=["disable", "--now", timer_unit],
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuScheduledTaskActionStatus.DRY_RUN,
                name=name,
                owner=owner,
                command=disable_command,
            )
            return dry

        service_exists = self._path_exists(path=service_path)
        timer_exists = self._path_exists(path=timer_path)
        if not service_exists and not timer_exists:
            skipped = self._build_result(
                status=UbuntuScheduledTaskActionStatus.SKIPPED,
                name=name,
                owner=owner,
                command=disable_command,
                message="task unit files not found",
            )
            return skipped

        # disable --now is informational here: a unit that is already
        # disabled or not loaded will report a non-zero exit which we
        # ignore so the deletion can proceed.
        self._runner.run(
            list(disable_command), elevated=self._config.elevated,
        )

        rm_paths = [p for p in (timer_path, service_path) if self._path_exists(path=p)]
        rm_command = ["rm", "-f"] + rm_paths
        rm_run = self._runner.run(
            list(rm_command), elevated=self._config.elevated,
        )
        if rm_run.exit_code != 0:
            failed = UbuntuScheduledTaskActionResult(
                status=UbuntuScheduledTaskActionStatus.FAILED,
                detail=name,
                exit_code=rm_run.exit_code,
                stdout=rm_run.stdout,
                stderr=rm_run.stderr,
                command=list(rm_command),
                name=name,
                owner=owner,
                message="unit_file_delete_failed",
            )
            return failed

        reload_command = self._systemctl_argv(
            owner=owner, args=["daemon-reload"],
        )
        reload_run = self._runner.run(
            list(reload_command), elevated=self._config.elevated,
        )

        if reload_run.exit_code == 0:
            mapped_status = UbuntuScheduledTaskActionStatus.SUCCESS
        else:
            mapped_status = UbuntuScheduledTaskActionStatus.FAILED

        rtnval = UbuntuScheduledTaskActionResult(
            status=mapped_status,
            detail=name,
            exit_code=reload_run.exit_code,
            stdout=reload_run.stdout,
            stderr=reload_run.stderr,
            command=list(reload_command),
            name=name,
            owner=owner,
        )
        return rtnval

    def enable_task(
        self,
        name: str,
        owner: UbuntuTaskOwner,
    ) -> UbuntuScheduledTaskActionResult:
        """
            Enable a scheduled task's timer (``systemctl enable --now
            <name>.timer``).

            Idempotent: when ``systemctl is-enabled <name>.timer``
            already reports ``"enabled"``, the verb returns ``SKIPPED``.

            :param name: Task name.
            :param owner: Owner scope.

            :returns: The :class:`UbuntuScheduledTaskActionResult`.
        """
        try:
            _ensure_valid_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=UbuntuScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="invalid_task_name",
                stderr=str(exc),
            )
            return failed

        timer_unit = "{}.timer".format(name)
        command = self._systemctl_argv(
            owner=owner, args=["enable", "--now", timer_unit],
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuScheduledTaskActionStatus.DRY_RUN,
                name=name,
                owner=owner,
                command=command,
            )
            return dry

        if self._is_unit_enabled(unit=timer_unit, owner=owner):
            skipped = self._build_result(
                status=UbuntuScheduledTaskActionStatus.SKIPPED,
                name=name,
                owner=owner,
                command=command,
                message="timer is already enabled",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command, name=name, owner=owner,
        )
        return rtnval

    def disable_task(
        self,
        name: str,
        owner: UbuntuTaskOwner,
    ) -> UbuntuScheduledTaskActionResult:
        """
            Disable a scheduled task's timer (``systemctl disable
            --now <name>.timer``).

            Idempotent: when ``systemctl is-enabled <name>.timer``
            already reports ``"disabled"``, the verb returns
            ``SKIPPED``.

            :param name: Task name.
            :param owner: Owner scope.

            :returns: The :class:`UbuntuScheduledTaskActionResult`.
        """
        try:
            _ensure_valid_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=UbuntuScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="invalid_task_name",
                stderr=str(exc),
            )
            return failed

        timer_unit = "{}.timer".format(name)
        command = self._systemctl_argv(
            owner=owner, args=["disable", "--now", timer_unit],
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuScheduledTaskActionStatus.DRY_RUN,
                name=name,
                owner=owner,
                command=command,
            )
            return dry

        if not self._is_unit_enabled(unit=timer_unit, owner=owner):
            skipped = self._build_result(
                status=UbuntuScheduledTaskActionStatus.SKIPPED,
                name=name,
                owner=owner,
                command=command,
                message="timer is already disabled",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command, name=name, owner=owner,
        )
        return rtnval

    def run_task_now(
        self,
        name: str,
        owner: UbuntuTaskOwner,
    ) -> UbuntuScheduledTaskActionResult:
        """
            Run a task immediately by starting its service unit
            (``systemctl start <name>.service``).

            No idempotency guard — the contract specifies an
            unconditional start.

            :param name: Task name.
            :param owner: Owner scope.

            :returns: The :class:`UbuntuScheduledTaskActionResult`.
        """
        try:
            _ensure_valid_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=UbuntuScheduledTaskActionStatus.FAILED,
                name=name,
                owner=owner,
                command=[],
                message="invalid_task_name",
                stderr=str(exc),
            )
            return failed

        service_unit = "{}.service".format(name)
        command = self._systemctl_argv(
            owner=owner, args=["start", service_unit],
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuScheduledTaskActionStatus.DRY_RUN,
                name=name,
                owner=owner,
                command=command,
            )
            return dry

        rtnval = self._dispatch_write(
            command=command, name=name, owner=owner,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_task(
        self,
        name: str,
        owner: UbuntuTaskOwner,
    ) -> Optional[UbuntuTaskInfo]:
        """
            Read state for a single task.

            Runs ``systemctl show <name>.timer`` and ``systemctl show
            <name>.service`` and folds the parsed key/value pairs into
            an :class:`UbuntuTaskInfo`.  Reads do not elevate.

            :param name: Task name.
            :param owner: Owner scope.

            :returns: The :class:`UbuntuTaskInfo`, or ``None`` when the
                      timer's ``LoadState`` reports ``not-found``.
            :raises UbuntuScheduledTaskQueryError: When ``systemctl
                                                    show`` fails with an
                                                    unexpected stderr.
        """
        _ensure_valid_name(name=name)

        timer_unit = "{}.timer".format(name)
        service_unit = "{}.service".format(name)

        timer_command = self._systemctl_argv(
            owner=owner,
            args=[
                "show", timer_unit,
                "--property=LoadState," + _SHOW_PROPERTIES,
            ],
        )
        timer_run = self._runner.run(list(timer_command), elevated=False)
        if timer_run.exit_code != 0 and len(timer_run.stderr.strip()) > 0:
            raise UbuntuScheduledTaskQueryError(
                command=timer_command,
                exit_code=timer_run.exit_code,
                stderr=timer_run.stderr,
            )

        timer_props = _parse_show_output(stdout=timer_run.stdout)
        if timer_props.get("LoadState", "") == "not-found":
            rtnval: Optional[UbuntuTaskInfo] = None
            return rtnval

        service_command = self._systemctl_argv(
            owner=owner,
            args=[
                "show", service_unit,
                "--property=LoadState," + _SHOW_PROPERTIES,
            ],
        )
        service_run = self._runner.run(list(service_command), elevated=False)
        if service_run.exit_code != 0 and len(service_run.stderr.strip()) > 0:
            raise UbuntuScheduledTaskQueryError(
                command=service_command,
                exit_code=service_run.exit_code,
                stderr=service_run.stderr,
            )
        service_props = _parse_show_output(stdout=service_run.stdout)

        active_state = timer_props.get("ActiveState", "")
        unit_file_state = timer_props.get("UnitFileState", "")

        next_run_raw = timer_props.get("NextElapseUSecRealtime", "")
        if len(next_run_raw) == 0:
            next_run = None
        else:
            next_run = next_run_raw

        last_run_raw = timer_props.get("LastTriggerUSec", "")
        if len(last_run_raw) == 0:
            last_run = None
        else:
            last_run = last_run_raw

        on_calendar = timer_props.get("OnCalendar", "")
        if len(on_calendar) == 0:
            schedule = None
        else:
            schedule = on_calendar

        exec_start_raw = service_props.get("ExecStart", "")
        if len(exec_start_raw) == 0:
            exec_start = None
        else:
            exec_start = exec_start_raw

        rtnval = UbuntuTaskInfo(
            name=name,
            owner=owner,
            enabled=(unit_file_state == "enabled"),
            active=(active_state == "active"),
            next_run=next_run,
            last_run=last_run,
            schedule=schedule,
            exec_start=exec_start,
        )
        return rtnval

    def list_tasks(
        self,
        owner: UbuntuTaskOwner,
    ) -> list[UbuntuTaskInfo]:
        """
            Enumerate scheduled tasks for ``owner``.

            Runs ``systemctl list-timers --all --no-legend`` and parses
            each row into an :class:`UbuntuTaskInfo`.  Reads do not
            elevate.

            :param owner: Owner scope.

            :returns: One :class:`UbuntuTaskInfo` per parsed row.
            :raises UbuntuScheduledTaskQueryError: When ``systemctl``
                                                    fails with a
                                                    non-zero exit and
                                                    non-empty stderr.
        """
        command = self._systemctl_argv(
            owner=owner,
            args=["list-timers", "--all", "--no-legend"],
        )
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise UbuntuScheduledTaskQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_list_timers(
            stdout=run_result.stdout, owner=owner,
        )
        return rtnval

    def validate_schedule(self, schedule: str) -> bool:
        """
            Pure-Python syntactic validation for an OnCalendar / cron
            expression.  No runner / file-operator calls.

            Accepts:
                * The systemd shorthands ``minutely``, ``hourly``,
                  ``daily``, ``weekly``, ``monthly``, ``yearly``,
                  ``annually``.
                * The OnCalendar shape ``[DOW] [Date] [Time]`` (any
                  combination of date and time components).
                * Five-field cron expressions for backwards
                  compatibility.

            :param schedule: Schedule string.

            :returns: True when ``schedule`` parses; False otherwise.
        """
        rtnval = _validate_schedule(schedule=schedule)
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _unit_dir(self, *, owner: UbuntuTaskOwner) -> str:
        """
            Resolve the unit directory for ``owner``.

            :param owner: Owner scope.

            :returns: Absolute directory path.
            :raises ValueError: When ``owner`` is ``USER`` and no
                                ``target_user`` is configured.
        """
        if owner == UbuntuTaskOwner.SYSTEM:
            rtnval = self._config.system_unit_dir
            return rtnval

        if owner == UbuntuTaskOwner.USER:
            target_user = self._config.target_user
            if target_user is None or len(target_user) == 0:
                err_msg = (
                    "user-scope operations require config.target_user"
                )
                raise ValueError(err_msg)
            rtnval = self._config.user_unit_dir_template.format(
                user=target_user,
            )
            return rtnval

        err_msg = "Unhandled task owner: {!r}".format(owner)
        raise ValueError(err_msg)

    def _systemctl_argv(
        self,
        *,
        owner: UbuntuTaskOwner,
        args: list[str],
    ) -> list[str]:
        """
            Build the ``systemctl`` argv prefix for ``owner`` and tack
            on ``args``.

            :param owner: Owner scope.
            :param args: Trailing argv segments (e.g.
                         ``["enable", "--now", "task.timer"]``).

            :returns: Full argv as a fresh list.
        """
        if owner == UbuntuTaskOwner.SYSTEM:
            prefix = [_SYSTEMCTL]
        elif owner == UbuntuTaskOwner.USER:
            prefix = [_SYSTEMCTL, _USER_FLAG]
        else:
            err_msg = "Unhandled task owner: {!r}".format(owner)
            raise ValueError(err_msg)

        rtnval = prefix + list(args)
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat``.

            :param path: Absolute path.

            :returns: File contents on success; ``None`` when ``cat``
                      reports a non-zero exit (i.e. the file is
                      missing).
        """
        command = ["cat", path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _path_exists(self, *, path: str) -> bool:
        """
            Probe a path's existence via ``test -e``.

            :param path: Absolute path.

            :returns: True when ``test -e`` exits zero.
        """
        command = ["test", "-e", path]
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _is_unit_enabled(
        self,
        *,
        unit: str,
        owner: UbuntuTaskOwner,
    ) -> bool:
        """
            Probe ``systemctl is-enabled <unit>``.  Reads do not
            elevate.

            :param unit: Unit name (with the ``.timer`` suffix).
            :param owner: Owner scope.

            :returns: True when ``is-enabled`` reports exactly
                      ``"enabled"``.
        """
        command = self._systemctl_argv(
            owner=owner, args=["is-enabled", unit],
        )
        run_result = self._runner.run(list(command), elevated=False)
        reported = run_result.stdout.strip()
        rtnval = (reported == "enabled")
        return rtnval

    def _dispatch_write(
        self,
        *,
        command: list[str],
        name: str,
        owner: UbuntuTaskOwner,
    ) -> UbuntuScheduledTaskActionResult:
        """
            Dispatch a non-file write command.

            :param command: Full argv.
            :param name: Task name.
            :param owner: Owner scope.

            :returns: The :class:`UbuntuScheduledTaskActionResult`.
        """
        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = UbuntuScheduledTaskActionStatus.SUCCESS
        else:
            mapped_status = UbuntuScheduledTaskActionStatus.FAILED

        rtnval = UbuntuScheduledTaskActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            owner=owner,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuScheduledTaskActionStatus,
        name: Optional[str],
        owner: Optional[UbuntuTaskOwner],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuScheduledTaskActionResult:
        """
            Construct an :class:`UbuntuScheduledTaskActionResult`
            without invoking the runner.

            :param status: Outcome status.
            :param name: Task name.
            :param owner: Owner scope.
            :param command: Would-have-been argv (copied for capture).
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`UbuntuScheduledTaskActionResult`.
        """
        rtnval = UbuntuScheduledTaskActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            owner=owner,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level renderers and parsers
# ----------------------------------------------------------------------


def _ensure_valid_name(*, name: str) -> None:
    """
        Raise ``ValueError`` when ``name`` contains disallowed characters.

        :param name: Candidate task name.
        :raises ValueError: When ``name`` does not match
                            ``[A-Za-z0-9][A-Za-z0-9\\-_]*``.
    """
    if _VALID_NAME.match(name) is None:
        err_msg = "invalid task name: {!r}".format(name)
        raise ValueError(err_msg)
    return


def _render_service_unit(*, spec: UbuntuTaskSpec) -> str:
    """
        Render the ``<name>.service`` body for ``spec``.

        :param spec: Task spec.

        :returns: The full unit-file body ending in ``\\n``.
    """
    description = spec.description
    if description is None or len(description) == 0:
        description = "automojo-managed task: {}".format(spec.name)

    lines: list[str] = []
    lines.append("[Unit]")
    lines.append("Description={}".format(description))
    lines.append("")
    lines.append("[Service]")
    lines.append("Type=oneshot")
    lines.append("ExecStart={}".format(_render_argv(argv=spec.command)))
    if spec.working_dir is not None and len(spec.working_dir) > 0:
        lines.append("WorkingDirectory={}".format(spec.working_dir))
    if spec.user is not None and len(spec.user) > 0:
        lines.append("User={}".format(spec.user))
    if spec.env is not None and len(spec.env) > 0:
        for key in sorted(spec.env.keys()):
            value = spec.env[key]
            lines.append("Environment={}={}".format(key, value))

    body = "\n".join(lines) + "\n"
    return body


def _render_timer_unit(*, spec: UbuntuTaskSpec) -> str:
    """
        Render the ``<name>.timer`` body for ``spec``.

        :param spec: Task spec.

        :returns: The full unit-file body ending in ``\\n``.
    """
    description = spec.description
    if description is None or len(description) == 0:
        description = "automojo-managed timer: {}".format(spec.name)

    lines: list[str] = []
    lines.append("[Unit]")
    lines.append("Description={} timer".format(description))
    lines.append("")
    lines.append("[Timer]")
    lines.append("OnCalendar={}".format(spec.schedule))
    lines.append("Persistent=true")
    lines.append("Unit={}.service".format(spec.name))
    lines.append("")
    lines.append("[Install]")
    lines.append("WantedBy=timers.target")

    body = "\n".join(lines) + "\n"
    return body


def _render_argv(*, argv: list[str]) -> str:
    """
        Render an ``argv`` list as a single shell-safe string for
        ``ExecStart=``.

        Tokens that contain spaces or shell metacharacters are wrapped
        in double quotes with embedded double quotes escaped.

        :param argv: The argument vector.

        :returns: A single-line string.
    """
    rendered: list[str] = []
    for token in argv:
        if any(ch.isspace() for ch in token) or '"' in token:
            escaped = token.replace("\\", "\\\\").replace('"', '\\"')
            rendered.append('"{}"'.format(escaped))
        else:
            rendered.append(token)
    rtnval = " ".join(rendered)
    return rtnval


def _parse_show_output(*, stdout: str) -> dict:
    """
        Parse ``systemctl show`` ``key=value`` lines into a dict.

        :param stdout: Raw stdout text.

        :returns: A ``dict[str, str]`` of parsed entries.
    """
    rtnval: dict[str, str] = {}
    for raw in stdout.splitlines():
        line = raw.strip()
        if len(line) == 0 or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        rtnval[key.strip()] = value.strip()
    return rtnval


def _parse_list_timers(
    *,
    stdout: str,
    owner: UbuntuTaskOwner,
) -> list[UbuntuTaskInfo]:
    """
        Parse ``systemctl list-timers --all --no-legend`` rows.

        Expected columns (whitespace-separated):

            ``NEXT       LEFT     LAST       PASSED   UNIT      ACTIVATES``

        Some columns may be reported as ``"n/a"``.  The unit column
        is the only required one; we extract the bare task name by
        stripping the trailing ``.timer`` suffix.

        :param stdout: Raw stdout text.
        :param owner: Owner scope to echo onto each parsed row.

        :returns: One :class:`UbuntuTaskInfo` per parsed row.
    """
    rtnval: list[UbuntuTaskInfo] = []

    for raw in stdout.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue

        tokens = line.split()
        # We need at least the UNIT token to do anything.  ``list-timers
        # --all`` may emit extra warnings on stderr, but stdout rows
        # always carry the unit column at index 4 in the legend-stripped
        # form.  Guard defensively.
        unit_token: Optional[str] = None
        for token in tokens:
            if token.endswith(".timer"):
                unit_token = token
                break
        if unit_token is None:
            continue

        name = unit_token[: -len(".timer")]

        # NEXT and LAST are the first and third whitespace-aware columns.
        # When the row is ``n/a   n/a   ...   <unit>`` we leave both
        # ``next_run`` and ``last_run`` as ``None``.  Tokens after the
        # unit (the activate-target column) are not candidates.
        unit_index = tokens.index(unit_token)
        leading_tokens = tokens[:unit_index]
        next_run = _extract_first_field(tokens=leading_tokens)
        last_run = _extract_last_field_before_unit(
            tokens=leading_tokens,
        )

        info = UbuntuTaskInfo(
            name=name,
            owner=owner,
            enabled=True,
            active=False,
            next_run=next_run,
            last_run=last_run,
            schedule=None,
            exec_start=None,
        )
        rtnval.append(info)

    return rtnval


def _extract_first_field(
    *,
    tokens: list[str],
) -> Optional[str]:
    """
        Pick the first non-``"n/a"`` token from ``tokens``.

        :param tokens: Whitespace-split row tokens.

        :returns: The first non-``n/a`` token, or ``None``.
    """
    for token in tokens:
        if token == "n/a":
            continue
        rtnval: Optional[str] = token
        return rtnval
    rtnval = None
    return rtnval


def _extract_last_field_before_unit(
    *,
    tokens: list[str],
) -> Optional[str]:
    """
        Find the last non-``"n/a"`` token in ``tokens``.

        :param tokens: Whitespace-split row tokens already trimmed to
                       exclude the unit token and anything after it.

        :returns: The last non-``"n/a"`` token, or ``None`` when no
                  candidate is found.
    """
    rtnval: Optional[str] = None
    for token in tokens:
        if token == "n/a":
            continue
        rtnval = token
    return rtnval


def _validate_schedule(*, schedule: str) -> bool:
    """
        Implementation of :meth:`UbuntuScheduledTasksManager.validate_schedule`.

        :param schedule: Schedule string.

        :returns: True when ``schedule`` parses; False otherwise.
    """
    if schedule is None:
        return False
    candidate = schedule.strip()
    if len(candidate) == 0:
        return False

    shorthands = {
        "minutely", "hourly", "daily", "weekly", "monthly",
        "yearly", "annually",
    }
    if candidate in shorthands:
        return True

    # Five-field cron form.  Permits ``*``, integers, ranges, and
    # comma-separated lists.  We do not enforce per-field ranges because
    # systemd's own parser handles them; this is a syntactic guard.
    cron_field = re.compile(r"^[\d\*\-/,]+$")
    tokens = candidate.split()
    if len(tokens) == 5 and all(cron_field.match(t) is not None for t in tokens):
        return True

    # OnCalendar shape: any combination of weekday list, ``YYYY-MM-DD``
    # date, and ``HH:MM[:SS]`` time.  We test the relaxed grammar
    # ``[DOW]? [Date]? [Time]?`` with at least one component present.
    rtnval = _looks_like_oncalendar(candidate=candidate)
    return rtnval


_DOW_PATTERN = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
    r"(\.\.(Mon|Tue|Wed|Thu|Fri|Sat|Sun))?"
    r"(,(Mon|Tue|Wed|Thu|Fri|Sat|Sun)(\.\.(Mon|Tue|Wed|Thu|Fri|Sat|Sun))?)*$"
)
_DATE_PATTERN = re.compile(r"^[\d\*]+-[\d\*]+-[\d\*]+$")
_TIME_PATTERN = re.compile(r"^[\d\*]+:[\d\*]+(:[\d\*]+)?$")


def _looks_like_oncalendar(*, candidate: str) -> bool:
    """
        Relaxed validator for an OnCalendar expression.

        :param candidate: Whitespace-trimmed schedule string.

        :returns: True when at least one component matches.
    """
    tokens = candidate.split()
    if len(tokens) == 0 or len(tokens) > 3:
        return False

    matched_any = False
    for token in tokens:
        if _DOW_PATTERN.match(token) is not None:
            matched_any = True
            continue
        if _DATE_PATTERN.match(token) is not None:
            matched_any = True
            continue
        if _TIME_PATTERN.match(token) is not None:
            matched_any = True
            continue
        return False

    return matched_any
