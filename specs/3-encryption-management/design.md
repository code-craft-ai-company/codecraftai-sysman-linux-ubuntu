# Design

## Concept Mapping (LUKS / cryptsetup ↔ macOS FileVault ↔ Windows BitLocker)

The cross-OS encryption-management contract talks about *encrypted
volumes*, *passphrases*, and *recovery keys*.  Ubuntu's surface is
LUKS via the `cryptsetup` user-space toolchain.  The table below
grounds the abstract contract in concrete Ubuntu / macOS / Windows
terms so a reader can compare the three Wave-2.2 implementations
side-by-side.

| Contract concept | Ubuntu (this spec) | macOS | Windows |
|---|---|---|---|
| Backend | LUKS (`cryptsetup`, kernel `dm-crypt`) | FileVault for boot; APFS encryption (`diskutil apfs`) for data | BitLocker (PowerShell `BitLocker` module) |
| Volume identity | Block-device path (`/dev/sda3`) plus mapping name (`cryptroot`) | APFS volume path (`/Volumes/Data`) or volume UUID | Drive letter (`C:`) or volume GPT path |
| Format verb | `cryptsetup luksFormat --type luks2 --cipher <c> --key-size <k>` | `fdesetup enable` (boot) or `diskutil apfs encryptVolume` (data) | `Enable-BitLocker -EncryptionMethod XtsAes256 -PasswordProtector` |
| Erase / disable verb | `cryptsetup erase <device>` (DESTRUCTIVE — wipes header) | `fdesetup disable` (boot) or `diskutil apfs decryptVolume` (data) | `Disable-BitLocker -MountPoint <letter>:` |
| Status verb | `cryptsetup luksDump <device>` + `cryptsetup status <name>` | `fdesetup status` (boot) or `diskutil info <volume>` (data) | `Get-BitLockerVolume -MountPoint <letter>:` |
| List verb | `lsblk -J -o NAME,FSTYPE,TYPE,MOUNTPOINT` filtered to `crypto_LUKS` | `diskutil apfs list -plist` filtered to encrypted volumes | `Get-BitLockerVolume \| ConvertTo-Json` |
| Rotate-key verb | `cryptsetup luksChangeKey` (old + new passphrase on stdin) | `fdesetup changerecovery -personal` (boot only) | `Add-BitLockerKeyProtector` then `Remove-BitLockerKeyProtector` (old) |
| Backup recovery key verb | `cryptsetup luksHeaderBackup --header-backup-file <path>` | `fdesetup showrecoverykey` (returns string) | `(Get-BitLockerVolume).KeyProtector` filtered to `RecoveryPassword` then write to path |
| Unlock verb | `cryptsetup luksOpen <device> <name>` | `diskutil apfs unlockVolume <volume>` | `Unlock-BitLocker -MountPoint <letter>: -Password $secure` |
| Passphrase delivery | runner `stdin` (NEVER on argv) | runner `stdin` (NEVER on argv) | inside the PowerShell scriptblock as `ConvertTo-SecureString -AsPlainText -Force '<pw>'`; recorded argv has the literal redacted |
| Destructive guard | `config.allow_destructive` gates `disable_encryption` | `config.allow_destructive` gates `disable_encryption` (data volume `decryptVolume`) | `config.allow_destructive` gates `Disable-BitLocker` |
| Elevation | `sudo cryptsetup ...` for writes; reads need no elevation on most installations | `sudo fdesetup ...` for writes; some `diskutil` reads need admin | UAC token / Administrator session for writes; reads on a domain-joined host can need admin |

The Ubuntu and Windows verbs map cleanly to a single command per
verb.  macOS is the outlier because its boot volume is governed by
`fdesetup` while data volumes are governed by `diskutil apfs`; the
macOS implementation branches on the volume identifier inside each
verb.

---

## Overview

