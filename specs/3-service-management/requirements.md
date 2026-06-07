# Requirements

## Introduction

The service-management feature provides the `codecraftai-sysman-linux-ubuntu` package
with the ability to manage `systemd` system and user services on Ubuntu systems.  This
spec implements the service-management contract defined at
`codecraftai-sysman-common/specs/contracts/service-management/contract.md` for Ubuntu
using `systemctl`.  It covers start/stop/restart/reload, enable/disable,
mask/unmask, status queries, service listing, and daemon-reload, with full idempotency
and dry-run support.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interface.  The feature does not decide *what* to
run — it provides the capability to do so reliably, idempotently, and auditably.  All
system interaction happens through `ICommandRunner`; no direct `subprocess` call
lives in the manager.  The abstract `ServiceScope.SYSTEM` maps to plain `systemctl
…`; `ServiceScope.USER` adds the `--user` flag.  Writes in system scope pass
`elevated=True` when the config allows; reads never elevate.

---

## Requirements

### Requirement 1: Start a Service

**User Story:** As a maintenance coordinator, I want to start a systemd service,
idempotently and with dry-run support, so that maintenance workflows are safe to
re-run.

#### Acceptance Criteria

1. WHEN the coordinator calls `start_service(name, scope)` with `scope=SYSTEM` THE
   SYSTEM SHALL invoke `systemctl start <name>` via the command runner; with
   `scope=USER` THE SYSTEM SHALL invoke `systemctl --user start <name>`.
2. WHEN the current `active_state` is already `"active"` THE SYSTEM SHALL return a
   `UbuntuServiceActionResult` with `status=SKIPPED` without invoking the start
   command.
3. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuServiceActionResult`
   with `status=SUCCESS` and the service/scope populated.
4. WHEN the command fails THE SYSTEM SHALL return a `UbuntuServiceActionResult` with
   `status=FAILED`, `exit_code`, `stdout`, `stderr`, and `command` populated.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke the runner and SHALL
   return a `UbuntuServiceActionResult` with `status=DRY_RUN`.

---

### Requirement 2: Stop a Service

**User Story:** As a maintenance coordinator, I want to stop a systemd service,
idempotently and with dry-run support.

#### Acceptance Criteria

1. WHEN the coordinator calls `stop_service(name, scope)` THE SYSTEM SHALL invoke
   `systemctl [--user] stop <name>` per scope.
2. WHEN the current `active_state` is `"inactive"` or `"failed"` THE SYSTEM SHALL
   return `status=SKIPPED` without invoking the stop command.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED` with the exit
   code, stdout, and stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN` without
   invoking the runner.

---

### Requirement 3: Restart a Service

**User Story:** As a maintenance coordinator, I want to restart a systemd service
unconditionally, so that configuration changes take effect.

#### Acceptance Criteria

1. WHEN the coordinator calls `restart_service(name, scope)` THE SYSTEM SHALL
   invoke `systemctl [--user] restart <name>` per scope.
2. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
3. WHEN the command fails THE SYSTEM SHALL return `status=FAILED` with captured
   exit code, stdout, and stderr.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN` without
   invoking the runner.
5. WHEN `restart_service` is called the idempotency guard SHALL NOT skip the
   command regardless of current state.

---

### Requirement 4: Reload a Service

**User Story:** As a maintenance coordinator, I want to send `systemctl reload` to a
running service, so that configuration reload happens without process restart.

#### Acceptance Criteria

1. WHEN the coordinator calls `reload_service(name, scope)` AND the current
   `active_state` is `"active"` THE SYSTEM SHALL invoke
   `systemctl [--user] reload <name>` per scope.
2. WHEN the current `active_state` is not `"active"` THE SYSTEM SHALL return
   `status=FAILED` with `message="service is not active; cannot reload"` without
   invoking the reload command.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails (non-zero exit while active) THE SYSTEM SHALL return
   `status=FAILED` with the captured exit code, stdout, and stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN` without
   invoking the runner and without consulting the active-state precondition.

---

### Requirement 5: Enable a Service

**User Story:** As a maintenance coordinator, I want to enable a service for
automatic start at boot, idempotently.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_service(name, scope)` THE SYSTEM SHALL invoke
   `systemctl [--user] enable <name>`.
2. WHEN the current `enabled_state` is already `"enabled"` THE SYSTEM SHALL return
   `status=SKIPPED` without invoking the enable command.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 6: Disable a Service

