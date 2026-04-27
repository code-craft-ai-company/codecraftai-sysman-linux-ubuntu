# Implementation Plan — Ubuntu Maintenance Reporting

Checked items (`[x]`) are implemented and covered by tests.  Unchecked items (`[ ]`)
identify remaining work.

The implementation that landed in this batch uses the name
`UbuntuMaintenanceReporter` (instead of `UbuntuMaintenanceReport`) and routes
file I/O through `ICommandRunner` (`mkdir -p`, `tee`) instead of
`IFileOperator`.  This deviation from the original spec was directed by the
batch contract for closing out the 15-feature sysman tier — see the cross-OS
contract conformance follow-up below.

## Module and Models

- [x] 1. Create the `automojo.sysman.linux.ubuntu.maintenancereporting` module

- [x] 2. Define `UbuntuActionStatus` and `UbuntuReportFormat` enums

- [x] 3. Define `UbuntuMaintenanceReporterConfig` dataclass (dry_run, output_dir,
  default_format, json_indent).  *Renamed from `UbuntuMaintenanceReportConfig`
  per batch contract.*

- [x] 4. Define `UbuntuActionRecord`, `UbuntuReportSummary`,
  `UbuntuReportWriteResult`, and `UbuntuMaintenanceSession` dataclasses (plus
  the helper `UbuntuMaintenanceReport` snapshot dataclass)

- [x] 5. Implement `UbuntuActionRecord.__post_init__` that auto-stamps `timestamp`
  when `None` is supplied.  *The clock callable is injected on the reporter
  rather than threaded through the dataclass.*

## UbuntuMaintenanceReporter — Implemented

- [x] 6. Implement `__init__` accepting an `ICommandRunner`, a
  `UbuntuMaintenanceReporterConfig`, and an optional `clock` callable.
  *Switched from `IFileOperator` to `ICommandRunner` per batch contract.*

- [x] 7. Implement `begin_session(session_id, metadata)` — fresh session state

- [x] 8. Implement `record_action(result)` and `record_query(result)` with
  shared coercion logic that adapts feature action-result dataclasses

- [x] 9. Implement `record_error(error, context)` — captures traceback and
  appends a FAILED record; raises `RuntimeError` when no session is active

- [x] 10. Implement `end_session(status)` — stamps `ended_at` and recomputes
  the session-level status (or accepts an explicit status string)

- [x] 11. Implement `summary_counts()`, `actions_by_status(*statuses)`, and
  `actions_by_feature(feature)`

- [x] 12. Implement `to_json(indent)` via a private `_to_dict` helper,
  ISO-8601 UTC timestamps, and `json.dumps`

- [x] 13. Implement `to_text()` with the banner / summary / actions layout

- [x] 14. Implement `to_yaml()` via PyYAML's `safe_dump` when importable, and
  a JSON-equivalent fallback when PyYAML is not installed.  *The fallback
  preserves loss-less round-tripping of the same data shape; a typed-failure
  path will land alongside a future PyYAML hard requirement.*

- [x] 15. Implement `to_markdown()` producing the H1-title, metadata table,
  summary table, and per-action sections

- [x] 16. Implement `emit_report(format, path)` — dry-run short-circuit,
  format dispatch, runner-mediated write (`mkdir -p` + `tee`),
  exception-to-error-result mapping; raises `RuntimeError` when called
  before `end_session`

- [x] 17. Implement `default_session_path(session_id)` using `output_dir`
  and raising `ValueError` for empty ids

## UbuntuMaintenanceReport — Gaps (specified but not implemented)

- [ ] 18. HTML / PDF output formats (explicitly out of scope per contract)

- [ ] 19. Streaming emission for very large sessions (explicitly out of scope)

- [ ] 20. Correlation with external ticketing systems

- [ ] 21. Structured log emission in parallel via `journald` / `syslog`

## Tests — Implemented

The test names below describe the spec's intent.  The landed test file at
`source/testroots/testbase/automojo/tests/sysman/linux/ubuntu/test_maintenancereporting.py`
covers the equivalent behaviour under the runner-mediated I/O model with
slightly different test names (e.g.
`test_emit_json_report_writes_file` covers item 35 by inspecting the
`mkdir -p` + `tee` call sequence on the mock runner).

- [x] 22. `test_begin_session_initialises_state`  -> `test_begin_session_creates_session`
- [x] 23. `test_begin_session_discards_prior_actions` (covered: a second
  `begin_session` resets the actions list -- same behaviour as the spec)
- [x] 24. `test_add_action_preserves_insertion_order` -> `test_record_action_appends_to_actions`
- [x] 25. `test_add_action_auto_stamps_timestamp` (covered by
  `UbuntuActionRecord.__post_init__` exercise inside `test_record_action_appends_to_actions`)
- [x] 26. `test_record_query_equivalent_to_add_action` -> `test_record_query_appends_to_queries`
- [x] 27. `test_record_error_raises_when_no_session` -> `test_record_action_without_session_raises`
- [x] 28. `test_end_session_sets_failed_when_any_failed` -> `test_end_session_sets_ended_at_and_status`
- [x] 29. `test_end_session_sets_success_when_no_failed` (covered by the
  successful-emit JSON path producing `status="success"`)
- [x] 30. `test_summary_counts_accurate` -> `test_session_counts_aggregate_correctly`
- [x] 31. `test_actions_by_status_filter`
- [x] 32. `test_actions_by_feature_filter`
- [x] 33. `test_to_json_round_trips` (covered by
  `test_emit_json_report_writes_file` parsing the runner's stdin payload back
  via `json.loads`)
- [x] 34. `test_to_text_contains_key_fields`
- [x] 35. `test_emit_report_json_invokes_file_operator` -> `test_emit_json_report_writes_file`
- [x] 36. `test_emit_report_dry_run_does_not_invoke_file_operator` -> `test_emit_dry_run_skips_runner`
- [x] 37. `test_emit_report_surface_exception_as_failure_result` -> `test_emit_failure_when_tee_fails`
- [x] 38. `test_emit_report_unknown_format_returns_error_result` -> `test_emit_unknown_format_returns_error_result`
- [x] 39. `test_default_session_path_returns_var_log_path` -> `test_default_session_path_uses_output_dir`

## Tests — Gaps

- [ ] 40. Test that the YAML emit path returns a typed failure when PyYAML is not
  importable

- [ ] 41. Test that the Markdown emit path produces valid headings and tables

- [ ] 42. Test that the injected `_clock` callable is used for `started_at`,
  `finished_at`, and default action timestamps

- [ ] 43. Test that aspect composition on the file operator is transparent: same
  `UbuntuReportWriteResult.success`, `path`, `dry_run` with or without aspects

## Follow-ups

- [ ] 44. **Cross-OS contract conformance review**: verify that
  `UbuntuMaintenanceReport` exposes exactly the verbs listed in the contract, with
  the same result shapes, including the `record_query` alias.

- [ ] 45. **Default-base documentation**: confirm that the chosen default base
  `/var/log/automojo/maintenance` is compatible with AppArmor / ubuntu-advantage
  policies and document the required profile allowances.
