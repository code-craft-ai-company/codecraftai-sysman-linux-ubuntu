"""
    Tests for :class:`UbuntuFilesystemManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    No live ``mount`` / ``findmnt`` / ``stat`` is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.filesystemmanagement import (
    UbuntuFilesystemActionStatus,
    UbuntuFilesystemManager,
    UbuntuFilesystemManagerConfig,
    UbuntuFstabEntry,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv tuple.
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


def _findmnt_mounted(*, device: str, mountpoint: str, fstype: str = "ext4") -> RunResult:
    """
        Build a ``findmnt --real --json`` result with one mount.
    """
    payload = json.dumps({
        "filesystems": [
            {
                "source": device,
                "target": mountpoint,
                "fstype": fstype,
                "options": "rw,relatime",
                "children": [],
            },
        ],
    })
    rtnval = RunResult(exit_code=0, stdout=payload, stderr="")
    return rtnval


def _findmnt_empty() -> RunResult:
    """
        Build a ``findmnt --real --json`` result with no mounts.
    """
    rtnval = RunResult(
        exit_code=0,
        stdout=json.dumps({"filesystems": []}),
        stderr="",
    )
    return rtnval


def _stat_output(
    *,
    owner: str = "root",
    group: str = "root",
    mode: str = "644",
    size: int = 1024,
    modified: str = "2024-01-01 12:00:00.000000000 +0000",
) -> RunResult:
    """
        Build a ``stat -c '%U:%G:%a:%s:%y'`` result.
    """
    body = "{}:{}:{}:{}:{}\n".format(owner, group, mode, size, modified)
    rtnval = RunResult(exit_code=0, stdout=body, stderr="")
    return rtnval


# ----------------------------------------------------------------------
# mount
# ----------------------------------------------------------------------


def test_mount_constructs_argv_with_options():
    """
        mount emits ``mount -t <fstype> -o <options> <device> <mp>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("findmnt", "--real", "--json")] = _findmnt_empty()

    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(elevated=True),
    )
    result = manager.mount(
        device="/dev/sdb1",
        mountpoint="/data",
        fstype="ext4",
        options="rw,noatime",
    )

    testbase.assert_equal(result.status, UbuntuFilesystemActionStatus.SUCCESS)
    mount_calls = [c for c in runner.calls if c["args"][0] == "mount"]
    testbase.assert_equal(len(mount_calls), 1)
    testbase.assert_equal(
        mount_calls[0]["args"],
        ["mount", "-t", "ext4", "-o", "rw,noatime", "/dev/sdb1", "/data"],
    )
    testbase.assert_equal(mount_calls[0]["elevated"], True)
    return


def test_mount_skipped_when_already_mounted():
    """
        mount returns SKIPPED when the same device is already mounted
        at the mountpoint.
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "--real", "--json")] = _findmnt_mounted(
        device="/dev/sdb1", mountpoint="/data",
    )

    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(elevated=True),
    )
    result = manager.mount(
        device="/dev/sdb1",
        mountpoint="/data",
        fstype="ext4",
    )

    testbase.assert_equal(result.status, UbuntuFilesystemActionStatus.SKIPPED)
    mount_calls = [c for c in runner.calls if c["args"][0] == "mount"]
    testbase.assert_equal(mount_calls, [])
    return


# ----------------------------------------------------------------------
# unmount
# ----------------------------------------------------------------------


def test_unmount_dispatches_umount():
    """
        unmount emits ``umount <mountpoint>`` when the mountpoint is
        currently mounted.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("findmnt", "--real", "--json")] = _findmnt_mounted(
        device="/dev/sdb1", mountpoint="/data",
    )

    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(elevated=True),
    )
    result = manager.unmount(mountpoint="/data")

    testbase.assert_equal(result.status, UbuntuFilesystemActionStatus.SUCCESS)
    umount_calls = [c for c in runner.calls if c["args"][0] == "umount"]
    testbase.assert_equal(len(umount_calls), 1)
    testbase.assert_equal(umount_calls[0]["args"], ["umount", "/data"])
    testbase.assert_equal(umount_calls[0]["elevated"], True)
    return


