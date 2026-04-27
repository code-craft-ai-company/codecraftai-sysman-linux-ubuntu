# Design

## Overview

The configuration management feature is implemented as a single module,
`automojo.sysman.linux.ubuntu.configurationmanagement`, that exposes a
`UbuntuConfigurationManager` class.  This spec implements the
configuration-management contract defined at
`automojo-sysman-common/specs/contracts/configuration-management/contract.md`
for Ubuntu.

All content mutation is delegated to an `IFileOperator`.  All permission/ownership
commands and service-reload commands are delegated to an `ICommandRunner` using
`chmod`, `chown`, `stat`, `cp`, and `systemctl`.  Maintaining a dual-abstraction
dependency (runner + file operator) is an extension over the contract — the
contract allows but does not require `IFileOperator`.  This package uses the file
operator for read/write because doing so produces better dry-run diffs and avoids
shell-quoting traps in content with embedded special characters.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).  When using
SSH, the coordinator can track reliability metrics and apply execution aspects
(persistence, retry, timeout) without any changes to the
`UbuntuConfigurationManager` code.

**See Also:** [Command Runner Architecture](../1-command-execution/design.md) for
details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator                      │
│  - Configures aspects (optional)                            │
│  - Injects both into UbuntuConfigurationManager             │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        UbuntuConfigurationManager                           │
│  automojo.sysman.linux.ubuntu.configurationmanagement       │
│                                                             │
│  read_setting()     write_setting()                         │
│  remove_setting()   list_settings()                         │
│  write()            diff()                                  │
│  validate()         backup_configuration()                  │
│  restore_configuration()                                    │
│  set_permissions()  set_ownership()                         │
│  reload_configuration()                                     │
└────────────┬────────────────────────────┬───────────────────┘
             │ delegates to               │ delegates to
             ▼                            ▼
┌──────────────────────┐         ┌──────────────────────┐
│   ICommandRunner     │         │   IFileOperator      │
│   chmod / chown /    │         │   read_text / write  │
│   stat / cp /        │         │   delete / exists    │
│   systemctl /        │         │                      │
│   validator argv     │         │                      │
└──────────┬───────────┘         └──────────┬───────────┘
           │                                │
           └────────────────┬───────────────┘
                            ▼
          ┌──────────────────────────────────┐
          │   LocalCommandRunner or          │
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

### `UbuntuConfigurationManager`

**Location:** `source/packages/automojo/sysman/linux/ubuntu/configurationmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `read_setting` | `(source, key) → UbuntuSettingValue` | Parse source, return value for key |
| `write_setting` | `(source, key, value) → UbuntuConfigActionResult` | Mutate key, rewrite file |
| `remove_setting` | `(source, key) → UbuntuConfigActionResult` | Delete key, rewrite file |
| `list_settings` | `(source) → list[UbuntuSettingValue]` | Parse all keys |
| `write` | `(source, content, mode=None, owner=None, group=None) → UbuntuConfigActionResult` | Whole-file write + permissions |
| `diff` | `(source, desired_content) → UbuntuConfigDiffResult` | Unified diff current vs desired |
| `validate` | `(source, validator_id) → UbuntuConfigValidationResult` | Run validator argv |
| `backup_configuration` | `(source) → UbuntuConfigActionResult` | `cp -p <source> <backup_path>` |
| `restore_configuration` | `(backup_path) → UbuntuConfigActionResult` | `cp -p <backup_path> <original>` |
| `set_permissions` | `(source, mode) → UbuntuConfigActionResult` | `chmod` |
| `set_ownership` | `(source, user=None, group=None) → UbuntuConfigActionResult` | `chown` |
| `reload_configuration` | `(service) → UbuntuConfigActionResult` | `systemctl reload <service>` |

### `UbuntuConfigurationManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |

### `ValidatorRegistry`

A module-level dict mapping validator ids to argv-builder callables:

