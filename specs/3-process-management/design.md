# Design

## Overview

This spec implements the process-management contract defined at
`codecraftai-sysman-common/specs/contracts/process-management/contract.md`
for Ubuntu using `ps`, `kill`, `pkill`, `pgrep`, `renice`, and `setsid`.

The process-management feature is implemented as a single module,
`ccai.sysman.linux.ubuntu.processmanagement`, that exposes an
`UbuntuProcessManager` class.  The coordinator instantiates
`UbuntuProcessManager` and calls its read and write verbs to manage
arbitrary user-space processes.  Every verb returns a typed `Ubuntu*`
dataclass; reads raise `UbuntuProcessQueryError` on unexpected failures
while writes return `UbuntuProcessActionResult` with one of four
statuses (`SUCCESS` / `FAILED` / `SKIPPED` / `DRY_RUN`).

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).
When using SSH, the coordinator can track reliability metrics and apply
execution aspects (persistence, retry, timeout) without any changes to
`UbuntuProcessManager` code.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into UbuntuProcessManager                 │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuProcessManager                             │
│  ccai.sysman.linux.ubuntu.processmanagement             │
│                                                             │
│  list_processes()        get_process()                      │
│  find_processes_by_name() wait_for_process()                │
│  kill_process()          kill_process_tree()                │
│  start_process()         set_process_priority()             │
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
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH connection     │
│ - sudo for elevation │      │ - reliability metrics│
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

### `UbuntuProcessManager`

**Location:** `source/packages/ccai/sysman/linux/ubuntu/processmanagement.py`

| Method | Returns | Description |
|---|---|---|
| `list_processes(owner=None)` | `list[UbuntuProcessInfo]` | Run `ps -eo …` (or `ps -u <owner> …`) and parse the table |
| `get_process(pid)` | `UbuntuProcessInfo \| None` | Run `ps -p <pid> -o …` and parse the single row |
| `find_processes_by_name(name)` | `list[UbuntuProcessInfo]` | Run `pgrep -a <name>` and parse `pid argv` rows |
| `wait_for_process(pid, timeout_seconds)` | `UbuntuProcessWaitResult` | Poll `kill -0 <pid>` until the PID disappears or the timeout elapses |
| `kill_process(pid, signal=None)` | `UbuntuProcessActionResult` | Run `kill -<signal> <pid>` after an idempotency probe |
| `kill_process_tree(pid, signal=None)` | `UbuntuProcessActionResult` | Run `pkill -<signal> -P <pid>` then `kill -<signal> <pid>` |
| `start_process(spec)` | `UbuntuProcessActionResult` | Run `setsid sh -c '<argv>'` |
| `set_process_priority(pid, priority)` | `UbuntuProcessActionResult` | Run `renice <priority> -p <pid>` after a current-value probe |

### `UbuntuProcessManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no mutating command is dispatched |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `default_signal` | `str` | `"TERM"` | Signal substituted when `kill_process` is called without an explicit signal |
| `wait_poll_interval_seconds` | `float` | `0.2` | Poll cadence for `wait_for_process` |

---

## Data Models