def test_unmount_skipped_when_not_mounted():
    """
        unmount returns SKIPPED when the mountpoint is not mounted.
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "--real", "--json")] = _findmnt_empty()

    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(elevated=True),
    )
    result = manager.unmount(mountpoint="/data")

    testbase.assert_equal(result.status, UbuntuFilesystemActionStatus.SKIPPED)
    umount_calls = [c for c in runner.calls if c["args"][0] == "umount"]
    testbase.assert_equal(umount_calls, [])
    return


# ----------------------------------------------------------------------
# add_fstab_entry / remove_fstab_entry
# ----------------------------------------------------------------------


def test_add_fstab_entry_appends_when_missing():
    """
        add_fstab_entry appends a tab-separated line via ``tee -a``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("cat", "/etc/fstab")] = RunResult(
        exit_code=0,
        stdout="# header\nUUID=root /                ext4    defaults 0 1\n",
        stderr="",
    )

    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(elevated=True),
    )
    entry = UbuntuFstabEntry(
        device="/dev/sdb1",
        mountpoint="/data",
        fstype="ext4",
        options="defaults",
    )
    result = manager.add_fstab_entry(spec=entry)

    testbase.assert_equal(result.status, UbuntuFilesystemActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["args"], ["tee", "-a", "/etc/fstab"])
    expected = "/dev/sdb1\t/data\text4\tdefaults\t0\t0\n"
    testbase.assert_equal(tee_calls[0]["stdin"], expected)
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


def test_add_fstab_entry_skipped_when_match_present():
    """
        add_fstab_entry returns SKIPPED when an existing line matches
        device, mountpoint, fstype, and options.
    """
    existing = (
        "# header\n"
        "UUID=root /     ext4    defaults 0 1\n"
        "/dev/sdb1\t/data\text4\tdefaults\t0\t0\n"
    )
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/fstab")] = RunResult(
        exit_code=0, stdout=existing, stderr="",
    )

    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(elevated=True),
    )
    entry = UbuntuFstabEntry(
        device="/dev/sdb1",
        mountpoint="/data",
        fstype="ext4",
        options="defaults",
    )
    result = manager.add_fstab_entry(spec=entry)

    testbase.assert_equal(result.status, UbuntuFilesystemActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


def test_remove_fstab_entry_filters_matching_mountpoint():
    """
        remove_fstab_entry rewrites fstab via ``tee`` excluding the
        matching mountpoint line and preserves comments and unrelated
        entries.
    """
    existing = (
        "# the system fstab\n"
        "UUID=root /     ext4    defaults 0 1\n"
        "/dev/sdb1 /data ext4 defaults 0 0\n"
        "/dev/sdc1 /backup ext4 defaults 0 0\n"
    )
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("cat", "/etc/fstab")] = RunResult(
        exit_code=0, stdout=existing, stderr="",
    )

    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(elevated=True),
    )
    result = manager.remove_fstab_entry(mountpoint="/data")

    testbase.assert_equal(result.status, UbuntuFilesystemActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["args"], ["tee", "/etc/fstab"])
    testbase.assert_true("/data" not in tee_calls[0]["stdin"])
    testbase.assert_true("# the system fstab" in tee_calls[0]["stdin"])
    testbase.assert_true("/backup" in tee_calls[0]["stdin"])
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


def test_remove_fstab_entry_skipped_when_no_match():
    """
        remove_fstab_entry returns SKIPPED when no line matches.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/fstab")] = RunResult(
        exit_code=0, stdout="UUID=root / ext4 defaults 0 1\n", stderr="",
    )

    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(elevated=True),
    )
    result = manager.remove_fstab_entry(mountpoint="/data")

    testbase.assert_equal(result.status, UbuntuFilesystemActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


# ----------------------------------------------------------------------
# get_mounts
# ----------------------------------------------------------------------


def test_get_mounts_parses_findmnt_json_with_children():
    """
        get_mounts walks every ``children`` collection breadth-first.
    """
    payload = json.dumps({
        "filesystems": [
            {
                "source": "/dev/sda1",
                "target": "/",
                "fstype": "ext4",
                "options": "rw",
                "children": [
                    {
                        "source": "/dev/sdb1",
                        "target": "/data",
                        "fstype": "ext4",
                        "options": "rw,noatime",
                        "children": [],
                    },
                ],
            },
        ],
    })
    runner = _MockCommandRunner()
    runner.results[("findmnt", "--real", "--json")] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )

    manager = UbuntuFilesystemManager(
        runner=runner, config=UbuntuFilesystemManagerConfig(),
    )
    mounts = manager.get_mounts()

    by_target = {m.mountpoint: m for m in mounts}
    testbase.assert_true("/" in by_target)
    testbase.assert_true("/data" in by_target)
    testbase.assert_equal(by_target["/data"].device, "/dev/sdb1")
    testbase.assert_equal(by_target["/data"].fstype, "ext4")
    testbase.assert_equal(by_target["/data"].options, "rw,noatime")
    return


def test_get_mounts_returns_empty_on_findmnt_failure():
    """
        get_mounts returns an empty list when ``findmnt`` exits
        non-zero (informational; no exception).
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "--real", "--json")] = RunResult(
        exit_code=1, stdout="", stderr="findmnt: not found",
    )

    manager = UbuntuFilesystemManager(
        runner=runner, config=UbuntuFilesystemManagerConfig(),
    )
    mounts = manager.get_mounts()

    testbase.assert_equal(mounts, [])
    return


