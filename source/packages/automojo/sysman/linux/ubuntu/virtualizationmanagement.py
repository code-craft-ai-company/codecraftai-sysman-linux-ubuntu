"""
    Ubuntu virtualization-management feature.

    Implements the cross-OS virtualization-management contract for
    Ubuntu using KVM / libvirt through ``virsh`` and ``virt-install``.
    Every operation is dispatched via the injected
    :class:`ICommandRunner`; this module never constructs its own
    subprocess.

    Backend selection:
        :class:`UbuntuVirtBackend` is the explicit selector recorded on
        :class:`UbuntuVirtualizationManagerConfig`.  Today the only
        supported value is ``LIBVIRT`` (KVM via the libvirt user-space
        toolchain).  The enum exists so future Ubuntu-flavoured backends
        (LXC, LXD) can plug into the same manager without churning the
        public API.  When ``UbuntuVirtBackend`` is anything other than
        ``LIBVIRT``, every verb returns a typed ``FAILED`` result with
        ``message="unsupported_backend"``.

    Connection URI:
        Every ``virsh`` invocation is prefixed with
        ``--connect <uri>`` sourced from
        :attr:`UbuntuVirtualizationManagerConfig.default_connection`
        (default ``"qemu:///system"``).  Setting the URI to the empty
        string suppresses the flag so libvirt's own URI-resolution rules
        apply.

    Elevation policy:
        Every write verb (``create_vm``, ``delete_vm``, ``start_vm``,
        ``stop_vm``, ``pause_vm``, ``resume_vm``, ``attach_disk``,
        ``detach_disk``, ``snapshot_vm``, ``restore_snapshot``) honours
        :attr:`FeatureConfig.elevated`.  Read verbs (``list_vms``,
        ``get_vm_info``, ``list_snapshots``, the idempotency probes)
        never elevate.  Libvirt typically requires either root or
        membership in the ``libvirt`` group; both situations are
        represented by ``elevated=True``.

    Stop semantics:
        ``stop_vm(force=False)`` issues a graceful ``virsh shutdown``;
        ``stop_vm(force=True)`` issues a hard ``virsh destroy`` (the
        libvirt name is misleading — ``destroy`` powers off the running
        domain but leaves the persistent definition in place).

    Snapshots:
        ``snapshot_vm`` is idempotent on snapshot name
        (``virsh snapshot-list`` is consulted first).
        ``restore_snapshot`` is unconditional, matching the contract.
        Both verbs operate on the libvirt snapshot surface, not on
        backing-file shadows.
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
# Module-level command tokens and defaults
# ----------------------------------------------------------------------


_VIRSH = "virsh"
_VIRT_INSTALL = "virt-install"

_DEFAULT_CONNECTION_URI = "qemu:///system"

# Common libvirt domain-state strings emitted by ``virsh dominfo``,
# ``virsh domstate``, ``virsh snapshot-list``, and the trailing
# ``State`` column of ``virsh list --all``.  ``virsh dominfo`` uses
# the human form ``shut off``; ``virsh snapshot-list`` collapses the
# space to ``shutoff``.  Both variants map onto
# :class:`UbuntuVmState.STOPPED`.
_LIBVIRT_RUNNING = "running"
_LIBVIRT_PAUSED = "paused"
_LIBVIRT_SHUT_OFF = "shut off"
_LIBVIRT_SHUT_OFF_NOSPACE = "shut-off"
_LIBVIRT_SHUTOFF_COLLAPSED = "shutoff"
_LIBVIRT_PMSUSPENDED = "pmsuspended"


# ----------------------------------------------------------------------
# Aliases / config / result types
# ----------------------------------------------------------------------


UbuntuVirtualizationActionStatus = ActionStatus


class UbuntuVirtBackend(Enum):
    """
        Selector for the virtualization backend driving the manager.

        ``LIBVIRT`` is the only supported value at present; the enum
        exists so future Ubuntu-flavoured backends (``LXC``, ``LXD``)
        can be plugged in without changing the public API.
    """
    LIBVIRT = "libvirt"
    LXC     = "lxc"
    LXD     = "lxd"


class UbuntuVmState(Enum):
    """
        Coarse power / lifecycle state for an Ubuntu / KVM VM.
    """
    RUNNING   = "running"
    STOPPED   = "stopped"
    PAUSED    = "paused"
    SUSPENDED = "suspended"
    OTHER     = "other"


@dataclass
class UbuntuVirtualizationManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuVirtualizationManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param backend: Virtualization backend selector.  Defaults to
                        :attr:`UbuntuVirtBackend.LIBVIRT`.  Anything else
                        causes every verb to return a typed FAILED with
                        ``message="unsupported_backend"``.
        :param virsh_path: Absolute path or PATH-relative name of the
                           ``virsh`` binary.  Defaults to ``"virsh"``.
        :param virt_install_path: Absolute path or PATH-relative name
                                  of the ``virt-install`` binary.
                                  Defaults to ``"virt-install"``.
        :param default_connection: libvirt connection URI passed via
                                   ``--connect``.  Defaults to
                                   ``"qemu:///system"``.  An empty
                                   string suppresses the flag entirely
                                   so libvirt's default URI-resolution
                                   logic applies.
    """
    backend: UbuntuVirtBackend = UbuntuVirtBackend.LIBVIRT
    virsh_path: str = _VIRSH
    virt_install_path: str = _VIRT_INSTALL
    default_connection: str = _DEFAULT_CONNECTION_URI


