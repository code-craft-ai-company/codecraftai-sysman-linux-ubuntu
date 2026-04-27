"""
    Tests for :class:`UbuntuVirtualizationManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  Reads (``virsh dominfo``, ``virsh dumpxml``,
    ``virsh snapshot-list``, ``virsh list``) are exercised via seeded
    payloads; no live ``virsh`` / ``virt-install`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.virtualizationmanagement import (
    UbuntuVirtBackend,
    UbuntuVirtualizationActionStatus,
    UbuntuVirtualizationManager,
    UbuntuVirtualizationManagerConfig,
    UbuntuVirtualizationQueryError,
    UbuntuVmDiskSpec,
    UbuntuVmSpec,
    UbuntuVmState,
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


_DOMINFO_RUNNING = (
    "Id:             3\n"
    "Name:           webvm\n"
    "UUID:           11111111-2222-3333-4444-555555555555\n"
    "OS Type:        hvm\n"
    "State:          running\n"
    "CPU(s):         2\n"
    "CPU time:       12.3s\n"
    "Max memory:     2097152 KiB\n"
    "Used memory:    1048576 KiB\n"
    "Persistent:     yes\n"
    "Autostart:      disable\n"
    "Managed save:   no\n"
)

_DOMINFO_STOPPED = (
    "Id:             -\n"
    "Name:           webvm\n"
    "UUID:           11111111-2222-3333-4444-555555555555\n"
    "OS Type:        hvm\n"
    "State:          shut off\n"
    "CPU(s):         2\n"
    "CPU time:       0.0s\n"
    "Max memory:     2097152 KiB\n"
    "Used memory:    0 KiB\n"
    "Persistent:     yes\n"
    "Autostart:      disable\n"
    "Managed save:   no\n"
)

_DOMINFO_PAUSED = (
    "Id:             4\n"
    "Name:           webvm\n"
    "UUID:           11111111-2222-3333-4444-555555555555\n"
    "OS Type:        hvm\n"
    "State:          paused\n"
    "CPU(s):         2\n"
    "Max memory:     2097152 KiB\n"
    "Used memory:    1048576 KiB\n"
)

_DUMPXML_WITH_DISK = (
    "<domain type='kvm'>\n"
    "  <name>webvm</name>\n"
    "  <devices>\n"
    "    <disk type='file' device='disk'>\n"
    "      <driver name='qemu' type='qcow2'/>\n"
    "      <source file='/var/lib/libvirt/images/webvm.qcow2'/>\n"
    "      <target dev='vda' bus='virtio'/>\n"
    "    </disk>\n"
    "  </devices>\n"
    "</domain>\n"
)

_DUMPXML_NO_DISK = (
    "<domain type='kvm'>\n"
    "  <name>webvm</name>\n"
    "  <devices/>\n"
    "</domain>\n"
)

_SNAPSHOT_LIST_TEXT = (
    " Name        Creation Time              State\n"
    "------------------------------------------------------------\n"
    " preupgrade  2024-01-01 12:00:00 +0000  shutoff\n"
    " checkpoint  2024-02-15 09:30:11 +0000  running\n"
)

_SNAPSHOT_LIST_EMPTY = (
    " Name        Creation Time              State\n"
    "------------------------------------------------------------\n"
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _virsh_argv(*tokens: str) -> tuple:
    """
        Produce a tuple suitable for indexing ``runner.results`` for a
        ``virsh`` invocation that includes the default ``--connect``
        flag.
    """
    rtnval = ("virsh", "--connect", "qemu:///system") + tuple(tokens)
    return rtnval


def _make_manager(
    *,
    runner: _MockCommandRunner,
    elevated: bool = True,
    dry_run: bool = False,
    backend: UbuntuVirtBackend = UbuntuVirtBackend.LIBVIRT,
) -> UbuntuVirtualizationManager:
    """
        Construct a manager with the canonical config used in tests.
    """
    config = UbuntuVirtualizationManagerConfig(
        dry_run=dry_run,
        elevated=elevated,
        backend=backend,
    )
    rtnval = UbuntuVirtualizationManager(runner=runner, config=config)
    return rtnval


def _seed_dominfo_running(runner: _MockCommandRunner, name: str) -> None:
    """
        Seed ``virsh dominfo <name>`` to report a running VM.
    """
    runner.results[_virsh_argv("dominfo", name)] = RunResult(
        exit_code=0, stdout=_DOMINFO_RUNNING, stderr="",
    )
    return


def _seed_dominfo_stopped(runner: _MockCommandRunner, name: str) -> None:
    """
        Seed ``virsh dominfo <name>`` to report a stopped VM.
    """
    runner.results[_virsh_argv("dominfo", name)] = RunResult(
        exit_code=0, stdout=_DOMINFO_STOPPED, stderr="",
    )
    return


def _seed_dominfo_paused(runner: _MockCommandRunner, name: str) -> None:
    """
        Seed ``virsh dominfo <name>`` to report a paused VM.
    """
    runner.results[_virsh_argv("dominfo", name)] = RunResult(
        exit_code=0, stdout=_DOMINFO_PAUSED, stderr="",
    )
    return


def _seed_dominfo_missing(runner: _MockCommandRunner, name: str) -> None:
    """
        Seed ``virsh dominfo <name>`` to report a missing VM.
    """
    runner.results[_virsh_argv("dominfo", name)] = RunResult(
        exit_code=1,
        stdout="",
        stderr="error: failed to get domain '{}'".format(name),
    )
    return


# ----------------------------------------------------------------------
# list_vms
# ----------------------------------------------------------------------


def test_list_vms_parses_virsh_output():
    """
        list_vms parses ``virsh list --all --name`` and joins per-VM
        ``virsh dominfo`` payloads.
    """
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("list", "--all", "--name")] = RunResult(
        exit_code=0, stdout="webvm\nbatchvm\n\n", stderr="",
    )
    runner.results[_virsh_argv("dominfo", "webvm")] = RunResult(
        exit_code=0, stdout=_DOMINFO_RUNNING, stderr="",
    )
    runner.results[_virsh_argv("dominfo", "batchvm")] = RunResult(
        exit_code=0, stdout=_DOMINFO_STOPPED, stderr="",
    )

    manager = _make_manager(runner=runner)
    rows = manager.list_vms()

    testbase.assert_equal(len(rows), 2)
    by_name = {row.name: row for row in rows}
    testbase.assert_equal(by_name["webvm"].state, UbuntuVmState.RUNNING)
    testbase.assert_equal(by_name["webvm"].cpu_count, 2)
    testbase.assert_equal(by_name["webvm"].memory_mb, 2048)
    testbase.assert_equal(
        by_name["webvm"].vm_id,
        "11111111-2222-3333-4444-555555555555",
    )
    testbase.assert_equal(by_name["batchvm"].state, UbuntuVmState.STOPPED)
    return


def test_list_vms_raises_on_unexpected_failure():
    """
        list_vms raises :class:`UbuntuVirtualizationQueryError` when
        ``virsh list`` exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("list", "--all", "--name")] = RunResult(
        exit_code=1, stdout="", stderr="connect failed",
    )

    manager = _make_manager(runner=runner)
    raised = False
    try:
        manager.list_vms()
    except UbuntuVirtualizationQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_reads_never_elevate():
    """
        Read verbs (``list_vms``, ``get_vm_info``, ``list_snapshots``)
        always pass ``elevated=False`` to the runner, regardless of
        ``config.elevated``.
    """
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("list", "--all", "--name")] = RunResult(
        exit_code=0, stdout="webvm\n", stderr="",
    )
    runner.results[_virsh_argv("dominfo", "webvm")] = RunResult(
        exit_code=0, stdout=_DOMINFO_RUNNING, stderr="",
    )
    runner.results[_virsh_argv("snapshot-list", "webvm")] = RunResult(
        exit_code=0, stdout=_SNAPSHOT_LIST_TEXT, stderr="",
    )

    manager = _make_manager(runner=runner, elevated=True)
    manager.list_vms()
    manager.get_vm_info(name="webvm")
    manager.list_snapshots(vm="webvm")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# get_vm_info
