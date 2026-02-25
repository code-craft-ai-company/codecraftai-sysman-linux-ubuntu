# Repository Configuration — Design

## Module Location

| Item | Path |
|------|------|
| Implementation | `source/packages/automojo/sysman/linux/ubuntu/repoconfig/` |
| Unit tests | `source/testroots/testbase/automojo/tests/sysman/linux/ubuntu/repoconfig/` |
| Python package | `automojo.sysman.linux.ubuntu.repoconfig` |

---

## Architecture Overview

```
RepoConfigManager
    ├── IFileOperator        (injected — file read/write/delete)
    ├── ICommandRunner       (injected — subprocess execution)
    ├── ElevationHelper      (shared, from automojo.sysman.linux.ubuntu.common)
    └── dry_run: bool        (constructor flag)
```

`RepoConfigManager` is the single public entry point.  It accepts the two
interface dependencies at construction time so that unit tests can substitute
in-memory fakes without touching the filesystem or spawning real processes.

---

## Data Models

### `RepoEntry`

```python
@dataclass
class RepoEntry:
    name: str           # Stem of the .list / .sources file (e.g. "docker")
    uri: str            # Repository URI (e.g. "https://download.docker.com/linux/ubuntu")
    suite: str          # Distribution suite (e.g. "jammy")
    components: list[str]  # e.g. ["stable"]
    enabled: bool       # False if the deb line is commented out
    file_path: str      # Absolute path to the .list or .sources file
    signed_by: str | None  # Path to keyring file, if present in the source entry
    format: str         # "one-line" or "deb822"
```

### `RepoPolicyInfo`

```python
@dataclass
class RepoPolicyInfo:
    name: str           # Repository identifier
    uri: str            # Repository URI as reported by apt-cache policy
    priority: int       # Pin priority (default 500)
    origin: str | None  # Origin field if present
```

### `RepoResult`

```python
@dataclass
class RepoResult:
    changed: bool       # True if a file was written/removed or command run
    path: str | None    # File path affected, if applicable
    command: str | None # Command string that was (or would have been) run
    stdout: str | None  # Command stdout, if applicable
    stderr: str | None  # Command stderr, if applicable
    dry_run: bool       # Echoes the dry_run flag for the caller's convenience
    message: str        # Human-readable summary
```

### Exceptions

```python
class RepoCommandError(RuntimeError):
    """Raised when a repository management command exits non-zero."""

class RepositoryNotFoundError(LookupError):
    """Raised when an operation targets a .list/.sources file that does not exist."""

class ElevationRequiredError(PermissionError):
    """Raised when the process lacks privileges to write to /etc/apt/."""
```

---

## File Format Support

### One-line format (`.list`)

```
deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu jammy stable
```

- Disable: prepend `# ` to each `deb` / `deb-src` line.
- Enable: remove the leading `# ` from each commented-out `deb` / `deb-src` line.

### DEB822 format (`.sources`)

```
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: jammy
Components: stable
Enabled: yes
Signed-By: /etc/apt/keyrings/docker.gpg
```

- Disable: set `Enabled: no`.
- Enable: set `Enabled: yes` (or add the line if absent).

The manager auto-detects the format by file extension when reading existing files.
When writing new entries, the format is determined by the `RepoEntry.format` field
(default: `"one-line"`).

---

## Command Sequences

### `add_repository(entry: RepoEntry) -> RepoResult`

1. Check for elevation via `ElevationHelper.check()`.
2. Compute destination path:
   - one-line → `/etc/apt/sources.list.d/<entry.name>.list`
   - deb822   → `/etc/apt/sources.list.d/<entry.name>.sources`
3. If the file already exists, read current content and compare.
   - Identical → return `RepoResult(changed=False)`.
4. If `dry_run`, return `RepoResult(changed=False, dry_run=True, message=<description>)`.
5. Render the entry string (one-line or DEB822).
6. `IFileOperator.write(path, content)`.
7. Return `RepoResult(changed=True, path=path)`.

### `remove_repository(name: str) -> RepoResult`

1. Check for elevation.
2. Resolve file path (try `.list` then `.sources`).
3. If neither exists → return `RepoResult(changed=False)`.
4. If `dry_run` → return description.
5. `IFileOperator.delete(path)`.
6. Return `RepoResult(changed=True, path=path)`.

### `enable_repository(name: str) -> RepoResult`

1. Resolve file path; raise `RepositoryNotFoundError` if absent.
2. Read content.
3. Apply enable transformation (uncomment / set `Enabled: yes`).
4. If content unchanged → return `RepoResult(changed=False)`.
5. If `dry_run` → return description.
6. `IFileOperator.write(path, new_content)`.
7. Return `RepoResult(changed=True, path=path)`.

