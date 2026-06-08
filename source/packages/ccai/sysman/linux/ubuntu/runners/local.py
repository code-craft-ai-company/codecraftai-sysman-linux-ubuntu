"""
    Local command runner for Ubuntu.

    :class:`UbuntuLocalCommandRunner` implements
    :class:`ccai.interfaces.icommandrunner.ICommandRunner` using
    Python's :mod:`subprocess` module.  Elevation is provided by
    prepending ``sudo -n`` (and ``-E`` when an explicit env is supplied)
    to the argv vector before dispatch, matching the Ubuntu sysman
    contract.

    The runner supports the standard aspect-composition hooks
    (``add_aspect`` / ``remove_aspect``) and tracks per-instance
    reliability metrics for parity with
    :class:`UbuntuSshCommandRunner`.
"""

import os
import subprocess
import time

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from ccai.sysman.linux.ubuntu.runners.models import (
    UbuntuExecutionContext,
    UbuntuRunResult,
    UbuntuSshReliabilityMetrics,
)


if TYPE_CHECKING:
    from ccai.sysman.linux.ubuntu.runners.aspects.base import IUbuntuExecutionAspect


__all__ = [
    "UbuntuLocalCommandRunner",
    "UbuntuLocalRunnerConfig",
]


@dataclass
class UbuntuLocalRunnerConfig:
    """
        Configuration for :class:`UbuntuLocalCommandRunner`.

        :param dry_run: When True, no subprocess is spawned and a
                        synthetic success result is returned.
        :param sudo_path: Path to the sudo binary used for elevation.
        :param merge_env_with_process: When True (the default), an
                                       explicit ``env`` is merged with
                                       :data:`os.environ`.  When False,
                                       ``env`` replaces the process
                                       env entirely (the standard
                                       :func:`subprocess.run` semantic).
    """

    dry_run: bool = False
    sudo_path: str = "/usr/bin/sudo"
    merge_env_with_process: bool = True


