# Design

## Concept Mapping (LVM ↔ APFS ↔ Storage Spaces)

The cross-OS storage-management contract talks about *disks*, *pools*,
and *volumes*.  Each OS's native storage stack carries a different
vocabulary; the table below grounds the abstract contract in concrete
LVM / APFS / Storage-Spaces terms so a reader of this spec can compare
all three implementations side-by-side.

| Contract concept | Ubuntu (this spec) | macOS | Windows |
|---|---|---|---|
| Disk | Block device of `type=disk` (`/dev/sda`) | Whole-disk node (`disk0`) | `Get-Disk` row keyed by `Number` |
| Pool | LVM **Volume Group** (`vgcreate`, `vgextend`, `vgreduce`, `vgremove`) | APFS **Container** (`diskutil apfs createContainer`) | **Storage Pool** (`New-StoragePool`) |
| Pool member | LVM **Physical Volume** initialised on a disk via `pvcreate`; `vgextend` adds the PV to the VG | APFS **Physical Store** (a partition on a disk) | `PhysicalDisk` flagged `CanPool=True` |
| Volume | LVM **Logical Volume** (`lvcreate`, `lvremove`, `lvresize`) | APFS **Volume** (`diskutil apfs addVolume`) | **Virtual Disk** (Storage Spaces) **or** raw partition (`New-Partition`) |
| Volume identifier | `<vg>/<lv>` (also surfaces under `/dev/<vg>/<lv>` and `/dev/mapper/<vg>-<lv>`) | APFS volume UUID / device id | Drive letter or volume id |
| Free space | VG free PE × PE size | Container free space (shared by every volume in the container) | Pool / Virtual-Disk free bytes |
| Resiliency level | Native VG has none — layer `mdadm` underneath if redundancy is required | APFS containers can span multiple physical stores; resiliency is Apple-managed | Pool `ResiliencySettingName` (Mirror / Parity / Simple) |

The Ubuntu implementation is the "canonical LVM" branch; the RHEL
implementation in `codecraftai-sysman-linux-redhat` covers the same toolset
with a different naming convention.  The macOS and Windows specs
(`codecraftai-sysman-macos/specs/3-storage-management/` and
`codecraftai-sysman-windows/specs/3-storage-management/`) translate the
same abstract verbs onto `diskutil apfs` / Storage-Spaces cmdlets
respectively.

---

## Overview

The storage-management feature is implemented as a single module,
`ccai.sysman.linux.ubuntu.storagemanagement`, that exposes an
`UbuntuStorageManager` class.  The coordinator instantiates
`UbuntuStorageManager` and calls its methods to perform discrete
block-level storage actions: disk enumeration, volume create / delete /
resize, Volume Group create / delete / extend / reduce, and detailed
disk and volume queries.

This feature **complements — and does not duplicate** — the mount,
unmount, and `/etc/fstab` management provided by the
`filesystem-management` feature.  The `UbuntuStorageManager` brings
disks, PVs, VGs, and LVs into the desired shape; the
`UbuntuFilesystemManager` is then responsible for mounting those
filesystems and persisting their entries in `/etc/fstab`.

All command execution is delegated to a command runner implementing the
`ICommandRunner` interface.  The `UbuntuStorageManager` never directly
calls `subprocess`, shell commands, or `open()`; it only constructs
argument lists and hands them to the injected runner.  This keeps
business logic separate from execution and makes the feature fully
testable via mock dependencies without root access or a live system.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).
Under SSH the coordinator can track reliability metrics and apply
execution aspects (persistence, retry, timeout) without any changes to
the `UbuntuStorageManager` code.

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
│  - Injects runner into UbuntuStorageManager                 │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuStorageManager                             │
│  ccai.sysman.linux.ubuntu.storagemanagement             │
│                                                             │
│  list_disks()           list_volumes()                      │
│  get_disk_info()        get_volume_info()                   │
│  create_volume()        delete_volume()                     │
│  resize_volume()                                            │
│  create_pool()          delete_pool()                       │
│  add_to_pool()          remove_from_pool()                  │
└─────────┬───────────────────────────────────────────────────┘
          │ ICommandRunner
          ▼
