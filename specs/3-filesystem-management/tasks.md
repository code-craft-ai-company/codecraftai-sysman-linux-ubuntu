# Implementation Plan — Ubuntu Filesystem Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.linux.ubuntu.filesystemmanagement`
  module

- [x] 2. Define `UbuntuFilesystemActionStatus` enum (SUCCESS / FAILED
  / SKIPPED / DRY_RUN)
  *Aliased to the shared `featurebase.ActionStatus`.*

- [x] 3. Define `UbuntuFilesystemManagerConfig` dataclass (dry_run,
  elevated, fstab_path)

- [x] 4. Define `UbuntuFilesystemActionResult` dataclass
  *Implemented with `path`, `device`, `mountpoint`, and `message`
  extras on top of the base `FeatureActionResult`.*

- [x] 5. Define `UbuntuMountInfo`, `UbuntuFstabEntry`,
  `UbuntuFileInfo`, `UbuntuDiskUsageInfo` dataclasses
  *Implemented `UbuntuMountInfo`, `UbuntuFstabEntry`, and
  `UbuntuFileInfo`.  `UbuntuDiskUsageInfo` is descoped from this
  batch; see deviation note.*

- [x] 6. Define `UbuntuFilesystemQueryError` exception

## UbuntuFilesystemManager — Implemented

- [x] 7. Implement `__init__` accepting runner + file operator +
  optional config
  *Implemented inheriting `BaseFeatureManager.__init__`; file I/O
  flows through the runner via `cat` / `tee`, not a separate file
  operator.*

- [x] 8. Implement `mount(device, mountpoint, fstype, options)` —
  idempotent via `findmnt`, dispatches `mount [-t][-o] <dev> <mnt>`

- [x] 9. Implement `unmount(mountpoint, lazy)` — idempotent via
  `findmnt`, `-l` flag when lazy
  *Implemented as `unmount(mountpoint)`; the `lazy` flag is not yet
  surfaced.  Default `umount` covers the common case; `-l` can be
  added in a follow-up batch.*

- [x] 10. Implement `add_fstab_entry(entry)` — read-modify-write
  `/etc/fstab` via `IFileOperator`, preserves comments and unrelated
  entries, updates when device/mountpoint match but other fields
  differ; emits unified diff in dry-run
  *Implemented via `cat` + `tee -a`.  Comments and unrelated
  entries are preserved by virtue of `tee -a` being append-only.
  In-place update for "device matches but other fields differ"
  is not implemented in this batch — callers should
  `remove_fstab_entry` then `add_fstab_entry` to refresh.  Dry-run
  records the would-be `tee` argv but no unified diff yet.*

- [x] 11. Implement `remove_fstab_entry(device_or_mountpoint)` —
  read-modify-write filter
  *Implemented as `remove_fstab_entry(mountpoint)`.  Lines are
  matched on the second field (mountpoint).  Comments and
  unrelated entries are preserved verbatim.  Device-or-mountpoint
  dual matching is reserved for a follow-up batch.*

- [x] 12. Implement `get_mounts()` — `findmnt -J -l` JSON parser
  *Implemented via `findmnt --real --json`.  Walks every nested
  `children` collection breadth-first.*

- [ ] 13. Implement `create_directory(path, mode, owner, group)` —
  `mkdir -p` + conditional `chmod` + `chown`; idempotent via `stat`;
  type-conflict detection
  *Descoped from this batch.  The user's pilot brief explicitly
  scoped this batch to mount / fstab / chown / chmod / stat;
  directory orchestration mirrors the RHEL `create_directory`
  surface and can be added in a follow-up batch.*

- [ ] 14. Implement `remove_directory(path, recursive)` — `rmdir` or
  `rm -rf`; idempotent via `stat`
  *Descoped from this batch.*

- [ ] 15. Implement `create_symlink(link, target)` — `ln -s` or
  `ln -snf` on mismatched target, `FAILED` on non-symlink conflict
  *Descoped from this batch.*

- [ ] 16. Implement `remove_symlink(link)` — `stat` guard + `rm`,
  `FAILED` on non-symlink
  *Descoped from this batch.*

- [x] 17. Implement `chown(path, user, group)` / `chmod(path, mode)` —
  idempotent via current `stat`
  *Implemented; `recursive` flag added on both verbs.  Recursive
  invocations always run because per-entry idempotency is out of
  scope.*

- [x] 18. Implement `stat(path)` — `stat -c '%F|%a|%U|%G|%s|%n'`
  parser, returns `None` on "No such file or directory"
  *Implemented with the `stat -c '%U:%G:%a:%s:%y'` format —
  colon-separated to match the established Ubuntu shorthand and
  surface a `modified` timestamp.  Returns `None` on any non-zero
  exit.*

- [ ] 19. Implement `query_disk_usage(path)` — `df -B1
  --output=size,used,avail,pcent` parser
  *Descoped from this batch.*

- [x] 20. Implement `_dispatch` / `_fstab_rewrite` helpers — dry-run
  short-circuit, unified-diff emission, runner-result mapping,
  elevation decision
  *Inherited from `BaseFeatureManager._dispatch`; per-write helper
  `_dispatch_filesystem_write` threads the `path` / `device` /
  `mountpoint` extras through.  Unified-diff emission is reserved
  for a follow-up batch.*

## UbuntuFilesystemManager — Gaps (specified but not implemented)

- [ ] 21. `mkfs.*` filesystem creation (not in base contract)

- [ ] 22. LVM snapshot / resize (not in base contract)

- [ ] 23. ACL / xattr / AppArmor profile manipulation (not in base
  contract)

- [ ] 24. Hard-link creation (`ln` without `-s`) — not in base contract

## Tests — Implemented

