# Requirements

## Introduction

The command-execution feature provides the `automojo-sysman-linux-ubuntu` package with the
ability to dispatch OS-level commands, ordered sequences, and short inline shell scripts
through an `ICommandRunner` abstraction.  This spec implements the command-execution
contract defined at `automojo-sysman-common/specs/contracts/command-execution/contract.md`
for Ubuntu using `subprocess.run` (wrapped by `UbuntuLocalCommandRunner`) and `paramiko`
SSH (wrapped by `UbuntuSSHCommandRunner` in `automojo-protocol-ssh`).  It covers single
command execution, ordered command sequences with stop-on-failure policy, and temp-file
backed script execution via `/bin/bash`.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interface.  The feature does not decide *what* to execute — it
provides the capability to *do it* reliably, dry-run-aware, auditable, and typed.  All
system interaction happens through `ICommandRunner`; no direct `subprocess` call lives in
the executor module itself except for the one temp-file-backed script path which uses
`tempfile.NamedTemporaryFile` for the file and still routes execution through the runner.
Elevation on Ubuntu is implemented by the runner prepending `sudo -n` (with `-E` when the
caller supplies an explicit environment) — the executor itself only forwards the
`elevated` boolean.

---

## Requirements

### Requirement 1: Execute a Single Command

**User Story:** As a maintenance coordinator, I want to run a single argv-based command
through the executor, so that I get a typed `UbuntuCommandResult` regardless of the
underlying runner (local subprocess or SSH).

#### Acceptance Criteria

1. WHEN the coordinator calls `run(args, elevated, working_dir, env, timeout_seconds)`
   THE SYSTEM SHALL invoke `ICommandRunner.run` once with the same `args` list, the
   supplied `elevated` value, a resolved cwd, a merged env, and the supplied timeout.
2. WHEN the runner returns `exit_code == 0` and `timed_out == False` THE SYSTEM SHALL
   return a `UbuntuCommandResult` with `success=True`, `exit_code=0`, populated `stdout`
   and `stderr`, `timed_out=False`, `dry_run=False`, and populated `started_at`,
   `finished_at`, and `duration_ms`.
3. WHEN the runner returns a non-zero exit code THE SYSTEM SHALL return a
   `UbuntuCommandResult` with `success=False`, the observed `exit_code`, the captured
   `stdout` and `stderr`, and `timed_out=False`.
4. WHEN the runner reports `timed_out=True` THE SYSTEM SHALL return a
   `UbuntuCommandResult` with `success=False` and `timed_out=True`, injecting a
   standardised timeout message into `stderr` if the runner did not populate one.
5. WHEN `args` is an empty list THE SYSTEM SHALL return a `UbuntuCommandResult` with
   `success=False` and a descriptive `stderr` without invoking the runner.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke the runner and SHALL return a
   `UbuntuCommandResult` with `success=True`, `dry_run=True`, `exit_code=0`, empty
   `stdout`/`stderr`, and preserving the supplied `args` and `elevated`.

---

### Requirement 2: Execute an Ordered Sequence of Commands

**User Story:** As a maintenance coordinator, I want to run a list of `UbuntuCommandSpec`
objects as an ordered sequence with a stop-on-failure policy, so that I can compose
multi-step maintenance workflows atomically.

#### Acceptance Criteria

1. WHEN the coordinator calls `run_sequence(commands, stop_on_failure=True)` and every
   command returns `success=True` THE SYSTEM SHALL return a `UbuntuSequenceResult` with
   `all_succeeded=True`, `failed_at=None`, and one `UbuntuCommandResult` per spec in the
   original order.
2. WHEN `stop_on_failure=True` and the N-th command returns `success=False` THE SYSTEM
   SHALL NOT invoke the runner for any subsequent command and SHALL set `failed_at=N`
   and `all_succeeded=False`.
3. WHEN `stop_on_failure=False` THE SYSTEM SHALL run every command in the sequence and
   SHALL set `failed_at` to the index of the first failing command or `None` when all
   succeeded.
4. WHEN `commands` is an empty list THE SYSTEM SHALL return a `UbuntuSequenceResult` with
   `all_succeeded=True`, `failed_at=None`, and an empty `results` list without invoking
   the runner.
