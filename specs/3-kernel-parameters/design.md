# Design

## Overview

The kernel parameters feature is implemented as a single module,
`ccai.sysman.linux.ubuntu.kernelparameters`, that exposes a
`UbuntuKernelParameterManager` class.  This spec implements the kernel-parameters
contract defined at
`codecraftai-sysman-common/specs/contracts/kernel-parameters/contract.md` for
Ubuntu using `sysctl` at runtime and `/etc/sysctl.d/` drop-in files for persistence.

The coordinator instantiates `UbuntuKernelParameterManager` and calls its methods
to perform discrete maintenance actions against the Ubuntu kernel tunable surface.
All runtime command interaction is delegated to an `ICommandRunner`; all drop-in
file mutations are delegated to an `IFileOperator`.  No `subprocess` calls or
direct filesystem access occur inside the module.

The feature works identically whether commands execute locally (via
`LocalCommandRunner` + `LocalFileOperator`) or remotely over SSH (via
`SSHCommandRunner` + `SSHFileOperator`).  When using SSH, the coordinator can
track reliability metrics and apply execution aspects (persistence, retry,
timeout) without any changes to the `UbuntuKernelParameterManager` code.

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
│  - Injects into UbuntuKernelParameterManager                │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│           UbuntuKernelParameterManager                      │
│  ccai.sysman.linux.ubuntu.kernelparameters              │
│                                                             │
│  get_parameter()             set_parameter()                │
│  list_parameters()           reload_parameters()            │
│  remove_persisted_parameter()                               │
└──────────────┬──────────────────────────────┬───────────────┘
               │ runner                       │ file I/O
               ▼                              ▼
┌──────────────────────┐         ┌──────────────────────┐
│   ICommandRunner     │         │   IFileOperator      │
│   run(args, elev)    │         │   read / write /     │
│                      │         │   delete             │
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

### `UbuntuKernelParameterManager`

**Location:** `source/packages/ccai/sysman/linux/ubuntu/kernelparameters.py`

| Method | Signature | Description |
|---|---|---|
| `get_parameter` | `(name) → UbuntuParameterValue \| None` | Read runtime value via `sysctl -n` |
| `set_parameter` | `(name, value, persistent) → UbuntuKernelParameterActionResult` | Write runtime / persistent / both |
| `list_parameters` | `() → dict[str, str]` | Parse `sysctl -a` output |
| `reload_parameters` | `() → UbuntuKernelParameterActionResult` | `sysctl --system` |
| `remove_persisted_parameter` | `(name) → UbuntuKernelParameterActionResult` | Delete / rewrite drop-in file |

### `UbuntuKernelParameterManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no mutations dispatch |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `persist_dir` | `str` | `"/etc/sysctl.d"` | Drop-in directory |

---

## Data Models

### Result Types

```python
class UbuntuKernelParameterActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class UbuntuParameterValue:
    name: str
    value: str
    source: str | None          # "runtime" or the drop-in file path

@dataclass
class UbuntuKernelParameterActionResult:
    status: UbuntuKernelParameterActionStatus
    name: str
    current_value: str | None
    applied_value: str | None
    persistent: bool
    requires_reboot: bool       # always False on Ubuntu sysctl
    source: str | None          # drop-in file path when persistent
    diff: str | None            # unified diff for DRY_RUN file writes
    message: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

class UbuntuKernelParameterQueryError(Exception):
    """Raised when sysctl -n (non-stat) or sysctl -a fails unexpectedly."""
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
        capture_output: bool = True,
        stdin: str | None = None,
    ) -> RunResult: ...
```

### `IFileOperator` (from `codecraftai-interfaces`)

```python
class IFileOperator(Protocol):
    def read_text(self, path: str, elevated: bool = False) -> str: ...
    def write_text(self, path: str, content: str, elevated: bool = False) -> None: ...
    def delete(self, path: str, elevated: bool = False) -> None: ...
    def exists(self, path: str, elevated: bool = False) -> bool: ...
```

