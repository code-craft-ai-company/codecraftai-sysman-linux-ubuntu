# Requirements

## Introduction

The bootloader-management feature provides the
`automojo-sysman-linux-ubuntu` package with the ability to manage the
**OS-level bootloader configuration layer** on Ubuntu / Debian-family
hosts.  It implements the cross-OS contract defined at
`automojo-sysman-common/specs/contracts/bootloader-management/contract.md`.

### What this feature is NOT

This feature is **distinct from `boot-management`** (the UEFI
firmware boot-entry feature in `automojo-sysman-uefi`).  The two
operate on different layers:

| Feature | Layer | Tools |
|---------|-------|-------|
| `boot-management` (UEFI) | Firmware boot **entries** (NVRAM) | `efibootmgr`, `bcdedit /enum firmware` |
| `bootloader-management` (this) | OS-level bootloader **config** | `update-grub`, `grub-mkconfig`, `bootctl` |

The firmware layer decides which loader to launch.  The OS-level
bootloader layer (this feature) decides which kernel that loader
launches.  Misconfiguring this layer can render a host unbootable
even when the firmware boot entry is correct.

### Backend selection

Ubuntu hosts ship with one of two OS-level bootloaders:

1. **GRUB 2** (the default).  Configured by `/etc/default/grub`,
   drop-ins under `/etc/grub.d/`, and the rendered
   `/boot/grub/grub.cfg`.  Driven by `update-grub`,
   `grub-mkconfig`, and `grub-set-default`.
2. **systemd-boot** (opt-in on UEFI hosts).  Configured by
   loader-conf files under `/boot/loader/entries/`.  Driven by
   `bootctl`.

`UbuntuBootloaderManagerConfig.backend` selects the active backend.
The default is `UbuntuBootloaderBackend.GRUB2`.  Every verb dispatches
to the GRUB2 plumbing or the systemd-boot plumbing based on this
field; there is no auto-detection.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interface.  The
feature does not decide *whether* a host should have a particular
boot entry as its default — it provides the capability to *do it*
reliably, idempotently, and with full auditability.  All system
commands (`update-grub`, `grub-mkconfig`, `bootctl`, `cat`, `tee`,
`rm`) and all reads / writes of GRUB config files flow through the
`automojo-interfaces` command-runner abstraction.  All mutating
operations respect dry-run mode.

The kernel command-line modification verbs (`add_kernel_param`,
`remove_kernel_param`) are OUT OF SCOPE for this spec and remain
delegated to the `kernel-parameters` feature.

---

## Requirements

### Requirement 1: List Boot Entries

**User Story:** As a maintenance coordinator, I want to enumerate the
boot entries known to the OS-level bootloader so that I can verify
what the host is offering at boot time.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_entries()` AND `config.backend` is
   `GRUB2` THE SYSTEM SHALL `cat /boot/grub/grub.cfg` via the runner
   and SHALL return one `UbuntuBootEntryInfo` per `menuentry`
   directive.
2. WHEN a `menuentry` lives inside a `submenu` block THE SYSTEM SHALL
   prefix the entry's `id` with the submenu titles joined by `>`
   (matching the format that `GRUB_DEFAULT=` accepts).
3. WHEN `config.backend` is `SYSTEMD_BOOT` THE SYSTEM SHALL invoke
   `bootctl list` via the runner and SHALL parse the resulting
   stanzas.
4. THE SYSTEM SHALL mark exactly one entry's `is_default` field as
   `True` when a default is resolvable.
5. THE SYSTEM SHALL never pass `elevated=True` for any of these
   reads.
6. WHEN `cat /boot/grub/grub.cfg` fails with a "no such file" error
   THE SYSTEM SHALL return an empty list.
7. WHEN the underlying read fails for any other reason THE SYSTEM
   SHALL raise `UbuntuBootloaderQueryError`.

---

### Requirement 2: Get Default Entry

**User Story:** As a maintenance coordinator, I want to know which
boot entry is currently the active default so that I can compare it
to the declared maintenance baseline.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_default_entry()` AND
   `config.backend` is `GRUB2` THE SYSTEM SHALL parse
   `/etc/default/grub` for the `GRUB_DEFAULT` token and SHALL resolve
   it against the parsed `grub.cfg` entry list.
2. THE SYSTEM SHALL accept three `GRUB_DEFAULT` token forms: numeric
   index (e.g. `"0"`), submenu-qualified id (e.g.
   `"Advanced options for Ubuntu>Ubuntu, with Linux 6.8.0"`), and
   bare entry name.
