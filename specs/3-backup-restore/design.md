# Design

## Overview

The Ubuntu backup-restore feature is implemented as a single module,
`automojo.sysman.linux.ubuntu.backuprestore`, exposing a
`UbuntuBackupRestoreManager` class and its associated dataclasses and enum.
The manager wraps three backend tools — `rsync`, `restic`, and `tar` — behind
a single verb surface defined by the cross-OS contract at
`automojo-sysman-common/specs/contracts/backup-restore/contract.md`.

Ubuntu has no first-party backup framework analogous to Time Machine on
macOS or `wbadmin` on Windows. The standard operator experience is to assemble
backup workflows out of POSIX archive primitives (`tar`), file-tree mirrors
(`rsync`), or, when available, the deduplicating snapshot tool `restic`. Each
of those tools has a different command surface, a different repository layout,
and a different verification model. This module hides those differences behind
the `UbuntuBackupKind` enum: the caller selects a backend per job, and every
verb dispatches the right command for that backend without exposing the
divergence to the caller.

All system commands (`rsync`, `restic`, `tar`, `tee`, `cat`, `ls`, `rm`) are
executed through the injected `ICommandRunner`. The module never directly
calls `subprocess`, `os`, or `pathlib` write APIs; it constructs argv lists and
optional stdin payloads, then forwards them to the runner. This keeps the
business logic separate from execution and makes the feature fully testable
via mocks without root access or live `restic` / `tar` / `rsync` binaries.

The feature is strictly scoped to **file-tree backup and restore**. It covers
job-spec persistence, on-demand backup execution, list / verify / restore, and
encryption-passphrase safety. It does **not** cover block-level snapshots,
LVM snapshot rotation, or database-consistent dumps. Schedule wiring is
delegated to the `scheduled-tasks` feature; this feature persists the
`schedule` field on each spec but does not itself create cron / systemd-timer
entries.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`). Aspect
composition (persistence, retry, timeout) is transparent to the manager.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                      │
│  - Configures aspects (optional)                           │
│  - Injects runner into UbuntuBackupRestoreManager          │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│         UbuntuBackupRestoreManager                          │
│  automojo.sysman.linux.ubuntu.backuprestore                 │
│                                                             │
│  create_backup_job()      run_backup_now()                  │
│  delete_backup_job()      restore_from_backup()             │
│  list_backups()           verify_backup()                   │
│                          get_job_status()                   │
└──────────┬──────────────────────────────────────────────────┘
           │ delegates to
           ▼
┌────────────────────────────┐
│      ICommandRunner        │
│  run(args, elevated, ...)  │
│  → RunResult               │
└────────────┬───────────────┘
             │
     ┌───────┴────────┐
     ▼                ▼
┌─────────────┐  ┌─────────────┐
│ LocalRunner │  │ SSHRunner   │
│ subprocess  │  │ SSH channel │
│ sudo elev.  │  │ reliability │
└──────┬──────┘  └──────┬──────┘
       │                │
       └────────┬───────┘
                │ optional
                ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  PersistenceAspect, RetryAspect, TimeoutAspect              │
└─────────────────────────────────────────────────────────────┘
```

The manager NEVER constructs a subprocess directly and NEVER reaches into
`pathlib`, `os.makedirs`, or `os.remove` to mutate the filesystem. Every
filesystem mutation is mediated through `tee`, `rm`, or the backend tool
itself.

---

## Verb / Tool Mapping

The Ubuntu manager dispatches verbs to backend tools as follows. Every verb
is dispatched through the same `ICommandRunner` so the manager works
identically under local and SSH execution.