# ----------------------------------------------------------------------
# stat
# ----------------------------------------------------------------------


def test_stat_parses_colon_separated_output():
    """
        stat parses ``%U:%G:%a:%s:%y`` into a :class:`UbuntuFileInfo`.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "stat", "-c", "%U:%G:%a:%s:%y", "/etc/app.conf",
    )] = _stat_output(
        owner="app",
        group="app",
        mode="644",
        size=2048,
        modified="2024-04-01 09:30:00.000000000 +0000",
    )

    manager = UbuntuFilesystemManager(
        runner=runner, config=UbuntuFilesystemManagerConfig(),
    )
    info = manager.stat(path="/etc/app.conf")

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.owner, "app")
    testbase.assert_equal(info.group, "app")
    testbase.assert_equal(info.mode, "644")
    testbase.assert_equal(info.size, 2048)
    testbase.assert_equal(
        info.modified, "2024-04-01 09:30:00.000000000 +0000",
    )
    return


def test_stat_returns_none_for_missing_path():
    """
        stat returns None when the path does not exist.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "stat", "-c", "%U:%G:%a:%s:%y", "/missing/path",
    )] = RunResult(
        exit_code=1, stdout="", stderr="stat: cannot statx '/missing/path': No such file or directory",
    )

    manager = UbuntuFilesystemManager(
        runner=runner, config=UbuntuFilesystemManagerConfig(),
    )
    info = manager.stat(path="/missing/path")

    testbase.assert_true(info is None)
    return


# ----------------------------------------------------------------------
# chown / chmod
# ----------------------------------------------------------------------


def test_chown_dispatches_user_group_shorthand():
    """
        chown emits ``chown user:group <path>`` when both are provided.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "stat", "-c", "%U:%G:%a:%s:%y", "/srv/app",
    )] = _stat_output(owner="root", group="root")

    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(elevated=True),
    )
    result = manager.chown(path="/srv/app", user="app", group="app")

    testbase.assert_equal(result.status, UbuntuFilesystemActionStatus.SUCCESS)
    chown_calls = [c for c in runner.calls if c["args"][0] == "chown"]
    testbase.assert_equal(len(chown_calls), 1)
    testbase.assert_equal(
        chown_calls[0]["args"], ["chown", "app:app", "/srv/app"],
    )
    testbase.assert_equal(chown_calls[0]["elevated"], True)
    return


def test_chown_skipped_when_already_matches():
    """
        chown returns SKIPPED when the path's stat already reports the
        target user and group.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "stat", "-c", "%U:%G:%a:%s:%y", "/srv/app",
    )] = _stat_output(owner="app", group="app")

    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(elevated=True),
    )
    result = manager.chown(path="/srv/app", user="app", group="app")

    testbase.assert_equal(result.status, UbuntuFilesystemActionStatus.SKIPPED)
    chown_calls = [c for c in runner.calls if c["args"][0] == "chown"]
    testbase.assert_equal(chown_calls, [])
    return


