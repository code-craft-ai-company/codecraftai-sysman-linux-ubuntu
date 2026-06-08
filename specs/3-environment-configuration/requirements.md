# Requirements

## Introduction

The environment configuration feature provides the `codecraftai-sysman-linux-ubuntu`
package with the ability to set, unset, and query system-wide and per-user
environment variables on Ubuntu systems.  This spec implements the
environment-configuration contract defined at
`codecraftai-sysman-common/specs/contracts/environment-configuration/contract.md`
for Ubuntu using `/etc/environment`, `/etc/profile.d/*.sh`, per-user
`~/.profile`/`~/.bashrc`, and optionally `~/.pam_environment`.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *what*
variables to apply — it provides the capability to *do it* reliably,
idempotently, and with full auditability.  All environment-file I/O is delegated
to the `IFileOperator` abstraction; `pwd.getpwnam` is the single direct call made
by the module and is isolated in a private helper.  No `subprocess`, no direct
`open`, and no direct `pathlib` calls are made inside the module.

The abstract `EnvScope` on Ubuntu maps to:

- **SYSTEM**: `/etc/environment` for bare `KEY=VALUE` lines plus
  `/etc/profile.d/ccai.sh` for `export KEY=VALUE` lines.
- **USER**: `~<user>/.profile` by default (or `~<user>/.bashrc` /
  `~<user>/.pam_environment` at caller request).

---

## Requirements

### Requirement 1: Set a System-Scope Variable

**User Story:** As a maintenance coordinator, I want to set a system-scope
environment variable, so that the value is visible to every login on the host.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_system_env(name, value)` THE SYSTEM SHALL
   write the variable as a `KEY=VALUE` line in `/etc/environment` and as an
   `export KEY=VALUE` line in `/etc/profile.d/ccai.sh`.
2. WHEN either file already has an identical value THE SYSTEM SHALL return
   `status=SKIPPED` for that file's role; the overall return value SHALL be
   `SKIPPED` only if BOTH files are already correct, otherwise `SUCCESS`.
3. WHEN `/etc/profile.d/ccai.sh` does not exist THE SYSTEM SHALL create it
   with a `#!/usr/bin/env bash` shebang followed by the export statement.
4. WHEN `dry_run=True` THE SYSTEM SHALL NOT call `IFileOperator.write_text` or
   `delete` and SHALL return `status=DRY_RUN`.
5. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to every
   `write_text` call.
6. WHEN the write fails THE SYSTEM SHALL return `status=FAILED` with `message`
   populated from the exception.

---

### Requirement 2: Unset a System-Scope Variable

**User Story:** As a maintenance coordinator, I want to unset a system-scope
environment variable, so that future logins no longer see it.

#### Acceptance Criteria

1. WHEN the coordinator calls `unset_system_env(name)` and the variable is not
   present in either `/etc/environment` or `/etc/profile.d/ccai.sh` THE
   SYSTEM SHALL return `status=SKIPPED`.
2. WHEN the variable is present in `/etc/environment` THE SYSTEM SHALL rewrite
   the file with the matching line removed.
3. WHEN the variable is present in `/etc/profile.d/ccai.sh` THE SYSTEM
   SHALL rewrite the file with the matching `export` line removed.
4. WHEN removing the last managed variable from `/etc/profile.d/ccai.sh`
   leaves the file with only the shebang and blank lines THE SYSTEM SHALL
   delete the file via `IFileOperator.delete`.
5. WHEN `dry_run=True` THE SYSTEM SHALL NOT call `write_text` or `delete` and
   SHALL return `status=DRY_RUN`.
6. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True`.

---

### Requirement 3: Get a System-Scope Variable

**User Story:** As a maintenance coordinator, I want to read a single
system-scope variable, so that I can check its current value.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_system_env(name)` THE SYSTEM SHALL read
   `/etc/environment` and `/etc/profile.d/ccai.sh` via
   `IFileOperator.read_text` and parse them in pure Python.
2. WHEN the variable is present in either file THE SYSTEM SHALL return a
   `UbuntuEnvValue` with `scope=SYSTEM` and `source` equal to the file it came
   from.  When the variable is present in both files the first listed source
   (`/etc/environment`) wins, and the mismatch is surfaced in `message` if
   the values differ.
3. WHEN the variable is not present in either file THE SYSTEM SHALL return
   `None`.
4. WHEN `get_system_env` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 4: List System-Scope Variables

