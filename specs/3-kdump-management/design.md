# Design

## Overview

The kdump-management feature is implemented as a single module,
`automojo.sysman.linux.ubuntu.kdumpmanagement`, that exposes an
`UbuntuKdumpManager` class. The coordinator instantiates
`UbuntuKdumpManager` with a command runner and calls its methods to
manage the kdump-tools subsystem: the `kdump-tools.service` lifecycle,
`/etc/default/kdump-tools` keys, and the on-disk crash-dump artifact
directory.

All system command execution is delegated to a command runner
implementing the `ICommandRunner` interface. All reads and writes
against `/etc/default/kdump-tools` and against `/var/crash/...` flow
through that same runner via `cat` / `tee` / `ls` / `find` / `stat`,
which keeps dry-run, elevation, and aspect composition uniform with
every other Ubuntu sysman feature. `UbuntuKdumpManager` never directly
calls `subprocess` and does not consume an `IFileOperator`.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).

## Cross-OS Concept Mapping

The kdump-management contract is **Linux-primary**. Ubuntu's surface
is the closest non-RHEL realisation; macOS and Windows expose
analogous-but-very-different facilities that this Ubuntu spec is NOT
responsible for. The table below documents how each verb maps onto the
Ubuntu native primitives.

| Cross-OS verb            | Ubuntu native primitive                                     | Native concept            |
|--------------------------|-------------------------------------------------------------|---------------------------|
| `enable_kdump`           | `USE_KDUMP=1` + `systemctl enable --now kdump-tools`        | kdump-tools service       |
| `disable_kdump`          | `USE_KDUMP=0` + `systemctl disable --now kdump-tools`       | kdump-tools service       |
| `configure_kdump_target` | rewrite `KDUMP_COREDIR`/`SSH`/`NFS`/`RAW` + restart service | kdump-tools defaults file |
| `get_kdump_status`       | `kdump-config show` + `systemctl is-*` + defaults parse     | kdump-tools state         |
| `list_recent_dumps`      | `ls -1t /var/crash/`                                        | crash-dump directory      |
| `clear_old_dumps`        | `find /var/crash -mtime +N -exec rm`                        | crash-dump directory      |
| `get_dump_info`          | `cat /var/crash/<id>/vmcore-dmesg.txt`                      | dmesg snapshot            |

Comparison with sister implementations:

| OS      | Configuration store              | Service unit               | Reboot required for target change? |
|---------|----------------------------------|----------------------------|------------------------------------|
| Ubuntu  | `/etc/default/kdump-tools`       | `kdump-tools.service`      | No (restart kdump-tools)           |
| RHEL    | `/etc/kdump.conf`                | `kdump.service`            | No (restart kdump + rebuild)       |
| macOS   | (none — always-on panic logging) | (none — diagnosticd)       | N/A                                |
| Windows | `HKLM:\...\CrashControl`         | (none — kernel-resident)   | YES                                |

**See also:** the cross-OS contract at
`automojo-sysman-common/specs/contracts/kdump-management/contract.md`.

---

## Architecture

```
+-------------------------------------------------------------+
|              Maintenance Coordinator                        |
|  - Instantiates runner (Local or SSH)                       |
|  - Configures aspects (optional)                            |
|  - Injects runner into UbuntuKdumpManager                   |
+----------------------+--------------------------------------+
                       | instantiates & injects
                       v
+-------------------------------------------------------------+
|            UbuntuKdumpManager                               |
|  automojo.sysman.linux.ubuntu.kdumpmanagement               |
|                                                             |
|  enable_kdump()              disable_kdump()                |
|  configure_kdump_target()    clear_old_dumps()              |
|  get_kdump_status()          list_recent_dumps()            |
|  get_dump_info()                                            |
+----------------------+--------------------------------------+
                       | delegates to
                       v
+-------------------------------------------------------------+
|            ICommandRunner                                   |
|  run(args, elevated, stdin, ...) -> RunResult               |
+----------------------+--------------------------------------+
        +--------------+--------------+
        v                             v
+----------------------+      +----------------------+
| LocalCommandRunner   |      | SSHCommandRunner     |
| - subprocess.run()   |      | - SSH connection     |
| - sudo for elevation |      | - reliability metrics|
+----------+-----------+      +-----------+----------+
           |                              |
           +-------------+----------------+
                         | optional
                         v
+-------------------------------------------------------------+
|                    Execution Aspects                        |
|  - PersistenceAspect: audit logging                         |
|  - RetryAspect: transient failure retry                     |
|  - TimeoutAspect: global timeout policy                     |
+-------------------------------------------------------------+
```

---

## Components

### `UbuntuKdumpManager`

