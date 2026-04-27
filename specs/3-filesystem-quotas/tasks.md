# Implementation Plan — Filesystem Quotas (Ubuntu)

All tasks unchecked — this is a new feature with no existing
implementation in `automojo-sysman-linux-ubuntu`.

## Module and Models

- [ ] 1. Create `source/packages/automojo/sysman/linux/ubuntu/filesystemquotas.py`
  - _Requirements: All_

- [ ] 2. Define `UbuntuQuotaActionStatus` (alias `ActionStatus`)
  - _Requirements: 10_

- [ ] 3. Define `UbuntuFilesystemQuotasManagerConfig` dataclass
  with `quotacheck_format: str = "vfsv0"`
  - _Requirements: 1_

- [ ] 4. Define `UbuntuQuotaActionResult` (subclasses
  `FeatureActionResult`) with `mountpoint: str | None` and
  `principal: str | None`
  - _Requirements: 10_

- [ ] 5. Define `UbuntuQuotaPrincipalKind` enum (`USER`,
  `GROUP`)
  - _Requirements: 3, 4, 7_

- [ ] 6. Define `UbuntuQuotaInfo` dataclass with `mountpoint`,
  `principal`, `principal_kind`, `used_bytes`, `soft_bytes`,
  `hard_bytes`, `used_inodes`, `soft_inodes`, `hard_inodes`,
  `grace_period_seconds`
  - _Requirements: 5, 6, 7_

- [ ] 7. Define `UbuntuQuotaStatus` dataclass with
  `mountpoint`, `enabled`, `enforcing`, `user_quotas_active`,
  `group_quotas_active`, `filesystem_type`
  - _Requirements: 8_

- [ ] 8. Define `UbuntuQuotaQueryError(Exception)` and
  `UbuntuQuotaFilesystemNotMountedError(Exception)`
  - _Requirements: 5, 6, 7, 8, 9_

## Helpers

- [ ] 9. Implement `__init__` accepting runner + optional
  config (delegate to `BaseFeatureManager`)

- [ ] 10. Implement `_resolve_fstype(mountpoint)` — `findmnt
  -n -o FSTYPE <mountpoint>` (no elevation); raises
  `UbuntuQuotaFilesystemNotMountedError` on non-zero exit
  - _Requirements: 9_

- [ ] 11. Implement `_planned_command_for_enable(mountpoint)` /
  `_planned_command_for_disable(mountpoint)` returning the argv
  for the dry-run record path

## Enable / Disable

- [ ] 12. Implement `enable_quotas(mountpoint)`:
  - Dry-run guard returning `DRY_RUN` with `quotacheck` argv
  - `_resolve_fstype` for mount validation
  - Probe `get_quota_status`; SKIPPED when both kinds active
  - Dispatch `quotacheck -ug -mF vfsv0 <mountpoint>`
  - On `quotacheck` failure return its result verbatim
  - Dispatch `quotaon -ug <mountpoint>`
  - Return the `quotaon` result
  - _Requirements: 1, 11.1_

- [ ] 13. Implement `disable_quotas(mountpoint)`:
  - Dry-run guard
  - `_resolve_fstype`
  - Probe `get_quota_status`; SKIPPED when both off
  - Dispatch `quotaoff -ug <mountpoint>`
  - _Requirements: 2, 11.2_

## Limits

- [ ] 14. Implement `set_user_quota(mountpoint, user, soft_kb,
  hard_kb)`:
  - Dry-run guard with planned argv
  - `_resolve_fstype`
  - Probe `get_user_quota`; SKIPPED when soft / hard match
  - Dispatch `setquota -u <user> <soft_kb> <hard_kb> 0 0
    <mountpoint>`
  - _Requirements: 3, 11.3_

- [ ] 15. Implement `set_group_quota(mountpoint, group,
  soft_kb, hard_kb)`:
  - Mirror `set_user_quota` with `-g` and `get_group_quota`
  - _Requirements: 4, 11.4_

## Queries

- [ ] 16. Implement `get_user_quota(mountpoint, user)`:
  - Dispatch `quota -u <user>` without elevation
  - Map exit code 0 / 1 / 2 to "no record" None when output
    has no matching mountpoint
  - Other exit codes raise `UbuntuQuotaQueryError`
  - Parse the `quota` output table; return matching row or
    None
  - _Requirements: 5_

- [ ] 17. Implement `get_group_quota(mountpoint, group)`:
  - Mirror `get_user_quota` with `-g`
  - _Requirements: 6_

- [ ] 18. Implement `list_quotas(mountpoint)`:
  - Dispatch `repquota -ug <mountpoint>` without elevation
  - Non-zero exit raises `UbuntuQuotaQueryError`
  - Parse user-section rows then group-section rows; return
    flat list
  - _Requirements: 7_

- [ ] 19. Implement `get_quota_status(mountpoint)`:
  - Dispatch `quotaon -p <mountpoint>` without elevation
  - Parse "user/group quota on /mnt is on/off" lines
  - Best-effort `findmnt` for `filesystem_type`
  - Non-zero `quotaon -p` raises `UbuntuQuotaQueryError`
  - _Requirements: 8_

## Parsers

- [ ] 20. Implement `_parse_quota_output(stdout, mountpoint,
  principal, principal_kind)` — parse `quota -u`/`-g`
  multi-mount table; convert KiB → bytes; convert grace
  duration to seconds

- [ ] 21. Implement `_parse_repquota_output(stdout,
  mountpoint)` — parse `repquota -ug` two-section report

- [ ] 22. Implement `_parse_quotaon_status(stdout)` — return
  tuple `(user_active, group_active)`

## Tests

- [ ] 23. Create `test_filesystemquotas.py` with
  `_MockCommandRunner`

- [ ] 24. Test `enable_quotas` invokes `quotacheck` then
  `quotaon` with the right argv

- [ ] 25. Test `enable_quotas` returns SKIPPED when both kinds
  already active

- [ ] 26. Test `enable_quotas` returns DRY_RUN when
  `dry_run=True`

- [ ] 27. Test `disable_quotas` invokes `quotaoff -ug`

- [ ] 28. Test `set_user_quota` invokes `setquota -u ...`

- [ ] 29. Test `set_user_quota` returns SKIPPED when current
  matches

- [ ] 30. Test `set_group_quota` invokes `setquota -g ...`

- [ ] 31. Test `get_user_quota` parses `quota -u` output;
  `None` for unknown mountpoint

- [ ] 32. Test `get_group_quota` parses `quota -g` output

- [ ] 33. Test `list_quotas` parses `repquota -ug` output

- [ ] 34. Test `get_quota_status` parses `quotaon -p` output

- [ ] 35. Test writes pass `elevated=True` when
  `config.elevated=True`

- [ ] 36. Test reads never elevate

- [ ] 37. Test failed `quotacheck` surfaces stderr on the
  result

- [ ] 38. Test `_resolve_fstype` raises
  `UbuntuQuotaFilesystemNotMountedError` when `findmnt` exits
  non-zero