Both interfaces are injected at construction time.

---

## Parameter-Namespace Mapping

The contract's abstract parameter name maps directly to an Ubuntu sysctl key.

| Accepted prefix | Example | Source |
|---|---|---|
| `kernel.` | `kernel.pid_max` | sysctl |
| `vm.` | `vm.swappiness` | sysctl |
| `net.` | `net.ipv4.ip_forward` | sysctl |
| `fs.` | `fs.file-max` | sysctl |
| `user.` | `user.max_user_namespaces` | sysctl |
| `abi.` | `abi.vsyscall32` | sysctl |
| `dev.` | `dev.scsi.logging_level` | sysctl |
| `debug.` | `debug.exception-trace` | sysctl |

Anything else returns `FAILED` with `message="parameter_not_supported: <name>"`.

---

## Command Mappings

### Write Commands

| Operation | Command |
|---|---|
| Runtime set | `sysctl -w <name>=<value>` |
| Reload persisted | `sysctl --system` |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| Get one parameter | `sysctl -n <name>` |
| List all parameters | `sysctl -a` |

### File Operations (via `IFileOperator`)

| Operation | Path |
|---|---|
| Persist parameter | `<persist_dir>/<name>.conf` (written) |
| Remove persisted parameter | `<persist_dir>/<name>.conf` (rewritten or deleted) |

---

## Output Parsing

### `sysctl -n` output

The output is a single line containing the value (possibly empty).  The parser
strips trailing whitespace and returns the result.  On "cannot stat" stderr, the
parser returns `None` rather than raising.

### `sysctl -a` output

Lines match `^(?P<name>[\w.]+)\s*=\s*(?P<value>.*)$`.  Non-matching lines are
skipped silently.  The parser iterates stdout lines, extracts name/value, and
builds a `dict`.

### Drop-in file content

Files in `/etc/sysctl.d/` use a simple `key = value` format, one entry per line,
with `#` comment lines.  The parser:

1. Splits file content on newlines.
2. Preserves lines beginning with `#` or empty lines verbatim.
3. Splits non-comment lines on the first `=`, stripping both sides.
4. Inline trailing comments (` # ...`) are stripped during comparison but
   preserved during rewrites.

---

## Idempotency Guards

| Method | Guard | Source of truth |
|---|---|---|
| `set_parameter(persistent=False)` | Runtime value equals desired | `sysctl -n <name>` |
| `set_parameter(persistent=True)` | Drop-in file line equals desired AND runtime value equals desired | File read + `sysctl -n <name>` |
| `remove_persisted_parameter` | File absent or key absent from file | `IFileOperator.exists` + file read |

