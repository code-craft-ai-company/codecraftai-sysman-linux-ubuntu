# Design

## Concept Mapping (KVM / libvirt vs Hyper-V vs Apple Virtualization)

The cross-OS virtualization-management contract talks about *VMs*,
*disks*, and *snapshots*.  Each OS's native hypervisor stack carries
a different vocabulary; the table below grounds the abstract contract
in concrete libvirt / Hyper-V / Apple-Virtualization terms so a
reader of this spec can compare all three implementations
side-by-side.

| Contract concept | Ubuntu (this spec) | macOS (libvirt-via-Homebrew) | Windows (Hyper-V) | Apple Virtualization (out of scope) |
|---|---|---|---|---|
| VM | libvirt **domain** (`virsh`) | libvirt **domain** (Homebrew QEMU/libvirt) | Hyper-V VM (`Get-VM`, `New-VM`) | `VZVirtualMachine` (Swift only) |
| VM identifier | Domain name | Domain name | VM name | Object reference |
| Configured memory | `Max memory` field of `virsh dominfo` (KiB) | Same | `MemoryAssigned` from `Get-VM` (bytes) | `VZVirtualMachineConfiguration.memorySize` |
| Power state | `running` / `shut off` / `paused` / `pmsuspended` | Same | `Running` / `Off` / `Paused` / `Saved` | `state` enum |
| Create | `virt-install --name --memory --vcpus --disk ...` | Same | `New-VM -Name ... -MemoryStartupBytes ...` | `start()` after configuration |
| Start | `virsh start` | `virsh start` | `Start-VM` | `start()` |
| Graceful stop | `virsh shutdown` | `virsh shutdown` | `Stop-VM` (default) | `requestStop()` |
| Force stop | `virsh destroy` | `virsh destroy` | `Stop-VM -Force -TurnOff` | `stop()` |
| Pause | `virsh suspend` | `virsh suspend` | `Suspend-VM` | `pause()` |
| Resume | `virsh resume` | `virsh resume` | `Resume-VM` | `resume()` |
| Delete | `virsh undefine --remove-all-storage` | Same | `Stop-VM -Force; Remove-VM -Force` | n/a |
| Attach disk | `virsh attach-disk --persistent` | `virsh attach-disk --persistent` | `Add-VMHardDiskDrive` | `addStorageDevice` (config-time) |
| Detach disk | `virsh detach-disk --persistent` | Same | `Remove-VMHardDiskDrive` | n/a |
| Snapshot create | `virsh snapshot-create-as` | Same | `Checkpoint-VM` | n/a |
| Snapshot restore | `virsh snapshot-revert` | Same | `Restore-VMSnapshot -Confirm:$false` | n/a |
| Snapshot list | `virsh snapshot-list` | Same | `Get-VMSnapshot` | n/a |
| Required runtime | libvirt + qemu-kvm + libvirt group membership or root | Homebrew libvirt + libvirtd | Hyper-V role | macOS 11+ |

The Ubuntu implementation is the canonical libvirt branch; the macOS
spec lives in `codecraftai-sysman-macos/specs/3-virtualization-management/`
and reuses the same verbs through Homebrew-installed libvirt.  The
Windows spec lives in
`codecraftai-sysman-windows/specs/3-virtualization-management/` and
re-targets the same verbs onto the Hyper-V PowerShell module.

Apple's Virtualization framework is **explicitly out of scope** —
its API is Swift-only and binding it from Python via `PyObjC` would
introduce a runtime dependency that does not match the rest of this
package.  Callers needing Apple Virtualization should use a separate,
Swift-driven package.

---

## Overview

The virtualization-management feature is implemented as a single
module,
`ccai.sysman.linux.ubuntu.virtualizationmanagement`, that exposes
an `UbuntuVirtualizationManager` class.  The coordinator instantiates
`UbuntuVirtualizationManager` and calls its methods to perform
discrete VM-lifecycle, disk, and snapshot actions.

This feature **complements — and does not duplicate** — the storage
manager in `3-storage-management` (which manages LVM Volume Groups
that may back libvirt storage pools), the firewall manager in
`3-firewall-management` (which opens ports for libvirt's virtual
networks), and the service manager in `3-service-management` (which
brings `libvirtd.service` up before any verb here is called).