def test_chmod_dispatches_with_mode_token_verbatim():
    """
        chmod emits ``chmod <mode> <path>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "stat", "-c", "%U:%G:%a:%s:%y", "/srv/app",
    )] = _stat_output(mode="755")

    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(elevated=True),
    )
    result = manager.chmod(path="/srv/app", mode="0644")

    testbase.assert_equal(result.status, UbuntuFilesystemActionStatus.SUCCESS)
    chmod_calls = [c for c in runner.calls if c["args"][0] == "chmod"]
    testbase.assert_equal(len(chmod_calls), 1)
    testbase.assert_equal(chmod_calls[0]["args"], ["chmod", "0644", "/srv/app"])
    testbase.assert_equal(chmod_calls[0]["elevated"], True)
    return


def test_chmod_skipped_when_already_matches():
    """
        chmod returns SKIPPED when the path's mode already matches.
        ``stat`` reports ``%a`` without a leading zero so ``"644"``
        and ``"0644"`` must be treated equivalently.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "stat", "-c", "%U:%G:%a:%s:%y", "/srv/app",
    )] = _stat_output(mode="644")

    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(elevated=True),
    )
    result = manager.chmod(path="/srv/app", mode="0644")

    testbase.assert_equal(result.status, UbuntuFilesystemActionStatus.SKIPPED)
    chmod_calls = [c for c in runner.calls if c["args"][0] == "chmod"]
    testbase.assert_equal(chmod_calls, [])
    return


def test_chmod_recursive_always_runs():
    """
        chmod with ``recursive=True`` always dispatches ``chmod -R``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "stat", "-c", "%U:%G:%a:%s:%y", "/srv/app",
    )] = _stat_output(mode="644")

    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(elevated=True),
    )
    result = manager.chmod(path="/srv/app", mode="0644", recursive=True)

    testbase.assert_equal(result.status, UbuntuFilesystemActionStatus.SUCCESS)
    chmod_calls = [c for c in runner.calls if c["args"][0] == "chmod"]
    testbase.assert_equal(len(chmod_calls), 1)
    testbase.assert_equal(
        chmod_calls[0]["args"], ["chmod", "-R", "0644", "/srv/app"],
    )
    return


# ----------------------------------------------------------------------
# Dry-run / elevation / failure surfacing
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_mount():
    """
        dry-run returns DRY_RUN without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(dry_run=True),
    )
    result = manager.mount(
        device="/dev/sdb1",
        mountpoint="/data",
        fstype="ext4",
    )

    testbase.assert_equal(result.status, UbuntuFilesystemActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command,
        ["mount", "-t", "ext4", "/dev/sdb1", "/data"],
    )
    return


def test_reads_never_pass_elevated_even_when_configured():
    """
        Reads must never elevate, even when the manager is configured
        with ``elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "--real", "--json")] = _findmnt_empty()
    runner.results[(
        "stat", "-c", "%U:%G:%a:%s:%y", "/srv/app",
    )] = _stat_output()
    runner.results[("cat", "/etc/fstab")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(elevated=True),
    )
    manager.get_mounts()
    manager.stat(path="/srv/app")
    manager._read_fstab()

    read_heads = (
        ("findmnt",),
        ("stat",),
        ("cat",),
    )
    for call in runner.calls:
        argv = tuple(call["args"])
        for head in read_heads:
            if argv[: len(head)] == head:
                testbase.assert_equal(call["elevated"], False)
                break
    return


def test_failed_command_surfaces_stderr_and_exit_code():
    """
        A non-zero ``mount`` exit maps to FAILED with the captured
        stderr / exit_code / command.
    """
    runner = _MockCommandRunner()
    runner.results[("findmnt", "--real", "--json")] = _findmnt_empty()
    runner.results[(
        "mount", "-t", "ext4", "/dev/sdb1", "/data",
    )] = RunResult(
        exit_code=32,
        stdout="",
        stderr="mount: /data: special device /dev/sdb1 does not exist.",
    )

    manager = UbuntuFilesystemManager(
        runner=runner,
        config=UbuntuFilesystemManagerConfig(elevated=True),
    )
    result = manager.mount(
        device="/dev/sdb1",
        mountpoint="/data",
        fstype="ext4",
    )

    testbase.assert_equal(result.status, UbuntuFilesystemActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 32)
    testbase.assert_true("/dev/sdb1 does not exist" in result.stderr)
    testbase.assert_equal(
        result.command, ["mount", "-t", "ext4", "/dev/sdb1", "/data"],
    )
    return
