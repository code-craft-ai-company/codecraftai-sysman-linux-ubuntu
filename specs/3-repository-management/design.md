# Design

## Overview

The repository management feature is implemented as a single module,
`automojo.sysman.linux.ubuntu.repositorymanagement`, that exposes a
`UbuntuRepositoryManager` class.  This spec implements the
repository-management contract defined at
`automojo-sysman-common/specs/contracts/repository-management/contract.md`
for Ubuntu using `apt` tooling plus direct file management of
`/etc/apt/sources.list.d/`, `/etc/apt/keyrings/`, and
`/etc/apt/preferences.d/`.

The coordinator instantiates `UbuntuRepositoryManager` and calls its
methods to perform discrete maintenance actions.  All command
invocation is delegated to an `ICommandRunner`; all source-file /
keyring-file / preferences-file mutations are delegated to an
`IFileOperator`.  No `subprocess` calls or direct filesystem access
occur inside the module.

The feature works identically whether commands execute locally (via
`LocalCommandRunner` + `LocalFileOperator`) or remotely over SSH.
When using SSH, the coordinator can track reliability metrics and
apply execution aspects without any changes to the
`UbuntuRepositoryManager` code.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator (Local or SSH)       │
│  - Configures aspects (optional)                            │
│  - Injects into UbuntuRepositoryManager                     │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│           UbuntuRepositoryManager                           │
│  automojo.sysman.linux.ubuntu.repositorymanagement          │
│                                                             │
│  add_repository()       remove_repository()                 │
│  enable_repository()    disable_repository()                │
│  list_repositories()    get_repository()                    │
│  refresh_repositories() set_priority()                      │
│  import_gpg_key()                                           │
└──────────────┬──────────────────────────────┬───────────────┘
               │ runner                       │ file I/O
               ▼                              ▼
┌──────────────────────┐         ┌──────────────────────┐
│   ICommandRunner     │         │   IFileOperator      │
│   run(args, elev)    │         │   read / write /     │
│                      │         │   delete / exists    │
└──────────┬───────────┘         └──────────┬───────────┘
           │                                │
   ┌───────┴────────┐             ┌─────────┴────────┐
   ▼                ▼             ▼                  ▼
┌─────────────┐  ┌──────────────┐ ┌──────────────┐ ┌────────────┐
│LocalCommand │  │SSHCommand    │ │LocalFile     │ │SSHFile     │
│Runner       │  │Runner        │ │Operator      │ │Operator    │
└─────────────┘  └──────────────┘ └──────────────┘ └────────────┘
           │                                │
           └────────────┬───────────────────┘
                        │ optional
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect   - RetryAspect   - TimeoutAspect      │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `UbuntuRepositoryManager`

**Location:** `source/packages/automojo/sysman/linux/ubuntu/repositorymanagement.py`

| Method | Signature | Description |
|---|---|---|
| `add_repository` | `(spec) → UbuntuRepositoryActionResult` | Write source file or dispatch PPA add |
| `remove_repository` | `(name) → UbuntuRepositoryActionResult` | Delete source file or dispatch PPA remove |
| `enable_repository` | `(name) → UbuntuRepositoryActionResult` | Rename `.list.disabled` → `.list` |
| `disable_repository` | `(name) → UbuntuRepositoryActionResult` | Rename `.list` → `.list.disabled` |
| `list_repositories` | `() → list[UbuntuRepositoryInfo]` | Enumerate + `apt-cache policy` |
| `get_repository` | `(name) → UbuntuRepositoryInfo \| None` | Enumerate + filter |
| `refresh_repositories` | `(name=None) → UbuntuRepositoryActionResult` | `apt-get update` |
| `set_priority` | `(name, priority) → UbuntuRepositoryActionResult` | Write pinning file |
| `import_gpg_key` | `(key_source) → UbuntuRepositoryActionResult` | Fetch + `gpg --dearmor` + write keyring |

### `UbuntuRepositoryManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no mutations dispatch |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `repos_dir` | `str` | `"/etc/apt/sources.list.d"` | Source file directory |
| `keyrings_dir` | `str` | `"/etc/apt/keyrings"` | GPG keyring directory |
| `preferences_dir` | `str` | `"/etc/apt/preferences.d"` | Pinning file directory |

