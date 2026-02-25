# Repository Configuration — Requirements

## Overview

The repository-configuration feature provides a Python API for managing APT software
repositories on Ubuntu/Debian hosts.  It covers adding and removing repository sources,
enabling and disabling existing sources, importing GPG signing keys, and refreshing the
local package index.

---

## User Stories and Acceptance Criteria

### US-1  Add a Repository

**As a** system administrator,
**I want to** add a new APT repository to `/etc/apt/sources.list.d/`,
**so that** packages from that source become available for installation.

#### Acceptance Criteria

| ID | Criterion |
|----|-----------|
| AC-1.1 | `add_repository(entry)` writes a valid one-line `deb` entry or DEB822 `*.sources` block to `/etc/apt/sources.list.d/<name>.list` (or `<name>.sources`). |
| AC-1.2 | If the file already exists with identical content, no write is performed and the call is idempotent. |
| AC-1.3 | If `dry_run=True`, no file is written; the method returns a `RepoResult` with `changed=False` and a description of what would have been written. |
| AC-1.4 | If elevation is required to write to `/etc/apt/sources.list.d/`, the call raises `ElevationRequiredError` when the process lacks sufficient privileges, rather than silently failing. |
| AC-1.5 | On success, `RepoResult.changed` is `True` and `RepoResult.path` contains the file path written. |

---

### US-2  Remove a Repository

**As a** system administrator,
**I want to** remove an APT repository source file from `/etc/apt/sources.list.d/`,
**so that** the repository is no longer consulted during package operations.

#### Acceptance Criteria

| ID | Criterion |
|----|-----------|
| AC-2.1 | `remove_repository(name)` deletes `/etc/apt/sources.list.d/<name>.list` (or `<name>.sources`) if it exists. |
| AC-2.2 | If the file does not exist, the call is idempotent and returns `RepoResult.changed=False`. |
| AC-2.3 | If `dry_run=True`, no file is removed; the method returns a description of what would have been removed. |
| AC-2.4 | Elevation requirements are enforced identically to AC-1.4. |

---

### US-3  Enable a Repository

**As a** system administrator,
**I want to** enable a previously disabled APT repository,
**so that** its packages are included in `apt-get` operations again.

#### Acceptance Criteria

| ID | Criterion |
|----|-----------|
| AC-3.1 | `enable_repository(name)` removes the `# ` comment prefix from the `deb` line(s) in the relevant `.list` file, or sets `Enabled: yes` in a DEB822 `.sources` file. |
| AC-3.2 | If the repository is already enabled, the call is idempotent. |
| AC-3.3 | If `dry_run=True`, no file is modified. |
| AC-3.4 | If the repository file does not exist, a `RepositoryNotFoundError` is raised. |

---

### US-4  Disable a Repository

**As a** system administrator,
**I want to** disable an APT repository without deleting its configuration file,
**so that** it can be re-enabled later without re-entering the source details.

#### Acceptance Criteria

| ID | Criterion |
|----|-----------|
| AC-4.1 | `disable_repository(name)` prepends `# ` to each `deb` line in the `.list` file, or sets `Enabled: no` in a DEB822 `.sources` file. |
| AC-4.2 | If the repository is already disabled, the call is idempotent. |
| AC-4.3 | If `dry_run=True`, no file is modified. |
| AC-4.4 | If the repository file does not exist, a `RepositoryNotFoundError` is raised. |

---

### US-5  Import a GPG Signing Key

**As a** system administrator,
**I want to** import a GPG signing key for a third-party repository,
**so that** APT can verify package signatures from that repository.

#### Acceptance Criteria

| ID | Criterion |
|----|-----------|
| AC-5.1 | `import_key(key_data, keyring_name)` dearmors the key with `gpg --dearmor` and writes the binary keyring to `/etc/apt/keyrings/<keyring_name>.gpg`. |
| AC-5.2 | If the keyring file already exists with identical content, no write is performed (idempotent). |
| AC-5.3 | The `/etc/apt/keyrings/` directory is created if it does not exist. |
| AC-5.4 | If `dry_run=True`, no file is written; the method describes what would have been written. |
| AC-5.5 | Elevation requirements are enforced identically to AC-1.4. |
| AC-5.6 | On success, `RepoResult.changed` is `True` and `RepoResult.path` contains the keyring file path. |

---

### US-6  Refresh the Package Index

**As a** system administrator,
**I want to** refresh the local APT package index,
**so that** the system has current metadata for all configured repositories.

#### Acceptance Criteria

| ID | Criterion |
|----|-----------|
| AC-6.1 | `refresh_index()` runs `apt-get update` and returns a `RepoResult` with the command's stdout/stderr. |
| AC-6.2 | If `dry_run=True`, the command is not run; the method returns a description of what would have been executed. |
| AC-6.3 | A non-zero exit code from `apt-get update` is propagated as a `RepoCommandError` with the full stderr included. |
| AC-6.4 | Elevation requirements are enforced identically to AC-1.4. |

---

### US-7  List Configured Repositories

**As a** system administrator,
**I want to** list all currently configured APT repositories and their enabled/disabled state,
**so that** I can audit the system's package sources.

#### Acceptance Criteria

| ID | Criterion |
|----|-----------|
| AC-7.1 | `list_repositories()` returns a list of `RepoEntry` objects parsed from files under `/etc/apt/sources.list.d/`. |
| AC-7.2 | Each `RepoEntry` includes: `name`, `uri`, `suite`, `components`, `enabled`, `file_path`, and optionally `signed_by`. |
| AC-7.3 | Disabled lines (prefixed with `# `) are included with `enabled=False`. |
| AC-7.4 | The result is sorted by `name` ascending. |
| AC-7.5 | An empty directory returns an empty list without raising an error. |

---

### US-8  Query Repository Status via `apt-cache policy`

**As a** system administrator,
**I want to** query the effective priority and availability of a repository,
**so that** I can understand how packages from that source are ranked.

#### Acceptance Criteria

| ID | Criterion |
|----|-----------|
| AC-8.1 | `get_repo_status(name)` runs `apt-cache policy` and returns a `RepoPolicyInfo` object containing the repository's pinning priority and origin information. |
| AC-8.2 | If the repository is not found in `apt-cache policy` output, `None` is returned. |
| AC-8.3 | Command errors are propagated as `RepoCommandError`. |

---

## Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NF-1 | All file I/O is performed through the injected `IFileOperator` abstraction so it can be replaced with an in-memory implementation during unit tests. |
| NF-2 | All command execution is performed through the injected `ICommandRunner` abstraction so commands can be stubbed in unit tests. |
| NF-3 | The `dry_run` flag is a constructor parameter on the manager class, not a global setting. |
| NF-4 | Elevation detection uses the shared `ElevationHelper` from `automojo.sysman.linux.ubuntu.common`. |
| NF-5 | All public methods return a `RepoResult` data class; they do not print to stdout. |
| NF-6 | The module must be importable on non-Ubuntu systems without raising `ImportError` (all subprocess calls are deferred to runtime). |
