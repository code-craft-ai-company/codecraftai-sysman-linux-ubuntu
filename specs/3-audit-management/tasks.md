# Implementation Plan — Ubuntu Audit Management

Checked items (`[x]`) are implemented and covered by tests.
Unchecked items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.linux.ubuntu.auditmanagement`
  module.

- [x] 2. Define `UbuntuAuditActionStatus` enum (re-exported alias of
  `automojo.sysman.linux.ubuntu.featurebase.ActionStatus`).

- [x] 3. Define `UbuntuAuditManagerConfig` dataclass (`dry_run`,
  `elevated`, `rules_d`, `default_basename`, `service_name`).

- [x] 4. Define `UbuntuAuditActionResult` dataclass (subclass of
  `FeatureActionResult`; adds `rule_id`, `rule_key`, `message`).

- [x] 5. Define `UbuntuAuditRuleSpec` dataclass (`key`, `watch_path`,
  `permissions`, `syscall_filter`, `architecture`, `raw_rule`,
  `persistent`).

- [x] 6. Define `UbuntuAuditRuleInfo` dataclass (`rule_id`, `key`,
  `expression`, `watch_path`, `permissions`, `subsystem`,
  `source`).

- [x] 7. Define `UbuntuAuditStatus` dataclass (`enabled`, `running`,
  `pid`, `failure_mode`, `rate_limit`, `backlog_limit`, `backlog`,
  `lost_events`, `locked`, `raw`).

- [x] 8. Define `UbuntuAuditEventQuery` dataclass and
  `UbuntuAuditEvent` dataclass.

- [x] 9. Define `UbuntuAuditQueryError` and
  `UbuntuAuditImmutableError` exceptions.

## UbuntuAuditManager — Implemented

- [x] 10. Implement `__init__` (inherited from
  `BaseFeatureManager`) accepting `runner` and optional `config`.
  No `IFileOperator` collaborator: file I/O is performed via `cat`
  / `tee` against the injected `ICommandRunner`.

- [x] 11. Implement `_render_auditctl_args` rendering the canonical
  `-w / -p / -a / -F arch=… / -k` argv body, with verbatim
  `raw_rule` fall-back.

- [x] 12. Implement `add_rule(spec)` with idempotency guard, dry-run
  short-circuit, runtime dispatch, and persistence (drop-in `tee` +
  `augenrules --load`).

- [x] 13. Implement `remove_rule(rule_id)` with idempotency guard,
  dry-run short-circuit, runtime dispatch, and drop-in rewrite.

- [x] 14. Implement `list_rules` composing `auditctl -l` plus the
  manager-owned drop-in.

- [x] 15. Implement `enable_auditing` issuing `auditctl -e 1` and
  `systemctl enable --now <service_name>` with idempotency and
  immutable-mode guards.

- [x] 16. Implement `disable_auditing` issuing `auditctl -e 0` and
  `systemctl disable --now <service_name>` with idempotency and
  immutable-mode guards.

- [x] 17. Implement `get_audit_status` parsing `auditctl -s` into
  the structured dataclass.

- [x] 18. Implement `search_events(query)` building the `ausearch`
  argv from the structured query and parsing the dashed record
  stream.

- [x] 19. Implement `_parse_auditctl_list`, `_parse_rules_file`,
  `_parse_auditctl_status`, `_parse_ausearch_output`,
  `_parse_ausearch_record`.

- [x] 20. Implement `_safe_runtime_rules`, `_safe_status` to keep
  write paths resilient to read-time failures.

- [x] 21. Implement `_append_to_dropin`, `_remove_from_dropin`,
  `_read_file`, `_dropin_path` for drop-in I/O via `cat`/`tee`.

## UbuntuAuditManager — Gaps (specified but not implemented)

- [ ] 22. Standalone `reload_rules` verb (currently folded into
  `add_rule` / `remove_rule`).

- [ ] 23. Multiple-drop-in awareness: today the manager owns one
  drop-in.  A future enhancement could index every file under
  `<rules_d>` so that hand-managed rules from other tooling are
  surfaced by `list_rules`.

- [ ] 24. Structured `architecture` enumeration (`b64` / `b32`) —
  today the field is opaque.

## Tests — Implemented

- [x] 25. `test_add_rule_invokes_auditctl`
- [x] 26. `test_add_rule_skipped_when_present`
- [x] 27. `test_add_rule_dry_run_short_circuits`
- [x] 28. `test_persistent_rule_writes_to_rules_d`
- [x] 29. `test_remove_rule_invokes_auditctl`
- [x] 30. `test_remove_rule_skipped_when_absent`
- [x] 31. `test_list_rules_parses_output`
- [x] 32. `test_enable_auditing_invokes_auditctl`
- [x] 33. `test_enable_auditing_skipped_when_enabled`
- [x] 34. `test_disable_auditing_invokes_auditctl`
- [x] 35. `test_get_audit_status_parses_auditctl_output`
- [x] 36. `test_get_audit_status_detects_locked_mode`
- [x] 37. `test_search_events_invokes_ausearch_with_query`
- [x] 38. `test_search_events_parses_record_stream`
- [x] 39. `test_writes_elevate`
- [x] 40. `test_reads_never_elevate`
- [x] 41. `test_failed_command_surfaces_stderr`
- [x] 42. `test_render_auditctl_args_structured`
- [x] 43. `test_render_auditctl_args_raw_appends_key`
- [x] 44. `test_parse_auditctl_status_handles_partial_output`
- [x] 45. `test_parse_auditctl_list_returns_rule_info`
- [x] 46. `test_parse_ausearch_output_respects_limit`

## Tests — Gaps

- [ ] 47. Coverage for partial-failure scenarios where the runtime
  add succeeds but the persistence tee fails (current
  implementation surfaces the runtime status and lets persistence
  drop on the floor).

- [ ] 48. Coverage for `augenrules --load` failure surfacing
  through the result.

## Spec Deviations / Revisions Needed

- [ ] 49. **`IFileOperator` collaborator**: the contract names
  `IFileOperator` as the route for rule-file I/O.  This
  implementation routes every read / write through the
  `ICommandRunner` instead.  The contract should be revised to
  match.

- [ ] 50. **`reload_rules` verb**: surface as a standalone public
  verb instead of folding it into `add_rule` / `remove_rule`.

- [ ] 51. **`AuditImmutableError` raising**: the contract allows
  either raising the exception or returning `FAILED` with a
  descriptive detail.  This implementation chose the latter.

## Follow-ups

- [ ] 52. **Cross-OS contract conformance review**: verify that
  the shipping dataclasses match the abstract names in
  `automojo-sysman-common/specs/contracts/audit-management/contract.md`.

- [ ] 53. **Coordinator wiring**: confirm that the maintenance
  coordinator's audit-management facade routes through
  `UbuntuAuditManager` rather than constructing `auditctl`
  invocations directly.
