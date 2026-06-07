# Requirements

## Introduction

The virtualization-management feature provides the
`codecraftai-sysman-linux-ubuntu` package with the ability to manage local
KVM virtual machines on Ubuntu hosts via libvirt's user-space
toolchain (`virsh`, `virt-install`).  This spec implements the
cross-OS virtualization-management contract defined at
`codecraftai-sysman-common/specs/contracts/virtualization-management/contract.md`
for Ubuntu.

A maintenance coordinator running in an external package drives every
operation by calling into this feature's public interfaces.  The
feature does not decide *which* VMs should exist, *what shape* their
disks should take, or *when* snapshots should be created — it provides
the capability to manipulate libvirt domain state reliably,
idempotently, and with full auditability.  All system commands are
executed exclusively through the `codecraftai-interfaces` command-runner
abstraction.

Apple's Virtualization framework, HyperKit, Hyper-V, and any
cloud-provider hypervisor surface are NOT in scope here — they live in
the macOS, Windows, and (future) cloud sysman packages respectively.
This feature stops at the libvirt-domain boundary: VM lifecycle,
disk attach / detach, and snapshot create / revert / list.  Network
and storage-pool definition (the libvirt-network and libvirt-pool
surfaces beyond an attached path) is intentionally excluded — it
either lives in `firewall-management` (network) or in
`storage-management` (LVM-backed pools).

---

## Requirements

### Requirement 1: Backend Selection

**User Story:** As a maintenance coordinator, I want the manager to
expose an explicit virtualization-backend selector, so that future
backends (LXC, LXD) can plug in without changing the public verb
surface.

#### Acceptance Criteria

1. THE SYSTEM SHALL expose `UbuntuVirtBackend` as an `Enum` with
   `LIBVIRT` (default), `LXC`, and `LXD` members.
2. THE SYSTEM SHALL store the selected backend on
   `UbuntuVirtualizationManagerConfig.backend`.
3. WHEN the configured backend is anything other than `LIBVIRT` THE
   SYSTEM SHALL return `UbuntuVirtualizationActionResult(status=FAILED,
   message="unsupported_backend")` from every verb without invoking
   the runner.

---

### Requirement 2: Connection URI

**User Story:** As a maintenance coordinator, I want every `virsh`
invocation to carry the libvirt connection URI, so that operations
target the intended hypervisor connection (`qemu:///system` by
default).

#### Acceptance Criteria

1. THE SYSTEM SHALL default
   `UbuntuVirtualizationManagerConfig.default_connection` to
   `"qemu:///system"`.
2. WHEN dispatching a `virsh` command THE SYSTEM SHALL prepend
   `--connect <uri>` to the argv when the configured URI is non-empty.
3. WHEN the configured URI is the empty string THE SYSTEM SHALL omit
   the `--connect` flag entirely so libvirt's own URI-resolution rules
   apply.
4. THE SYSTEM SHALL do the same on `virt-install` invocations, mapping
   the URI onto the `--connect <uri>` flag.

---

### Requirement 3: List VMs

**User Story:** As a maintenance coordinator, I want to enumerate
every defined VM with its power state, vCPU count, and memory, so that
I can compare actual VM posture against the declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_vms()` THE SYSTEM SHALL dispatch
   `virsh --connect <uri> list --all --name` through the runner with
   `elevated=False`.
2. THE SYSTEM SHALL parse the line-per-name output into a list of VM
   names.
3. FOR each name THE SYSTEM SHALL dispatch `virsh dominfo <name>` and
   merge the parsed fields into an `UbuntuVmInfo`.
4. THE SYSTEM SHALL map libvirt's reported State string onto
   `UbuntuVmState` (`running`, `shut off`, `paused`, `pmsuspended` ->
   `RUNNING` / `STOPPED` / `PAUSED` / `SUSPENDED`; everything else ->
   `OTHER`).
5. WHEN `virsh list` exits non-zero THE SYSTEM SHALL raise
   `UbuntuVirtualizationQueryError` carrying the failed argv,
   `exit_code`, and `stderr`.

---

### Requirement 4: Get VM Info

**User Story:** As a maintenance coordinator, I want to look up a
single VM by name, so that I can branch on its power state and
configuration without parsing a full enumeration.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_vm_info(name)` THE SYSTEM SHALL
   dispatch `virsh dominfo <name>` with `elevated=False`.
