# Design

## Overview

The scheduled-tasks feature is implemented as a single module,
`ccai.sysman.linux.ubuntu.scheduledtasks`, that exposes a
`UbuntuScheduledTaskManager` class.  This spec implements the scheduled-tasks
contract defined at
`codecraftai-sysman-common/specs/contracts/scheduled-tasks/contract.md` for
Ubuntu using systemd timers as the primary scheduling backend and cron
drop-ins as an optional fallback.  The coordinator instantiates
`UbuntuScheduledTaskManager` with a command runner, a file operator, and a
config, then calls the contract's verbs against it.

All unit-file authoring goes through the `IFileOperator` (write / read /
delete / exists).  All `systemctl` invocations go through the
`ICommandRunner`.  Schedule validation is a pure-Python syntactic check — no
external process is ever invoked for validation.  The module has no
dependency on third-party libraries.

The feature works identically whether commands execute locally (via
`UbuntuLocalCommandRunner`) or remotely over SSH (via
`UbuntuSSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner, file operator (Local or SSH)        │
│  - Configures aspects (optional)                            │
│  - Injects into UbuntuScheduledTaskManager                  │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuScheduledTaskManager                       │
│  ccai.sysman.linux.ubuntu.scheduledtasks                │
│                                                             │
│  create_task()   delete_task()                              │
│  enable_task()   disable_task()   run_task_now()            │
│  get_task()      list_tasks()                               │
│  validate_schedule()                                        │
└──────────┬───────────────────────┬──────────────────────────┘
           │ file I/O              │ commands
           ▼                       ▼
┌──────────────────┐   ┌─────────────────────────────────────────┐
│  IFileOperator   │   │   ICommandRunner                        │
│  write_text()    │   │   run(args, elevated, capture_output)   │
│  read_text()     │   └────────────┬────────────────────────────┘
│  delete()        │                │
│  exists()        │     ┌──────────┴──────────────┐
│  list_dir()      │     ▼                         ▼
└──────────────────┘   ┌──────────────────────┐  ┌──────────────────────┐
                       │ LocalCommandRunner   │  │ SSHCommandRunner     │
                       └──────────────────────┘  └──────────────────────┘
                                 │                         │
                                 └──────────┬──────────────┘
                                            │ optional
                                            ▼
                       ┌─────────────────────────────────────────────────┐
                       │            Execution Aspects                    │
                       │  - PersistenceAspect, RetryAspect, TimeoutAspect│
                       └─────────────────────────────────────────────────┘
```

---

## Components

### `UbuntuScheduledTaskManager`

**Location:** `source/packages/ccai/sysman/linux/ubuntu/scheduledtasks.py`

| Method | Signature | Description |
|---|---|---|
| `create_task` | `(spec: UbuntuTaskSpec) → UbuntuScheduledTaskActionResult` | Write `<name>.service` + `<name>.timer`, daemon-reload, optionally enable --now |
| `delete_task` | `(name, owner) → UbuntuScheduledTaskActionResult` | Disable --now, delete unit files, daemon-reload |
| `enable_task` | `(name, owner) → UbuntuScheduledTaskActionResult` | `systemctl enable --now <name>.timer` |
| `disable_task` | `(name, owner) → UbuntuScheduledTaskActionResult` | `systemctl disable --now <name>.timer` |
| `run_task_now` | `(name, owner) → UbuntuScheduledTaskActionResult` | `systemctl start <name>.service` |
| `get_task` | `(name, owner) → UbuntuTaskInfo \| None` | `systemctl show <name>.timer` + `<name>.service` |
| `list_tasks` | `(owner) → list[UbuntuTaskInfo]` | `systemctl list-timers --all` + `list-units --type=timer --all` |
| `validate_schedule` | `(schedule) → bool` | Pure-Python OnCalendar / cron syntax check |

### `UbuntuScheduledTaskManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no files are written/deleted and no commands run |
| `elevated` | `bool` | `False` | When `True`, system-owner writes pass `elevated=True` |
| `system_store_dir` | `str` | `"/etc/systemd/system"` | Directory for system-owner unit files |
| `user_store_dir` | `str \| None` | `None` | Override for `~/.config/systemd/user`; `None` resolves per target user |

---

## Data Models

### Enums

```python
class UbuntuScheduledTaskActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"


class UbuntuTaskOwner(Enum):
    SYSTEM = "system"
    USER   = "user"
```

### Dataclasses

```python
@dataclass
class UbuntuTaskSpec:
    name: str
    schedule: str                    # OnCalendar=<value>, e.g. "daily" or "*-*-* 02:00:00"
    command: str
    arguments: list[str] | None = None
    working_dir: str | None = None
    env: dict[str, str] | None = None
    user: str | None = None          # required when owner=USER and target != running user
    owner: UbuntuTaskOwner = UbuntuTaskOwner.SYSTEM
    enabled: bool = True
    description: str | None = None


@dataclass
class UbuntuTaskInfo:
    name: str
    owner: UbuntuTaskOwner
    schedule: str | None = None
    command: str | None = None
    user: str | None = None
    enabled: bool | None = None
    active: bool | None = None
    last_run: datetime | None = None
    next_run: datetime | None = None
    readable: bool = True


@dataclass
class UbuntuScheduledTaskActionResult:
    status: UbuntuScheduledTaskActionStatus
    name: str
    owner: UbuntuTaskOwner
    message: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    command: list[str] | None = None


class UbuntuScheduledTaskQueryError(Exception):
    """Raised when list_tasks or an unexpected get_task failure occurs."""
```

---

## Owner-Scope Mapping

| `UbuntuTaskOwner` | Unit directory | `systemctl` flag |
|---|---|---|
| `SYSTEM` | `/etc/systemd/system/` | `--system` (default) |
| `USER` (self) | `~/.config/systemd/user/` | `--user` |
| `USER` (other, via `spec.user`) | `~<user>/.config/systemd/user/` | wrapper: `runuser -u <user> -- systemctl --user ...` with `elevated=True` |

When the OS spec resolves the target user, the file-operator writes go to
the absolute path under that user's home directory; the runner wrapper runs
`systemctl --user` as that user.

---

## Unit-File Templates

### `<name>.service`

```ini
[Unit]
Description=<spec.description or spec.name>

[Service]
Type=oneshot
User=<spec.user>                                 # omitted when owner=USER (inherent)
WorkingDirectory=<spec.working_dir>              # omitted when None
Environment=<KEY>=<VALUE>                        # one line per env entry
ExecStart=<spec.command> <joined arguments>

[Install]
WantedBy=multi-user.target                       # omitted for user-scope units
```

### `<name>.timer`

```ini
[Unit]
Description=<spec.description or spec.name> (timer)

[Timer]
OnCalendar=<spec.schedule>
Persistent=true
Unit=<spec.name>.service

[Install]
WantedBy=timers.target                           # default.target for user-scope
```

The rendered bytes are the idempotency key: `create_task` reads the existing
`<name>.timer` and `<name>.service` files (when present) and compares them
byte-identically to the new render.  Any difference triggers a rewrite.

---

## `create_task` Sequence

```
1. validate_schedule(spec.schedule); FAILED if invalid.
2. Resolve owner → unit_dir + systemctl prefix.
3. Render <name>.service and <name>.timer contents.
4. If both files exist AND contents match AND current enabled-state
   matches spec.enabled → return SKIPPED.
5. If dry_run → return DRY_RUN with the rendered `command` reporting
   the daemon-reload invocation.
6. file_operator.write_text(<unit_dir>/<name>.service, content, elevated=…)
7. file_operator.write_text(<unit_dir>/<name>.timer,   content, elevated=…)
8. runner.run(["systemctl", <scope_flag>, "daemon-reload"], elevated=…)
9. If spec.enabled:
       runner.run(["systemctl", <scope_flag>, "enable", "--now",
                   f"{spec.name}.timer"], elevated=…)
10. Return SUCCESS (or FAILED with message="enable_failed" at step 9).
```

## `delete_task` Sequence

```
1. If neither <name>.timer nor <name>.service exists → SKIPPED.
2. If dry_run → DRY_RUN.
3. runner.run(["systemctl", <scope>, "disable", "--now",
                f"{name}.timer"], elevated=…)   # non-fatal
4. file_operator.delete(<unit_dir>/<name>.timer, elevated=…)
5. file_operator.delete(<unit_dir>/<name>.service, elevated=…)
6. runner.run(["systemctl", <scope>, "daemon-reload"], elevated=…)
7. Return SUCCESS (or FAILED with step-identifying message on any
   failure at steps 4–6).
```

---

## `validate_schedule` Logic

Pure-Python, no external process.

```
ONCALENDAR_SHORTHANDS = {"minutely", "hourly", "daily",
                         "weekly", "monthly", "yearly"}

1. Strip whitespace.
2. Lowercase-match against ONCALENDAR_SHORTHANDS → True.
3. If the string has the shape
   `(<dow>) <YYYY>-<MM>-<DD> <HH>:<MM>:<SS>`
   with wildcards/ranges per systemd.time(7) → True.
4. Otherwise, try the five-field cron validator (for the cron
   fallback): split on whitespace, ensure 5 fields, each validated
   via the same rules as the RHEL reference.
5. Otherwise → False.
```

---

## `get_task` Parsing

`systemctl show <name>.timer --no-pager` emits `Key=Value` lines.  The parser
captures:

- `Id` → confirmed unit name
- `LoadState` → `not-found` maps `get_task` to `None`
- `UnitFileState` → `enabled` / `disabled` / `static` → `UbuntuTaskInfo.enabled`
- `NextElapseUSecRealtime` → `next_run` (µs since epoch)
- `LastTriggerUSec` → `last_run`

The corresponding `<name>.service` show output adds:

- `ExecStart` → `command`
- `User` → `user`
- `ActiveState` / `SubState` → `active`

---

## `list_tasks` Parsing

`systemctl list-timers --all --no-legend --no-pager` has columns:
`NEXT LEFT LAST PASSED UNIT ACTIVATES`.  The parser strips the `.timer`
suffix from `UNIT`, marks `active=True` iff `NEXT != "n/a"`, and records
`schedule=None` (OnCalendar is only recoverable via `systemctl show`).

`systemctl list-units --type=timer --all --no-legend --no-pager` covers
never-run timers absent from `list-timers`.  Duplicates are de-duplicated by
timer name (list-timers wins).

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `create_task` | Both unit files exist AND render-compare match AND `enabled-state` matches `spec.enabled` | `file_operator.read_text` + `systemctl show` |
| `delete_task` | Neither unit file exists | `file_operator.exists` |
| `enable_task` | `UnitFileState=enabled` | `systemctl show <name>.timer` |
| `disable_task` | `UnitFileState=disabled` | `systemctl show <name>.timer` |
| `run_task_now` | never skips | — |

---

## Error Handling

| Exception | Raised When |
|---|---|
| `UbuntuScheduledTaskQueryError` | `list_tasks` fails, or `get_task` fails for a reason other than the unit not being found |

All write failures surface as
`UbuntuScheduledTaskActionResult(status=FAILED, ...)` carrying the runner's
(or file-operator's) exit code, stdout, stderr, and a `message` identifying
the failing step.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system—essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Create Task Schedule Validation

*For any* `create_task(spec)` whose `spec.schedule` is invalid, the result is `FAILED` with `message="invalid_schedule"` and the runner / file operator are not invoked.

**Validates: Requirements 1.2**

### Property 2: Create Task Unit-File Rendering

*For any* `create_task(spec)` with a valid schedule and no pre-existing files, both `<name>.service` and `<name>.timer` are written via the file operator with byte content produced by the templates.

**Validates: Requirements 1.1**

### Property 3: Create Task Idempotency

*For any* `create_task(spec)` where both unit files already exist with byte-identical content and the enabled-state matches, the result is `SKIPPED` and no file writes or command dispatches occur.

**Validates: Requirements 1.3**

### Property 4: Create Task Daemon-Reload And Enable

*For any* `create_task(spec)` with `spec.enabled=True`, `systemctl daemon-reload` and `systemctl enable --now <name>.timer` are dispatched in that order after the writes succeed.

**Validates: Requirements 1.1**

### Property 5: Create Task Enable Failure Reporting

*For any* `create_task(spec)` with `spec.enabled=True` where the enable dispatch fails, the result is `FAILED` with `message="enable_failed"` and the runner's exit code / stderr are captured.

**Validates: Requirements 1.6**

### Property 6: Delete Task Sequence

*For any* `delete_task(name, owner)` where the timer exists, the dispatched sequence is: disable --now, delete `.timer`, delete `.service`, daemon-reload.

**Validates: Requirements 2.1**

### Property 7: Delete Task Idempotency

*For any* `delete_task(name, owner)` where neither unit file exists, the result is `SKIPPED`.

**Validates: Requirements 2.2**

### Property 8: Enable Task Idempotency

*For any* `enable_task(name, owner)` where `UnitFileState=enabled`, the result is `SKIPPED`.

**Validates: Requirements 3.3**

### Property 9: Enable Task Not Found

*For any* `enable_task(name, owner)` where the timer does not exist, the result is `FAILED` with `message="task_not_found"`.

**Validates: Requirements 3.2**

### Property 10: Disable Task Idempotency

*For any* `disable_task(name, owner)` where `UnitFileState=disabled`, the result is `SKIPPED`.

**Validates: Requirements 4.3**

### Property 11: Disable Task Not Found

*For any* `disable_task(name, owner)` where the timer does not exist, the result is `FAILED` with `message="task_not_found"`.

**Validates: Requirements 4.2**

### Property 12: Run Task Now Dispatch

*For any* `run_task_now(name, owner)` where the service exists, the runner is dispatched with `["systemctl", <scope>, "start", f"{name}.service"]`.

**Validates: Requirements 5.1**

### Property 13: Run Task Now Not Found

*For any* `run_task_now(name, owner)` where the service unit does not exist, the result is `FAILED` with `message="task_not_found"`.

**Validates: Requirements 5.2**

### Property 14: Get Task Populated Info

*For any* `get_task(name, owner)` where the timer exists, the returned `UbuntuTaskInfo` contains `schedule`, `command`, `enabled`, `active`, `last_run`, and `next_run` populated from `systemctl show`.

**Validates: Requirements 6.1**

### Property 15: Get Task Missing Returns None

*For any* `get_task(name, owner)` where `LoadState=not-found`, the result is `None`.

**Validates: Requirements 6.2**

### Property 16: Get Task Unexpected Error

*For any* other failure of `systemctl show`, `get_task` raises `UbuntuScheduledTaskQueryError`.

**Validates: Requirements 6.3**

### Property 17: List Tasks Merge

*For any* `list_tasks(owner)`, the returned list is the union of `list-timers` and `list-units --type=timer --all`, de-duplicated by timer name.

**Validates: Requirements 7.1, 7.2**

### Property 18: List Tasks Error

*For any* failure of `systemctl list-timers` or `list-units`, `list_tasks` raises `UbuntuScheduledTaskQueryError`.

**Validates: Requirements 7.3**

### Property 19: Validate Schedule Shorthand

*For any* shorthand in `{minutely, hourly, daily, weekly, monthly, yearly}`, `validate_schedule` returns `True`.

**Validates: Requirements 8.1**

### Property 20: Validate Schedule OnCalendar

*For any* systemd OnCalendar expression matching the documented shape, `validate_schedule` returns `True`.

**Validates: Requirements 8.1**

### Property 21: Validate Schedule Cron Fallback

*For any* five-field cron expression that passes the cron validator, `validate_schedule` returns `True`.

**Validates: Requirements 8.2**

### Property 22: Validate Schedule Side-Effect Free

*For any* `validate_schedule(expr)` call, the runner and file operator are never invoked.

**Validates: Requirements 8.4**

### Property 23: Dry-Run Safety

*For any* write method with `dry_run=True`, the file operator and runner are never invoked and the returned status is `DRY_RUN`.

**Validates: Requirements 1.8, 2.4, 3.4, 4.4, 5.5**

### Property 24: System-Owner Elevation

*For any* write with `spec.owner=SYSTEM` and `config.elevated=True`, both the file operator and the runner are invoked with `elevated=True`.

**Validates: Requirements 9.1**

### Property 25: Same-User-Owner No Elevation

*For any* write with `spec.owner=USER` and `spec.user` equal to the running user, the file operator and runner are invoked with `elevated=False`.

**Validates: Requirements 9.2**

### Property 26: Other-User-Owner Elevation

*For any* write with `spec.owner=USER` and `spec.user` differing from the running user, the file operator and runner are invoked with `elevated=True`, and the runner argv is wrapped with `runuser -u <user> -- systemctl --user ...`.

**Validates: Requirements 9.3**

### Property 27: Reads Do Not Elevate

*For any* read (`get_task`, `list_tasks`), the runner is never invoked with `elevated=True`.

**Validates: Requirements 9.4**

---

## Module Layout

```
source/packages/ccai/sysman/linux/ubuntu/
├── __init__.py
└── scheduledtasks.py    ← UbuntuScheduledTaskManager, models, config, exception

source/testroots/testbase/
└── ccai/tests/sysman/linux/ubuntu/
    └── test_scheduledtasks.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.linux.ubuntu.runners import (
    UbuntuLocalCommandRunner, UbuntuLocalRunnerConfig,
    UbuntuLocalFileOperator, UbuntuLocalFileOperatorConfig,
)
from ccai.sysman.linux.ubuntu.scheduledtasks import (
    UbuntuScheduledTaskManager,
    UbuntuScheduledTaskManagerConfig,
    UbuntuTaskSpec,
    UbuntuTaskOwner,
)

runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
file_operator = UbuntuLocalFileOperator(UbuntuLocalFileOperatorConfig())

config = UbuntuScheduledTaskManagerConfig(elevated=True, dry_run=False)
task_mgr = UbuntuScheduledTaskManager(
    runner=runner, file_operator=file_operator, config=config,
)

spec = UbuntuTaskSpec(
    name="nightly-backup",
    schedule="*-*-* 02:00:00",
    command="/usr/local/bin/backup.sh",
    owner=UbuntuTaskOwner.SYSTEM,
    description="Nightly filesystem backup",
)
result = task_mgr.create_task(spec=spec)
print(f"create_task: {result.status.value}")
```

### SSH Execution With Aspects

```python
from ccai.sysman.linux.ubuntu.runners import (
    UbuntuSSHCommandRunner, UbuntuSSHRunnerConfig,
)
from ccai.sysman.linux.ubuntu.aspects import (
    PersistenceAspect, RetryAspect, TimeoutAspect,
    PersistenceAspectConfig, RetryAspectConfig, TimeoutAspectConfig,
)
from ccai.sysman.linux.ubuntu.storage import SQLiteStorage

runner = UbuntuSSHCommandRunner(
    UbuntuSSHRunnerConfig(host="ubuntu-prod-01", username="ops"),
)
runner.add_aspect(aspect=PersistenceAspect(
    PersistenceAspectConfig(storage_backend=SQLiteStorage(path="/var/log/audit.db")),
))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2)))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=30.0)))

task_mgr = UbuntuScheduledTaskManager(
    runner=runner, file_operator=file_operator,
    config=UbuntuScheduledTaskManagerConfig(elevated=True),
)
task_mgr.disable_task(name="stale-probe", owner=UbuntuTaskOwner.SYSTEM)
```

### Dry-Run Mode

```python
config = UbuntuScheduledTaskManagerConfig(elevated=True, dry_run=True)
task_mgr = UbuntuScheduledTaskManager(
    runner=runner, file_operator=file_operator, config=config,
)
result = task_mgr.delete_task(name="nightly-backup", owner=UbuntuTaskOwner.SYSTEM)
assert result.status.value == "dry_run"
print(result.command)
```

---

## Testing Strategy

1. `MockFileOperator` records `write_text`, `delete`, `read_text`, `exists`
   calls with their arguments; tests assert exact path + content round-trips.
2. `MockCommandRunner` keyed on argv prefix lets each test queue
   responses for `systemctl daemon-reload`, `enable`, `disable`,
   `start`, `show`, `list-timers`, and `list-units` independently.
3. `create_task` tests cover: valid schedule, invalid schedule (no
   dispatch), byte-identical idempotency, render mismatch triggering
   rewrite, enable-step failure after successful writes.
4. `delete_task` tests cover: both files present, one file present,
   neither file present (SKIPPED).
5. `enable_task` / `disable_task` tests cover: already-enabled,
   already-disabled, not-found.
6. `run_task_now` tests cover: service exists (SUCCESS), service
   missing (FAILED with message), runner failure (FAILED with
   captured exit code).
7. `get_task` tests cover: populated info, `LoadState=not-found`
   → `None`, unexpected error → `UbuntuScheduledTaskQueryError`.
8. `list_tasks` tests cover: union of `list-timers` and `list-units`,
   empty output, failure → raises.
9. `validate_schedule` tests cover: shorthands, OnCalendar shapes,
   cron-fallback shapes, and invalid strings.
10. Dry-run tests prove every write method short-circuits without
    invoking the file operator or runner.
11. Elevation tests prove system-owner writes elevate when
    configured, user-self writes do not, and other-user writes wrap
    with `runuser` and elevate.
12. No test requires root access, a live systemd instance, or a
    networked target.

---

## Scope Mapping

| Contract verb | Ubuntu implementation |
|---|---|
| `create_task` | file-op writes + `systemctl daemon-reload` + optional `enable --now` |
| `delete_task` | `systemctl disable --now` + file-op deletes + `daemon-reload` |
| `enable_task` | `systemctl enable --now <name>.timer` |
| `disable_task` | `systemctl disable --now <name>.timer` |
| `run_task_now` | `systemctl start <name>.service` |
| `get_task` | `systemctl show <name>.timer` + `systemctl show <name>.service` |
| `list_tasks` | `systemctl list-timers --all` + `list-units --type=timer --all` |
| `validate_schedule` | pure-Python OnCalendar + five-field cron check |

---

## Extensions Beyond the Contract

### Cron Drop-In Fallback

Legacy callers that want `/etc/cron.d/<name>` drop-in files instead of
systemd timers can set `UbuntuTaskSpec.owner=SYSTEM` plus a
manager-level `cron_backend=True` extension flag (reserved; not part of
the base contract).  The render path matches the RHEL reference's cron
templates and writes via the file operator.  The validator accepts
five-field cron expressions to support this path.

### Per-User Calendar Sync

macOS and Windows cover per-user calendar-bound tasks; Ubuntu's systemd
`--user` scope already does this, but a future extension will add
`calendar_sync=True` that chains a `loginctl enable-linger <user>` call so
user-scope timers fire even when the user is not logged in.  This chain
is documented here but not implemented in the base contract surface.
