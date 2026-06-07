# Requirements

## Introduction

This spec implements the process-management contract defined at
`codecraftai-sysman-common/specs/contracts/process-management/contract.md`
for Ubuntu using `ps`, `kill`, `pkill`, `pgrep`, `renice`, and `setsid`.

The process-management feature provides the
`codecraftai-sysman-linux-ubuntu` package with the ability to enumerate,
inspect, terminate, spawn, prioritise, wait for, and search for
arbitrary user-space processes on Ubuntu.  A maintenance coordinator
running in an external package drives all operations by calling into
this feature's public interfaces.  The feature does not decide *which*
processes to manage — it provides the capability to *do it* reliably,
idempotently, and with full auditability.  All system commands are
executed exclusively through the `codecraftai-interfaces` command-runner
abstraction, enabling dry-run simulation, auditing, and test mocking
without requiring a live system.

This feature mixes **read** verbs (`list_processes`, `get_process`,
`find_processes_by_name`, `wait_for_process`) and **write** verbs
(`kill_process`, `kill_process_tree`, `start_process`,
`set_process_priority`).  Per the cross-OS contract:

- Writes are dry-run-aware and idempotent where the verb's nature
  permits.  They never raise on failure; failures surface as
  `UbuntuProcessActionResult` records with `status=FAILED`.
- Reads never elevate.  They raise `UbuntuProcessQueryError` for
  unexpected failures; an unknown PID on `get_process` surfaces as
  `None` rather than an exception.
- Writes elevate when `config.elevated=True`.  The runner is
  responsible for actually applying `sudo` when `elevated=True` is
  forwarded.

---

## Requirements

### Requirement 1: Enumerate Running Processes

**User Story:** As a maintenance coordinator, I want to list every
running process on the host with PID, parent PID, owner, state, nice
value, memory footprint, and command line, so that I can inventory the
running workload before taking any action.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_processes()` THE SYSTEM SHALL
   invoke `ps -eo pid,ppid,user,stat,pri,ni,vsz,rss,pcpu,pmem,etime,
   comm,args --no-headers` via the command runner with
   `elevated=False`.
2. WHEN the coordinator passes `owner=<username>` THE SYSTEM SHALL
   invoke `ps -u <username> -o <format> --no-headers` instead.
3. THE SYSTEM SHALL parse every output row into an
   `UbuntuProcessInfo` populating `pid`, `ppid`, `user`, `state`,
   `priority`, `nice`, `vsz`, `rss`, `cpu_percent`, `mem_percent`,
   `elapsed`, `command`, and `argv`.
4. WHEN `ps` returns a non-zero exit code THE SYSTEM SHALL raise
   `UbuntuProcessQueryError` carrying the argv, exit code, and stderr.
5. THE SYSTEM SHALL NOT pass `elevated=True` for this call regardless
   of `config.elevated`.

---

### Requirement 2: Inspect a Single Process

**User Story:** As a maintenance coordinator, I want to look up a
single process by PID, so that I can confirm its existence, owner, and
nice value before issuing a write.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_process(pid=<pid>)` THE SYSTEM
   SHALL invoke `ps -p <pid> -o <format> --no-headers` via the command
   runner with `elevated=False`.
2. WHEN `ps` exits 0 with one parsed row THE SYSTEM SHALL return the
   parsed `UbuntuProcessInfo`.
3. WHEN `ps -p` exits non-zero with empty stderr (the canonical
   "unknown PID" shape) THE SYSTEM SHALL return `None`.
4. WHEN `ps -p` exits non-zero with non-empty stderr THE SYSTEM SHALL
   raise `UbuntuProcessQueryError`.
5. THE SYSTEM SHALL NOT pass `elevated=True` for this call.

---

### Requirement 3: Find Processes by Name

**User Story:** As a maintenance coordinator, I want to search for
running processes by name pattern, so that I can locate every instance
of an application without enumerating the entire process table.

#### Acceptance Criteria

1. WHEN the coordinator calls `find_processes_by_name(name=<pattern>)`
   THE SYSTEM SHALL invoke `pgrep -a <pattern>` via the command runner
   with `elevated=False`.
2. WHEN `pgrep` exits 0 THE SYSTEM SHALL parse each line of the form
   `<pid> <argv...>` into an `UbuntuProcessInfo` whose `pid` and
   `argv` fields are populated.
3. WHEN `pgrep` exits 1 (its canonical "no matches" code) THE SYSTEM
   SHALL return an empty list, not raise.