| Verb                  | RSYNC backend                              | RESTIC backend                                   | TAR backend                              |
|-----------------------|--------------------------------------------|--------------------------------------------------|------------------------------------------|
| `create_backup_job`   | `tee /var/lib/automojo/backups/jobs/<id>.json` | `tee /var/lib/automojo/backups/jobs/<id>.json` | `tee /var/lib/automojo/backups/jobs/<id>.json` |
| `delete_backup_job`   | `rm -f /var/lib/automojo/backups/jobs/<id>.json` | `rm -f /var/lib/automojo/backups/jobs/<id>.json` | `rm -f /var/lib/automojo/backups/jobs/<id>.json` |
| `run_backup_now`      | `rsync -aAXv --delete <src> <dst>`         | `restic backup <paths> --exclude <pat>`          | `tar -czf <archive> <paths>`             |
| `list_backups`        | `ls -1 /var/lib/automojo/backups/jobs` + `cat <spec>` | same                                  | same                                     |
| `restore_from_backup` | `rsync -aAXv --delete <dst> <target>`      | `restic restore <id> --target <target>`          | `tar -xzf <archive> -C <target>`         |
| `verify_backup`       | best-effort `rsync -an --checksum`         | `restic check`                                   | `tar -tzf <archive>`                     |
| `get_job_status`      | read spec; report `last_run_status`        | same                                             | same                                     |

The `tee` / `rm` / `cat` / `ls` invocations all run through the same
`ICommandRunner`. There is no `IFileOperator` dependency; persistence is
managed via the same runner that dispatches the backend tools.

---

## Backend Dispatch

The dispatch decision is made in three places: `run_backup_now`,
`restore_from_backup`, and `verify_backup`. Each verb resolves the spec from
disk (via `cat`), parses the persisted JSON, and chooses the backend by the
spec's `kind` field. The dispatch is implemented as a small `if / elif`
ladder rather than a registry, in line with this codebase's "flat is better
than nested" convention.

For the `RSYNC` backend, the run direction is forward (sources → destination);
the restore direction is reverse (destination → target). For the `RESTIC`
backend, the destination field is treated as the repository path and the
backend is invoked with `--repo <destination>`. For the `TAR` backend, the
destination field is treated as the archive file path; `run_backup_now`
creates it, `restore_from_backup` reads it.

---

## Encryption Passphrase Handling

The contract requires that any encryption material be delivered via the
runner's stdin channel and never appear on argv. For Ubuntu, only the
`RESTIC` backend natively supports encryption (each repository has a single
master passphrase). The dispatch path:

1. The manager builds the argv with `--password-file=/dev/stdin` appended.
2. The manager calls `runner.run(argv, elevated=True, stdin=passphrase)`.
3. `restic` reads the passphrase from stdin via the `/dev/stdin`
   pseudo-device.
4. The recorded `command` field on the returned `UbuntuBackupActionResult`
   is the argv as given — the passphrase is never on argv, so there is
   nothing to redact.
5. The persisted on-disk spec, however, MUST NOT carry the plaintext
   passphrase. The serialiser substitutes the sentinel string
   `"***REDACTED***"` before writing.

For the `TAR` and `RSYNC` backends, encryption is out of scope; the
`encryption_passphrase` field is ignored and the spec is persisted as-is
(with the redaction sentinel substituted regardless of backend, so a spec
that gets re-read does not expose the value).

---

## Job Persistence

Backup-job specs are persisted as JSON files at
`/var/lib/automojo/backups/jobs/<job_id>.json`. The on-disk schema is the
JSON serialisation of `UbuntuBackupJobSpec`, with one transformation: the
`encryption_passphrase` field is replaced by the sentinel `"***REDACTED***"`
before serialisation.

Idempotency for `create_backup_job` is implemented by reading the existing
spec via `cat`, comparing its content byte-for-byte to the proposed
serialisation, and returning `SKIPPED` when they match. The comparison is
done after the same redaction transformation so that a re-create of an
existing job — with the same passphrase — does not unnecessarily re-write
the file.

The job directory itself is created lazily by the first `create_backup_job`
call: `tee` invoked on a path under a non-existent directory will fail.
The manager does NOT pre-create the directory; instead, it documents that
operators MUST `mkdir -p /var/lib/automojo/backups/jobs` once at host
provisioning time. Pre-creating the directory is the responsibility of the
package's installation manifest, not the runtime manager.

---

## Module Layout

