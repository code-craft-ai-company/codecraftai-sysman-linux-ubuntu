# Implementation Plan — Ubuntu Python Environment Management

Checked items (`[x]`) are implemented and covered by tests.

## Module

- [x] 1. Create `ccai.sysman.linux.ubuntu.pythonenvironmentmanagement`
- [x] 2. Define OS-prefixed aliases of the shared types
- [x] 3. Define `UbuntuPythonEnvManagerConfig` (prerequisite_packages, apt_options)
- [x] 4. Implement `UbuntuPythonEnvironmentManager._build_layout` → POSIX layout
- [x] 5. Implement `ensure_prerequisites` delegating to `UbuntuPackageManager`
- [x] 6. Implement `_typed_config` upgrade of a plain base config

## Tests

- [x] 7. `test_layout_is_posix`
- [x] 8. `test_ensure_prerequisites_installs_default_apt_packages`
- [x] 9. `test_ensure_prerequisites_honours_override_and_config`
- [x] 10. `test_ensure_prerequisites_stops_at_first_failure`
- [x] 11. `test_ensure_prerequisites_dry_run_dispatches_nothing`
- [x] 12. `test_plain_base_config_is_accepted`
- [x] 13. `test_deploy_end_to_end_on_ubuntu_argv`

## Follow-ups

- [ ] 14. Optional `chown -R <user>` step after an elevated create (delegate to
  `user-group-management` / `acl-management`), pending a contract extension
- [ ] 15. Live-host verification on Ubuntu 22.04 / 24.04 images (stock image
  without `python3-venv`, then with)
  *Partially done 2026-09-02 on Ubuntu 24.04.4 with `python3-venv` present,
  unelevated, through the real `UbuntuLocalCommandRunner`: prerequisites
  probe `SKIPPED` ×3 with no `apt-get` call; fresh `deploy` created the
  venv, upgraded pip, installed `six==1.16.0` and `packaging>=23`; reads
  returned 3.12.3 and the three distributions; a second `deploy` was
  `SKIPPED` throughout; a changed pin dispatched; uninstall / remove and
  their repeats behaved idempotently.  Still open: the stock image without
  `python3-venv` (needs an elevated runner or a container host).*
