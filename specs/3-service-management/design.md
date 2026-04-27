# Design

## Overview

The service-management feature is implemented as a single module,
`automojo.sysman.linux.ubuntu.servicemanagement`, that exposes a
`UbuntuServiceManager` class.  This spec implements the service-management contract
defined at `automojo-sysman-common/specs/contracts/service-management/contract.md`
for Ubuntu using `systemctl`.  The coordinator instantiates `UbuntuServiceManager`
with a command runner and a config, then calls its verbs to start, stop, restart,
reload, enable, disable, mask, unmask, query status, list services, and reload the
daemon.

All system interaction happens through `ICommandRunner`.  Idempotency guards are
implemented by calling `get_service_status` (which wraps `is-active` and `show
--property=…`) before dispatching a write verb.  The abstract
`ServiceScope.SYSTEM/USER` maps to the presence/absence of the `--user` flag in the
constructed argv.

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
│  - Injects runner into UbuntuServiceManager                 │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuServiceManager                             │
│  automojo.sysman.linux.ubuntu.servicemanagement             │
│                                                             │
│  start_service()  stop_service()  restart_service()         │
│  reload_service() enable_service()  disable_service()       │
│  mask_service()   unmask_service()  get_service_status()    │
│  list_services()  reload_daemon()                           │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output) → RunResult            │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────────┐  ┌──────────────────────────┐
│ UbuntuLocalCommandRunner │  │ UbuntuSSHCommandRunner   │
└──────────────────────────┘  └──────────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect   - RetryAspect    - TimeoutAspect     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `UbuntuServiceManager`

**Location:** `source/packages/automojo/sysman/linux/ubuntu/servicemanagement.py`

| Method | Signature | Description |
|---|---|---|
| `start_service` | `(name, scope) → UbuntuServiceActionResult` | `systemctl [--user] start <name>` |
| `stop_service` | `(name, scope) → UbuntuServiceActionResult` | `systemctl [--user] stop <name>` |
| `restart_service` | `(name, scope) → UbuntuServiceActionResult` | `systemctl [--user] restart <name>` |
| `reload_service` | `(name, scope) → UbuntuServiceActionResult` | `systemctl [--user] reload <name>` |
| `enable_service` | `(name, scope) → UbuntuServiceActionResult` | `systemctl [--user] enable <name>` |
| `disable_service` | `(name, scope) → UbuntuServiceActionResult` | `systemctl [--user] disable <name>` |
| `mask_service` | `(name, scope) → UbuntuServiceActionResult` | `systemctl [--user] mask <name>` |
| `unmask_service` | `(name, scope) → UbuntuServiceActionResult` | `systemctl [--user] unmask <name>` |
| `get_service_status` | `(name, scope) → UbuntuServiceStatus` | `is-active` + `show --property=…` |
| `list_services` | `(scope) → list[UbuntuServiceInfo]` | `list-units --type=service --all --no-legend --plain` |
| `reload_daemon` | `(scope) → UbuntuServiceActionResult` | `systemctl [--user] daemon-reload` |

### `UbuntuServiceManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no write command dispatches |
| `elevated` | `bool` | `False` | When `True`, system-scope writes pass `elevated=True` |

---

## Data Models

### Enums

```python
class UbuntuServiceActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

class UbuntuServiceScope(Enum):
    SYSTEM = "system"
    USER   = "user"
```

### Result / Info Types

