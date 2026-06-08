# Implementation Plan — Ubuntu Encryption Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the
  `ccai.sysman.linux.ubuntu.encryptionmanagement` module.

- [x] 2. Define `UbuntuEncryptionActionStatus` (aliased to the shared
  `featurebase.ActionStatus`).

- [x] 3. Define `UbuntuEncryptionManagerConfig` dataclass with
  `allow_destructive`, `default_cipher`, `default_key_size` extras
  on top of the base `dry_run` / `elevated`.

- [x] 4. Define `UbuntuEncryptionActionResult` dataclass with
  `volume`, `device`, `requires_reboot` extras.

- [x] 5. Define `UbuntuEncryptionSpec`, `UbuntuEncryptionStatus`,
  `UbuntuEncryptedVolumeInfo` dataclasses.

- [x] 6. Define `UbuntuEncryptionQueryError` exception.

- [x] 7. Define module-level `_DESTRUCTIVE_GUARD_DETAIL`,
  `_DEFAULT_CIPHER`, `_DEFAULT_KEY_SIZE` constants.

## UbuntuEncryptionManager — Implemented

- [x] 8. `__init__` accepting runner + optional config (inherited
  from `BaseFeatureManager`).

- [x] 9. `enable_encryption(volume, spec)` — `cryptsetup luksFormat
  --batch-mode --type luks2 --cipher <c> --key-size <k> <device>`
  with passphrase via stdin; followed by `cryptsetup luksOpen`;
  idempotent via `cryptsetup isLuks`.

- [x] 10. `disable_encryption(volume)` — `cryptsetup erase <device>`;
  destructive; gated by `config.allow_destructive`; idempotent via
  `cryptsetup isLuks`.

- [x] 11. `get_encryption_status(volume)` — merges `luksDump` and
  `cryptsetup status` output into an `UbuntuEncryptionStatus`.

- [x] 12. `list_encrypted_volumes()` — `lsblk -J ...` filtered to
  `crypto_LUKS`; per-row `luksDump`.

- [x] 13. `rotate_key(volume, old, new)` — `cryptsetup luksChangeKey
  <device>` with both passphrases on a single stdin payload
  separated by `\n`; unconditional.

- [x] 14. `backup_recovery_key(volume, path)` — `cryptsetup
  luksHeaderBackup <device> --header-backup-file <path>`;
  unconditional.

- [x] 15. `unlock_volume(volume, passphrase)` — `cryptsetup luksOpen
  <device> <name>` with passphrase via stdin; idempotent via
  `cryptsetup status`.

- [x] 16. `_resolve_device_path` / `_resolve_mapping_name` helpers.

- [x] 17. `_is_luks` / `_luks_dump` / `_cryptsetup_status` probe
  helpers.

- [x] 18. `_dispatch_encryption_write` and `_build_result` helpers
  that thread `volume` / `device` / `requires_reboot` through
  `BaseFeatureManager._dispatch`.

## Module-level parsers — Implemented

- [x] 19. `_parse_luks_dump` — handles both LUKS1 and LUKS2 dump
  layouts; emits cipher / key_size / per-slot strings.

- [x] 20. `_parse_cryptsetup_status` — detects active mappings and
  surfaces cipher / device fields.

- [x] 21. `_parse_lsblk_crypto_rows` and `_flatten_lsblk` — flatten
  the `lsblk -J` tree and filter to `crypto_LUKS` rows; resolve
  active-mapping name and mountpoint from `crypt`-type children.

## UbuntuEncryptionManager — Gaps (specified but not implemented)

- [ ] 22. UUID / label resolver so callers can pass `UUID=<uuid>`
  or `LABEL=<label>` instead of a device path or mapping name.

- [ ] 23. Strict-read mode for `get_encryption_status` /
  `list_encrypted_volumes` that raises
  `UbuntuEncryptionQueryError` instead of returning empty / plain
  records.