5. WHEN dry-run mode is active THE SYSTEM SHALL build one synthetic dry-run
   `UbuntuCommandResult` per spec and SHALL NOT invoke the runner for any spec.
6. WHEN a spec carries per-command `elevated`, `working_dir`, `env`, or
   `timeout_seconds` THE SYSTEM SHALL forward those values to the corresponding `run`
   invocation.

---

### Requirement 3: Execute a Shell Script

**User Story:** As a maintenance coordinator, I want to run a short inline bash script
fragment through the executor, so that I can express multi-command logic without
authoring a separate shell file.

#### Acceptance Criteria

1. WHEN the coordinator calls `run_script(script, elevated, working_dir, env,
   timeout_seconds)` THE SYSTEM SHALL write the script text to a temporary file under
   the system temp directory with `suffix=".sh"`, `chmod 0o755` the file, and invoke
   the runner with `args=["/bin/bash", "<tmpfile>"]`, forwarding the remaining arguments.
2. WHEN the runner returns THE SYSTEM SHALL delete the temporary file in a `finally`
   block regardless of success, failure, or exception.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT create any file and SHALL return a
   `UbuntuCommandResult` with `success=True`, `dry_run=True`, and `args=["/bin/bash",
   "<dry-run>"]`.
4. WHEN the `script` argument is an empty string THE SYSTEM SHALL return a
   `UbuntuCommandResult` with `success=False` and a descriptive `stderr` without creating
   a temp file.
5. WHEN the filesystem denies temp-file creation THE SYSTEM SHALL return a
   `UbuntuCommandResult` with `success=False` carrying the OSError text in `stderr` and
   SHALL NOT invoke the runner.

---

### Requirement 4: Working Directory Precondition

**User Story:** As a maintenance coordinator, I want the executor to reject invocations
targeting a non-existent `working_dir` up front, so that I get a clear error without
waiting for the runner to fail opaquely.

#### Acceptance Criteria

1. WHEN `working_dir` is `None` THE SYSTEM SHALL forward `cwd=None` to the runner and
   the runner SHALL use its own default.
2. WHEN `working_dir` is supplied and `pathlib.Path(working_dir).is_dir()` is `True` THE
   SYSTEM SHALL forward the absolute path via `cwd` to the runner.
3. WHEN `working_dir` is supplied and the path does not exist or is not a directory THE
   SYSTEM SHALL return a `UbuntuCommandResult` with `success=False`, `exit_code=None`,
   and a `stderr` of the form `working_dir does not exist: <path>` WITHOUT invoking the
   runner.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT evaluate the `working_dir`
   precondition and SHALL return a dry-run `UbuntuCommandResult`.

---

### Requirement 5: Environment Merging

**User Story:** As a maintenance coordinator, I want caller-supplied environment
overrides layered on top of the process environment, so that I can inject per-command
variables (e.g., `DEBIAN_FRONTEND=noninteractive`, `LANG=C.UTF-8`) without leaking them
into the parent process.

#### Acceptance Criteria

1. WHEN `env` is `None` THE SYSTEM SHALL pass `env=None` to the runner so the runner
   uses its own default inheritance rules.
2. WHEN `env` is supplied THE SYSTEM SHALL compute the merged env as
   `{**os.environ, **env}` and SHALL pass that merged dict to the runner, where caller
   keys override process-environment keys on conflict.
3. WHEN `env` is an empty dict THE SYSTEM SHALL still pass `{**os.environ}` (i.e., a
   fresh copy) to the runner so that downstream aspects do not mutate the caller's
   dict.
4. WHEN a call is elevated AND the runner is the local `UbuntuLocalCommandRunner` THE
   SYSTEM SHALL rely on the runner to add `sudo -n -E ...` so that the merged env
   survives the `sudo` invocation.

---

### Requirement 6: Timeout Propagation

**User Story:** As a maintenance coordinator, I want `timeout_seconds` to propagate to
the runner and surface as a typed field on the result, so that I can detect timeouts
without pattern-matching stderr.

#### Acceptance Criteria

1. WHEN `timeout_seconds` is supplied THE SYSTEM SHALL forward it to the runner's
   `timeout` argument.
