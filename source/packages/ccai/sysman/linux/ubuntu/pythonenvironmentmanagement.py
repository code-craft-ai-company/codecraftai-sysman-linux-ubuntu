"""
    Ubuntu python-environment-management feature.

    Implements the cross-OS python-environment-management contract for
    Ubuntu (and Debian-family) hosts.  All venv / pip behaviour is
    inherited from :class:`PythonEnvironmentManagementBase`; this module
    only selects the POSIX layout (``<venv>/bin/python``, ``python3``
    launcher, ``test -d`` / ``rm -rf`` probes) and adds the Ubuntu
    prerequisite step.

    Prerequisites:
        Debian-family hosts split the standard library's ``venv`` /
        ``ensurepip`` support into the ``python3-venv`` package, so
        ``python3 -m venv`` fails on a stock image until that package is
        installed.  :meth:`UbuntuPythonEnvironmentManager.ensure_prerequisites`
        installs ``python3``, ``python3-venv``, and ``python3-pip`` through
        :class:`UbuntuPackageManager` — the existing apt feature — rather
        than re-implementing apt calls here.

    See:
        * :doc:`/specs/contracts/python-environment-management/contract.md`
        * :doc:`/specs/python-environment-management-base/design.md`
        * :doc:`/specs/3-python-environment-management/design.md`
"""

from dataclasses import dataclass
from typing import List, Optional

from ccai.sysman.common.pythonenvironmentlayout import (
    PosixPythonEnvironmentLayout,
    PythonEnvironmentLayout,
)
from ccai.sysman.common.pythonenvironmentmanagementbase import (
    PythonEnvActionResult,
    PythonEnvActionStatus,
    PythonEnvDeployResult,
    PythonEnvInfo,
    PythonEnvManagerConfig,
    PythonEnvQueryError,
    PythonEnvSpec,
    PythonEnvironmentManagementBase,
    PythonPackageInfo,
)

from ccai.sysman.linux.ubuntu.packagemanagement import (
    UbuntuPackageActionResult,
    UbuntuPackageActionStatus,
    UbuntuPackageManager,
    UbuntuPackageManagerConfig,
)


UbuntuPythonEnvActionStatus = PythonEnvActionStatus
UbuntuPythonEnvActionResult = PythonEnvActionResult
UbuntuPythonEnvDeployResult = PythonEnvDeployResult
UbuntuPythonEnvInfo = PythonEnvInfo
UbuntuPythonEnvQueryError = PythonEnvQueryError
UbuntuPythonEnvSpec = PythonEnvSpec
UbuntuPythonPackageInfo = PythonPackageInfo


_DEFAULT_PREREQUISITE_PACKAGES = ["python3", "python3-venv", "python3-pip"]


@dataclass
class UbuntuPythonEnvManagerConfig(PythonEnvManagerConfig):
    """
        Configuration for :class:`UbuntuPythonEnvironmentManager`.
        Inherits every contract field from :class:`PythonEnvManagerConfig`.

        :param prerequisite_packages: apt packages installed by
                                      :meth:`ensure_prerequisites`.
                                      ``None`` (the default) means
                                      ``python3``, ``python3-venv``,
                                      ``python3-pip``.
        :param apt_options: Extra flags forwarded to the
                            :class:`UbuntuPackageManagerConfig` used for
                            prerequisite installs.
    """
    prerequisite_packages: Optional[List[str]] = None
    apt_options: Optional[List[str]] = None


class UbuntuPythonEnvironmentManager(PythonEnvironmentManagementBase):
    """
        Ubuntu concrete implementation of the python-environment-management
        contract.
    """

    def _build_layout(self) -> PythonEnvironmentLayout:
        """
            :returns: The POSIX layout.
        """
        rtnval = PosixPythonEnvironmentLayout()
        return rtnval

    def ensure_prerequisites(
        self,
        packages: Optional[List[str]] = None,
    ) -> List[UbuntuPackageActionResult]:
        """
            Install the apt packages needed before ``python3 -m venv``
            can succeed, delegating to :class:`UbuntuPackageManager`.

            Each package is installed in order; the sequence stops at the
            first ``FAILED`` result.  Dry-run and elevation follow this
            manager's config.

            :param packages: Override of the package list; ``None`` uses
                             the config, then the module default.

            :returns: One :class:`UbuntuPackageActionResult` per package
                      attempted, in order.
        """
        names = self._resolve_prerequisite_packages(packages=packages)
        package_manager = self._build_package_manager()

        rtnval: List[UbuntuPackageActionResult] = []
        for name in names:
            result = package_manager.install_package(name=name)
            rtnval.append(result)
            if result.status == UbuntuPackageActionStatus.FAILED:
                break
        return rtnval

    def _resolve_prerequisite_packages(
        self,
        *,
        packages: Optional[List[str]],
    ) -> List[str]:
        """
            Pick the prerequisite list: explicit argument, then config,
            then the module default.

            :param packages: Caller-supplied override or ``None``.

            :returns: A copy of the chosen list.
        """
        if packages is not None and len(packages) > 0:
            rtnval = list(packages)
            return rtnval

        config = self._typed_config()
        if config.prerequisite_packages is not None and len(config.prerequisite_packages) > 0:
            rtnval = list(config.prerequisite_packages)
            return rtnval

        rtnval = list(_DEFAULT_PREREQUISITE_PACKAGES)
        return rtnval

    def _build_package_manager(self) -> UbuntuPackageManager:
        """
            Construct the :class:`UbuntuPackageManager` used for
            prerequisite installs, sharing this manager's runner and its
            ``dry_run`` / ``elevated`` flags.

            :returns: The package manager.
        """
        config = self._typed_config()
        package_config = UbuntuPackageManagerConfig(
            dry_run=config.dry_run,
            elevated=config.elevated,
            apt_options=config.apt_options,
        )
        rtnval = UbuntuPackageManager(runner=self._runner, config=package_config)
        return rtnval

    def _typed_config(self) -> UbuntuPythonEnvManagerConfig:
        """
            Return the config as :class:`UbuntuPythonEnvManagerConfig`,
            upgrading a plain :class:`PythonEnvManagerConfig` so the
            Ubuntu-only fields read as their defaults.

            :returns: The typed config.
        """
        if isinstance(self._config, UbuntuPythonEnvManagerConfig):
            rtnval = self._config
            return rtnval

        rtnval = UbuntuPythonEnvManagerConfig(
            dry_run=self._config.dry_run,
            elevated=self._config.elevated,
            base_interpreter=self._config.base_interpreter,
            index_url=self._config.index_url,
            extra_index_urls=self._config.extra_index_urls,
            trusted_hosts=self._config.trusted_hosts,
            pip_options=self._config.pip_options,
            upgrade_pip=self._config.upgrade_pip,
            system_site_packages=self._config.system_site_packages,
        )
        return rtnval


__all__ = [
    "UbuntuPythonEnvActionResult",
    "UbuntuPythonEnvActionStatus",
    "UbuntuPythonEnvDeployResult",
    "UbuntuPythonEnvInfo",
    "UbuntuPythonEnvManagerConfig",
    "UbuntuPythonEnvQueryError",
    "UbuntuPythonEnvSpec",
    "UbuntuPythonEnvironmentManager",
    "UbuntuPythonPackageInfo",
]
