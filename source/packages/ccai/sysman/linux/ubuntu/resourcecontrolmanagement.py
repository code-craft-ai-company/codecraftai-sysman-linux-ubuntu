"""
    Ubuntu resource-control-management feature.

    Implements the cross-OS resource-control-management contract
    for Ubuntu using systemd slice units (cgroups v2).  Slice
    units are written to ``/etc/systemd/system/<name>.slice``;
    the kernel maintains the cgroup tree at
    ``/sys/fs/cgroup/<name>.slice/``; per-slice ceilings are
    applied via ``systemctl set-property``; per-PID attachment is
    performed by writing the decimal PID to
    ``cgroup.procs`` in the slice's cgroup directory.

    File I/O policy:
        Every read / write / probe is dispatched through the
        injected :class:`ICommandRunner` (``cat``, ``tee``,
        ``ls -1``, ``rm``, ``test -e``).  This module does NOT
        consume an :class:`IFileOperator`.

    Tooling:
        * ``systemctl`` — daemon-reload, start, stop, set-property,
          show, is-active.
        * ``tee`` — write slice unit body, append to
          ``cgroup.procs``.
        * ``cat`` — read slice unit body, ``cpu.stat``,
          ``memory.current``, ``io.stat``, ``cgroup.procs``.
        * ``ls -1`` — enumerate slice cgroup directories.
        * ``rm`` — remove slice unit file.
        * ``test -e`` — slice unit existence probe.

    Idempotency:
        Every write verb queries the live state (``systemctl
        show``, ``cat``, ``test -e``) before invoking its
        mutating step and returns ``SKIPPED`` when the requested
        state already holds.

    Elevation:
        Writes honour ``config.elevated``.  Internal probes
        (``systemctl show``, ``cat``, ``ls``, ``test -e``) and
        the dedicated read verbs (``list_groups``,
        ``get_group_usage``) NEVER elevate.
"""

import os
import re

from dataclasses import dataclass, field
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

_SYSTEMCTL = "systemctl"
_TEE = "tee"
_CAT = "cat"
_LS = "ls"
_RM = "rm"
_TEST = "test"

_DEFAULT_SLICE_DIR = "/etc/systemd/system"
_DEFAULT_CGROUP_ROOT = "/sys/fs/cgroup"

# Default destination for detached processes — the systemd default
# slice for system services.  This is the documented kernel-side
# detach destination because cgroup.procs writes atomically remove
# the PID from its previous cgroup as a side effect of the move.
_SYSTEM_SLICE = "system.slice"

_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")

_SHOW_PROPERTIES = (
    "ActiveState,CPUQuotaPerSecUSec,CPUWeight,MemoryMax,"
    "IOWeight,IOReadBandwidthMax,IOWriteBandwidthMax"
)


UbuntuResourceControlActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class UbuntuResourceControlManagerConfig(FeatureConfig):
    """
        Configuration for
        :class:`UbuntuResourceControlManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param slice_dir: Directory holding system-scope slice
                          unit files.  Defaults to
                          ``/etc/systemd/system``.
        :param cgroup_root: Root of the unified cgroups v2
                            hierarchy.  Defaults to
                            ``/sys/fs/cgroup``.
    """
    slice_dir: str = _DEFAULT_SLICE_DIR
    cgroup_root: str = _DEFAULT_CGROUP_ROOT


@dataclass
class UbuntuResourceControlActionResult(FeatureActionResult):
    """
        Result of a resource-control write operation.

        :param group_name: Slice / group name acted upon.
        :param pid: Process ID acted upon (only populated by
                    :meth:`UbuntuResourceControlManager.attach_process`
                    and :meth:`detach_process`).
        :param message: Optional short reason token for
                        ``SKIPPED`` / ``FAILED`` outcomes.
    """
    group_name: Optional[str] = None
    pid: Optional[int] = None
    message: Optional[str] = None


@dataclass
class UbuntuIoLimitSpec:
    """
        IO-limit specification carried by
        :meth:`UbuntuResourceControlManager.set_io_limits`.

        :param weight: ``IOWeight`` value (10-10000), or ``None``
                       to leave the weight unchanged.
        :param read_bps: ``IOReadBandwidthMax`` cap in bytes per
                         second, or ``None`` to leave unchanged.
                         Requires ``device`` to be set.
        :param write_bps: ``IOWriteBandwidthMax`` cap in bytes
                          per second, or ``None`` to leave
                          unchanged.  Requires ``device`` to be
                          set.
        :param device: Block device path (e.g. ``"/dev/sda"``)
                       to which ``read_bps`` / ``write_bps``
                       apply.  Required when either bps cap is
                       set; ignored when both bps caps are
                       ``None``.
    """
    weight: Optional[int] = None
    read_bps: Optional[int] = None
    write_bps: Optional[int] = None
    device: Optional[str] = None


def _empty_pid_list() -> list[int]:
    """
        Default-factory for :class:`UbuntuResourceGroupSpec.attached_pids`.

        :returns: A fresh empty list per call.
    """
    rtnval: list[int] = []
    return rtnval


