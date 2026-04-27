# Implementation Plan — Ubuntu Samba Server Management

All tasks are unchecked — new feature.

## Module and Models

- [ ] 1. Create `source/packages/automojo/sysman/linux/ubuntu/sambaservermanagement.py`
- [ ] 2. Define module-level constants (`_CAT`, `_TEE`, `_SMBCONTROL`,
  `_SMBPASSWD`, `_PDBEDIT`, default smb.conf path, default service
  name, reserved section names).
- [ ] 3. Define `UbuntuSambaActionStatus` alias of `ActionStatus`.
- [ ] 4. Define `UbuntuSambaManagerConfig` dataclass extending
  `FeatureConfig` with `smb_conf` and `service_name`.
- [ ] 5. Define `UbuntuSambaActionResult` dataclass extending
  `FeatureActionResult` with `share_name`, `username`, `message`.
- [ ] 6. Define `UbuntuSambaShareSpec`, `UbuntuSambaShareInfo`,
  `UbuntuSambaShareAcl`, `UbuntuSambaUserInfo` dataclasses.
- [ ] 7. Define `UbuntuSambaQueryError` with `command` /
  `exit_code` / `stderr` / `os.linesep`-joined message.

## Helpers — file I/O

- [ ] 8. Implement `_read_file(path)` helper invoking `cat` without
  elevation and returning `str | None`.

## Helpers — parser / renderer

- [ ] 9. Implement `_parse_smb_conf(content)` that splits the file
  into ordered sections by `[name]` headers, preserving comments
  and blank lines inside each section.
- [ ] 10. Implement `_render_smb_conf(sections)` that renders the
  section list back to a canonical INI body.
- [ ] 11. Implement `_section_to_share_info(section)` that maps a
  parsed section into a `UbuntuSambaShareInfo`.
- [ ] 12. Implement `_share_spec_to_section(spec)` that renders a
  `UbuntuSambaShareSpec` into the canonical `[name]` block.
- [ ] 13. Implement `_specs_match(existing, spec)` for the share-
  level idempotency comparison (order-insensitive on list-valued
  directives).
- [ ] 14. Implement `_acl_match(existing_section, acl)` for the
  permissions-level idempotency comparison.
- [ ] 15. Implement `_apply_acl(section, acl)` rewriting the
  permission keys.
- [ ] 16. Implement `_parse_pdbedit_output(text)` returning a
  `list[UbuntuSambaUserInfo]`.

## Manager — lifecycle

- [ ] 17. Implement `UbuntuSambaServerManager` class extending
  `BaseFeatureManager` with `result_class = UbuntuSambaActionResult`,
  `feature_name = "samba-server-management"`.
- [ ] 18. Implement `__init__(runner, config=None)` defaulting
  `config` to a fresh `UbuntuSambaManagerConfig` when `None`.

## Manager — write verbs

- [ ] 19. Implement `add_share(spec)` per design.md.
  - _Requirements: 1, 13.1_
- [ ] 20. Implement `remove_share(name)` per design.md.
  - _Requirements: 2, 13.2_
- [ ] 21. Implement `set_share_permissions(name, acl)` per design.md.
  - _Requirements: 5, 13.3_
- [ ] 22. Implement `reload_config()` per design.md.
  - _Requirements: 6, 13.6_
- [ ] 23. Implement `add_samba_user(name, password)` per design.md.
  - _Requirements: 7, 13.4_
- [ ] 24. Implement `remove_samba_user(name)` per design.md.
  - _Requirements: 8, 13.5_

## Manager — read verbs

- [ ] 25. Implement `list_shares()` per design.md.
  - _Requirements: 3_
- [ ] 26. Implement `get_share(name)` per design.md.
  - _Requirements: 4_
- [ ] 27. Implement `list_samba_users()` per design.md.
  - _Requirements: 9_

## Manager — internal helpers

- [ ] 28. Implement `_invoke_smbcontrol_reload()` that dispatches
  `smbcontrol all reload-config` elevated and returns the
  `UbuntuSambaActionResult` (used by add/remove/set chains).
- [ ] 29. Implement `_invoke_pdbedit_list()` returning the parsed
  user list for idempotency probes.
- [ ] 30. Implement `_build_result(...)` and `_wrap_runner_result(...)`.

## Tests

- [ ] 31. Create
  `source/testroots/testbase/automojo/tests/sysman/linux/ubuntu/test_sambaservermanagement.py`
  with `_MockCommandRunner` + `_seed_cat` helpers.
- [ ] 32. Add `test_add_share_invokes_smbcontrol`.
- [ ] 33. Add `test_add_share_skipped_when_present`.
- [ ] 34. Add `test_add_share_dry_run`.
- [ ] 35. Add `test_remove_share_invokes_smbcontrol`.
- [ ] 36. Add `test_list_shares_parses_output`.
- [ ] 37. Add `test_get_share_returns_correct_record`.
- [ ] 38. Add `test_set_share_permissions_invokes_smbcontrol`.
- [ ] 39. Add `test_reload_config_invokes_smbcontrol`.
- [ ] 40. Add `test_add_samba_user_passes_password_via_stdin`.
- [ ] 41. Add `test_remove_samba_user_invokes_smbpasswd`.
- [ ] 42. Add `test_list_samba_users_parses_pdbedit_output`.
- [ ] 43. Add `test_writes_elevate`.
- [ ] 44. Add `test_reads_never_elevate`.
- [ ] 45. Add `test_failed_command_surfaces_stderr`.
- [ ] 46. Add `test_password_redacted_in_recorded_command`.

## Follow-ups

- [ ] 47. **Drop-in support.** Extend the manager to write under
  `/etc/samba/smb.conf.d/<name>.conf` for callers who prefer drop-
  ins over a monolithic `smb.conf`.
- [ ] 48. **`testparm` validation.** Add a `validate_config()` read
  verb that wraps `testparm -s --suppress-prompt` and a pre-write
  validation gate inside `add_share` / `set_share_permissions`.
- [ ] 49. **`net usershare` integration.** Expose a per-user share
  surface for non-root callers.
- [ ] 50. **AD join.** Mirror the RHEL `net ads` surface for
  domain-joined Ubuntu hosts.
