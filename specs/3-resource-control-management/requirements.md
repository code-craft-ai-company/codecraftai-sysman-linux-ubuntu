# Requirements

## Introduction

The resource-control-management feature provides the
`automojo-sysman-linux-ubuntu` package with the ability to manage
named, idempotent, auditable resource ceilings for groups of
processes on Ubuntu systems.  The implementation rides on cgroups
v2 via systemd's `slice` unit type — the same kernel substrate
used by the canonical
[`automojo-sysman-linux-redhat`](../../../automojo-sysman-linux-redhat/specs/3-resource-control-management/requirements.md)
implementation — but exposes the cross-OS verb set defined in the
[`resource-control-management` contract](../../../automojo-sysman-common/specs/contracts/resource-control-management/contract.md).

The Ubuntu manager covers:

- Creating and deleting named slice units under
  `/etc/systemd/system/<name>.slice`.
- Setting per-slice CPU, memory, and IO ceilings via
  `systemctl set-property`.
- Attaching and detaching processes to / from a slice using
  `systemd-run --scope --slice=<name>` for new processes and
  direct writes to `/sys/fs/cgroup/<name>.slice/cgroup.procs` for
  existing PIDs.
- Listing all slices the manager knows about under
  `/sys/fs/cgroup/`.
- Reading per-slice usage counters from
  `cpu.stat`, `memory.current`, and `io.stat` under
  `/sys/fs/cgroup/<name>.slice/`.

All system commands and pseudo-filesystem reads are dispatched
through the injected `ICommandRunner`.  This feature does NOT
consume an `IFileOperator`; cgroup pseudo-files are read through
`cat` and slice unit files are written through `tee`.

This feature complements — and does not duplicate — the systemd
service-lifecycle surface owned by `3-service-management` and the
process-affinity / niceness surface owned by `3-process-management`.

---

## Requirements

### Requirement 1: Create a Resource Group

**User Story:** As a maintenance coordinator, I want to declare
a named resource group with optional initial CPU, memory, and IO
ceilings, so that subsequently spawned or attached processes are
bounded.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_group(spec)` with
   `spec.name == "automojo_batch"` and a populated
   `UbuntuResourceGroupSpec` THE SYSTEM SHALL render a slice
   unit body containing `[Unit]` and `[Slice]` sections with the
   requested `CPUQuota`, `MemoryMax`, and `IOWeight` directives.
2. WHEN the slice unit file does not yet exist THE SYSTEM SHALL
   write `/etc/systemd/system/<name>.slice` via `tee`, then
   invoke `systemctl daemon-reload`, then
   `systemctl start <name>.slice`.
3. WHEN the on-disk slice file matches the rendered body
   byte-for-byte AND the slice's `ActiveState` is already
   `"active"` THE SYSTEM SHALL return
   `UbuntuResourceControlActionResult(status=SKIPPED)` without
   invoking any mutating runner call.
4. WHEN `config.dry_run` is True THE SYSTEM SHALL return
   `status=DRY_RUN` carrying the would-have-been `tee` argv and
   shall not invoke the runner.
5. WHEN the runner reports a non-zero exit code on any step THE
   SYSTEM SHALL return `status=FAILED` with `exit_code`,
   `stdout`, `stderr`, and the failing argv on the result.

---

### Requirement 2: Delete a Resource Group

**User Story:** As a maintenance coordinator, I want to remove a
previously created resource group, so that its slice unit and
cgroup tree are no longer present.

#### Acceptance Criteria

1. WHEN the coordinator calls `delete_group(name)` for an existing
   slice THE SYSTEM SHALL invoke `systemctl stop <name>.slice`,
   `rm /etc/systemd/system/<name>.slice`, and
   `systemctl daemon-reload` in that order.
2. WHEN the slice unit file does not exist THE SYSTEM SHALL
   return `status=SKIPPED` without invoking the runner.
3. WHEN `config.dry_run` is True THE SYSTEM SHALL return
   `status=DRY_RUN` with the planned `systemctl stop` argv.
4. WHEN any step fails THE SYSTEM SHALL return `status=FAILED`
   with the failing step's `exit_code`, `stdout`, `stderr`, and
   `command`.

---

### Requirement 3: Set CPU Limit

**User Story:** As a maintenance coordinator, I want to update a
slice's CPU ceiling without recreating the slice, so that running
workloads are throttled in place.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_cpu_limit(name, percent=50)`
   THE SYSTEM SHALL invoke
   `systemctl set-property <name>.slice CPUQuota=50%`.