```python
@dataclass
class UbuntuServiceActionResult:
    status: UbuntuServiceActionStatus
    service_name: str
    scope: UbuntuServiceScope
    message: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    command: list[str] | None = None

@dataclass
class UbuntuServiceStatus:
    service_name: str
    scope: UbuntuServiceScope
    active_state: str        # "active" | "inactive" | "failed" | "activating" | "deactivating" | "not-found"
    enabled_state: str       # "enabled" | "disabled" | "masked" | "static" | "not-found"
    main_pid: int | None = None
    description: str | None = None

@dataclass
class UbuntuServiceInfo:
    service_name: str
    scope: UbuntuServiceScope
    active_state: str
    enabled_state: str
    description: str | None

class UbuntuServiceQueryError(Exception):
    """Raised when a read fails for an unexpected reason."""
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

---

## Scope Mapping

| `UbuntuServiceScope` | argv prefix |
|---|---|
| `SYSTEM` | `["systemctl"]` |
| `USER` | `["systemctl", "--user"]` |

The scope is used to construct both the action commands and the idempotency-probe
commands.  `get_service_status(name, SYSTEM)` uses system-scope probes;
`get_service_status(name, USER)` uses user-scope probes.

---

## Command Mappings

### Write Commands

| Operation | `SYSTEM` command | `USER` command |
|---|---|---|
| Start | `systemctl start <name>` | `systemctl --user start <name>` |
| Stop | `systemctl stop <name>` | `systemctl --user stop <name>` |
| Restart | `systemctl restart <name>` | `systemctl --user restart <name>` |
| Reload | `systemctl reload <name>` | `systemctl --user reload <name>` |
| Enable | `systemctl enable <name>` | `systemctl --user enable <name>` |
| Disable | `systemctl disable <name>` | `systemctl --user disable <name>` |
| Mask | `systemctl mask <name>` | `systemctl --user mask <name>` |
| Unmask | `systemctl unmask <name>` | `systemctl --user unmask <name>` |
| Reload daemon | `systemctl daemon-reload` | `systemctl --user daemon-reload` |

System-scope writes pass `elevated=True` when `config.elevated=True`.  User-scope
writes do NOT elevate — per-user `systemctl --user` operates against the user's
bus and does not require `sudo`.

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| Active state | `systemctl [--user] is-active <name>` |
| Property block | `systemctl [--user] show <name> --property=LoadState,UnitFileState,MainPID,Description` |
| List services | `systemctl [--user] list-units --type=service --all --no-legend --plain` |

---

## Output Parsing

### `is-active` Stdout

| Stdout (stripped) | `active_state` |
|---|---|
| `active` | `active` |
| `inactive` | `inactive` |
| `failed` | `failed` |
| `activating` | `activating` |
| `deactivating` | `deactivating` |
| `unknown` | `not-found` |

`is-active` exits non-zero for every value except `active` / `activating`; the
manager treats these as informational and does NOT raise.

### `show --property=…` Output

```
LoadState=loaded
UnitFileState=enabled
MainPID=1234
Description=A high performance web server and a reverse proxy server
```

Parsed into a dict keyed by the property name.  `UnitFileState` populates
`enabled_state`; an empty value or `LoadState=not-found` produces
`enabled_state="not-found"`.  `MainPID=0` becomes `None`; any non-zero integer
populates `main_pid`.

### `list-units` Output (`--plain --no-legend`)

```
cron.service        loaded active     running Regular background program processing daemon
ssh.service         loaded active     running OpenBSD Secure Shell server
nginx.service       loaded failed     failed  A high performance web server
apparmor.service    loaded active     exited  Load AppArmor profiles
```

Split on two-or-more spaces into `UNIT`, `LOAD`, `ACTIVE`, `SUB`, `DESCRIPTION`.  The
`.service` suffix is stripped to produce `service_name`; `active_state = ACTIVE`;
`enabled_state=""` (the listing does not include enabled state — callers that need
it can call `get_service_status` per service).

---

## Idempotency Guards

| Method | Guard | Expected state to skip |
|---|---|---|
| `start_service` | `active_state == "active"` | already running |
| `stop_service` | `active_state in {"inactive", "failed"}` | already stopped |
| `restart_service` | never skips | — |
| `reload_service` | `active_state != "active"` → `FAILED` not `SKIPPED` | must be active |
| `enable_service` | `enabled_state == "enabled"` | already enabled |
| `disable_service` | `enabled_state in {"disabled", "masked"}` | already disabled or masked |
| `mask_service` | `enabled_state == "masked"` | already masked |
| `unmask_service` | `enabled_state != "masked"` | not masked |
| `reload_daemon` | never skips | — |

Every idempotency probe calls `get_service_status` which does NOT pass
`elevated=True` regardless of the config (reads never elevate, and the probe is a
read).

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UbuntuServiceQueryError` | `list_services` fails with a non-informational non-zero exit, or `get_service_status`'s `show` command fails with an unexpected stderr |

All other failures are surfaced as `UbuntuServiceActionResult(status=FAILED, …)`.
`is-active` returning non-zero for `inactive` / `failed` / `activating` is
informational and does NOT raise.

---

## Correctness Properties

### Property 1: Start Command Construction

*For any* call to `start_service(name, scope)` whose idempotency guard does not
skip, the runner is invoked with `["systemctl", "start", name]` for `SYSTEM` or
`["systemctl", "--user", "start", name]` for `USER`.

