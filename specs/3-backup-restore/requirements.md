# Requirements

## Introduction

The backup-restore feature provides the `codecraftai-sysman-linux-ubuntu` package with
the ability to define, persist, run, list, restore, and verify file-tree backup
jobs on Ubuntu / Debian systems. Ubuntu has no first-party backup framework
analogous to macOS Time Machine or Windows `wbadmin`; instead, the standard
operator surface is a small family of POSIX archive and synchronisation tools:
`tar`, `rsync`, and (when installed) `restic`. This feature exposes a single
`UbuntuBackupRestoreManager` whose backend dispatches to whichever of those
three tools the caller selects via the `UbuntuBackupKind` enum.

A maintenance coordinator uses this feature to capture known-good snapshots of
specified paths before destructive or risky changes, to restore from those
snapshots on demand, and to verify that an archive is intact. All system
commands (`rsync`, `restic`, `tar`, `tee`, `cat`, `ls`, `rm`) are executed
through the `codecraftai-interfaces` command-runner abstraction so the manager
works identically under local and SSH execution. All mutating operations
respect dry-run mode.

This feature does not perform block-level snapshots, LVM snapshot rotation,
APFS / Btrfs snapshot capture, or database-consistent dumps. Block-level
snapshots are the responsibility of the `storage-management` feature, and
application-consistent dumps remain the responsibility of application-specific
tooling. Schedule wiring is delegated to the `scheduled-tasks` feature; this
feature persists the `schedule` field in the on-disk job spec but does not
itself create cron / systemd-timer entries.

---

## Requirements

### Requirement 1: Persist a Backup Job Specification

**User Story:** As a maintenance coordinator, I want to declare a named backup
job from a declarative spec so that subsequent runs can dispatch the correct
backend tool without re-supplying its parameters.

#### Acceptance Criteria

1. WHEN the coordinator supplies a `UbuntuBackupJobSpec` (or its `UbuntuBackupSpec`
   alias) THE SYSTEM SHALL serialise the spec to JSON and write it to
   `/var/lib/ccai/backups/jobs/<job_id>.json` via `tee` with the JSON
   payload supplied on stdin.
2. THE SYSTEM SHALL pass `elevated=True` to the runner so that `tee` can write
   under `/var/lib/ccai/backups/jobs/`, which is owned by `root`.
3. WHEN a job with the same `job_id` already exists on disk AND the persisted
   JSON is byte-equivalent to the proposed JSON, THE SYSTEM SHALL return
   `UbuntuBackupActionStatus.SKIPPED` without invoking `tee`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any runner command
   and SHALL return a `UbuntuBackupActionResult` with status `DRY_RUN` and the
   would-have-been argv recorded on `command`.
5. WHEN `tee` exits non-zero THE SYSTEM SHALL return `FAILED` with the runner's
   exit code, captured stdout, captured stderr, and the argv populated on the
   result's `command` field.

---

### Requirement 2: Delete a Persisted Backup Job

**User Story:** As a maintenance coordinator, I want to delete a previously-defined
backup job so that the host stops carrying its declarative footprint.

#### Acceptance Criteria

1. WHEN the coordinator supplies a `job_id` THE SYSTEM SHALL invoke
   `rm -f /var/lib/ccai/backups/jobs/<job_id>.json` through the runner with
   `elevated=True`.
2. WHEN the spec file does not exist on disk THE SYSTEM SHALL return
   `UbuntuBackupActionStatus.SKIPPED` without invoking `rm`.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke `rm` and SHALL
   return `DRY_RUN` with the would-have-been argv recorded on `command`.
4. WHEN `rm` exits non-zero THE SYSTEM SHALL return `FAILED` with the runner's
   captured exit code, stdout, stderr, and argv.

---

### Requirement 3: Run a Backup On Demand

**User Story:** As a maintenance coordinator, I want to run a previously-defined
job on demand so that a fresh archive is captured before an immediate risky
change.

#### Acceptance Criteria

1. WHEN the coordinator supplies a `job_id` THE SYSTEM SHALL read the persisted
   spec via `cat /var/lib/ccai/backups/jobs/<job_id>.json`, parse the JSON,
   and dispatch the backend command corresponding to the spec's `kind`:
   `RSYNC` → `rsync -aAXv --delete <sources> <destination>`,
   `RESTIC` → `restic backup <sources> --exclude <pat> ...`,
   `TAR` → `tar -czf <archive_path> <sources>` (with `--exclude` for each
   exclude pattern, and no `-z` when `compression=False`).
2. WHEN the persisted spec carries `excludes` THE SYSTEM SHALL forward each
   exclude pattern to the backend tool with the backend's exclude flag
   (`--exclude` for `rsync` and `restic`, `--exclude=<pat>` for `tar`).
