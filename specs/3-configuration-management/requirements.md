# Requirements

## Introduction

The configuration management feature provides the `automojo-sysman-linux-ubuntu`
package with the ability to read, write, diff, validate, back up, restore, and
adjust permissions/ownership on Ubuntu configuration files.  This spec implements
the configuration-management contract defined at
`automojo-sysman-common/specs/contracts/configuration-management/contract.md`
for Ubuntu.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *what*
configuration values to apply — it provides the capability to *do it* reliably,
idempotently, and with full auditability.  All system commands are executed
exclusively through the `automojo-interfaces` command-runner abstraction, and all
configuration file I/O is delegated to the `IFileOperator` abstraction.  No
`subprocess`, no direct `open`, and no direct `pathlib` calls are made inside this
module.

The abstract configuration-source identifier on Ubuntu is always a filesystem path
(e.g., `/etc/ssh/sshd_config`, `/etc/default/ufw`, `/etc/myapp/app.yaml`).
Implementations understand three textual formats: INI, YAML, and shell-style
`KEY=VALUE`.  Format detection is driven by file suffix with an explicit caller
override available.

---

## Requirements

### Requirement 1: Read a Setting

**User Story:** As a maintenance coordinator, I want to read a single setting from
a configuration file, so that I can inspect it without having to parse the whole
file.

#### Acceptance Criteria

1. WHEN the coordinator calls `read_setting(source, key)` THE SYSTEM SHALL read
   the file via `IFileOperator.read_text(path=source)` and parse it using the
   format-appropriate parser.
2. WHEN the requested key exists THE SYSTEM SHALL return a `UbuntuSettingValue`
   populated with `source`, `key`, `value`, and `value_type` (`"ini"`, `"yaml"`,
   or `"shell"`).
3. WHEN the source exists but the key does not THE SYSTEM SHALL raise
   `UbuntuConfigSettingNotFoundError`.
4. WHEN the source does not exist THE SYSTEM SHALL raise
   `UbuntuConfigSettingNotFoundError` with `detail="source missing"`.
5. WHEN the file is unreadable for any other reason THE SYSTEM SHALL raise
   `UbuntuConfigQueryError`.
6. WHEN `read_setting` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 2: Write a Setting

**User Story:** As a maintenance coordinator, I want to write a single setting in
a configuration file, so that I can change a value without rewriting the whole
file.

#### Acceptance Criteria

1. WHEN the coordinator calls `write_setting(source, key, value)` THE SYSTEM SHALL
   read the current file via `IFileOperator.read_text`, parse, mutate the key in
   memory, re-serialise, and write the result via
   `IFileOperator.write_text(path=source, content=..., elevated=elevated)`.
2. WHEN the existing value already equals the requested value THE SYSTEM SHALL
   return `status=SKIPPED` without calling `write_text`.
3. WHEN the source does not exist THE SYSTEM SHALL create it via
   `write_text(path=source, content=<rendered>, elevated=elevated)`.
4. WHEN the write succeeds THE SYSTEM SHALL return `status=SUCCESS`.
5. WHEN the write fails THE SYSTEM SHALL return `status=FAILED` with `stderr` and
   `exit_code` populated.
6. WHEN `dry_run=True` THE SYSTEM SHALL compute a unified diff, populate the
   `diff` field, and return `status=DRY_RUN` without calling `write_text`.

---

### Requirement 3: Remove a Setting

**User Story:** As a maintenance coordinator, I want to remove a single setting
from a configuration file, so that the system falls back to its default.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_setting(source, key)` and the key is not
   present THE SYSTEM SHALL return `status=SKIPPED`.
2. WHEN the key is present THE SYSTEM SHALL read, mutate, re-serialise, and write
   the result via `write_text(..., elevated=elevated)`.
3. WHEN the write succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with the planned
   diff in `diff` and no `write_text` call.
5. WHEN the write fails THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 4: List Settings

**User Story:** As a maintenance coordinator, I want to list every setting in a
configuration file, so that I can enumerate options without having to know them
in advance.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_settings(source)` and the source does not
   exist THE SYSTEM SHALL return an empty list (no raise).
2. WHEN the source exists THE SYSTEM SHALL parse and return one
   `UbuntuSettingValue` per key, preserving original order.
3. WHEN the parser fails THE SYSTEM SHALL raise `UbuntuConfigQueryError`.
4. WHEN `list_settings` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 5: Write Whole-File Content

**User Story:** As a maintenance coordinator, I want to overwrite an entire
configuration file with deterministic content, so that I can apply a whole-file
template.

#### Acceptance Criteria

1. WHEN the coordinator calls `write(source, content, mode, owner, group)` THE
   SYSTEM SHALL call `IFileOperator.write_text(path=source, content=content,
   elevated=elevated)` when `content` differs from the current file content.
2. WHEN `content` matches the current file content THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN `mode` is supplied and the current mode differs THE SYSTEM SHALL invoke
   `chmod <mode> <source>` via the runner after the write.
4. WHEN `owner`/`group` is supplied and differs THE SYSTEM SHALL invoke
   `chown <user>:<group> <source>` via the runner after the write.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with the full diff
   in `diff` and no mutating calls.