# ----------------------------------------------------------------------


def test_get_vm_info_returns_none_for_unknown_vm():
    """
        get_vm_info returns None when the VM is not defined.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_missing(runner=runner, name="ghostvm")

    manager = _make_manager(runner=runner)
    result = manager.get_vm_info(name="ghostvm")
    testbase.assert_equal(result, None)
    return


# ----------------------------------------------------------------------
# create_vm
# ----------------------------------------------------------------------


def test_create_vm_invokes_virt_install():
    """
        create_vm dispatches ``virt-install`` with the rendered argv and
        passes ``elevated=True`` when configured.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_missing(runner=runner, name="webvm")

    manager = _make_manager(runner=runner, elevated=True)
    spec = UbuntuVmSpec(
        name="webvm",
        cpu_count=2,
        memory_mb=2048,
        disk_size_gb=20,
        os_variant="ubuntu22.04",
        network="network=default",
        graphics="none",
    )
    result = manager.create_vm(spec=spec)

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SUCCESS)

    install_calls = [
        c for c in runner.calls if c["args"][0] == "virt-install"
    ]
    testbase.assert_equal(len(install_calls), 1)
    argv = install_calls[0]["args"]
    testbase.assert_equal("--name" in argv, True)
    testbase.assert_equal(argv[argv.index("--name") + 1], "webvm")
    testbase.assert_equal(argv[argv.index("--memory") + 1], "2048")
    testbase.assert_equal(argv[argv.index("--vcpus") + 1], "2")
    testbase.assert_equal(argv[argv.index("--disk") + 1], "size=20")
    testbase.assert_equal(
        argv[argv.index("--os-variant") + 1], "ubuntu22.04",
    )
    testbase.assert_equal(
        argv[argv.index("--network") + 1], "network=default",
    )
    testbase.assert_equal(argv[argv.index("--graphics") + 1], "none")
    testbase.assert_equal("--noautoconsole" in argv, True)
    testbase.assert_equal(install_calls[0]["elevated"], True)
    return