3. WHEN the backend is `RESTIC` and the spec has an `encryption_passphrase`
   THE SYSTEM SHALL pass the passphrase through the runner's stdin channel via
   the `RESTIC_PASSWORD` environment-variable convention (the manager dispatches
   `restic ... --password-file=/dev/stdin` and forwards the passphrase on
   stdin) so the passphrase NEVER appears on argv.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any backend tool
   and SHALL return `DRY_RUN` with the would-have-been argv recorded on
   `command`.
5. WHEN the spec file does not exist on disk THE SYSTEM SHALL return `FAILED`
   with `detail="job not found"`.
6. WHEN the backend tool exits zero THE SYSTEM SHALL return `SUCCESS` with the
   runner's captured stdout, stderr, exit code, and argv.
7. WHEN the backend tool exits non-zero THE SYSTEM SHALL return `FAILED` with
   the runner's captured stderr available on the result.

---

### Requirement 4: List Persisted Backup Jobs

**User Story:** As a maintenance coordinator, I want to list every persisted
backup job so that audit reports can enumerate the host's backup footprint.

#### Acceptance Criteria

1. WHEN the coordinator requests the list THE SYSTEM SHALL invoke
   `ls -1 /var/lib/ccai/backups/jobs` through the runner with
   `elevated=False`.
2. THE SYSTEM SHALL filter the directory listing to entries whose suffix is
   `.json`, derive each `job_id` by stripping the suffix, and read each spec
   via `cat /var/lib/ccai/backups/jobs/<job_id>.json`.
3. THE SYSTEM SHALL parse each JSON spec and emit one `UbuntuBackupInfo` per
   spec, with `id`, `kind`, `created` (the on-disk file's mtime when
   available, otherwise `None`), `size_bytes` (`None` when not yet computed),
   `source_count` (the length of the `source_paths` list), and `destination`.
4. WHEN the job directory does not exist on disk THE SYSTEM SHALL return an
   empty list rather than raising.
5. WHEN any individual spec fails to parse THE SYSTEM SHALL skip that spec
   and continue with the rest; the parse failure SHALL NOT abort the listing.
6. WHEN `ls` exits non-zero with non-empty stderr THE SYSTEM SHALL raise a
   `UbuntuBackupQueryError` carrying the argv, exit code, and stderr.

---

### Requirement 5: Restore from a Backup

**User Story:** As a maintenance coordinator, I want to restore a previously-taken
backup into a target path so that a prior state can be recovered.

#### Acceptance Criteria

