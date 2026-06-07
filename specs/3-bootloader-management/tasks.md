# Implementation Plan -- Bootloader Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `bootloadermanagement` module under
  `source/packages/ccai/sysman/linux/ubuntu/`.

- [x] 2. Alias `UbuntuBootloaderActionStatus = ActionStatus` so the
  package-wide outcome enum is reused.

- [x] 3. Define `UbuntuBootloaderBackend` enum with the values
  `GRUB2` (default) and `SYSTEMD_BOOT`.

- [x] 4. Define `UbuntuBootloaderManagerConfig` dataclass with
  `dry_run`, `elevated`, `backend`, `grub_defaults`
  (`/etc/default/grub`), `grub_custom` (`/etc/grub.d/40_custom`),
  `grub_cfg` (`/boot/grub/grub.cfg`), and `loader_entries_dir`
  (`/boot/loader/entries`).

- [x] 5. Define `UbuntuBootloaderActionResult` dataclass extending
  `FeatureActionResult` with `entry_id`, `requires_reboot`, and
  `message`.

- [x] 6. Define `UbuntuBootEntrySpec` dataclass holding `id`, `name`,
  `kernel`, `initrd`, `cmdline`, and `device`.

- [x] 7. Define `UbuntuBootEntryInfo` dataclass holding `id`, `name`,
  `kernel`, `initrd`, `cmdline`, `device`, `is_default`, and
  `boot_count`.

- [x] 8. Define `UbuntuBootloaderQueryError` carrying `command`,
  `exit_code`, and `stderr`.

## Module-level Helpers

- [x] 9. Implement `_parse_grub_cfg_menuentries(content)` that walks
  `menuentry` and `submenu` directives and returns one
  `UbuntuBootEntryInfo` per top-level `menuentry`.

- [x] 10. Implement `_extract_quoted_argument(line)` for
  single- and double-quoted arguments to GRUB directives.

- [x] 11. Implement `_parse_grub_defaults(content)` returning a
  `KEY -> value` dict with quoting stripped.

- [x] 12. Implement `_apply_grub_defaults_keys(content, updates)`
  preserving ordering and quoting style.

- [x] 13. Implement `_render_grub_value(new_value, existing_value)`
  preserving the existing quote style.

- [x] 14. Implement `_render_custom_block(spec)` that renders a
  sentinel-bracketed `menuentry` block for
  `/etc/grub.d/40_custom`.

- [x] 15. Implement `_has_custom_block(content, entry_id)` and
  `_strip_custom_block(content, entry_id)` for sentinel-block
  membership and removal.

- [x] 16. Implement `_parse_bootctl_list(text)` that walks blank-line-
  separated stanzas and returns `UbuntuBootEntryInfo` per stanza.

- [x] 17. Implement `_parse_bootctl_status_default(text)` for the
  `Default Boot Loader Entry:` line.

- [x] 18. Implement `_render_loader_conf(spec)` producing the
  `/boot/loader/entries/<id>.conf` body.

- [x] 19. Implement `_resolve_grub_default(entries, token)` for the
  three GRUB_DEFAULT token forms (numeric, qualified id, bare title).

- [x] 20. Implement `_resolve_systemd_boot_default(entries, token)`
  matching loader-conf basenames.

## UbuntuBootloaderManager Verbs

- [x] 21. Implement `list_entries()` dispatching on backend.

- [x] 22. Implement `get_default_entry()` dispatching on backend and
  raising `UbuntuBootloaderQueryError` when unresolvable.

- [x] 23. Implement `set_default_entry(entry_id)` for both backends:
  idempotency probe -> defaults rewrite (GRUB2) /
  `bootctl set-default` (systemd-boot) -> apply step.

- [x] 24. Implement `add_entry(spec)` for both backends: idempotency
  probe -> sentinel-block append (GRUB2) / loader-conf write
  (systemd-boot) -> apply step.

- [x] 25. Implement `remove_entry(entry_id)` for both backends:
  idempotency probe -> sentinel-block strip (GRUB2) / `rm -f`
  (systemd-boot) -> apply step.

- [x] 26. Implement `update_bootloader_config()` dispatching on
  backend.

- [x] 27. Implement `regenerate_config()` dispatching on backend.

## UbuntuBootloaderManager Internal Helpers

- [x] 28. Implement `_read_grub_cfg`, `_read_grub_defaults`,
  `_read_grub_custom`, `_read_grub_default_token`, and
  `_read_loader_entry` -- all via `cat`-through-runner without
  elevation.

- [x] 29. Implement `_write_grub_defaults` and `_write_grub_custom`
  via `tee`-through-`_dispatch`.

- [x] 30. Implement `_read_systemd_boot_default` running
  `bootctl status`.

- [x] 31. Implement `_build_result(...)` constructing a
  `UbuntuBootloaderActionResult` without dispatch.

## Tests

- [x] 32. `test_list_entries_parses_grub_cfg_output` -- GRUB2 parser.

- [x] 33. `test_list_entries_systemd_boot_parses_bootctl_output` --
  systemd-boot parser.

- [x] 34. `test_get_default_entry_resolves_id` -- token resolution.

- [x] 35. `test_get_default_entry_raises_when_unresolvable` -- typed
  error.

- [x] 36. `test_set_default_entry_invokes_update_grub` -- happy path.

- [x] 37. `test_set_default_entry_skipped_when_already_default` --
  idempotency.

- [x] 38. `test_set_default_entry_systemd_boot_invokes_bootctl` --
  systemd-boot dispatch.

- [x] 39. `test_add_entry_invokes_update_grub` -- happy path GRUB2.

- [x] 40. `test_add_entry_skipped_when_present` -- idempotency.

- [x] 41. `test_add_entry_dry_run_short_circuits` -- dry-run.

- [x] 42. `test_add_entry_systemd_boot_invokes_tee` -- systemd-boot
  dispatch.

- [x] 43. `test_remove_entry_invokes_update_grub` -- happy path
  GRUB2.

- [x] 44. `test_remove_entry_skipped_when_absent` -- idempotency.

- [x] 45. `test_update_bootloader_config_invokes_update_grub` -- apply
  step.

- [x] 46. `test_regenerate_config_invokes_grub_mkconfig` -- regen
  step.

- [x] 47. `test_regenerate_config_systemd_boot_invokes_bootctl_install`
  -- systemd-boot regen step.

- [x] 48. `test_writes_elevate` -- elevation policy.

- [x] 49. `test_reads_never_elevate` -- elevation policy.

- [x] 50. `test_failed_command_surfaces_stderr` -- error surface.

## Follow-ups

- [ ] 51. Wire `running-kernel safety` for `remove_entry` once the
  Ubuntu manager exposes a parsed `/proc/cmdline` view.  The
  cross-OS contract requires `FAILED` when removing the entry
  matches the active boot selection; this manager currently relies on
  the GRUB config being self-consistent and does not consult
  `/proc/cmdline` directly.

- [ ] 52. Surface a `boot_count` field on systemd-boot entries when
  the host supports the `LoaderBootCountPath` boot-count protocol.

- [ ] 53. Add a `set_default_saved_entry` helper that switches GRUB
  into `GRUB_DEFAULT=saved` mode and runs `grub-set-default <id>`.
  Currently `set_default_entry` only writes the literal id form.
