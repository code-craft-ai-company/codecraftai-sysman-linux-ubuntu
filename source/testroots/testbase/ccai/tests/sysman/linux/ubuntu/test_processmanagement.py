"""
    Tests for :class:`UbuntuProcessManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``ps`` / ``kill`` / ``pkill`` / ``pgrep`` /
    ``renice`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.processmanagement import (
    UbuntuProcessActionStatus,
    UbuntuProcessManager,
    UbuntuProcessManagerConfig,
    UbuntuProcessQueryError,
    UbuntuProcessSpec,
)


_PS_FORMAT = (
    "pid,ppid,user,stat,pri,ni,vsz,rss,pcpu,pmem,etime,comm,args"
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
            "cwd": cwd,
            "env": env,
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

        rtnval = RunResult(
            exit_code=1,
            stdout="",
            stderr="not implemented",
        )
        return rtnval


def _ok(stdout: str = "") -> RunResult:
    """
        Build a canonical success :class:`RunResult` for the mock.
    """
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr="")
    return rtnval


def _fail(*, exit_code: int = 1, stderr: str = "boom") -> RunResult:
    """
        Build a canonical failure :class:`RunResult` for the mock.
    """
    rtnval = RunResult(exit_code=exit_code, stdout="", stderr=stderr)
    return rtnval


def _ps_line(
    *,
    pid: int,
    ppid: int = 1,
    user: str = "root",
    state: str = "S",
    pri: int = 20,
    ni: int = 0,
    vsz: int = 1024,
    rss: int = 512,
    pcpu: float = 0.1,
    pmem: float = 0.2,
    etime: str = "00:01",
    comm: str = "demo",
    args: str = "demo --flag",
) -> str:
    """
        Build a single ``ps`` ``--no-headers`` row matching the
        ``_PS_FORMAT`` column order.
    """
    rtnval = (
        "{} {} {} {} {} {} {} {} {} {} {} {} {}".format(
            pid, ppid, user, state, pri, ni, vsz, rss,
            pcpu, pmem, etime, comm, args,
        )
    )
    return rtnval


# ----------------------------------------------------------------------
# list_processes
# ----------------------------------------------------------------------


def test_list_processes_invokes_ps_and_parses():
    """
        list_processes() shells out to
        ``ps -eo <format> --no-headers`` and parses every row.
    """
    output = "\n".join([
        _ps_line(pid=1, comm="systemd", args="/sbin/init"),
        _ps_line(pid=42, ppid=1, comm="bash", args="bash -i"),
    ]) + "\n"
    runner = _MockCommandRunner()
    runner.results[("ps", "-eo", _PS_FORMAT, "--no-headers")] = _ok(
        stdout=output,
    )

    manager = UbuntuProcessManager(
        runner, UbuntuProcessManagerConfig(),
    )
    records = manager.list_processes()

    testbase.assert_equal(len(records), 2)
    testbase.assert_equal(records[0].pid, 1)
    testbase.assert_equal(records[0].command, "systemd")
    testbase.assert_equal(records[0].argv[0], "/sbin/init")
    testbase.assert_equal(records[1].pid, 42)
    testbase.assert_equal(records[1].ppid, 1)
    testbase.assert_equal(records[1].command, "bash")
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


# ----------------------------------------------------------------------
# get_process
# ----------------------------------------------------------------------


def test_get_process_parses_single_record():
    """
        get_process() runs ``ps -p <pid>`` and parses the single row.
    """
    output = _ps_line(
        pid=4242, ppid=1, user="alice", comm="python", args="python app.py",
    ) + "\n"
    runner = _MockCommandRunner()
    runner.results[(
        "ps", "-p", "4242", "-o", _PS_FORMAT, "--no-headers",
    )] = _ok(stdout=output)

    manager = UbuntuProcessManager(
        runner, UbuntuProcessManagerConfig(),
    )
    info = manager.get_process(pid=4242)

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.pid, 4242)
    testbase.assert_equal(info.user, "alice")
    testbase.assert_equal(info.command, "python")
    return


def test_get_process_returns_none_for_unknown_pid():
    """
        ``ps -p <unknown>`` exits 1 with empty stderr; get_process()
        maps that to ``None``.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "ps", "-p", "9999", "-o", _PS_FORMAT, "--no-headers",
    )] = RunResult(exit_code=1, stdout="", stderr="")

    manager = UbuntuProcessManager(
        runner, UbuntuProcessManagerConfig(),
    )
    info = manager.get_process(pid=9999)

    testbase.assert_true(info is None)
    return


# ----------------------------------------------------------------------
# kill_process
# ----------------------------------------------------------------------


