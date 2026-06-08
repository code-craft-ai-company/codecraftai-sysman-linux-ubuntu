# Requirements

## Introduction

The kernel parameters feature provides the `codecraftai-sysman-linux-ubuntu` package with
the ability to read, write, and persist `sysctl` kernel tunables on Ubuntu systems.
This spec implements the kernel-parameters contract defined at
`codecraftai-sysman-common/specs/contracts/kernel-parameters/contract.md` for Ubuntu
using `sysctl` at runtime and drop-in files under `/etc/sysctl.d/` for persistence.
It includes querying a single parameter, listing all parameters, writing parameters
either at runtime, persistently, or both, reloading persisted parameters into the
running kernel, and removing a previously persisted parameter.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *which*
parameters to apply — it provides the capability to *do it* reliably, idempotently,
and with full auditability.  All system commands are executed exclusively through the
`codecraftai-interfaces` command-runner abstraction and all drop-in file mutations are
executed through the `codecraftai-interfaces` file-operator abstraction, enabling
dry-run simulation, auditing, and test mocking without requiring a live system.

Ubuntu's kernel-parameter surface matches the RHEL reference almost exactly — both
distributions ship the same `sysctl(8)` command and the same `/etc/sysctl.d/`
drop-in convention — so the verb mapping is a direct adaptation.  The Ubuntu
implementation differs only in its default drop-in directory precedence (Ubuntu
also honors `/usr/lib/sysctl.d/` and `/run/sysctl.d/`) and in the allowlist of
parameter prefixes it accepts.

---

## Requirements

### Requirement 1: Get a Single Parameter

**User Story:** As a maintenance coordinator, I want to read the current runtime
value of a single sysctl parameter, so that I can make conditional decisions based
on its value.

#### Acceptance Criteria

1. WHEN the coordinator provides a parameter name THE SYSTEM SHALL invoke
   `sysctl -n <name>` via the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuParameterValue` with
   `name=<name>`, `value=<stripped stdout>`, and `source="runtime"`.
3. WHEN the command fails with a "sysctl: cannot stat" error THE SYSTEM SHALL
   return `None`.
4. WHEN the command fails for any other reason THE SYSTEM SHALL raise a
   `UbuntuKernelParameterQueryError` carrying the exit code and captured stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL still execute the read — reads
   do not mutate and are not short-circuited by dry-run.

---

### Requirement 2: Set a Parameter

**User Story:** As a maintenance coordinator, I want to set a sysctl parameter
value at runtime, persistently, or both, so that I can enforce a desired kernel
tuning state.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_parameter(name, value, persistent=False)` AND
   the current runtime value (read via `sysctl -n <name>`) already equals the
   requested value THE SYSTEM SHALL return a `UbuntuKernelParameterActionResult`
   with status `SKIPPED` and populate `current_value` / `applied_value` with the
   matching value.
2. WHEN the coordinator calls `set_parameter(name, value, persistent=False)` AND
   the runtime value differs THE SYSTEM SHALL invoke `sysctl -w <name>=<value>`
   via the command runner.
3. WHEN the coordinator calls `set_parameter(name, value, persistent=True)` AND
   the drop-in file `<persist_dir>/<name>.conf` already contains the matching
   `name = value` line THE SYSTEM SHALL return a
   `UbuntuKernelParameterActionResult` with status `SKIPPED`.
4. WHEN the coordinator calls `set_parameter(name, value, persistent=True)` AND
   the persisted value differs or is absent THE SYSTEM SHALL update the drop-in
   file `<persist_dir>/<name>.conf` via the `IFileOperator`, AND THEN SHALL also
   apply the runtime change via `sysctl -w <name>=<value>`.
5. WHEN the parameter name is not in the Ubuntu-supported prefix allowlist THE
   SYSTEM SHALL return a `UbuntuKernelParameterActionResult` with status
   `FAILED`, `message="parameter_not_supported"`, and SHALL NOT invoke the
   runner or file operator.
6. WHEN either the persistent-write step or the runtime-apply step fails THE
   SYSTEM SHALL return a `UbuntuKernelParameterActionResult` with status
   `FAILED`, a `message` identifying which step failed, and the captured
   exit code / stdout / stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command or file
   mutation and SHALL return a `UbuntuKernelParameterActionResult` with status
   `DRY_RUN`; for persistent writes the `diff` field SHALL carry the unified
   diff that would have been applied to the drop-in file.
8. WHEN `requires_reboot` applies THE SYSTEM SHALL return `requires_reboot=False`
   — no Ubuntu sysctl parameter requires reboot.

---

### Requirement 3: List All Parameters

**User Story:** As a maintenance coordinator, I want to enumerate every sysctl
parameter and its current value, so that I can audit system configuration or
drive mass-set logic externally.

#### Acceptance Criteria

1. WHEN the coordinator requests all parameters THE SYSTEM SHALL invoke
   `sysctl -a` via the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse every `name = value` line
   in stdout and SHALL return a `dict[str, str]` mapping name to value.
3. WHEN the command exits non-zero THE SYSTEM SHALL raise a
   `UbuntuKernelParameterQueryError` carrying the exit code and captured
   stderr.
4. WHEN stdout contains lines that do not match the `name = value` pattern THE
   SYSTEM SHALL silently skip those lines (some `sysctl -a` rows are diagnostic
   stderr echoes).

