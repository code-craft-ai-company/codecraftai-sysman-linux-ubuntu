# Design

## Overview

The command-execution feature is implemented as a single module,
`ccai.sysman.linux.ubuntu.commandexecution`, that exposes a `UbuntuCommandExecutor`
class.  This spec implements the command-execution contract defined at
`codecraftai-sysman-common/specs/contracts/command-execution/contract.md` for Ubuntu
using `subprocess.run` (wrapped inside `UbuntuLocalCommandRunner`) for local execution
and `paramiko` (wrapped inside `UbuntuSSHCommandRunner`, which lives in
`codecraftai-protocol-ssh`) for remote execution.  The coordinator instantiates the
executor with a runner and a config, then calls `run`, `run_sequence`, or `run_script`
to dispatch work.

No `subprocess` call lives in the executor itself.  The only direct filesystem work
performed in the module is:

- `tempfile.NamedTemporaryFile(suffix=".sh", delete=False)` for `run_script` temp files.
- `os.chmod(path, 0o755)` on the temp file.
- `pathlib.Path(working_dir).is_dir()` for the working-directory precondition.
- `pathlib.Path(tmpfile).unlink(missing_ok=True)` in the `finally` block of `run_script`.

Environment merging is performed in pure Python using `os.environ`.  Elevation on
Ubuntu is delegated to the runner; `UbuntuLocalCommandRunner` prepends `sudo -n` (plus
`-E` when an explicit env is supplied) when `elevated=True`.

The feature works identically whether commands execute locally (via
`UbuntuLocalCommandRunner`) or remotely over SSH (via `UbuntuSSHCommandRunner`).  The
executor has no knowledge of transport — it only speaks `ICommandRunner`.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md) for
details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into UbuntuCommandExecutor                │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuCommandExecutor                            │
│  ccai.sysman.linux.ubuntu.commandexecution              │
│                                                             │
│  run()           run_sequence()                             │
│  run_script()                                               │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, cwd, env, timeout) → RunResult         │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────────┐  ┌──────────────────────────┐
│ UbuntuLocalCommandRunner │  │ UbuntuSSHCommandRunner   │
│ - subprocess.run()       │  │ - paramiko connection    │
│ - sudo -n [-E] prepend   │  │ - reliability metrics    │
│ - local execution        │  │ - connection retry       │
└──────────────────────────┘  └──────────────────────────┘
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

### `UbuntuCommandExecutor`

**Location:** `source/packages/ccai/sysman/linux/ubuntu/commandexecution.py`

| Method | Signature | Description |
|---|---|---|
| `run` | `(args, *, elevated, working_dir, env, timeout_seconds) → UbuntuCommandResult` | Execute a single argv-based command |
| `run_sequence` | `(commands, *, stop_on_failure) → UbuntuSequenceResult` | Execute an ordered `list[UbuntuCommandSpec]` |
| `run_script` | `(script, *, elevated, working_dir, env, timeout_seconds) → UbuntuCommandResult` | Write script to temp file and invoke `/bin/bash <tmpfile>` |

All keyword-only arguments are optional with documented defaults.

### `UbuntuCommandExecutorConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no runner call occurs for any verb |
| `default_timeout_seconds` | `float \| None` | `None` | Fallback timeout when no per-call value is supplied |
| `shell_path` | `str` | `"/bin/bash"` | Interpreter path used by `run_script` |

---

## Data Models

### Status Enum

```python
class UbuntuCommandExecutionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"    # reserved for coordinator use; executor never emits this
    DRY_RUN  = "dry_run"
```

### Spec

```python
@dataclass
class UbuntuCommandSpec:
    args: list[str]
    elevated: bool = False
    working_dir: str | None = None
    env: dict[str, str] | None = None
    timeout_seconds: float | None = None
    label: str | None = None
```

### Result Types