```python
UBUNTU_VALIDATORS: dict[str, Callable[[str], list[str]]] = {
    "nginx":    lambda path: ["nginx", "-t", "-c", path],
    "sshd":     lambda path: ["sshd", "-t", "-f", path],
    "apache2":  lambda path: ["apache2ctl", "-t", "-f", path],
    "named":    lambda path: ["named-checkconf", path],
    "rsyslog":  lambda path: ["rsyslogd", "-N1", "-f", path],
    "sudoers":  lambda path: ["visudo", "-c", "-f", path],
    "ufw":      lambda path: ["ufw", "status"],  # proxy validator
    "netplan":  lambda path: ["netplan", "try", "--config-file", path],
}
```

Callers can extend via `register_validator(id, builder)` at runtime.

---

## Data Models

```python
class UbuntuConfigActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class UbuntuConfigActionResult:
    status: UbuntuConfigActionStatus
    source: str
    key: str | None
    backup_path: str | None
    diff: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

@dataclass
class UbuntuSettingValue:
    source: str
    key: str
    value: str
    value_type: str | None    # "ini" / "yaml" / "shell"

@dataclass
class UbuntuConfigDiffResult:
    source: str
    has_diff: bool
    diff: str

@dataclass
class UbuntuConfigValidationResult:
    source: str
    validator_id: str
    valid: bool
    exit_code: int
    stdout: str
    stderr: str

@dataclass
class UbuntuConfigFileInfo:
    source: str
    exists: bool
    mode: str | None
    owner: str | None
    group: str | None
    size_bytes: int | None

class UbuntuConfigFileNotFoundError(Exception): ...
class UbuntuConfigTemplateError(Exception): ...
class UbuntuConfigValidatorNotFoundError(Exception): ...
class UbuntuConfigSettingNotFoundError(Exception): ...
class UbuntuConfigQueryError(Exception): ...
```

---

## Interfaces

### `ICommandRunner` and `IFileOperator`

Imported from `automojo-interfaces`.  The manager takes one of each in its
constructor and delegates all I/O to them.

---

## Source Format Detection

`_detect_format(source)` returns one of `"ini"`, `"yaml"`, `"shell"` based on:

1. Suffix `.ini`, `.cfg`, `.conf` → `"ini"` (most `/etc/*.conf` are INI-ish;
   fall back to shell on configparser parse failure).
2. Suffix `.yaml`, `.yml` → `"yaml"`.
3. Suffix none / `.env` / `.sh` → `"shell"`.
4. Explicit `format=` parameter on each method always overrides.

---

## Command Mappings

### Read Commands (no elevation)

| Operation | Command / Call |
|---|---|
| `read_setting` | `IFileOperator.read_text(path=source)` + in-process parse |
| `list_settings` | Same |
| `diff` | `IFileOperator.read_text(path=source)` + `difflib.unified_diff` |
| `validate` | validator-specific argv (see registry) |
| `stat` for mode/owner | `stat -c "%a %U %G" <source>` |

### Write Commands (elevation-aware)

| Operation | Command / Call |
|---|---|
| `write_setting` | `IFileOperator.write_text(path, rewritten_content, elevated=<cfg>)` |
| `remove_setting` | Same |
| `write` | Same, plus optional `chmod` / `chown` |
| `backup_configuration` | `cp -p <source> <backup_path>` |
| `restore_configuration` | `cp -p <backup_path> <source>` |
| `set_permissions` | `chmod <mode> <source>` |
| `set_ownership` | `chown <user>:<group> <source>` |
| `reload_configuration` | `systemctl reload <service>` |

---

## Output Parsing

### `stat -c "%a %U %G" <source>` Output

Example output: `0644 root root`

Parser splits on single space into three tokens and maps to
`UbuntuConfigFileInfo(mode, owner, group)`.  An empty-stdout response with
non-zero exit code means the file does not exist.

### Format Parsers (Pure Python)

- **INI**: Python's `configparser.ConfigParser` with `strict=False` to tolerate
  duplicates; round-trip uses the default serialiser.  Comments within sections
  are preserved by `configparser` 3.2+.
- **YAML**: `yaml.safe_load` / `yaml.safe_dump` with `default_flow_style=False`,
  `sort_keys=False`.