class UbuntuLocalCommandRunner:
    """
        ``ICommandRunner`` implementation that runs commands locally
        via :func:`subprocess.run`.
    """

    def __init__(
        self,
        config: Optional[UbuntuLocalRunnerConfig] = None,
    ) -> None:
        """
            :param config: Runner configuration.  Defaults are used
                           when ``None`` is supplied.
        """
        if config is None:
            config = UbuntuLocalRunnerConfig()

        self._config: UbuntuLocalRunnerConfig = config
        self._aspects: list["IUbuntuExecutionAspect"] = []
        self._metrics: UbuntuSshReliabilityMetrics = UbuntuSshReliabilityMetrics()
        return

    @property
    def config(self) -> UbuntuLocalRunnerConfig:
        """
            The active :class:`UbuntuLocalRunnerConfig`.
        """
        return self._config

    @property
    def aspects(self) -> list["IUbuntuExecutionAspect"]:
        """
            Snapshot of registered aspects in registration order.
        """
        rtnval = list(self._aspects)
        return rtnval

    def add_aspect(self, aspect: "IUbuntuExecutionAspect") -> None:
        """
            Register an aspect to participate in subsequent executions.
        """
        self._aspects.append(aspect)
        return

    def remove_aspect(self, aspect: "IUbuntuExecutionAspect") -> None:
        """
            Unregister a previously registered aspect.  Silent when
            the aspect was not registered.
        """
        if aspect in self._aspects:
            self._aspects.remove(aspect)
        return

    def get_metrics(self) -> Optional[UbuntuSshReliabilityMetrics]:
        """
            Return the accumulated execution metrics for this runner.
        """
        return self._metrics

    def reset_metrics(self) -> None:
        """
            Reset all execution counters.
        """
        self._metrics = UbuntuSshReliabilityMetrics()
        return

    def run(
        self,
        args: list[str],
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> UbuntuRunResult:
        """
            Execute a single command locally.

            :param args: Argument vector for the command.
            :param elevated: When True, prepend ``sudo -n`` to the argv
                             (and ``-E`` when ``env`` is supplied).
            :param cwd: Working directory for the command.
            :param env: Environment overrides.  Behaviour controlled by
                        :attr:`UbuntuLocalRunnerConfig.merge_env_with_process`.
            :param stdin: Optional stdin payload passed via
                          :func:`subprocess.run`'s ``input`` parameter.
            :param timeout: Timeout in seconds; ``None`` means no
                            timeout.
            :param on_output: Callback invoked with the captured stdout.
            :param on_error: Callback invoked with the captured stderr.
            :param capture_output: When False, stdout/stderr are
                                   inherited from the parent process and
                                   the callbacks receive nothing.

            :returns: A :class:`UbuntuRunResult`.
        """
        context = UbuntuExecutionContext(
            args=list(args),
            elevated=elevated,
            cwd=cwd,
            env=env,
            timeout=timeout,
            transport="local",
            dry_run=self._config.dry_run,
        )

        self._apply_before_aspects(context=context)

        if context.dry_run is True:
            dry_result = UbuntuRunResult(
                exit_code=0,
                stdout="",
                stderr="",
                command=list(context.args),
                dry_run=True,
                transport="local",
            )
            final_dry = self._apply_after_aspects(context=context, result=dry_result)
            return final_dry

        invocation_args = list(context.args)
        if context.elevated is True:
            sudo_prefix = [self._config.sudo_path, "-n"]
            if context.env is not None:
                sudo_prefix.append("-E")
            invocation_args = sudo_prefix + invocation_args

        invocation_env = self._compose_env(env=context.env)

        start_monotonic = time.monotonic()
        invocation_result = self._invoke_subprocess(
            args=invocation_args,
            cwd=context.cwd,
            env=invocation_env,
            timeout=context.timeout,
            capture_output=capture_output,
            on_output=on_output,
            on_error=on_error,
            stdin=stdin,
        )
        end_monotonic = time.monotonic()

        invocation_result.duration_ms = (end_monotonic - start_monotonic) * 1000.0
        self._update_metrics(result=invocation_result)
        final_result = self._apply_after_aspects(
            context=context,
            result=invocation_result,
        )
        return final_result

    def _compose_env(
        self,
        *,
        env: Optional[dict[str, str]],
    ) -> Optional[dict[str, str]]:
        """
            Compose the environment dict that will be handed to
            :func:`subprocess.run`.

            :param env: Caller-supplied env overrides.

            :returns: ``None`` when ``env`` is ``None`` (subprocess
                      inherits the parent env), or a dict assembled per
                      the runner's merge policy.
        """
        if env is None:
            return None

        if self._config.merge_env_with_process is True:
            merged_env = dict(os.environ)
            merged_env.update(env)
            return merged_env

        replacement_env = dict(env)
        return replacement_env

    def _update_metrics(self, *, result: UbuntuRunResult) -> None:
        """
            Update the cumulative metrics based on a single subprocess
            invocation outcome.
        """
        if result.exit_code == 0 and result.timed_out is False:
            self._metrics.success_count = self._metrics.success_count + 1
        else:
            self._metrics.failure_count = self._metrics.failure_count + 1

        if result.timed_out is True:
            self._metrics.timeout_count = self._metrics.timeout_count + 1

        if len(result.stderr) > 0 and result.exit_code != 0:
            self._metrics.last_error = result.stderr

        if result.duration_ms is not None:
            self._metrics.total_duration_ms = (
                self._metrics.total_duration_ms + result.duration_ms
            )
            if self._metrics.min_duration_ms is None:
                self._metrics.min_duration_ms = result.duration_ms
            elif result.duration_ms < self._metrics.min_duration_ms:
                self._metrics.min_duration_ms = result.duration_ms
            if self._metrics.max_duration_ms is None:
                self._metrics.max_duration_ms = result.duration_ms
            elif result.duration_ms > self._metrics.max_duration_ms:
                self._metrics.max_duration_ms = result.duration_ms
            total_count = self._metrics.success_count + self._metrics.failure_count
            if total_count > 0:
                self._metrics.avg_duration_ms = (
                    self._metrics.total_duration_ms / total_count
                )
        return

    def _apply_before_aspects(
        self,
        *,
        context: UbuntuExecutionContext,
    ) -> None:
        """
            Invoke ``before_execute`` on every enabled aspect in
            registration order.
        """
        for aspect in self._aspects:
            if aspect.enabled is True:
                aspect.before_execute(context)
        return

    def _apply_after_aspects(
        self,
        *,
        context: UbuntuExecutionContext,
        result: UbuntuRunResult,
    ) -> UbuntuRunResult:
        """
            Invoke ``after_execute`` on every enabled aspect in
            reverse registration order, threading the result through
            each.
        """
        current = result
        for aspect in reversed(self._aspects):
            if aspect.enabled is True:
                current = aspect.after_execute(context, current)
        return current

    def _invoke_subprocess(
        self,
        *,
        args: list[str],
        cwd: Optional[str],
        env: Optional[dict[str, str]],
        timeout: Optional[float],
        capture_output: bool,
        on_output: Optional[Callable[[str], None]],
        on_error: Optional[Callable[[str], None]],
        stdin: Optional[str],
    ) -> UbuntuRunResult:
        """
            Invoke :func:`subprocess.run` and map the outcome to a
            :class:`UbuntuRunResult`.
        """
        try:
            proc = subprocess.run(
                args,
                cwd=cwd,
                env=env,
                timeout=timeout,
                capture_output=capture_output,
                text=True,
                input=stdin,
            )
        except subprocess.TimeoutExpired as te:
            partial_stdout: str = ""
            partial_stderr: str = "Command timed out"
            if te.stdout is not None:
                if isinstance(te.stdout, bytes):
                    partial_stdout = te.stdout.decode(errors="replace")
                else:
                    partial_stdout = te.stdout
            if te.stderr is not None:
                if isinstance(te.stderr, bytes):
                    partial_stderr = te.stderr.decode(errors="replace")
                else:
                    partial_stderr = te.stderr

            timeout_result = UbuntuRunResult(
                exit_code=-1,
                stdout=partial_stdout,
                stderr=partial_stderr,
                command=list(args),
                timed_out=True,
                transport="local",
            )
            return timeout_result
        except FileNotFoundError as fnf:
            missing_result = UbuntuRunResult(
                exit_code=127,
                stdout="",
                stderr=str(fnf),
                command=list(args),
                transport="local",
            )
            return missing_result

        stdout_text = ""
        stderr_text = ""
        if proc.stdout is not None:
            stdout_text = proc.stdout
        if proc.stderr is not None:
            stderr_text = proc.stderr

        if on_output is not None and len(stdout_text) > 0:
            on_output(stdout_text)
        if on_error is not None and len(stderr_text) > 0:
            on_error(stderr_text)

        subprocess_result = UbuntuRunResult(
            exit_code=proc.returncode,
            stdout=stdout_text,
            stderr=stderr_text,
            command=list(args),
            transport="local",
        )
        return subprocess_result
