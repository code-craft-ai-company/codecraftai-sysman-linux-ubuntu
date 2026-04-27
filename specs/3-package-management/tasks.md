# Implementation Plan — Ubuntu Package Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.linux.ubuntu.packagemanagement`
  module

- [x] 2. Define `UbuntuPackageActionStatus` enum (SUCCESS / FAILED /
  SKIPPED / DRY_RUN)

- [ ] 3. Define `UbuntuPackageManagerConfig` dataclass (dry_run,
  elevated, assume_yes, extra_env)
  *Implemented with `dry_run`, `elevated`, `apt_options`, and
  `noninteractive_env` instead of `assume_yes` / `extra_env`.  See the
  spec deviations note below.*

- [ ] 4. Define `UbuntuPackageActionResult`, `UbuntuPackageInfo`,
  and `UbuntuPackageUpdateInfo` dataclasses
  *Implemented `UbuntuPackageActionResult` and `UbuntuPackageInfo`.
  `UbuntuPackageUpdateInfo` is not implemented because
  `list_upgradable` was descoped from this batch.*

- [ ] 5. Define `UbuntuPackageQueryError` and
  `UbuntuPackageFileNotFoundError` exceptions
  *Implemented `UbuntuPackageQueryError`.  `UbuntuPackageFileNotFoundError`
  is not implemented because `install_local` was descoped from this
  batch.*

## UbuntuPackageManager — Implemented

- [x] 6. Implement `__init__` accepting runner + optional config;
  initialise the default `DEBIAN_FRONTEND=noninteractive` env when
  `extra_env` is `None`
  *Inherited from `BaseFeatureManager`; the noninteractive env is
  built per-write from the `noninteractive_env` config flag.*

- [x] 7. Implement `install_package(name, version=None)` — probe
  via `dpkg -l` / `dpkg -s`, dispatch `apt-get install -y <name>`
  or `apt-get install -y <name>=<version>`, honor `assume_yes=False`
  by omitting `-y`
  *Probe is via `dpkg -s` (`get_package_info`).  `assume_yes=False`
  branch is not implemented.*

- [x] 8. Implement `remove_package(name)` — probe via `dpkg -l`,
  dispatch `apt-get remove -y <name>`, `SKIPPED` when not installed

- [x] 9. Implement `upgrade_package(name, version=None)` — probe
  via `dpkg -s`, dispatch
  `apt-get install -y --only-upgrade <name>[=<version>]`, `SKIPPED`
  when not installed (message `"package is not installed; nothing
  to upgrade"`) or already at requested version
  *Implemented without the `version` parameter.  Probe is via
  `apt-cache policy` so the candidate version comparison is precise;
  the SKIPPED-when-not-installed message matches.*

- [x] 10. Implement `upgrade_all()` — dispatch `apt-get upgrade -y`
  with `DEBIAN_FRONTEND=noninteractive`; never skips

- [x] 11. Implement `is_installed(name)` — read-only `dpkg -l <name>`
  status-byte inspection, returns `True` only for `ii` rows

- [x] 12. Implement `get_package_info(name)` — `dpkg -s <name>` with
  `None` fallback for the "not installed and no information" sentinel
  and `UbuntuPackageQueryError` for other failures

- [x] 13. Implement `list_installed()` — `dpkg-query -W
  -f='${Package}\t${Version}\t${Architecture}\t${Status}\n'`,
  filter rows whose `Status` ends in `installed`

- [ ] 14. Implement `list_upgradable()` — `apt list --upgradable`
  parser skipping the `Listing…` header, regex over the
  `<name>/<repo> <avail> <arch> [upgradable from: <installed>]`
  lines

- [ ] 15. Implement `install_local(filepath)` — precondition
  `pathlib.Path.exists()` check raising
  `UbuntuPackageFileNotFoundError`, then
  `apt-get install -y <filepath>` with the same env and elevation
  policy as other writes

- [x] 16. Implement `refresh_metadata()` — dispatch
  `apt-get update`; never skips

- [x] 17. Implement `_dispatch` helper that merges `extra_env` with
  `DEBIAN_FRONTEND=noninteractive`, honors `dry_run`, populates
  `command` on the returned result, and sets `elevated` according
  to whether the verb is a write
  *Implemented as `_dispatch_with_env` because the shared
  `BaseFeatureManager._dispatch` does not currently forward `env`.*

## UbuntuPackageManager — Gaps (specified but not implemented)

- [ ] 18. `apt-mark hold` / `apt-mark unhold` for version-hold
  semantics — out of base contract, reserved as future extension

- [ ] 19. Snap / Flatpak delegation — out of base contract; separate
  manager

- [ ] 20. Package-group install (`apt-get install <metapackage>`
  with `tasksel`) — out of base contract

- [ ] 21. Offline cache install (`apt-get install --no-download`
  with a pre-populated `/var/cache/apt/archives`) — out of base
  contract

## Tests — Implemented

- [x] 22. `test_install_package_dispatches_apt_get_install`
  *Implemented as `test_install_package_invokes_apt_install`.*
- [x] 23. `test_install_package_pins_version_with_equals_syntax`
  *Implemented as
  `test_install_package_with_version_constructs_correct_argv`.*
