# Requirements

## Introduction

The storage management feature provides the
`codecraftai-sysman-linux-ubuntu` package with the ability to enumerate
disks and volumes, create / delete / resize volumes, manage Volume
Groups (the cross-OS-contract "pool"), add and remove physical disks
from a Volume Group, and query detailed disk and volume metadata on
Ubuntu systems.  This spec implements the storage-management contract
defined at
`codecraftai-sysman-common/specs/contracts/storage-management/contract.md`
for Ubuntu using the LVM2 user-space toolchain (`lsblk`, `lvs`, `vgs`,
`pvs`, `pvcreate`, `pvremove`, `vgcreate`, `vgextend`, `vgreduce`,
`vgremove`, `lvcreate`, `lvremove`, `lvresize`, `blkid`).

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *which* volumes should exist, *which* disks
should be in a pool, or *what* size a volume should have — it provides
the capability to *manipulate LVM state* reliably, idempotently, and
with full auditability.  All system commands are executed exclusively
through the `codecraftai-interfaces` command-runner abstraction.

Filesystem creation (`mkfs.*`) and mount-table management (`/etc/fstab`)
are NOT in scope here — they live in `filesystem-management`.  This
feature stops at the volume boundary: `create_volume` provisions the
LV but does not format it; `resize_volume` calls `lvresize --resizefs`
to grow / shrink any existing filesystem in lock-step with the LV but
does not create one.

---

## Requirements

### Requirement 1: Enumerate Physical Disks

**User Story:** As a maintenance coordinator, I want to enumerate every
physical disk on the system with its size, model, serial, and pool
membership, so that I can decide which disks are eligible for inclusion
in a Volume Group.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_disks()` THE SYSTEM SHALL dispatch
   `lsblk -J -b -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL`
   through the command runner WITHOUT elevation.
2. THE SYSTEM SHALL filter the output to rows whose `type` is `disk`
   and SHALL return one `UbuntuDiskInfo` per matching row.
3. THE SYSTEM SHALL populate `UbuntuDiskInfo.is_pool_member` from the
   `pvs --reportformat json --units b` probe — `True` when the disk
   itself or any of its child partitions is reported as an LVM
   Physical Volume.
4. THE SYSTEM SHALL populate `UbuntuDiskInfo.mount_points` with every
   mountpoint of any descendant partition or LV.
5. WHEN `lsblk` exits non-zero THE SYSTEM SHALL return an empty list
   rather than raise — Wave 1 manager pattern treats absence as
   informational.

---

### Requirement 2: Enumerate Volumes

**User Story:** As a maintenance coordinator, I want to enumerate every
volume on the system (LVM Logical Volumes plus raw partitions surfaced
as volumes), so that I can compare actual storage state against the
declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_volumes()` THE SYSTEM SHALL
   dispatch `lvs --reportformat json --units b` through the command
   runner WITHOUT elevation, parse the JSON, and emit one
   `UbuntuVolumeInfo` per LV.
2. THE SYSTEM SHALL also dispatch `lsblk -J -b -o
   NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,KNAME` and emit one
   `UbuntuVolumeInfo` per `type=part` row.  LV rows come first; raw
   partitions follow.
3. THE SYSTEM SHALL populate `UbuntuVolumeInfo.id` as `<vg>/<lv>` for
   LVs and `/dev/<part-name>` for raw partitions.
4. THE SYSTEM SHALL populate `UbuntuVolumeInfo.used_bytes` from
   `lv_size` × `data_percent` when the LV reports a non-empty
   `data_percent`; otherwise the field is `None`.
5. WHEN either probe exits non-zero THE SYSTEM SHALL silently omit the
   failing source and return whatever the other source produced.

---

### Requirement 3: Create a Logical Volume

**User Story:** As a maintenance coordinator, I want to create an LVM
Logical Volume of a given size in a given Volume Group, so that an
application or container can claim it.

#### Acceptance Criteria

1. WHEN the coordinator provides an `UbuntuVolumeSpec` with `name`,
   `pool` (the VG), and `size_bytes` THE SYSTEM SHALL dispatch
   `lvcreate -L <size_bytes>B -n <name> <pool>` through the command
   runner with `elevated=True` when configured.
2. WHEN the spec's `pool` is `None` THE SYSTEM SHALL return
   `UbuntuStorageActionResult(status=FAILED)` with a descriptive
   `message` because LVM volumes always belong to a VG.
