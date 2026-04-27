"""
    Ubuntu filesystem-quotas feature.

    Implements the cross-OS filesystem-quotas contract for Ubuntu
    using the standard Linux quota family (``quotacheck``,
    ``quotaon``, ``quotaoff``, ``setquota``, ``quota``,
    ``repquota``) plus a ``findmnt`` mount probe.  Every command
    is dispatched through the injected :class:`ICommandRunner` so
    the manager works identically under local and SSH execution.

    Scope:
        This Ubuntu pilot targets ext-family filesystems
        (``ext2`` / ``ext3`` / ``ext4``).  XFS-specific tooling
        (``xfs_quota``) and project quotas remain in the canonical
        ``automojo-sysman-linux-redhat`` implementation.  Callers
        running against XFS / BTRFS / ZFS will surface the
        underlying tool's failure verbatim through the
        :class:`UbuntuQuotaActionResult` ``stderr`` / ``exit_code``
        fields.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``get_user_quota``, ``get_group_quota``, ``list_quotas``,
        ``get_quota_status``, the ``findmnt`` mount probe) NEVER
        elevate, regardless of the config.

    Mount validation:
        Every write verb calls ``_resolve_fstype(mountpoint)``
        before any quota command.  A path that ``findmnt`` does
        not recognise raises
        :class:`UbuntuQuotaFilesystemNotMountedError` before any
        runner call.

    Idempotency:
        * ``enable_quotas``: SKIPPED when ``quotaon -p`` already
          reports both user and group quotas on.
        * ``disable_quotas``: SKIPPED when both already off.
        * ``set_user_quota`` / ``set_group_quota``: SKIPPED when
          ``quota -u/-g`` reports the requested soft / hard
          limits.

    Inode-quota policy:
        ``setquota`` is invoked with ``0 0`` for the inode soft /
        hard arguments (no inode enforcement).  Inode quotas are
        SURFACED on reads via :class:`UbuntuQuotaInfo` for
        reporting but are not a top-level write verb in this
        pilot.

    Unit conversions:
        The cross-OS contract speaks bytes.  Linux ``quota`` /
        ``repquota`` / ``setquota`` speak 1-KiB blocks.  The
        manager multiplies by 1024 on every read and divides by
        1024 on every write so callers operate in bytes
        end-to-end.
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


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_QUOTACHECK = "quotacheck"
_QUOTAON = "quotaon"
_QUOTAOFF = "quotaoff"
_SETQUOTA = "setquota"
_QUOTA = "quota"
_REPQUOTA = "repquota"
_FINDMNT = "findmnt"

_BLOCK_BYTES = 1024


UbuntuQuotaActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------


class UbuntuQuotaPrincipalKind(Enum):
    """
        Identifies whether a quota record applies to a user
        principal or a group principal.

        +-----------+------------------------+
        | Value     | Meaning                |
        +===========+========================+
        | ``USER``  | Per-user quotas (-u)   |
        +-----------+------------------------+
        | ``GROUP`` | Per-group quotas (-g)  |
        +-----------+------------------------+
    """
    USER = "user"
    GROUP = "group"


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class UbuntuFilesystemQuotasManagerConfig(FeatureConfig):
    """
        Configuration for
        :class:`UbuntuFilesystemQuotasManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param quotacheck_format: Format string passed via
                                  ``quotacheck -F``.  Defaults to
                                  ``"vfsv0"``.  ``vfsv1`` exists
                                  for 64-bit-block deployments and
                                  may be passed by callers needing
                                  it.
    """
    quotacheck_format: str = "vfsv0"


@dataclass
class UbuntuQuotaActionResult(FeatureActionResult):
    """
        Result of a filesystem-quotas write operation.  Adds
        quota-scope fields on top of :class:`FeatureActionResult`.

        :param mountpoint: The mountpoint acted upon, when
                           applicable.
        :param principal: The user or group acted upon, when
                          applicable.
        :param message: Optional human-readable detail for
                        ``SKIPPED`` or ``FAILED`` outcomes.
    """
    mountpoint: Optional[str] = None
    principal: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuQuotaInfo:
    """
        Per-principal quota record on a single mountpoint.

        :param mountpoint: Filesystem mountpoint the record applies
                           to.
        :param principal: Username or group name.
        :param principal_kind: USER or GROUP.
        :param used_bytes: Current block usage in bytes.  ``None``
                           when the underlying tool did not report
                           a value.
        :param soft_bytes: Soft block limit in bytes; ``None`` when
                           absent.
        :param hard_bytes: Hard block limit in bytes; ``None`` when
                           absent.
        :param used_inodes: Current inode usage; ``None`` when not
                            reported.
        :param soft_inodes: Soft inode limit; ``None`` when not
                            reported or unset (``0`` is preserved
                            verbatim because the tool's "no limit"
                            convention is part of the contract).
        :param hard_inodes: Hard inode limit; same semantics.
        :param grace_period_seconds: Duration of the block grace
                                     period in seconds, when
                                     reported by the underlying
                                     tool, else ``None``.
    """
    mountpoint: str
    principal: str
    principal_kind: UbuntuQuotaPrincipalKind
    used_bytes: Optional[int]
    soft_bytes: Optional[int]
    hard_bytes: Optional[int]
    used_inodes: Optional[int]
    soft_inodes: Optional[int]
    hard_inodes: Optional[int]
    grace_period_seconds: Optional[int]


@dataclass
class UbuntuQuotaStatus:
    """
        Mountpoint-wide quota enforcement posture.

        :param mountpoint: Filesystem mountpoint.
        :param enabled: True when at least one of user / group
                        quotas is active.
        :param enforcing: True when at least one of user / group
                          quotas is enforced.  On Ubuntu the two
                          flags collapse to the same value because
                          ``quotaon`` controls both accounting and
                          enforcement together; the field exists
                          for cross-OS parity.
        :param user_quotas_active: True when ``quotaon -p`` reports
                                   user quotas as on.
        :param group_quotas_active: True when ``quotaon -p`` reports
                                    group quotas as on.
        :param filesystem_type: Detected filesystem type
                                (``"ext4"``, ``"ext3"``, ...) or
                                ``None`` when ``findmnt`` was
                                unavailable.
    """
    mountpoint: str
    enabled: bool
    enforcing: bool
    user_quotas_active: bool
    group_quotas_active: bool
    filesystem_type: Optional[str]


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class UbuntuQuotaQueryError(Exception):
    """
        Raised when a quota read fails for a reason other than
        the principal simply being absent.
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

        err_msg_lines = ["Quota query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuQuotaFilesystemNotMountedError(Exception):
    """
        Raised when a write verb is invoked against a mountpoint
        that is not currently mounted (per
        ``findmnt -n -o FSTYPE``).
    """

    def __init__(
        self,
        *,
        mountpoint: str,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param mountpoint: The mountpoint that failed
                               validation.
            :param command: The ``findmnt`` argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.mountpoint = mountpoint
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = [
            "Filesystem-quotas write requires a mounted target.",
        ]
        err_msg_lines.append("    MOUNTPOINT: {}".format(mountpoint))
        err_msg_lines.append("    PROBE: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class UbuntuFilesystemQuotasManager(BaseFeatureManager):
    """
        Ubuntu filesystem-quotas manager.  Wraps the standard
        Linux quota family through the injected
        :class:`ICommandRunner`.
    """

    result_class = UbuntuQuotaActionResult
    status_class = UbuntuQuotaActionStatus
    feature_name = "filesystem-quotas"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def enable_quotas(
        self,
        mountpoint: str,
    ) -> UbuntuQuotaActionResult:
        """
            Enable quota enforcement on ``mountpoint``.

            Runs ``quotacheck -ug -mF <format> <mountpoint>`` to
            (re)build the on-disk accounting database, then
            ``quotaon -ug <mountpoint>`` to begin enforcement.
            Returns ``SKIPPED`` when both user and group quotas
            are already active.

            :param mountpoint: The filesystem mountpoint.

            :returns: The :class:`UbuntuQuotaActionResult`.

            :raises UbuntuQuotaFilesystemNotMountedError: when the
                mountpoint is not currently mounted.
        """
        planned_command = self._planned_command_for_enable(
            mountpoint=mountpoint,
        )

        if self._config.dry_run:
            dry_result = UbuntuQuotaActionResult(
                status=UbuntuQuotaActionStatus.DRY_RUN,
                detail=mountpoint,
                command=list(planned_command),
                mountpoint=mountpoint,
            )
            return dry_result

        self._resolve_fstype(mountpoint=mountpoint)

        current_status = self.get_quota_status(mountpoint=mountpoint)
        if current_status.user_quotas_active and current_status.group_quotas_active:
            skipped = UbuntuQuotaActionResult(
                status=UbuntuQuotaActionStatus.SKIPPED,
                detail=mountpoint,
                command=list(planned_command),
                mountpoint=mountpoint,
                message="quotas are already active for both user and group",
            )
            return skipped

        check_result = self._dispatch(
            command=planned_command,
            detail=mountpoint,
            mountpoint=mountpoint,
            principal=None,
        )

        if check_result.status != UbuntuQuotaActionStatus.SUCCESS:
            return check_result

        on_command = [_QUOTAON, "-ug", mountpoint]
        on_result = self._dispatch(
            command=on_command,
            detail=mountpoint,
            mountpoint=mountpoint,
            principal=None,
        )
        return on_result

    def disable_quotas(
        self,
        mountpoint: str,
    ) -> UbuntuQuotaActionResult:
        """
            Disable quota enforcement on ``mountpoint`` via
            ``quotaoff -ug <mountpoint>``.  Returns ``SKIPPED``
            when both user and group quotas are already off.

            :param mountpoint: The filesystem mountpoint.

            :returns: The :class:`UbuntuQuotaActionResult`.

            :raises UbuntuQuotaFilesystemNotMountedError: when the
                mountpoint is not currently mounted.
        """
        command = [_QUOTAOFF, "-ug", mountpoint]

        if self._config.dry_run:
            dry_result = UbuntuQuotaActionResult(
                status=UbuntuQuotaActionStatus.DRY_RUN,
                detail=mountpoint,
                command=list(command),
                mountpoint=mountpoint,
            )
            return dry_result

        self._resolve_fstype(mountpoint=mountpoint)

        current_status = self.get_quota_status(mountpoint=mountpoint)
        already_off = (
            not current_status.user_quotas_active
            and not current_status.group_quotas_active
        )
        if already_off:
            skipped = UbuntuQuotaActionResult(
                status=UbuntuQuotaActionStatus.SKIPPED,
                detail=mountpoint,
                command=list(command),
                mountpoint=mountpoint,
                message="quotas are already off for both user and group",
            )
            return skipped

        rtnval = self._dispatch(
            command=command,
            detail=mountpoint,
            mountpoint=mountpoint,
            principal=None,
        )
        return rtnval

    def set_user_quota(
        self,
        mountpoint: str,
        user: str,
        soft_bytes: int,
        hard_bytes: int,
    ) -> UbuntuQuotaActionResult:
        """
            Set the soft and hard block quota for ``user`` on
            ``mountpoint``.

            ``setquota`` is invoked with ``0 0`` for the inode
            arguments (no inode enforcement).  Returns ``SKIPPED``
            when the user's current record already matches the
            requested values.

            :param mountpoint: The filesystem mountpoint.
            :param user: The username.
            :param soft_bytes: Soft block limit in bytes.
            :param hard_bytes: Hard block limit in bytes.

            :returns: The :class:`UbuntuQuotaActionResult`.

            :raises UbuntuQuotaFilesystemNotMountedError: when the
                mountpoint is not currently mounted.
        """
        rtnval = self._set_quota(
            mountpoint=mountpoint,
            principal=user,
            principal_kind=UbuntuQuotaPrincipalKind.USER,
            soft_bytes=soft_bytes,
            hard_bytes=hard_bytes,
        )
        return rtnval

    def set_group_quota(
        self,
        mountpoint: str,
        group: str,
        soft_bytes: int,
        hard_bytes: int,
    ) -> UbuntuQuotaActionResult:
        """
            Set the soft and hard block quota for ``group`` on
            ``mountpoint``.

            :param mountpoint: The filesystem mountpoint.
            :param group: The group name.
            :param soft_bytes: Soft block limit in bytes.
            :param hard_bytes: Hard block limit in bytes.

            :returns: The :class:`UbuntuQuotaActionResult`.

            :raises UbuntuQuotaFilesystemNotMountedError: when the
                mountpoint is not currently mounted.
        """
        rtnval = self._set_quota(
            mountpoint=mountpoint,
            principal=group,
            principal_kind=UbuntuQuotaPrincipalKind.GROUP,
            soft_bytes=soft_bytes,
            hard_bytes=hard_bytes,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_user_quota(
        self,
        mountpoint: str,
        user: str,
    ) -> Optional[UbuntuQuotaInfo]:
        """
            Return :class:`UbuntuQuotaInfo` for ``user`` on
            ``mountpoint`` or ``None`` when the principal has no
            record on this mount.

            :param mountpoint: The filesystem mountpoint.
            :param user: The username.

            :returns: The parsed info or ``None``.

            :raises UbuntuQuotaQueryError: when ``quota`` exits
                with an unexpected status.
        """
        rtnval = self._query_principal_quota(
            mountpoint=mountpoint,
            principal=user,
            principal_kind=UbuntuQuotaPrincipalKind.USER,
        )
        return rtnval

    def get_group_quota(
        self,
        mountpoint: str,
        group: str,
    ) -> Optional[UbuntuQuotaInfo]:
        """
            Return :class:`UbuntuQuotaInfo` for ``group`` on
            ``mountpoint`` or ``None`` when the principal has no
            record on this mount.

            :param mountpoint: The filesystem mountpoint.
            :param group: The group name.

            :returns: The parsed info or ``None``.

            :raises UbuntuQuotaQueryError: when ``quota`` exits
                with an unexpected status.
        """
        rtnval = self._query_principal_quota(
            mountpoint=mountpoint,
            principal=group,
            principal_kind=UbuntuQuotaPrincipalKind.GROUP,
        )
        return rtnval

    def list_quotas(
        self,
        mountpoint: str,
    ) -> list[UbuntuQuotaInfo]:
        """
            Enumerate every user and group quota on ``mountpoint``
            via ``repquota -ug <mountpoint>``.

            :param mountpoint: The filesystem mountpoint.

            :returns: One :class:`UbuntuQuotaInfo` per principal.

            :raises UbuntuQuotaQueryError: when ``repquota`` exits
                non-zero.
        """
        command = [_REPQUOTA, "-ug", mountpoint]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise UbuntuQuotaQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_repquota_output(
            stdout=run_result.stdout,
            mountpoint=mountpoint,
        )
        return rtnval

    def get_quota_status(
        self,
        mountpoint: str,
    ) -> UbuntuQuotaStatus:
        """
            Query mountpoint-wide quota enforcement state via
            ``quotaon -p <mountpoint>`` and best-effort
            ``findmnt`` for ``filesystem_type``.

            :param mountpoint: The filesystem mountpoint.

            :returns: The :class:`UbuntuQuotaStatus`.

            :raises UbuntuQuotaQueryError: when ``quotaon -p``
                exits non-zero.
        """
        command = [_QUOTAON, "-p", mountpoint]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise UbuntuQuotaQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        user_active, group_active = _parse_quotaon_status(
            stdout=run_result.stdout,
        )

        fstype_value = self._best_effort_fstype(mountpoint=mountpoint)

        either_active = user_active or group_active
        rtnval = UbuntuQuotaStatus(
            mountpoint=mountpoint,
            enabled=either_active,
            enforcing=either_active,
            user_quotas_active=user_active,
            group_quotas_active=group_active,
            filesystem_type=fstype_value,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _planned_command_for_enable(
        self,
        *,
        mountpoint: str,
    ) -> list[str]:
        """
            Build the ``quotacheck`` argv used as the dry-run
            record for :meth:`enable_quotas`.

            :param mountpoint: The filesystem mountpoint.
        """
        config = self._config
        if isinstance(config, UbuntuFilesystemQuotasManagerConfig):
            format_value = config.quotacheck_format
        else:
            format_value = "vfsv0"

        rtnval = [
            _QUOTACHECK,
            "-ug",
            "-mF",
            format_value,
            mountpoint,
        ]
        return rtnval

    def _resolve_fstype(
        self,
        *,
        mountpoint: str,
    ) -> str:
        """
            Probe ``findmnt -n -o FSTYPE <mountpoint>`` and return
            the fstype.  Raises
            :class:`UbuntuQuotaFilesystemNotMountedError` on
            non-zero exit.

            :param mountpoint: The filesystem mountpoint.
        """
        command = [_FINDMNT, "-n", "-o", "FSTYPE", mountpoint]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise UbuntuQuotaFilesystemNotMountedError(
                mountpoint=mountpoint,
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = run_result.stdout.strip()
        return rtnval

    def _best_effort_fstype(
        self,
        *,
        mountpoint: str,
    ) -> Optional[str]:
        """
            Return the fstype for ``mountpoint`` or ``None`` when
            ``findmnt`` is unavailable / non-zero.  Used by
            :meth:`get_quota_status` so the read path never raises
            because of a missing util-linux package.

            :param mountpoint: The filesystem mountpoint.
        """
        command = [_FINDMNT, "-n", "-o", "FSTYPE", mountpoint]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval

        body = run_result.stdout.strip()
        if len(body) == 0:
            rtnval = None
            return rtnval

        rtnval = body
        return rtnval

    def _set_quota(
        self,
        *,
        mountpoint: str,
        principal: str,
        principal_kind: UbuntuQuotaPrincipalKind,
        soft_bytes: int,
        hard_bytes: int,
    ) -> UbuntuQuotaActionResult:
        """
            Shared implementation of :meth:`set_user_quota` and
            :meth:`set_group_quota`.

            :param mountpoint: Filesystem mountpoint.
            :param principal: Username or group name.
            :param principal_kind: Which kind.
            :param soft_bytes: Soft block limit in bytes.
            :param hard_bytes: Hard block limit in bytes.
        """
        soft_kb = soft_bytes // _BLOCK_BYTES
        hard_kb = hard_bytes // _BLOCK_BYTES

        if principal_kind is UbuntuQuotaPrincipalKind.USER:
            kind_flag = "-u"
        else:
            kind_flag = "-g"

        command = [
            _SETQUOTA,
            kind_flag,
            principal,
            str(soft_kb),
            str(hard_kb),
            "0",
            "0",
            mountpoint,
        ]

        if self._config.dry_run:
            dry_result = UbuntuQuotaActionResult(
                status=UbuntuQuotaActionStatus.DRY_RUN,
                detail=principal,
                command=list(command),
                mountpoint=mountpoint,
                principal=principal,
            )
            return dry_result

        self._resolve_fstype(mountpoint=mountpoint)

        current = self._query_principal_quota(
            mountpoint=mountpoint,
            principal=principal,
            principal_kind=principal_kind,
        )
        if current is not None:
            soft_matches = current.soft_bytes == soft_bytes
            hard_matches = current.hard_bytes == hard_bytes
            if soft_matches and hard_matches:
                skipped = UbuntuQuotaActionResult(
                    status=UbuntuQuotaActionStatus.SKIPPED,
                    detail=principal,
                    command=list(command),
                    mountpoint=mountpoint,
                    principal=principal,
                    message="current quota already matches the requested limits",
                )
                return skipped

        rtnval = self._dispatch(
            command=command,
            detail=principal,
            mountpoint=mountpoint,
            principal=principal,
        )
        return rtnval

    def _query_principal_quota(
        self,
        *,
        mountpoint: str,
        principal: str,
        principal_kind: UbuntuQuotaPrincipalKind,
    ) -> Optional[UbuntuQuotaInfo]:
        """
            Shared implementation of :meth:`get_user_quota` and
            :meth:`get_group_quota`.

            :param mountpoint: Filesystem mountpoint.
            :param principal: Username or group name.
            :param principal_kind: Which kind.
        """
        if principal_kind is UbuntuQuotaPrincipalKind.USER:
            kind_flag = "-u"
        else:
            kind_flag = "-g"

        command = [_QUOTA, kind_flag, principal]
        run_result = self._runner.run(list(command), elevated=False)

        # ``quota`` returns 0 when the principal has at least one
        # row, 1 when no quotas are defined on the host at all,
        # and 2 when the principal has no quotas.  All three are
        # "no record" outcomes that we surface as None when the
        # parser cannot find a matching mountpoint.  Anything
        # else is a query failure.
        if run_result.exit_code not in (0, 1, 2):
            raise UbuntuQuotaQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_quota_output(
            stdout=run_result.stdout,
            mountpoint=mountpoint,
            principal=principal,
            principal_kind=principal_kind,
        )
        return rtnval


# ----------------------------------------------------------------------
# Parser helpers (module-scoped, pure functions)
# ----------------------------------------------------------------------


def _parse_quota_output(
    *,
    stdout: str,
    mountpoint: str,
    principal: str,
    principal_kind: UbuntuQuotaPrincipalKind,
) -> Optional[UbuntuQuotaInfo]:
    """
        Parse ``quota -u <user>`` (or ``-g <group>``) output and
        return the row matching ``mountpoint``, or ``None`` when
        no row matches.

        ``quota`` output looks like::

            Disk quotas for user alice (uid 1001):
                 Filesystem  blocks   quota   limit   grace
                                 files   quota   limit   grace
                   /dev/sda1  102400  204800  409600
                                  1234       0       0

        The ``Filesystem`` column is typically the device, not the
        mountpoint.  We accept either: a row whose first token
        ends with ``mountpoint`` is also a match (used by callers
        running ``quota`` with the ``--show-mntpoint`` option).

        :param stdout: Raw stdout from ``quota``.
        :param mountpoint: The mountpoint we want.
        :param principal: The principal name (echoed onto the
                          returned info).
        :param principal_kind: USER or GROUP.

        :returns: A :class:`UbuntuQuotaInfo` or ``None``.
    """
    rows: list[list[str]] = []
    for raw_line in stdout.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("Disk quotas"):
            continue
        if stripped.startswith("Filesystem"):
            continue
        if stripped.startswith("none"):
            continue
        if stripped.startswith("blocks"):
            continue

        tokens = stripped.split()
        rows.append(tokens)

    if len(rows) == 0:
        rtnval: Optional[UbuntuQuotaInfo] = None
        return rtnval

    # Find a row whose first token resolves to the requested
    # mountpoint.  ``quota`` typically prints ``/dev/sdaN`` here;
    # ``quota --show-mntpoint`` prints the mountpoint.  Either is
    # accepted.
    matched: Optional[list[str]] = None
    for tokens in rows:
        first = tokens[0]
        if first == mountpoint:
            matched = tokens
            break
        if first.endswith(mountpoint):
            matched = tokens
            break
        # Fallback: when only one row is present, take it.
        if len(rows) == 1:
            matched = tokens
            break

    if matched is None:
        rtnval = None
        return rtnval

    rtnval = _build_quota_info_from_quota_row(
        row=matched,
        mountpoint=mountpoint,
        principal=principal,
        principal_kind=principal_kind,
    )
    return rtnval


def _build_quota_info_from_quota_row(
    *,
    row: list[str],
    mountpoint: str,
    principal: str,
    principal_kind: UbuntuQuotaPrincipalKind,
) -> UbuntuQuotaInfo:
    """
        Map a ``quota`` row token list into a
        :class:`UbuntuQuotaInfo`.  Token order is:

        ``[<filesystem>, <blocks_used>, <blocks_soft>,
           <blocks_hard>, <grace_blocks?>,
           <files_used>, <files_soft>, <files_hard>,
           <grace_files?>]``

        Block values are 1-KiB; the parser converts to bytes.
        ``*`` suffixes on the used columns indicate a soft-limit
        breach; they are stripped.

        :param row: The token list (filesystem column first).
        :param mountpoint: Mountpoint to record on the info.
        :param principal: Principal name.
        :param principal_kind: USER or GROUP.
    """
    used_blocks = _strip_warning_marker(token=row[1] if len(row) > 1 else "")
    soft_blocks = _strip_warning_marker(token=row[2] if len(row) > 2 else "")
    hard_blocks = _strip_warning_marker(token=row[3] if len(row) > 3 else "")

    used_bytes = _kb_token_to_bytes(token=used_blocks)
    soft_bytes = _kb_token_to_bytes(token=soft_blocks)
    hard_bytes = _kb_token_to_bytes(token=hard_blocks)

    # Inode columns may or may not be present; ``quota`` wraps the
    # inode columns onto a second line in some versions.  We
    # accept either contiguous-row or only-block-row layouts.
    used_inodes = None
    soft_inodes = None
    hard_inodes = None
    grace_period_seconds = None

    # When a row carries 7 or 8 tokens, columns 5 / 6 / 7 are the
    # inode used / soft / hard.  Column 4 may be the block grace
    # ("none" / "7days").
    if len(row) >= 5:
        candidate_grace = row[4]
        grace_period_seconds = _grace_token_to_seconds(token=candidate_grace)

    if len(row) >= 8:
        used_inodes = _int_token_or_none(token=row[5])
        soft_inodes = _int_token_or_none(token=row[6])
        hard_inodes = _int_token_or_none(token=row[7])

    rtnval = UbuntuQuotaInfo(
        mountpoint=mountpoint,
        principal=principal,
        principal_kind=principal_kind,
        used_bytes=used_bytes,
        soft_bytes=soft_bytes,
        hard_bytes=hard_bytes,
        used_inodes=used_inodes,
        soft_inodes=soft_inodes,
        hard_inodes=hard_inodes,
        grace_period_seconds=grace_period_seconds,
    )
    return rtnval


def _parse_repquota_output(
    *,
    stdout: str,
    mountpoint: str,
) -> list[UbuntuQuotaInfo]:
    """
        Parse ``repquota -ug <mountpoint>`` two-section output
        into a flat ``list[UbuntuQuotaInfo]``.

        :param stdout: Raw repquota stdout.
        :param mountpoint: Mountpoint to record on every info.
    """
    rtnval: list[UbuntuQuotaInfo] = []

    current_kind: Optional[UbuntuQuotaPrincipalKind] = None

    for raw_line in stdout.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue

        if stripped.startswith("***"):
            lower = stripped.lower()
            if "user quotas" in lower:
                current_kind = UbuntuQuotaPrincipalKind.USER
            elif "group quotas" in lower:
                current_kind = UbuntuQuotaPrincipalKind.GROUP
            else:
                current_kind = None
            continue

        # Skip the column-name banner lines and dashed separator.
        lowered = stripped.lower()
        if lowered.startswith("block grace") or lowered.startswith("inode grace"):
            continue
        if lowered.startswith("user") and "used" in lowered:
            continue
        if lowered.startswith("group") and "used" in lowered:
            continue
        if stripped.startswith("---"):
            continue
        if "Block limits" in stripped or "File limits" in stripped:
            continue

        tokens = stripped.split()
        # A repquota data row is at least: principal, flags,
        # blocks_used, blocks_soft, blocks_hard, [grace], files_used,
        # files_soft, files_hard, [grace].
        if len(tokens) < 5:
            continue

        if current_kind is None:
            continue

        info = _build_quota_info_from_repquota_row(
            row=tokens,
            mountpoint=mountpoint,
            principal_kind=current_kind,
        )
        if info is not None:
            rtnval.append(info)

    return rtnval


def _build_quota_info_from_repquota_row(
    *,
    row: list[str],
    mountpoint: str,
    principal_kind: UbuntuQuotaPrincipalKind,
) -> Optional[UbuntuQuotaInfo]:
    """
        Map a single repquota data row into a
        :class:`UbuntuQuotaInfo`.

        Repquota row shape::

            <principal> <flags> <bused> <bsoft> <bhard> [<bgrace>]
                                <iused> <isoft> <ihard> [<igrace>]

        Where ``<flags>`` is a 2-char marker (``--``, ``+-``, etc.).

        :param row: The token list.
        :param mountpoint: Mountpoint to record.
        :param principal_kind: USER or GROUP.
    """
    principal_name = row[0]

    # The flags column is optional in some repquota variants; we
    # detect it heuristically by checking whether row[1] is the
    # standard 2-char marker.
    if len(row) >= 6 and _is_repquota_flag_token(token=row[1]):
        offset = 2
    else:
        offset = 1

    if len(row) < offset + 3:
        rtnval: Optional[UbuntuQuotaInfo] = None
        return rtnval

    bused_token = row[offset]
    bsoft_token = row[offset + 1]
    bhard_token = row[offset + 2]

    used_bytes = _kb_token_to_bytes(token=bused_token)
    soft_bytes = _kb_token_to_bytes(token=bsoft_token)
    hard_bytes = _kb_token_to_bytes(token=bhard_token)

    used_inodes = None
    soft_inodes = None
    hard_inodes = None
    grace_period_seconds = None

    # Walk the remaining tokens; the next three integer tokens are
    # the inode used / soft / hard, and any non-integer token is
    # treated as a grace value.
    remaining = row[offset + 3:]
    int_tokens: list[str] = []
    grace_tokens: list[str] = []
    for token in remaining:
        if _is_integer_token(token=token):
            int_tokens.append(token)
        else:
            grace_tokens.append(token)

    if len(int_tokens) >= 3:
        used_inodes = _int_token_or_none(token=int_tokens[0])
        soft_inodes = _int_token_or_none(token=int_tokens[1])
        hard_inodes = _int_token_or_none(token=int_tokens[2])

    if len(grace_tokens) > 0:
        grace_period_seconds = _grace_token_to_seconds(
            token=grace_tokens[0],
        )

    rtnval = UbuntuQuotaInfo(
        mountpoint=mountpoint,
        principal=principal_name,
        principal_kind=principal_kind,
        used_bytes=used_bytes,
        soft_bytes=soft_bytes,
        hard_bytes=hard_bytes,
        used_inodes=used_inodes,
        soft_inodes=soft_inodes,
        hard_inodes=hard_inodes,
        grace_period_seconds=grace_period_seconds,
    )
    return rtnval


def _parse_quotaon_status(
    *,
    stdout: str,
) -> tuple[bool, bool]:
    """
        Parse ``quotaon -p <mountpoint>`` output and return
        ``(user_active, group_active)``.

        Output rows look like::

            user quota on /mnt/data (/dev/sda1) is on
            group quota on /mnt/data (/dev/sda1) is off

        :param stdout: Raw quotaon stdout.
    """
    user_active = False
    group_active = False

    for raw_line in stdout.splitlines():
        stripped = raw_line.strip().lower()
        if len(stripped) == 0:
            continue

        last_token = stripped.split()[-1]
        is_on = last_token == "on"

        if stripped.startswith("user quota"):
            user_active = is_on
            continue
        if stripped.startswith("group quota"):
            group_active = is_on
            continue

    rtnval = (user_active, group_active)
    return rtnval


def _strip_warning_marker(*, token: str) -> str:
    """
        Strip the trailing ``*`` that ``quota`` appends to the
        used-blocks column when the soft limit is currently
        breached.

        :param token: A raw token from ``quota`` output.
    """
    if token.endswith("*"):
        rtnval = token[:-1]
        return rtnval
    rtnval = token
    return rtnval


def _kb_token_to_bytes(*, token: str) -> Optional[int]:
    """
        Convert a ``quota`` 1-KiB-block token (e.g. ``"102400"``)
        into bytes.  Returns ``None`` for non-numeric tokens.

        :param token: The token to convert.
    """
    if len(token) == 0:
        rtnval: Optional[int] = None
        return rtnval

    try:
        block_value = int(token)
    except ValueError:
        rtnval = None
        return rtnval

    rtnval = block_value * _BLOCK_BYTES
    return rtnval


def _int_token_or_none(*, token: str) -> Optional[int]:
    """
        Parse ``token`` as an integer; return ``None`` for
        non-numeric tokens.

        :param token: The token to convert.
    """
    if len(token) == 0:
        rtnval: Optional[int] = None
        return rtnval

    try:
        rtnval = int(token)
    except ValueError:
        rtnval = None
    return rtnval


def _grace_token_to_seconds(*, token: str) -> Optional[int]:
    """
        Convert a ``quota`` / ``repquota`` grace token (``"none"``
        / ``"7days"`` / ``"6:23"``) into seconds.  Returns
        ``None`` for unparseable tokens.

        :param token: The token to convert.
    """
    if len(token) == 0:
        rtnval: Optional[int] = None
        return rtnval

    lowered = token.lower()
    if lowered == "none":
        rtnval = None
        return rtnval

    # ``7days`` / ``3day`` / ``2hours`` / ``45minutes``
    suffix_map = {
        "days": 86400,
        "day": 86400,
        "hours": 3600,
        "hour": 3600,
        "minutes": 60,
        "minute": 60,
        "seconds": 1,
        "second": 1,
    }
    for suffix, multiplier in suffix_map.items():
        if lowered.endswith(suffix):
            number_part = lowered[: -len(suffix)]
            try:
                value = int(number_part)
            except ValueError:
                continue
            rtnval = value * multiplier
            return rtnval

    # ``6:23`` (hours:minutes) form
    if ":" in lowered:
        hours_token, minutes_token = lowered.split(":", 1)
        try:
            hours_value = int(hours_token)
            minutes_value = int(minutes_token)
        except ValueError:
            rtnval = None
            return rtnval
        rtnval = hours_value * 3600 + minutes_value * 60
        return rtnval

    rtnval = None
    return rtnval


def _is_repquota_flag_token(*, token: str) -> bool:
    """
        Return True when ``token`` looks like a 2-char repquota
        over-grace flag (``"--"``, ``"+-"``, ``"-+"``, ``"++"``).

        :param token: The token to check.
    """
    if len(token) != 2:
        rtnval = False
        return rtnval

    rtnval = True
    for ch in token:
        if ch not in "+-":
            rtnval = False
            break
    return rtnval


def _is_integer_token(*, token: str) -> bool:
    """
        Return True when ``token`` parses as a plain integer.

        :param token: The token to check.
    """
    if len(token) == 0:
        rtnval = False
        return rtnval

    if token[0] in "+-":
        body = token[1:]
    else:
        body = token

    if len(body) == 0:
        rtnval = False
        return rtnval

    rtnval = True
    for ch in body:
        if not ch.isdigit():
            rtnval = False
            break
    return rtnval
