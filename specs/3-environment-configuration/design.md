# Design

## Overview

The environment configuration feature is implemented as a single module,
`ccai.sysman.linux.ubuntu.environmentconfiguration`, that exposes a
`UbuntuEnvironmentConfigurator` class.  This spec implements the
environment-configuration contract defined at
`codecraftai-sysman-common/specs/contracts/environment-configuration/contract.md`
for Ubuntu.

All file reads and writes go through the `IFileOperator` abstraction from
`codecraftai-interfaces`.  User home directory resolution is the only place where
`pwd.getpwnam` is called directly, isolated in a private helper.  No subprocess
calls are made; no direct `open`/`write` calls touch the filesystem outside the
file operator.  This keeps the module compatible with both local and SSH file
operators, supports dry-run mode uniformly, and makes aspects (persistence,
retry, timeout) transparent to the caller.

**See Also:** [Command Runner Architecture](../1-command-execution/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates file operator                               │
│  - Configures aspects (optional)                            │
│  - Injects file operator into UbuntuEnvironmentConfigurator │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        UbuntuEnvironmentConfigurator                        │
│  ccai.sysman.linux.ubuntu.environmentconfiguration      │
│                                                             │
│  set_system_env()    unset_system_env()                     │
│  get_system_env()    list_system_env()                      │
│  set_user_env()      unset_user_env()                       │
│  get_user_env()      list_user_env()                        │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        IFileOperator                                        │
│  read_text / write_text / delete / exists                   │
└────────────────────────┬────────────────────────────────────┘
                         │ implemented via
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  LocalCommandRunner or SSHCommandRunner                     │
└────────────────────────┬────────────────────────────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  PersistenceAspect / RetryAspect / TimeoutAspect            │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `UbuntuEnvironmentConfigurator`

**Location:** `source/packages/ccai/sysman/linux/ubuntu/environmentconfiguration.py`

| Method | Signature | Description |
|---|---|---|
| `set_system_env` | `(name, value) → UbuntuEnvActionResult` | Write to `/etc/environment` + drop-in |
| `unset_system_env` | `(name) → UbuntuEnvActionResult` | Remove from both files |
| `get_system_env` | `(name) → UbuntuEnvValue \| None` | Read merged system-scope value |
| `list_system_env` | `() → dict[str, str]` | Merge both files |
| `set_user_env` | `(user, name, value, rc_file=".profile") → UbuntuEnvActionResult` | Write to user rc file |
| `unset_user_env` | `(user, name, rc_file=".profile") → UbuntuEnvActionResult` | Remove from user rc file |
| `get_user_env` | `(user, name, rc_file=".profile") → UbuntuEnvValue \| None` | Read user-scope value |
| `list_user_env` | `(user, rc_file=".profile") → dict[str, str]` | Parse user rc file |

### `UbuntuEnvironmentConfiguratorConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no `write_text` or `delete` calls occur |
| `elevated` | `bool` | `False` | When `True`, system-scope writes and other-user writes pass `elevated=True` |

---

## Data Models

```python
class UbuntuEnvActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

class UbuntuEnvScope(Enum):
    SYSTEM = "system"
    USER   = "user"

@dataclass
class UbuntuEnvValue:
    name: str
    value: str
    scope: UbuntuEnvScope
    source: str | None        # file path

@dataclass
class UbuntuEnvActionResult:
    status: UbuntuEnvActionStatus
    scope: UbuntuEnvScope
    name: str
    value: str | None
    path: str | None
    user: str | None
    message: str | None

class UbuntuEnvConfigError(Exception):
    """Raised when a user-scope read targets an unknown user or a file is unreadable."""
```

---

## Interfaces

### `IFileOperator`

Imported from `codecraftai-interfaces`.  The configurator holds a single instance
and delegates all file I/O to it.

---

## Scope Mapping

| Abstract | Ubuntu backing storage |
|---|---|
| `SYSTEM` | `/etc/environment` (bare `KEY=VALUE`) AND `/etc/profile.d/ccai.sh` (`export KEY=VALUE`) |
| `USER` | `<home>/.profile` by default, with `<home>/.bashrc` and `<home>/.pam_environment` opt-in via `rc_file` |

---

## File Format Conventions

### `/etc/environment`

- No shebang, no `export` keyword.
- Each managed variable is exactly one line `NAME="value"`.
- Values containing `"` are escaped as `\"`.
- Lines not matching the target `NAME=` are preserved verbatim.

### `/etc/profile.d/ccai.sh`

- `#!/usr/bin/env bash` shebang on first line when the file is created.
- Each managed variable is exactly one line `export NAME="value"`.
- Values containing `"` are escaped as `\"`.
- When the last managed variable is unset and the file contains only the
  shebang + blank lines, the file is deleted via `IFileOperator.delete`.

### User rc file (`~<user>/.profile`, `.bashrc`, `.pam_environment`)

- Each managed variable is exactly one line `export NAME="value"` for shell rc
  files; `NAME="value"` (no `export`) for `.pam_environment`.
- Values containing `"` are escaped as `\"`.

---

## Parsing

### `_parse_bare_env(text)` — used for `/etc/environment` and `.pam_environment`

1. Split by line; strip; skip blank lines and `#`-leading.
2. Match `r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$'`.
3. Strip optional surrounding `"`; unescape `\"` → `"`.
4. Return `dict[str, str]`.

### `_parse_export_env(text)` — used for profile.d drop-ins and shell rc files

1. Split by line; strip; skip blank lines and `#`-leading.
2. Match `r'^export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$'`.
3. Strip optional surrounding `"`; unescape `\"` → `"`.
4. Return `dict[str, str]`.

---

## Line Update Strategy

All set/unset operations use an in-place line rewrite:

```
current_lines = file_operator.read_text(path).splitlines(keepends=True)
new_lines = _apply_change(current_lines, name, value_or_None)
file_operator.write_text(path, "".join(new_lines), elevated=elevated)
```

`_apply_change` for set:

1. Scan lines for a matching variable.
2. If found with same value → return unchanged (caller returns `SKIPPED`).
3. If found with different value → replace in place.
4. If not found → append new line.

`_apply_change` for unset:

1. Scan lines for a matching variable.
2. If not found → return unchanged (caller returns `SKIPPED`).
3. If found → remove that line.

Trailing newline is preserved: if the original file ends with `\n`, the
rewritten file does too.

---

## User Home Directory Resolution

```python
def _resolve_home(username: str) -> str:
    import pwd
    try:
        home = pwd.getpwnam(username).pw_dir
    except KeyError:
        raise UbuntuEnvConfigError(f"User not found: {username}")
    return home
```

Called by `set_user_env`, `unset_user_env`, `get_user_env`, and `list_user_env`
before any file operation.  For `set_user_env` / `unset_user_env` the caller-
facing code catches `UbuntuEnvConfigError` and converts it into a
`UbuntuEnvActionResult(status=FAILED, message=...)` to satisfy the contract's
`FAILED`-with-message behaviour for user-scope writes.

---

## Current-User Detection for Elevation

```python
def _is_current_user(username: str) -> bool:
    import pwd, os
    current = pwd.getpwuid(os.geteuid()).pw_name
    return current == username
```

Used by `set_user_env` / `unset_user_env` to suppress elevation when the target
is the caller's own account (typical in test/CI environments).

---

## Idempotency Guards

| Method | Guard | Mechanism |
|---|---|---|
| `set_system_env` | `/etc/environment` AND profile.d drop-in already have same value | Parse both + compare |
| `unset_system_env` | Neither file has the variable | Parse both + check |
| `set_user_env` | rc file already has same value | Parse + compare |
| `unset_user_env` | rc file missing or variable absent | `exists` + parse |

When a file does not exist:
- set operations: skip the read, create a new file with the single entry.
- unset operations: return `SKIPPED` immediately.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `UbuntuEnvConfigError` | `get_user_env` / `list_user_env` called with unknown user or unreadable source |

For user-scope writes, unknown-user is surfaced as
`UbuntuEnvActionResult(status=FAILED, message="user not found: <user>")` —
not raised — per contract.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all
valid executions of the system.*

### Property 1: `set_system_env` Writes Both Files
*For any* `(name, value)`, `set_system_env` should write the value as a bare
`KEY=VALUE` line in `/etc/environment` AND as an `export KEY=VALUE` line in
`/etc/profile.d/ccai.sh`.
**Validates: Requirements 1.1**

### Property 2: `set_system_env` Idempotency
*For any* `(name, value)` where BOTH files already contain the target line,
`set_system_env` should return `status=SKIPPED` without calling `write_text`.
**Validates: Requirements 1.2**

### Property 3: `set_system_env` Creates Missing Drop-In
*For any* `(name, value)` where `/etc/profile.d/ccai.sh` does not exist,
`set_system_env` should create it with a `#!/usr/bin/env bash` shebang plus
the export line.
**Validates: Requirements 1.3**

### Property 4: `set_system_env` Dry-Run
*For any* call with `dry_run=True`, `set_system_env` should return
`status=DRY_RUN` and not invoke `write_text` or `delete`.
**Validates: Requirements 1.4, 10.1**

### Property 5: `set_system_env` Elevation
*For any* call with `elevated=True`, `set_system_env` should pass
`elevated=True` to every `write_text` call.
**Validates: Requirements 1.5, 9.1**

### Property 6: `unset_system_env` Idempotency
*For any* name not present in either file, `unset_system_env` should return
`status=SKIPPED` without calling `write_text`.
**Validates: Requirements 2.1**

### Property 7: `unset_system_env` Environment File Rewrite
*For any* name present in `/etc/environment`, `unset_system_env` should
rewrite the file with the matching line removed.
**Validates: Requirements 2.2**

### Property 8: `unset_system_env` Drop-In Rewrite
*For any* name present in `/etc/profile.d/ccai.sh`, `unset_system_env`
should rewrite the file with the matching line removed.
**Validates: Requirements 2.3**

### Property 9: `unset_system_env` Empty-File Cleanup
*For any* unset that leaves the drop-in with only the shebang and blank lines,
`unset_system_env` should call `IFileOperator.delete(path)` instead of
`write_text`.
**Validates: Requirements 2.4**

### Property 10: `get_system_env` Precedence
*For any* variable present in both files, `get_system_env` should return the
value from `/etc/environment`; when values differ, the `message` field
reflects the discrepancy.
**Validates: Requirements 3.2**

### Property 11: `get_system_env` Absent
*For any* variable absent from both files, `get_system_env` should return
`None`.
**Validates: Requirements 3.3**

### Property 12: `list_system_env` Merge
*For any* call, `list_system_env` should return a dict merged from both files
with `/etc/environment` taking precedence on conflict.
**Validates: Requirements 4.1, 4.3**

### Property 13: `list_system_env` Missing Files
*For any* call where both files are missing, `list_system_env` should return
an empty dict.
**Validates: Requirements 4.4**

### Property 14: `set_user_env` Unknown User
*For any* unknown user, `set_user_env` should return `status=FAILED` with
`message="user not found: <user>"` and not invoke the file operator.
**Validates: Requirements 5.1**

### Property 15: `set_user_env` Home Resolution
*For any* known user, `set_user_env` should resolve the home directory via
`pwd.getpwnam(user).pw_dir`.
**Validates: Requirements 5.2**

### Property 16: `set_user_env` Creates Missing RC
*For any* known user where the rc file does not exist, `set_user_env` should
create it with the single export line.
**Validates: Requirements 5.3**

### Property 17: `set_user_env` Idempotency
*For any* known user where the rc file already contains the target value,
`set_user_env` should return `status=SKIPPED`.
**Validates: Requirements 5.4**

### Property 18: `set_user_env` In-Place Replace
*For any* known user where the rc file contains the variable with a different
value, `set_user_env` should rewrite the file with the line replaced in
place.
**Validates: Requirements 5.5**

### Property 19: `set_user_env` Dry-Run
*For any* call with `dry_run=True`, `set_user_env` should return
`status=DRY_RUN` without calling `write_text`.
**Validates: Requirements 5.6, 10.1**

### Property 20: `set_user_env` Same-User Elevation Suppression
*For any* call where the target user equals the current user,
`set_user_env` should pass `elevated=False` even if config has `elevated=True`.
**Validates: Requirements 5.7, 9.3**

### Property 21: `unset_user_env` Unknown User
*For any* unknown user, `unset_user_env` should return `status=FAILED` with
`message="user not found: <user>"` and not invoke the file operator.
**Validates: Requirements 6.1**

### Property 22: `unset_user_env` Idempotency
*For any* known user where the rc file is missing or does not contain the
variable, `unset_user_env` should return `status=SKIPPED`.
**Validates: Requirements 6.2**

### Property 23: `unset_user_env` Rewrite
*For any* known user where the rc file contains the variable,
`unset_user_env` should rewrite the file with the matching line removed.
**Validates: Requirements 6.3**

### Property 24: `get_user_env` Unknown User
*For any* unknown user, `get_user_env` should raise
`UbuntuEnvConfigError`.
**Validates: Requirements 7.1**

### Property 25: `get_user_env` Present
*For any* known user where the rc file contains the variable, `get_user_env`
should return a `UbuntuEnvValue(scope=USER, source=<rc path>, ...)`.
**Validates: Requirements 7.2**

### Property 26: `list_user_env` Unknown User
*For any* unknown user, `list_user_env` should raise
`UbuntuEnvConfigError`.
**Validates: Requirements 8.1**

### Property 27: `list_user_env` Missing File
*For any* known user where the rc file does not exist, `list_user_env` should
return an empty dict.
**Validates: Requirements 8.2**

### Property 28: No Elevation for Reads
*For any* read operation (`get_system_env`, `list_system_env`, `get_user_env`,
`list_user_env`), the file operator should never be invoked with
`elevated=True`.
**Validates: Requirements 9.5**

### Property 29: Dry-Run Reads Still Run
*For any* call with `dry_run=True`, the file operator's `read_text` should
still be invoked so that `SKIPPED` vs `DRY_RUN` can be distinguished.
**Validates: Requirements 10.3**

---

## Module Layout

```
source/packages/ccai/sysman/linux/ubuntu/
├── __init__.py
└── environmentconfiguration.py   ← UbuntuEnvironmentConfigurator, config,
                                     models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/linux/ubuntu/
    └── test_environmentconfiguration.py
```

---

## Usage Examples

### Local Execution

```python
from ccai.sysman.linux.ubuntu.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.linux.ubuntu.fileoperators import LocalFileOperator
from ccai.sysman.linux.ubuntu.environmentconfiguration import (
    UbuntuEnvironmentConfigurator,
    UbuntuEnvironmentConfiguratorConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_operator = LocalFileOperator(runner=runner)

env_cfg = UbuntuEnvironmentConfigurator(
    file_operator=file_operator,
    config=UbuntuEnvironmentConfiguratorConfig(elevated=True, dry_run=False),
)

result = env_cfg.set_system_env(name="JAVA_HOME", value="/usr/lib/jvm/java-11")
print(f"Set system env: {result.status}")

result = env_cfg.set_user_env(user="appuser", name="NODE_ENV", value="production")
print(f"Set user env: {result.status}")

user_vars = env_cfg.list_user_env(user="appuser")
print(f"User vars: {user_vars}")
```

### SSH Execution with Aspects

```python
from ccai.sysman.linux.ubuntu.runners import SSHCommandRunner, SSHRunnerConfig
from ccai.sysman.linux.ubuntu.aspects import (
    PersistenceAspect, PersistenceAspectConfig,
    RetryAspect, RetryAspectConfig,
    TimeoutAspect, TimeoutAspectConfig,
)
from ccai.sysman.linux.ubuntu.storage import SQLiteStorage
from ccai.sysman.linux.ubuntu.fileoperators import SSHFileOperator
from ccai.sysman.linux.ubuntu.environmentconfiguration import (
    UbuntuEnvironmentConfigurator, UbuntuEnvironmentConfiguratorConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-ubuntu", username="admin"))
storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

file_operator = SSHFileOperator(runner=runner)
env_cfg = UbuntuEnvironmentConfigurator(
    file_operator=file_operator,
    config=UbuntuEnvironmentConfiguratorConfig(elevated=True),
)

env_cfg.set_system_env(name="LANG", value="en_US.UTF-8")
```

### Dry-Run Mode

```python
cfg = UbuntuEnvironmentConfiguratorConfig(elevated=True, dry_run=True)
env_cfg = UbuntuEnvironmentConfigurator(file_operator=file_operator, config=cfg)

result = env_cfg.set_system_env(name="DANGEROUS_VAR", value="value")
assert result.status == UbuntuEnvActionStatus.DRY_RUN
```

---

## Testing Strategy

1. `MockFileOperator` backed by an in-memory `dict[str, str]` drives every
   read/write/delete path.
2. `_parse_bare_env` and `_parse_export_env` unit tests cover blank lines,
   comments, values with embedded `"`, and malformed lines (which must be
   skipped without raising).
3. `_resolve_home` is tested by monkey-patching `pwd.getpwnam`.
4. `_is_current_user` is tested by monkey-patching `pwd.getpwuid` and
   `os.geteuid`.
5. Empty-file cleanup test verifies that after unsetting the last managed
   variable, `MockFileOperator.delete` is called for the drop-in when only
   the shebang remains.
6. Dry-run matrix test iterates every write verb and asserts zero mutating
   file-operator calls while reads still occur.
7. Elevation matrix test asserts `elevated=True` flows to system-scope writes
   and to other-user writes, and is absent from same-user writes and all
   reads.
8. Unknown-user tests for each of `set_user_env`, `unset_user_env`,
   `get_user_env`, `list_user_env` assert the correct contract-defined
   behaviour (FAILED for writes, raise for reads).
9. No test requires root access or a live filesystem.

---

## Scope Mapping

| Abstract | Ubuntu |
|---|---|
| `SYSTEM` | `/etc/environment` + `/etc/profile.d/ccai.sh` |
| `USER` | `~<user>/.profile` (default) or `.bashrc` / `.pam_environment` (opt-in) |

---

## Extensions Beyond the Contract

- **Dual-file SYSTEM scope.**  Writes to both `/etc/environment` (for
  non-login services that read it directly) and `/etc/profile.d/ccai.sh`
  (for login shells).  Either source alone would miss one of the two common
  consumer populations.
- **`.pam_environment` opt-in.**  Callers can request the PAM-based
  environment file when integration with PAM sessions matters.
- **Same-user elevation suppression.**  Writes targeting the current user
  never pass `elevated=True`, even when the config says so.