def test_kill_process_invokes_kill():
    """
        kill_process() dispatches ``kill -<signal> <pid>`` after
        confirming the PID exists.
    """
    runner = _MockCommandRunner()
    # First call: get_process probe returning a single row.
    runner.results[(
        "ps", "-p", "1234", "-o", _PS_FORMAT, "--no-headers",
    )] = _ok(stdout=_ps_line(pid=1234) + "\n")
    runner.results[("kill", "-TERM", "1234")] = _ok()

    manager = UbuntuProcessManager(
        runner, UbuntuProcessManagerConfig(),
    )
    result = manager.kill_process(pid=1234)

    testbase.assert_equal(result.status, UbuntuProcessActionStatus.SUCCESS)
    testbase.assert_equal(result.pid, 1234)
    testbase.assert_equal(result.signal, "TERM")
    testbase.assert_equal(result.command, ["kill", "-TERM", "1234"])
    return


def test_kill_process_skipped_when_pid_not_found():
    """
        When the PID is not present, kill_process() returns SKIPPED
        without invoking ``kill``.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "ps", "-p", "5555", "-o", _PS_FORMAT, "--no-headers",
    )] = RunResult(exit_code=1, stdout="", stderr="")

    manager = UbuntuProcessManager(
        runner, UbuntuProcessManagerConfig(),
    )
    result = manager.kill_process(pid=5555)

    testbase.assert_equal(result.status, UbuntuProcessActionStatus.SKIPPED)
    kill_calls: list = []
    for record in runner.calls:
        if len(record["args"]) > 0 and record["args"][0] == "kill":
            kill_calls.append(record)
    testbase.assert_equal(len(kill_calls), 0)
    return


def test_kill_process_dry_run_short_circuits():
    """
        Dry-run config returns DRY_RUN without invoking the runner.
    """
    runner = _MockCommandRunner()
    config = UbuntuProcessManagerConfig(dry_run=True)
    manager = UbuntuProcessManager(runner, config)

    result = manager.kill_process(pid=1234, signal="KILL")

    testbase.assert_equal(result.status, UbuntuProcessActionStatus.DRY_RUN)
    testbase.assert_equal(result.command, ["kill", "-KILL", "1234"])
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# kill_process_tree
# ----------------------------------------------------------------------


def test_kill_process_tree_kills_children_first():
    """
        kill_process_tree() runs ``pkill -<sig> -P <pid>`` before the
        root ``kill -<sig> <pid>``.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "ps", "-p", "1000", "-o", _PS_FORMAT, "--no-headers",
    )] = _ok(stdout=_ps_line(pid=1000) + "\n")
    runner.results[("pkill", "-TERM", "-P", "1000")] = _ok()
    runner.results[("kill", "-TERM", "1000")] = _ok()

    manager = UbuntuProcessManager(
        runner, UbuntuProcessManagerConfig(),
    )
    result = manager.kill_process_tree(pid=1000)

    testbase.assert_equal(result.status, UbuntuProcessActionStatus.SUCCESS)
    testbase.assert_equal(result.pid, 1000)

    write_argvs: list = []
    for record in runner.calls:
        if len(record["args"]) > 0:
            head = record["args"][0]
            if head in ("pkill", "kill"):
                write_argvs.append(record["args"])

    testbase.assert_equal(len(write_argvs), 2)
    testbase.assert_equal(write_argvs[0][0], "pkill")
    testbase.assert_equal(write_argvs[1][0], "kill")
    return


# ----------------------------------------------------------------------
# start_process
# ----------------------------------------------------------------------


def test_start_process_invokes_setsid_sh():
    """
        start_process() shells out to ``setsid sh -c '<argv>'``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    spec = UbuntuProcessSpec(
        executable="/usr/bin/python3",
        args=["-c", "print('hi')"],
    )

    manager = UbuntuProcessManager(
        runner, UbuntuProcessManagerConfig(),
    )
    result = manager.start_process(spec=spec)

    testbase.assert_equal(result.status, UbuntuProcessActionStatus.SUCCESS)
    testbase.assert_equal(result.command[0], "setsid")
    testbase.assert_equal(result.command[1], "sh")
    testbase.assert_equal(result.command[2], "-c")
    testbase.assert_true("/usr/bin/python3" in result.command[3])
    return


def test_start_process_dry_run_short_circuits():
    """
        Dry-run start_process() returns DRY_RUN without calling the
        runner.
    """
    runner = _MockCommandRunner()
    config = UbuntuProcessManagerConfig(dry_run=True)
    manager = UbuntuProcessManager(runner, config)

    spec = UbuntuProcessSpec(executable="/bin/true")
    result = manager.start_process(spec=spec)

    testbase.assert_equal(result.status, UbuntuProcessActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# set_process_priority
# ----------------------------------------------------------------------


def test_set_process_priority_invokes_renice():
    """
        set_process_priority() dispatches ``renice <prio> -p <pid>``.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "ps", "-p", "1234", "-o", _PS_FORMAT, "--no-headers",
    )] = _ok(stdout=_ps_line(pid=1234, ni=0) + "\n")
    runner.results[("renice", "10", "-p", "1234")] = _ok()

    manager = UbuntuProcessManager(
        runner, UbuntuProcessManagerConfig(),
    )
    result = manager.set_process_priority(pid=1234, priority=10)

    testbase.assert_equal(result.status, UbuntuProcessActionStatus.SUCCESS)
    testbase.assert_equal(result.command, ["renice", "10", "-p", "1234"])
    testbase.assert_equal(result.pid, 1234)
    return