┌─────────────────────────────────────────────────────────────┐
│               LocalCommandRunner / SSHCommandRunner         │
└─────────┬───────────────────────────────────────────────────┘
          │ optional aspect composition
          ▼
┌─────────────────────────────────────────────────────────────┐
│   PersistenceAspect  RetryAspect  TimeoutAspect             │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `UbuntuStorageManager`

**Location:**
`source/packages/ccai/sysman/linux/ubuntu/storagemanagement.py`

| Method | Signature | Description |
|---|---|---|
| `list_disks` | `() → list[UbuntuDiskInfo]` | `lsblk -J -b -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL` filtered to `type=disk`; `is_pool_member` derived from `pvs` |
| `list_volumes` | `() → list[UbuntuVolumeInfo]` | `lvs --reportformat json --units b` (LVs) plus `lsblk` (raw partitions) |
| `create_volume` | `(spec) → UbuntuStorageActionResult` | `lvcreate -L <bytes>B -n <name> <vg>` |
| `delete_volume` | `(volume_id) → UbuntuStorageActionResult` | `lvremove -f <vg>/<lv>` with data-loss advisory on success |
| `resize_volume` | `(volume_id, size_bytes) → UbuntuStorageActionResult` | `lvresize -L <bytes>B --resizefs <vg>/<lv>` |
| `create_pool` | `(spec) → UbuntuStorageActionResult` | `pvcreate <disk>` per member, then `vgcreate <name> <disks...>` |
| `delete_pool` | `(pool_name) → UbuntuStorageActionResult` | `vgremove -f <name>` with data-loss advisory on success |
| `add_to_pool` | `(pool_name, disk_id) → UbuntuStorageActionResult` | `pvcreate <disk>` then `vgextend <pool> <disk>` |
| `remove_from_pool` | `(pool_name, disk_id) → UbuntuStorageActionResult` | `vgreduce <pool> <disk>` then `pvremove <disk>` with data-loss advisory on success |
| `get_volume_info` | `(volume_id) → UbuntuVolumeInfo \| None` | indexed lookup over `list_volumes()` |
| `get_disk_info` | `(disk_id) → UbuntuDiskInfo \| None` | indexed lookup over `list_disks()` |

### `UbuntuStorageManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no command is dispatched; the would-have-been argv is recorded |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `default_filesystem` | `str` | `"ext4"` | Filesystem hint recorded onto `UbuntuVolumeSpec`; this feature does NOT call `mkfs` |
| `lvs_format_args` | `list[str]` | `["--reportformat", "json", "--units", "b"]` | Argv suffix appended to every `lvs` / `vgs` / `pvs` invocation |

---

## Data Models

### Result Types

```python
class UbuntuStorageActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class UbuntuStorageActionResult:
    status: UbuntuStorageActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    target: str | None             # volume id, pool name, or disk path
    pool: str | None
    volume: str | None
    message: str | None            # SKIPPED reason or data-loss advisory
```

### Disk / Volume Types

```python
@dataclass
class UbuntuDiskInfo:
    id: str                        # /dev/sda
    name: str                      # sda
    size_bytes: int
    model: str | None
    serial: str | None
    partition_style: str | None    # gpt / dos / unknown / None
    health_status: str | None
    mount_points: list[str]        # mounts of every descendant
    is_pool_member: bool

@dataclass
class UbuntuVolumeInfo:
    id: str                        # vg/lv or /dev/sdaN
    name: str
    size_bytes: int
    used_bytes: int | None
    filesystem: str | None
    mount_point: str | None
    pool: str | None               # vg name for LVs, None for partitions
    health_status: str | None

@dataclass
class UbuntuPoolSpec:
    name: str                      # vg name
    disks: list[str]               # disk ids
    resiliency: str | None = None  # ignored on Ubuntu

@dataclass
class UbuntuVolumeSpec:
    name: str                      # lv name
    size_bytes: int
    pool: str | None = None        # vg name (required)
    filesystem: str = "ext4"       # hint; not applied
```