```python
@dataclass
class UbuntuCommandResult:
    success: bool
    exit_code: int | None
    stdout: str
    stderr: str
    args: list[str]
    timed_out: bool = False
    dry_run: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    elevated: bool = False

@dataclass
class UbuntuSequenceResult:
    all_succeeded: bool
    results: list[UbuntuCommandResult]
    failed_at: int | None
    dry_run: bool = False
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
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `RunResult` (from `codecraftai-interfaces`)

```python
@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    command: list[str]
    timed_out: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
```

---

## Command Mappings

### Local Execution

| Situation | argv passed to `subprocess.run` by `UbuntuLocalCommandRunner` |
|---|---|
| `elevated=False`, no env | `[*args]` |
| `elevated=True`, no env | `["sudo", "-n", *args]` |
| `elevated=True`, explicit env | `["sudo", "-n", "-E", *args]` |
| `run_script` | `[shell_path, "<tmpfile>"]` after the same elevation rules applied |

The local runner uses `shell=False` in every case.  Shell semantics are reachable only
through `run_script`, which explicitly writes the script and invokes `/bin/bash`.

### SSH Execution

| Situation | Behaviour of `UbuntuSSHCommandRunner` |
|---|---|
| Any invocation | Joined argv with `shlex.quote` applied per arg, sent over the existing paramiko channel |
| `elevated=True` | Prepended with `sudo -n ` (and `-E ` when env present) before joining |
| `run_script` | Temp file is created on the remote host via the SSH file operator; executor supplies the local path, runner takes care of the upload + cleanup |

---

## Environment Merging

The merged environment is computed by the executor in pure Python:

```python
if env is None:
    merged_env = None
else:
    merged_env = {**os.environ, **env}