@dataclass
class UbuntuResourceGroupSpec:
    """
        Specification used by
        :meth:`UbuntuResourceControlManager.create_group`.

        :param name: Slice / group identifier.  Must match
                     ``^[A-Za-z0-9][A-Za-z0-9._\\-]*$``.
        :param cpu_percent: Initial ``CPUQuota`` percent
                            (0-100*<n_cpus>).  ``None`` skips
                            ``CPUQuota`` from the rendered slice
                            body.
        :param memory_bytes: Initial ``MemoryMax`` in bytes.
                             ``None`` skips the directive.
        :param io_weight: Initial ``IOWeight`` (10-10000).
                          ``None`` skips.
        :param io_read_bps: Initial ``IOReadBandwidthMax`` in
                            bytes per second.  Skipped when
                            ``device`` is not set.
        :param io_write_bps: Initial ``IOWriteBandwidthMax`` in
                             bytes per second.  Skipped when
                             ``device`` is not set.
        :param attached_pids: Reserved field for future
                              attach-on-create extension; the
                              v1 manager does NOT auto-attach
                              and only records the field for
                              informational reflection.
    """
    name: str
    cpu_percent: Optional[int] = None
    memory_bytes: Optional[int] = None
    io_weight: Optional[int] = None
    io_read_bps: Optional[int] = None
    io_write_bps: Optional[int] = None
    attached_pids: list[int] = field(default_factory=_empty_pid_list)


@dataclass
class UbuntuResourceGroupInfo:
    """
        Current state of a single resource group.

        :param name: Slice / group name.
        :param cpu_percent: ``CPUQuota`` parsed from
                            ``CPUQuotaPerSecUSec``, or ``None``
                            when unlimited / unset.
        :param memory_bytes: ``MemoryMax`` integer, or ``None``
                             when unlimited / unset.
        :param io_weight: ``IOWeight`` integer, or ``None``.
        :param io_read_bps: Aggregate ``IOReadBandwidthMax``
                            value, or ``None`` when unset.
        :param io_write_bps: Aggregate ``IOWriteBandwidthMax``
                             value, or ``None``.
        :param attached_pids: PIDs currently in the slice's
                              ``cgroup.procs``.
        :param is_active: ``True`` when ``ActiveState`` is
                          ``"active"``.
        :param effective_cpu_percent: Same as ``cpu_percent``;
                                       held separately for
                                       cross-OS parity (macOS
                                       distinguishes declared vs
                                       effective).
        :param effective_memory_bytes: Same as ``memory_bytes``;
                                        cross-OS parity field.
    """
    name: str
    cpu_percent: Optional[int]
    memory_bytes: Optional[int]
    io_weight: Optional[int]
    io_read_bps: Optional[int]
    io_write_bps: Optional[int]
    attached_pids: list[int]
    is_active: bool
    effective_cpu_percent: Optional[int]
    effective_memory_bytes: Optional[int]


@dataclass
class UbuntuResourceGroupUsage:
    """
        Live usage snapshot for a single resource group.

        :param name: Slice / group name.
        :param cpu_usage_seconds: Cumulative CPU time consumed,
                                  parsed from ``cpu.stat``'s
                                  ``usage_usec`` line and
                                  converted to seconds.  ``None``
                                  when ``cpu.stat`` did not
                                  report the line.
        :param memory_used_bytes: Current resident memory in
                                  bytes from ``memory.current``.
                                  ``None`` when not reported.
        :param io_read_bytes: Sum of every ``rbytes=`` field
                              across every device line in
                              ``io.stat``.  ``None`` when
                              ``io.stat`` is empty.
        :param io_write_bytes: Sum of every ``wbytes=`` field
                               across every device line in
                               ``io.stat``.  ``None`` when
                               ``io.stat`` is empty.
        :param process_count: Number of PIDs in the slice's
                              ``cgroup.procs``.
    """
    name: str
    cpu_usage_seconds: Optional[float]
    memory_used_bytes: Optional[int]
    io_read_bytes: Optional[int]
    io_write_bytes: Optional[int]
    process_count: int


