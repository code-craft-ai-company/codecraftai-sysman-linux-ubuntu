"""
    Tests for :class:`UbuntuRepositoryManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    File I/O is exercised entirely through the runner (``cat`` / ``tee`` /
    ``rm`` / ``mv`` / ``ls`` / ``test -e``); no live filesystem is touched.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.repositorymanagement import (
    UbuntuRepositoryActionStatus,
    UbuntuRepositoryManager,
    UbuntuRepositoryManagerConfig,
    UbuntuRepositorySpec,
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

        rtnval = RunResult(
            exit_code=0,
            stdout="",
            stderr="",
        )
        return rtnval


def _exists(*, path: str) -> RunResult:
    """
        Build a ``test -e`` result indicating ``path`` exists.
    """
    rtnval = RunResult(exit_code=0, stdout="", stderr="")
    return rtnval


def _missing(*, path: str) -> RunResult:
    """
        Build a ``test -e`` result indicating ``path`` does not exist.
    """
    rtnval = RunResult(exit_code=1, stdout="", stderr="")
    return rtnval


def _make_spec() -> UbuntuRepositorySpec:
    """
        Build a canonical :class:`UbuntuRepositorySpec` used across tests.
    """
    rtnval = UbuntuRepositorySpec(
        name="docker",
        uri="https://download.docker.com/linux/ubuntu",
        suites=["jammy"],
        components=["stable"],
        types=["deb"],
        signed_by="/etc/apt/keyrings/docker.gpg",
        architectures=["amd64"],
    )
    return rtnval


def _expected_docker_body() -> str:
    """
        Build the canonical rendered file body for ``_make_spec``.
    """
    rtnval = (
        "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg]"
        " https://download.docker.com/linux/ubuntu jammy stable\n"
    )
    return rtnval


# ----------------------------------------------------------------------
# add_repository
# ----------------------------------------------------------------------


def test_add_repository_writes_sources_file():
    """
        add_repository writes a new ``.list`` file via ``tee`` when absent.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/etc/apt/sources.list.d/docker.list",
    )] = RunResult(exit_code=1, stdout="", stderr="")

    manager = UbuntuRepositoryManager(
        runner=runner,
        config=UbuntuRepositoryManagerConfig(elevated=True),
    )
    result = manager.add_repository(spec=_make_spec())

    testbase.assert_equal(result.status, UbuntuRepositoryActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"], ["tee", "/etc/apt/sources.list.d/docker.list"],
    )
    testbase.assert_equal(tee_calls[0]["stdin"], _expected_docker_body())
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    testbase.assert_equal(
        result.path, "/etc/apt/sources.list.d/docker.list",
    )
    testbase.assert_equal(result.name, "docker")
    return


def test_add_repository_skipped_when_content_matches():
    """
        add_repository returns SKIPPED when the file already contains
        byte-identical content.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "cat", "/etc/apt/sources.list.d/docker.list",
    )] = RunResult(
        exit_code=0,
        stdout=_expected_docker_body(),
        stderr="",
    )

    manager = UbuntuRepositoryManager(
        runner=runner, config=UbuntuRepositoryManagerConfig(),
    )
    result = manager.add_repository(spec=_make_spec())

    testbase.assert_equal(result.status, UbuntuRepositoryActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


# ----------------------------------------------------------------------
# remove_repository
# ----------------------------------------------------------------------


def test_remove_repository_deletes_file():
    """
        remove_repository runs ``rm -f`` on the ``.list`` path when
        present.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "test", "-e", "/etc/apt/sources.list.d/docker.list",
    )] = _exists(path="/etc/apt/sources.list.d/docker.list")

    manager = UbuntuRepositoryManager(
        runner=runner,
        config=UbuntuRepositoryManagerConfig(elevated=True),
    )
    result = manager.remove_repository(name="docker")

    testbase.assert_equal(result.status, UbuntuRepositoryActionStatus.SUCCESS)
    rm_calls = [c for c in runner.calls if c["args"][0] == "rm"]
    testbase.assert_equal(len(rm_calls), 1)
    testbase.assert_equal(
        rm_calls[0]["args"],
        ["rm", "-f", "/etc/apt/sources.list.d/docker.list"],
    )
    testbase.assert_equal(rm_calls[0]["elevated"], True)
    return


