# Requirements

## Introduction

The precision-time-management feature provides the
`automojo-sysman-linux-ubuntu` package with the ability to query and
configure the host's NTP synchronisation surface on Ubuntu systems.
This spec implements the precision-time-management contract defined at
`automojo-sysman-common/specs/contracts/precision-time-management/contract.md`
for Ubuntu using either `chrony` (the default; `chronyc` and the
`chrony.service` unit) or `systemd-timesyncd` (`timedatectl` and the
`systemd-timesyncd.service` unit).  The selection between backends is
declared at construction time on
`UbuntuPrecisionTimeManagerConfig`; a manager instance binds to a
single backend for its lifetime.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *which* time sources should be active or
*whether* clock sync should be on — it provides the capability to
*manipulate the precision-time surface* reliably, idempotently, and
with full auditability.  All system commands flow through the
`automojo-interfaces` command-runner abstraction.  Mutations against
`/etc/chrony/chrony.conf` and `/etc/systemd/timesyncd.conf` flow
through the runner via `cat` and `tee`, matching the locked-in
file-I/O pattern of every other Ubuntu feature in this batch.

This feature is parallel to — and does not duplicate — the
clock-source surface of any other feature.  Timezone management lives
under `localization-management`; this feature owns clock
synchronisation only.

---

## Requirements

### Requirement 1: Query Overall Time Status

**User Story:** As a maintenance coordinator, I want to read the
host's overall sync status, so that reporting and idempotency
workflows can branch on whether the clock is synchronised, which
upstream is currently selected, and how recently a sync occurred.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_time_status()` AND the active
   backend is chrony THE SYSTEM SHALL dispatch `chronyc tracking`
   through the command runner without elevation.
2. WHEN the coordinator calls `get_time_status()` AND the active
   backend is timesyncd THE SYSTEM SHALL dispatch `timedatectl status`
   through the command runner without elevation.
3. WHEN the chrony backend's tooling reports a "daemon not running"
   stderr signature (``506 Cannot talk to daemon``,
   ``connection refused``) THE SYSTEM SHALL return a
   `UbuntuTimeStatus` with `enabled=False` and `synchronized=False`
   rather than raise.
4. WHEN the backend tooling fails for any other reason THE SYSTEM
   SHALL raise a `UbuntuPrecisionTimeQueryError` carrying the failing
   command, exit code, and stderr.

---

### Requirement 2: Enumerate Configured Time Sources

**User Story:** As a maintenance coordinator, I want to enumerate the
host's configured upstream time sources so that idempotency guards on
`add_time_source` / `remove_time_source` / `set_time_servers` can
reason about current state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_time_sources()` AND the active
   backend is chrony THE SYSTEM SHALL dispatch `chronyc sources` and
   parse the columnar output into one `UbuntuTimeSourceInfo` per
   data row, mapping the chrony `MS` token's state character to
   `"selected"` (`*`), `"candidate"` (`+`, `-`), or
   `"unreachable"` (`?`, `x`).
2. WHEN the coordinator calls `get_time_sources()` AND the active
   backend is timesyncd THE SYSTEM SHALL dispatch
   `timedatectl show --property=NTP --property=NTPSynchronized
   --property=SystemNTPServers --property=FallbackNTPServers
   --property=ServerName --property=ServerAddress` and parse the
   property block, marking the `ServerName` entry as `"selected"`.
3. WHEN no upstream sources are configured THE SYSTEM SHALL return
   an empty list rather than raise.

---

### Requirement 3: Add a Time Source

**User Story:** As a maintenance coordinator, I want to add an
upstream NTP source so that the host gains another peer to compare
against.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_time_source(server)` AND the
   active backend is chrony THE SYSTEM SHALL append a
   `pool <server> iburst` line to `/etc/chrony/chrony.conf` and
   restart the chrony service.
2. WHEN the coordinator calls `add_time_source(server)` AND the
   active backend is timesyncd THE SYSTEM SHALL rewrite the `NTP=`
   line of `/etc/systemd/timesyncd.conf` with the new full server
   list and restart `systemd-timesyncd`.
3. WHEN the supplied server already appears in the active config
   THE SYSTEM SHALL skip execution and return a
   `UbuntuPrecisionTimeActionResult` with `status=SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return a `UbuntuPrecisionTimeActionResult` with
   `status=DRY_RUN` and the would-have-been `tee` argv recorded on
   `command`.
5. WHEN the file write fails OR the service restart fails THE SYSTEM
   SHALL return `status=FAILED` with the captured `exit_code`,
   `stdout`, `stderr`, and the failing argv on `command`.

---

### Requirement 4: Remove a Time Source

**User Story:** As a maintenance coordinator, I want to remove an
upstream NTP source so that the host stops considering it.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_time_source(server)` THE
   SYSTEM SHALL recompute the active server list with the requested
   server removed and rewrite the active backend's config file plus
   restart the backend's service.
2. WHEN the supplied server is not present in the active config
   THE SYSTEM SHALL skip execution and return `status=SKIPPED`.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return `status=DRY_RUN`.

---

### Requirement 5: Replace the Time-Source List

**User Story:** As a maintenance coordinator, I want to replace the
host's full upstream list in a single transaction so that the
declared peer set is the only peer set.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_time_servers(servers)` THE
   SYSTEM SHALL render the supplied list into the active backend's
   config file and restart the backend's service.
2. WHEN the existing list (under sorted comparison) already equals
   the desired list THE SYSTEM SHALL skip execution and return
   `status=SKIPPED`.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return `status=DRY_RUN`.