```python
class UbuntuProcessActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class UbuntuProcessInfo:
    pid: int
    ppid: int | None
    user: str | None
    state: str | None
    priority: int | None
    nice: int | None
    vsz: int | None
    rss: int | None
    cpu_percent: float | None
    mem_percent: float | None
    elapsed: str | None
    command: str | None
    argv: list[str]


@dataclass
class UbuntuProcessSpec:
    executable: str
    args: list[str]
    working_dir: str | None
    env: dict[str, str] | None
    user: str | None


@dataclass
class UbuntuProcessActionResult:
    status: UbuntuProcessActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    pid: int | None
    signal: str | None


@dataclass
class UbuntuProcessWaitResult:
    pid: int
    exit_code: int | None
    timed_out: bool


class UbuntuProcessQueryError(Exception):
    command: list[str]
    exit_code: int | None
    stderr: str
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
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `RunResult` (from `codecraftai-interfaces`)

```python
@dataclass
class RunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
```

---

## Process-Identity Mapping

| Concept | Ubuntu surface |
|---|---|
| PID | POSIX numeric PID; integer in every `ps` / `kill` / `pkill` invocation |
| Owner | `user` column from `ps`; matches the local Unix username |
| Signal | POSIX signal name (`TERM`, `KILL`, `HUP`, `INT`, `QUIT`, `USR1`, `USR2`); passed verbatim with a leading `-` to `kill` / `pkill` |
| Process tree | `pkill -P <pid>` reaches direct children only; deeper descendants are out of scope for this verb |

---

## Command Mappings

### Read Commands (no elevation)

| Verb | Argv |
|---|---|
| `list_processes` | `["ps", "-eo", "pid,ppid,user,stat,pri,ni,vsz,rss,pcpu,pmem,etime,comm,args", "--no-headers"]` |
| `list_processes(owner=<u>)` | `["ps", "-u", "<u>", "-o", "<format>", "--no-headers"]` |
| `get_process` | `["ps", "-p", "<pid>", "-o", "<format>", "--no-headers"]` |
| `find_processes_by_name` | `["pgrep", "-a", "<name>"]` |
| `wait_for_process` (probe) | `["kill", "-0", "<pid>"]` |

### Write Commands (elevation honored)

| Verb | Argv |
|---|---|
| `kill_process` | `["kill", "-<signal>", "<pid>"]` |
| `kill_process_tree` (children) | `["pkill", "-<signal>", "-P", "<pid>"]` |
| `kill_process_tree` (root) | `["kill", "-<signal>", "<pid>"]` |
| `start_process` | `["setsid", "sh", "-c", "<shell-quoted argv>"]` |
| `set_process_priority` | `["renice", "<priority>", "-p", "<pid>"]` |

All write commands pass `elevated=config.elevated` to the runner.

---

## `ps` Output Parsing

`ps -eo pid,ppid,user,stat,pri,ni,vsz,rss,pcpu,pmem,etime,comm,args
--no-headers` produces fixed-column output with the `args` column
guaranteed to be last so embedded whitespace does not break the
parser.  Sample row:

```
    1     0 root     Ss    20   0  168416 12544  0.0  0.3 01:23 systemd /sbin/init splash
```

Parse rules:

1. `splitlines()` on stdout.  Skip empty lines.
2. `line.split(None, 12)` to capture the first 12 whitespace-separated
   tokens; the trailing `args` column receives whatever is left.
3. Coerce numeric tokens (`pid`, `ppid`, `pri`, `ni`, `vsz`, `rss`)
   with `int()`; `pcpu` / `pmem` with `float()`.  Any token that
   fails to parse becomes `None` on the dataclass; the row is dropped
   entirely if `pid` itself is non-numeric.
4. `argv` is the whitespace-split form of the `args` column; `command`
   is the `comm` column verbatim.

---

## `pgrep -a` Output Parsing

`pgrep -a <name>` produces one line per match in the form:

```
3245 nginx: master process /usr/sbin/nginx
3246 nginx: worker process
```

Parse rules:

1. `splitlines()` on stdout.  Skip empty lines.
2. `line.split(None, 1)` to capture the leading PID and the trailing
   argv blob.
3. The blob is whitespace-split into `argv`; `command` is `argv[0]`
   when present, otherwise `None`.
4. Exit code 1 is the canonical "no matches" signal — return an empty
   list rather than raise.

---

## Idempotency Guards

| Verb | Guard | Probe |
|---|---|---|
| `kill_process` | PID already gone | `get_process(pid)` is `None` |
| `kill_process_tree` | Root PID already gone | `get_process(pid)` is `None` |
| `set_process_priority` | Current `nice` already matches | `get_process(pid).nice == priority` |
| `start_process` | None — always runs | — |

`wait_for_process` has no idempotency guard; a missing PID returns
immediately with `timed_out=False` because the first `kill -0` probe
exits non-zero.

---

## Signal Validation

`UbuntuProcessSignal` accepts the contract's seven canonical names:

```
{TERM, KILL, HUP, INT, QUIT, USR1, USR2}
```

`_resolve_signal` strips an optional leading `SIG`, upper-cases the
rest, and rejects anything outside the canonical set with a
`ValueError`.  This catches typo-class bugs (`"sigterm"` vs
`"sigtermx"`) before they hit the runner.

---

## `start_process` Pilot Limitation

The current `start_process` implementation builds the command line
``setsid sh -c '<shell-quoted argv>'`` and dispatches it through the
runner with `cwd=spec.working_dir, env=spec.env`.  In the pilot the
runner blocks until the spawned shell returns, so this verb is
effectively synchronous — the spec field `detach` from the cross-OS
contract has no equivalent here yet.  After a successful exit the
manager runs `pgrep -a <executable>` to recover a best-effort `pid`
for the result.

True detached / async-spawn semantics require either:

- A `LocalCommandRunner.spawn(...)` extension that returns a PID
  without `wait()`-ing, or
- A platform-specific `daemon(3)` shim invoked through the runner.

Both are documented as follow-ups in `tasks.md`.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UbuntuProcessQueryError` | `list_processes`, `find_processes_by_name`, or `wait_for_process` observes a runner exit code other than the documented success / no-match codes |
| `ValueError` | An unrecognised signal name is passed to `kill_process` / `kill_process_tree` |

