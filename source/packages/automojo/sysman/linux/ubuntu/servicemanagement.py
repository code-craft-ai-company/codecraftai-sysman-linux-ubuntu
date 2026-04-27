"""
    Ubuntu service-management feature.

    Implements the cross-OS service-management contract for Ubuntu using
    ``systemctl``.  Every verb is dispatched through an
    :class:`ICommandRunner` so the manager works identically under local and
    SSH execution.

    Scope mapping:
        ``UbuntuServiceScope.SYSTEM`` -> ``["systemctl", ...]``
        ``UbuntuServiceScope.USER``   -> ``["systemctl", "--user", ...]``

    Elevation policy:
        System-scope writes elevate when ``config.elevated`` is True.  User-
        scope writes never elevate (per-user systemd instances accept no
        ``sudo``).  Reads and idempotency-probe commands never elevate.
"""

import os

from dataclasses import dataclass
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

_SHOW_PROPERTIES = "LoadState,UnitFileState,MainPID,Description"

_INACTIVE_ACTIVE_STATES = frozenset({"inactive", "failed"})
_DISABLE_SKIP_STATES = frozenset({"disabled", "masked"})

_INFORMATIONAL_ACTIVE_STATES = frozenset({
    "active", "inactive", "failed", "activating", "deactivating", "reloading",
})

_KNOWN_ACTIVE_STATES = frozenset({
    "active", "inactive", "failed", "activating", "deactivating", "reloading",
    "not-found",
})


UbuntuServiceActionStatus = ActionStatus


class UbuntuServiceScope(Enum):
    """
        Scope selector for the service manager.  Maps directly to the
        presence or absence of the ``--user`` flag in the constructed argv.
    """
    SYSTEM = "system"
    USER   = "user"


