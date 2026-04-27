# Requirements

## Introduction

The ssh-server-configuration feature provides the
`automojo-sysman-linux-ubuntu` package with the ability to manage the
OpenSSH server configuration on Ubuntu hosts: top-level `sshd_config`
keywords, `Match` blocks, configuration validation, daemon reload, and
per-user `authorized_keys` entries.  This spec implements the
ssh-server-configuration contract defined at
`automojo-sysman-common/specs/contracts/ssh-server-configuration/contract.md`
for Ubuntu.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *what* policy the server enforces — it provides
the capability to *apply* policy reliably, idempotently, and with full
auditability.  All system commands (`sshd`, `systemctl`, `cat`, `tee`)
are executed exclusively through the `automojo-interfaces`
command-runner abstraction.  No `subprocess`, no direct `open`, and no
direct `pathlib` calls are made inside this module.

The Ubuntu `sshd_config` lives at `/etc/ssh/sshd_config`.  Ubuntu's
OpenSSH server daemon is exposed through systemd as **`ssh.service`**
(NOT `sshd.service`); this is a hard divergence from RHEL and is
captured in the manager's default `service_name`.  Per-user
`authorized_keys` lives at `/home/<user>/.ssh/authorized_keys`.

---

## Requirements

### Requirement 1: Read a Top-Level Setting

**User Story:** As a maintenance coordinator, I want to read a single
`sshd_config` keyword's value, so that I can inspect declared policy
without parsing the whole file.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_sshd_setting(keyword)` THE SYSTEM
   SHALL read `/etc/ssh/sshd_config` via `cat` (no elevation) and parse
   it line-by-line.
2. WHEN the keyword exists at the top level (outside any `Match`
   block) THE SYSTEM SHALL return a `UbuntuSshSettingValue` with
   `keyword`, `value`, and `source_file` populated.
3. WHEN the keyword is absent THE SYSTEM SHALL return `None`.
4. WHEN the source file does not exist THE SYSTEM SHALL return `None`.
5. WHEN `get_sshd_setting` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True` to the runner.

---

### Requirement 2: Set a Top-Level Setting

**User Story:** As a maintenance coordinator, I want to set a
`sshd_config` keyword to a value, so that policy is applied
declaratively.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_sshd_setting(keyword, value)` THE
   SYSTEM SHALL read the current file via `cat`, parse, mutate the
   keyword in memory, re-render, and write the result via `tee
   /etc/ssh/sshd_config` with the rendered content on stdin.
2. WHEN the file already contains `keyword value` AND the rendered
   content equals the existing content THE SYSTEM SHALL return
   `status=SKIPPED` without invoking `tee`.
3. WHEN the keyword exists with a different value THE SYSTEM SHALL
   replace the value in the rendered content.
4. WHEN the file does not exist THE SYSTEM SHALL create it with a
   single line `keyword value`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with
   `command=["tee", "/etc/ssh/sshd_config"]` and SHALL NOT invoke the
   runner.
6. WHEN the write fails THE SYSTEM SHALL return `status=FAILED` with
   `exit_code`, `stdout`, `stderr`, and `command` populated.

---

### Requirement 3: Remove a Top-Level Setting

**User Story:** As a maintenance coordinator, I want to remove a
`sshd_config` keyword, so that the daemon falls back to its default.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_sshd_setting(keyword)` and the
   file does not exist THE SYSTEM SHALL return `status=SKIPPED`.
2. WHEN the keyword is not present THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN the keyword is present THE SYSTEM SHALL parse, drop every
   top-level occurrence of the keyword, re-render, and write via
   `tee`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN the write fails THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 4: List Top-Level Settings

**User Story:** As a maintenance coordinator, I want to enumerate every
top-level `sshd_config` keyword, so that I can audit the declared
configuration.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_sshd_settings()` and the file does
   not exist THE SYSTEM SHALL return an empty dict.
2. WHEN the file exists THE SYSTEM SHALL parse and return a `dict[str,
   str]` of every top-level entry, preserving the case of the keyword
   as written.
3. WHEN a keyword appears more than once at the top level THE SYSTEM
   SHALL return the last-written value (matches `sshd`'s precedence
   semantics for repeated keywords).
4. WHEN `list_sshd_settings` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True`.

---

### Requirement 5: Add a Match Block

**User Story:** As a maintenance coordinator, I want to declare a
`Match <criteria>` block with a set of child settings, so that policy
can be scoped to a user, group, host, or address.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_match_block(criteria, settings)`
   THE SYSTEM SHALL read the current file, parse, upsert the block
   identified by `criteria` (whitespace-collapsed comparison),
   re-render, and write via `tee`.
2. WHEN a block with identical criteria AND identical settings already
   exists THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN a block with the same criteria exists with different settings
   THE SYSTEM SHALL replace the block's settings.
4. WHEN no matching block exists THE SYSTEM SHALL append a new block
   to the end of the file.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN the write fails THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 6: Remove a Match Block

**User Story:** As a maintenance coordinator, I want to remove a
`Match` block, so that scoped policy can be retired.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_match_block(criteria)` and no
   block with that criteria exists THE SYSTEM SHALL return
   `status=SKIPPED`.
