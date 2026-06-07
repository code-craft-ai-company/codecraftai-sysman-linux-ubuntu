"""
    SSH command runner for Ubuntu.

    The Ubuntu SSH runner is a *skeleton* in this milestone.  Three
    deployment modes are anticipated:

    1. Wrap ``codecraftai-protocol-ssh`` if the package is importable in
       the host environment.
    2. Provide a thin ``paramiko`` adapter when ``paramiko`` is
       installed.
    3. Raise :class:`NotImplementedError` with a clear message when
       neither dependency is available.

    None of the dependencies above are listed in this package's
    ``pyproject.toml``.  Adding a runtime dependency is out of scope
    for this batch, so :class:`UbuntuSshCommandRunner` ships in mode 3:
    construction succeeds (callers can introspect it for protocol
    conformance and aspect wiring), but :meth:`run` raises a typed
    :class:`NotImplementedError` describing the missing dependency.

    The class deliberately preserves the same surface as
    :class:`UbuntuLocalCommandRunner` (aspect registry, metrics,
    ``run`` signature) so that a future patch supplying the SSH
    backend can fill in :meth:`_dispatch_remote` without changing any
    caller code.
"""

import time

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

from ccai.sysman.linux.ubuntu.runners.models import (
    UbuntuExecutionContext,
    UbuntuRunResult,
    UbuntuSshErrorType,
    UbuntuSshReliabilityMetrics,
)


if TYPE_CHECKING:
    from ccai.sysman.linux.ubuntu.runners.aspects.base import IUbuntuExecutionAspect


__all__ = [
    "UbuntuSshCommandRunner",
    "UbuntuSshRunnerConfig",
]


_NOT_IMPLEMENTED_MSG = (
    "UbuntuSshCommandRunner.run is not yet wired to a transport. "
    "Either install one of 'codecraftai-protocol-ssh' or 'paramiko' and "
    "supply a backend, or use UbuntuLocalCommandRunner for local "
    "execution."
)


@dataclass
class UbuntuSshRunnerConfig:
    """
        Configuration for :class:`UbuntuSshCommandRunner`.

        Until the SSH backend lands, the only field consumed by the
        skeleton is ``dry_run``.  Every other field is preserved
        verbatim so that a future patch can construct an SshAgent
        without breaking existing call sites.

        :param host: Target host.
        :param username: SSH username.
        :param port: TCP port for SSH.
        :param password: Password credential (optional).
        :param key_filename: Path to a private key file (optional).
        :param agent: Pre-built backend instance, when the caller wants
                      to inject its own.
        :param dry_run: When True, ``run`` returns synthetic success.
        :param sudo_path: Path to sudo on the remote host.
        :param default_timeout: Default per-command timeout.
    """

    host: Optional[str] = None
    username: Optional[str] = None
    port: int = 22
    password: Optional[str] = None
    key_filename: Optional[str] = None
    agent: Optional[Any] = None
    dry_run: bool = False
    sudo_path: str = "/usr/bin/sudo"
    default_timeout: float = 60.0