**Validates: Requirements 1.1, 12.1, 12.2**

### Property 2: Start Idempotency

*For any* service whose `active_state == "active"`, `start_service` returns
`SKIPPED` without dispatching the start command.

**Validates: Requirements 1.2**

### Property 3: Stop Command Construction

*For any* call to `stop_service(name, scope)` whose idempotency guard does not
skip, the runner is invoked with the scoped `stop <name>` argv.

**Validates: Requirements 2.1**

### Property 4: Stop Idempotency

*For any* service whose `active_state in {"inactive", "failed"}`, `stop_service`
returns `SKIPPED`.

**Validates: Requirements 2.2**

### Property 5: Restart Unconditional

*For any* call to `restart_service`, the runner is invoked regardless of current
state (unless `dry_run=True`).

**Validates: Requirements 3.1, 3.5**

### Property 6: Reload Precondition

*For any* call to `reload_service` when `active_state != "active"`, the result is
`FAILED` with `message="service is not active; cannot reload"` and the reload
command is NOT dispatched.

**Validates: Requirements 4.2**

### Property 7: Reload Happy Path

*For any* call to `reload_service` when `active_state == "active"`, the runner is
invoked with the scoped `reload <name>` argv.

**Validates: Requirements 4.1**

### Property 8: Enable Idempotency

*For any* service whose `enabled_state == "enabled"`, `enable_service` returns
`SKIPPED`.

**Validates: Requirements 5.2**

### Property 9: Disable Idempotency

*For any* service whose `enabled_state in {"disabled", "masked"}`,
`disable_service` returns `SKIPPED`.

**Validates: Requirements 6.2**

### Property 10: Mask Idempotency

*For any* service whose `enabled_state == "masked"`, `mask_service` returns
`SKIPPED`.

**Validates: Requirements 7.2**

### Property 11: Unmask Idempotency

*For any* service whose `enabled_state != "masked"`, `unmask_service` returns
`SKIPPED`.

**Validates: Requirements 8.2**

### Property 12: Status Query Command

*For any* call to `get_service_status(name, scope)`, the runner is invoked with
the scoped `is-active` command and the scoped `show --property=…` command, neither
carrying `elevated=True`.

**Validates: Requirements 9.1, 13.3**

### Property 13: Status Informational Exit

*For any* `is-active` stdout in `{"inactive", "failed", "activating",
"deactivating"}`, the returned `UbuntuServiceStatus.active_state` equals that
string and no exception is raised.

**Validates: Requirements 9.2**

### Property 14: Status Unexpected Error

*For any* `show` command failure with an unrecognised stderr, a
`UbuntuServiceQueryError` is raised.

**Validates: Requirements 9.3**

### Property 15: Status Not Found

*For any* unknown service, `get_service_status` returns a status with
`active_state="not-found"` and `enabled_state="not-found"`.

**Validates: Requirements 9.4**

### Property 16: List Services Command

*For any* call to `list_services(scope)`, the runner is invoked with the scoped
`list-units --type=service --all --no-legend --plain` argv without elevation.

**Validates: Requirements 10.1**

### Property 17: List Services Parsing

*For any* valid `list-units` output, the returned list has one
`UbuntuServiceInfo` per row with the `.service` suffix stripped from
`service_name`.

**Validates: Requirements 10.2**

### Property 18: List Services Error

*For any* `list-units` failure, `UbuntuServiceQueryError` is raised.

**Validates: Requirements 10.3**

### Property 19: Daemon Reload

*For any* call to `reload_daemon(scope)`, the runner is invoked with the scoped
`daemon-reload` argv.

**Validates: Requirements 11.1**

### Property 20: Daemon Reload Never Skips

*For any* state, `reload_daemon` never returns `SKIPPED`.

**Validates: Requirements 11.5**

### Property 21: Invalid Scope

*For any* call with a scope that is not a `UbuntuServiceScope` member, `ValueError`
is raised before the runner is invoked.

**Validates: Requirements 12.3**

### Property 22: Scope Echo On Result

*For any* write result, `UbuntuServiceActionResult.scope` equals the scope that was
used to build the command.

**Validates: Requirements 12.4**

### Property 23: System Writes Elevate

*For any* write with `config.elevated=True` and `scope=SYSTEM`, the runner is
invoked with `elevated=True`.