2. WHEN `systemctl show <name>.slice --property=CPUQuotaPerSecUSec`
   reports the equivalent of the requested percent THE SYSTEM
   SHALL return `status=SKIPPED` without invoking
   `set-property`.
3. WHEN `config.dry_run` is True THE SYSTEM SHALL return
   `status=DRY_RUN` with the would-have-been argv.
4. WHEN the slice does not exist THE SYSTEM SHALL surface the
   underlying `systemctl set-property` failure as
   `status=FAILED`; the manager does NOT auto-create slices.

---

### Requirement 4: Set Memory Limit

**User Story:** As a maintenance coordinator, I want to update a
slice's memory ceiling, so that processes inside the slice are
OOM-killed when they exceed the limit.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_memory_limit(name, bytes_)`
   with `bytes_=536870912` (512 MiB) THE SYSTEM SHALL invoke
   `systemctl set-property <name>.slice MemoryMax=536870912`.
2. WHEN `systemctl show <name>.slice --property=MemoryMax`
   reports `MemoryMax=536870912` THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN `config.dry_run` is True THE SYSTEM SHALL return
   `status=DRY_RUN`.
4. THE SYSTEM SHALL surface a non-zero `set-property` exit as
   `status=FAILED`.

---

### Requirement 5: Set IO Limits

**User Story:** As a maintenance coordinator, I want to set
per-slice IO weighting and bandwidth caps, so that disk-heavy
workloads do not starve other slices.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `set_io_limits(name, UbuntuIoLimitSpec(weight=200))` THE
   SYSTEM SHALL invoke
   `systemctl set-property <name>.slice IOWeight=200`.
2. WHEN the spec also carries `read_bps=1048576` and the
   coordinator supplies `device="/dev/sda"` (via the spec's
   `device` field) THE SYSTEM SHALL append
   `IOReadBandwidthMax=/dev/sda 1048576`.
3. WHEN every requested key already matches the value reported
   by `systemctl show` THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN `config.dry_run` is True THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 6: Attach Process

**User Story:** As a maintenance coordinator, I want to attach a
running PID to a resource group, so that the existing process
inherits the group's ceilings.

#### Acceptance Criteria

1. WHEN the coordinator calls `attach_process(name, pid)` THE
   SYSTEM SHALL append the PID's decimal representation to
   `/sys/fs/cgroup/<name>.slice/cgroup.procs` via a `tee
   --append` invocation, elevated.
2. WHEN the PID is already present in
   `/sys/fs/cgroup/<name>.slice/cgroup.procs` THE SYSTEM SHALL
   return `status=SKIPPED`.
3. WHEN `config.dry_run` is True THE SYSTEM SHALL return
   `status=DRY_RUN` and shall not write to the cgroup procs file.
4. WHEN the slice cgroup directory does not exist THE SYSTEM
   SHALL return `status=FAILED` with
   `message="slice cgroup not present"`.

---

### Requirement 7: Detach Process

**User Story:** As a maintenance coordinator, I want to detach a
PID from a resource group, returning it to the default
`system.slice`.

#### Acceptance Criteria

1. WHEN the coordinator calls `detach_process(name, pid)` THE
   SYSTEM SHALL append the PID's decimal representation to
   `/sys/fs/cgroup/system.slice/cgroup.procs` via a `tee
   --append` invocation, elevated.  The kernel atomically removes
   the PID from its current cgroup as part of this write.
2. WHEN the PID is not currently in
   `/sys/fs/cgroup/<name>.slice/cgroup.procs` THE SYSTEM SHALL
   return `status=SKIPPED`.
3. WHEN `config.dry_run` is True THE SYSTEM SHALL return
   `status=DRY_RUN`.

---

### Requirement 8: List Resource Groups

**User Story:** As a maintenance coordinator, I want to enumerate
every resource group the manager knows about, so that I can
reconcile observed state with declared state.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_groups()` THE SYSTEM SHALL
   list `/sys/fs/cgroup/` via `ls -1` and select every entry
   ending in `.slice`.