**User Story:** As a maintenance coordinator, I want to disable a service so that it
does not start at boot, idempotently.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_service(name, scope)` THE SYSTEM SHALL invoke
   `systemctl [--user] disable <name>`.
2. WHEN the current `enabled_state` is `"disabled"` or `"masked"` THE SYSTEM SHALL
   return `status=SKIPPED` without invoking the disable command.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 7: Mask a Service

**User Story:** As a maintenance coordinator, I want to fully mask a systemd service,
idempotently.

#### Acceptance Criteria

1. WHEN the coordinator calls `mask_service(name, scope)` THE SYSTEM SHALL invoke
   `systemctl [--user] mask <name>`.
2. WHEN the current `enabled_state` is `"masked"` THE SYSTEM SHALL return
   `status=SKIPPED` without invoking the mask command.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 8: Unmask a Service

**User Story:** As a maintenance coordinator, I want to remove a mask from a
service, idempotently.

#### Acceptance Criteria

1. WHEN the coordinator calls `unmask_service(name, scope)` THE SYSTEM SHALL invoke
   `systemctl [--user] unmask <name>`.
2. WHEN the current `enabled_state` is not `"masked"` THE SYSTEM SHALL return
   `status=SKIPPED` without invoking the unmask command.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 9: Get Service Status

**User Story:** As a maintenance coordinator, I want to query a service's current
state, so that I can drive conditional logic.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_service_status(name, scope)` THE SYSTEM SHALL
   invoke `systemctl [--user] is-active <name>` and `systemctl [--user] show <name>
   --property=LoadState,UnitFileState,MainPID,Description` via the command runner
   without elevation.
2. WHEN `is-active` exits non-zero with a recognised informational stdout (`inactive`,
   `failed`, `activating`, `deactivating`) THE SYSTEM SHALL NOT raise; the
   `active_state` SHALL be set to the stdout verbatim (stripped).
3. WHEN `show` fails with an unexpected error THE SYSTEM SHALL raise
   `UbuntuServiceQueryError` with the captured exit code and stderr.
4. WHEN the service is unknown THE SYSTEM SHALL return
   `UbuntuServiceStatus(active_state="not-found", enabled_state="not-found",
   main_pid=None, description=None)`.

---

### Requirement 10: List Services

**User Story:** As a maintenance coordinator, I want to list all services for a
scope, so that I can drive cross-service orchestration.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_services(scope)` THE SYSTEM SHALL invoke
   `systemctl [--user] list-units --type=service --all --no-legend --plain` via the
   runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse each line into a
   `UbuntuServiceInfo` with `service_name`, `active_state`, `enabled_state` (left
   empty here — filled by a subsequent `is-enabled` when needed), and `description`.
3. WHEN the command fails THE SYSTEM SHALL raise `UbuntuServiceQueryError` with the
   exit code and stderr.
4. WHEN `scope=USER` and no user instance is running THE SYSTEM SHALL surface the
   runner's failure as `UbuntuServiceQueryError` without special-casing.

---

### Requirement 11: Reload the Daemon

**User Story:** As a maintenance coordinator, I want to run `systemctl daemon-reload`
so that unit-file changes take effect.

#### Acceptance Criteria

1. WHEN the coordinator calls `reload_daemon(scope)` THE SYSTEM SHALL invoke
   `systemctl [--user] daemon-reload`.
2. WHEN the command succeeds THE SYSTEM SHALL return
   `UbuntuServiceActionResult(status=SUCCESS, service_name="", scope=scope, ...)`.
3. WHEN the command fails THE SYSTEM SHALL return `status=FAILED` with the exit code
   and stderr.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN the idempotency guard is evaluated for `reload_daemon` THE SYSTEM SHALL
   NEVER skip.

---

### Requirement 12: Scope Mapping

**User Story:** As a maintenance coordinator, I want `ServiceScope` to map
deterministically to `systemctl` flags, so that my cross-OS orchestration behaves
predictably.

#### Acceptance Criteria

1. WHEN `scope=ServiceScope.SYSTEM` THE SYSTEM SHALL construct commands WITHOUT the
   `--user` flag.
2. WHEN `scope=ServiceScope.USER` THE SYSTEM SHALL construct commands WITH the
   `--user` flag.
3. WHEN an unrecognised scope is supplied THE SYSTEM SHALL raise `ValueError` before
   invoking the runner.
4. WHEN the returned `UbuntuServiceActionResult` has a `scope` field the value SHALL
   equal the scope that was used to build the command.

---

### Requirement 13: Elevation

**User Story:** As a maintenance coordinator, I want system-scope writes to pass
`elevated=True` when the config enables elevation, and reads to never elevate.

#### Acceptance Criteria

1. WHEN `config.elevated=True` AND the verb is a write AND the scope is `SYSTEM` THE
   SYSTEM SHALL pass `elevated=True` to the runner.
2. WHEN `config.elevated=True` AND the verb is a write AND the scope is `USER` THE
   SYSTEM SHALL NOT pass `elevated=True` (per-user services do not require sudo on
   Ubuntu — `loginctl enable-linger` aside).
3. WHEN the verb is a read (`get_service_status`, `list_services`) THE SYSTEM SHALL
   NEVER pass `elevated=True` regardless of config.
4. WHEN the idempotency-probe `is-active` / `show` / `is-enabled` commands are
   issued as part of a write's idempotency check THE SYSTEM SHALL NOT pass
   `elevated=True` — probes never elevate.
