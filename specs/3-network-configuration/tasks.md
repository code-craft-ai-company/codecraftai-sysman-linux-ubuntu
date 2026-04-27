# Implementation Plan — Ubuntu Network Configuration

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.linux.ubuntu.networkconfiguration`
  module

- [x] 2. Define `UbuntuNetworkActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN)

- [x] 3. Define `UbuntuNetworkConfigurationManagerConfig` dataclass
  (dry_run, elevated, netplan_dir, netplan_basename).  Spec deviation:
  shipped `netplan_dir` + `netplan_basename` instead of a single
  `netplan_path` so the directory and filename can vary independently;
  `persist_dns` was dropped because the current implementation persists
  DNS via runtime `resolvectl` only (see follow-ups).

- [x] 4. Define `UbuntuNetworkActionResult` dataclass with `diff` +
  `message` fields, plus `interface`, `detail_name`, and `destination`
  carry-throughs

- [x] 5. Define `UbuntuInterfaceInfo`, `UbuntuRouteInfo` dataclasses

- [x] 6. Define `UbuntuNetworkQueryError` exception

## UbuntuNetworkConfigurationManager — Implemented

- [x] 7. Implement `__init__` accepting runner + optional config.  Spec
  deviation: no `IFileOperator` collaborator — file I/O goes through
  the runner via `cat` / `tee` to match the locked-in pattern of the
  other Ubuntu features.

- [x] 8. Implement `get_interfaces()` — `ip -j addr show` JSON parse

- [ ] 9. Implement `get_interface(name)` — single-interface read.
  Currently absent; callers can filter the `get_interfaces()` result.

- [x] 10. Implement `configure_interface(name, address, netmask,
  gateway)` — netplan YAML rewrite + `netplan apply`; idempotent via
  byte-for-byte comparison with the desired rendered body

- [x] 11. Implement `enable_interface(name)` / `disable_interface`
  — `ip link set <name> up|down`; idempotent via current operstate
  reported by `ip -j link show <iface>`

- [x] 12. Implement `add_route(dest, gateway)` /
  `remove_route(dest)` — `ip route add|del`; idempotent via `ip route
  show`.  Spec deviation: `add_route` does not currently take an
  `interface` parameter; the kernel selects the egress interface from
  the gateway.

- [ ] 13. Implement `get_routes()` — not implemented in this batch

- [x] 14. Implement `set_dns_servers(interface, servers)` —
  `resolvectl dns <iface> <servers>`; idempotent via
  `resolvectl dns <iface>` parse and order-insensitive comparison.
  Spec deviation: this implementation is keyed by interface, not the
  cross-OS contract's interface-less form, because per-link DNS is
  the natural shape under systemd-resolved.

- [x] 15. Implement `get_dns_servers(interface)` — `resolvectl dns
  <iface>` parser

- [ ] 16. Implement `set_search_domains(domains)` — not implemented in
  this batch

- [x] 17. Implement `get_hostname()` — `hostname` command, stripped

- [x] 18. Implement `set_hostname(name)` — single `hostnamectl
  set-hostname <name>` call; idempotent via current hostname

- [x] 19. Implement netplan rendering — string-builder renderer
  emitting deterministic body so byte-for-byte comparison is the
  idempotency check.  Spec deviation: no YAML library used and no
  unified-diff emission in dry-run; a future iteration may add a
  rendered diff to the result's `diff` field.

- [x] 20. Implement dispatch helper — `_dispatch_write` performs the
  runner-result mapping and elevation, while `_build_result` handles
  dry-run / skipped / synthetic results

## UbuntuNetworkConfigurationManager — Gaps (specified but not implemented)

- [ ] 21. VLAN / bridge / bond declaration — not in base contract

- [ ] 22. NetworkManager-profile authoring (`nmcli connection add`)
  — not in base contract; Ubuntu implementation prefers netplan

- [ ] 23. Wi-Fi SSID / WPA configuration — not in base contract

- [ ] 24. IPv6 privacy extensions — not in base contract

## Tests — Implemented

- [x] 25. `test_get_interfaces_parses_ip_addr_json`
- [ ] 26. `test_get_interfaces_joins_default_gateway_from_route_json`
  — not implemented in this batch