**Location:**
`source/packages/automojo/sysman/linux/ubuntu/kdumpmanagement.py`

The primary public class. Constructed with an `ICommandRunner` and a
`UbuntuKdumpManagerConfig`.

| Method | Description |
|---|---|
| `enable_kdump()` -> `UbuntuKdumpActionResult` | Set `USE_KDUMP=1` and `systemctl enable --now`. |
| `disable_kdump()` -> `UbuntuKdumpActionResult` | Set `USE_KDUMP=0` and `systemctl disable --now`. |
| `configure_kdump_target(spec)` -> `UbuntuKdumpActionResult` | Rewrite the defaults-file target keys; restart service. |
| `clear_old_dumps(age_days)` -> `UbuntuKdumpActionResult` | `find /var/crash -mtime +N -exec rm`. |
| `get_kdump_status()` -> `UbuntuKdumpStatus` | Combined defaults + systemctl + `kdump-config show` snapshot. |
| `list_recent_dumps()` -> `list[UbuntuKdumpInfo]` | `ls -1t /var/crash/`. |
| `get_dump_info(dump_id)` -> `UbuntuKdumpInfo` | `cat` + `stat` on a dump directory. |

### `UbuntuKdumpManagerConfig`

A dataclass holding runtime configuration for `UbuntuKdumpManager`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `dry_run` | `bool` | `False` | Inherited. When `True`, no commands execute. |
| `elevated` | `bool` | `False` | Inherited. When `True`, write verbs elevate. |
| `kdump_defaults` | `str` | `/etc/default/kdump-tools` | Defaults-file path. |
| `crash_dir` | `str` | `/var/crash` | Crash-dump search root. |
| `service_name` | `str` | `kdump-tools.service` | Systemd unit name. |

---

## Data Models

### `UbuntuKdumpActionStatus`

Aliased to the canonical `ActionStatus` enum from
`featurebase.py`. Values: `SUCCESS`, `FAILED`, `SKIPPED`, `DRY_RUN`.

### `UbuntuKdumpTargetKind`

```python
class UbuntuKdumpTargetKind(Enum):
    LOCAL_PATH = "local_path"
    SSH        = "ssh"
    NFS        = "nfs"
    RAW        = "raw"
```

### `UbuntuKdumpTargetSpec`

```python
@dataclass
class UbuntuKdumpTargetSpec:
    target_kind: UbuntuKdumpTargetKind
    path: Optional[str] = None         # LOCAL_PATH / RAW
    ssh_user: Optional[str] = None     # SSH
    ssh_host: Optional[str] = None     # SSH
    nfs_export: Optional[str] = None   # NFS
    compression: Optional[str] = None  # MAKEDUMPFILE_ARGS
```

### `UbuntuKdumpActionResult`

Returned by every write verb.

```python
@dataclass
class UbuntuKdumpActionResult(FeatureActionResult):
    target: Optional[str] = None
    requires_reboot: bool = False
    message: Optional[str] = None
```

### `UbuntuKdumpStatus`

Returned by `get_kdump_status()`.

```python
@dataclass
class UbuntuKdumpStatus:
    enabled: bool
    running: bool
    target_kind: Optional[UbuntuKdumpTargetKind] = None
    target_path: Optional[str] = None
    kernel: Optional[str] = None
    reserved_memory_mb: Optional[int] = None
    message: Optional[str] = None
```

### `UbuntuKdumpInfo`

Returned by `list_recent_dumps()` and `get_dump_info()`.

```python
@dataclass
class UbuntuKdumpInfo:
    id: str
    path: str
    created: str
    size_bytes: Optional[int] = None
    kernel: Optional[str] = None
    panic_message: Optional[str] = None
```

### `UbuntuKdumpQueryError`

Raised by `list_recent_dumps()` / `get_dump_info()` when the read fails
for a reason other than the target being absent. Carries `command`,
`exit_code`, and `stderr` for incident triage.

---

## Defaults-File Mutation

`/etc/default/kdump-tools` is a shell-sourced file. Every line is one
of:

* a comment (`# ...`)
* a blank line
* a `KEY=VALUE` assignment, where `VALUE` may be unquoted, single-
  quoted, or double-quoted

The module-level helper `_apply_defaults_keys` rewrites a set of
key/value pairs into the file body while preserving comments,
ordering, and the existing quoting style for any key that already
exists. Keys that do not exist are appended at the bottom with double
quoting.

`_render_defaults_value` selects the quoting style:

* If the existing entry was double-quoted, use double quoting.
* If the existing entry was single-quoted, use single quoting.
* If the entry was absent or had no quotes, use double quoting (the
  conservative default that handles values containing spaces,
  e.g. `"-c -d 31"`).

---

## Idempotency