All command execution is delegated to a command runner implementing
the `ICommandRunner` interface.  The `UbuntuVirtualizationManager`
never directly calls `subprocess`, shell commands, or `open()`; it
only constructs argument lists and hands them to the injected runner.
This keeps business logic separate from execution and makes the
feature fully testable via mock dependencies without root access or
a live libvirt daemon.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).
Under SSH the coordinator can track reliability metrics and apply
execution aspects (persistence, retry, timeout) without any changes
to the `UbuntuVirtualizationManager` code.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into UbuntuVirtualizationManager          │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        UbuntuVirtualizationManager                          │
│  ccai.sysman.linux.ubuntu.virtualizationmanagement      │
│                                                             │
│  list_vms()              get_vm_info()                      │
│  create_vm()             delete_vm()                        │
│  start_vm()              stop_vm()                          │
│  pause_vm()              resume_vm()                        │
│  attach_disk()           detach_disk()                      │
│  snapshot_vm()           restore_snapshot()                 │
│  list_snapshots()                                           │
└─────────┬───────────────────────────────────────────────────┘
          │ ICommandRunner
          ▼
┌─────────────────────────────────────────────────────────────┐
│           LocalCommandRunner / SSHCommandRunner             │
└─────────┬───────────────────────────────────────────────────┘
          │ optional aspect composition
          ▼
┌─────────────────────────────────────────────────────────────┐
│   PersistenceAspect  RetryAspect  TimeoutAspect             │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `UbuntuVirtualizationManager`

**Location:**
`source/packages/ccai/sysman/linux/ubuntu/virtualizationmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `list_vms` | `() → list[UbuntuVmInfo]` | `virsh list --all --name` + per-name `virsh dominfo` |
| `get_vm_info` | `(name) → UbuntuVmInfo \| None` | `virsh dominfo <name>` parsed; `None` for unknown |
| `create_vm` | `(spec) → UbuntuVirtualizationActionResult` | `virt-install` with rendered argv |
| `delete_vm` | `(name) → UbuntuVirtualizationActionResult` | `virsh undefine <name> --remove-all-storage` with data-loss advisory |
| `start_vm` | `(name) → UbuntuVirtualizationActionResult` | `virsh start <name>` |
| `stop_vm` | `(name, force=False) → UbuntuVirtualizationActionResult` | `virsh shutdown` (graceful) or `virsh destroy` (force) |
| `pause_vm` | `(name) → UbuntuVirtualizationActionResult` | `virsh suspend <name>` |
| `resume_vm` | `(name) → UbuntuVirtualizationActionResult` | `virsh resume <name>` |
| `attach_disk` | `(vm, disk_spec) → UbuntuVirtualizationActionResult` | `virsh attach-disk <vm> <path> <target> --persistent [--targetbus]` |
| `detach_disk` | `(vm, disk_id) → UbuntuVirtualizationActionResult` | `virsh detach-disk <vm> <disk_id> --persistent` with data-loss advisory |
| `snapshot_vm` | `(vm, snapshot_name) → UbuntuVirtualizationActionResult` | `virsh snapshot-create-as <vm> <name>` |
| `restore_snapshot` | `(vm, snapshot_name) → UbuntuVirtualizationActionResult` | `virsh snapshot-revert <vm> <name>` (unconditional) with data-loss advisory |
| `list_snapshots` | `(vm) → list[UbuntuSnapshotInfo]` | `virsh snapshot-list <vm>` parsed |

### `UbuntuVirtualizationManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no mutating command is dispatched |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `backend` | `UbuntuVirtBackend` | `LIBVIRT` | Selector for the virtualization backend |
| `virsh_path` | `str` | `"virsh"` | PATH-relative or absolute name of the `virsh` binary |
| `virt_install_path` | `str` | `"virt-install"` | PATH-relative or absolute name of `virt-install` |
| `default_connection` | `str` | `"qemu:///system"` | libvirt URI passed via `--connect`; empty string suppresses the flag |

---

## Data Models

