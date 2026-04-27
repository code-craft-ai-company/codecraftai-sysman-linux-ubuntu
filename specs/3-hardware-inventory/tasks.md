# Implementation Plan — Hardware Inventory (Ubuntu)

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

This feature is read-only — no dry-run distinction, no idempotency
guards, no mutating operations.

## Module and Models

- [x] 1. Create the `hardwareinventory` module under
  `automojo.sysman.linux.ubuntu`.

- [x] 2. Define `UbuntuHardwareInventoryActionStatus` with
  `SUCCESS`, `FAILED`, `SKIPPED` (no `DRY_RUN` per contract).

- [x] 3. Define `UbuntuHardwareInventoryManagerConfig` inheriting from
  `FeatureConfig` (no extra fields; documents that `dry_run` is
  ignored and `elevated` is reserved for selective elevation).

- [x] 4. Define `UbuntuHardwareInventoryQueryError` with
  `command` / `exit_code` / `stderr` and a multi-line message
  built via `os.linesep.join`.

- [x] 5. Define query-result dataclasses: `UbuntuSystemInfo`,
  `UbuntuCpuInfo`, `UbuntuMemoryInfo`, `UbuntuMemoryModule`,
  `UbuntuStorageDeviceInfo`, `UbuntuNetworkDeviceInfo`,
  `UbuntuPciDeviceInfo`, `UbuntuUsbDeviceInfo`,
  `UbuntuFirmwareInfo`.

## Manager — Implemented

- [x] 6. Implement `__init__` (inherited; no overrides needed).

- [x] 7. Implement `_read` helper that dispatches a runner call with
  `elevated=False` and raises `UbuntuHardwareInventoryQueryError`
  on a non-zero exit code.

- [x] 8. Implement `get_system_info` via `dmidecode -t system`,
  parsing the System Information and BIOS Information handle blocks.

- [x] 9. Implement `get_cpu_info` via `lscpu -J`, mapping the
  JSON `field` / `data` entries onto the `UbuntuCpuInfo` slots and
  computing `physical_cores` from sockets × cores-per-socket.

- [x] 10. Implement `get_memory_info` via `dmidecode -t memory` for
  per-DIMM detail and `cat /proc/meminfo` for the live aggregate;
  tolerate `dmidecode` failure by leaving the modules list empty
  while still merging the meminfo aggregates.

- [x] 11. Implement `list_storage_devices` via
  `lsblk -J -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL` and
  walk parent / `children` depth-first.

- [x] 12. Implement `list_network_devices` via `ip -j link show`,
  classifying virtual interfaces by `link_type=loopback` or by
  `ifname` prefix.

- [x] 13. Implement `list_pci_devices` via `lspci -mm -nn -k`,
  parsing primary lines and `\tDriver:` continuation lines.

- [x] 14. Implement `list_usb_devices` via `lsusb`, parsing the
  canonical `Bus / Device / ID / name` line shape.

- [x] 15. Implement `get_firmware_info` via `dmidecode -t bios`,
  parsing the BIOS Information handle block and detecting UEFI via
  the `UEFI is supported` characteristic line.

## Module-Level Parsers — Implemented

- [x] 16. `_parse_dmidecode_system`, `_parse_dmidecode_bios`,
  `_parse_dmidecode_memory`, `_parse_dmidecode_size`,
  `_parse_dmidecode_mhz`, `_commit_memory_module`,
  `_merge_proc_meminfo`.

- [x] 17. `_parse_lscpu_json`, `_parse_lscpu_cache_size`.

- [x] 18. `_parse_lsblk_json`, `_parse_lsblk_human_size`.

- [x] 19. `_parse_ip_link_json`, `_is_virtual_link`.

- [x] 20. `_parse_lspci_output`, `_parse_lspci_primary_line`,
  `_split_lspci_tokens`, `_strip_lspci_id`.

- [x] 21. `_parse_lsusb_output`.

## Tests — Implemented

- [x] 22. `test_get_system_info_invokes_dmidecode_and_parses`
- [x] 23. `test_get_cpu_info_parses_lscpu_output`
- [x] 24. `test_get_memory_info_aggregates_modules_and_total`
- [x] 25. `test_list_storage_devices_parses_lsblk_output`
- [x] 26. `test_list_network_devices_parses_ip_link_output`
- [x] 27. `test_list_pci_devices_parses_lspci_output`
- [x] 28. `test_list_usb_devices_parses_lsusb_output`
- [x] 29. `test_get_firmware_info_parses_dmidecode_bios_output`
- [x] 30. `test_failed_command_raises_query_error`
- [x] 31. `test_reads_never_elevate`

## Gaps (specified but not implemented)

- [ ] 32. Selective elevation for `dmidecode` when
  `config.elevated=True`.  The contract permits passing
  `elevated=True` to DMI-class sub-queries when the caller has
  asked for elevation, but the pilot intentionally hard-codes
  `elevated=False` so that the "reads never elevate" property is
  unambiguous in the first delivered increment.

- [ ] 33. `secure_boot` detection via the
  `/sys/firmware/efi/efivars/SecureBoot-*` or equivalent
  efivars source.  Currently `secure_boot` is always `None`.

- [ ] 34. `network speed` lookup from `/sys/class/net/<iface>/speed`.
  Out of scope per the cross-OS contract — link speed belongs to
  the `network-configuration` feature.

- [ ] 35. `lsblk` rotational / interface columns.  The contract's
  optional `rotational` and `interface` fields are not surfaced
  through the current `lsblk -o` field list.  Adding `ROTA,TRAN`
  here is a one-line change but is left for a follow-up so the
  parser snapshot in the spec matches the implementation byte-for-
  byte.

- [ ] 36. `lsusb -v` per-device serial extraction.  Currently
  `serial` is always `None` for USB devices.

- [ ] 37. `collect_snapshot` aggregator.  The contract describes a
  `collect_snapshot` verb with partial-tolerance semantics; this
  pilot does not ship it.  Adding it is a small refactor on top of
  the eight existing verbs and is the natural next step.

## Tests — Gaps

- [ ] 38. Per-verb fallback tests for cases where the primary tool
  exits non-zero.  Currently only `get_memory_info` has an implicit
  fallback; the remaining verbs raise on the first failure.

- [ ] 39. Additional storage parser tests covering loop, swap, and
  encrypted-LUKS device types.

## Follow-ups

- [ ] 40. Once the cross-OS contract gains a stable
  `HardwareSnapshot` shape, ship `collect_snapshot()` here and
  delegate per-verb partial-tolerance to the snapshot wrapper.

- [ ] 41. **automojo-* reuse check**: `automojo-interop` may already
  expose generic PCI / USB enumeration helpers that would shrink
  the `_parse_lspci_output` / `_parse_lsusb_output` logic.  Audit
  before shipping the next sysman OS.

- [ ] 42. Spec-vs-contract drift discovered while writing this
  spec: the contract names the system-identity dataclass
  `SystemIdentity` and the field set is `{manufacturer, product,
  version, serial, uuid, machine_id, hostname, source}`.  This pilot
  ships `UbuntuSystemInfo` with a different field set — closer to
  the `dmidecode -t system` surface than the contract's
  cross-OS minimal set.  Reconciliation needed in the next
  contract revision.