```
backuprestore.py
├── module-level constants
│   ├── _RSYNC, _RESTIC, _TAR, _TEE, _CAT, _RM, _LS
│   └── _DEFAULT_JOB_DIR
├── enums
│   └── UbuntuBackupKind (RSYNC, RESTIC, TAR)
├── data classes
│   ├── UbuntuBackupManagerConfig (extends FeatureConfig)
│   ├── UbuntuBackupActionResult  (extends FeatureActionResult)
│   ├── UbuntuBackupJobSpec       (alias UbuntuBackupSpec)
│   ├── UbuntuBackupInfo
│   ├── UbuntuBackupVerifyResult
│   └── UbuntuBackupJobStatus
├── exceptions
│   ├── UbuntuBackupQueryError
│   ├── UbuntuBackupNotFoundError
│   └── UbuntuBackupVerifyError
├── manager
│   └── UbuntuBackupRestoreManager
│       ├── create_backup_job
│       ├── delete_backup_job
│       ├── run_backup_now
│       ├── list_backups
│       ├── restore_from_backup
│       ├── verify_backup
│       └── get_job_status
└── module helpers
    ├── _serialise_spec
    ├── _deserialise_spec
    ├── _redact_passphrase
    ├── _build_rsync_argv
    ├── _build_restic_argv
    ├── _build_tar_argv
    └── _job_path
```

---

## Backup-Kind Mapping

The cross-OS contract lists four abstract `BackupKind` values: `ARCHIVE`,
`MIRROR`, `TIME_MACHINE`, `SYSTEM_IMAGE`. The Ubuntu manager re-frames the
kind axis as a backend-tool axis (`UbuntuBackupKind = RSYNC | RESTIC | TAR`)
because in practice the operator chooses the tool, not the abstract kind.
The mapping back to the contract is:

| Abstract `BackupKind` | Ubuntu `UbuntuBackupKind` | Notes                                  |
|-----------------------|---------------------------|----------------------------------------|
| `ARCHIVE`             | `TAR`                     | Single-archive `tar.gz` snapshot.      |
| `MIRROR`              | `RSYNC`                   | `rsync -aAXv --delete` mirror.         |
| (deduplicating archive) | `RESTIC`                | Restic deduplicating snapshot store.   |
| `TIME_MACHINE`        | not supported             | macOS-only.                            |
| `SYSTEM_IMAGE`        | not supported             | Windows-only.                          |

Callers MUST select one of the three Ubuntu kinds; passing an unmapped value
returns `FAILED` with a descriptive `detail`.

---

## Idempotency Decision Table

| Verb                  | Idempotent? | Skip condition                                         |
|-----------------------|-------------|--------------------------------------------------------|
| `create_backup_job`   | yes         | spec on disk byte-equivalent to proposed JSON          |
| `delete_backup_job`   | yes         | spec file absent                                       |
| `run_backup_now`      | no          | always dispatches                                      |
| `restore_from_backup` | no          | always dispatches                                      |
| `verify_backup`       | no          | always dispatches                                      |
| `list_backups`        | n/a         | read verb                                              |
| `get_job_status`      | n/a         | read verb                                              |

---

## Elevation Decision Table

| Verb                  | `elevated=` argument forwarded   |
|-----------------------|----------------------------------|
| `create_backup_job`   | `self._config.elevated`          |
| `delete_backup_job`   | `self._config.elevated`          |
| `run_backup_now`      | `self._config.elevated`          |
| `restore_from_backup` | `self._config.elevated`          |
| `verify_backup`       | `self._config.elevated`          |
| `list_backups`        | always `False`                   |
| `get_job_status`      | always `False`                   |

The `cat` and `ls` invocations inside `list_backups` and `get_job_status` use
`elevated=False` unconditionally — the contract requires reads not to
elevate, and the spec files are world-readable by convention. If a deployment
hardens the spec directory to `0700`, the operator must adjust the directory
permissions or run the manager under an account that has read access.

---

## Error Surface

`UbuntuBackupQueryError` is raised only by `list_backups` and `get_job_status`,
and only when the underlying `ls` / `cat` exits non-zero with non-empty
stderr. An empty or missing job directory returns an empty list rather than
raising. A spec file that fails to parse is skipped from the listing — the
parse failure does not abort the entire enumeration.

