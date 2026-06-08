# Implementation Plan — Ubuntu Storage Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.linux.ubuntu.storagemanagement`
  module.

- [x] 2. Define `UbuntuStorageActionStatus` (aliased to the shared
  `featurebase.ActionStatus`).

- [x] 3. Define `UbuntuStorageManagerConfig` dataclass with
  `default_filesystem` and `lvs_format_args` extras on top of the
  base `dry_run` / `elevated`.

- [x] 4. Define `UbuntuStorageActionResult` dataclass with
  `target` / `pool` / `volume` / `message` extras.

- [x] 5. Define `UbuntuDiskInfo`, `UbuntuVolumeInfo`,
  `UbuntuPoolSpec`, `UbuntuVolumeSpec` dataclasses.

- [x] 6. Define `UbuntuStorageQueryError` exception (defined; not yet
  raised — see deviation note).

## UbuntuStorageManager — Implemented

- [x] 7. `__init__` accepting runner + optional config (inherited
  from `BaseFeatureManager`).

- [x] 8. `list_disks()` — `lsblk -J -b -o
  NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL` plus `pvs` probe to
  derive `is_pool_member`.

- [x] 9. `list_volumes()` — `lvs --reportformat json --units b`
  (LVs) plus `lsblk` walk (raw partitions).

- [x] 10. `create_volume(spec)` — `lvcreate -L <bytes>B -n <name>
  <vg>`; idempotent via `lvs`; missing-pool returns `FAILED`.

- [x] 11. `delete_volume(volume_id)` — `lvremove -f <vg>/<lv>`;
  idempotent via `lvs`; data-loss advisory on success.

- [x] 12. `resize_volume(volume_id, size_bytes)` — `lvresize -L
  <bytes>B --resizefs <vg>/<lv>`; idempotent via `lvs` (1-MiB
  tolerance); missing-volume returns `FAILED`.

- [x] 13. `create_pool(spec)` — `pvcreate <disk>` × N + `vgcreate
  <name> <disks...>`; idempotent via `vgs`; halts on `pvcreate`
  failure.

- [x] 14. `delete_pool(pool_name)` — `vgremove -f <name>`;
  idempotent via `vgs`; data-loss advisory on success.

- [x] 15. `add_to_pool(pool_name, disk_id)` — `pvcreate <disk>` +
  `vgextend <pool> <disk>`; idempotent via `pvs`.

- [x] 16. `remove_from_pool(pool_name, disk_id)` — `vgreduce <pool>
  <disk>` + `pvremove <disk>`; idempotent via `pvs`; data-loss
  advisory on success.

- [x] 17. `get_volume_info(volume_id)` — indexed lookup over
  `list_volumes()`; returns `None` when not found.

- [x] 18. `get_disk_info(disk_id)` — indexed lookup over
  `list_disks()`; returns `None` when not found.

- [x] 19. `_dispatch_storage_write` and `_build_result` helpers —
  thread `target` / `pool` / `volume` / `message` through
  `BaseFeatureManager._dispatch`.

## UbuntuStorageManager — Gaps (specified but not implemented)

- [ ] 20. Strict-read mode for the read verbs that raises
  `UbuntuStorageQueryError` instead of returning empty / `None`.

- [ ] 21. Populate `UbuntuDiskInfo.partition_style` via `parted -s
  -m <device> unit B print`.

- [ ] 22. Populate `UbuntuVolumeInfo.filesystem` and `mount_point`
  via `blkid` and `findmnt` cross-references for LV rows.

- [ ] 23. Honour `UbuntuPoolSpec.resiliency` by composing `mdadm`
  underneath the VG when set (current behaviour: ignored).

- [ ] 24. Active-LV dependency check before `delete_pool` (current
  behaviour: relies on `vgremove -f` to fail with a descriptive
  stderr).

## Tests — Implemented