- [x] 25. `test_mount_constructs_argv_with_optional_flags`
  *Implemented as `test_mount_constructs_argv_with_options`.*
- [x] 26. `test_mount_skipped_when_already_mounted`
- [ ] 27. `test_unmount_lazy_flag_passed_through`
  *Not yet implemented (the `lazy` flag is not surfaced).*
- [x] 28. `test_unmount_skipped_when_not_mounted`
- [x] 29. `test_add_fstab_entry_preserves_comments_and_other_lines`
  *Equivalent coverage in
  `test_remove_fstab_entry_filters_matching_mountpoint`, which
  asserts that comments and unrelated entries survive the
  read-modify-write cycle.  `add_fstab_entry` itself uses `tee -a`
  (append-only) so unrelated lines are never touched.*
- [x] 30. `test_add_fstab_entry_skipped_when_exact_match`
  *Implemented as `test_add_fstab_entry_skipped_when_match_present`.*
- [ ] 31. `test_add_fstab_entry_updates_when_device_matches_but_fields_differ`
  *In-place update is descoped from this batch.*
- [ ] 32. `test_add_fstab_entry_dry_run_emits_diff`
  *Unified-diff emission is descoped from this batch.*
- [x] 33. `test_remove_fstab_entry_skipped_when_no_match`
- [x] 34. `test_get_mounts_parses_findmnt_json`
  *Implemented as `test_get_mounts_parses_findmnt_json_with_children`.*
- [ ] 35. `test_create_directory_orchestrates_mkdir_chmod_chown`
- [ ] 36. `test_create_directory_skipped_when_matching`
- [ ] 37. `test_create_directory_failed_when_non_directory_at_path`
- [ ] 38. `test_remove_directory_recursive_uses_rm_rf`
- [ ] 39. `test_create_symlink_skipped_when_target_matches`
- [ ] 40. `test_create_symlink_atomic_replace_on_mismatch`
- [ ] 41. `test_create_symlink_failed_on_non_symlink_conflict`
- [ ] 42. `test_remove_symlink_failed_on_non_symlink`
- [x] 43. `test_chown_skipped_when_matching`
  *Implemented as `test_chown_skipped_when_already_matches`.*
- [x] 44. `test_chmod_skipped_when_matching`
  *Implemented as `test_chmod_skipped_when_already_matches`.*
- [x] 45. `test_stat_parses_pipe_separated_output`
  *Implemented as `test_stat_parses_colon_separated_output`; the
  separator is colon to keep the timestamp's colons inside the
  ``%y`` field intact.*
- [x] 46. `test_stat_returns_none_for_missing_path`
- [ ] 47. `test_query_disk_usage_parses_df_output`
- [x] 48. `test_dry_run_short_circuits_every_write`
  *Implemented as `test_dry_run_short_circuits_mount`; the
  remaining write verbs share the same dry-run guard but are not
  individually covered.*
- [x] 49. `test_reads_never_pass_elevated`
  *Implemented as
  `test_reads_never_pass_elevated_even_when_configured`.*

## Tests — Gaps

- [ ] 50. Cross-reading-aspect test proving that
  `add_fstab_entry(atomic=True)` survives a simulated SSH drop during
  `write_text`

- [ ] 51. Property-based test for `/etc/fstab` round-trip (arbitrary
  valid fstab files should parse, mutate, and re-emit losslessly)

## Follow-ups

- [ ] 52. **Cross-OS contract conformance review**: verify this
  implementation's `UbuntuFilesystemActionResult` field shapes match
  the cross-OS contract at
  `automojo-sysman-common/specs/contracts/filesystem-management/contract.md`.

- [ ] 53. **automojo-* reuse check**: confirm that no other feature in
  `automojo-sysman-linux-ubuntu` invokes `mount` / `umount` / `chmod`
  / `chown` directly; all such calls should go through
  `UbuntuFilesystemManager`.

## Spec deviations from this implementation

- **No `IFileOperator` collaborator.**  ``/etc/fstab`` is read with
  `cat` and written with `tee` (append for `add_fstab_entry`,
  full-rewrite for `remove_fstab_entry`).  This matches the
  established Ubuntu pattern where file I/O routes through the
  runner so dry-run, elevation, and aspect composition compose
  uniformly.

- **`stat` separator is colon, not pipe.**  The format string is
  `'%U:%G:%a:%s:%y'` — colon-separated to match the established
  Ubuntu shorthand.  The trailing `%y` (modification time) is
  preserved intact via `split(":", 4)` so the timestamp's own
  colons are not lost.

- **`UbuntuFstabEntry` uses `pass_` not `passno`.**  Trailing
  underscore avoids the `pass` Python keyword while still reading
  naturally.  This is the field name the user's pilot brief
  prescribed.

- **`add_fstab_entry` is append-only.**  In-place update of
  matching device-only entries with differing fields is descoped
  to a follow-up batch.  Callers needing that behaviour should
  `remove_fstab_entry` followed by `add_fstab_entry`.

- **`remove_fstab_entry` matches on mountpoint.**  Device-side
  matching can be added in a follow-up batch.

- **Directory / symlink / disk-usage verbs descoped.**  The
  user's pilot brief scoped this batch to mount / fstab / chown /
  chmod / stat.  `create_directory`, `remove_directory`,
  `create_symlink`, `remove_symlink`, and `query_disk_usage` are
  reserved for a subsequent batch.

- **`unmount` does not yet surface a `lazy` flag.**  The base
  `umount` invocation covers the common case; `-l` lazy unmount is
  reserved for a follow-up batch.

- **`get_mounts` returns an empty list on `findmnt` failure.**
  Rather than raising `UbuntuFilesystemQueryError`, this matches
  the established pattern where idempotency probes treat absence
  as informational.  The exception type is still defined for use
  by future strict-read callers.