2. FOR each slice THE SYSTEM SHALL invoke
   `systemctl show <name>.slice --property=ActiveState,CPUQuotaPerSecUSec,MemoryMax,IOWeight`
   without elevation and assemble an `UbuntuResourceGroupInfo`
   record per slice.
3. THE SYSTEM SHALL return an empty list (not raise) when no
   `.slice` entries are present in the cgroup root.
4. WHEN any underlying `ls` or `systemctl show` exits non-zero
   AND its stderr is non-empty THE SYSTEM SHALL raise
   `UbuntuResourceControlQueryError` carrying the failing argv
   and stderr.

---

### Requirement 9: Get Group Usage

**User Story:** As a maintenance coordinator, I want to read live
CPU, memory, and IO usage for a slice, so that I can confirm the
ceilings are being honoured.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_group_usage(name)` THE SYSTEM
   SHALL `cat /sys/fs/cgroup/<name>.slice/cpu.stat`, parse the
   `usage_usec` line, and convert microseconds to seconds for
   the returned `cpu_usage_seconds` field.
2. THE SYSTEM SHALL `cat
   /sys/fs/cgroup/<name>.slice/memory.current` and parse the
   integer for `memory_used_bytes`.
3. THE SYSTEM SHALL `cat /sys/fs/cgroup/<name>.slice/io.stat`,
   sum the `rbytes=` and `wbytes=` columns across every device
   line, and return the totals as `io_read_bytes` and
   `io_write_bytes`.
4. THE SYSTEM SHALL count the lines in
   `/sys/fs/cgroup/<name>.slice/cgroup.procs` for
   `process_count`.
5. WHEN the slice cgroup directory does not exist THE SYSTEM
   SHALL raise `UbuntuResourceControlQueryError` with
   `detail="unknown_group"`.

---

### Requirement 10: Idempotency

**User Story:** As a maintenance coordinator, I want every write
verb to short-circuit when the requested state already holds.

#### Acceptance Criteria

1. THE SYSTEM SHALL implement the per-verb idempotency rules
   listed in Requirements 1-7 above.
2. THE SYSTEM SHALL NOT invoke `systemctl set-property`,
   `tee`, or `rm` when the idempotency guard fires.
3. THE SYSTEM SHALL still record the would-have-been argv on
   the returned `UbuntuResourceControlActionResult.command`
   field for audit.

---

### Requirement 11: Elevation

**User Story:** As a maintenance coordinator, I want every write
verb to elevate (`sudo`) when the manager is configured for
elevation, and every read verb to never elevate.

#### Acceptance Criteria

1. WHEN `config.elevated == True` THE SYSTEM SHALL pass
   `elevated=True` to the runner for every write verb's
   mutating command.
2. THE SYSTEM SHALL NOT pass `elevated=True` for the
   idempotency-guard `systemctl show`, `cat`, or `ls`
   probes — those are reads even when called from a write verb.
3. THE SYSTEM SHALL NEVER pass `elevated=True` for read verbs
   (`list_groups`, `get_group_usage`).

---

### Requirement 12: Dry-Run, and Error Reporting

**User Story:** As a maintenance coordinator, I want every
mutating operation to respect dry-run and surface failures
structurally.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch
   `systemctl set-property`, NOT write slice unit files, NOT
   invoke `daemon-reload`, NOT mutate cgroup procs files.  A
   `UbuntuResourceControlActionResult(status=DRY_RUN)` is
   returned with the intended argv on `command`.
2. WHEN the runner returns non-zero THE SYSTEM SHALL return
   `UbuntuResourceControlActionResult(status=FAILED,
   exit_code=<code>, stdout=<captured>, stderr=<captured>,
   command=<argv>)`.
3. THE SYSTEM SHALL NOT raise from any write verb on a
   dispatched-command failure.  Reads MAY raise
   `UbuntuResourceControlQueryError`.