class UbuntuResourceControlQueryError(Exception):
    """
        Raised when a resource-control read fails for a reason
        other than the target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
        detail: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
            :param detail: Optional short reason token (e.g.
                           ``"unknown_group"``).
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr
        self.detail = detail

        err_msg_lines = ["Resource-control query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        if detail is not None:
            err_msg_lines.append("    DETAIL: {}".format(detail))

        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class UbuntuResourceControlManager(BaseFeatureManager):
    """
        Ubuntu cgroups-v2 resource-control manager.  Every verb
        is dispatched through the injected
        :class:`ICommandRunner`.
    """

    result_class = UbuntuResourceControlActionResult
    status_class = UbuntuResourceControlActionStatus
    feature_name = "resource-control-management"

    def __init__(
        self,
        runner,
        config: Optional[UbuntuResourceControlManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner.
            :param config: Manager configuration.  Defaults to a
                           fresh
                           :class:`UbuntuResourceControlManagerConfig`
                           when omitted so the verb
                           implementations can rely on
                           ``self._config.slice_dir`` and
                           ``self._config.cgroup_root`` always
                           being present.
        """
        if config is None:
            config = UbuntuResourceControlManagerConfig()
        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def create_group(
        self,
        spec: UbuntuResourceGroupSpec,
    ) -> UbuntuResourceControlActionResult:
        """
            Create or update a resource group.

            Renders the slice unit body, writes
            ``/etc/systemd/system/<name>.slice`` via ``tee``,
            invokes ``systemctl daemon-reload``, then
            ``systemctl start <name>.slice``.

            Idempotent: when the on-disk slice file matches the
            rendered body byte-for-byte AND the slice's
            ``ActiveState`` is already ``"active"``, returns
            ``SKIPPED``.

            :param spec: The :class:`UbuntuResourceGroupSpec`.

            :returns: The
                      :class:`UbuntuResourceControlActionResult`.
        """
        try:
            _validate_name(name=spec.name)
        except ValueError as exc:
            failed = self._build_result(
                status=UbuntuResourceControlActionStatus.FAILED,
                group_name=spec.name,
                command=[],
                message="invalid_group_name",
                stderr=str(exc),
            )
            return failed

        rendered_body = _render_slice_body(spec=spec)
        slice_path = self._slice_path(name=spec.name)
        write_command = [_TEE, slice_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuResourceControlActionStatus.DRY_RUN,
                group_name=spec.name,
                command=write_command,
            )
            return dry

        existing_body = self._read_file(path=slice_path)
        is_active = self._slice_is_active(name=spec.name)

        if existing_body is not None and existing_body == rendered_body and is_active:
            skipped = self._build_result(
                status=UbuntuResourceControlActionStatus.SKIPPED,
                group_name=spec.name,
                command=write_command,
                message="group already in desired state",
            )
            return skipped

        # Write the slice unit body.
        write_run = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=rendered_body,
        )
        if write_run.exit_code != 0:
            failed = UbuntuResourceControlActionResult(
                status=UbuntuResourceControlActionStatus.FAILED,
                detail=spec.name,
                exit_code=write_run.exit_code,
                stdout=write_run.stdout,
                stderr=write_run.stderr,
                command=list(write_command),
                group_name=spec.name,
                message="slice_unit_write_failed",
            )
            return failed

        # daemon-reload after the write.
        reload_command = [_SYSTEMCTL, "daemon-reload"]
        reload_run = self._runner.run(
            list(reload_command), elevated=self._config.elevated,
        )
        if reload_run.exit_code != 0:
            failed = UbuntuResourceControlActionResult(
                status=UbuntuResourceControlActionStatus.FAILED,
                detail=spec.name,
                exit_code=reload_run.exit_code,
                stdout=reload_run.stdout,
                stderr=reload_run.stderr,
                command=list(reload_command),
                group_name=spec.name,
                message="daemon_reload_failed",
            )
            return failed

        # Start the slice (idempotent in systemd, even when already active).
        start_command = [_SYSTEMCTL, "start", "{}.slice".format(spec.name)]
        start_run = self._runner.run(
            list(start_command), elevated=self._config.elevated,
        )
        if start_run.exit_code != 0:
            failed = UbuntuResourceControlActionResult(
                status=UbuntuResourceControlActionStatus.FAILED,
                detail=spec.name,
                exit_code=start_run.exit_code,
                stdout=start_run.stdout,
                stderr=start_run.stderr,
                command=list(start_command),
                group_name=spec.name,
                message="slice_start_failed",
            )
            return failed

        success = UbuntuResourceControlActionResult(
            status=UbuntuResourceControlActionStatus.SUCCESS,
            detail=spec.name,
            exit_code=start_run.exit_code,
            stdout=start_run.stdout,
            stderr=start_run.stderr,
            command=list(start_command),
            group_name=spec.name,
        )
        return success

    def delete_group(
        self,
        name: str,
    ) -> UbuntuResourceControlActionResult:
        """
            Delete a resource group.

            Stops the slice (informational; failure is ignored),
            removes ``/etc/systemd/system/<name>.slice``, then
            invokes ``systemctl daemon-reload``.

            Idempotent: when the slice unit file does not exist
            AND the slice is not active, returns ``SKIPPED``
            without invoking the runner.

            :param name: Group name.

            :returns: The
                      :class:`UbuntuResourceControlActionResult`.
        """
        try:
            _validate_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=UbuntuResourceControlActionStatus.FAILED,
                group_name=name,
                command=[],
                message="invalid_group_name",
                stderr=str(exc),
            )
            return failed

        slice_path = self._slice_path(name=name)
        stop_command = [_SYSTEMCTL, "stop", "{}.slice".format(name)]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuResourceControlActionStatus.DRY_RUN,
                group_name=name,
                command=stop_command,
            )
            return dry

        slice_exists = self._path_exists(path=slice_path)
        is_active = self._slice_is_active(name=name)
        if not slice_exists and not is_active:
            skipped = self._build_result(
                status=UbuntuResourceControlActionStatus.SKIPPED,
                group_name=name,
                command=stop_command,
                message="slice unit not present",
            )
            return skipped

        # `stop` is informational; an inactive slice that has not been
        # loaded reports a non-zero exit which we ignore so the
        # deletion can proceed.
        self._runner.run(
            list(stop_command), elevated=self._config.elevated,
        )

        if slice_exists:
            rm_command = [_RM, "-f", slice_path]
            rm_run = self._runner.run(
                list(rm_command), elevated=self._config.elevated,
            )
            if rm_run.exit_code != 0:
                failed = UbuntuResourceControlActionResult(
                    status=UbuntuResourceControlActionStatus.FAILED,
                    detail=name,
                    exit_code=rm_run.exit_code,
                    stdout=rm_run.stdout,
                    stderr=rm_run.stderr,
                    command=list(rm_command),
                    group_name=name,
                    message="slice_unit_delete_failed",
                )
                return failed

        reload_command = [_SYSTEMCTL, "daemon-reload"]
        reload_run = self._runner.run(
            list(reload_command), elevated=self._config.elevated,
        )

        if reload_run.exit_code == 0:
            mapped_status = UbuntuResourceControlActionStatus.SUCCESS
            message = None
        else:
            mapped_status = UbuntuResourceControlActionStatus.FAILED
            message = "daemon_reload_failed"

        rtnval = UbuntuResourceControlActionResult(
            status=mapped_status,
            detail=name,
            exit_code=reload_run.exit_code,
            stdout=reload_run.stdout,
            stderr=reload_run.stderr,
            command=list(reload_command),
            group_name=name,
            message=message,
        )
        return rtnval

    def set_cpu_limit(
        self,
        name: str,
        percent: int,
    ) -> UbuntuResourceControlActionResult:
        """
            Set the slice's CPU ceiling.

            Invokes
            ``systemctl set-property <name>.slice CPUQuota=<percent>%``.

            Idempotent: when ``systemctl show`` reports a
            ``CPUQuotaPerSecUSec`` value already equivalent to
            ``percent``, returns ``SKIPPED``.

            :param name: Group name.
            :param percent: CPU percent (0-100*<n_cpus>).  Pass
                            ``0`` or a negative value to clear
                            the cap (rendered as ``CPUQuota=`` to
                            systemd).

            :returns: The
                      :class:`UbuntuResourceControlActionResult`.
        """
        try:
            _validate_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=UbuntuResourceControlActionStatus.FAILED,
                group_name=name,
                command=[],
                message="invalid_group_name",
                stderr=str(exc),
            )
            return failed

        if percent <= 0:
            quota_value = ""
        else:
            quota_value = "{}%".format(percent)

        set_command = [
            _SYSTEMCTL, "set-property",
            "{}.slice".format(name),
            "CPUQuota={}".format(quota_value),
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuResourceControlActionStatus.DRY_RUN,
                group_name=name,
                command=set_command,
            )
            return dry

        # Idempotency probe.
        props = self._get_show_property(
            name=name, props=("CPUQuotaPerSecUSec",),
        )
        current_quota = props.get("CPUQuotaPerSecUSec", "")
        current_percent = _parse_cpu_quota(raw=current_quota)
        if percent <= 0:
            already_match = (current_percent is None)
        else:
            already_match = (current_percent == percent)

        if already_match:
            skipped = self._build_result(
                status=UbuntuResourceControlActionStatus.SKIPPED,
                group_name=name,
                command=set_command,
                message="cpu quota already at requested value",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=set_command, group_name=name, message="set_property_failed",
        )
        return rtnval

    def set_memory_limit(
        self,
        name: str,
        bytes_: int,
    ) -> UbuntuResourceControlActionResult:
        """
            Set the slice's memory ceiling.

            Invokes
            ``systemctl set-property <name>.slice MemoryMax=<bytes_>``.

            Idempotent: when ``systemctl show`` reports
            ``MemoryMax`` already equal to ``bytes_``, returns
            ``SKIPPED``.

            :param name: Group name.
            :param bytes_: Memory ceiling in bytes.  Pass ``0``
                           or negative to clear the cap (rendered
                           as ``MemoryMax=infinity``).

            :returns: The
                      :class:`UbuntuResourceControlActionResult`.
        """
        try:
            _validate_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=UbuntuResourceControlActionStatus.FAILED,
                group_name=name,
                command=[],
                message="invalid_group_name",
                stderr=str(exc),
            )
            return failed

        if bytes_ <= 0:
            memory_value = "infinity"
        else:
            memory_value = "{}".format(bytes_)

        set_command = [
            _SYSTEMCTL, "set-property",
            "{}.slice".format(name),
            "MemoryMax={}".format(memory_value),
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuResourceControlActionStatus.DRY_RUN,
                group_name=name,
                command=set_command,
            )
            return dry

        props = self._get_show_property(
            name=name, props=("MemoryMax",),
        )
        current_raw = props.get("MemoryMax", "")
        current_bytes = _parse_memory_max(raw=current_raw)
        if bytes_ <= 0:
            already_match = (current_bytes is None)
        else:
            already_match = (current_bytes == bytes_)

        if already_match:
            skipped = self._build_result(
                status=UbuntuResourceControlActionStatus.SKIPPED,
                group_name=name,
                command=set_command,
                message="memory limit already at requested value",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=set_command, group_name=name, message="set_property_failed",
        )
        return rtnval

    def set_io_limits(
        self,
        name: str,
        spec: UbuntuIoLimitSpec,
    ) -> UbuntuResourceControlActionResult:
        """
            Set per-slice IO ceilings.

            Builds a single ``systemctl set-property`` invocation
            containing one ``KEY=VALUE`` argument per non-None
            field on ``spec``.

            Idempotent: when every requested field already
            matches the slice's reported value, returns
            ``SKIPPED``.

            :param name: Group name.
            :param spec: The :class:`UbuntuIoLimitSpec`.

            :returns: The
                      :class:`UbuntuResourceControlActionResult`.
        """
        try:
            _validate_name(name=name)
        except ValueError as exc:
            failed = self._build_result(
                status=UbuntuResourceControlActionStatus.FAILED,
                group_name=name,
                command=[],
                message="invalid_group_name",
                stderr=str(exc),
            )
            return failed

        property_args: list[str] = []
        if spec.weight is not None:
            property_args.append("IOWeight={}".format(spec.weight))
        if spec.read_bps is not None and spec.device is not None:
            property_args.append(
                "IOReadBandwidthMax={} {}".format(spec.device, spec.read_bps),
            )
        if spec.write_bps is not None and spec.device is not None:
            property_args.append(
                "IOWriteBandwidthMax={} {}".format(spec.device, spec.write_bps),
            )

        if len(property_args) == 0:
            failed = self._build_result(
                status=UbuntuResourceControlActionStatus.FAILED,
                group_name=name,
                command=[],
                message="empty_io_limit_spec",
                stderr="UbuntuIoLimitSpec carried no actionable field",
            )
            return failed

        set_command = (
            [_SYSTEMCTL, "set-property", "{}.slice".format(name)]
            + list(property_args)
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuResourceControlActionStatus.DRY_RUN,
                group_name=name,
                command=set_command,
            )
            return dry

        # Idempotency.  We compare each requested key against the
        # slice's reported value; if all match, SKIPPED.
        props = self._get_show_property(
            name=name,
            props=("IOWeight", "IOReadBandwidthMax", "IOWriteBandwidthMax"),
        )
        all_match = True
        if spec.weight is not None:
            current_weight = props.get("IOWeight", "")
            try:
                if int(current_weight) != int(spec.weight):
                    all_match = False
            except ValueError:
                all_match = False
        if spec.read_bps is not None and spec.device is not None:
            current_read = props.get("IOReadBandwidthMax", "")
            expected = "{} {}".format(spec.device, spec.read_bps)
            if expected not in current_read:
                all_match = False
        if spec.write_bps is not None and spec.device is not None:
            current_write = props.get("IOWriteBandwidthMax", "")
            expected = "{} {}".format(spec.device, spec.write_bps)
            if expected not in current_write:
                all_match = False

        if all_match:
            skipped = self._build_result(
                status=UbuntuResourceControlActionStatus.SKIPPED,
                group_name=name,
                command=set_command,
                message="io limits already at requested values",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=set_command, group_name=name, message="set_property_failed",
        )
        return rtnval

    def attach_process(
        self,
        name: str,
        pid: int,
    ) -> UbuntuResourceControlActionResult:
        """
            Attach an existing PID to the resource group.

            Writes ``str(pid)`` to
            ``/sys/fs/cgroup/<name>.slice/cgroup.procs`` via
            ``tee --append``.  The kernel atomically removes the
            PID from its previous cgroup as a side effect.

            Idempotent: when the PID is already in the slice's
            ``cgroup.procs``, returns ``SKIPPED``.

            :param name: Group name.
            :param pid: Positive process ID.

            :returns: The
                      :class:`UbuntuResourceControlActionResult`.
        """
        try:
            _validate_name(name=name)
            _validate_pid(pid=pid)
        except ValueError as exc:
            failed = self._build_result(
                status=UbuntuResourceControlActionStatus.FAILED,
                group_name=name,
                pid=pid,
                command=[],
                message="invalid_attach_arguments",
                stderr=str(exc),
            )
            return failed

        procs_path = self._cgroup_procs_path(name=name)
        write_command = [_TEE, "--append", procs_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuResourceControlActionStatus.DRY_RUN,
                group_name=name,
                pid=pid,
                command=write_command,
            )
            return dry

        # Pre-flight: the slice cgroup directory must exist.
        cgroup_dir = self._cgroup_dir(name=name)
        if not self._path_exists(path=cgroup_dir):
            failed = self._build_result(
                status=UbuntuResourceControlActionStatus.FAILED,
                group_name=name,
                pid=pid,
                command=write_command,
                message="slice cgroup not present",
                stderr="cgroup directory not found: {}".format(cgroup_dir),
            )
            return failed

        attached = self._read_cgroup_procs(name=name)
        if pid in attached:
            skipped = self._build_result(
                status=UbuntuResourceControlActionStatus.SKIPPED,
                group_name=name,
                pid=pid,
                command=write_command,
                message="pid already attached",
            )
            return skipped

        write_run = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin="{}\n".format(pid),
        )

        if write_run.exit_code == 0:
            mapped_status = UbuntuResourceControlActionStatus.SUCCESS
            message = None
        else:
            mapped_status = UbuntuResourceControlActionStatus.FAILED
            message = "cgroup_procs_write_failed"

        rtnval = UbuntuResourceControlActionResult(
            status=mapped_status,
            detail=name,
            exit_code=write_run.exit_code,
            stdout=write_run.stdout,
            stderr=write_run.stderr,
            command=list(write_command),
            group_name=name,
            pid=pid,
            message=message,
        )
        return rtnval

    def detach_process(
        self,
        name: str,
        pid: int,
    ) -> UbuntuResourceControlActionResult:
        """
            Detach a PID from the resource group, returning it to
            ``system.slice``.

            Writes ``str(pid)`` to
            ``/sys/fs/cgroup/system.slice/cgroup.procs`` via
            ``tee --append``.  The kernel atomically removes the
            PID from the source slice as part of the move.

            Idempotent: when the PID is NOT currently in the
            slice's ``cgroup.procs``, returns ``SKIPPED``.

            :param name: Group name.
            :param pid: Positive process ID.

            :returns: The
                      :class:`UbuntuResourceControlActionResult`.
        """
        try:
            _validate_name(name=name)
            _validate_pid(pid=pid)
        except ValueError as exc:
            failed = self._build_result(
                status=UbuntuResourceControlActionStatus.FAILED,
                group_name=name,
                pid=pid,
                command=[],
                message="invalid_detach_arguments",
                stderr=str(exc),
            )
            return failed

        target_procs_path = "{}/{}/cgroup.procs".format(
            self._config.cgroup_root, _SYSTEM_SLICE,
        )
        write_command = [_TEE, "--append", target_procs_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuResourceControlActionStatus.DRY_RUN,
                group_name=name,
                pid=pid,
                command=write_command,
            )
            return dry

        attached = self._read_cgroup_procs(name=name)
        if pid not in attached:
            skipped = self._build_result(
                status=UbuntuResourceControlActionStatus.SKIPPED,
                group_name=name,
                pid=pid,
                command=write_command,
                message="pid not currently attached",
            )
            return skipped

        write_run = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin="{}\n".format(pid),
        )

        if write_run.exit_code == 0:
            mapped_status = UbuntuResourceControlActionStatus.SUCCESS
            message = None
        else:
            mapped_status = UbuntuResourceControlActionStatus.FAILED
            message = "cgroup_procs_write_failed"

        rtnval = UbuntuResourceControlActionResult(
            status=mapped_status,
            detail=name,
            exit_code=write_run.exit_code,
            stdout=write_run.stdout,
            stderr=write_run.stderr,
            command=list(write_command),
            group_name=name,
            pid=pid,
            message=message,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_groups(self) -> list[UbuntuResourceGroupInfo]:
        """
            Enumerate every resource group present under the
            cgroup root.

            Lists ``<cgroup_root>`` via ``ls -1``, filters
            entries ending in ``.slice``, then issues one
            ``systemctl show`` per slice and reads each slice's
            ``cgroup.procs`` for attached PIDs.  Reads do not
            elevate.

            :returns: One
                      :class:`UbuntuResourceGroupInfo` per
                      detected slice.  Empty list when no slices
                      exist.
            :raises UbuntuResourceControlQueryError: When ``ls``
                                                     fails with a
                                                     non-zero exit
                                                     and non-empty
                                                     stderr.
        """
        ls_command = [_LS, "-1", self._config.cgroup_root]
        ls_run = self._runner.run(list(ls_command), elevated=False)
        if ls_run.exit_code != 0 and len(ls_run.stderr.strip()) > 0:
            raise UbuntuResourceControlQueryError(
                command=ls_command,
                exit_code=ls_run.exit_code,
                stderr=ls_run.stderr,
            )

        rtnval: list[UbuntuResourceGroupInfo] = []
        for raw in ls_run.stdout.splitlines():
            entry = raw.strip()
            if len(entry) == 0:
                continue
            if not entry.endswith(".slice"):
                continue
            name = entry[: -len(".slice")]

            props = self._get_show_property(
                name=name,
                props=tuple(_SHOW_PROPERTIES.split(",")),
            )

            cpu_percent = _parse_cpu_quota(
                raw=props.get("CPUQuotaPerSecUSec", ""),
            )
            memory_bytes = _parse_memory_max(
                raw=props.get("MemoryMax", ""),
            )
            io_weight_raw = props.get("IOWeight", "")
            try:
                io_weight: Optional[int] = int(io_weight_raw)
            except ValueError:
                io_weight = None

            io_read_bps = _parse_io_bandwidth(
                raw=props.get("IOReadBandwidthMax", ""),
            )
            io_write_bps = _parse_io_bandwidth(
                raw=props.get("IOWriteBandwidthMax", ""),
            )

            attached_pids = self._read_cgroup_procs(name=name)

            is_active = (props.get("ActiveState", "") == "active")

            info = UbuntuResourceGroupInfo(
                name=name,
                cpu_percent=cpu_percent,
                memory_bytes=memory_bytes,
                io_weight=io_weight,
                io_read_bps=io_read_bps,
                io_write_bps=io_write_bps,
                attached_pids=attached_pids,
                is_active=is_active,
                effective_cpu_percent=cpu_percent,
                effective_memory_bytes=memory_bytes,
            )
            rtnval.append(info)

        return rtnval

    def get_group_usage(
        self,
        name: str,
    ) -> UbuntuResourceGroupUsage:
        """
            Read live usage counters for a single resource
            group.

            Reads ``cpu.stat``, ``memory.current``, ``io.stat``,
            and ``cgroup.procs`` from the slice's cgroup
            directory via ``cat``.  Reads do not elevate.

            :param name: Group name.

            :returns: The
                      :class:`UbuntuResourceGroupUsage`.
            :raises UbuntuResourceControlQueryError: When the
                                                     slice cgroup
                                                     directory
                                                     does not
                                                     exist (the
                                                     verb raises
                                                     with
                                                     ``detail="unknown_group"``)
                                                     or when any
                                                     ``cat``
                                                     fails with a
                                                     non-empty
                                                     stderr.
        """
        _validate_name(name=name)

        cgroup_dir = self._cgroup_dir(name=name)
        if not self._path_exists(path=cgroup_dir):
            raise UbuntuResourceControlQueryError(
                command=[_TEST, "-e", cgroup_dir],
                exit_code=1,
                stderr="cgroup directory not found: {}".format(cgroup_dir),
                detail="unknown_group",
            )

        cpu_stat_path = "{}/cpu.stat".format(cgroup_dir)
        cpu_run = self._runner.run([_CAT, cpu_stat_path], elevated=False)
        if cpu_run.exit_code != 0 and len(cpu_run.stderr.strip()) > 0:
            raise UbuntuResourceControlQueryError(
                command=[_CAT, cpu_stat_path],
                exit_code=cpu_run.exit_code,
                stderr=cpu_run.stderr,
            )
        cpu_usage_seconds = _parse_cpu_stat(stdout=cpu_run.stdout)

        mem_path = "{}/memory.current".format(cgroup_dir)
        mem_run = self._runner.run([_CAT, mem_path], elevated=False)
        if mem_run.exit_code != 0 and len(mem_run.stderr.strip()) > 0:
            raise UbuntuResourceControlQueryError(
                command=[_CAT, mem_path],
                exit_code=mem_run.exit_code,
                stderr=mem_run.stderr,
            )
        memory_used_bytes = _parse_memory_current(stdout=mem_run.stdout)

        io_path = "{}/io.stat".format(cgroup_dir)
        io_run = self._runner.run([_CAT, io_path], elevated=False)
        if io_run.exit_code != 0 and len(io_run.stderr.strip()) > 0:
            raise UbuntuResourceControlQueryError(
                command=[_CAT, io_path],
                exit_code=io_run.exit_code,
                stderr=io_run.stderr,
            )
        io_read_bytes, io_write_bytes = _parse_io_stat(stdout=io_run.stdout)

        attached_pids = self._read_cgroup_procs(name=name)

        rtnval = UbuntuResourceGroupUsage(
            name=name,
            cpu_usage_seconds=cpu_usage_seconds,
            memory_used_bytes=memory_used_bytes,
            io_read_bytes=io_read_bytes,
            io_write_bytes=io_write_bytes,
            process_count=len(attached_pids),
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _slice_path(self, *, name: str) -> str:
        """
            :returns: ``<slice_dir>/<name>.slice``.
        """
        rtnval = "{}/{}.slice".format(self._config.slice_dir, name)
        return rtnval

    def _cgroup_dir(self, *, name: str) -> str:
        """
            :returns: ``<cgroup_root>/<name>.slice``.
        """
        rtnval = "{}/{}.slice".format(self._config.cgroup_root, name)
        return rtnval

    def _cgroup_procs_path(self, *, name: str) -> str:
        """
            :returns: ``<cgroup_root>/<name>.slice/cgroup.procs``.
        """
        rtnval = "{}/cgroup.procs".format(self._cgroup_dir(name=name))
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat``.  Reads do not elevate.

            :param path: Absolute path.

            :returns: File contents on success; ``None`` when
                      ``cat`` reports a non-zero exit (file is
                      missing).
        """
        command = [_CAT, path]
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
        command = [_TEST, "-e", path]
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _slice_is_active(self, *, name: str) -> bool:
        """
            Probe ``systemctl is-active <name>.slice``.  Reads
            do not elevate.

            :param name: Group name.

            :returns: True when ``is-active`` reports exactly
                      ``"active"``.
        """
        command = [_SYSTEMCTL, "is-active", "{}.slice".format(name)]
        run_result = self._runner.run(list(command), elevated=False)
        reported = run_result.stdout.strip()
        rtnval = (reported == "active")
        return rtnval

    def _read_cgroup_procs(self, *, name: str) -> list[int]:
        """
            Read the slice's ``cgroup.procs`` file and parse the
            integer PIDs.

            :param name: Group name.

            :returns: Parsed PID list.  Empty list when the file
                      is missing or empty.
        """
        procs_path = self._cgroup_procs_path(name=name)
        body = self._read_file(path=procs_path)
        if body is None:
            rtnval: list[int] = []
            return rtnval

        rtnval = []
        for raw in body.splitlines():
            line = raw.strip()
            if len(line) == 0:
                continue
            try:
                pid_value = int(line)
            except ValueError:
                continue
            rtnval.append(pid_value)
        return rtnval

    def _get_show_property(
        self,
        *,
        name: str,
        props: tuple,
    ) -> dict:
        """
            Invoke ``systemctl show <name>.slice
            --property=<P1,P2,...>`` and parse the output into a
            dict.  Reads do not elevate.

            :param name: Group name.
            :param props: Tuple of property names to query.

            :returns: ``dict[str, str]`` keyed by property name.
                      Empty when ``systemctl show`` exits
                      non-zero.
        """
        prop_string = ",".join(props)
        command = [
            _SYSTEMCTL, "show", "{}.slice".format(name),
            "--property={}".format(prop_string),
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: dict = {}
            return rtnval
        rtnval = _parse_show_output(stdout=run_result.stdout)
        return rtnval

    def _dispatch_write(
        self,
        *,
        command: list[str],
        group_name: str,
        pid: Optional[int] = None,
        message: Optional[str] = None,
    ) -> UbuntuResourceControlActionResult:
        """
            Dispatch a non-file write command and wrap the
            result.

            :param command: Full argv.
            :param group_name: Group name to echo onto the
                               result.
            :param pid: Optional PID for attach / detach
                        results.
            :param message: Short FAILED reason token attached
                            on non-zero exit.

            :returns: The
                      :class:`UbuntuResourceControlActionResult`.
        """
        run_result = self._runner.run(
            list(command), elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = UbuntuResourceControlActionStatus.SUCCESS
            mapped_message = None
        else:
            mapped_status = UbuntuResourceControlActionStatus.FAILED
            mapped_message = message

        rtnval = UbuntuResourceControlActionResult(
            status=mapped_status,
            detail=group_name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            group_name=group_name,
            pid=pid,
            message=mapped_message,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuResourceControlActionStatus,
        group_name: Optional[str],
        command: list[str],
        pid: Optional[int] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuResourceControlActionResult:
        """
            Construct an
            :class:`UbuntuResourceControlActionResult` without
            invoking the runner.

            :param status: Outcome status.
            :param group_name: Group name.
            :param command: Would-have-been argv (copied for
                            capture).
            :param pid: Optional PID echoed onto the result.
            :param message: Optional short reason token.
            :param stderr: Optional synthetic stderr payload.

            :returns: The
                      :class:`UbuntuResourceControlActionResult`.
        """
        rtnval = UbuntuResourceControlActionResult(
            status=status,
            detail=group_name,
            command=list(command),
            group_name=group_name,
            pid=pid,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — validation, rendering, parsing
# ----------------------------------------------------------------------


def _validate_name(*, name: str) -> None:
    """
        Raise ``ValueError`` when ``name`` does not match the
        allowed character set.

        :param name: Candidate group name.
        :raises ValueError: When ``name`` does not match
                            ``[A-Za-z0-9][A-Za-z0-9._\\-]*``.
    """
    if name is None:
        err_msg = "group name must not be None"
        raise ValueError(err_msg)
    if _VALID_NAME.match(name) is None:
        err_msg = "invalid group name: {!r}".format(name)
        raise ValueError(err_msg)
    return


def _validate_pid(*, pid: int) -> None:
    """
        Raise ``ValueError`` when ``pid`` is not a positive
        integer.

        :param pid: Candidate PID.
        :raises ValueError: When ``pid`` is None or non-positive.
    """
    if pid is None:
        err_msg = "pid must not be None"
        raise ValueError(err_msg)
    if pid <= 0:
        err_msg = "pid must be a positive integer: {}".format(pid)
        raise ValueError(err_msg)
    return


def _render_slice_body(*, spec: UbuntuResourceGroupSpec) -> str:
    """
        Render the ``<name>.slice`` unit body for ``spec``.

        :param spec: The resource group spec.

        :returns: The full unit-file body ending in ``\\n``.
    """
    lines: list[str] = []
    lines.append("[Unit]")
    lines.append(
        "Description=codecraftai-managed resource group: {}".format(spec.name),
    )
    lines.append("")
    lines.append("[Slice]")
    if spec.cpu_percent is not None:
        lines.append("CPUQuota={}%".format(spec.cpu_percent))
    if spec.memory_bytes is not None:
        lines.append("MemoryMax={}".format(spec.memory_bytes))
    if spec.io_weight is not None:
        lines.append("IOWeight={}".format(spec.io_weight))

    body = "\n".join(lines) + "\n"
    return body


def _parse_show_output(*, stdout: str) -> dict:
    """
        Parse ``systemctl show`` ``key=value`` lines into a dict.

        :param stdout: Raw stdout text.

        :returns: ``dict[str, str]`` of parsed entries.
    """
    rtnval: dict = {}
    for raw in stdout.splitlines():
        line = raw.strip()
        if len(line) == 0 or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        rtnval[key.strip()] = value.strip()
    return rtnval


def _parse_cpu_quota(*, raw: str) -> Optional[int]:
    """
        Convert systemd's ``CPUQuotaPerSecUSec`` value to a
        percent (relative to one core).

        ``CPUQuotaPerSecUSec`` is reported as the number of
        microseconds of CPU time allowed per real second.  A
        50% quota is reported as ``500000us`` (or simply
        ``500000``); ``infinity`` means unlimited.

        :param raw: The raw value from ``systemctl show``.

        :returns: Integer percent, or ``None`` for ``infinity``
                  / unparseable values.
    """
    if raw is None:
        rtnval: Optional[int] = None
        return rtnval

    candidate = raw.strip()
    if len(candidate) == 0:
        rtnval = None
        return rtnval
    if candidate == "infinity":
        rtnval = None
        return rtnval

    # Strip the optional ``us`` suffix.
    if candidate.endswith("us"):
        numeric_part = candidate[:-2]
    else:
        numeric_part = candidate

    try:
        usec = int(numeric_part)
    except ValueError:
        rtnval = None
        return rtnval

    # 1_000_000 microseconds per second = 100% of one core.
    rtnval = int(round(usec / 10000.0))
    return rtnval


def _parse_memory_max(*, raw: str) -> Optional[int]:
    """
        Convert systemd's ``MemoryMax`` value to a byte count.

        ``MemoryMax`` is reported as a decimal byte count or
        ``infinity``.

        :param raw: The raw value.

        :returns: Integer bytes, or ``None`` for ``infinity``
                  / unparseable values.
    """
    if raw is None:
        rtnval: Optional[int] = None
        return rtnval

    candidate = raw.strip()
    if len(candidate) == 0:
        rtnval = None
        return rtnval
    if candidate == "infinity":
        rtnval = None
        return rtnval

    try:
        rtnval = int(candidate)
    except ValueError:
        rtnval = None
    return rtnval


def _parse_io_bandwidth(*, raw: str) -> Optional[int]:
    """
        Parse a single bps value out of an
        ``IOReadBandwidthMax`` / ``IOWriteBandwidthMax`` field.

        The systemd value form is ``<device> <bps>``; for empty
        values ``""`` is reported.  When multiple device-bps
        pairs are present (space-separated) we take the FIRST
        pair's bps.  This is the contract-mandated
        single-aggregate field; per-device enumeration lives on
        a future extension.

        :param raw: The raw value.

        :returns: Integer bps, or ``None`` when unset.
    """
    if raw is None:
        rtnval: Optional[int] = None
        return rtnval
    candidate = raw.strip()
    if len(candidate) == 0:
        rtnval = None
        return rtnval
    tokens = candidate.split()
    if len(tokens) < 2:
        rtnval = None
        return rtnval
    bps_token = tokens[1]
    try:
        rtnval = int(bps_token)
    except ValueError:
        rtnval = None
    return rtnval


def _parse_cpu_stat(*, stdout: str) -> Optional[float]:
    """
        Parse ``cpu.stat`` and return cumulative CPU usage in
        seconds.

        ``cpu.stat`` reports lines like::

            usage_usec 12345678
            user_usec 11111111
            system_usec 1234567

        :param stdout: Raw ``cpu.stat`` body.

        :returns: ``usage_usec / 1_000_000`` as a float, or
                  ``None`` when the line is not present.
    """
    for raw in stdout.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        key = parts[0]
        value = parts[1].strip()
        if key != "usage_usec":
            continue
        try:
            usec = int(value)
        except ValueError:
            continue
        rtnval: Optional[float] = usec / 1_000_000.0
        return rtnval
    rtnval = None
    return rtnval


def _parse_memory_current(*, stdout: str) -> Optional[int]:
    """
        Parse ``memory.current`` (a single integer on one line).

        :param stdout: Raw file body.

        :returns: Integer bytes, or ``None`` when the body does
                  not parse.
    """
    candidate = stdout.strip()
    if len(candidate) == 0:
        rtnval: Optional[int] = None
        return rtnval
    try:
        rtnval = int(candidate)
    except ValueError:
        rtnval = None
    return rtnval


def _parse_io_stat(*, stdout: str) -> tuple:
    """
        Parse ``io.stat`` and return aggregate read / write
        byte totals.

        Each line of ``io.stat`` has the form::

            <major>:<minor> rbytes=<n> wbytes=<n> rios=<n> wios=<n> ...

        We sum ``rbytes`` and ``wbytes`` across every line.

        :param stdout: Raw ``io.stat`` body.

        :returns: ``(io_read_bytes, io_write_bytes)`` tuple.
                  Both elements are ``None`` when no parseable
                  line was found.
    """
    total_read = 0
    total_write = 0
    found_any = False

    for raw in stdout.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        tokens = line.split()
        for token in tokens:
            if "=" not in token:
                continue
            key, _, value = token.partition("=")
            try:
                numeric = int(value)
            except ValueError:
                continue
            if key == "rbytes":
                total_read = total_read + numeric
                found_any = True
            elif key == "wbytes":
                total_write = total_write + numeric
                found_any = True

    if not found_any:
        rtnval = (None, None)
        return rtnval
    rtnval = (total_read, total_write)
    return rtnval
