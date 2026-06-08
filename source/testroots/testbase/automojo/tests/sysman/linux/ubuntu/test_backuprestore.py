"""
    Tests for :class:`UbuntuBackupRestoreManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult` values
    keyed by argv tuple.  No live ``rsync`` / ``restic`` /
    ``tar`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.backuprestore import (
    UbuntuBackupActionStatus,
    UbuntuBackupJobSpec,
    UbuntuBackupKind,
    UbuntuBackupManagerConfig,
    UbuntuBackupQueryError,
    UbuntuBackupRestoreManager,
    _serialise_spec,
)


_DEFAULT_JOB_DIR = "/var/lib/ccai/backups/jobs"


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every
        ``run`` call and returns pre-seeded :class:`RunResult`
        values keyed by argv tuple.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)

    def run(
        self,
        args: list,
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult:
        call_record = {
            "args": list(args),
            "elevated": elevated,
            "stdin": stdin,
            "env": env,
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _seed_test_dash_e(
    runner: _MockCommandRunner,
    *,
    path: str,
    exists: bool,
) -> None:
    """
        Seed the runner so that ``test -e <path>`` reports
        existence according to ``exists``.
    """
    if exists:
        result = RunResult(exit_code=0, stdout="", stderr="")
    else:
        result = RunResult(exit_code=1, stdout="", stderr="")
    runner.results[("test", "-e", path)] = result
    return


def _seed_cat(
    runner: _MockCommandRunner,
    *,
    path: str,
    body: Optional[str],
) -> None:
    """
        Seed the runner so that ``cat <path>`` returns ``body``
        on success, or non-zero when ``body`` is None.
    """
    if body is None:
        result = RunResult(
            exit_code=1, stdout="", stderr="No such file",
        )
    else:
        result = RunResult(exit_code=0, stdout=body, stderr="")
    runner.results[("cat", path)] = result
    return


def _seed_ls(
    runner: _MockCommandRunner,
    *,
    path: str,
    listing: Optional[list],
) -> None:
    """
        Seed the runner so that ``ls -1 <path>`` returns
        ``listing`` (one entry per line) on success, or non-zero
        when ``listing`` is None.
    """
    if listing is None:
        result = RunResult(
            exit_code=2,
            stdout="",
            stderr="ls: cannot access {}: No such file".format(path),
        )
    else:
        result = RunResult(
            exit_code=0,
            stdout="\n".join(listing),
            stderr="",
        )
    runner.results[("ls", "-1", path)] = result
    return


def _make_spec_rsync() -> UbuntuBackupJobSpec:
    """
        Build a stable RSYNC spec for re-use across tests.
    """
    rtnval = UbuntuBackupJobSpec(
        id="etc-mirror",
        kind=UbuntuBackupKind.RSYNC,
        source_paths=["/etc"],
        destination="/srv/backups/etc",
        excludes=["*.swp"],
    )
    return rtnval


def _make_spec_restic_encrypted() -> UbuntuBackupJobSpec:
    """
        Build a stable RESTIC spec with an encryption passphrase.
    """
    rtnval = UbuntuBackupJobSpec(
        id="home-restic",
        kind=UbuntuBackupKind.RESTIC,
        source_paths=["/home"],
        destination="/srv/backups/restic-repo",
        excludes=None,
        encryption_passphrase="topsecret-passphrase",
    )
    return rtnval


def _make_spec_tar() -> UbuntuBackupJobSpec:
    """
        Build a stable TAR spec.
    """
    rtnval = UbuntuBackupJobSpec(
        id="opt-archive",
        kind=UbuntuBackupKind.TAR,
        source_paths=["/opt/myapp"],
        destination="/srv/backups/myapp.tar.gz",
        excludes=["cache"],
        compression=True,
    )
    return rtnval


# ----------------------------------------------------------------------
# create_backup_job
# ----------------------------------------------------------------------


def test_create_backup_job_persists_spec():
    """
        create_backup_job dispatches ``tee`` with the JSON
        payload on stdin and ``elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_rsync()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=None)

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    result = manager.create_backup_job(spec=spec)

    testbase.assert_equal(
        result.status, UbuntuBackupActionStatus.SUCCESS,
    )
    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["args"][1], spec_path)
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    testbase.assert_true(tee_calls[0]["stdin"] is not None)
    testbase.assert_true("\"id\": \"etc-mirror\"" in tee_calls[0]["stdin"])
    return