- [x] 24. `test_install_package_skipped_when_already_installed`
- [ ] 25. `test_install_package_skipped_when_version_matches`
- [ ] 26. `test_install_package_assume_yes_false_omits_dash_y`
- [ ] 27. `test_install_package_injects_debian_frontend_env`
  *Covered transitively by `test_install_package_invokes_apt_install`,
  which asserts the env dict contains
  `DEBIAN_FRONTEND=noninteractive`.*
- [x] 28. `test_remove_package_dispatches_apt_get_remove`
  *Implemented as `test_remove_package_invokes_apt_remove`.*
- [x] 29. `test_remove_package_skipped_when_not_installed`
- [x] 30. `test_upgrade_package_dispatches_only_upgrade`
  *Implemented as
  `test_upgrade_package_invokes_apt_install_only_upgrade`.*
- [ ] 31. `test_upgrade_package_skipped_when_not_installed_has_message`
- [x] 32. `test_upgrade_package_skipped_when_already_at_version`
  *Implemented as
  `test_upgrade_package_skipped_when_already_at_latest`; uses the
  apt-cache candidate as the comparator instead of an explicit
  argument.*
- [x] 33. `test_upgrade_all_dispatches_apt_get_upgrade`
  *Implemented as `test_upgrade_all_invokes_apt_upgrade`.*
- [ ] 34. `test_upgrade_all_never_skips`
- [x] 35. `test_is_installed_true_for_ii_row`
  *Implemented as `test_is_installed_returns_true_for_installed`.*
- [x] 36. `test_is_installed_false_for_rc_row`
  *Implemented as `test_is_installed_returns_false_for_rc_status`
  plus `test_is_installed_returns_false_for_not_installed`.*
- [x] 37. `test_get_package_info_parses_dpkg_s_output`
- [x] 38. `test_get_package_info_returns_none_for_unknown_package`
  *Implemented as
  `test_get_package_info_returns_none_when_not_installed`.*
- [ ] 39. `test_get_package_info_raises_on_unexpected_error`
- [x] 40. `test_list_installed_parses_dpkg_query_tab_output`
  *Implemented as
  `test_list_installed_parses_dpkg_query_output_and_filters_removed`.*
- [x] 41. `test_list_installed_raises_on_non_zero_exit`
  *Implemented as `test_list_installed_raises_on_runner_failure`.*
- [ ] 42. `test_list_upgradable_parses_apt_list_output`
- [ ] 43. `test_list_upgradable_empty_when_only_listing_header`
- [ ] 44. `test_install_local_dispatches_apt_get_install_filepath`
- [ ] 45. `test_install_local_raises_for_missing_file`
- [x] 46. `test_refresh_metadata_dispatches_apt_get_update`
  *Implemented as `test_refresh_metadata_invokes_apt_update`.*
- [ ] 47. `test_refresh_metadata_never_skips`

## Tests — Gaps

- [ ] 48. Test dry-run short-circuits for every mutating method
  (install, remove, upgrade, upgrade_all, install_local,
  refresh_metadata)
  *`test_dry_run_short_circuits_install` covers the install branch;
  the other write verbs share the same dry-run guard but are not
  individually covered.*

- [ ] 49. Test dry-run still raises
  `UbuntuPackageFileNotFoundError` (precondition exception takes
  precedence over dry-run)

- [x] 50. Test elevation is set for writes when config enables it
  *Implemented as `test_write_verbs_pass_elevated_true`.*

- [x] 51. Test elevation is NEVER set for reads or idempotency
  probes regardless of config
  *Implemented as `test_read_verbs_never_elevate`.*

- [ ] 52. Property-based tests (hypothesis) covering Properties
  1 through 30

## Follow-ups

- [ ] 53. **Cross-OS contract conformance review**: verify the
  `UbuntuPackageActionResult` / `UbuntuPackageInfo` /
  `UbuntuPackageUpdateInfo` field shapes match the cross-OS
  contract at
  `automojo-sysman-common/specs/contracts/package-management/contract.md`
  and that `install_local` precondition semantics align with the
  contract.

- [ ] 54. **automojo-* reuse check**: confirm no other feature in
  `automojo-sysman-linux-ubuntu` invokes `apt-get` / `dpkg` /
  `apt` directly; all such calls should go through
  `UbuntuPackageManager`.

- [ ] 55. **`apt-mark hold` extension**: plan a follow-up that
  adds `hold_package` / `release_hold` verbs matching the RHEL
  reference's `hold` semantics while remaining outside the base
  contract surface.

## Spec deviations from this implementation

- The drafted spec calls for an `assume_yes: bool` and `extra_env:
  dict[str,str] | None` config.  The pilot batch instead uses
  `apt_options: list[str] | None` (caller-controlled tail flags) and
  `noninteractive_env: bool` (toggle for the noninteractive env
  injection).  The contract surface — `apt-get install -y <name>` with
  `DEBIAN_FRONTEND=noninteractive` — is preserved.

- `upgrade_package(name)` does not accept a `version` argument in this
  batch.  The candidate comparison is delegated to ``apt-cache
  policy``.  A future increment can add an explicit-version variant.

- `list_upgradable`, `install_local`, and the
  `UbuntuPackageFileNotFoundError` / `UbuntuPackageUpdateInfo` types
  are out of scope for this batch and remain unchecked.
