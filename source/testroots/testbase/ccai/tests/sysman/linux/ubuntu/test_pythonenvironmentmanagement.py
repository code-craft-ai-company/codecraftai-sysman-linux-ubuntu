"""
    Tests for :class:`UbuntuPythonEnvironmentManager`.

    The venv / pip verbs are exercised in depth by the shared base tests
    in ``codecraftai-sysman-common``; these tests cover what the Ubuntu
    subclass adds — POSIX layout selection, the apt prerequisite step,
    and one end-to-end deploy on Ubuntu argv.  All tests use a
    ``_MockCommandRunner``; no live ``apt-get``, ``python3`` or ``pip``
    is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.common.pythonenvironmentlayout import PosixPythonEnvironmentLayout
from ccai.sysman.common.pythonenvironmentmanagementbase import PythonEnvManagerConfig

from ccai.sysman.linux.ubuntu.packagemanagement import UbuntuPackageActionStatus
from ccai.sysman.linux.ubuntu.pythonenvironmentmanagement import (
    UbuntuPythonEnvActionStatus,
    UbuntuPythonEnvManagerConfig,
    UbuntuPythonEnvSpec,
    UbuntuPythonEnvironmentManager,
)


_VENV = "/opt/venvs/app"
_VENV_PYTHON = "/opt/venvs/app/bin/python"
_PIP_PREFIX = [_VENV_PYTHON, "-m", "pip", "--disable-pip-version-check", "--no-input"]
_VERSION_SNIPPET = "import platform; print(platform.python_version())"


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in keyed by argv tuple, with an
        optional list of argv prefixes that fail.
    """

    results: dict = field(default_factory=dict)
    fail_prefixes: list = field(default_factory=list)
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
        self.calls.append({"args": list(args), "elevated": elevated, "env": env})

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        for prefix in self.fail_prefixes:
            if tuple(args[:len(prefix)]) == tuple(prefix):
                rtnval = RunResult(exit_code=100, stdout="", stderr="apt failure")
                return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _apt_install_calls(runner: _MockCommandRunner) -> list:
    rtnval = [c for c in runner.calls if c["args"][:3] == ["apt-get", "install", "-y"]]
    return rtnval


def _pip_argv(*pip_args: str) -> list:
    rtnval = list(_PIP_PREFIX)
    rtnval.extend(pip_args)
    return rtnval


def test_layout_is_posix():
    """
        The Ubuntu manager selects the POSIX layout.
    """
    manager = UbuntuPythonEnvironmentManager(runner=_MockCommandRunner())
    testbase.assert_true(isinstance(manager.layout, PosixPythonEnvironmentLayout))
    testbase.assert_equal(manager.layout.venv_python_path(path=_VENV), _VENV_PYTHON)
    testbase.assert_equal(manager.layout.default_interpreter_argv(), ["python3"])
    return


def test_ensure_prerequisites_installs_default_apt_packages():
    """
        The default prerequisite set is python3, python3-venv, python3-pip
        installed via apt-get with the manager's elevation flag.
    """
    runner = _MockCommandRunner()
    manager = UbuntuPythonEnvironmentManager(
        runner=runner, config=UbuntuPythonEnvManagerConfig(elevated=True),
    )

    results = manager.ensure_prerequisites()

    testbase.assert_equal([r.status for r in results], [UbuntuPackageActionStatus.SUCCESS] * 3)
    installs = _apt_install_calls(runner)
    testbase.assert_equal(
        [c["args"] for c in installs],
        [
            ["apt-get", "install", "-y", "python3"],
            ["apt-get", "install", "-y", "python3-venv"],
            ["apt-get", "install", "-y", "python3-pip"],
        ],
    )
    testbase.assert_equal(all(c["elevated"] is True for c in installs), True)
    testbase.assert_equal(installs[0]["env"], {"DEBIAN_FRONTEND": "noninteractive"})
    return