`reload_parameters` has no idempotency guard — it is always executed when not
in dry-run mode.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UbuntuKernelParameterQueryError` | `sysctl -a` fails, or `sysctl -n` fails with an unexpected (non-"cannot stat") error |

All other write failures are surfaced as `UbuntuKernelParameterActionResult`
with `status=FAILED`.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Get-Parameter Command Construction

*For any* parameter name, calling `get_parameter()` should construct the command `sysctl -n <name>` and pass it to the command runner without elevation.

**Validates: Requirements 1.1**

### Property 2: Get-Parameter Missing Value Is None

*For any* parameter name that `sysctl` reports as "cannot stat", `get_parameter()` should return `None`.

**Validates: Requirements 1.3**

### Property 3: Get-Parameter Unexpected Failure Raises

*For any* `sysctl -n` exit code that is non-zero and not the "cannot stat" case, `get_parameter()` should raise `UbuntuKernelParameterQueryError`.

**Validates: Requirements 1.4**

### Property 4: Set-Runtime Command Construction

*For any* allowlisted parameter name and value, calling `set_parameter(name, value, persistent=False)` should construct the command `sysctl -w <name>=<value>`.

**Validates: Requirements 2.2**

### Property 5: Set-Runtime Idempotency

*For any* parameter whose current runtime value already equals the requested value, `set_parameter(persistent=False)` should return `SKIPPED` without invoking the runner write.

**Validates: Requirements 2.1**

### Property 6: Set-Persistent Writes Drop-in File

*For any* allowlisted parameter with `persistent=True`, the manager should write `<persist_dir>/<name>.conf` via the file operator with the rendered `<name> = <value>` line.

**Validates: Requirements 2.4, 7.2**

### Property 7: Set-Persistent Composition With Runtime Apply

*For any* `set_parameter(name, value, persistent=True)`, the manager should ALSO invoke `sysctl -w <name>=<value>` after the persistent write.

**Validates: Requirements 2.4**

### Property 8: Set-Persistent Idempotency

*For any* parameter whose drop-in file already contains the matching line AND whose runtime value matches, `set_parameter(persistent=True)` should return `SKIPPED`.

**Validates: Requirements 2.3**

### Property 9: Allowlist Rejection

*For any* parameter name whose dotted prefix is not in the allowlist, `set_parameter()` should return `FAILED` with `message="parameter_not_supported: <name>"` without invoking the runner or file operator.

**Validates: Requirements 2.5, 6.2, 6.3**

### Property 10: Partial-Failure Step Identification

*For any* `set_parameter(persistent=True)` whose persistent-write step fails, the result should be `FAILED` with a `message` identifying the persistent step, and the runtime-apply step should NOT execute.

**Validates: Requirements 2.6**

### Property 11: List-Parameters Command Construction

*For any* call to `list_parameters()`, the manager should invoke `sysctl -a` without elevation.

**Validates: Requirements 3.1**

### Property 12: List-Parameters Parsing

*For any* `sysctl -a` output, `list_parameters()` should return a `dict[str, str]` containing every `name = value` line.

**Validates: Requirements 3.2**

### Property 13: List-Parameters Error Raises

*For any* non-zero exit from `sysctl -a`, `list_parameters()` should raise `UbuntuKernelParameterQueryError`.

**Validates: Requirements 3.3**

### Property 14: List-Parameters Skips Malformed Lines

*For any* stdout line not matching `name = value`, the parser should silently skip it.

**Validates: Requirements 3.4**

### Property 15: Reload Command Construction

*For any* call to `reload_parameters()`, the manager should invoke `sysctl --system`.

**Validates: Requirements 4.1**

### Property 16: Reload Failure Is Failed Result

*For any* non-zero exit from `sysctl --system`, the result should be `FAILED` with the exit code and stderr.

**Validates: Requirements 4.3**

### Property 17: Remove-Persisted Idempotency When File Absent

*For any* parameter whose drop-in file does not exist, `remove_persisted_parameter()` should return `SKIPPED`.

**Validates: Requirements 5.1**

### Property 18: Remove-Persisted Idempotency When Parameter Absent

*For any* parameter whose drop-in file exists but does not contain the parameter, `remove_persisted_parameter()` should return `SKIPPED`.

**Validates: Requirements 5.2**

### Property 19: Remove-Persisted Deletes Empty File

*For any* parameter whose removal would leave the drop-in file empty or comment-only, the manager should delete the file entirely via the file operator.

**Validates: Requirements 5.3**

### Property 20: Drop-in File Header Comment

*For any* newly written drop-in file, the first line should be `# managed by codecraftai-sysman-linux-ubuntu`.

**Validates: Requirements 7.4**

### Property 21: Drop-in File Per-Parameter Filename

*For any* persistent write of `<name>`, the file path should be `<persist_dir>/<name>.conf`.

**Validates: Requirements 7.1**

### Property 22: Requires-Reboot Never True

*For any* successful `set_parameter` on Ubuntu, the returned result's `requires_reboot` should be `False`.

