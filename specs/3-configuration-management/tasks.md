# Implementation Plan — Ubuntu Configuration Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the
  `ccai.sysman.linux.ubuntu.configurationmanagement` module

- [x] 2. Define `UbuntuConfigActionStatus` enum

- [x] 3. Define `UbuntuConfigManagerConfig` dataclass.  Pilot
  fields: `dry_run`, `elevated` (inherited), plus
  `backup_suffix_format` and `backup_extension` for backup
  filename rendering.  The contract's
  `UbuntuConfigurationManagerConfig` name is a Follow-up alias.

- [x] 4. Define `UbuntuConfigActionResult` and `UbuntuSettingValue`
  dataclasses.  `UbuntuConfigDiffResult`,
  `UbuntuConfigValidationResult`, and `UbuntuConfigFileInfo` are
  NOT defined in this pilot — see Gaps.

- [x] 5. Define `UbuntuConfigQueryError` exception.  The other
  contract-mandated exceptions
  (`UbuntuConfigFileNotFoundError`,
  `UbuntuConfigTemplateError`,
  `UbuntuConfigValidatorNotFoundError`,
  `UbuntuConfigSettingNotFoundError`) are NOT defined in this
  pilot — see Gaps.

## ValidatorRegistry

- [ ] 6. Define `UBUNTU_VALIDATORS` module-level dict with nginx,
  sshd, apache2, named, rsyslog, sudoers, ufw, netplan entries.
  Validator surface deferred to the next iteration.

- [ ] 7. Implement `register_validator(id, builder)` runtime
  extension hook

## UbuntuConfigurationManager — Implemented

- [x] 8. Implement `__init__` accepting `runner` and optional
  `config`.  Implementation deviation: this manager takes only
  an `ICommandRunner`, not an `IFileOperator`, matching the
  established Ubuntu pattern.

- [ ] 9. Implement `_detect_format(source, format_override)`
  returning `"ini"`/`"yaml"`/`"shell"`.  Pilot supports shell
  only — see Gaps.

- [x] 10. Implement `_parse` / `_serialise` helpers for the shell
  format only (`_parse_shell_kv` / `_set_shell_kv` /
  `_remove_shell_kv`).  INI / YAML support is a follow-up.

- [ ] 11. Implement `_stat(source)` parser mapping
  `stat -c "%a %U %G"` to `UbuntuConfigFileInfo`

- [x] 12. Implement `read_setting(source, key)` raising
  `UbuntuConfigQueryError` for missing source or key.  The
  contract calls for `UbuntuConfigSettingNotFoundError` here;
  alignment is a Follow-up.

- [x] 13. Implement `write_setting(source, key, value)` with
  idempotency guard and dry-run short-circuit.  Dry-run does
  not yet populate `diff` — see Gaps.

- [x] 14. Implement `remove_setting(source, key)` with
  idempotency guard

- [x] 15. Implement `list_settings(source)` returning a
  `dict[str, str]`.  The contract calls for an order-preserving
  `list[SettingValue]`; pilot returns dict for parity with
  other Ubuntu managers.  Reconciliation tracked as a
  Follow-up.

- [ ] 16. Implement `write(source, content, mode, owner, group)`
  with idempotency guard and permission/ownership follow-up

- [ ] 17. Implement `diff(source, desired_content)` using
  `difflib.unified_diff`

- [ ] 18. Implement `validate(source, validator_id)` dispatching
  via the registry

- [x] 19. Implement `backup_configuration(source)` with
  timestamped backup path and elevation

- [x] 20. Implement `restore_configuration(backup_path,
  target_path)` with elevation.  The contract spells the
  signature as `restore_configuration(backup_path)`; this
  implementation requires both the source and the destination
  because the backup-suffix is configurable and may not be
  reversible.  Tracked as a Follow-up.

- [ ] 21. Implement `set_permissions(source, mode)` with
  idempotency guard

- [ ] 22. Implement `set_ownership(source, user, group)` with
  idempotency guard

- [x] 23. Implement `reload_configuration(service)` via
  `systemctl reload <service>`

## UbuntuConfigurationManager — Gaps (specified but not implemented)

- [ ] 24. Template rendering helper (`write_template`) using
  `string.Template`