def test_set_process_priority_skipped_when_already_matches():
    """
        When the current ``ni`` value already equals the requested
        priority, set_process_priority() returns SKIPPED.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "ps", "-p", "1234", "-o", _PS_FORMAT, "--no-headers",
    )] = _ok(stdout=_ps_line(pid=1234, ni=10) + "\n")

    manager = UbuntuProcessManager(
        runner, UbuntuProcessManagerConfig(),
    )
    result = manager.set_process_priority(pid=1234, priority=10)

    testbase.assert_equal(result.status, UbuntuProcessActionStatus.SKIPPED)
    renice_calls: list = []
    for record in runner.calls:
        if len(record["args"]) > 0 and record["args"][0] == "renice":
            renice_calls.append(record)
    testbase.assert_equal(len(renice_calls), 0)
    return


# ----------------------------------------------------------------------
# find_processes_by_name
# ----------------------------------------------------------------------


def test_find_processes_by_name_parses_pgrep_output():
    """
        find_processes_by_name() runs ``pgrep -a <name>`` and parses
        each row into a :class:`UbuntuProcessInfo`.
    """
    output = (
        "100 nginx: master process /usr/sbin/nginx\n"
        "101 nginx: worker process\n"
    )
    runner = _MockCommandRunner()
    runner.results[("pgrep", "-a", "nginx")] = _ok(stdout=output)

    manager = UbuntuProcessManager(
        runner, UbuntuProcessManagerConfig(),
    )
    records = manager.find_processes_by_name(name="nginx")

    testbase.assert_equal(len(records), 2)
    testbase.assert_equal(records[0].pid, 100)
    testbase.assert_equal(records[0].command, "nginx:")
    testbase.assert_equal(records[1].pid, 101)
    return


# ----------------------------------------------------------------------
# Failure paths
# ----------------------------------------------------------------------


def test_failed_command_returns_failed_or_raises_on_read():
    """
        Reads raise :class:`UbuntuProcessQueryError` on non-zero
        exit; writes return status FAILED.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "ps", "-eo", _PS_FORMAT, "--no-headers",
    )] = _fail(exit_code=2, stderr="ps explosion")

    manager = UbuntuProcessManager(
        runner, UbuntuProcessManagerConfig(),
    )

    raised = False
    try:
        manager.list_processes()
    except UbuntuProcessQueryError as ex:
        raised = True
        testbase.assert_equal(ex.exit_code, 2)

    testbase.assert_true(raised)

    # Now the write side: kill_process when ``kill`` itself fails.
    runner_w = _MockCommandRunner()
    runner_w.results[(
        "ps", "-p", "1234", "-o", _PS_FORMAT, "--no-headers",
    )] = _ok(stdout=_ps_line(pid=1234) + "\n")
    runner_w.results[("kill", "-TERM", "1234")] = _fail(
        exit_code=1, stderr="permission denied",
    )

    manager_w = UbuntuProcessManager(
        runner_w, UbuntuProcessManagerConfig(),
    )
    result = manager_w.kill_process(pid=1234)
    testbase.assert_equal(result.status, UbuntuProcessActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_true("permission denied" in result.stderr)
    return


# ----------------------------------------------------------------------
# Read elevation discipline
# ----------------------------------------------------------------------


def test_reads_never_elevate():
    """
        Even when ``config.elevated=True``, every read passes
        ``elevated=False`` to the runner.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    runner.results[("ps", "-eo", _PS_FORMAT, "--no-headers")] = _ok(
        stdout=_ps_line(pid=1, comm="systemd") + "\n",
    )
    runner.results[(
        "ps", "-p", "1", "-o", _PS_FORMAT, "--no-headers",
    )] = _ok(stdout=_ps_line(pid=1, comm="systemd") + "\n")
    runner.results[("pgrep", "-a", "systemd")] = _ok(
        stdout="1 systemd\n",
    )

    config = UbuntuProcessManagerConfig(elevated=True)
    manager = UbuntuProcessManager(runner, config)

    manager.list_processes()
    manager.get_process(pid=1)
    manager.find_processes_by_name(name="systemd")

    elevated_calls: list = []
    for record in runner.calls:
        if record["elevated"] is True:
            elevated_calls.append(record)

    testbase.assert_equal(len(elevated_calls), 0)
    return
