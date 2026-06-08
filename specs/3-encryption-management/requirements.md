# Requirements

## Introduction

The encryption management feature provides the
`codecraftai-sysman-linux-ubuntu` package with the ability to enable /
disable LUKS encryption on a block device, query encryption status,
list encrypted volumes, rotate passphrases, back up the LUKS header
(the "recovery key"), and unlock an encrypted volume by attaching it
to a `/dev/mapper/<name>` mapping.  This spec implements the
encryption-management contract defined at
`codecraftai-sysman-common/specs/contracts/encryption-management/contract.md`
for Ubuntu by wrapping the `cryptsetup` user-space toolchain plus
`lsblk` for volume enumeration.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *which* devices should be encrypted, *which*
passphrases should be used, or *whether* a recovery-key backup should
be archived to off-host storage — it provides the capability to
*manipulate LUKS state* reliably, idempotently, and with full
auditability.  All `cryptsetup` and `lsblk` commands are executed
exclusively through the `codecraftai-interfaces` command-runner
abstraction; passphrase bytes flow on runner `stdin` and never appear
on argv.

Filesystem creation, mounting, and `/etc/crypttab` boot-time wiring
are NOT in scope here.  Filesystem-level concerns live in
`filesystem-management`; partition / LVM concerns live in
`storage-management`.  Hardware key escrow (TPM2 enrolment via
`systemd-cryptenroll`, network-bound disk encryption via `clevis`) is
also deferred — they are intentionally available on the RHEL canonical
implementation and reserved as Ubuntu extensions.

---

## Requirements

### Requirement 1: Enable LUKS Encryption on a Volume

**User Story:** As a maintenance coordinator, I want to encrypt a
block device with LUKS by passing a passphrase, so that the device's
contents are protected at rest.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_encryption(volume, spec)` THE
   SYSTEM SHALL dispatch
   `cryptsetup luksFormat --batch-mode --type luks2 --cipher <cipher>
   --key-size <bits> <device>` through the command runner with
   `elevated=True` when configured, with the passphrase delivered via
   runner `stdin`.
2. WHEN the format succeeds THE SYSTEM SHALL immediately follow with
   `cryptsetup luksOpen <device> <name>` so the resulting plaintext
   mapping is available to the caller without an extra round-trip.
3. WHEN the device is already a LUKS device (probed via
   `cryptsetup isLuks <device>`) THE SYSTEM SHALL skip execution and
   return `UbuntuEncryptionActionResult(status=SKIPPED)`.  This is
   the safety rail that prevents accidental data loss; an existing
   LUKS header MUST NOT be overwritten by an idempotent re-run.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuEncryptionActionResult(status=DRY_RUN)` carrying the
   would-have-been argv.
5. WHEN `spec.passphrase` is None THE SYSTEM SHALL return
   `UbuntuEncryptionActionResult(status=FAILED)` carrying
   `detail="passphrase_required"` and SHALL NOT dispatch any
   command.
6. THE SYSTEM SHALL NEVER include the passphrase value in the
   recorded `command` field on the returned result.

---

### Requirement 2: Disable LUKS Encryption (Destructive)

**User Story:** As a maintenance coordinator, I want a guarded path
to disable encryption on a device, so that decommissioning is
auditable and accidental destruction is impossible.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_encryption(volume)` THE
   SYSTEM SHALL dispatch `cryptsetup erase <device>` through the
   command runner with `elevated=True` when configured.
2. WHEN `config.allow_destructive` is False THE SYSTEM SHALL NOT
   dispatch the erase command and SHALL return
   `UbuntuEncryptionActionResult(status=FAILED,
   detail="destructive_operation_requires_explicit_confirm")`.
3. WHEN the device is not currently a LUKS device (probed via
   `cryptsetup isLuks`) THE SYSTEM SHALL skip execution and return
   `UbuntuEncryptionActionResult(status=SKIPPED)`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuEncryptionActionResult(status=DRY_RUN)`.
5. WHEN the operation succeeds THE SYSTEM SHALL return
   `UbuntuEncryptionActionResult(status=SUCCESS)`.
