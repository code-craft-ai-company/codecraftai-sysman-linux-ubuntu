"""
    Tests for :class:`UbuntuResourceControlManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult`
    values keyed by argv tuple.  No live ``systemctl`` / ``tee``
    / ``cat`` / ``ls`` / ``rm`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.resourcecontrolmanagement import (
    UbuntuIoLimitSpec,
    UbuntuResourceControlActionStatus,
    UbuntuResourceControlManager,
    UbuntuResourceControlManagerConfig,
    UbuntuResourceControlQueryError,
    UbuntuResourceGroupSpec,
    _parse_cpu_quota,
    _parse_show_output,
    _render_slice_body,
)


# ----------------------------------------------------------------------
# Mock runner
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every
        call and returns pre-seeded :class:`RunResult` values
        keyed by argv tuple.
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


def _seed_test_e_present(
    runner: _MockCommandRunner,
    *,
    path: str,
    present: bool = True,
) -> None:
    """
        Seed ``test -e <path>`` to report present / missing.
    """
    key = ("test", "-e", path)
    if present:
        runner.results[key] = RunResult(exit_code=0, stdout="", stderr="")
    else:
        runner.results[key] = RunResult(exit_code=1, stdout="", stderr="")
    return


def _seed_cat(
    runner: _MockCommandRunner,
    *,
    path: str,
    body: Optional[str],
    exit_code: int = 0,
    stderr: str = "",
) -> None:
    """
        Seed ``cat <path>``.  Pass ``body=None`` to mark the
        file as missing (exit_code 1, empty stdout).
    """
    key = ("cat", path)
    if body is None:
        runner.results[key] = RunResult(
            exit_code=1, stdout="", stderr=stderr,
        )
    else:
        runner.results[key] = RunResult(
            exit_code=exit_code, stdout=body, stderr=stderr,
        )
    return


def _seed_is_active(
    runner: _MockCommandRunner,
    *,
    name: str,
    active: bool,
) -> None:
    """
        Seed ``systemctl is-active <name>.slice``.
    """
    key = ("systemctl", "is-active", "{}.slice".format(name))
    if active:
        runner.results[key] = RunResult(
            exit_code=0, stdout="active\n", stderr="",
        )
    else:
        runner.results[key] = RunResult(
            exit_code=3, stdout="inactive\n", stderr="",
        )
    return


def _seed_show_property(
    runner: _MockCommandRunner,
    *,
    name: str,
    props: list,
    body: str,
) -> None:
    """
        Seed ``systemctl show <name>.slice --property=<P1,P2,...>``.
    """
    prop_string = ",".join(props)
    key = (
        "systemctl", "show", "{}.slice".format(name),
        "--property={}".format(prop_string),
    )
    runner.results[key] = RunResult(
        exit_code=0, stdout=body, stderr="",
    )
    return


# ----------------------------------------------------------------------
# create_group
# ----------------------------------------------------------------------


def test_create_group_invokes_systemctl():
    """
        ``create_group`` must write the slice unit body via
        ``tee``, then invoke ``daemon-reload`` and ``start``.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path="/etc/systemd/system/batch.slice", body=None)
    _seed_is_active(runner, name="batch", active=False)

    config = UbuntuResourceControlManagerConfig(elevated=True)
    manager = UbuntuResourceControlManager(runner=runner, config=config)

    spec = UbuntuResourceGroupSpec(
        name="batch", cpu_percent=50, memory_bytes=536870912,
        io_weight=200,
    )
    result = manager.create_group(spec=spec)

    testbase.assert_equal(result.status, UbuntuResourceControlActionStatus.SUCCESS)

    mutating = [
        call for call in runner.calls
        if call["args"][0] in ("tee", "systemctl")
        and call["args"][:2] != ["systemctl", "is-active"]
        and not (
            call["args"][0] == "systemctl"
            and len(call["args"]) >= 2
            and call["args"][1] == "show"
        )
    ]
    testbase.assert_equal(len(mutating), 3)
    testbase.assert_equal(
        mutating[0]["args"],
        ["tee", "/etc/systemd/system/batch.slice"],
    )
    testbase.assert_equal(
        mutating[1]["args"], ["systemctl", "daemon-reload"],
    )
    testbase.assert_equal(
        mutating[2]["args"], ["systemctl", "start", "batch.slice"],
    )

    # Confirm the rendered body landed on stdin of the tee call.
    tee_call = mutating[0]
    testbase.assert_equal("CPUQuota=50%" in tee_call["stdin"], True)
    testbase.assert_equal("MemoryMax=536870912" in tee_call["stdin"], True)
    testbase.assert_equal("IOWeight=200" in tee_call["stdin"], True)
    return


