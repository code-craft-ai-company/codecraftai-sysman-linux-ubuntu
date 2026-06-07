# Design

## Overview

The ssh-server-configuration feature is implemented as a single
module, `ccai.sysman.linux.ubuntu.sshserverconfiguration`, that
exposes a `UbuntuSshServerConfigurationManager` class.  This spec
implements the ssh-server-configuration contract defined at
`codecraftai-sysman-common/specs/contracts/ssh-server-configuration/contract.md`
for Ubuntu using the stock OpenSSH server (`openssh-server`) shipped
with current Ubuntu LTS releases.

All system interaction is delegated to a command runner implementing
the `ICommandRunner` interface.  No direct filesystem access or
`subprocess` calls are made inside the module — file I/O routes
through the runner via `cat` and `tee`, and service interaction
routes through `systemctl`.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).
When using SSH, the coordinator can track reliability metrics and
apply execution aspects (persistence, retry, timeout) without any
changes to the `UbuntuSshServerConfigurationManager` code.

**See Also:** [Command Runner Architecture](../1-command-execution/design.md) for
details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into UbuntuSshServerConfigurationManager  │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│         UbuntuSshServerConfigurationManager                 │
│  ccai.sysman.linux.ubuntu.sshserverconfiguration        │
│                                                             │
│  get_sshd_setting()       set_sshd_setting()                │
│  remove_sshd_setting()    list_sshd_settings()              │
│  add_match_block()        remove_match_block()              │
│  list_match_blocks()      reload_sshd()                     │
│  test_sshd_config()       list_authorized_keys()            │
│  add_authorized_key()     remove_authorized_key()           │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output, stdin) → RunResult     │
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

### `UbuntuSshServerConfigurationManager`

**Location:**
`source/packages/ccai/sysman/linux/ubuntu/sshserverconfiguration.py`

| Method | Signature | Description |
|---|---|---|
| `get_sshd_setting` | `(keyword) → UbuntuSshSettingValue \| None` | Read a top-level keyword |
| `set_sshd_setting` | `(keyword, value) → UbuntuSshActionResult` | Upsert a top-level keyword |
| `remove_sshd_setting` | `(keyword) → UbuntuSshActionResult` | Drop a top-level keyword |
| `list_sshd_settings` | `() → dict[str, str]` | Enumerate top-level keywords |
| `add_match_block` | `(criteria, settings) → UbuntuSshActionResult` | Upsert a Match block |
| `remove_match_block` | `(criteria) → UbuntuSshActionResult` | Drop a Match block |
| `list_match_blocks` | `() → list[UbuntuSshMatchBlock]` | Enumerate Match blocks |
| `reload_sshd` | `() → UbuntuSshActionResult` | `systemctl reload ssh` |
| `test_sshd_config` | `() → UbuntuSshActionResult` | `sshd -t` |
| `list_authorized_keys` | `(user) → list[UbuntuSshAuthorizedKey]` | Read a user's keys |
| `add_authorized_key` | `(user, key) → UbuntuSshActionResult` | Append a key |
| `remove_authorized_key` | `(user, fingerprint) → UbuntuSshActionResult` | Drop a key by fingerprint |

### `UbuntuSshServerConfigurationManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `sshd_config_path` | `str` | `"/etc/ssh/sshd_config"` | Primary `sshd_config` |
| `authorized_keys_path_template` | `str` | `"/home/{user}/.ssh/authorized_keys"` | Per-user key path |
| `service_name` | `str` | `"ssh"` | systemd unit name (NOT `sshd` on Ubuntu) |

---

## Data Models

### Result Types

```python
class UbuntuSshActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class UbuntuSshActionResult(FeatureActionResult):
    status: UbuntuSshActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    keyword: str | None       # sshd_config keyword targeted
    path: str | None          # file path targeted
    user: str | None          # user whose authorized_keys was targeted
    message: str | None       # SKIPPED / FAILED human-readable detail
```

### Query Result Types

```python
@dataclass
class UbuntuSshSettingValue:
    keyword: str
    value: str
    source_file: str

@dataclass
class UbuntuSshMatchBlock:
    criteria: str             # full string after the Match keyword
    settings: dict[str, str]  # block-scoped keyword/value pairs

@dataclass
class UbuntuSshAuthorizedKey:
    key_type: str
    key_data: str             # base64-encoded body
    comment: str | None
    fingerprint: str | None   # "SHA256:<base64-no-padding>"
    options: str | None
```