---

## Data Models

### Enums

```python
class UbuntuRepositoryActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"
```

### Dataclasses

```python
@dataclass
class UbuntuRepositorySpec:
    name: str
    display_name: str
    base_url: str | None
    mirror_list_url: str | None        # unused on Ubuntu; reserved for contract parity
    enabled: bool
    gpg_check: bool
    gpg_key: str | None
    priority: int | None
    channel: str | None                # suite, e.g., "jammy main" — component(s) included

@dataclass
class UbuntuRepositoryInfo:
    name: str
    display_name: str
    enabled: bool
    url: str | None
    priority: int | None

@dataclass
class UbuntuRepositoryActionResult:
    status: UbuntuRepositoryActionStatus
    name: str
    file_path: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

class UbuntuRepositoryQueryError(Exception):
    """Raised when list_repositories fails unexpectedly."""

class UbuntuRepositoryNotFoundError(Exception):
    """Raised when set_priority targets an unknown repository."""

class UbuntuRepositoryKeyFileNotFoundError(Exception):
    """Raised when import_gpg_key is given a missing local file path."""
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
        stdin: str | None = None,
    ) -> RunResult: ...
```

### `IFileOperator` (from `automojo-interfaces`)

```python
class IFileOperator(Protocol):
    def read_text(self, path: str, elevated: bool = False) -> str: ...
    def read_bytes(self, path: str, elevated: bool = False) -> bytes: ...
    def write_text(self, path: str, content: str, elevated: bool = False) -> None: ...
    def write_bytes(self, path: str, content: bytes, elevated: bool = False) -> None: ...
    def delete(self, path: str, elevated: bool = False) -> None: ...
    def exists(self, path: str, elevated: bool = False) -> bool: ...
    def listdir(self, path: str, elevated: bool = False) -> list[str]: ...
```

---

## Repository-Name Mapping

| Contract field | Ubuntu mapping | Notes |
|---|---|---|
| `name` | filename stem of `.list` under `sources.list.d/` OR `ppa:user/repo` | Also used as stem for the keyring filename and pinning filename. |
| `display_name` | Informational; stored in the source file comment | Not parsed back. |
| `base_url` | URL field of the source line | First URL in the file becomes `UbuntuRepositoryInfo.url`. |
| `mirror_list_url` | N/A on Ubuntu | Accepted but ignored; documented in design. |
| `enabled` | `.list` → True; `.list.disabled` → False | — |
| `gpg_check` | Always honored; `signed-by` is always rendered when `gpg_key` is set | — |
| `gpg_key` | URL or local path consumed by `import_gpg_key` | — |
| `priority` | Pinning file `/etc/apt/preferences.d/<name>.pref` | Set via `set_priority`. |
| `channel` | Suite + component(s) as a single whitespace-joined string | e.g., `"jammy main universe"` |

---

## Command Mappings

### Write Commands

| Operation | Command |
|---|---|
| Add PPA | `add-apt-repository -y ppa:<user/repo>` |
| Remove PPA | `add-apt-repository -r -y ppa:<user/repo>` |
| Refresh all | `apt-get update` |
| Refresh one | `apt-get update -o Dir::Etc::sourcelist=... -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0` |
| Dearmor key (URL source) | `bash -c "curl -fsSL <url> \| gpg --dearmor -o <keyring-path>"` |
| Dearmor key (local file) | `gpg --dearmor --output <keyring-path>` with stdin = file bytes |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| Policy metadata | `apt-cache policy` |

### File Operations (via `IFileOperator`)

| Operation | Path |
|---|---|
| Write source file | `<repos_dir>/<name>.list` |
| Delete source file | `<repos_dir>/<name>.list` or `<name>.list.disabled` |
| Rename to disable | read `.list` + write `.list.disabled` + delete `.list` |
| Rename to enable | read `.list.disabled` + write `.list` + delete `.list.disabled` |
| List sources | `listdir(<repos_dir>)` |
| Write pinning | `<preferences_dir>/<name>.pref` |
| Write keyring | `<keyrings_dir>/<name>.gpg` (binary) |