---

### Requirement 6: Diff Current vs Desired

**User Story:** As a maintenance coordinator, I want to compute a diff between
current file content and a desired content, so that I can preview changes.

#### Acceptance Criteria

1. WHEN the coordinator calls `diff(source, desired_content)` and the source
   exists THE SYSTEM SHALL read via `IFileOperator.read_text` and compute the
   unified diff using `difflib.unified_diff` in pure Python.
2. WHEN the source does not exist THE SYSTEM SHALL treat the current content as
   an empty string.
3. WHEN `current == desired` THE SYSTEM SHALL return `UbuntuConfigDiffResult`
   with `has_diff=False` and `diff=""`.
4. WHEN they differ THE SYSTEM SHALL return `has_diff=True` and the joined
   unified-diff string.
5. WHEN `diff` is invoked THE SYSTEM SHALL NOT pass `elevated=True`.

---

### Requirement 7: Validate a Source

**User Story:** As a maintenance coordinator, I want to validate a configuration
file using a registered validator, so that I can catch errors before a reload.

#### Acceptance Criteria

1. WHEN the coordinator calls `validate(source, validator_id)` and `validator_id`
   is registered THE SYSTEM SHALL invoke the validator's argv (e.g., `sshd -t -f
   <source>`, `nginx -t -c <source>`, `apachectl -t -f <source>`,
   `named-checkconf <source>`) via the command runner without elevation.
2. WHEN exit code 0 THE SYSTEM SHALL return
   `UbuntuConfigValidationResult(valid=True, exit_code=0, ...)`.
3. WHEN exit code is non-zero THE SYSTEM SHALL return
   `UbuntuConfigValidationResult(valid=False, exit_code, stdout, stderr)`.
4. WHEN `validator_id` is not registered THE SYSTEM SHALL raise
   `UbuntuConfigValidatorNotFoundError`.
5. WHEN `dry_run=True` THE SYSTEM SHALL still invoke the validator since
   validation is read-only.

---

### Requirement 8: Backup a Source

**User Story:** As a maintenance coordinator, I want to take a timestamped backup
of a configuration file, so that I can recover from a bad change.

#### Acceptance Criteria

1. WHEN the coordinator calls `backup_configuration(source)` and the source does
   not exist THE SYSTEM SHALL raise `UbuntuConfigFileNotFoundError`.
2. WHEN the source exists THE SYSTEM SHALL compute a path
   `<source>.bak.<YYYYMMDDTHHMMSS>` using `datetime.utcnow()` and invoke `cp -p
   <source> <backup_path>` via the runner with `elevated=elevated`.
3. WHEN the command succeeds THE SYSTEM SHALL return
   `UbuntuConfigActionResult(status=SUCCESS, backup_path=<path>, ...)`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with the planned
   `backup_path` in the result.

---

### Requirement 9: Restore from a Backup

**User Story:** As a maintenance coordinator, I want to restore a backup over its
original path, so that I can roll back a bad change.

#### Acceptance Criteria

1. WHEN the coordinator calls `restore_configuration(backup_path)` THE SYSTEM
   SHALL derive the original path by stripping the `.bak.<YYYYMMDDTHHMMSS>`
   suffix and invoke `cp -p <backup_path> <original>` via the runner.
2. WHEN the backup does not exist THE SYSTEM SHALL raise
   `UbuntuConfigFileNotFoundError`.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS` with
   `backup_path=<backup_path>`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 10: Set Permissions

**User Story:** As a maintenance coordinator, I want to apply `chmod` to a
configuration file, so that file permissions match policy.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_permissions(source, mode)` and the source
   does not exist THE SYSTEM SHALL raise `UbuntuConfigFileNotFoundError`.
2. WHEN the current mode already matches THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN the current mode differs THE SYSTEM SHALL invoke
   `chmod <mode-octal> <source>` via the runner with `elevated=elevated` and
   return `status=SUCCESS`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 11: Set Ownership

**User Story:** As a maintenance coordinator, I want to apply `chown` to a
configuration file, so that ownership matches policy.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_ownership(source, user, group)` and the source
   does not exist THE SYSTEM SHALL raise `UbuntuConfigFileNotFoundError`.
2. WHEN the current owner and group already match THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN either differs THE SYSTEM SHALL invoke `chown <user>:<group> <source>`
   (with `user:` or `:group` forms when one of the two is `None`) via the runner
   with `elevated=elevated`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 12: Reload Configuration

**User Story:** As a maintenance coordinator, I want to trigger the service that
owns a configuration file to reload, so that changes take effect.

#### Acceptance Criteria

1. WHEN the coordinator calls `reload_configuration(service)` THE SYSTEM SHALL
   invoke `systemctl reload <service>` via the runner.
2. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
3. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 13: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all write operations to run
with root privileges, so that writes under `/etc/` succeed.

#### Acceptance Criteria

1. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to the runner and
   file operator for every write verb.
2. WHEN performing reads THE SYSTEM SHALL NOT pass `elevated=True`.
3. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False` on every path.
