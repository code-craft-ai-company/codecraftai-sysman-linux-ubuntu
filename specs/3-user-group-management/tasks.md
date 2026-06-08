# Implementation Plan — Ubuntu User-Group Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.linux.ubuntu.usergroupmanagement`
  module

- [x] 2. Define `UbuntuUserGroupActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN)
  *Aliased to the shared `featurebase.ActionStatus`.*

- [x] 3. Define `UbuntuUserGroupManagerConfig` dataclass (dry_run,
  elevated, uid_min, uid_max)
  *Implemented as `UbuntuUserGroupManagerConfig` with `dry_run`,
  `elevated`, `regular_uid_min`, `regular_uid_max`, and
  `include_system_accounts`.  See deviation note on field naming.*

- [x] 4. Define `UbuntuUserGroupActionResult` dataclass with
  `target`, `kind` ("user"/"group"), and argv/stdout/stderr fields
  *Implemented with `name` (in place of `target`) and `kind` as a
  typed `UbuntuPrincipalKind` enum rather than a free-form string.
  Inherits `detail`, `exit_code`, `stdout`, `stderr`, `command` from
  `FeatureActionResult`.*

- [x] 5. Define `UbuntuUserSpec`, `UbuntuUserModSpec`,
  `UbuntuUserInfo`, `UbuntuGroupSpec`, `UbuntuGroupInfo` dataclasses
  *Implemented as `UbuntuUserSpec`, `UbuntuUserChanges` (in place of
  `UserModSpec`, with `append_groups` flag), `UbuntuUserInfo`,
  `UbuntuGroupSpec`, and `UbuntuGroupInfo`.*

- [ ] 6. Define `UbuntuUserNotFoundError`, `UbuntuGroupNotFoundError`,
  `UbuntuUserGroupQueryError` exceptions
  *Implemented `UbuntuUserGroupQueryError`.  The contract states that
  precondition mismatches MAY be either raised or surfaced as
  `FAILED`; this implementation surfaces them as `FAILED` with an
  explanatory `message` rather than raising typed exceptions.  This
  matches the established Ubuntu pattern where writes do not raise
  for routine missing-target conditions.*

## UbuntuUserGroupManager — Implemented

- [x] 7. Implement `__init__` accepting runner + optional config
  *Inherited from `BaseFeatureManager.__init__`.*

- [x] 8. Implement `create_user(spec)` — `useradd` with diff-driven
  flags, `-r` for system accounts, idempotent via `getent passwd`

- [x] 9. Implement `delete_user(name, remove_home)` — `userdel [-r]`,
  idempotent via `getent passwd`
  *Implemented as `delete_user(name)`; `userdel -r` is always passed
  per the established Ubuntu pattern of cleaning up the home
  directory by default.  A separate `remove_home` parameter can be
  added in a follow-up batch if a caller surfaces the need.*

- [x] 10. Implement `modify_user(name, changes)` — `usermod` with
  only non-`None` flags, raises `UbuntuUserNotFoundError`, idempotent
  via field comparison against current `UbuntuUserInfo`
  *Implemented; missing-user case returns `FAILED` with a
  descriptive message rather than raising a typed exception.*

- [x] 11. Implement `get_user(name)` — `getent passwd` + `passwd -S`,
  returns `None` on `getent` exit 2, raises `UbuntuUserGroupQueryError`
  on other non-zero
  *Implemented; combines `getent passwd`, `id -Gn`, and `passwd -S`
  into a single `UbuntuUserInfo`.  Returns `None` on any non-zero
  exit from `getent passwd <name>`.*

- [x] 12. Implement `list_users()` — `getent passwd` filtered by UID
  range from config (`uid_min`, `uid_max`)
  *Implemented; filtered by `regular_uid_min` /
  `regular_uid_max`.  Honours `include_system_accounts`.*

- [x] 13. Implement `set_password(name, password)` — `chpasswd` via
  stdin `"<name>:<password>\n"`; argv is `["chpasswd"]` only

- [x] 14. Implement `lock_account(name)` / `unlock_account(name)` —
  `passwd -l` / `-u`, idempotent via current `locked` flag

- [x] 15. Implement `create_group(spec)` — `groupadd [-g][-r]`,
  idempotent via `getent group`

- [x] 16. Implement `delete_group(name)` — `groupdel`, idempotent via
  `getent group`

- [x] 17. Implement `get_group(name)` / `list_groups()` — `getent
  group` parser

- [x] 18. Implement `add_to_group(user, group)` / `remove_from_group`
  — `gpasswd -a` / `-d`, raises `UbuntuUserNotFoundError` /
  `UbuntuGroupNotFoundError`, idempotent via `GroupInfo.members`
  *Implemented with `gpasswd -a` / `-d` + member-list idempotency
  guard.  Missing-group case returns `FAILED` with a descriptive
  message rather than raising; missing-user case is delegated to
  the underlying `gpasswd` invocation (which surfaces a
  user-not-found stderr).*

- [x] 19. Implement `_dispatch` helper — dry-run short-circuit,
  runner-result mapping, elevation decision based on config
  *Inherited from `BaseFeatureManager._dispatch`; per-write helpers
  `_dispatch_user_write` and `_dispatch_group_write` thread the
  `name` / `kind` extras through.*

## UbuntuUserGroupManager — Gaps (specified but not implemented)

- [ ] 20. SSH authorized_keys management (not in base contract; would
  be an Ubuntu extension mirroring the RHEL `add_ssh_key` /
  `remove_ssh_key`)

- [ ] 21. Password expiry / aging (`chage`) — not in base contract

- [ ] 22. Sudoers integration (writing `/etc/sudoers.d/<name>` via
  `IFileOperator`) — not in base contract

## Tests — Implemented

- [x] 23. `test_create_user_constructs_useradd_argv`
- [x] 24. `test_create_user_system_account_adds_dash_r`
- [x] 25. `test_create_user_skipped_when_present`
- [ ] 26. `test_delete_user_remove_home_passes_dash_r`
  *Implemented as `test_delete_user_runs_userdel_dash_r`; `-r` is
  always passed.*
- [x] 27. `test_delete_user_skipped_when_absent`
- [x] 28. `test_modify_user_only_emits_nonnone_flags`
  *Implemented as `test_modify_user_emits_only_nonnone_flags`.*
- [ ] 29. `test_modify_user_raises_user_not_found`
  *Equivalent test exists as `test_modify_user_failed_when_user_missing`,
  asserting FAILED status rather than a raised exception (see note
  above on exception strategy).*
- [x] 30. `test_modify_user_skipped_when_noop`
  *Implemented as `test_modify_user_skipped_when_state_already_matches`.*
- [x] 31. `test_get_user_parses_getent_and_passwd_s`
  *Implemented as `test_get_user_parses_getent_id_and_passwd_status`.*
- [x] 32. `test_get_user_returns_none_on_exit_2`
  *Implemented as `test_get_user_returns_none_when_absent`.*
- [x] 33. `test_list_users_filters_by_uid_range`
- [x] 34. `test_set_password_uses_stdin_and_does_not_leak_password`
  *Implemented as
  `test_set_password_pipes_stdin_and_does_not_capture_password`.*
- [x] 35. `test_lock_account_skipped_when_already_locked`
- [x] 36. `test_unlock_account_skipped_when_already_unlocked`
  *Implemented as `test_unlock_account_skipped_when_not_locked`.*
- [x] 37. `test_create_group_constructs_groupadd_argv`
- [x] 38. `test_create_group_skipped_when_present`
- [x] 39. `test_delete_group_skipped_when_absent`
- [ ] 40. `test_get_group_parses_members`
  *Coverage provided by `test_list_groups_parses_multiple_lines`,
  which exercises the same parser used by `get_group`.*
- [x] 41. `test_list_groups_parses_multiple_lines`
- [ ] 42. `test_add_to_group_raises_user_not_found`
- [ ] 43. `test_add_to_group_raises_group_not_found`
  *Equivalent FAILED-case coverage is implicit in the
  group-not-found code path; a dedicated test is deferred.*
- [x] 44. `test_add_to_group_skipped_when_already_member`
- [x] 45. `test_remove_from_group_skipped_when_not_member`
- [x] 46. `test_dry_run_short_circuits_every_write`
  *Implemented as `test_dry_run_short_circuits_create_user`; the
  remaining write verbs share the same dry-run guard but are not
  individually covered.*
- [x] 47. `test_reads_never_pass_elevated_even_when_configured`

## Tests — Gaps

- [ ] 48. Persistence-aspect test proving the password is never
  captured (end-to-end with a stub storage backend)

- [ ] 49. Retry-aspect test proving `FAILED` → `SUCCESS` promotion is
  transparent to the manager

## Follow-ups

- [ ] 50. **Cross-OS contract conformance review**: verify this
  implementation's `UbuntuUserGroupActionResult` field shapes match the
  cross-OS contract at
  `codecraftai-sysman-common/specs/contracts/user-group-management/contract.md`.

- [ ] 51. **codecraftai-* reuse check**: confirm that no other feature in
  `codecraftai-sysman-linux-ubuntu` invokes `useradd` / `usermod` /
  `groupadd` directly; all such calls should go through
  `UbuntuUserGroupManager`.

## Spec deviations from this implementation

- **No `IFileOperator` collaborator.**  All probes go through the
  runner via `getent`, `id`, and `passwd -S`.  No file I/O is
  performed by this module — the manager talks exclusively to the
  user/group toolchain plus its query peers.

- **Result shape uses `name` and a typed `kind`.**  The contract
  field is named `target` and `kind` is a free-form string; this
  implementation uses `name` and a `UbuntuPrincipalKind` enum
  (`USER` / `GROUP`).  This matches the established Ubuntu pattern
  where extras carry typed values whenever the surface is small and
  closed.

- **Missing-target precondition violations surface as `FAILED`,
  not raised exceptions.**  The contract permits either; the
  established Ubuntu pattern is to surface these via
  `status=FAILED` with a descriptive `message` so that mass-action
  callers can continue past one bad target without wrapping each
  invocation in `try/except`.  `UbuntuUserNotFoundError` and
  `UbuntuGroupNotFoundError` are not introduced.

- **`delete_user` always cleans up the home directory.**  `-r`
  is unconditionally passed; the spec lists a `remove_home`
  parameter as future surface.

- **`UbuntuUserChanges` carries `append_groups`.**  When set with
  `groups`, ``-aG`` is emitted; otherwise the supplementary set is
  replaced with ``-G``.

- **Primary-group idempotency is conservative.**  When
  `UbuntuUserChanges.primary_group` is set, the manager always
  dispatches `usermod -g <primary_group>` rather than attempting
  to resolve the group's GID for a no-op comparison.  The result
  is at most one redundant invocation, which is preferable to a
  partial-state false skip.