3. WHEN an LV with the same name already exists in the same VG with a
   matching size (within 1 MiB) THE SYSTEM SHALL skip execution and
   return `UbuntuStorageActionResult(status=SKIPPED)`.
4. WHEN the operation succeeds THE SYSTEM SHALL return
   `UbuntuStorageActionResult(status=SUCCESS)` populated with
   `target=<vg>/<lv>`, `pool=<vg>`, and `volume=<lv>`.
5. WHEN the operation fails THE SYSTEM SHALL return
   `UbuntuStorageActionResult(status=FAILED)` carrying the runner's
   exit code, stdout, and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuStorageActionResult(status=DRY_RUN)` carrying the
   would-have-been argv.

---

### Requirement 4: Delete a Logical Volume

**User Story:** As a maintenance coordinator, I want to delete an LV
by `<vg>/<lv>` identifier, so that decommissioned data can be reclaimed.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_volume(volume_id)` THE SYSTEM
   SHALL dispatch `lvremove -f <volume_id>` through the command runner
   with `elevated=True` when configured.
2. WHEN no LV matching `volume_id` exists THE SYSTEM SHALL skip
   execution and return
   `UbuntuStorageActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `UbuntuStorageActionResult(status=SUCCESS)` whose `message` carries
   a data-loss advisory naming the destroyed LV.
4. WHEN the operation fails THE SYSTEM SHALL return
   `UbuntuStorageActionResult(status=FAILED)` carrying the runner's
   exit code and stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuStorageActionResult(status=DRY_RUN)`.

---

### Requirement 5: Resize a Logical Volume

**User Story:** As a maintenance coordinator, I want to grow or shrink
an LV (and its contained filesystem in lock-step) by `<vg>/<lv>`
identifier, so that capacity tracks changing workload.

#### Acceptance Criteria

1. WHEN the coordinator calls `resize_volume(volume_id, size_bytes)`
   THE SYSTEM SHALL dispatch `lvresize -L <size_bytes>B --resizefs
   <volume_id>` through the command runner with `elevated=True` when
   configured.
2. WHEN the LV's current size is within 1 MiB of `size_bytes` THE
   SYSTEM SHALL skip execution and return
   `UbuntuStorageActionResult(status=SKIPPED)`.
3. WHEN no LV matching `volume_id` exists THE SYSTEM SHALL return
   `UbuntuStorageActionResult(status=FAILED)` with a descriptive
   `message`.  The contract reserves SKIPPED for the delete verb's
   absence semantics.
4. WHEN the operation succeeds THE SYSTEM SHALL return
   `UbuntuStorageActionResult(status=SUCCESS)`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuStorageActionResult(status=DRY_RUN)`.

---

### Requirement 6: Create a Volume Group (Pool)

**User Story:** As a maintenance coordinator, I want to create a
Volume Group from one or more disks, so that LVs can be carved out of
the resulting pool.

#### Acceptance Criteria

1. WHEN the coordinator provides an `UbuntuPoolSpec` with `name` and
   `disks` THE SYSTEM SHALL first dispatch `pvcreate <disk>` for every
   member disk, then dispatch `vgcreate <name> <disks...>`, both with
   `elevated=True` when configured.
2. WHEN a VG with the same name already exists THE SYSTEM SHALL skip
   execution and return
   `UbuntuStorageActionResult(status=SKIPPED)` without running any
   `pvcreate` or `vgcreate`.
3. WHEN any `pvcreate` step fails THE SYSTEM SHALL stop, NOT run
   `vgcreate`, and return the failing step's
   `UbuntuStorageActionResult(status=FAILED)`.
4. WHEN the `vgcreate` step succeeds THE SYSTEM SHALL return
   `UbuntuStorageActionResult(status=SUCCESS)`.
5. THE SYSTEM SHALL ignore `UbuntuPoolSpec.resiliency` because LVM
   Volume Groups have no native resiliency level (`mdadm` provides
   that beneath them).
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuStorageActionResult(status=DRY_RUN)`.

---

### Requirement 7: Delete a Volume Group (Pool)