### Exceptions

```python
class UbuntuSshQueryError(Exception):
    """Raised when a read fails for a reason other than 'not present'."""
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
        stdin: str | None = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

The manager consumes only `args`, `elevated`, and `stdin` — reads use
`cat` (no stdin); writes use `tee` (rendered content on stdin).

The manager does NOT consume an `IFileOperator`.  The
configuration-management feature on Ubuntu uses an `IFileOperator` for
content with embedded special characters; the SSH manager elects the
`tee` path because (a) sshd_config and authorized_keys content is
ASCII-clean by construction, (b) the `tee` path keeps the dependency
graph small, and (c) the parser/renderer are deterministic so dry-run
diffs can still be reconstructed by callers.

---

## Configuration Path Mapping

| OS | Path |
|----|------|
| RHEL | `/etc/ssh/sshd_config` |
| **Ubuntu** | **`/etc/ssh/sshd_config`** |
| macOS | `/etc/ssh/sshd_config` |
| Windows | `%ProgramData%\ssh\sshd_config` |

Ubuntu shares the path with the other Unixes.  The systemd unit name,
however, differs:

| OS | Unit name |
|----|-----------|
| RHEL | `sshd.service` |
| **Ubuntu** | **`ssh.service`** |
| macOS | (launchd domain `system/com.openssh.sshd`) |
| Windows | `sshd` (Windows service) |

The Ubuntu unit-name divergence is the principal hazard of porting
RHEL-shaped automation: a coordinator that issues `systemctl reload
sshd` on Ubuntu silently fails (the unit does not exist).  The
`UbuntuSshServerConfigurationManager` defaults `service_name` to
`"ssh"`; callers can override for unusual setups (e.g. a manually
named unit).

---

## sshd_config Parsing

### Grammar

`sshd_config` is line-based.  Each non-comment, non-empty line is one
of:

- `<Keyword> <value>` — top-level setting.
- `Match <criteria>` — opens a block; subsequent setting lines apply
  to the block until the next `Match` line or EOF.
- `# comment` — ignored.
- (whitespace) — ignored.

The keyword is case-insensitive (per `man sshd_config`); value text
runs to the end of the line.  A keyword may appear more than once at
the top level — `sshd` uses the **first** occurrence by default, but
some directives accept repetition (e.g. `HostKey`).  This module
normalises by treating "set" as upsert (case-insensitive match,
last-occurrence wins on read), which matches the principle of least
surprise for declarative tooling.

### Parser output shape

`_parse_sshd_config(content) → (top_settings, match_blocks)`:

- `top_settings: dict[str, str]` — keyword (original case) to value.
  When a keyword repeats, the **last** occurrence wins.
- `match_blocks: list[UbuntuSshMatchBlock]` — every `Match` block in
  declaration order.

Comments and blank lines are not preserved across mutation — the
renderer emits a deterministic file.  This is a documented pilot
behaviour; preserving comment lines is tracked in `tasks.md`.

### Renderer output shape

`_render_sshd_config(top_settings, match_blocks) → str`:

1. Top-level settings in dict order (insertion order = file order
   after parse), one per line.
2. A blank-free transition into Match blocks.
3. For each `Match` block: a `Match <criteria>` line followed by
   four-space-indented child settings.
4. Trailing newline.

`sshd` accepts any indentation inside Match blocks; four-space is
chosen for readability.

---

## Match Block Idempotency

`add_match_block` and `remove_match_block` use whitespace-collapsed
criteria comparison: `"  User    git "` matches `"User git"`.  This
prevents formatting drift between coordinator-supplied criteria and
the parser-reconstructed form.  Settings are compared as-rendered: a
block exists if the rendered `sshd_config` after upsert is byte-equal
to the existing file.

---

## authorized_keys Parsing

### Grammar

Each non-comment, non-empty line follows the OpenSSH grammar:

```
[options ]<key-type> <key-data> [<comment>]
```

- `options` (optional): comma-separated directives like
  `no-pty,from="10.0.0.0/8"`.
- `<key-type>`: one of the OpenSSH-recognised tokens (`ssh-rsa`,
  `ssh-ed25519`, `ecdsa-sha2-nistp256`, etc.).
- `<key-data>`: base64-encoded key body.
- `<comment>` (optional): trailing identifier.

The parser scans the line tokens for the first recognised key-type
token; everything before it (when non-empty) becomes `options`.

### Fingerprint Computation

