"""
    Ubuntu backup-restore feature.

    Implements the cross-OS backup-restore contract for Ubuntu by
    wrapping the three POSIX archive / synchronisation tools
    available on a stock Ubuntu host: ``rsync``, ``restic``, and
    ``tar``.  Ubuntu has no first-party backup framework analogous
    to macOS Time Machine or Windows ``wbadmin``; the operator
    chooses the backend per job via the :class:`UbuntuBackupKind`
    enum, and every verb dispatches the right command for the
    selected backend.

    Verb / backend mapping:

    +-----------------------+-------------------------------+--------------------------------------------+--------------------------------------+
    | Verb                  | RSYNC                         | RESTIC                                     | TAR                                  |
    +=======================+===============================+============================================+======================================+
    | ``create_backup_job`` | ``tee <job-path>`` (stdin)    | ``tee <job-path>`` (stdin)                 | ``tee <job-path>`` (stdin)           |
    +-----------------------+-------------------------------+--------------------------------------------+--------------------------------------+
    | ``delete_backup_job`` | ``rm -f <job-path>``          | ``rm -f <job-path>``                       | ``rm -f <job-path>``                 |
    +-----------------------+-------------------------------+--------------------------------------------+--------------------------------------+
    | ``run_backup_now``    | ``rsync -aAXv --delete``      | ``restic backup --password-file=/dev/stdin`` | ``tar -czf <archive> <paths>``     |
    +-----------------------+-------------------------------+--------------------------------------------+--------------------------------------+
    | ``list_backups``      | ``ls -1`` + ``cat`` per spec  | same                                       | same                                 |
    +-----------------------+-------------------------------+--------------------------------------------+--------------------------------------+
    | ``restore_from_backup``| ``rsync -aAXv --delete``     | ``restic restore <id> --target <dst>``     | ``tar -xzf <archive> -C <target>``  |
    +-----------------------+-------------------------------+--------------------------------------------+--------------------------------------+
    | ``verify_backup``     | best-effort ``rsync --checksum``| ``restic check``                         | ``tar -tzf <archive>``               |
    +-----------------------+-------------------------------+--------------------------------------------+--------------------------------------+

    Schedule wiring:
        This feature persists the ``schedule`` field on each spec
        but does NOT itself create cron entries, systemd-timer
        units, or any other scheduling primitive.  Schedule
        wiring is delegated to the ``scheduled-tasks`` feature so
        the host has a single source of truth for periodic jobs.

    Encryption passphrase handling:
        When the spec carries an ``encryption_passphrase`` and
        the backend is ``RESTIC``, the passphrase is delivered via
        the runner's stdin channel and the argv carries
        ``--password-file=/dev/stdin`` so the secret never lands
        in process listings or audit logs.  The persisted on-disk
        spec replaces the passphrase with the sentinel string
        ``"***REDACTED***"`` so the secret never lands on disk
        either.

    Idempotency:
        * ``create_backup_job``: returns ``SKIPPED`` when the
          on-disk JSON spec is byte-equivalent to the proposed
          serialisation.
        * ``delete_backup_job``: returns ``SKIPPED`` when the
          spec file is absent.
        * ``run_backup_now``, ``restore_from_backup``,
          ``verify_backup``: never skip.

    Elevation policy:
        Write verbs (``create_backup_job``, ``delete_backup_job``,
        ``run_backup_now``, ``restore_from_backup``,
        ``verify_backup``) honour ``config.elevated``.  Reads
        (``list_backups``, ``get_job_status``) never elevate.

    File I/O policy:
        Reads use ``cat`` and ``ls``; writes use ``tee`` with the
        new content on stdin; deletions use ``rm``.  This module
        does NOT consume an :class:`IFileOperator`.
"""

import json
import os

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_RSYNC = "rsync"
_RESTIC = "restic"
_TAR = "tar"
_TEE = "tee"
_CAT = "cat"
_RM = "rm"
_LS = "ls"
_TEST = "test"

_DEFAULT_JOB_DIR = "/var/lib/automojo/backups/jobs"

_REDACTION_SENTINEL = "***REDACTED***"

_MODE_RUN = "run"
_MODE_RESTORE = "restore"
_MODE_VERIFY = "verify"


UbuntuBackupActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Enum
# ----------------------------------------------------------------------