1. WHEN the coordinator supplies a `job_id` and a `target` path THE SYSTEM
   SHALL read the persisted spec, dispatch the restore command for the spec's
   `kind`:
   `RSYNC` → `rsync -aAXv --delete <destination> <target>` (reverse direction),
   `RESTIC` → `restic restore <snapshot_id> --target <target>`
   (with the spec's destination as the repository),
   `TAR` → `tar -xzf <archive_path> -C <target>`.
2. WHEN the backend is `RESTIC` and the spec has an `encryption_passphrase`
   THE SYSTEM SHALL pass the passphrase via stdin and `--password-file=/dev/stdin`
   so it never appears on argv.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any backend tool
   and SHALL return `DRY_RUN`.
4. WHEN the spec file does not exist on disk THE SYSTEM SHALL return `FAILED`
   with `detail="job not found"`.
5. WHEN the restore command exits non-zero THE SYSTEM SHALL return `FAILED`
   with the captured stdout, stderr, exit code, and argv.

---

### Requirement 6: Verify a Backup

**User Story:** As a maintenance coordinator, I want to verify that an archive
is intact so that corrupted archives can be detected before being relied on.

#### Acceptance Criteria

1. WHEN the coordinator supplies a `job_id` THE SYSTEM SHALL read the persisted
   spec and dispatch the verify command for the spec's `kind`:
   `RESTIC` → `restic check`,
   `TAR` → `tar -tzf <archive_path>` (or `tar -tf <archive_path>` when
   `compression=False`),
   `RSYNC` → best-effort `rsync -an --delete --checksum <destination> <destination>`
   which reports zero diff when the mirror is intact.
2. WHEN the backend is `RESTIC` and the spec has an `encryption_passphrase`
   THE SYSTEM SHALL pass the passphrase via stdin and the `--password-file=/dev/stdin`
   flag so it never appears on argv.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any backend tool.
4. WHEN the verify command exits zero THE SYSTEM SHALL return `SUCCESS`.
5. WHEN the verify command exits non-zero THE SYSTEM SHALL return `FAILED`
   with the captured stdout, stderr, exit code, and argv.

---

### Requirement 7: Encryption Passphrase Safety

**User Story:** As a maintenance coordinator, I want any passphrase used to
encrypt or decrypt a `RESTIC` repository to never appear on argv so that the
passphrase does not leak to process listings, audit logs, or aspect-level
persistence stores.

#### Acceptance Criteria

1. THE SYSTEM SHALL NEVER include `encryption_passphrase` as a positional or
   named argv token on any `restic` command.
2. THE SYSTEM SHALL deliver the passphrase via the runner's stdin channel and
   pass `--password-file=/dev/stdin` on argv so `restic` reads it from stdin.
3. THE SYSTEM SHALL also exclude the passphrase from the recorded `command`
   field on every returned `UbuntuBackupActionResult`.
4. THE SYSTEM SHALL exclude the passphrase from any persisted on-disk spec
   under `/var/lib/ccai/backups/jobs/`. The serialised spec MUST replace
   the passphrase with the sentinel string `"***REDACTED***"` before writing.

---

### Requirement 8: Backend Dispatch via `UbuntuBackupKind`

**User Story:** As a maintenance coordinator, I want to choose between three
backends (`RSYNC`, `RESTIC`, `TAR`) per job so that the host can use the right
tool for the right path tree without forcing a global default.

#### Acceptance Criteria

1. THE SYSTEM SHALL accept a `kind` field on every `UbuntuBackupJobSpec` of
   type `UbuntuBackupKind`.
2. WHEN `kind=RSYNC` THE SYSTEM SHALL route every backend operation
   (run / restore / verify) to the `rsync` flag set described in Requirements
   3, 5, and 6.
3. WHEN `kind=RESTIC` THE SYSTEM SHALL route every backend operation to the
   `restic` flag set described in Requirements 3, 5, and 6.
4. WHEN `kind=TAR` THE SYSTEM SHALL route every backend operation to the
   `tar` flag set described in Requirements 3, 5, and 6.
5. WHEN `kind` is none of `RSYNC`, `RESTIC`, or `TAR` THE SYSTEM SHALL return
   `FAILED` with `detail` describing the unsupported kind, and SHALL NOT
   invoke any backend command.

---

### Requirement 9: Schedule Field is Persisted but Not Wired

**User Story:** As a maintenance coordinator, I want the `schedule` field on a
backup spec to be persisted alongside the job so that the `scheduled-tasks`
feature can wire it up later without losing the operator's intent.

#### Acceptance Criteria

1. WHEN the coordinator supplies a `schedule` field on the spec THE SYSTEM
   SHALL serialise the field into the persisted JSON.
2. THE SYSTEM SHALL NOT itself create a cron entry, systemd-timer unit, or
   any other scheduling primitive. Schedule wiring is delegated to the
   `scheduled-tasks` feature.
3. THE SYSTEM SHALL document this division of responsibility prominently in
   the module docstring.

---

### Requirement 10: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want every mutating backup
operation to run with the privileges required by the underlying tools so that
archives capture the full permission and ownership set without quietly
truncating the snapshot.

#### Acceptance Criteria

1. WHEN the manager is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to every runner invocation in
   `create_backup_job`, `delete_backup_job`, `run_backup_now`,
   `restore_from_backup`, and `verify_backup`.
2. WHEN the manager is configured with `elevated=False` THE SYSTEM SHALL pass
   `elevated=False` for every runner invocation.
3. THE SYSTEM SHALL NEVER pass `elevated=True` for read verbs (`list_backups`),
   regardless of the `elevated` config value, except where the underlying
   `cat` of a `0600`-mode spec file requires it.

---

### Requirement 11: Idempotency Boundaries

**User Story:** As a maintenance coordinator, I want every write verb to
short-circuit when the desired state is already in place so that re-runs of
the same playbook are inexpensive and noise-free.

#### Acceptance Criteria

1. THE SYSTEM SHALL implement idempotency for `create_backup_job` by comparing
   the proposed serialised JSON to the on-disk spec; identical bytes return
   `SKIPPED`.
2. THE SYSTEM SHALL implement idempotency for `delete_backup_job` by skipping
   when the spec file is absent.
3. THE SYSTEM SHALL NOT implement idempotency on `run_backup_now`,
   `restore_from_backup`, or `verify_backup`. These verbs are operational and
   MUST always dispatch the runner (subject to dry-run).

---

### Requirement 12: Result Shape and Error Surface

**User Story:** As a maintenance coordinator, I want every action to return a
typed result so that audit-report generation can enumerate every action with a
predictable shape.

#### Acceptance Criteria

1. THE SYSTEM SHALL return a `UbuntuBackupActionResult` from every write verb,
   carrying `status`, `detail`, `exit_code`, `stdout`, `stderr`, `command`,
   `job_id`, and `target_path`.
2. THE SYSTEM SHALL raise `UbuntuBackupQueryError` from `list_backups` only
   when `ls` exits non-zero with non-empty stderr; an empty / missing job
   directory returns an empty list rather than raising.
3. THE SYSTEM SHALL never include the encryption passphrase or any other
   secret material on the `command` field.