def test_create_group_skipped_when_present():
    """
        ``create_group`` must return SKIPPED when the on-disk
        slice file matches the rendered body and the slice is
        already active.
    """
    runner = _MockCommandRunner()

    spec = UbuntuResourceGroupSpec(
        name="batch", cpu_percent=50, memory_bytes=536870912,
        io_weight=200,
    )
    body = _render_slice_body(spec=spec)
    _seed_cat(runner, path="/etc/systemd/system/batch.slice", body=body)
    _seed_is_active(runner, name="batch", active=True)

    manager = UbuntuResourceControlManager(runner=runner)
    result = manager.create_group(spec=spec)

    testbase.assert_equal(result.status, UbuntuResourceControlActionStatus.SKIPPED)

    # No tee / start / daemon-reload calls.
    mutation = [
        call for call in runner.calls
        if call["args"][0] == "tee"
        or (call["args"][0] == "systemctl" and call["args"][1] in ("daemon-reload", "start"))
    ]
    testbase.assert_equal(len(mutation), 0)
    return


def test_create_group_dry_run():
    """
        ``create_group`` must return DRY_RUN with the planned
        ``tee`` argv and never invoke the runner.
    """
    runner = _MockCommandRunner()
    config = UbuntuResourceControlManagerConfig(dry_run=True)
    manager = UbuntuResourceControlManager(runner=runner, config=config)

    spec = UbuntuResourceGroupSpec(name="batch", cpu_percent=25)
    result = manager.create_group(spec=spec)

    testbase.assert_equal(result.status, UbuntuResourceControlActionStatus.DRY_RUN)
    testbase.assert_equal(
        result.command, ["tee", "/etc/systemd/system/batch.slice"],
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# delete_group
# ----------------------------------------------------------------------


def test_delete_group_invokes_systemctl():
    """
        ``delete_group`` must invoke ``systemctl stop``, ``rm``,
        and ``systemctl daemon-reload``.
    """
    runner = _MockCommandRunner()
    _seed_test_e_present(
        runner, path="/etc/systemd/system/batch.slice", present=True,
    )
    _seed_is_active(runner, name="batch", active=True)

    manager = UbuntuResourceControlManager(runner=runner)
    result = manager.delete_group(name="batch")

    testbase.assert_equal(result.status, UbuntuResourceControlActionStatus.SUCCESS)

    mutating = [
        call for call in runner.calls
        if call["args"][0] in ("systemctl", "rm")
        and call["args"][:2] != ["systemctl", "is-active"]
    ]
    testbase.assert_equal(len(mutating), 3)
    testbase.assert_equal(
        mutating[0]["args"], ["systemctl", "stop", "batch.slice"],
    )
    testbase.assert_equal(
        mutating[1]["args"],
        ["rm", "-f", "/etc/systemd/system/batch.slice"],
    )
    testbase.assert_equal(
        mutating[2]["args"], ["systemctl", "daemon-reload"],
    )
    return


def test_delete_group_skipped_when_absent():
    """
        ``delete_group`` must return SKIPPED when the slice
        unit file does not exist and the slice is not active.
    """
    runner = _MockCommandRunner()
    _seed_test_e_present(
        runner, path="/etc/systemd/system/batch.slice", present=False,
    )
    _seed_is_active(runner, name="batch", active=False)

    manager = UbuntuResourceControlManager(runner=runner)
    result = manager.delete_group(name="batch")

    testbase.assert_equal(result.status, UbuntuResourceControlActionStatus.SKIPPED)
    # No mutating calls were made.
    mutation = [
        call for call in runner.calls
        if call["args"][0] == "rm"
        or (call["args"][0] == "systemctl" and len(call["args"]) >= 2 and call["args"][1] in ("stop", "daemon-reload"))
    ]
    testbase.assert_equal(len(mutation), 0)
    return


# ----------------------------------------------------------------------
# set_cpu_limit
# ----------------------------------------------------------------------


def test_set_cpu_limit_invokes_systemctl():
    """
        ``set_cpu_limit`` must invoke ``systemctl set-property
        <name>.slice CPUQuota=N%``.
    """
    runner = _MockCommandRunner()
    _seed_show_property(
        runner, name="batch", props=["CPUQuotaPerSecUSec"],
        body="CPUQuotaPerSecUSec=infinity\n",
    )

    config = UbuntuResourceControlManagerConfig(elevated=True)
    manager = UbuntuResourceControlManager(runner=runner, config=config)

    result = manager.set_cpu_limit(name="batch", percent=50)

    testbase.assert_equal(result.status, UbuntuResourceControlActionStatus.SUCCESS)

    mutating = [
        call for call in runner.calls
        if call["args"][0] == "systemctl" and call["args"][1] == "set-property"
    ]
    testbase.assert_equal(len(mutating), 1)
    testbase.assert_equal(
        mutating[0]["args"],
        ["systemctl", "set-property", "batch.slice", "CPUQuota=50%"],
    )
    testbase.assert_equal(mutating[0]["elevated"], True)
    return


def test_set_cpu_limit_skipped_when_match():
    """
        ``set_cpu_limit`` must return SKIPPED when the current
        ``CPUQuotaPerSecUSec`` already matches the requested
        percent.
    """
    runner = _MockCommandRunner()
    # 500_000 us / 10_000 = 50%.
    _seed_show_property(
        runner, name="batch", props=["CPUQuotaPerSecUSec"],
        body="CPUQuotaPerSecUSec=500000us\n",
    )

    manager = UbuntuResourceControlManager(runner=runner)
    result = manager.set_cpu_limit(name="batch", percent=50)

    testbase.assert_equal(result.status, UbuntuResourceControlActionStatus.SKIPPED)

    mutating = [
        call for call in runner.calls
        if call["args"][0] == "systemctl" and call["args"][1] == "set-property"
    ]
    testbase.assert_equal(len(mutating), 0)
    return


# ----------------------------------------------------------------------
# set_memory_limit
# ----------------------------------------------------------------------


def test_set_memory_limit_invokes_systemctl():
    """
        ``set_memory_limit`` must invoke ``systemctl set-property
        <name>.slice MemoryMax=N``.
    """
    runner = _MockCommandRunner()
    _seed_show_property(
        runner, name="batch", props=["MemoryMax"],
        body="MemoryMax=infinity\n",
    )

    manager = UbuntuResourceControlManager(runner=runner)
    result = manager.set_memory_limit(name="batch", bytes_=536870912)

    testbase.assert_equal(result.status, UbuntuResourceControlActionStatus.SUCCESS)

    mutating = [
        call for call in runner.calls
        if call["args"][0] == "systemctl" and call["args"][1] == "set-property"
    ]
    testbase.assert_equal(len(mutating), 1)
    testbase.assert_equal(
        mutating[0]["args"],
        [
            "systemctl", "set-property", "batch.slice",
            "MemoryMax=536870912",
        ],
    )
    return


# ----------------------------------------------------------------------
# set_io_limits
# ----------------------------------------------------------------------


def test_set_io_limits_invokes_systemctl():
    """
        ``set_io_limits`` must invoke a single ``systemctl
        set-property`` carrying every supplied IO knob.
    """
    runner = _MockCommandRunner()
    _seed_show_property(
        runner, name="batch",
        props=["IOWeight", "IOReadBandwidthMax", "IOWriteBandwidthMax"],
        body="IOWeight=100\nIOReadBandwidthMax=\nIOWriteBandwidthMax=\n",
    )

    manager = UbuntuResourceControlManager(runner=runner)
    spec = UbuntuIoLimitSpec(
        weight=200, read_bps=1048576, write_bps=2097152,
        device="/dev/sda",
    )
    result = manager.set_io_limits(name="batch", spec=spec)

    testbase.assert_equal(result.status, UbuntuResourceControlActionStatus.SUCCESS)

    mutating = [
        call for call in runner.calls
        if call["args"][0] == "systemctl" and call["args"][1] == "set-property"
    ]
    testbase.assert_equal(len(mutating), 1)
    args = mutating[0]["args"]
    testbase.assert_equal(args[:3], ["systemctl", "set-property", "batch.slice"])
    testbase.assert_equal("IOWeight=200" in args, True)
    testbase.assert_equal(
        "IOReadBandwidthMax=/dev/sda 1048576" in args, True,
    )
    testbase.assert_equal(
        "IOWriteBandwidthMax=/dev/sda 2097152" in args, True,
    )
    return


# ----------------------------------------------------------------------
# attach_process
# ----------------------------------------------------------------------


def test_attach_process_invokes_tee():
    """
        ``attach_process`` must write the PID to
        ``cgroup.procs`` via ``tee --append``.
    """
    runner = _MockCommandRunner()
    _seed_test_e_present(
        runner, path="/sys/fs/cgroup/batch.slice", present=True,
    )
    _seed_cat(
        runner, path="/sys/fs/cgroup/batch.slice/cgroup.procs",
        body="123\n456\n",
    )

    config = UbuntuResourceControlManagerConfig(elevated=True)
    manager = UbuntuResourceControlManager(runner=runner, config=config)

    result = manager.attach_process(name="batch", pid=789)

    testbase.assert_equal(result.status, UbuntuResourceControlActionStatus.SUCCESS)
    testbase.assert_equal(result.pid, 789)

    tee_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"],
        ["tee", "--append", "/sys/fs/cgroup/batch.slice/cgroup.procs"],
    )
    testbase.assert_equal(tee_calls[0]["stdin"], "789\n")
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