class UbuntuBackupKind(Enum):
    """
        Backend selector for an :class:`UbuntuBackupJobSpec`.

        +-----------+--------------------------------------------+
        | Value     | Backend                                    |
        +===========+============================================+
        | ``RSYNC`` | ``rsync -aAXv --delete`` mirror            |
        +-----------+--------------------------------------------+
        | ``RESTIC``| ``restic`` deduplicating snapshot store    |
        +-----------+--------------------------------------------+
        | ``TAR``   | ``tar -czf`` single-archive snapshot       |
        +-----------+--------------------------------------------+
    """
    RSYNC = "rsync"
    RESTIC = "restic"
    TAR = "tar"


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class UbuntuBackupManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuBackupRestoreManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param default_kind: Backend selected when an
                             :class:`UbuntuBackupJobSpec` does not
                             pin its own ``kind``.  Defaults to
                             :attr:`UbuntuBackupKind.RSYNC`.
        :param job_dir: Directory holding persisted job specs.
                        Defaults to
                        ``/var/lib/automojo/backups/jobs``.
    """
    default_kind: UbuntuBackupKind = UbuntuBackupKind.RSYNC
    job_dir: str = _DEFAULT_JOB_DIR


@dataclass
class UbuntuBackupActionResult(FeatureActionResult):
    """
        Result of an :class:`UbuntuBackupRestoreManager` write
        verb.

        :param job_id: The job identifier the action targeted,
                       when applicable.
        :param target_path: The on-disk target the action
                            produced (archive path / spec path /
                            restore root), when applicable.
    """
    job_id: Optional[str] = None
    target_path: Optional[str] = None


@dataclass
class UbuntuBackupJobSpec:
    """
        Declarative specification for a single backup job.

        :param id: Caller-chosen deterministic job identifier.
                   Used as the on-disk spec filename and as the
                   ``backup_id`` for ``restore_from_backup`` /
                   ``verify_backup``.
        :param kind: The backend to dispatch.
        :param source_paths: Filesystem paths to back up.
        :param destination: Repository root / archive path
                            interpreted per backend (rsync target
                            directory, restic repository, tar
                            archive file).
        :param excludes: Optional list of exclude patterns
                         forwarded to the backend's exclude flag.
        :param compression: When ``True`` (default) the ``TAR``
                            backend emits a gzip-compressed
                            archive (``-czf``); when ``False`` it
                            emits an uncompressed archive
                            (``-cf``).  Ignored by the ``RSYNC``
                            and ``RESTIC`` backends.
        :param encryption_passphrase: When set, used as the
                                      ``RESTIC`` repository
                                      passphrase.  Delivered via
                                      runner stdin so it never
                                      appears on argv; redacted
                                      from the persisted on-disk
                                      spec.
        :param retention_days: Optional retention horizon (in
                               days).  Persisted on the spec but
                               not enforced by this feature; the
                               ``scheduled-tasks`` feature is
                               responsible for prune dispatch.
        :param schedule: Optional schedule expression.  Persisted
                         but not wired by this feature; see the
                         module docstring.
    """
    id: str
    kind: UbuntuBackupKind
    source_paths: list
    destination: str
    excludes: Optional[list] = None
    compression: bool = True
    encryption_passphrase: Optional[str] = None
    retention_days: Optional[int] = None
    schedule: Optional[str] = None


# Alias for cross-spec compatibility — the contract calls this
# ``BackupJobSpec`` but earlier specs in this codebase used
# ``BackupSpec``.
UbuntuBackupSpec = UbuntuBackupJobSpec


@dataclass
class UbuntuBackupInfo:
    """
        One row from
        :meth:`UbuntuBackupRestoreManager.list_backups`.

        :param id: Job identifier.
        :param kind: Backend kind.
        :param created: ISO-8601 timestamp of the spec file's
                        creation, when known; ``None`` otherwise.
        :param size_bytes: On-disk size in bytes when known;
                           ``None`` when not.
        :param source_count: Length of the ``source_paths`` list.
        :param destination: Repository root / archive path from
                            the persisted spec.
    """
    id: str
    kind: UbuntuBackupKind
    created: Optional[str]
    size_bytes: Optional[int]
    source_count: Optional[int]
    destination: str


@dataclass
class UbuntuBackupVerifyResult:
    """
        Result of :meth:`UbuntuBackupRestoreManager.verify_backup`.

        :param backup_id: The archive identifier verified.
        :param ok: True when verification passed.
        :param recorded_checksum: Recorded checksum, when known.
        :param computed_checksum: Computed checksum, when known.
        :param detail: Human-readable detail on failure.
    """
    backup_id: str
    ok: bool
    recorded_checksum: Optional[str] = None
    computed_checksum: Optional[str] = None
    detail: Optional[str] = None


@dataclass
class UbuntuBackupJobStatus:
    """
        Result of :meth:`UbuntuBackupRestoreManager.get_job_status`.

        :param job_id: The job identifier.
        :param kind: Backend kind.
        :param enabled: True when the spec is on disk (the
                        manager treats persistence as enablement;
                        wiring belongs to ``scheduled-tasks``).
        :param last_run_at: Reserved for future runtime tracking.
        :param last_run_status: Reserved for future runtime
                                tracking.
        :param next_run_at: Reserved for future runtime tracking.
    """
    job_id: str
    kind: UbuntuBackupKind
    enabled: bool
    last_run_at: Optional[str] = None
    last_run_status: Optional[UbuntuBackupActionStatus] = None
    next_run_at: Optional[str] = None


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class UbuntuBackupQueryError(Exception):
    """
        Raised when a backup-restore read fails for a reason
        other than the target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list,
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Backup-restore query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuBackupNotFoundError(Exception):
    """
        Raised when a ``job_id`` or ``backup_id`` is supplied to a
        read verb and the spec does not exist on disk.
    """

    def __init__(self, *, job_id: str) -> None:
        """
            :param job_id: The missing job identifier.
        """
        self.job_id = job_id

        err_msg_lines = ["Backup job not found."]
        err_msg_lines.append("    JOB_ID: {}".format(job_id))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuBackupVerifyError(Exception):
    """
        Raised when verification logic encounters a structural
        error distinct from a mismatched checksum (which returns
        ``ok=False`` on the result instead).
    """

    def __init__(self, *, message: str) -> None:
        """
            :param message: A human-readable description of the
                            structural failure.
        """
        super().__init__(message)
        return


