# Implementation Plan — Ubuntu Scheduled Tasks

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.linux.ubuntu.scheduledtasks`
  module

- [x] 2. Define `UbuntuScheduledTaskActionStatus` and
  `UbuntuTaskOwner` enums

- [x] 3. Define `UbuntuScheduledTasksManagerConfig` dataclass
  (dry_run, elevated, system_unit_dir, user_unit_dir_template,
  target_user).  Spec deviation: the original spec named the fields
  `system_store_dir` / `user_store_dir`; this implementation uses
  `system_unit_dir` / `user_unit_dir_template` because the storage is
  unit-file directories and the user variant needs a `{user}`
  substitution token.  A `target_user` field was added to carry the
  user identity for user-scope writes — see "Pending follow-ups"
  for a deferred running-user discovery refactor.

- [x] 4. Define `UbuntuTaskSpec` and `UbuntuTaskInfo` dataclasses

- [x] 5. Define `UbuntuScheduledTaskActionResult` dataclass and
  `UbuntuScheduledTaskQueryError` exception

## Rendering and Parsing Helpers

- [x] 6. Implement `_render_service_unit(spec)` returning the
  `<name>.service` file content

- [x] 7. Implement `_render_timer_unit(spec)` returning the
  `<name>.timer` file content

- [x] 8. Implement `_unit_dir(owner)` returning the absolute path
  under `/etc/systemd/system/` or
  `/home/<target_user>/.config/systemd/user/`.  Spec deviation: the
  user is read from `config.target_user` rather than passed per-call.

- [x] 9. Implement `_systemctl_argv(owner, args)` returning the
  full systemctl argv prefix (`systemctl` or `systemctl --user`).
  Spec deviation: no `runuser -u <user> --` wrapper — the simplified
  pilot policy elevates user-scope writes through the runner instead
  (matching the locked-in `environmentconfiguration.py` pattern).

- [x] 10. Implement `_parse_show_output(stdout)` into a
  `dict[str, str]` of `Key=Value` pairs

- [x] 11. Implement `_parse_list_timers(stdout)` extracting
  `(name, next_run, last_run, active)` tuples.  Implementation
  detects the `.timer` token and treats `n/a` columns as `None`.

- [ ] 12. Implement `_parse_list_units_timer(stdout)` extracting
  never-run timer names — folded into `_parse_list_timers` because
  `list-timers --all` already includes timers that have never fired.

## UbuntuScheduledTasksManager — Implemented

- [x] 13. Implement `__init__` accepting runner + optional config.
  Spec deviation: no `IFileOperator` collaborator — file I/O goes
  through the runner via `cat` / `tee` / `rm` / `mkdir -p`,
  matching the locked-in pattern of the other Ubuntu features.

- [x] 14. Implement `create_task(spec)` — name validation,
  byte-identical idempotency guard for both unit files plus
  enabled-state cross-check, write + daemon-reload + optional
  enable-now; `FAILED` on invalid name; per-step `FAILED` results for
  service/timer write, daemon-reload, and enable-now failures

- [x] 15. Implement `delete_task(name, owner)` — disable --now,
  delete `.timer` + `.service`, daemon-reload; `SKIPPED` when neither
  file exists

- [x] 16. Implement `enable_task(name, owner)` — dispatch
  `systemctl enable --now <name>.timer`; `SKIPPED` when already
  enabled

- [x] 17. Implement `disable_task(name, owner)` — dispatch
  `systemctl disable --now <name>.timer`; `SKIPPED` when not enabled

- [x] 18. Implement `run_task_now(name, owner)` — dispatch
  `systemctl start <name>.service`; no idempotency guard per contract

- [x] 19. Implement `get_task(name, owner)` — dispatch `systemctl
  show <name>.timer` + `<name>.service`, parse, return
  :class:`UbuntuTaskInfo` or `None`; raises on unexpected stderr

- [x] 20. Implement `list_tasks(owner)` — `systemctl list-timers
  --all --no-legend` parser; raises on non-zero exit with stderr

- [x] 21. Implement `validate_schedule(schedule)` — pure-Python
  OnCalendar / cron / shorthand validator; no runner / file-operator
  calls

- [x] 22. Implement dispatch helper — `_dispatch_write` for non-file
  writes, `_build_result` for synthetic results.  Spec deviation:
  USER-other-user elevation is currently uniform with USER-self
  elevation pending a reliable running-user probe (see follow-ups).

## UbuntuScheduledTasksManager — Gaps (specified but not implemented)

- [ ] 23. Cron drop-in backend (`/etc/cron.d/<name>`) — reserved
  extension; not implemented in base contract surface

- [ ] 24. `loginctl enable-linger <user>` chain for user-scope
  timers that must fire while the user is not logged in — out of
  base contract

- [ ] 25. Event-driven triggers (`OnBootSec`, `OnUnitActiveSec`) —
  out of base contract

- [ ] 26. Task-chaining / dependencies between tasks — out of base
  contract

## Tests — Implemented

- [x] 27. `test_create_task_writes_service_and_timer_units`
  (implemented as `test_create_task_writes_unit_files_and_enables`)
- [x] 28. `test_create_task_dispatches_daemon_reload_and_enable`
  (covered by `test_create_task_writes_unit_files_and_enables`)
- [x] 29. `test_create_task_skipped_when_files_and_state_match`
- [ ] 30. `test_create_task_failed_when_schedule_invalid`
  — current implementation guards on name; an explicit schedule guard
  should be wired into `create_task` (see follow-ups)
- [x] 31. `test_create_task_failed_enable_surface_reports_step`
  (implemented as `test_create_task_failed_when_enable_step_fails`)
- [x] 32. `test_create_task_user_scope_writes_under_user_home`
  (implemented as
  `test_create_task_user_scope_writes_under_user_home_and_passes_user_flag`)
- [x] 33. `test_delete_task_runs_disable_delete_reload_sequence`
  (implemented as `test_delete_task_disables_removes_and_reloads`)
- [x] 34. `test_delete_task_skipped_when_neither_file_exists`
  (implemented as `test_delete_task_skipped_when_files_absent`)
- [x] 35. `test_enable_task_skipped_when_unit_file_state_enabled`
  (implemented as `test_enable_task_skipped_when_already_enabled`)
- [ ] 36. `test_enable_task_failed_when_timer_missing`
  — not implemented; `enable_task` currently surfaces the runner's
  stderr without a custom missing-timer FAILED
- [x] 37. `test_disable_task_skipped_when_unit_file_state_disabled`
  (implemented as `test_disable_task_skipped_when_already_disabled`)
- [ ] 38. `test_disable_task_failed_when_timer_missing`
  — not implemented; same rationale as #36
- [x] 39. `test_run_task_now_dispatches_systemctl_start_service`
- [ ] 40. `test_run_task_now_failed_when_service_missing`
  — not implemented; runner stderr passes through
- [x] 41. `test_get_task_populates_schedule_command_enabled_active`
  (implemented as `test_get_task_populates_fields_from_systemctl_show`)
- [x] 42. `test_get_task_returns_none_on_load_state_not_found`
- [ ] 43. `test_get_task_raises_on_unexpected_show_failure`
  — partial coverage via `test_list_tasks_raises_on_failure_with_stderr`;
  the equivalent `get_task` raise path is not yet exercised
- [x] 44. `test_list_tasks_merges_list_timers_and_list_units`
  (implemented as `test_list_tasks_parses_list_timers_rows`; merge
  with `list-units --type=timer` was folded into `list-timers --all`)
- [x] 45. `test_list_tasks_raises_on_systemctl_failure`
  (implemented as `test_list_tasks_raises_on_failure_with_stderr`)
- [x] 46. `test_validate_schedule_accepts_shorthands`
- [x] 47. `test_validate_schedule_accepts_oncalendar_shape`
  (covered by `test_validate_schedule_accepts_oncalendar_and_cron`)
- [x] 48. `test_validate_schedule_accepts_cron_fallback`
  (covered by `test_validate_schedule_accepts_oncalendar_and_cron`)
- [x] 49. `test_validate_schedule_rejects_bad_input`
  (implemented as `test_validate_schedule_rejects_garbage`)
- [x] 50. `test_validate_schedule_never_invokes_runner`
  (covered by the assertion at the end of every validate test)

Additional tests added beyond the original spec:

- [x] `test_create_task_dry_run_short_circuits`
- [x] `test_create_task_failed_on_invalid_name`
- [x] `test_enable_task_dispatches_systemctl_enable_now`
- [x] `test_dry_run_short_circuits_every_write`
- [x] `test_reads_never_pass_elevated_true`
- [x] `test_writes_pass_elevated_true_when_configured`
- [x] `test_failed_write_surfaces_stderr_in_result`
- [x] `test_user_owner_without_target_user_raises_on_write`

## Tests — Gaps

- [ ] 51. Test dry-run short-circuits every mutating method — covered
  for the five primary verbs by `test_dry_run_short_circuits_every_write`,
  but not for the rendered-but-not-dispatched dry-run command captured
  on the result for every code path

- [ ] 52. Test system-owner writes pass `elevated=True` when config
  enables it — covered by `test_writes_pass_elevated_true_when_configured`

- [ ] 53. Test user-owner writes for the running user never pass
  `elevated=True` — not yet covered (no current-user discovery)

- [ ] 54. Test user-owner writes for another user wrap with
  `runuser -u <user> --` and pass `elevated=True` — not yet covered;
  the simplified pilot policy elevates without `runuser`

- [ ] 55. Test reads never pass `elevated=True` regardless of config
  — covered by `test_reads_never_pass_elevated_true`

- [ ] 56. Property-based tests (hypothesis) covering Properties
  1 through 27

## Follow-ups

- [ ] 57. **Cross-OS contract conformance review**: verify the
  `UbuntuTaskSpec` / `UbuntuTaskInfo` /
  `UbuntuScheduledTaskActionResult` field shapes match the cross-OS
  contract at
  `codecraftai-sysman-common/specs/contracts/scheduled-tasks/contract.md`
  and that the owner-scope mapping matches the contract's Ubuntu row.

- [ ] 58. **Cron-backend extension plan**: scope whether the
  `cron_backend=True` fallback belongs in this module or in a
  separate `UbuntuCronTaskManager` companion.

- [ ] 59. **codecraftai-* reuse check**: confirm no other feature in
  `codecraftai-sysman-linux-ubuntu` writes unit files or invokes
  `systemctl enable --now` directly; all such work should go
  through `UbuntuScheduledTasksManager`.

- [ ] 60. **Schedule guard inside `create_task`**: invoke
  `validate_schedule` before any runner / file write so an invalid
  schedule fails fast with no side effects, matching the contract's
  Method-level invariants for writes.

- [ ] 61. **Running-user probe**: introduce a reliable
  current-user discovery mechanism and switch user-scope elevation
  policy to "elevate only when target_user differs from running user",
  per the contract's elevation table.