- **Shell**: regex `^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$`;
  surrounding quotes are stripped from the value.

---

## Idempotency Guards

| Method | Guard | Mechanism |
|---|---|---|
| `write_setting` | Current value equals desired | Parse, compare string |
| `remove_setting` | Key absent | Parse, check presence |
| `write` | Current content equals desired | `read_text`, string compare |
| `set_permissions` | Current mode equals desired | `stat`, compare |
| `set_ownership` | Current owner/group equal desired | `stat`, compare |
| `reload_configuration` | None — operational action | — |

---

## Backup Naming

`_backup_path(source)` returns `f"{source}.bak.{utcnow:%Y%m%dT%H%M%S}"`, e.g.,
`/etc/ssh/sshd_config.bak.20240405T123438`.

`restore_configuration(backup_path)` derives the original by stripping
`r"\.bak\.\d{8}T\d{6}$"` from the path.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `UbuntuConfigFileNotFoundError` | `backup_configuration`, `restore_configuration`, `set_permissions`, `set_ownership` target a missing path |
| `UbuntuConfigTemplateError` | A template helper encounters a missing variable |
| `UbuntuConfigValidatorNotFoundError` | `validate` called with an unregistered id |
| `UbuntuConfigSettingNotFoundError` | `read_setting` called with a missing key or missing source |
| `UbuntuConfigQueryError` | Any other read-path failure |

All other write failures are surfaced as
`UbuntuConfigActionResult(status=FAILED)`.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid
executions of the system.*

### Property 1: `read_setting` Parse for INI
*For any* INI source with an existing key, `read_setting` should return a
`UbuntuSettingValue` with `value_type="ini"` and `value` equal to the parsed string.
**Validates: Requirements 1.2**

### Property 2: `read_setting` Parse for YAML
*For any* YAML source with an existing key, `read_setting` should return a
`UbuntuSettingValue` with `value_type="yaml"` and `value` as the stringified
node.
**Validates: Requirements 1.2**

### Property 3: `read_setting` Parse for Shell
*For any* shell source with an existing key, `read_setting` should return a
`UbuntuSettingValue` with `value_type="shell"` and the dequoted value.
**Validates: Requirements 1.2**

### Property 4: `read_setting` Missing Key
*For any* source with no matching key, `read_setting` should raise
`UbuntuConfigSettingNotFoundError`.
**Validates: Requirements 1.3**

### Property 5: `read_setting` Missing Source
*For any* missing source path, `read_setting` should raise
`UbuntuConfigSettingNotFoundError` with `detail="source missing"`.
**Validates: Requirements 1.4**

### Property 6: `write_setting` Idempotency
*For any* source where the key already has the requested value,
`write_setting` should return `status=SKIPPED` without calling `write_text`.
**Validates: Requirements 2.2**

### Property 7: `write_setting` Create Source
*For any* missing source, `write_setting` should render a fresh file and write
it via `write_text`.
**Validates: Requirements 2.3**

### Property 8: `write_setting` Success Path
*For any* source where the key differs, `write_setting` should call `write_text`
with updated content and return `status=SUCCESS`.
**Validates: Requirements 2.4**

### Property 9: `write_setting` Dry-Run
*For any* call with `dry_run=True`, `write_setting` should populate `diff` with
the unified diff and return `status=DRY_RUN` without calling `write_text`.
**Validates: Requirements 2.6**

### Property 10: `remove_setting` Idempotency
*For any* source where the key is absent, `remove_setting` should return
`status=SKIPPED`.
**Validates: Requirements 3.1**

### Property 11: `remove_setting` Dry-Run
*For any* call with `dry_run=True`, `remove_setting` should populate `diff` and
return `status=DRY_RUN` without calling `write_text`.
**Validates: Requirements 3.4**

### Property 12: `list_settings` Missing Source
*For any* missing source, `list_settings` should return an empty list without
raising.
**Validates: Requirements 4.1**

### Property 13: `list_settings` Order Preservation
*For any* source, the returned list's order should match the file's key order.
**Validates: Requirements 4.2**