# ----------------------------------------------------------------------
# UbuntuBackupRestoreManager
# ----------------------------------------------------------------------


class UbuntuBackupRestoreManager(BaseFeatureManager):
    """
        Ubuntu backup-restore manager.

        Every verb is dispatched through the injected
        :class:`ICommandRunner`.  File I/O is performed via
        ``cat`` / ``tee`` / ``rm`` rather than a separate file
        operator.

        See the module docstring for the full verb-to-tool table
        and the schedule-delegation policy.
    """

    result_class = UbuntuBackupActionResult
    status_class = UbuntuBackupActionStatus
    feature_name = "backup-restore"

    def __init__(
        self,
        runner,
        config: Optional[UbuntuBackupManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every
                           dispatch.
            :param config: Manager configuration.  Defaults to a
                           fresh
                           :class:`UbuntuBackupManagerConfig`
                           when omitted.
        """
        if config is None:
            config = UbuntuBackupManagerConfig()

        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def create_backup_job(
        self,
        spec: UbuntuBackupJobSpec,
    ) -> UbuntuBackupActionResult:
        """
            Persist a backup-job spec to disk.

            Serialises ``spec`` to JSON (with the encryption
            passphrase replaced by the redaction sentinel) and
            writes it to ``<job_dir>/<spec.id>.json`` via ``tee``.

            Idempotent: when the on-disk spec is byte-equivalent
            to the proposed serialisation, returns ``SKIPPED``.

            :param spec: The :class:`UbuntuBackupJobSpec`.

            :returns: The :class:`UbuntuBackupActionResult`.
        """
        cfg = self._typed_config()
        job_path = _job_path(job_dir=cfg.job_dir, job_id=spec.id)
        new_payload = _serialise_spec(spec=spec)
        write_command = [_TEE, job_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuBackupActionStatus.DRY_RUN,
                job_id=spec.id,
                target_path=job_path,
                command=write_command,
            )
            return dry

        existing = self._read_spec_text(path=job_path)
        if existing is not None and existing.strip() == new_payload.strip():
            skipped = self._build_result(
                status=UbuntuBackupActionStatus.SKIPPED,
                job_id=spec.id,
                target_path=job_path,
                command=write_command,
                detail="spec already persisted",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_payload,
        )
        rtnval = self._wrap_run_result(
            run_result=run_result,
            command=write_command,
            job_id=spec.id,
            target_path=job_path,
        )
        return rtnval

    def delete_backup_job(
        self,
        job_id: str,
    ) -> UbuntuBackupActionResult:
        """
            Remove a persisted backup-job spec.

            Idempotent: when the spec file is absent, returns
            ``SKIPPED``.

            :param job_id: The job identifier to remove.

            :returns: The :class:`UbuntuBackupActionResult`.
        """
        cfg = self._typed_config()
        job_path = _job_path(job_dir=cfg.job_dir, job_id=job_id)
        rm_command = [_RM, "-f", job_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuBackupActionStatus.DRY_RUN,
                job_id=job_id,
                target_path=job_path,
                command=rm_command,
            )
            return dry

        present = self._path_exists(path=job_path)
        if not present:
            skipped = self._build_result(
                status=UbuntuBackupActionStatus.SKIPPED,
                job_id=job_id,
                target_path=job_path,
                command=rm_command,
                detail="spec already absent",
            )
            return skipped

        run_result = self._runner.run(
            list(rm_command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_run_result(
            run_result=run_result,
            command=rm_command,
            job_id=job_id,
            target_path=job_path,
        )
        return rtnval

    def run_backup_now(
        self,
        job_id: str,
    ) -> UbuntuBackupActionResult:
        """
            Dispatch a backup run for an already-persisted job.

            Reads the spec via ``cat``, parses the JSON, and
            dispatches the backend command corresponding to the
            spec's ``kind``.  When the backend is ``RESTIC`` and
            the spec carries an ``encryption_passphrase``, the
            passphrase is forwarded via runner stdin and the argv
            carries ``--password-file=/dev/stdin``.

            Operational; never skips.

            :param job_id: The job identifier to run.

            :returns: The :class:`UbuntuBackupActionResult`.
        """
        rtnval = self._dispatch_backend_verb(
            job_id=job_id,
            mode=_MODE_RUN,
            target=None,
        )
        return rtnval

    def restore_from_backup(
        self,
        job_id: str,
        target: str,
    ) -> UbuntuBackupActionResult:
        """
            Restore from a previously-taken backup into ``target``.

            Reads the spec, dispatches the restore command for
            the spec's ``kind``.  RESTIC restores are written
            into ``target`` (the spec's ``destination`` becomes
            the ``--repo``); RSYNC restores reverse the
            sources / destination so ``destination`` is read and
            ``target`` is written; TAR restores extract the
            archive into ``target``.

            Operational; never skips.

            :param job_id: The job identifier to restore from.
            :param target: The on-disk target path / restore
                           root.

            :returns: The :class:`UbuntuBackupActionResult`.
        """
        rtnval = self._dispatch_backend_verb(
            job_id=job_id,
            mode=_MODE_RESTORE,
            target=target,
        )
        return rtnval

    def verify_backup(
        self,
        job_id: str,
    ) -> UbuntuBackupActionResult:
        """
            Verify a backup's on-disk integrity.

            For ``RESTIC`` backends, dispatches ``restic check``;
            for ``TAR``, dispatches ``tar -tzf <archive>``; for
            ``RSYNC``, dispatches a best-effort dry-run /
            checksum-compare command.  When the backend is
            ``RESTIC`` and the spec carries an
            ``encryption_passphrase``, the passphrase is
            forwarded via runner stdin.

            Operational; never skips.

            :param job_id: The job identifier to verify.

            :returns: The :class:`UbuntuBackupActionResult`.
        """
        rtnval = self._dispatch_backend_verb(
            job_id=job_id,
            mode=_MODE_VERIFY,
            target=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_backups(self) -> list:
        """
            Enumerate every persisted backup-job spec.

            Invokes ``ls -1 <job_dir>`` and ``cat`` per spec
            file.  Each successful parse becomes one
            :class:`UbuntuBackupInfo`.  A missing job directory
            returns an empty list rather than raising; an
            individual spec parse failure is skipped silently.

            :returns: One :class:`UbuntuBackupInfo` per spec file.

            :raises UbuntuBackupQueryError: When ``ls`` exits
                                            non-zero with
                                            non-empty stderr.
        """
        cfg = self._typed_config()
        ls_command = [_LS, "-1", cfg.job_dir]

        ls_result = self._runner.run(
            list(ls_command),
            elevated=False,
        )
        if ls_result.exit_code != 0:
            stderr_text = ls_result.stderr or ""
            if len(stderr_text.strip()) > 0:
                # ENOENT-style errors typically write to stderr.
                # Treat "no such file or directory" as an empty
                # listing rather than a query failure.
                lowered = stderr_text.lower()
                if (
                    "no such file" in lowered
                    or "not found" in lowered
                ):
                    rtnval: list = []
                    return rtnval
                raise UbuntuBackupQueryError(
                    command=ls_command,
                    exit_code=ls_result.exit_code,
                    stderr=stderr_text,
                )
            rtnval = []
            return rtnval

        rtnval = []
        for raw_name in ls_result.stdout.splitlines():
            name = raw_name.strip()
            if len(name) == 0:
                continue
            if not name.endswith(".json"):
                continue
            job_id = name[: -len(".json")]
            spec_path = _job_path(job_dir=cfg.job_dir, job_id=job_id)
            text = self._read_spec_text(path=spec_path)
            if text is None:
                continue
            try:
                spec = _deserialise_spec(text=text)
            except (ValueError, KeyError):
                continue
            info = UbuntuBackupInfo(
                id=spec.id,
                kind=spec.kind,
                created=None,
                size_bytes=None,
                source_count=len(spec.source_paths),
                destination=spec.destination,
            )
            rtnval.append(info)
        return rtnval

    def get_job_status(self, job_id: str) -> UbuntuBackupJobStatus:
        """
            Report the persistence-and-runtime status of a job.

            Reads the persisted spec via ``cat``.  When the spec
            is present, returns a :class:`UbuntuBackupJobStatus`
            with ``enabled=True``.  Runtime fields
            (``last_run_at``, ``last_run_status``,
            ``next_run_at``) are reserved for future scheduled-
            tasks integration and are reported as ``None``.

            :param job_id: The job identifier.

            :returns: The :class:`UbuntuBackupJobStatus`.

            :raises UbuntuBackupNotFoundError: When the spec is
                                               absent.
        """
        cfg = self._typed_config()
        spec_path = _job_path(job_dir=cfg.job_dir, job_id=job_id)
        text = self._read_spec_text(path=spec_path)
        if text is None:
            raise UbuntuBackupNotFoundError(job_id=job_id)
        spec = _deserialise_spec(text=text)
        rtnval = UbuntuBackupJobStatus(
            job_id=spec.id,
            kind=spec.kind,
            enabled=True,
            last_run_at=None,
            last_run_status=None,
            next_run_at=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dispatch_backend_verb(
        self,
        *,
        job_id: str,
        mode: str,
        target: Optional[str],
    ) -> UbuntuBackupActionResult:
        """
            Shared dispatch path for ``run_backup_now``,
            ``restore_from_backup``, and ``verify_backup``.

            :param job_id: The job identifier whose spec to load.
            :param mode: One of ``"run"``, ``"restore"``,
                         ``"verify"``.
            :param target: The restore-target path; required
                           when ``mode == "restore"``.

            :returns: The :class:`UbuntuBackupActionResult`.
        """
        cfg = self._typed_config()
        spec_path = _job_path(job_dir=cfg.job_dir, job_id=job_id)

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuBackupActionStatus.DRY_RUN,
                job_id=job_id,
                target_path=target,
                command=[_CAT, spec_path],
            )
            return dry

        text = self._read_spec_text(path=spec_path)
        if text is None:
            failed = self._build_result(
                status=UbuntuBackupActionStatus.FAILED,
                job_id=job_id,
                target_path=target,
                command=[_CAT, spec_path],
                detail="job not found",
                stderr="job not found",
            )
            return failed

        try:
            spec = _deserialise_spec(text=text)
        except (ValueError, KeyError) as parse_error:
            failed = self._build_result(
                status=UbuntuBackupActionStatus.FAILED,
                job_id=job_id,
                target_path=target,
                command=[_CAT, spec_path],
                detail="malformed spec",
                stderr=str(parse_error),
            )
            return failed

        # Re-attach the runtime passphrase if present — the
        # on-disk spec is redacted but a runtime spec object may
        # carry it.
        spec.encryption_passphrase = (
            self._resolve_runtime_passphrase(persisted=spec)
        )

        argv, stdin_payload = self._build_backend_command(
            spec=spec,
            mode=mode,
            target=target,
        )
        if argv is None:
            failed = self._build_result(
                status=UbuntuBackupActionStatus.FAILED,
                job_id=job_id,
                target_path=target,
                command=[_CAT, spec_path],
                detail=(
                    "unsupported backup kind: {}".format(
                        spec.kind,
                    )
                ),
                stderr="unsupported_kind",
            )
            return failed

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
            stdin=stdin_payload,
        )
        rtnval = self._wrap_run_result(
            run_result=run_result,
            command=argv,
            job_id=job_id,
            target_path=target,
        )
        return rtnval

    def _build_backend_command(
        self,
        *,
        spec: UbuntuBackupJobSpec,
        mode: str,
        target: Optional[str],
    ) -> tuple:
        """
            Build the argv and optional stdin payload for a
            backend dispatch.

            :param spec: The :class:`UbuntuBackupJobSpec`.
            :param mode: One of the ``_MODE_*`` constants.
            :param target: The restore-target path; required
                           when ``mode == _MODE_RESTORE``.

            :returns: A two-tuple ``(argv, stdin)``.  ``argv`` is
                      ``None`` for unsupported kinds.  ``stdin``
                      is ``None`` when the dispatch does not
                      require a stdin payload.
        """
        if spec.kind == UbuntuBackupKind.RSYNC:
            argv = _build_rsync_argv(
                spec=spec, mode=mode, target=target,
            )
            rtnval = (argv, None)
            return rtnval
        if spec.kind == UbuntuBackupKind.RESTIC:
            argv, needs_passphrase = _build_restic_argv(
                spec=spec, mode=mode, target=target,
            )
            stdin_payload: Optional[str] = None
            if (
                needs_passphrase is True
                and spec.encryption_passphrase is not None
                and len(spec.encryption_passphrase) > 0
            ):
                stdin_payload = spec.encryption_passphrase
            rtnval = (argv, stdin_payload)
            return rtnval
        if spec.kind == UbuntuBackupKind.TAR:
            argv = _build_tar_argv(
                spec=spec, mode=mode, target=target,
            )
            rtnval = (argv, None)
            return rtnval
        rtnval = (None, None)
        return rtnval

    def _resolve_runtime_passphrase(
        self,
        *,
        persisted: UbuntuBackupJobSpec,
    ) -> Optional[str]:
        """
            Resolve the runtime passphrase to forward to
            ``restic``.

            The on-disk spec is always redacted, so when the
            persisted field equals the redaction sentinel the
            manager has no passphrase to forward.  A future
            integration with a secrets store would override this
            method.

            :param persisted: The deserialised on-disk spec.

            :returns: The runtime passphrase string, or ``None``
                      when none is available.
        """
        if persisted.encryption_passphrase is None:
            rtnval: Optional[str] = None
            return rtnval
        if persisted.encryption_passphrase == _REDACTION_SENTINEL:
            rtnval = None
            return rtnval
        rtnval = persisted.encryption_passphrase
        return rtnval

    def _typed_config(self) -> UbuntuBackupManagerConfig:
        """
            Return the manager's config typed as
            :class:`UbuntuBackupManagerConfig`.

            :returns: The active config typed.
        """
        if isinstance(self._config, UbuntuBackupManagerConfig):
            rtnval = self._config
            return rtnval
        rtnval = UbuntuBackupManagerConfig(
            dry_run=self._config.dry_run,
            elevated=self._config.elevated,
        )
        return rtnval

    def _read_spec_text(self, *, path: str) -> Optional[str]:
        """
            Read a spec file via ``cat``.

            :param path: The absolute spec file path.

            :returns: The file contents on success; ``None``
                      when ``cat`` exits non-zero.
        """
        command = [_CAT, path]
        run_result = self._runner.run(
            list(command), elevated=False,
        )
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _path_exists(self, *, path: str) -> bool:
        """
            Probe a path's existence via ``test -e``.

            :param path: The absolute path to probe.

            :returns: True when ``test -e`` exits zero; False
                      otherwise.
        """
        command = [_TEST, "-e", path]
        run_result = self._runner.run(
            list(command), elevated=False,
        )
        rtnval = run_result.exit_code == 0
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuBackupActionStatus,
        job_id: Optional[str],
        target_path: Optional[str],
        command: list,
        detail: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuBackupActionResult:
        """
            Construct a :class:`UbuntuBackupActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param job_id: The job identifier echoed onto the
                           result.
            :param target_path: The on-disk target echoed onto
                                the result.
            :param command: The would-have-been argv.
            :param detail: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`UbuntuBackupActionResult`.
        """
        rtnval = UbuntuBackupActionResult(
            status=status,
            detail=detail,
            command=list(command),
            job_id=job_id,
            target_path=target_path,
            stderr=stderr,
        )
        return rtnval

    def _wrap_run_result(
        self,
        *,
        run_result,
        command: list,
        job_id: Optional[str],
        target_path: Optional[str],
    ) -> UbuntuBackupActionResult:
        """
            Map a :class:`RunResult` to an
            :class:`UbuntuBackupActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = UbuntuBackupActionStatus.SUCCESS
        else:
            mapped_status = UbuntuBackupActionStatus.FAILED

        rtnval = UbuntuBackupActionResult(
            status=mapped_status,
            detail=job_id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            job_id=job_id,
            target_path=target_path,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — serialisation
# ----------------------------------------------------------------------


def _serialise_spec(*, spec: UbuntuBackupJobSpec) -> str:
    """
        Serialise an :class:`UbuntuBackupJobSpec` to JSON with
        the encryption passphrase redacted.

        :param spec: The :class:`UbuntuBackupJobSpec`.

        :returns: The JSON document body.
    """
    redacted = _redact_passphrase(spec=spec)
    payload = {
        "id": redacted.id,
        "kind": redacted.kind.value,
        "source_paths": list(redacted.source_paths),
        "destination": redacted.destination,
        "excludes": (
            None if redacted.excludes is None
            else list(redacted.excludes)
        ),
        "compression": redacted.compression,
        "encryption_passphrase": redacted.encryption_passphrase,
        "retention_days": redacted.retention_days,
        "schedule": redacted.schedule,
    }
    rtnval = json.dumps(payload, indent=2, sort_keys=True)
    return rtnval


def _deserialise_spec(*, text: str) -> UbuntuBackupJobSpec:
    """
        Parse a JSON spec body into a
        :class:`UbuntuBackupJobSpec`.

        :param text: The JSON body.

        :returns: The :class:`UbuntuBackupJobSpec`.

        :raises ValueError: When the JSON cannot be parsed or
                            required fields are missing.
        :raises KeyError: When an expected key is absent.
    """
    payload = json.loads(text)

    raw_kind = payload["kind"]
    matched_kind: Optional[UbuntuBackupKind] = None
    for member in UbuntuBackupKind:
        if member.value == raw_kind:
            matched_kind = member
            break
    if matched_kind is None:
        err_msg_lines = ["Unknown UbuntuBackupKind value."]
        err_msg_lines.append("    GOT: {}".format(raw_kind))
        err_msg_lines.append(
            "    EXPECTED: one of {}".format(
                [member.value for member in UbuntuBackupKind],
            ),
        )
        err_msg = os.linesep.join(err_msg_lines)
        raise ValueError(err_msg)

    raw_excludes = payload.get("excludes")
    excludes_value: Optional[list] = None
    if raw_excludes is not None:
        excludes_value = list(raw_excludes)

    rtnval = UbuntuBackupJobSpec(
        id=payload["id"],
        kind=matched_kind,
        source_paths=list(payload["source_paths"]),
        destination=payload["destination"],
        excludes=excludes_value,
        compression=payload.get("compression", True),
        encryption_passphrase=payload.get("encryption_passphrase"),
        retention_days=payload.get("retention_days"),
        schedule=payload.get("schedule"),
    )
    return rtnval


def _redact_passphrase(
    *,
    spec: UbuntuBackupJobSpec,
) -> UbuntuBackupJobSpec:
    """
        Return a copy of ``spec`` with the encryption
        passphrase replaced by the redaction sentinel.

        :param spec: The :class:`UbuntuBackupJobSpec`.

        :returns: A new spec with the passphrase redacted.
    """
    if spec.encryption_passphrase is None:
        passphrase_field: Optional[str] = None
    else:
        passphrase_field = _REDACTION_SENTINEL

    redacted_excludes: Optional[list] = None
    if spec.excludes is not None:
        redacted_excludes = list(spec.excludes)

    rtnval = UbuntuBackupJobSpec(
        id=spec.id,
        kind=spec.kind,
        source_paths=list(spec.source_paths),
        destination=spec.destination,
        excludes=redacted_excludes,
        compression=spec.compression,
        encryption_passphrase=passphrase_field,
        retention_days=spec.retention_days,
        schedule=spec.schedule,
    )
    return rtnval


def _job_path(*, job_dir: str, job_id: str) -> str:
    """
        Compose the absolute path to a persisted spec file.

        :param job_dir: The directory holding spec files.
        :param job_id: The job identifier.

        :returns: ``<job_dir>/<job_id>.json``.
    """
    rtnval = "{}/{}.json".format(job_dir, job_id)
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — argv builders
# ----------------------------------------------------------------------


def _build_rsync_argv(
    *,
    spec: UbuntuBackupJobSpec,
    mode: str,
    target: Optional[str],
) -> list:
    """
        Build the ``rsync`` argv for the requested mode.

        :param spec: The :class:`UbuntuBackupJobSpec`.
        :param mode: One of the ``_MODE_*`` constants.
        :param target: The restore-target path; required when
                       ``mode == _MODE_RESTORE``.

        :returns: The argv as a list of strings.
    """
    if mode == _MODE_RUN:
        argv: list = [_RSYNC, "-aAXv", "--delete"]
        if spec.excludes is not None:
            for pattern in spec.excludes:
                argv.append("--exclude")
                argv.append(pattern)
        for source in spec.source_paths:
            argv.append(source)
        argv.append(spec.destination)
        rtnval = argv
        return rtnval
    if mode == _MODE_RESTORE:
        argv = [_RSYNC, "-aAXv", "--delete"]
        if spec.excludes is not None:
            for pattern in spec.excludes:
                argv.append("--exclude")
                argv.append(pattern)
        argv.append(spec.destination)
        if target is not None:
            argv.append(target)
        rtnval = argv
        return rtnval
    if mode == _MODE_VERIFY:
        # Best-effort: rsync dry-run with checksum compare
        # against the source list returns zero diff when the
        # mirror is intact.
        argv = [_RSYNC, "-an", "--checksum", "--delete"]
        for source in spec.source_paths:
            argv.append(source)
        argv.append(spec.destination)
        rtnval = argv
        return rtnval
    err_msg_lines = ["Unknown rsync dispatch mode."]
    err_msg_lines.append("    GOT: {}".format(mode))
    err_msg = os.linesep.join(err_msg_lines)
    raise ValueError(err_msg)


def _build_restic_argv(
    *,
    spec: UbuntuBackupJobSpec,
    mode: str,
    target: Optional[str],
) -> tuple:
    """
        Build the ``restic`` argv for the requested mode.

        The passphrase is NEVER placed on argv.  Instead,
        ``--password-file=/dev/stdin`` is appended and the caller
        forwards the passphrase via runner stdin.

        :param spec: The :class:`UbuntuBackupJobSpec`.
        :param mode: One of the ``_MODE_*`` constants.
        :param target: The restore-target path; required when
                       ``mode == _MODE_RESTORE``.

        :returns: A two-tuple ``(argv, needs_passphrase)``.
    """
    needs_passphrase = (
        spec.encryption_passphrase is not None
        and len(spec.encryption_passphrase) > 0
        and spec.encryption_passphrase != _REDACTION_SENTINEL
    )

    base: list = [_RESTIC, "--repo", spec.destination]
    if needs_passphrase is True:
        base.append("--password-file=/dev/stdin")

    if mode == _MODE_RUN:
        argv = list(base)
        argv.append("backup")
        for source in spec.source_paths:
            argv.append(source)
        if spec.excludes is not None:
            for pattern in spec.excludes:
                argv.append("--exclude")
                argv.append(pattern)
        rtnval = (argv, needs_passphrase)
        return rtnval
    if mode == _MODE_RESTORE:
        argv = list(base)
        argv.append("restore")
        argv.append("latest")
        argv.append("--target")
        if target is not None:
            argv.append(target)
        rtnval = (argv, needs_passphrase)
        return rtnval
    if mode == _MODE_VERIFY:
        argv = list(base)
        argv.append("check")
        rtnval = (argv, needs_passphrase)
        return rtnval

    err_msg_lines = ["Unknown restic dispatch mode."]
    err_msg_lines.append("    GOT: {}".format(mode))
    err_msg = os.linesep.join(err_msg_lines)
    raise ValueError(err_msg)


def _build_tar_argv(
    *,
    spec: UbuntuBackupJobSpec,
    mode: str,
    target: Optional[str],
) -> list:
    """
        Build the ``tar`` argv for the requested mode.

        :param spec: The :class:`UbuntuBackupJobSpec`.
        :param mode: One of the ``_MODE_*`` constants.
        :param target: The restore-target path; required when
                       ``mode == _MODE_RESTORE``.

        :returns: The argv as a list of strings.
    """
    if spec.compression is True:
        create_flags = "-czf"
        extract_flags = "-xzf"
        list_flags = "-tzf"
    else:
        create_flags = "-cf"
        extract_flags = "-xf"
        list_flags = "-tf"

    if mode == _MODE_RUN:
        argv: list = [_TAR, create_flags, spec.destination]
        if spec.excludes is not None:
            for pattern in spec.excludes:
                argv.append("--exclude={}".format(pattern))
        for source in spec.source_paths:
            argv.append(source)
        rtnval = argv
        return rtnval
    if mode == _MODE_RESTORE:
        argv = [_TAR, extract_flags, spec.destination, "-C"]
        if target is not None:
            argv.append(target)
        rtnval = argv
        return rtnval
    if mode == _MODE_VERIFY:
        argv = [_TAR, list_flags, spec.destination]
        rtnval = argv
        return rtnval

    err_msg_lines = ["Unknown tar dispatch mode."]
    err_msg_lines.append("    GOT: {}".format(mode))
    err_msg = os.linesep.join(err_msg_lines)
    raise ValueError(err_msg)
