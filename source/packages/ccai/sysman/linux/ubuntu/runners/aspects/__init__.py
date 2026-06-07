"""
    Execution aspects that compose cross-cutting behaviour onto the
    Ubuntu command runners.
"""

from ccai.sysman.linux.ubuntu.runners.aspects.base import IUbuntuExecutionAspect
from ccai.sysman.linux.ubuntu.runners.aspects.persistence import (
    UbuntuPersistenceAspect,
    UbuntuPersistenceAspectConfig,
)
from ccai.sysman.linux.ubuntu.runners.aspects.retry import (
    UbuntuRetryAspect,
    UbuntuRetryAspectConfig,
    UbuntuRetryDecision,
)
from ccai.sysman.linux.ubuntu.runners.aspects.timeout import (
    UbuntuTimeoutAspect,
    UbuntuTimeoutAspectConfig,
)


__all__ = [
    "IUbuntuExecutionAspect",
    "UbuntuPersistenceAspect",
    "UbuntuPersistenceAspectConfig",
    "UbuntuRetryAspect",
    "UbuntuRetryAspectConfig",
    "UbuntuRetryDecision",
    "UbuntuTimeoutAspect",
    "UbuntuTimeoutAspectConfig",
]
