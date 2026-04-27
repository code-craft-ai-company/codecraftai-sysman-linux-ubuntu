# Implementation Plan -- Kdump Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `kdumpmanagement` module under
  `source/packages/automojo/sysman/linux/ubuntu/`.

- [x] 2. Alias `UbuntuKdumpActionStatus = ActionStatus` so the
  package-wide outcome enum is reused.

- [x] 3. Define `UbuntuKdumpTargetKind` enum with the values
  `LOCAL_PATH`, `SSH`, `NFS`, `RAW`.

- [x] 4. Define `UbuntuKdumpManagerConfig` dataclass with `dry_run`,
  `elevated`, `kdump_defaults` (`/etc/default/kdump-tools`),
  `crash_dir` (`/var/crash`), and `service_name`
  (`kdump-tools.service`).

- [x] 5. Define `UbuntuKdumpActionResult` dataclass extending
  `FeatureActionResult` with `target`, `requires_reboot`, and
  `message`.

- [x] 6. Define `UbuntuKdumpTargetSpec` dataclass holding
  `target_kind`, `path`, `ssh_user`, `ssh_host`, `nfs_export`, and
  `compression`.

- [x] 7. Define `UbuntuKdumpStatus` dataclass holding `enabled`,
  `running`, `target_kind`, `target_path`, `kernel`,
  `reserved_memory_mb`, and `message`.

- [x] 8. Define `UbuntuKdumpInfo` dataclass holding `id`, `path`,
  `created`, `size_bytes`, `kernel`, and `panic_message`.

- [x] 9. Define `UbuntuKdumpQueryError` carrying `command`,
  `exit_code`, and `stderr`.

## Module-level Helpers

- [x] 10. Implement `_apply_defaults_keys(content, updates)` that
  rewrites a shell-sourced `KEY=VALUE` body in place, preserves
  comments and ordering, and appends absent keys.

- [x] 11. Implement `_render_defaults_value(new_value, existing_value)`
  to preserve quoting style.

- [x] 12. Implement `_parse_defaults_file(content)` that returns a
  dict of unquoted values.

- [x] 13. Implement `_build_target_updates(spec)` that returns the
  per-key updates required by `target_kind`.

- [x] 14. Implement `_deduce_target_kind(parsed)` and
  `_deduce_target_locator(parsed, kind)` for status reads.

- [x] 15. Implement `_parse_kdump_config_show(text)` that parses the
  multi-line output of `kdump-config show` into a dict.

- [x] 16. Implement `_parse_dmesg_metadata(text)` that surfaces the
  first `Linux version` and `Kernel panic` lines.

## UbuntuKdumpManager Verbs

- [x] 17. Implement `enable_kdump()`: idempotency probe ->
  defaults rewrite -> `systemctl enable --now`.

- [x] 18. Implement `disable_kdump()`: idempotency probe ->
  defaults rewrite -> `systemctl disable --now`.

- [x] 19. Implement `configure_kdump_target(spec)`: spec validation
  -> defaults rewrite -> `systemctl restart`.

- [x] 20. Implement `clear_old_dumps(age_days)`: argument validation
  -> `find` invocation.

- [x] 21. Implement `get_kdump_status()` combining
  `cat /etc/default/kdump-tools`, `systemctl is-active`,
  `systemctl is-enabled`, and `kdump-config show`.

- [x] 22. Implement `list_recent_dumps()` via `ls -1t`.

- [x] 23. Implement `get_dump_info(dump_id)` via `cat` over
  `vmcore-dmesg.txt` + `dmesg.<id>` + `stat`.

## UbuntuKdumpManager Internal Helpers

- [x] 24. Implement `_read_defaults()` via `cat`.

- [x] 25. Implement `_write_defaults(content)` via
  `tee`-through-`_dispatch`.

- [x] 26. Implement `_query_service_state()` running
  `systemctl is-active` + `systemctl is-enabled`.

- [x] 27. Implement `_build_result(...)` constructing a
  `UbuntuKdumpActionResult` without dispatch.

## Tests

- [x] 28. `test_enable_kdump_invokes_systemctl` -- happy path.

- [x] 29. `test_enable_kdump_skipped_when_enabled` -- idempotency.

- [x] 30. `test_enable_kdump_dry_run_short_circuits` -- dry-run.

- [x] 31. `test_disable_kdump_invokes_systemctl` -- happy path.

- [x] 32. `test_configure_kdump_target_invokes_tee` -- happy path.

- [x] 33. `test_configure_kdump_target_skipped_when_already_set` --
  idempotency.

- [x] 34. `test_get_kdump_status_parses_kdump_config_output` --
  combined parser.

- [x] 35. `test_list_recent_dumps_parses_directory_listing` -- listing
  parser.

- [x] 36. `test_clear_old_dumps_invokes_find_with_mtime` -- argv
  shape.

- [x] 37. `test_get_dump_info_parses_dmesg_output` -- combined parser.

- [x] 38. `test_writes_elevate` -- elevation policy.

- [x] 39. `test_reads_never_elevate` -- elevation policy.

- [x] 40. `test_failed_command_surfaces_stderr` -- error surface.

- [x] 41. `test_list_recent_dumps_raises_on_unexpected_failure` --
  typed error.

## Follow-ups

- [ ] 42. Wire `crashkernel=` updates via `boot-management`'s
  `add_kernel_param` / `remove_kernel_param`.  The cross-OS contract
  scopes that to the boot-management feature; this manager
  intentionally does not duplicate it.

- [ ] 43. Add a `kdumpctl rebuild`-equivalent helper for explicit
  initramfs rebuilds beyond the implicit rebuild that
  `systemctl restart kdump-tools.service` triggers.

- [ ] 44. Surface reserved-memory information from
  `/sys/kernel/kexec_crash_size` once a file-operator-style read seam
  is wired into this package.
