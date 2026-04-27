# Requirements

## Introduction

The repository management feature provides the `automojo-sysman-linux-ubuntu`
package with the ability to manage `apt` software sources on Ubuntu systems.
This spec implements the repository-management contract defined at
`automojo-sysman-common/specs/contracts/repository-management/contract.md`
for Ubuntu using `apt`, `add-apt-repository`, and direct file management of
`/etc/apt/sources.list.d/` plus `/etc/apt/keyrings/`.  It covers adding,
removing, enabling, disabling, listing, and refreshing repositories, setting
a repository priority (apt pinning), and importing GPG keys.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The feature
does not decide *which* repositories to configure — it provides the
capability to *do it* reliably, idempotently, and with full auditability.
All system commands are executed exclusively through the
`automojo-interfaces` command-runner abstraction, and all source-file /
keyring-file mutations are executed through the `automojo-interfaces`
file-operator abstraction, enabling dry-run simulation, auditing, and test
mocking without requiring a live system.

Ubuntu's repository model differs from RHEL in that source entries live in
one-per-line `.list` files under `/etc/apt/sources.list.d/`, GPG keys live
as binary files under `/etc/apt/keyrings/` and are referenced inline in
the source entry with `[signed-by=...]`, and PPA sources are managed by
`add-apt-repository`.  The manager prefers file-based management (via
`IFileOperator`) for non-PPA sources so that dry-run and idempotency
guards operate on the same file-read surface.

---

## Requirements

### Requirement 1: Add a Repository

**User Story:** As a maintenance coordinator, I want to add a new apt
source, so that packages from a new channel become available.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_repository(spec)` AND `spec.name`
   begins with the literal prefix `ppa:` THE SYSTEM SHALL invoke
   `add-apt-repository -y ppa:<user/repo>` via the command runner
   with elevation.
2. WHEN `spec.name` does NOT begin with `ppa:` THE SYSTEM SHALL
   render the source entry as a deb822-style line
   `deb [signed-by=/etc/apt/keyrings/<name>.gpg] <base_url>
   <channel> <components>` and write it via the `IFileOperator` to
   `/etc/apt/sources.list.d/<name>.list`, AND IF `spec.gpg_key` is
   set THE SYSTEM SHALL first import the key via
   `import_gpg_key(spec.gpg_key)` into
   `/etc/apt/keyrings/<name>.gpg`.
3. WHEN the target `.list` file already exists AND its content is
   byte-identical to the rendered content THE SYSTEM SHALL return a
   `UbuntuRepositoryActionResult` with status `SKIPPED`.
4. WHEN the command or file write succeeds THE SYSTEM SHALL return
   a `UbuntuRepositoryActionResult` with status `SUCCESS` and
   `file_path` set to the written file.
5. WHEN the command or file write fails THE SYSTEM SHALL return a
   `UbuntuRepositoryActionResult` with status `FAILED`, the exit
   code, captured stderr, and the `command`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command or file write and SHALL return a
   `UbuntuRepositoryActionResult` with status `DRY_RUN`.

---

### Requirement 2: Remove a Repository

**User Story:** As a maintenance coordinator, I want to remove an apt
source, so that packages from that channel are no longer available.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_repository(name)` AND `name`
   begins with `ppa:` THE SYSTEM SHALL invoke `add-apt-repository -r
   -y ppa:<user/repo>` via the command runner with elevation.
2. WHEN `name` does NOT begin with `ppa:` THE SYSTEM SHALL delete
   `/etc/apt/sources.list.d/<name>.list` via the `IFileOperator`.
3. WHEN the target file (or disabled variant) does not exist THE
   SYSTEM SHALL return a `UbuntuRepositoryActionResult` with status
   `SKIPPED`.
4. WHEN the target file exists in its `.list.disabled` form THE
   SYSTEM SHALL delete the `.list.disabled` file via the
   `IFileOperator`.
5. WHEN the delete succeeds THE SYSTEM SHALL return a
   `UbuntuRepositoryActionResult` with status `SUCCESS` and
   `file_path` set to the deleted path.
6. WHEN the delete fails THE SYSTEM SHALL return a
   `UbuntuRepositoryActionResult` with status `FAILED`.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   mutation and SHALL return `DRY_RUN`.

---

### Requirement 3: Enable a Repository