4. WHEN `pgrep` exits with any other code THE SYSTEM SHALL raise
   `UbuntuProcessQueryError`.
5. THE SYSTEM SHALL NOT pass `elevated=True` for this call.

---

### Requirement 4: Terminate a Process

**User Story:** As a maintenance coordinator, I want to deliver a
POSIX signal to a running process, so that I can request graceful
termination, force termination, or arbitrary signals such as `HUP`.

#### Acceptance Criteria

1. WHEN the coordinator calls `kill_process(pid=<pid>, signal=<sig>)`
   THE SYSTEM SHALL invoke `kill -<sig> <pid>` via the command runner.
2. WHEN `signal` is omitted THE SYSTEM SHALL substitute
   `config.default_signal` (defaulting to `"TERM"`).
3. WHEN `signal` is not in the supported set
   `{TERM, KILL, HUP, INT, QUIT, USR1, USR2}` THE SYSTEM SHALL raise
   `ValueError` with a descriptive message; it SHALL NOT invoke the
   runner.
4. BEFORE invoking `kill`, THE SYSTEM SHALL probe the PID via
   `get_process`; WHEN the PID is no longer present THE SYSTEM SHALL
   return `UbuntuProcessActionResult` with status `SKIPPED` and detail
   `"pid <pid> not present"`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return
   `UbuntuProcessActionResult` with status `DRY_RUN` and SHALL NOT
   invoke the runner.
6. WHEN the `kill` command exits 0 THE SYSTEM SHALL return status
   `SUCCESS`.
7. WHEN the `kill` command exits non-zero THE SYSTEM SHALL return
   status `FAILED` carrying the exit code, stdout, and stderr.
8. THE SYSTEM SHALL pass `elevated=config.elevated` to the runner for
   the `kill` invocation.

---

### Requirement 5: Terminate a Process Tree

**User Story:** As a maintenance coordinator, I want to terminate a
process and all of its direct children, so that runaway worker pools
can be reaped without leaving orphans behind.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `kill_process_tree(pid=<pid>, signal=<sig>)` THE SYSTEM SHALL
   invoke `pkill -<sig> -P <pid>` first (children sweep) and then
   `kill -<sig> <pid>` (root kill), in that order.
2. WHEN `pkill -P` exits 1 (its canonical "no children matched" code)
   THE SYSTEM SHALL treat the children sweep as a non-failure and
   continue to the root kill.
3. WHEN `pkill -P` exits with any other non-zero code THE SYSTEM
   SHALL annotate the final result's `detail` with
   `"(child sweep exit=<code>)"` but SHALL NOT override the root
   kill's status.
4. THE SYSTEM SHALL apply the same dry-run, idempotency, signal
   validation, and elevation rules as `kill_process`.

---

### Requirement 6: Start a New Process

**User Story:** As a maintenance coordinator, I want to spawn a new
process from a structured spec, so that arbitrary helper commands can
be issued under the same auditable runner path used by every other
verb.

#### Acceptance Criteria

1. WHEN the coordinator calls `start_process(spec=<spec>)` THE SYSTEM
   SHALL build the argv `["setsid", "sh", "-c", "<shell-quoted argv>"]`
   from `spec.executable + spec.args`.
2. THE SYSTEM SHALL forward `spec.working_dir` to the runner as `cwd`
   and `spec.env` as `env`.
3. WHEN `dry_run=True` THE SYSTEM SHALL return status `DRY_RUN`
   without invoking the runner.
4. WHEN the runner exits 0 THE SYSTEM SHALL return status `SUCCESS`.
   On `SUCCESS` THE SYSTEM SHALL attempt a best-effort PID lookup via
   `pgrep -a <spec.executable>` and populate `pid` with the most
   recent match (or leave it `None` when no match is found).
5. WHEN the runner exits non-zero THE SYSTEM SHALL return status
   `FAILED` carrying the exit code, stdout, and stderr.
6. THE SYSTEM SHALL pass `elevated=config.elevated` to the runner.
7. THIS PILOT documents that `start_process` is effectively
   synchronous: the runner blocks until the spawned shell exits.  True
   detached / async-spawn semantics are out of scope and recorded in
   `tasks.md`.

---

### Requirement 7: Adjust Process Priority

**User Story:** As a maintenance coordinator, I want to renice a
running process, so that long-running workloads can be backgrounded
without restarting them.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `set_process_priority(pid=<pid>, priority=<n>)` THE SYSTEM SHALL
   invoke `renice <n> -p <pid>`.