def test_attach_process_skipped_when_present():
    """
        ``attach_process`` must return SKIPPED when the PID is
        already in ``cgroup.procs``.
    """
    runner = _MockCommandRunner()
    _seed_test_e_present(
        runner, path="/sys/fs/cgroup/batch.slice", present=True,
    )
    _seed_cat(
        runner, path="/sys/fs/cgroup/batch.slice/cgroup.procs",
        body="123\n456\n789\n",
    )

    manager = UbuntuResourceControlManager(runner=runner)
    result = manager.attach_process(name="batch", pid=789)

    testbase.assert_equal(result.status, UbuntuResourceControlActionStatus.SKIPPED)

    tee_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


# ----------------------------------------------------------------------
# detach_process
# ----------------------------------------------------------------------


def test_detach_process_invokes_tee():
    """
        ``detach_process`` must append the PID to
        ``system.slice/cgroup.procs`` via ``tee --append``.
    """
    runner = _MockCommandRunner()
    _seed_cat(
        runner, path="/sys/fs/cgroup/batch.slice/cgroup.procs",
        body="123\n789\n",
    )

    manager = UbuntuResourceControlManager(runner=runner)
    result = manager.detach_process(name="batch", pid=789)

    testbase.assert_equal(result.status, UbuntuResourceControlActionStatus.SUCCESS)
    testbase.assert_equal(result.pid, 789)

    tee_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"],
        ["tee", "--append", "/sys/fs/cgroup/system.slice/cgroup.procs"],
    )
    testbase.assert_equal(tee_calls[0]["stdin"], "789\n")
    return