@dataclass
class UbuntuServiceManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuServiceManager`.  Inherits ``dry_run``
        and ``elevated`` from :class:`FeatureConfig`.

        :param user_uid: Optional numeric UID used when the manager needs to
                         reach into another user's bus.  The current
                         implementation does not consume this field but it is
                         reserved for the ``sudo -u`` or ``machinectl
                         shell --uid`` variants that a future iteration of
                         the runner may adopt.
    """
    user_uid: Optional[int] = None


@dataclass
class UbuntuServiceActionResult(FeatureActionResult):
    """
        Result of a service-management write operation.

        :param service_name: The service targeted.  Empty for
                             :meth:`UbuntuServiceManager.reload_daemon`.
        :param scope: The scope targeted.
        :param message: Optional human-readable detail for ``SKIPPED`` or
                        ``FAILED`` outcomes.
    """
    service_name: str = ""
    scope: Optional[UbuntuServiceScope] = None
    message: Optional[str] = None


@dataclass
class UbuntuServiceStatus:
    """
        Current state of a single service.

        :param service_name: The service queried.
        :param scope: The scope queried.
        :param active_state: One of ``"active"``, ``"inactive"``, ``"failed"``,
                             ``"activating"``, ``"deactivating"``,
                             ``"reloading"``, ``"not-found"``.
        :param enabled_state: One of ``"enabled"``, ``"disabled"``,
                              ``"masked"``, ``"static"``, ``"not-found"``.
        :param main_pid: Main process PID when active; otherwise None.
        :param description: The service's description when available.
    """
    service_name: str
    scope: UbuntuServiceScope
    active_state: str
    enabled_state: str
    main_pid: Optional[int] = None
    description: Optional[str] = None


@dataclass
class UbuntuServiceInfo:
    """
        One row from ``systemctl list-units --type=service``.

        :param service_name: Unit name with the ``.service`` suffix stripped.
        :param scope: The scope the listing was taken from.
        :param active_state: Active state column from the listing.
        :param enabled_state: Intentionally left empty — ``list-units`` does
                              not carry the enabled state.  Callers that need
                              it can call :meth:`get_service_status` per
                              service.
        :param description: Description column from the listing.
    """
    service_name: str
    scope: UbuntuServiceScope
    active_state: str
    enabled_state: str
    description: Optional[str]


class UbuntuServiceQueryError(Exception):
    """
        Raised when a service read fails for a reason other than an
        informational non-zero exit.
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

        err_msg_lines = ["Service query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


def _scope_prefix(scope: UbuntuServiceScope) -> list[str]:
    """
        Build the ``systemctl`` argv prefix for the given scope.

        :param scope: The scope selector.

        :returns: ``["systemctl"]`` for SYSTEM, ``["systemctl", "--user"]`` for
                  USER.
        :raises ValueError: When ``scope`` is not a recognised
                            :class:`UbuntuServiceScope` member.
    """
    if not isinstance(scope, UbuntuServiceScope):
        err_msg = "Unsupported service scope: {!r}".format(scope)
        raise ValueError(err_msg)

    if scope == UbuntuServiceScope.SYSTEM:
        prefix = [_SYSTEMCTL]
    elif scope == UbuntuServiceScope.USER:
        prefix = [_SYSTEMCTL, _USER_FLAG]
    else:
        # Defensive: enum extension without matching branch above.
        err_msg = "Unhandled service scope: {!r}".format(scope)
        raise ValueError(err_msg)

    return prefix


class UbuntuServiceManager(BaseFeatureManager):
    """
        Ubuntu systemd service manager.  Every verb is scoped by a
        :class:`UbuntuServiceScope` and dispatched through the injected
        :class:`ICommandRunner`.
    """

    result_class = UbuntuServiceActionResult
    status_class = UbuntuServiceActionStatus
    feature_name = "service-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def start_service(
        self,
        name: str,
        scope: UbuntuServiceScope,
    ) -> UbuntuServiceActionResult:
        """
            Start a service unit.  Skipped when the unit is already active.

            :param name: The service unit name.
            :param scope: The scope selector.

            :returns: The :class:`UbuntuServiceActionResult`.
        """
        command = _scope_prefix(scope) + ["start", name]
        skip_result = self._maybe_skip_on_active(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in={"active"},
            skip_message="service is already active",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="start_service",
        )
        return rtnval

    def stop_service(
        self,
        name: str,
        scope: UbuntuServiceScope,
    ) -> UbuntuServiceActionResult:
        """
            Stop a service unit.  Skipped when already inactive or failed.

            :param name: The service unit name.
            :param scope: The scope selector.

            :returns: The :class:`UbuntuServiceActionResult`.
        """
        command = _scope_prefix(scope) + ["stop", name]
        skip_result = self._maybe_skip_on_active(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in=_INACTIVE_ACTIVE_STATES,
            skip_message="service is already stopped",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="stop_service",
        )
        return rtnval

    def restart_service(
        self,
        name: str,
        scope: UbuntuServiceScope,
    ) -> UbuntuServiceActionResult:
        """
            Restart a service unit.  Never skips.

            :param name: The service unit name.
            :param scope: The scope selector.

            :returns: The :class:`UbuntuServiceActionResult`.
        """
        command = _scope_prefix(scope) + ["restart", name]
        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="restart_service",
        )
        return rtnval

    def reload_service(
        self,
        name: str,
        scope: UbuntuServiceScope,
    ) -> UbuntuServiceActionResult:
        """
            Send a reload signal to a running service.  Returns ``FAILED``
            without invoking the runner when the service is not currently
            active (reload requires a running process).  Dry-run short-
            circuits before the precondition check per the spec.

            :param name: The service unit name.
            :param scope: The scope selector.

            :returns: The :class:`UbuntuServiceActionResult`.
        """
        command = _scope_prefix(scope) + ["reload", name]

        if self._config.dry_run:
            rtnval = self._build_result(
                status=UbuntuServiceActionStatus.DRY_RUN,
                name=name,
                scope=scope,
                command=command,
            )
            return rtnval

        status_info = self.get_service_status(name=name, scope=scope)
        if status_info.active_state != "active":
            message = "service is not active; cannot reload"
            rtnval = self._build_result(
                status=UbuntuServiceActionStatus.FAILED,
                name=name,
                scope=scope,
                command=command,
                message=message,
                stderr="service is not active: {}".format(
                    status_info.active_state
                ),
            )
            return rtnval

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="reload_service",
        )
        return rtnval

    def enable_service(
        self,
        name: str,
        scope: UbuntuServiceScope,
    ) -> UbuntuServiceActionResult:
        """
            Enable a service for automatic start at boot.  Skipped when
            already enabled.

            :param name: The service unit name.
            :param scope: The scope selector.

            :returns: The :class:`UbuntuServiceActionResult`.
        """
        command = _scope_prefix(scope) + ["enable", name]
        skip_result = self._maybe_skip_on_enabled(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in={"enabled"},
            skip_message="service is already enabled",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="enable_service",
        )
        return rtnval

    def disable_service(
        self,
        name: str,
        scope: UbuntuServiceScope,
    ) -> UbuntuServiceActionResult:
        """
            Disable a service so that it does not start at boot.  Skipped
            when already disabled or masked.

            :param name: The service unit name.
            :param scope: The scope selector.

            :returns: The :class:`UbuntuServiceActionResult`.
        """
        command = _scope_prefix(scope) + ["disable", name]
        skip_result = self._maybe_skip_on_enabled(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in=_DISABLE_SKIP_STATES,
            skip_message="service is already disabled or masked",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="disable_service",
        )
        return rtnval

    def mask_service(
        self,
        name: str,
        scope: UbuntuServiceScope,
    ) -> UbuntuServiceActionResult:
        """
            Mask a service unit so it cannot be started.  Skipped when
            already masked.

            :param name: The service unit name.
            :param scope: The scope selector.

            :returns: The :class:`UbuntuServiceActionResult`.
        """
        command = _scope_prefix(scope) + ["mask", name]
        skip_result = self._maybe_skip_on_enabled(
            name=name,
            scope=scope,
            command=command,
            skip_when_state_in={"masked"},
            skip_message="service is already masked",
        )
        if skip_result is not None:
            return skip_result

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="mask_service",
        )
        return rtnval

    def unmask_service(
        self,
        name: str,
        scope: UbuntuServiceScope,
    ) -> UbuntuServiceActionResult:
        """
            Unmask a previously-masked service unit.  Skipped when the
            service is not masked.

            :param name: The service unit name.
            :param scope: The scope selector.

            :returns: The :class:`UbuntuServiceActionResult`.
        """
        command = _scope_prefix(scope) + ["unmask", name]

        if self._config.dry_run:
            rtnval = self._build_result(
                status=UbuntuServiceActionStatus.DRY_RUN,
                name=name,
                scope=scope,
                command=command,
            )
            return rtnval

        status_info = self.get_service_status(name=name, scope=scope)
        if status_info.enabled_state != "masked":
            message = "service is not masked"
            rtnval = self._build_result(
                status=UbuntuServiceActionStatus.SKIPPED,
                name=name,
                scope=scope,
                command=command,
                message=message,
            )
            return rtnval

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            scope=scope,
            operation="unmask_service",
        )
        return rtnval

    def reload_daemon(
        self,
        scope: UbuntuServiceScope,
    ) -> UbuntuServiceActionResult:
        """
            Reload the systemd manager configuration.  Never skips.

            :param scope: The scope selector.

            :returns: The :class:`UbuntuServiceActionResult`.
        """
        command = _scope_prefix(scope) + ["daemon-reload"]
        rtnval = self._dispatch_write(
            command=command,
            name="",
            scope=scope,
            operation="reload_daemon",
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_service_status(
        self,
        name: str,
        scope: UbuntuServiceScope,
    ) -> UbuntuServiceStatus:
        """
            Query a service's current state.

            Runs ``systemctl [--user] is-active <name>`` and ``systemctl
            [--user] show <name> --property=LoadState,UnitFileState,MainPID,
            Description``.  Neither call elevates.

            :param name: The service unit name.
            :param scope: The scope selector.

            :returns: The :class:`UbuntuServiceStatus` describing the
                      service's current state.
            :raises UbuntuServiceQueryError: When ``show`` fails with an
                                             unexpected stderr.
        """
        prefix = _scope_prefix(scope)

        is_active_command = prefix + ["is-active", name]
        active_state = self._query_active_state(command=is_active_command)

        if active_state == "not-found":
            rtnval = UbuntuServiceStatus(
                service_name=name,
                scope=scope,
                active_state="not-found",
                enabled_state="not-found",
                main_pid=None,
                description=None,
            )
            return rtnval

        show_command = prefix + [
            "show", name, "--property=" + _SHOW_PROPERTIES,
        ]
        properties = self._query_show_properties(command=show_command)

        enabled_state = properties.get("UnitFileState", "")
        if len(enabled_state) == 0:
            enabled_state = "not-found"

        load_state = properties.get("LoadState", "")
        if load_state == "not-found":
            enabled_state = "not-found"

        main_pid_raw = properties.get("MainPID", "0").strip()
        if len(main_pid_raw) == 0:
            main_pid = None
        else:
            main_pid_value = int(main_pid_raw)
            if main_pid_value == 0:
                main_pid = None
            else:
                main_pid = main_pid_value

        description_raw = properties.get("Description", "")
        if len(description_raw) == 0:
            description = None
        else:
            description = description_raw

        rtnval = UbuntuServiceStatus(
            service_name=name,
            scope=scope,
            active_state=active_state,
            enabled_state=enabled_state,
            main_pid=main_pid,
            description=description,
        )
        return rtnval

    def list_services(
        self,
        scope: UbuntuServiceScope,
    ) -> list[UbuntuServiceInfo]:
        """
            List all service units for the given scope.

            Runs ``systemctl [--user] list-units --type=service --all
            --no-legend --plain``.  Never elevates.

            :param scope: The scope selector.

            :returns: One :class:`UbuntuServiceInfo` per unit row.
            :raises UbuntuServiceQueryError: When the command fails.
        """
        command = _scope_prefix(scope) + [
            "list-units", "--type=service", "--all", "--no-legend", "--plain",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuServiceQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = self._parse_list_units_output(
            output=run_result.stdout,
            scope=scope,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dispatch_write(
        self,
        *,
        command: list[str],
        name: str,
        scope: UbuntuServiceScope,
        operation: str,
    ) -> UbuntuServiceActionResult:
        """
            Dispatch a write command, applying the scope-based elevation
            policy and short-circuiting dry-run.

            :param command: The full argv.
            :param name: The service name to echo onto the result.
            :param scope: The scope targeted.
            :param operation: Reserved for future auto-reporting.

            :returns: The :class:`UbuntuServiceActionResult`.
        """
        if scope == UbuntuServiceScope.USER:
            elevated_override = False
        else:
            elevated_override = None

        rtnval = self._dispatch(
            command=command,
            detail=name,
            operation=operation,
            elevated_override=elevated_override,
            service_name=name,
            scope=scope,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuServiceActionStatus,
        name: str,
        scope: UbuntuServiceScope,
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuServiceActionResult:
        """
            Construct an :class:`UbuntuServiceActionResult` without dispatching.

            :param status: The outcome status.
            :param name: The service name.
            :param scope: The scope targeted.
            :param command: The would-have-been argv.
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`UbuntuServiceActionResult`.
        """
        rtnval = UbuntuServiceActionResult(
            status=status,
            detail=name,
            command=list(command),
            service_name=name,
            scope=scope,
            message=message,
            stderr=stderr,
        )
        return rtnval

    def _maybe_skip_on_active(
        self,
        *,
        name: str,
        scope: UbuntuServiceScope,
        command: list[str],
        skip_when_state_in,
        skip_message: str,
    ) -> Optional[UbuntuServiceActionResult]:
        """
            Evaluate the dry-run short-circuit and the ``active_state``
            idempotency guard.

            :param name: The service name.
            :param scope: The scope targeted.
            :param command: The would-have-been argv, for dry-run capture.
            :param skip_when_state_in: States that trigger ``SKIPPED``.
            :param skip_message: Human-readable reason for the skip.

            :returns: A result when the dispatch should short-circuit; None
                      when the caller should proceed with the write.
        """
        if self._config.dry_run:
            dry_result = self._build_result(
                status=UbuntuServiceActionStatus.DRY_RUN,
                name=name,
                scope=scope,
                command=command,
            )
            return dry_result

        status_info = self.get_service_status(name=name, scope=scope)
        if status_info.active_state in skip_when_state_in:
            skip_result = self._build_result(
                status=UbuntuServiceActionStatus.SKIPPED,
                name=name,
                scope=scope,
                command=command,
                message=skip_message,
            )
            return skip_result

        return None

    def _maybe_skip_on_enabled(
        self,
        *,
        name: str,
        scope: UbuntuServiceScope,
        command: list[str],
        skip_when_state_in,
        skip_message: str,
    ) -> Optional[UbuntuServiceActionResult]:
        """
            Evaluate the dry-run short-circuit and the ``enabled_state``
            idempotency guard.

            :param name: The service name.
            :param scope: The scope targeted.
            :param command: The would-have-been argv, for dry-run capture.
            :param skip_when_state_in: States that trigger ``SKIPPED``.
            :param skip_message: Human-readable reason for the skip.

            :returns: A result when the dispatch should short-circuit; None
                      when the caller should proceed with the write.
        """
        if self._config.dry_run:
            dry_result = self._build_result(
                status=UbuntuServiceActionStatus.DRY_RUN,
                name=name,
                scope=scope,
                command=command,
            )
            return dry_result

        status_info = self.get_service_status(name=name, scope=scope)
        if status_info.enabled_state in skip_when_state_in:
            skip_result = self._build_result(
                status=UbuntuServiceActionStatus.SKIPPED,
                name=name,
                scope=scope,
                command=command,
                message=skip_message,
            )
            return skip_result

        return None

    def _query_active_state(self, *, command: list[str]) -> str:
        """
            Run ``systemctl is-active`` and translate the output into one of
            the known active-state strings.

            :param command: The full argv for ``is-active``.

            :returns: One of the known active-state strings.  ``unknown`` is
                      mapped to ``"not-found"``.
        """
        run_result = self._runner.run(list(command), elevated=False)
        reported = run_result.stdout.strip()
        if len(reported) == 0:
            reported = run_result.stderr.strip()

        if reported == "unknown":
            rtnval = "not-found"
        elif reported in _KNOWN_ACTIVE_STATES:
            rtnval = reported
        else:
            # Empty or unrecognised — treat as not-found to keep reads from
            # raising on informational non-zero exits.
            rtnval = "not-found"

        return rtnval

    def _query_show_properties(self, *, command: list[str]) -> dict:
        """
            Run ``systemctl show --property=...`` and parse ``key=value``
            lines into a dict.

            :param command: The full argv for ``show``.

            :returns: A dict of parsed properties.  Missing keys are simply
                      absent from the mapping.
            :raises UbuntuServiceQueryError: When the runner reports an
                                             unexpected stderr with a
                                             non-zero exit code.
        """
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise UbuntuServiceQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval: dict = {}
        for line in run_result.stdout.splitlines():
            stripped = line.strip()
            if len(stripped) == 0:
                continue
            if "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            rtnval[key] = value

        return rtnval

    def _parse_list_units_output(
        self,
        *,
        output: str,
        scope: UbuntuServiceScope,
    ) -> list[UbuntuServiceInfo]:
        """
            Parse the output of ``systemctl list-units --plain --no-legend``.

            Expected columns: ``UNIT  LOAD  ACTIVE  SUB  DESCRIPTION``.

            :param output: Raw stdout text.
            :param scope: The scope the listing came from.

            :returns: One :class:`UbuntuServiceInfo` per parsed row.
        """
        rtnval: list[UbuntuServiceInfo] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if len(line) == 0:
                continue

            # ``--plain --no-legend`` emits whitespace-separated columns.  The
            # description may contain single spaces, so we split on the first
            # four whitespace runs and keep the rest intact.
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue

            unit = parts[0]
            # parts[1] is LOAD, parts[2] is ACTIVE, parts[3] is SUB.
            active_state = parts[2]
            if len(parts) == 5:
                description = parts[4]
            else:
                description = None

            if unit.endswith(".service"):
                service_name = unit[:-len(".service")]
            else:
                service_name = unit

            info = UbuntuServiceInfo(
                service_name=service_name,
                scope=scope,
                active_state=active_state,
                enabled_state="",
                description=description,
            )
            rtnval.append(info)

        return rtnval