2. WHEN `dry_run=True` THE SYSTEM SHALL return status `DRY_RUN`
   without invoking the runner.
3. THE SYSTEM SHALL probe the current nice value via `get_process`;
   when the existing `nice` already equals `priority` THE SYSTEM SHALL
   return status `SKIPPED` with detail `"nice already <n>"` and SHALL
   NOT invoke `renice`.
4. WHEN `renice` exits 0 THE SYSTEM SHALL return status `SUCCESS`.
5. WHEN `renice` exits non-zero THE SYSTEM SHALL return status
   `FAILED` carrying the exit code and stderr.
6. THE SYSTEM SHALL pass `elevated=config.elevated` to the runner.

---

### Requirement 8: Wait for a Process to Exit

**User Story:** As a maintenance coordinator, I want to wait for a
process to exit with an optional timeout, so that orchestration
pipelines can pause until prior work has settled.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `wait_for_process(pid=<pid>, timeout_seconds=<t>)` THE SYSTEM SHALL
   poll `kill -0 <pid>` at the configured
   `wait_poll_interval_seconds` cadence.
2. WHEN `kill -0` exits non-zero (the PID is gone) THE SYSTEM SHALL
   return `UbuntuProcessWaitResult(pid=<pid>, exit_code=None,
   timed_out=False)`.
3. WHEN `timeout_seconds` is supplied AND the elapsed wall-clock time
   reaches it before the PID disappears THE SYSTEM SHALL return
   `UbuntuProcessWaitResult(timed_out=True)` rather than raise.
4. WHEN `timeout_seconds=None` THE SYSTEM SHALL poll forever until the
   PID disappears.
5. THE SYSTEM SHALL NOT pass `elevated=True` for any `kill -0` probe.
6. THE `exit_code` field MAY be left `None`; on Linux only the parent
   process can recover the exit code of a child, and this verb is
   intentionally process-table-driven rather than ptrace / waitpid
   driven.

---

### Requirement 9: Write Discipline

**User Story:** As a maintenance coordinator, I want every write verb
to be safe under retry / persistence / timeout aspects, so that
process-management actions can be issued under whatever runner
configuration the coordinator already has set up.

#### Acceptance Criteria

1. THE SYSTEM SHALL NEVER raise from a write verb.  Every
   write SHALL return `UbuntuProcessActionResult` with one of the four
   statuses `SUCCESS`, `FAILED`, `SKIPPED`, `DRY_RUN`.
2. ON `FAILED` THE SYSTEM SHALL populate `exit_code`, `stdout`,
   `stderr`, and `command` on the result so callers can render an
   actionable error.
3. ON `SKIPPED` THE SYSTEM SHALL populate `command` with the argv that
   *would* have been executed and `detail` with a human-readable
   reason.
4. ON `DRY_RUN` THE SYSTEM SHALL populate `command` with the argv that
   *would* have been executed and SHALL NOT invoke the runner.
5. THE SYSTEM SHALL hold no state across calls beyond the injected
   runner and config.

---

### Requirement 10: Read Discipline

**User Story:** As a maintenance coordinator, I want every read verb
to be strictly read-only and never elevate, so that I can collect
process state under aspect compositions that retry transient failures
without risking host mutation.

#### Acceptance Criteria

1. Every runner invocation issued by `list_processes`, `get_process`,
   `find_processes_by_name`, and `wait_for_process` SHALL pass
   `elevated=False` regardless of `config.elevated`.
2. No read verb SHALL invoke a command that mutates host state.
3. The `wait_for_process` poll SHALL use signal `0` (`kill -0 <pid>`)
   so that no signal is actually delivered.

---

### Requirement 11: Aspect-Transparent Execution

**User Story:** As a maintenance coordinator, I want every
process-management verb to be safe under retry / persistence / timeout
aspects, so that fleet-wide orchestration is not forced to special-case
this feature.

#### Acceptance Criteria

1. THE manager SHALL accept an `ICommandRunner` and an
   `UbuntuProcessManagerConfig` in its constructor and SHALL hold no
   other mutable state.
2. EACH public verb SHALL parse only its own command's output; it
   SHALL NOT read across verbs.
3. WHEN a retry aspect re-runs a transient failure THE SYSTEM SHALL
   produce the same dataclass shape from the retried success that it
   would have produced from a first-try success.