2. WHEN `timeout_seconds` is `None` and `config.default_timeout_seconds` is set THE
   SYSTEM SHALL forward the config default.
3. WHEN both are `None` THE SYSTEM SHALL forward `timeout=None`, giving the runner
   latitude to enforce its own default.
4. WHEN the runner returns `timed_out=True` THE SYSTEM SHALL set
   `UbuntuCommandResult.timed_out=True` and `success=False`, and if `stderr` is empty,
   SHALL inject `Command timed out after <N> seconds`.

---

### Requirement 7: Exit-Code to Success Mapping

**User Story:** As a maintenance coordinator, I want a uniform boolean `success` field
that follows the `exit_code == 0` convention, so that I do not have to re-derive it in
the caller.

#### Acceptance Criteria

1. WHEN the runner returns `exit_code=0` and `timed_out=False` THE SYSTEM SHALL set
   `success=True`.
2. WHEN the runner returns any non-zero `exit_code` THE SYSTEM SHALL set `success=False`.
3. WHEN the runner returns `timed_out=True` THE SYSTEM SHALL set `success=False`
   regardless of `exit_code`.
4. WHEN the result is synthetic `DRY_RUN` THE SYSTEM SHALL set `success=True` and
   `exit_code=0`.

---

### Requirement 8: Elevation Propagation

**User Story:** As a maintenance coordinator, I want `elevated` to forward verbatim to
the runner, so that the executor never re-interprets the caller's policy.

#### Acceptance Criteria

1. WHEN the coordinator passes `elevated=True` THE SYSTEM SHALL pass `elevated=True` to
   `ICommandRunner.run`.
2. WHEN the coordinator passes `elevated=False` THE SYSTEM SHALL pass `elevated=False`
   to the runner.
3. WHEN dry-run mode is active THE SYSTEM SHALL reflect the requested value verbatim on
   `UbuntuCommandResult.elevated` even though the runner is not invoked.
4. WHEN the caller sets `elevated=True` on Ubuntu and the runner is
   `UbuntuLocalCommandRunner`, the runner SHALL prepend `["sudo", "-n"]` (and `-E` when
   a merged env was supplied) so that a non-interactive sudo escalation occurs.

---

### Requirement 9: Dry-Run Safety for All Verbs

**User Story:** As a maintenance coordinator, I want all three verbs to short-circuit in
dry-run mode, so that I can preview an entire plan without touching the system.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` and `run` is called THE SYSTEM SHALL NOT invoke the runner
   and SHALL return `UbuntuCommandResult(success=True, dry_run=True, exit_code=0,
   args=args, elevated=elevated)`.
2. WHEN `config.dry_run=True` and `run_sequence` is called THE SYSTEM SHALL build one
   synthetic dry-run `UbuntuCommandResult` per spec and return a
   `UbuntuSequenceResult(all_succeeded=True, dry_run=True, results=[...])`.
3. WHEN `config.dry_run=True` and `run_script` is called THE SYSTEM SHALL NOT create a
   temp file and SHALL return
   `UbuntuCommandResult(success=True, dry_run=True, args=["/bin/bash", "<dry-run>"])`.
4. WHEN a `UbuntuCommandResult` is synthetic the `started_at`, `finished_at`, and
   `duration_ms` fields SHALL be `None`.

---

### Requirement 10: Composition with Aspects

**User Story:** As a maintenance coordinator, I want to attach persistence, retry, and
timeout aspects to the runner without touching the executor, so that cross-cutting
concerns compose transparently.

#### Acceptance Criteria

1. WHEN aspects are attached to the runner THE SYSTEM SHALL produce
   `UbuntuCommandResult` values whose `success`, `exit_code`, `args`, `elevated`, and
   `dry_run` fields match those produced without aspects (modulo retry-induced flips
   and timing fields).
2. WHEN a retry aspect flips a failure to a success THE SYSTEM SHALL report
   `success=True` without mutating `args`, `elevated`, or `dry_run`.
3. WHEN a persistence aspect observes the run THE SYSTEM SHALL NOT mutate the returned
   `UbuntuCommandResult`.
4. WHEN a timeout aspect wraps the runner THE SYSTEM SHALL still surface `timed_out=True`
   on the returned `UbuntuCommandResult` when the aspect forces a timeout.
