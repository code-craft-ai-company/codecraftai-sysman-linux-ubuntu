"""
    Tests for :class:`UbuntuLocalCommandRunner`.

    Tests use the real :mod:`subprocess` against harmless commands
    (``/bin/true``, ``/bin/false``, ``echo``, ``cat``).  Where a
    deterministic outcome is hard to achieve in real subprocess (timeout,
    aspect ordering), :func:`unittest.mock.patch` is used instead.
"""

import os
import subprocess

from unittest import mock

from ccai import testbase

from ccai.sysman.linux.ubuntu.runners.aspects.base import IUbuntuExecutionAspect
from ccai.sysman.linux.ubuntu.runners.local import (
    UbuntuLocalCommandRunner,
    UbuntuLocalRunnerConfig,
)
from ccai.sysman.linux.ubuntu.runners.models import (
    UbuntuExecutionContext,
    UbuntuRunResult,
)


def _resolve_binary(*candidates: str) -> str:
    """
        Pick the first executable path from ``candidates`` that exists
        on the current host, or return an empty string when none are
        available.  This lets tests run identically on Linux (where
        ``/bin/true`` / ``/bin/false`` exist) and macOS (where they
        live under ``/usr/bin``).
    """
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return ""


_ECHO = _resolve_binary("/bin/echo", "/usr/bin/echo")
_TRUE = _resolve_binary("/bin/true", "/usr/bin/true")
_FALSE = _resolve_binary("/bin/false", "/usr/bin/false")
_CAT = _resolve_binary("/bin/cat", "/usr/bin/cat")


def _has_binary(path: str) -> bool:
    """
        Return True when ``path`` is an executable file on disk.
    """
    if path is None or len(path) == 0:
        return False
    rtnval = os.path.isfile(path) and os.access(path, os.X_OK)
    return rtnval


def test_run_success_returns_zero_exit():
    """
        A command that exits zero must produce a result with
        ``exit_code == 0`` and ``success == True``.
    """
    if not _has_binary(_TRUE):
        return
    runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
    result = runner.run([_TRUE])
    testbase.assert_equal(result.exit_code, 0)
    testbase.assert_true(result.success)
    testbase.assert_equal(result.transport, "local")
    return


def test_run_failure_returns_nonzero_exit():
    """
        A command that exits non-zero must produce a result with a
        non-zero exit code and ``success == False``.
    """
    if not _has_binary(_FALSE):
        return
    runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
    result = runner.run([_FALSE])
    testbase.assert_false(result.exit_code == 0)
    testbase.assert_false(result.success)
    return


def test_run_captures_stdout_and_stderr():
    """
        Stdout written by the child must appear in
        :attr:`UbuntuRunResult.stdout`.
    """
    if not _has_binary(_ECHO):
        return
    runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
    result = runner.run([_ECHO, "hello-ubuntu"])
    testbase.assert_true("hello-ubuntu" in result.stdout)
    testbase.assert_equal(result.stderr, "")
    return


def test_run_with_env_passes_env():
    """
        Caller-supplied env values must be visible to the child
        process when the runner merges with :data:`os.environ`.
    """
    runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
    completed = subprocess.CompletedProcess(
        args=["env"], returncode=0, stdout="UBUNTU_TEST_VAR=42\n", stderr="",
    )
    with mock.patch("subprocess.run", return_value=completed) as run_mock:
        runner.run(["env"], env={"UBUNTU_TEST_VAR": "42"})
    env_passed = run_mock.call_args.kwargs["env"]
    testbase.assert_equal(env_passed["UBUNTU_TEST_VAR"], "42")
    return


def test_run_with_cwd_uses_cwd():
    """
        The cwd argument must be forwarded to :func:`subprocess.run`.
    """
    runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
    completed = subprocess.CompletedProcess(
        args=["pwd"], returncode=0, stdout="/tmp\n", stderr="",
    )
    with mock.patch("subprocess.run", return_value=completed) as run_mock:
        runner.run(["pwd"], cwd="/tmp")
    testbase.assert_equal(run_mock.call_args.kwargs["cwd"], "/tmp")
    return


def test_run_with_stdin_passes_stdin():
    """
        Stdin payload must be piped to the child.
    """
    if not _has_binary(_CAT):
        return
    runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
    result = runner.run([_CAT], stdin="ubuntu-stdin-payload")
    testbase.assert_equal(result.exit_code, 0)
    testbase.assert_true("ubuntu-stdin-payload" in result.stdout)
    return


def test_run_with_elevated_prepends_sudo():
    """
        ``elevated=True`` must prepend ``sudo -n`` to the argv.
    """
    runner = UbuntuLocalCommandRunner(
        UbuntuLocalRunnerConfig(sudo_path="/usr/bin/sudo"),
    )
    completed = subprocess.CompletedProcess(
        args=["/usr/bin/sudo", "-n", "ls"], returncode=0, stdout="", stderr="",
    )
    with mock.patch("subprocess.run", return_value=completed) as run_mock:
        runner.run(["ls"], elevated=True)
    actual_args = run_mock.call_args.args[0]
    testbase.assert_equal(actual_args[0], "/usr/bin/sudo")
    testbase.assert_equal(actual_args[1], "-n")
    testbase.assert_equal(actual_args[2], "ls")
    return