def test_create_backup_job_skipped_when_present():
    """
        create_backup_job returns SKIPPED when the on-disk spec
        is byte-equivalent to the proposed serialisation.
    """
    runner = _MockCommandRunner()
    spec = _make_spec_rsync()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    persisted_payload = _serialise_spec(spec=spec)
    _seed_cat(runner, path=spec_path, body=persisted_payload)

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    result = manager.create_backup_job(spec=spec)

    testbase.assert_equal(
        result.status, UbuntuBackupActionStatus.SKIPPED,
    )
    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


def test_create_backup_job_dry_run():
    """
        create_backup_job under dry-run does not invoke the
        runner and returns DRY_RUN.
    """
    runner = _MockCommandRunner()
    config = UbuntuBackupManagerConfig(elevated=True, dry_run=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    spec = _make_spec_rsync()

    result = manager.create_backup_job(spec=spec)

    testbase.assert_equal(
        result.status, UbuntuBackupActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# delete_backup_job
# ----------------------------------------------------------------------


def test_delete_backup_job_invokes_remove():
    """
        delete_backup_job dispatches ``rm -f <spec_path>`` with
        ``elevated=True`` when the spec exists.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec_path = "{}/etc-mirror.json".format(_DEFAULT_JOB_DIR)
    _seed_test_dash_e(runner, path=spec_path, exists=True)

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    result = manager.delete_backup_job(job_id="etc-mirror")

    testbase.assert_equal(
        result.status, UbuntuBackupActionStatus.SUCCESS,
    )
    rm_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "rm"
    ]
    testbase.assert_equal(len(rm_calls), 1)
    testbase.assert_equal(
        rm_calls[0]["args"], ["rm", "-f", spec_path],
    )
    testbase.assert_equal(rm_calls[0]["elevated"], True)
    return


def test_delete_backup_job_skipped_when_absent():
    """
        delete_backup_job returns SKIPPED when the spec does not
        exist on disk.
    """
    runner = _MockCommandRunner()
    spec_path = "{}/etc-mirror.json".format(_DEFAULT_JOB_DIR)
    _seed_test_dash_e(runner, path=spec_path, exists=False)

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    result = manager.delete_backup_job(job_id="etc-mirror")

    testbase.assert_equal(
        result.status, UbuntuBackupActionStatus.SKIPPED,
    )
    rm_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "rm"
    ]
    testbase.assert_equal(len(rm_calls), 0)
    return


# ----------------------------------------------------------------------
# run_backup_now
# ----------------------------------------------------------------------


def test_run_backup_now_dispatches_rsync():
    """
        run_backup_now with kind=RSYNC dispatches
        ``rsync -aAXv --delete <sources> <destination>``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_rsync()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=_serialise_spec(spec=spec))

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    result = manager.run_backup_now(job_id=spec.id)

    testbase.assert_equal(
        result.status, UbuntuBackupActionStatus.SUCCESS,
    )
    rsync_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "rsync"
    ]
    testbase.assert_equal(len(rsync_calls), 1)
    rsync_argv = rsync_calls[0]["args"]
    testbase.assert_equal(rsync_argv[0], "rsync")
    testbase.assert_equal(rsync_argv[1], "-aAXv")
    testbase.assert_equal(rsync_argv[2], "--delete")
    # The exclude pattern must follow --exclude.
    testbase.assert_true("--exclude" in rsync_argv)
    testbase.assert_true("*.swp" in rsync_argv)
    # The source and destination must be the last two argv
    # tokens.
    testbase.assert_equal(rsync_argv[-2], "/etc")
    testbase.assert_equal(rsync_argv[-1], "/srv/backups/etc")
    testbase.assert_equal(rsync_calls[0]["elevated"], True)
    return