3. WHEN `config.backend` is `SYSTEMD_BOOT` THE SYSTEM SHALL invoke
   `bootctl status` via the runner and SHALL parse the
   `Default Boot Loader Entry:` line for the loader-conf basename.
4. WHEN no default can be resolved THE SYSTEM SHALL raise
   `UbuntuBootloaderQueryError`.
5. THE SYSTEM SHALL never pass `elevated=True` for any of these
   reads.

---

### Requirement 3: Set Default Entry

**User Story:** As a maintenance coordinator, I want to change the
active default boot entry so that the next reboot lands on the
declared kernel.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_default_entry(entry_id=...)` AND
   `config.backend` is `GRUB2` THE SYSTEM SHALL rewrite
   `GRUB_DEFAULT="<entry_id>"` in `/etc/default/grub` via
   `cat`/`tee`-through-runner THEN SHALL invoke `update-grub` with
   elevated privileges.
2. WHEN the existing `GRUB_DEFAULT` token already matches the
   requested `entry_id` THE SYSTEM SHALL return a
   `UbuntuBootloaderActionResult` with status `SKIPPED` and SHALL NOT
   issue the rewrite or the `update-grub` call.
3. WHEN `config.backend` is `SYSTEMD_BOOT` THE SYSTEM SHALL invoke
   `bootctl set-default <entry_id>`, idempotency-skipped when
   `bootctl status` already reports the requested entry as the
   default.
4. WHEN the rewrite or the `update-grub`/`bootctl` call fails THE
   SYSTEM SHALL return `UbuntuBootloaderActionResult(status=FAILED,
   exit_code=<code>, stderr=<captured>)`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return `DRY_RUN`.

---

### Requirement 4: Add Boot Entry

**User Story:** As a maintenance coordinator, I want to register a
new boot entry so that operators can offer a custom kernel /
initramfs / cmdline at boot time.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_entry(spec=UbuntuBootEntrySpec(...))`
   AND `config.backend` is `GRUB2` THE SYSTEM SHALL append a sentinel-
   bracketed `menuentry` block to `/etc/grub.d/40_custom` THEN SHALL
   invoke `update-grub` with elevated privileges.
2. THE SYSTEM SHALL bracket each appended block with comment
   sentinels of the form
   `# automojo-bootloader-management:begin <id>` and
   `# automojo-bootloader-management:end <id>` so that
   `remove_entry` can excise it without touching operator-authored
   blocks.
3. WHEN `config.backend` is `SYSTEMD_BOOT` THE SYSTEM SHALL render a
   loader-conf file body (`title`, `linux`, `initrd`, `options`) and
   SHALL `tee` it to `/boot/loader/entries/<id>.conf`.
4. WHEN an entry with the same `id` is already present in
   `/etc/grub.d/40_custom` (GRUB2) or
   `/boot/loader/entries/<id>.conf` (systemd-boot) AND the rendered
   body matches the existing body THE SYSTEM SHALL return `SKIPPED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return `DRY_RUN`.

---

### Requirement 5: Remove Boot Entry

**User Story:** As a maintenance coordinator, I want to remove a
previously registered boot entry so that operators no longer see it
at boot time.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_entry(entry_id=...)` AND
   `config.backend` is `GRUB2` THE SYSTEM SHALL excise the sentinel-
   bracketed block from `/etc/grub.d/40_custom` THEN SHALL invoke
   `update-grub` with elevated privileges.
2. WHEN no matching block is present THE SYSTEM SHALL return
   `SKIPPED` and SHALL NOT issue the rewrite or the `update-grub`
   call.
3. WHEN `config.backend` is `SYSTEMD_BOOT` THE SYSTEM SHALL invoke
   `rm -f /boot/loader/entries/<id>.conf` via the runner with
   elevated privileges.
4. WHEN the loader-conf file is absent THE SYSTEM SHALL return
   `SKIPPED` and SHALL NOT issue the `rm` call.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command.

---

### Requirement 6: Update Bootloader Config

**User Story:** As a maintenance coordinator, I want a single verb
that regenerates AND applies the bootloader configuration so that
operators do not need to remember which apply step the active
backend requires.

#### Acceptance Criteria

1. WHEN the coordinator calls `update_bootloader_config()` AND
   `config.backend` is `GRUB2` THE SYSTEM SHALL invoke
   `update-grub` (the Debian-family alias for `grub-mkconfig -o
   /boot/grub/grub.cfg`) with elevated privileges.
2. WHEN `config.backend` is `SYSTEMD_BOOT` THE SYSTEM SHALL invoke
   `bootctl update` with elevated privileges.
