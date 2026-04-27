# Design

## Overview

The log management feature is implemented as a single module,
`automojo.sysman.linux.ubuntu.logmanagement`, that exposes a `UbuntuLogManager` class.
This spec implements the log-management contract defined at
`automojo-sysman-common/specs/contracts/log-management/contract.md` for Ubuntu
using `journalctl` for the systemd journal, `tail`/`cat`/`stat` for plain-file logs,
and `logrotate` with drop-ins under `/etc/logrotate.d/`.

All system commands go through the `ICommandRunner` interface; all rotation drop-in
file I/O goes through the `IFileOperator` interface — this dual-abstraction dependency
is an extension over the contract, motivated by the need to manage both live journald
and plain text logs on the same host.  No `subprocess`, no direct `open`, and no
direct `pathlib` calls are made inside the module.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).  When using SSH,
the coordinator can track reliability metrics and apply execution aspects
(persistence, retry, timeout) without any changes to the `UbuntuLogManager` code.

**See Also:** [Command Runner Architecture](../1-command-execution/design.md) for
details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator                      │
│  - Configures aspects (optional)                            │
│  - Injects both into UbuntuLogManager                       │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuLogManager                                 │
│  automojo.sysman.linux.ubuntu.logmanagement                 │
│                                                             │
│  get_log()              tail_log()                          │
│  list_logs()            rotate_log()                        │
│  truncate_log()         configure_rotation()                │
│  remove_rotation_config()  get_rotation_config()            │
│  query_disk_usage()                                         │
└───────────┬───────────────────────────┬─────────────────────┘
            │ delegates to              │ delegates to
            ▼                           ▼
┌──────────────────────┐        ┌──────────────────────┐
│   ICommandRunner     │        │   IFileOperator      │
│   run(args, elevated)│        │   read_text / write  │
│   → RunResult        │        │   delete / exists    │
└──────────┬───────────┘        └──────────┬───────────┘
           │                               │
           └───────────────┬───────────────┘
                           ▼
          ┌──────────────────────────────────┐
          │   LocalCommandRunner   or        │
          │   SSHCommandRunner               │
          └─────────────┬────────────────────┘
                        │ optional
                        ▼
          ┌──────────────────────────────────┐
          │   PersistenceAspect /            │
          │   RetryAspect / TimeoutAspect    │
          └──────────────────────────────────┘
```

---

## Components

### `UbuntuLogManager`

**Location:** `source/packages/automojo/sysman/linux/ubuntu/logmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `get_log` | `(source, lines=None, since=None, until=None, severity=None, pattern=None) → UbuntuLogEntries` | Retrieve recent entries |
| `tail_log` | `(source, follow=True) → Iterator[UbuntuLogEntry]` | Stream entries |
| `list_logs` | `() → list[UbuntuLogInfo]` | Enumerate units + `/var/log/*` |
| `rotate_log` | `(source) → UbuntuLogActionResult` | Force rotation |
| `truncate_log` | `(source) → UbuntuLogActionResult` | Zero out a plain-file log or vacuum the journal |
| `configure_rotation` | `(source, policy) → UbuntuLogActionResult` | Write drop-in |
| `remove_rotation_config` | `(source) → UbuntuLogActionResult` | Delete drop-in |
| `get_rotation_config` | `(source) → UbuntuRotationPolicy \| None` | Parse drop-in |
| `query_disk_usage` | `(source) → UbuntuLogDiskUsage` | Compute disk usage |

### `UbuntuLogManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit (see §rotate_log exception) |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |

---

## Data Models

### Enums

```python
class UbuntuLogActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"
```

### Result Types

```python
@dataclass
class UbuntuLogEntry:
    timestamp: datetime | None
    severity: str | None
    source: str | None
    message: str

@dataclass
class UbuntuLogEntries:
    source: str
    entries: list[UbuntuLogEntry]
    raw: list[str]
    truncated: bool

@dataclass
class UbuntuLogInfo:
    source: str
    path: str | None
    size_bytes: int | None
    rotating: bool

@dataclass
class UbuntuLogActionResult:
    status: UbuntuLogActionStatus
    source: str
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

@dataclass
class UbuntuRotationPolicy:
    source: str
    keep_count: int
    frequency: str              # "daily" / "weekly" / "monthly" / "size:100M"
    compress: bool
    max_size_bytes: int | None
    post_rotate: str | None

@dataclass
class UbuntuLogDiskUsage:
    source: str
    size_human: str
    size_bytes: int

class UbuntuLogQueryError(Exception):
    """Raised when a read fails for a reason other than source-missing."""
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(self, args: list[str], elevated: bool = False,
            capture_output: bool = True, stdin: str | None = None) -> RunResult: ...
    def stream(self, args: list[str], elevated: bool = False) -> Iterator[str]: ...
```

