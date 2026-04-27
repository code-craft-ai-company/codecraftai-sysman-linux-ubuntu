# Requirements

## Introduction

The kdump-management feature provides the `automojo-sysman-linux-ubuntu`
package with the ability to manage the kernel-crash-dump capture subsystem
on Ubuntu / Debian-family hosts. It implements the cross-OS contract
defined at
`automojo-sysman-common/specs/contracts/kdump-management/contract.md`.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interface. The feature
does not decide *whether* a host should have kdump enabled — it provides
the capability to *do it* reliably, idempotently, and with full
auditability. All system commands (`systemctl`, `kdump-config`, `find`,
`ls`, `cat`, `tee`, `stat`) are executed through the
`automojo-interfaces` command-runner abstraction. All reads and writes
against `/etc/default/kdump-tools` flow through the same runner. All
mutating operations respect dry-run mode.

Ubuntu's kdump implementation diverges from RHEL's in two notable
ways:

1. Configuration lives in `/etc/default/kdump-tools` (a shell-sourced
   `KEY="value"` file consumed by the kdump-tools init scripts) rather
   than RHEL's directive-style `/etc/kdump.conf`.
2. The systemd unit name is `kdump-tools.service`, not `kdump.service`.

The verb mapping otherwise tracks the cross-OS contract directly. The
`crashkernel=` kernel-command-line reservation is OUT OF SCOPE for this
spec and remains delegated to the `boot-management` feature.

---

## Requirements

### Requirement 1: Enable kdump-tools

**User Story:** As a maintenance coordinator, I want to enable kdump
capture on Ubuntu so that the next kernel panic produces a usable
vmcore.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_kdump()` THE SYSTEM SHALL set
   `USE_KDUMP=1` in `/etc/default/kdump-tools` via
   `cat`/`tee`-through-runner.
2. THEN THE SYSTEM SHALL invoke
   `systemctl enable --now kdump-tools.service` with elevated privileges.
3. WHEN the defaults file already carries `USE_KDUMP=1` AND the systemd
   unit is already `active`+`enabled` THE SYSTEM SHALL return a
   `UbuntuKdumpActionResult` with status `SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command
   and SHALL return a `UbuntuKdumpActionResult` with status `DRY_RUN`
   carrying the would-be `systemctl` argv on the `command` field.
5. WHEN the `tee` write succeeds but the `systemctl` invocation fails
   THE SYSTEM SHALL return `UbuntuKdumpActionResult(status=FAILED,
   exit_code=<code>, stderr=<captured>)`.

---

### Requirement 2: Disable kdump-tools

**User Story:** As a maintenance coordinator, I want to disable kdump
capture so that the host does not reserve memory for a capture kernel
that operations no longer requires.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_kdump()` THE SYSTEM SHALL set
   `USE_KDUMP=0` in `/etc/default/kdump-tools` via
   `cat`/`tee`-through-runner.
2. THEN THE SYSTEM SHALL invoke
   `systemctl disable --now kdump-tools.service` with elevated
   privileges.
3. WHEN the defaults file already carries `USE_KDUMP=0` AND the systemd
   unit is already inactive+disabled THE SYSTEM SHALL return a
   `UbuntuKdumpActionResult` with status `SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command.

---

### Requirement 3: Configure kdump Target

**User Story:** As a maintenance coordinator, I want to point kdump at
a specific dump destination (local path, NFS export, SSH endpoint, raw
device) so that captured dumps land where my fleet's storage policy
requires.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `configure_kdump_target(spec=UbuntuKdumpTargetSpec(...))` AND
   `target_kind=LOCAL_PATH` THE SYSTEM SHALL rewrite `KDUMP_COREDIR` to
   `spec.path` and SHALL blank the `SSH`, `NFS`, and `RAW` keys.
2. WHEN `target_kind=SSH` THE SYSTEM SHALL rewrite the `SSH` key to
   `<ssh_user>@<ssh_host>` and SHALL blank the `NFS` and `RAW` keys.
3. WHEN `target_kind=NFS` THE SYSTEM SHALL rewrite the `NFS` key to
   `spec.nfs_export` and SHALL blank the `SSH` and `RAW` keys.
4. WHEN `target_kind=RAW` THE SYSTEM SHALL rewrite the `RAW` key to
   `spec.path` and SHALL blank the `SSH` and `NFS` keys.
5. WHEN the `compression` field is non-None THE SYSTEM SHALL also write
   `MAKEDUMPFILE_ARGS=<compression>`.
6. AFTER the defaults file is written THE SYSTEM SHALL invoke
   `systemctl restart kdump-tools.service` with elevated privileges.
7. WHEN the existing defaults file already encodes the requested target
   THE SYSTEM SHALL return a `UbuntuKdumpActionResult` with status
   `SKIPPED` and SHALL NOT issue the restart.
8. WHEN the spec is invalid (e.g. `LOCAL_PATH` with `path=None`) THE
   SYSTEM SHALL return `FAILED` with a descriptive `message` and SHALL
   NOT touch the defaults file.

---

### Requirement 4: Query kdump Status

**User Story:** As a maintenance coordinator, I want to know whether
kdump is enabled, armed, and pointed at a known target so that I can
verify a host's compliance with the declared maintenance baseline.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_kdump_status()` THE SYSTEM SHALL
   read `/etc/default/kdump-tools` via `cat`-through-runner and SHALL
   query `systemctl is-active` and `systemctl is-enabled` for the
   configured service.