`get_process` does NOT raise on the canonical "unknown PID" shape
(exit code non-zero with empty stderr) — it returns `None`.  Only
unexpected stderr surfaces as `UbuntuProcessQueryError`.

### Write Errors

Writes never raise; failures surface as
`UbuntuProcessActionResult(status=FAILED, exit_code=..., stderr=...)`.

---

## Correctness Properties

### Property 1: list_processes Argv Construction

*For any* call to `list_processes()`, the runner SHALL receive
`["ps", "-eo", "pid,ppid,user,stat,pri,ni,vsz,rss,pcpu,pmem,etime,
comm,args", "--no-headers"]` with `elevated=False`.

**Validates: Requirements 1.1, 1.5**

### Property 2: list_processes(owner) Argv Construction

*For any* call to `list_processes(owner=<u>)`, the runner SHALL
receive `["ps", "-u", "<u>", "-o", <format>, "--no-headers"]`.

**Validates: Requirement 1.2**

### Property 3: list_processes Parser Coverage

*For any* `ps --no-headers` output containing N rows, `list_processes`
SHALL emit exactly N `UbuntuProcessInfo` records with fields populated
from the corresponding columns.

**Validates: Requirement 1.3**

### Property 4: list_processes Failure Surfacing

*For any* non-zero `ps` exit, `list_processes` SHALL raise
`UbuntuProcessQueryError` carrying the argv, exit code, and stderr.

**Validates: Requirement 1.4**

### Property 5: get_process Argv Construction

*For any* call to `get_process(pid)`, the runner SHALL receive
`["ps", "-p", str(pid), "-o", <format>, "--no-headers"]`.

**Validates: Requirement 2.1**

### Property 6: get_process Returns None for Unknown PID

*For any* `ps -p` invocation that exits non-zero with empty stderr,
`get_process` SHALL return `None`.

**Validates: Requirement 2.3**

### Property 7: find_processes_by_name Argv Construction

*For any* call to `find_processes_by_name(name)`, the runner SHALL
receive `["pgrep", "-a", name]`.

**Validates: Requirement 3.1**

### Property 8: find_processes_by_name Empty Match Handling

*For any* `pgrep` invocation that exits 1, `find_processes_by_name`
SHALL return an empty list, not raise.

**Validates: Requirement 3.3**

### Property 9: kill_process Argv Construction

*For any* call to `kill_process(pid, signal)` against an existing PID
in non-dry-run mode, the runner SHALL receive
`["kill", "-<signal>", str(pid)]`.

**Validates: Requirement 4.1**

### Property 10: kill_process Default Signal

*For any* call to `kill_process(pid)` without an explicit signal,
`config.default_signal` SHALL be substituted.

**Validates: Requirement 4.2**

