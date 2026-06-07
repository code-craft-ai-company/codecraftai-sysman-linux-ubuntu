"""
    Tests for :class:`UbuntuKernelParameterManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    No live ``sysctl`` is invoked; drop-in file I/O is exercised entirely
    through ``cat`` / ``tee`` results on the mock runner.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.kernelparameters import (
    UbuntuKernelParameterActionStatus,
    UbuntuKernelParameterManager,
    UbuntuKernelParameterManagerConfig,
    UbuntuKernelParameterQueryError,
    UbuntuParameterValue,
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


def _ok(stdout: str = "", stderr: str = "") -> RunResult:
    """
        Build a successful :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr=stderr)
    return rtnval


def _fail(
    stdout: str = "", stderr: str = "", exit_code: int = 1,
) -> RunResult:
    """
        Build a failure :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


# ----------------------------------------------------------------------
# get_parameter
# ----------------------------------------------------------------------


def test_get_parameter_returns_value_on_success():
    """
        get_parameter parses ``sysctl -n`` stdout and never elevates.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-n", "vm.swappiness")] = _ok(stdout="10\n")

    manager = UbuntuKernelParameterManager(
        runner=runner, config=UbuntuKernelParameterManagerConfig(elevated=True),
    )
    info = manager.get_parameter(name="vm.swappiness")

    testbase.assert_equal(isinstance(info, UbuntuParameterValue), True)
    testbase.assert_equal(info.name, "vm.swappiness")
    testbase.assert_equal(info.value, "10")
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


def test_get_parameter_returns_none_on_cannot_stat():
    """
        get_parameter returns None when sysctl reports "cannot stat".
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-n", "vm.bogus")] = _fail(
        stderr="sysctl: cannot stat /proc/sys/vm/bogus: No such file or directory",
    )

    manager = UbuntuKernelParameterManager(
        runner=runner, config=UbuntuKernelParameterManagerConfig(),
    )
    info = manager.get_parameter(name="vm.bogus")

    testbase.assert_equal(info, None)
    return


def test_get_parameter_raises_on_unexpected_failure():
    """
        get_parameter raises UbuntuKernelParameterQueryError for an
        unrecognised non-zero stderr.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-n", "vm.swappiness")] = _fail(
        stderr="sysctl: permission denied",
    )

    manager = UbuntuKernelParameterManager(
        runner=runner, config=UbuntuKernelParameterManagerConfig(),
    )
    raised = False
    try:
        manager.get_parameter(name="vm.swappiness")
    except UbuntuKernelParameterQueryError:
        raised = True

    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# list_parameters
# ----------------------------------------------------------------------


def test_list_parameters_parses_mixed_output():
    """
        list_parameters parses a representative sysctl -a dump and skips
        malformed lines.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-a", "--ignore")] = _ok(
        stdout=(
            "vm.swappiness = 10\n"
            "kernel.pid_max = 4194304\n"
            "no-equals-line-here\n"
            "net.ipv4.ip_forward = 1\n"
        ),
    )

    manager = UbuntuKernelParameterManager(
        runner=runner, config=UbuntuKernelParameterManagerConfig(),
    )
    parsed = manager.list_parameters()

    testbase.assert_equal(parsed["vm.swappiness"], "10")
    testbase.assert_equal(parsed["kernel.pid_max"], "4194304")
    testbase.assert_equal(parsed["net.ipv4.ip_forward"], "1")
    testbase.assert_equal(len(parsed), 3)
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


def test_list_parameters_raises_on_non_zero_exit():
    """
        list_parameters raises when sysctl exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-a", "--ignore")] = _fail(stderr="boom")

    manager = UbuntuKernelParameterManager(
        runner=runner, config=UbuntuKernelParameterManagerConfig(),
    )
    raised = False
    try:
        manager.list_parameters()
    except UbuntuKernelParameterQueryError:
        raised = True

    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# set_parameter — runtime only
# ----------------------------------------------------------------------


def test_set_runtime_dispatches_sysctl_w():
    """
        set_parameter(persistent=False) dispatches ``sysctl -w name=value``
        when the runtime value differs.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("sysctl", "-n", "vm.swappiness")] = _ok(stdout="60\n")

    manager = UbuntuKernelParameterManager(
        runner=runner,
        config=UbuntuKernelParameterManagerConfig(elevated=True),
    )
    result = manager.set_parameter(
        name="vm.swappiness", value="10", persistent=False,
    )

    testbase.assert_equal(
        result.status, UbuntuKernelParameterActionStatus.SUCCESS,
    )
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["sysctl", "-w"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"], ["sysctl", "-w", "vm.swappiness=10"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    testbase.assert_equal(result.persistent, False)
    testbase.assert_equal(result.applied_value, "10")
    return


def test_set_runtime_skipped_when_value_matches():
    """
        set_parameter(persistent=False) returns SKIPPED when current
        runtime value already matches.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-n", "vm.swappiness")] = _ok(stdout="10\n")

    manager = UbuntuKernelParameterManager(
        runner=runner,
        config=UbuntuKernelParameterManagerConfig(elevated=True),
    )
    result = manager.set_parameter(
        name="vm.swappiness", value="10", persistent=False,
    )

    testbase.assert_equal(
        result.status, UbuntuKernelParameterActionStatus.SKIPPED,
    )
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["sysctl", "-w"]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# set_parameter — persistent
# ----------------------------------------------------------------------