# ----------------------------------------------------------------------
# list_groups
# ----------------------------------------------------------------------


def test_list_groups_returns_list():
    """
        ``list_groups`` must enumerate ``.slice`` entries under
        the cgroup root and return one
        :class:`UbuntuResourceGroupInfo` per slice.
    """
    runner = _MockCommandRunner()
    runner.results[("ls", "-1", "/sys/fs/cgroup")] = RunResult(
        exit_code=0,
        stdout="batch.slice\nworker.slice\nuser.slice\nsome_other_dir\n",
        stderr="",
    )
    show_props = [
        "ActiveState", "CPUQuotaPerSecUSec", "CPUWeight",
        "MemoryMax", "IOWeight",
        "IOReadBandwidthMax", "IOWriteBandwidthMax",
    ]
    body_batch = (
        "ActiveState=active\nCPUQuotaPerSecUSec=500000us\n"
        "CPUWeight=100\nMemoryMax=536870912\nIOWeight=200\n"
        "IOReadBandwidthMax=\nIOWriteBandwidthMax=\n"
    )
    body_worker = (
        "ActiveState=active\nCPUQuotaPerSecUSec=infinity\n"
        "CPUWeight=100\nMemoryMax=infinity\nIOWeight=100\n"
        "IOReadBandwidthMax=\nIOWriteBandwidthMax=\n"
    )
    body_user = (
        "ActiveState=active\nCPUQuotaPerSecUSec=infinity\n"
        "CPUWeight=100\nMemoryMax=infinity\nIOWeight=100\n"
        "IOReadBandwidthMax=\nIOWriteBandwidthMax=\n"
    )
    _seed_show_property(
        runner, name="batch", props=show_props, body=body_batch,
    )
    _seed_show_property(
        runner, name="worker", props=show_props, body=body_worker,
    )
    _seed_show_property(
        runner, name="user", props=show_props, body=body_user,
    )
    _seed_cat(
        runner, path="/sys/fs/cgroup/batch.slice/cgroup.procs",
        body="111\n222\n",
    )
    _seed_cat(
        runner, path="/sys/fs/cgroup/worker.slice/cgroup.procs",
        body="333\n",
    )
    _seed_cat(
        runner, path="/sys/fs/cgroup/user.slice/cgroup.procs",
        body="",
    )

    manager = UbuntuResourceControlManager(runner=runner)
    groups = manager.list_groups()

    testbase.assert_equal(len(groups), 3)
    by_name = {info.name: info for info in groups}
    testbase.assert_equal("batch" in by_name, True)
    testbase.assert_equal(by_name["batch"].cpu_percent, 50)
    testbase.assert_equal(by_name["batch"].memory_bytes, 536870912)
    testbase.assert_equal(by_name["batch"].io_weight, 200)
    testbase.assert_equal(by_name["batch"].is_active, True)
    testbase.assert_equal(by_name["batch"].attached_pids, [111, 222])

    testbase.assert_equal(by_name["worker"].cpu_percent, None)
    testbase.assert_equal(by_name["worker"].memory_bytes, None)

    # All read calls were elevated=False.
    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# get_group_usage