---

## Output Parsing

### Source file parsing

Each `.list` file contains lines of the form:

```
deb [signed-by=/etc/apt/keyrings/foo.gpg] https://example.com/apt jammy main
deb-src https://example.com/apt jammy main
```

The parser:

1. Ignores blank lines and lines starting with `#`.
2. Tokenizes each line on whitespace; tokens starting with `[` and
   ending with `]` are `[options]` blocks that are preserved but not
   interpreted for the purpose of `UbuntuRepositoryInfo.url`.
3. The URL is the first token that starts with `http://` or
   `https://`.

### `apt-cache policy` parsing

`apt-cache policy` emits blocks separated by blank lines, each
containing a source with its priority.  The parser is permissive: it
extracts `<priority> <url> <suite>/<component> <arch> <origin>` and
uses the URL to correlate back to the source file (the first file
whose contents reference that URL).  This correlation is best-effort;
if no source file matches, the `UbuntuRepositoryInfo.priority` is
`None` even when `apt-cache policy` reports a value.

### Pinning file rendering

A pinning file content template:

```
Package: *
Pin: origin "<url-hostname>"
Pin-Priority: <priority>
```

The `<url-hostname>` is derived from the source file's first URL via
`urlparse(url).hostname`.

---

## Idempotency Guards

| Method | Guard | Source of truth |
|---|---|---|
| `add_repository` | Source file byte-identical to rendered | File read |
| `remove_repository` | Neither `.list` nor `.list.disabled` exists | `IFileOperator.exists` |
| `enable_repository` | `.list` already exists | `IFileOperator.exists` |
| `disable_repository` | `.list.disabled` already exists | `IFileOperator.exists` |
| `set_priority` | Pinning file byte-identical to rendered | File read |
| `import_gpg_key` | Keyring file byte-identical to dearmored content | File read |

`refresh_repositories` has no guard — it is always executed.
`list_repositories` / `get_repository` are reads.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `UbuntuRepositoryQueryError` | `apt-cache policy` returns non-zero |
| `UbuntuRepositoryNotFoundError` | `set_priority` for an unknown repo |
| `UbuntuRepositoryKeyFileNotFoundError` | `import_gpg_key` with a missing local file |

All other write failures are surfaced as
`UbuntuRepositoryActionResult` with `status=FAILED`.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Add-PPA Command Construction

*For any* spec with `name` starting `ppa:`, `add_repository()` should construct `add-apt-repository -y ppa:<user/repo>`.

**Validates: Requirements 1.1**

### Property 2: Add-File Source Rendering

*For any* non-PPA spec, `add_repository()` should render a `deb [signed-by=...] <url> <channel>` line and write it to `<repos_dir>/<name>.list`.

**Validates: Requirements 1.2**

### Property 3: Add-Repository Idempotency

*For any* non-PPA spec whose target file already contains byte-identical content, `add_repository()` should return `SKIPPED`.

**Validates: Requirements 1.3**

### Property 4: Add-Repository Rendering Is Deterministic

*For any* two invocations with the same `UbuntuRepositorySpec`, the rendered file content should be byte-identical.

**Validates: Requirements 1.2, 1.3**

### Property 5: Remove-PPA Command Construction

*For any* `remove_repository(name)` with `name` starting `ppa:`, the manager should dispatch `add-apt-repository -r -y ppa:...`.

**Validates: Requirements 2.1**

### Property 6: Remove-File Deletes Correct File

*For any* non-PPA `remove_repository(name)`, the manager should delete `<repos_dir>/<name>.list` OR `<repos_dir>/<name>.list.disabled`, whichever exists.

**Validates: Requirements 2.2, 2.4**

### Property 7: Remove-Repository Idempotency

*For any* `remove_repository(name)` where neither file variant exists, the result should be `SKIPPED`.

**Validates: Requirements 2.3**

### Property 8: Enable-Repository Renames Disabled File

