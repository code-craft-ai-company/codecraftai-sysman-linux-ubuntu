"""
    Ubuntu kdump-management feature.

    Implements the cross-OS kdump-management contract for Ubuntu using
    the Debian-family ``kdump-tools`` service.  Ubuntu's kdump surface
    differs from RHEL in two ways:

        1. Configuration lives in ``/etc/default/kdump-tools`` (a shell-
           sourced ``KEY=value`` file) rather than ``/etc/kdump.conf``.
        2. The service unit is named ``kdump-tools.service``, not
           ``kdump.service``.

    Verb plumbing maps to:

        * ``enable_kdump``  -> rewrite ``USE_KDUMP=1`` and ``systemctl
          enable --now kdump-tools.service``
        * ``disable_kdump`` -> rewrite ``USE_KDUMP=0`` and ``systemctl
          disable --now kdump-tools.service``
        * ``configure_kdump_target`` -> rewrite ``KDUMP_COREDIR`` /
          ``SSH`` / ``NFS`` keys appropriate to the target kind, then
          ``systemctl restart kdump-tools.service``
        * ``get_kdump_status`` -> ``kdump-config show`` + parser
        * ``list_recent_dumps`` -> ``ls -1t`` over ``/var/crash/``
        * ``clear_old_dumps`` -> ``find /var/crash -mtime +N -exec rm``
        * ``get_dump_info`` -> ``cat`` of ``vmcore-dmesg.txt`` /
          ``dmesg.<id>``

    File I/O policy:
        Every read / write is performed through the injected
        :class:`ICommandRunner` (``cat`` / ``tee`` / ``ls`` / ``find``
        / ``stat``).  This module does NOT consume an
        :class:`IFileOperator`; the rest of the Ubuntu sysman package
        routes file I/O through the runner so dry-run, elevation, and
        aspect composition compose uniformly.

    Elevation policy:
        Every write verb (``enable_kdump``, ``disable_kdump``,
        ``configure_kdump_target``, ``clear_old_dumps``) honours
        ``config.elevated``.  Reads (``get_kdump_status``,
        ``list_recent_dumps``, ``get_dump_info``) and idempotency
        probes never elevate.

    Reboot semantics:
        Ubuntu's kdump-tools package re-arms the capture kernel on
        ``systemctl restart kdump-tools`` when ``USE_KDUMP=1`` and a
        ``crashkernel=`` reservation is already present on the running
        kernel command line.  Changing the ``crashkernel=`` reservation
        is delegated to ``boot-management``; this manager does NOT
        rewrite GRUB.  When ``configure_kdump_target`` succeeds, the
        ``requires_reboot`` flag is left ``False`` because the
        kdump-tools restart is enough.
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
_KDUMP_CONFIG = "kdump-config"
_CAT = "cat"
_TEE = "tee"
_LS = "ls"
_FIND = "find"
_STAT = "stat"

_DEFAULT_KDUMP_DEFAULTS = "/etc/default/kdump-tools"
_DEFAULT_CRASH_DIR = "/var/crash"
_DEFAULT_SERVICE_NAME = "kdump-tools.service"

_USE_KDUMP_KEY = "USE_KDUMP"
_KDUMP_COREDIR_KEY = "KDUMP_COREDIR"
_KDUMP_SSH_USER_KEY = "SSH"
_KDUMP_SSH_KEY_KEY = "SSH_KEY"
_KDUMP_NFS_KEY = "NFS"
_KDUMP_RAW_KEY = "RAW"
_KDUMP_COMPRESS_KEY = "MAKEDUMPFILE_ARGS"


UbuntuKdumpActionStatus = ActionStatus


class UbuntuKdumpTargetKind(Enum):
    """
        Native Ubuntu kdump-tools target kinds.

        Maps directly to the ``/etc/default/kdump-tools`` keys consulted
        when assembling the capture-kernel command line.  Values are
        intentionally short identifiers so they round-trip through JSON
        / TOML / YAML serialisation cleanly.
    """
    LOCAL_PATH = "local_path"
    SSH        = "ssh"
    NFS        = "nfs"
    RAW        = "raw"


@dataclass
class UbuntuKdumpManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuKdumpManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param kdump_defaults: Path to the kdump-tools defaults file.
                               Default: ``/etc/default/kdump-tools``.
        :param crash_dir: Directory where kdump-tools writes captured
                          dumps.  Default: ``/var/crash``.  Used as the
                          search root for ``list_recent_dumps``,
                          ``clear_old_dumps``, and ``get_dump_info``.
        :param service_name: Systemd unit name.  Default:
                             ``kdump-tools.service``.
    """
    kdump_defaults: str = _DEFAULT_KDUMP_DEFAULTS
    crash_dir: str = _DEFAULT_CRASH_DIR
    service_name: str = _DEFAULT_SERVICE_NAME