**Validates: Requirements 13.1**

### Property 24: User Writes Do Not Elevate

*For any* write with `scope=USER`, the runner is NEVER invoked with
`elevated=True`, regardless of config.

**Validates: Requirements 13.2**

### Property 25: Reads Do Not Elevate

*For any* read or idempotency-probe call, the runner is NEVER invoked with
`elevated=True`.

**Validates: Requirements 13.3, 13.4**

### Property 26: Dry-Run Safety (All Writes)

*For any* write verb with `config.dry_run=True`, the runner is NOT invoked and the
result has `status=DRY_RUN`.

**Validates: Requirements 1.5, 2.5, 3.4, 4.5, 5.5, 6.5, 7.5, 8.5, 11.4**

### Property 27: Status Mapping

*For any* write command result from the runner, `status=SUCCESS` iff
`exit_code==0`; otherwise `status=FAILED` with the captured fields.

**Validates: Requirements 1.3, 1.4, 2.3, 2.4, 3.2, 3.3, 4.3, 4.4, 5.3, 5.4, 6.3,
6.4, 7.3, 7.4, 8.3, 8.4, 11.2, 11.3**

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/
├── __init__.py
└── servicemanagement.py       ← UbuntuServiceManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/ubuntu/
    └── test_servicemanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.linux.ubuntu.runners import (
    UbuntuLocalCommandRunner, UbuntuLocalRunnerConfig,
)
from automojo.sysman.linux.ubuntu.servicemanagement import (
    UbuntuServiceManager, UbuntuServiceManagerConfig, UbuntuServiceScope,
)

runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
config = UbuntuServiceManagerConfig(elevated=True, dry_run=False)
svc_mgr = UbuntuServiceManager(runner=runner, config=config)

result = svc_mgr.restart_service(name="nginx", scope=UbuntuServiceScope.SYSTEM)
print(f"restart nginx: {result.status.value}")

status = svc_mgr.get_service_status(name="nginx", scope=UbuntuServiceScope.SYSTEM)
print(f"active={status.active_state} enabled={status.enabled_state}")
```

### Aspect Composition (SSH)

```python
from automojo.sysman.linux.ubuntu.runners import (
    UbuntuSSHCommandRunner, UbuntuSSHRunnerConfig,
)
from automojo.sysman.linux.ubuntu.aspects import (
    RetryAspect, RetryAspectConfig, TimeoutAspect, TimeoutAspectConfig,
)
from automojo.sysman.linux.ubuntu.servicemanagement import (
    UbuntuServiceManager, UbuntuServiceManagerConfig, UbuntuServiceScope,
)

runner = UbuntuSSHCommandRunner(UbuntuSSHRunnerConfig(host="ubuntu-prod", username="ops"))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3,
                                                      retryable_exit_codes=[5])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

svc_mgr = UbuntuServiceManager(runner=runner,
                               config=UbuntuServiceManagerConfig(elevated=True))
svc_mgr.enable_service(name="cron", scope=UbuntuServiceScope.SYSTEM)
svc_mgr.reload_daemon(scope=UbuntuServiceScope.SYSTEM)
```

### Dry-Run Preview

```python
config = UbuntuServiceManagerConfig(elevated=True, dry_run=True)
svc_mgr = UbuntuServiceManager(runner=runner, config=config)
result = svc_mgr.stop_service(name="nginx", scope=UbuntuServiceScope.SYSTEM)
assert result.status.value == "dry_run"
```

---

## Testing Strategy

1. **MockCommandRunner** keyed on argv prefix lets each test queue the responses
   for `is-active`, `show`, and the action command separately.
2. One command-construction test per write verb per scope asserts the exact argv
   and the `elevated` flag.
3. One idempotency test per write asserts `SKIPPED` without the action command
   being invoked.
4. `reload_service` precondition is tested against an inactive-state probe.
5. `get_service_status` is tested for every documented `is-active` value and for
   `not-found`.
6. `list_services` parsing is tested with a representative `list-units` output
   including `loaded/active/running`, `loaded/failed/failed`, and `masked`.
7. `reload_daemon` is tested for success, failure, and dry-run.
8. Elevation tests assert `elevated=True` is passed for `SYSTEM` writes, not for
   `USER` writes, and never for reads / probes.
9. Invalid-scope test asserts `ValueError` is raised before the runner is invoked.
10. No test requires root access or a live `systemctl`.