| Verb | Skip when ... |
|------|---------------|
| `enable_kdump` | defaults file has `USE_KDUMP=1` AND systemd is `active`+`enabled` |
| `disable_kdump` | defaults file has `USE_KDUMP=0` AND systemd is not `active`+`enabled` |
| `configure_kdump_target` | rendered defaults file body byte-matches current on-disk body |
| `clear_old_dumps` | never (find is itself idempotent) |

The skip check uses `cat` to read the defaults file and `systemctl
is-active` / `systemctl is-enabled` to read the unit state. None of
these probes elevate.

---

## Correctness Properties

### Property 1: Enable Idempotency

For any host where the defaults file already carries `USE_KDUMP=1` and
the systemd unit reports `active`+`enabled`, `enable_kdump()` returns
`SKIPPED` and the runner is NOT invoked for the `tee` write or the
`systemctl enable --now` call.

**Validates: Requirements 1.3**

### Property 2: Disable Idempotency

For any host where the defaults file already carries `USE_KDUMP=0` and
the systemd unit is not `active`+`enabled`, `disable_kdump()` returns
`SKIPPED` and the runner is NOT invoked for the `tee` write or the
`systemctl disable --now` call.

**Validates: Requirements 2.3**

### Property 3: Configure-Target Idempotency

For any `UbuntuKdumpTargetSpec` where the rendered defaults file body
matches the existing on-disk body byte-for-byte, `configure_kdump_target`
returns `SKIPPED` and the systemd restart is NOT issued.

**Validates: Requirements 3.7**

### Property 4: Target-Kind Mutual Exclusion

For any successful `configure_kdump_target` call, exactly one of the
target keys (`KDUMP_COREDIR`, `SSH`, `NFS`, `RAW`) carries a non-empty
value in the rewritten defaults file; the other three are blanked.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

### Property 5: Dry-Run Safety

For any mutating verb invoked with `config.dry_run=True`, the runner
is NOT invoked for the mutating step, and the returned result has
status `DRY_RUN` with the `command` field populated.

**Validates: Requirements 1.4, 2.4, 3.x, 6.4, 10.x**

### Property 6: Elevation Consistency

For any write verb invoked with `config.elevated=True`, every mutating
runner call is invoked with `elevated=True`. For any read verb or
idempotency probe, every runner call is invoked with `elevated=False`,
regardless of the config.

**Validates: Requirements 9.1, 9.2, 9.3, 9.4**

### Property 7: Failed-Command Surface

For any write verb whose mutating step returns a non-zero exit code,
the result has status `FAILED`, with `exit_code`, `stderr`, and
`command` populated.

**Validates: Requirements 1.5**

### Property 8: List-Empty-On-Missing

For any host whose `<crash_dir>` does not exist, `list_recent_dumps()`
returns an empty list rather than raising.

**Validates: Requirements 5.3**

### Property 9: Status Reads Never Elevate

For any invocation of `get_kdump_status()`, every runner call is
invoked with `elevated=False`, regardless of `config.elevated`.

**Validates: Requirements 4.4**

### Property 10: Quote Preservation

For any defaults-file key that exists with a particular quoting style,
a successful rewrite preserves that quoting style.

**Validates: Requirements 1.1, 2.1, 3.1**

### Property 11: Age Validation

For any call to `clear_old_dumps(age_days=N)` with `N < 0`, the runner
is NOT invoked and the result has status `FAILED` with
`message="age_days must be >= 0"`.

**Validates: Requirements 6.2**

---

## Interfaces

### `ICommandRunner`

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        stdin: Optional[str] = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `RunResult`

```python
@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
```

---

## Command Mappings

| Operation                       | Command                                                              |
|---------------------------------|----------------------------------------------------------------------|
| Read defaults file              | `cat /etc/default/kdump-tools`                                       |
| Write defaults file             | `tee /etc/default/kdump-tools` (via stdin)                           |
| Service active probe            | `systemctl is-active kdump-tools.service`                            |
| Service enabled probe           | `systemctl is-enabled kdump-tools.service`                           |
| Enable service                  | `systemctl enable --now kdump-tools.service`                         |
| Disable service                 | `systemctl disable --now kdump-tools.service`                        |
| Restart service                 | `systemctl restart kdump-tools.service`                              |
| kdump runtime status            | `kdump-config show`                                                  |
| List dump directory             | `ls -1t /var/crash`                                                  |
| Find / clear old dumps          | `find /var/crash -maxdepth 1 -type d -mtime +N -exec rm -rf {} \\;`  |
| Read dump dmesg                 | `cat /var/crash/<id>/vmcore-dmesg.txt`                               |
| Read fallback dmesg             | `cat /var/crash/<id>/dmesg.<id>`                                     |
| Read dump metadata              | `stat -c %y;%s /var/crash/<id>`                                      |

