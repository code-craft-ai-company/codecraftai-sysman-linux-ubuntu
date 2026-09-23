"""
    Ubuntu storage-management feature.

    Implements the cross-OS storage-management contract for Ubuntu using
    the LVM2 user-space toolchain (``lsblk``, ``lvs``, ``vgs``, ``pvs``,
    ``pvcreate``, ``vgcreate``, ``vgextend``, ``vgreduce``, ``vgremove``,
    ``lvcreate``, ``lvremove``, ``lvresize``, ``blkid``) through the
    injected :class:`ICommandRunner`.

    Concept mapping:
        The cross-OS contract talks about *pools* and *volumes*.  On
        Ubuntu these map to LVM concepts:

        * **Pool** — LVM Volume Group (``vgcreate`` / ``vgremove``).
        * **Volume** — LVM Logical Volume
          (``lvcreate`` / ``lvremove`` / ``lvresize``).
        * **Disk** — a block device of type ``disk`` reported by
          ``lsblk``.  Adding a disk to a pool first initialises a
          Physical Volume on it via ``pvcreate``, then extends the VG
          with ``vgextend``.

        See ``specs/3-storage-management/design.md`` for the full
        concept-mapping table.

    Volume identifier syntax:
        Volumes are identified by ``<vg>/<lv>`` (matching the LVM
        device-mapper naming convention; the on-disk node lives at
        ``/dev/<vg>/<lv>`` or ``/dev/mapper/<vg>-<lv>``).  The
        :meth:`UbuntuStorageManager.delete_volume` /
        :meth:`UbuntuStorageManager.resize_volume` /
        :meth:`UbuntuStorageManager.get_volume_info` verbs all accept
        this combined form.

    Elevation policy:
        Every write verb (``create_volume``, ``delete_volume``,
        ``resize_volume``, ``create_pool``, ``delete_pool``,
        ``add_to_pool``, ``remove_from_pool``) honours
        ``config.elevated``.  Reads (``list_disks``, ``list_volumes``,
        ``get_volume_info``, ``get_disk_info``, the idempotency probes)
        never elevate.

    Filesystem creation and mounting are NOT part of this feature; they
    live in ``filesystem-management``.  ``create_volume`` provisions an
    LV but does not run ``mkfs``; ``resize_volume`` calls
    ``lvresize --resizefs`` to grow / shrink the contained filesystem
    when one is present, but does not create one.
"""

import json
import os

from dataclasses import dataclass, field
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level command tokens
# ----------------------------------------------------------------------


_LSBLK = "lsblk"
_LVS = "lvs"
_VGS = "vgs"
_PVS = "pvs"
_LVCREATE = "lvcreate"
_LVREMOVE = "lvremove"
_LVRESIZE = "lvresize"
_VGCREATE = "vgcreate"
_VGEXTEND = "vgextend"
_VGREDUCE = "vgreduce"
_VGREMOVE = "vgremove"
_PVCREATE = "pvcreate"
_PVREMOVE = "pvremove"


# Tolerance, in bytes, used by :meth:`UbuntuStorageManager.resize_volume`
# when comparing the requested size against the current size.  LVM
# rounds up to the physical-extent boundary, so a request that already
# matches within one PE (4 MiB by default) is treated as already
# satisfied.  We pick 1 MiB as a more conservative bound — anything
# coarser would risk surprising callers with a SKIPPED outcome on a
# request that LVM would actually grow.
_RESIZE_TOLERANCE_BYTES = 1 * 1024 * 1024


# ----------------------------------------------------------------------
# Aliases / config / result types
# ----------------------------------------------------------------------


UbuntuStorageActionStatus = ActionStatus