6. WHEN the operation fails THE SYSTEM SHALL return
   `UbuntuEncryptionActionResult(status=FAILED)` carrying the
   runner's exit code, stdout, and stderr.

---

### Requirement 3: Get Encryption Status for a Volume

**User Story:** As a maintenance coordinator, I want to query the
encryption status of a single device or active mapping, so that I
can branch on cipher / key size / unlock-state without parsing
`cryptsetup` output myself.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_encryption_status(volume)` THE
   SYSTEM SHALL dispatch BOTH `cryptsetup luksDump <device>` and
   `cryptsetup status <name>` through the command runner WITHOUT
   elevation.
2. THE SYSTEM SHALL return a `UbuntuEncryptionStatus` populated with
   `is_encrypted` (True when the LUKS header is present),
   `is_unlocked` (True when an active `/dev/mapper/<name>` mapping
   exists), `cipher`, `key_size`, and `protector_types` (one entry
   per enabled key slot).
3. WHEN the device is not a LUKS device THE SYSTEM SHALL return a
   record with `is_encrypted=False` and `cipher=None`.
4. THE SYSTEM SHALL NEVER pass `elevated=True` to the runner for
   this verb.

---

### Requirement 4: List Encrypted Volumes

**User Story:** As a maintenance coordinator, I want to enumerate
every encrypted volume on the host, so that I can compare actual
encryption state against the declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_encrypted_volumes()` THE SYSTEM
   SHALL dispatch `lsblk -J -o NAME,FSTYPE,TYPE,MOUNTPOINT` through
   the command runner WITHOUT elevation.
2. THE SYSTEM SHALL filter the response to rows where
   `FSTYPE=crypto_LUKS` and emit one `UbuntuEncryptedVolumeInfo`
   per matching device.
3. THE SYSTEM SHALL pull the LUKS header for every matched device
   via `cryptsetup luksDump` to populate `cipher`, `key_size`, and
   `protector_types`.
4. WHEN an encrypted device has an active `/dev/mapper/<name>`
   child of type `crypt` THE SYSTEM SHALL set `is_unlocked=True`
   and copy the child's `mountpoint` field onto the parent's
   `mount_point` field.
5. WHEN the engine exits non-zero THE SYSTEM SHALL return an empty
   list rather than raise.

---

### Requirement 5: Rotate the LUKS Passphrase

**User Story:** As a maintenance coordinator, I want to rotate the
LUKS passphrase on a device, so that compromised credentials can be
replaced.

#### Acceptance Criteria

1. WHEN the coordinator calls `rotate_key(volume, old_passphrase,
   new_passphrase)` THE SYSTEM SHALL dispatch
   `cryptsetup luksChangeKey <device>` through the command runner
   with `elevated=True` when configured.
2. THE SYSTEM SHALL deliver the old passphrase followed by a single
   newline followed by the new passphrase on a single ``stdin``
   payload, matching `cryptsetup`'s wire protocol for
   `luksChangeKey`.
3. THE SYSTEM SHALL NEVER include either passphrase value in the
   recorded `command` field on the returned result.
4. THE SYSTEM SHALL NOT skip rotation — rotation is always
   dispatched (the new passphrase is opaque to the manager).
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuEncryptionActionResult(status=DRY_RUN)`.

---

### Requirement 6: Back Up the LUKS Header (Recovery Key)

**User Story:** As a maintenance coordinator, I want to back up the
LUKS header to a filesystem path, so that a copy is available for
header-restore in the event of accidental destruction.

#### Acceptance Criteria

1. WHEN the coordinator calls `backup_recovery_key(volume, path)`
   THE SYSTEM SHALL dispatch
   `cryptsetup luksHeaderBackup <device>
   --header-backup-file <path>` through the command runner with
   `elevated=True` when configured.
2. THE SYSTEM SHALL pass NO `stdin` payload (the operation does not
   require a passphrase).
3. THE SYSTEM SHALL NOT skip — backup is always dispatched.
4. THE SYSTEM SHALL NOT create the parent directory of `path`;
   coordinators are expected to ensure the path is writable.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuEncryptionActionResult(status=DRY_RUN)`.

