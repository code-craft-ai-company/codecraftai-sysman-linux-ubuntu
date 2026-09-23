"""
    Tests for :class:`UbuntuFilesystemQuotasManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by
    argv tuple.  No live ``quotacheck`` / ``quotaon`` /
    ``setquota`` / ``quota`` / ``repquota`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.filesystemquotas import (
    UbuntuFilesystemQuotasManager,
    UbuntuFilesystemQuotasManagerConfig,
    UbuntuQuotaActionStatus,
    UbuntuQuotaFilesystemNotMountedError,
    UbuntuQuotaPrincipalKind,
    _parse_quotaon_status,
)


# ----------------------------------------------------------------------
# Mock runner
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call
        and returns pre-seeded :class:`RunResult` values keyed by
        argv tuple.
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


# ----------------------------------------------------------------------
# Seed helpers
# ----------------------------------------------------------------------


def _seed_findmnt_mounted(
    runner: _MockCommandRunner,
    *,
    mountpoint: str,
    fstype: str = "ext4",
) -> None:
    """
        Seed ``findmnt -n -o FSTYPE <mountpoint>`` to return
        ``fstype``.
    """
    key = ("findmnt", "-n", "-o", "FSTYPE", mountpoint)
    runner.results[key] = RunResult(
        exit_code=0,
        stdout=fstype + "\n",
        stderr="",
    )
    return


def _seed_findmnt_unmounted(
    runner: _MockCommandRunner,
    *,
    mountpoint: str,
) -> None:
    """
        Seed ``findmnt`` to fail for ``mountpoint``.
    """
    key = ("findmnt", "-n", "-o", "FSTYPE", mountpoint)
    runner.results[key] = RunResult(
        exit_code=1,
        stdout="",
        stderr="",
    )
    return


def _seed_quotaon_status(
    runner: _MockCommandRunner,
    *,
    mountpoint: str,
    user_on: bool,
    group_on: bool,
    exit_code: int = 0,
    stderr: str = "",
) -> None:
    """
        Seed ``quotaon -p <mountpoint>`` output.
    """
    user_state = "on" if user_on else "off"
    group_state = "on" if group_on else "off"
    body_lines = [
        "user quota on {} (/dev/sda1) is {}".format(mountpoint, user_state),
        "group quota on {} (/dev/sda1) is {}".format(mountpoint, group_state),
    ]
    body = "\n".join(body_lines) + "\n"
    key = ("quotaon", "-p", mountpoint)
    runner.results[key] = RunResult(
        exit_code=exit_code,
        stdout=body if exit_code == 0 else "",
        stderr=stderr,
    )
    return


def _seed_quota_user(
    runner: _MockCommandRunner,
    *,
    user: str,
    mountpoint: str,
    used_kb: int,
    soft_kb: int,
    hard_kb: int,
) -> None:
    """
        Seed ``quota -u <user>`` to report a single row for
        ``mountpoint``.
    """
    body_lines = [
        "Disk quotas for user {} (uid 1001):".format(user),
        "     Filesystem   blocks   quota   limit   grace   files   quota   limit   grace",
        "       {}   {}  {}  {}        1234       0       0".format(
            mountpoint, used_kb, soft_kb, hard_kb,
        ),
    ]
    body = "\n".join(body_lines) + "\n"
    key = ("quota", "-u", user)
    runner.results[key] = RunResult(
        exit_code=0,
        stdout=body,
        stderr="",
    )
    return


def _seed_quota_group(
    runner: _MockCommandRunner,
    *,
    group: str,
    mountpoint: str,
    used_kb: int,
    soft_kb: int,
    hard_kb: int,
) -> None:
    """
        Seed ``quota -g <group>`` to report a single row for
        ``mountpoint``.
    """
    body_lines = [
        "Disk quotas for group {} (gid 2001):".format(group),
        "     Filesystem   blocks   quota   limit   grace   files   quota   limit   grace",
        "       {}   {}  {}  {}        56       0       0".format(
            mountpoint, used_kb, soft_kb, hard_kb,
        ),
    ]
    body = "\n".join(body_lines) + "\n"
    key = ("quota", "-g", group)
    runner.results[key] = RunResult(
        exit_code=0,
        stdout=body,
        stderr="",
    )
    return


def _seed_quota_user_no_record(
    runner: _MockCommandRunner,
    *,
    user: str,
) -> None:
    """
        Seed ``quota -u`` with empty body and exit code 2 (no
        quotas for this principal).
    """
    key = ("quota", "-u", user)
    runner.results[key] = RunResult(
        exit_code=2,
        stdout="",
        stderr="",
    )
    return


# ----------------------------------------------------------------------
# enable_quotas
# ----------------------------------------------------------------------


def test_enable_quotas_invokes_quotacheck_and_quotaon():
    """
        ``enable_quotas`` must invoke ``quotacheck -ug -mF vfsv0``
        followed by ``quotaon -ug`` with the right argv.
    """
    runner = _MockCommandRunner()
    _seed_findmnt_mounted(runner, mountpoint="/mnt/data")
    _seed_quotaon_status(
        runner,
        mountpoint="/mnt/data",
        user_on=False,
        group_on=False,
    )

    config = UbuntuFilesystemQuotasManagerConfig(elevated=True)
    manager = UbuntuFilesystemQuotasManager(runner=runner, config=config)

    result = manager.enable_quotas(mountpoint="/mnt/data")

    testbase.assert_equal(result.status, UbuntuQuotaActionStatus.SUCCESS)

    mutation_calls = [
        call for call in runner.calls
        if call["args"][0] in ("quotacheck", "quotaon")
        and call["args"][1] != "-p"
    ]
    testbase.assert_equal(len(mutation_calls), 2)
    testbase.assert_equal(
        mutation_calls[0]["args"],
        ["quotacheck", "-ug", "-mF", "vfsv0", "/mnt/data"],
    )
    testbase.assert_equal(
        mutation_calls[1]["args"],
        ["quotaon", "-ug", "/mnt/data"],
    )
    return


def test_enable_quotas_skipped_when_enabled():
    """
        ``enable_quotas`` must return SKIPPED when both user and
        group quotas are already on.
    """
    runner = _MockCommandRunner()
    _seed_findmnt_mounted(runner, mountpoint="/mnt/data")
    _seed_quotaon_status(
        runner,
        mountpoint="/mnt/data",
        user_on=True,
        group_on=True,
    )

    manager = UbuntuFilesystemQuotasManager(runner=runner)
    result = manager.enable_quotas(mountpoint="/mnt/data")

    testbase.assert_equal(result.status, UbuntuQuotaActionStatus.SKIPPED)

    mutation_calls = [
        call for call in runner.calls if call["args"][0] in ("quotacheck", "quotaon") and call["args"][1] != "-p"
    ]
    testbase.assert_equal(len(mutation_calls), 0)
    return


def test_enable_quotas_dry_run():
    """
        ``enable_quotas`` must return DRY_RUN with the planned argv
        and never invoke the runner.
    """
    runner = _MockCommandRunner()
    config = UbuntuFilesystemQuotasManagerConfig(dry_run=True)
    manager = UbuntuFilesystemQuotasManager(runner=runner, config=config)

    result = manager.enable_quotas(mountpoint="/mnt/data")

    testbase.assert_equal(result.status, UbuntuQuotaActionStatus.DRY_RUN)
    testbase.assert_equal(
        result.command,
        ["quotacheck", "-ug", "-mF", "vfsv0", "/mnt/data"],
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# disable_quotas
# ----------------------------------------------------------------------


def test_disable_quotas_invokes_quotaoff():
    """
        ``disable_quotas`` must invoke ``quotaoff -ug
        <mountpoint>``.
    """
    runner = _MockCommandRunner()
    _seed_findmnt_mounted(runner, mountpoint="/mnt/data")
    _seed_quotaon_status(
        runner,
        mountpoint="/mnt/data",
        user_on=True,
        group_on=True,
    )

    manager = UbuntuFilesystemQuotasManager(runner=runner)
    result = manager.disable_quotas(mountpoint="/mnt/data")

    testbase.assert_equal(result.status, UbuntuQuotaActionStatus.SUCCESS)

    mutation_calls = [
        call for call in runner.calls if call["args"][0] == "quotaoff"
    ]
    testbase.assert_equal(len(mutation_calls), 1)
    testbase.assert_equal(
        mutation_calls[0]["args"],
        ["quotaoff", "-ug", "/mnt/data"],
    )
    return


def test_disable_quotas_skipped_when_off():
    """
        ``disable_quotas`` must return SKIPPED when both kinds are
        already off.
    """
    runner = _MockCommandRunner()
    _seed_findmnt_mounted(runner, mountpoint="/mnt/data")
    _seed_quotaon_status(
        runner,
        mountpoint="/mnt/data",
        user_on=False,
        group_on=False,
    )

    manager = UbuntuFilesystemQuotasManager(runner=runner)
    result = manager.disable_quotas(mountpoint="/mnt/data")

    testbase.assert_equal(result.status, UbuntuQuotaActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# set_user_quota / set_group_quota
# ----------------------------------------------------------------------


def test_set_user_quota_invokes_setquota():
    """
        ``set_user_quota`` must invoke ``setquota -u <user>
        <soft_kb> <hard_kb> 0 0 <mountpoint>``.
    """
    runner = _MockCommandRunner()
    _seed_findmnt_mounted(runner, mountpoint="/mnt/data")
    _seed_quota_user_no_record(runner, user="alice")

    config = UbuntuFilesystemQuotasManagerConfig(elevated=True)
    manager = UbuntuFilesystemQuotasManager(runner=runner, config=config)

    result = manager.set_user_quota(
        mountpoint="/mnt/data",
        user="alice",
        soft_bytes=200 * 1024 * 1024,    # 200 MiB → 204800 KiB
        hard_bytes=400 * 1024 * 1024,    # 400 MiB → 409600 KiB
    )

    testbase.assert_equal(result.status, UbuntuQuotaActionStatus.SUCCESS)

    setquota_calls = [
        call for call in runner.calls if call["args"][0] == "setquota"
    ]
    testbase.assert_equal(len(setquota_calls), 1)
    testbase.assert_equal(
        setquota_calls[0]["args"],
        ["setquota", "-u", "alice", "204800", "409600", "0", "0", "/mnt/data"],
    )
    return


def test_set_user_quota_skipped_when_match():
    """
        ``set_user_quota`` must return SKIPPED when the current
        record already matches the requested limits.
    """
    runner = _MockCommandRunner()
    _seed_findmnt_mounted(runner, mountpoint="/mnt/data")
    _seed_quota_user(
        runner,
        user="alice",
        mountpoint="/mnt/data",
        used_kb=102400,
        soft_kb=204800,
        hard_kb=409600,
    )

    manager = UbuntuFilesystemQuotasManager(runner=runner)
    result = manager.set_user_quota(
        mountpoint="/mnt/data",
        user="alice",
        soft_bytes=204800 * 1024,
        hard_bytes=409600 * 1024,
    )

    testbase.assert_equal(result.status, UbuntuQuotaActionStatus.SKIPPED)

    setquota_calls = [
        call for call in runner.calls if call["args"][0] == "setquota"
    ]
    testbase.assert_equal(len(setquota_calls), 0)
    return


def test_set_group_quota_invokes_setquota():
    """
        ``set_group_quota`` must invoke ``setquota -g <group>
        <soft_kb> <hard_kb> 0 0 <mountpoint>``.
    """
    runner = _MockCommandRunner()
    _seed_findmnt_mounted(runner, mountpoint="/mnt/data")
    _seed_quota_user_no_record(runner, user="staff")
    runner.results[("quota", "-g", "staff")] = RunResult(
        exit_code=2, stdout="", stderr="",
    )

    manager = UbuntuFilesystemQuotasManager(runner=runner)
    result = manager.set_group_quota(
        mountpoint="/mnt/data",
        group="staff",
        soft_bytes=100 * 1024 * 1024,
        hard_bytes=200 * 1024 * 1024,
    )

    testbase.assert_equal(result.status, UbuntuQuotaActionStatus.SUCCESS)

    setquota_calls = [
        call for call in runner.calls if call["args"][0] == "setquota"
    ]
    testbase.assert_equal(len(setquota_calls), 1)
    testbase.assert_equal(
        setquota_calls[0]["args"],
        ["setquota", "-g", "staff", "102400", "204800", "0", "0", "/mnt/data"],
    )
    return


# ----------------------------------------------------------------------
# get_user_quota / get_group_quota
# ----------------------------------------------------------------------


def test_get_user_quota_parses_quota_output():
    """
        ``get_user_quota`` must parse ``quota -u`` output into a
        :class:`UbuntuQuotaInfo` and convert KiB to bytes.
    """
    runner = _MockCommandRunner()
    _seed_quota_user(
        runner,
        user="alice",
        mountpoint="/mnt/data",
        used_kb=1024,
        soft_kb=2048,
        hard_kb=4096,
    )

    manager = UbuntuFilesystemQuotasManager(runner=runner)
    info = manager.get_user_quota(mountpoint="/mnt/data", user="alice")

    testbase.assert_not_equal(info, None)
    testbase.assert_equal(info.principal, "alice")
    testbase.assert_equal(info.principal_kind, UbuntuQuotaPrincipalKind.USER)
    testbase.assert_equal(info.used_bytes, 1024 * 1024)
    testbase.assert_equal(info.soft_bytes, 2048 * 1024)
    testbase.assert_equal(info.hard_bytes, 4096 * 1024)
    return


def test_get_user_quota_unknown_principal_returns_none():
    """
        ``get_user_quota`` must return ``None`` when ``quota``
        reports no record for the user.
    """
    runner = _MockCommandRunner()
    _seed_quota_user_no_record(runner, user="ghost")

    manager = UbuntuFilesystemQuotasManager(runner=runner)
    info = manager.get_user_quota(mountpoint="/mnt/data", user="ghost")

    testbase.assert_equal(info, None)
    return


def test_get_group_quota_parses_quota_output():
    """
        ``get_group_quota`` must parse ``quota -g`` output.
    """
    runner = _MockCommandRunner()
    _seed_quota_group(
        runner,
        group="staff",
        mountpoint="/mnt/data",
        used_kb=512,
        soft_kb=1024,
        hard_kb=2048,
    )

    manager = UbuntuFilesystemQuotasManager(runner=runner)
    info = manager.get_group_quota(mountpoint="/mnt/data", group="staff")

    testbase.assert_not_equal(info, None)
    testbase.assert_equal(info.principal, "staff")
    testbase.assert_equal(info.principal_kind, UbuntuQuotaPrincipalKind.GROUP)
    testbase.assert_equal(info.used_bytes, 512 * 1024)
    testbase.assert_equal(info.soft_bytes, 1024 * 1024)
    testbase.assert_equal(info.hard_bytes, 2048 * 1024)
    return


# ----------------------------------------------------------------------
# list_quotas
# ----------------------------------------------------------------------


def test_list_quotas_parses_repquota_output():
    """
        ``list_quotas`` must parse ``repquota -ug`` two-section
        output into a flat list with the right principal_kind tags.
    """
    body_lines = [
        "*** Report for user quotas on device /dev/sda1",
        "Block grace time: 7days; Inode grace time: 7days",
        "                        Block limits                File limits",
        "User            used    soft    hard  grace    used  soft  hard  grace",
        "----------------------------------------------------------------------",
        "root      --       8       0       0              2     0     0",
        "alice     --   102400  204800  409600           1234     0     0",
        "*** Report for group quotas on device /dev/sda1",
        "Block grace time: 7days; Inode grace time: 7days",
        "                        Block limits                File limits",
        "Group           used    soft    hard  grace    used  soft  hard  grace",
        "----------------------------------------------------------------------",
        "staff     --      16       0       0              4     0     0",
    ]
    body = "\n".join(body_lines) + "\n"

    runner = _MockCommandRunner()
    runner.results[("repquota", "-ug", "/mnt/data")] = RunResult(
        exit_code=0,
        stdout=body,
        stderr="",
    )

    manager = UbuntuFilesystemQuotasManager(runner=runner)
    entries = manager.list_quotas(mountpoint="/mnt/data")

    testbase.assert_equal(len(entries), 3)

    user_entries = [
        entry for entry in entries
        if entry.principal_kind is UbuntuQuotaPrincipalKind.USER
    ]
    group_entries = [
        entry for entry in entries
        if entry.principal_kind is UbuntuQuotaPrincipalKind.GROUP
    ]
    testbase.assert_equal(len(user_entries), 2)
    testbase.assert_equal(len(group_entries), 1)
    testbase.assert_equal(user_entries[1].principal, "alice")
    testbase.assert_equal(user_entries[1].used_bytes, 102400 * 1024)
    testbase.assert_equal(user_entries[1].soft_bytes, 204800 * 1024)
    testbase.assert_equal(user_entries[1].hard_bytes, 409600 * 1024)
    testbase.assert_equal(group_entries[0].principal, "staff")
    return


# ----------------------------------------------------------------------
# get_quota_status
# ----------------------------------------------------------------------


def test_get_quota_status_parses_quotaon_output():
    """
        ``get_quota_status`` must parse ``quotaon -p`` output into
        the per-kind booleans.
    """
    runner = _MockCommandRunner()
    _seed_findmnt_mounted(runner, mountpoint="/mnt/data", fstype="ext4")
    _seed_quotaon_status(
        runner,
        mountpoint="/mnt/data",
        user_on=True,
        group_on=False,
    )

    manager = UbuntuFilesystemQuotasManager(runner=runner)
    status = manager.get_quota_status(mountpoint="/mnt/data")

    testbase.assert_equal(status.user_quotas_active, True)
    testbase.assert_equal(status.group_quotas_active, False)
    testbase.assert_equal(status.enabled, True)
    testbase.assert_equal(status.enforcing, True)
    testbase.assert_equal(status.filesystem_type, "ext4")
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write commands must dispatch with ``elevated=True`` when
        ``config.elevated=True``.
    """
    runner = _MockCommandRunner()
    _seed_findmnt_mounted(runner, mountpoint="/mnt/data")
    _seed_quotaon_status(
        runner,
        mountpoint="/mnt/data",
        user_on=False,
        group_on=False,
    )

    config = UbuntuFilesystemQuotasManagerConfig(elevated=True)
    manager = UbuntuFilesystemQuotasManager(runner=runner, config=config)

    manager.enable_quotas(mountpoint="/mnt/data")

    mutation_calls = [
        call for call in runner.calls if call["args"][0] in ("quotacheck", "quotaon") and call["args"][1] != "-p"
    ]
    testbase.assert_equal(len(mutation_calls), 2)
    for call in mutation_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs must dispatch with ``elevated=False`` regardless
        of ``config.elevated``.
    """
    runner = _MockCommandRunner()
    _seed_quota_user(
        runner,
        user="alice",
        mountpoint="/mnt/data",
        used_kb=1, soft_kb=2, hard_kb=3,
    )
    runner.results[("repquota", "-ug", "/mnt/data")] = RunResult(
        exit_code=0,
        stdout="",
        stderr="",
    )
    _seed_findmnt_mounted(runner, mountpoint="/mnt/data")
    _seed_quotaon_status(
        runner,
        mountpoint="/mnt/data",
        user_on=True,
        group_on=True,
    )

    config = UbuntuFilesystemQuotasManagerConfig(elevated=True)
    manager = UbuntuFilesystemQuotasManager(runner=runner, config=config)

    manager.get_user_quota(mountpoint="/mnt/data", user="alice")
    manager.list_quotas(mountpoint="/mnt/data")
    manager.get_quota_status(mountpoint="/mnt/data")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Failure surface
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failing mutation must return ``FAILED`` with the
        runner's stderr captured.
    """
    runner = _MockCommandRunner()
    _seed_findmnt_mounted(runner, mountpoint="/mnt/data")
    _seed_quotaon_status(
        runner,
        mountpoint="/mnt/data",
        user_on=False,
        group_on=False,
    )
    runner.results[("quotacheck", "-ug", "-mF", "vfsv0", "/mnt/data")] = RunResult(
        exit_code=2,
        stdout="",
        stderr="quotacheck: Cannot find filesystem to check or filesystem not mounted with quota option.",
    )

    manager = UbuntuFilesystemQuotasManager(runner=runner)
    result = manager.enable_quotas(mountpoint="/mnt/data")

    testbase.assert_equal(result.status, UbuntuQuotaActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(
        "quotacheck:" in result.stderr, True,
    )
    return


# ----------------------------------------------------------------------
# Mount validation
# ----------------------------------------------------------------------


def test_resolve_fstype_raises_when_unmounted():
    """
        Writes must raise
        :class:`UbuntuQuotaFilesystemNotMountedError` when
        ``findmnt`` fails for the supplied mountpoint.
    """
    runner = _MockCommandRunner()
    _seed_findmnt_unmounted(runner, mountpoint="/mnt/missing")

    manager = UbuntuFilesystemQuotasManager(runner=runner)

    raised = False
    try:
        manager.enable_quotas(mountpoint="/mnt/missing")
    except UbuntuQuotaFilesystemNotMountedError:
        raised = True

    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# Pure-parser tests
# ----------------------------------------------------------------------


def test_parse_quotaon_status_handles_mixed():
    """
        ``_parse_quotaon_status`` must report the right booleans
        for mixed on/off output.
    """
    body = (
        "user quota on /mnt (/dev/sda1) is on\n"
        "group quota on /mnt (/dev/sda1) is off\n"
    )
    user_active, group_active = _parse_quotaon_status(stdout=body)
    testbase.assert_equal(user_active, True)
    testbase.assert_equal(group_active, False)
    return