@dataclass
class UbuntuStorageManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuStorageManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param default_filesystem: Filesystem hint recorded onto
                                   :class:`UbuntuVolumeSpec` when the
                                   caller does not override.  This
                                   feature does NOT call ``mkfs``;
                                   ``filesystem-management`` does.  The
                                   field exists so cross-OS callers can
                                   round-trip a default.
        :param lvs_format_args: Argv suffix appended to every ``lvs`` /
                                ``vgs`` / ``pvs`` invocation so the
                                output is JSON in bytes.
    """
    default_filesystem: str = "ext4"
    lvs_format_args: list = field(
        default_factory=lambda: ["--reportformat", "json", "--units", "b"],
    )


@dataclass
class UbuntuStorageActionResult(FeatureActionResult):
    """
        Result of a storage-management write operation.

        :param target: Generic target identifier (volume id, pool name,
                       or disk path), populated for every result so
                       cross-OS coordinators can index uniformly.
        :param pool: The pool / VG name targeted, when applicable.
        :param volume: The volume / LV name targeted, when applicable.
        :param message: Human-readable detail for ``SKIPPED`` or
                        ``FAILED`` outcomes (e.g. data-loss advisories
                        on delete and remove-from-pool verbs).
    """
    target: Optional[str] = None
    pool: Optional[str] = None
    volume: Optional[str] = None
    message: Optional[str] = None


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class UbuntuDiskInfo:
    """
        Information about a single block device of type ``disk``.

        :param id: Stable disk identifier (``/dev/sda``).
        :param name: Short disk name (``sda``).
        :param size_bytes: Total disk size in bytes.
        :param model: Vendor model string (``lsblk -o MODEL``).  May be
                      empty.
        :param serial: Vendor serial (``lsblk -o SERIAL``).  May be
                       empty.
        :param partition_style: Partition-table label as detected via
                                ``blkid``.  ``"gpt"`` / ``"dos"`` /
                                ``"unknown"`` / ``None``.
        :param health_status: Best-effort SMART-derived health hint, or
                              ``None`` when SMART is not consulted.
        :param mount_points: Every mountpoint of any descendant
                             partition / LV.  Empty when the disk has no
                             mounted descendants.
        :param is_pool_member: True when the disk is initialised as an
                               LVM Physical Volume.  Derived from the
                               ``lvs`` / ``pvs`` output rather than from
                               ``lsblk``'s ``type=lvm``.
    """
    id: str
    name: str
    size_bytes: int
    model: Optional[str] = None
    serial: Optional[str] = None
    partition_style: Optional[str] = None
    health_status: Optional[str] = None
    mount_points: list = field(default_factory=list)
    is_pool_member: bool = False


@dataclass
class UbuntuVolumeInfo:
    """
        Information about a single Logical Volume (LVM LV) or raw
        partition surfaced as a volume.

        :param id: Stable volume identifier.  For LVs:
                   ``<vg>/<lv>``.  For raw partitions: ``/dev/sdaN``.
        :param name: Human-facing name (LV name or partition basename).
        :param size_bytes: Current size in bytes.
        :param used_bytes: Used bytes when reported (``data_percent``
                           computation), else ``None``.
        :param filesystem: Filesystem type (``ext4``, ``xfs``, ``swap``,
                           ...) as reported by ``blkid``.  ``None`` when
                           no filesystem signature is detected.
        :param mount_point: Active mountpoint when the volume's
                            filesystem is mounted, else ``None``.
        :param pool: VG name for LVs; ``None`` for raw partitions.
        :param health_status: LV ``lv_health_status`` field when
                              non-empty, else ``None``.
    """
    id: str
    name: str
    size_bytes: int
    used_bytes: Optional[int] = None
    filesystem: Optional[str] = None
    mount_point: Optional[str] = None
    pool: Optional[str] = None
    health_status: Optional[str] = None


@dataclass
class UbuntuPoolSpec:
    """
        Specification for :meth:`UbuntuStorageManager.create_pool`.

        :param name: Volume group name.
        :param disks: Disk identifiers to include at creation.  Each
                      will be initialised as a Physical Volume via
                      ``pvcreate`` before ``vgcreate`` runs.
        :param resiliency: Recorded for cross-OS-contract parity but
                           ignored on Ubuntu — LVM Volume Groups have no
                           native resiliency level (``mdadm`` provides
                           that beneath them).  Coordinators that care
                           about resiliency should compose ``mdadm`` and
                           LVM through the Linux pipeline manager.
    """
    name: str
    disks: list
    resiliency: Optional[str] = None


@dataclass
class UbuntuVolumeSpec:
    """
        Specification for :meth:`UbuntuStorageManager.create_volume`.

        :param name: Logical volume name (the ``-n`` argument to
                     ``lvcreate``).
        :param size_bytes: Requested size in bytes.  Mapped onto the
                           ``-L <bytes>B`` flag.
        :param pool: Volume Group name (the trailing positional argument
                     to ``lvcreate``).  Required for LVM volumes; only
                     ``None`` is rejected with a typed FAILED result.
        :param filesystem: Filesystem hint recorded onto the spec.  This
                           feature does not call ``mkfs``; the field is
                           kept so cross-OS callers can round-trip a
                           default.  Defaults to ``"ext4"``.
    """
    name: str
    size_bytes: int
    pool: Optional[str] = None
    filesystem: str = "ext4"


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class UbuntuStorageQueryError(Exception):
    """
        Raised when a storage read fails for a reason other than the
        target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list,
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command: list = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Storage query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# UbuntuStorageManager
# ----------------------------------------------------------------------


