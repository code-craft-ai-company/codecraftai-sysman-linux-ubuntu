# Requirements

## Introduction

The samba-server-management feature provides the
`automojo-sysman-linux-ubuntu` package with the ability to manage a
local Samba (SMB/CIFS) file server on Ubuntu hosts.  Ubuntu's stock
Samba implementation ships in the `samba` package; the daemon is
exposed under systemd as `smbd.service`.  This spec implements the
cross-OS samba-server-management contract defined at
`automojo-sysman-common/specs/contracts/samba-server-management/contract.md`
for Ubuntu, scoped to the `smbd` daemon and the
`/etc/samba/smb.conf` configuration surface.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *what* share policy the server enforces — it
provides the capability to *apply* policy reliably, idempotently,
and with full auditability.  All system commands (`smbcontrol`,
`smbpasswd`, `pdbedit`, `cat`, `tee`) are executed exclusively
through the `automojo-interfaces` command-runner abstraction.  No
`subprocess`, no direct `open`, and no direct `pathlib` calls are
made inside this module.

The Ubuntu `/etc/samba/smb.conf` file is wire-compatible with the
canonical Samba INI form: every share is a `[<name>]` section
followed by `key = value` directives.  Reload occurs through
`smbcontrol all reload-config`.

This feature complements — and does not duplicate — the
`smbd.service` lifecycle owned by `3-service-management`, the
SMB-port firewall surface (137/138/139/445) owned by
`3-firewall-management`, and the local POSIX-account surface owned
by `3-user-group-management`.  Samba-specific user password storage
is the concern of this feature; local POSIX passwords remain with
user-group-management.

---

## Requirements

### Requirement 1: Add a Share

**User Story:** As a maintenance coordinator, I want to add a Samba
share to `/etc/samba/smb.conf`, so that the declared share set
matches the desired state and the daemon advertises the new share
after a configuration reload.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_share(spec)` THE SYSTEM SHALL
   read `/etc/samba/smb.conf` via `cat`, parse the existing sections,
   render the requested spec into a `[<name>]` section block, and
   write the result via `tee /etc/samba/smb.conf` with the rendered
   content on stdin.
2. WHEN the file already contains a `[<name>]` section whose
   directive set matches the spec exactly THE SYSTEM SHALL return
   `status=SKIPPED` without invoking `tee`.
3. WHEN the file contains a `[<name>]` section with a different
   directive set THE SYSTEM SHALL replace the existing section in the
   rendered content (preserving section order).
4. WHEN the write succeeds THE SYSTEM SHALL invoke
   `smbcontrol all reload-config` to reload the running daemon.
5. WHEN `smbcontrol all reload-config` fails THE SYSTEM SHALL map
   the verb to `status=FAILED` even when the `tee` write succeeded.
6. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
7. WHEN the write fails THE SYSTEM SHALL return `status=FAILED` with
   `exit_code`, `stdout`, `stderr`, and `command` populated.

---

### Requirement 2: Remove a Share

**User Story:** As a maintenance coordinator, I want to remove a
share from `/etc/samba/smb.conf`, so that the daemon stops
advertising the share and the declared share set matches the desired
state.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_share(name)` and the file does
   not exist THE SYSTEM SHALL return `status=SKIPPED`.
2. WHEN the file does not contain a `[<name>]` section THE SYSTEM
   SHALL return `status=SKIPPED`.
3. WHEN the file contains a `[<name>]` section THE SYSTEM SHALL
   strip the entire section block (header through last directive
   before the next section header), re-render, and write via
   `tee /etc/samba/smb.conf`.
4. WHEN the write succeeds THE SYSTEM SHALL invoke
   `smbcontrol all reload-config`.
5. WHEN `smbcontrol all reload-config` fails THE SYSTEM SHALL map
   the verb to `status=FAILED`.
6. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 3: List Shares