---

## Error Handling

### Typed Exceptions

| Exception | Raised when |
|-----------|-------------|
| `UbuntuKdumpQueryError` | A read fails for a reason other than the target being absent (`list_recent_dumps`, `get_dump_info`). |

All other failures surface as `UbuntuKdumpActionResult(status=FAILED)`
so the coordinator decides how to respond without catching exceptions
for normal operational failures.

### Reboot-Required Advisory

Unlike Windows, Ubuntu's kdump-tools subsystem re-arms the capture
kernel on `systemctl restart kdump-tools.service` when `USE_KDUMP=1`
and a `crashkernel=` reservation is already present on the running
kernel command line. `requires_reboot` is therefore always `False` on
Ubuntu writes; the field is carried for cross-OS contract conformance.
Changing the `crashkernel=` reservation itself is delegated to the
`boot-management` feature and is OUT OF SCOPE for this manager.

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/
+-- __init__.py
+-- kdumpmanagement.py            <- UbuntuKdumpManager + models

source/testroots/testbase/
+-- automojo/tests/sysman/linux/ubuntu/
    +-- test_kdumpmanagement.py   <- unit tests using a MockCommandRunner
```

---

## Testing Strategy

### Unit Tests

Every test uses a `_MockCommandRunner` that records every `run` call
and returns pre-seeded `RunResult` values keyed by argv tuple. Tests
verify:

* the exact argv passed to the runner,
* the stdin payload for `tee` calls,
* the elevation flag on every call,
* the result `status`, `command`, `exit_code`, `stderr`, and
  `message` fields.

No live `systemctl`, `kdump-config`, `find`, `ls`, or `tee` invocation
occurs; tests run in any environment, including macOS / Windows CI.

### Test Coverage

Required tests:

* `test_enable_kdump_invokes_systemctl`
* `test_enable_kdump_skipped_when_enabled`
* `test_enable_kdump_dry_run_short_circuits`
* `test_disable_kdump_invokes_systemctl`
* `test_configure_kdump_target_invokes_tee`
* `test_configure_kdump_target_skipped_when_already_set`
* `test_get_kdump_status_parses_kdump_config_output`
* `test_list_recent_dumps_parses_directory_listing`
* `test_clear_old_dumps_invokes_find_with_mtime`
* `test_get_dump_info_parses_dmesg_output`
* `test_writes_elevate`
* `test_reads_never_elevate`
* `test_failed_command_surfaces_stderr`
* `test_list_recent_dumps_raises_on_unexpected_failure`

Total: 14 tests.

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.linux.ubuntu.runners import (
    LocalCommandRunner, LocalRunnerConfig,
)
from automojo.sysman.linux.ubuntu.kdumpmanagement import (
    UbuntuKdumpManager, UbuntuKdumpManagerConfig,
    UbuntuKdumpTargetKind, UbuntuKdumpTargetSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = UbuntuKdumpManagerConfig(elevated=True)
manager = UbuntuKdumpManager(runner=runner, config=config)

# Configure dump target.
spec = UbuntuKdumpTargetSpec(
    target_kind=UbuntuKdumpTargetKind.LOCAL_PATH,
    path="/var/crash",
)
target_result = manager.configure_kdump_target(spec=spec)
print(f"target status: {target_result.status}")

# Enable capture.
enable_result = manager.enable_kdump()
print(f"enable status: {enable_result.status}")
```

### SSH Execution with Reliability Tracking

```python
from automojo.sysman.linux.ubuntu.runners import (
    SSHCommandRunner, SSHRunnerConfig,
)
from automojo.sysman.linux.ubuntu.kdumpmanagement import (
    UbuntuKdumpManager, UbuntuKdumpManagerConfig,
)

ssh_config = SSHRunnerConfig(
    host="192.168.1.100",
    username="admin",
    key_filename="/home/user/.ssh/id_rsa",
    max_connection_retries=5,
)
runner = SSHCommandRunner(ssh_config)
manager = UbuntuKdumpManager(
    runner=runner,
    config=UbuntuKdumpManagerConfig(elevated=True),
)

# Snapshot the remote host state.
status = manager.get_kdump_status()
print(f"enabled={status.enabled} running={status.running} target={status.target_path}")
```

### Dry-Run Mode

```python
config = UbuntuKdumpManagerConfig(elevated=True, dry_run=True)
manager = UbuntuKdumpManager(runner=runner, config=config)
result = manager.enable_kdump()
assert result.status.value == "dry_run"
print(result.command)  # ['systemctl', 'enable', '--now', 'kdump-tools.service']
```