### `disable_repository(name: str) -> RepoResult`

1. Resolve file path; raise `RepositoryNotFoundError` if absent.
2. Read content.
3. Apply disable transformation (comment / set `Enabled: no`).
4. If content unchanged → return `RepoResult(changed=False)`.
5. If `dry_run` → return description.
6. `IFileOperator.write(path, new_content)`.
7. Return `RepoResult(changed=True, path=path)`.

### `import_key(key_data: str, keyring_name: str) -> RepoResult`

1. Check for elevation.
2. Compute keyring path: `/etc/apt/keyrings/<keyring_name>.gpg`.
3. Ensure `/etc/apt/keyrings/` exists via `IFileOperator.makedirs()`.
4. Run `gpg --dearmor` on `key_data` via `ICommandRunner.run(["gpg", "--dearmor"])` with
   `key_data` as stdin; capture binary stdout as the dearmored keyring bytes.
5. If the keyring file already exists with identical content → return `RepoResult(changed=False)`.
6. If `dry_run` → return description.
7. `IFileOperator.write_bytes(keyring_path, dearmored_bytes)`.
8. Return `RepoResult(changed=True, path=keyring_path)`.

### `refresh_index() -> RepoResult`

1. Check for elevation.
2. If `dry_run` → return `RepoResult(changed=False, command="apt-get update", dry_run=True)`.
3. `ICommandRunner.run(["apt-get", "update"])`.
4. On non-zero exit code → raise `RepoCommandError(stderr)`.
5. Return `RepoResult(changed=True, stdout=stdout, stderr=stderr)`.

### `list_repositories() -> list[RepoEntry]`

1. `IFileOperator.listdir("/etc/apt/sources.list.d/")`.
2. For each file with `.list` or `.sources` extension, parse into one or more `RepoEntry`
   objects.
3. Sort by `RepoEntry.name` ascending.
4. Return list (empty list if directory is empty or no valid entries found).

### `get_repo_status(name: str) -> RepoPolicyInfo | None`

1. `ICommandRunner.run(["apt-cache", "policy"])`.
2. Parse output to locate the repository matching `name` (by URI or label).
3. Return `RepoPolicyInfo` if found, `None` otherwise.
4. On non-zero exit code → raise `RepoCommandError(stderr)`.

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/repoconfig/
    __init__.py          # exports RepoConfigManager, RepoEntry, RepoPolicyInfo,
                         #         RepoResult, RepoCommandError,
                         #         RepositoryNotFoundError, ElevationRequiredError
    _manager.py          # RepoConfigManager class
    _models.py           # RepoEntry, RepoPolicyInfo, RepoResult dataclasses
    _exceptions.py       # RepoCommandError, RepositoryNotFoundError,
                         # ElevationRequiredError
    _parsers.py          # one-line and DEB822 format parsers/renderers
```

### Test Layout

```
source/testroots/testbase/automojo/tests/sysman/linux/ubuntu/repoconfig/
    __init__.py
    test_manager.py      # RepoConfigManager unit tests (faked I/O and commands)
    test_parsers.py      # Parser / renderer round-trip tests
    test_models.py       # Dataclass field and default tests
```

---

## Dependency Injection Wiring

```python
# Production wiring (simplified)
from automojo.sysman.linux.ubuntu.repoconfig import RepoConfigManager
from automojo.interfaces import DefaultFileOperator, DefaultCommandRunner

manager = RepoConfigManager(
    file_operator=DefaultFileOperator(),
    command_runner=DefaultCommandRunner(),
    dry_run=False,
)

# Test wiring
from automojo.interfaces.testing import FakeFileOperator, FakeCommandRunner

manager = RepoConfigManager(
    file_operator=FakeFileOperator(),
    command_runner=FakeCommandRunner(responses={...}),
    dry_run=False,
)
```

---

## Key Ubuntu-Specific Notes

1. **No zones concept** — unlike `firewalld`, APT repositories have no zone equivalent.
   All scope is implicit in the source file.

2. **GPG keyring location** — keys are stored as dearmored binary files under
   `/etc/apt/keyrings/` (not the legacy `/etc/apt/trusted.gpg.d/`), following current
   Ubuntu best practice.

3. **Two source file formats** — the manager must handle both the legacy one-line `.list`
   format and the modern DEB822 `.sources` format, which are both in active use on
   Ubuntu systems.

4. **`apt-get update` instead of `dnf makecache`** — the refresh command has no
   `--repo=<name>` scoping equivalent; it always refreshes all configured sources.

5. **`apt-cache policy` instead of `dnf repolist`** — the status query uses
   `apt-cache policy` to retrieve pinning priorities and origin information.
