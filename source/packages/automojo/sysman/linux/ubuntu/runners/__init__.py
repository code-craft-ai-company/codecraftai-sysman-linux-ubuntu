"""
    Command runner implementations for the Ubuntu sysman package.

    Public surface:

    - :class:`UbuntuLocalCommandRunner` -- subprocess-backed local
      runner that satisfies
      :class:`automojo.interfaces.icommandrunner.ICommandRunner`.
    - :class:`UbuntuSshCommandRunner` -- skeleton SSH runner that
      preserves the public surface of the local runner so that a
      future patch can plug in the transport without affecting
      callers.
    - :class:`UbuntuRunResult` -- the runner result dataclass.
    - The aspect classes for retry, timeout, and persistence.
"""

from automojo.sysman.linux.ubuntu.runners.aspects import (
    IUbuntuExecutionAspect,
    UbuntuPersistenceAspect,
    UbuntuPersistenceAspectConfig,
    UbuntuRetryAspect,
    UbuntuRetryAspectConfig,
    UbuntuRetryDecision,
    UbuntuTimeoutAspect,
    UbuntuTimeoutAspectConfig,
)
from automojo.sysman.linux.ubuntu.runners.local import (
    UbuntuLocalCommandRunner,
    UbuntuLocalRunnerConfig,
)
from automojo.sysman.linux.ubuntu.runners.models import (
    RunResult,
    UbuntuExecutionContext,
    UbuntuRunRecord,
    UbuntuRunResult,
    UbuntuSshErrorType,
    UbuntuSshReliabilityMetrics,
)
from automojo.sysman.linux.ubuntu.runners.ssh import (
    UbuntuSshCommandRunner,
    UbuntuSshRunnerConfig,
)


__all__ = [
    "IUbuntuExecutionAspect",
    "RunResult",
    "UbuntuExecutionContext",
    "UbuntuLocalCommandRunner",
    "UbuntuLocalRunnerConfig",
    "UbuntuPersistenceAspect",
    "UbuntuPersistenceAspectConfig",
    "UbuntuRetryAspect",
    "UbuntuRetryAspectConfig",
    "UbuntuRetryDecision",
    "UbuntuRunRecord",
    "UbuntuRunResult",
    "UbuntuSshCommandRunner",
    "UbuntuSshErrorType",
    "UbuntuSshReliabilityMetrics",
    "UbuntuSshRunnerConfig",
    "UbuntuTimeoutAspect",
    "UbuntuTimeoutAspectConfig",
]
