# Implementation Plan — Ubuntu Environment Configuration

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the
  `ccai.sysman.linux.ubuntu.environmentconfiguration` module.
  Class is named `UbuntuEnvironmentConfigurationManager` (per the
  user-instruction naming convention) rather than the contract's
  `UbuntuEnvironmentConfigurator`.

- [x] 2. Define `UbuntuEnvActionStatus` and `UbuntuEnvScope` enums

- [x] 3. Define `UbuntuEnvironmentManagerConfig` dataclass
  (`dry_run`, `elevated`, `environment_file`,
  `profile_d_drop_in`, `user_profile_basename`)

- [x] 4. Define `UbuntuEnvValue` and `UbuntuEnvActionResult`
  dataclasses

- [x] 5. Define `UbuntuEnvQueryError` exception (named
  `UbuntuEnvQueryError` per user-instruction conventions; the
  spec calls for `UbuntuEnvConfigError` — alias is a follow-up)

## UbuntuEnvironmentConfigurator — Implemented

- [x] 6. Implement `__init__` accepting `runner` and optional
  `config`.  Implementation deviation: this manager takes an
  `ICommandRunner`, not an `IFileOperator`, matching the Ubuntu
  pattern established by service / repository / package /
  kernel-parameters managers.  All file I/O routes through
  `cat` / `tee` / `rm`.

- [x] 7. Implement `_parse_bare_env` parser for `/etc/environment`

- [x] 8. Implement `_parse_export_env` parser for profile.d
  drop-ins and user rc files

- [x] 9. Implement `_resolve_home` helper (via `getent passwd`)
  raising `UbuntuEnvQueryError` on unknown user

- [ ] 10. Implement `_is_current_user` helper.  Not implemented in
  the pilot — the simplification is that user-scope writes
  always honour `config.elevated`.  See Follow-ups.

- [x] 11. Implement line-rewrite helpers for the bare and shell
  formats (`_set_bare_env_line` / `_remove_bare_env_line` /
  `_set_export_line` / `_remove_export_line`) plus the
  user-scope sentinel-bracketed block helpers
  (`_set_user_block_export` / `_remove_user_block_export` /
  `_find_user_block`).

- [x] 12. Implement `set_system_env(name, value)` writing to both
  `/etc/environment` and `/etc/profile.d/ccai.sh` with
  drop-in creation path

- [x] 13. Implement `unset_system_env(name)` with drop-in empty-
  file cleanup

- [x] 14. Implement `get_system_env(name)` with
  `/etc/environment`-wins precedence

- [x] 15. Implement `list_system_env()` merging both files

- [x] 16. Implement `set_user_env(user, name, value)` with
  unknown-user `FAILED`, creation path, in-place block
  replacement.  Same-user elevation suppression is NOT yet
  implemented; see Follow-ups.

- [x] 17. Implement `unset_user_env(user, name)` with unknown-user
  `FAILED` and idempotency

- [ ] 18. Implement `get_user_env(user, name, rc_file)` raising
  `UbuntuEnvQueryError` on unknown user

- [ ] 19. Implement `list_user_env(user, rc_file)` returning empty
  dict on missing file

## UbuntuEnvironmentConfigurator — Gaps (specified but not implemented)

- [ ] 20. `pam_env.d` drop-in authoring beyond the basic
  `.pam_environment` opt-in

- [ ] 21. Variable interpolation (`${OTHER}`) — values are treated
  as opaque strings

- [ ] 22. `PATH`-aware prepend/append semantics — values are opaque

## Tests — Implemented

- [x] 23. `test_set_system_env_writes_both_files`
- [x] 24. `test_set_system_env_skipped_when_both_files_current`
- [ ] 25. `test_set_system_env_creates_dropin_with_shebang`
  (covered indirectly by `test_set_system_env_writes_both_files`
  — see Follow-ups for an explicit test).
- [x] 26. `test_set_system_env_dry_run_makes_no_writes`
- [x] 27. `test_unset_system_env_skipped_when_absent`
- [x] 28. `test_unset_system_env_rewrites_environment_file`
- [ ] 29. `test_unset_system_env_rewrites_dropin`
- [x] 30. `test_unset_system_env_deletes_dropin_when_only_shebang`
- [x] 31. `test_get_system_env_prefers_environment_file`
- [x] 32. `test_list_system_env_merges_both_files`
- [x] 33. `test_list_system_env_empty_when_both_missing`
- [x] 34. `test_set_user_env_failed_for_unknown_user`
- [x] 35. `test_set_user_env_creates_block_in_missing_profile`
- [x] 36. `test_set_user_env_skipped_when_block_already_matches`
- [ ] 37. `test_set_user_env_replaces_in_place_when_value_differs`
- [ ] 38. `test_set_user_env_suppresses_elevation_for_current_user`
  (depends on Item 10 / Follow-up 47)
- [ ] 39. `test_unset_user_env_failed_for_unknown_user`
- [x] 40. `test_unset_user_env_skipped_when_profile_missing`
- [x] 41. `test_unset_user_env_rewrites_block`
- [ ] 42. `test_list_user_env_returns_empty_when_file_missing`
  (depends on Items 18 / 19)

## Tests — Gaps

- [ ] 43. Dry-run matrix test covering every write verb asserting
  zero mutating runner calls

- [ ] 44. Elevation matrix test confirming system-scope writes
  elevate, reads do not, and same-user writes suppress
  elevation

- [ ] 45. Parser robustness on malformed lines ensuring those lines
  are skipped without raising

## Follow-ups

- [ ] 46. **Cross-OS contract conformance review**: rename the
  exception to `UbuntuEnvConfigError` (or alias) and verify the
  shipping dataclasses match the abstract names in
  `codecraftai-sysman-common/specs/contracts/environment-configuration/contract.md`.

- [ ] 47. **Same-user elevation suppression**: implement
  `_is_current_user` (or equivalent) so user-scope writes for
  the running user can opt out of `elevated=True`.  Needs a
  reliable cross-runner "current user" probe that works under
  both local and SSH execution.

- [ ] 48. **`get_user_env` / `list_user_env` read verbs**: add the
  user-scope read surface so callers can introspect a user's
  managed block without parsing the file themselves.

- [ ] 49. **Configurable user `rc_file`**: accept an `rc_file`
  argument on `set_user_env` / `unset_user_env` so callers can
  target `.bashrc` / `.pam_environment` in addition to the
  default `.profile`.

- [ ] 50. **codecraftai-sysman-linux-ubuntu reuse check**: confirm that
  features which need environment variables (for example, a
  future `3-locale-configuration`) delegate to
  `UbuntuEnvironmentConfigurationManager` rather than writing to
  `/etc/environment` directly.
