"""
    Ubuntu precision-time-management feature.

    Implements the cross-OS precision-time-management contract for Ubuntu
    using either ``chrony`` (the default; ``chronyc`` + ``chronyd``) or
    ``systemd-timesyncd`` (``timedatectl``).  The selection is exposed via
    :class:`UbuntuTimeBackend` on
    :class:`UbuntuPrecisionTimeManagerConfig`; instances bind to a single
    backend at construction time.

    File I/O policy:
        Mutating verbs that need to rewrite ``/etc/chrony/chrony.conf`` or
        ``/etc/systemd/timesyncd.conf`` flow through the injected
        :class:`ICommandRunner` via ``cat`` / ``tee``.  This module does
        not consume an :class:`IFileOperator`; the cross-OS contract names
        ``IFileOperator`` as a permitted collaborator but the locked-in
        Ubuntu pattern across this batch is to route file I/O through the
        runner to match the rest of the package.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``get_time_status``, ``get_time_sources``, ``get_drift``,
        idempotency probes) never elevate.

    Backend dispatch:
        * :data:`UbuntuTimeBackend.CHRONY` (default) — ``chronyc tracking``,
          ``chronyc sources``, ``chronyc makestep``, plus
          ``cat`` / ``tee`` against the chrony config and
          ``systemctl`` against the chrony service.
        * :data:`UbuntuTimeBackend.TIMESYNCD` — ``timedatectl status``,
          ``timedatectl show``, ``timedatectl set-ntp`` plus
          ``cat`` / ``tee`` against ``/etc/systemd/timesyncd.conf`` and
          ``systemctl`` against ``systemd-timesyncd``.
        * :data:`UbuntuTimeBackend.NTPD` — accepted by the config enum but
          not yet implemented; constructing a manager with this backend
          raises :class:`NotImplementedError` at first use.

    macOS multi-server limitation does not apply here; chrony and
    timesyncd both natively support multi-server lists.
"""

import os

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_CHRONYC = "chronyc"
_TIMEDATECTL = "timedatectl"
_SYSTEMCTL = "systemctl"
_CAT = "cat"
_TEE = "tee"

_DEFAULT_CHRONY_CONF = "/etc/chrony/chrony.conf"
_DEFAULT_TIMESYNCD_CONF = "/etc/systemd/timesyncd.conf"
_CHRONY_SERVICE = "chrony"
_TIMESYNCD_SERVICE = "systemd-timesyncd"


class UbuntuTimeBackend(Enum):
    """
        Selector for the underlying NTP backend.

        :cvar CHRONY: Use chrony via ``chronyc`` and the chrony service.
                      The Ubuntu default in this package.
        :cvar TIMESYNCD: Use systemd-timesyncd via ``timedatectl`` and
                         the ``systemd-timesyncd`` unit.
        :cvar NTPD: Reserved for a future ``ntpd`` backend.  Selecting it
                    today raises ``NotImplementedError`` from the manager.
    """
    CHRONY = "chrony"
    TIMESYNCD = "timesyncd"
    NTPD = "ntpd"


UbuntuPrecisionTimeActionStatus = ActionStatus


