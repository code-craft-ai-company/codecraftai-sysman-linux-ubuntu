# Implementation Plan — Ubuntu NFS Server Management

All tasks are unchecked — new feature.

## Module and Models

- [ ] 1. Create `source/packages/automojo/sysman/linux/ubuntu/nfsservermanagement.py`
- [ ] 2. Define module-level constants (`_CAT`, `_TEE`, `_EXPORTFS`,
  default exports path, default service name, default options).
- [ ] 3. Define `UbuntuNfsActionStatus` alias of `ActionStatus`.
- [ ] 4. Define `UbuntuNfsManagerConfig` dataclass extending
  `FeatureConfig` with `exports_file` and `service_name`.
- [ ] 5. Define `UbuntuNfsActionResult` dataclass extending
  `FeatureActionResult` with `path`, `share_name`, `message`.
- [ ] 6. Define `UbuntuNfsClientSpec`, `UbuntuNfsExportSpec`,
  `UbuntuNfsExportInfo`, `UbuntuNfsExportStatus` dataclasses.
- [ ] 7. Define `UbuntuNfsQueryError` with `command` /
  `exit_code` / `stderr` / `os.linesep`-joined message.

## Helpers — file I/O

- [ ] 8. Implement `_read_file(path)` helper invoking `cat` without
  elevation and returning `str | None`.

## Helpers — parser / renderer

- [ ] 9. Implement `_parse_exports_file(content) →
  list[UbuntuNfsExportInfo]` that splits each non-comment, non-blank
  line into `path` + per-client tokens, with each client token
  parsed as `<host>(<opts>)`.
- [ ] 10. Implement `_render_exports_file(exports) → str` rendering
  the canonical Linux export-line shape.
- [ ] 11. Implement `_render_export_line(spec) → str` building a
  single canonical line for a `UbuntuNfsExportSpec`.
- [ ] 12. Implement `_parse_client_token(token) → UbuntuNfsClientSpec`.
- [ ] 13. Implement `_parse_exportfs_v_output(text) → dict[str,
  list[str]]` mapping path to flag-list.
- [ ] 14. Implement `_locate_export_line(content, path) →
  Optional[int]` returning the index of the line whose first token
  equals `path`.
- [ ] 15. Implement `_upsert_export(content, spec) → str` that
  rewrites the path's existing line or appends a new line.
- [ ] 16. Implement `_remove_export(content, path) → str` that
  strips every line whose first token equals `path`.
- [ ] 17. Implement `_set_options_for_path(content, path, options) →
  Optional[str]` that returns the rewritten content with new options
  on every client of the matched line, or `None` when no line
  matched.

## Manager — lifecycle

- [ ] 18. Implement `UbuntuNfsServerManager` class extending
  `BaseFeatureManager` with `result_class = UbuntuNfsActionResult`,
  `feature_name = "nfs-server-management"`.
- [ ] 19. Implement `__init__(runner, config=None)` defaulting
  `config` to a fresh `UbuntuNfsManagerConfig` when `None`.

## Manager — write verbs

- [ ] 20. Implement `add_export(spec)` per design.md.
  - _Requirements: 1, 7, 11.1_
- [ ] 21. Implement `remove_export(path)` per design.md.
  - _Requirements: 2, 11.2_
- [ ] 22. Implement `reload_exports()` per design.md.
  - _Requirements: 4, 11.4_
- [ ] 23. Implement `set_export_options(path, options)` per design.md.
  - _Requirements: 6, 11.3_

## Manager — read verbs

- [ ] 24. Implement `list_exports()` per design.md.
  - _Requirements: 3_
- [ ] 25. Implement `get_export_status(path)` per design.md.
  - _Requirements: 5_

## Manager — internal helpers

- [ ] 26. Implement `_invoke_exportfs_reload()` that dispatches
  `exportfs -ra` elevated and returns the
  :class:`UbuntuNfsActionResult` (used by add/remove/set).
- [ ] 27. Implement `_build_result(...)` and `_wrap_runner_result(...)`
  in line with the mailtransport pattern.

## Tests

- [ ] 28. Create
  `source/testroots/testbase/automojo/tests/sysman/linux/ubuntu/test_nfsservermanagement.py`
  with `_MockCommandRunner` + `_seed_cat` helpers.
- [ ] 29. Add `test_add_export_invokes_exportfs` exercising tee +
  reload.
- [ ] 30. Add `test_add_export_skipped_when_present`.
- [ ] 31. Add `test_add_export_dry_run`.
- [ ] 32. Add `test_remove_export_invokes_exportfs`.
- [ ] 33. Add `test_remove_export_skipped_when_absent`.
- [ ] 34. Add `test_list_exports_parses_exportfs_output`.
- [ ] 35. Add `test_reload_exports_invokes_exportfs`.
- [ ] 36. Add `test_get_export_status_parses_exportfs_output`.
- [ ] 37. Add `test_set_export_options_invokes_exportfs`.
- [ ] 38. Add `test_writes_elevate`.
- [ ] 39. Add `test_reads_never_elevate`.
- [ ] 40. Add `test_failed_command_surfaces_stderr`.
- [ ] 41. Add `test_export_line_renders_clients_correctly`.
- [ ] 42. Add `test_reload_failure_propagates`.

## Follow-ups

- [ ] 43. **Drop-in support.** Extend the manager to manage exports
  inside `/etc/exports.d/<name>.exports`.
- [ ] 44. **Option-canonicalisation.** Fold semantically-equivalent
  option orderings into a single canonical form before the
  idempotency check.
- [ ] 45. **`showmount -e localhost` integration.** Expose an
  `external_view()` read verb that queries the daemon's
  externally-visible export table.
- [ ] 46. **NFSv4 idmapping.** Add the `/etc/idmapd.conf` Domain
  directive surface to bring the Ubuntu manager into parity with the
  RHEL canonical implementation.