- [ ] 25. INI / YAML format support — pilot is shell-only
  (`KEY=VALUE`, `KEY="VALUE"`).  Plan: add `_parse_ini` /
  `_set_ini_key` mirroring the RHEL reference; add
  `_parse_yaml` / `_set_yaml_key` once the dependency story is
  pinned.

- [ ] 26. Structured JSON output parsing for validators (today
  only exit code is consumed)

- [ ] 27. Automatic rollback on `validate(..., valid=False)` —
  left to the coordinator

- [ ] 28. Whole-file `write(...)` verb with permission /
  ownership follow-up.  This is verb 16 above; tracked here
  because the gap is multi-step.

- [ ] 29. Diff / validation / file-info models
  (`UbuntuConfigDiffResult`, `UbuntuConfigValidationResult`,
  `UbuntuConfigFileInfo`) and the corresponding exception
  classes.

## Tests — Implemented

- [ ] 30. `test_read_setting_parses_ini_value`
- [ ] 31. `test_read_setting_parses_yaml_value`
- [x] 32. `test_read_setting_parses_shell_value`
- [x] 33. `test_read_setting_missing_key_raises_setting_not_found`
  (raises `UbuntuConfigQueryError` rather than the contract's
  `UbuntuConfigSettingNotFoundError`)
- [x] 34. `test_read_setting_missing_source_raises_setting_not_found`
  (same caveat as above)
- [x] 35. `test_write_setting_skipped_when_value_matches`
- [x] 36. `test_write_setting_creates_missing_source`
  (`test_write_setting_creates_missing_file`)
- [x] 37. `test_write_setting_dry_run_short_circuits`
  (does not yet populate `diff`; see Gap)
- [x] 38. `test_remove_setting_skipped_when_key_absent` and
  `test_remove_setting_skipped_when_file_missing`
- [x] 39. `test_list_settings_returns_dict` covering populated
  source plus missing-source empty case
- [ ] 40. `test_write_applies_chmod_and_chown_when_needed`
- [ ] 41. `test_diff_source_missing_returns_full_addition`
- [ ] 42. `test_validate_unknown_id_raises_validator_not_found`
- [ ] 43. `test_validate_returns_valid_true_on_exit_zero`
- [x] 44. `test_backup_configuration_dispatches_cp` covering the
  timestamped backup path
- [x] 45. `test_restore_configuration_dispatches_cp`
- [ ] 46. `test_set_permissions_skipped_when_mode_matches`
- [x] 47. `test_reload_configuration_invokes_systemctl`

## Tests — Gaps

- [ ] 48. Dry-run matrix test covering every write verb asserting
  zero mutating calls.  Pilot covers `write_setting` and
  `backup_configuration` individually.

- [ ] 49. Elevation matrix test confirming writes elevate and
  reads do not.  Pilot covers reads via
  `test_reads_never_elevate` and individual write verbs assert
  `elevated=True`.

- [ ] 50. Parser robustness on malformed INI / YAML inputs raising
  `UbuntuConfigQueryError` (depends on Gap 25).

## Follow-ups

- [ ] 51. **Cross-OS contract conformance review**: align the
  config-class name (`UbuntuConfigManagerConfig` →
  `UbuntuConfigurationManagerConfig`), exception set
  (`UbuntuConfigFileNotFoundError` etc.), and `list_settings`
  return shape with the contract.

- [ ] 52. **Validator registry implementation**: ship
  `UBUNTU_VALIDATORS` plus `register_validator()` and the
  `validate(source, validator_id)` verb.

- [ ] 53. **INI and YAML format support**: extend `_parse` /
  `_serialise` so the manager handles `/etc/sysctl.d/*.conf`,
  systemd unit files (INI-ish), and netplan / cloud-init
  configs (YAML).

- [ ] 54. **Whole-file `write()` verb**: ship the
  content-and-permissions mutation path that the contract calls
  out separately from `write_setting`.

- [ ] 55. **`restore_configuration` single-arg signature**:
  reconcile the contract's `restore_configuration(backup_path)`
  with the pilot's `(backup_path, target_path)` form by deriving
  `target_path` from the backup name when an extension matches
  `backup_extension`.

- [ ] 56. **codecraftai-sysman-linux-ubuntu reuse check**: confirm
  that every other feature which touches `/etc/*` delegates to
  `UbuntuConfigurationManager.write_setting` rather than writing
  directly through `tee`.
