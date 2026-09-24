# Tasks

## Source Module

- [x] Create `aclmanagement.py` under
      `source/packages/ccai/sysman/linux/ubuntu/`.
- [x] Module docstring covering the verb catalogue, file-I/O policy,
      elevation policy, and idempotency policy.
- [x] Module-level binary / flag constants (`_GETFACL`, `_SETFACL`,
      `_SETFACL_REMOVE_ALL`,
      `_SETFACL_MODIFY`, `_SETFACL_REMOVE`,
      `_SETFACL_SET_FILE`).
- [x] Alias `UbuntuAclActionStatus` to the package-shared
      `ActionStatus`.
- [x] `UbuntuAcePrincipalType` enum with `USER`, `GROUP`, `OTHER`,
      `MASK`.
- [x] `UbuntuAclManagerConfig` dataclass extending `FeatureConfig`
      with `getfacl_path` and `setfacl_path` fields.
- [x] `UbuntuAclActionResult` dataclass extending
      `FeatureActionResult` with `path`, `principal`, `message`
      fields.
- [x] `UbuntuAceInfo` dataclass for parsed read-shape ACEs.
- [x] `UbuntuAceSpec` dataclass for caller-supplied write-shape
      ACEs.
- [x] `UbuntuAclInfo` dataclass for the per-path snapshot.
- [x] `UbuntuAclQueryError` exception with `command`, `exit_code`,
      `stderr` fields and a multi-line message body.

## Manager Class

- [x] `UbuntuAclManager(BaseFeatureManager)` with class variables
      `result_class`, `status_class`, `feature_name`.
- [x] `get_acl(path)` -> `UbuntuAclInfo`.  Dispatches `getfacl
      <path>`; raises `UbuntuAclQueryError` on non-zero.
- [x] `list_aces(path)` -> `list[UbuntuAceInfo]`.  Re-uses
      `get_acl`; emits regular-then-default ACEs.
- [x] `set_acl(path, acl_spec)` -> `UbuntuAclActionResult`.
      Dispatches `setfacl -b <path>` then `setfacl -m
      <comma-joined> <path>`.  Skipped when the parsed current ACL
      matches.  Dry-run short-circuits before any probe.
- [x] `add_ace(path, principal, rights=None)` ->
      `UbuntuAclActionResult`.  Dispatches `setfacl -m <token>
      <path>`.  Skipped when the ACE is already present.  Dry-run
      short-circuits.
- [x] `remove_ace(path, principal)` -> `UbuntuAclActionResult`.
      Dispatches `setfacl -x <token> <path>`.  Skipped when no ACE
      matches the principal.  Dry-run short-circuits.
- [x] `clear_acl(path)` -> `UbuntuAclActionResult`.  Dispatches
      `setfacl -b <path>`.  Skipped when no extended ACEs exist.
      Dry-run short-circuits.
- [x] `copy_acl(src, dst)` -> `UbuntuAclActionResult`.  Reads the
      source via `getfacl` and pipes into `setfacl --set-file=-
      <dst>` via `stdin`.  Skipped when source / destination ACLs
      match.  Dry-run short-circuits.

## Module-Level Helpers

- [x] `_principal_type_letter(t)` mapping enum -> single character.
- [x] `_principal_type_from_word(word)` mapping `getfacl` long-form
      prefix -> enum.
- [x] `_materialise_ace_spec(principal, rights_override)` for
      caller convenience on `add_ace`.
- [x] `_render_single_setfacl_entry(spec)` rendering one ACE token.
- [x] `_render_setfacl_remove_token(spec)` rendering a `-x` token
      (no rights).
- [x] `_render_setfacl_entries(spec)` rendering a comma-joined list.
- [x] `_parse_getfacl_output(output, path)` -> `UbuntuAclInfo`.
- [x] `_parse_getfacl_header_line(line, info)` for `# owner:` /
      `# group:`.
- [x] `_parse_getfacl_ace_line(line)` for non-comment ACE rows;
      strips trailing `#effective:` annotations.
- [x] `_ace_already_present(current, spec)` for `add_ace`
      idempotency.
- [x] `_ace_principal_present(current, spec)` for `remove_ace`
      idempotency.
- [x] `_has_extended_aces(info)` for `clear_acl` idempotency.
- [x] `_acl_matches_spec(current, desired)` for `set_acl`
      idempotency.
- [x] `_ace_sort_key(item)` ordering helper for the comparison
      tuples.
- [x] `_acl_infos_equal(left, right)` for `copy_acl` idempotency.

## Test Module

- [x] Create `test_aclmanagement.py` under
      `source/testroots/testbase/ccai/tests/sysman/linux/ubuntu/`.
- [x] `_MockCommandRunner` recorder.
- [x] `_getfacl_full_payload()` fixture (named user / group / mask /
      default ACEs).
- [x] `_getfacl_minimal_payload()` fixture (base entries only).
- [x] `_seed_full_acl(runner, path)` and `_seed_minimal_acl(...)`
      seeding helpers.
- [x] `test_get_acl_parses_getfacl_output`.
- [x] `test_list_aces_returns_full_set`.
- [x] `test_set_acl_invokes_setfacl`.
- [x] `test_set_acl_skipped_when_match`.
- [x] `test_set_acl_dry_run_short_circuits`.
- [x] `test_add_ace_invokes_setfacl`.
- [x] `test_add_ace_skipped_when_present`.
- [x] `test_remove_ace_invokes_setfacl`.
- [x] `test_remove_ace_skipped_when_absent`.
- [x] `test_clear_acl_invokes_setfacl`.
- [x] `test_clear_acl_skipped_when_already_clear`.
- [x] `test_copy_acl_invokes_setfacl`.
- [x] `test_copy_acl_skipped_when_match`.
- [x] `test_writes_elevate`.
- [x] `test_reads_never_elevate`.
- [x] `test_failed_command_surfaces_stderr`.
- [x] `test_get_acl_raises_on_failure`.

## Specs

- [x] `requirements.md` covering the 12 numbered requirements.
- [x] `design.md` covering the cross-OS ACL-model divergence,
      verb-to-command mapping, parsing, idempotency, dry-run,
      elevation, error surface, and aspect composition.
- [x] `tasks.md` (this document).

## Out of Scope

- POSIX file capabilities (`getcap` / `setcap`) — covered by a
  follow-up extension feature.
- Extended filesystem attributes (`chattr` / `lsattr`) — covered
  by a follow-up extension feature.
- AppArmor / SELinux policy management — covered by separate MAC
  features.
- Recursive ACL operations — the cross-OS contract on Ubuntu
  exposes per-path verbs only; `setfacl -R` is reserved for a
  later extension.
