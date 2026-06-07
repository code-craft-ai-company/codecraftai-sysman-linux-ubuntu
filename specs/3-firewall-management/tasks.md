# Implementation Plan — Ubuntu Firewall Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `ccai.sysman.linux.ubuntu.firewallmanagement`
  module

- [x] 2. Define `UbuntuFirewallActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN)

- [x] 3. Define `UbuntuFirewallManagerConfig` dataclass (dry_run,
  elevated)

- [x] 4. Define `UbuntuFirewallActionResult` dataclass

- [x] 5. Define `UbuntuZoneInfo` + `UbuntuFirewallState` query
  dataclasses and `UbuntuFirewallQueryError` exception

## UbuntuFirewallManager — Implemented

- [x] 6. Implement `__init__` accepting runner + optional config
  (inherited from `BaseFeatureManager`)

- [x] 7. Implement `add_service(service, zone)` / `remove_service`;
  both validate `zone == "default"`, idempotent via
  `ufw status verbose` parsing, dispatch `ufw allow <service>` /
  `ufw delete allow <service>`

- [x] 8. Implement `add_port(port, protocol, zone)` /
  `remove_port(port, protocol, zone)`; both validate zone, idempotent,
  dispatch `ufw allow <port>/<proto>` / `ufw delete allow <port>/<proto>`

- [x] 9. Implement `add_rich_rule(rule, zone)` /
  `remove_rich_rule(rule, zone)`; both validate zone AND that the rule
  begins with a ufw action verb, idempotent via normalised comparison

- [x] 10. Implement `set_default_zone(zone)` as a validated no-op —
  `SKIPPED` when `zone == "default"`, `FAILED` otherwise

- [x] 11. Implement `reload()` — `ufw reload`

- [x] 12. Implement `query_state()` — returns `UbuntuFirewallState`,
  parses `ufw status` output (`Status: active` → running=True)

- [x] 13. Implement `query_zone(zone)` /
  `query_all_zones()` — parse `ufw status verbose` into `UbuntuZoneInfo`

- [x] 14. Implement `_dispatch_write` helper — runner-result mapping;
  zone validation handled by `_reject_non_default_zone`; dry-run
  short-circuit handled at each verb's entry point

## UbuntuFirewallManager — Gaps (specified but not implemented)

- [ ] 15. Runtime-only rules — none currently; ufw has no runtime vs
  permanent split.  No additional code path needed.

- [ ] 16. `ufw route` commands for forward-port equivalents

- [ ] 17. Application profile authoring (`/etc/ufw/applications.d/<app>`)
  — currently the manager assumes profiles pre-exist; explicit
  authoring would require an `ICommandRunner`-driven `tee` writer.

- [ ] 18. IPv6 selective enable/disable via `/etc/default/ufw`

## Tests — Implemented

- [x] 19. `test_query_state_reports_running_and_default`
- [x] 20. `test_query_state_reports_not_running_when_inactive`
- [x] 21. `test_query_state_handles_runner_failure`
- [x] 22. `test_query_zone_parses_status_verbose`
- [x] 23. `test_query_zone_rejects_non_default_zone`
- [x] 24. `test_query_all_zones_returns_single_default_entry`
- [x] 25. `test_add_service_runs_ufw_allow`
- [x] 26. `test_add_service_skipped_when_present`
- [x] 27. `test_remove_service_runs_ufw_delete_allow`
- [x] 28. `test_remove_service_skipped_when_absent`
- [x] 29. `test_add_port_runs_ufw_allow_port_proto`
- [x] 30. `test_add_port_skipped_when_present`
- [x] 31. `test_remove_port_runs_ufw_delete_allow_port_proto`
- [x] 32. `test_remove_port_skipped_when_absent`
- [x] 33. `test_add_rich_rule_runs_ufw_with_rule_tokens`
- [x] 34. `test_add_rich_rule_rejects_invalid_verb`
- [x] 35. `test_remove_rich_rule_runs_ufw_delete_with_rule_tokens`
- [x] 36. `test_set_default_zone_skipped_when_default`
- [x] 37. `test_set_default_zone_failed_when_other`
- [x] 38. `test_reload_dispatches_command`
- [x] 39. `test_invalid_zone_on_write_returns_failed`
- [x] 40. `test_dry_run_short_circuits_for_every_write_verb`
- [x] 41. `test_writes_pass_elevated_when_configured`
- [x] 42. `test_reads_never_elevate`
- [x] 43. `test_failed_write_returns_failed_with_stderr`

## Tests — Gaps

- [ ] 44. Property-based hypothesis coverage for the design's 28
  correctness properties.

## Spec Deviations / Revisions Needed

- [ ] 45. **`detail_name` field**: the design's `UbuntuFirewallActionResult`
  shape lists `zone` and `detail`.  This implementation adds a
  `detail_name` extra so `detail` (inherited from
  `FeatureActionResult`) and a feature-specific identifier can both
  ride on the result.  Both fields are populated identically when a
  per-verb identifier exists.

## Follow-ups

- [ ] 46. **Cross-OS contract conformance review**: verify that this
  implementation's `FirewallActionResult` / `ZoneInfo` /
  `FirewallState` field shapes match the cross-OS contract at
  `codecraftai-sysman-common/specs/contracts/firewall-management/contract.md`
  and that the zone-validation behaviour matches the contract's
  Ubuntu row exactly.

- [ ] 47. **codecraftai-* reuse check**: confirm that
  `3-cockpit-management` does not invoke `ufw` directly; it should
  delegate to `UbuntuFirewallManager.add_service("cockpit", "default")`
  to avoid drift.