def test_set_persistent_writes_dropin_and_applies_runtime():
    """
        set_parameter(persistent=True) writes the drop-in file via tee and
        then applies via ``sysctl -w``.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[
        ("cat", "/etc/sysctl.d/99-ccai.conf")
    ] = _fail()
    runner.results[("sysctl", "-n", "vm.swappiness")] = _ok(stdout="60\n")

    manager = UbuntuKernelParameterManager(
        runner=runner,
        config=UbuntuKernelParameterManagerConfig(elevated=True),
    )
    result = manager.set_parameter(
        name="vm.swappiness", value="10", persistent=True,
    )

    testbase.assert_equal(
        result.status, UbuntuKernelParameterActionStatus.SUCCESS,
    )
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"], ["tee", "/etc/sysctl.d/99-ccai.conf"],
    )
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    testbase.assert_equal(
        tee_calls[0]["stdin"],
        "# managed by codecraftai-sysman-linux-ubuntu\nvm.swappiness = 10\n",
    )

    sysctl_w_calls = [
        c for c in runner.calls if c["args"][:2] == ["sysctl", "-w"]
    ]
    testbase.assert_equal(len(sysctl_w_calls), 1)
    testbase.assert_equal(
        sysctl_w_calls[0]["args"], ["sysctl", "-w", "vm.swappiness=10"],
    )
    testbase.assert_equal(result.source, "/etc/sysctl.d/99-ccai.conf")
    testbase.assert_equal(result.persistent, True)
    return


def test_set_persistent_skipped_when_file_and_runtime_match():
    """
        set_parameter(persistent=True) returns SKIPPED when both runtime
        and persisted values already match.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("cat", "/etc/sysctl.d/99-ccai.conf")
    ] = _ok(
        stdout="# managed by codecraftai-sysman-linux-ubuntu\nvm.swappiness = 10\n",
    )
    runner.results[("sysctl", "-n", "vm.swappiness")] = _ok(stdout="10\n")

    manager = UbuntuKernelParameterManager(
        runner=runner,
        config=UbuntuKernelParameterManagerConfig(elevated=True),
    )
    result = manager.set_parameter(
        name="vm.swappiness", value="10", persistent=True,
    )

    testbase.assert_equal(
        result.status, UbuntuKernelParameterActionStatus.SKIPPED,
    )
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    sysctl_w_calls = [
        c for c in runner.calls if c["args"][:2] == ["sysctl", "-w"]
    ]
    testbase.assert_equal(tee_calls, [])
    testbase.assert_equal(sysctl_w_calls, [])
    return


def test_set_rejects_non_allowlisted_prefix():
    """
        set_parameter rejects a parameter whose prefix is not in the
        allowlist without invoking the runner.
    """
    runner = _MockCommandRunner()

    manager = UbuntuKernelParameterManager(
        runner=runner,
        config=UbuntuKernelParameterManagerConfig(elevated=True),
    )
    result = manager.set_parameter(
        name="bogus.thing", value="1", persistent=False,
    )

    testbase.assert_equal(
        result.status, UbuntuKernelParameterActionStatus.FAILED,
    )
    testbase.assert_equal(
        result.message, "parameter_not_supported: bogus.thing",
    )
    testbase.assert_equal(runner.calls, [])
    return


