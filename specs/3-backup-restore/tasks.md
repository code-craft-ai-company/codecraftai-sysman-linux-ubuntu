# Tasks

The following implementation tasks are derived from the requirements and design
documents. Tasks are listed in suggested execution order. Each task is bounded
to a single file and a single concern.

---

## Module Skeleton

- [ ] T1. Create `source/packages/ccai/sysman/linux/ubuntu/backuprestore.py`
      with the module docstring describing the verb-to-tool mapping, the
      backend dispatch table, and the schedule-delegation note.
- [ ] T2. Add the module-level constants for the backend tool names
      (`_RSYNC`, `_RESTIC`, `_TAR`), the helper tools (`_TEE`, `_CAT`, `_RM`,
      `_LS`, `_TEST`), and the default job directory path
      (`_DEFAULT_JOB_DIR = "/var/lib/ccai/backups/jobs"`).
- [ ] T3. Add the `UbuntuBackupActionStatus = ActionStatus` re-export so
      callers can refer to a feature-prefixed status without importing
      `featurebase`.

---

## Data Models

- [ ] T4. Define the `UbuntuBackupKind` enum with values `RSYNC`, `RESTIC`,
      `TAR`. Document each value's backend tool in the docstring.
- [ ] T5. Define the `UbuntuBackupManagerConfig` dataclass extending
      `FeatureConfig`, with fields `default_kind: UbuntuBackupKind = RSYNC`
      and `job_dir: str = _DEFAULT_JOB_DIR`.
- [ ] T6. Define the `UbuntuBackupActionResult` dataclass extending
      `FeatureActionResult`, adding `job_id: Optional[str]` and
      `target_path: Optional[str]`.
- [ ] T7. Define the `UbuntuBackupJobSpec` dataclass with fields `id`,
      `kind`, `source_paths`, `destination`, `excludes`, `compression`,
      `encryption_passphrase`, `retention_days`, `schedule`. Add an alias
      `UbuntuBackupSpec = UbuntuBackupJobSpec` for cross-spec compatibility.
- [ ] T8. Define the `UbuntuBackupInfo` dataclass with fields `id`, `kind`,
      `created`, `size_bytes`, `source_count`, `destination`.
- [ ] T9. Define the `UbuntuBackupVerifyResult` and
      `UbuntuBackupJobStatus` dataclasses per the contract.
- [ ] T10. Define the `UbuntuBackupQueryError`,
      `UbuntuBackupNotFoundError`, and `UbuntuBackupVerifyError` exception
      classes. Each carries `command`, `exit_code`, `stderr` plus a
      multi-line error message built per the codebase convention.

---

## Module Helpers

- [ ] T11. Implement `_serialise_spec(spec)` returning a JSON string with
      passphrase redacted via `_redact_passphrase`.
- [ ] T12. Implement `_deserialise_spec(text)` returning an
      `UbuntuBackupJobSpec` (with the passphrase set to `None` when the
      sentinel is encountered).
- [ ] T13. Implement `_redact_passphrase(spec)` returning a copy of the
      spec with `encryption_passphrase` replaced by `"***REDACTED***"`
      (or `None` when the original was `None`).
- [ ] T14. Implement `_build_rsync_argv(spec, *, mode)` for the run /
      restore / verify directions.
- [ ] T15. Implement `_build_restic_argv(spec, *, mode)` returning argv
      and a `bool` indicating whether the runner stdin must carry the
      passphrase.
- [ ] T16. Implement `_build_tar_argv(spec, *, mode)` for the
      run / restore / verify directions.
- [ ] T17. Implement `_job_path(*, job_dir, job_id)` returning the
      absolute path to the persisted spec JSON.

---

## Manager Class

- [ ] T18. Implement `UbuntuBackupRestoreManager.__init__`, defaulting the
      config when none is passed.
- [ ] T19. Implement `create_backup_job(spec)` per Requirements 1, 7, 11.
- [ ] T20. Implement `delete_backup_job(job_id)` per Requirements 2, 11.
- [ ] T21. Implement `run_backup_now(job_id)` per Requirements 3, 7, 8.
- [ ] T22. Implement `list_backups()` per Requirement 4.
- [ ] T23. Implement `restore_from_backup(job_id, target)` per Requirements
      5, 7, 8.
- [ ] T24. Implement `verify_backup(job_id)` per Requirements 6, 7, 8.
- [ ] T25. Implement `get_job_status(job_id)` returning a
      `UbuntuBackupJobStatus` parsed from the persisted spec; for now
      always reports `enabled=True` with `last_run_status=None`. The
      `scheduled-tasks` integration will fill in the runtime fields in
      a future wave.
- [ ] T26. Implement the helper methods `_typed_config`, `_read_spec`,
      `_path_exists`, `_build_result`, `_wrap_failure`, and
      `_wrap_run_result` per the pattern in `dnsservermanagement.py`.

---

## Tests

- [ ] T27. Create
      `source/testroots/testbase/ccai/tests/sysman/linux/ubuntu/test_backuprestore.py`
      with a `_MockCommandRunner` matching the pattern in
      `test_dnsservermanagement.py`.
- [ ] T28. `test_create_backup_job_persists_spec` — drives the happy
      path; asserts a `tee` call to the spec path with the JSON on stdin
      and `elevated=True`.
- [ ] T29. `test_create_backup_job_skipped_when_present` — seeds the
      `cat` of an existing spec to match the proposed JSON; asserts
      `SKIPPED`.
- [ ] T30. `test_create_backup_job_dry_run` — asserts the runner is
      not invoked; result `status=DRY_RUN`.
- [ ] T31. `test_delete_backup_job_invokes_remove` — asserts an `rm -f`
      call with `elevated=True`.
- [ ] T32. `test_run_backup_now_dispatches_rsync` — asserts the `rsync
      -aAXv --delete <src> <dst>` argv shape.
- [ ] T33. `test_run_backup_now_dispatches_restic` — asserts the
      `restic backup` argv shape; passphrase rides on stdin.
- [ ] T34. `test_run_backup_now_dispatches_tar` — asserts the
      `tar -czf` argv shape.
- [ ] T35. `test_list_backups_parses_ls_output` — seeds a fake
      `ls` + `cat` and asserts the parsed `UbuntuBackupInfo` rows.
- [ ] T36. `test_restore_from_backup_invokes_rsync` /
      `..._restic` / `..._tar` — one per backend.
- [ ] T37. `test_verify_backup_invokes_restic_check` /
      `..._tar_tzf` — one per backend.
- [ ] T38. `test_writes_elevate` — asserts `elevated=True` on every
      write-path call.
- [ ] T39. `test_reads_never_elevate` — asserts `elevated=False` on
      every read-path call.
- [ ] T40. `test_failed_command_surfaces_stderr` — seeds a non-zero
      `RunResult` on a backend run; asserts `FAILED` with the captured
      stderr on the result.
- [ ] T41. `test_passphrase_redacted_when_restic_encrypted` — asserts
      the recorded `command` does NOT contain the passphrase, and that
      the persisted JSON written via `tee` carries the redaction
      sentinel.

---

## Documentation

- [ ] T42. Update the package-level `Agents.md` entry for
      `codecraftai-sysman-linux-ubuntu` to point at the
      `specs/3-backup-restore/` directory.
- [ ] T43. Note in the manager's module docstring that schedule wiring
      is delegated to `scheduled-tasks` and that the
      `encryption_passphrase` field is redacted from on-disk spec
      files.
