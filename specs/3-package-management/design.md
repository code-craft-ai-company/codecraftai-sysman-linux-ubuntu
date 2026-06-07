# Design

## Overview

The package-management feature is implemented as a single module,
`ccai.sysman.linux.ubuntu.packagemanagement`, that exposes a
`UbuntuPackageManager` class.  This spec implements the package-management contract
defined at `codecraftai-sysman-common/specs/contracts/package-management/contract.md`
for Ubuntu using `apt-get` for writes and `dpkg` / `dpkg-query` / `apt list` for
reads.  The coordinator instantiates the manager with a command runner and a
config, then calls install/remove/upgrade verbs plus a read-only query surface.

All system interaction happens through `ICommandRunner`.  Idempotency guards rely on
`dpkg -l` (is-installed check) and `dpkg -s` (version check).  Non-interactive
execution is enforced by passing `DEBIAN_FRONTEND=noninteractive` in the runner's
env dict on writes.

The feature works identically whether commands execute locally (via
`UbuntuLocalCommandRunner`) or remotely over SSH (via `UbuntuSSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md) for
details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into UbuntuPackageManager                 │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuPackageManager                             │
│  ccai.sysman.linux.ubuntu.packagemanagement             │
│                                                             │
│  install_package()  remove_package()  upgrade_package()     │
│  upgrade_all()      is_installed()                          │
│  get_package_info() list_installed()                        │
│  list_upgradable()  install_local()                         │
│  refresh_metadata()                                         │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `UbuntuPackageManager`

**Location:** `source/packages/ccai/sysman/linux/ubuntu/packagemanagement.py`

| Method | Signature | Description |
|---|---|---|
| `install_package` | `(name, version=None) → UbuntuPackageActionResult` | `apt-get install -y <name>[=<version>]` |
| `remove_package` | `(name) → UbuntuPackageActionResult` | `apt-get remove -y <name>` |
| `upgrade_package` | `(name, version=None) → UbuntuPackageActionResult` | `apt-get install -y --only-upgrade <name>[=<version>]` |
| `upgrade_all` | `() → UbuntuPackageActionResult` | `apt-get upgrade -y` |
| `is_installed` | `(name) → bool` | `dpkg -l <name>` → status byte check |
| `get_package_info` | `(name) → UbuntuPackageInfo \| None` | `dpkg -s <name>` parsed |
| `list_installed` | `() → list[UbuntuPackageInfo]` | `dpkg-query -W -f=<fmt>` parsed |
| `list_upgradable` | `() → list[UbuntuPackageUpdateInfo]` | `apt list --upgradable` parsed |
| `install_local` | `(filepath) → UbuntuPackageActionResult` | `apt-get install -y <filepath>` |
| `refresh_metadata` | `() → UbuntuPackageActionResult` | `apt-get update` |

### `UbuntuPackageManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no write dispatches |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `assume_yes` | `bool` | `True` | When `True`, apt-get writes include `-y` |
| `extra_env` | `dict[str,str] \| None` | `None` | Extra env merged into writes; defaults to injecting `DEBIAN_FRONTEND=noninteractive` when `None` |

---

## Data Models

### Enum

```python
class UbuntuPackageActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"
```

### Dataclasses

```python
@dataclass
class UbuntuPackageActionResult:
    status: UbuntuPackageActionStatus
    package_name: str
    version: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    command: list[str] | None = None

@dataclass
class UbuntuPackageInfo:
    name: str
    version: str
    release: str        # empty on Ubuntu — apt has no release/revision split
    arch: str
    origin: str | None  # apt source / component, or "apt"

@dataclass
class UbuntuPackageUpdateInfo:
    name: str
    installed_version: str
    available_version: str
    arch: str

class UbuntuPackageQueryError(Exception):
    """Raised when list_installed, list_upgradable, or get_package_info fails unexpectedly."""

class UbuntuPackageFileNotFoundError(Exception):
    """Raised when install_local() is called with a non-existent path."""
```

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        elevated: bool = False,
        env: dict[str, str] | None = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

---

## Command Mappings

### Write Commands

| Operation | Command | Env |
|---|---|---|
| Install | `apt-get install -y <name>` | `DEBIAN_FRONTEND=noninteractive` |
| Install at version | `apt-get install -y <name>=<version>` | `DEBIAN_FRONTEND=noninteractive` |
| Remove | `apt-get remove -y <name>` | `DEBIAN_FRONTEND=noninteractive` |
| Upgrade | `apt-get install -y --only-upgrade <name>` | `DEBIAN_FRONTEND=noninteractive` |
| Upgrade at version | `apt-get install -y --only-upgrade <name>=<version>` | `DEBIAN_FRONTEND=noninteractive` |
| Upgrade all | `apt-get upgrade -y` | `DEBIAN_FRONTEND=noninteractive` |
| Install local | `apt-get install -y <filepath>` | `DEBIAN_FRONTEND=noninteractive` |
| Refresh | `apt-get update` | `DEBIAN_FRONTEND=noninteractive` |

When `config.assume_yes=False`, the `-y` flag is omitted.  All writes pass
`elevated=True` when `config.elevated=True`.

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| Is installed | `dpkg -l <name>` |
| Package info | `dpkg -s <name>` |
| List installed | `dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\t${Status}\n'` |
| List upgradable | `apt list --upgradable` |

---

## Output Parsing

### `dpkg -l` Output

```
Desired=Unknown/Install/Remove/Purge/Hold
| Status=Not/Inst/Conf-files/Unpacked/halF-conf/Half-inst/trig-aWait/Trig-pend
|/ Err?=(none)/Reinst-required (Status,Err: uppercase=bad)
||/ Name           Version      Architecture Description
+++-==============-============-============-==================================
ii  nginx          1.18.0-0ubun amd64        small, powerful, scalable web/proxy server
```

The parser locates the first row starting with two non-space status bytes
(e.g. `ii`, `rc`, `un`) and returns `True` iff the status bytes are `ii`.  Any
other status → `False`.

### `dpkg -s` Output

```
Package: nginx
Status: install ok installed
Priority: optional
Section: web
Installed-Size: 540
Maintainer: Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>
Architecture: amd64
Source: nginx (1.18.0-0ubuntu1)
Version: 1.18.0-0ubuntu1.4
Depends: nginx-core | nginx-full | nginx-light | nginx-extras
Description: small, powerful, scalable web/proxy server
 nginx ("engine X") is a high-performance web and reverse proxy server
 …
```

Parsed by splitting on lines, capturing `<key>: <value>` pairs until the first
continuation line (begins with a space).  `Package` → `name`, `Version` →
`version`, `Architecture` → `arch`, `Source` → `origin` (fallback `"apt"` when
absent).

### `dpkg-query -W` Output

```
adduser	3.118ubuntu5	all	install ok installed
apt	2.4.12	amd64	install ok installed
base-files	12ubuntu4.6	amd64	install ok installed
```

Tab-separated; the parser filters rows whose `Status` ends in `installed`.

### `apt list --upgradable` Output

```
Listing... Done
curl/jammy-updates,jammy-security 7.81.0-1ubuntu1.16 amd64 [upgradable from: 7.81.0-1ubuntu1.14]
libssl3/jammy-updates,jammy-security 3.0.2-0ubuntu1.15 amd64 [upgradable from: 3.0.2-0ubuntu1.13]
```

Regex: `^(?P<name>\S+)/\S+\s+(?P<avail>\S+)\s+(?P<arch>\S+)\s+\[upgradable from:
\s*(?P<inst>\S+)\]$`.  The `Listing…` header is skipped.

---

## Idempotency Guards

| Method | Guard | Expected state to skip |
|---|---|---|
| `install_package(name, None)` | `is_installed(name)` is `True` | already installed |
| `install_package(name, V)` | `get_package_info(name).version == V` | already at target version |
| `remove_package(name)` | `is_installed(name)` is `False` | already removed |
| `upgrade_package(name, None)` | `is_installed(name)` is `False` → SKIPPED (nothing to upgrade) |
| `upgrade_package(name, V)` | installed version equals/exceeds `V` | already at target version |
| `upgrade_all` | never skips | — |
| `install_local` | never skips (the file is presumed to be a package the caller wants re-installed) | — |
| `refresh_metadata` | never skips (contract: operational, no freshness heuristic) | — |

All idempotency probes go through the read paths (`dpkg -l`, `dpkg -s`) and do NOT
pass `elevated=True`.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `UbuntuPackageQueryError` | `list_installed`, `list_upgradable`, or `get_package_info` fails unexpectedly (not the informational "not installed" case) |
| `UbuntuPackageFileNotFoundError` | `install_local()` called with a path that does not exist on disk |

All write failures surface as `UbuntuPackageActionResult(status=FAILED, …)`.

---

## Correctness Properties

### Property 1: Install Command Construction

*For any* `install_package(name, None)` whose guard does not skip, the runner is
invoked with `["apt-get", "install", "-y", name]` and env containing
`DEBIAN_FRONTEND=noninteractive`.

**Validates: Requirements 1.1, 1.8**

### Property 2: Install Versioned Command Construction

*For any* `install_package(name, "1.2.3")` whose guard does not skip, the runner is
invoked with `["apt-get", "install", "-y", "name=1.2.3"]`.

**Validates: Requirements 1.1**

### Property 3: Install Idempotency (Any Version)

*For any* already-installed package, `install_package(name, None)` returns
`SKIPPED` without dispatching the install command.

**Validates: Requirements 1.3**

### Property 4: Install Idempotency (Exact Version)

*For any* package installed at version `V`, `install_package(name, V)` returns
`SKIPPED` without dispatching the install command.

**Validates: Requirements 1.4**

### Property 5: Assume-Yes Off Omits -y

*For any* install call with `config.assume_yes=False`, the runner argv does NOT
contain `-y`.

**Validates: Requirements 1.2**

### Property 6: Remove Command Construction

*For any* `remove_package(name)` whose guard does not skip, the runner is invoked
with `["apt-get", "remove", "-y", name]`.

**Validates: Requirements 2.1**

### Property 7: Remove Idempotency

*For any* package not installed, `remove_package(name)` returns `SKIPPED`.

**Validates: Requirements 2.2**

### Property 8: Upgrade Command Construction

*For any* `upgrade_package(name, None)` whose guard does not skip, the runner is
invoked with `["apt-get", "install", "-y", "--only-upgrade", name]`.

**Validates: Requirements 3.1**

### Property 9: Upgrade Not Installed Skipped

*For any* `upgrade_package(name)` where the package is not installed, the result
is `SKIPPED` with `message="package is not installed; nothing to upgrade"`.

**Validates: Requirements 3.2**

### Property 10: Upgrade Already At Version

*For any* `upgrade_package(name, V)` where the installed version equals or
exceeds `V`, the result is `SKIPPED`.

**Validates: Requirements 3.3**

### Property 11: Upgrade All Command

*For any* `upgrade_all()`, the runner is invoked with
`["apt-get", "upgrade", "-y"]`.

**Validates: Requirements 4.1**

### Property 12: Upgrade All Never Skips

*For any* `upgrade_all()`, the idempotency guard does not skip.

**Validates: Requirements 4.5**

### Property 13: Is Installed True

*For any* package whose `dpkg -l` row begins with `ii`, `is_installed` returns
`True`.

**Validates: Requirements 5.2**

### Property 14: Is Installed False

*For any* package whose `dpkg -l` exit is non-zero or whose row does not begin
with `ii`, `is_installed` returns `False`.

**Validates: Requirements 5.3**

### Property 15: Package Info Parse

*For any* `dpkg -s` output with `Package`, `Version`, `Architecture` fields,
`get_package_info` returns a `UbuntuPackageInfo` with those values.

**Validates: Requirements 5.4**

### Property 16: Package Info Not Installed

*For any* `dpkg -s` failure matching the "not installed and no information"
pattern, `get_package_info` returns `None` without raising.

**Validates: Requirements 5.5**

### Property 17: Package Info Unexpected Error

*For any* other `dpkg -s` failure, `UbuntuPackageQueryError` is raised.

**Validates: Requirements 5.6**

### Property 18: List Installed Parse

*For any* valid `dpkg-query -W` output, `list_installed` returns one
`UbuntuPackageInfo` per `installed`-status row.

**Validates: Requirements 6.2**

### Property 19: List Installed Error

*For any* failure of `dpkg-query`, `UbuntuPackageQueryError` is raised.

**Validates: Requirements 6.3**

### Property 20: List Upgradable Parse

*For any* valid `apt list --upgradable` output, `list_upgradable` returns one
`UbuntuPackageUpdateInfo` per upgradable line.

**Validates: Requirements 7.2**

### Property 21: List Upgradable Empty

*For any* output that is only the `Listing…` header, `list_upgradable` returns
an empty list.

**Validates: Requirements 7.4**

### Property 22: Install Local Missing File

*For any* `install_local(path)` where `path` does not exist,
`UbuntuPackageFileNotFoundError` is raised and the runner is NOT invoked.

**Validates: Requirements 8.2**

### Property 23: Install Local Command

*For any* `install_local(path)` where the file exists and we are not in dry-run,
the runner is invoked with `["apt-get", "install", "-y", path]`.

**Validates: Requirements 8.1**

### Property 24: Install Local Dry-Run After File Check

*For any* `install_local(path)` in dry-run mode with a non-existent file, the
result is still `UbuntuPackageFileNotFoundError` (dry-run does NOT bypass the
file-existence precondition).

**Validates: Requirements 8.2, 8.5**

### Property 25: Refresh Metadata Command

*For any* `refresh_metadata()`, the runner is invoked with
`["apt-get", "update"]`.

**Validates: Requirements 9.1**

### Property 26: Refresh Metadata Never Skips

*For any* `refresh_metadata()`, the idempotency guard does not skip.

**Validates: Requirements 9.5**

### Property 27: Writes Elevate When Configured

*For any* write with `config.elevated=True`, the runner is invoked with
`elevated=True`.

**Validates: Requirements 10.1**

### Property 28: Reads Do Not Elevate

*For any* read or idempotency-probe call, the runner is NEVER invoked with
`elevated=True`.

**Validates: Requirements 10.2, 10.3**

### Property 29: Dry-Run Safety (All Writes)

*For any* write verb with `config.dry_run=True`, the runner is NOT invoked and the
result has `status=DRY_RUN`.

**Validates: Requirements 1.7, 2.5, 3.6, 4.4, 8.5, 9.4**

### Property 30: Status Mapping

*For any* write command result from the runner, `status=SUCCESS` iff
`exit_code==0`; otherwise `status=FAILED` with captured fields.

**Validates: Requirements 1.5, 1.6, 2.3, 2.4, 3.4, 3.5, 4.2, 4.3, 8.3, 8.4, 9.2,
9.3**

---

## Module Layout

```
source/packages/ccai/sysman/linux/ubuntu/
├── __init__.py
└── packagemanagement.py       ← UbuntuPackageManager, config, models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/linux/ubuntu/
    └── test_packagemanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.linux.ubuntu.runners import (
    UbuntuLocalCommandRunner, UbuntuLocalRunnerConfig,
)
from ccai.sysman.linux.ubuntu.packagemanagement import (
    UbuntuPackageManager, UbuntuPackageManagerConfig,
)

runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
config = UbuntuPackageManagerConfig(elevated=True, dry_run=False, assume_yes=True)
pkg_mgr = UbuntuPackageManager(runner=runner, config=config)

refresh = pkg_mgr.refresh_metadata()
print(f"refresh: {refresh.status.value}")

result = pkg_mgr.install_package(name="nginx", version=None)
print(f"install nginx: {result.status.value} version={result.version}")
```

### Aspect Composition (SSH)

```python
from ccai.sysman.linux.ubuntu.runners import (
    UbuntuSSHCommandRunner, UbuntuSSHRunnerConfig,
)
from ccai.sysman.linux.ubuntu.aspects import (
    RetryAspect, RetryAspectConfig, TimeoutAspect, TimeoutAspectConfig,
)

runner = UbuntuSSHCommandRunner(
    UbuntuSSHRunnerConfig(host="ubuntu-prod-01", username="ops"),
)
runner.add_aspect(aspect=RetryAspect(
    RetryAspectConfig(max_retries=3, retryable_exit_codes=[100]),
))
runner.add_aspect(aspect=TimeoutAspect(
    TimeoutAspectConfig(default_timeout=900.0),
))

pkg_mgr = UbuntuPackageManager(
    runner=runner,
    config=UbuntuPackageManagerConfig(elevated=True),
)
pkg_mgr.upgrade_all()
```

### Dry-Run Preview

```python
config = UbuntuPackageManagerConfig(elevated=True, dry_run=True)
pkg_mgr = UbuntuPackageManager(runner=runner, config=config)
result = pkg_mgr.remove_package(name="apache2")
assert result.status.value == "dry_run"
```

---

## Testing Strategy

1. **MockCommandRunner** keyed on argv prefix lets each test queue responses for
   `dpkg -l`, `dpkg -s`, `apt list --upgradable`, `dpkg-query -W`, and the write
   commands separately.
2. One command-construction test per write verb asserts the exact argv and the
   env dict contains `DEBIAN_FRONTEND=noninteractive`.
3. Idempotency tests cover installed/not-installed and version-match/version-miss
   scenarios.
4. `get_package_info` tests cover the installed path, the "not installed" path
   (returns `None`), and an unexpected-error path (raises).
5. `list_installed` parsing is tested with a realistic multi-line `dpkg-query`
   output including `installed`, `config-files`, and `not-installed` statuses.
6. `list_upgradable` parsing is tested with the `Listing…` header and
   representative upgradable lines.
7. `install_local` tests assert `UbuntuPackageFileNotFoundError` when the path is
   missing AND that the runner is not invoked in that case.
8. `assume_yes=False` test asserts the `-y` flag is omitted.
9. Elevation tests assert `elevated=True` for writes and never for reads/probes.
10. No test requires root access or a live `apt` installation.
