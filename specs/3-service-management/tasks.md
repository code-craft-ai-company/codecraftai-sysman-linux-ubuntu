# Implementation Plan — Ubuntu Service Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked items (`[ ]`)
identify remaining work.  All items below are unchecked because this package has not
yet been implemented; the specs precede the code.

## Module and Models

- [x] 1. Create the `automojo.sysman.linux.ubuntu.servicemanagement` module

- [x] 2. Define `UbuntuServiceActionStatus` and `UbuntuServiceScope` enums

- [x] 3. Define `UbuntuServiceManagerConfig` dataclass (dry_run, elevated)

- [x] 4. Define `UbuntuServiceActionResult`, `UbuntuServiceStatus`,
  `UbuntuServiceInfo` dataclasses and the `UbuntuServiceQueryError` exception

- [x] 5. Implement the scope→argv-prefix helper that returns `["systemctl"]` or
  `["systemctl", "--user"]`

## UbuntuServiceManager — Implemented

- [x] 6. Implement `__init__` accepting a runner and a config

- [x] 7. Implement `start_service` / `stop_service` with idempotency guards based
  on `active_state`

- [x] 8. Implement `restart_service` as unconditional (never skips)

- [x] 9. Implement `reload_service` with the precondition `active_state == "active"`
  returning `FAILED` otherwise

- [x] 10. Implement `enable_service` / `disable_service` with idempotency guards
  based on `enabled_state`

- [x] 11. Implement `mask_service` / `unmask_service` with the appropriate
  `masked` / not-`masked` guards

- [x] 12. Implement `get_service_status` wrapping `is-active` + `show
  --property=LoadState,UnitFileState,MainPID,Description`, normalising the output

- [x] 13. Implement `list_services` parsing `list-units --type=service --all
  --no-legend --plain`

- [x] 14. Implement `reload_daemon` (never skips)

- [x] 15. Implement the `_dispatch` helper that applies dry-run short-circuit,
  scope-to-argv mapping, elevation rules, and runner-result mapping

## UbuntuServiceManager — Gaps (specified but not implemented)

- [ ] 16. `systemctl --now` one-shot enable-and-start pattern — the contract treats
  these as separate verbs; document the absence

- [ ] 17. `loginctl enable-linger <user>` for persistent per-user services —
  out of scope for the base contract

- [ ] 18. `systemctl edit` drop-in authoring (covered separately by
  `configuration-management` when implemented)

- [ ] 19. Service logs via `journalctl -u <name>` — belongs to `log-management`

## Tests — Implemented

- [x] 20. `test_start_service_system_runs_systemctl_start`
- [x] 21. `test_start_service_user_runs_systemctl_user_start`
- [x] 22. `test_start_service_skipped_when_active`
- [x] 23. `test_stop_service_skipped_when_inactive`
- [x] 24. `test_restart_service_never_skips`
- [x] 25. `test_reload_service_failed_when_not_active`
- [ ] 26. `test_reload_service_success_when_active`
- [x] 27. `test_enable_service_skipped_when_enabled`
- [ ] 28. `test_disable_service_skipped_when_masked`
- [ ] 29. `test_mask_service_skipped_when_masked`
- [ ] 30. `test_unmask_service_skipped_when_not_masked`
- [x] 31. `test_get_service_status_parses_is_active_and_show`
- [ ] 32. `test_get_service_status_handles_not_found`
- [x] 33. `test_list_services_parses_list_units_output`
- [x] 34. `test_reload_daemon_dispatches_command`
- [x] 35. `test_system_writes_pass_elevated_true_when_configured`
- [x] 36. `test_user_writes_never_pass_elevated_true`
- [x] 37. `test_reads_never_pass_elevated_true`
- [x] 38. `test_dry_run_skips_runner_for_every_write`
- [x] 39. `test_invalid_scope_raises_value_error`

## Tests — Gaps

- [x] 40. Test that `list_services` raises `UbuntuServiceQueryError` on non-zero
  exit from `list-units`

- [ ] 41. Test that `get_service_status` raises `UbuntuServiceQueryError` when
  `show` fails with an unrecognised stderr

- [ ] 42. Test that aspect composition on the runner is transparent: same
  `UbuntuServiceActionResult.status` with and without aspects

- [x] 43. Test that the idempotency-probe commands themselves do not pass
  `elevated=True`, even when the subsequent write would

## Follow-ups

- [ ] 44. **Cross-OS contract conformance review**: verify that
  `UbuntuServiceActionResult`, `UbuntuServiceStatus`, and `UbuntuServiceInfo` field
  shapes match the cross-OS contract at
  `automojo-sysman-common/specs/contracts/service-management/contract.md`
  exactly, and that the Ubuntu scope-mapping (`--user` for USER, no flag for
  SYSTEM) is documented in the contract's Ubuntu row.

- [ ] 45. **user-scope review**: confirm whether per-user services started by a
  non-logged-in system account require `loginctl enable-linger` and document the
  caller's responsibility to enable linger before invoking `start_service` with
  `scope=USER` in that context.