def test_remove_repository_skipped_when_not_found():
    """
        remove_repository returns SKIPPED when neither file variant
        exists.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "test", "-e", "/etc/apt/sources.list.d/ghost.list",
    )] = _missing(path="/etc/apt/sources.list.d/ghost.list")
    runner.results[(
        "test", "-e", "/etc/apt/sources.list.d/ghost.list.disabled",
    )] = _missing(path="/etc/apt/sources.list.d/ghost.list.disabled")

    manager = UbuntuRepositoryManager(
        runner=runner, config=UbuntuRepositoryManagerConfig(),
    )
    result = manager.remove_repository(name="ghost")

    testbase.assert_equal(result.status, UbuntuRepositoryActionStatus.SKIPPED)
    rm_calls = [c for c in runner.calls if c["args"][0] == "rm"]
    testbase.assert_equal(rm_calls, [])
    return


# ----------------------------------------------------------------------
# enable_repository
# ----------------------------------------------------------------------


def test_enable_repository_renames_disabled_file():
    """
        enable_repository renames ``<name>.list.disabled`` to
        ``<name>.list``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "test", "-e", "/etc/apt/sources.list.d/docker.list",
    )] = _missing(path="/etc/apt/sources.list.d/docker.list")
    runner.results[(
        "test", "-e", "/etc/apt/sources.list.d/docker.list.disabled",
    )] = _exists(path="/etc/apt/sources.list.d/docker.list.disabled")

    manager = UbuntuRepositoryManager(
        runner=runner,
        config=UbuntuRepositoryManagerConfig(elevated=True),
    )
    result = manager.enable_repository(name="docker")

    testbase.assert_equal(result.status, UbuntuRepositoryActionStatus.SUCCESS)
    mv_calls = [c for c in runner.calls if c["args"][0] == "mv"]
    testbase.assert_equal(len(mv_calls), 1)
    testbase.assert_equal(
        mv_calls[0]["args"],
        [
            "mv",
            "/etc/apt/sources.list.d/docker.list.disabled",
            "/etc/apt/sources.list.d/docker.list",
        ],
    )
    testbase.assert_equal(mv_calls[0]["elevated"], True)
    return


def test_enable_repository_skipped_when_already_enabled():
    """
        enable_repository returns SKIPPED when the ``.list`` file already
        exists.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "test", "-e", "/etc/apt/sources.list.d/docker.list",
    )] = _exists(path="/etc/apt/sources.list.d/docker.list")

    manager = UbuntuRepositoryManager(
        runner=runner, config=UbuntuRepositoryManagerConfig(),
    )
    result = manager.enable_repository(name="docker")

    testbase.assert_equal(result.status, UbuntuRepositoryActionStatus.SKIPPED)
    mv_calls = [c for c in runner.calls if c["args"][0] == "mv"]
    testbase.assert_equal(mv_calls, [])
    return


# ----------------------------------------------------------------------
# disable_repository
# ----------------------------------------------------------------------


def test_disable_repository_renames_list_file():
    """
        disable_repository renames ``<name>.list`` to
        ``<name>.list.disabled``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "test", "-e", "/etc/apt/sources.list.d/docker.list.disabled",
    )] = _missing(path="/etc/apt/sources.list.d/docker.list.disabled")
    runner.results[(
        "test", "-e", "/etc/apt/sources.list.d/docker.list",
    )] = _exists(path="/etc/apt/sources.list.d/docker.list")

    manager = UbuntuRepositoryManager(
        runner=runner,
        config=UbuntuRepositoryManagerConfig(elevated=True),
    )
    result = manager.disable_repository(name="docker")

    testbase.assert_equal(result.status, UbuntuRepositoryActionStatus.SUCCESS)
    mv_calls = [c for c in runner.calls if c["args"][0] == "mv"]
    testbase.assert_equal(len(mv_calls), 1)
    testbase.assert_equal(
        mv_calls[0]["args"],
        [
            "mv",
            "/etc/apt/sources.list.d/docker.list",
            "/etc/apt/sources.list.d/docker.list.disabled",
        ],
    )
    testbase.assert_equal(mv_calls[0]["elevated"], True)
    return