def test_run_backup_now_dispatches_restic():
    """
        run_backup_now with kind=RESTIC dispatches
        ``restic --repo <dst> --password-file=/dev/stdin
        backup <sources>``, with the passphrase on stdin.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_restic_encrypted()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    # The on-disk spec is redacted; the runtime spec is not.
    # For this test we need the runtime passphrase to flow to
    # stdin, so we forge the on-disk text with the live
    # passphrase to simulate a runtime override path.
    forged_payload = _serialise_spec(spec=spec).replace(
        "\"***REDACTED***\"",
        "\"topsecret-passphrase\"",
    )
    _seed_cat(runner, path=spec_path, body=forged_payload)

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    result = manager.run_backup_now(job_id=spec.id)

    testbase.assert_equal(
        result.status, UbuntuBackupActionStatus.SUCCESS,
    )
    restic_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "restic"
    ]
    testbase.assert_equal(len(restic_calls), 1)
    restic_argv = restic_calls[0]["args"]
    testbase.assert_true("--repo" in restic_argv)
    testbase.assert_true(
        "--password-file=/dev/stdin" in restic_argv,
    )
    testbase.assert_true("backup" in restic_argv)
    testbase.assert_true("/home" in restic_argv)
    # The passphrase must NEVER appear on argv.
    for token in restic_argv:
        testbase.assert_true(
            "topsecret-passphrase" not in token,
        )
    # And confirm the runner saw the passphrase on stdin.
    testbase.assert_equal(
        restic_calls[0]["stdin"], "topsecret-passphrase",
    )
    testbase.assert_equal(restic_calls[0]["elevated"], True)
    return


def test_run_backup_now_dispatches_tar():
    """
        run_backup_now with kind=TAR dispatches
        ``tar -czf <archive> <sources>``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_tar()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=_serialise_spec(spec=spec))

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    result = manager.run_backup_now(job_id=spec.id)

    testbase.assert_equal(
        result.status, UbuntuBackupActionStatus.SUCCESS,
    )
    tar_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tar"
    ]
    testbase.assert_equal(len(tar_calls), 1)
    tar_argv = tar_calls[0]["args"]
    testbase.assert_equal(tar_argv[0], "tar")
    testbase.assert_equal(tar_argv[1], "-czf")
    testbase.assert_equal(tar_argv[2], "/srv/backups/myapp.tar.gz")
    testbase.assert_true("--exclude=cache" in tar_argv)
    testbase.assert_true("/opt/myapp" in tar_argv)
    testbase.assert_equal(tar_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# list_backups
# ----------------------------------------------------------------------


def test_list_backups_parses_ls_output():
    """
        list_backups invokes ``ls -1 <job_dir>``, then ``cat``
        per spec, and returns one UbuntuBackupInfo per row.
    """
    runner = _MockCommandRunner()
    rsync_spec = _make_spec_rsync()
    tar_spec = _make_spec_tar()
    _seed_ls(
        runner,
        path=_DEFAULT_JOB_DIR,
        listing=[
            "{}.json".format(rsync_spec.id),
            "{}.json".format(tar_spec.id),
            "README",
        ],
    )
    _seed_cat(
        runner,
        path="{}/{}.json".format(_DEFAULT_JOB_DIR, rsync_spec.id),
        body=_serialise_spec(spec=rsync_spec),
    )
    _seed_cat(
        runner,
        path="{}/{}.json".format(_DEFAULT_JOB_DIR, tar_spec.id),
        body=_serialise_spec(spec=tar_spec),
    )

    manager = UbuntuBackupRestoreManager(runner=runner)
    results = manager.list_backups()

    testbase.assert_equal(len(results), 2)
    by_id = {info.id: info for info in results}
    testbase.assert_true("etc-mirror" in by_id)
    testbase.assert_true("opt-archive" in by_id)
    testbase.assert_equal(
        by_id["etc-mirror"].kind, UbuntuBackupKind.RSYNC,
    )
    testbase.assert_equal(
        by_id["opt-archive"].kind, UbuntuBackupKind.TAR,
    )
    testbase.assert_equal(by_id["etc-mirror"].source_count, 1)
    return


def test_list_backups_empty_when_directory_missing():
    """
        list_backups returns an empty list when ``ls`` reports
        the job directory does not exist.
    """
    runner = _MockCommandRunner()
    _seed_ls(runner, path=_DEFAULT_JOB_DIR, listing=None)

    manager = UbuntuBackupRestoreManager(runner=runner)
    results = manager.list_backups()

    testbase.assert_equal(len(results), 0)
    return


# ----------------------------------------------------------------------
# restore_from_backup
# ----------------------------------------------------------------------


def test_restore_from_backup_invokes_rsync():
    """
        restore_from_backup with kind=RSYNC dispatches
        ``rsync -aAXv --delete <destination> <target>``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_rsync()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=_serialise_spec(spec=spec))

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    result = manager.restore_from_backup(
        job_id=spec.id, target="/restore-root/etc",
    )

    testbase.assert_equal(
        result.status, UbuntuBackupActionStatus.SUCCESS,
    )
    rsync_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "rsync"
    ]
    testbase.assert_equal(len(rsync_calls), 1)
    argv = rsync_calls[0]["args"]
    # Restore reverses direction: destination first, target last.
    testbase.assert_equal(argv[-2], "/srv/backups/etc")
    testbase.assert_equal(argv[-1], "/restore-root/etc")
    return


def test_restore_from_backup_invokes_tar():
    """
        restore_from_backup with kind=TAR dispatches
        ``tar -xzf <archive> -C <target>``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_tar()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=_serialise_spec(spec=spec))

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    result = manager.restore_from_backup(
        job_id=spec.id, target="/restore-root/opt",
    )

    testbase.assert_equal(
        result.status, UbuntuBackupActionStatus.SUCCESS,
    )
    tar_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tar"
    ]
    testbase.assert_equal(len(tar_calls), 1)
    argv = tar_calls[0]["args"]
    testbase.assert_equal(argv[0], "tar")
    testbase.assert_equal(argv[1], "-xzf")
    testbase.assert_equal(argv[2], "/srv/backups/myapp.tar.gz")
    testbase.assert_equal(argv[3], "-C")
    testbase.assert_equal(argv[4], "/restore-root/opt")
    return