*For any* repository whose `<repos_dir>/<name>.list.disabled` exists, `enable_repository()` should read it, write the contents to `<repos_dir>/<name>.list`, and delete the `.disabled` file.

**Validates: Requirements 3.1**

### Property 9: Enable-Repository Skipped When Already Enabled

*For any* repository whose `<repos_dir>/<name>.list` already exists, `enable_repository()` should return `SKIPPED`.

**Validates: Requirements 3.2**

### Property 10: Enable-Repository Not-Found Failure

*For any* repository where neither file variant exists, `enable_repository()` should return `FAILED` with `message="repository_not_found"`.

**Validates: Requirements 3.3**

### Property 11: Disable-Repository Renames Enabled File

*For any* repository whose `<repos_dir>/<name>.list` exists, `disable_repository()` should rename it to `.list.disabled`.

**Validates: Requirements 4.1**

### Property 12: Disable-Repository Skipped When Already Disabled

*For any* repository whose `.list.disabled` already exists, `disable_repository()` should return `SKIPPED`.

**Validates: Requirements 4.2**

### Property 13: Disable-Repository Not-Found Failure

*For any* repository where neither file variant exists, `disable_repository()` should return `FAILED` with `message="repository_not_found"`.

**Validates: Requirements 4.3**

### Property 14: List-Repositories Enumerates Files

*For any* call to `list_repositories()`, the manager should enumerate `<repos_dir>` and produce one entry per `.list` / `.list.disabled` file.

**Validates: Requirements 5.1**

### Property 15: List-Repositories Enabled Flag Derived From Suffix

*For any* file ending `.list`, `enabled=True`; for `.list.disabled`, `enabled=False`.

**Validates: Requirements 5.3**

### Property 16: List-Repositories Malformed Line Skip

*For any* source file containing a malformed line, `list_repositories()` should silently skip the malformed line but still emit the entry.

**Validates: Requirements 5.5**

### Property 17: List-Repositories Empty Dir Skips Runner

*For any* empty `<repos_dir>`, `list_repositories()` should return `[]` without invoking the runner.

**Validates: Requirements 5.4**

### Property 18: List-Repositories Error Raises

*For any* non-zero exit from `apt-cache policy`, `list_repositories()` should raise `UbuntuRepositoryQueryError`.

**Validates: Requirements 5.2**

### Property 19: Get-Repository Returns None For Unknown

*For any* unknown name, `get_repository()` should return `None`.

**Validates: Requirements 6.2**

### Property 20: Refresh-All Command Construction

*For any* `refresh_repositories(name=None)`, the manager should dispatch `apt-get update`.

**Validates: Requirements 7.1**

### Property 21: Refresh-One Command Construction

*For any* `refresh_repositories(name=<name>)`, the manager should dispatch `apt-get update -o Dir::Etc::sourcelist=... -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0`.

**Validates: Requirements 7.2**

### Property 22: Refresh No Idempotency Guard

*For any* `refresh_repositories()` call in non-dry-run mode, the runner should be invoked — no `SKIPPED`.

**Validates: Requirements 7.6**

### Property 23: Set-Priority Not-Found Raises

*For any* repository whose neither file variant exists, `set_priority()` should raise `UbuntuRepositoryNotFoundError`.

**Validates: Requirements 8.1**

### Property 24: Set-Priority Idempotency

*For any* repository whose pinning file is byte-identical to the rendered content, `set_priority()` should return `SKIPPED`.

**Validates: Requirements 8.2**

### Property 25: Set-Priority File Rendering

*For any* `set_priority(name, priority)`, the rendered content should contain `Pin-Priority: <priority>` and `Pin: origin "<hostname>"` derived from the source URL.

**Validates: Requirements 8.3**

### Property 26: Import-Key URL Dispatch

*For any* `import_gpg_key(url)` where `url` starts `http://` or `https://`, the manager should dispatch `bash -c "curl -fsSL <url> | gpg --dearmor -o <keyring>"`.

**Validates: Requirements 9.1**

### Property 27: Import-Key Missing Local File Raises

*For any* `import_gpg_key(path)` where `path` is local and does not exist, `UbuntuRepositoryKeyFileNotFoundError` should be raised.