OpenSSH fingerprint output (`ssh-keygen -lf <file>`) is `SHA256:`
followed by a base64-encoded SHA-256 of the **decoded** key body, with
trailing `=` padding stripped.  This module reproduces that exactly
in pure Python:

```python
digest = hashlib.sha256(base64.b64decode(key_data)).digest()
fingerprint = "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
```

No external `ssh-keygen` invocation is required for identity
comparisons inside `add_authorized_key` / `remove_authorized_key`.

### Idempotency

`add_authorized_key`: parse the supplied key, compute its fingerprint,
read the user's existing file, and check whether any existing entry
has the same fingerprint.  If yes, return `SKIPPED`.  Otherwise,
append the line and write the new content via `tee`.

`remove_authorized_key`: read the file, drop every line whose computed
fingerprint matches the supplied fingerprint, write the new content
via `tee`.  If no line matches, return `SKIPPED`.

---

## Command Mappings

### Write Commands

| Operation | Command | stdin |
|---|---|---|
| Set keyword | `tee /etc/ssh/sshd_config` | rendered file |
| Remove keyword | `tee /etc/ssh/sshd_config` | rendered file |
| Add Match block | `tee /etc/ssh/sshd_config` | rendered file |
| Remove Match block | `tee /etc/ssh/sshd_config` | rendered file |
| Add authorized key | `tee /home/<user>/.ssh/authorized_keys` | rendered file |
| Remove authorized key | `tee /home/<user>/.ssh/authorized_keys` | rendered file |
| Reload | `systemctl reload ssh` | — |
| Test config | `sshd -t` | — |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| Read sshd_config | `cat /etc/ssh/sshd_config` |
| Read authorized_keys | `cat /home/<user>/.ssh/authorized_keys` |

All read commands pass `elevated=False` regardless of
`config.elevated`.  Per the contract, reads MUST NOT pass
`elevated=True`.  A non-root runner that encounters a permission
denial on `cat` will receive a non-zero exit code, which the manager
maps to "file does not exist" (returns `None` / empty list / `SKIPPED`
on writes).  This is consistent with `cat`'s exit-code semantics and
with the prior wave-1 modules.

---

## Idempotency Guards