The encryption-management feature is implemented as a single module,
`automojo.sysman.linux.ubuntu.encryptionmanagement`, that exposes an
`UbuntuEncryptionManager` class.  The coordinator instantiates
`UbuntuEncryptionManager` and calls its methods to perform discrete
LUKS actions: enable / disable encryption, query status, list
encrypted volumes, rotate the passphrase, back up the LUKS header,
and unlock a volume.

This feature **complements — and does not duplicate** —
`storage-management` (partition / LVM / RAID),
`filesystem-management` (mkfs / mount / `/etc/fstab`),
`boot-management` (initramfs / GRUB / `crashkernel=`), and
`package-management` (the `cryptsetup` package itself).  The
`UbuntuEncryptionManager` brings LUKS state into the desired shape;
everything outside that verb set lives in another feature.

All command execution is delegated to a command runner implementing
the `ICommandRunner` interface.  The `UbuntuEncryptionManager` never
directly calls `subprocess`, shell commands, or `open()`; it only
constructs argument lists and hands them to the injected runner.
This keeps business logic separate from execution and makes the
feature fully testable via mock dependencies without a live block
device, root access, or a kernel `dm-crypt` module.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via
`SSHCommandRunner`).  Under SSH the coordinator can track
reliability metrics and apply execution aspects (persistence,
retry, timeout) without any changes to the
`UbuntuEncryptionManager` code.