### `IFileOperator` (from `automojo-interfaces`)

```python
class IFileOperator(Protocol):
    def read_text(self, path: str) -> str: ...
    def write_text(self, path: str, content: str, elevated: bool = False) -> None: ...
    def delete(self, path: str, elevated: bool = False) -> None: ...
    def exists(self, path: str) -> bool: ...
```

---

## Source Mapping

| Shape | Example | Routing |
|---|---|---|
| Systemd unit name | `nginx.service`, `ssh.service` | `journalctl -u <name>` |
| Syslog facility | `syslog.facility:auth` | `journalctl SYSLOG_FACILITY=<num>` |
| Filesystem path | `/var/log/auth.log` | `tail` / `cat` / `stat` |

The routing helper `_classify_source(source)` returns one of the tokens
`"unit"`, `"facility"`, `"file"` based on:

1. If `source.startswith("/")` → `"file"`.
2. If `source.startswith("syslog.facility:")` → `"facility"`.
3. Otherwise → `"unit"` (default).

---

## Command Mappings

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| `get_log` — unit | `journalctl -u <name> --no-pager -o json [-n N] [--since S] [--until U] [-p P] [--grep G]` |
| `get_log` — file | `tail -n <N> <path>` or `cat <path>` |
| `tail_log` — unit | `journalctl -u <name> -f --no-pager -o json` |
| `tail_log` — file | `tail -F <path>` |
| `list_logs` | `systemctl list-units --type=service --no-legend --no-pager`, `ls -1 /var/log`, `stat -c%s <path>` |
| `get_rotation_config` | (file read only — `IFileOperator.read_text`) |
| `query_disk_usage` — unit | `journalctl --disk-usage --no-pager` |
| `query_disk_usage` — file | `stat -c%s <path>` |

### Write Commands (elevation-aware)

| Operation | Command |
|---|---|
| `rotate_log` — drop-in | `logrotate -f /etc/logrotate.d/<basename>` |
| `rotate_log` — fallback | `logrotate -f /etc/logrotate.conf` |
| `rotate_log` — dry-run | `logrotate --debug <config-path>` |
| `truncate_log` — file | `IFileOperator.write_text(path, "", elevated=True)` |
| `truncate_log` — unit | `journalctl --rotate`, `journalctl --vacuum-size=1` |
| `configure_rotation` | `IFileOperator.write_text("/etc/logrotate.d/<basename>", rendered, elevated=True)` |
| `remove_rotation_config` | `IFileOperator.delete("/etc/logrotate.d/<basename>", elevated=True)` |

---

## `journalctl -o json` Output Parsing

Each line of stdout is a JSON object with at least:

```
{"__REALTIME_TIMESTAMP":"1712345678901234",
 "PRIORITY":"6",
 "_SYSTEMD_UNIT":"nginx.service",
 "MESSAGE":"1.2.3.4 - - [05/Apr/2024:12:34:38 +0000] \"GET /\" 200"}
```

Parse rules:

1. `timestamp` — `datetime.utcfromtimestamp(int(__REALTIME_TIMESTAMP) / 1_000_000)`.
2. `severity` — `_PRIORITY_MAP[int(PRIORITY)]` where `_PRIORITY_MAP` is `{0:"emerg",
   1:"alert", 2:"crit", 3:"err", 4:"warning", 5:"notice", 6:"info", 7:"debug"}`.
3. `source` — `_SYSTEMD_UNIT` if present, else `SYSLOG_IDENTIFIER`.
4. `message` — `MESSAGE` field; when missing, an empty string.

Malformed JSON lines are appended to `raw` and skipped for `entries` (parser does not
raise).

---

## `journalctl --disk-usage` Output Parsing

Single line example: `Archived and active journals take up 1.2G in the file system.`

1. Match against `r"take up ([\d.]+)([BKMGT])"`.
2. Multiply by suffix table (`B=1`, `K=1024`, `M=1_048_576`, `G=1_073_741_824`,
   `T=1_099_511_627_776`).