def test_disable_repository_failed_when_not_found():
    """
        disable_repository returns FAILED with ``message="repository_not_found"``
        when neither file variant exists.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "test", "-e", "/etc/apt/sources.list.d/docker.list.disabled",
    )] = _missing(path="/etc/apt/sources.list.d/docker.list.disabled")
    runner.results[(
        "test", "-e", "/etc/apt/sources.list.d/docker.list",
    )] = _missing(path="/etc/apt/sources.list.d/docker.list")

    manager = UbuntuRepositoryManager(
        runner=runner, config=UbuntuRepositoryManagerConfig(),
    )
    result = manager.disable_repository(name="docker")

    testbase.assert_equal(result.status, UbuntuRepositoryActionStatus.FAILED)
    testbase.assert_equal(result.message, "repository_not_found")
    mv_calls = [c for c in runner.calls if c["args"][0] == "mv"]
    testbase.assert_equal(mv_calls, [])
    return


# ----------------------------------------------------------------------
# list_repositories
# ----------------------------------------------------------------------


def test_list_repositories_parses_sources_list_d():
    """
        list_repositories enumerates files under ``sources.list.d`` and
        parses each.
    """
    runner = _MockCommandRunner()
    runner.results[("ls", "-1", "/etc/apt/sources.list.d")] = RunResult(
        exit_code=0,
        stdout="docker.list\nbackports.list.disabled\n",
        stderr="",
    )
    runner.results[(
        "cat", "/etc/apt/sources.list.d/docker.list",
    )] = RunResult(
        exit_code=0,
        stdout=_expected_docker_body(),
        stderr="",
    )
    runner.results[(
        "cat", "/etc/apt/sources.list.d/backports.list.disabled",
    )] = RunResult(
        exit_code=0,
        stdout="deb http://example.com/backports jammy main\n",
        stderr="",
    )
    runner.results[(
        "test", "-e", "/etc/apt/sources.list",
    )] = _missing(path="/etc/apt/sources.list")

    manager = UbuntuRepositoryManager(
        runner=runner, config=UbuntuRepositoryManagerConfig(),
    )
    listed = manager.list_repositories()

    testbase.assert_equal(len(listed), 2)
    by_name = {info.name: info for info in listed}

    docker = by_name["docker"]
    testbase.assert_equal(docker.enabled, True)
    testbase.assert_equal(
        docker.uri, "https://download.docker.com/linux/ubuntu",
    )
    testbase.assert_equal(docker.suites, ["jammy"])
    testbase.assert_equal(docker.components, ["stable"])
    testbase.assert_equal(docker.types, ["deb"])
    testbase.assert_equal(
        docker.path, "/etc/apt/sources.list.d/docker.list",
    )

    backports = by_name["backports"]
    testbase.assert_equal(backports.enabled, False)
    testbase.assert_equal(backports.uri, "http://example.com/backports")
    return


# ----------------------------------------------------------------------
# refresh_repositories
# ----------------------------------------------------------------------


def test_refresh_repositories_invokes_apt_update():
    """
        refresh_repositories dispatches ``apt-get update``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = UbuntuRepositoryManager(
        runner=runner,
        config=UbuntuRepositoryManagerConfig(elevated=True),
    )
    result = manager.refresh_repositories()

    testbase.assert_equal(result.status, UbuntuRepositoryActionStatus.SUCCESS)
    testbase.assert_equal(runner.calls[-1]["args"], ["apt-get", "update"])
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Dry-run, elevation, error surfaces
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_add():
    """
        Dry-run short-circuits add_repository without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuRepositoryManager(
        runner=runner,
        config=UbuntuRepositoryManagerConfig(dry_run=True),
    )
    result = manager.add_repository(spec=_make_spec())

    testbase.assert_equal(result.status, UbuntuRepositoryActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command, ["tee", "/etc/apt/sources.list.d/docker.list"],
    )
    testbase.assert_equal(
        result.path, "/etc/apt/sources.list.d/docker.list",
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
    runner.results[(
        "cat", "/etc/apt/sources.list.d/docker.list",
    )] = RunResult(exit_code=1, stdout="", stderr="")
    runner.results[(
        "test", "-e", "/etc/apt/sources.list.d/curl.list",
    )] = _exists(path="/etc/apt/sources.list.d/curl.list")
    runner.results[(
        "test", "-e", "/etc/apt/sources.list.d/vim.list",
    )] = _missing(path="/etc/apt/sources.list.d/vim.list")
    runner.results[(
        "test", "-e", "/etc/apt/sources.list.d/vim.list.disabled",
    )] = _exists(path="/etc/apt/sources.list.d/vim.list.disabled")
    runner.results[(
        "test", "-e", "/etc/apt/sources.list.d/sed.list.disabled",
    )] = _missing(path="/etc/apt/sources.list.d/sed.list.disabled")
    runner.results[(
        "test", "-e", "/etc/apt/sources.list.d/sed.list",
    )] = _exists(path="/etc/apt/sources.list.d/sed.list")

    manager = UbuntuRepositoryManager(
        runner=runner,
        config=UbuntuRepositoryManagerConfig(elevated=True),
    )
    manager.add_repository(spec=_make_spec())
    manager.remove_repository(name="curl")
    manager.enable_repository(name="vim")
    manager.disable_repository(name="sed")
    manager.refresh_repositories()

    write_heads = (
        ["tee"],
        ["rm", "-f"],
        ["mv"],
        ["apt-get", "update"],
    )
    for call in runner.calls:
        head_args = call["args"]
        for head in write_heads:
            if head_args[: len(head)] == head:
                testbase.assert_equal(call["elevated"], True)
                break
    return


def test_failed_command_surfaces_stderr_in_result():
    """
        A non-zero exit on a write maps to FAILED with the captured
        stderr / exit_code / command.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "cat", "/etc/apt/sources.list.d/docker.list",
    )] = RunResult(exit_code=1, stdout="", stderr="")
    runner.results[(
        "tee", "/etc/apt/sources.list.d/docker.list",
    )] = RunResult(
        exit_code=1, stdout="", stderr="tee: permission denied",
    )

    manager = UbuntuRepositoryManager(
        runner=runner,
        config=UbuntuRepositoryManagerConfig(elevated=True),
    )
    result = manager.add_repository(spec=_make_spec())

    testbase.assert_equal(result.status, UbuntuRepositoryActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "tee: permission denied")
    testbase.assert_equal(
        result.command, ["tee", "/etc/apt/sources.list.d/docker.list"],
    )
    return