### Property 11: kill_process Signal Validation

*For any* signal name outside the canonical set, `kill_process` SHALL
raise `ValueError` and SHALL NOT invoke the runner.

**Validates: Requirement 4.3**

### Property 12: kill_process Idempotency

*For any* PID that does not appear in `get_process`, `kill_process`
SHALL return `SKIPPED` without invoking `kill`.

**Validates: Requirement 4.4**

### Property 13: kill_process Dry-Run

*For any* call to `kill_process` with `config.dry_run=True`, the
runner SHALL NOT be invoked and the result SHALL carry status
`DRY_RUN` and the unrun argv.

**Validates: Requirement 4.5**

### Property 14: kill_process_tree Order

*For any* call to `kill_process_tree(pid)` against an existing PID
in non-dry-run mode, the children sweep `pkill -<sig> -P <pid>` SHALL
run before the root kill `kill -<sig> <pid>`.

**Validates: Requirement 5.1**

### Property 15: kill_process_tree Tolerates Empty Children

*For any* `pkill -P` invocation that exits 1 (no children), the root
kill SHALL still execute and the result status SHALL reflect the root
kill alone.

**Validates: Requirement 5.2**

### Property 16: start_process Argv Construction

*For any* call to `start_process(spec)`, the runner SHALL receive
`["setsid", "sh", "-c", <shell-quoted argv>]` with
`cwd=spec.working_dir` and `env=spec.env`.

**Validates: Requirement 6.1, 6.2**

### Property 17: start_process Dry-Run

*For any* call to `start_process` with `config.dry_run=True`, the
runner SHALL NOT be invoked and the result SHALL carry status
`DRY_RUN`.

**Validates: Requirement 6.3**

### Property 18: start_process PID Best Effort

*For any* successful `start_process` invocation, the result's `pid`
SHALL be either `None` or a PID returned by a follow-up
`pgrep -a <executable>` lookup.

**Validates: Requirement 6.4**

### Property 19: set_process_priority Argv Construction

*For any* call to `set_process_priority(pid, priority)` against a PID
whose current `nice` differs from `priority`, the runner SHALL receive
`["renice", str(priority), "-p", str(pid)]`.

**Validates: Requirement 7.1**

### Property 20: set_process_priority Idempotency

*For any* PID whose current `nice` already equals `priority`,
`set_process_priority` SHALL return `SKIPPED` without invoking
`renice`.

**Validates: Requirement 7.3**

### Property 21: wait_for_process Probe Loop

*For any* call to `wait_for_process(pid, timeout_seconds)`, the verb
SHALL repeatedly invoke `["kill", "-0", str(pid)]` until the probe
exits non-zero or the timeout elapses.

**Validates: Requirement 8.1**

### Property 22: wait_for_process Timeout Path

*For any* call to `wait_for_process(pid, timeout_seconds=t)` where
the PID stays alive longer than `t` seconds, the verb SHALL return
`UbuntuProcessWaitResult(timed_out=True)` rather than raise.

**Validates: Requirement 8.3**

### Property 23: Reads Never Elevate

*For any* call to `list_processes`, `get_process`,
`find_processes_by_name`, or `wait_for_process`, every runner
invocation SHALL pass `elevated=False`, regardless of
`config.elevated`.

**Validates: Requirement 10.1**

### Property 24: Writes Honor Elevation

*For any* call to `kill_process`, `kill_process_tree`,
`start_process`, or `set_process_priority` with `config.elevated=True`,
every runner invocation issued by the verb SHALL pass `elevated=True`.

**Validates: Requirement 4.8, 5.4, 6.6, 7.6**

### Property 25: No Hidden State

*For any* sequence of calls against the same `UbuntuProcessManager`
instance, the manager SHALL hold no mutable state across calls beyond
the injected runner and config.

**Validates: Requirement 9.5, 11.1**

---

## Module Layout

```
source/packages/ccai/sysman/linux/ubuntu/
└── processmanagement.py            ← manager + dataclasses + parsers + exception

source/testroots/testbase/ccai/tests/sysman/linux/ubuntu/
└── test_processmanagement.py       ← unit tests using a MockCommandRunner
```