3. If no match raise `UbuntuLogQueryError(...)`.

---

## `UbuntuRotationPolicy` Rendering and Parsing

### Rendered shape

```
/var/log/<source-basename> {
    <frequency>
    rotate <keep_count>
    [compress]
    [size <max_size_bytes>]
    missingok
    notifempty
    [postrotate
        <post_rotate>
    endscript]
}
```

### Parse rules

Tokenise the file by `{` and `}`; within the block:

- `daily|weekly|monthly|yearly` → `frequency`
- `rotate N` → `keep_count = N`
- `compress` → `compress = True`
- `size N[KMG]` → `max_size_bytes = N * suffix`
- `postrotate ... endscript` → `post_rotate = "..."`

---

## Idempotency Guards

| Method | Guard | Mechanism |
|---|---|---|
| `configure_rotation` | Rendered content matches existing | `read_text` → string compare |
| `remove_rotation_config` | Drop-in absent | `IFileOperator.exists` |
| `rotate_log` | None — operational action | — |
| `truncate_log` | None — operational action | — |

---

## `rotate_log` Dry-Run Exception

Per the contract, `rotate_log` is permitted to execute a native simulation command
even in dry-run mode.  This implementation invokes `logrotate --debug <config-path>`
so that the coordinator receives the same debug output the operator would see when
running the command manually.  The returned `UbuntuLogActionResult` carries
`status=DRY_RUN` and the debug stdout in the `stdout` field.

All other write verbs follow the strict contract rule: no runner invocation in
dry-run mode.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `UbuntuLogQueryError` | `get_log`, `tail_log` (first iter), `list_logs`, `get_rotation_config` (unparseable content), or `query_disk_usage` sees a failure that is NOT "source unknown" |

All other failures from write operations are surfaced as
`UbuntuLogActionResult(status=FAILED)` with exit code and stderr populated.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid
executions of the system.*

### Property 1: `get_log` Command Construction for Units
*For any* systemd unit name and filter set, `get_log` should construct a `journalctl`
command with `-u <unit>`, `--no-pager`, `-o json`, plus flag equivalents for each
supplied filter.
**Validates: Requirements 1.1**

### Property 2: `get_log` Command Construction for Files
*For any* file path with `lines=N`, `get_log` should construct `tail -n N <path>`.
**Validates: Requirements 1.2**

### Property 3: `get_log` JSON Parsing
*For any* well-formed `journalctl -o json` stdout, `get_log` should produce a
`UbuntuLogEntries` whose `entries` list contains one `UbuntuLogEntry` per line, in
chronological order.
**Validates: Requirements 1.3**

### Property 4: `get_log` Truncation Flag
*For any* query where the resulting entry count equals `lines`, the result's
`truncated` field should be `True`.
**Validates: Requirements 1.4**

### Property 5: `get_log` Unknown Source
*For any* source that does not exist, `get_log` should return an empty
`UbuntuLogEntries` without raising.
**Validates: Requirements 1.5**

### Property 6: `get_log` Error Raises
*For any* failure other than "source unknown", `get_log` should raise
`UbuntuLogQueryError` with exit code and stderr.
**Validates: Requirements 1.6**

### Property 7: `tail_log` Streaming Construction
*For any* unit and `follow=True`, `tail_log` should invoke
`journalctl -u <name> -f --no-pager -o json` via the runner's streaming API.
**Validates: Requirements 2.1**

### Property 8: `tail_log` File Construction
*For any* file path and `follow=True`, `tail_log` should invoke `tail -F <path>`.
**Validates: Requirements 2.2**

### Property 9: `tail_log` Non-Follow Termination
*For any* non-follow call, the iterator should yield the backlog and then terminate.
**Validates: Requirements 2.3**

### Property 10: `list_logs` Composition
*For any* call, `list_logs` should invoke both the `systemctl list-units` and `ls
-1 /var/log` commands and compose the results.
**Validates: Requirements 3.1**

### Property 11: `list_logs` Rotating Flag
*For any* source, `UbuntuLogInfo.rotating` should be `True` iff
`/etc/logrotate.d/<basename>` exists.
**Validates: Requirements 3.2, 3.3**

### Property 12: `list_logs` Error Raises
*For any* failure in either sub-command, `list_logs` should raise
`UbuntuLogQueryError`.
**Validates: Requirements 3.4**