2. THE SYSTEM SHALL also invoke `kdump-config show` and parse its
   output for the capture-kernel image path.
3. THE SYSTEM SHALL return a `UbuntuKdumpStatus` carrying `enabled`,
   `running`, `target_kind`, `target_path`, `kernel`, and
   `reserved_memory_mb`.
4. THE SYSTEM SHALL NEVER pass `elevated=True` for any of these reads.
5. WHEN `kdump-config show` returns a non-zero exit THE SYSTEM SHALL
   populate `message` with an advisory but SHALL still return a
   well-formed status describing the defaults-file state.

---

### Requirement 5: List Recent Dumps

**User Story:** As a maintenance coordinator, I want to enumerate
recently captured kdump artifacts so that I can audit how often the
fleet is panicking and which hosts produced the dumps.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_recent_dumps()` THE SYSTEM SHALL
   invoke `ls -1t <crash_dir>` via the runner without elevation.
2. THE SYSTEM SHALL return one `UbuntuKdumpInfo` per non-hidden listing
   entry, freshest first, with `id` set to the entry name and `path`
   set to `<crash_dir>/<id>`.
3. WHEN `<crash_dir>` does not exist THE SYSTEM SHALL return an empty
   list.
4. WHEN `ls` fails for any other reason THE SYSTEM SHALL raise
   `UbuntuKdumpQueryError`.

---

### Requirement 6: Clear Old Dumps

**User Story:** As a maintenance coordinator, I want to purge dump
artifacts older than a threshold so that the crash directory does not
grow without bound.

#### Acceptance Criteria

1. WHEN the coordinator calls `clear_old_dumps(age_days=N)` THE SYSTEM
   SHALL invoke `find <crash_dir> -maxdepth 1 -type d -mtime +N -not
   -path <crash_dir> -exec rm -rf {} \;` via the runner with elevated
   privileges.
2. WHEN `age_days < 0` THE SYSTEM SHALL return a `FAILED` result with
   `message="age_days must be >= 0"` and SHALL NOT execute the command.
3. THE SYSTEM SHALL return `SUCCESS` even when no directories matched
   the filter — the find command is itself idempotent.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute the find
   command.

---

### Requirement 7: Get Dump Info

**User Story:** As a maintenance coordinator, I want to surface
metadata about a single dump (kernel string, panic line, size,
mtime) so that I can attach context to a maintenance ticket.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_dump_info(dump_id=ID)` THE SYSTEM
   SHALL `cat <crash_dir>/<ID>/vmcore-dmesg.txt` and
   `cat <crash_dir>/<ID>/dmesg.<ID>` via the runner without elevation.
2. THE SYSTEM SHALL parse the first ~30 lines of the combined text for
   the `Linux version` and `Kernel panic` lines.
3. THE SYSTEM SHALL also `stat -c %y;%s <crash_dir>/<ID>` to populate
   `created` and `size_bytes`.
4. THE SYSTEM SHALL return a `UbuntuKdumpInfo` carrying `id`, `path`,
   `created`, `size_bytes`, `kernel`, and `panic_message`.
5. WHEN both metadata files are unreadable THE SYSTEM SHALL raise
   `UbuntuKdumpQueryError`.

---

### Requirement 8: Idempotency Guards

**User Story:** As a maintenance coordinator, I want repeat calls to
the same write verb to be no-ops when the host already matches the
desired state, so that maintenance runs are safe to re-execute.

#### Acceptance Criteria

1. `enable_kdump()` SHALL skip the write when both the defaults flag
   and the systemd unit already report enabled.
2. `disable_kdump()` SHALL skip the write when both the defaults flag
   and the systemd unit already report disabled.
3. `configure_kdump_target()` SHALL skip the write when the rendered
   defaults body byte-matches the current on-disk body.
4. `clear_old_dumps()` SHALL execute unconditionally — `find -mtime +N`
   is naturally idempotent.

---

### Requirement 9: Elevation Policy

**User Story:** As a maintenance coordinator, I want writes to elevate
when configured but reads to never elevate, so that the manager works
on hosts that do not run as root.

#### Acceptance Criteria

1. WHEN `config.elevated=True` AND the verb is a write
   (`enable_kdump`, `disable_kdump`, `configure_kdump_target`,
   `clear_old_dumps`) THE SYSTEM SHALL pass `elevated=True` to the
   runner for both the file-write and the systemctl call.
2. WHEN `config.elevated=False` THE SYSTEM SHALL never pass
   `elevated=True`.
3. WHEN the verb is a read (`get_kdump_status`, `list_recent_dumps`,
   `get_dump_info`) THE SYSTEM SHALL never pass `elevated=True`,
   regardless of the config.
4. Idempotency probes (the `cat` of the defaults file, `is-active`
   / `is-enabled`) MUST never elevate.

---

### Requirement 10: Dry-Run Safety

**User Story:** As a maintenance coordinator, I want a dry-run mode
that exercises the planning logic without touching the host, so that
maintenance plans can be reviewed before execution.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` AND a write verb is invoked THE SYSTEM
   SHALL NOT call `runner.run` for the mutating step.
2. THE SYSTEM SHALL still return a `UbuntuKdumpActionResult` with
   status `DRY_RUN` and the `command` field populated with the argv
   that would have been executed.
3. THE SYSTEM SHALL NEVER write to `/etc/default/kdump-tools` in
   dry-run mode.
4. Read verbs SHALL execute unchanged in dry-run mode — reads do not
   mutate.
