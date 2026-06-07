# Requirements

## Introduction

The scheduled-tasks feature provides the `codecraftai-sysman-linux-ubuntu` package
with the ability to manage periodic and calendar-triggered task execution on
Ubuntu systems.  This spec implements the scheduled-tasks contract defined at
`codecraftai-sysman-common/specs/contracts/scheduled-tasks/contract.md` for
Ubuntu using **systemd timers** as the primary scheduling backend (via
`systemctl` and unit-file pairs authored through an `IFileOperator`).  Cron
drop-ins under `/etc/cron.d/` are documented as a secondary fallback for legacy
callers but the `UbuntuScheduledTaskManager` renders timers by default.

A maintenance coordinator running in an external package drives all operations
by calling into this feature's public interfaces.  The feature does not decide
*which* tasks to schedule — it provides the capability to *do it* reliably,
idempotently, and with full auditability.  All system commands are executed
exclusively through the `codecraftai-interfaces` command-runner abstraction, and
all unit-file writes/deletes are executed through the `codecraftai-interfaces`
file-operator abstraction, enabling dry-run simulation, auditing, and test
mocking without requiring a live system.

Scope mapping for `TaskOwner`:

- `TaskOwner.SYSTEM` → unit files under `/etc/systemd/system/` and
  `systemctl --system`.
- `TaskOwner.USER` → unit files under `~/.config/systemd/user/` for the
  running user, or `runuser -u <user> -- systemctl --user ...` for another
  user's scope; `elevated=True` is used when writing into another user's
  home directory.

---

## Requirements

### Requirement 1: Create a Task

**User Story:** As a maintenance coordinator, I want to create a systemd
timer + service unit pair from a `UbuntuTaskSpec`, so that a periodic command
runs at the requested schedule.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_task(spec)` AND the schedule string
   is valid THE SYSTEM SHALL render `<spec.name>.service` and
   `<spec.name>.timer` contents, write both files via the `IFileOperator`
   to the owner's unit directory, dispatch `systemctl [--user]
   daemon-reload`, and — when `spec.enabled=True` — dispatch `systemctl
   [--user] enable --now <spec.name>.timer`.
2. WHEN `spec.schedule` is NOT a valid `OnCalendar=` expression THE SYSTEM
   SHALL return `UbuntuScheduledTaskActionResult` with `status=FAILED`,
   `message="invalid_schedule"`, and SHALL NOT invoke the runner or the
   file operator.
3. WHEN an existing unit-file pair already renders byte-identically to the
   new render AND its enabled-state matches `spec.enabled` THE SYSTEM
   SHALL return `status=SKIPPED`.
4. WHEN `spec.owner=TaskOwner.SYSTEM` THE SYSTEM SHALL pass `elevated=True`
   to the file operator and runner when `config.elevated=True`.
5. WHEN `spec.owner=TaskOwner.USER` AND `spec.user` is not the running
   user THE SYSTEM SHALL pass `elevated=True` to the file operator.
6. WHEN `spec.enabled=True` AND the timer enable dispatch fails after a
   successful write THE SYSTEM SHALL return `status=FAILED` with
   `message="enable_failed"` carrying the runner's exit code / stderr.
7. WHEN the command or file operation succeeds THE SYSTEM SHALL return
   `status=SUCCESS`.
8. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke the file
   operator or the runner and SHALL return `status=DRY_RUN`.

---

### Requirement 2: Delete a Task

**User Story:** As a maintenance coordinator, I want to remove a systemd
timer + service pair, so that a task no longer exists on disk or in memory.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_task(name, owner)` AND the timer
   exists THE SYSTEM SHALL dispatch `systemctl [--user] disable --now
   <name>.timer` via the runner, delete `<name>.timer` and `<name>.service`
   via the file operator, then dispatch `systemctl [--user] daemon-reload`.
2. WHEN neither unit file exists THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN any of disable / delete / daemon-reload fails THE SYSTEM SHALL
   return `status=FAILED` with captured `exit_code` / `stderr` and a
   `message` identifying the failing step.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`
   without mutating state.

---

### Requirement 3: Enable a Task

**User Story:** As a maintenance coordinator, I want to enable an existing
timer, so that a previously-disabled task resumes its schedule.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_task(name, owner)` AND the timer
   exists THE SYSTEM SHALL dispatch `systemctl [--user] enable --now
   <name>.timer`.
2. WHEN the timer does not exist THE SYSTEM SHALL return `status=FAILED`
   with `message="task_not_found"`.
3. WHEN the timer is already enabled THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.

---

### Requirement 4: Disable a Task