### Exceptions

```python
class UbuntuStorageQueryError(Exception):
    """Raised when an lsblk / lvs / vgs / pvs query fails unexpectedly."""
```

---

## Pool / Volume Mapping

| Contract verb | Ubuntu command surface | Idempotency probe |
|---|---|---|
| `list_disks` | `lsblk -J -b -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL` | n/a |
| `list_volumes` | `lvs --reportformat json --units b` + `lsblk -J -b -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,KNAME` | n/a |
| `create_volume` | `lvcreate -L <bytes>B -n <name> <vg>` | `lvs` (LV in VG with matching size) |
| `delete_volume` | `lvremove -f <vg>/<lv>` | `lvs` (LV absence) |
| `resize_volume` | `lvresize -L <bytes>B --resizefs <vg>/<lv>` | `lvs` (size match within 1 MiB) |
| `create_pool` | `pvcreate <disk>` × N + `vgcreate <name> <disks...>` | `vgs` (VG name) |
| `delete_pool` | `vgremove -f <name>` | `vgs` (VG absence) |
| `add_to_pool` | `pvcreate <disk>` + `vgextend <pool> <disk>` | `pvs` (pv_name + vg_name match) |
| `remove_from_pool` | `vgreduce <pool> <disk>` + `pvremove <disk>` | `pvs` (pv_name + vg_name absence) |
| `get_volume_info` | `list_volumes` lookup | n/a |
| `get_disk_info` | `list_disks` lookup | n/a |

### Volume Identifier Syntax

Volumes are identified by the LVM-canonical `<vg>/<lv>` form.  The
on-disk node lives at `/dev/<vg>/<lv>` and `/dev/mapper/<vg>-<lv>`;
those names are NOT accepted by `delete_volume` / `resize_volume` /
`get_volume_info` because the contract picks one canonical form.  Raw
partitions surfaced by `list_volumes` use `/dev/<part-name>`; they are
read-only — none of the write verbs accept a raw-partition id.

### Shrink-Support Matrix

| Contained filesystem | Shrink support |
|---|---|
| `ext4` | Supported via `lvresize --resizefs`; the resize2fs invocation handles the filesystem shrink |
| `ext3` / `ext2` | Supported (`resize2fs` handles both grow and shrink) |
| `xfs` | NOT supported — `xfs_growfs` is grow-only.  `lvresize --resizefs` will fail; this feature surfaces the failure as `FAILED` |
| `btrfs` | Supported but multi-step; `lvresize --resizefs` does not invoke the `btrfs filesystem resize` flow.  Coordinators that want to shrink a btrfs LV should not pass `--resizefs` |
| `swap` | NOT supported — swap is recreated, not resized.  Caller should `swapoff`, `lvresize`, `mkswap`, `swapon` themselves |
| no filesystem | Always supported (no on-disk metadata to maintain) |

---

## Command Mappings

### Write Commands (elevation honoured)

| Operation | Argv |
|---|---|
| `create_volume` | `lvcreate -L <bytes>B -n <name> <vg>` |
| `delete_volume` | `lvremove -f <vg>/<lv>` |
| `resize_volume` | `lvresize -L <bytes>B --resizefs <vg>/<lv>` |
| `create_pool` (per disk) | `pvcreate <disk>` |
| `create_pool` (final) | `vgcreate <name> <disks...>` |
| `delete_pool` | `vgremove -f <name>` |
| `add_to_pool` (PV step) | `pvcreate <disk>` |
| `add_to_pool` (extend step) | `vgextend <pool> <disk>` |
| `remove_from_pool` (reduce step) | `vgreduce <pool> <disk>` |
| `remove_from_pool` (PV strip) | `pvremove <disk>` |

### Read Commands (no elevation)