---

## Usage Examples

### Local execution

```python
from ccai.sysman.linux.ubuntu.runners import (
    LocalCommandRunner, LocalRunnerConfig,
)
from ccai.sysman.linux.ubuntu.processmanagement import (
    UbuntuProcessManager,
    UbuntuProcessManagerConfig,
    UbuntuProcessSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = UbuntuProcessManagerConfig(elevated=False)
proc = UbuntuProcessManager(runner, config)

# Read
processes = proc.list_processes()
info = proc.get_process(pid=1)
nginx_pids = proc.find_processes_by_name(name="nginx")

# Write
proc.kill_process(pid=4242, signal="TERM")
proc.set_process_priority(pid=4242, priority=10)
proc.start_process(spec=UbuntuProcessSpec(
    executable="/usr/bin/python3",
    args=["-c", "print('hi')"],
))
```

### Dry-run

```python
config = UbuntuProcessManagerConfig(dry_run=True)
proc = UbuntuProcessManager(runner, config)

result = proc.kill_process(pid=4242)
assert result.status == UbuntuProcessActionStatus.DRY_RUN
assert result.command == ["kill", "-TERM", "4242"]
```

### SSH execution with aspects

```python
from ccai.sysman.linux.ubuntu.runners import (
    SSHCommandRunner, SSHRunnerConfig,
)
from ccai.sysman.linux.ubuntu.aspects import (
    PersistenceAspect, RetryAspect, TimeoutAspect,
    PersistenceAspectConfig, RetryAspectConfig, TimeoutAspectConfig,
)
from ccai.sysman.linux.ubuntu.storage import SQLiteStorage

runner = SSHCommandRunner(SSHRunnerConfig(host="remote", username="admin"))
runner.add_aspect(PersistenceAspect(PersistenceAspectConfig(
    storage_backend=SQLiteStorage("/var/log/maintenance/process.db"),
)))
runner.add_aspect(RetryAspect(RetryAspectConfig(max_retries=2)))
runner.add_aspect(TimeoutAspect(TimeoutAspectConfig(default_timeout=30.0)))

proc = UbuntuProcessManager(
    runner, UbuntuProcessManagerConfig(elevated=True),
)
proc.kill_process_tree(pid=4242, signal="KILL")
```

---

## Testing Strategy

### Unit Testing

- Tests use a `_MockCommandRunner` keyed on argv tuples.  Each test
  seeds the responses for the exact argvs the verb under test should
  produce and asserts on `runner.calls` to verify argv construction.
- `ps` outputs are hand-built from a `_ps_line` helper that matches
  the column order in `_PS_FORMAT` so adding new columns is a
  one-line change.
- Idempotency tests prime the `get_process` probe response so the
  verb sees an "already-gone" or "already-matches" state and never
  reaches the real write argv.
- Dry-run tests assert on `len(runner.calls) == 0` so any accidental
  runner invocation under dry-run trips immediately.
- The read-elevation test exercises every read verb under
  `config.elevated=True` and asserts that no recorded call carries
  `elevated=True`.

### Test File

`source/testroots/testbase/ccai/tests/sysman/linux/ubuntu/test_processmanagement.py`

### Tests Implemented

1. `test_list_processes_invokes_ps_and_parses`
2. `test_get_process_parses_single_record`
3. `test_get_process_returns_none_for_unknown_pid`
4. `test_kill_process_invokes_kill`
5. `test_kill_process_skipped_when_pid_not_found`
6. `test_kill_process_dry_run_short_circuits`
7. `test_kill_process_tree_kills_children_first`
8. `test_start_process_invokes_setsid_sh`
9. `test_start_process_dry_run_short_circuits`
10. `test_set_process_priority_invokes_renice`
11. `test_set_process_priority_skipped_when_already_matches`
12. `test_find_processes_by_name_parses_pgrep_output`
13. `test_failed_command_returns_failed_or_raises_on_read`
14. `test_reads_never_elevate`
