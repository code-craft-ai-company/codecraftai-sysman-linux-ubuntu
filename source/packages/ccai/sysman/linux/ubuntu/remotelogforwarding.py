"""
    Ubuntu remote-log-forwarding feature.

    Implements the cross-OS remote-log-forwarding contract for
    Ubuntu using ``rsyslog`` forward drop-ins under
    ``/etc/rsyslog.d/``.  Every verb is dispatched through an
    :class:`ICommandRunner` so the manager works identically under
    local and SSH execution.

    File I/O policy:
        Every file read / write / probe / delete is performed
        through the injected :class:`ICommandRunner` (``cat``,
        ``tee``, ``rm``, ``mv``, ``test -e``, ``ls -1``).  This
        module does NOT consume an :class:`IFileOperator`.  The
        contract names ``IFileOperator`` as a forward-looking
        collaborator; the current implementation deviates from it
        pending the introduction of a concrete file-operator
        abstraction in this codebase.

    Drop-in naming:
        Every drop-in this manager writes is named
        ``<config.prefix><id>.conf``.  The default prefix is
        ``codecraftai-fwd-`` so that managed drop-ins do not collide
        with operator-authored drop-ins.  Disabled drop-ins are
        renamed to ``<config.prefix><id>.conf.disabled`` so that
        rsyslog ignores them on its next restart while the manager
        can still find them for re-enable / cleanup.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads,
        idempotency probes, and ``test_forwarder`` never elevate.

    Service reload:
        After every successful mutation the manager dispatches
        ``systemctl restart <config.rsyslog_service>`` so that the
        new drop-in is loaded immediately.  Restart failures
        surface as ``FAILED`` on the mutating verb's result.

    Documented limitations:
        Only the ``omfwd`` (UDP / TCP / TLS) transport family is
        rendered.  Disk-spool / queue tuning, structured output,
        RELP, ``omkafka``, ``omhttp``, and mutual-TLS rendering are
        out of scope for this pilot and tracked as follow-ups in
        the corresponding ``tasks.md``.
"""

import os
import time

from dataclasses import dataclass
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_TEE = "tee"
_CAT = "cat"
_RM = "rm"
_MV = "mv"
_LS = "ls"
_TEST = "test"
_LOGGER = "logger"
_SYSTEMCTL = "systemctl"

_DEFAULT_RSYSLOG_D = "/etc/rsyslog.d"
_DEFAULT_RSYSLOG_SERVICE = "rsyslog"
_DEFAULT_PREFIX = "codecraftai-fwd-"

_PROTOCOL_UDP = "udp"
_PROTOCOL_TCP = "tcp"
_PROTOCOL_TLS = "tls"

_VALID_PROTOCOLS = frozenset({_PROTOCOL_UDP, _PROTOCOL_TCP, _PROTOCOL_TLS})

_DISABLED_SUFFIX = ".disabled"
_CONF_SUFFIX = ".conf"


UbuntuForwarderActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class UbuntuForwarderManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuRemoteLogForwardingManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param rsyslog_d: Directory holding rsyslog drop-ins.
                          Defaults to ``/etc/rsyslog.d``.
        :param rsyslog_service: ``systemctl`` unit name used for
                                the post-write restart.  Defaults
                                to ``rsyslog``.
        :param prefix: Filename prefix used for every managed
                       drop-in.  Defaults to ``codecraftai-fwd-``.
                       The listing parser uses this string as a
                       sentinel to distinguish managed drop-ins
                       from third-party drop-ins.
    """
    rsyslog_d: str = _DEFAULT_RSYSLOG_D
    rsyslog_service: str = _DEFAULT_RSYSLOG_SERVICE
    prefix: str = _DEFAULT_PREFIX


@dataclass
class UbuntuForwarderActionResult(FeatureActionResult):
    """
        Result of an :class:`UbuntuRemoteLogForwardingManager`
        write verb.

        :param forwarder_id: The forwarder identifier targeted.
        :param path: The drop-in path the action targeted, when
                     applicable.
        :param message: Optional human-readable detail for
                        ``SKIPPED`` / ``FAILED`` outcomes.
    """
    forwarder_id: Optional[str] = None
    path: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuForwarderSpec:
    """
        Declarative specification for a single rsyslog forwarder.

        :param id: File-name-friendly identifier.  Combined with
                   the manager's ``prefix`` to form the drop-in
                   basename.
        :param host: Collector hostname or IP.
        :param port: Collector port.  Defaults to 514.
        :param protocol: ``"tcp"``, ``"udp"``, or ``"tls"``.
                         Defaults to ``"tcp"``.
        :param selector: rsyslog facility.severity selector.
                         Defaults to ``"*.*"``.
        :param template: Optional rsyslog template name appended
                         to the action line as
                         ``;<template>``.  ``None`` omits the
                         suffix.
        :param tls_ca_cert: Path to the CA bundle.  Required when
                            ``protocol == "tls"``.
        :param enabled: When ``False``, the drop-in is written
                        with the ``.conf.disabled`` suffix so that
                        rsyslog ignores it on the next restart.
                        Defaults to ``True``.
    """
    id: str
    host: str
    port: int = 514
    protocol: str = _PROTOCOL_TCP
    selector: str = "*.*"
    template: Optional[str] = None
    tls_ca_cert: Optional[str] = None
    enabled: bool = True


@dataclass
class UbuntuForwarderInfo:
    """
        One row from
        :meth:`UbuntuRemoteLogForwardingManager.list_forwarders`.

        :param id: Forwarder identifier (drop-in basename minus
                   the ``prefix`` and the file suffix).
        :param host: Collector hostname or IP parsed from the
                     drop-in body.
        :param port: Collector port parsed from the drop-in body.
        :param protocol: ``"tcp"``, ``"udp"``, or ``"tls"`` as
                         parsed from the action line.
        :param enabled: True when the on-disk file is
                        ``<prefix><id>.conf``; False when it is
                        ``<prefix><id>.conf.disabled``.
        :param path: Absolute path of the on-disk drop-in.
        :param selector_or_query: rsyslog selector parsed from
                                  the action line.  ``None`` when
                                  the parser could not extract
                                  it.
    """
    id: str
    host: str
    port: int
    protocol: str
    enabled: bool
    path: Optional[str]
    selector_or_query: Optional[str]


@dataclass
class UbuntuForwarderStatus:
    """
        Result of
        :meth:`UbuntuRemoteLogForwardingManager.get_forwarder_status`.

        :param id: The forwarder identifier queried.
        :param enabled: True when the active ``.conf`` drop-in is
                        present; False otherwise (including the
                        absent case).
        :param last_event_time: Reserved for future ``impstats``
                                integration.  ``None`` for the
                                pilot.
        :param error_count: Reserved for future ``impstats``
                            integration.  ``None`` for the pilot.
        :param bytes_sent: Reserved for future ``impstats``
                           integration.  ``None`` for the pilot.
    """
    id: str
    enabled: bool
    last_event_time: Optional[str] = None
    error_count: Optional[int] = None
    bytes_sent: Optional[int] = None


@dataclass
class UbuntuForwarderTestResult(FeatureActionResult):
    """
        Result of
        :meth:`UbuntuRemoteLogForwardingManager.test_forwarder`.

        :param forwarder_id: The forwarder identifier the probe
                             targeted.
        :param test_message_id: The unique identifier embedded in
                                the dispatched ``logger`` message
                                so that the collector can correlate
                                the test event.
    """
    forwarder_id: str = ""
    test_message_id: Optional[str] = None


class UbuntuForwarderQueryError(Exception):
    """
        Raised when a remote-log-forwarding read fails for a
        reason other than the target simply being absent.
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

        err_msg_lines = ["Forwarder query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# UbuntuRemoteLogForwardingManager
# ----------------------------------------------------------------------


class UbuntuRemoteLogForwardingManager(BaseFeatureManager):
    """
        Ubuntu rsyslog forward-drop-in manager.

        Every verb is dispatched through the injected
        :class:`ICommandRunner`.  File I/O for drop-ins is
        performed via ``cat`` / ``tee`` / ``rm`` / ``mv`` /
        ``test -e`` / ``ls -1`` rather than a separate file
        operator.
    """

    result_class = UbuntuForwarderActionResult
    status_class = UbuntuForwarderActionStatus
    feature_name = "remote-log-forwarding"

    def __init__(
        self,
        runner,
        config: Optional[UbuntuForwarderManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every
                           dispatch.
            :param config: Manager configuration.  Defaults to a
                           fresh
                           :class:`UbuntuForwarderManagerConfig`
                           when omitted.
        """
        if config is None:
            config = UbuntuForwarderManagerConfig()

        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_forwarder(
        self,
        spec: UbuntuForwarderSpec,
    ) -> UbuntuForwarderActionResult:
        """
            Write or update an rsyslog forward drop-in for
            ``spec``.

            Idempotent: returns ``SKIPPED`` when the on-disk
            content matches the rendered desired content
            byte-for-byte.

            :param spec: The :class:`UbuntuForwarderSpec` to
                         render.

            :returns: The :class:`UbuntuForwarderActionResult`.
        """
        validation_failure = _validate_spec(spec=spec)
        if validation_failure is not None:
            failed = self._build_result(
                status=UbuntuForwarderActionStatus.FAILED,
                forwarder_id=spec.id,
                path=None,
                command=[_TEE, self._dropin_path(name=spec.id)],
                message=validation_failure,
                stderr=validation_failure,
            )
            return failed

        path = self._dropin_path(name=spec.id)
        desired = _render_drop_in(spec=spec)
        write_command = [_TEE, path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuForwarderActionStatus.DRY_RUN,
                forwarder_id=spec.id,
                path=path,
                command=write_command,
            )
            return dry

        existing = self._read_file(path=path)
        if existing is not None and existing == desired:
            skipped = self._build_result(
                status=UbuntuForwarderActionStatus.SKIPPED,
                forwarder_id=spec.id,
                path=path,
                command=write_command,
                message="forwarder already matches desired content",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=self._typed_config().elevated,
            stdin=desired,
        )

        if run_result.exit_code != 0:
            failed = UbuntuForwarderActionResult(
                status=UbuntuForwarderActionStatus.FAILED,
                detail=spec.id,
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                forwarder_id=spec.id,
                path=path,
                message="tee failed; drop-in not written",
            )
            return failed

        reload_result = self._reload_rsyslog()
        if reload_result.exit_code != 0:
            failed = UbuntuForwarderActionResult(
                status=UbuntuForwarderActionStatus.FAILED,
                detail=spec.id,
                exit_code=reload_result.exit_code,
                stdout=reload_result.stdout,
                stderr=reload_result.stderr,
                command=list(write_command),
                forwarder_id=spec.id,
                path=path,
                message="rsyslog restart failed after drop-in write",
            )
            return failed

        rtnval = UbuntuForwarderActionResult(
            status=UbuntuForwarderActionStatus.SUCCESS,
            detail=spec.id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            forwarder_id=spec.id,
            path=path,
        )
        return rtnval

    def remove_forwarder(self, id: str) -> UbuntuForwarderActionResult:
        """
            Remove an rsyslog forward drop-in.

            Idempotent: returns ``SKIPPED`` when neither the
            ``.conf`` nor the ``.disabled`` variant is present.

            :param id: The forwarder identifier.

            :returns: The :class:`UbuntuForwarderActionResult`.
        """
        active_path = self._dropin_path(name=id)
        disabled_path = active_path + _DISABLED_SUFFIX

        active_present = self._path_exists(path=active_path)
        disabled_present = self._path_exists(path=disabled_path)

        if not active_present and not disabled_present:
            command = [_RM, active_path]
            skipped = self._build_result(
                status=UbuntuForwarderActionStatus.SKIPPED,
                forwarder_id=id,
                path=active_path,
                command=command,
                message="forwarder drop-in already absent",
            )
            return skipped

        if disabled_present:
            target_path = disabled_path
        else:
            target_path = active_path

        command = [_RM, target_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuForwarderActionStatus.DRY_RUN,
                forwarder_id=id,
                path=target_path,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._typed_config().elevated,
        )

        if run_result.exit_code != 0:
            failed = UbuntuForwarderActionResult(
                status=UbuntuForwarderActionStatus.FAILED,
                detail=id,
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(command),
                forwarder_id=id,
                path=target_path,
                message="rm failed; drop-in still on disk",
            )
            return failed

        reload_result = self._reload_rsyslog()
        if reload_result.exit_code != 0:
            failed = UbuntuForwarderActionResult(
                status=UbuntuForwarderActionStatus.FAILED,
                detail=id,
                exit_code=reload_result.exit_code,
                stdout=reload_result.stdout,
                stderr=reload_result.stderr,
                command=list(command),
                forwarder_id=id,
                path=target_path,
                message="rsyslog restart failed after drop-in removal",
            )
            return failed

        rtnval = UbuntuForwarderActionResult(
            status=UbuntuForwarderActionStatus.SUCCESS,
            detail=id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            forwarder_id=id,
            path=target_path,
        )
        return rtnval

    def enable_forwarder(self, id: str) -> UbuntuForwarderActionResult:
        """
            Enable a previously disabled forwarder by renaming
            ``<prefix><id>.conf.disabled`` -> ``<prefix><id>.conf``.

            Idempotent: returns ``SKIPPED`` when the active
            ``.conf`` drop-in is already on disk.

            :param id: The forwarder identifier.

            :returns: The :class:`UbuntuForwarderActionResult`.
        """
        active_path = self._dropin_path(name=id)
        disabled_path = active_path + _DISABLED_SUFFIX

        active_present = self._path_exists(path=active_path)
        disabled_present = self._path_exists(path=disabled_path)

        command = [_MV, disabled_path, active_path]

        if active_present and not disabled_present:
            skipped = self._build_result(
                status=UbuntuForwarderActionStatus.SKIPPED,
                forwarder_id=id,
                path=active_path,
                command=command,
                message="forwarder already enabled",
            )
            return skipped

        if not disabled_present:
            failed = self._build_result(
                status=UbuntuForwarderActionStatus.FAILED,
                forwarder_id=id,
                path=disabled_path,
                command=command,
                message="disabled drop-in not found; cannot enable",
                stderr="disabled_drop_in_missing",
            )
            return failed

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuForwarderActionStatus.DRY_RUN,
                forwarder_id=id,
                path=active_path,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._typed_config().elevated,
        )

        if run_result.exit_code != 0:
            failed = UbuntuForwarderActionResult(
                status=UbuntuForwarderActionStatus.FAILED,
                detail=id,
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(command),
                forwarder_id=id,
                path=active_path,
                message="mv failed; forwarder remains disabled",
            )
            return failed

        reload_result = self._reload_rsyslog()
        if reload_result.exit_code != 0:
            failed = UbuntuForwarderActionResult(
                status=UbuntuForwarderActionStatus.FAILED,
                detail=id,
                exit_code=reload_result.exit_code,
                stdout=reload_result.stdout,
                stderr=reload_result.stderr,
                command=list(command),
                forwarder_id=id,
                path=active_path,
                message="rsyslog restart failed after enable",
            )
            return failed

        rtnval = UbuntuForwarderActionResult(
            status=UbuntuForwarderActionStatus.SUCCESS,
            detail=id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            forwarder_id=id,
            path=active_path,
        )
        return rtnval

    def disable_forwarder(self, id: str) -> UbuntuForwarderActionResult:
        """
            Disable an active forwarder by renaming
            ``<prefix><id>.conf`` -> ``<prefix><id>.conf.disabled``.

            Idempotent: returns ``SKIPPED`` when only the
            ``.disabled`` variant is on disk.

            :param id: The forwarder identifier.

            :returns: The :class:`UbuntuForwarderActionResult`.
        """
        active_path = self._dropin_path(name=id)
        disabled_path = active_path + _DISABLED_SUFFIX

        active_present = self._path_exists(path=active_path)
        disabled_present = self._path_exists(path=disabled_path)

        command = [_MV, active_path, disabled_path]

        if not active_present and disabled_present:
            skipped = self._build_result(
                status=UbuntuForwarderActionStatus.SKIPPED,
                forwarder_id=id,
                path=disabled_path,
                command=command,
                message="forwarder already disabled",
            )
            return skipped

        if not active_present:
            failed = self._build_result(
                status=UbuntuForwarderActionStatus.FAILED,
                forwarder_id=id,
                path=active_path,
                command=command,
                message="active drop-in not found; cannot disable",
                stderr="active_drop_in_missing",
            )
            return failed

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuForwarderActionStatus.DRY_RUN,
                forwarder_id=id,
                path=disabled_path,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._typed_config().elevated,
        )

        if run_result.exit_code != 0:
            failed = UbuntuForwarderActionResult(
                status=UbuntuForwarderActionStatus.FAILED,
                detail=id,
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(command),
                forwarder_id=id,
                path=disabled_path,
                message="mv failed; forwarder remains active",
            )
            return failed

        reload_result = self._reload_rsyslog()
        if reload_result.exit_code != 0:
            failed = UbuntuForwarderActionResult(
                status=UbuntuForwarderActionStatus.FAILED,
                detail=id,
                exit_code=reload_result.exit_code,
                stdout=reload_result.stdout,
                stderr=reload_result.stderr,
                command=list(command),
                forwarder_id=id,
                path=disabled_path,
                message="rsyslog restart failed after disable",
            )
            return failed

        rtnval = UbuntuForwarderActionResult(
            status=UbuntuForwarderActionStatus.SUCCESS,
            detail=id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            forwarder_id=id,
            path=disabled_path,
        )
        return rtnval

    def test_forwarder(self, id: str) -> UbuntuForwarderTestResult:
        """
            Send a test message into the local syslog pipeline so
            that downstream forwarders pick it up.

            Dispatches ``logger -t codecraftai-test
            'forwarder-test-<timestamp>'`` without elevation.  No
            idempotency guard.  Never raises on dispatch failure.

            :param id: The forwarder identifier the test is
                       targeted at.  The verb does not steer the
                       message to a specific forwarder — every
                       configured forwarder receives the test
                       message via the local syslog facility.

            :returns: The :class:`UbuntuForwarderTestResult`.
        """
        timestamp = int(time.time())
        message_id = "forwarder-test-{}".format(timestamp)
        message = message_id

        command = [_LOGGER, "-t", "codecraftai-test", message]

        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code == 0:
            mapped_status = UbuntuForwarderActionStatus.SUCCESS
        else:
            mapped_status = UbuntuForwarderActionStatus.FAILED

        rtnval = UbuntuForwarderTestResult(
            status=mapped_status,
            detail=id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            forwarder_id=id,
            test_message_id=message_id,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_forwarders(self) -> list[UbuntuForwarderInfo]:
        """
            Enumerate every managed forwarder.

            Dispatches ``ls -1 <rsyslog_d>`` without elevation,
            filters the listing to entries starting with the
            configured ``prefix``, and parses each managed drop-in
            via ``cat``.

            :returns: One :class:`UbuntuForwarderInfo` per
                      managed drop-in.

            :raises UbuntuForwarderQueryError: When ``ls`` exits
                                                non-zero with
                                                non-empty stderr.
        """
        cfg = self._typed_config()
        ls_command = [_LS, "-1", cfg.rsyslog_d]
        ls_result = self._runner.run(list(ls_command), elevated=False)

        if ls_result.exit_code != 0 and len(ls_result.stderr.strip()) > 0:
            raise UbuntuForwarderQueryError(
                command=ls_command,
                exit_code=ls_result.exit_code,
                stderr=ls_result.stderr,
            )

        rtnval: list[UbuntuForwarderInfo] = []
        for raw_entry in ls_result.stdout.splitlines():
            entry = raw_entry.strip()
            if len(entry) == 0:
                continue
            if not entry.startswith(cfg.prefix):
                continue

            full_path = "{}/{}".format(cfg.rsyslog_d, entry)
            id_value, enabled = _classify_dropin(
                basename=entry, prefix=cfg.prefix,
            )
            if id_value is None:
                continue

            body = self._read_file(path=full_path)
            if body is None:
                continue

            host, port, protocol, selector = _parse_drop_in(body=body)
            info = UbuntuForwarderInfo(
                id=id_value,
                host=host,
                port=port,
                protocol=protocol,
                enabled=enabled,
                path=full_path,
                selector_or_query=selector,
            )
            rtnval.append(info)

        return rtnval

    def get_forwarder_status(self, id: str) -> UbuntuForwarderStatus:
        """
            Probe a forwarder's enabled / disabled state.

            Dispatches ``test -e`` against both the active
            ``.conf`` path and the ``.disabled`` shadow.  Reads
            never elevate.

            :param id: The forwarder identifier.

            :returns: The :class:`UbuntuForwarderStatus`.
                      ``enabled`` is ``False`` when neither file
                      is present.
        """
        active_path = self._dropin_path(name=id)
        disabled_path = active_path + _DISABLED_SUFFIX

        active_present = self._path_exists(path=active_path)
        disabled_present = self._path_exists(path=disabled_path)

        if active_present:
            enabled_value = True
        elif disabled_present:
            enabled_value = False
        else:
            enabled_value = False

        rtnval = UbuntuForwarderStatus(
            id=id,
            enabled=enabled_value,
            last_event_time=None,
            error_count=None,
            bytes_sent=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> UbuntuForwarderManagerConfig:
        """
            Return the manager's config typed as
            :class:`UbuntuForwarderManagerConfig`.

            :returns: The active config typed.
        """
        if isinstance(self._config, UbuntuForwarderManagerConfig):
            rtnval = self._config
        else:
            rtnval = UbuntuForwarderManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _dropin_path(self, *, name: str) -> str:
        """
            Build the absolute path to the active drop-in for
            ``name``.

            :param name: The forwarder identifier.

            :returns: ``<rsyslog_d>/<prefix><name>.conf``.
        """
        cfg = self._typed_config()
        rtnval = "{}/{}{}{}".format(
            cfg.rsyslog_d, cfg.prefix, name, _CONF_SUFFIX,
        )
        return rtnval

    def _path_exists(self, *, path: str) -> bool:
        """
            Probe a path's existence via ``test -e``.

            :param path: The absolute path to probe.

            :returns: True when ``test -e`` exits zero; False
                      otherwise.
        """
        command = [_TEST, "-e", path]
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat``.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when
                      the file is missing or unreadable.
        """
        command = [_CAT, path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _reload_rsyslog(self):
        """
            Dispatch ``systemctl restart <rsyslog_service>``.

            :returns: The runner's :class:`RunResult`.
        """
        cfg = self._typed_config()
        command = [_SYSTEMCTL, "restart", cfg.rsyslog_service]
        rtnval = self._runner.run(
            list(command),
            elevated=cfg.elevated,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuForwarderActionStatus,
        forwarder_id: Optional[str],
        path: Optional[str],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuForwarderActionResult:
        """
            Construct an :class:`UbuntuForwarderActionResult`
            without invoking the runner.

            :param status: The outcome status.
            :param forwarder_id: The forwarder identifier to echo
                                 onto the result.
            :param path: The drop-in path to echo onto the result.
            :param command: The would-have-been argv (copied for
                            capture).
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`UbuntuForwarderActionResult`.
        """
        rtnval = UbuntuForwarderActionResult(
            status=status,
            detail=forwarder_id,
            command=list(command),
            forwarder_id=forwarder_id,
            path=path,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _validate_spec(*, spec: UbuntuForwarderSpec) -> Optional[str]:
    """
        Validate a spec before the renderer runs.

        :param spec: The :class:`UbuntuForwarderSpec` to validate.

        :returns: ``None`` when the spec is valid; an error-message
                  string when it is not.
    """
    if spec.id is None or len(spec.id) == 0:
        rtnval: Optional[str] = "spec.id is required"
        return rtnval
    if spec.host is None or len(spec.host) == 0:
        rtnval = "spec.host is required"
        return rtnval
    if spec.port is None:
        rtnval = "spec.port is required"
        return rtnval
    if spec.port <= 0:
        rtnval = "spec.port must be positive"
        return rtnval
    if spec.protocol not in _VALID_PROTOCOLS:
        rtnval = "spec.protocol must be one of {}".format(
            sorted(_VALID_PROTOCOLS),
        )
        return rtnval
    if spec.protocol == _PROTOCOL_TLS:
        if spec.tls_ca_cert is None or len(spec.tls_ca_cert) == 0:
            rtnval = "spec.tls_ca_cert is required when protocol is 'tls'"
            return rtnval

    rtnval = None
    return rtnval


def _render_drop_in(*, spec: UbuntuForwarderSpec) -> str:
    """
        Render a :class:`UbuntuForwarderSpec` into an rsyslog
        drop-in body.

        Output is byte-stable for matching inputs so that the
        idempotency comparison in
        :meth:`UbuntuRemoteLogForwardingManager.add_forwarder`
        works.

        :param spec: The :class:`UbuntuForwarderSpec` to render.

        :returns: The full drop-in body as a string ending in
                  ``\\n``.
    """
    if spec.protocol == _PROTOCOL_UDP:
        target_marker = "@"
    else:
        # Both TCP and TLS use the double-`@` action; TLS adds
        # the gtls driver block above the action line.
        target_marker = "@@"

    action_target = "{}{}:{}".format(target_marker, spec.host, spec.port)
    if spec.template is not None and len(spec.template) > 0:
        action_line = "{} {};{}".format(
            spec.selector, action_target, spec.template,
        )
    else:
        action_line = "{} {}".format(spec.selector, action_target)

    body_lines: list[str] = []
    body_lines.append("# codecraftai-fwd-{}".format(spec.id))
    body_lines.append(
        "# Generated by ccai UbuntuRemoteLogForwardingManager",
    )

    if spec.protocol == _PROTOCOL_TLS:
        body_lines.append("$DefaultNetstreamDriver gtls")
        body_lines.append(
            "$DefaultNetstreamDriverCAFile {}".format(spec.tls_ca_cert),
        )
        body_lines.append("$ActionSendStreamDriver gtls")
        body_lines.append("$ActionSendStreamDriverMode 1")
        body_lines.append("$ActionSendStreamDriverAuthMode x509/name")

    body_lines.append(action_line)

    rtnval = "\n".join(body_lines) + "\n"
    return rtnval


def _classify_dropin(
    *,
    basename: str,
    prefix: str,
) -> tuple:
    """
        Classify a drop-in basename as enabled or disabled, and
        extract the forwarder identifier.

        :param basename: The directory-entry basename.
        :param prefix: The configured drop-in prefix.

        :returns: A two-tuple ``(id, enabled)`` where ``id`` is
                  ``None`` when the basename is not a managed
                  drop-in.
    """
    if not basename.startswith(prefix):
        rtnval = (None, False)
        return rtnval

    after_prefix = basename[len(prefix):]
    if after_prefix.endswith(_DISABLED_SUFFIX):
        without_disabled = after_prefix[: -len(_DISABLED_SUFFIX)]
        if without_disabled.endswith(_CONF_SUFFIX):
            id_value = without_disabled[: -len(_CONF_SUFFIX)]
            rtnval = (id_value, False)
            return rtnval
        rtnval = (None, False)
        return rtnval

    if after_prefix.endswith(_CONF_SUFFIX):
        id_value = after_prefix[: -len(_CONF_SUFFIX)]
        rtnval = (id_value, True)
        return rtnval

    rtnval = (None, False)
    return rtnval


def _parse_drop_in(*, body: str) -> tuple:
    """
        Parse the rendered rsyslog drop-in body back into
        ``(host, port, protocol, selector)``.

        Permissive parser: walks the body line-by-line until it
        finds the action line, then extracts the four tokens.
        ``# DISABLED`` comment blocks are tolerated because the
        manager keeps disabled drop-ins under a different
        suffix; the body itself is unchanged.

        :param body: The drop-in body.

        :returns: A four-tuple
                  ``(host, port, protocol, selector)``.  Missing
                  values surface as empty strings or ``0``.
    """
    host = ""
    port = 0
    protocol = ""
    selector = ""

    has_gtls_global = False
    has_gtls_action = False

    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("$DefaultNetstreamDriver"):
            tokens = stripped.split()
            if len(tokens) >= 2 and tokens[1] == "gtls":
                has_gtls_global = True
            continue
        if stripped.startswith("$ActionSendStreamDriver"):
            tokens = stripped.split()
            if len(tokens) >= 2 and tokens[1] == "gtls":
                has_gtls_action = True
            continue
        if stripped.startswith("$"):
            # Ignore other rsyslog $-directives (e.g. CAFile,
            # Mode, AuthMode); they are part of the gtls block.
            continue

        # Action line: <selector> <target>;[template]
        tokens = stripped.split(None, 1)
        if len(tokens) < 2:
            continue
        selector_token = tokens[0]
        target_remainder = tokens[1]

        # Drop the optional ;<template> suffix.
        target_clean = target_remainder.split(";", 1)[0].strip()

        if target_clean.startswith("@@"):
            transport_marker = "@@"
            host_port = target_clean[2:]
        elif target_clean.startswith("@"):
            transport_marker = "@"
            host_port = target_clean[1:]
        else:
            continue

        host_value, port_value = _split_host_port(host_port=host_port)
        if host_value is None:
            continue

        host = host_value
        port = port_value
        selector = selector_token

        if transport_marker == "@":
            protocol = _PROTOCOL_UDP
        else:
            protocol = _PROTOCOL_TCP

        break

    if has_gtls_global and has_gtls_action and protocol == _PROTOCOL_TCP:
        protocol = _PROTOCOL_TLS

    rtnval = (host, port, protocol, selector)
    return rtnval


def _split_host_port(*, host_port: str) -> tuple:
    """
        Split a ``host:port`` string into its components.

        :param host_port: The combined token.

        :returns: A two-tuple ``(host, port)``.  ``host`` is
                  ``None`` when the token cannot be parsed.
    """
    if host_port is None or len(host_port) == 0:
        rtnval = (None, 0)
        return rtnval

    if ":" not in host_port:
        rtnval = (host_port, 0)
        return rtnval

    last_colon = host_port.rfind(":")
    host_value = host_port[:last_colon]
    port_text = host_port[last_colon + 1:]
    try:
        port_value = int(port_text)
    except ValueError:
        rtnval = (None, 0)
        return rtnval

    rtnval = (host_value, port_value)
    return rtnval