**User Story:** As a maintenance coordinator, I want to disable an existing
timer, so that a task stops firing without deleting its unit files.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_task(name, owner)` AND the timer
   exists THE SYSTEM SHALL dispatch `systemctl [--user] disable --now
   <name>.timer`.
2. WHEN the timer does not exist THE SYSTEM SHALL return `status=FAILED`
   with `message="task_not_found"`.
3. WHEN the timer is already disabled THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.

---

### Requirement 5: Run a Task Now

**User Story:** As a maintenance coordinator, I want to trigger a task's
service unit immediately, so that an on-demand run happens outside its
schedule.

#### Acceptance Criteria

1. WHEN the coordinator calls `run_task_now(name, owner)` AND the service
   unit exists THE SYSTEM SHALL dispatch `systemctl [--user] start
   <name>.service`.
2. WHEN the service unit does not exist THE SYSTEM SHALL return
   `status=FAILED` with `message="task_not_found"`.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED` with
   captured `exit_code` / `stderr`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 6: Get a Task

**User Story:** As a maintenance coordinator, I want to resolve a task
by name, so that downstream logic can branch on its existence and state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_task(name, owner)` AND the timer
   exists THE SYSTEM SHALL dispatch `systemctl [--user] show
   <name>.timer` and `systemctl [--user] show <name>.service` without
   elevation and return a `UbuntuTaskInfo` populated from the key=value
   output (`ActiveState`, `UnitFileState`, `OnCalendar*`, `ExecStart`,
   `NextElapseUSecRealtime`, `LastTriggerUSec`).
2. WHEN the timer does not exist (i.e. `UnitFileState=not-found` or the
   runner reports the unit is unknown) THE SYSTEM SHALL return `None`.
3. WHEN the `systemctl show` dispatch fails for any other reason THE
   SYSTEM SHALL raise `UbuntuScheduledTaskQueryError`.
4. WHEN the unit file exists but cannot be parsed THE SYSTEM SHALL
   return a `UbuntuTaskInfo` with `readable=False` and the name +
   owner populated.

---

### Requirement 7: List Tasks

**User Story:** As a maintenance coordinator, I want to enumerate every
timer at a given owner scope, so that audit workflows can inventory
scheduled tasks.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_tasks(owner)` THE SYSTEM SHALL
   dispatch `systemctl [--user] list-timers --all --no-legend
   --no-pager` through the runner without elevation AND dispatch
   `systemctl [--user] list-units --type=timer --all --no-legend
   --no-pager` to capture timers that have never run.
2. WHEN both commands succeed THE SYSTEM SHALL merge the output into a
   list of `UbuntuTaskInfo` records with unique timer names.
3. WHEN either command exits non-zero THE SYSTEM SHALL raise
   `UbuntuScheduledTaskQueryError` carrying the exit code and stderr.
4. WHEN the output is empty THE SYSTEM SHALL return an empty list.

---

### Requirement 8: Validate a Schedule

**User Story:** As a maintenance coordinator, I want to validate a
schedule expression without dispatching any command.

#### Acceptance Criteria

1. WHEN the coordinator calls `validate_schedule(schedule)` AND the
   string matches a valid systemd `OnCalendar=` specifier (including
   the shorthands `minutely`, `hourly`, `daily`, `weekly`, `monthly`,
   `yearly`) THE SYSTEM SHALL return `True`.
2. WHEN the string matches a five-field cron expression (used when the
   manager is asked to render a cron fallback) THE SYSTEM SHALL return
   `True`.
3. WHEN the string matches neither THE SYSTEM SHALL return `False`.
4. THE SYSTEM SHALL NOT invoke the runner or the file operator for
   `validate_schedule`.

---

### Requirement 9: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want system-scope writes
and other-user writes to run with `sudo`, so that the daemon reload and
file writes succeed on a standard system where the process does not run
as root.

#### Acceptance Criteria

1. WHEN `config.elevated=True` AND `spec.owner=TaskOwner.SYSTEM` THE
   SYSTEM SHALL pass `elevated=True` to both the file operator (for
   unit-file writes/deletes) and the runner (for `systemctl --system`
   invocations).
2. WHEN `config.elevated=True` AND `spec.owner=TaskOwner.USER` AND the
   target user equals the running user THE SYSTEM SHALL NOT pass
   `elevated=True`.
3. WHEN `config.elevated=True` AND `spec.owner=TaskOwner.USER` AND the
   target user is NOT the running user THE SYSTEM SHALL pass
   `elevated=True` for every file operation and runner call
   (including the `runuser -u <user> -- systemctl --user ...`
   wrapper).
4. WHEN performing any read (`get_task`, `list_tasks`) THE SYSTEM
   SHALL NOT pass `elevated=True`.
5. WHEN `validate_schedule` is called THE SYSTEM SHALL NOT invoke the
   runner or file operator and SHALL NOT pass `elevated=True` (no
   call is made).