| Method | Guard | Comparison |
|---|---|---|
| `set_sshd_setting` | Rendered content matches existing | Byte-equal |
| `remove_sshd_setting` | Keyword absent OR file absent | Pure-Python |
| `add_match_block` | Rendered content matches existing | Byte-equal |
| `remove_match_block` | No block with matching criteria | Whitespace-collapsed |
| `add_authorized_key` | Fingerprint already present | SHA-256 |
| `remove_authorized_key` | Fingerprint not present | SHA-256 |
| `reload_sshd` | (none — always executes) | — |
| `test_sshd_config` | (none — always executes) | — |

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UbuntuSshQueryError` | A read fails for a reason other than the file simply being absent (reserved — the pilot's read paths use ``cat`` and treat any non-zero exit as "absent"; the typed exception is exposed for future read paths) |

All other failures from write operations are surfaced as
`UbuntuSshActionResult` with `status=FAILED`.  Writes do not raise.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true
across all valid executions of a system — essentially, a formal
statement about what the system should do.*

### Property 1: Read Returns Parsed Value

*For any* `sshd_config` containing a top-level `<keyword> <value>`,
`get_sshd_setting(keyword)` returns a `UbuntuSshSettingValue` with
matching `keyword`, `value`, and `source_file=/etc/ssh/sshd_config`.

**Validates: Requirements 1.1, 1.2**

### Property 2: Read Returns None When Absent

*For any* `sshd_config` not containing a top-level keyword,
`get_sshd_setting(keyword)` returns `None`.

**Validates: Requirements 1.3, 1.4**

### Property 3: Read Never Elevates

*For any* call to a read verb (`get_sshd_setting`,
`list_sshd_settings`, `list_match_blocks`, `list_authorized_keys`),
the runner is invoked with `elevated=False` regardless of
`config.elevated`.

**Validates: Requirements 1.5, 4.4, 7.4, 10.5, 13.2**

### Property 4: Set Idempotency

*For any* `sshd_config` already containing `<keyword> <value>` at the
top level, `set_sshd_setting(keyword, value)` returns `SKIPPED`
without invoking `tee`.

**Validates: Requirement 2.2**

### Property 5: Set Replaces Existing Value

*For any* `sshd_config` containing `<keyword> <other-value>` at the
top level, `set_sshd_setting(keyword, value)` writes a rendered file
in which the keyword's value is `value`.

**Validates: Requirement 2.3**

### Property 6: Set Creates Missing File

*For any* call to `set_sshd_setting(keyword, value)` when the file
does not exist, the manager writes a single-line file with `<keyword>
<value>`.

**Validates: Requirement 2.4**

### Property 7: Remove Skipped When Absent

*For any* `sshd_config` not containing the keyword,
`remove_sshd_setting(keyword)` returns `SKIPPED`.

**Validates: Requirements 3.1, 3.2**

### Property 8: List Returns Top-Level Only

*For any* `sshd_config` with both top-level settings and a `Match`
block, `list_sshd_settings()` returns only the top-level settings.

**Validates: Requirement 4.2**

### Property 9: Add Match Block Idempotency

*For any* `sshd_config` already containing a `Match` block with the
identical criteria and identical settings, `add_match_block(criteria,
settings)` returns `SKIPPED`.

**Validates: Requirement 5.2**

### Property 10: Add Match Block Replaces Settings

*For any* `sshd_config` containing a `Match` block with the matching
criteria but different settings, `add_match_block(criteria, settings)`
writes a rendered file in which the block's settings equal `settings`.

**Validates: Requirement 5.3**

### Property 11: Remove Match Block Idempotency

*For any* `sshd_config` without a matching `Match` block,
`remove_match_block(criteria)` returns `SKIPPED`.

**Validates: Requirement 6.1**

### Property 12: List Match Blocks Returns Declaration Order

*For any* `sshd_config` with multiple `Match` blocks,
`list_match_blocks()` returns them in declaration order.

**Validates: Requirement 7.2**

### Property 13: Reload Command Construction

*For any* call to `reload_sshd()`, the manager invokes `systemctl
reload ssh` (the Ubuntu-specific unit name) via the runner.

**Validates: Requirement 8.1**

### Property 14: Test Config Command Construction

*For any* call to `test_sshd_config()`, the manager invokes `sshd -t`
via the runner.

**Validates: Requirement 9.1**

### Property 15: Test Config Failure Maps to FAILED

*For any* `sshd -t` invocation returning non-zero, the result has
`status=FAILED` with `stderr` populated.

**Validates: Requirement 9.3**

### Property 16: Authorized-Key Parser Recognises Common Types

*For any* `authorized_keys` line of the form `<key-type> <key-data>
[<comment>]` with `<key-type>` in `{ssh-rsa, ssh-ed25519,
ecdsa-sha2-nistp256, ecdsa-sha2-nistp384, ecdsa-sha2-nistp521}`, the
parser populates `key_type`, `key_data`, and `comment` correctly.

**Validates: Requirement 10.2**

### Property 17: Authorized-Key Parser Captures Options

*For any* `authorized_keys` line beginning with options (e.g.
`no-pty,from="10.0.0.0/8" ssh-ed25519 ...`), the parser captures the
options prefix into the `options` field.

**Validates: Requirement 10.3**

### Property 18: Add Authorized Key Idempotency by Fingerprint

*For any* user with an existing entry whose fingerprint matches the
supplied key's fingerprint, `add_authorized_key(user, key)` returns
`SKIPPED`.

**Validates: Requirement 11.3**

### Property 19: Add Authorized Key Format Validation

*For any* `key` that does not parse as an OpenSSH `authorized_keys`
line, `add_authorized_key(user, key)` returns `FAILED` with
`message="key line failed to parse"` without invoking the runner.

**Validates: Requirement 11.2**

### Property 20: Remove Authorized Key By Fingerprint

*For any* user with multiple entries, `remove_authorized_key(user,
fingerprint)` writes a file in which no remaining line's computed
fingerprint equals the supplied fingerprint, and every other line is
preserved.

**Validates: Requirement 12.3**

### Property 21: Remove Authorized Key Idempotency

*For any* user without an entry matching the supplied fingerprint,
`remove_authorized_key(user, fingerprint)` returns `SKIPPED`.

**Validates: Requirement 12.2**

### Property 22: Dry-Run Safety

*For any* write verb (`set_sshd_setting`, `remove_sshd_setting`,
`add_match_block`, `remove_match_block`, `add_authorized_key`,
`remove_authorized_key`, `reload_sshd`, `test_sshd_config`) with
`dry_run=True`, the runner is never invoked for the write command and
the result status is `DRY_RUN`.

**Validates: Requirements 2.5, 3.4, 5.5, 6.4, 8.5, 9.4, 11.5, 12.4**

### Property 23: Elevation for Write Operations

*For any* write verb invoked with `config.elevated=True`, the runner
is invoked with `elevated=True` for the mutating command.

**Validates: Requirement 13.1**

### Property 24: Error Result Consistency

*For any* failed runner command, the returned
`UbuntuSshActionResult` has `status=FAILED` and contains the
`exit_code`, `stdout`, `stderr`, and `command` from the runner.

**Validates: Requirements 2.6, 3.5, 5.6, 8.4, 9.3, 11.6, 12.5**

---

## Module Layout

```
source/packages/ccai/sysman/linux/ubuntu/
├── __init__.py
└── sshserverconfiguration.py   ← UbuntuSshServerConfigurationManager,
                                   config, models, exceptions, parser
                                   helpers