| Operation | Argv |
|---|---|
| `list_disks` (lsblk) | `lsblk -J -b -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL` |
| `list_disks` (PV probe) | `pvs --reportformat json --units b` |
| `list_volumes` (LVs) | `lvs --reportformat json --units b` |
| `list_volumes` (parts) | `lsblk -J -b -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,KNAME` |
| `vg_exists` probe | `vgs --reportformat json --units b` |
| `disk_in_pool` probe | `pvs --reportformat json --units b` |

All writes pass `elevated=True` when the config enables it.  Reads
never elevate.

---

## Output Parsing

### `lvs --reportformat json --units b`

```json
{
  "report": [
    {
      "lv": [
        {
          "lv_name": "data",
          "vg_name": "vg0",
          "lv_size": "10737418240B",
          "data_percent": "25.0",
          "lv_health_status": ""
        }
      ]
    }
  ]
}
```

* `lv_size` carries the suffix `B` — the parser strips it.
* `data_percent` is a stringified float; an empty string means the
  field was not reported (thin volumes, snapshots).  When non-empty
  the parser computes `used_bytes = size_bytes * data_percent / 100`.
* `lv_health_status` is empty for healthy volumes; the parser maps the
  empty string to `None`.

### `vgs --reportformat json --units b`

The parser iterates `report[*].vg[*]` and matches `vg_name`.  No size
information is consumed — VGs do not carry their own size in this
feature; the disks underneath them are the source of truth.

### `pvs --reportformat json --units b`

```json
{
  "report": [
    {
      "pv": [
        { "pv_name": "/dev/sda1", "vg_name": "vg0" }
      ]
    }
  ]
}
```

The parser indexes by `(pv_name, vg_name)` for the
`disk_in_pool` probe and by `pv_name` alone for the
`is_pool_member` derivation in `list_disks`.

### `lsblk -J` output

```json
{
  "blockdevices": [
    {
      "name": "sda",
      "size": 256000000000,
      "type": "disk",
      "fstype": null,
      "mountpoint": null,
      "model": "Samsung SSD",
      "serial": "ABC123",
      "children": [
        {
          "name": "sda1",
          "size": 1024000000,
          "type": "part",
          "fstype": "ext4",
          "mountpoint": "/boot"
        }
      ]
    }
  ]
}
```

* The disk parser walks the top-level array, picks `type=disk` rows,
  and recurses into `children` only to collect descendant
  mountpoints.
* The partition parser walks every level of the tree breadth-first
  (so partitions on RAID volumes are surfaced).

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `create_volume` | LV with same name exists in same VG with matching size (within 1 MiB) | `lvs` |
| `delete_volume` | LV does not exist | `lvs` |
| `resize_volume` | Size matches within 1 MiB | `lvs` |
| `create_pool` | VG name already exists | `vgs` |
| `delete_pool` | VG does not exist | `vgs` |
| `add_to_pool` | Disk already a PV in the VG | `pvs` |
| `remove_from_pool` | Disk not a PV in the VG | `pvs` |

The 1-MiB tolerance on `create_volume` and `resize_volume` is
deliberate: LVM rounds requested sizes up to the physical-extent
boundary (4 MiB by default), so a request that already matches within
one extent is treated as already satisfied.  We pick 1 MiB as a more
conservative bound so a request slightly smaller than the rounded-up
size still triggers a real `lvresize` rather than a silent SKIPPED.

---

## Data-Loss Advisories

| Verb | Advisory text |
|---|---|
| `delete_volume` | `"logical volume <vg>/<lv> was removed; all data on the volume has been destroyed"` |
| `delete_pool` | `"volume group <name> was removed; every logical volume in the group has been destroyed"` |
| `remove_from_pool` | `"disk <disk> was removed from volume group <pool>; the LVM physical-volume signature has been wiped"` |