def test_create_vm_skipped_when_present():
    """
        create_vm returns SKIPPED when a domain with the same name
        already exists.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_running(runner=runner, name="webvm")

    manager = _make_manager(runner=runner)
    spec = UbuntuVmSpec(name="webvm", memory_mb=2048, cpu_count=2)
    result = manager.create_vm(spec=spec)

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SKIPPED)
    install_calls = [
        c for c in runner.calls if c["args"][0] == "virt-install"
    ]
    testbase.assert_equal(install_calls, [])
    return


def test_create_vm_dry_run():
    """
        create_vm returns DRY_RUN and never invokes the runner.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, dry_run=True)
    spec = UbuntuVmSpec(name="webvm", memory_mb=1024, cpu_count=1)
    result = manager.create_vm(spec=spec)

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(result.command[0], "virt-install")
    return


# ----------------------------------------------------------------------
# delete_vm
# ----------------------------------------------------------------------


def test_delete_vm_invokes_virsh_undefine_and_carries_advisory():
    """
        delete_vm dispatches ``virsh undefine --remove-all-storage`` and
        records a data-loss advisory on success.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_stopped(runner=runner, name="webvm")

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.delete_vm(name="webvm")

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SUCCESS)
    undefine_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["virsh"] and "undefine" in c["args"]
    ]
    testbase.assert_equal(len(undefine_calls), 1)
    testbase.assert_equal("--remove-all-storage" in undefine_calls[0]["args"], True)
    testbase.assert_equal(undefine_calls[0]["elevated"], True)
    testbase.assert_equal(result.message is not None, True)
    testbase.assert_equal("destroyed" in result.message, True)
    return


def test_delete_vm_skipped_when_absent():
    """
        delete_vm returns SKIPPED when the VM is not defined.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_missing(runner=runner, name="ghostvm")

    manager = _make_manager(runner=runner)
    result = manager.delete_vm(name="ghostvm")

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# start_vm / stop_vm / pause_vm / resume_vm
# ----------------------------------------------------------------------