3. THE SYSTEM SHALL return `UbuntuBootloaderActionResult(status=SUCCESS,
   requires_reboot=True)` on success.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command.

---

### Requirement 7: Regenerate Config

**User Story:** As a maintenance coordinator, I want to rebuild the
bootloader configuration without applying it so that I can diff the
proposed config against the current one before committing.

#### Acceptance Criteria

1. WHEN the coordinator calls `regenerate_config()` AND
   `config.backend` is `GRUB2` THE SYSTEM SHALL invoke
   `grub-mkconfig -o /boot/grub/grub.cfg` with elevated privileges.
2. WHEN `config.backend` is `SYSTEMD_BOOT` THE SYSTEM SHALL invoke
   `bootctl install` (the idempotent rebuild step) with elevated
   privileges.
3. THE SYSTEM SHALL return `UbuntuBootloaderActionResult(status=SUCCESS,
   requires_reboot=True)` on success.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command.

---

### Requirement 8: Idempotency Guards

**User Story:** As a maintenance coordinator, I want repeat calls to
the same write verb to be no-ops when the host already matches the
desired state, so that maintenance runs are safe to re-execute.

#### Acceptance Criteria

1. `set_default_entry()` SHALL skip when the current default
   matches the requested entry id.
2. `add_entry()` SHALL skip when an entry with the same id is
   already present with a matching rendered body.
3. `remove_entry()` SHALL skip when no entry with the supplied id is
   present.
4. `update_bootloader_config()` and `regenerate_config()` SHALL
   execute unconditionally — the underlying tools are themselves
   idempotent.

---

### Requirement 9: Elevation Policy

**User Story:** As a maintenance coordinator, I want writes to elevate
when configured but reads to never elevate, so that the manager works
on hosts that do not run as root.

#### Acceptance Criteria

1. WHEN `config.elevated=True` AND the verb is a write
   (`set_default_entry`, `add_entry`, `remove_entry`,
   `update_bootloader_config`, `regenerate_config`) THE SYSTEM SHALL
   pass `elevated=True` to the runner for both the file-write and
   the `update-grub` / `bootctl` call.
2. WHEN `config.elevated=False` THE SYSTEM SHALL never pass
   `elevated=True`.
3. WHEN the verb is a read (`list_entries`, `get_default_entry`)
   THE SYSTEM SHALL never pass `elevated=True`, regardless of the
   config.
4. Idempotency probes (the `cat` of `/etc/default/grub` /
   `/etc/grub.d/40_custom` / `grub.cfg`, `bootctl status`) MUST never
   elevate.

---

### Requirement 10: Dry-Run Safety

**User Story:** As a maintenance coordinator, I want a dry-run mode
that exercises the planning logic without touching the host, so that
maintenance plans can be reviewed before execution.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` AND a write verb is invoked THE SYSTEM
   SHALL NOT call `runner.run` for the mutating step.
2. THE SYSTEM SHALL still return a `UbuntuBootloaderActionResult`
   with status `DRY_RUN` and the `command` field populated with the
   argv that would have been executed.
3. THE SYSTEM SHALL NEVER write to `/etc/default/grub`,
   `/etc/grub.d/40_custom`, or `/boot/loader/entries/` in dry-run
   mode.
4. Read verbs SHALL execute unchanged in dry-run mode — reads do not
   mutate.

---

### Requirement 11: Distinction From UEFI Boot-Management

**User Story:** As a maintenance coordinator, I want the
bootloader-management feature to never accidentally touch the UEFI
firmware NVRAM so that the firmware boot entries remain owned by
`automojo-sysman-uefi`.

#### Acceptance Criteria

1. THE SYSTEM SHALL NEVER invoke `efibootmgr`.
2. THE SYSTEM SHALL NEVER read or write
   `/sys/firmware/efi/efivars/`.
3. THE SYSTEM SHALL document the distinction in the design.md and
   the module docstring so that downstream readers do not confuse
   the two layers.

---

### Requirement 12: Destructive-Change Safety

**User Story:** As a maintenance coordinator, I want clear signals
when a write changes the bootloader so that I can pair the change
with a backup-and-verify workflow.

#### Acceptance Criteria

1. THE SYSTEM SHALL set `requires_reboot=True` on every successful
   write result.
2. THE SYSTEM SHALL surface the underlying `update-grub` /
   `grub-mkconfig` / `bootctl` stderr verbatim on `FAILED` results
   so that operators can triage misconfigurations.
3. THE SYSTEM SHALL document in the module docstring that
   misconfigurations can render the host unbootable and that
   callers SHOULD pair every write with a backup of `grub.cfg` or
   `/boot/loader/entries/`.