Advisories surface on `SUCCESS` results in both `message` and
`detail` so callers that already index on `detail` (RHEL pattern)
continue to see them.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UbuntuStorageQueryError` | Reserved for stricter read paths added in a follow-up batch.  Wave 1 reads return empty / `None` on probe failure to mirror the established Ubuntu pattern (`get_mounts`, `stat`). |

All write failures are surfaced as
`UbuntuStorageActionResult(status=FAILED, ...)` carrying the runner's
exit code, stdout, and stderr.  Writes never raise for routine I/O.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system.*

### Property 1: list_disks Argv

*For any* call to `list_disks()`, the runner sees exactly
`["lsblk", "-J", "-b", "-o",
"NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL"]` plus a `pvs` probe.

**Validates: Requirements 1.1, 1.3**

### Property 2: list_disks Pool Membership

*For any* disk reported by `lsblk` whose name (or any child's name)
matches a `pv_name` reported by `pvs`, `is_pool_member` MUST be True.

**Validates: Requirements 1.3**

### Property 3: list_volumes Composition

*For any* call to `list_volumes()`, the result list comprises every
LV reported by `lvs` (LVs first) followed by every `type=part` row
reported by `lsblk` (raw partitions second).

**Validates: Requirements 2.1, 2.2**

### Property 4: list_volumes used_bytes Math

*For any* LV with non-empty `data_percent`, `used_bytes` MUST equal
`int(size_bytes * data_percent / 100)`.

**Validates: Requirements 2.4**

### Property 5: create_volume Argv

*For any* `UbuntuVolumeSpec(name=N, pool=P, size_bytes=S)`,
`create_volume()` MUST dispatch
`["lvcreate", "-L", f"{S}B", "-n", N, P]`.

**Validates: Requirements 3.1**

### Property 6: create_volume Idempotency

*For any* LV with the same name in the same VG already at the
requested size (within 1 MiB), `create_volume()` MUST return SKIPPED
and NOT dispatch `lvcreate`.

**Validates: Requirements 3.3**

### Property 7: create_volume Missing Pool

*For any* `UbuntuVolumeSpec(pool=None)`, `create_volume()` MUST return
FAILED without dispatching any command.

**Validates: Requirements 3.2**

### Property 8: delete_volume Argv

*For any* `volume_id="<vg>/<lv>"`, `delete_volume()` MUST dispatch
`["lvremove", "-f", "<vg>/<lv>"]`.

**Validates: Requirements 4.1**

### Property 9: delete_volume Advisory

*For any* successful `delete_volume()` call, the result's `message`
MUST contain the substring `"destroyed"`.

**Validates: Requirements 4.3**

### Property 10: delete_volume Idempotency

*For any* `volume_id` not present in `lvs`, `delete_volume()` MUST
return SKIPPED.

**Validates: Requirements 4.2**

### Property 11: resize_volume Argv

*For any* `(volume_id, size_bytes)` pair, `resize_volume()` MUST
dispatch
`["lvresize", "-L", f"{size_bytes}B", "--resizefs", volume_id]`.

**Validates: Requirements 5.1**

### Property 12: resize_volume Idempotency

*For any* LV whose current size is within 1 MiB of `size_bytes`,
`resize_volume()` MUST return SKIPPED.

**Validates: Requirements 5.2**

### Property 13: create_pool Step Order

*For any* `UbuntuPoolSpec(name=N, disks=[D1, D2])`, `create_pool()`
MUST dispatch `["pvcreate", D1]`, `["pvcreate", D2]`, then
`["vgcreate", N, D1, D2]` in that order.

**Validates: Requirements 6.1**

### Property 14: create_pool Idempotency

*For any* VG name already present in `vgs`, `create_pool()` MUST return
SKIPPED without dispatching `pvcreate` or `vgcreate`.

**Validates: Requirements 6.2**

### Property 15: delete_pool Argv

*For any* `pool_name=N`, `delete_pool()` MUST dispatch
`["vgremove", "-f", N]`.

**Validates: Requirements 7.1**

### Property 16: add_to_pool Step Order

*For any* `(pool_name=P, disk_id=D)` not already a PV in P,
`add_to_pool()` MUST dispatch `["pvcreate", D]` then
`["vgextend", P, D]` in that order.

**Validates: Requirements 8.1**

### Property 17: add_to_pool Idempotency

*For any* disk already a PV in the target VG, `add_to_pool()` MUST
return SKIPPED without dispatching any command.

**Validates: Requirements 8.2**

### Property 18: remove_from_pool Step Order

*For any* `(pool_name=P, disk_id=D)` currently a PV in P,
`remove_from_pool()` MUST dispatch `["vgreduce", P, D]` first; on
success it MUST follow with `["pvremove", D]`.

**Validates: Requirements 9.1**

### Property 19: remove_from_pool Advisory

*For any* successful `remove_from_pool()` call, the result's `message`
MUST contain the substring `"wiped"`.

**Validates: Requirements 9.5**

### Property 20: Dry-Run Safety

*For any* write verb invoked with `dry_run=True`, the runner MUST NOT
be invoked and the result status MUST be `DRY_RUN`.

**Validates: Requirements 3.6, 4.5, 5.5, 6.6, 7.4, 8.4, 9.6**

### Property 21: Elevation for Writes

*For any* write verb with `elevated=True` in the config, every runner
invocation MUST receive `elevated=True`.

**Validates: Requirements 12.1**

### Property 22: No Elevation for Reads

*For any* read verb (and any idempotency probe), the runner MUST NEVER
be invoked with `elevated=True`, regardless of config.

**Validates: Requirements 12.2**

### Property 23: Error Surface

*For any* non-zero exit from a write command, the returned
`UbuntuStorageActionResult` MUST have `status=FAILED` and carry the
runner's exit code and stderr.

**Validates: Requirements 3.5, 4.4**

---

## Module Layout

```
source/packages/ccai/sysman/linux/ubuntu/
├── __init__.py
├── featurebase.py
├── filesystemmanagement.py
└── storagemanagement.py        ← UbuntuStorageManager + types