**User Story:** As a maintenance coordinator, I want to enumerate
the configured shares (file content) along with each share's basic
state, so that I can compare against the declared desired state and
detect drift.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_shares()` and `/etc/samba/smb.conf`
   does not exist THE SYSTEM SHALL return an empty list.
2. WHEN the file exists THE SYSTEM SHALL parse every section whose
   header is NOT one of the reserved names (`[global]`, `[homes]`,
   `[printers]`) into a `UbuntuSambaShareInfo`.
3. WHEN `list_shares` is invoked THE SYSTEM SHALL pass
   `elevated=False` to every runner call.
4. WHEN `cat` exits non-zero AND `stderr` is non-empty THE SYSTEM
   SHALL raise `UbuntuSambaQueryError` with `command`,
   `exit_code`, and `stderr` populated.

---

### Requirement 4: Get Share

**User Story:** As a maintenance coordinator, I want to fetch a
single share record by name, so that I can compare the live block
against the declared spec.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_share(name)` THE SYSTEM SHALL
   read `/etc/samba/smb.conf` and return the `UbuntuSambaShareInfo`
   for the matching section.
2. WHEN no `[<name>]` section exists THE SYSTEM SHALL return `None`.
3. WHEN the file does not exist THE SYSTEM SHALL return `None`.
4. WHEN `get_share` is invoked THE SYSTEM SHALL pass
   `elevated=False` to every runner call.

---

### Requirement 5: Set Share Permissions

**User Story:** As a maintenance coordinator, I want to update the
permission keys (`valid users`, `read list`, `write list`) on an
existing share, so that share-level ACLs can be tightened or
relaxed without rebuilding the share.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_share_permissions(name, acl)`
   THE SYSTEM SHALL read `/etc/samba/smb.conf`, locate the matching
   section, and rewrite the `valid users`, `read list`, and
   `write list` directives from the supplied
   `UbuntuSambaShareAcl`.
2. WHEN the existing directives already match the supplied ACL
   (order-insensitive) THE SYSTEM SHALL return `status=SKIPPED`
   without invoking `tee`.
3. WHEN no `[<name>]` section exists THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN the write succeeds THE SYSTEM SHALL invoke
   `smbcontrol all reload-config`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 6: Reload Config

**User Story:** As a maintenance coordinator, I want to ask the
running daemon to re-read `smb.conf`, so that changes that bypassed
`add_share` / `remove_share` (e.g., manual edits) take effect.

#### Acceptance Criteria

1. WHEN the coordinator calls `reload_config()` THE SYSTEM SHALL
   invoke `smbcontrol all reload-config` via the runner.
2. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
3. WHEN the runner exits 0 THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the runner exits non-zero THE SYSTEM SHALL return
   `status=FAILED`.
5. `reload_config` MUST NOT skip — it is operational and
   unconditional.

---

### Requirement 7: Add a Samba User

**User Story:** As a maintenance coordinator, I want to register a
Samba user (with password) via `smbpasswd -a`, so that SMB clients
can authenticate against declared accounts.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_samba_user(name, password)` THE
   SYSTEM SHALL invoke `smbpasswd -s -a <name>` with the password
   supplied via runner `stdin` (delivered twice — `-s` reads new
   password and confirmation from stdin).
2. THE SYSTEM SHALL NEVER place the password on argv.  The
   recorded `command` field on the returned result MUST NOT contain
   the password.
3. WHEN `pdbedit -L` already lists the user THE SYSTEM SHALL return
   `status=SKIPPED` without invoking `smbpasswd`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN the runner exits non-zero THE SYSTEM SHALL return
   `status=FAILED` with `exit_code`, `stderr`, and the **sanitised**
   `command` populated.

---

### Requirement 8: Remove a Samba User

**User Story:** As a maintenance coordinator, I want to delete a
Samba user via `smbpasswd -x`, so that SMB authentication for the
account is revoked.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_samba_user(name)` THE SYSTEM
   SHALL invoke `smbpasswd -x <name>`.
2. WHEN `pdbedit -L` does NOT list the user THE SYSTEM SHALL return
   `status=SKIPPED` without invoking `smbpasswd`.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
4. WHEN the runner exits non-zero THE SYSTEM SHALL return
   `status=FAILED`.

---

### Requirement 9: List Samba Users

**User Story:** As a maintenance coordinator, I want to enumerate
the registered Samba users, so that drift between the declared user
set and the live `passdb.tdb` can be detected.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_samba_users()` THE SYSTEM SHALL
   invoke `pdbedit -L -v` via the runner with `elevated=False`.