**Validates: Requirements 2.8**

### Property 23: Dry-Run Short-Circuit for Writes

*For any* write operation (`set_parameter`, `reload_parameters`, `remove_persisted_parameter`) with `dry_run=True`, the runner's write commands and the file operator's write/delete methods should never be invoked, and the result status should be `DRY_RUN`.

**Validates: Requirements 2.7, 4.4, 5.6, 9.1**

### Property 24: Dry-Run Populates Diff For File Writes

*For any* persistent write in dry-run mode, the result's `diff` field should be a non-empty unified-diff string.

**Validates: Requirements 2.7, 9.3**

### Property 25: Dry-Run Still Reads

*For any* write operation in dry-run mode, the idempotency-guard reads (`sysctl -n <name>`, drop-in file read) SHOULD still execute.

**Validates: Requirements 9.4**

### Property 26: Elevation For Writes

*For any* write operation with `elevated=True` in the config, the runner / file operator should be invoked with `elevated=True`.

**Validates: Requirements 8.1**

### Property 27: No Elevation For Reads

*For any* read operation (`get_parameter`, `list_parameters`), the runner should never be invoked with `elevated=True` regardless of the elevation configuration.

**Validates: Requirements 8.2**

### Property 28: Persistent Source Field Populated

*For any* persistent `set_parameter` result, the `source` field should equal the drop-in file path that was (or would have been) written.

**Validates: Requirements 2.4, 7.1**

---

## Module Layout

```
source/packages/ccai/sysman/linux/ubuntu/
├── __init__.py
└── kernelparameters.py         ← UbuntuKernelParameterManager, config,
                                   models, exceptions

source/testroots/testbase/
└── ccai/tests/sysman/linux/ubuntu/
    └── test_kernelparameters.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.linux.ubuntu.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.linux.ubuntu.fileops import LocalFileOperator, LocalFileOperatorConfig
from ccai.sysman.linux.ubuntu.kernelparameters import (
    UbuntuKernelParameterManager,
    UbuntuKernelParameterManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_op = LocalFileOperator(LocalFileOperatorConfig())

config = UbuntuKernelParameterManagerConfig(elevated=True, dry_run=False)
kp_mgr = UbuntuKernelParameterManager(runner=runner, file_operator=file_op, config=config)

# Runtime only
result = kp_mgr.set_parameter(name="vm.swappiness", value="10", persistent=False)
print(f"Runtime set: {result.status}")

# Persistent + runtime
result = kp_mgr.set_parameter(name="net.ipv4.ip_forward", value="1", persistent=True)
print(f"Persistent + runtime: {result.status}")

# Read
value = kp_mgr.get_parameter(name="vm.swappiness")
if value is not None:
    print(f"vm.swappiness = {value.value}")

# Reload
kp_mgr.reload_parameters()
```

### SSH Execution with Aspects

```python
from ccai.sysman.linux.ubuntu.runners import SSHCommandRunner, SSHRunnerConfig
from ccai.sysman.linux.ubuntu.fileops import SSHFileOperator, SSHFileOperatorConfig
from ccai.sysman.linux.ubuntu.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from ccai.sysman.linux.ubuntu.storage import SQLiteStorage
from ccai.sysman.linux.ubuntu.kernelparameters import (
    UbuntuKernelParameterManager,
    UbuntuKernelParameterManagerConfig,
)

ssh_config = SSHRunnerConfig(host="ubuntu-node-01", username="admin")
runner = SSHCommandRunner(ssh_config)
file_op = SSHFileOperator(SSHFileOperatorConfig(host="ubuntu-node-01", username="admin"))

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=3, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

kp_mgr = UbuntuKernelParameterManager(
    runner=runner,
    file_operator=file_op,
    config=UbuntuKernelParameterManagerConfig(elevated=True),
)

kp_mgr.set_parameter(name="vm.overcommit_memory", value="1", persistent=True)
kp_mgr.set_parameter(name="net.core.somaxconn", value="4096", persistent=True)
kp_mgr.reload_parameters()
```