`UbuntuBackupNotFoundError` is reserved for the case where a `job_id` or
`backup_id` is supplied to `restore_from_backup`, `verify_backup`, or
`get_job_status` and the spec does not exist on disk. Write verbs map this
condition to a `FAILED` result with `detail="job not found"` rather than
raising, because the contract treats the verbs as result-returning rather
than exception-raising.

`UbuntuBackupVerifyError` is reserved for structural errors during
verification (e.g. an archive header that cannot be parsed at all).

---

## Test Strategy

The test suite at
`source/testroots/testbase/automojo/tests/sysman/linux/ubuntu/test_backuprestore.py`
covers, at minimum, the following surface:

* Command construction: one test per write verb per backend.
* Idempotency: `create_backup_job` returns `SKIPPED` on matching state;
  `delete_backup_job` returns `SKIPPED` when absent.
* Dry-run: every write verb returns `DRY_RUN` without invoking the runner.
* `list_backups` parsing: a seeded `ls` + `cat` pair drives a list result.
* Verify-before-restore is NOT enforced by Ubuntu (the contract permits it
  but does not require it); the test surface enumerates the verify command
  shape per backend instead.
* Credential safety: a `RESTIC` job with a passphrase produces an argv
  that does NOT contain the passphrase but DOES contain
  `--password-file=/dev/stdin`, and the runner sees the passphrase on
  stdin.
* Elevation: writes pass `elevated=True` and reads do not, when configured.
* Failure: the runner's stderr surfaces unchanged on `FAILED` results.

The tests use a `_MockCommandRunner` that records every `run` call and
returns pre-seeded `RunResult` values keyed by argv tuple, mirroring the
pattern used by `test_dnsservermanagement.py` and `test_sambaservermanagement.py`.

---

## Cross-OS Concept Mapping

| Concept                | Ubuntu                                    | macOS                                         | Windows                                                |
|------------------------|-------------------------------------------|-----------------------------------------------|--------------------------------------------------------|
| Default backup tool    | `rsync` (selected via enum)               | Time Machine via `tmutil`                     | `wbadmin` (Windows Server) / Robocopy (client SKUs)    |
| Job persistence        | JSON spec at `/var/lib/automojo/backups/` | JSON spec at `/var/db/automojo/backups/`      | JSON spec at `%ProgramData%\Automojo\backup\`          |
| Repository layout      | tool-specific (rsync mirror / restic repo / tar archive) | TM volume + listing                | `wbadmin` catalogue on a backup volume                 |
| Encryption native?     | only for `restic`                         | Time Machine encrypts at the volume layer     | `wbadmin` does not encrypt; relies on volume-level     |
| Snapshot model         | none (full mirror or deduplicating store) | block-level (HFS+/APFS hard-link snapshot)    | block-level VHDX                                       |
| Schedule wiring        | delegated to `scheduled-tasks`            | partly built in via `tmutil setdestination`   | partly built in via Task Scheduler                     |

The fact that Ubuntu has no native backup daemon is the dominant divergence:
on Ubuntu, the operator picks a tool; on macOS, the OS provides one; on
Windows, the OS provides one but only on Server SKUs. Each spec documents
its own deviation from the cross-OS contract.

---

## Aspect Composition

The manager is safe under any aspect composition on the runner:

* `RetryAspect` may transparently flip a `FAILED` result to `SUCCESS` on
  retry. The manager's result-mapping logic does not care.
* `PersistenceAspect` MUST NOT log the stdin payload from a `RESTIC` run
  (it is the encryption passphrase). The aspect is responsible for the
  redaction; the manager's job is to never put the passphrase on argv.
* `TimeoutAspect` may abort a long `restic check` and turn it into a
  `FAILED` result. That is acceptable: `verify_backup` is not idempotent
  and a re-run is the operator's responsibility.

---

## Out of Scope

* Block-level / LVM / Btrfs snapshots.
* Database-consistent dumps (PostgreSQL, MSSQL).
* Cross-host backup replication.
* Cloud-storage destinations (S3, GCS) beyond a mounted local-looking path.
* Bare-metal recovery ISO generation.
* Verify-before-restore enforcement (permitted by the contract but
  intentionally not implemented for Ubuntu — the operator chooses to call
  `verify_backup` manually before `restore_from_backup`).