def test_ensure_prerequisites_honours_override_and_config():
    """
        An explicit package list beats the config list, which beats the
        default; apt_options are forwarded.
    """
    runner = _MockCommandRunner()
    config = UbuntuPythonEnvManagerConfig(
        prerequisite_packages=["python3.12-venv"], apt_options=["--no-install-recommends"],
    )
    manager = UbuntuPythonEnvironmentManager(runner=runner, config=config)

    manager.ensure_prerequisites()
    manager.ensure_prerequisites(packages=["python3.11-venv"])

    testbase.assert_equal(
        [c["args"] for c in _apt_install_calls(runner)],
        [
            ["apt-get", "install", "-y", "python3.12-venv", "--no-install-recommends"],
            ["apt-get", "install", "-y", "python3.11-venv", "--no-install-recommends"],
        ],
    )
    return


def test_ensure_prerequisites_stops_at_first_failure():
    """
        A failed install aborts the remaining packages.
    """
    runner = _MockCommandRunner(fail_prefixes=[["apt-get", "install", "-y", "python3-venv"]])
    manager = UbuntuPythonEnvironmentManager(runner=runner)

    results = manager.ensure_prerequisites()

    testbase.assert_equal(
        [r.status for r in results],
        [UbuntuPackageActionStatus.SUCCESS, UbuntuPackageActionStatus.FAILED],
    )
    testbase.assert_equal(len(_apt_install_calls(runner)), 2)
    return


def test_ensure_prerequisites_dry_run_dispatches_nothing():
    """
        Dry-run yields DRY_RUN per package with no apt-get call.
    """
    runner = _MockCommandRunner()
    manager = UbuntuPythonEnvironmentManager(
        runner=runner, config=UbuntuPythonEnvManagerConfig(dry_run=True),
    )

    results = manager.ensure_prerequisites()

    testbase.assert_equal([r.status for r in results], [UbuntuPackageActionStatus.DRY_RUN] * 3)
    testbase.assert_equal(runner.calls, [])
    return


def test_plain_base_config_is_accepted():
    """
        A plain PythonEnvManagerConfig works; Ubuntu-only fields default.
    """
    runner = _MockCommandRunner()
    manager = UbuntuPythonEnvironmentManager(
        runner=runner, config=PythonEnvManagerConfig(elevated=True, upgrade_pip=False),
    )

    manager.ensure_prerequisites()

    installs = _apt_install_calls(runner)
    testbase.assert_equal(len(installs), 3)
    testbase.assert_equal(installs[0]["elevated"], True)
    return


def test_deploy_end_to_end_on_ubuntu_argv():
    """
        A fresh deploy issues python3 -m venv, pip upgrade and pip install
        in order through the runner.
    """
    runner = _MockCommandRunner()
    runner.results[("test", "-d", _VENV)] = RunResult(exit_code=1, stdout="", stderr="")
    runner.results[(_VENV_PYTHON, "-c", _VERSION_SNIPPET)] = RunResult(exit_code=127, stdout="", stderr="")
    runner.results[tuple(_pip_argv("list", "--format", "json"))] = RunResult(
        exit_code=0, stdout=json.dumps([]), stderr="",
    )
    manager = UbuntuPythonEnvironmentManager(
        runner=runner, config=UbuntuPythonEnvManagerConfig(elevated=True),
    )

    result = manager.deploy(spec=UbuntuPythonEnvSpec(path=_VENV, packages=["requests==2.32.3"]))

    testbase.assert_equal(result.status, UbuntuPythonEnvActionStatus.SUCCESS)
    writes = [c["args"] for c in runner.calls if c["elevated"] is True]
    testbase.assert_equal(
        writes,
        [
            ["python3", "-m", "venv", _VENV],
            _pip_argv("install", "--upgrade", "pip"),
            _pip_argv("install", "requests==2.32.3"),
        ],
    )
    return