# ----------------------------------------------------------------------


def test_get_group_usage_parses_cgroup_files():
    """
        ``get_group_usage`` must parse ``cpu.stat``,
        ``memory.current``, ``io.stat``, and ``cgroup.procs``.
    """
    runner = _MockCommandRunner()
    _seed_test_e_present(
        runner, path="/sys/fs/cgroup/batch.slice", present=True,
    )
    _seed_cat(
        runner, path="/sys/fs/cgroup/batch.slice/cpu.stat",
        body=(
            "usage_usec 12345678\n"
            "user_usec 11111111\n"
            "system_usec 1234567\n"
        ),
    )
    _seed_cat(
        runner, path="/sys/fs/cgroup/batch.slice/memory.current",
        body="104857600\n",
    )
    _seed_cat(
        runner, path="/sys/fs/cgroup/batch.slice/io.stat",
        body=(
            "8:0 rbytes=1024 wbytes=2048 rios=10 wios=20\n"
            "8:16 rbytes=4096 wbytes=8192 rios=30 wios=40\n"
        ),
    )
    _seed_cat(
        runner, path="/sys/fs/cgroup/batch.slice/cgroup.procs",
        body="111\n222\n333\n",
    )

    manager = UbuntuResourceControlManager(runner=runner)
    usage = manager.get_group_usage(name="batch")

    testbase.assert_equal(usage.name, "batch")
    testbase.assert_equal(usage.cpu_usage_seconds, 12.345678)
    testbase.assert_equal(usage.memory_used_bytes, 104857600)
    testbase.assert_equal(usage.io_read_bytes, 1024 + 4096)
    testbase.assert_equal(usage.io_write_bytes, 2048 + 8192)
    testbase.assert_equal(usage.process_count, 3)
    return


