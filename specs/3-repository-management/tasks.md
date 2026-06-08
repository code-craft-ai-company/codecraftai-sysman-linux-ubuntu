# Implementation Plan — Ubuntu Repository Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.linux.ubuntu.repositorymanagement`
  module

- [x] 2. Define `UbuntuRepositoryActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN)

- [ ] 3. Define `UbuntuRepositoryManagerConfig` dataclass (dry_run,
  elevated, repos_dir, keyrings_dir, preferences_dir)
  *Implemented with `dry_run`, `elevated`, `sources_list_d`, and
  `main_sources_list`.  `keyrings_dir` and `preferences_dir` are
  unused in this batch (no key import / pinning surface yet) and the
  source-file directory is named `sources_list_d` per the user's
  pilot brief.*

- [x] 4. Define `UbuntuRepositorySpec` and `UbuntuRepositoryInfo`
  dataclasses
  *Implemented per the user's pilot brief (`name`, `uri`, `suites`,
  `components`, `types`, `signed_by`, `architectures` /
  `enabled`, `path`, `uri`, `suites`, `components`, `types`).  The
  contract-parity fields `display_name`, `mirror_list_url`, and
  `priority` are absent — see deviation note.*

- [x] 5. Define `UbuntuRepositoryActionResult` dataclass
  *Implemented with `name` and `path` extras on top of the base
  `FeatureActionResult`.*

- [ ] 6. Define `UbuntuRepositoryQueryError`,
  `UbuntuRepositoryNotFoundError`, and
  `UbuntuRepositoryKeyFileNotFoundError` exceptions
  *Implemented `UbuntuRepositoryQueryError`.  The other two exceptions
  are not implemented because `set_priority` and `import_gpg_key` were
  descoped from this batch.*

## Parsing and Rendering Helpers

- [x] 7. Implement `_render_source_line(spec, keyring_path)` returning
  the `deb [signed-by=...] <url> <channel>` string
  *Implemented as `_render_one_line_sources(spec)`; the spec's
  `signed_by` field is rendered into the `[signed-by=...]` option
  along with `arch=` when `architectures` is set.*

- [x] 8. Implement `_parse_source_file(content)` returning a
  `dict{url, components, options}` for the first valid `deb` line
  and silently skipping malformed lines
  *Implemented as `_parse_one_line_sources(content)` returning
  `{uri, suites, components, types}`.  The bracketed
  `[options]` block is consumed and discarded (multi-token forms are
  handled).*

- [ ] 9. Implement `_render_pinning_file(url, priority)` extracting
  the URL's hostname via `urlparse` and rendering the
  `Package/Pin/Pin-Priority` block

- [ ] 10. Implement `_parse_apt_cache_policy(stdout)` extracting
  `(priority, url, suite)` tuples per block

- [ ] 11. Implement `_correlate_priority_to_file(policy_rows,
  source_files)` for best-effort priority lookup

- [ ] 12. Implement `_is_ppa(name)` returning True for names
  starting `ppa:`

- [ ] 13. Implement `_keyring_name_from_source(key_source)` deriving
  `<name>.gpg` from a URL's last path segment or a local file's
  stem

## UbuntuRepositoryManager — Implemented

- [x] 14. Implement `__init__` accepting runner, file_operator, and
  optional config
  *Implemented inheriting `BaseFeatureManager.__init__`; `runner` is
  injected, file I/O is performed through the runner via
  `cat`/`tee`/`rm`/`mv`/`ls`/`test -e` rather than a separate
  `IFileOperator`.*

- [x] 15. Implement `add_repository(spec)` — PPA path, file path,
  optional key import, byte-identical idempotency
  *File path with byte-identical idempotency is implemented.  PPA
  short-circuit and key import are out of scope for this batch.*

- [x] 16. Implement `remove_repository(name)` — PPA path, file path,
  handles `.list.disabled`, idempotent when absent
  *File path with `.list.disabled` fallback and SKIPPED-when-absent
  idempotency is implemented.  PPA short-circuit is out of scope.*

- [x] 17. Implement `enable_repository(name)` — rename via read +
  write + delete, SKIPPED / FAILED branches
  *Implemented via a single `mv` invocation rather than read +
  write + delete.  SKIPPED-when-already-enabled and
  FAILED-when-not-found branches are present.*

- [x] 18. Implement `disable_repository(name)` — rename via read +
  write + delete, SKIPPED / FAILED branches
  *Implemented via a single `mv` invocation.  SKIPPED-when-already-
  disabled and FAILED-when-not-found branches are present.*

- [x] 19. Implement `list_repositories()` — enumerate files + invoke
  `apt-cache policy`, parse, correlate priorities
  *Implemented as a directory enumeration plus per-file parse.  The
  `apt-cache policy` priority correlation is out of scope for this
  batch — the `UbuntuRepositoryInfo` shape does not carry a
  `priority` field yet.*

- [ ] 20. Implement `get_repository(name)` — enumerate + filter

- [x] 21. Implement `refresh_repositories(name=None)` — both single
  and all-sources variants, no idempotency guard
  *Implemented with the all-sources variant only (`apt-get update`).
  The single-source variant is out of scope for this batch.*

- [ ] 22. Implement `set_priority(name, priority)` — raise
  `UbuntuRepositoryNotFoundError` when absent, byte-identical
  idempotency

- [ ] 23. Implement `import_gpg_key(key_source)` — URL via `curl |
  gpg --dearmor`, local file via read_bytes + stdin dearmor, raise
  `UbuntuRepositoryKeyFileNotFoundError` for missing local,
  byte-identical idempotency

## UbuntuRepositoryManager — Gaps

- [ ] 24. deb822-format source rendering — NOT implemented; future
  enhancement

- [ ] 25. Repository mirroring / local mirror hosting — NOT
  implemented; separate feature

- [ ] 26. Client-certificate authentication — NOT implemented; out
  of scope

- [ ] 27. Mass-apply from a policy document — NOT implemented; out
  of scope

## Tests — Implemented

- [ ] 28. `test_add_ppa_dispatches_add_apt_repository`
- [x] 29. `test_add_file_writes_deb_line`
  *Implemented as `test_add_repository_writes_sources_file`.*
- [x] 30. `test_add_repository_idempotent_when_identical`
  *Implemented as `test_add_repository_skipped_when_content_matches`.*
- [ ] 31. `test_add_repository_rendering_deterministic`
- [ ] 32. `test_remove_ppa_dispatches_add_apt_repository_r`
- [x] 33. `test_remove_file_deletes_list_or_disabled`
  *Implemented as `test_remove_repository_deletes_file`.*
- [x] 34. `test_remove_repository_idempotent_when_absent`
  *Implemented as `test_remove_repository_skipped_when_not_found`.*
- [x] 35. `test_enable_repository_renames_disabled_file`
- [x] 36. `test_enable_repository_skipped_when_already_enabled`
- [ ] 37. `test_enable_repository_failed_when_not_found`
  *Equivalent symmetry test exists for disable —
  `test_disable_repository_failed_when_not_found`.  An explicit
  enable-side variant is not yet implemented.*
- [x] 38. `test_disable_repository_renames_list_file`
- [ ] 39. `test_disable_repository_skipped_when_already_disabled`
- [x] 40. `test_disable_repository_failed_when_not_found`
- [x] 41. `test_list_repositories_enumerates_files`
  *Implemented as `test_list_repositories_parses_sources_list_d`.*
- [x] 42. `test_list_repositories_enabled_flag_by_suffix`
  *Covered by
  `test_list_repositories_parses_sources_list_d`, which asserts
  `enabled=True` for `.list` and `enabled=False` for
  `.list.disabled`.*
- [ ] 43. `test_list_repositories_skips_malformed_lines`
- [ ] 44. `test_list_repositories_empty_dir_no_runner`
- [ ] 45. `test_list_repositories_raises_on_policy_failure`
- [ ] 46. `test_get_repository_returns_none_on_miss`
- [x] 47. `test_refresh_all_dispatches_apt_get_update`
  *Implemented as `test_refresh_repositories_invokes_apt_update`.*
- [ ] 48. `test_refresh_one_dispatches_single_source_variant`
- [ ] 49. `test_refresh_has_no_idempotency_guard`
- [ ] 50. `test_set_priority_raises_for_unknown_repo`
- [ ] 51. `test_set_priority_idempotent_when_file_matches`
- [ ] 52. `test_set_priority_renders_pin_block_with_hostname`
- [ ] 53. `test_import_gpg_key_url_dispatches_curl_dearmor`
- [ ] 54. `test_import_gpg_key_missing_local_raises`
- [ ] 55. `test_import_gpg_key_idempotent_when_byte_identical`

## Tests — Gaps

- [ ] 56. Test dry-run short-circuits for every mutating method
  *`test_dry_run_short_circuits_add` covers the add branch; the
  remove / enable / disable / refresh write verbs share the same
  dry-run guard but are not individually covered.*

- [ ] 57. Test dry-run still raises precondition exceptions
  (`UbuntuRepositoryNotFoundError`,
  `UbuntuRepositoryKeyFileNotFoundError`)

- [x] 58. Test elevation is set for writes when config enables it
  *Implemented as `test_write_verbs_pass_elevated_true`.*

- [ ] 59. Test elevation is NEVER set for read-only operations

- [ ] 60. Property-based tests (hypothesis) covering Properties
  1 through 32

## Follow-ups

- [ ] 61. **Cross-OS contract conformance review**: verify the data
  classes match the contract exactly and that PPA handling
  preserves the `name` round-trip through `list_repositories`.

- [ ] 62. **deb822 rendering enhancement**: plan a follow-up that
  emits `.sources` files in deb822 format for setups that prefer
  the modern source syntax.

- [ ] 63. **Key-source naming convention**: document how
  `_keyring_name_from_source` derives the stem so callers can
  predict the resulting path and attach it to their own `spec`.

## Spec deviations from this implementation

- **No `IFileOperator` collaborator.**  The drafted spec describes
  file I/O as flowing through an injected `IFileOperator`; the pilot
  batch performs every read / write / delete / rename / enumerate /
  exists check through the existing `ICommandRunner` (`cat`, `tee`,
  `rm`, `mv`, `ls`, `test -e`).  This matches the RHEL reference
  implementation, since no concrete `IFileOperator` exists in the
  codebase yet.  When `IFileOperator` lands, a refactor can swap the
  internal helpers without changing the public surface.

- **Config field naming.**  `repos_dir` is named `sources_list_d` and
  the system-wide single-file `/etc/apt/sources.list` is exposed as
  the `main_sources_list` field.  `keyrings_dir` and
  `preferences_dir` are not present because key import and pinning
  are out of scope for this batch.

- **Spec fields not implemented.**  `display_name`,
  `mirror_list_url`, and `priority` are absent from
  `UbuntuRepositorySpec` because the verbs that consume them
  (`set_priority`, contract-parity reads) are out of scope for this
  batch.  `UbuntuRepositoryInfo` similarly omits `display_name` and
  `priority`; it does carry the parsed `uri`, `suites`,
  `components`, and `types` derived from the source file.

- **Single-line `deb` format only.**  The renderer emits one-line
  `deb [...] <uri> <suite> <components>` form.  deb822 (`.sources`)
  rendering is reserved as a follow-up.

- **PPA path, key import, single-source refresh, and `set_priority`
  are out of scope for this batch.**  These are all flagged unchecked
  above and remain to be added in subsequent increments.