**User Story:** As a maintenance coordinator, I want to delete a Volume
Group by name, so that the underlying disks become reusable.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_pool(pool_name)` THE SYSTEM
   SHALL dispatch `vgremove -f <pool_name>` through the command runner
   with `elevated=True` when configured.
2. WHEN no VG with that name exists THE SYSTEM SHALL skip execution
   and return `UbuntuStorageActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `UbuntuStorageActionResult(status=SUCCESS)` whose `message`
   carries a data-loss advisory.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuStorageActionResult(status=DRY_RUN)`.

---

### Requirement 8: Add a Disk to a Volume Group

**User Story:** As a maintenance coordinator, I want to extend an
existing VG with another disk, so that the pool's free capacity grows.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_to_pool(pool_name, disk_id)` THE
   SYSTEM SHALL first dispatch `pvcreate <disk_id>`, then dispatch
   `vgextend <pool_name> <disk_id>`, both with `elevated=True` when
   configured.
2. WHEN `pvs` already reports `disk_id` as a Physical Volume in
   `pool_name` THE SYSTEM SHALL skip execution and return
   `UbuntuStorageActionResult(status=SKIPPED)` without running
   `pvcreate` or `vgextend`.
3. WHEN the `pvcreate` step fails THE SYSTEM SHALL stop and return
   the failing step's
   `UbuntuStorageActionResult(status=FAILED)`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuStorageActionResult(status=DRY_RUN)`.

---

### Requirement 9: Remove a Disk from a Volume Group

**User Story:** As a maintenance coordinator, I want to evict a disk
from a VG, so that the disk can be reused or decommissioned.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_from_pool(pool_name, disk_id)`
   THE SYSTEM SHALL dispatch `vgreduce <pool_name> <disk_id>` and, on
   success, follow with `pvremove <disk_id>`, both with
   `elevated=True` when configured.
2. WHEN `pvs` does not report `disk_id` as a Physical Volume in
   `pool_name` THE SYSTEM SHALL skip execution and return
   `UbuntuStorageActionResult(status=SKIPPED)` without running
   `vgreduce`.
3. WHEN the `vgreduce` step fails THE SYSTEM SHALL stop and return
   the failing step's
   `UbuntuStorageActionResult(status=FAILED)` (no `pvremove`).
4. WHEN the `vgreduce` step succeeds but `pvremove` fails THE SYSTEM
   SHALL return `UbuntuStorageActionResult(status=FAILED)` whose
   `message` notes that the PV signature may need manual cleanup.
5. WHEN both steps succeed THE SYSTEM SHALL return
   `UbuntuStorageActionResult(status=SUCCESS)` whose `message` carries
   a data-loss advisory.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuStorageActionResult(status=DRY_RUN)`.

---

### Requirement 10: Get Volume Info

**User Story:** As a maintenance coordinator, I want to look up a single
volume by id, so that I can branch on its size, filesystem, and pool
without parsing `lvs` output myself.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_volume_info(volume_id)` THE SYSTEM
   SHALL invoke `list_volumes()` and return the matching entry.
2. WHEN no entry matches THE SYSTEM SHALL return `None`.

---

### Requirement 11: Get Disk Info

**User Story:** As a maintenance coordinator, I want to look up a single
disk by id, so that I can branch on its size, model, serial, and pool
membership.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_disk_info(disk_id)` THE SYSTEM SHALL
   invoke `list_disks()` and return the matching entry.
2. WHEN no entry matches THE SYSTEM SHALL return `None`.

---

### Requirement 12: Elevation Policy

**User Story:** As a maintenance coordinator, I want every mutating LVM
command to run with root privilege via `sudo`, so that `pvcreate` /
`vgcreate` / `lvcreate` / `lvremove` / `lvresize` / `vgextend` /
`vgreduce` / `vgremove` / `pvremove` succeed on a standard system where
the process does not run as root, while reads stay unprivileged.

#### Acceptance Criteria

1. WHEN the manager is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the runner for every write verb
   (`create_volume`, `delete_volume`, `resize_volume`, `create_pool`,
   `delete_pool`, `add_to_pool`, `remove_from_pool`).
2. WHEN performing read-only operations (`list_disks`, `list_volumes`,
   `get_volume_info`, `get_disk_info`, all idempotency probes) THE
   SYSTEM SHALL NEVER pass `elevated=True`.
3. WHEN the manager is configured with `elevated=False` THE SYSTEM
   SHALL NEVER pass `elevated=True`.