# ----------------------------------------------------------------------
# verify_backup
# ----------------------------------------------------------------------


def test_verify_backup_invokes_restic_check():
    """
        verify_backup with kind=RESTIC dispatches
        ``restic --repo <dst> check`` (with the passphrase on
        stdin when configured).
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_restic_encrypted()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    forged_payload = _serialise_spec(spec=spec).replace(
        "\"***REDACTED***\"",
        "\"topsecret-passphrase\"",
    )
    _seed_cat(runner, path=spec_path, body=forged_payload)

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    result = manager.verify_backup(job_id=spec.id)

    testbase.assert_equal(
        result.status, UbuntuBackupActionStatus.SUCCESS,
    )
    restic_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "restic"
    ]
    testbase.assert_equal(len(restic_calls), 1)
    argv = restic_calls[0]["args"]
    testbase.assert_true("check" in argv)
    testbase.assert_equal(
        restic_calls[0]["stdin"], "topsecret-passphrase",
    )
    return


def test_verify_backup_invokes_tar_tzf():
    """
        verify_backup with kind=TAR dispatches
        ``tar -tzf <archive>``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_tar()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=_serialise_spec(spec=spec))

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    result = manager.verify_backup(job_id=spec.id)

    testbase.assert_equal(
        result.status, UbuntuBackupActionStatus.SUCCESS,
    )
    tar_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tar"
    ]
    testbase.assert_equal(len(tar_calls), 1)
    argv = tar_calls[0]["args"]
    testbase.assert_equal(argv[0], "tar")
    testbase.assert_equal(argv[1], "-tzf")
    testbase.assert_equal(argv[2], "/srv/backups/myapp.tar.gz")
    return