### Property 13: `rotate_log` Drop-In Path
*For any* source with an existing drop-in, `rotate_log` should invoke
`logrotate -f /etc/logrotate.d/<basename>`.
**Validates: Requirements 4.1**

### Property 14: `rotate_log` Fallback
*For any* source lacking a drop-in, `rotate_log` should fall back to
`logrotate -f /etc/logrotate.conf` and fail-`FAILED` if no matching stanza exists.
**Validates: Requirements 4.2**

### Property 15: `rotate_log` Dry-Run Debug
*For any* call with `dry_run=True`, `rotate_log` should invoke
`logrotate --debug <config-path>` and return `status=DRY_RUN` with the debug output
in `stdout`.
**Validates: Requirements 4.5, 11.2**

### Property 16: `truncate_log` File Path
*For any* filesystem source, `truncate_log` should call
`IFileOperator.write_text(path, "", elevated=<cfg>)`.
**Validates: Requirements 5.1**

### Property 17: `truncate_log` Unit Path
*For any* unit source, `truncate_log` should invoke
`journalctl --rotate` then `journalctl --vacuum-size=1`.
**Validates: Requirements 5.2**

### Property 18: `truncate_log` Dry-Run Short-Circuit
*For any* call with `dry_run=True`, `truncate_log` should not invoke the file operator
or runner and should return `status=DRY_RUN`.
**Validates: Requirements 5.5, 11.1**

### Property 19: `configure_rotation` Idempotency
*For any* source where the rendered policy matches existing drop-in content,
`configure_rotation` should return `status=SKIPPED`.
**Validates: Requirements 6.2**

### Property 20: `configure_rotation` Write
*For any* source where content differs, `configure_rotation` should call
`write_text(..., elevated=<cfg>)` and return `status=SUCCESS`.
**Validates: Requirements 6.3**

### Property 21: `configure_rotation` Dry-Run
*For any* call with `dry_run=True`, `configure_rotation` should compute the diff,
populate `detail`, and return `status=DRY_RUN`.
**Validates: Requirements 6.5, 11.1**

### Property 22: `remove_rotation_config` Idempotency
*For any* source with no existing drop-in, `remove_rotation_config` should return
`status=SKIPPED` without calling `delete`.
**Validates: Requirements 7.1**

### Property 23: `remove_rotation_config` Delete
*For any* source with an existing drop-in, `remove_rotation_config` should call
`delete(..., elevated=<cfg>)` and return `status=SUCCESS`.
**Validates: Requirements 7.2**

### Property 24: `get_rotation_config` Absent
*For any* source with no drop-in, `get_rotation_config` should return `None`.
**Validates: Requirements 8.1**

### Property 25: `get_rotation_config` Parse
*For any* source with a recognised drop-in, `get_rotation_config` should parse the
file into a `UbuntuRotationPolicy`.
**Validates: Requirements 8.2**

### Property 26: `get_rotation_config` Unparseable
*For any* drop-in whose content cannot be parsed, `get_rotation_config` should raise
`UbuntuLogQueryError`.
**Validates: Requirements 8.3**

### Property 27: `query_disk_usage` Unit Parse
*For any* unit, `query_disk_usage` should parse `journalctl --disk-usage` into a
`UbuntuLogDiskUsage` with both `size_human` and `size_bytes` populated.
**Validates: Requirements 9.1, 9.3**

### Property 28: Elevation for Writes
*For any* write operation with `elevated=True` in the config, the runner or file
operator should be invoked with `elevated=True`.
**Validates: Requirements 10.1**

### Property 29: No Elevation for Reads
*For any* read operation, the runner and file operator should never be invoked with
`elevated=True`.
**Validates: Requirements 10.2**

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/
├── __init__.py
└── logmanagement.py          ← UbuntuLogManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/ubuntu/
    └── test_logmanagement.py
