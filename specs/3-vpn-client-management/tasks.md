# Implementation Plan — Ubuntu VPN Client Management

## Module and Types

- [x] 1. Create the `automojo.sysman.linux.ubuntu.vpnclientmanagement` module.

- [x] 2. Re-export `UbuntuVPNActionStatus`, `UbuntuVPNActionResult`,
  `UbuntuVPNClientStatus`, `UbuntuVPNClientQueryError` from the
  `vpnclientmanagementbase` module under Ubuntu-prefixed names.

- [x] 3. Define `UbuntuVPNClientManagerConfig` extending
  `VPNClientManagerConfig` with `tailscale_channel`, `keyring_path`,
  `sources_path`, `twingate_sources_path`, `twingate_config_path` fields.

## UbuntuVPNClientManager — Implementation

- [x] 4. Implement `__init__(*, runner, config=None)` calling
  `super().__init__(runner=runner, config=config or UbuntuVPNClientManagerConfig())`.

- [x] 5. Implement `_dpkg_installed_version(pkg)` helper using
  `dpkg-query -W -f='${Version}'` via the runner; returns `None` when absent.

- [x] 6. Implement `_ensure_tailscale_repo(aspects)` — heredoc-write keyring
  + sources file, then `apt-get update`. Each step routed through the runner
  with the contract elevation rule.

- [x] 7. Implement `_ensure_twingate_repo(aspects)` — same shape for Twingate.

- [x] 8. Implement `install_tailscale(*, version, channel, aspects)` —
  idempotency guard via `_dpkg_installed_version`, then repo bootstrap,
  then `apt-get install -y tailscale[=<version>]`.

- [x] 9. Implement `install_twingate_connector(*, version, aspects)` —
  parallel shape for Twingate.

- [x] 10. Implement `configure(*, provider, settings, aspects)` —
  Twingate writes `/etc/twingate/connector.conf` via heredoc through `tee`;
  Tailscale returns `SKIPPED` with explanatory `detail`.

- [x] 11. Implement `bring_up(*, provider, authkey, hostname, advertise_tags,
  advertise_routes, exit_node, aspects)` — Tailscale builds `tailscale up …`
  argv; Twingate runs `systemctl enable --now twingate-connector` after
  `configure`. Idempotency guard via `status(...)`.

- [x] 12. Implement `bring_down(*, provider, aspects)` —
  `tailscale down` / `systemctl stop twingate-connector`. Idempotency guard.

- [x] 13. Implement `status(*, provider, aspects)` — invoke
  `tailscale status --json` / `twingate status`; parse to
  `UbuntuVPNClientStatus`; return `installed=False` shape when the binary
  is missing.

- [x] 14. Implement `uninstall(*, provider, purge, aspects)` — bring down,
  `apt-get remove -y` (or `apt-get purge -y` when `purge=True`),
  optionally remove the apt source and keyring files via runner `rm`.

## Parsers

- [x] 15. Implement `_parse_tailscale_status_json(payload: str)` returning
  `UbuntuVPNClientStatus`. Total over all backend states.

- [x] 16. Implement `_parse_twingate_status_text(payload: str)` returning
  `UbuntuVPNClientStatus(self_address=None, peer_addresses={})`.

## Tests — Examples

- [x] 17. `test_install_tailscale_runs_apt_install`.
- [x] 18. `test_install_tailscale_skipped_when_present`.
- [x] 19. `test_install_tailscale_failed_on_apt_error`.
- [x] 20. `test_install_twingate_connector_runs_apt_install`.
- [x] 21. `test_install_twingate_connector_skipped_when_present`.
- [x] 22. `test_configure_tailscale_returns_skipped`.
- [x] 23. `test_configure_twingate_writes_conf_via_tee`.
- [x] 24. `test_configure_twingate_skipped_when_content_matches`.
- [x] 25. `test_bring_up_tailscale_runs_tailscale_up_with_flags`.
- [x] 26. `test_bring_up_tailscale_skipped_when_running_with_matching_state`.
- [x] 27. `test_bring_up_twingate_runs_systemctl_enable_now`.
- [x] 28. `test_bring_down_tailscale_runs_tailscale_down`.
- [x] 29. `test_bring_down_twingate_runs_systemctl_stop`.
- [x] 30. `test_status_tailscale_parses_running_json`.
- [x] 31. `test_status_tailscale_returns_installed_false_when_missing`.
- [x] 32. `test_status_twingate_parses_text`.
- [x] 33. `test_uninstall_tailscale_purge_removes_repo_files`.
- [x] 34. `test_dry_run_short_circuits_every_write_verb`.
- [x] 35. `test_writes_pass_elevated_when_configured`.
- [x] 36. `test_reads_never_elevate`.

## Tests — Property (hypothesis)

- [x] 37. `test_property_install_tailscale_idempotent` — generate
  `(version, channel)`; second call returns `SKIPPED`.
- [x] 38. `test_property_uninstall_idempotent` — second call returns
  `SKIPPED` for both providers.
- [x] 39. `test_property_bring_up_idempotent_on_matching_state`.
- [x] 40. `test_property_dry_run_never_invokes_runner`.

## Documentation

- [x] 41. Sphinx-style docstrings on every public method.

- [x] 42. Module-level docstring linking to the contract and the base spec.

## Follow-ups

- [x] 43. Cross-OS contract conformance check — diff dataclass field shapes
  against the contract.

- [x] 44. Confirm `3-package-management` and `3-service-management`
  primitives are reused for any future refactor (do not duplicate apt /
  systemctl logic inline once those expose the right helpers).