# ----------------------------------------------------------------------
# Cross-cutting invariants
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write verbs honour ``config.elevated=True`` on every
        runner invocation that mutates state.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_rsync()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=None)

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    manager.create_backup_job(spec=spec)

    write_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tee"
    ]
    testbase.assert_true(len(write_calls) >= 1)
    for call in write_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs never pass ``elevated=True`` to the runner.
    """
    runner = _MockCommandRunner()
    rsync_spec = _make_spec_rsync()
    _seed_ls(
        runner,
        path=_DEFAULT_JOB_DIR,
        listing=["{}.json".format(rsync_spec.id)],
    )
    _seed_cat(
        runner,
        path="{}/{}.json".format(_DEFAULT_JOB_DIR, rsync_spec.id),
        body=_serialise_spec(spec=rsync_spec),
    )

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    manager.list_backups()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        When a backend command exits non-zero the FAILED result
        carries the captured stderr.
    """
    runner = _MockCommandRunner()
    spec = _make_spec_tar()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=_serialise_spec(spec=spec))
    runner.default_result = RunResult(
        exit_code=2,
        stdout="",
        stderr="tar: cannot open /srv/backups/myapp.tar.gz",
    )

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    result = manager.run_backup_now(job_id=spec.id)

    testbase.assert_equal(
        result.status, UbuntuBackupActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_true(
        result.stderr is not None
        and "cannot open" in result.stderr,
    )
    return


def test_passphrase_redacted_when_restic_encrypted():
    """
        When create_backup_job persists a RESTIC spec carrying
        a passphrase, the JSON written via tee replaces the
        passphrase with the redaction sentinel.  Subsequent
        run_backup_now calls also keep the passphrase off argv.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_restic_encrypted()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=None)

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    result = manager.create_backup_job(spec=spec)

    testbase.assert_equal(
        result.status, UbuntuBackupActionStatus.SUCCESS,
    )
    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    persisted_text = tee_calls[0]["stdin"]
    testbase.assert_true(persisted_text is not None)
    testbase.assert_true(
        "topsecret-passphrase" not in persisted_text,
    )
    testbase.assert_true("***REDACTED***" in persisted_text)
    # And confirm the result.command also lacks the passphrase.
    testbase.assert_true(result.command is not None)
    for token in result.command:
        testbase.assert_true(
            "topsecret-passphrase" not in token,
        )
    return


def test_unsupported_kind_returns_failed_without_dispatch():
    """
        When the persisted spec carries a kind value that the
        manager does not support, run_backup_now returns FAILED
        without invoking any backend.
    """
    runner = _MockCommandRunner()
    spec_path = "{}/orphan.json".format(_DEFAULT_JOB_DIR)
    forged = "{\n"
    forged += "  \"id\": \"orphan\",\n"
    forged += "  \"kind\": \"orphan-backend\",\n"
    forged += "  \"source_paths\": [\"/etc\"],\n"
    forged += "  \"destination\": \"/srv/backups/etc\",\n"
    forged += "  \"excludes\": null,\n"
    forged += "  \"compression\": true,\n"
    forged += "  \"encryption_passphrase\": null,\n"
    forged += "  \"retention_days\": null,\n"
    forged += "  \"schedule\": null\n"
    forged += "}"
    _seed_cat(runner, path=spec_path, body=forged)

    config = UbuntuBackupManagerConfig(elevated=True)
    manager = UbuntuBackupRestoreManager(runner=runner, config=config)
    result = manager.run_backup_now(job_id="orphan")

    testbase.assert_equal(
        result.status, UbuntuBackupActionStatus.FAILED,
    )
    backend_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1
        and call["args"][0] in ("rsync", "restic", "tar")
    ]
    testbase.assert_equal(len(backend_calls), 0)
    return


def test_list_backups_query_error_when_ls_fails_unexpectedly():
    """
        list_backups raises UbuntuBackupQueryError when ``ls``
        fails with an unexpected (non-ENOENT) stderr.
    """
    runner = _MockCommandRunner()
    runner.results[("ls", "-1", _DEFAULT_JOB_DIR)] = RunResult(
        exit_code=2,
        stdout="",
        stderr="ls: permission denied",
    )

    manager = UbuntuBackupRestoreManager(runner=runner)
    raised = False
    try:
        manager.list_backups()
    except UbuntuBackupQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_action_result_carries_target_path():
    """
        Every successful action result populates the
        ``target_path`` field with the on-disk path the action
        produced or referenced.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = _make_spec_rsync()
    spec_path = "{}/{}.json".format(_DEFAULT_JOB_DIR, spec.id)
    _seed_cat(runner, path=spec_path, body=None)

    manager = UbuntuBackupRestoreManager(
        runner=runner,
        config=UbuntuBackupManagerConfig(elevated=True),
    )
    result = manager.create_backup_job(spec=spec)

    testbase.assert_equal(result.target_path, spec_path)
    testbase.assert_equal(result.job_id, spec.id)
    return
