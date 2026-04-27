# Implementation Plan — Ubuntu Kernel Parameters

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.linux.ubuntu.kernelparameters`
  module

- [x] 2. Define `UbuntuKernelParameterActionStatus` enum (SUCCESS /
  FAILED / SKIPPED / DRY_RUN)

- [x] 3. Define `UbuntuKernelParameterManagerConfig` dataclass
  (dry_run, elevated, persist_dir).  Implementation note: the
  drop-in directory is named `sysctl_d_path` and a new
  `default_basename` field selects the per-write filename
  (default `99-automojo.conf`).  The 99-prefix is an intentional
  deviation from the spec's `<name>.conf` convention so the
  drop-in applies last.

- [x] 4. Define `UbuntuParameterValue` dataclass.  Implementation
  fields are `name`, `value`, and a tri-state `is_default`
  reserved for future kernel-default discovery.  The contract's
  `source` field is recorded on the result rather than the value
  per the user-instructions data-model spec.

- [x] 5. Define `UbuntuKernelParameterActionResult` dataclass with
  `status`, `name`, `value`, `persistent`, `current_value`,
  `applied_value`, `requires_reboot=False`, `source`, `diff`,
  `message`, plus the standard `exit_code` / `stdout` / `stderr` /
  `command` from the base.

- [x] 6. Define `UbuntuKernelParameterQueryError` exception

## Allowlist and Parsing Helpers

- [x] 7. Define `_ALLOWED_PREFIXES` constant and
  `_is_allowlisted(name)` helper.  Prefixes:
  `kernel.`, `vm.`, `net.`, `fs.`, `user.`, `abi.`, `dev.`,
  `debug.`, `crypto.`.

- [x] 8. Implement `_parse_sysctl_a(stdout)` — splits `name = value`
  lines, skips malformed rows, returns `dict[str, str]`

- [ ] 9. Implement `_parse_dropin_file(content)` — preserves comments
  and blank lines, extracts `(name, value)` pairs.  The current
  implementation reads drop-ins only to compute idempotency; a
  dedicated parser is not yet exposed.

- [x] 10. Implement `_set_sysctl_line(name, value)` and
  header-comment-prefixed body for empty drop-ins.  The
  fully-rendered new-file path uses `_set_sysctl_line` against
  empty content rather than a separate
  `_render_new_dropin_file` helper.

- [ ] 11. Implement `_compute_dropin_diff(old_content, new_content,
  path)` for dry-run diff output

## UbuntuKernelParameterManager — Implemented

- [x] 12. Implement `__init__` accepting runner and optional config.
  Implementation deviates from the spec: file I/O is performed
  through the same `ICommandRunner` (`cat` / `tee`) rather than
  via a separate `IFileOperator`.  The deviation is documented in
  the module docstring and matches the established Ubuntu
  pattern for service / repository / package management.

- [x] 13. Implement `get_parameter(name)` — `sysctl -n`, return
  `UbuntuParameterValue` or `None` for cannot-stat, raise
  `UbuntuKernelParameterQueryError` for other failures

- [x] 14. Implement `list_parameters()` — `sysctl -a --ignore`,
  parse with `_parse_sysctl_a`, raise on non-zero exit

- [x] 15. Implement `set_parameter(name, value, persistent)` —
  allowlist check, idempotency check(s), runtime dispatch,
  optional persistent drop-in file write + runtime apply

- [x] 16. Implement `reload_parameters()` — `sysctl --system`

- [x] 17. Implement `remove_persisted_parameter(name)` — drop-in
  file read, rewrite via `tee`, idempotent when file or key
  absent.  Empty-after-removal files are NOT auto-deleted in
  this pilot; tracked in Follow-ups.

- [x] 18. Implement `_dispatch_runtime_set` helper logic
  (`_set_parameter_runtime_only`).

- [x] 19. Implement `_dispatch_persistent_set` helper logic
  (`_set_parameter_persistent`).

## UbuntuKernelParameterManager — Gaps

- [ ] 20. Mass-apply from a policy document (YAML / TOML) — NOT
  implemented; out of scope

- [ ] 21. Parameter-discovery metadata (type, range, default) — NOT
  implemented; out of scope

- [ ] 22. Boot-time parameter editing (grub) — NOT implemented;
  separate feature

## Tests — Implemented

- [x] 23. `test_get_parameter_returns_value_on_success`
- [x] 24. `test_get_parameter_returns_none_on_cannot_stat`
- [x] 25. `test_get_parameter_raises_on_unexpected_failure`
- [x] 26. `test_list_parameters_parses_mixed_output`
- [x] 27. `test_list_parameters_raises_on_non_zero_exit`
- [x] 28. `test_set_runtime_skipped_when_value_matches`
- [x] 29. `test_set_runtime_dispatches_sysctl_w`
- [x] 30. `test_set_persistent_writes_dropin_and_applies_runtime`
- [x] 31. `test_set_persistent_skipped_when_file_and_runtime_match`
- [x] 32. `test_set_rejects_non_allowlisted_prefix`
- [x] 33. `test_set_persistent_partial_failure_reports_step`
- [x] 34. `test_reload_dispatches_sysctl_system`
- [x] 35. `test_remove_persisted_skipped_when_file_absent`
- [x] 36. `test_remove_persisted_rewrites_file_or_deletes_when_empty`
  (covers rewrite path; empty-file delete is a follow-up)

## Tests — Gaps

- [ ] 37. Test dry-run populates `diff` for persistent writes
  (currently `diff` is reserved on the result; populating it
  requires the unified-diff helper noted in Gap 11)

- [ ] 38. Test dry-run still executes read-only idempotency guards

- [ ] 39. Test elevation is set for writes when config enables it

- [ ] 40. Test elevation is NEVER set for read-only operations

- [ ] 41. Test drop-in file header comment is present on newly
  written files

- [ ] 42. Property-based tests (hypothesis) covering Properties
  1 through 28

## Follow-ups

- [ ] 43. **Cross-OS contract conformance review**: verify that
  this implementation's data classes match the cross-OS contract
  at `automojo-sysman-common/specs/contracts/kernel-parameters/contract.md`
  exactly, including `requires_reboot=False` on every Ubuntu
  result.

- [ ] 44. **Allowlist maintenance**: document a process for adding
  new accepted prefixes (e.g., `crypto.`) when upstream kernel
  tunable namespaces evolve.

- [ ] 45. **Empty-file cleanup for `remove_persisted_parameter`**:
  when the rewrite would leave only the
  `# managed by automojo-sysman-linux-ubuntu` header, delete the
  drop-in entirely so the directory listing stays clean.

- [ ] 46. **Unified-diff dry-run output**: populate the
  `UbuntuKernelParameterActionResult.diff` field for persistent
  dry-run writes using `difflib.unified_diff` against the prior
  drop-in content.

- [ ] 47. **`IFileOperator` migration**: swap the `cat` / `tee`
  runner pattern for a future `IFileOperator` abstraction once
  one ships in this codebase.  Tracked separately from the
  established Ubuntu pattern for service / repo / package
  management.