### Property 14: `write` Idempotency
*For any* source whose current content equals the desired content, `write`
should return `status=SKIPPED`.
**Validates: Requirements 5.2**

### Property 15: `write` Permissions Follow-Up
*For any* write with `mode`, `owner`, or `group` set and differing from current,
`write` should invoke `chmod`/`chown` via the runner after the write.
**Validates: Requirements 5.3, 5.4**

### Property 16: `diff` Current-Equals-Desired
*For any* source where current equals desired, `diff` should return
`has_diff=False` and `diff=""`.
**Validates: Requirements 6.3**

### Property 17: `diff` Source-Missing Treat-As-Empty
*For any* missing source, `diff` should treat current as empty and produce a
full-addition unified diff.
**Validates: Requirements 6.2**

### Property 18: `validate` Unknown Id
*For any* unregistered `validator_id`, `validate` should raise
`UbuntuConfigValidatorNotFoundError` before invoking the runner.
**Validates: Requirements 7.4**

### Property 19: `validate` Valid Mapping
*For any* registered validator with exit code 0, `validate` should return
`valid=True`.
**Validates: Requirements 7.2**

### Property 20: `validate` Invalid Mapping
*For any* registered validator with non-zero exit code, `validate` should return
`valid=False` with `exit_code`, `stdout`, and `stderr` populated.
**Validates: Requirements 7.3**

### Property 21: `backup_configuration` Missing Source
*For any* missing source, `backup_configuration` should raise
`UbuntuConfigFileNotFoundError`.
**Validates: Requirements 8.1**

### Property 22: `backup_configuration` Path Format
*For any* existing source, the returned `backup_path` should match the format
`<source>.bak.<YYYYMMDDTHHMMSS>`.
**Validates: Requirements 8.2**

### Property 23: `restore_configuration` Path Derivation
*For any* backup path matching the documented format,
`restore_configuration` should derive the original path by stripping the
`.bak.<timestamp>` suffix.
**Validates: Requirements 9.1**

### Property 24: `set_permissions` Idempotency
*For any* source whose current mode equals the desired mode, `set_permissions`
should return `status=SKIPPED`.
**Validates: Requirements 10.2**

### Property 25: `set_ownership` Idempotency
*For any* source whose current owner and group already match, `set_ownership`
should return `status=SKIPPED`.
**Validates: Requirements 11.2**

### Property 26: `reload_configuration` Command
*For any* call, `reload_configuration` should invoke `systemctl reload <service>`.
**Validates: Requirements 12.1**

### Property 27: Elevation for Writes
*For any* write operation with `elevated=True`, the runner or file operator
should be invoked with `elevated=True`.
**Validates: Requirements 13.1**

### Property 28: No Elevation for Reads
*For any* read operation, neither the runner nor the file operator should be
invoked with `elevated=True`.
**Validates: Requirements 13.2**

### Property 29: Dry-Run Matrix
*For any* write verb with `dry_run=True`, the runner and file operator should
not be invoked for any mutating call and the result status should be `DRY_RUN`.
**Validates: Requirements 2.6, 3.4, 5.5, 8.5, 9.4, 10.4, 11.4, 12.4**

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/
├── __init__.py
└── configurationmanagement.py   ← UbuntuConfigurationManager, config, models,
                                    exceptions, ValidatorRegistry

source/testroots/testbase/
└── automojo/tests/sysman/linux/ubuntu/
    └── test_configurationmanagement.py