def test_set_persistent_partial_failure_reports_step():
    """
        set_parameter(persistent=True) returns FAILED with a message
        identifying the failing step when the file write fails.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("cat", "/etc/sysctl.d/99-ccai.conf")
    ] = _fail()
    runner.results[("sysctl", "-n", "vm.swappiness")] = _ok(stdout="60\n")
    runner.results[
        ("tee", "/etc/sysctl.d/99-ccai.conf")
    ] = _fail(stderr="tee: permission denied")

    manager = UbuntuKernelParameterManager(
        runner=runner,
        config=UbuntuKernelParameterManagerConfig(elevated=True),
    )
    result = manager.set_parameter(
        name="vm.swappiness", value="10", persistent=True,
    )

    testbase.assert_equal(
        result.status, UbuntuKernelParameterActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "persist_failed")
    sysctl_w_calls = [
        c for c in runner.calls if c["args"][:2] == ["sysctl", "-w"]
    ]
    testbase.assert_equal(sysctl_w_calls, [])
    return


# ----------------------------------------------------------------------
# reload_parameters
# ----------------------------------------------------------------------


def test_reload_dispatches_sysctl_system():
    """
        reload_parameters dispatches ``sysctl --system`` and elevates.
    """
    runner = _MockCommandRunner(default_result=_ok())

    manager = UbuntuKernelParameterManager(
        runner=runner,
        config=UbuntuKernelParameterManagerConfig(elevated=True),
    )
    result = manager.reload_parameters()

    testbase.assert_equal(
        result.status, UbuntuKernelParameterActionStatus.SUCCESS,
    )
    testbase.assert_equal(
        runner.calls[-1]["args"], ["sysctl", "--system"],
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    return


# ----------------------------------------------------------------------
# remove_persisted_parameter
# ----------------------------------------------------------------------


def test_remove_persisted_skipped_when_file_absent():
    """
        remove_persisted_parameter returns SKIPPED when the drop-in file
        does not exist.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("cat", "/etc/sysctl.d/99-ccai.conf")
    ] = _fail()

    manager = UbuntuKernelParameterManager(
        runner=runner, config=UbuntuKernelParameterManagerConfig(),
    )
    result = manager.remove_persisted_parameter(name="vm.swappiness")

    testbase.assert_equal(
        result.status, UbuntuKernelParameterActionStatus.SKIPPED,
    )
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


def test_remove_persisted_rewrites_file():
    """
        remove_persisted_parameter rewrites the drop-in file with the
        matching line removed.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[
        ("cat", "/etc/sysctl.d/99-ccai.conf")
    ] = _ok(
        stdout=(
            "# managed by codecraftai-sysman-linux-ubuntu\n"
            "vm.swappiness = 10\n"
            "kernel.pid_max = 4194304\n"
        ),
    )

    manager = UbuntuKernelParameterManager(
        runner=runner,
        config=UbuntuKernelParameterManagerConfig(elevated=True),
    )
    result = manager.remove_persisted_parameter(name="vm.swappiness")

    testbase.assert_equal(
        result.status, UbuntuKernelParameterActionStatus.SUCCESS,
    )
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["stdin"],
        "# managed by codecraftai-sysman-linux-ubuntu\nkernel.pid_max = 4194304\n",
    )
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Dry-run, elevation, error surfaces
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_set_runtime():
    """
        Dry-run short-circuits ``set_parameter(persistent=False)`` without
        invoking the runner.
    """
    runner = _MockCommandRunner()

    manager = UbuntuKernelParameterManager(
        runner=runner,
        config=UbuntuKernelParameterManagerConfig(dry_run=True),
    )
    result = manager.set_parameter(
        name="vm.swappiness", value="10", persistent=False,
    )

    testbase.assert_equal(
        result.status, UbuntuKernelParameterActionStatus.DRY_RUN,
    )
    testbase.assert_equal(runner.calls, [])
    testbase.assert_equal(
        result.command, ["sysctl", "-w", "vm.swappiness=10"],
    )
    return


def test_dry_run_short_circuits_set_persistent():
    """
        Dry-run short-circuits ``set_parameter(persistent=True)``.
    """
    runner = _MockCommandRunner()

    manager = UbuntuKernelParameterManager(
        runner=runner,
        config=UbuntuKernelParameterManagerConfig(dry_run=True),
    )
    result = manager.set_parameter(
        name="vm.swappiness", value="10", persistent=True,
    )

    testbase.assert_equal(
        result.status, UbuntuKernelParameterActionStatus.DRY_RUN,
    )
    testbase.assert_equal(runner.calls, [])
    testbase.assert_equal(
        result.command, ["tee", "/etc/sysctl.d/99-ccai.conf"],
    )
    testbase.assert_equal(result.source, "/etc/sysctl.d/99-ccai.conf")
    return


def test_reads_never_elevate():
    """
        Read verbs never pass elevated=True, regardless of config.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("sysctl", "-n", "vm.swappiness")] = _ok(stdout="10\n")
    runner.results[("sysctl", "-a", "--ignore")] = _ok(stdout="")

    manager = UbuntuKernelParameterManager(
        runner=runner,
        config=UbuntuKernelParameterManagerConfig(elevated=True),
    )
    manager.get_parameter(name="vm.swappiness")
    manager.list_parameters()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_runtime_apply_surfaces_stderr():
    """
        A non-zero exit on the runtime apply maps to FAILED with stderr
        captured on the result.
    """
    runner = _MockCommandRunner()
    runner.results[("sysctl", "-n", "vm.swappiness")] = _ok(stdout="60\n")
    runner.results[
        ("sysctl", "-w", "vm.swappiness=10")
    ] = _fail(stderr="sysctl: permission denied")

    manager = UbuntuKernelParameterManager(
        runner=runner,
        config=UbuntuKernelParameterManagerConfig(elevated=True),
    )
    result = manager.set_parameter(
        name="vm.swappiness", value="10", persistent=False,
    )

    testbase.assert_equal(
        result.status, UbuntuKernelParameterActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "sysctl: permission denied")
    return