2. WHEN the file does not exist THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN a matching block exists THE SYSTEM SHALL parse, drop the
   block, re-render, and write via `tee`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 7: List Match Blocks

**User Story:** As a maintenance coordinator, I want to enumerate every
`Match` block in `sshd_config`, so that scoped policy can be audited.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_match_blocks()` and the file does
   not exist THE SYSTEM SHALL return an empty list.
2. WHEN the file exists THE SYSTEM SHALL parse every `Match` block
   and return a `list[UbuntuSshMatchBlock]` in declaration order.
3. WHEN a block declares no child settings THE SYSTEM SHALL include it
   in the list with an empty `settings` dict.
4. WHEN `list_match_blocks` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True`.

---

### Requirement 8: Reload sshd

**User Story:** As a maintenance coordinator, I want to reload the
sshd daemon, so that configuration changes take effect without
dropping established connections.

#### Acceptance Criteria

1. WHEN the coordinator calls `reload_sshd()` THE SYSTEM SHALL invoke
   `systemctl reload ssh` (Ubuntu's OpenSSH systemd unit name) via
   the runner.
2. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to the
   runner.
3. WHEN the reload succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the reload fails THE SYSTEM SHALL return `status=FAILED` with
   `exit_code` and `stderr` populated.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.

---

### Requirement 9: Validate sshd Configuration

**User Story:** As a maintenance coordinator, I want to validate the
current `sshd_config`, so that policy errors can be caught before a
reload.

#### Acceptance Criteria

1. WHEN the coordinator calls `test_sshd_config()` THE SYSTEM SHALL
   invoke `sshd -t` via the runner with `elevated=elevated`.
2. WHEN exit code is 0 THE SYSTEM SHALL return `status=SUCCESS`.
3. WHEN exit code is non-zero THE SYSTEM SHALL return `status=FAILED`
   with the captured `stderr` populated.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.

---

### Requirement 10: List Authorized Keys

**User Story:** As a maintenance coordinator, I want to enumerate the
keys in a user's `authorized_keys` file, so that I can audit access.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_authorized_keys(user)` and the
   file does not exist THE SYSTEM SHALL return an empty list.
2. WHEN the file exists THE SYSTEM SHALL parse each non-comment,
   non-empty line into a `UbuntuSshAuthorizedKey` with `key_type`,
   `key_data`, `comment`, `fingerprint`, and `options`.
3. WHEN a line begins with options (everything before the recognised
   key-type token) THE SYSTEM SHALL capture them in `options`.
4. WHEN a key body fails to base64-decode THE SYSTEM SHALL set
   `fingerprint` to `None` while still returning the entry.
5. WHEN `list_authorized_keys` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True`.

---

### Requirement 11: Add an Authorized Key

**User Story:** As a maintenance coordinator, I want to add a public
key to a user's `authorized_keys`, so that the user can authenticate.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_authorized_key(user, key)` THE
   SYSTEM SHALL parse `key` as an OpenSSH `authorized_keys` line.
2. WHEN parsing fails THE SYSTEM SHALL return `status=FAILED` with
   `message="key line failed to parse"` and SHALL NOT invoke the
   runner.
3. WHEN the user's existing `authorized_keys` already contains an
   entry with the same SHA-256 fingerprint THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN the key is new THE SYSTEM SHALL append the line to the file
   content and write it via `tee
   /home/<user>/.ssh/authorized_keys` with the rendered content on
   stdin.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN the write fails THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 12: Remove an Authorized Key

**User Story:** As a maintenance coordinator, I want to remove a
specific key from a user's `authorized_keys` by fingerprint, so that
revocation is unambiguous.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_authorized_key(user,
   fingerprint)` and the file does not exist THE SYSTEM SHALL return
   `status=SKIPPED`.
2. WHEN no entry has the supplied fingerprint THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN a matching entry exists THE SYSTEM SHALL drop every line whose
   computed fingerprint matches and write the new content via `tee`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN the write fails THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 13: Elevation Policy

**User Story:** As a maintenance coordinator, I want all writes that
target `/etc/ssh/sshd_config` to elevate, so that operations succeed
on standard Ubuntu hosts where the process does not run as root.

#### Acceptance Criteria

1. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to the
   runner for every write verb (`set_sshd_setting`,
   `remove_sshd_setting`, `add_match_block`, `remove_match_block`,
   `add_authorized_key`, `remove_authorized_key`, `reload_sshd`,
   `test_sshd_config`).
2. WHEN performing reads (`get_sshd_setting`, `list_sshd_settings`,
   `list_match_blocks`, `list_authorized_keys`) THE SYSTEM SHALL
   pass `elevated=False` regardless of config.
3. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False` on
   every path.
4. (Pilot follow-up) Authorized-key writes for the calling user
   technically do not require elevation; the pilot honours
   `config.elevated` uniformly and tracks the optimisation in
   `tasks.md`.
