# Tasks

This task list maps the Ubuntu localization-management implementation
to the requirements in `requirements.md` and the design in
`design.md`. Every task is exercised by at least one test in
`source/testroots/testbase/ccai/tests/sysman/linux/ubuntu/test_localizationmanagement.py`.

---

## In-scope tasks

### 1. Module skeleton and exports

- [x] Create
      `source/packages/ccai/sysman/linux/ubuntu/localizationmanagement.py`.
- [x] Export `UbuntuLocalizationManager`,
      `UbuntuLocalizationManagerConfig`,
      `UbuntuLocalizationActionStatus`,
      `UbuntuLocalizationActionResult`, `UbuntuLocaleInfo`,
      `UbuntuKeymapInfo`, `UbuntuLocalizationQueryError`.
- [x] Subclass `BaseFeatureManager` and set `feature_name =
      "localization-management"`, `result_class =
      UbuntuLocalizationActionResult`,
      `status_class = UbuntuLocalizationActionStatus`.

Covers Requirement 1, 4, 7 and the contract's naming clause.

### 2. Read verbs

- [x] `get_locale()` — `localectl status` parser; derive country and
      encoding from `LANG`. Raises `UbuntuLocalizationQueryError` on
      non-zero exit. (Requirement 1)
- [x] `list_supported_locales()` — `locale -a` parser. (Requirement 3)
- [x] `get_keymap()` — `localectl status` parser; populate VC and
      X11 fields. (Requirement 4)
- [x] `get_supported_keymaps()` — `localectl list-keymaps` parser.
      (Requirement 6)
- [x] `get_timezone()` — `timedatectl show --property=Timezone
      --value` parser. (Requirement 7)
- [x] `get_supported_timezones()` — `timedatectl list-timezones`
      parser. (Requirement 9)
- [x] None of the read paths pass `elevated=True`. (Requirement 10)

### 3. Write verbs

- [x] `set_locale(locale, *, lc_overrides=None)` — argv build,
      idempotency probe, dry-run short-circuit, runner dispatch,
      result wrap. (Requirement 2)
- [x] `set_keymap(keymap, *, x11_layout=None, x11_variant=None,
      x11_model=None)` — VC-then-X11 two-phase dispatch, idempotency
      across both phases, dry-run short-circuit. (Requirement 5)
- [x] `set_timezone(tz)` — argv build, idempotency probe, dry-run
      short-circuit. (Requirement 8)
- [x] Each write honours `config.elevated` on the dispatch step
      and never elevates on the probe step. (Requirement 10)

### 4. Result-shape population

- [x] Populate `result.setting` to `"locale"`, `"keymap"`, or
      `"timezone"` on every returned write result.
- [x] Populate `result.previous_value` from the read probe (when it
      ran cleanly).
- [x] Set `result.requires_reboot=False` on Ubuntu (no reboot
      required for `localectl` / `timedatectl` writes).

### 5. Module-level helpers

- [x] `_parse_localectl_status(text)` — line-prefix-driven parser.
- [x] `_parse_system_locale_kv(text)` — whitespace-split tokeniser.
- [x] `_derive_country_and_encoding(language)` — pure derivation.
- [x] `_parse_locale_a(text)` — one-entry-per-line parser.
- [x] `_parse_list_lines(text)` — generic one-entry-per-line parser
      reused by `list-keymaps` and `list-timezones`.

### 6. Tests

- [x] `test_get_locale_parses_localectl_output`
- [x] `test_get_keymap_parses_localectl_output`
- [x] `test_get_timezone_parses_timedatectl_output`
- [x] `test_list_supported_locales_returns_list`
- [x] `test_get_supported_timezones_parses_list`
- [x] `test_get_supported_keymaps_returns_list`
- [x] `test_set_locale_invokes_localectl`
- [x] `test_set_locale_skipped_when_match`
- [x] `test_set_locale_dry_run_short_circuits`
- [x] `test_set_locale_with_lc_overrides_includes_kv_pairs`
- [x] `test_set_keymap_invokes_localectl`
- [x] `test_set_keymap_skipped_when_match`
- [x] `test_set_keymap_with_x11_invokes_set_x11_keymap`
- [x] `test_set_timezone_invokes_timedatectl`
- [x] `test_set_timezone_skipped_when_match`
- [x] `test_set_timezone_dry_run_short_circuits`
- [x] `test_writes_elevate_per_policy`
- [x] `test_reads_never_elevate`
- [x] `test_failed_command_surfaces_stderr`
- [x] `test_get_locale_raises_on_localectl_failure`

Total: 20 tests; the cross-OS-divergent contract requires at least
14.

### 7. Spec deliverables

- [x] `specs/3-localization-management/requirements.md`
- [x] `specs/3-localization-management/design.md`
- [x] `specs/3-localization-management/tasks.md`

---

## Out-of-scope (deferred follow-ups)

The following items live outside the present pilot. They are tracked
here so a future revision can be opened without re-discovering them.

* **`regenerate_locale(locale)`** — wrap `locale-gen <locale>` plus
  `update-locale` for hosts that have not yet generated the requested
  locale. The current pilot assumes the requested locale already
  exists in the host's compiled archive (i.e., is reported by
  `locale -a`).
* **Per-user locale.** Mutating `~/.pam_environment` or
  `~/.config/locale.conf` is not supported. The pilot only mutates
  system-default state.
* **`localectl --convert` opt-out on `set_keymap`.** The manager
  currently lets `localectl` decide whether a VC change should also
  flow into the X11 layout. Surfacing the flag explicitly is a
  documented follow-up.
* **`UbuntuTimezoneInfo`.** The cross-OS contract names a
  `TimezoneInfo` with `utc_offset_seconds` and `dst_active`. Ubuntu
  surfaces those via `precision-time-management` instead, so this
  pilot returns a plain `str` from `get_timezone`. If the contract
  is revised to collapse the overlap, this manager will grow a
  full `UbuntuTimezoneInfo`.
* **Input-method frameworks.** IBus / SCIM / IME are explicitly
  out of scope per the cross-OS contract and are not on the
  follow-up list.
