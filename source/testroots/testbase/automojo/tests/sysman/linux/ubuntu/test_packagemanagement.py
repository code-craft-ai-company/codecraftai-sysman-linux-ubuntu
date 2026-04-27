"""
    Tests for :class:`UbuntuPackageManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call and
    returns pre-seeded :class:`RunResult` values keyed by argv tuple.  No
    live ``apt-get`` / ``dpkg`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.packagemanagement import (
    UbuntuPackageActionStatus,
    UbuntuPackageManager,
    UbuntuPackageManagerConfig,
    UbuntuPackageQueryError,
)


_DPKG_QUERY_FORMAT_ARG = "-f=${Package}\t${Version}\t${Architecture}\t${Status}\n"


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

        rtnval = RunResult(
            exit_code=0,
            stdout="",
            stderr="",
        )
        return rtnval


def _dpkg_l_installed_result(name: str) -> RunResult:
    """
        Build a canonical ``dpkg -l`` result for an installed package.
    """
    body = (
        "Desired=Unknown/Install/Remove/Purge/Hold\n"
        "| Status=Not/Inst/Conf-files/Unpacked/halF-conf/Half-inst/trig-aWait/"
        "Trig-pend\n"
        "|/ Err?=(none)/Reinst-required (Status,Err: uppercase=bad)\n"
        "||/ Name           Version           Architecture Description\n"
        "+++-==============-=================-============-"
        "===================================\n"
        "ii  {}          1.18.0-0ubuntu1   amd64        web server\n"
    ).format(name)
    rtnval = RunResult(exit_code=0, stdout=body, stderr="")
    return rtnval


def _dpkg_l_not_installed_result(name: str) -> RunResult:
    """
        Build a canonical ``dpkg -l`` result for a not-installed package.
    """
    body = (
        "dpkg-query: no packages found matching {}\n"
    ).format(name)
    rtnval = RunResult(exit_code=1, stdout="", stderr=body)
    return rtnval


def _dpkg_s_installed_result(
    *,
    name: str,
    version: str = "1.18.0-0ubuntu1",
    architecture: str = "amd64",
) -> RunResult:
    """
        Build a canonical ``dpkg -s`` result for an installed package.
    """
    body = (
        "Package: {}\n"
        "Status: install ok installed\n"
        "Priority: optional\n"
        "Section: web\n"
        "Maintainer: Ubuntu Devs <devs@ubuntu.com>\n"
        "Architecture: {}\n"
        "Source: {} (1.18.0-0ubuntu1)\n"
        "Version: {}\n"
        "Description: small web server\n"
        " A high-performance reverse proxy.\n"
    ).format(name, architecture, name, version)
    rtnval = RunResult(exit_code=0, stdout=body, stderr="")
    return rtnval


def _dpkg_s_not_installed_result(name: str) -> RunResult:
    """
        Build a canonical ``dpkg -s`` result for a not-installed package.
    """
    body = (
        "dpkg-query: package '{}' is not installed and no information"
        " is available\n"
    ).format(name)
    rtnval = RunResult(exit_code=1, stdout="", stderr=body)
    return rtnval


def _apt_cache_policy_result(
    *,
    installed: Optional[str],
    candidate: Optional[str],
) -> RunResult:
    """
        Build a canonical ``apt-cache policy <name>`` result.
    """
    installed_token = installed if installed is not None else "(none)"
    candidate_token = candidate if candidate is not None else "(none)"
    body = (
        "nginx:\n"
        "  Installed: {}\n"
        "  Candidate: {}\n"
        "  Version table:\n"
    ).format(installed_token, candidate_token)
    rtnval = RunResult(exit_code=0, stdout=body, stderr="")
    return rtnval


# ----------------------------------------------------------------------
# install_package
# ----------------------------------------------------------------------


def test_install_package_invokes_apt_install():
    """
        install_package on a missing package dispatches ``apt-get install``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("dpkg", "-s", "nginx")] = _dpkg_s_not_installed_result(
        name="nginx",
    )

    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(elevated=True),
    )
    result = manager.install_package(name="nginx")

    testbase.assert_equal(result.status, UbuntuPackageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["apt-get", "install", "-y"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"], ["apt-get", "install", "-y", "nginx"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    testbase.assert_equal(
        write_calls[0]["env"], {"DEBIAN_FRONTEND": "noninteractive"},
    )
    return


def test_install_package_skipped_when_already_installed():
    """
        install_package returns SKIPPED when the package is already
        installed (any version).
    """
    runner = _MockCommandRunner()
    runner.results[("dpkg", "-s", "nginx")] = _dpkg_s_installed_result(
        name="nginx", version="1.18.0-0ubuntu1",
    )

    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(),
    )
    result = manager.install_package(name="nginx")

    testbase.assert_equal(result.status, UbuntuPackageActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["apt-get", "install", "-y"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_install_package_with_version_constructs_correct_argv():
    """
        install_package(name, version) dispatches
        ``apt-get install -y name=version``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("dpkg", "-s", "nginx")] = _dpkg_s_not_installed_result(
        name="nginx",
    )

    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(elevated=True),
    )
    result = manager.install_package(name="nginx", version="1.20.0")

    testbase.assert_equal(result.status, UbuntuPackageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["apt-get", "install", "-y"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["apt-get", "install", "-y", "nginx=1.20.0"],
    )
    return


# ----------------------------------------------------------------------
# remove_package
# ----------------------------------------------------------------------


def test_remove_package_invokes_apt_remove():
    """
        remove_package on an installed package dispatches
        ``apt-get remove``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("dpkg", "-l", "nginx")] = _dpkg_l_installed_result(
        name="nginx",
    )

    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(elevated=True),
    )
    result = manager.remove_package(name="nginx")

    testbase.assert_equal(result.status, UbuntuPackageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["apt-get", "remove", "-y"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"], ["apt-get", "remove", "-y", "nginx"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_remove_package_skipped_when_not_installed():
    """
        remove_package on a missing package returns SKIPPED.
    """
    runner = _MockCommandRunner()
    runner.results[("dpkg", "-l", "nginx")] = _dpkg_l_not_installed_result(
        name="nginx",
    )

    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(),
    )
    result = manager.remove_package(name="nginx")

    testbase.assert_equal(result.status, UbuntuPackageActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["apt-get", "remove", "-y"]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# upgrade_package / upgrade_all
# ----------------------------------------------------------------------


def test_upgrade_package_invokes_apt_install_only_upgrade():
    """
        upgrade_package dispatches ``apt-get install -y --only-upgrade``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("apt-cache", "policy", "nginx")] = (
        _apt_cache_policy_result(installed="1.18.0", candidate="1.20.0")
    )

    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(elevated=True),
    )
    result = manager.upgrade_package(name="nginx")

    testbase.assert_equal(result.status, UbuntuPackageActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:4] == ["apt-get", "install", "-y", "--only-upgrade"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["apt-get", "install", "-y", "--only-upgrade", "nginx"],
    )
    return


def test_upgrade_package_skipped_when_already_at_latest():
    """
        upgrade_package returns SKIPPED when installed == candidate.
    """
    runner = _MockCommandRunner()
    runner.results[("apt-cache", "policy", "nginx")] = (
        _apt_cache_policy_result(installed="1.20.0", candidate="1.20.0")
    )

    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(),
    )
    result = manager.upgrade_package(name="nginx")

    testbase.assert_equal(result.status, UbuntuPackageActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:4] == ["apt-get", "install", "-y", "--only-upgrade"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_upgrade_all_invokes_apt_upgrade():
    """
        upgrade_all dispatches ``apt-get upgrade -y``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(elevated=True),
    )
    result = manager.upgrade_all()

    testbase.assert_equal(result.status, UbuntuPackageActionStatus.SUCCESS)
    testbase.assert_equal(
        runner.calls[-1]["args"], ["apt-get", "upgrade", "-y"],
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    testbase.assert_equal(
        runner.calls[-1]["env"], {"DEBIAN_FRONTEND": "noninteractive"},
    )
    return


# ----------------------------------------------------------------------
# is_installed
# ----------------------------------------------------------------------


def test_is_installed_returns_true_for_installed():
    """
        is_installed returns True when ``dpkg -l`` reports ``ii``.
    """
    runner = _MockCommandRunner()
    runner.results[("dpkg", "-l", "nginx")] = _dpkg_l_installed_result(
        name="nginx",
    )
    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(),
    )
    result = manager.is_installed(name="nginx")
    testbase.assert_true(result)
    return


def test_is_installed_returns_false_for_not_installed():
    """
        is_installed returns False when ``dpkg -l`` exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("dpkg", "-l", "ghost")] = _dpkg_l_not_installed_result(
        name="ghost",
    )
    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(),
    )
    result = manager.is_installed(name="ghost")
    testbase.assert_false(result)
    return


def test_is_installed_returns_false_for_rc_status():
    """
        is_installed returns False when the status is not ``ii`` (e.g. ``rc``).
    """
    runner = _MockCommandRunner()
    rc_body = (
        "Desired=Unknown/Install/Remove/Purge/Hold\n"
        "| Status=Not/Inst\n"
        "||/ Name      Version  Architecture Description\n"
        "+++-=========-========-============-=========================\n"
        "rc  nginx     1.18.0   amd64        small web server\n"
    )
    runner.results[("dpkg", "-l", "nginx")] = RunResult(
        exit_code=0, stdout=rc_body, stderr="",
    )
    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(),
    )
    result = manager.is_installed(name="nginx")
    testbase.assert_false(result)
    return


# ----------------------------------------------------------------------
# get_package_info / list_installed
# ----------------------------------------------------------------------


def test_get_package_info_parses_dpkg_s_output():
    """
        get_package_info parses ``dpkg -s`` into a :class:`UbuntuPackageInfo`.
    """
    runner = _MockCommandRunner()
    runner.results[("dpkg", "-s", "nginx")] = _dpkg_s_installed_result(
        name="nginx", version="1.18.0-0ubuntu1", architecture="amd64",
    )
    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(),
    )
    info = manager.get_package_info(name="nginx")

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.name, "nginx")
    testbase.assert_equal(info.version, "1.18.0-0ubuntu1")
    testbase.assert_equal(info.architecture, "amd64")
    testbase.assert_equal(info.status, "install ok installed")
    testbase.assert_equal(info.description, "small web server")
    testbase.assert_equal(info.source, "nginx (1.18.0-0ubuntu1)")
    return


def test_get_package_info_returns_none_when_not_installed():
    """
        get_package_info returns ``None`` when ``dpkg -s`` reports
        "not installed".
    """
    runner = _MockCommandRunner()
    runner.results[("dpkg", "-s", "ghost")] = _dpkg_s_not_installed_result(
        name="ghost",
    )
    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(),
    )
    info = manager.get_package_info(name="ghost")
    testbase.assert_equal(info, None)
    return


def test_list_installed_parses_dpkg_query_output_and_filters_removed():
    """
        list_installed parses the tab output and filters rows whose
        Status does not begin with ``install ok installed``.
    """
    body = (
        "adduser\t3.118ubuntu5\tall\tinstall ok installed\n"
        "apt\t2.4.12\tamd64\tinstall ok installed\n"
        "removed-pkg\t0.0.1\tamd64\tdeinstall ok config-files\n"
        "ghost-pkg\t1.0\tamd64\tinstall ok unpacked\n"
        "base-files\t12ubuntu4.6\tamd64\tinstall ok installed\n"
    )
    runner = _MockCommandRunner()
    runner.results[("dpkg-query", "-W", _DPKG_QUERY_FORMAT_ARG)] = RunResult(
        exit_code=0, stdout=body, stderr="",
    )

    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(),
    )
    rows = manager.list_installed()

    testbase.assert_equal(len(rows), 3)

    first = rows[0]
    testbase.assert_equal(first.name, "adduser")
    testbase.assert_equal(first.version, "3.118ubuntu5")
    testbase.assert_equal(first.architecture, "all")
    testbase.assert_equal(first.status, "install ok installed")

    last = rows[-1]
    testbase.assert_equal(last.name, "base-files")
    return


# ----------------------------------------------------------------------
# refresh_metadata
# ----------------------------------------------------------------------


def test_refresh_metadata_invokes_apt_update():
    """
        refresh_metadata dispatches ``apt-get update``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(elevated=True),
    )
    result = manager.refresh_metadata()

    testbase.assert_equal(result.status, UbuntuPackageActionStatus.SUCCESS)
    testbase.assert_equal(
        runner.calls[-1]["args"], ["apt-get", "update"],
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    testbase.assert_equal(
        runner.calls[-1]["env"], {"DEBIAN_FRONTEND": "noninteractive"},
    )
    return


# ----------------------------------------------------------------------
# Dry-run, elevation, error surfaces
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_install():
    """
        Dry-run short-circuits install_package without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuPackageManager(
        runner=runner,
        config=UbuntuPackageManagerConfig(dry_run=True),
    )
    result = manager.install_package(name="nginx")

    testbase.assert_equal(result.status, UbuntuPackageActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command, ["apt-get", "install", "-y", "nginx"],
    )
    return


def test_write_verbs_pass_elevated_true():
    """
        Every write verb passes ``elevated=True`` when the config is
        configured to elevate.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("dpkg", "-s", "nginx")] = _dpkg_s_not_installed_result(
        name="nginx",
    )
    runner.results[("dpkg", "-l", "curl")] = _dpkg_l_installed_result(
        name="curl",
    )
    runner.results[("apt-cache", "policy", "vim")] = _apt_cache_policy_result(
        installed="2.0", candidate="2.1",
    )

    manager = UbuntuPackageManager(
        runner=runner,
        config=UbuntuPackageManagerConfig(elevated=True),
    )
    manager.install_package(name="nginx")
    manager.remove_package(name="curl")
    manager.upgrade_package(name="vim")
    manager.upgrade_all()
    manager.refresh_metadata()

    write_calls = [
        c for c in runner.calls if c["args"][0] == "apt-get"
    ]
    testbase.assert_true(len(write_calls) >= 5)
    for call in write_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_read_verbs_never_elevate():
    """
        Reads and idempotency-probe commands never elevate, even when the
        config is configured to elevate writes.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("dpkg", "-l", "nginx")] = _dpkg_l_installed_result(
        name="nginx",
    )
    runner.results[("dpkg", "-s", "nginx")] = _dpkg_s_installed_result(
        name="nginx",
    )
    runner.results[("dpkg-query", "-W", _DPKG_QUERY_FORMAT_ARG)] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[("apt-cache", "policy", "nginx")] = _apt_cache_policy_result(
        installed="1.18.0", candidate="1.20.0",
    )

    manager = UbuntuPackageManager(
        runner=runner,
        config=UbuntuPackageManagerConfig(elevated=True),
    )
    manager.is_installed(name="nginx")
    manager.get_package_info(name="nginx")
    manager.list_installed()
    # Probe via a write-verb idempotency path (the apt-cache policy probe
    # for upgrade_package must not elevate even though the write that
    # follows will).
    manager.upgrade_package(name="nginx")

    read_argv_heads = (
        ["dpkg", "-l"],
        ["dpkg", "-s"],
        ["dpkg-query", "-W"],
        ["apt-cache", "policy"],
    )
    for call in runner.calls:
        if any(call["args"][: len(head)] == head for head in read_argv_heads):
            testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr_in_result():
    """
        A non-zero exit on a write maps to FAILED with the captured
        stderr / exit_code / command.
    """
    runner = _MockCommandRunner()
    runner.results[("dpkg", "-s", "nginx")] = _dpkg_s_not_installed_result(
        name="nginx",
    )
    runner.results[("apt-get", "install", "-y", "nginx")] = RunResult(
        exit_code=100,
        stdout="",
        stderr="E: Unable to locate package nginx",
    )
    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(elevated=True),
    )
    result = manager.install_package(name="nginx")

    testbase.assert_equal(result.status, UbuntuPackageActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 100)
    testbase.assert_equal(
        result.stderr, "E: Unable to locate package nginx",
    )
    testbase.assert_equal(
        result.command, ["apt-get", "install", "-y", "nginx"],
    )
    return


def test_list_installed_raises_on_runner_failure():
    """
        list_installed raises :class:`UbuntuPackageQueryError` on
        non-zero exit.
    """
    runner = _MockCommandRunner()
    runner.results[("dpkg-query", "-W", _DPKG_QUERY_FORMAT_ARG)] = RunResult(
        exit_code=2, stdout="", stderr="boom",
    )
    manager = UbuntuPackageManager(
        runner=runner, config=UbuntuPackageManagerConfig(),
    )
    raised = False
    try:
        manager.list_installed()
    except UbuntuPackageQueryError:
        raised = True
    testbase.assert_true(raised)
    return