### Result Types

```python
class UbuntuVirtualizationActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class UbuntuVirtualizationActionResult(FeatureActionResult):
    vm_name: str | None = None
    snapshot_name: str | None = None
    message: str | None = None
```

### Lifecycle Types

```python
class UbuntuVirtBackend(Enum):
    LIBVIRT = "libvirt"
    LXC     = "lxc"
    LXD     = "lxd"

class UbuntuVmState(Enum):
    RUNNING   = "running"
    STOPPED   = "stopped"
    PAUSED    = "paused"
    SUSPENDED = "suspended"
    OTHER     = "other"

@dataclass
class UbuntuVmSpec:
    name: str
    cpu_count: int = 1
    memory_mb: int = 1024
    disk_size_gb: int | None = None
    disk_path: str | None = None
    os_variant: str | None = None
    network: str | None = None
    iso_path: str | None = None
    graphics: str = "none"
    boot_kind: int = 1            # cross-OS-spec parity (Windows generation)

@dataclass
class UbuntuVmInfo:
    name: str
    state: UbuntuVmState
    cpu_count: int | None = None
    memory_mb: int | None = None
    uptime_seconds: int | None = None
    os_type: str | None = None
    vm_id: str | None = None

@dataclass
class UbuntuVmDiskSpec:
    path: str
    target: str | None = None
    controller_type: str | None = None
    bus: str | None = None

@dataclass
class UbuntuSnapshotInfo:
    name: str
    created: str
    description: str | None = None
    parent: str | None = None
    vm_state: UbuntuVmState | None = None
```

### Exceptions

```python
class UbuntuVirtualizationQueryError(Exception):
    """Raised when a virtualization read fails for an unexpected reason."""
```

---

## Command Construction

### `virsh` argv prefix

```python
def _virsh_argv(self, *, args: list) -> list:
    config = self._typed_config()
    prefix = [config.virsh_path]
    if len(config.default_connection) > 0:
        prefix.append("--connect")
        prefix.append(config.default_connection)
    rtnval = prefix + list(args)
    return rtnval
```

### `virt-install` argv

```python
argv = [config.virt_install_path]
if config.default_connection:
    argv += ["--connect", config.default_connection]
argv += ["--name", spec.name]
argv += ["--memory", str(spec.memory_mb)]
argv += ["--vcpus", str(spec.cpu_count)]
disk = _build_disk_argument(spec=spec)         # "path=..." or "size=..."
if disk is not None:
    argv += ["--disk", disk]
if spec.os_variant:
    argv += ["--os-variant", spec.os_variant]
if spec.network:
    argv += ["--network", spec.network]
if spec.iso_path:
    argv += ["--cdrom", spec.iso_path]
argv += ["--graphics", spec.graphics]
argv += ["--noautoconsole"]
```

`--noautoconsole` is mandatory: without it `virt-install` blocks
waiting for the serial console, which is incompatible with the
fire-and-forget contract.

### Disk argument resolution (`_build_disk_argument`)

| Field | Effect on `--disk` |
|---|---|
| `disk_path` is set | `path=<disk_path>` |
| `disk_path` is None and `disk_size_gb` is set | `size=<gb>` |
| Both are None | `--disk` is omitted entirely |

This matches the `virt-install` flag grammar precisely.

---

## Idempotency Probes

| Verb | Probe | SKIPPED condition |
|---|---|---|
| `create_vm` | `virsh dominfo <name>` | Domain exists |
| `delete_vm` | `virsh dominfo <name>` | Domain does not exist |
| `start_vm` | `virsh dominfo <name>` | `state == RUNNING` |
| `stop_vm` | `virsh dominfo <name>` | `state == STOPPED` |
| `pause_vm` | `virsh dominfo <name>` | `state == PAUSED` |
| `resume_vm` | `virsh dominfo <name>` | `state == RUNNING` |
| `attach_disk` | `virsh dumpxml <vm>` | `<source file="<path>"/>` present |
| `detach_disk` | `virsh dumpxml <vm>` | No `<target dev=...>` and no `<source file=...>` matches `disk_id` |
| `snapshot_vm` | `virsh snapshot-list <vm>` | A snapshot with the same name exists |
| `restore_snapshot` | — (always reverts) | Never |