2. THE SYSTEM SHALL parse the output into
   `list[UbuntuSambaUserInfo]` carrying `username`, `enabled`,
   `last_password_change`, and `description` fields.
3. WHEN `pdbedit -L -v` exits non-zero AND `stderr` is non-empty
   THE SYSTEM SHALL raise `UbuntuSambaQueryError`.

---

### Requirement 10: Elevation Policy

**User Story:** As a maintenance coordinator, I want every write
that mutates server state to elevate, so that operations succeed on
standard Ubuntu hosts where the process does not run as root.

#### Acceptance Criteria

1. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` for
   every write verb (`add_share`, `remove_share`,
   `set_share_permissions`, `reload_config`, `add_samba_user`,
   `remove_samba_user`) and any chained `smbcontrol` dispatches.
2. WHEN performing reads (`list_shares`, `get_share`,
   `list_samba_users`) THE SYSTEM SHALL pass `elevated=False`
   regardless of the config.
3. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False` on
   every path.

---

### Requirement 11: Dry-Run

**User Story:** As a maintenance coordinator, I want to preview every
mutation without dispatching, so that diff-vs-state reports can be
generated before changes go live.

#### Acceptance Criteria

1. WHEN `dry_run=True` THE SYSTEM SHALL NOT dispatch any mutating
   command (`tee /etc/samba/smb.conf`, `smbcontrol all reload-config`,
   `smbpasswd`).
2. WHEN `dry_run=True` THE SYSTEM SHALL still allow read-only
   probes that drive idempotency decisions (`cat /etc/samba/smb.conf`,
   `pdbedit -L`).
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` for
   every mutating verb with `command` populated to the
   would-have-been argv (sanitised for `add_samba_user`).

---

### Requirement 12: Failure Reporting

**User Story:** As a maintenance coordinator, I want every failed
write to surface a structured result, so that downstream automation
can categorise failures without scraping logs.

#### Acceptance Criteria

1. WHEN any write verb's underlying runner call exits non-zero THE
   SYSTEM SHALL return `status=FAILED` with `exit_code`, `stdout`,
   `stderr`, and `command` populated from the runner's
   `RunResult`.
2. WHEN a chained command fails (e.g., `smbcontrol` after a
   successful `tee`) THE SYSTEM SHALL surface the chained command's
   `command`, `exit_code`, and `stderr` on the returned result.
3. WHEN a read verb's underlying runner call exits non-zero with
   non-empty `stderr` THE SYSTEM SHALL raise
   `UbuntuSambaQueryError` carrying `command`, `exit_code`, and
   `stderr`.
4. THE SYSTEM SHALL NEVER include passphrase material in the
   recorded `command` field on any returned result for
   `add_samba_user`.

---

### Requirement 13: Idempotency Across Verbs

**User Story:** As a maintenance coordinator, I want every
configuration-mutating verb to be safe to call repeatedly, so that
declarative reconciliation loops do not produce duplicate sections
or churn the file unnecessarily.

#### Acceptance Criteria

1. WHEN `add_share(spec)` is called with a spec already present in
   `/etc/samba/smb.conf` THE SYSTEM SHALL return `status=SKIPPED`
   and SHALL NOT invoke `tee` or `smbcontrol`.
2. WHEN `remove_share(name)` is called with no matching section THE
   SYSTEM SHALL return `status=SKIPPED` and SHALL NOT invoke `tee`
   or `smbcontrol`.
3. WHEN `set_share_permissions(name, acl)` is called and every
   permission key already matches THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN `add_samba_user(name, password)` is called and `pdbedit -L`
   already lists the user THE SYSTEM SHALL return
   `status=SKIPPED`.
5. WHEN `remove_samba_user(name)` is called and `pdbedit -L` does
   not list the user THE SYSTEM SHALL return `status=SKIPPED`.
6. WHEN `reload_config()` is called THE SYSTEM SHALL NOT short-
   circuit; it is the only write verb that always dispatches.