@dataclass
class UbuntuVirtualizationActionResult(FeatureActionResult):
    """
        Result of a virtualization-management write operation.

        :param vm_name: VM (libvirt domain) name targeted by the
                        operation, when applicable.
        :param snapshot_name: Snapshot name targeted by the operation,
                              when applicable.
        :param message: Human-readable detail for ``SKIPPED`` /
                        ``FAILED`` outcomes (e.g. data-loss advisories
                        on ``delete_vm`` / ``detach_disk`` /
                        ``restore_snapshot``).
    """
    vm_name: Optional[str] = None
    snapshot_name: Optional[str] = None
    message: Optional[str] = None


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class UbuntuVmSpec:
    """
        Specification for :meth:`UbuntuVirtualizationManager.create_vm`.

        :param name: Domain name (must be unique within the libvirt
                     connection).  Becomes the ``--name`` argument to
                     ``virt-install``.
        :param cpu_count: Number of vCPUs (``--vcpus``).  Defaults to 1.
        :param memory_mb: Allocated memory in mebibytes (``--memory``).
                          Defaults to 1024 (1 GiB).
        :param disk_size_gb: When provided, ``virt-install`` is asked to
                             create a fresh qcow2 disk of this size in
                             the default storage pool unless
                             ``disk_path`` overrides.  ``None`` lets the
                             caller supply a disk-spec via
                             :class:`UbuntuVmDiskSpec` later.
        :param disk_path: When provided, used as the ``--disk path=``
                          argument so ``virt-install`` attaches an
                          existing disk image (boot media or a
                          pre-built qcow2).
        :param os_variant: ``--os-variant`` token (e.g.
                           ``"ubuntu22.04"``).  Optional; libvirt may
                           emit a non-fatal warning when omitted.
        :param network: ``--network`` argument (e.g.
                        ``"network=default"`` or ``"bridge=br0"``).
                        Optional; ``virt-install`` defaults to NAT
                        when missing.
        :param iso_path: When provided, ``virt-install`` is asked to
                         boot from this ISO via ``--cdrom``.
        :param graphics: ``--graphics`` argument.  Defaults to
                         ``"none"`` for headless installs.
        :param boot_kind: Reserved for cross-OS-spec parity (Windows
                          calls this *generation*; Linux ignores it).
                          Defaults to 1.
    """
    name: str
    cpu_count: int = 1
    memory_mb: int = 1024
    disk_size_gb: Optional[int] = None
    disk_path: Optional[str] = None
    os_variant: Optional[str] = None
    network: Optional[str] = None
    iso_path: Optional[str] = None
    graphics: str = "none"
    boot_kind: int = 1


@dataclass
class UbuntuVmInfo:
    """
        Snapshot of a single VM's configuration and state.

        :param name: Domain name (libvirt domain Name field).
        :param state: Coarse power state.
        :param cpu_count: Configured vCPUs (``CPU(s)`` from
                          ``virsh dominfo``).  ``None`` when unparseable.
        :param memory_mb: Configured memory in mebibytes (``Max
                          memory`` from ``virsh dominfo``).  ``None``
                          when unparseable.
        :param uptime_seconds: Reserved.  ``virsh dominfo`` does not
                               report a numeric uptime; this is None
                               unless a richer probe is added.
        :param os_type: ``OS Type`` from ``virsh dominfo`` when reported
                        (typically ``"hvm"``).
        :param vm_id: Domain UUID from ``virsh dominfo``.  ``None`` when
                      not reported.
    """
    name: str
    state: UbuntuVmState
    cpu_count: Optional[int] = None
    memory_mb: Optional[int] = None
    uptime_seconds: Optional[int] = None
    os_type: Optional[str] = None
    vm_id: Optional[str] = None


@dataclass
class UbuntuVmDiskSpec:
    """
        Specification for :meth:`UbuntuVirtualizationManager.attach_disk`.

        :param path: Backing-file path on the host that libvirt will
                     attach.  Mapped onto the ``<source>`` argument of
                     ``virsh attach-disk``.
        :param target: Guest-side device label (``"vdb"``,
                       ``"sdc"``).  Mapped onto the ``<target>``
                       argument.  When ``None`` the manager falls back
                       to ``"vdb"`` so callers can omit it for the
                       common case.
        :param controller_type: Reserved for cross-OS-spec parity
                                (Windows uses ``ControllerType``);
                                ignored on Ubuntu.
        :param bus: ``--targetbus`` argument (e.g. ``"virtio"``,
                    ``"sata"``).  Optional.
    """
    path: str
    target: Optional[str] = None
    controller_type: Optional[str] = None
    bus: Optional[str] = None