---

### Requirement 6: Force a Synchronisation

**User Story:** As a maintenance coordinator, I want to force the
host to attempt an immediate sync so that operator-driven workflows
can re-converge after a known clock drift event.

#### Acceptance Criteria

1. WHEN the coordinator calls `force_sync_now()` AND the active
   backend is chrony THE SYSTEM SHALL dispatch `chronyc makestep`
   through the runner.
2. WHEN the coordinator calls `force_sync_now()` AND the active
   backend is timesyncd THE SYSTEM SHALL dispatch
   `systemctl restart systemd-timesyncd` through the runner so the
   daemon re-polls its sources on startup.
3. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN` without invoking the runner.
4. `force_sync_now` MUST NOT short-circuit on idempotency; the
   verb is operational.

---

### Requirement 7: Read Drift

**User Story:** As a maintenance coordinator, I want to read the
host's current drift so that reporting workflows can capture
offset / frequency / jitter without hand-parsing tool output.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_drift()` AND the active backend
   is chrony THE SYSTEM SHALL parse `chronyc tracking`'s
   `Last offset`, `RMS offset`, `Frequency`, and `Stratum` fields
   into a `UbuntuTimeDrift` record.
2. WHEN the coordinator calls `get_drift()` AND the active backend
   is timesyncd THE SYSTEM SHALL parse the
   `NTPMessage={ ... Offset=..., Jitter=... }` blob from
   `timedatectl show --property=NTPMessage --property=ServerName`.
3. WHEN the chrony backend's tooling reports a "daemon not running"
   stderr signature THE SYSTEM SHALL return a `UbuntuTimeDrift` with
   `offset_seconds=0.0` and the optional fields set to `None` rather
   than raise.

---

### Requirement 8: Enable Sync

**User Story:** As a maintenance coordinator, I want to enable the
host's clock-sync service so that the host begins keeping its clock
synchronised.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_sync()` AND the active backend
   is chrony THE SYSTEM SHALL dispatch
   `systemctl enable --now chrony` (or the configured service name).
2. WHEN the coordinator calls `enable_sync()` AND the active backend
   is timesyncd THE SYSTEM SHALL dispatch
   `timedatectl set-ntp true`.
3. WHEN the service is already enabled / active THE SYSTEM SHALL
   skip execution and return `status=SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN` without invoking the runner.

---

### Requirement 9: Disable Sync

**User Story:** As a maintenance coordinator, I want to disable the
host's clock-sync service so that the host stops adjusting its clock
from upstream.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_sync()` AND the active
   backend is chrony THE SYSTEM SHALL dispatch
   `systemctl disable --now chrony` (or the configured service
   name).
2. WHEN the coordinator calls `disable_sync()` AND the active
   backend is timesyncd THE SYSTEM SHALL dispatch
   `timedatectl set-ntp false`.
3. WHEN the service is already disabled THE SYSTEM SHALL skip
   execution and return `status=SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return
   `status=DRY_RUN` without invoking the runner.

---

### Requirement 10: Backend Selection

**User Story:** As a maintenance coordinator, I want to select the
NTP backend at construction time so that the same manager class can
be driven across both chrony hosts and timesyncd hosts.

#### Acceptance Criteria

1. WHEN `config.backend` is `UbuntuTimeBackend.CHRONY` (the default)
   THE SYSTEM SHALL route every verb through the chrony tool family.
2. WHEN `config.backend` is `UbuntuTimeBackend.TIMESYNCD` THE SYSTEM
   SHALL route every verb through the timedatectl / timesyncd tool
   family.
3. WHEN `config.backend` is `UbuntuTimeBackend.NTPD` THE SYSTEM
   SHALL raise `NotImplementedError` from the first verb that
   touches the runner.  This Wave 2.4 batch does not include an
   `ntpd` backend; the enum value is reserved for a future spec
   iteration.
4. WHEN `config.service_name` is set THE SYSTEM SHALL prefer the
   override over the auto-derived service name when issuing
   `systemctl` calls.

---

### Requirement 11: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all
precision-time write operations to run with root privilege via
`sudo`, so that mutations against `/etc/chrony/chrony.conf`,
`/etc/systemd/timesyncd.conf`, and the systemd unit lifecycle
succeed on a standard system where the process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the runner for every write
   (`add_time_source`, `remove_time_source`, `set_time_servers`,
   `force_sync_now`, `enable_sync`, `disable_sync`).
2. WHEN performing read-only operations (`get_time_status`,
   `get_time_sources`, `get_drift`, the idempotency probes that
   read the active config, the `systemctl is-active` probe) THE
   SYSTEM SHALL NOT pass `elevated=True`, regardless of
   configuration.
3. WHEN the elevation configuration is not set THE SYSTEM SHALL
   default to no elevation and allow the runner to surface the
   resulting permission error as a
   `UbuntuPrecisionTimeActionResult` with `status=FAILED` for
   writes or as a `UbuntuPrecisionTimeQueryError` for reads.

---

## Out of Scope

- PTP / IEEE 1588 (`ptp4l`, `phc2sys`, `pmc`) — owned by
  `automojo-sysman-linux-redhat` for the canonical PTP surface and
  not in scope for the Ubuntu spec.
- NTP authentication keys (`keyfile`, `keys`).
- Per-source PPS hardware configuration.
- Timezone selection (lives under `localization-management`).
- Hardware clock (`hwclock`) management.