source/testroots/testbase/
└── ccai/tests/sysman/linux/ubuntu/
    └── test_sshserverconfiguration.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.linux.ubuntu.runners import (
    LocalCommandRunner,
    LocalRunnerConfig,
)
from ccai.sysman.linux.ubuntu.sshserverconfiguration import (
    UbuntuSshServerConfigurationManager,
    UbuntuSshServerConfigurationManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = UbuntuSshServerConfigurationManagerConfig(elevated=True)
mgr = UbuntuSshServerConfigurationManager(runner=runner, config=config)

# Disable root login
result = mgr.set_sshd_setting(keyword="PermitRootLogin", value="no")
print(result.status)

# Add a Match block restricting password auth for git users
result = mgr.add_match_block(
    criteria="User git",
    settings={"PasswordAuthentication": "no"},
)

# Validate, then reload
test_result = mgr.test_sshd_config()
if test_result.status.name == "SUCCESS":
    mgr.reload_sshd()
```

### SSH Execution with Aspects

```python
from ccai.sysman.linux.ubuntu.runners import (
    SSHCommandRunner,
    SSHRunnerConfig,
)
from ccai.sysman.linux.ubuntu.aspects import (
    PersistenceAspect,
    PersistenceAspectConfig,
    RetryAspect,
    RetryAspectConfig,
)
from ccai.sysman.linux.ubuntu.storage import SQLiteStorage
from ccai.sysman.linux.ubuntu.sshserverconfiguration import (
    UbuntuSshServerConfigurationManager,
    UbuntuSshServerConfigurationManagerConfig,
)

runner = SSHCommandRunner(
    SSHRunnerConfig(host="remote-ubuntu", username="admin"),
)

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(
    aspect=PersistenceAspect(
        PersistenceAspectConfig(storage_backend=storage),
    ),
)
runner.add_aspect(
    aspect=RetryAspect(RetryAspectConfig(max_retries=2)),
)

mgr = UbuntuSshServerConfigurationManager(
    runner=runner,
    config=UbuntuSshServerConfigurationManagerConfig(elevated=True),
)

mgr.set_sshd_setting(keyword="PasswordAuthentication", value="no")
mgr.reload_sshd()
```

### Dry-Run Mode

```python
config = UbuntuSshServerConfigurationManagerConfig(
    elevated=True, dry_run=True,
)
mgr = UbuntuSshServerConfigurationManager(runner=runner, config=config)

# No file is touched; the planned argv is recorded on the result.
plan = mgr.set_sshd_setting(keyword="PermitRootLogin", value="no")
assert plan.status.name == "DRY_RUN"
assert plan.command == ["tee", "/etc/ssh/sshd_config"]
```

---

## Testing Strategy

- All tests use a `_MockCommandRunner` keyed on argv tuple.
- `cat /etc/ssh/sshd_config` mock responses drive every read and
  every idempotency guard.
- `tee /etc/ssh/sshd_config` mock responses drive write outcomes.
- Authorized-key tests synthesise OpenSSH-shaped lines using a
  `_build_test_key()` helper that emits a deterministic ed25519
  body and the matching SHA-256 fingerprint, so tests assert
  fingerprint comparisons end-to-end.
- A dedicated test proves `reload_sshd` invokes `systemctl reload
  ssh` (NOT `sshd`) — this is the principal Ubuntu-vs-RHEL
  divergence.
- A dedicated test proves the manager honours `elevated=True` on
  write paths and `elevated=False` on read paths regardless of
  config.
- A dedicated test proves `add_authorized_key` returns `FAILED` with
  a descriptive `message` when the key text fails to parse.