---

### Requirement 4: Reload Persisted Parameters

**User Story:** As a maintenance coordinator, I want to re-apply every persisted
sysctl setting into the running kernel, so that a batch of drop-in file changes
takes effect without rebooting.

#### Acceptance Criteria

1. WHEN the coordinator requests a reload THE SYSTEM SHALL invoke
   `sysctl --system` via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a
   `UbuntuKernelParameterActionResult` with status `SUCCESS` and
   `name="*"`.
3. WHEN the command fails THE SYSTEM SHALL return a
   `UbuntuKernelParameterActionResult` with status `FAILED`, the exit code,
   and captured stderr.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and
   SHALL return a `UbuntuKernelParameterActionResult` with status `DRY_RUN`.

---

### Requirement 5: Remove a Persisted Parameter

**User Story:** As a maintenance coordinator, I want to remove a previously
persisted sysctl parameter from its drop-in file, so that future reboots revert
to the kernel default.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_persisted_parameter(name)` AND the
   drop-in file `<persist_dir>/<name>.conf` does not exist THE SYSTEM SHALL
   return a `UbuntuKernelParameterActionResult` with status `SKIPPED`.
2. WHEN the coordinator calls `remove_persisted_parameter(name)` AND the
   drop-in file exists but does not contain the parameter THE SYSTEM SHALL
   return a `UbuntuKernelParameterActionResult` with status `SKIPPED`.
3. WHEN the parameter is present in the drop-in file THE SYSTEM SHALL remove
   the matching line via the `IFileOperator` (rewriting the file with the
   line removed); WHEN the file would be left empty or contain only comments
   the system SHALL delete the file entirely.
4. WHEN the file write or delete succeeds THE SYSTEM SHALL return a
   `UbuntuKernelParameterActionResult` with status `SUCCESS`.
5. WHEN the file write or delete fails THE SYSTEM SHALL return a
   `UbuntuKernelParameterActionResult` with status `FAILED`, the exit code,
   and captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any mutation and
   SHALL return a `UbuntuKernelParameterActionResult` with status `DRY_RUN`
   and a `diff` field showing the would-be file mutation.

---

### Requirement 6: Parameter-Prefix Allowlist

**User Story:** As a maintenance coordinator, I want the manager to reject
parameters outside Ubuntu's supported namespace, so that typos or unsupported
tunables fail fast with a clear error.

#### Acceptance Criteria

1. THE SYSTEM SHALL accept parameter names whose dotted prefix begins with any
   of: `kernel.`, `vm.`, `net.`, `fs.`, `user.`, `abi.`, `dev.`, `debug.`.
2. WHEN a parameter name does not match the allowlist THE SYSTEM SHALL return
   a `UbuntuKernelParameterActionResult` with status `FAILED` and a `message`
   equal to `"parameter_not_supported: <name>"`.
3. THE SYSTEM SHALL NOT treat allowlist rejection as a runner failure — no
   command or file mutation is invoked.

---

### Requirement 7: Drop-in File Format

**User Story:** As a maintenance coordinator, I want persisted parameters
written in the Ubuntu-standard sysctl drop-in format, so that distribution
tooling (`systemd-sysctl`) consumes them correctly on reboot.

#### Acceptance Criteria

1. THE SYSTEM SHALL write drop-in files as `<persist_dir>/<parameter-name>.conf`
   where `<persist_dir>` defaults to `/etc/sysctl.d/`.
2. THE SYSTEM SHALL render each entry as a single line `<name> = <value>\n`.
3. WHEN a drop-in file already contains the parameter the system SHALL rewrite
   the file preserving all other lines and comments.
4. WHEN writing a new drop-in file the system SHALL prepend a header comment
   line `# managed by codecraftai-sysman-linux-ubuntu` on its own line.

---

### Requirement 8: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all kernel-parameter write
commands to run with the privileges required by `sysctl` and drop-in file
writes, so that mutations succeed on a standard system where the process does
not run as root.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner for `sysctl -w`, `sysctl --system`,
   and to the file operator for every drop-in file write or delete.
2. WHEN performing read-only operations (`get_parameter`, `list_parameters`)
   THE SYSTEM SHALL NOT pass `elevated=True`, regardless of config.
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass
   `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to
   no elevation and allow the runner/file operator to surface the resulting
   permission error as a `UbuntuKernelParameterActionResult` with
   `status=FAILED` for writes.

---

### Requirement 9: Dry-Run Safety

**User Story:** As a maintenance coordinator, I want a dry-run mode that
produces the full would-be command sequence without mutating the system, so
that change reviews are possible before roll-out.

#### Acceptance Criteria

1. WHEN the system is configured with `dry_run=True` THE SYSTEM SHALL NOT
   invoke `sysctl -w`, `sysctl --system`, or any file-operator write/delete.
2. WHEN dry-run is active THE SYSTEM SHALL populate the `command` field of
   every returned `UbuntuKernelParameterActionResult` with the argv that
   would have been executed.
3. WHEN dry-run is active AND the operation is a persistent write or remove
   THE SYSTEM SHALL populate the `diff` field with the unified diff that
   would have been applied to the drop-in file.
4. WHEN dry-run is active THE SYSTEM SHALL still perform read operations to
   evaluate idempotency guards — dry-run is a write-only short-circuit.
