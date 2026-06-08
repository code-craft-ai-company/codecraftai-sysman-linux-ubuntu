# Tasks

## Implementation Plan

### 1. Module Skeleton

- [ ] Create
  `source/packages/ccai/sysman/linux/ubuntu/authenticationmanagement.py`.
- [ ] Add module-level constants for `cat`, `tee`,
  `pam-auth-update`, the default `/etc/pam.d` directory, and the
  default `/usr/share/pam-configs/` directory.
- [ ] Import `ActionStatus`, `BaseFeatureManager`,
  `FeatureActionResult`, `FeatureConfig` from
  `ccai.sysman.linux.ubuntu.featurebase`.

---

### 2. Data Models

- [ ] Alias `UbuntuAuthActionStatus = ActionStatus`.
- [ ] Define `UbuntuAuthSubsystem` dataclass with `kind` and `name`.
- [ ] Define module-level constants `UBUNTU_AUTH_LOGIN`,
  `UBUNTU_AUTH_SUDO`, `UBUNTU_AUTH_SU`, `UBUNTU_AUTH_SSH`,
  `UBUNTU_AUTH_CRON` as ready-to-use `UbuntuAuthSubsystem`
  instances.
- [ ] Define `UbuntuAuthEntry` dataclass.
- [ ] Define `UbuntuAuthConfig` dataclass.
- [ ] Define `UbuntuAuthMethodInfo` dataclass.
- [ ] Define `UbuntuAuthValidationResult` dataclass.
- [ ] Define `UbuntuAuthActionResult` extending `FeatureActionResult`
  with `subsystem` and `method` fields.
- [ ] Define `UbuntuAuthenticationManagerConfig` extending
  `FeatureConfig` with `pam_dir`, `pam_configs_dir`,
  `pam_auth_update`.

---

### 3. Exceptions

- [ ] Define `UbuntuAuthenticationQueryError` with `command` /
  `exit_code` / `stderr` constructor.
- [ ] Define `UbuntuAuthenticationConfigError` for the malformed
  config case (cross-contract conformance even though the pilot
  only enforces it lightly).
- [ ] Define `UbuntuAuthenticationSubsystemNotFoundError` for
  unknown subsystems and unregistered method names.

---

### 4. Parser / Renderer

- [ ] Implement `_parse_pam_stack(content)` returning
  `list[UbuntuAuthEntry]`. Handle `[bracketed]` controls,
  `@include`, and arg-tail tokens.
- [ ] Implement `_render_pam_stack(entries)` returning a
  newline-terminated `str`.
- [ ] Implement `_parse_pam_auth_update_list(text)` returning
  `dict[str, tuple[bool, str]]` keyed by method name, value
  `(enabled, description)`.
- [ ] Implement `_parse_pam_config_profile(text)` returning a
  `dict[str, str]` of the top-level keys (`Name`, `Default`,
  `Priority`, ...).

---

### 5. Manager — Read Verbs

- [ ] Implement `get_auth_config(subsystem)`. Resolve basename;
  invoke `cat`; raise on stderr "No such file"; parse.
- [ ] Implement `list_auth_methods()`. Run
  `pam-auth-update --list`; merge with `ls -1` of the configs
  directory; parse each profile file for description.
- [ ] Implement `validate_auth_config(subsystem)`. Re-parse and
  walk for structural errors.

---

### 6. Manager — Write Verbs

- [ ] Implement `set_auth_config(subsystem, config)`. Read; render;
  compare; SKIPPED on equal; DRY_RUN under dry-run; otherwise
  `tee` with `elevated=True`.
- [ ] Implement `enable_method(name)`. Probe
  `list_auth_methods()`; SKIPPED if already enabled; raise
  `UbuntuAuthenticationSubsystemNotFoundError` if unregistered;
  `pam-auth-update --enable <name>` with elevation.
- [ ] Implement `disable_method(name)`. Symmetric.

---

### 7. Manager — Internal Helpers

- [ ] `_typed_config()` narrows the base config to
  `UbuntuAuthenticationManagerConfig`.
- [ ] `_resolve_subsystem(subsystem)` returns a
  `(basename, kind_label)` tuple from either an enum, a string, or
  an `UbuntuAuthSubsystem` instance.
- [ ] `_pam_path(basename)` joins the configured directory.
- [ ] `_read_file(path)` runs `cat` with `elevated=False`, returns
  `Optional[str]`.
- [ ] `_build_result(...)` composes a result without invoking the
  runner.
- [ ] `_wrap_runner_result(...)` maps a `RunResult` to an
  `UbuntuAuthActionResult`.

---

### 8. Tests

- [ ] Create
  `source/testroots/testbase/ccai/tests/sysman/linux/ubuntu/test_authenticationmanagement.py`.
- [ ] Add the `_MockCommandRunner` shape used in
  `test_sshserverconfiguration.py`.
- [ ] `test_get_auth_config_parses_cat_output`.
- [ ] `test_get_auth_config_raises_subsystem_not_found_when_missing`.
- [ ] `test_set_auth_config_invokes_tee`.
- [ ] `test_set_auth_config_skipped_when_match`.
- [ ] `test_set_auth_config_dry_run`.
- [ ] `test_list_auth_methods_parses_pam_auth_update_output`.
- [ ] `test_list_auth_methods_falls_back_to_pam_configs_when_list_fails`.
- [ ] `test_enable_method_invokes_pam_auth_update`.
- [ ] `test_enable_method_skipped_when_enabled`.
- [ ] `test_enable_method_raises_when_not_registered`.
- [ ] `test_disable_method_invokes_pam_auth_update`.
- [ ] `test_disable_method_skipped_when_disabled`.
- [ ] `test_validate_auth_config_returns_result`.
- [ ] `test_validate_auth_config_flags_unrecognised_module_type`.
- [ ] `test_writes_elevate`.
- [ ] `test_reads_never_elevate`.
- [ ] `test_failed_command_surfaces_stderr`.
- [ ] `test_pam_stack_parser_handles_includes`.
- [ ] `test_pam_stack_parser_handles_bracketed_control`.
- [ ] `test_render_round_trip_is_stable`.

---

### 9. Quality Bar

- [ ] No emojis anywhere in source / specs / tests.
- [ ] All public types prefixed `Ubuntu`.
- [ ] Sphinx docstrings on every public method.
- [ ] Explicit `return` statements; no compound returns.
- [ ] No mutable default values.
- [ ] Named-parameter passing throughout.
- [ ] Multi-line error messages built via the list / join pattern.