class UbuntuSshCommandRunner:
    """
        SSH-transport command runner for Ubuntu.  Skeleton; see the
        module docstring.
    """

    def __init__(self, config: UbuntuSshRunnerConfig) -> None:
        """
            :param config: SSH runner configuration.
        """
        self._config: UbuntuSshRunnerConfig = config
        self._aspects: list["IUbuntuExecutionAspect"] = []
        self._metrics: UbuntuSshReliabilityMetrics = UbuntuSshReliabilityMetrics()
        self._agent: Any = config.agent
        return

    @property
    def config(self) -> UbuntuSshRunnerConfig:
        """
            The active :class:`UbuntuSshRunnerConfig`.
        """
        return self._config

    @property
    def agent(self) -> Any:
        """
            The injected backend agent, or ``None``.
        """
        return self._agent

    @property
    def aspects(self) -> list["IUbuntuExecutionAspect"]:
        """
            Snapshot of registered aspects.
        """
        rtnval = list(self._aspects)
        return rtnval

    def add_aspect(self, aspect: "IUbuntuExecutionAspect") -> None:
        """
            Register an aspect for subsequent executions.
        """
        self._aspects.append(aspect)
        return

    def remove_aspect(self, aspect: "IUbuntuExecutionAspect") -> None:
        """
            Unregister a previously registered aspect.
        """
        if aspect in self._aspects:
            self._aspects.remove(aspect)
        return

    def get_metrics(self) -> Optional[UbuntuSshReliabilityMetrics]:
        """
            Return the reliability metrics.
        """
        return self._metrics

    def reset_metrics(self) -> None:
        """
            Reset the reliability counters.
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
            Execute a command remotely.  In skeleton mode, the only
            supported path is dry-run: any non-dry-run invocation
            raises :class:`NotImplementedError`.
        """
        context = UbuntuExecutionContext(
            args=list(args),
            elevated=elevated,
            cwd=cwd,
            env=env,
            timeout=timeout,
            transport="ssh",
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
                transport="ssh",
            )
            final_dry = self._apply_after_aspects(
                context=context,
                result=dry_result,
            )
            return final_dry

        if self._agent is None:
            err_msg_lines = [_NOT_IMPLEMENTED_MSG]
            err_msg_lines.append("    HOST: {}".format(self._config.host))
            err_msg_lines.append("    USER: {}".format(self._config.username))
            err_msg = "\n".join(err_msg_lines)
            raise NotImplementedError(err_msg)

        start_monotonic = time.monotonic()
        dispatch_result = self._dispatch_remote(
            context=context,
            stdin=stdin,
            on_output=on_output,
            on_error=on_error,
            capture_output=capture_output,
        )
        end_monotonic = time.monotonic()
        dispatch_result.duration_ms = (end_monotonic - start_monotonic) * 1000.0

        self._update_metrics(result=dispatch_result, error=None)
        final_result = self._apply_after_aspects(
            context=context,
            result=dispatch_result,
        )
        return final_result

    def _dispatch_remote(
        self,
        *,
        context: UbuntuExecutionContext,
        stdin: Optional[str],
        on_output: Optional[Callable[[str], None]],
        on_error: Optional[Callable[[str], None]],
        capture_output: bool,
    ) -> UbuntuRunResult:
        """
            Dispatch the command via the configured agent.

            The skeleton form raises :class:`NotImplementedError`.  A
            future patch will replace the body with calls into either
            ``codecraftai-protocol-ssh`` or a ``paramiko`` channel.
        """
        err_msg_lines = [_NOT_IMPLEMENTED_MSG]
        err_msg_lines.append("    AGENT: {!r}".format(self._agent))
        err_msg = "\n".join(err_msg_lines)
        raise NotImplementedError(err_msg)

    def _update_metrics(
        self,
        *,
        result: UbuntuRunResult,
        error: Optional[Exception],
    ) -> None:
        """
            Update the cumulative metrics with a single execution
            outcome.
        """
        if result.exit_code == 0 and result.timed_out is False:
            self._metrics.success_count = self._metrics.success_count + 1
        else:
            self._metrics.failure_count = self._metrics.failure_count + 1

        if result.timed_out is True:
            self._metrics.timeout_count = self._metrics.timeout_count + 1

        if error is not None:
            self._metrics.last_error = str(error)
            self._metrics.last_error_type = UbuntuSshErrorType.UNKNOWN_ERROR
            self._metrics.connection_error_count = (
                self._metrics.connection_error_count + 1
            )

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
            Invoke ``before_execute`` on every enabled aspect.
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
            Invoke ``after_execute`` in reverse registration order.
        """
        current = result
        for aspect in reversed(self._aspects):
            if aspect.enabled is True:
                current = aspect.after_execute(context, current)
        return current