```

- `env is None` → runner receives `env=None` and uses its own default.
- `env == {}` → runner receives a fresh copy of `os.environ` so mutations in aspects do
  not leak into the caller.
- Caller-supplied keys always win on conflicts.

---

## Elevation

| Situation | Executor behaviour | Runner behaviour |
|---|---|---|
| `elevated=True` | Forward to runner unchanged | `UbuntuLocalCommandRunner` prepends `sudo -n` (plus `-E` when env is present); `UbuntuSSHCommandRunner` prepends `sudo -n ` to the joined command |
| `elevated=False` | Forward to runner unchanged | Runner calls `subprocess.run` (or ssh channel) with the argv as-is |
| Running as root | `elevated` is still forwarded; the runner MAY short-circuit elevation when `os.geteuid() == 0` to avoid unnecessary `sudo` noise |

`sudo -n` is used for non-interactive elevation.  A sudoers-configured escalation path is
required for the runner to succeed in automation; a password-prompt situation surfaces
as `exit_code=1` with a `sudo: a password is required` stderr, which the executor maps
to `UbuntuCommandResult(success=False, ...)` without any special-casing.

---

## `run()` Logic

1. If `config.dry_run` is `True`: return
   `UbuntuCommandResult(success=True, exit_code=0, stdout="", stderr="", args=args,
   dry_run=True, elevated=elevated)`.
2. If `args` is an empty list: return a `UbuntuCommandResult` with `success=False`,
   `exit_code=None`, and `stderr="args must be a non-empty list"` without invoking the
   runner.
3. Compute `effective_timeout` as `timeout_seconds` if supplied, else
   `config.default_timeout_seconds`, else `None`.
4. If `working_dir` is not `None` and `pathlib.Path(working_dir).is_dir()` is `False`:
   return `UbuntuCommandResult(success=False, exit_code=None, stderr=f"working_dir does
   not exist: {working_dir}", args=args, elevated=elevated)`.
5. Compute `merged_env` per the Environment Merging section.
6. Record `started_at = datetime.utcnow()`.
7. Call `self._runner.run(args=args, elevated=elevated, cwd=working_dir,
   env=merged_env, timeout=effective_timeout)`.
8. Record `finished_at = datetime.utcnow()` and `duration_ms = (finished_at -
   started_at).total_seconds() * 1000.0`.
9. Map the `RunResult` to `UbuntuCommandResult` with `success = (exit_code == 0 and not
   timed_out)` and `timed_out = run_result.timed_out`.
10. When `timed_out=True` and `stderr` is empty, inject
    `f"Command timed out after {effective_timeout} seconds"`.

## `run_sequence()` Logic

1. If `commands` is an empty list: return
   `UbuntuSequenceResult(all_succeeded=True, results=[], failed_at=None)` without
   invoking the runner.
2. If `config.dry_run=True`: build one synthetic `UbuntuCommandResult` per spec with
   `dry_run=True`, return `UbuntuSequenceResult(all_succeeded=True, results=[...],
   dry_run=True, failed_at=None)`.
3. Iterate `commands`:
   - Call `self.run(args=spec.args, elevated=spec.elevated,
     working_dir=spec.working_dir, env=spec.env,
     timeout_seconds=spec.timeout_seconds)`.
   - Append the resulting `UbuntuCommandResult` to `results`.
   - If `not result.success` and `stop_on_failure=True`: break out of the loop.
4. Compute `all_succeeded = all(r.success for r in results)`.
5. Compute `failed_at = next((i for i, r in enumerate(results) if not r.success),
   None)`.
6. Return `UbuntuSequenceResult(all_succeeded=all_succeeded, results=results,
   failed_at=failed_at)`.

## `run_script()` Logic

1. If `config.dry_run=True`: return
   `UbuntuCommandResult(success=True, exit_code=0, stdout="", stderr="",
   args=[self._config.shell_path, "<dry-run>"], dry_run=True, elevated=elevated)`.
2. If `script` is an empty string: return a `UbuntuCommandResult` with `success=False`
   and `stderr="script must be a non-empty string"` without creating a temp file.
3. Use `tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, dir=None)`
   to write the script text; capture the resulting path as `tmpfile_path`.
4. Call `os.chmod(tmpfile_path, 0o755)` so the file is executable even if umask is
   restrictive.
5. Wrap the following in `try/finally`:
   - `try:` call
     `self.run(args=[self._config.shell_path, tmpfile_path], elevated=elevated,
     working_dir=working_dir, env=env, timeout_seconds=timeout_seconds)`.
   - `finally:` call `pathlib.Path(tmpfile_path).unlink(missing_ok=True)`.
6. Return the result from step 5.

If step 3 or 4 raises an `OSError`, the executor catches it, returns a
`UbuntuCommandResult(success=False, stderr=str(os_err), args=[self._config.shell_path,
"<tempfile-error>"])`, and does NOT invoke the runner.

---

## Error Handling

### Typed Exceptions

Command-execution does not define typed query exceptions — all failures are surfaced on
`UbuntuCommandResult` or `UbuntuSequenceResult`.  Callers that want typed failures
re-raise from the result at their own layer.

| Situation | Surface |
|---|---|
| Non-zero exit | `UbuntuCommandResult(success=False, exit_code=N, ...)` |
| Timeout | `UbuntuCommandResult(success=False, timed_out=True, ...)` |
| Missing `working_dir` | `UbuntuCommandResult(success=False, exit_code=None, ...)` |
| Empty `args` | `UbuntuCommandResult(success=False, exit_code=None, ...)` |
| Tempfile OSError in `run_script` | `UbuntuCommandResult(success=False, exit_code=None, ...)` |
| Runner raises an uncaught exception | The executor does NOT swallow it; the exception propagates |

---

## Dry-Run Behaviour

When `UbuntuCommandExecutorConfig.dry_run=True`:

- `run()` returns immediately with a synthetic `UbuntuCommandResult(dry_run=True,
  success=True, exit_code=0, stdout="", stderr="", args=args, elevated=elevated)`.
- `run_sequence()` builds one synthetic result per spec and returns a
  `UbuntuSequenceResult(all_succeeded=True, dry_run=True, ...)`.
- `run_script()` never creates the temp file and returns a synthetic result whose
  `args=["/bin/bash", "<dry-run>"]`.

Synthetic results have `started_at=None`, `finished_at=None`, and `duration_ms=None`.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid
executions of a system — essentially, a formal statement about what the system should
do.*

### Property 1: Single-Command Success Mapping

*For any* runner returning `exit_code=0` and `timed_out=False`, `run()` should return a
`UbuntuCommandResult` with `success=True`, the observed exit code, and the observed
stdout/stderr.

**Validates: Requirements 1.2, 7.1**

### Property 2: Single-Command Failure Mapping

*For any* runner returning `exit_code != 0`, `run()` should return a
`UbuntuCommandResult` with `success=False` and the observed exit code.

**Validates: Requirements 1.3, 7.2**

### Property 3: Timeout Surfacing

*For any* runner returning `timed_out=True`, `run()` should return a
`UbuntuCommandResult` with `timed_out=True`, `success=False`, and a non-empty `stderr`.

**Validates: Requirements 1.4, 6.4, 7.3**

### Property 4: Empty Args Rejection

*For any* call to `run([])`, the system should return a `UbuntuCommandResult` with
`success=False` and SHALL NOT invoke the runner.

**Validates: Requirements 1.5**

### Property 5: Run Dry-Run Short-Circuit

*For any* call to `run()` with `dry_run=True`, the runner is never invoked and the
result has `dry_run=True`, `success=True`, `exit_code=0`.

**Validates: Requirements 1.6, 9.1**

### Property 6: Sequence Ordering

*For any* sequence call, the order of `results` matches the order of `commands`, with
one result per executed or dry-run spec.

**Validates: Requirements 2.1**

### Property 7: Stop-On-Failure Stops

*For any* sequence with `stop_on_failure=True` where command N fails, no runner call
happens for commands with index > N, and `failed_at == N`.

**Validates: Requirements 2.2**

### Property 8: Continue-On-Failure Continues

*For any* sequence with `stop_on_failure=False`, every command is dispatched exactly
once, and `failed_at` is the lowest failing index or `None`.

**Validates: Requirements 2.3**

### Property 9: Empty Sequence

*For any* empty `commands` list, `run_sequence` returns `all_succeeded=True`,
`results=[]`, `failed_at=None` without invoking the runner.

**Validates: Requirements 2.4**

### Property 10: Sequence Dry-Run

*For any* sequence call with `dry_run=True`, the runner is never invoked and one
synthetic result per spec appears in `results`.

**Validates: Requirements 2.5, 9.2**

### Property 11: Sequence Spec Forwarding

*For any* `UbuntuCommandSpec` carrying `elevated`, `working_dir`, `env`, or
`timeout_seconds`, the executor forwards those values to the per-command `run` call.

**Validates: Requirements 2.6**

### Property 12: Script Temp File Lifecycle

*For any* `run_script()` call that is not dry-run, a temp file with suffix `.sh` is
created, the runner is invoked with argv `["/bin/bash", "<tmpfile>"]`, and the file is
deleted after the call regardless of success.

**Validates: Requirements 3.1, 3.2**

### Property 13: Script Dry-Run

*For any* `run_script()` call with `dry_run=True`, no temp file is created and the
returned result has `args=["/bin/bash", "<dry-run>"]` and `dry_run=True`.

**Validates: Requirements 3.3, 9.3**

### Property 14: Empty Script Rejection

*For any* `run_script("")` call, a failed `UbuntuCommandResult` is returned without
creating a temp file or invoking the runner.

**Validates: Requirements 3.4**

### Property 15: Tempfile OSError Surface

*For any* failure from `tempfile.NamedTemporaryFile` or `os.chmod`, the result has
`success=False` and the runner is not invoked.

**Validates: Requirements 3.5**

### Property 16: Working-Dir None is Forwarded

*For any* `run()` call with `working_dir=None`, the runner receives `cwd=None`.

**Validates: Requirements 4.1**

### Property 17: Working-Dir Existence Accepted

*For any* `run()` call where `working_dir` resolves to an existing directory, the
runner receives that path via `cwd`.

**Validates: Requirements 4.2**

### Property 18: Working-Dir Missing Rejected

*For any* `run()` call with a non-existent `working_dir`, the result has `success=False`
and the runner is not invoked.

**Validates: Requirements 4.3**

### Property 19: Working-Dir Skipped in Dry-Run

*For any* `run()` call in dry-run mode, `working_dir` existence is not inspected and the
result is synthetic success.

**Validates: Requirements 4.4**

### Property 20: Env None Propagation

*For any* `run()` call with `env=None`, the runner receives `env=None`.

**Validates: Requirements 5.1**

### Property 21: Env Merge Precedence

*For any* `run()` call with `env={k: v}`, the runner's env contains every key of
`os.environ` plus `k`, with the caller's `v` winning on conflicts.

**Validates: Requirements 5.2**

### Property 22: Empty Env Merge

*For any* `run()` call with `env={}`, the runner receives a dict equal to `os.environ`
(value equality), and caller's empty dict is never mutated.

**Validates: Requirements 5.3**

### Property 23: Default Timeout Fallback

*For any* `run()` call with `timeout_seconds=None` and a `config.default_timeout_seconds`
set, the runner receives that default.

**Validates: Requirements 6.2**

### Property 24: Dual None Timeout

*For any* `run()` call with both `timeout_seconds=None` and
`config.default_timeout_seconds=None`, the runner receives `timeout=None`.

**Validates: Requirements 6.3**

### Property 25: Elevation True Forwarding

*For any* `run()` call with `elevated=True`, the runner is invoked with `elevated=True`.

**Validates: Requirements 8.1**

### Property 26: Elevation False Forwarding

*For any* `run()` call with `elevated=False`, the runner is invoked with `elevated=False`.

**Validates: Requirements 8.2**

### Property 27: Dry-Run Preserves Elevation On Result

*For any* `run()` call with `dry_run=True`, `UbuntuCommandResult.elevated` equals the
caller's requested value.

**Validates: Requirements 8.3**

### Property 28: Aspect Transparency

*For any* runner with no aspects and an otherwise identical runner with Persistence /
Retry / Timeout aspects attached, the `UbuntuCommandResult` fields `args`, `elevated`,
and `dry_run` agree.

**Validates: Requirements 10.1, 10.3**

### Property 29: Retry Flip

*For any* retry aspect that converts the final attempt's non-zero exit to zero, the
returned `success` equals `True`; `args`, `elevated`, and `dry_run` are preserved.

**Validates: Requirements 10.2**

---

## Module Layout

```
source/packages/ccai/sysman/linux/ubuntu/
├── __init__.py
└── commandexecution.py      ← UbuntuCommandExecutor, config, spec, results, enum

source/testroots/testbase/
└── ccai/tests/sysman/linux/ubuntu/
    └── test_commandexecution.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.linux.ubuntu.runners import (
    UbuntuLocalCommandRunner,
    UbuntuLocalRunnerConfig,
)
from ccai.sysman.linux.ubuntu.commandexecution import (
    UbuntuCommandExecutor,
    UbuntuCommandExecutorConfig,
    UbuntuCommandSpec,
)

runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
config = UbuntuCommandExecutorConfig(dry_run=False, default_timeout_seconds=120.0)
executor = UbuntuCommandExecutor(runner=runner, config=config)

# Single command
result = executor.run(
    args=["apt-get", "-y", "update"],
    elevated=True,
    working_dir="/var/tmp",
    env={"DEBIAN_FRONTEND": "noninteractive"},
    timeout_seconds=180.0,
)
print(f"exit_code={result.exit_code} success={result.success}")
```

### Sequence with Stop-on-Failure

```python
commands = [
    UbuntuCommandSpec(args=["systemctl", "stop", "nginx"], elevated=True, label="stop"),
    UbuntuCommandSpec(
        args=["apt-get", "-y", "install", "--only-upgrade", "nginx"],
        elevated=True,
        env={"DEBIAN_FRONTEND": "noninteractive"},
        label="upgrade",
    ),
    UbuntuCommandSpec(args=["systemctl", "start", "nginx"], elevated=True, label="start"),
]

seq_result = executor.run_sequence(commands=commands, stop_on_failure=True)
print(f"all_succeeded={seq_result.all_succeeded} failed_at={seq_result.failed_at}")
```

### SSH with Aspects

```python
from ccai.sysman.linux.ubuntu.runners import (
    UbuntuSSHCommandRunner,
    UbuntuSSHRunnerConfig,
)
from ccai.sysman.linux.ubuntu.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from ccai.sysman.linux.ubuntu.storage import SQLiteStorage

runner = UbuntuSSHCommandRunner(
    UbuntuSSHRunnerConfig(host="ubuntu-prod-01", username="deploy"),
)
runner.add_aspect(aspect=PersistenceAspect(
    PersistenceAspectConfig(storage_backend=SQLiteStorage(path="/tmp/cmds.db")),
))
runner.add_aspect(aspect=RetryAspect(
    RetryAspectConfig(max_retries=3, retryable_exit_codes=[255]),
))
runner.add_aspect(aspect=TimeoutAspect(
    TimeoutAspectConfig(default_timeout=300.0),
))

executor = UbuntuCommandExecutor(runner=runner, config=UbuntuCommandExecutorConfig())
result = executor.run_script(
    script="#!/bin/bash\nset -euo pipefail\nsystemctl --no-pager status nginx\n",
    elevated=False,
)
```

### Dry-Run Preview

```python
config = UbuntuCommandExecutorConfig(dry_run=True)
executor = UbuntuCommandExecutor(runner=runner, config=config)

result = executor.run(args=["rm", "-rf", "/var/log/old"], elevated=True)
assert result.dry_run is True
assert result.success is True
assert result.started_at is None
```

---

## Testing Strategy

1. **Mock runner** — a small `_MockRunner` dataclass stubs `ICommandRunner`, letting
   each test queue a sequence of `RunResult` instances and inspect the call history.
2. **Success mapping** — a canned `RunResult(exit_code=0, ...)` produces
   `success=True`, and a non-zero run produces `success=False`.
3. **Timeout surface** — a `RunResult(timed_out=True, stderr="")` produces
   `timed_out=True`, `success=False`, and a synthesised `stderr`.
4. **Working-dir** — a known-bad path returns a failure result and asserts the mock
   runner is never called.
5. **Env merge** — `monkeypatch.setenv` patches `os.environ`, and the test asserts the
   `env` kwarg passed to the mock runner contains both process and caller keys with
   caller winning.
6. **Sequence stop-on-failure** — a three-spec sequence with a middle failure asserts
   exactly two runner calls and `failed_at==1`.
7. **Sequence continue-on-failure** — same three-spec sequence with
   `stop_on_failure=False` asserts three runner calls and `failed_at==1`.
8. **Script lifecycle** — the mock runner inspects `args[0] == "/bin/bash"` and
   `args[1].endswith(".sh")`, and `Path(args[1]).exists()` is `False` after the call.
9. **Script dry-run** — `Path` is never probed; the result’s `args[1] == "<dry-run>"`.
10. **Elevation forwarding** — tests assert the mock runner receives the supplied
    `elevated` value verbatim.
11. **Aspect composition** — a test wires `RetryAspect` to flip a failed exit into
    success and asserts the `args`/`elevated`/`dry_run` fields remain stable.
12. No test requires root, a live `apt`, or real SSH.