@dataclass
class UbuntuKdumpActionResult(FeatureActionResult):
    """
        Result of a kdump-management write operation.

        :param target: The dump target affected by the operation, when
                       applicable (e.g. ``"/var/crash"`` for a
                       LOCAL_PATH configure call).  ``None`` for
                       lifecycle verbs that do not touch the target.
        :param requires_reboot: Always ``False`` on Ubuntu — the
                                kdump-tools restart re-arms the capture
                                kernel without a reboot.  Carried on the
                                result for cross-OS contract
                                conformance.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    target: Optional[str] = None
    requires_reboot: bool = False
    message: Optional[str] = None


@dataclass
class UbuntuKdumpTargetSpec:
    """
        Desired kdump-tools target.

        Exactly one of ``path``, ``(ssh_user, ssh_host)``, or
        ``nfs_export`` is consulted depending on ``target_kind``.  The
        manager validates the combination and rejects mismatches with a
        ``FAILED`` result rather than raising.

        :param target_kind: The :class:`UbuntuKdumpTargetKind` selector.
        :param path: Local filesystem path for ``LOCAL_PATH``.
        :param ssh_user: Remote user for ``SSH``.
        :param ssh_host: Remote host for ``SSH``.
        :param nfs_export: ``host:/export`` string for ``NFS``.
        :param compression: Optional ``makedumpfile`` compression flags
                            (e.g. ``"-c"``).  When non-None, written
                            into the ``MAKEDUMPFILE_ARGS`` key.
    """
    target_kind: UbuntuKdumpTargetKind
    path: Optional[str] = None
    ssh_user: Optional[str] = None
    ssh_host: Optional[str] = None
    nfs_export: Optional[str] = None
    compression: Optional[str] = None


@dataclass
class UbuntuKdumpStatus:
    """
        Snapshot of the current kdump-tools state.

        :param enabled: True when ``USE_KDUMP=1`` in the defaults file
                        AND the systemd unit is enabled at boot.
        :param running: True when ``kdump-config show`` reports the
                        capture kernel as armed (``current state:
                        ready to kdump``).
        :param target_kind: The :class:`UbuntuKdumpTargetKind` deduced
                            from the active configuration, or ``None``
                            when no target is configured.
        :param target_path: The active dump target locator (path,
                            ``user@host:/path``, or ``host:/export``)
                            as reported by the parser.
        :param kernel: Capture-kernel image path as reported by
                       ``kdump-config show`` (e.g.
                       ``/var/lib/kdump/vmlinuz``).
        :param reserved_memory_mb: Memory reserved for the capture
                                   kernel in MiB, when reported.
        :param message: Optional human-readable advisory.
    """
    enabled: bool
    running: bool
    target_kind: Optional[UbuntuKdumpTargetKind] = None
    target_path: Optional[str] = None
    kernel: Optional[str] = None
    reserved_memory_mb: Optional[int] = None
    message: Optional[str] = None


@dataclass
class UbuntuKdumpInfo:
    """
        Metadata for a single captured kdump artifact.

        :param id: Identifier — the timestamp directory basename
                   (e.g. ``202604240931``) or a file basename for raw
                   captures.
        :param path: Absolute path to the artifact directory or file.
        :param created: Modification timestamp as reported by ``stat``
                        / ``ls -l``.  Free-form text — callers SHOULD
                        treat this as opaque.
        :param size_bytes: On-disk size of the artifact, when known.
        :param kernel: Capture-kernel image string parsed from
                       ``vmcore-dmesg.txt``, when available.
        :param panic_message: First panic-line text parsed from
                              ``vmcore-dmesg.txt`` / ``dmesg.<id>``,
                              when available.
    """
    id: str
    path: str
    created: str
    size_bytes: Optional[int] = None
    kernel: Optional[str] = None
    panic_message: Optional[str] = None


class UbuntuKdumpQueryError(Exception):
    """
        Raised when a kdump-management read fails for a reason other
        than the target simply being absent.
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

        err_msg_lines = ["Kdump query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Module-level helpers — defaults-file mutation
# ----------------------------------------------------------------------


def _apply_defaults_keys(
    *,
    content: str,
    updates: dict,
) -> str:
    """
        Apply a set of ``KEY=value`` updates to a shell-sourced
        defaults file, preserving comments and ordering.

        Only keys present in ``updates`` are touched.  Existing matching
        keys are replaced in place; absent keys are appended at the
        bottom.  Quoting style of the existing entry is preserved when
        present (single-quoted, double-quoted, or unquoted); when
        appending a new entry, the value is double-quoted.

        :param content: Original file body.
        :param updates: Mapping of ``KEY`` to desired raw string value
                        (without quotes).

        :returns: The rewritten file body, ending with a newline.
    """
    pending = dict(updates)
    lines = content.splitlines()
    rewritten: list[str] = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if len(stripped) == 0 or stripped.startswith("#"):
            rewritten.append(raw_line)
            continue
        if "=" not in stripped:
            rewritten.append(raw_line)
            continue

        key_part, _, value_part = stripped.partition("=")
        key = key_part.strip()
        if key not in pending:
            rewritten.append(raw_line)
            continue

        new_value = pending.pop(key)
        existing_value = value_part.strip()
        rendered = _render_defaults_value(
            new_value=new_value,
            existing_value=existing_value,
        )
        rewritten.append("{}={}".format(key, rendered))

    for key, value in pending.items():
        rendered = _render_defaults_value(
            new_value=value,
            existing_value="",
        )
        rewritten.append("{}={}".format(key, rendered))

    body = "\n".join(rewritten)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _render_defaults_value(
    *,
    new_value: str,
    existing_value: str,
) -> str:
    """
        Render ``new_value`` using the same quoting style as
        ``existing_value`` when one is detectable.

        :param new_value: Raw replacement value.
        :param existing_value: The existing right-hand side, possibly
                               quoted.

        :returns: The formatted right-hand side suitable for direct
                  concatenation after ``KEY=``.
    """
    if len(existing_value) >= 2:
        first = existing_value[0]
        last = existing_value[-1]
        if first == "'" and last == "'":
            escaped = new_value.replace("'", "'\"'\"'")
            rtnval = "'{}'".format(escaped)
            return rtnval
        if first == '"' and last == '"':
            escaped = new_value.replace('"', '\\"')
            rtnval = '"{}"'.format(escaped)
            return rtnval

    if len(existing_value) == 0:
        escaped = new_value.replace('"', '\\"')
        rtnval = '"{}"'.format(escaped)
        return rtnval

    rtnval = new_value
    return rtnval


def _parse_defaults_file(*, content: str) -> dict:
    """
        Parse ``KEY=value`` lines from a shell-sourced defaults file,
        stripping quotes from values.

        :param content: The file body.

        :returns: A dict of ``KEY`` to unquoted string value.  Lines
                  that are blank, commented, or do not match
                  ``KEY=value`` are silently ignored.
    """
    rtnval: dict = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if len(line) == 0 or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key_part, _, value_part = line.partition("=")
        key = key_part.strip()
        value = value_part.strip()
        if len(value) >= 2:
            first = value[0]
            last = value[-1]
            if (first == "'" and last == "'") or (first == '"' and last == '"'):
                value = value[1:-1]
        rtnval[key] = value
    return rtnval


def _build_target_updates(
    *,
    spec: UbuntuKdumpTargetSpec,
) -> dict:
    """
        Translate a :class:`UbuntuKdumpTargetSpec` into the set of
        defaults-file key updates that realise it.

        Always sets every target-related key — including blanking those
        that are not relevant to the requested kind — so that toggling
        between target kinds does not leave stale keys behind.

        :param spec: The desired target.

        :returns: A dict of ``KEY`` to string value (empty string means
                  "blank the entry").

        :raises ValueError: When ``spec`` carries a combination of
                            fields that cannot be reduced to a target.
    """
    target_kind = spec.target_kind
    if target_kind == UbuntuKdumpTargetKind.LOCAL_PATH:
        if spec.path is None or len(spec.path) == 0:
            err_msg = "LOCAL_PATH target requires a non-empty path"
            raise ValueError(err_msg)
        rtnval = {
            _KDUMP_COREDIR_KEY: spec.path,
            _KDUMP_SSH_USER_KEY: "",
            _KDUMP_NFS_KEY: "",
            _KDUMP_RAW_KEY: "",
        }
    elif target_kind == UbuntuKdumpTargetKind.SSH:
        if spec.ssh_user is None or len(spec.ssh_user) == 0:
            err_msg = "SSH target requires ssh_user"
            raise ValueError(err_msg)
        if spec.ssh_host is None or len(spec.ssh_host) == 0:
            err_msg = "SSH target requires ssh_host"
            raise ValueError(err_msg)
        endpoint = "{}@{}".format(spec.ssh_user, spec.ssh_host)
        rtnval = {
            _KDUMP_SSH_USER_KEY: endpoint,
            _KDUMP_NFS_KEY: "",
            _KDUMP_RAW_KEY: "",
        }
    elif target_kind == UbuntuKdumpTargetKind.NFS:
        if spec.nfs_export is None or len(spec.nfs_export) == 0:
            err_msg = "NFS target requires nfs_export"
            raise ValueError(err_msg)
        rtnval = {
            _KDUMP_NFS_KEY: spec.nfs_export,
            _KDUMP_SSH_USER_KEY: "",
            _KDUMP_RAW_KEY: "",
        }
    elif target_kind == UbuntuKdumpTargetKind.RAW:
        if spec.path is None or len(spec.path) == 0:
            err_msg = "RAW target requires a path (block device)"
            raise ValueError(err_msg)
        rtnval = {
            _KDUMP_RAW_KEY: spec.path,
            _KDUMP_SSH_USER_KEY: "",
            _KDUMP_NFS_KEY: "",
        }
    else:
        err_msg = "Unhandled target kind: {!r}".format(target_kind)
        raise ValueError(err_msg)

    if spec.compression is not None:
        rtnval[_KDUMP_COMPRESS_KEY] = spec.compression

    return rtnval


def _deduce_target_kind(*, parsed: dict) -> Optional[UbuntuKdumpTargetKind]:
    """
        Deduce the active :class:`UbuntuKdumpTargetKind` from a parsed
        defaults file.

        Order of precedence: SSH -> NFS -> RAW -> LOCAL_PATH.  Returns
        ``None`` when no target-bearing key has a non-empty value.

        :param parsed: Result of :func:`_parse_defaults_file`.

        :returns: The :class:`UbuntuKdumpTargetKind`, or ``None``.
    """
    ssh_value = parsed.get(_KDUMP_SSH_USER_KEY, "")
    if len(ssh_value) > 0:
        rtnval: Optional[UbuntuKdumpTargetKind] = UbuntuKdumpTargetKind.SSH
        return rtnval

    nfs_value = parsed.get(_KDUMP_NFS_KEY, "")
    if len(nfs_value) > 0:
        rtnval = UbuntuKdumpTargetKind.NFS
        return rtnval

    raw_value = parsed.get(_KDUMP_RAW_KEY, "")
    if len(raw_value) > 0:
        rtnval = UbuntuKdumpTargetKind.RAW
        return rtnval

    coredir_value = parsed.get(_KDUMP_COREDIR_KEY, "")
    if len(coredir_value) > 0:
        rtnval = UbuntuKdumpTargetKind.LOCAL_PATH
        return rtnval

    rtnval = None
    return rtnval


def _deduce_target_locator(
    *,
    parsed: dict,
    kind: Optional[UbuntuKdumpTargetKind],
) -> Optional[str]:
    """
        Pick the locator string that corresponds to ``kind`` from
        ``parsed``.

        :param parsed: Result of :func:`_parse_defaults_file`.
        :param kind: The deduced target kind.

        :returns: The locator string, or ``None``.
    """
    if kind is None:
        rtnval: Optional[str] = None
        return rtnval
    if kind == UbuntuKdumpTargetKind.LOCAL_PATH:
        rtnval = parsed.get(_KDUMP_COREDIR_KEY) or None
        return rtnval
    if kind == UbuntuKdumpTargetKind.SSH:
        rtnval = parsed.get(_KDUMP_SSH_USER_KEY) or None
        return rtnval
    if kind == UbuntuKdumpTargetKind.NFS:
        rtnval = parsed.get(_KDUMP_NFS_KEY) or None
        return rtnval
    if kind == UbuntuKdumpTargetKind.RAW:
        rtnval = parsed.get(_KDUMP_RAW_KEY) or None
        return rtnval

    rtnval = None
    return rtnval


def _parse_kdump_config_show(*, text: str) -> dict:
    """
        Parse ``kdump-config show`` output into a dict.

        Sample output::

            DUMP_MODE:        kdump
            USE_KDUMP:        1
            KDUMP_SYSCTL:     kernel.panic_on_oops=1
            KDUMP_COREDIR:    /var/crash
            crashkernel addr: 0x2c000000
              /var/lib/kdump/vmlinuz: symbolic link to /boot/...
              kdump initrd:
              /var/lib/kdump/initrd.img: symbolic link to ...
            current state:    ready to kdump

        :param text: Raw stdout text.

        :returns: Dict of stripped ``key`` -> ``value`` strings.
    """
    rtnval: dict = {}
    for raw_line in text.splitlines():
        if ":" not in raw_line:
            continue
        key_part, _, value_part = raw_line.partition(":")
        key = key_part.strip()
        value = value_part.strip()
        if len(key) == 0:
            continue
        rtnval[key] = value
    return rtnval


def _parse_dmesg_metadata(*, text: str) -> dict:
    """
        Best-effort parse of the first ~30 lines of a vmcore dmesg dump
        / panic dmesg log.

        :param text: Raw text payload.

        :returns: A dict that may carry ``kernel`` and / or
                  ``panic_message`` keys.  Missing keys mean the parser
                  could not find the corresponding fragment.
    """
    rtnval: dict = {}
    line_count = 0
    for raw_line in text.splitlines():
        line_count = line_count + 1
        if line_count > 30:
            break
        line = raw_line.strip()

        if rtnval.get("kernel") is None and "Linux version" in line:
            rtnval["kernel"] = line
            continue

        if rtnval.get("panic_message") is None and "Kernel panic" in line:
            rtnval["panic_message"] = line
            continue

    return rtnval


# ----------------------------------------------------------------------
# UbuntuKdumpManager
# ----------------------------------------------------------------------


class UbuntuKdumpManager(BaseFeatureManager):
    """
        Ubuntu kdump-tools manager.  Every verb is dispatched through
        the injected :class:`ICommandRunner`; defaults-file I/O is
        performed via ``cat`` / ``tee`` rather than a separate file
        operator.

        Writes (``enable_kdump``, ``disable_kdump``,
        ``configure_kdump_target``, ``clear_old_dumps``) honour
        ``config.elevated`` and pass it through to the runner.  Reads
        (``get_kdump_status``, ``list_recent_dumps``, ``get_dump_info``)
        and idempotency probes always pass ``elevated=False``.
    """

    result_class = UbuntuKdumpActionResult
    status_class = UbuntuKdumpActionStatus
    feature_name = "kdump-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def enable_kdump(self) -> UbuntuKdumpActionResult:
        """
            Enable the kdump-tools subsystem.

            Rewrites ``USE_KDUMP=1`` in the defaults file (when it is
            not already so), then runs ``systemctl enable --now
            <service>``.  Skipped when both the defaults flag is
            already ``1`` AND the systemd unit is already active+enabled.

            :returns: The :class:`UbuntuKdumpActionResult`.
        """
        action = "enable"
        enable_command = [
            _SYSTEMCTL, "enable", "--now", self._config.service_name,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuKdumpActionStatus.DRY_RUN,
                detail=action,
                command=list(enable_command),
                target=self._config.kdump_defaults,
            )
            return dry

        defaults_text = self._read_defaults()
        parsed = _parse_defaults_file(content=defaults_text)
        already_use = parsed.get(_USE_KDUMP_KEY, "").strip() == "1"

        service_state = self._query_service_state()
        already_enabled = (
            service_state["active"] == "active"
            and service_state["enabled"] == "enabled"
        )

        if already_use and already_enabled:
            skipped = self._build_result(
                status=UbuntuKdumpActionStatus.SKIPPED,
                detail=action,
                command=list(enable_command),
                target=self._config.kdump_defaults,
                message="kdump-tools is already enabled and active",
            )
            return skipped

        if not already_use:
            new_body = _apply_defaults_keys(
                content=defaults_text,
                updates={_USE_KDUMP_KEY: "1"},
            )
            write_result = self._write_defaults(content=new_body)
            if write_result.status == UbuntuKdumpActionStatus.FAILED:
                write_result.detail = action
                return write_result

        rtnval = self._dispatch(
            command=enable_command,
            detail=action,
            operation="enable_kdump",
            target=self._config.kdump_defaults,
        )
        return rtnval

    def disable_kdump(self) -> UbuntuKdumpActionResult:
        """
            Disable the kdump-tools subsystem.

            Rewrites ``USE_KDUMP=0`` in the defaults file (when it is
            not already so), then runs ``systemctl disable --now
            <service>``.  Skipped when both the defaults flag is
            already ``0`` AND the systemd unit is already inactive+disabled.

            :returns: The :class:`UbuntuKdumpActionResult`.
        """
        action = "disable"
        disable_command = [
            _SYSTEMCTL, "disable", "--now", self._config.service_name,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuKdumpActionStatus.DRY_RUN,
                detail=action,
                command=list(disable_command),
                target=self._config.kdump_defaults,
            )
            return dry

        defaults_text = self._read_defaults()
        parsed = _parse_defaults_file(content=defaults_text)
        already_disabled_in_defaults = (
            parsed.get(_USE_KDUMP_KEY, "").strip() == "0"
        )

        service_state = self._query_service_state()
        already_disabled_in_systemd = (
            service_state["active"] != "active"
            and service_state["enabled"] != "enabled"
        )

        if already_disabled_in_defaults and already_disabled_in_systemd:
            skipped = self._build_result(
                status=UbuntuKdumpActionStatus.SKIPPED,
                detail=action,
                command=list(disable_command),
                target=self._config.kdump_defaults,
                message="kdump-tools is already disabled",
            )
            return skipped

        if not already_disabled_in_defaults:
            new_body = _apply_defaults_keys(
                content=defaults_text,
                updates={_USE_KDUMP_KEY: "0"},
            )
            write_result = self._write_defaults(content=new_body)
            if write_result.status == UbuntuKdumpActionStatus.FAILED:
                write_result.detail = action
                return write_result

        rtnval = self._dispatch(
            command=disable_command,
            detail=action,
            operation="disable_kdump",
            target=self._config.kdump_defaults,
        )
        return rtnval

    def configure_kdump_target(
        self,
        spec: UbuntuKdumpTargetSpec,
    ) -> UbuntuKdumpActionResult:
        """
            Apply ``spec`` to the kdump-tools defaults file.

            Rewrites the target-bearing keys (``KDUMP_COREDIR``,
            ``SSH``, ``NFS``, ``RAW``) so that exactly the requested
            target is active and the others are blank, then restarts the
            kdump-tools service so the change takes effect.  Skipped
            when the existing defaults already encode the requested
            target.

            :param spec: The :class:`UbuntuKdumpTargetSpec` to apply.

            :returns: The :class:`UbuntuKdumpActionResult`.
        """
        action = "configure_target"
        restart_command = [
            _SYSTEMCTL, "restart", self._config.service_name,
        ]

        try:
            updates = _build_target_updates(spec=spec)
        except ValueError as exc:
            failed = self._build_result(
                status=UbuntuKdumpActionStatus.FAILED,
                detail=action,
                command=None,
                target=None,
                message=str(exc),
                stderr="invalid_target_spec",
            )
            return failed

        target_locator = _deduce_target_locator(
            parsed=updates,
            kind=spec.target_kind,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuKdumpActionStatus.DRY_RUN,
                detail=action,
                command=list(restart_command),
                target=target_locator,
            )
            return dry

        defaults_text = self._read_defaults()
        parsed_existing = _parse_defaults_file(content=defaults_text)
        existing_kind = _deduce_target_kind(parsed=parsed_existing)
        existing_locator = _deduce_target_locator(
            parsed=parsed_existing,
            kind=existing_kind,
        )

        if (
            existing_kind == spec.target_kind
            and existing_locator == target_locator
        ):
            skipped = self._build_result(
                status=UbuntuKdumpActionStatus.SKIPPED,
                detail=action,
                command=list(restart_command),
                target=target_locator,
                message="defaults file already matches requested target",
            )
            return skipped

        new_body = _apply_defaults_keys(
            content=defaults_text,
            updates=updates,
        )

        write_result = self._write_defaults(content=new_body)
        if write_result.status == UbuntuKdumpActionStatus.FAILED:
            write_result.detail = action
            write_result.target = target_locator
            return write_result

        rtnval = self._dispatch(
            command=restart_command,
            detail=action,
            operation="configure_kdump_target",
            target=target_locator,
        )
        return rtnval

    def clear_old_dumps(
        self,
        age_days: int,
    ) -> UbuntuKdumpActionResult:
        """
            Remove crash-dump directories older than ``age_days``.

            Runs ``find <crash_dir> -maxdepth 1 -type d -mtime +<age>
            -exec rm -rf {} \\;``.  Never skips — the find command is
            itself idempotent.

            :param age_days: Minimum age in days.  Must be ``>= 0``.

            :returns: The :class:`UbuntuKdumpActionResult`.
        """
        action = "clear_old_dumps"
        if age_days < 0:
            failed = self._build_result(
                status=UbuntuKdumpActionStatus.FAILED,
                detail=action,
                command=None,
                target=self._config.crash_dir,
                message="age_days must be >= 0",
                stderr="invalid_age_days",
            )
            return failed

        command = [
            _FIND,
            self._config.crash_dir,
            "-maxdepth", "1",
            "-type", "d",
            "-mtime", "+{}".format(age_days),
            "-not", "-path", self._config.crash_dir,
            "-exec", "rm", "-rf", "{}", ";",
        ]
        rtnval = self._dispatch(
            command=command,
            detail=action,
            operation="clear_old_dumps",
            target=self._config.crash_dir,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_kdump_status(self) -> UbuntuKdumpStatus:
        """
            Snapshot of the current kdump-tools state.

            Combines:

                * ``USE_KDUMP`` from the defaults file,
                * the parsed ``KDUMP_*`` target keys,
                * the systemd unit's enabled / active state, and
                * ``kdump-config show`` output.

            Reads never elevate.

            :returns: The :class:`UbuntuKdumpStatus`.
        """
        defaults_text = self._read_defaults()
        parsed_defaults = _parse_defaults_file(content=defaults_text)
        use_kdump = parsed_defaults.get(_USE_KDUMP_KEY, "").strip() == "1"

        service_state = self._query_service_state()
        running = service_state["active"] == "active"
        enabled_in_systemd = service_state["enabled"] == "enabled"
        enabled = use_kdump and enabled_in_systemd

        target_kind = _deduce_target_kind(parsed=parsed_defaults)
        target_path = _deduce_target_locator(
            parsed=parsed_defaults,
            kind=target_kind,
        )

        kdump_show_command = [_KDUMP_CONFIG, "show"]
        show_run = self._runner.run(list(kdump_show_command), elevated=False)
        kernel: Optional[str] = None
        reserved_memory_mb: Optional[int] = None
        message: Optional[str] = None

        if show_run.exit_code == 0:
            show_parsed = _parse_kdump_config_show(text=show_run.stdout)
            for show_key in ("kdump kernel", "kdump initrd", "/var/lib/kdump/vmlinuz"):
                value = show_parsed.get(show_key)
                if value is not None and len(value) > 0:
                    kernel = value
                    break
        else:
            message = "kdump-config show returned non-zero exit"

        rtnval = UbuntuKdumpStatus(
            enabled=enabled,
            running=running,
            target_kind=target_kind,
            target_path=target_path,
            kernel=kernel,
            reserved_memory_mb=reserved_memory_mb,
            message=message,
        )
        return rtnval

    def list_recent_dumps(self) -> list[UbuntuKdumpInfo]:
        """
            Enumerate dump artifacts under ``crash_dir``.

            Runs ``ls -1t <crash_dir>`` and treats every entry that
            looks like a timestamped directory (digits, dot-separated
            tokens, or ``YYYYMMDD``-style prefixes) as a dump
            identifier.  Reads never elevate.

            :returns: A list of :class:`UbuntuKdumpInfo`, freshest
                      first.
            :raises UbuntuKdumpQueryError: When ``ls`` fails for a
                                           reason other than a missing
                                           directory.
        """
        command = [_LS, "-1t", self._config.crash_dir]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_lower = (run_result.stderr or "").lower()
            if "no such file" in stderr_lower:
                rtnval: list[UbuntuKdumpInfo] = []
                return rtnval
            raise UbuntuKdumpQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )

        rtnval = []
        for raw_line in run_result.stdout.splitlines():
            entry = raw_line.strip()
            if len(entry) == 0:
                continue
            if entry.startswith("."):
                continue

            full_path = "{}/{}".format(self._config.crash_dir, entry)
            info = UbuntuKdumpInfo(
                id=entry,
                path=full_path,
                created="",
                size_bytes=None,
                kernel=None,
                panic_message=None,
            )
            rtnval.append(info)

        return rtnval

    def get_dump_info(self, dump_id: str) -> UbuntuKdumpInfo:
        """
            Return parsed metadata for a single dump artifact.

            Reads ``<crash_dir>/<dump_id>/vmcore-dmesg.txt`` and
            ``<crash_dir>/<dump_id>/dmesg.<dump_id>`` (the latter is
            the kdump-tools fallback name) via ``cat`` and surfaces the
            first ``Linux version`` and ``Kernel panic`` lines parsed
            from either file.

            :param dump_id: The dump identifier as returned by
                            :meth:`list_recent_dumps`.

            :returns: The :class:`UbuntuKdumpInfo`.
            :raises UbuntuKdumpQueryError: When neither metadata file
                                           is readable.
        """
        full_path = "{}/{}".format(self._config.crash_dir, dump_id)

        primary_path = "{}/vmcore-dmesg.txt".format(full_path)
        primary_command = [_CAT, primary_path]
        primary_run = self._runner.run(list(primary_command), elevated=False)
        primary_text = primary_run.stdout if primary_run.exit_code == 0 else ""

        fallback_path = "{}/dmesg.{}".format(full_path, dump_id)
        fallback_command = [_CAT, fallback_path]
        fallback_run = self._runner.run(list(fallback_command), elevated=False)
        fallback_text = (
            fallback_run.stdout if fallback_run.exit_code == 0 else ""
        )

        if (primary_run.exit_code != 0) and (fallback_run.exit_code != 0):
            raise UbuntuKdumpQueryError(
                command=primary_command,
                exit_code=primary_run.exit_code,
                stderr=primary_run.stderr or fallback_run.stderr or "",
            )

        combined_text = primary_text + "\n" + fallback_text
        parsed = _parse_dmesg_metadata(text=combined_text)

        stat_command = [_STAT, "-c", "%y;%s", full_path]
        stat_run = self._runner.run(list(stat_command), elevated=False)
        if stat_run.exit_code == 0:
            raw = stat_run.stdout.strip()
            created_part, _, size_part = raw.partition(";")
            created = created_part.strip()
            size_text = size_part.strip()
            if len(size_text) > 0 and size_text.isdigit():
                size_bytes: Optional[int] = int(size_text)
            else:
                size_bytes = None
        else:
            created = ""
            size_bytes = None

        rtnval = UbuntuKdumpInfo(
            id=dump_id,
            path=full_path,
            created=created,
            size_bytes=size_bytes,
            kernel=parsed.get("kernel"),
            panic_message=parsed.get("panic_message"),
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_defaults(self) -> str:
        """
            Read the kdump-tools defaults file via ``cat``.  Reads
            never elevate.

            :returns: The file body, or an empty string when the file
                      is missing.
        """
        command = [_CAT, self._config.kdump_defaults]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = ""
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _write_defaults(self, *, content: str) -> UbuntuKdumpActionResult:
        """
            Persist ``content`` to the kdump-tools defaults file via
            ``tee``.  Honours ``config.elevated`` and ``dry_run``.

            :param content: The full file body to write.

            :returns: The :class:`UbuntuKdumpActionResult`.
        """
        command = [_TEE, self._config.kdump_defaults]
        rtnval = self._dispatch(
            command=command,
            detail="write_defaults",
            stdin=content,
            operation="write_defaults",
            target=self._config.kdump_defaults,
        )
        return rtnval

    def _query_service_state(self) -> dict:
        """
            Run ``systemctl is-active`` and ``systemctl is-enabled``
            against the configured service.  Reads never elevate.

            :returns: A dict with keys ``active`` and ``enabled``,
                      each carrying the raw stdout token (e.g.
                      ``"active"``, ``"inactive"``, ``"failed"``,
                      ``"enabled"``, ``"disabled"``, ``"masked"``,
                      or ``"unknown"`` on read failure).
        """
        active_command = [
            _SYSTEMCTL, "is-active", self._config.service_name,
        ]
        active_run = self._runner.run(list(active_command), elevated=False)
        active_text = active_run.stdout.strip()
        if len(active_text) == 0:
            active_text = active_run.stderr.strip()
        if len(active_text) == 0:
            active_text = "unknown"

        enabled_command = [
            _SYSTEMCTL, "is-enabled", self._config.service_name,
        ]
        enabled_run = self._runner.run(list(enabled_command), elevated=False)
        enabled_text = enabled_run.stdout.strip()
        if len(enabled_text) == 0:
            enabled_text = enabled_run.stderr.strip()
        if len(enabled_text) == 0:
            enabled_text = "unknown"

        rtnval = {
            "active": active_text,
            "enabled": enabled_text,
        }
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuKdumpActionStatus,
        detail: Optional[str],
        command: Optional[list[str]],
        target: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
        exit_code: Optional[int] = None,
    ) -> UbuntuKdumpActionResult:
        """
            Construct a :class:`UbuntuKdumpActionResult` without
            dispatching.

            :param status: The outcome status.
            :param detail: Short identifier of the action.
            :param command: Would-have-been argv (or executed argv).
            :param target: The dump target affected.
            :param message: Optional human-readable explanation.
            :param stderr: Optional synthetic stderr payload.
            :param exit_code: Optional exit code.

            :returns: The :class:`UbuntuKdumpActionResult`.
        """
        if command is None:
            command_copy: Optional[list[str]] = None
        else:
            command_copy = list(command)
        rtnval = UbuntuKdumpActionResult(
            status=status,
            detail=detail,
            command=command_copy,
            target=target,
            requires_reboot=False,
            message=message,
            stderr=stderr,
            exit_code=exit_code,
        )
        return rtnval