The XML probes use string matching rather than a full XML parse to
keep the runtime path free of optional stdlib imports; the libvirt
serialiser deterministically uses single quotes, but we accept
double-quoted variants too for resilience.

---

## Stop Semantics

| `force` | Verb | Argv |
|---|---|---|
| `False` | Graceful | `virsh shutdown <name>` |
| `True` | Hard power-off | `virsh destroy <name>` |

`virsh destroy` is the libvirt convention for a hard power-off; the
persistent definition stays intact, matching the contract.  Callers
that want full removal must follow with `delete_vm`.

---

## Dry-Run Behaviour

Dry-run disables every mutating entry point and returns
`DRY_RUN` with the would-have-been argv recorded on the result.
Read verbs (`list_vms`, `get_vm_info`, `list_snapshots`) and the
idempotency probes (`virsh dominfo`, `virsh dumpxml`,
`virsh snapshot-list`) run freely so the result still reflects the
correct skipped/non-skipped branch when dry-run is later disabled
against the same host.

---

## Elevation Policy

Every write verb pulls `elevated` from the config; reads and
idempotency probes always pass `elevated=False`.  Libvirt typically
requires either root or membership in the `libvirt` group; both
situations are represented by `elevated=True` here.  The
`storage-management` and `service-management` features that compose
with this one follow the same policy.

---

## Correctness Properties

1. **Idempotency.**  Every lifecycle transition returns `SKIPPED`
   when the resource is already in the requested state.  The contract
   exempts `restore_snapshot` from this rule.
2. **No raises from writes.**  Failures from the runner surface as
   `FAILED` results with `exit_code`, `stdout`, `stderr`, and
   `command` populated.
3. **Typed read errors.**  `list_vms`, `get_vm_info`, and
   `list_snapshots` raise `UbuntuVirtualizationQueryError` for
   unexpected failures.  `get_vm_info` returns `None` for the
   well-defined "unknown VM" case.
4. **Unsupported backends fail fast.**  Every verb checks the
   configured backend and short-circuits with
   `FAILED message="unsupported_backend"` when it is not `LIBVIRT`.

---

## Testing Strategy

- `_MockCommandRunner` queues `RunResult` objects keyed by argv tuple
  and records every call.
- Unit tests per public method covering happy / SKIPPED / FAILED /
  DRY_RUN branches.
- Parser tests for `virsh list --all --name`, `virsh dominfo`, and
  `virsh snapshot-list`.
- XML-probe tests asserting the `<source file=...>` and
  `<target dev=...>` matchers behave as expected.
- Stop-semantics test asserting `force=True` and `force=False`
  dispatch different argv.
- Elevation tests asserting writes pass `elevated=True` and reads
  always pass `elevated=False` regardless of the config flag.
- Backend test asserting `LXC` / `LXD` short-circuit with
  `unsupported_backend`.
- No live libvirt is required.

---

## Seams With Other Features

| Seam | Owner | This Feature |
|---|---|---|
| `libvirtd.service` lifecycle | `3-service-management` | Coordinator brings `libvirtd` up before any verb here |
| Storage pool LVM backing | `3-storage-management` | Coordinator prepares the VG / LV; this feature attaches the resulting path |
| Network firewall holes | `3-firewall-management` | Coordinator opens the libvirt-network ports explicitly |
| Container engines | `3-container-management` | Disjoint surface; never overlaps with VM verbs here |

---

## Out of Scope (this iteration)

- Apple Virtualization framework backend (Swift-only API).
- Live migration between hosts (`virsh migrate ...`).
- libvirt virtual-network create/destroy verbs.
- libvirt storage-pool create/destroy verbs (only attaching an
  existing path is covered).
- Guest agent installation / RPC.
- Console / VNC / SPICE session management.
- Cloning VMs beyond snapshot-and-restore.
- WSL2 distribution management.

These extensions are tracked under
`/Users/myron/repos/ccai/codecraftai-sysman-linux-ubuntu/specs/3-virtualization-management/tasks.md`
follow-ups.