**User Story:** As a maintenance coordinator, I want to re-enable a
previously disabled apt source, so that `apt update` consults it.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_repository(name)` AND the file
   `/etc/apt/sources.list.d/<name>.list.disabled` exists THE SYSTEM
   SHALL rename it to `/etc/apt/sources.list.d/<name>.list` via the
   `IFileOperator` (read old, write new, delete old).
2. WHEN the file `/etc/apt/sources.list.d/<name>.list` already
   exists THE SYSTEM SHALL return a `UbuntuRepositoryActionResult`
   with status `SKIPPED`.
3. WHEN neither the enabled nor the disabled file exists THE SYSTEM
   SHALL return a `UbuntuRepositoryActionResult` with status
   `FAILED` and `message="repository_not_found"`.
4. WHEN the rename succeeds THE SYSTEM SHALL return a result with
   status `SUCCESS` and `file_path` set to the newly enabled path.
5. WHEN any file operation fails THE SYSTEM SHALL return a result
   with status `FAILED` carrying the error.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   mutation and SHALL return `DRY_RUN`.

---

### Requirement 4: Disable a Repository

**User Story:** As a maintenance coordinator, I want to disable an
apt source without losing its configuration, so that it can be
re-enabled later.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_repository(name)` AND the file
   `/etc/apt/sources.list.d/<name>.list` exists THE SYSTEM SHALL
   rename it to `/etc/apt/sources.list.d/<name>.list.disabled`
   via the `IFileOperator`.
2. WHEN the file `/etc/apt/sources.list.d/<name>.list.disabled`
   already exists THE SYSTEM SHALL return a
   `UbuntuRepositoryActionResult` with status `SKIPPED`.
3. WHEN neither the enabled nor the disabled file exists THE SYSTEM
   SHALL return a result with status `FAILED` and
   `message="repository_not_found"`.
4. WHEN the rename succeeds THE SYSTEM SHALL return a result with
   status `SUCCESS`.
5. WHEN the rename fails THE SYSTEM SHALL return a result with
   status `FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   mutation and SHALL return `DRY_RUN`.

---

### Requirement 5: List Repositories

**User Story:** As a maintenance coordinator, I want to enumerate
every configured apt source, so that I can audit configuration or
drive conditional logic.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_repositories()` THE SYSTEM SHALL
   invoke `apt-cache policy` via the command runner without
   elevation AND SHALL enumerate files under
   `/etc/apt/sources.list.d/` via the `IFileOperator`.
2. WHEN `apt-cache policy` exits non-zero THE SYSTEM SHALL raise a
   `UbuntuRepositoryQueryError` carrying the exit code and
   captured stderr.
3. WHEN enumeration succeeds THE SYSTEM SHALL return a
   `list[UbuntuRepositoryInfo]` with one entry per source file,
   populating `name` from the file stem (without the trailing
   `.list` or `.list.disabled`), `enabled=True` for `.list` and
   `False` for `.list.disabled`, `url` from the first URL extracted
   from the file, `priority` from the pinning file if present
   (see Requirement 8), and `display_name=<name>`.
4. WHEN no source files exist THE SYSTEM SHALL return an empty list
   without invoking the runner.
5. WHEN the enumeration reads a malformed source line THE SYSTEM
   SHALL silently skip it; malformed lines do not raise.

---

### Requirement 6: Get a Single Repository

**User Story:** As a maintenance coordinator, I want to resolve a
repository by name, so that I can inspect its state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_repository(name)` THE SYSTEM SHALL
   invoke `list_repositories()` internally and return the matching
   entry.
2. WHEN no entry matches THE SYSTEM SHALL return `None`.
3. WHEN `list_repositories()` raises THE SYSTEM SHALL propagate the
   `UbuntuRepositoryQueryError`.

---

### Requirement 7: Refresh Repositories

**User Story:** As a maintenance coordinator, I want to refresh apt
metadata, so that newly added sources are consulted.

#### Acceptance Criteria

1. WHEN the coordinator calls `refresh_repositories(name=None)` THE
   SYSTEM SHALL invoke `apt-get update` via the command runner with
   elevation.
2. WHEN the coordinator calls `refresh_repositories(name=<name>)`
   AND `<name>` is a known repository THE SYSTEM SHALL invoke
   `apt-get update -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/<name>.list
   -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0` via the
   command runner with elevation.
3. WHEN `apt-get update` succeeds THE SYSTEM SHALL return a
   `UbuntuRepositoryActionResult` with status `SUCCESS`.
4. WHEN `apt-get update` fails THE SYSTEM SHALL return a result
   with status `FAILED`, the exit code, and captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return `DRY_RUN`.
6. THE SYSTEM SHALL NOT evaluate an idempotency guard on
   `refresh_repositories` — refresh is operational.

---

### Requirement 8: Set Repository Priority (Pinning)

**User Story:** As a maintenance coordinator, I want to set a
repository's priority via apt pinning, so that package resolution
favors one source over another.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_priority(name, priority)` AND the
   repository does not exist (neither `.list` nor `.list.disabled`)
   THE SYSTEM SHALL raise a `UbuntuRepositoryNotFoundError` with
   the missing name.