source/testroots/testbase/ccai/tests/sysman/linux/ubuntu/
└── test_storagemanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.linux.ubuntu.runners import (
    LocalCommandRunner,
    LocalRunnerConfig,
)
from ccai.sysman.linux.ubuntu.storagemanagement import (
    UbuntuPoolSpec,
    UbuntuStorageManager,
    UbuntuStorageManagerConfig,
    UbuntuVolumeSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = UbuntuStorageManagerConfig(elevated=True, dry_run=False)
storage = UbuntuStorageManager(runner=runner, config=config)

# Bring a fresh data disk into a brand-new VG
pool_spec = UbuntuPoolSpec(name="vgdata", disks=["/dev/sdb"])
storage.create_pool(spec=pool_spec)

# Carve a 100 GiB LV out of it
vol_spec = UbuntuVolumeSpec(
    name="metrics",
    pool="vgdata",
    size_bytes=100 * 1024 * 1024 * 1024,
)
storage.create_volume(spec=vol_spec)

# Grow it to 200 GiB later
storage.resize_volume(
    volume_id="vgdata/metrics",
    size_bytes=200 * 1024 * 1024 * 1024,
)

# Inspect
for disk in storage.list_disks():
    print(f"{disk.id}\t{disk.size_bytes}\tpool_member={disk.is_pool_member}")
for vol in storage.list_volumes():
    print(f"{vol.id}\t{vol.size_bytes}\tpool={vol.pool}")
```

### Dry-Run Mode

```python
config = UbuntuStorageManagerConfig(elevated=True, dry_run=True)
storage = UbuntuStorageManager(runner=runner, config=config)

result = storage.delete_volume(volume_id="vgdata/metrics")
assert result.status == UbuntuStorageActionStatus.DRY_RUN
print(result.command)  # ["lvremove", "-f", "vgdata/metrics"]
```

### SSH Execution with Aspects

```python
from ccai.sysman.linux.ubuntu.runners import (
    SSHCommandRunner,
    SSHRunnerConfig,
)
from ccai.sysman.linux.ubuntu.aspects import (
    PersistenceAspect,
    PersistenceAspectConfig,
    RetryAspect,
    RetryAspectConfig,
)
from ccai.sysman.linux.ubuntu.storage import SQLiteStorage

runner = SSHCommandRunner(
    SSHRunnerConfig(host="ubuntu-server-1", username="admin"),
)
storage_db = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(
    aspect=PersistenceAspect(
        PersistenceAspectConfig(storage_backend=storage_db),
    ),
)
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2)))