class UbuntuStorageManager(BaseFeatureManager):
    """
        Ubuntu storage manager.  Every verb is dispatched through the
        injected :class:`ICommandRunner`.  See the module docstring for
        the LVM concept mapping and elevation policy.
    """

    result_class = UbuntuStorageActionResult
    status_class = UbuntuStorageActionStatus
    feature_name = "storage-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_disks(self) -> list:
        """
            Enumerate physical disks via ``lsblk -J -b -o
            NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL`` (filtered to
            ``type=disk``).

            Pool-membership is derived from ``pvs`` so a disk shows up
            as ``is_pool_member=True`` when it (or any of its child
            partitions) is initialised as an LVM Physical Volume.

            :returns: One :class:`UbuntuDiskInfo` per ``type=disk`` row.
                      Returns an empty list when ``lsblk`` exits
                      non-zero or emits malformed JSON.
        """
        lsblk_command = [
            _LSBLK, "-J", "-b",
            "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL",
        ]
        run_result = self._runner.run(list(lsblk_command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval

        pv_disks = self._collect_pv_disks()
        rtnval = _parse_lsblk_disks(
            output=run_result.stdout, pv_disks=pv_disks,
        )
        return rtnval

    def list_volumes(self) -> list:
        """
            Enumerate volumes via ``lvs --reportformat json --units b``
            for LVs plus a ``lsblk`` walk for raw partitions.

            :returns: A list of :class:`UbuntuVolumeInfo`.  LVs come
                      first (in the order ``lvs`` reports them), then
                      raw partitions (``type=part`` rows from
                      ``lsblk``).  An empty list is returned when both
                      probes fail.
        """
        lvs_volumes = self._list_lvm_volumes()
        partition_volumes = self._list_partition_volumes()
        rtnval = list(lvs_volumes)
        rtnval.extend(partition_volumes)
        return rtnval

    def get_volume_info(
        self,
        volume_id: str,
    ) -> Optional[UbuntuVolumeInfo]:
        """
            Return the :class:`UbuntuVolumeInfo` matching ``volume_id``,
            or ``None`` when no such volume exists.

            :param volume_id: ``<vg>/<lv>`` for LVs, ``/dev/sdaN`` for
                              raw partitions.

            :returns: The matching info, or ``None``.
        """
        all_volumes = self.list_volumes()
        rtnval: Optional[UbuntuVolumeInfo] = None
        for volume in all_volumes:
            if volume.id == volume_id:
                rtnval = volume
                break
        return rtnval

    def get_disk_info(
        self,
        disk_id: str,
    ) -> Optional[UbuntuDiskInfo]:
        """
            Return the :class:`UbuntuDiskInfo` matching ``disk_id``, or
            ``None`` when no such disk exists.

            :param disk_id: ``/dev/sda`` (or any ``id`` value previously
                            returned by :meth:`list_disks`).

            :returns: The matching info, or ``None``.
        """
        all_disks = self.list_disks()
        rtnval: Optional[UbuntuDiskInfo] = None
        for disk in all_disks:
            if disk.id == disk_id:
                rtnval = disk
                break
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — volumes
    # ------------------------------------------------------------------

    def create_volume(
        self,
        spec: UbuntuVolumeSpec,
    ) -> UbuntuStorageActionResult:
        """
            Create a Logical Volume via ``lvcreate -L <bytes>B -n
            <name> <vg>``.

            Idempotent: when an LV with the same name in the same VG
            already exists with a matching size (within
            :data:`_RESIZE_TOLERANCE_BYTES`), returns ``SKIPPED``.

            :param spec: The :class:`UbuntuVolumeSpec` describing the
                         desired LV.

            :returns: The :class:`UbuntuStorageActionResult`.  Failures
                      surface ``FAILED`` with the runner's exit code,
                      stdout, and stderr.
        """
        if spec.pool is None:
            failed = self._build_result(
                status=UbuntuStorageActionStatus.FAILED,
                command=[],
                target=spec.name,
                pool=None,
                volume=spec.name,
                message="UbuntuVolumeSpec.pool is required for LVM volumes",
                stderr="missing pool",
            )
            return failed

        volume_id = "{}/{}".format(spec.pool, spec.name)
        size_token = "{}B".format(spec.size_bytes)
        command = [
            _LVCREATE,
            "-L", size_token,
            "-n", spec.name,
            spec.pool,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuStorageActionStatus.DRY_RUN,
                command=command,
                target=volume_id,
                pool=spec.pool,
                volume=spec.name,
            )
            return dry

        existing = self._lookup_lv(vg_name=spec.pool, lv_name=spec.name)
        if existing is not None:
            size_delta = abs(existing.size_bytes - spec.size_bytes)
            if size_delta <= _RESIZE_TOLERANCE_BYTES:
                skipped = self._build_result(
                    status=UbuntuStorageActionStatus.SKIPPED,
                    command=command,
                    target=volume_id,
                    pool=spec.pool,
                    volume=spec.name,
                    message=(
                        "logical volume {} already exists at "
                        "{} bytes".format(volume_id, existing.size_bytes)
                    ),
                )
                return skipped

        rtnval = self._dispatch_storage_write(
            command=command,
            target=volume_id,
            pool=spec.pool,
            volume=spec.name,
        )
        return rtnval

    def delete_volume(
        self,
        volume_id: str,
    ) -> UbuntuStorageActionResult:
        """
            Delete a Logical Volume via ``lvremove -f <vg>/<lv>``.

            Idempotent: when no LV with that ``<vg>/<lv>`` identifier
            exists, returns ``SKIPPED``.  On ``SUCCESS`` the result's
            ``message`` carries a data-loss advisory so the coordinator
            can surface it.

            :param volume_id: The ``<vg>/<lv>`` identifier.

            :returns: The :class:`UbuntuStorageActionResult`.
        """
        vg_name, lv_name = _split_volume_id(volume_id=volume_id)
        command = [_LVREMOVE, "-f", volume_id]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuStorageActionStatus.DRY_RUN,
                command=command,
                target=volume_id,
                pool=vg_name,
                volume=lv_name,
            )
            return dry

        existing = self._lookup_lv(vg_name=vg_name, lv_name=lv_name)
        if existing is None:
            skipped = self._build_result(
                status=UbuntuStorageActionStatus.SKIPPED,
                command=command,
                target=volume_id,
                pool=vg_name,
                volume=lv_name,
                message="logical volume {} does not exist".format(volume_id),
            )
            return skipped

        result = self._dispatch_storage_write(
            command=command,
            target=volume_id,
            pool=vg_name,
            volume=lv_name,
        )
        if result.status == UbuntuStorageActionStatus.SUCCESS:
            advisory = (
                "logical volume {} was removed; all data on the volume "
                "has been destroyed".format(volume_id)
            )
            result.message = advisory
            result.detail = advisory
        return result

    def resize_volume(
        self,
        volume_id: str,
        size_bytes: int,
    ) -> UbuntuStorageActionResult:
        """
            Resize a Logical Volume via ``lvresize -L <bytes>B
            --resizefs <vg>/<lv>``.

            Idempotent: when the LV's current size is within
            :data:`_RESIZE_TOLERANCE_BYTES` of ``size_bytes``, returns
            ``SKIPPED``.  ``--resizefs`` is always passed so the
            contained filesystem (if any) is grown / shrunk in lock-step
            with the LV.

            :param volume_id: The ``<vg>/<lv>`` identifier.
            :param size_bytes: The requested new size in bytes.

            :returns: The :class:`UbuntuStorageActionResult`.  Returns
                      ``FAILED`` when the LV does not exist (cf.
                      :meth:`delete_volume`'s SKIPPED, which is the
                      contract-mandated semantics for delete).
        """
        vg_name, lv_name = _split_volume_id(volume_id=volume_id)
        size_token = "{}B".format(size_bytes)
        command = [_LVRESIZE, "-L", size_token, "--resizefs", volume_id]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuStorageActionStatus.DRY_RUN,
                command=command,
                target=volume_id,
                pool=vg_name,
                volume=lv_name,
            )
            return dry

        existing = self._lookup_lv(vg_name=vg_name, lv_name=lv_name)
        if existing is None:
            failed = self._build_result(
                status=UbuntuStorageActionStatus.FAILED,
                command=command,
                target=volume_id,
                pool=vg_name,
                volume=lv_name,
                message="logical volume {} does not exist".format(volume_id),
                stderr="missing volume",
            )
            return failed

        size_delta = abs(existing.size_bytes - size_bytes)
        if size_delta <= _RESIZE_TOLERANCE_BYTES:
            skipped = self._build_result(
                status=UbuntuStorageActionStatus.SKIPPED,
                command=command,
                target=volume_id,
                pool=vg_name,
                volume=lv_name,
                message=(
                    "logical volume {} already at {} bytes".format(
                        volume_id, existing.size_bytes,
                    )
                ),
            )
            return skipped

        rtnval = self._dispatch_storage_write(
            command=command,
            target=volume_id,
            pool=vg_name,
            volume=lv_name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — pools
    # ------------------------------------------------------------------

    def create_pool(
        self,
        spec: UbuntuPoolSpec,
    ) -> UbuntuStorageActionResult:
        """
            Create a Volume Group.  Each member disk is initialised as a
            Physical Volume via ``pvcreate <disk>`` first; the VG is
            then created with ``vgcreate <name> <disks...>``.

            Idempotent: when a VG with the same name already exists,
            returns ``SKIPPED`` without touching the member disks.

            :param spec: The :class:`UbuntuPoolSpec` describing the
                         desired VG.

            :returns: The :class:`UbuntuStorageActionResult`.  When a
                      ``pvcreate`` step fails the VG is not created and
                      the failed step's exit code / stderr are surfaced.
        """
        vgcreate_command = [_VGCREATE, spec.name] + list(spec.disks)

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuStorageActionStatus.DRY_RUN,
                command=vgcreate_command,
                target=spec.name,
                pool=spec.name,
            )
            return dry

        if self._vg_exists(vg_name=spec.name):
            skipped = self._build_result(
                status=UbuntuStorageActionStatus.SKIPPED,
                command=vgcreate_command,
                target=spec.name,
                pool=spec.name,
                message="volume group {} already exists".format(spec.name),
            )
            return skipped

        for disk in spec.disks:
            pv_result = self._pvcreate(disk=disk)
            if pv_result.status != UbuntuStorageActionStatus.SUCCESS:
                return pv_result

        rtnval = self._dispatch_storage_write(
            command=vgcreate_command,
            target=spec.name,
            pool=spec.name,
        )
        return rtnval

    def delete_pool(
        self,
        pool_name: str,
    ) -> UbuntuStorageActionResult:
        """
            Delete a Volume Group via ``vgremove -f <name>``.

            Idempotent: when no VG with that name exists, returns
            ``SKIPPED``.  On ``SUCCESS`` the result's ``message``
            carries a data-loss advisory.

            :param pool_name: The volume-group name.

            :returns: The :class:`UbuntuStorageActionResult`.
        """
        command = [_VGREMOVE, "-f", pool_name]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuStorageActionStatus.DRY_RUN,
                command=command,
                target=pool_name,
                pool=pool_name,
            )
            return dry

        if not self._vg_exists(vg_name=pool_name):
            skipped = self._build_result(
                status=UbuntuStorageActionStatus.SKIPPED,
                command=command,
                target=pool_name,
                pool=pool_name,
                message="volume group {} does not exist".format(pool_name),
            )
            return skipped

        result = self._dispatch_storage_write(
            command=command,
            target=pool_name,
            pool=pool_name,
        )
        if result.status == UbuntuStorageActionStatus.SUCCESS:
            advisory = (
                "volume group {} was removed; every logical volume in "
                "the group has been destroyed".format(pool_name)
            )
            result.message = advisory
            result.detail = advisory
        return result

    def add_to_pool(
        self,
        pool_name: str,
        disk_id: str,
    ) -> UbuntuStorageActionResult:
        """
            Add ``disk_id`` to ``pool_name``.  ``pvcreate <disk>`` runs
            first, then ``vgextend <pool> <disk>``.

            Idempotent: when ``disk_id`` is already a Physical Volume
            inside ``pool_name``, returns ``SKIPPED``.

            :param pool_name: The volume-group name.
            :param disk_id: The disk identifier.

            :returns: The :class:`UbuntuStorageActionResult`.
        """
        vgextend_command = [_VGEXTEND, pool_name, disk_id]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuStorageActionStatus.DRY_RUN,
                command=vgextend_command,
                target=disk_id,
                pool=pool_name,
            )
            return dry

        if self._disk_in_pool(pool_name=pool_name, disk_id=disk_id):
            skipped = self._build_result(
                status=UbuntuStorageActionStatus.SKIPPED,
                command=vgextend_command,
                target=disk_id,
                pool=pool_name,
                message=(
                    "disk {} is already a physical volume in {}".format(
                        disk_id, pool_name,
                    )
                ),
            )
            return skipped

        pv_result = self._pvcreate(disk=disk_id)
        if pv_result.status != UbuntuStorageActionStatus.SUCCESS:
            pv_result.pool = pool_name
            return pv_result

        rtnval = self._dispatch_storage_write(
            command=vgextend_command,
            target=disk_id,
            pool=pool_name,
        )
        return rtnval

    def remove_from_pool(
        self,
        pool_name: str,
        disk_id: str,
    ) -> UbuntuStorageActionResult:
        """
            Remove ``disk_id`` from ``pool_name``.  ``vgreduce <pool>
            <disk>`` runs first; on success ``pvremove <disk>`` strips
            the LVM signature so the disk can be reused.

            Idempotent: when ``disk_id`` is not currently a Physical
            Volume inside ``pool_name``, returns ``SKIPPED``.  On
            ``SUCCESS`` the result's ``message`` carries a data-loss
            advisory.

            :param pool_name: The volume-group name.
            :param disk_id: The disk identifier.

            :returns: The :class:`UbuntuStorageActionResult`.
        """
        vgreduce_command = [_VGREDUCE, pool_name, disk_id]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuStorageActionStatus.DRY_RUN,
                command=vgreduce_command,
                target=disk_id,
                pool=pool_name,
            )
            return dry

        if not self._disk_in_pool(pool_name=pool_name, disk_id=disk_id):
            skipped = self._build_result(
                status=UbuntuStorageActionStatus.SKIPPED,
                command=vgreduce_command,
                target=disk_id,
                pool=pool_name,
                message=(
                    "disk {} is not a physical volume in {}".format(
                        disk_id, pool_name,
                    )
                ),
            )
            return skipped

        reduce_result = self._dispatch_storage_write(
            command=vgreduce_command,
            target=disk_id,
            pool=pool_name,
        )
        if reduce_result.status != UbuntuStorageActionStatus.SUCCESS:
            return reduce_result

        # Strip the PV signature.  A failed pvremove is non-fatal but is
        # surfaced via the result so coordinators can act on it.
        pvremove_command = [_PVREMOVE, disk_id]
        pvremove_result = self._dispatch_storage_write(
            command=pvremove_command,
            target=disk_id,
            pool=pool_name,
        )
        if pvremove_result.status != UbuntuStorageActionStatus.SUCCESS:
            pvremove_result.message = (
                "vgreduce succeeded but pvremove failed; the PV "
                "signature on {} may need manual cleanup".format(disk_id)
            )
            return pvremove_result

        advisory = (
            "disk {} was removed from volume group {}; the LVM "
            "physical-volume signature has been wiped".format(
                disk_id, pool_name,
            )
        )
        reduce_result.message = advisory
        reduce_result.detail = advisory
        return reduce_result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> UbuntuStorageManagerConfig:
        """
            Return ``self._config`` typed as
            :class:`UbuntuStorageManagerConfig`, falling back to a
            synthesised default when the caller passed a plain
            :class:`FeatureConfig`.
        """
        if isinstance(self._config, UbuntuStorageManagerConfig):
            rtnval = self._config
        else:
            rtnval = UbuntuStorageManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _lvs_format_args(self) -> list:
        """
            Return the ``--reportformat json --units b`` suffix from
            the typed config.
        """
        rtnval = list(self._typed_config().lvs_format_args)
        return rtnval

    def _list_lvm_volumes(self) -> list:
        """
            Run ``lvs --reportformat json --units b`` and parse the
            output into :class:`UbuntuVolumeInfo` rows.
        """
        command = [_LVS] + self._lvs_format_args()
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval
        rtnval = _parse_lvs_output(output=run_result.stdout)
        return rtnval

    def _list_partition_volumes(self) -> list:
        """
            Run ``lsblk -J -b -o
            NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,KNAME`` and pull out
            ``type=part`` rows as :class:`UbuntuVolumeInfo`.
        """
        command = [
            _LSBLK, "-J", "-b",
            "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,KNAME",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval
        rtnval = _parse_lsblk_partitions(output=run_result.stdout)
        return rtnval

    def _lookup_lv(
        self,
        *,
        vg_name: str,
        lv_name: str,
    ) -> Optional[UbuntuVolumeInfo]:
        """
            Probe ``lvs`` for a single LV and return its
            :class:`UbuntuVolumeInfo`, or ``None``.
        """
        all_lvs = self._list_lvm_volumes()
        rtnval: Optional[UbuntuVolumeInfo] = None
        target_id = "{}/{}".format(vg_name, lv_name)
        for lv in all_lvs:
            if lv.id == target_id:
                rtnval = lv
                break
        return rtnval

    def _vg_exists(self, *, vg_name: str) -> bool:
        """
            Return True when ``vgs`` reports a VG with the given name.
        """
        command = [_VGS] + self._lvs_format_args()
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        rtnval = _vg_present_in_output(
            output=run_result.stdout, vg_name=vg_name,
        )
        return rtnval

    def _disk_in_pool(self, *, pool_name: str, disk_id: str) -> bool:
        """
            Return True when ``pvs`` reports ``disk_id`` as a Physical
            Volume in ``pool_name``.
        """
        command = [_PVS] + self._lvs_format_args()
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        rtnval = _pv_in_vg(
            output=run_result.stdout,
            pv_name=disk_id,
            vg_name=pool_name,
        )
        return rtnval

    def _collect_pv_disks(self) -> set:
        """
            Return the set of disk identifiers that ``pvs`` reports as
            initialised PVs.  Used to populate
            :attr:`UbuntuDiskInfo.is_pool_member`.
        """
        command = [_PVS] + self._lvs_format_args()
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: set = set()
            return rtnval
        rtnval = _pv_disks_from_output(output=run_result.stdout)
        return rtnval

    def _pvcreate(self, *, disk: str) -> UbuntuStorageActionResult:
        """
            Initialise ``disk`` as an LVM Physical Volume.
        """
        command = [_PVCREATE, disk]
        rtnval = self._dispatch_storage_write(
            command=command,
            target=disk,
        )
        return rtnval

    def _dispatch_storage_write(
        self,
        *,
        command: list,
        target: Optional[str] = None,
        pool: Optional[str] = None,
        volume: Optional[str] = None,
    ) -> UbuntuStorageActionResult:
        """
            Dispatch a write command and wrap the result with the
            relevant storage-scope fields.
        """
        rtnval = self._dispatch(
            command=command,
            detail=target,
            operation="storage_write",
            target=target,
            pool=pool,
            volume=volume,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuStorageActionStatus,
        command: list,
        target: Optional[str] = None,
        pool: Optional[str] = None,
        volume: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuStorageActionResult:
        """
            Construct an :class:`UbuntuStorageActionResult` without
            invoking the runner.
        """
        if message is not None:
            detail_value: Optional[str] = message
        else:
            detail_value = target
        rtnval = UbuntuStorageActionResult(
            status=status,
            detail=detail_value,
            command=list(command),
            target=target,
            pool=pool,
            volume=volume,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers and helpers
# ----------------------------------------------------------------------


def _split_volume_id(*, volume_id: str) -> tuple:
    """
        Split a ``<vg>/<lv>`` identifier into ``(vg, lv)``.  Tolerates
        leading whitespace and trailing slashes; returns
        ``(volume_id, "")`` when no slash is present.
    """
    text = volume_id.strip()
    if "/" not in text:
        rtnval = (text, "")
        return rtnval
    vg_name, sep, lv_name = text.partition("/")
    rtnval = (vg_name, lv_name)
    return rtnval


def _parse_lvm_size(*, size_str: str) -> int:
    """
        Parse an LVM JSON size string (``"<number>B"`` or
        ``"<number>"``) into bytes.  Tolerates a trailing ``B`` suffix
        emitted by ``--units b``.

        :param size_str: The raw size string.

        :returns: The byte count, or ``0`` when unparseable.
    """
    token = size_str.strip()
    if len(token) == 0:
        rtnval = 0
        return rtnval
    if token.endswith("B"):
        token = token[:-1]
    try:
        rtnval = int(token)
    except ValueError:
        rtnval = 0
    return rtnval


def _parse_lvm_percent(*, percent_str: str) -> Optional[int]:
    """
        Parse an LVM JSON percent string (``"42.50"``) into a 0-100
        integer percentage.  Returns ``None`` when empty or
        unparseable.
    """
    text = percent_str.strip()
    if len(text) == 0:
        rtnval: Optional[int] = None
        return rtnval
    try:
        rtnval = int(float(text))
    except ValueError:
        rtnval = None
    return rtnval


def _parse_lvs_output(*, output: str) -> list:
    """
        Parse ``lvs --reportformat json`` output into
        :class:`UbuntuVolumeInfo` rows.

        :param output: Raw stdout text.

        :returns: A list (possibly empty) of :class:`UbuntuVolumeInfo`.
    """
    rtnval: list = []
    stripped = output.strip()
    if len(stripped) == 0:
        return rtnval
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval
    reports = data.get("report", [])
    if not isinstance(reports, list):
        return rtnval
    for report in reports:
        if not isinstance(report, dict):
            continue
        for entry in report.get("lv", []):
            if not isinstance(entry, dict):
                continue
            lv_name = entry.get("lv_name", "")
            vg_name = entry.get("vg_name", "")
            if len(lv_name) == 0 or len(vg_name) == 0:
                continue
            size_bytes = _parse_lvm_size(size_str=entry.get("lv_size", "0"))
            data_percent_raw = entry.get("data_percent", "")
            data_percent = _parse_lvm_percent(
                percent_str=str(data_percent_raw),
            )
            if data_percent is not None:
                used_bytes: Optional[int] = int(
                    size_bytes * data_percent / 100,
                )
            else:
                used_bytes = None
            health_value = entry.get("lv_health_status", "")
            if isinstance(health_value, str) and len(health_value) == 0:
                health: Optional[str] = None
            else:
                health = health_value
            volume = UbuntuVolumeInfo(
                id="{}/{}".format(vg_name, lv_name),
                name=lv_name,
                size_bytes=size_bytes,
                used_bytes=used_bytes,
                filesystem=None,
                mount_point=None,
                pool=vg_name,
                health_status=health,
            )
            rtnval.append(volume)
    return rtnval


def _parse_lsblk_partitions(*, output: str) -> list:
    """
        Walk ``lsblk -J`` output breadth-first and return one
        :class:`UbuntuVolumeInfo` per ``type=part`` row.
    """
    rtnval: list = []
    stripped = output.strip()
    if len(stripped) == 0:
        return rtnval
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval
    queue: list = []
    blockdevices = data.get("blockdevices", [])
    if not isinstance(blockdevices, list):
        return rtnval
    for entry in blockdevices:
        if isinstance(entry, dict):
            queue.append(entry)
    while len(queue) > 0:
        head = queue.pop(0)
        device_type = head.get("type", "")
        if device_type == "part":
            name = head.get("name", "")
            size_raw = head.get("size", 0)
            try:
                size_bytes = int(size_raw)
            except (TypeError, ValueError):
                size_bytes = 0
            volume = UbuntuVolumeInfo(
                id="/dev/{}".format(name),
                name=name,
                size_bytes=size_bytes,
                used_bytes=None,
                filesystem=head.get("fstype"),
                mount_point=head.get("mountpoint"),
                pool=None,
                health_status=None,
            )
            rtnval.append(volume)
        children = head.get("children", [])
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    queue.append(child)
    return rtnval


def _parse_lsblk_disks(
    *,
    output: str,
    pv_disks: set,
) -> list:
    """
        Walk ``lsblk -J`` output and return one :class:`UbuntuDiskInfo`
        per ``type=disk`` row.

        :param output: Raw stdout from ``lsblk``.
        :param pv_disks: Set of disk identifiers that are LVM PVs (used
                         to populate ``is_pool_member``).
    """
    rtnval: list = []
    stripped = output.strip()
    if len(stripped) == 0:
        return rtnval
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval
    blockdevices = data.get("blockdevices", [])
    if not isinstance(blockdevices, list):
        return rtnval

    for entry in blockdevices:
        if not isinstance(entry, dict):
            continue
        device_type = entry.get("type", "")
        if device_type != "disk":
            continue
        name = entry.get("name", "")
        if len(name) == 0:
            continue
        disk_id = "/dev/{}".format(name)
        size_raw = entry.get("size", 0)
        try:
            size_bytes = int(size_raw)
        except (TypeError, ValueError):
            size_bytes = 0

        model_raw = entry.get("model")
        if isinstance(model_raw, str) and len(model_raw.strip()) > 0:
            model_value: Optional[str] = model_raw.strip()
        else:
            model_value = None

        serial_raw = entry.get("serial")
        if isinstance(serial_raw, str) and len(serial_raw.strip()) > 0:
            serial_value: Optional[str] = serial_raw.strip()
        else:
            serial_value = None

        mount_points: list = []
        own_mount = entry.get("mountpoint")
        if isinstance(own_mount, str) and len(own_mount) > 0:
            mount_points.append(own_mount)
        children = entry.get("children", [])
        if isinstance(children, list):
            child_queue = list(children)
            while len(child_queue) > 0:
                child = child_queue.pop(0)
                if not isinstance(child, dict):
                    continue
                child_mount = child.get("mountpoint")
                if (
                    isinstance(child_mount, str)
                    and len(child_mount) > 0
                ):
                    mount_points.append(child_mount)
                grandchildren = child.get("children", [])
                if isinstance(grandchildren, list):
                    for grand in grandchildren:
                        if isinstance(grand, dict):
                            child_queue.append(grand)

        is_member = disk_id in pv_disks
        if not is_member and isinstance(children, list):
            for child in children:
                if not isinstance(child, dict):
                    continue
                child_name = child.get("name", "")
                child_id = "/dev/{}".format(child_name)
                if child_id in pv_disks:
                    is_member = True
                    break

        info = UbuntuDiskInfo(
            id=disk_id,
            name=name,
            size_bytes=size_bytes,
            model=model_value,
            serial=serial_value,
            partition_style=None,
            health_status=None,
            mount_points=mount_points,
            is_pool_member=is_member,
        )
        rtnval.append(info)
    return rtnval


def _vg_present_in_output(*, output: str, vg_name: str) -> bool:
    """
        Return True when ``vgs --reportformat json`` output contains a
        ``vg`` entry whose ``vg_name`` matches ``vg_name``.
    """
    stripped = output.strip()
    if len(stripped) == 0:
        rtnval = False
        return rtnval
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        rtnval = False
        return rtnval
    rtnval = False
    reports = data.get("report", [])
    if not isinstance(reports, list):
        return rtnval
    for report in reports:
        if not isinstance(report, dict):
            continue
        for entry in report.get("vg", []):
            if not isinstance(entry, dict):
                continue
            if entry.get("vg_name", "") == vg_name:
                rtnval = True
                return rtnval
    return rtnval


def _pv_in_vg(*, output: str, pv_name: str, vg_name: str) -> bool:
    """
        Return True when ``pvs --reportformat json`` output contains a
        row whose ``pv_name`` and ``vg_name`` both match.
    """
    stripped = output.strip()
    if len(stripped) == 0:
        rtnval = False
        return rtnval
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        rtnval = False
        return rtnval
    rtnval = False
    reports = data.get("report", [])
    if not isinstance(reports, list):
        return rtnval
    for report in reports:
        if not isinstance(report, dict):
            continue
        for entry in report.get("pv", []):
            if not isinstance(entry, dict):
                continue
            if entry.get("pv_name", "") != pv_name:
                continue
            if entry.get("vg_name", "") != vg_name:
                continue
            rtnval = True
            return rtnval
    return rtnval


def _pv_disks_from_output(*, output: str) -> set:
    """
        Return the set of ``pv_name`` values present in ``pvs
        --reportformat json`` output.
    """
    rtnval: set = set()
    stripped = output.strip()
    if len(stripped) == 0:
        return rtnval
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval
    reports = data.get("report", [])
    if not isinstance(reports, list):
        return rtnval
    for report in reports:
        if not isinstance(report, dict):
            continue
        for entry in report.get("pv", []):
            if not isinstance(entry, dict):
                continue
            pv_name = entry.get("pv_name", "")
            if isinstance(pv_name, str) and len(pv_name) > 0:
                rtnval.add(pv_name)
    return rtnval
