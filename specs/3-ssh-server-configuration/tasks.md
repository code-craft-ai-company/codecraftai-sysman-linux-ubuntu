# Implementation Plan — Ubuntu SSH-Server-Configuration

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.linux.ubuntu.sshserverconfiguration`
  module

- [x] 2. Define `UbuntuSshActionStatus` alias of the shared
  `ActionStatus` enum (SUCCESS / FAILED / SKIPPED / DRY_RUN)

- [x] 3. Define `UbuntuSshServerConfigurationManagerConfig` dataclass
  (`dry_run`, `elevated`, `sshd_config_path`,
  `authorized_keys_path_template`, `service_name`)

- [x] 4. Define `UbuntuSshActionResult` dataclass (adds `keyword`,
  `path`, `user`, `message` fields over `FeatureActionResult`)

- [x] 5. Define `UbuntuSshSettingValue`, `UbuntuSshMatchBlock`, and
  `UbuntuSshAuthorizedKey` query dataclasses

- [x] 6. Define `UbuntuSshQueryError` exception (reserved for future
  read paths)

## UbuntuSshServerConfigurationManager — Implemented

- [x] 7. Implement `__init__` accepting runner + optional config
  (inherited from `BaseFeatureManager`)

- [x] 8. Implement `get_sshd_setting(keyword)` — `cat
  /etc/ssh/sshd_config` + parse + return first top-level match or
  `None`

- [x] 9. Implement `list_sshd_settings()` — `cat` + parse + return
  top-level dict

- [x] 10. Implement `set_sshd_setting(keyword, value)` — read, parse,
  upsert top setting, render, idempotency-skip on byte-equal,
  honour dry-run, dispatch `tee /etc/ssh/sshd_config`

- [x] 11. Implement `remove_sshd_setting(keyword)` — read, parse,
  drop every top-level occurrence (case-insensitive), render,
  idempotency-skip when absent or content-equal, dispatch `tee`

- [x] 12. Implement `add_match_block(criteria, settings)` — read,
  parse, upsert block (whitespace-collapsed criteria match),
  render, dispatch `tee`

- [x] 13. Implement `remove_match_block(criteria)` — read, parse,
  drop matching block, render, idempotency-skip when no match,
  dispatch `tee`

- [x] 14. Implement `list_match_blocks()` — `cat` + parse + return
  blocks in declaration order

- [x] 15. Implement `reload_sshd()` — dispatch `systemctl reload ssh`
  (Ubuntu unit name)

- [x] 16. Implement `test_sshd_config()` — dispatch `sshd -t`

- [x] 17. Implement `list_authorized_keys(user)` — resolve path from
  template, `cat`, parse with options + comment + fingerprint

- [x] 18. Implement `add_authorized_key(user, key)` — parse the
  supplied line (FAILED on parse failure), idempotency-skip by
  fingerprint, dispatch `tee` to user's path

- [x] 19. Implement `remove_authorized_key(user, fingerprint)` —
  drop every line whose computed fingerprint matches, dispatch
  `tee` (idempotency-skip when no match or file absent)

- [x] 20. Implement helper `_parse_sshd_config(content)` — returns
  `(top_settings, match_blocks)`

- [x] 21. Implement helper `_render_sshd_config(top_settings,
  match_blocks)` — deterministic rendering, four-space-indented
  Match-block bodies

- [x] 22. Implement helper `_parse_authorized_keys(content)` and
  `_parse_authorized_key_line(line)` — recognise common OpenSSH
  key types, capture options prefix and trailing comment

- [x] 23. Implement helper `_compute_fingerprint(key_data)` —
  SHA-256 of base64-decoded body, base64-encoded, padding stripped,
  prefixed with `SHA256:`

## UbuntuSshServerConfigurationManager — Gaps

- [ ] 24. Comment-line preservation across mutation.  The pilot
  renderer rebuilds the file deterministically and discards
  comments / blank lines.  A future revision can attach comment
  context to settings during parse and emit it during render.

- [ ] 25. Drop-in directory authoring (`/etc/ssh/sshd_config.d/`).
  Ubuntu's stock `sshd_config` includes
  `Include /etc/ssh/sshd_config.d/*.conf`.  The pilot edits the
  primary file only; an `add_dropin(name, settings)` /
  `remove_dropin(name)` extension is queued.

- [ ] 26. Pre-write `sshd -t` validation with rollback.  The contract
  invariant is that writes mutating `sshd_config` must validate via
  `sshd -t` and roll back on failure.  The pilot dispatches the
  write and exposes `test_sshd_config()` separately; a follow-up
  will (a) snapshot the file before write, (b) run `sshd -t` after,
  and (c) restore on validation failure.

- [ ] 27. Per-user authorized-key writes without elevation.  When
  `user` matches the runner's calling user, the manager could skip
  elevation.  Currently honours `config.elevated` uniformly.

- [ ] 28. Host-key generation / rotation (`ssh-keygen -t ...`).  Out
  of scope per the cross-OS contract; tracked here for future
  parity with the RHEL reference.

- [ ] 29. Banner-file authoring.  Out of scope per the contract; the
  base feature stops at sshd_config / Match-block / authorized-key
  surface.

## Tests — Implemented

- [x] 30. `test_get_sshd_setting_parses_value`
- [x] 31. `test_list_sshd_settings_parses_top_level`
- [x] 32. `test_list_match_blocks_parses_blocks`
- [x] 33. `test_list_authorized_keys_parses_user_keys`
- [x] 34. `test_set_sshd_setting_writes_when_absent`
- [x] 35. `test_set_sshd_setting_skipped_when_already_set`
- [x] 36. `test_set_sshd_setting_replaces_existing_value`
- [x] 37. `test_dry_run_short_circuits_set_sshd_setting`
- [x] 38. `test_remove_sshd_setting_skipped_when_absent`
- [x] 39. `test_remove_sshd_setting_rewrites_file`
- [x] 40. `test_add_match_block_appends_to_config`
- [x] 41. `test_remove_match_block_skipped_when_absent`
- [x] 42. `test_remove_match_block_rewrites_file`
- [x] 43. `test_reload_sshd_invokes_systemctl`
- [x] 44. `test_test_sshd_config_invokes_sshd_dash_t`
- [x] 45. `test_add_authorized_key_writes_when_absent`
- [x] 46. `test_add_authorized_key_skipped_when_already_present`
- [x] 47. `test_add_authorized_key_failed_on_parse_error`
- [x] 48. `test_remove_authorized_key_finds_by_fingerprint`
- [x] 49. `test_remove_authorized_key_skipped_when_fingerprint_absent`
- [x] 50. `test_writes_pass_elevated_true`
- [x] 51. `test_reads_never_elevate`
- [x] 52. `test_failed_command_returns_failed`
- [x] 53. `test_parser_handles_match_block_followed_by_eof`

## Tests — Gaps

- [ ] 54. Property-based hypothesis coverage for the design's
  correctness properties.

- [ ] 55. End-to-end test with a real `sshd_config` snapshot from a
  stock Ubuntu LTS install.

## Spec Deviations / Revisions Needed

- [ ] 56. **Service name divergence**: the contract's RHEL reference
  uses `sshd`; Ubuntu uses `ssh`.  This is captured in
  `UbuntuSshServerConfigurationManagerConfig.service_name` (default
  `"ssh"`).  No contract amendment is required because the
  contract's "Reload mechanism" column already lists the Ubuntu
  unit name explicitly.

- [ ] 57. **Type naming**: the contract uses `SshdSetting` /
  `SshdMatchBlock` / `AuthorizedKey` (with OS prefixes).  This
  module uses `UbuntuSshSettingValue` / `UbuntuSshMatchBlock` /
  `UbuntuSshAuthorizedKey`.  The `SettingValue` suffix tracks the
  prior wave-1 `UbuntuConfigSettingValue` / `UbuntuSettingValue`
  naming used by `configurationmanagement.py` and is preferred for
  consistency inside this package.

## Follow-ups

- [ ] 58. **Cross-OS contract conformance review**: verify that this
  implementation's `UbuntuSshActionResult` field shape matches the
  contract's `SshdActionResult` exactly once the RHEL reference is
  upgraded to the contract's `keyword` / `path` / `user` extras.

- [ ] 59. **Shared `featurebase` upstreaming**: the
  `ccai.sysman.linux.ubuntu.featurebase` module is vendored
  from RHEL.  Once the SSH manager is in place across all OSes
  the shared base can be lifted into a common package.