def test_run_with_elevated_and_env_appends_dash_e():
    """
        ``elevated=True`` together with an explicit env must prepend
        ``sudo -n -E`` per the Ubuntu sysman contract.
    """
    runner = UbuntuLocalCommandRunner(
        UbuntuLocalRunnerConfig(sudo_path="/usr/bin/sudo"),
    )
    completed = subprocess.CompletedProcess(
        args=["/usr/bin/sudo", "-n", "-E", "ls"], returncode=0, stdout="", stderr="",
    )
    with mock.patch("subprocess.run", return_value=completed) as run_mock:
        runner.run(["ls"], elevated=True, env={"FOO": "bar"})
    actual_args = run_mock.call_args.args[0]
    testbase.assert_equal(actual_args[0], "/usr/bin/sudo")
    testbase.assert_equal(actual_args[1], "-n")
    testbase.assert_equal(actual_args[2], "-E")
    testbase.assert_equal(actual_args[3], "ls")
    return


def test_run_with_timeout_returns_timed_out_true():
    """
        A subprocess timeout must surface as
        ``timed_out=True`` and ``exit_code=-1``.
    """
    runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
    expired = subprocess.TimeoutExpired(cmd=["sleep", "10"], timeout=1.0)
    with mock.patch("subprocess.run", side_effect=expired):
        result = runner.run(["sleep", "10"], timeout=1.0)
    testbase.assert_true(result.timed_out)
    testbase.assert_equal(result.exit_code, -1)
    testbase.assert_false(result.success)
    return


def test_run_with_on_output_callback_streams():
    """
        The ``on_output`` callback must receive the captured stdout.
    """
    chunks_out: list = []
    chunks_err: list = []
    runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
    completed = subprocess.CompletedProcess(
        args=["echo"], returncode=0, stdout="hi", stderr="warn",
    )
    with mock.patch("subprocess.run", return_value=completed):
        runner.run(
            ["echo"],
            on_output=chunks_out.append,
            on_error=chunks_err.append,
        )
    testbase.assert_equal(chunks_out, ["hi"])
    testbase.assert_equal(chunks_err, ["warn"])
    return


def test_run_without_capture_does_not_collect():
    """
        ``capture_output=False`` must not raise and must produce a
        result; ``stdout`` and ``stderr`` are empty under capture-off
        because the child writes to the parent's tty.
    """
    runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
    completed = subprocess.CompletedProcess(
        args=["echo"], returncode=0, stdout=None, stderr=None,
    )
    with mock.patch("subprocess.run", return_value=completed) as run_mock:
        result = runner.run(["echo", "off"], capture_output=False)
    testbase.assert_equal(result.exit_code, 0)
    testbase.assert_equal(result.stdout, "")
    testbase.assert_equal(result.stderr, "")
    testbase.assert_equal(run_mock.call_args.kwargs["capture_output"], False)
    return


def test_run_missing_executable_returns_nonzero():
    """
        A missing executable must produce a non-zero result rather
        than raising.
    """
    runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
    result = runner.run(["/no/such/binary/zzz"])
    testbase.assert_false(result.exit_code == 0)
    testbase.assert_false(result.success)
    return


def test_run_dry_run_skips_subprocess():
    """
        Dry-run mode must not invoke :func:`subprocess.run`.
    """
    runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig(dry_run=True))
    with mock.patch("subprocess.run") as run_mock:
        result = runner.run(["rm", "-rf", "/tmp/junk"])
    testbase.assert_true(result.dry_run)
    testbase.assert_equal(result.exit_code, 0)
    run_mock.assert_not_called()
    return


def test_metrics_track_success_and_failure():
    """
        Successful and failed runs must each increment the
        appropriate metrics counter.
    """
    runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
    ok = subprocess.CompletedProcess(
        args=["t"], returncode=0, stdout="", stderr="",
    )
    bad = subprocess.CompletedProcess(
        args=["f"], returncode=1, stdout="", stderr="bad",
    )
    with mock.patch("subprocess.run", side_effect=[ok, bad]):
        runner.run(["t"])
        runner.run(["f"])
    metrics = runner.get_metrics()
    testbase.assert_equal(metrics.success_count, 1)
    testbase.assert_equal(metrics.failure_count, 1)
    testbase.assert_equal(metrics.last_error, "bad")
    return


def test_aspect_invocation_order():
    """
        Aspects must run before-execute in registration order and
        after-execute in reverse.
    """
    events: list = []

    class _Recorder:
        enabled = True

        def __init__(self, name: str) -> None:
            self._name = name
            return

        def before_execute(self, context: UbuntuExecutionContext) -> None:
            events.append(("before", self._name))
            return

        def after_execute(
            self,
            context: UbuntuExecutionContext,
            result: UbuntuRunResult,
        ) -> UbuntuRunResult:
            events.append(("after", self._name))
            return result

    runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
    aspect_a = _Recorder(name="A")
    aspect_b = _Recorder(name="B")
    runner.add_aspect(aspect_a)
    runner.add_aspect(aspect_b)

    completed = subprocess.CompletedProcess(
        args=["echo"], returncode=0, stdout="", stderr="",
    )
    with mock.patch("subprocess.run", return_value=completed):
        runner.run(["echo"])

    expected = [
        ("before", "A"),
        ("before", "B"),
        ("after", "B"),
        ("after", "A"),
    ]
    testbase.assert_equal(events, expected)
    testbase.assert_true(isinstance(aspect_a, IUbuntuExecutionAspect))
    return
