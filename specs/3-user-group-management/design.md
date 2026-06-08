# Design

## Overview

The user and group management feature is implemented as a single module,
`ccai.sysman.linux.ubuntu.usergroupmanagement`, that exposes a
`UbuntuUserGroupManager` class.  This spec implements the user-group-management contract
defined at `codecraftai-sysman-common/specs/contracts/user-group-management/contract.md`
for Ubuntu using the standard `shadow-utils` tooling (`useradd`, `userdel`, `usermod`,
`groupadd`, `groupdel`, `gpasswd`, `passwd`, `chpasswd`) together with the name-service
query tool `getent`.

All read paths used for idempotency guards go through `getent passwd`, `getent group`,
or `passwd -S`; the output is parsed in pure Python into `UbuntuUserInfo` /
`UbuntuGroupInfo` records.  All system interaction is delegated to a command runner
implementing the `ICommandRunner` interface.  No direct file system access or
`subprocess` calls are made inside the module.

Passwords are forwarded to `chpasswd` via stdin so no plaintext credential ever reaches
argv, a persistence aspect, or the returned `command` field.

The feature works identically whether commands execute locally (via `LocalCommandRunner`)
or remotely over SSH (via `SSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md) for
details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into UbuntuUserGroupManager               │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuUserGroupManager                           │
│  ccai.sysman.linux.ubuntu.usergroupmanagement           │
│                                                             │
│  create_user()    delete_user()    modify_user()            │
│  get_user()       list_users()     set_password()           │
│  lock_account()   unlock_account()                          │
│  create_group()   delete_group()                            │
│  get_group()      list_groups()                             │
│  add_to_group()   remove_from_group()                       │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, stdin, capture_output) → RunResult     │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH connection     │
│ - sudo for elevation │      │ - reliability metrics│
│ - local execution    │      │ - connection retry   │
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect: audit logging                         │
│  - RetryAspect: transient failure retry                     │
│  - TimeoutAspect: global timeout policy                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `UbuntuUserGroupManager`

**Location:** `source/packages/ccai/sysman/linux/ubuntu/usergroupmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `create_user` | `(spec) → UbuntuUserGroupActionResult` | `useradd` with spec fields mapped to flags |
| `delete_user` | `(name, remove_home) → UbuntuUserGroupActionResult` | `userdel [-r] <name>` |
| `modify_user` | `(name, changes) → UbuntuUserGroupActionResult` | `usermod` with diff-driven flags |
| `get_user` | `(name) → UbuntuUserInfo \| None` | `getent passwd` + `passwd -S` for lock state |
| `list_users` | `() → list[UbuntuUserInfo]` | `getent passwd` filtered by UID range |
| `set_password` | `(name, password) → UbuntuUserGroupActionResult` | `chpasswd` via stdin |
| `lock_account` | `(name) → UbuntuUserGroupActionResult` | `passwd -l <name>` |
| `unlock_account` | `(name) → UbuntuUserGroupActionResult` | `passwd -u <name>` |
| `create_group` | `(spec) → UbuntuUserGroupActionResult` | `groupadd` with optional `-g`/`-r` |
| `delete_group` | `(name) → UbuntuUserGroupActionResult` | `groupdel <name>` |
| `get_group` | `(name) → UbuntuGroupInfo \| None` | `getent group <name>` |
| `list_groups` | `() → list[UbuntuGroupInfo]` | `getent group` |
| `add_to_group` | `(user, group) → UbuntuUserGroupActionResult` | `gpasswd -a <user> <group>` |
| `remove_from_group` | `(user, group) → UbuntuUserGroupActionResult` | `gpasswd -d <user> <group>` |

### `UbuntuUserGroupManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `uid_min` | `int` | `1000` | Lower bound for "local" UID range used by `list_users` |
| `uid_max` | `int` | `60000` | Upper bound for "local" UID range used by `list_users` |

---

## Data Models

### Result Types

```python
class UbuntuUserGroupActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class UbuntuUserGroupActionResult:
    status: UbuntuUserGroupActionStatus
    target: str                       # user or group name
    kind: str                         # "user" or "group"
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None         # argv — never contains a plaintext password
```

### Spec / Info Types

```python
@dataclass
class UbuntuUserSpec:
    name: str
    uid: int | None
    home: str | None
    shell: str | None
    primary_group: str | None
    comment: str | None
    system_account: bool

@dataclass
class UbuntuUserModSpec:
    shell: str | None
    home: str | None
    move_home: bool
    primary_group: str | None
    comment: str | None
    uid: int | None

@dataclass
class UbuntuUserInfo:
    name: str
    uid: int
    gid: int
    comment: str
    home: str
    shell: str
    locked: bool

@dataclass
class UbuntuGroupSpec:
    name: str
    gid: int | None
    system_group: bool

@dataclass
class UbuntuGroupInfo:
    name: str
    gid: int
    members: list[str]
```

### Exceptions

```python
class UbuntuUserNotFoundError(Exception):
    """Raised when a user-targeting verb cannot locate the user."""

class UbuntuGroupNotFoundError(Exception):
    """Raised when a group-targeting verb cannot locate the group."""

class UbuntuUserGroupQueryError(Exception):
    """Raised when a getent / passwd query fails unexpectedly."""
```

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        stdin: str | None = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

No `IFileOperator` is required: every read and write path flows through the shadow-utils
CLIs and `getent`.

---

## User-vs-Group Mapping

Ubuntu uses the same name-service switch and password / group / shadow files as every
other Debian derivative: `/etc/passwd`, `/etc/group`, `/etc/shadow`.  UIDs and GIDs are
POSIX integers.  The module relies exclusively on `getent` rather than reading the files
directly so that any nsswitch plugin (e.g. `systemd-userdb`, `nss-ldap`) transparently
participates without code changes.

The default "local" UID range used by `list_users` is `[1000, 60000]`, matching the
`UID_MIN` / `UID_MAX` defaults shipped in `/etc/login.defs` on modern Ubuntu releases.
Callers may override the range via `UbuntuUserGroupManagerConfig.uid_min` /
`uid_max`.  System users (UID `< UID_MIN`) are always included.

---

## Command Mappings

### User Write Commands

| Operation | Command |
|---|---|
| create_user (normal) | `useradd -m [-s <shell>] [-u <uid>] [-g <group>] [-c <comment>] [-d <home>] <name>` |
| create_user (system) | `useradd -r ...<same flags>... <name>` |
| delete_user (remove home) | `userdel -r <name>` |
| delete_user (keep home) | `userdel <name>` |
| modify_user | `usermod [-s <shell>] [-d <home> [-m]] [-g <group>] [-c <comment>] [-u <uid>] <name>` |
| set_password | `chpasswd` (password supplied via stdin as `<name>:<password>\n`) |
| lock_account | `passwd -l <name>` |
| unlock_account | `passwd -u <name>` |

### Group Write Commands

| Operation | Command |
|---|---|
| create_group (normal) | `groupadd [-g <gid>] <name>` |
| create_group (system) | `groupadd -r [-g <gid>] <name>` |
| delete_group | `groupdel <name>` |
| add_to_group | `gpasswd -a <user> <group>` |
| remove_from_group | `gpasswd -d <user> <group>` |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| get_user / list_users | `getent passwd [<name>]` |
| lock state | `passwd -S <name>` |
| get_group / list_groups | `getent group [<name>]` |

All write commands pass `elevated=True` when the config enables it.  Read commands never
elevate regardless of configuration.

---

## Output Parsing

### `getent passwd` output

A single line:

```
svc-build:x:1002:1002:Build Service:/var/lib/svc-build:/usr/sbin/nologin
```

The parser splits on `:` producing the seven fields `(name, password, uid, gid, gecos,
home, shell)`.  The `password` column is ignored (always `x` under shadow).  The parsed
record becomes a `UbuntuUserInfo` with `locked=False`; the lock flag is populated
separately by parsing `passwd -S <name>`.

### `passwd -S` output

```
svc-build L 2024-04-01 0 99999 7 -1
```

The second whitespace-separated token is the status character:

| Token | Meaning | `locked` |
|---|---|---|
| `L` | password locked | `True` |
| `P` | usable password set | `False` |
| `NP` | no password | `False` |

### `getent group` output

```
docker:x:998:alice,bob
```

The parser splits on `:` producing `(name, password, gid, members_csv)`.
`members_csv` is split on `,`, stripped of empty strings, and becomes
`UbuntuGroupInfo.members`.

---

## Idempotency Guards

All idempotency checks call `get_user`, `get_group`, or inspect the user's lock state via
`passwd -S` before any write, then compare the desired state to the current state in pure
Python.

| Method | Guard | Source |
|---|---|---|
| `create_user` | user already exists | `get_user(name) is not None` |
| `delete_user` | user does not exist | `get_user(name) is None` |
| `modify_user` | every non-`None` field already matches | compare `UbuntuUserInfo` fields |
| `set_password` | no idempotency guard (always runs when user exists) | — |
| `lock_account` | already locked | `get_user(name).locked is True` |
| `unlock_account` | already unlocked | `get_user(name).locked is False` |
| `create_group` | group already exists | `get_group(name) is not None` |
| `delete_group` | group does not exist | `get_group(name) is None` |
| `add_to_group` | user already in group's members list | `user in get_group(group).members` |
| `remove_from_group` | user not in group's members list | `user not in get_group(group).members` |

Preconditions raised before any idempotency check:

- `modify_user`, `set_password`, `lock_account`, `unlock_account`, `add_to_group`,
  `remove_from_group` raise `UbuntuUserNotFoundError` when `get_user(name)` returns
  `None`.
- `add_to_group`, `remove_from_group` raise `UbuntuGroupNotFoundError` when
  `get_group(group)` returns `None`.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UbuntuUserNotFoundError` | A user-targeting write is called with a non-existent user |
| `UbuntuGroupNotFoundError` | A group-targeting write is called with a non-existent group |
| `UbuntuUserGroupQueryError` | `getent` or `passwd -S` exits with an unexpected non-zero code (not the documented `2` for "not found") |

All other failures — non-zero exit codes from `useradd`, `userdel`, `usermod`, `groupadd`,
`groupdel`, `gpasswd`, `passwd`, `chpasswd` — are surfaced as
`UbuntuUserGroupActionResult(status=FAILED, ...)` carrying the runner's exit code, stdout,
and stderr.  Writes never raise for routine I/O failures.

### Password Confidentiality

`set_password` builds a `stdin` payload `"<name>:<password>\n"` but stores
`command=["chpasswd"]` (or `["sudo", "chpasswd"]` when the runner reports elevation) in
the result.  The persistence aspect records `command` and `exit_code` / `stdout` /
`stderr` — none of which contain the plaintext password unless `chpasswd` itself echoes
it back, which it does not.  Tests assert that the password string does not appear in
any field of `UbuntuUserGroupActionResult`.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: create_user Command Construction

*For any* `UbuntuUserSpec`, `create_user()` should produce a `useradd -m [-s][-u][-g][-c][-d] <name>` argv reflecting the non-`None` spec fields and pass it to the runner.

**Validates: Requirements 1.1**

### Property 2: create_user System Flag

*For any* `UbuntuUserSpec` with `system_account=True`, `create_user()` should include the `-r` flag in the argv.

**Validates: Requirements 1.2**

### Property 3: create_user Idempotency

*For any* user already present in `getent passwd`, `create_user()` should return `SKIPPED` without invoking `useradd`.

**Validates: Requirements 1.3**

### Property 4: delete_user Command Construction

*For any* existing user, `delete_user(name, remove_home=True)` should construct `userdel -r <name>`; `remove_home=False` should construct `userdel <name>`.

**Validates: Requirements 2.1, 2.2**

### Property 5: delete_user Idempotency

*For any* user absent from `getent passwd`, `delete_user()` should return `SKIPPED` without invoking `userdel`.

**Validates: Requirements 2.3**

### Property 6: modify_user Diff-Driven Argv

*For any* `UbuntuUserModSpec`, `modify_user()` should emit only the flags corresponding to non-`None` fields, and should emit `-d <home> -m` together when `move_home=True`.

**Validates: Requirements 3.1**

### Property 7: modify_user Precondition

*For any* non-existent user, `modify_user()` should raise `UbuntuUserNotFoundError` before invoking `usermod`.

**Validates: Requirements 3.2**

### Property 8: modify_user Idempotency

*For any* `UbuntuUserModSpec` whose non-`None` fields already match the current `UbuntuUserInfo`, `modify_user()` should return `SKIPPED`.

**Validates: Requirements 3.3**

### Property 9: get_user Parsing

*For any* `getent passwd <name>` line, `get_user()` should return a `UbuntuUserInfo` with `name`, `uid`, `gid`, `comment`, `home`, and `shell` populated from the colon-separated fields, and `locked` derived from `passwd -S`.

**Validates: Requirements 4.1, 4.2**

### Property 10: get_user Missing Returns None

*For any* name for which `getent` exits `2`, `get_user()` should return `None`.

**Validates: Requirements 4.3**

### Property 11: list_users Range Filter

*For any* `getent passwd` output, `list_users()` should return only entries with UID below `uid_min` or within `[uid_min, uid_max]`.

**Validates: Requirements 5.2**

### Property 12: set_password Stdin Delivery

*For any* call to `set_password(name, password)`, the password MUST be delivered via the runner's stdin as `"<name>:<password>\n"` and MUST NOT appear in the returned `UbuntuUserGroupActionResult.command`, `stdout`, or `stderr`.

**Validates: Requirements 6.1, 6.2**

### Property 13: set_password Precondition

*For any* non-existent user, `set_password()` should raise `UbuntuUserNotFoundError` before any command dispatch.

**Validates: Requirements 6.3**

### Property 14: lock_account Idempotency

*For any* user whose `UbuntuUserInfo.locked` is `True`, `lock_account()` should return `SKIPPED` without dispatching `passwd -l`.

**Validates: Requirements 7.2**

### Property 15: unlock_account Idempotency

*For any* user whose `UbuntuUserInfo.locked` is `False`, `unlock_account()` should return `SKIPPED` without dispatching `passwd -u`.

**Validates: Requirements 8.2**

### Property 16: create_group Command Construction

*For any* `UbuntuGroupSpec`, `create_group()` should construct a `groupadd [-g <gid>] [-r] <name>` argv with `-r` present when `system_group=True`.

**Validates: Requirements 9.1**

### Property 17: create_group Idempotency

*For any* group already present in `getent group`, `create_group()` should return `SKIPPED`.

**Validates: Requirements 9.2**

### Property 18: delete_group Idempotency

*For any* group absent from `getent group`, `delete_group()` should return `SKIPPED`.

**Validates: Requirements 10.2**

### Property 19: get_group Parsing

*For any* `getent group <name>` line, `get_group()` should return a `UbuntuGroupInfo` with `name`, `gid`, and `members` populated from the colon-separated fields.

**Validates: Requirements 11.1, 11.2**

### Property 20: list_groups Parsing

*For any* successful `getent group` output, `list_groups()` should parse every line into a `UbuntuGroupInfo`.

**Validates: Requirements 12.1, 12.2**

### Property 21: add_to_group Command Construction

*For any* existing user and group, `add_to_group(user, group)` should dispatch `gpasswd -a <user> <group>`.

**Validates: Requirements 13.1**

### Property 22: add_to_group Preconditions

*For any* non-existent user, `add_to_group()` should raise `UbuntuUserNotFoundError`; for any non-existent group, it should raise `UbuntuGroupNotFoundError`.

**Validates: Requirements 13.2, 13.3**

### Property 23: add_to_group Membership Idempotency

*For any* user already listed in `UbuntuGroupInfo.members`, `add_to_group()` should return `SKIPPED`.

**Validates: Requirements 13.4**

### Property 24: remove_from_group Command Construction

*For any* existing user and group with the user as a member, `remove_from_group(user, group)` should dispatch `gpasswd -d <user> <group>`.

**Validates: Requirements 14.1**

### Property 25: remove_from_group Idempotency

*For any* user not listed in `UbuntuGroupInfo.members`, `remove_from_group()` should return `SKIPPED`.

**Validates: Requirements 14.4**

### Property 26: Dry-Run Safety

*For any* write operation (create_user, delete_user, modify_user, set_password, lock_account, unlock_account, create_group, delete_group, add_to_group, remove_from_group) with `dry_run=True`, the command runner MUST NOT be invoked and the result status MUST be `DRY_RUN`.

**Validates: Requirements 1.6, 2.6, 3.6, 6.5, 7.6, 8.6, 9.5, 10.5, 13.7, 14.7**

### Property 27: Elevation for Writes

*For any* write operation with `elevated=True` in the config, the command runner MUST be invoked with `elevated=True`.

**Validates: Requirements 15.1**

### Property 28: No Elevation for Reads

*For any* read operation (`get_user`, `list_users`, `get_group`, `list_groups`), the command runner MUST NEVER be invoked with `elevated=True`.

**Validates: Requirements 15.2**

### Property 29: Error Result Consistency

*For any* non-zero exit from a write command, the returned `UbuntuUserGroupActionResult` should have `status=FAILED`, the runner's exit code in `exit_code`, and the runner's stderr in `stderr`.

**Validates: Requirements 1.5, 2.5, 3.5, 6.4, 7.5, 8.5, 9.4, 10.4, 13.6, 14.6**

---

## Module Layout

```
source/packages/ccai/sysman/linux/ubuntu/
├── __init__.py
└── usergroupmanagement.py        ← UbuntuUserGroupManager, config, models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/linux/ubuntu/
    └── test_usergroupmanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.linux.ubuntu.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.linux.ubuntu.usergroupmanagement import (
    UbuntuUserGroupManager,
    UbuntuUserGroupManagerConfig,
    UbuntuUserSpec,
    UbuntuGroupSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())

config = UbuntuUserGroupManagerConfig(elevated=True, dry_run=False)
mgr = UbuntuUserGroupManager(runner=runner, config=config)

user_spec = UbuntuUserSpec(
    name="svc-build",
    uid=1500,
    home="/var/lib/svc-build",
    shell="/usr/sbin/nologin",
    primary_group="svc-build",
    comment="Build service account",
    system_account=False,
)
result = mgr.create_user(spec=user_spec)
print(f"create_user: {result.status}")

group_spec = UbuntuGroupSpec(name="build-ops", gid=None, system_group=False)
mgr.create_group(spec=group_spec)

mgr.add_to_group(user="svc-build", group="build-ops")

mgr.set_password(name="svc-build", password="rotated-by-vault")

mgr.lock_account(name="svc-build")

user_info = mgr.get_user(name="svc-build")
print(f"locked: {user_info.locked}")
```

### SSH Execution with Aspects

```python
from ccai.sysman.linux.ubuntu.runners import SSHCommandRunner, SSHRunnerConfig
from ccai.sysman.linux.ubuntu.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from ccai.sysman.linux.ubuntu.storage import SQLiteStorage
from ccai.sysman.linux.ubuntu.usergroupmanagement import (
    UbuntuUserGroupManager,
    UbuntuUserGroupManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-ubuntu", username="admin"))

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
persistence = PersistenceAspect(PersistenceAspectConfig(storage_backend=storage))
runner.add_aspect(aspect=persistence)

retry = RetryAspect(RetryAspectConfig(max_retries=2, retryable_exit_codes=[1]))
runner.add_aspect(aspect=retry)

timeout = TimeoutAspect(TimeoutAspectConfig(default_timeout=30.0))
runner.add_aspect(aspect=timeout)

mgr = UbuntuUserGroupManager(
    runner=runner,
    config=UbuntuUserGroupManagerConfig(elevated=True),
)

for user in mgr.list_users():
    print(f"{user.name}\t{user.uid}\t{user.locked}")
```

### Dry-Run Mode

```python
config = UbuntuUserGroupManagerConfig(elevated=True, dry_run=True)
mgr = UbuntuUserGroupManager(runner=runner, config=config)

result = mgr.delete_user(name="svc-retired", remove_home=True)
assert result.status == UbuntuUserGroupActionStatus.DRY_RUN
print(f"would run: {result.command}")
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on `(argv_prefix, stdin_present)` so that
  `chpasswd` + stdin can be distinguished from argv-based commands.
- `getent passwd` / `getent group` mock responses drive every idempotency guard.
- `passwd -S` mock responses drive lock-state detection for
  `lock_account` / `unlock_account`.
- A dedicated test proves `set_password` delivers the plaintext via stdin and that the
  plaintext does not appear anywhere in the returned `UbuntuUserGroupActionResult`.
- Preconditions are covered by tests that assert `UbuntuUserNotFoundError` and
  `UbuntuGroupNotFoundError` raise before any runner call for `modify_user`,
  `set_password`, `lock_account`, `unlock_account`, `add_to_group`,
  `remove_from_group`.
- Dry-run tests prove that every write short-circuits to `DRY_RUN` without invoking the
  runner.
- Elevation tests prove writes receive `elevated=True` when configured and reads never
  receive `elevated=True`.
- Parser tests cover `getent passwd`, `getent group`, and `passwd -S` output with a
  representative mix of normal, system, and locked accounts.
- No test requires root access or a live `/etc/passwd`.

---

## Scope Mapping

| Contract verb | Ubuntu implementation |
|---|---|
| `create_user` | `useradd` |
| `delete_user` | `userdel` |
| `modify_user` | `usermod` |
| `get_user` | `getent passwd` + `passwd -S` |
| `list_users` | `getent passwd` (filtered by UID range) |
| `set_password` | `chpasswd` via stdin |
| `lock_account` | `passwd -l` |
| `unlock_account` | `passwd -u` |
| `create_group` | `groupadd` |
| `delete_group` | `groupdel` |
| `get_group` | `getent group` |
| `list_groups` | `getent group` |
| `add_to_group` | `gpasswd -a` |
| `remove_from_group` | `gpasswd -d` |

---

## Extensions Beyond the Contract

None in this Ubuntu implementation.  The RHEL reference's `add_ssh_key`,
`remove_ssh_key`, `set_password_age`, and `expire_password` extensions are not included
— when they are added they will live behind their own feature flags so a coordinator
writing against the contract does not have to special-case Ubuntu.