**Validates: Requirements 9.2**

### Property 28: Import-Key Idempotency

*For any* `import_gpg_key` whose target keyring file already has the identical dearmored content, the result should be `SKIPPED`.

**Validates: Requirements 9.4**

### Property 29: Dry-Run Short-Circuit For All Writes

*For any* write operation with `dry_run=True`, the runner's write commands and the file operator's write/delete calls should never be invoked, and the result should be `DRY_RUN`.

**Validates: Requirements 1.6, 2.7, 3.6, 4.6, 7.5, 8.6, 9.7, 11.1**

### Property 30: Dry-Run Precondition Exceptions Still Raise

*For any* dry-run `import_gpg_key` with a missing local file, or dry-run `set_priority` against an unknown repository, the appropriate exception should still be raised.

**Validates: Requirements 11.4**

### Property 31: Elevation For Writes

*For any* write operation with `elevated=True` in the config, the runner / file operator should be invoked with `elevated=True`.

**Validates: Requirements 10.1**

### Property 32: No Elevation For Reads

*For any* read operation (`list_repositories`, `get_repository`), the runner / file operator should never be invoked with `elevated=True`.

**Validates: Requirements 10.2**

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/
├── __init__.py
└── repositorymanagement.py    ← UbuntuRepositoryManager, config,
                                  models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/ubuntu/
    └── test_repositorymanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.linux.ubuntu.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.linux.ubuntu.fileops import LocalFileOperator, LocalFileOperatorConfig
from automojo.sysman.linux.ubuntu.repositorymanagement import (
    UbuntuRepositoryManager,
    UbuntuRepositoryManagerConfig,
    UbuntuRepositorySpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_op = LocalFileOperator(LocalFileOperatorConfig())

config = UbuntuRepositoryManagerConfig(elevated=True, dry_run=False)
repo_mgr = UbuntuRepositoryManager(runner=runner, file_operator=file_op, config=config)

# Add a standard apt source
spec = UbuntuRepositorySpec(
    name="docker",
    display_name="Docker CE apt repository",
    base_url="https://download.docker.com/linux/ubuntu",
    mirror_list_url=None,
    enabled=True,
    gpg_check=True,
    gpg_key="https://download.docker.com/linux/ubuntu/gpg",
    priority=None,
    channel="jammy stable",
)
result = repo_mgr.add_repository(spec=spec)
print(f"Add: {result.status}")

# Add a PPA
ppa_spec = UbuntuRepositorySpec(
    name="ppa:deadsnakes/ppa",
    display_name="Deadsnakes Python",
    base_url=None,
    mirror_list_url=None,
    enabled=True,
    gpg_check=True,
    gpg_key=None,
    priority=None,
    channel=None,
)
repo_mgr.add_repository(spec=ppa_spec)

# Refresh
repo_mgr.refresh_repositories()

# List
for info in repo_mgr.list_repositories():
    print(f"{info.name} enabled={info.enabled} url={info.url}")
```

### SSH Execution with Aspects

```python
from automojo.sysman.linux.ubuntu.runners import SSHCommandRunner, SSHRunnerConfig
from automojo.sysman.linux.ubuntu.fileops import SSHFileOperator, SSHFileOperatorConfig
from automojo.sysman.linux.ubuntu.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from automojo.sysman.linux.ubuntu.storage import SQLiteStorage
from automojo.sysman.linux.ubuntu.repositorymanagement import (
    UbuntuRepositoryManager,
    UbuntuRepositoryManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="ubuntu-node-01", username="admin"))
file_op = SSHFileOperator(SSHFileOperatorConfig(host="ubuntu-node-01", username="admin"))

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3, retryable_exit_codes=[100])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=600.0)))

repo_mgr = UbuntuRepositoryManager(
    runner=runner,
    file_operator=file_op,
    config=UbuntuRepositoryManagerConfig(elevated=True),
)

repo_mgr.refresh_repositories()
repo_mgr.set_priority(name="docker", priority=600)
```

### Dry-Run Mode

```python
config = UbuntuRepositoryManagerConfig(elevated=True, dry_run=True)
repo_mgr = UbuntuRepositoryManager(runner=runner, file_operator=file_op, config=config)