def test_start_vm_skipped_when_running():
    """
        start_vm returns SKIPPED when the VM is already running.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_running(runner=runner, name="webvm")

    manager = _make_manager(runner=runner)
    result = manager.start_vm(name="webvm")

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SKIPPED)
    start_calls = [c for c in runner.calls if "start" in c["args"]]
    testbase.assert_equal(start_calls, [])
    return


def test_start_vm_invokes_virsh_when_stopped():
    """
        start_vm dispatches ``virsh start`` when the VM is stopped.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_stopped(runner=runner, name="webvm")

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.start_vm(name="webvm")

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SUCCESS)
    start_calls = [
        c for c in runner.calls
        if c["args"] == list(_virsh_argv("start", "webvm"))
    ]
    testbase.assert_equal(len(start_calls), 1)
    testbase.assert_equal(start_calls[0]["elevated"], True)
    return


def test_stop_vm_force_invokes_virsh_destroy():
    """
        stop_vm with ``force=True`` dispatches ``virsh destroy``;
        ``force=False`` dispatches ``virsh shutdown``.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_running(runner=runner, name="webvm")

    manager = _make_manager(runner=runner, elevated=True)
    forced = manager.stop_vm(name="webvm", force=True)
    testbase.assert_equal(forced.status, UbuntuVirtualizationActionStatus.SUCCESS)
    destroy_calls = [
        c for c in runner.calls
        if c["args"] == list(_virsh_argv("destroy", "webvm"))
    ]
    testbase.assert_equal(len(destroy_calls), 1)

    shutdown_calls = [
        c for c in runner.calls
        if c["args"] == list(_virsh_argv("shutdown", "webvm"))
    ]
    testbase.assert_equal(shutdown_calls, [])
    return


def test_stop_vm_graceful_invokes_virsh_shutdown():
    """
        stop_vm with the default ``force=False`` dispatches ``virsh
        shutdown``.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_running(runner=runner, name="webvm")

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.stop_vm(name="webvm", force=False)

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SUCCESS)
    shutdown_calls = [
        c for c in runner.calls
        if c["args"] == list(_virsh_argv("shutdown", "webvm"))
    ]
    testbase.assert_equal(len(shutdown_calls), 1)
    return


def test_stop_vm_skipped_when_already_stopped():
    """
        stop_vm returns SKIPPED when the VM is already stopped.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_stopped(runner=runner, name="webvm")

    manager = _make_manager(runner=runner)
    result = manager.stop_vm(name="webvm", force=True)

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SKIPPED)
    return


def test_pause_vm_invokes_virsh_suspend():
    """
        pause_vm dispatches ``virsh suspend``.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_running(runner=runner, name="webvm")

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.pause_vm(name="webvm")

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SUCCESS)
    suspend_calls = [
        c for c in runner.calls
        if c["args"] == list(_virsh_argv("suspend", "webvm"))
    ]
    testbase.assert_equal(len(suspend_calls), 1)
    testbase.assert_equal(suspend_calls[0]["elevated"], True)
    return


def test_pause_vm_skipped_when_already_paused():
    """
        pause_vm returns SKIPPED when the VM is already paused.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_paused(runner=runner, name="webvm")

    manager = _make_manager(runner=runner)
    result = manager.pause_vm(name="webvm")

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SKIPPED)
    return


def test_resume_vm_invokes_virsh_resume():
    """
        resume_vm dispatches ``virsh resume`` when the VM is paused.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_paused(runner=runner, name="webvm")

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.resume_vm(name="webvm")

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SUCCESS)
    resume_calls = [
        c for c in runner.calls
        if c["args"] == list(_virsh_argv("resume", "webvm"))
    ]
    testbase.assert_equal(len(resume_calls), 1)
    testbase.assert_equal(resume_calls[0]["elevated"], True)
    return


