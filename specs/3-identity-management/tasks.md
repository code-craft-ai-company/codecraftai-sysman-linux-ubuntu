# Implementation Plan — Ubuntu Identity Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work tracked for follow-up batches.

## Module and Models

- [x] 1. Create the `automojo.sysman.linux.ubuntu.identitymanagement`
  module.

- [x] 2. Define `UbuntuIdentityActionStatus` (SUCCESS / FAILED /
  SKIPPED / DRY_RUN).
  *Aliased to the shared `featurebase.ActionStatus` to honour the
  contract's prefixed-naming requirement without forking the enum.*

- [x] 3. Define `UbuntuIdentityManagerConfig` dataclass (dry_run,
  elevated, realm_executable, sssctl_executable, getent_executable,
  default_realm).

- [x] 4. Define `UbuntuIdentityActionResult` dataclass with `realm`,
  `username`, and `message` fields layered on top of
  `FeatureActionResult` (status, detail, exit_code, stdout, stderr,
  command).

- [x] 5. Define `UbuntuRealmKind` enum with `FREEIPA`,
  `ACTIVE_DIRECTORY`, `LDAP`, and `UNKNOWN` values.

- [x] 6. Define `UbuntuRealmJoinSpec` dataclass with `realm`,
  `admin_user`, `admin_password`, `computer_name`, `ou_path`, and
  `domain_controller` fields.

- [x] 7. Define `UbuntuRealmInfo`, `UbuntuRealmStatus`,
  `UbuntuUserResolveResult`, `UbuntuGroupResolveResult` dataclasses.

- [x] 8. Define `UbuntuIdentityQueryError` exception.

- [ ] 9. Define `UbuntuIdentityCredentialError` exception.
  *Deferred — `UbuntuRealmJoinSpec.admin_password` is a required
  field, so the missing-credential case is structurally impossible
  in the current API.  Will be re-introduced when the cross-OS
  credential abstraction lands.*

- [ ] 10. Define `UbuntuIdentityRealmNotFoundError` exception.
  *Deferred — the in-scope verbs do not need the typed exception;
  `leave_realm` reports SKIPPED for unknown realms and `resolve_*`
  returns `found=False`.*

## UbuntuIdentityManager — Implemented

- [x] 11. Implement `__init__` accepting runner + optional config.
  *Inherited from `BaseFeatureManager.__init__`.*

- [x] 12. Implement `join_realm(spec)` — `realm join --user=<admin>
  <realm>` with optional `--computer-name` / `--computer-ou` /
  `--server` flags.  Password rides on stdin; argv is sanitised by
  construction (the password never lands on argv).  Idempotent via
  `realm list`.

- [x] 13. Implement `leave_realm(realm=None)` — `realm leave
  [<realm>]`.  Falls back to `config.default_realm` when the caller
  passes `None`; idempotent via `realm list` for the named-realm
  case.

- [x] 14. Implement `get_realm_status()` — `realm list` parsed.
  Reports `UbuntuRealmStatus(joined=False)` for the empty case;
  otherwise reports the first stanza's realm + kind +
  `computer-name`.

- [x] 15. Implement `list_joined_realms()` — `realm list` parsed
  multi-stanza.  Empty stdout → empty list.  Non-zero exit →
  `UbuntuIdentityQueryError`.

- [x] 16. Implement `sync_users()` — `sssctl cache-expire -E`.  Not
  idempotent.

- [x] 17. Implement `resolve_user(username)` — `getent passwd
  <username>` parsed colon-separated.  Detects realm suffix in the
  first field.  Returns `found=False` on `getent` non-zero.

- [x] 18. Implement `resolve_group(groupname)` — `getent group
  <groupname>` parsed colon-separated.  Returns `found=False` on
  `getent` non-zero.

- [x] 19. Implement `_classify_realm_kind` mapping `realm list`
  `server-software` values to `UbuntuRealmKind`.

- [x] 20. Implement `_parse_realm_list_output` — multi-stanza
  parser tracking `current_name` / `current_attributes`.

- [x] 21. Implement `_extract_computer_name_for_realm` — second
  pass over `realm list` output to pull the per-realm
  `computer-name` line.

- [x] 22. Implement `_parse_getent_passwd_line` and
  `_parse_getent_group_line`.

- [x] 23. Implement `_extract_realm_from_principal` — splits on
  rightmost `@` to detect realm suffix on directory-backed
  principals.

## Tests — Implemented

- [x] 24. `_MockCommandRunner` recording every `run` call.

- [x] 25. `test_join_realm_invokes_realm_join` — argv shape +
  `elevated=True` + `stdin=password`.

- [x] 26. `test_join_realm_emits_optional_placement_flags` —
  `--computer-name`, `--computer-ou`, `--server`.

- [x] 27. `test_join_realm_skipped_when_joined` — idempotency
  guard returns `SKIPPED`.

- [x] 28. `test_join_realm_dry_run` — `DRY_RUN` without invoking
  the runner.

- [x] 29. `test_leave_realm_invokes_realm_leave`.

- [x] 30. `test_leave_realm_skipped_when_not_joined`.

- [x] 31. `test_get_realm_status_parses_realm_list_output`.

- [x] 32. `test_get_realm_status_returns_unjoined_when_empty`.

- [x] 33. `test_list_joined_realms_returns_list` — multi-realm
  parsing.

- [x] 34. `test_sync_users_invokes_sssctl`.

- [x] 35. `test_resolve_user_parses_getent_output` — directory-
  backed record with realm suffix.

- [x] 36. `test_resolve_user_local_no_realm` — local record
  without suffix.

- [x] 37. `test_resolve_user_returns_not_found_when_unknown`.

- [x] 38. `test_resolve_group_parses_getent_output`.

- [x] 39. `test_password_redacted_in_recorded_command` — argv MUST
  NOT contain the password; stdin DOES.

- [x] 40. `test_writes_elevate` — every write verb passes
  `elevated=True` when configured.

- [x] 41. `test_reads_never_elevate` — every read verb passes
  `elevated=False`.

- [x] 42. `test_failed_command_surfaces_stderr` — non-zero
  `realm join` exit propagates exit_code + stderr.

- [x] 43. `test_list_joined_realms_raises_on_query_failure` —
  `UbuntuIdentityQueryError` on `realm list` non-zero exit.

## Tracked Follow-Ups

- [ ] 44. `discover_realm(domain)` — `realm discover <domain>`
  parsed into a `UbuntuRealmDiscovery` record.

- [ ] 45. SSSD config-file surface (`read_sssd_conf`,
  `write_sssd_dropin`, `set_sssd_directive`,
  `remove_sssd_directive`, `config_check`).  Lifted from RHEL.

- [ ] 46. Kerberos config-file surface (`read_krb5_conf`,
  `set_default_realm`, `add_kdc`, `remove_kdc`).  Lifted from RHEL.

- [ ] 47. Per-user Kerberos ticket lifecycle (`kinit`, `klist`,
  `kdestroy`).  Lifted from RHEL.

- [ ] 48. `IdentityCredentialError` integration once the
  cross-OS credential abstraction lands.

- [ ] 49. NSS source-ordering seam with
  `3-authentication-management` (when it lands on Ubuntu).

- [ ] 50. `restart_sssd` delegation to `3-service-management`
  (when it lands on Ubuntu).

- [ ] 51. `realm` / `sssctl` tool-availability probe with a
  typed `UbuntuIdentityToolNotAvailableError`.

- [ ] 52. Realm-discovery cache so `get_realm_status` can avoid
  the second `realm list` invocation when populating
  `computer_name`.