### Dry-Run Mode

```python
config = UbuntuKernelParameterManagerConfig(elevated=True, dry_run=True)
kp_mgr = UbuntuKernelParameterManager(runner=runner, file_operator=file_op, config=config)

result = kp_mgr.set_parameter(name="vm.swappiness", value="10", persistent=True)
assert result.status == UbuntuKernelParameterActionStatus.DRY_RUN
print(result.command)      # argv that would have run
print(result.diff)         # unified diff of /etc/sysctl.d/vm.swappiness.conf
```

---

## Testing Strategy

### Unit Testing

- All tests use a `MockCommandRunner` and `MockFileOperator` keyed on command
  arg-prefix and file path respectively.
- `sysctl -n <name>` mock responses drive the runtime idempotency guard for
  `set_parameter(persistent=False)`.
- `sysctl -a` mock responses drive `list_parameters` parser tests including
  well-formed, mixed-line, and failure cases.
- Drop-in file read mocks drive `set_parameter(persistent=True)` and
  `remove_persisted_parameter` idempotency.
- `persist_dir` is overridden in tests to a synthetic path so that no system
  path is ever touched.
- Dry-run tests assert zero calls to mutating runner methods and zero calls to
  `file_operator.write_text` / `file_operator.delete`.
- Allowlist rejection tests assert zero runner and file-operator invocations
  for disallowed names.
- A dedicated test covers the partial-failure step (`message="persist_failed"` vs
  `message="runtime_apply_failed"`).
- No test requires root access or a live `sysctl`.

### Property-Based Testing

The implementation uses `hypothesis` for property-based testing.  Each
correctness property is implemented as a property-based test that:

- Generates random but valid inputs (allowlisted parameter names, arbitrary
  string values).
- Executes the operation under test against the mock runner / file operator.
- Verifies the property holds across all generated inputs.
- Runs at least 100 iterations per property test.

Property-based tests are tagged as
`# Feature: kernel-parameters (ubuntu), Property N: <property description>`.

---

## Scope Mapping

| Contract verb | Ubuntu implementation | Notes |
|---|---|---|
| `get_parameter` | `sysctl -n <name>` | Returns `None` on cannot-stat. |
| `set_parameter(persistent=False)` | `sysctl -w <name>=<value>` | Idempotency via `sysctl -n`. |
| `set_parameter(persistent=True)` | File write + `sysctl -w` | Both steps MUST succeed. |
| `list_parameters` | `sysctl -a` | Pure-Python line parser. |
| `reload_parameters` | `sysctl --system` | Always executes (no guard). |
| `remove_persisted_parameter` | File rewrite / delete | Deletes empty-after-removal files. |

---

## Extensions beyond the contract

- **Parameter-prefix allowlist.**  The manager rejects parameters outside
  `kernel./vm./net./fs./user./abi./dev./debug.` with `FAILED
  message="parameter_not_supported"`.  This is permitted by the contract's
  "Unsupported parameters" clause.
- **Drop-in file header comment.**  Every newly created file is prefixed with
  a header `# managed by codecraftai-sysman-linux-ubuntu` for future auditing.
- **IFileOperator usage.**  Ubuntu uses `IFileOperator` for drop-in file
  management (contract-permitted extension; RHEL reference uses `tee` through
  the runner — both are valid contract-conformant strategies).

## Out of Scope

- Kernel module load/unload (handled by a separate `KernelModuleManager` —
  not part of this contract).
- Boot-time kernel parameter editing (`/etc/default/grub`, initramfs tuning).
- Per-cgroup or per-namespace tunables.
- Runtime-only writes with no persistence path (use `persistent=False`).
- Parameter discovery metadata (type, range, default).
- Mass-apply from a structured policy document (YAML / TOML).