@dataclass
class UbuntuPrecisionTimeManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuPrecisionTimeManager`.

        Inherits ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param backend: The NTP backend.  Defaults to
                        :data:`UbuntuTimeBackend.CHRONY`.
        :param chrony_conf: Absolute path to the chrony configuration
                            file used for source mutations under the
                            chrony backend.
        :param timesyncd_conf: Absolute path to the systemd-timesyncd
                               configuration file used for source
                               mutations under the timesyncd backend.
        :param service_name: Optional override for the service unit
                             name.  ``None`` means auto-derive from
                             ``backend`` (``"chrony"`` or
                             ``"systemd-timesyncd"``).
    """
    backend: UbuntuTimeBackend = UbuntuTimeBackend.CHRONY
    chrony_conf: str = _DEFAULT_CHRONY_CONF
    timesyncd_conf: str = _DEFAULT_TIMESYNCD_CONF
    service_name: Optional[str] = None


@dataclass
class UbuntuPrecisionTimeActionResult(FeatureActionResult):
    """
        Result of a precision-time write operation.

        :param server: The server hostname or address the action targeted,
                       or ``None`` for verbs that do not target a single
                       server (``force_sync_now``, ``enable_sync``,
                       ``disable_sync``).
    """
    server: Optional[str] = None


@dataclass
class UbuntuTimeStatus:
    """
        Snapshot of the host's overall time-synchronisation status.

        :param enabled: Whether the sync service is enabled / running.
        :param synchronized: Whether the host's clock is currently
                             considered synchronised.
        :param current_server: The selected upstream source, or ``None``
                               when no source is selected.
        :param stratum: NTP stratum of the selected source, or ``None``
                        when not reported.
        :param last_sync: Last successful sync timestamp as a string
                          (native format from the backend), or ``None``.
        :param offset_seconds: Offset from the selected source in
                               seconds, or ``None`` when not reported.
    """
    enabled: bool
    synchronized: bool
    current_server: Optional[str]
    stratum: Optional[int]
    last_sync: Optional[str]
    offset_seconds: Optional[float]


@dataclass
class UbuntuTimeSourceInfo:
    """
        One configured upstream time source.

        :param server: Hostname or address of the source.
        :param state: Native state token (``"selected"``,
                      ``"candidate"``, ``"unreachable"``).
        :param stratum: Source stratum, or ``None`` when not reported.
        :param offset_seconds: Last measured offset in seconds, or
                               ``None``.
        :param last_response: Native last-response token (e.g. chrony's
                              ``"LastRx"``), or ``None``.
    """
    server: str
    state: str
    stratum: Optional[int]
    offset_seconds: Optional[float]
    last_response: Optional[str]


@dataclass
class UbuntuTimeDrift:
    """
        Measured clock-drift snapshot.

        :param offset_seconds: Smoothed offset from the selected source
                               in seconds.
        :param frequency_ppm: Frequency adjustment in parts per million,
                              or ``None`` when the backend does not
                              report it.
        :param jitter_seconds: Jitter estimate in seconds, or ``None``.
        :param stratum: Stratum of the current source, or ``None``.
    """
    offset_seconds: float
    frequency_ppm: Optional[float]
    jitter_seconds: Optional[float]
    stratum: Optional[int]


class UbuntuPrecisionTimeQueryError(Exception):
    """
        Raised when a precision-time read fails for a reason other than
        the sync service simply not running.
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

        err_msg_lines = ["Precision-time query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuPrecisionTimeManager(BaseFeatureManager):
    """
        Ubuntu precision-time manager.

        Dispatches every operation through the injected
        :class:`ICommandRunner`.  At construction time the manager binds
        to the backend declared in :class:`UbuntuPrecisionTimeManagerConfig`
        and routes every verb through the matching tool family.
    """

    result_class = UbuntuPrecisionTimeActionResult
    status_class = UbuntuPrecisionTimeActionStatus
    feature_name = "precision-time-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_time_status(self) -> UbuntuTimeStatus:
        """
            Query the host's overall sync status.

            Routes to ``chronyc tracking`` under the chrony backend or
            ``timedatectl status`` under the timesyncd backend.  Never
            elevates.

            When the sync service is not running the method returns a
            ``UbuntuTimeStatus`` with ``enabled=False`` and
            ``synchronized=False`` rather than raising.

            :returns: The :class:`UbuntuTimeStatus`.
            :raises UbuntuPrecisionTimeQueryError: When the backend tool
                                                    fails for a reason
                                                    other than the
                                                    service simply being
                                                    inactive.
        """
        backend = self._config.backend
        if backend == UbuntuTimeBackend.CHRONY:
            rtnval = self._chrony_get_time_status()
            return rtnval
        if backend == UbuntuTimeBackend.TIMESYNCD:
            rtnval = self._timesyncd_get_time_status()
            return rtnval
        self._unsupported_backend()
        return UbuntuTimeStatus(
            enabled=False,
            synchronized=False,
            current_server=None,
            stratum=None,
            last_sync=None,
            offset_seconds=None,
        )

    def get_time_sources(self) -> list[UbuntuTimeSourceInfo]:
        """
            Enumerate currently configured upstream time sources.

            Routes to ``chronyc sources`` under the chrony backend or
            ``timedatectl show`` under the timesyncd backend.  Never
            elevates.

            :returns: One :class:`UbuntuTimeSourceInfo` per source.  May
                      be empty when no sources are configured or the
                      service is inactive.
            :raises UbuntuPrecisionTimeQueryError: When the backend tool
                                                    fails unexpectedly.
        """
        backend = self._config.backend
        if backend == UbuntuTimeBackend.CHRONY:
            rtnval = self._chrony_get_time_sources()
            return rtnval
        if backend == UbuntuTimeBackend.TIMESYNCD:
            rtnval = self._timesyncd_get_time_sources()
            return rtnval
        self._unsupported_backend()
        return []

    def get_drift(self) -> UbuntuTimeDrift:
        """
            Read the current drift snapshot.

            Routes to ``chronyc tracking`` under the chrony backend or
            ``timedatectl show`` under the timesyncd backend.  Never
            elevates.

            :returns: The :class:`UbuntuTimeDrift`.
            :raises UbuntuPrecisionTimeQueryError: When the backend tool
                                                    fails unexpectedly.
        """
        backend = self._config.backend
        if backend == UbuntuTimeBackend.CHRONY:
            rtnval = self._chrony_get_drift()
            return rtnval
        if backend == UbuntuTimeBackend.TIMESYNCD:
            rtnval = self._timesyncd_get_drift()
            return rtnval
        self._unsupported_backend()
        return UbuntuTimeDrift(
            offset_seconds=0.0,
            frequency_ppm=None,
            jitter_seconds=None,
            stratum=None,
        )

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_time_source(self, server: str) -> UbuntuPrecisionTimeActionResult:
        """
            Add an upstream time source.

            Idempotent: ``SKIPPED`` when the server is already present
            in the active configuration.

            :param server: Server hostname or address.

            :returns: The :class:`UbuntuPrecisionTimeActionResult`.
        """
        action = "add_time_source:{}".format(server)
        backend = self._config.backend
        conf_path = self._conf_path()
        write_command = [_TEE, conf_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuPrecisionTimeActionStatus.DRY_RUN,
                detail=action,
                command=write_command,
                server=server,
            )
            return dry

        existing_text = self._read_file(path=conf_path)
        current_servers = self._parse_servers_from_conf(
            text=existing_text, backend=backend,
        )
        if server in current_servers:
            skipped = self._build_result(
                status=UbuntuPrecisionTimeActionStatus.SKIPPED,
                detail=action,
                command=write_command,
                server=server,
            )
            return skipped

        new_servers = list(current_servers)
        new_servers.append(server)
        rtnval = self._rewrite_servers_and_reload(
            backend=backend,
            conf_path=conf_path,
            existing_text=existing_text,
            new_servers=new_servers,
            detail=action,
            server=server,
        )
        return rtnval

    def remove_time_source(
        self, server: str,
    ) -> UbuntuPrecisionTimeActionResult:
        """
            Remove an upstream time source.

            Idempotent: ``SKIPPED`` when the server is not currently
            configured.

            :param server: Server hostname or address.

            :returns: The :class:`UbuntuPrecisionTimeActionResult`.
        """
        action = "remove_time_source:{}".format(server)
        backend = self._config.backend
        conf_path = self._conf_path()
        write_command = [_TEE, conf_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuPrecisionTimeActionStatus.DRY_RUN,
                detail=action,
                command=write_command,
                server=server,
            )
            return dry

        existing_text = self._read_file(path=conf_path)
        current_servers = self._parse_servers_from_conf(
            text=existing_text, backend=backend,
        )
        if server not in current_servers:
            skipped = self._build_result(
                status=UbuntuPrecisionTimeActionStatus.SKIPPED,
                detail=action,
                command=write_command,
                server=server,
            )
            return skipped

        new_servers = [item for item in current_servers if item != server]
        rtnval = self._rewrite_servers_and_reload(
            backend=backend,
            conf_path=conf_path,
            existing_text=existing_text,
            new_servers=new_servers,
            detail=action,
            server=server,
        )
        return rtnval

    def set_time_servers(
        self, servers: list[str],
    ) -> UbuntuPrecisionTimeActionResult:
        """
            Replace the active set of upstream sources.

            Idempotent: ``SKIPPED`` when the current and desired lists
            are equal under sorted comparison.

            :param servers: Ordered list of desired servers.

            :returns: The :class:`UbuntuPrecisionTimeActionResult`.
        """
        action = "set_time_servers:{}".format(",".join(servers))
        backend = self._config.backend
        conf_path = self._conf_path()
        write_command = [_TEE, conf_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuPrecisionTimeActionStatus.DRY_RUN,
                detail=action,
                command=write_command,
                server=None,
            )
            return dry

        existing_text = self._read_file(path=conf_path)
        current_servers = self._parse_servers_from_conf(
            text=existing_text, backend=backend,
        )
        if sorted(current_servers) == sorted(servers):
            skipped = self._build_result(
                status=UbuntuPrecisionTimeActionStatus.SKIPPED,
                detail=action,
                command=write_command,
                server=None,
            )
            return skipped

        rtnval = self._rewrite_servers_and_reload(
            backend=backend,
            conf_path=conf_path,
            existing_text=existing_text,
            new_servers=list(servers),
            detail=action,
            server=None,
        )
        return rtnval

    def force_sync_now(self) -> UbuntuPrecisionTimeActionResult:
        """
            Force an immediate synchronisation with the active source.

            Under chrony this dispatches ``chronyc makestep``; under
            timesyncd this restarts ``systemd-timesyncd`` (which causes
            it to re-poll its sources on startup).

            :returns: The :class:`UbuntuPrecisionTimeActionResult`.
        """
        backend = self._config.backend
        if backend == UbuntuTimeBackend.CHRONY:
            command = [_CHRONYC, "makestep"]
        elif backend == UbuntuTimeBackend.TIMESYNCD:
            command = [_SYSTEMCTL, "restart", self._service_name()]
        else:
            self._unsupported_backend()
            command = []

        rtnval = self._dispatch_write(
            command=command,
            detail="force_sync_now",
            server=None,
        )
        return rtnval

    def enable_sync(self) -> UbuntuPrecisionTimeActionResult:
        """
            Enable the sync service.

            Idempotent: ``SKIPPED`` when the service is already enabled
            and active.

            :returns: The :class:`UbuntuPrecisionTimeActionResult`.
        """
        backend = self._config.backend
        if backend == UbuntuTimeBackend.CHRONY:
            command = [
                _SYSTEMCTL, "enable", "--now", self._service_name(),
            ]
        elif backend == UbuntuTimeBackend.TIMESYNCD:
            command = [_TIMEDATECTL, "set-ntp", "true"]
        else:
            self._unsupported_backend()
            command = []

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuPrecisionTimeActionStatus.DRY_RUN,
                detail="enable_sync",
                command=command,
                server=None,
            )
            return dry

        if self._sync_is_enabled():
            skipped = self._build_result(
                status=UbuntuPrecisionTimeActionStatus.SKIPPED,
                detail="enable_sync",
                command=command,
                server=None,
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            detail="enable_sync",
            server=None,
        )
        return rtnval

    def disable_sync(self) -> UbuntuPrecisionTimeActionResult:
        """
            Disable the sync service.

            Idempotent: ``SKIPPED`` when the service is already
            disabled.

            :returns: The :class:`UbuntuPrecisionTimeActionResult`.
        """
        backend = self._config.backend
        if backend == UbuntuTimeBackend.CHRONY:
            command = [
                _SYSTEMCTL, "disable", "--now", self._service_name(),
            ]
        elif backend == UbuntuTimeBackend.TIMESYNCD:
            command = [_TIMEDATECTL, "set-ntp", "false"]
        else:
            self._unsupported_backend()
            command = []

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuPrecisionTimeActionStatus.DRY_RUN,
                detail="disable_sync",
                command=command,
                server=None,
            )
            return dry

        if not self._sync_is_enabled():
            skipped = self._build_result(
                status=UbuntuPrecisionTimeActionStatus.SKIPPED,
                detail="disable_sync",
                command=command,
                server=None,
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            detail="disable_sync",
            server=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Backend-specific reads
    # ------------------------------------------------------------------

    def _chrony_get_time_status(self) -> UbuntuTimeStatus:
        """
            Implement :meth:`get_time_status` for the chrony backend.

            :returns: The :class:`UbuntuTimeStatus`.
        """
        command = [_CHRONYC, "tracking"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_text = run_result.stderr or ""
            if _stderr_indicates_inactive(stderr=stderr_text):
                rtnval = UbuntuTimeStatus(
                    enabled=False,
                    synchronized=False,
                    current_server=None,
                    stratum=None,
                    last_sync=None,
                    offset_seconds=None,
                )
                return rtnval
            raise UbuntuPrecisionTimeQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=stderr_text,
            )

        rtnval = _parse_chrony_tracking_status(text=run_result.stdout)
        return rtnval

    def _chrony_get_time_sources(self) -> list[UbuntuTimeSourceInfo]:
        """
            Implement :meth:`get_time_sources` for the chrony backend.

            :returns: One :class:`UbuntuTimeSourceInfo` per source.
        """
        command = [_CHRONYC, "sources"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_text = run_result.stderr or ""
            if _stderr_indicates_inactive(stderr=stderr_text):
                rtnval: list[UbuntuTimeSourceInfo] = []
                return rtnval
            raise UbuntuPrecisionTimeQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=stderr_text,
            )

        rtnval = _parse_chrony_sources(text=run_result.stdout)
        return rtnval

    def _chrony_get_drift(self) -> UbuntuTimeDrift:
        """
            Implement :meth:`get_drift` for the chrony backend.

            :returns: The :class:`UbuntuTimeDrift`.
        """
        command = [_CHRONYC, "tracking"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_text = run_result.stderr or ""
            if _stderr_indicates_inactive(stderr=stderr_text):
                rtnval = UbuntuTimeDrift(
                    offset_seconds=0.0,
                    frequency_ppm=None,
                    jitter_seconds=None,
                    stratum=None,
                )
                return rtnval
            raise UbuntuPrecisionTimeQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=stderr_text,
            )

        rtnval = _parse_chrony_tracking_drift(text=run_result.stdout)
        return rtnval

    def _timesyncd_get_time_status(self) -> UbuntuTimeStatus:
        """
            Implement :meth:`get_time_status` for the timesyncd backend.

            :returns: The :class:`UbuntuTimeStatus`.
        """
        command = [_TIMEDATECTL, "status"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuPrecisionTimeQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )

        rtnval = _parse_timedatectl_status(text=run_result.stdout)
        return rtnval

    def _timesyncd_get_time_sources(self) -> list[UbuntuTimeSourceInfo]:
        """
            Implement :meth:`get_time_sources` for the timesyncd backend.

            :returns: One :class:`UbuntuTimeSourceInfo` per source.
        """
        command = [
            _TIMEDATECTL, "show",
            "--property=NTP",
            "--property=NTPSynchronized",
            "--property=SystemNTPServers",
            "--property=FallbackNTPServers",
            "--property=ServerName",
            "--property=ServerAddress",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuPrecisionTimeQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )

        rtnval = _parse_timedatectl_show_sources(text=run_result.stdout)
        return rtnval

    def _timesyncd_get_drift(self) -> UbuntuTimeDrift:
        """
            Implement :meth:`get_drift` for the timesyncd backend.

            :returns: The :class:`UbuntuTimeDrift`.
        """
        command = [
            _TIMEDATECTL, "show",
            "--property=NTPMessage",
            "--property=ServerName",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuPrecisionTimeQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )

        rtnval = _parse_timedatectl_show_drift(text=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _conf_path(self) -> str:
        """
            Resolve the active backend's config-file path.

            :returns: The absolute path of the chrony or timesyncd
                      config file.
        """
        backend = self._config.backend
        if backend == UbuntuTimeBackend.CHRONY:
            rtnval = self._config.chrony_conf
            return rtnval
        if backend == UbuntuTimeBackend.TIMESYNCD:
            rtnval = self._config.timesyncd_conf
            return rtnval
        self._unsupported_backend()
        return ""

    def _service_name(self) -> str:
        """
            Resolve the active backend's systemd unit name.

            :returns: The unit name (e.g. ``"chrony"`` or
                      ``"systemd-timesyncd"``).
        """
        if self._config.service_name is not None:
            rtnval = self._config.service_name
            return rtnval
        backend = self._config.backend
        if backend == UbuntuTimeBackend.CHRONY:
            rtnval = _CHRONY_SERVICE
            return rtnval
        if backend == UbuntuTimeBackend.TIMESYNCD:
            rtnval = _TIMESYNCD_SERVICE
            return rtnval
        self._unsupported_backend()
        return ""

    def _unsupported_backend(self) -> None:
        """
            Raise ``NotImplementedError`` for backends that the manager
            does not yet support.

            :raises NotImplementedError: Always.
        """
        err_msg_lines = ["Backend is not yet supported by this manager."]
        err_msg_lines.append(
            "    BACKEND: {}".format(self._config.backend.value),
        )
        err_msg = os.linesep.join(err_msg_lines)
        raise NotImplementedError(err_msg)

    def _read_file(self, *, path: str) -> str:
        """
            Read a file via ``cat``.

            :param path: Absolute path.

            :returns: File contents on success; empty string when the
                      file is missing.
        """
        command = [_CAT, path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = ""
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _parse_servers_from_conf(
        self, *, text: str, backend: UbuntuTimeBackend,
    ) -> list[str]:
        """
            Parse the configured server list out of a backend's config
            file content.

            :param text: Raw file contents.
            :param backend: The active backend.

            :returns: Configured servers in declaration order.
        """
        if backend == UbuntuTimeBackend.CHRONY:
            rtnval = _parse_chrony_conf_servers(text=text)
            return rtnval
        if backend == UbuntuTimeBackend.TIMESYNCD:
            rtnval = _parse_timesyncd_conf_servers(text=text)
            return rtnval
        self._unsupported_backend()
        return []

    def _render_servers_into_conf(
        self,
        *,
        existing_text: str,
        servers: list[str],
        backend: UbuntuTimeBackend,
    ) -> str:
        """
            Render a desired set of servers back into the existing
            config-file body.

            :param existing_text: Raw existing file contents.
            :param servers: Desired servers in order.
            :param backend: The active backend.

            :returns: New file body with source declarations replaced.
        """
        if backend == UbuntuTimeBackend.CHRONY:
            rtnval = _render_chrony_conf_servers(
                existing_text=existing_text, servers=servers,
            )
            return rtnval
        if backend == UbuntuTimeBackend.TIMESYNCD:
            rtnval = _render_timesyncd_conf_servers(
                existing_text=existing_text, servers=servers,
            )
            return rtnval
        self._unsupported_backend()
        return ""

    def _rewrite_servers_and_reload(
        self,
        *,
        backend: UbuntuTimeBackend,
        conf_path: str,
        existing_text: str,
        new_servers: list[str],
        detail: str,
        server: Optional[str],
    ) -> UbuntuPrecisionTimeActionResult:
        """
            Rewrite the config file and reload the sync service.

            :param backend: The active backend.
            :param conf_path: Absolute config-file path.
            :param existing_text: Raw existing file contents.
            :param new_servers: Desired server list.
            :param detail: ``detail`` value to echo on the result.
            :param server: ``server`` value to echo on the result.

            :returns: The :class:`UbuntuPrecisionTimeActionResult`.
        """
        new_text = self._render_servers_into_conf(
            existing_text=existing_text,
            servers=new_servers,
            backend=backend,
        )

        write_command = [_TEE, conf_path]
        write_run = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_text,
        )
        if write_run.exit_code != 0:
            failed = UbuntuPrecisionTimeActionResult(
                status=UbuntuPrecisionTimeActionStatus.FAILED,
                detail=detail,
                exit_code=write_run.exit_code,
                stdout=write_run.stdout,
                stderr=write_run.stderr,
                command=list(write_command),
                server=server,
            )
            return failed

        reload_command = [
            _SYSTEMCTL, "restart", self._service_name(),
        ]
        reload_run = self._runner.run(
            list(reload_command),
            elevated=self._config.elevated,
        )
        if reload_run.exit_code != 0:
            failed = UbuntuPrecisionTimeActionResult(
                status=UbuntuPrecisionTimeActionStatus.FAILED,
                detail=detail,
                exit_code=reload_run.exit_code,
                stdout=reload_run.stdout,
                stderr=reload_run.stderr,
                command=list(reload_command),
                server=server,
            )
            return failed

        success = UbuntuPrecisionTimeActionResult(
            status=UbuntuPrecisionTimeActionStatus.SUCCESS,
            detail=detail,
            exit_code=reload_run.exit_code,
            stdout=reload_run.stdout,
            stderr=reload_run.stderr,
            command=list(reload_command),
            server=server,
        )
        return success

    def _sync_is_enabled(self) -> bool:
        """
            Probe whether the sync service is currently enabled.

            :returns: ``True`` when enabled / running; ``False``
                      otherwise.
        """
        backend = self._config.backend
        if backend == UbuntuTimeBackend.CHRONY:
            command = [_SYSTEMCTL, "is-active", self._service_name()]
            run_result = self._runner.run(list(command), elevated=False)
            if run_result.exit_code == 0:
                rtnval = True
                return rtnval
            rtnval = False
            return rtnval
        if backend == UbuntuTimeBackend.TIMESYNCD:
            command = [
                _TIMEDATECTL, "show", "--property=NTP", "--value",
            ]
            run_result = self._runner.run(list(command), elevated=False)
            if run_result.exit_code != 0:
                rtnval = False
                return rtnval
            stripped = run_result.stdout.strip().lower()
            if stripped == "yes":
                rtnval = True
                return rtnval
            rtnval = False
            return rtnval
        self._unsupported_backend()
        return False

    def _dispatch_write(
        self,
        *,
        command: list[str],
        detail: str,
        server: Optional[str],
    ) -> UbuntuPrecisionTimeActionResult:
        """
            Dispatch a single-shot write command and wrap the result.

            :param command: The full argv.
            :param detail: ``detail`` value to echo on the result.
            :param server: ``server`` value to echo on the result.

            :returns: The :class:`UbuntuPrecisionTimeActionResult`.
        """
        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuPrecisionTimeActionStatus.DRY_RUN,
                detail=detail,
                command=command,
                server=server,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = UbuntuPrecisionTimeActionStatus.SUCCESS
        else:
            mapped_status = UbuntuPrecisionTimeActionStatus.FAILED

        rtnval = UbuntuPrecisionTimeActionResult(
            status=mapped_status,
            detail=detail,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            server=server,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuPrecisionTimeActionStatus,
        detail: str,
        command: list[str],
        server: Optional[str],
    ) -> UbuntuPrecisionTimeActionResult:
        """
            Construct an :class:`UbuntuPrecisionTimeActionResult` without
            invoking the runner.

            :param status: Outcome status.
            :param detail: ``detail`` value to echo on the result.
            :param command: ``command`` argv to record.
            :param server: ``server`` value to echo on the result.

            :returns: The :class:`UbuntuPrecisionTimeActionResult`.
        """
        rtnval = UbuntuPrecisionTimeActionResult(
            status=status,
            detail=detail,
            command=list(command),
            server=server,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level renderers and parsers
# ----------------------------------------------------------------------


def _stderr_indicates_inactive(*, stderr: str) -> bool:
    """
        Heuristic for "the sync service simply is not running".

        :param stderr: Captured stderr text.

        :returns: True when stderr matches a known inactive-service
                  signature.
    """
    haystack = (stderr or "").lower()
    needles = (
        "506 cannot talk to daemon",
        "no such file or directory",
        "connection refused",
        "could not connect",
    )
    for needle in needles:
        if needle in haystack:
            rtnval = True
            return rtnval
    rtnval = False
    return rtnval


def _parse_chrony_tracking_status(*, text: str) -> UbuntuTimeStatus:
    """
        Parse ``chronyc tracking`` output into a status snapshot.

        :param text: Raw stdout.

        :returns: The :class:`UbuntuTimeStatus`.
    """
    fields = _parse_keyed_lines(text=text, separator=":")

    reference_id = fields.get("Reference ID", "")
    current_server: Optional[str] = None
    if len(reference_id) > 0:
        # ``Reference ID    : C0A80001 (foo.example.com)``
        if "(" in reference_id and reference_id.endswith(")"):
            head, _, tail = reference_id.partition("(")
            name = tail.rstrip(")").strip()
            if len(name) > 0:
                current_server = name
        if current_server is None:
            current_server = reference_id.split()[0]

    stratum = _parse_optional_int(value=fields.get("Stratum", ""))

    last_sync_raw = fields.get("Ref time (UTC)", "")
    last_sync: Optional[str]
    if len(last_sync_raw) > 0:
        last_sync = last_sync_raw
    else:
        last_sync = None

    offset = _parse_chrony_offset_field(
        value=fields.get("Last offset", ""),
    )

    leap_status = fields.get("Leap status", "").lower()
    if "normal" in leap_status:
        synchronized = True
    elif "synchronised" in leap_status or "synchronized" in leap_status:
        synchronized = True
    else:
        synchronized = False

    rtnval = UbuntuTimeStatus(
        enabled=True,
        synchronized=synchronized,
        current_server=current_server,
        stratum=stratum,
        last_sync=last_sync,
        offset_seconds=offset,
    )
    return rtnval


def _parse_chrony_tracking_drift(*, text: str) -> UbuntuTimeDrift:
    """
        Parse ``chronyc tracking`` output into a drift snapshot.

        :param text: Raw stdout.

        :returns: The :class:`UbuntuTimeDrift`.
    """
    fields = _parse_keyed_lines(text=text, separator=":")

    offset = _parse_chrony_offset_field(
        value=fields.get("Last offset", ""),
    )
    if offset is None:
        offset = 0.0

    frequency = _parse_chrony_frequency_ppm(
        value=fields.get("Frequency", ""),
    )
    jitter = _parse_chrony_offset_field(
        value=fields.get("RMS offset", ""),
    )
    stratum = _parse_optional_int(value=fields.get("Stratum", ""))

    rtnval = UbuntuTimeDrift(
        offset_seconds=offset,
        frequency_ppm=frequency,
        jitter_seconds=jitter,
        stratum=stratum,
    )
    return rtnval


def _parse_chrony_sources(*, text: str) -> list[UbuntuTimeSourceInfo]:
    """
        Parse ``chronyc sources`` columnar output.

        Sample lines::

            MS Name/IP address         Stratum Poll Reach LastRx Last sample
            ===============================================================
            ^* foo.ntp.example.com           2   6   377    32   +12us[+18us] +/- 9ms
            ^- bar.ntp.example.com           3   6   377    33   -42us[-37us] +/- 22ms

        :param text: Raw stdout.

        :returns: One :class:`UbuntuTimeSourceInfo` per data row.
    """
    rtnval: list[UbuntuTimeSourceInfo] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("="):
            continue
        if stripped.startswith("MS Name") or stripped.startswith("M Name"):
            continue

        # Mode/state token is the first whitespace-delimited token.
        parts = stripped.split()
        if len(parts) < 6:
            continue

        mode_state = parts[0]
        server = parts[1]
        stratum = _parse_optional_int(value=parts[2])
        last_rx = parts[5]

        state = _classify_chrony_state(mode_state=mode_state)

        offset_seconds: Optional[float] = None
        if len(parts) >= 7:
            offset_seconds = _parse_chrony_sample_offset(value=parts[6])

        info = UbuntuTimeSourceInfo(
            server=server,
            state=state,
            stratum=stratum,
            offset_seconds=offset_seconds,
            last_response=last_rx,
        )
        rtnval.append(info)
    return rtnval


def _classify_chrony_state(*, mode_state: str) -> str:
    """
        Map a chronyc MS column token onto the contract's
        ``selected``/``candidate``/``unreachable`` vocabulary.

        :param mode_state: The two-character ``MS`` token.

        :returns: The mapped state token.
    """
    if len(mode_state) < 2:
        rtnval = "candidate"
        return rtnval
    state_char = mode_state[1]
    if state_char == "*":
        rtnval = "selected"
        return rtnval
    if state_char == "+":
        rtnval = "candidate"
        return rtnval
    if state_char == "-":
        rtnval = "candidate"
        return rtnval
    if state_char == "?":
        rtnval = "unreachable"
        return rtnval
    if state_char == "x":
        rtnval = "unreachable"
        return rtnval
    rtnval = "candidate"
    return rtnval


def _parse_chrony_offset_field(*, value: str) -> Optional[float]:
    """
        Parse a chrony ``Last offset`` / ``RMS offset`` field
        (``"+0.000123456 seconds"``) into seconds.

        :param value: Raw field value.

        :returns: Offset in seconds, or ``None`` when unparseable.
    """
    stripped = value.strip()
    if len(stripped) == 0:
        rtnval: Optional[float] = None
        return rtnval
    head = stripped.split()[0]
    try:
        parsed = float(head)
    except ValueError:
        rtnval = None
        return rtnval
    rtnval = parsed
    return rtnval


def _parse_chrony_sample_offset(*, value: str) -> Optional[float]:
    """
        Parse a chronyc sample-offset token like ``+12us`` or ``-42ms``.

        :param value: Token value.

        :returns: Offset in seconds, or ``None`` when unparseable.
    """
    stripped = value.strip()
    if len(stripped) == 0:
        rtnval: Optional[float] = None
        return rtnval
    if stripped.endswith("ns"):
        head = stripped[:-2]
        scale = 1e-9
    elif stripped.endswith("us"):
        head = stripped[:-2]
        scale = 1e-6
    elif stripped.endswith("ms"):
        head = stripped[:-2]
        scale = 1e-3
    elif stripped.endswith("s"):
        head = stripped[:-1]
        scale = 1.0
    else:
        head = stripped
        scale = 1.0
    try:
        parsed = float(head)
    except ValueError:
        rtnval = None
        return rtnval
    rtnval = parsed * scale
    return rtnval


def _parse_chrony_frequency_ppm(*, value: str) -> Optional[float]:
    """
        Parse a chrony ``Frequency`` field
        (``"12.345 ppm slow"``) into PPM.

        :param value: Raw field value.

        :returns: Frequency in PPM, or ``None``.
    """
    stripped = value.strip()
    if len(stripped) == 0:
        rtnval: Optional[float] = None
        return rtnval
    parts = stripped.split()
    if len(parts) == 0:
        rtnval = None
        return rtnval
    try:
        parsed = float(parts[0])
    except ValueError:
        rtnval = None
        return rtnval
    if len(parts) >= 3 and parts[2].lower() == "slow":
        parsed = -parsed
    rtnval = parsed
    return rtnval


def _parse_optional_int(*, value: str) -> Optional[int]:
    """
        Parse a numeric field, returning ``None`` on failure / blank.

        :param value: Raw value.

        :returns: Integer or ``None``.
    """
    stripped = value.strip()
    if len(stripped) == 0:
        rtnval: Optional[int] = None
        return rtnval
    head = stripped.split()[0]
    try:
        parsed = int(head)
    except ValueError:
        rtnval = None
        return rtnval
    rtnval = parsed
    return rtnval


def _parse_keyed_lines(*, text: str, separator: str) -> dict:
    """
        Split a multi-line ``key<separator>value`` payload into a dict.

        :param text: Raw text.
        :param separator: Separator string.

        :returns: Mapping from key to value (last write wins).
    """
    rtnval: dict = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if separator not in line:
            continue
        head, _, tail = line.partition(separator)
        key = head.strip()
        value = tail.strip()
        if len(key) == 0:
            continue
        rtnval[key] = value
    return rtnval


def _parse_timedatectl_status(*, text: str) -> UbuntuTimeStatus:
    """
        Parse ``timedatectl status`` into a status snapshot.

        :param text: Raw stdout.

        :returns: The :class:`UbuntuTimeStatus`.
    """
    fields = _parse_keyed_lines(text=text, separator=":")

    enabled_raw = fields.get(
        "NTP service",
        fields.get("System clock synchronized", ""),
    ).lower()
    if "active" in enabled_raw:
        enabled = True
    elif enabled_raw == "yes":
        enabled = True
    else:
        enabled = False

    sync_raw = fields.get("System clock synchronized", "").lower()
    if sync_raw == "yes":
        synchronized = True
    else:
        synchronized = False

    last_sync_raw = fields.get("Universal time", "")
    last_sync: Optional[str]
    if len(last_sync_raw) > 0:
        last_sync = last_sync_raw
    else:
        last_sync = None

    rtnval = UbuntuTimeStatus(
        enabled=enabled,
        synchronized=synchronized,
        current_server=None,
        stratum=None,
        last_sync=last_sync,
        offset_seconds=None,
    )
    return rtnval


def _parse_timedatectl_show_sources(
    *, text: str,
) -> list[UbuntuTimeSourceInfo]:
    """
        Parse ``timedatectl show --property=...`` source listing.

        :param text: Raw stdout.

        :returns: One :class:`UbuntuTimeSourceInfo` per declared
                  server.  The current ``ServerName`` (when present) is
                  marked ``"selected"``; remaining entries are
                  ``"candidate"``.
    """
    rtnval: list[UbuntuTimeSourceInfo] = []
    fields = _parse_keyed_lines(text=text, separator="=")

    primary = fields.get("ServerName", "").strip()

    declared: list[str] = []
    for key in ("SystemNTPServers", "FallbackNTPServers"):
        raw = fields.get(key, "").strip()
        if len(raw) == 0:
            continue
        for token in raw.split():
            if token not in declared:
                declared.append(token)

    if len(primary) > 0 and primary not in declared:
        declared.insert(0, primary)

    for server in declared:
        if server == primary and len(primary) > 0:
            state = "selected"
        else:
            state = "candidate"
        info = UbuntuTimeSourceInfo(
            server=server,
            state=state,
            stratum=None,
            offset_seconds=None,
            last_response=None,
        )
        rtnval.append(info)

    return rtnval


def _parse_timedatectl_show_drift(*, text: str) -> UbuntuTimeDrift:
    """
        Parse ``timedatectl show --property=NTPMessage`` to extract a
        drift snapshot.

        ``timedatectl`` reports an ``NTPMessage`` blob like::

            NTPMessage={ Leap=0, Version=4, ..., OriginateTimestamp=...,
            ReceiveTimestamp=..., ..., Offset=12345us, Delay=2ms,
            Jitter=1ms, ... }

        The parser pulls ``Offset`` and ``Jitter`` out of the blob.

        :param text: Raw stdout.

        :returns: The :class:`UbuntuTimeDrift`.
    """
    fields = _parse_keyed_lines(text=text, separator="=")
    blob = fields.get("NTPMessage", "")

    offset_seconds = 0.0
    jitter_seconds: Optional[float] = None

    sub_fields = _parse_ntpmessage_blob(blob=blob)
    offset_token = sub_fields.get("Offset", "")
    parsed_offset = _parse_chrony_sample_offset(value=offset_token)
    if parsed_offset is not None:
        offset_seconds = parsed_offset

    jitter_token = sub_fields.get("Jitter", "")
    parsed_jitter = _parse_chrony_sample_offset(value=jitter_token)
    if parsed_jitter is not None:
        jitter_seconds = parsed_jitter

    rtnval = UbuntuTimeDrift(
        offset_seconds=offset_seconds,
        frequency_ppm=None,
        jitter_seconds=jitter_seconds,
        stratum=None,
    )
    return rtnval


def _parse_ntpmessage_blob(*, blob: str) -> dict:
    """
        Parse a timedatectl ``NTPMessage={ ... }`` blob into a dict.

        :param blob: Blob value (with surrounding braces preserved).

        :returns: Mapping of inner keys to string values.
    """
    rtnval: dict = {}
    stripped = blob.strip()
    if len(stripped) == 0:
        return rtnval
    if stripped.startswith("{"):
        stripped = stripped[1:]
    if stripped.endswith("}"):
        stripped = stripped[:-1]
    for raw in stripped.split(","):
        token = raw.strip()
        if "=" not in token:
            continue
        head, _, tail = token.partition("=")
        key = head.strip()
        value = tail.strip()
        if len(key) == 0:
            continue
        rtnval[key] = value
    return rtnval


def _parse_chrony_conf_servers(*, text: str) -> list[str]:
    """
        Parse the ``server``/``pool`` declarations out of a
        ``chrony.conf`` body.

        :param text: Raw file contents.

        :returns: Servers in declaration order.
    """
    rtnval: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        if line.startswith("#"):
            continue
        tokens = line.split()
        if len(tokens) < 2:
            continue
        head = tokens[0].lower()
        if head not in ("server", "pool"):
            continue
        rtnval.append(tokens[1])
    return rtnval


def _render_chrony_conf_servers(
    *, existing_text: str, servers: list[str],
) -> str:
    """
        Rewrite the ``server``/``pool`` lines of a ``chrony.conf`` body.

        Strips every existing ``server``/``pool`` declaration and
        appends a fresh ``pool <server> iburst`` line per requested
        server.  All other lines (comments, ``driftfile``,
        ``makestep``, ``allow``, ...) are preserved.

        :param existing_text: Raw existing file contents.
        :param servers: Desired server list.

        :returns: New file body.
    """
    kept: list[str] = []
    for raw in existing_text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if len(stripped) > 0 and not stripped.startswith("#"):
            tokens = stripped.split()
            head = tokens[0].lower()
            if head in ("server", "pool"):
                continue
        kept.append(line)

    if len(kept) > 0 and len(kept[-1].strip()) != 0:
        kept.append("")

    for server in servers:
        kept.append("pool {} iburst".format(server))

    body = "\n".join(kept) + "\n"
    return body


def _parse_timesyncd_conf_servers(*, text: str) -> list[str]:
    """
        Parse the active ``NTP=`` value out of a
        ``/etc/systemd/timesyncd.conf`` body.

        :param text: Raw file contents.

        :returns: Declared servers in order.
    """
    rtnval: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        if line.startswith("#"):
            continue
        if "=" not in line:
            continue
        head, _, tail = line.partition("=")
        key = head.strip().lower()
        if key != "ntp":
            continue
        value = tail.strip()
        for token in value.split():
            if token not in rtnval:
                rtnval.append(token)
    return rtnval


def _render_timesyncd_conf_servers(
    *, existing_text: str, servers: list[str],
) -> str:
    """
        Rewrite the ``NTP=`` line of a ``timesyncd.conf`` body.

        Strips every existing uncommented ``NTP=`` line and appends
        a single replacement under the ``[Time]`` section if present;
        otherwise prepends a fresh ``[Time]`` block.

        :param existing_text: Raw existing file contents.
        :param servers: Desired server list.

        :returns: New file body.
    """
    kept: list[str] = []
    saw_time_section = False
    for raw in existing_text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped == "[Time]":
            saw_time_section = True
            kept.append(line)
            continue
        if len(stripped) > 0 and not stripped.startswith("#"):
            if "=" in stripped:
                head, _, _ = stripped.partition("=")
                key = head.strip().lower()
                if key == "ntp":
                    continue
        kept.append(line)

    rendered_value = " ".join(servers)
    new_line = "NTP={}".format(rendered_value)

    if saw_time_section is True:
        # Insert the new NTP= line directly under [Time].
        emitted: list[str] = []
        inserted = False
        for line in kept:
            emitted.append(line)
            if line.strip() == "[Time]" and inserted is False:
                emitted.append(new_line)
                inserted = True
        if inserted is False:
            emitted.append(new_line)
        body = "\n".join(emitted) + "\n"
        return body

    if len(kept) > 0 and len(kept[-1].strip()) != 0:
        kept.append("")
    kept.append("[Time]")
    kept.append(new_line)

    body = "\n".join(kept) + "\n"
    return body