def test_get_group_usage_unknown_group_raises():
    """
        ``get_group_usage`` must raise
        :class:`UbuntuResourceControlQueryError` with
        ``detail="unknown_group"`` when the slice cgroup
        directory does not exist.
    """
    runner = _MockCommandRunner()
    _seed_test_e_present(
        runner, path="/sys/fs/cgroup/batch.slice", present=False,
    )

    manager = UbuntuResourceControlManager(runner=runner)
    raised: Optional[UbuntuResourceControlQueryError] = None
    try:
        manager.get_group_usage(name="batch")
    except UbuntuResourceControlQueryError as exc:
        raised = exc
    testbase.assert_not_equal(raised, None)
    testbase.assert_equal(raised.detail, "unknown_group")
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write verbs must pass ``elevated=True`` to the runner
        when ``config.elevated`` is True.
    """
    runner = _MockCommandRunner()
    _seed_show_property(
        runner, name="batch", props=["MemoryMax"],
        body="MemoryMax=infinity\n",
    )

    config = UbuntuResourceControlManagerConfig(elevated=True)
    manager = UbuntuResourceControlManager(runner=runner, config=config)

    manager.set_memory_limit(name="batch", bytes_=536870912)

    setprop = [
        call for call in runner.calls
        if call["args"][0] == "systemctl" and call["args"][1] == "set-property"
    ]
    testbase.assert_equal(len(setprop), 1)
    testbase.assert_equal(setprop[0]["elevated"], True)

    # The probe (systemctl show) must be elevated=False.
    show = [
        call for call in runner.calls
        if call["args"][0] == "systemctl" and call["args"][1] == "show"
    ]
    testbase.assert_equal(len(show), 1)
    testbase.assert_equal(show[0]["elevated"], False)
    return


def test_reads_never_elevate():
    """
        Read verbs must always pass ``elevated=False`` to the
        runner, regardless of ``config.elevated``.
    """
    runner = _MockCommandRunner()
    runner.results[("ls", "-1", "/sys/fs/cgroup")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    config = UbuntuResourceControlManagerConfig(elevated=True)
    manager = UbuntuResourceControlManager(runner=runner, config=config)

    groups = manager.list_groups()
    testbase.assert_equal(groups, [])

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Failure surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failing dispatched command must surface its
        ``exit_code``, ``stderr``, and ``command`` on the
        FAILED result.
    """
    runner = _MockCommandRunner()
    _seed_show_property(
        runner, name="batch", props=["MemoryMax"],
        body="MemoryMax=infinity\n",
    )
    runner.results[(
        "systemctl", "set-property", "batch.slice",
        "MemoryMax=536870912",
    )] = RunResult(
        exit_code=1, stdout="", stderr="Failed to set MemoryMax: not authorized\n",
    )

    manager = UbuntuResourceControlManager(runner=runner)
    result = manager.set_memory_limit(name="batch", bytes_=536870912)

    testbase.assert_equal(result.status, UbuntuResourceControlActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal("not authorized" in result.stderr, True)
    testbase.assert_equal(
        result.command,
        ["systemctl", "set-property", "batch.slice", "MemoryMax=536870912"],
    )
    return


# ----------------------------------------------------------------------
# Pure parser tests
# ----------------------------------------------------------------------


def test_parse_cpu_quota_handles_us_suffix():
    """
        ``_parse_cpu_quota`` must convert ``500000us`` to 50.
    """
    testbase.assert_equal(_parse_cpu_quota(raw="500000us"), 50)
    testbase.assert_equal(_parse_cpu_quota(raw="1000000"), 100)
    testbase.assert_equal(_parse_cpu_quota(raw="infinity"), None)
    testbase.assert_equal(_parse_cpu_quota(raw=""), None)
    return


def test_parse_show_output_dict():
    """
        ``_parse_show_output`` must split ``key=value`` lines
        into a dict.
    """
    body = (
        "ActiveState=active\n"
        "MemoryMax=536870912\n"
        "IOWeight=200\n"
    )
    parsed = _parse_show_output(stdout=body)
    testbase.assert_equal(parsed["ActiveState"], "active")
    testbase.assert_equal(parsed["MemoryMax"], "536870912")
    testbase.assert_equal(parsed["IOWeight"], "200")
    return