2. WHEN the repository exists AND the current pinning file
   `/etc/apt/preferences.d/<name>.pref` is byte-identical to the
   rendered content THE SYSTEM SHALL return a
   `UbuntuRepositoryActionResult` with status `SKIPPED`.
3. WHEN the pinning file differs or is absent THE SYSTEM SHALL
   render the pinning block as:
   ```
   Package: *
   Pin: origin "<url-hostname>"
   Pin-Priority: <priority>
   ```
   and write it to `/etc/apt/preferences.d/<name>.pref` via the
   `IFileOperator`.
4. WHEN the write succeeds THE SYSTEM SHALL return a result with
   status `SUCCESS` and `file_path` set to the preferences file.
5. WHEN the write fails THE SYSTEM SHALL return a result with
   status `FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute the
   write and SHALL return `DRY_RUN`.

---

### Requirement 9: Import a GPG Key

**User Story:** As a maintenance coordinator, I want to import a
repository's GPG signing key, so that package signatures can be
verified.

#### Acceptance Criteria

1. WHEN the coordinator calls `import_gpg_key(key_source)` AND
   `key_source` starts with `http://` or `https://` THE SYSTEM SHALL
   invoke `curl -fsSL <url> | gpg --dearmor -o
   /etc/apt/keyrings/<derived-name>.gpg` via the command runner
   with elevation where `<derived-name>` is derived from the URL's
   last path segment.
2. WHEN `key_source` is a local file path THE SYSTEM SHALL first
   call `IFileOperator.exists(path)` and SHALL raise a
   `UbuntuRepositoryKeyFileNotFoundError` if the file does not
   exist.
3. WHEN `key_source` is a local file path AND exists THE SYSTEM
   SHALL read its contents via the `IFileOperator`, dearmor them
   via `gpg --dearmor`, and write the binary result to
   `/etc/apt/keyrings/<derived-name>.gpg` via the `IFileOperator`.
4. WHEN the target keyring file already exists with identical
   content THE SYSTEM SHALL return a
   `UbuntuRepositoryActionResult` with status `SKIPPED`.
5. WHEN the import succeeds THE SYSTEM SHALL return a result with
   status `SUCCESS` and `file_path` set to the keyring path.
6. WHEN the import fails THE SYSTEM SHALL return a result with
   status `FAILED`, exit code, and captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command or file write and SHALL return `DRY_RUN`.

---

### Requirement 10: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all
repository-management writes to run with the privileges required by
`apt` and file mutations under `/etc/apt/`.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the command runner for
   `add-apt-repository`, `apt-get update`, `gpg --dearmor`, and to
   the file operator for every `/etc/apt/sources.list.d/`,
   `/etc/apt/keyrings/`, and `/etc/apt/preferences.d/` mutation.
2. WHEN performing read-only operations (`list_repositories`,
   `get_repository`) THE SYSTEM SHALL NOT pass `elevated=True`,
   regardless of config.
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass
   `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL
   default to no elevation and allow the runner/file operator to
   surface the resulting permission error as a
   `UbuntuRepositoryActionResult` with `status=FAILED`.

---

### Requirement 11: Dry-Run Safety

**User Story:** As a maintenance coordinator, I want a dry-run mode
that simulates repository changes without mutating the system.

#### Acceptance Criteria

1. WHEN the system is configured with `dry_run=True` THE SYSTEM
   SHALL NOT invoke `add-apt-repository`, `apt-get update`, `gpg
   --dearmor`, or any file-operator write/delete.
2. WHEN dry-run is active THE SYSTEM SHALL populate the `command`
   field of every returned result with the argv that would have
   been executed.
3. WHEN dry-run is active THE SYSTEM SHALL still perform read
   operations (file-operator `exists` / `read_text`,
   `apt-cache policy`) to evaluate idempotency guards.
4. WHEN dry-run is active THE SYSTEM SHALL still raise
   `UbuntuRepositoryNotFoundError` and
   `UbuntuRepositoryKeyFileNotFoundError` — precondition
   exceptions take precedence over dry-run.