2. WHEN the VM is not defined THE SYSTEM SHALL return `None`.  The
   runner's "domain not found" stderr is recognised heuristically
   (`failed to get domain`, `domain not found`, `no such domain`).
3. WHEN `virsh dominfo` fails for any other reason THE SYSTEM SHALL
   raise `UbuntuVirtualizationQueryError`.

---

### Requirement 5: Create VM

**User Story:** As a maintenance coordinator, I want to provision a
fresh KVM VM from a spec, so that bringing a new guest online is a
single call.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_vm(spec)` THE SYSTEM SHALL build
   the `virt-install` argv from `spec` (`--name`, `--memory`,
   `--vcpus`, `--disk`, optional `--os-variant`, optional `--network`,
   optional `--cdrom`, `--graphics <value>`, `--noautoconsole`).
2. WHEN `spec.disk_path` is set THE SYSTEM SHALL pass
   `--disk path=<disk_path>`; otherwise WHEN `spec.disk_size_gb` is set
   THE SYSTEM SHALL pass `--disk size=<gb>`; otherwise THE SYSTEM SHALL
   omit the `--disk` flag entirely.
3. WHEN a VM with `spec.name` already exists THE SYSTEM SHALL return
   `SKIPPED` without dispatching `virt-install`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `DRY_RUN` carrying the
   would-have-been argv.
5. WHEN the runner reports a non-zero exit THE SYSTEM SHALL return
   `FAILED` carrying `exit_code`, `stdout`, and `stderr`.

---

### Requirement 6: Delete VM

**User Story:** As a maintenance coordinator, I want to undefine a VM
and its backing storage, so that decommissioning a guest is a single
call.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_vm(name)` THE SYSTEM SHALL
   dispatch `virsh undefine <name> --remove-all-storage`.
2. WHEN no VM with `name` is defined THE SYSTEM SHALL return `SKIPPED`
   without dispatching.
3. WHEN the operation succeeds THE SYSTEM SHALL populate
   `UbuntuVirtualizationActionResult.message` with a data-loss advisory
   naming the destroyed VM and noting that
   `--remove-all-storage` ran.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `DRY_RUN`.

---

### Requirement 7: Power-State Verbs

**User Story:** As a maintenance coordinator, I want to start, stop,
pause, and resume VMs, so that VM runtime state matches the declared
desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `start_vm(name)` THE SYSTEM SHALL
   dispatch `virsh start <name>`; SKIPPED when the VM's state is
   already `RUNNING`.
2. WHEN the coordinator calls `stop_vm(name, force=False)` THE SYSTEM
   SHALL dispatch `virsh shutdown <name>`; SKIPPED when the VM is
   `STOPPED`.
3. WHEN the coordinator calls `stop_vm(name, force=True)` THE SYSTEM
   SHALL dispatch `virsh destroy <name>`; SKIPPED when the VM is
   `STOPPED`.
4. WHEN the coordinator calls `pause_vm(name)` THE SYSTEM SHALL
   dispatch `virsh suspend <name>`; SKIPPED when the VM is `PAUSED`.
5. WHEN the coordinator calls `resume_vm(name)` THE SYSTEM SHALL
   dispatch `virsh resume <name>`; SKIPPED when the VM is `RUNNING`
   (i.e. not paused).
6. WHEN `dry_run=True` for any of these verbs THE SYSTEM SHALL return
   `DRY_RUN` and not invoke the runner.

---

### Requirement 8: Attach Disk

**User Story:** As a maintenance coordinator, I want to attach an
existing disk image to a VM persistently, so that capacity follows
the workload.

#### Acceptance Criteria

1. WHEN the coordinator calls `attach_disk(vm, disk_spec)` THE SYSTEM
   SHALL dispatch `virsh attach-disk <vm> <disk_spec.path>
   <target> --persistent` (target defaults to `"vdb"` when not
   supplied).
2. WHEN `disk_spec.bus` is set THE SYSTEM SHALL append
   `--targetbus <bus>` to the argv.
