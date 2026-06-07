# Implementation Plan — Virtualization Management

All tasks below are unchecked at start; tick them in order.

## Module and Models

- [ ] 1. Create
  `source/packages/ccai/sysman/linux/ubuntu/virtualizationmanagement.py`
  with the module docstring covering backend selection, connection-URI
  rules, elevation policy, and stop semantics.
  - _Requirements: All_

- [ ] 2. Define `UbuntuVirtualizationActionStatus` (alias of
  `ActionStatus`).
  - _Requirements: 12_

- [ ] 3. Define `UbuntuVirtBackend` enum (`LIBVIRT`, `LXC`, `LXD`).
  - _Requirements: 1_

- [ ] 4. Define `UbuntuVmState` enum (`RUNNING`, `STOPPED`, `PAUSED`,
  `SUSPENDED`, `OTHER`).
  - _Requirements: 3, 7_

- [ ] 5. Define `UbuntuVirtualizationManagerConfig` dataclass with
  `backend`, `virsh_path`, `virt_install_path`, `default_connection`.
  - _Requirements: 1, 2_

- [ ] 6. Define `UbuntuVirtualizationActionResult` adding `vm_name`,
  `snapshot_name`, `message`.
  - _Requirements: 12_

- [ ] 7. Define `UbuntuVmSpec`, `UbuntuVmInfo`, `UbuntuVmDiskSpec`,
  `UbuntuSnapshotInfo` dataclasses.
  - _Requirements: 5, 8, 10_

- [ ] 8. Define `UbuntuVirtualizationQueryError` carrying `command`,
  `exit_code`, `stderr`.
  - _Requirements: 3, 4, 10_

## Helpers

- [ ] 9. Implement `_typed_config()` that returns a
  `UbuntuVirtualizationManagerConfig` instance, falling back to a
  synthesised default when the caller passed a plain `FeatureConfig`.

- [ ] 10. Implement `_backend_supported()` returning True only for
  `LIBVIRT`.

- [ ] 11. Implement `_virsh_argv(args)` building the `virsh` argv with
  the optional `--connect <uri>` prefix.

- [ ] 12. Implement `_build_virt_install_argv(spec)` rendering the
  full `virt-install` argv from the spec.

- [ ] 13. Implement `_build_disk_argument(spec)` resolving
  `disk_path` vs `disk_size_gb` to a `--disk` value.

- [ ] 14. Implement `_dispatch_virt_write(...)` wrapping `_dispatch`
  with `vm_name` / `snapshot_name` keys.

- [ ] 15. Implement `_build_result(...)` for non-runner result
  construction (DRY_RUN / SKIPPED / unsupported-backend).

- [ ] 16. Implement `_read_dominfo(name)` probing `virsh dominfo` and
  parsing into `UbuntuVmInfo`; recognise the unknown-domain stderr.

- [ ] 17. Implement `_disk_path_in_xml(vm, path)` and
  `_disk_attached(vm, disk_id)` probing `virsh dumpxml`.

- [ ] 18. Implement `_snapshot_exists(vm, snapshot_name)` probing
  `virsh snapshot-list`.

## Read Verbs

- [ ] 19. Implement `list_vms()` per Requirement 3.

- [ ] 20. Implement `get_vm_info(name)` per Requirement 4.

- [ ] 21. Implement `list_snapshots(vm)` per Requirement 10.4.

## Write Verbs

- [ ] 22. Implement `create_vm(spec)` per Requirement 5.

- [ ] 23. Implement `delete_vm(name)` per Requirement 6.

- [ ] 24. Implement `start_vm(name)` / `stop_vm(name, force)` /
  `pause_vm(name)` / `resume_vm(name)` per Requirement 7.

- [ ] 25. Implement `attach_disk(vm, disk_spec)` per Requirement 8.

- [ ] 26. Implement `detach_disk(vm, disk_id)` per Requirement 9.

- [ ] 27. Implement `snapshot_vm(vm, snapshot_name)` and
  `restore_snapshot(vm, snapshot_name)` per Requirement 10.

## Parsers

- [ ] 28. Implement `_parse_virsh_list_names(stdout)` for the
  `--name` line-per-name format.

- [ ] 29. Implement `_parse_virsh_dominfo(stdout, name)` mapping the
  `Key: value` shape onto `UbuntuVmInfo`.

- [ ] 30. Implement `_parse_virsh_snapshot_list(stdout)` for the
  tabular output.

- [ ] 31. Implement `_normalise_libvirt_state(state_text)` mapping the
  reported string onto `UbuntuVmState`.

## Tests

- [ ] 32. Create
  `source/testroots/testbase/ccai/tests/sysman/linux/ubuntu/test_virtualizationmanagement.py`
  with `_MockCommandRunner`.

- [ ] 33. Tests for `list_vms` parser, `list_snapshots` parser, and
  the unknown-VM case for `get_vm_info`.

- [ ] 34. Tests for every write verb's command construction, dry-run
  short-circuit, and idempotency SKIPPED branch.

- [ ] 35. Test asserting `stop_vm(force=True)` dispatches
  `virsh destroy` and `stop_vm(force=False)` dispatches
  `virsh shutdown`.

- [ ] 36. Tests for the data-loss advisory on `delete_vm`,
  `detach_disk`, and `restore_snapshot` SUCCESS results.

- [ ] 37. Elevation tests proving writes pass `elevated=True` and
  reads pass `elevated=False`, regardless of `config.elevated`.

- [ ] 38. Failure-surfacing test (non-zero runner exit -> FAILED with
  `stderr` populated).

- [ ] 39. Backend test asserting `LXC` / `LXD` short-circuit with
  `unsupported_backend`.

## Follow-ups

- [ ] 40. **virsh-network parity** — extend the manager with
  `list_networks` / `define_network` / `start_network` /
  `destroy_network` once the contract grows the network surface.

- [ ] 41. **virsh storage-pool parity** — add `list_pools` /
  `define_pool` / `start_pool` once the contract grows the pool
  surface beyond attaching an existing path.

- [ ] 42. **Migration verb** — add `migrate_vm(name, target_uri)` once
  the contract grows the live-migration surface.

- [ ] 43. **OVA / OVF import** — track separately under a new
  `vm-image-management` feature.

- [ ] 44. **Apple Virtualization** — explicitly out of scope for the
  Linux package; tracked instead in the (future) macOS Apple
  Virtualization feature.