storage = UbuntuStorageManager(
    runner=runner,
    config=UbuntuStorageManagerConfig(elevated=True),
)
for vol in storage.list_volumes():
    print(vol.id, vol.size_bytes)
```

---

## Testing Strategy

* All tests use a `MockCommandRunner` keyed on argv tuples.  Both write
  argv assertions and idempotency-probe seeding share the same fixture
  builders (`_seed_lvs`, `_seed_vgs`, `_seed_pvs`).
* Argv-construction tests assert the exact argv shape for every write
  verb: `["lvcreate", "-L", "<bytes>B", "-n", N, P]`,
  `["lvremove", "-f", "<vg>/<lv>"]`, etc.
* Idempotency tests cover every verb's SKIPPED path (LV present /
  absent, VG present / absent, PV present / absent in VG, size match
  within 1 MiB).
* Dry-run tests prove the runner is not invoked and the recorded
  `command` carries the would-have-been argv.
* Elevation tests cover every write verb passing `elevated=True` and
  every read verb passing `elevated=False`, regardless of config.
* Parser tests cover `lvs`, `vgs`, `pvs`, and `lsblk` JSON shapes
  including empty-data-percent and absent-mountpoint edge cases.
* Failure surface is tested by seeding the runner with a non-zero
  exit; the result MUST be `FAILED` with stderr captured.
* No test requires root access or live LVM state.

---

## Scope Mapping

| Contract verb | Ubuntu implementation |
|---|---|
| `list_disks` | `lsblk -J -b -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL` |
| `list_volumes` | `lvs --reportformat json --units b` + `lsblk -J -b` |
| `create_volume` | `lvcreate -L <bytes>B -n <name> <vg>` |
| `delete_volume` | `lvremove -f <vg>/<lv>` |
| `resize_volume` | `lvresize -L <bytes>B --resizefs <vg>/<lv>` |
| `create_pool` | `pvcreate <disk>` × N + `vgcreate <name> <disks...>` |
| `delete_pool` | `vgremove -f <name>` |
| `add_to_pool` | `pvcreate <disk>` + `vgextend <pool> <disk>` |
| `remove_from_pool` | `vgreduce <pool> <disk>` + `pvremove <disk>` |
| `get_volume_info` | `list_volumes` lookup by id |
| `get_disk_info` | `list_disks` lookup by id |

---

## Extensions Beyond the Contract

None in this Ubuntu implementation.  Filesystem creation (`mkfs.*`),
mount-point management, partition-table editing, encryption (LUKS),
RAID array management (`mdadm`), snapshot creation, and multipath /
iSCSI / NVMe-oF target configuration are intentionally deferred.  When
added they will live behind their own feature flags so a coordinator
writing against the contract does not have to special-case Ubuntu.

The feature also intentionally omits a `partition_style` resolver
(`gpt` / `dos`) on `UbuntuDiskInfo`; the field exists to satisfy the
cross-OS data shape but is left as `None` until a `parted -s -m
<device> unit B print` parser is added in a follow-up batch.

---

## Future Considerations

* Add a `dependencies` resolver so `delete_pool` can refuse when
  active LVs would be destroyed (current behaviour relies on
  `vgremove -f` to fail with a descriptive stderr).
* Surface a strict-read mode that raises `UbuntuStorageQueryError`
  instead of returning empty lists / `None`.  The exception is
  defined; only the call sites need to elect the strict variant.
* Layer `mdadm`-based resiliency beneath the VG when
  `UbuntuPoolSpec.resiliency` is set.  Currently the field is
  documented as ignored.