---

### Requirement 7: Unlock an Encrypted Volume

**User Story:** As a maintenance coordinator, I want to attach a
LUKS device to a `/dev/mapper/<name>` mapping by passing a
passphrase, so that the plaintext block device becomes available for
mounting.

#### Acceptance Criteria

1. WHEN the coordinator calls `unlock_volume(volume, passphrase)`
   THE SYSTEM SHALL dispatch
   `cryptsetup luksOpen <device> <name>` through the command runner
   with `elevated=True` when configured, with the passphrase
   delivered via runner `stdin`.
2. WHEN the mapping is already active (probed via
   `cryptsetup status <name>`) THE SYSTEM SHALL skip execution and
   return `UbuntuEncryptionActionResult(status=SKIPPED)`.
3. THE SYSTEM SHALL NEVER include the passphrase value in the
   recorded `command` field on the returned result.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuEncryptionActionResult(status=DRY_RUN)`.

---

### Requirement 8: Elevation Policy

**User Story:** As a maintenance coordinator, I want every write
verb to honour the configured elevation flag without having to
re-pass it to each call, so that infrastructure code does not
duplicate `sudo`-style routing.

#### Acceptance Criteria

1. WHEN `config.elevated` is True THE SYSTEM SHALL pass
   `elevated=True` to the runner for every write verb
   (`enable_encryption`, `disable_encryption`, `rotate_key`,
   `backup_recovery_key`, `unlock_volume`).
2. WHEN performing read-only operations
   (`get_encryption_status`, `list_encrypted_volumes`, all
   idempotency probes) THE SYSTEM SHALL NEVER pass `elevated=True`.
3. WHEN `config.elevated` is False THE SYSTEM SHALL NEVER pass
   `elevated=True`.

---

### Requirement 9: Passphrase Hygiene on Argv

**User Story:** As a security-conscious operator, I want to be
certain that no passphrase byte ever appears in a recorded argv,
log line, or audit capture from this manager, so that secrets do not
leak through ordinary observability.

#### Acceptance Criteria

1. THE SYSTEM SHALL pass every passphrase via the runner's `stdin`
   parameter.
2. THE SYSTEM SHALL NEVER include a passphrase value as a positional
   argument on the `cryptsetup` argv.
3. THE SYSTEM SHALL NEVER include a passphrase value as the value
   of a `cryptsetup` flag.
4. THE SYSTEM SHALL NEVER include a passphrase value in the
   `command` field on any returned `UbuntuEncryptionActionResult`.

---

### Requirement 10: Destructive Operation Guard

**User Story:** As a security-conscious operator, I want destructive
encryption operations to be opt-in, so that an accidental call to a
destructive verb cannot wipe a key slot.

#### Acceptance Criteria

1. THE SYSTEM SHALL treat `disable_encryption` as a destructive
   verb because `cryptsetup erase` removes every key slot from the
   header.
2. WHEN `config.allow_destructive` is False AND the destructive
   verb is invoked AND the device is currently encrypted THE
   SYSTEM SHALL return
   `UbuntuEncryptionActionResult(status=FAILED,
   detail="destructive_operation_requires_explicit_confirm")`
   without dispatching the destructive command.
3. WHEN `config.allow_destructive` is True THE SYSTEM SHALL
   dispatch the destructive command after verifying the
   idempotency guard.

---

### Requirement 11: Observability

**User Story:** As a maintenance coordinator, I want the manager's
result objects to carry enough metadata to drive cross-OS reporting
without OS-specific branching, so that operator-facing dashboards
remain shape-stable.

#### Acceptance Criteria

1. THE SYSTEM SHALL populate every `UbuntuEncryptionActionResult`
   with `volume`, `device`, `command`, and `status`.
2. THE SYSTEM SHALL set `requires_reboot=False` on every result —
   `cryptsetup` operations apply immediately at the device-mapper
   layer.  The field exists on the dataclass so the shape mirrors
   the `WindowsEncryptionActionResult` sibling.
3. THE SYSTEM SHALL surface the runner's exit code, stdout, and
   stderr on every result whose status is `SUCCESS` or `FAILED`.