```

---

## Usage Examples

### Local Execution

```python
from automojo.sysman.linux.ubuntu.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.linux.ubuntu.fileoperators import LocalFileOperator
from automojo.sysman.linux.ubuntu.logmanagement import (
    UbuntuLogManager,
    UbuntuLogManagerConfig,
    UbuntuRotationPolicy,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_operator = LocalFileOperator(runner=runner)

log_mgr = UbuntuLogManager(
    runner=runner,
    file_operator=file_operator,
    config=UbuntuLogManagerConfig(elevated=True, dry_run=False),
)

entries = log_mgr.get_log(source="nginx.service", lines=100, severity="err")
for entry in entries.entries:
    print(f"{entry.timestamp} {entry.severity} {entry.message}")

policy = UbuntuRotationPolicy(
    source="myapp",
    keep_count=7,
    frequency="daily",
    compress=True,
    max_size_bytes=None,
    post_rotate=None,
)
result = log_mgr.configure_rotation(source="myapp", policy=policy)
print(f"Configure rotation: {result.status}")

rotate_result = log_mgr.rotate_log(source="myapp")
print(f"Rotate: {rotate_result.status}")
```

### SSH Execution with Aspects

```python
from automojo.sysman.linux.ubuntu.runners import SSHCommandRunner, SSHRunnerConfig
from automojo.sysman.linux.ubuntu.aspects import (
    PersistenceAspect, PersistenceAspectConfig,
    RetryAspect, RetryAspectConfig,
    TimeoutAspect, TimeoutAspectConfig,
)
from automojo.sysman.linux.ubuntu.storage import SQLiteStorage
from automojo.sysman.linux.ubuntu.fileoperators import SSHFileOperator
from automojo.sysman.linux.ubuntu.logmanagement import UbuntuLogManager, UbuntuLogManagerConfig

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-ubuntu", username="admin"))

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

file_operator = SSHFileOperator(runner=runner)
log_mgr = UbuntuLogManager(
    runner=runner,
    file_operator=file_operator,
    config=UbuntuLogManagerConfig(elevated=True),
)

result = log_mgr.truncate_log(source="/var/log/auth.log")
print(f"Truncate: {result.status}")
```

### Dry-Run Mode

```python
cfg = UbuntuLogManagerConfig(elevated=True, dry_run=True)
log_mgr = UbuntuLogManager(runner=runner, file_operator=file_operator, config=cfg)

rot_result = log_mgr.rotate_log(source="nginx.service")
assert rot_result.status == UbuntuLogActionStatus.DRY_RUN
print(rot_result.stdout)  # logrotate --debug output

trunc_result = log_mgr.truncate_log(source="/var/log/auth.log")
assert trunc_result.status == UbuntuLogActionStatus.DRY_RUN
```

---

## Testing Strategy

1. `MockCommandRunner` keyed on command args prefix returns canned `journalctl -o
   json` lines for `get_log`; asserts the runner received the correct flag set for
   each filter combination.
2. `MockFileOperator` backed by an in-memory `dict[str, str]` drives
   `configure_rotation` idempotency tests (pre-populate the target with matching
   content → expect `SKIPPED`).
3. `rotate_log` dry-run test asserts the runner was invoked exactly once with
   `["logrotate", "--debug", ...]` and the result status is `DRY_RUN`.
4. `list_logs` parser test uses representative `systemctl list-units` and `ls -1
   /var/log` outputs to verify the composed `UbuntuLogInfo` list, including
   `rotating` derivation.
5. `query_disk_usage` unit tests cover every suffix in the size table (`B`/`K`/`M`/
   `G`/`T`), plus an unparseable output case asserting `UbuntuLogQueryError`.
6. `UbuntuRotationPolicy` render/parse round-trip test confirms that rendering then
   parsing yields the original policy.
7. Dry-run matrix test iterates every write verb and asserts no mutating runner or
   file-operator call occurs (except the documented `rotate_log` debug path).
8. Elevation matrix test asserts `elevated=True` flows to the runner/file operator
   for writes and is absent for reads regardless of config.
9. No test requires root access or a live Ubuntu host.

---

## Scope Mapping

| Abstract | Ubuntu |
|---|---|
| Log source (unit) | Systemd unit name like `nginx.service` |
| Log source (facility) | `syslog.facility:<name>` prefix |
| Log source (file) | Absolute path, e.g., `/var/log/auth.log` |
| Rotation drop-in store | `/etc/logrotate.d/<basename>` |

---

## Extensions Beyond the Contract

- **Dual abstraction dependency.**  Uses `IFileOperator` for rotation drop-in I/O
  in addition to `ICommandRunner`.  Documented and reflected in the constructor
  signature.
- **Source classification.**  `_classify_source` switches between unit, facility,
  and file routing so the coordinator can use a single abstract `source` identifier.
- **`rotate_log` dry-run simulation.**  Uses `logrotate --debug` to surface useful
  simulation output rather than returning a silent `DRY_RUN` (explicitly permitted
  by the contract).