- [ ] 24. TPM2 enrolment via `systemd-cryptenroll` (RHEL
  canonical's `enroll_tpm2`).

- [ ] 25. Clevis-based network-bound disk encryption (RHEL
  canonical's `enroll_clevis`).

- [ ] 26. `/etc/crypttab` reader / writer for boot-time unlock
  wiring.

- [ ] 27. Header-restore verb (`cryptsetup luksHeaderRestore
  --header-backup-file <path>`).

- [ ] 28. Spec-equivalence comparison in `enable_encryption`'s
  idempotency guard (currently checks LUKS-vs-not-LUKS only; a
  stricter guard would compare cipher and key-size against the
  existing header and return SKIPPED only when they match).

## Tests — Implemented

- [x] 29. `test_enable_encryption_invokes_cryptsetup_luksformat`
- [x] 30. `test_enable_encryption_skipped_when_already_encrypted`
- [x] 31. `test_enable_encryption_dry_run_short_circuits`
- [x] 32. `test_disable_encryption_requires_allow_destructive`
- [x] 33. `test_disable_encryption_invokes_cryptsetup_erase_when_allowed`
- [x] 34. `test_disable_encryption_skipped_when_already_plain`
- [x] 35. `test_get_encryption_status_parses_luksdump_and_status`
- [x] 36. `test_get_encryption_status_handles_plain_device`
- [x] 37. `test_list_encrypted_volumes_filters_crypto_luks`
- [x] 38. `test_rotate_key_invokes_luksChangeKey_with_combined_stdin`
- [x] 39. `test_backup_recovery_key_writes_to_path`
- [x] 40. `test_unlock_volume_passes_passphrase_via_stdin`
- [x] 41. `test_unlock_volume_skipped_when_already_unlocked`
- [x] 42. `test_passphrase_never_appears_in_recorded_argv`
- [x] 43. `test_writes_elevate_per_config`
- [x] 44. `test_reads_never_elevate`
- [x] 45. `test_failed_command_surfaces_stderr`
- [x] 46. `test_parse_luks_dump_handles_luks2_output`
- [x] 47. `test_parse_cryptsetup_status_detects_active`
- [x] 48. `test_parse_lsblk_filters_crypto_luks_only`

## Tests — Gaps

- [ ] 49. LUKS1 dump parser test (current parser supports both;
  test only covers LUKS2).
- [ ] 50. `enable_encryption` test that asserts the post-format
  `luksOpen` is dispatched with the same passphrase on stdin.
- [ ] 51. `disable_encryption` confirmation-string test (asserts
  `stdin="YES\n"` is delivered to `cryptsetup erase`).
- [ ] 52. Last-key safety-rail test once a key-slot lifecycle verb
  set lands (the contract calls out the rail; today's surface has
  no per-slot remove verb).

## Follow-ups

- [ ] 53. **Cross-OS contract conformance review**: verify
  `UbuntuEncryptionActionResult` field shapes match the cross-OS
  contract at
  `codecraftai-sysman-common/specs/contracts/encryption-management/contract.md`.

- [ ] 54. **codecraftai-* reuse check**: confirm that no other
  feature in `codecraftai-sysman-linux-ubuntu` invokes `cryptsetup`
  directly; all such calls should go through
  `UbuntuEncryptionManager`.

## Spec deviations from this implementation

- **No per-slot `add_key` / `remove_key_by_slot` verbs.**  The
  cross-OS contract describes a key-slot lifecycle (enumerate /
  add / remove / remove-by-passphrase).  This Ubuntu surface
  ships only `rotate_key` (which combines old + new on a single
  stdin payload) because Wave-2.2 deliberately scopes to the
  contract's seven core verbs; the per-slot lifecycle is reserved
  for follow-up.

- **No TPM2 / Clevis bindings.**  The RHEL canonical
  implementation supports `enroll_tpm2`, `wipe_tpm2_slot`,
  `enroll_clevis`, `unbind_clevis`.  Ubuntu's Wave-2.2 surface
  ships passphrase-only.  Coordinators needing
  network-bound-disk-encryption should track the follow-up tasks
  above.

- **No `/etc/crypttab` reader / writer.**  The RHEL canonical
  implementation owns `/etc/crypttab` reads and writes.  Ubuntu's
  Wave-2.2 surface intentionally excludes them so the boundary
  between encryption-management and boot-management remains
  clean — boot-time unlock is a `boot-management` concern even
  when the data lives in `/etc/crypttab`.

- **No spec-equivalence comparison in `enable_encryption`.**  The
  idempotency guard checks "is this device LUKS or not"; it does
  NOT compare the existing cipher / key-size against the spec.
  This means a coordinator that re-runs `enable_encryption` with
  a different cipher gets a SKIPPED rather than a FAILED-or-rotate.
  The behaviour is intentional — re-formatting destroys the
  master key — but coordinators that need the stricter comparison
  should call `get_encryption_status` first.

- **No header-restore verb.**  `backup_recovery_key` writes the
  header out via `luksHeaderBackup`; the symmetrical
  `luksHeaderRestore` is a future enhancement.

- **`requires_reboot` is always False.**  LUKS operations apply
  immediately at the device-mapper layer; the field exists on the
  result dataclass so the shape mirrors the
  `WindowsEncryptionActionResult` sibling for cross-OS
  coordinators.