**User Story:** As a maintenance coordinator, I want to list every system-scope
variable, so that I can enumerate the full environment.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_system_env()` THE SYSTEM SHALL parse both
   `/etc/environment` and `/etc/profile.d/ccai.sh` and merge the results
   into a `dict[str, str]`.
2. WHEN a variable appears in both files with identical values THE SYSTEM
   SHALL emit one entry.
3. WHEN a variable appears in both files with different values THE SYSTEM
   SHALL use `/etc/environment` as the authoritative source and log a warning
   (returned via a sibling method or a log sink — not raised).
4. WHEN both files are missing THE SYSTEM SHALL return an empty dict.
5. WHEN `list_system_env` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 5: Set a User-Scope Variable

**User Story:** As a maintenance coordinator, I want to set a user-scope
environment variable, so that a specific user's future logins see the value.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_user_env(user, name, value)` and the user
   does not exist THE SYSTEM SHALL return `status=FAILED` with `message="user
   not found: <user>"` and SHALL NOT call the file operator.
2. WHEN the user exists THE SYSTEM SHALL resolve the home directory via
   `pwd.getpwnam(user).pw_dir` and select
   `<home>/.profile` as the default target file (caller may override to
   `.bashrc` or `.pam_environment`).
3. WHEN the target file does not exist THE SYSTEM SHALL create it with the
   single `export KEY=VALUE` line.
4. WHEN the target file exists and already has the variable with identical
   value THE SYSTEM SHALL return `status=SKIPPED`.
5. WHEN the target file exists with a different value for the variable THE
   SYSTEM SHALL rewrite the file with the line replaced in place.
6. WHEN `dry_run=True` THE SYSTEM SHALL NOT call `write_text` or `delete` and
   SHALL return `status=DRY_RUN`.
7. WHEN `elevated=True` AND the target user is not the current user THE
   SYSTEM SHALL pass `elevated=True` to `write_text`.  When the target is the
   current user THE SYSTEM SHALL pass `elevated=False`.

---

### Requirement 6: Unset a User-Scope Variable

**User Story:** As a maintenance coordinator, I want to unset a user-scope
environment variable.

#### Acceptance Criteria

1. WHEN the coordinator calls `unset_user_env(user, name)` and the user does
   not exist THE SYSTEM SHALL return `status=FAILED` with `message="user not
   found: <user>"` and SHALL NOT call the file operator.
2. WHEN the target file does not exist or does not contain the variable THE
   SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the variable is present THE SYSTEM SHALL rewrite the file with the
   matching line removed.
4. WHEN `dry_run=True` THE SYSTEM SHALL NOT call `write_text` or `delete`.
5. WHEN `elevated=True` THE SYSTEM SHALL follow the same elevation policy as
   `set_user_env`.

---

### Requirement 7: Get a User-Scope Variable

**User Story:** As a maintenance coordinator, I want to read a single
user-scope variable, so that I can check its current value.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_user_env(user, name)` and the user does not
   exist THE SYSTEM SHALL raise `UbuntuEnvConfigError`.
2. WHEN the user exists and the target file contains the variable THE SYSTEM
   SHALL return a `UbuntuEnvValue` with `scope=USER` and `source=<file path>`.
3. WHEN the variable is absent THE SYSTEM SHALL return `None`.
4. WHEN `get_user_env` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 8: List User-Scope Variables

**User Story:** As a maintenance coordinator, I want to list every user-scope
variable set for a user.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_user_env(user)` and the user does not
   exist THE SYSTEM SHALL raise `UbuntuEnvConfigError`.
2. WHEN the target file does not exist THE SYSTEM SHALL return an empty dict.
3. WHEN the target file exists THE SYSTEM SHALL parse it and return a
   `dict[str, str]` of its variable definitions.
4. WHEN `list_user_env` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 9: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want system-scope writes to
run with root privileges, so that writes under `/etc/` succeed.

#### Acceptance Criteria

1. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` on every
   `write_text` / `delete` call for system-scope writes.
2. WHEN `elevated=True` AND the user-scope target is a different user THE
   SYSTEM SHALL pass `elevated=True`.
3. WHEN `elevated=True` AND the user-scope target is the current user THE
   SYSTEM SHALL pass `elevated=False`.
4. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False` everywhere.
5. WHEN performing reads THE SYSTEM SHALL NEVER pass `elevated=True`.

---

### Requirement 10: Dry-Run Semantics

**User Story:** As a maintenance coordinator, I want every write verb to honour
dry-run mode, so that plans can be validated without system change.

#### Acceptance Criteria

1. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` for every
   write verb without invoking any mutating file-operator method.
2. WHEN `dry_run=False` THE SYSTEM SHALL execute the normal code path
   regardless of other configuration.
3. WHEN `dry_run=True` THE SYSTEM SHALL still read files via
   `IFileOperator.read_text` so that the guard logic can distinguish
   `SKIPPED` from `DRY_RUN`.