result = repo_mgr.disable_repository(name="docker")
assert result.status == UbuntuRepositoryActionStatus.DRY_RUN
print(result.command)      # argv that would have run (or None for file-only)
print(result.file_path)    # /etc/apt/sources.list.d/docker.list.disabled
```

---

## Testing Strategy

### Unit Testing

- Tests use a `MockCommandRunner` and `MockFileOperator`.
- Source file fixtures drive `list_repositories` parser tests
  covering well-formed, malformed, and empty-dir cases.
- `apt-cache policy` fixture strings drive the priority-correlation
  branch.
- Dry-run tests assert zero `apt-get update` invocations and zero
  `write_text` / `delete` calls.
- Idempotency tests overwrite the mock file-operator's read response
  with the expected rendered bytes so that the byte-identical path
  triggers.
- PPA tests assert `add-apt-repository` argv shape.
- `set_priority` tests assert the raised
  `UbuntuRepositoryNotFoundError` when neither file variant exists.
- `import_gpg_key` tests cover the URL path, the local-file path
  with existence check, the missing-local-file raise, and the
  keyring-identical idempotency branch.
- No test requires root access or a live apt installation.

### Property-Based Testing

The implementation uses `hypothesis` for property-based testing.  Each
correctness property is implemented as a property-based test that:

- Generates random but valid inputs (name strings, URLs, channels,
  priorities).
- Executes the operation under test against mocks.
- Verifies the property holds across all generated inputs.
- Runs at least 100 iterations per property test.

Property-based tests are tagged as
`# Feature: repository-management (ubuntu), Property N: <property description>`.

---

## Scope Mapping

| Contract verb | Ubuntu implementation | Notes |
|---|---|---|
| `add_repository` | File write OR `add-apt-repository -y` for PPA | `gpg_key` triggers auto-import. |
| `remove_repository` | File delete OR `add-apt-repository -r -y` | Handles `.list.disabled`. |
| `enable_repository` | Rename `.list.disabled` → `.list` | — |
| `disable_repository` | Rename `.list` → `.list.disabled` | — |
| `list_repositories` | Enumerate + `apt-cache policy` | Priority correlation best-effort. |
| `get_repository` | Enumerate + filter | `None` on miss. |
| `refresh_repositories` | `apt-get update` [single-source variant] | No idempotency guard. |
| `set_priority` | Write pinning file | Raises for unknown repo. |
| `import_gpg_key` | `curl | gpg --dearmor` OR local file dearmor | Raises for missing local file. |

---

## Extensions beyond the contract

- **Rename-on-disable/enable.**  Ubuntu has no native "disabled" flag
  inside the source file, so the manager uses the convention of
  renaming to `.list.disabled`; this is a common Ubuntu idiom that
  `apt` does not consult (the file-extension filter makes `apt`
  ignore non-`.list` files).
- **Pinning file for priority.**  Contract's abstract `priority`
  maps to apt pinning files under `/etc/apt/preferences.d/`.
- **Keyring file import.**  GPG keys are stored as binary dearmored
  files under `/etc/apt/keyrings/<name>.gpg` and referenced by the
  source line's `[signed-by=...]` option — the modern, deprecated
  `apt-key` workflow is NOT used.
- **IFileOperator usage.**  Ubuntu uses `IFileOperator` for all
  source-file, keyring-file, and preferences-file mutations
  (contract-permitted extension).

## Out of Scope

- **deb822-format advanced fields** (Architectures, Signed-By with
  multi-key, URIs-list) — the one-line `deb` form is sufficient for
  the contract's field set.  A future enhancement may add deb822
  rendering.
- **Repository mirroring / local mirror hosting** (`apt-mirror`) —
  separate feature.
- **Client-certificate authentication** — out of scope.
- **Subscription-manager / entitlement integration** — RHEL-specific.
- **`Homebrew bundle` equivalent** — not applicable to apt.
- **Mass-apply from a structured policy document** (YAML / TOML) —
  out of scope.