def test_resume_vm_skipped_when_running():
    """
        resume_vm returns SKIPPED when the VM is already running.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_running(runner=runner, name="webvm")

    manager = _make_manager(runner=runner)
    result = manager.resume_vm(name="webvm")

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# attach_disk / detach_disk
# ----------------------------------------------------------------------


def test_attach_disk_invokes_virsh_attach_disk():
    """
        attach_disk dispatches ``virsh attach-disk`` with the correct
        argv and elevation.
    """
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("dumpxml", "webvm")] = RunResult(
        exit_code=0, stdout=_DUMPXML_NO_DISK, stderr="",
    )

    manager = _make_manager(runner=runner, elevated=True)
    disk = UbuntuVmDiskSpec(
        path="/var/lib/libvirt/images/extra.qcow2",
        target="vdb",
        bus="virtio",
    )
    result = manager.attach_disk(vm="webvm", disk_spec=disk)

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SUCCESS)
    attach_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["virsh"] and "attach-disk" in c["args"]
    ]
    testbase.assert_equal(len(attach_calls), 1)
    argv = attach_calls[0]["args"]
    testbase.assert_equal("/var/lib/libvirt/images/extra.qcow2" in argv, True)
    testbase.assert_equal("vdb" in argv, True)
    testbase.assert_equal("--persistent" in argv, True)
    testbase.assert_equal("--targetbus" in argv, True)
    testbase.assert_equal(attach_calls[0]["elevated"], True)
    return


def test_attach_disk_skipped_when_attached():
    """
        attach_disk returns SKIPPED when the backing path is already
        present in the VM XML.
    """
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("dumpxml", "webvm")] = RunResult(
        exit_code=0, stdout=_DUMPXML_WITH_DISK, stderr="",
    )

    manager = _make_manager(runner=runner)
    disk = UbuntuVmDiskSpec(
        path="/var/lib/libvirt/images/webvm.qcow2",
        target="vda",
    )
    result = manager.attach_disk(vm="webvm", disk_spec=disk)

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SKIPPED)
    attach_calls = [
        c for c in runner.calls if "attach-disk" in c["args"]
    ]
    testbase.assert_equal(attach_calls, [])
    return


def test_detach_disk_invokes_virsh_detach_disk():
    """
        detach_disk dispatches ``virsh detach-disk`` with the target
        label and ``--persistent``, plus a data-loss advisory.
    """
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("dumpxml", "webvm")] = RunResult(
        exit_code=0, stdout=_DUMPXML_WITH_DISK, stderr="",
    )

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.detach_disk(vm="webvm", disk_id="vda")

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SUCCESS)
    detach_calls = [
        c for c in runner.calls if "detach-disk" in c["args"]
    ]
    testbase.assert_equal(len(detach_calls), 1)
    testbase.assert_equal("--persistent" in detach_calls[0]["args"], True)
    testbase.assert_equal(detach_calls[0]["elevated"], True)
    testbase.assert_equal(result.message is not None, True)
    testbase.assert_equal("detached" in result.message, True)
    return


def test_detach_disk_skipped_when_not_attached():
    """
        detach_disk returns SKIPPED when the disk id does not appear in
        the VM XML.
    """
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("dumpxml", "webvm")] = RunResult(
        exit_code=0, stdout=_DUMPXML_NO_DISK, stderr="",
    )

    manager = _make_manager(runner=runner)
    result = manager.detach_disk(vm="webvm", disk_id="vdz")

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# Snapshots
# ----------------------------------------------------------------------


def test_snapshot_vm_invokes_virsh_snapshot_create_as():
    """
        snapshot_vm dispatches ``virsh snapshot-create-as``.
    """
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("snapshot-list", "webvm")] = RunResult(
        exit_code=0, stdout=_SNAPSHOT_LIST_EMPTY, stderr="",
    )

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.snapshot_vm(vm="webvm", snapshot_name="preupgrade")

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SUCCESS)
    create_calls = [
        c for c in runner.calls if "snapshot-create-as" in c["args"]
    ]
    testbase.assert_equal(len(create_calls), 1)
    testbase.assert_equal(create_calls[0]["elevated"], True)
    testbase.assert_equal(result.snapshot_name, "preupgrade")
    return


def test_snapshot_vm_skipped_when_named_snapshot_exists():
    """
        snapshot_vm returns SKIPPED when a snapshot with the same name
        already exists.
    """
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("snapshot-list", "webvm")] = RunResult(
        exit_code=0, stdout=_SNAPSHOT_LIST_TEXT, stderr="",
    )

    manager = _make_manager(runner=runner)
    result = manager.snapshot_vm(vm="webvm", snapshot_name="preupgrade")

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SKIPPED)
    return


def test_restore_snapshot_invokes_virsh_snapshot_revert():
    """
        restore_snapshot dispatches ``virsh snapshot-revert`` and
        records the data-loss advisory.
    """
    runner = _MockCommandRunner()

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.restore_snapshot(
        vm="webvm", snapshot_name="preupgrade",
    )

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.SUCCESS)
    revert_calls = [
        c for c in runner.calls if "snapshot-revert" in c["args"]
    ]
    testbase.assert_equal(len(revert_calls), 1)
    testbase.assert_equal(revert_calls[0]["elevated"], True)
    testbase.assert_equal(result.message is not None, True)
    testbase.assert_equal("reverted" in result.message, True)
    return


def test_list_snapshots_parses_virsh_snapshot_list_output():
    """
        list_snapshots parses ``virsh snapshot-list`` output into
        :class:`UbuntuSnapshotInfo` rows.
    """
    runner = _MockCommandRunner()
    runner.results[_virsh_argv("snapshot-list", "webvm")] = RunResult(
        exit_code=0, stdout=_SNAPSHOT_LIST_TEXT, stderr="",
    )

    manager = _make_manager(runner=runner)
    rows = manager.list_snapshots(vm="webvm")

    testbase.assert_equal(len(rows), 2)
    by_name = {row.name: row for row in rows}
    testbase.assert_equal(
        by_name["preupgrade"].vm_state, UbuntuVmState.STOPPED,
    )
    testbase.assert_equal(
        by_name["checkpoint"].vm_state, UbuntuVmState.RUNNING,
    )
    testbase.assert_equal(
        by_name["preupgrade"].created.startswith("2024-01-01"), True,
    )
    return


# ----------------------------------------------------------------------
# Elevation / failure surfacing / backend
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Every write verb passes ``elevated=True`` to the runner when
        ``config.elevated`` is True.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_stopped(runner=runner, name="webvm")
    runner.results[_virsh_argv("dumpxml", "webvm")] = RunResult(
        exit_code=0, stdout=_DUMPXML_NO_DISK, stderr="",
    )
    runner.results[_virsh_argv("snapshot-list", "webvm")] = RunResult(
        exit_code=0, stdout=_SNAPSHOT_LIST_EMPTY, stderr="",
    )

    manager = _make_manager(runner=runner, elevated=True)
    manager.start_vm(name="webvm")
    manager.attach_disk(
        vm="webvm",
        disk_spec=UbuntuVmDiskSpec(path="/tmp/x.qcow2", target="vdb"),
    )
    manager.snapshot_vm(vm="webvm", snapshot_name="snap1")

    write_calls = [
        c for c in runner.calls
        if any(
            token in c["args"]
            for token in ("start", "attach-disk", "snapshot-create-as")
        )
    ]
    testbase.assert_equal(len(write_calls) > 0, True)
    for call in write_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero exit from the runner surfaces FAILED with stderr,
        stdout, and exit_code populated.
    """
    runner = _MockCommandRunner()
    _seed_dominfo_stopped(runner=runner, name="webvm")
    runner.results[_virsh_argv("start", "webvm")] = RunResult(
        exit_code=1, stdout="", stderr="error: failed to start domain webvm",
    )

    manager = _make_manager(runner=runner, elevated=True)
    result = manager.start_vm(name="webvm")

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(
        result.stderr, "error: failed to start domain webvm",
    )
    return


def test_unsupported_backend_returns_failed():
    """
        When the configured backend is anything other than LIBVIRT,
        every verb returns FAILED with ``message="unsupported_backend"``.
    """
    runner = _MockCommandRunner()
    manager = _make_manager(
        runner=runner, backend=UbuntuVirtBackend.LXD,
    )
    result = manager.start_vm(name="webvm")

    testbase.assert_equal(result.status, UbuntuVirtualizationActionStatus.FAILED)
    testbase.assert_equal(result.message, "unsupported_backend")
    testbase.assert_equal(len(runner.calls), 0)
    return