```

---

## Usage Examples

### Local Execution

```python
from automojo.sysman.linux.ubuntu.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.linux.ubuntu.fileoperators import LocalFileOperator
from automojo.sysman.linux.ubuntu.configurationmanagement import (
    UbuntuConfigurationManager,
    UbuntuConfigurationManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_operator = LocalFileOperator(runner=runner)

cfg_mgr = UbuntuConfigurationManager(
    runner=runner,
    file_operator=file_operator,
    config=UbuntuConfigurationManagerConfig(elevated=True, dry_run=False),
)

result = cfg_mgr.write_setting(
    source="/etc/ssh/sshd_config",
    key="PermitRootLogin",
    value="no",
)
print(f"Write setting: {result.status}")

validation = cfg_mgr.validate(source="/etc/nginx/nginx.conf", validator_id="nginx")
print(f"nginx valid: {validation.valid}")

backup = cfg_mgr.backup_configuration(source="/etc/ssh/sshd_config")
print(f"Backup at: {backup.backup_path}")

reload_result = cfg_mgr.reload_configuration(service="ssh")
print(f"Reload: {reload_result.status}")
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
from automojo.sysman.linux.ubuntu.configurationmanagement import (
    UbuntuConfigurationManager, UbuntuConfigurationManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-ubuntu", username="admin"))

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

file_operator = SSHFileOperator(runner=runner)
cfg_mgr = UbuntuConfigurationManager(
    runner=runner,
    file_operator=file_operator,
    config=UbuntuConfigurationManagerConfig(elevated=True),
)

cfg_mgr.write(
    source="/etc/myapp/app.yaml",
    content="database:\n  host: localhost\n  port: 5432\n",
    mode=0o644,
    owner="myapp",
    group="myapp",
)
```

### Dry-Run Mode

```python
cfg = UbuntuConfigurationManagerConfig(elevated=True, dry_run=True)
cfg_mgr = UbuntuConfigurationManager(runner=runner, file_operator=file_operator, config=cfg)

result = cfg_mgr.write_setting(
    source="/etc/ssh/sshd_config",
    key="PermitRootLogin",
    value="no",
)
assert result.status == UbuntuConfigActionStatus.DRY_RUN
print(result.diff)  # unified diff of what would change
```

---

## Testing Strategy

1. `MockFileOperator` backed by an in-memory `dict[str, str]` for `read_text` /
   `write_text`.  `MockCommandRunner` keyed on command args prefix for
   `stat`, `chmod`, `chown`, validator argv, `cp`, and `systemctl`.
2. Format-detector unit tests cover all suffixes plus explicit overrides.
3. Parser round-trip tests for INI, YAML, and shell — verify that parse +
   serialise with no mutation yields byte-equal content.
4. `write_setting` idempotency test: pre-populate `MockFileOperator` with a file
   where the key already has the requested value; expect `SKIPPED`.
5. `validate` matrix covers every registered validator id with both a passing
   (exit code 0) and failing (non-zero) `MockCommandRunner` response.
6. `backup_configuration` timestamp test monkey-patches `datetime.utcnow` and
   asserts the computed path format.
7. `restore_configuration` path-derivation test asserts the reverse of the
   backup format.
8. Dry-run matrix test iterates every write verb, sets `dry_run=True`, and
   asserts no mutating runner or file-operator call occurs.
9. Elevation matrix test asserts `elevated=True` flows to writes and is absent
   for reads.
10. No test requires root access or a live Ubuntu host.

---

## Scope Mapping

| Abstract | Ubuntu |
|---|---|
| Configuration source | Absolute filesystem path under `/etc/` or `/opt/*/etc/` |
| Supported formats | INI, YAML, shell `KEY=VALUE` |
| Validators | `nginx`, `sshd`, `apache2`, `named`, `rsyslog`, `sudoers`, `ufw`, `netplan` |
| Reload mechanism | `systemctl reload <service>` |
| Backup naming | `<source>.bak.<YYYYMMDDTHHMMSS>` |

---

## Extensions Beyond the Contract

- **Dual abstraction dependency.**  Uses `IFileOperator` for all content I/O in
  addition to `ICommandRunner`.  Contract allows this; we make it explicit.
- **Format auto-detect.**  `_detect_format` picks INI/YAML/shell by suffix; an
  explicit caller-supplied `format` overrides.
- **`netplan try` validator.**  Ubuntu-specific network-configuration validator
  registered at import time.
- **`ufw status` as proxy validator.**  `ufw status` returning 0 means the ufw
  ruleset is at least loadable; useful as a smoke test before a service
  reload.
