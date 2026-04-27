"""
    Base protocol shared by every Ubuntu execution aspect.

    Aspects compose cross-cutting behaviour (persistence, retry,
    timeout) onto an :class:`UbuntuLocalCommandRunner` /
    :class:`UbuntuSshCommandRunner` without modifying the runner.  The
    runner invokes ``before_execute`` in registration order before
    dispatching the command and ``after_execute`` in reverse order
    afterwards, threading the :class:`UbuntuRunResult` through each
    aspect.
"""

from typing import Protocol, runtime_checkable

from automojo.sysman.linux.ubuntu.runners.models import (
    UbuntuExecutionContext,
    UbuntuRunResult,
)


__all__ = [
    "IUbuntuExecutionAspect",
]


@runtime_checkable
class IUbuntuExecutionAspect(Protocol):
    """
        Protocol that every Ubuntu execution aspect satisfies.
    """

    def before_execute(self, context: UbuntuExecutionContext) -> None:
        """
            Invoked before the command is dispatched.  Aspects may
            inspect or mutate ``context``; raising aborts execution.

            :param context: The execution context for the pending
                            command.
        """
        ...

    def after_execute(
        self,
        context: UbuntuExecutionContext,
        result: UbuntuRunResult,
    ) -> UbuntuRunResult:
        """
            Invoked after the command has completed.  Aspects may
            inspect or transform ``result`` before returning it.

            :param context: The execution context for the completed
                            command.
            :param result: The result returned by the runner.

            :returns: The (possibly modified) :class:`UbuntuRunResult`.
        """
        ...

    @property
    def enabled(self) -> bool:
        """
            Whether the aspect is currently enabled.  Disabled aspects
            are skipped by the runner.
        """
        ...