@dataclass
class UbuntuSnapshotInfo:
    """
        Single snapshot entry returned by :meth:`list_snapshots`.

        :param name: Snapshot name.
        :param created: Creation timestamp string as reported by
                        ``virsh snapshot-list``.  ``"-"`` is normalised
                        to an empty string.
        :param description: Optional description recorded against the
                            snapshot.  ``None`` when not reported.
        :param parent: Parent snapshot name, when reported.  ``None``
                       for top-level snapshots.
        :param vm_state: Domain state captured by the snapshot, when
                         reported.  ``None`` when unparseable.
    """
    name: str
    created: str
    description: Optional[str] = None
    parent: Optional[str] = None
    vm_state: Optional[UbuntuVmState] = None


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class UbuntuVirtualizationQueryError(Exception):
    """
        Raised when a virtualization read fails for a reason other than
        the target VM / snapshot simply being absent.
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

        err_msg_lines = ["Virtualization query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# UbuntuVirtualizationManager
# ----------------------------------------------------------------------


class UbuntuVirtualizationManager(BaseFeatureManager):
    """
        Ubuntu KVM / libvirt virtualization manager.  Every verb is
        dispatched through the injected :class:`ICommandRunner`.  See
        the module docstring for backend selection, connection-URI, and
        elevation rules.
    """

    result_class = UbuntuVirtualizationActionResult
    status_class = UbuntuVirtualizationActionStatus
    feature_name = "virtualization-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_vms(self) -> list:
        """
            Enumerate every defined VM.

            Dispatches ``virsh list --all --name`` to gather the bare
            list of names, then ``virsh dominfo <name>`` per name to
            collect detailed state.  Reads do not elevate.

            :returns: One :class:`UbuntuVmInfo` per defined VM.  Empty
                      list when no VMs are defined.
            :raises UbuntuVirtualizationQueryError: When ``virsh list``
                                                    fails for an
                                                    unexpected reason.
        """
        if not self._backend_supported():
            rtnval: list = []
            return rtnval

        list_command = self._virsh_argv(
            args=["list", "--all", "--name"],
        )
        list_run = self._runner.run(list(list_command), elevated=False)
        if list_run.exit_code != 0:
            raise UbuntuVirtualizationQueryError(
                command=list_command,
                exit_code=list_run.exit_code,
                stderr=list_run.stderr,
            )

        names = _parse_virsh_list_names(stdout=list_run.stdout)
        rtnval = []
        for name in names:
            info = self._read_dominfo(name=name)
            if info is not None:
                rtnval.append(info)
        return rtnval

    def get_vm_info(self, name: str) -> Optional[UbuntuVmInfo]:
        """
            Return the :class:`UbuntuVmInfo` for ``name``, or ``None``
            when the VM is not defined.

            Reads do not elevate.

            :param name: Domain name.

            :returns: The matching :class:`UbuntuVmInfo`, or ``None``.
            :raises UbuntuVirtualizationQueryError: When ``virsh
                                                    dominfo`` fails for
                                                    an unexpected reason.
        """
        if not self._backend_supported():
            rtnval: Optional[UbuntuVmInfo] = None
            return rtnval

        info = self._read_dominfo(name=name, raise_on_unknown=True)
        rtnval = info
        return rtnval

    def list_snapshots(self, vm: str) -> list:
        """
            Enumerate snapshots for ``vm``.

            Dispatches ``virsh snapshot-list <vm>`` and parses the
            tabular output into :class:`UbuntuSnapshotInfo` rows.  Reads
            do not elevate.

            :param vm: Domain name.

            :returns: One :class:`UbuntuSnapshotInfo` per parsed row.
            :raises UbuntuVirtualizationQueryError: When the cmdlet
                                                    fails for an
                                                    unexpected reason.
        """
        if not self._backend_supported():
            rtnval: list = []
            return rtnval

        command = self._virsh_argv(
            args=["snapshot-list", vm],
        )
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuVirtualizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_virsh_snapshot_list(stdout=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — VM lifecycle
    # ------------------------------------------------------------------

    def create_vm(
        self,
        spec: UbuntuVmSpec,
    ) -> UbuntuVirtualizationActionResult:
        """
            Provision a fresh VM via ``virt-install``.

            Idempotent: when a VM with the same ``spec.name`` already
            exists, returns ``SKIPPED`` regardless of whether the spec
            matches the existing definition (full XML compare is out of
            scope at this layer).

            :param spec: The :class:`UbuntuVmSpec`.

            :returns: The :class:`UbuntuVirtualizationActionResult`.
        """
        if not self._backend_supported():
            failed = self._build_result(
                status=UbuntuVirtualizationActionStatus.FAILED,
                command=[],
                vm_name=spec.name,
                message="unsupported_backend",
            )
            return failed

        command = self._build_virt_install_argv(spec=spec)

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=spec.name,
            )
            return dry

        existing = self._read_dominfo(name=spec.name)
        if existing is not None:
            skipped = self._build_result(
                status=UbuntuVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=spec.name,
                message="VM {!r} already exists".format(spec.name),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command,
            vm_name=spec.name,
        )
        return rtnval

    def delete_vm(self, name: str) -> UbuntuVirtualizationActionResult:
        """
            Delete the persistent definition of ``name`` and remove its
            backing storage via ``virsh undefine <name>
            --remove-all-storage``.

            Idempotent: when the VM is not defined, returns ``SKIPPED``.
            On ``SUCCESS`` ``message`` carries a data-loss advisory.

            :param name: Domain name.

            :returns: The :class:`UbuntuVirtualizationActionResult`.
        """
        if not self._backend_supported():
            failed = self._build_result(
                status=UbuntuVirtualizationActionStatus.FAILED,
                command=[],
                vm_name=name,
                message="unsupported_backend",
            )
            return failed

        command = self._virsh_argv(
            args=["undefine", name, "--remove-all-storage"],
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=name,
            )
            return dry

        existing = self._read_dominfo(name=name)
        if existing is None:
            skipped = self._build_result(
                status=UbuntuVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=name,
                message="VM {!r} is not defined".format(name),
            )
            return skipped

        result = self._dispatch_virt_write(
            command=command,
            vm_name=name,
        )
        if result.status == UbuntuVirtualizationActionStatus.SUCCESS:
            advisory = (
                "VM {!r} was undefined; --remove-all-storage destroyed "
                "every backing volume associated with the domain"
            ).format(name)
            result.message = advisory
            result.detail = advisory
        return result

    def start_vm(self, name: str) -> UbuntuVirtualizationActionResult:
        """
            Power on a defined VM via ``virsh start <name>``.

            Idempotent: when the VM is already running, returns
            ``SKIPPED``.

            :param name: Domain name.

            :returns: The :class:`UbuntuVirtualizationActionResult`.
        """
        if not self._backend_supported():
            failed = self._build_result(
                status=UbuntuVirtualizationActionStatus.FAILED,
                command=[],
                vm_name=name,
                message="unsupported_backend",
            )
            return failed

        command = self._virsh_argv(args=["start", name])

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=name,
            )
            return dry

        existing = self._read_dominfo(name=name)
        if existing is not None and existing.state == UbuntuVmState.RUNNING:
            skipped = self._build_result(
                status=UbuntuVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=name,
                message="VM {!r} is already running".format(name),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command,
            vm_name=name,
        )
        return rtnval

    def stop_vm(
        self,
        name: str,
        force: bool = False,
    ) -> UbuntuVirtualizationActionResult:
        """
            Power off a running VM.

            ``force=False`` issues a graceful ``virsh shutdown``;
            ``force=True`` issues a hard ``virsh destroy`` (the libvirt
            spelling — the persistent definition stays intact).

            Idempotent: when the VM is already stopped, returns
            ``SKIPPED``.

            :param name: Domain name.
            :param force: When True, hard-power-off via ``virsh destroy``.

            :returns: The :class:`UbuntuVirtualizationActionResult`.
        """
        if not self._backend_supported():
            failed = self._build_result(
                status=UbuntuVirtualizationActionStatus.FAILED,
                command=[],
                vm_name=name,
                message="unsupported_backend",
            )
            return failed

        if force:
            verb = "destroy"
        else:
            verb = "shutdown"
        command = self._virsh_argv(args=[verb, name])

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=name,
            )
            return dry

        existing = self._read_dominfo(name=name)
        if existing is not None and existing.state == UbuntuVmState.STOPPED:
            skipped = self._build_result(
                status=UbuntuVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=name,
                message="VM {!r} is already stopped".format(name),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command,
            vm_name=name,
        )
        return rtnval

    def pause_vm(self, name: str) -> UbuntuVirtualizationActionResult:
        """
            Pause a running VM via ``virsh suspend <name>``.

            Idempotent: when the VM is already paused, returns
            ``SKIPPED``.

            :param name: Domain name.

            :returns: The :class:`UbuntuVirtualizationActionResult`.
        """
        if not self._backend_supported():
            failed = self._build_result(
                status=UbuntuVirtualizationActionStatus.FAILED,
                command=[],
                vm_name=name,
                message="unsupported_backend",
            )
            return failed

        command = self._virsh_argv(args=["suspend", name])

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=name,
            )
            return dry

        existing = self._read_dominfo(name=name)
        if existing is not None and existing.state == UbuntuVmState.PAUSED:
            skipped = self._build_result(
                status=UbuntuVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=name,
                message="VM {!r} is already paused".format(name),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command,
            vm_name=name,
        )
        return rtnval

    def resume_vm(self, name: str) -> UbuntuVirtualizationActionResult:
        """
            Resume a paused VM via ``virsh resume <name>``.

            Idempotent: when the VM is already running (i.e. not
            paused), returns ``SKIPPED``.

            :param name: Domain name.

            :returns: The :class:`UbuntuVirtualizationActionResult`.
        """
        if not self._backend_supported():
            failed = self._build_result(
                status=UbuntuVirtualizationActionStatus.FAILED,
                command=[],
                vm_name=name,
                message="unsupported_backend",
            )
            return failed

        command = self._virsh_argv(args=["resume", name])

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=name,
            )
            return dry

        existing = self._read_dominfo(name=name)
        if existing is not None and existing.state == UbuntuVmState.RUNNING:
            skipped = self._build_result(
                status=UbuntuVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=name,
                message="VM {!r} is already running".format(name),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command,
            vm_name=name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — disks
    # ------------------------------------------------------------------

    def attach_disk(
        self,
        vm: str,
        disk_spec: UbuntuVmDiskSpec,
    ) -> UbuntuVirtualizationActionResult:
        """
            Attach a disk to ``vm`` via ``virsh attach-disk <vm>
            <source> <target> --persistent``.

            Idempotency:
                The active VM XML (``virsh dumpxml <vm>``) is consulted;
                when a ``<source file=...>`` element already references
                ``disk_spec.path``, the verb returns ``SKIPPED``.

            :param vm: Domain name.
            :param disk_spec: Backing-file path plus optional target
                              label and bus.

            :returns: The :class:`UbuntuVirtualizationActionResult`.
        """
        if not self._backend_supported():
            failed = self._build_result(
                status=UbuntuVirtualizationActionStatus.FAILED,
                command=[],
                vm_name=vm,
                message="unsupported_backend",
            )
            return failed

        if disk_spec.target is None or len(disk_spec.target) == 0:
            target = "vdb"
        else:
            target = disk_spec.target

        attach_args = [
            "attach-disk", vm, disk_spec.path, target, "--persistent",
        ]
        if disk_spec.bus is not None and len(disk_spec.bus) > 0:
            attach_args.extend(["--targetbus", disk_spec.bus])
        command = self._virsh_argv(args=attach_args)

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=vm,
            )
            return dry

        if self._disk_path_in_xml(vm=vm, path=disk_spec.path):
            skipped = self._build_result(
                status=UbuntuVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=vm,
                message=(
                    "disk {!r} is already attached to VM {!r}".format(
                        disk_spec.path, vm,
                    )
                ),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command,
            vm_name=vm,
        )
        return rtnval

    def detach_disk(
        self,
        vm: str,
        disk_id: str,
    ) -> UbuntuVirtualizationActionResult:
        """
            Detach a disk from ``vm`` via ``virsh detach-disk <vm>
            <target> --persistent``.

            ``disk_id`` is the guest-side target label (``"vdb"``,
            ``"sdc"``).  This matches libvirt's
            ``virsh detach-disk`` argument shape; callers that hold the
            host-side path can pass it through without translation —
            libvirt accepts both.

            Idempotency:
                The active VM XML is consulted; when no
                ``<target dev=...>`` matching ``disk_id`` and no
                ``<source file=...>`` matching ``disk_id`` is present,
                the verb returns ``SKIPPED``.

            :param vm: Domain name.
            :param disk_id: Target label or backing-file path.

            :returns: The :class:`UbuntuVirtualizationActionResult`.
        """
        if not self._backend_supported():
            failed = self._build_result(
                status=UbuntuVirtualizationActionStatus.FAILED,
                command=[],
                vm_name=vm,
                message="unsupported_backend",
            )
            return failed

        command = self._virsh_argv(
            args=["detach-disk", vm, disk_id, "--persistent"],
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=vm,
            )
            return dry

        if not self._disk_attached(vm=vm, disk_id=disk_id):
            skipped = self._build_result(
                status=UbuntuVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=vm,
                message=(
                    "disk {!r} is not attached to VM {!r}".format(
                        disk_id, vm,
                    )
                ),
            )
            return skipped

        result = self._dispatch_virt_write(
            command=command,
            vm_name=vm,
        )
        if result.status == UbuntuVirtualizationActionStatus.SUCCESS:
            advisory = (
                "disk {!r} was detached from VM {!r}; the backing file "
                "was NOT removed".format(disk_id, vm)
            )
            result.message = advisory
            result.detail = advisory
        return result

    # ------------------------------------------------------------------
    # Write verbs — snapshots
    # ------------------------------------------------------------------

    def snapshot_vm(
        self,
        vm: str,
        snapshot_name: str,
    ) -> UbuntuVirtualizationActionResult:
        """
            Create a libvirt snapshot via ``virsh snapshot-create-as
            <vm> <snapshot_name>``.

            Idempotent: when a snapshot with the same name already
            exists for ``vm``, returns ``SKIPPED``.

            :param vm: Domain name.
            :param snapshot_name: Snapshot name.

            :returns: The :class:`UbuntuVirtualizationActionResult`.
        """
        if not self._backend_supported():
            failed = self._build_result(
                status=UbuntuVirtualizationActionStatus.FAILED,
                command=[],
                vm_name=vm,
                snapshot_name=snapshot_name,
                message="unsupported_backend",
            )
            return failed

        command = self._virsh_argv(
            args=["snapshot-create-as", vm, snapshot_name],
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=vm,
                snapshot_name=snapshot_name,
            )
            return dry

        if self._snapshot_exists(vm=vm, snapshot_name=snapshot_name):
            skipped = self._build_result(
                status=UbuntuVirtualizationActionStatus.SKIPPED,
                command=command,
                vm_name=vm,
                snapshot_name=snapshot_name,
                message=(
                    "snapshot {!r} already exists for VM {!r}".format(
                        snapshot_name, vm,
                    )
                ),
            )
            return skipped

        rtnval = self._dispatch_virt_write(
            command=command,
            vm_name=vm,
            snapshot_name=snapshot_name,
        )
        return rtnval

    def restore_snapshot(
        self,
        vm: str,
        snapshot_name: str,
    ) -> UbuntuVirtualizationActionResult:
        """
            Revert ``vm`` to ``snapshot_name`` via ``virsh
            snapshot-revert <vm> <snapshot_name>``.

            Unconditional: per the contract, the verb does NOT short-
            circuit on any matching state.  On ``SUCCESS`` ``message``
            carries a data-loss advisory.

            :param vm: Domain name.
            :param snapshot_name: Snapshot name.

            :returns: The :class:`UbuntuVirtualizationActionResult`.
        """
        if not self._backend_supported():
            failed = self._build_result(
                status=UbuntuVirtualizationActionStatus.FAILED,
                command=[],
                vm_name=vm,
                snapshot_name=snapshot_name,
                message="unsupported_backend",
            )
            return failed

        command = self._virsh_argv(
            args=["snapshot-revert", vm, snapshot_name],
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuVirtualizationActionStatus.DRY_RUN,
                command=command,
                vm_name=vm,
                snapshot_name=snapshot_name,
            )
            return dry

        result = self._dispatch_virt_write(
            command=command,
            vm_name=vm,
            snapshot_name=snapshot_name,
        )
        if result.status == UbuntuVirtualizationActionStatus.SUCCESS:
            advisory = (
                "VM {!r} was reverted to snapshot {!r}; any state "
                "written after the snapshot was discarded".format(
                    vm, snapshot_name,
                )
            )
            result.message = advisory
            result.detail = advisory
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> UbuntuVirtualizationManagerConfig:
        """
            Return ``self._config`` typed.  Falls back to a synthesised
            default when the caller passed a plain
            :class:`FeatureConfig`.
        """
        if isinstance(self._config, UbuntuVirtualizationManagerConfig):
            rtnval = self._config
        else:
            rtnval = UbuntuVirtualizationManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _backend_supported(self) -> bool:
        """
            Return True when the configured backend is supported.
        """
        backend = self._typed_config().backend
        rtnval = (backend == UbuntuVirtBackend.LIBVIRT)
        return rtnval

    def _virsh_argv(self, *, args: list) -> list:
        """
            Build the ``virsh`` argv prefix and tack on ``args``.

            The connection URI is appended via ``--connect <uri>``
            unless the configured URI is the empty string.

            :param args: Trailing argv segments.

            :returns: Full argv as a fresh list.
        """
        config = self._typed_config()
        prefix = [config.virsh_path]
        if len(config.default_connection) > 0:
            prefix.append("--connect")
            prefix.append(config.default_connection)
        rtnval = prefix + list(args)
        return rtnval

    def _build_virt_install_argv(
        self,
        *,
        spec: UbuntuVmSpec,
    ) -> list:
        """
            Render the ``virt-install`` argv for ``spec``.

            :param spec: The VM spec.

            :returns: The full argv list.
        """
        config = self._typed_config()
        argv = [config.virt_install_path]
        if len(config.default_connection) > 0:
            argv.append("--connect")
            argv.append(config.default_connection)

        argv.append("--name")
        argv.append(spec.name)
        argv.append("--memory")
        argv.append(str(spec.memory_mb))
        argv.append("--vcpus")
        argv.append(str(spec.cpu_count))

        disk_arg = _build_disk_argument(spec=spec)
        if disk_arg is not None:
            argv.append("--disk")
            argv.append(disk_arg)

        if spec.os_variant is not None and len(spec.os_variant) > 0:
            argv.append("--os-variant")
            argv.append(spec.os_variant)

        if spec.network is not None and len(spec.network) > 0:
            argv.append("--network")
            argv.append(spec.network)

        if spec.iso_path is not None and len(spec.iso_path) > 0:
            argv.append("--cdrom")
            argv.append(spec.iso_path)

        argv.append("--graphics")
        argv.append(spec.graphics)

        argv.append("--noautoconsole")

        rtnval = argv
        return rtnval

    def _read_dominfo(
        self,
        *,
        name: str,
        raise_on_unknown: bool = False,
    ) -> Optional[UbuntuVmInfo]:
        """
            Probe ``virsh dominfo <name>`` and parse the output into a
            :class:`UbuntuVmInfo`.

            :param name: Domain name.
            :param raise_on_unknown: When True, unexpected errors raise
                                     :class:`UbuntuVirtualizationQueryError`.
                                     The "domain not found" stderr is
                                     always treated as ``None``.

            :returns: The parsed :class:`UbuntuVmInfo`, or ``None``.
        """
        command = self._virsh_argv(args=["dominfo", name])
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            if _stderr_looks_unknown_domain(stderr=run_result.stderr or ""):
                rtnval: Optional[UbuntuVmInfo] = None
                return rtnval
            if raise_on_unknown:
                raise UbuntuVirtualizationQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            rtnval = None
            return rtnval

        rtnval = _parse_virsh_dominfo(
            stdout=run_result.stdout, name=name,
        )
        return rtnval

    def _disk_path_in_xml(self, *, vm: str, path: str) -> bool:
        """
            Return True when ``virsh dumpxml <vm>`` contains a
            ``<source file=...>`` element matching ``path``.
        """
        command = self._virsh_argv(args=["dumpxml", vm])
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        rtnval = _xml_contains_source_file(
            xml_text=run_result.stdout, path=path,
        )
        return rtnval

    def _disk_attached(self, *, vm: str, disk_id: str) -> bool:
        """
            Return True when ``disk_id`` matches either a
            ``<target dev=...>`` or a ``<source file=...>`` value in the
            VM's active XML.
        """
        command = self._virsh_argv(args=["dumpxml", vm])
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        rtnval = _xml_contains_disk_id(
            xml_text=run_result.stdout, disk_id=disk_id,
        )
        return rtnval

    def _snapshot_exists(
        self,
        *,
        vm: str,
        snapshot_name: str,
    ) -> bool:
        """
            Return True when ``virsh snapshot-list <vm>`` reports a
            snapshot whose name matches ``snapshot_name``.
        """
        command = self._virsh_argv(args=["snapshot-list", vm])
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        snapshots = _parse_virsh_snapshot_list(stdout=run_result.stdout)
        rtnval = False
        for entry in snapshots:
            if entry.name == snapshot_name:
                rtnval = True
                break
        return rtnval

    def _dispatch_virt_write(
        self,
        *,
        command: list,
        vm_name: Optional[str] = None,
        snapshot_name: Optional[str] = None,
    ) -> UbuntuVirtualizationActionResult:
        """
            Dispatch a write command and wrap the result with the
            virtualization-scope fields.
        """
        rtnval = self._dispatch(
            command=command,
            detail=vm_name,
            operation="virtualization_write",
            vm_name=vm_name,
            snapshot_name=snapshot_name,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuVirtualizationActionStatus,
        command: list,
        vm_name: Optional[str] = None,
        snapshot_name: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuVirtualizationActionResult:
        """
            Construct an :class:`UbuntuVirtualizationActionResult`
            without invoking the runner.
        """
        if message is not None:
            detail_value: Optional[str] = message
        else:
            detail_value = vm_name
        rtnval = UbuntuVirtualizationActionResult(
            status=status,
            detail=detail_value,
            command=list(command),
            vm_name=vm_name,
            snapshot_name=snapshot_name,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers / helpers
# ----------------------------------------------------------------------


def _build_disk_argument(*, spec: UbuntuVmSpec) -> Optional[str]:
    """
        Render the value for ``virt-install --disk`` from a spec.

        Preference order:
            1. ``disk_path`` is present -> ``path=<disk_path>``.
            2. ``disk_size_gb`` is present -> ``size=<gb>``.
            3. Neither -> ``None``.

        :param spec: The VM spec.

        :returns: The argument string, or ``None``.
    """
    if spec.disk_path is not None and len(spec.disk_path) > 0:
        rtnval: Optional[str] = "path={}".format(spec.disk_path)
        return rtnval
    if spec.disk_size_gb is not None and spec.disk_size_gb > 0:
        rtnval = "size={}".format(spec.disk_size_gb)
        return rtnval
    rtnval = None
    return rtnval


def _stderr_looks_unknown_domain(*, stderr: str) -> bool:
    """
        Heuristically detect a libvirt "domain not found" stderr.
    """
    lower = stderr.lower()
    if "failed to get domain" in lower:
        rtnval = True
        return rtnval
    if "domain not found" in lower:
        rtnval = True
        return rtnval
    if "no such domain" in lower:
        rtnval = True
        return rtnval
    rtnval = False
    return rtnval


def _normalise_libvirt_state(*, state_text: str) -> UbuntuVmState:
    """
        Map a raw libvirt domain-state string onto :class:`UbuntuVmState`.

        :param state_text: The raw state text (case-insensitive).

        :returns: The normalised state.
    """
    text = state_text.strip().lower()
    if text == _LIBVIRT_RUNNING:
        rtnval = UbuntuVmState.RUNNING
        return rtnval
    if text in (
        _LIBVIRT_SHUT_OFF,
        _LIBVIRT_SHUT_OFF_NOSPACE,
        _LIBVIRT_SHUTOFF_COLLAPSED,
    ):
        rtnval = UbuntuVmState.STOPPED
        return rtnval
    if text == _LIBVIRT_PAUSED:
        rtnval = UbuntuVmState.PAUSED
        return rtnval
    if text == _LIBVIRT_PMSUSPENDED:
        rtnval = UbuntuVmState.SUSPENDED
        return rtnval
    rtnval = UbuntuVmState.OTHER
    return rtnval


def _parse_virsh_list_names(*, stdout: str) -> list:
    """
        Parse ``virsh list --all --name`` output into a list of names.

        ``--name`` emits one name per line, terminated by an empty line
        and possibly preceded by header noise.  Any blank line is
        ignored.

        :param stdout: Raw stdout text.

        :returns: A list of domain names in the order reported.
    """
    rtnval: list = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        rtnval.append(line)
    return rtnval


def _parse_virsh_dominfo(
    *,
    stdout: str,
    name: str,
) -> Optional[UbuntuVmInfo]:
    """
        Parse ``virsh dominfo`` output into :class:`UbuntuVmInfo`.

        ``dominfo`` emits ``Key: value`` lines.  We extract the
        relevant subset:

            * ``Name:`` (echoed back; we trust the caller's ``name``
              when the parsed value is empty).
            * ``UUID:``
            * ``OS Type:``
            * ``State:``
            * ``CPU(s):``
            * ``Max memory:``  (always reported in KiB by libvirt).

        :param stdout: Raw stdout text.
        :param name: Caller-supplied name used when the parsed Name is
                     missing.

        :returns: The :class:`UbuntuVmInfo`.  Returns ``None`` only when
                  the output is empty (defensive guard).
    """
    stripped = stdout.strip()
    if len(stripped) == 0:
        rtnval: Optional[UbuntuVmInfo] = None
        return rtnval

    fields: dict = {}
    for raw in stdout.splitlines():
        line = raw.rstrip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()

    # Prefer the caller-supplied name as the canonical identifier;
    # libvirt's ``Name:`` field echoes the same value, but enumeration
    # paths feed each VM's name through here so the result must reflect
    # the caller's lookup key.  The parsed value is only used when the
    # caller passed an empty string (defensive guard).
    if name is not None and len(name) > 0:
        info_name = name
    else:
        info_name = fields.get("Name", "")

    state_text = fields.get("State", "")
    state = _normalise_libvirt_state(state_text=state_text)

    cpu_text = fields.get("CPU(s)", "")
    if len(cpu_text) == 0:
        cpu_count: Optional[int] = None
    else:
        try:
            cpu_count = int(cpu_text)
        except ValueError:
            cpu_count = None

    memory_text = fields.get("Max memory", "")
    memory_mb: Optional[int] = None
    if len(memory_text) > 0:
        # ``Max memory`` is reported as e.g. "1048576 KiB".  Trim the
        # unit and convert to MiB.
        tokens = memory_text.split()
        if len(tokens) > 0:
            try:
                kib_value = int(tokens[0])
                memory_mb = int(kib_value / 1024)
            except ValueError:
                memory_mb = None

    os_type_raw = fields.get("OS Type", "")
    if len(os_type_raw) == 0:
        os_type: Optional[str] = None
    else:
        os_type = os_type_raw

    uuid_raw = fields.get("UUID", "")
    if len(uuid_raw) == 0:
        vm_id: Optional[str] = None
    else:
        vm_id = uuid_raw

    rtnval = UbuntuVmInfo(
        name=info_name,
        state=state,
        cpu_count=cpu_count,
        memory_mb=memory_mb,
        uptime_seconds=None,
        os_type=os_type,
        vm_id=vm_id,
    )
    return rtnval


def _parse_virsh_snapshot_list(*, stdout: str) -> list:
    """
        Parse ``virsh snapshot-list <vm>`` tabular output.

        The default output shape is a header line, a separator dash
        line, and one row per snapshot:

            Name                 Creation Time             State
            ------------------------------------------------------------
            snap1                2024-01-01 12:00:00 +0000 running
            snap2                2024-01-02 13:00:00 +0000 shutoff

        We split each row on whitespace and reconstruct the timestamp
        from the middle tokens because libvirt's timestamp format is
        deterministic (date, time, timezone).

        :param stdout: Raw stdout text.

        :returns: One :class:`UbuntuSnapshotInfo` per parsed row.
    """
    rtnval: list = []
    saw_separator = False
    for raw in stdout.splitlines():
        line = raw.rstrip()
        if len(line.strip()) == 0:
            continue
        if line.lstrip().startswith("---"):
            saw_separator = True
            continue
        if not saw_separator:
            # Header row.
            continue

        tokens = line.split()
        if len(tokens) < 2:
            continue

        snapshot_name = tokens[0]
        # The state is always the trailing token (``running``,
        # ``shutoff``, ``paused``, ``no-state``).  Everything between
        # the name and the state belongs to the timestamp.
        state_token = tokens[-1]
        timestamp_tokens = tokens[1:-1]
        if len(timestamp_tokens) == 0:
            timestamp = ""
        else:
            timestamp = " ".join(timestamp_tokens)
        if timestamp == "-":
            timestamp = ""

        if state_token in ("-", ""):
            vm_state: Optional[UbuntuVmState] = None
        else:
            vm_state = _normalise_libvirt_state(state_text=state_token)

        info = UbuntuSnapshotInfo(
            name=snapshot_name,
            created=timestamp,
            description=None,
            parent=None,
            vm_state=vm_state,
        )
        rtnval.append(info)
    return rtnval


def _xml_contains_source_file(*, xml_text: str, path: str) -> bool:
    """
        Return True when ``xml_text`` contains
        ``<source file="<path>"/>`` (with either single or double
        quotes).  This is a string-level probe rather than a real XML
        parse to avoid an unnecessary stdlib import in this module's
        runtime path.
    """
    if path is None or len(path) == 0:
        rtnval = False
        return rtnval
    needles = (
        'source file="{}"'.format(path),
        "source file='{}'".format(path),
    )
    rtnval = False
    for needle in needles:
        if needle in xml_text:
            rtnval = True
            break
    return rtnval


def _xml_contains_disk_id(*, xml_text: str, disk_id: str) -> bool:
    """
        Return True when ``xml_text`` carries a ``<source file=...>``
        or ``<target dev=...>`` whose value equals ``disk_id``.
    """
    if disk_id is None or len(disk_id) == 0:
        rtnval = False
        return rtnval
    needles = (
        'source file="{}"'.format(disk_id),
        "source file='{}'".format(disk_id),
        'target dev="{}"'.format(disk_id),
        "target dev='{}'".format(disk_id),
    )
    rtnval = False
    for needle in needles:
        if needle in xml_text:
            rtnval = True
            break
    return rtnval