3. WHEN the active VM XML (`virsh dumpxml <vm>`) already references
   `disk_spec.path` in a `<source file=...>` element THE SYSTEM SHALL
   return `SKIPPED` without dispatching `attach-disk`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `DRY_RUN`.

---

### Requirement 9: Detach Disk

**User Story:** As a maintenance coordinator, I want to detach a disk
from a VM persistently, so that the backing image can be reused or
archived elsewhere.

#### Acceptance Criteria

1. WHEN the coordinator calls `detach_disk(vm, disk_id)` THE SYSTEM
   SHALL dispatch `virsh detach-disk <vm> <disk_id> --persistent`
   (libvirt accepts either the target label or the host-side path).
2. WHEN no `<target dev=...>` and no `<source file=...>` matching
   `disk_id` is present in the active VM XML THE SYSTEM SHALL return
   `SKIPPED` without dispatching `detach-disk`.
3. WHEN the operation succeeds THE SYSTEM SHALL populate
   `UbuntuVirtualizationActionResult.message` with a data-loss advisory
   noting that the backing file itself was NOT removed.

---

### Requirement 10: Snapshot Lifecycle

**User Story:** As a maintenance coordinator, I want to create, list,
and revert snapshots on a VM, so that pre-change checkpoints are
available.

#### Acceptance Criteria

1. WHEN the coordinator calls `snapshot_vm(vm, snapshot_name)` THE
   SYSTEM SHALL dispatch
   `virsh snapshot-create-as <vm> <snapshot_name>`.
2. WHEN a snapshot with that name already exists for `vm` (probed via
   `virsh snapshot-list <vm>`) THE SYSTEM SHALL return `SKIPPED`.
3. WHEN the coordinator calls `restore_snapshot(vm, snapshot_name)`
   THE SYSTEM SHALL dispatch
   `virsh snapshot-revert <vm> <snapshot_name>` UNCONDITIONALLY (no
   idempotency guard) and SHALL populate `message` with a data-loss
   advisory on `SUCCESS`.
4. WHEN the coordinator calls `list_snapshots(vm)` THE SYSTEM SHALL
   dispatch `virsh snapshot-list <vm>` and parse the tabular output
   into `list[UbuntuSnapshotInfo]`.

---

### Requirement 11: Elevation Policy

**User Story:** As a maintenance coordinator, I want every mutating
libvirt operation to run with root privilege via `sudo`, so that
`virsh` and `virt-install` succeed on a standard system where the
process is not in the `libvirt` group.

#### Acceptance Criteria

1. WHEN the manager is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the runner for every write verb
   (`create_vm`, `delete_vm`, `start_vm`, `stop_vm`, `pause_vm`,
   `resume_vm`, `attach_disk`, `detach_disk`, `snapshot_vm`,
   `restore_snapshot`).
2. WHEN performing read verbs (`list_vms`, `get_vm_info`,
   `list_snapshots`, every idempotency probe) THE SYSTEM SHALL NEVER
   pass `elevated=True`.
3. WHEN the manager is configured with `elevated=False` THE SYSTEM
   SHALL NEVER pass `elevated=True`.

---

### Requirement 12: Dry-Run, Failure Reporting, and Result Shape

**User Story:** As a maintenance coordinator, I want every mutating
operation to respect dry-run, surface failures structurally, and carry
the targeted VM / snapshot name in the result, so that the audit trail
is unambiguous.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch any
   mutating command and SHALL return
   `UbuntuVirtualizationActionResult(status=DRY_RUN)` carrying the
   would-have-been argv.
2. WHEN the runner returns non-zero THE SYSTEM SHALL return
   `UbuntuVirtualizationActionResult(status=FAILED, exit_code=<code>,
   stdout=<captured>, stderr=<captured>)`.
3. THE SYSTEM SHALL populate `vm_name` on every result and
   `snapshot_name` on snapshot-related results.
4. THE SYSTEM SHALL store the data-loss advisory in `message` and echo
   it onto `detail` for `delete_vm`, `detach_disk`, and
   `restore_snapshot` `SUCCESS` outcomes.