- [x] 25. `test_list_disks_invokes_lsblk_and_parses`
- [x] 26. `test_list_volumes_parses_output`
- [x] 27. `test_create_volume_invokes_lvcreate`
- [x] 28. `test_create_volume_skipped_when_already_present`
- [x] 29. `test_create_volume_dry_run_short_circuits`
- [x] 30. `test_delete_volume_invokes_lvremove`
- [x] 31. `test_delete_volume_skipped_when_absent`
- [x] 32. `test_resize_volume_invokes_lvresize`
- [x] 33. `test_resize_volume_skipped_when_size_matches`
- [x] 34. `test_create_pool_invokes_pvcreate_then_vgcreate`
- [x] 35. `test_create_pool_skipped_when_already_present`
- [x] 36. `test_delete_pool_invokes_vgremove`
- [x] 37. `test_add_to_pool_invokes_pvcreate_and_vgextend`
- [x] 38. `test_remove_from_pool_invokes_vgreduce_and_pvremove`
- [x] 39. `test_get_volume_info_parses_output`
- [x] 40. `test_get_disk_info_parses_output`
- [x] 41. `test_writes_elevate`
- [x] 42. `test_reads_never_elevate`
- [x] 43. `test_failed_command_surfaces_stderr`
- [x] 44. Module-level parser helpers (`_split_volume_id`,
  `_parse_lvm_size`, `_vg_present_in_output`, `_pv_in_vg`,
  `_pv_disks_from_output`).

## Tests — Gaps

- [ ] 45. Shrink-refused path for `xfs` filesystems on
  `resize_volume` (currently bubbles whatever `lvresize --resizefs`
  reports without an OS-level pre-check).

- [ ] 46. Multi-disk `create_pool` failure-rollback test (currently
  the partial PV state is left in place when `vgcreate` fails after
  successful `pvcreate` runs).

- [ ] 47. Property-based test for the LVM `lvs` JSON parser against
  randomised reports.

## Follow-ups

- [ ] 48. **Cross-OS contract conformance review**: verify
  `UbuntuStorageActionResult` field shapes match the cross-OS contract
  at `codecraftai-sysman-common/specs/contracts/storage-management/contract.md`.

- [ ] 49. **codecraftai-* reuse check**: confirm that no other feature in
  `codecraftai-sysman-linux-ubuntu` invokes `lvcreate` / `vgcreate` /
  `pvcreate` directly; all such calls should go through
  `UbuntuStorageManager`.

## Spec deviations from this implementation

- **Volume identifier syntax is `<vg>/<lv>` only.**  The on-disk
  symlinks `/dev/<vg>/<lv>` and `/dev/mapper/<vg>-<lv>` are NOT
  accepted by the write verbs.  Coordinators that have a `/dev/...`
  path should split the trailing component themselves.

- **`UbuntuPoolSpec.resiliency` is ignored.**  LVM Volume Groups have
  no native resiliency level; the field exists for cross-OS-contract
  parity.  When the OS-level `mdadm` integration lands the field
  will become a directive to layer mdadm beneath the VG.

- **No strict-read mode.**  Read verbs return empty lists / `None`
  when their underlying command fails, mirroring the established
  Ubuntu pattern (`get_mounts`, `stat`).  The
  `UbuntuStorageQueryError` exception is defined and reserved for a
  future strict-read variant.

- **`UbuntuDiskInfo.partition_style` is always `None`.**  The field
  exists for cross-OS-contract parity but a `parted -s -m <device>
  unit B print` parser has not been wired in.

- **`UbuntuDiskInfo.health_status` is always `None`.**  SMART
  integration via `smartctl -i` is reserved for a follow-up batch.

- **`UbuntuVolumeInfo.filesystem` and `mount_point` are always
  `None` for LVM rows.**  The fields are populated only for the raw
  partition rows surfaced by the `lsblk` walk.  A `blkid` + `findmnt`
  cross-reference for LV rows is reserved for a follow-up batch.

- **No active-LV dependency check before `delete_pool`.**  The
  feature relies on `vgremove -f` to fail with a descriptive stderr
  when active LVs prevent removal; the failure surfaces as a normal
  `FAILED` result.

- **Resize tolerance is 1 MiB, not the LVM physical-extent size.**
  LVM rounds requests up to the PE boundary (4 MiB by default).  We
  pick a tighter bound so a request slightly smaller than the
  rounded-up size still triggers a real `lvresize` rather than a
  silent SKIPPED.