- [ ] 27. `test_get_interface_returns_none_on_missing_device`
  — `get_interface` itself is not yet implemented
- [x] 28. `test_configure_interface_rewrites_netplan_yaml`
  (implemented as `test_configure_interface_writes_netplan_and_applies`)
- [x] 29. `test_configure_interface_skipped_when_yaml_matches`
- [x] 30. `test_configure_interface_failed_with_message_when_apply_fails`
  (implemented as `test_configure_interface_failed_when_apply_fails`)
- [ ] 31. `test_configure_interface_dry_run_emits_diff` — dry-run is
  covered (see #45) but no `diff` field is currently populated
- [x] 32. `test_enable_interface_skipped_when_already_up`
- [x] 33. `test_disable_interface_skipped_when_already_down`
- [x] 34. `test_add_route_skipped_when_already_present`
- [x] 35. `test_remove_route_skipped_when_absent`
- [ ] 36. `test_get_routes_normalises_default_to_zero_prefix`
  — `get_routes` not implemented
- [x] 37. `test_set_dns_servers_writes_both_runtime_and_netplan`
  (implemented as `test_set_dns_servers_dispatches_resolvectl`; only
  the runtime surface is currently written — see follow-ups)
- [x] 38. `test_set_dns_servers_skipped_when_matching`
- [ ] 39. `test_set_dns_servers_partial_failure_reports_message`
  — not exercised because no two-surface write is currently performed
- [x] 40. `test_get_dns_servers_dedupes_global_and_link`
  (implemented as `test_get_dns_servers_parses_resolvectl_output`)
- [ ] 41. `test_set_search_domains_writes_both_surfaces`
  — `set_search_domains` not implemented
- [x] 42. `test_get_hostname_returns_stripped_stdout`
  (implemented as `test_get_hostname_strips_whitespace`)
- [x] 43. `test_set_hostname_single_command_dispatched`
  (implemented as `test_set_hostname_dispatches_hostnamectl`)
- [x] 44. `test_set_hostname_skipped_when_matching`
- [x] 45. `test_dry_run_short_circuits_every_write`
- [x] 46. `test_reads_never_pass_elevated`
  (implemented as `test_reads_never_pass_elevated_true`)
- [ ] 47. `test_netplan_rewrite_preserves_unrelated_keys`
  — not implemented; current renderer always emits a fresh body and
  does not merge with existing user-authored keys (see follow-ups)

Additional tests added beyond the original spec:

- [x] `test_get_interfaces_does_not_elevate`
- [x] `test_get_interfaces_raises_on_failure`
- [x] `test_configure_interface_dry_run_short_circuits`
- [x] `test_enable_interface_dispatches_ip_link_set_up`
- [x] `test_add_route_dispatches_ip_route_add`
- [x] `test_failed_write_surfaces_stderr_in_result`
- [x] `test_writes_pass_elevated_true_when_configured`
- [x] `test_render_netplan_omits_gateway_when_none`
- [x] `test_dotted_quad_netmask_is_converted_to_prefix`

## Tests — Gaps

- [ ] 48. Property-based test for netplan YAML round-trip (arbitrary
  valid netplan files should parse, mutate, and re-emit deterministically)

- [ ] 49. Retry-aspect test proving a transient `netplan apply`
  failure followed by success is transparent to the manager

## Follow-ups

- [ ] 50. **Cross-OS contract conformance review**: verify this
  implementation's `UbuntuNetworkActionResult` field shapes match the
  cross-OS contract at
  `automojo-sysman-common/specs/contracts/network-configuration/contract.md`.

- [ ] 51. **automojo-* reuse check**: confirm that no other feature
  in `automojo-sysman-linux-ubuntu` invokes `ip` / `netplan` /
  `resolvectl` directly; all such calls should go through
  `UbuntuNetworkConfigurationManager`.

- [ ] 52. **DNS persistence**: extend `set_dns_servers` to also write
  the per-interface `nameservers.addresses` block of the managed
  netplan drop-in so the configuration survives a reboot.

- [ ] 53. **Netplan merge semantics**: extend the renderer to preserve
  unrelated keys in the existing drop-in by parsing it with a real
  YAML library before mutating only the targeted ethernet block.

- [ ] 54. **Get-routes / get-interface**: implement the missing
  read verbs to fully cover the cross-OS contract's surface.