**See Also:**
[Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.  See `3-storage-management` for partitioning and LVM,
`3-filesystem-management` for mkfs / mount, and the cross-OS
contract at
`automojo-sysman-common/specs/contracts/encryption-management/contract.md`.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Supplies passphrase / recovery-key bytes                 │
│  - Sets allow_destructive=True only when destructive intent │
│    is explicit                                              │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuEncryptionManager                          │
│  automojo.sysman.linux.ubuntu.encryptionmanagement          │
│                                                             │
│  enable_encryption()       disable_encryption()             │
│  get_encryption_status()                                    │
│  list_encrypted_volumes()                                   │
│  rotate_key()              backup_recovery_key()            │
│  unlock_volume()                                            │
│                                                             │
│  Internal probes:                                           │
│    cryptsetup isLuks       cryptsetup luksDump              │
│    cryptsetup status       lsblk -J                         │
└─────────┬───────────────────────────────────────────────────┘
          │ ICommandRunner.run(args, elevated=, stdin=<secret>)
          ▼
┌─────────────────────────────────────────────────────────────┐
│               LocalCommandRunner / SSHCommandRunner         │
└─────────┬───────────────────────────────────────────────────┘
          │ optional aspect composition
          ▼
┌─────────────────────────────────────────────────────────────┐
│   PersistenceAspect  RetryAspect  TimeoutAspect             │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `UbuntuEncryptionManager`

**Location:**
`source/packages/automojo/sysman/linux/ubuntu/encryptionmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `enable_encryption` | `(volume, spec) → UbuntuEncryptionActionResult` | `cryptsetup luksFormat ... <device>` followed by `cryptsetup luksOpen <device> <name>`; passphrase via stdin; idempotent via `cryptsetup isLuks` |
| `disable_encryption` | `(volume) → UbuntuEncryptionActionResult` | `cryptsetup erase <device>`; DESTRUCTIVE; gated by `config.allow_destructive`; idempotent via `cryptsetup isLuks` |
| `get_encryption_status` | `(volume) → UbuntuEncryptionStatus` | merges `cryptsetup luksDump` and `cryptsetup status` output |
| `list_encrypted_volumes` | `() → list[UbuntuEncryptedVolumeInfo]` | `lsblk -J ...` filtered to `crypto_LUKS`; per-row `luksDump` |
| `rotate_key` | `(volume, old_passphrase, new_passphrase) → UbuntuEncryptionActionResult` | `cryptsetup luksChangeKey <device>`; both passphrases on a single stdin payload separated by `\n`; unconditional |
| `backup_recovery_key` | `(volume, path) → UbuntuEncryptionActionResult` | `cryptsetup luksHeaderBackup <device> --header-backup-file <path>`; unconditional |
| `unlock_volume` | `(volume, passphrase) → UbuntuEncryptionActionResult` | `cryptsetup luksOpen <device> <name>`; passphrase via stdin; idempotent via `cryptsetup status` |

### `UbuntuEncryptionManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no command is dispatched; the would-have-been argv is recorded |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `allow_destructive` | `bool` | `False` | When `True`, `disable_encryption` is permitted to dispatch `cryptsetup erase` |
| `default_cipher` | `str` | `"aes-xts-plain64:sha256"` | Default `--cipher` token passed to `luksFormat` |
| `default_key_size` | `int` | `512` | Default `--key-size` (in BITS) passed to `luksFormat` |

---

## Data Models

### Result and status types

```python
class UbuntuEncryptionActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class UbuntuEncryptionActionResult:
    status: UbuntuEncryptionActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None              # SANITISED — never carries a passphrase
    volume: str | None
    device: str | None
    requires_reboot: bool                  # always False for LUKS
```

### Spec / status / list types

```python
@dataclass
class UbuntuEncryptionSpec:
    cipher: str | None = None              # falls back to config.default_cipher
    key_size: int | None = None            # falls back to config.default_key_size
    passphrase: str | None = None          # required for enable_encryption
    recovery_key_protector: bool = True
    mount_point: str | None = None         # informational; this feature does NOT mount

@dataclass
class UbuntuEncryptionStatus:
    volume: str
    is_encrypted: bool
    is_unlocked: bool
    cipher: str | None
    key_size: int | None
    progress_percent: float | None         # reserved for future reformat / reencrypt operations
    protector_types: list[str]             # one entry per enabled key slot ("slot-0", "slot-1", ...)

@dataclass
class UbuntuEncryptedVolumeInfo:
    volume: str
    device: str
    mount_point: str | None
    is_encrypted: bool
    is_unlocked: bool
    cipher: str | None
    key_size: int | None
    progress_percent: float | None
    protector_types: list[str]
```

---

## Key Method Designs

### `enable_encryption(volume, spec)`

1. Resolve `device_path` and `mapping_name` from `volume`.
2. Build the `luksFormat` argv carrying `--batch-mode`, `--type
   luks2`, `--cipher` (from spec or config default), `--key-size`
   (from spec or config default), and the device path.
3. If `dry_run`, return `DRY_RUN` carrying the format argv.
4. If `spec.passphrase is None`, return
   `FAILED(detail="passphrase_required")` without dispatching.
5. Probe `cryptsetup isLuks <device_path>`.  When the probe exits
   0 (the device is already LUKS), return `SKIPPED` with a
   descriptive detail.  This is the safety rail.
6. Dispatch the format command with `stdin=spec.passphrase`,
   `elevated=config.elevated`.
7. If the format failed, return the FAILED result.
8. Build the `luksOpen <device> <name>` argv and dispatch it with
   the same passphrase on stdin.  The result of the open call is
   what the manager returns to the caller.

### `disable_encryption(volume)`

1. Resolve `device_path` and `mapping_name`.
2. Build the `cryptsetup erase <device>` argv.
3. If `dry_run`, return `DRY_RUN`.
4. Probe `cryptsetup isLuks`.  If False, return `SKIPPED` (the
   device is already plaintext).
5. If `config.allow_destructive` is False, return
   `FAILED(detail=_DESTRUCTIVE_GUARD_DETAIL)` WITHOUT dispatching.
   The guard is the safety rail.
6. Dispatch the `erase` command with `stdin="YES\n"` (cryptsetup's
   confirmation prompt) and `elevated=config.elevated`.

### `get_encryption_status(volume)`

1. Resolve device path and mapping name.
2. Run `cryptsetup luksDump <device_path>`.  Pure-Python parser
   extracts `cipher`, `key_size`, and per-slot `slot-N` strings.
3. Run `cryptsetup status <mapping_name>`.  Pure-Python parser
   extracts the `active` / `cipher` / `device` fields.
4. Merge into an `UbuntuEncryptionStatus`.  When the dump probe
   fails, `is_encrypted=False` and `cipher=None`.
5. NEVER pass `elevated=True` for this read.

### `list_encrypted_volumes()`

1. Run `lsblk -J -o NAME,FSTYPE,TYPE,MOUNTPOINT`.
2. Pure-Python flatten of the `blockdevices` tree.
3. Filter to rows where `fstype == "crypto_LUKS"`.
4. For each surviving row, run `cryptsetup luksDump` to populate
   `cipher` / `key_size` / `protector_types`.
5. Detect the active mapping by inspecting the row's immediate
   children; a child of type `crypt` is the mapping name.  When
   present the row's `volume` field carries the mapping name and
   the `is_unlocked` field is `True`.

### `rotate_key(volume, old, new)`

1. Build the `cryptsetup luksChangeKey <device>` argv.
2. If `dry_run`, return `DRY_RUN`.
3. Build the combined stdin payload as `f"{old}\n{new}"`.  This
   matches `cryptsetup`'s wire protocol — the tool prompts for the
   existing key, then for the replacement.
4. Dispatch the command.  No idempotency guard (unconditional).

### `backup_recovery_key(volume, path)`

1. Build the `cryptsetup luksHeaderBackup <device>
   --header-backup-file <path>` argv.
2. If `dry_run`, return `DRY_RUN`.
3. Dispatch the command with no stdin.  No idempotency guard
   (unconditional).

### `unlock_volume(volume, passphrase)`

1. Build the `cryptsetup luksOpen <device> <name>` argv.
2. If `dry_run`, return `DRY_RUN`.
3. Probe `cryptsetup status <mapping_name>`.  When the parsed
   record shows `active=True`, return `SKIPPED`.
4. Dispatch the command with `stdin=passphrase`.

---

## Passphrase / Recovery-Key Handling

Passphrase bytes flow through the runner's `stdin` parameter; they
NEVER appear on argv.  Every `cryptsetup` verb that needs a
passphrase accepts it on standard input (a long-standing
`cryptsetup` design decision, exposed via `--key-file=-` and the
default no-flag prompt).  The manager exploits this by passing the
literal passphrase string as the runner's `stdin` argument:

```python
self._runner.run(
    ["cryptsetup", "luksOpen", "/dev/sda3", "cryptroot"],
    elevated=True,
    stdin=passphrase,          # <- passphrase here
)
```

The single argv-style exception is `cryptsetup luksChangeKey`,
which prompts for BOTH the existing and the replacement passphrase
on the SAME stdin stream.  The manager joins them with a single
newline and passes the combined buffer:

```python
combined = f"{old_passphrase}\n{new_passphrase}"
self._runner.run(
    ["cryptsetup", "luksChangeKey", "/dev/sda3"],
    elevated=True,
    stdin=combined,
)
```

The `command` field on every returned
`UbuntuEncryptionActionResult` contains only the argv that was
actually dispatched.  Because the passphrase never appears on argv,
the field is automatically free of secrets — no post-hoc
sanitisation step is required.  The corresponding macOS sibling
follows the same pattern.  The Windows sibling — which builds a
PowerShell scriptblock that embeds the passphrase as a `''`-quoted
literal inside the script — handles the same problem differently:
the recorded argv passes through a regex-based sanitisation step
that replaces every `ConvertTo-SecureString -AsPlainText -Force
'<pw>'` literal with a redacted sentinel before the argv is captured
on the result.

---

## Destructive Operation Guard

`disable_encryption` is destructive: `cryptsetup erase` overwrites
every key slot in the LUKS header, leaving the underlying ciphertext
unrecoverable.  The manager refuses to dispatch the `erase` command
unless `config.allow_destructive=True`; otherwise it returns
`FAILED` carrying `detail="destructive_operation_requires_explicit_confirm"`.

The guard mirrors the Wave-2.1 pattern in
`UbuntuStorageManager.delete_pool` / `delete_volume`, which also
require an explicit destructive flag.  Coordinators that drive a
"factory-reset" workflow are expected to set the flag in their own
config layer; the default-False guard prevents accidental data loss
during routine reconfiguration.

---

## Dry-Run Behaviour

Dry-run disables: `enable_encryption`, `disable_encryption`,
`rotate_key`, `backup_recovery_key`, `unlock_volume`.

Dry-run allows: `get_encryption_status`, `list_encrypted_volumes`
(read-only).

Dry-run results carry the would-have-been argv on `command`, the
relevant `volume` and `device` fields, and `status=DRY_RUN`.  No
runner call is made.

---

## Safety and Correctness Properties

1. **No passphrase on argv.** Every passphrase-accepting verb
   supplies the passphrase via `stdin`.  The
   `UbuntuEncryptionActionResult.command` field preserves this
   invariant.
2. **Idempotent format.** `enable_encryption` refuses to run
   `luksFormat` against an already-formatted device; prevents
   accidental data loss.
3. **Destructive guard.** `disable_encryption` requires
   `config.allow_destructive=True`; without the flag the manager
   returns FAILED without invoking `cryptsetup erase`.
4. **Idempotent unlock.** `unlock_volume` returns SKIPPED when the
   mapping is already active.
5. **Pure-Python parsing.** The `luksDump`, `cryptsetup status`,
   and `lsblk -J` parsers run in pure Python; no shell pipes are
   inserted between the runner and the parser.
6. **Read-only reads.** `get_encryption_status` and
   `list_encrypted_volumes` never dispatch a write.  They never
   pass `elevated=True`.

---

## Testing Strategy

- `_MockCommandRunner` records every `run` call (args + elevation
  flag + stdin payload) and answers from a pre-seeded `results`
  dict keyed by argv tuple.
- Per-verb tests for command construction, idempotency (SKIPPED),
  dry-run (`DRY_RUN`), and FAILED-on-non-zero.
- Passphrase-hygiene test that scans every recorded argv and
  asserts no plaintext passphrase appears.
- Elevation tests that assert writes pass `elevated=True` (per
  config) and reads do not.
- Module-level parser tests for `luksDump`, `cryptsetup status`,
  and `lsblk -J`.
- Destructive-guard test that asserts
  `disable_encryption(allow_destructive=False)` returns FAILED
  without dispatching `cryptsetup erase`.
- No live block device required.

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/
└── encryptionmanagement.py

source/testroots/testbase/
└── automojo/tests/sysman/linux/ubuntu/
    └── test_encryptionmanagement.py
```

---

## Seams With Other Features

| Seam | Owner | This Feature |
|---|---|---|
| Partitioning / LVM / RAID | `3-storage-management` | Coordinator runs storage first; this feature LUKS-formats the partition; coordinator runs storage again for PV/VG/LV on the mapped device |
| `mkfs` / `/etc/fstab` mounts | `3-filesystem-management` | Coordinator mkfs-es and mounts `/dev/mapper/<name>` after this feature opens it |
| Boot-time unlock (`/etc/crypttab`, initramfs) | `3-boot-management` (extension) | This feature does NOT touch `/etc/crypttab`; coordinators that need boot-time unlock wire it from a boot-management extension |
| TPM2 / Clevis (NBDE) | RHEL canonical (`3-encryption-management`) | Reserved as Ubuntu extension; the Ubuntu Wave-2.2 surface ships passphrase-only |
| Package install (`cryptsetup`) | `3-package-management` | This feature assumes the binary is on PATH; missing binary surfaces as FAILED on first call |
| Credential / passphrase routing | `automojo-credentials` | Caller passes the passphrase string into the verbs; bytes flow on `stdin` to the runner |
