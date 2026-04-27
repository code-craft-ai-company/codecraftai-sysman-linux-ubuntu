"""
    Tests for :class:`UbuntuEnvironmentConfigurationManager`.

    Tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    All file I/O (``cat`` / ``tee`` / ``rm``) and ``getent`` lookups are
    exercised entirely through the mock.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.environmentconfiguration import (
    UbuntuEnvActionStatus,
    UbuntuEnvironmentConfigurationManager,
    UbuntuEnvironmentManagerConfig,
    UbuntuEnvQueryError,
    UbuntuEnvScope,
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

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


def _ok(stdout: str = "", stderr: str = "") -> RunResult:
    """
        Build a successful :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr=stderr)
    return rtnval


def _fail(
    stdout: str = "", stderr: str = "", exit_code: int = 1,
) -> RunResult:
    """
        Build a failure :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


def _passwd_row(*, user: str, home: str) -> RunResult:
    """
        Build a ``getent passwd`` result for ``user`` whose home is
        ``home``.
    """
    line = "{}:x:1000:1000:{}:{}:/bin/bash\n".format(user, user, home)
    rtnval = RunResult(exit_code=0, stdout=line, stderr="")
    return rtnval


# ----------------------------------------------------------------------
# set_system_env
# ----------------------------------------------------------------------


def test_set_system_env_writes_both_files():
    """
        set_system_env writes /etc/environment and the profile.d drop-in
        in lockstep.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("cat", "/etc/environment")] = _fail()
    runner.results[("cat", "/etc/profile.d/automojo.sh")] = _fail()

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner,
        config=UbuntuEnvironmentManagerConfig(elevated=True),
    )
    result = manager.set_system_env(name="JAVA_HOME", value="/opt/java")

    testbase.assert_equal(result.status, UbuntuEnvActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 2)

    by_path = {c["args"][1]: c for c in tee_calls}
    testbase.assert_equal(
        by_path["/etc/environment"]["stdin"],
        'JAVA_HOME="/opt/java"\n',
    )
    testbase.assert_equal(
        by_path["/etc/profile.d/automojo.sh"]["stdin"],
        '#!/usr/bin/env bash\nexport JAVA_HOME="/opt/java"\n',
    )
    for call in tee_calls:
        testbase.assert_equal(call["elevated"], True)

    testbase.assert_equal(result.scope, UbuntuEnvScope.SYSTEM)
    testbase.assert_equal(result.name, "JAVA_HOME")
    testbase.assert_equal(result.value, "/opt/java")
    return


def test_set_system_env_skipped_when_both_files_current():
    """
        set_system_env returns SKIPPED when both files already contain
        the desired value.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/environment")] = _ok(
        stdout='JAVA_HOME="/opt/java"\n',
    )
    runner.results[("cat", "/etc/profile.d/automojo.sh")] = _ok(
        stdout='#!/usr/bin/env bash\nexport JAVA_HOME="/opt/java"\n',
    )

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner,
        config=UbuntuEnvironmentManagerConfig(elevated=True),
    )
    result = manager.set_system_env(name="JAVA_HOME", value="/opt/java")

    testbase.assert_equal(result.status, UbuntuEnvActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


def test_set_system_env_dry_run_makes_no_writes():
    """
        Dry-run short-circuits set_system_env without invoking writes.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/environment")] = _fail()
    runner.results[("cat", "/etc/profile.d/automojo.sh")] = _fail()

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner,
        config=UbuntuEnvironmentManagerConfig(dry_run=True, elevated=True),
    )
    result = manager.set_system_env(name="LANG", value="en_US.UTF-8")

    testbase.assert_equal(result.status, UbuntuEnvActionStatus.DRY_RUN)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


# ----------------------------------------------------------------------
# unset_system_env
# ----------------------------------------------------------------------


def test_unset_system_env_skipped_when_absent():
    """
        unset_system_env returns SKIPPED when neither file contains the
        variable.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/environment")] = _ok(stdout="OTHER=1\n")
    runner.results[("cat", "/etc/profile.d/automojo.sh")] = _fail()

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner,
        config=UbuntuEnvironmentManagerConfig(elevated=True),
    )
    result = manager.unset_system_env(name="JAVA_HOME")

    testbase.assert_equal(result.status, UbuntuEnvActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    rm_calls = [c for c in runner.calls if c["args"][0] == "rm"]
    testbase.assert_equal(tee_calls, [])
    testbase.assert_equal(rm_calls, [])
    return


def test_unset_system_env_rewrites_environment_file():
    """
        unset_system_env rewrites /etc/environment with the matching line
        removed.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("cat", "/etc/environment")] = _ok(
        stdout='JAVA_HOME="/opt/java"\nKEEP="value"\n',
    )
    runner.results[("cat", "/etc/profile.d/automojo.sh")] = _fail()

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner,
        config=UbuntuEnvironmentManagerConfig(elevated=True),
    )
    result = manager.unset_system_env(name="JAVA_HOME")

    testbase.assert_equal(result.status, UbuntuEnvActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["args"], ["tee", "/etc/environment"])
    testbase.assert_equal(tee_calls[0]["stdin"], 'KEEP="value"\n')
    return


def test_unset_system_env_deletes_dropin_when_only_shebang():
    """
        unset_system_env deletes the profile.d drop-in when the removal
        leaves only the shebang.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("cat", "/etc/environment")] = _fail()
    runner.results[("cat", "/etc/profile.d/automojo.sh")] = _ok(
        stdout='#!/usr/bin/env bash\nexport JAVA_HOME="/opt/java"\n',
    )

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner,
        config=UbuntuEnvironmentManagerConfig(elevated=True),
    )
    result = manager.unset_system_env(name="JAVA_HOME")

    testbase.assert_equal(result.status, UbuntuEnvActionStatus.SUCCESS)
    rm_calls = [c for c in runner.calls if c["args"][0] == "rm"]
    testbase.assert_equal(len(rm_calls), 1)
    testbase.assert_equal(
        rm_calls[0]["args"], ["rm", "-f", "/etc/profile.d/automojo.sh"],
    )
    testbase.assert_equal(rm_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# get_system_env / list_system_env
# ----------------------------------------------------------------------


def test_get_system_env_prefers_environment_file():
    """
        get_system_env reads from /etc/environment first; the profile.d
        drop-in is only consulted when /etc/environment lacks the
        variable.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/environment")] = _ok(
        stdout='LANG="en_US.UTF-8"\n',
    )
    runner.results[("cat", "/etc/profile.d/automojo.sh")] = _ok(
        stdout='#!/usr/bin/env bash\nexport LANG="C.UTF-8"\n',
    )

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner, config=UbuntuEnvironmentManagerConfig(),
    )
    info = manager.get_system_env(name="LANG")

    testbase.assert_equal(info.value, "en_US.UTF-8")
    testbase.assert_equal(info.source, "/etc/environment")
    testbase.assert_equal(info.scope, UbuntuEnvScope.SYSTEM)
    return


def test_list_system_env_merges_both_files():
    """
        list_system_env merges entries from both files, with
        /etc/environment winning on conflict.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/environment")] = _ok(
        stdout='LANG="en_US.UTF-8"\n',
    )
    runner.results[("cat", "/etc/profile.d/automojo.sh")] = _ok(
        stdout='#!/usr/bin/env bash\nexport JAVA_HOME="/opt/java"\nexport LANG="C.UTF-8"\n',
    )

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner, config=UbuntuEnvironmentManagerConfig(),
    )
    merged = manager.list_system_env()

    testbase.assert_equal(merged["LANG"], "en_US.UTF-8")
    testbase.assert_equal(merged["JAVA_HOME"], "/opt/java")
    testbase.assert_equal(len(merged), 2)
    return


def test_list_system_env_empty_when_both_missing():
    """
        list_system_env returns an empty dict when both files are
        absent.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/environment")] = _fail()
    runner.results[("cat", "/etc/profile.d/automojo.sh")] = _fail()

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner, config=UbuntuEnvironmentManagerConfig(),
    )
    merged = manager.list_system_env()

    testbase.assert_equal(merged, {})
    return


# ----------------------------------------------------------------------
# set_user_env / unset_user_env
# ----------------------------------------------------------------------


def test_set_user_env_creates_block_in_missing_profile():
    """
        set_user_env creates the user profile with a sentinel-bracketed
        block when the file is missing.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("getent", "passwd", "appuser")] = _passwd_row(
        user="appuser", home="/home/appuser",
    )
    runner.results[("cat", "/home/appuser/.profile")] = _fail()

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner,
        config=UbuntuEnvironmentManagerConfig(elevated=True),
    )
    result = manager.set_user_env(
        user="appuser", name="NODE_ENV", value="production",
    )

    testbase.assert_equal(result.status, UbuntuEnvActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"], ["tee", "/home/appuser/.profile"],
    )
    testbase.assert_equal(
        tee_calls[0]["stdin"],
        (
            "# --- automojo env start ---\n"
            'export NODE_ENV="production"\n'
            "# --- automojo env end ---\n"
        ),
    )
    testbase.assert_equal(result.user, "appuser")
    testbase.assert_equal(result.scope, UbuntuEnvScope.USER)
    return


def test_set_user_env_failed_for_unknown_user():
    """
        set_user_env returns FAILED with a descriptive message when the
        user does not exist; no file write occurs.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd", "ghost")] = _fail()

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner,
        config=UbuntuEnvironmentManagerConfig(elevated=True),
    )
    result = manager.set_user_env(user="ghost", name="X", value="1")

    testbase.assert_equal(result.status, UbuntuEnvActionStatus.FAILED)
    testbase.assert_equal(result.message, "user not found: ghost")
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


def test_set_user_env_skipped_when_block_already_matches():
    """
        set_user_env returns SKIPPED when the managed block already
        contains the desired line.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd", "appuser")] = _passwd_row(
        user="appuser", home="/home/appuser",
    )
    runner.results[("cat", "/home/appuser/.profile")] = _ok(
        stdout=(
            "# user content above\n"
            "# --- automojo env start ---\n"
            'export NODE_ENV="production"\n'
            "# --- automojo env end ---\n"
        ),
    )

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner,
        config=UbuntuEnvironmentManagerConfig(elevated=True),
    )
    result = manager.set_user_env(
        user="appuser", name="NODE_ENV", value="production",
    )

    testbase.assert_equal(result.status, UbuntuEnvActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


def test_unset_user_env_skipped_when_profile_missing():
    """
        unset_user_env returns SKIPPED when the user profile does not
        exist.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd", "appuser")] = _passwd_row(
        user="appuser", home="/home/appuser",
    )
    runner.results[("cat", "/home/appuser/.profile")] = _fail()

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner,
        config=UbuntuEnvironmentManagerConfig(elevated=True),
    )
    result = manager.unset_user_env(user="appuser", name="NODE_ENV")

    testbase.assert_equal(result.status, UbuntuEnvActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


def test_unset_user_env_rewrites_block():
    """
        unset_user_env rewrites the profile with the variable removed
        from the managed block.
    """
    runner = _MockCommandRunner(default_result=_ok())
    runner.results[("getent", "passwd", "appuser")] = _passwd_row(
        user="appuser", home="/home/appuser",
    )
    runner.results[("cat", "/home/appuser/.profile")] = _ok(
        stdout=(
            "user_top_line=keep\n"
            "# --- automojo env start ---\n"
            'export NODE_ENV="production"\n'
            'export OTHER="1"\n'
            "# --- automojo env end ---\n"
        ),
    )

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner,
        config=UbuntuEnvironmentManagerConfig(elevated=True),
    )
    result = manager.unset_user_env(user="appuser", name="NODE_ENV")

    testbase.assert_equal(result.status, UbuntuEnvActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["stdin"],
        (
            "user_top_line=keep\n"
            "# --- automojo env start ---\n"
            'export OTHER="1"\n'
            "# --- automojo env end ---\n"
        ),
    )
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Reads, elevation, error surfaces
# ----------------------------------------------------------------------


def test_reads_never_elevate():
    """
        get_system_env / list_system_env never pass elevated=True.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/environment")] = _ok(stdout="LANG=\"X\"\n")
    runner.results[("cat", "/etc/profile.d/automojo.sh")] = _fail()

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner,
        config=UbuntuEnvironmentManagerConfig(elevated=True),
    )
    manager.get_system_env(name="LANG")
    manager.list_system_env()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_resolve_home_raises_for_unknown_user():
    """
        get_user_env-style resolution path raises UbuntuEnvQueryError when
        getent returns an empty stdout body.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd", "ghost")] = _ok(stdout="")

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner, config=UbuntuEnvironmentManagerConfig(),
    )
    raised = False
    try:
        manager._resolve_home(user="ghost")
    except UbuntuEnvQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_failed_runner_surfaces_stderr_on_set_system_env():
    """
        A non-zero exit on the /etc/environment write maps to FAILED with
        the captured stderr / message.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/environment")] = _fail()
    runner.results[("cat", "/etc/profile.d/automojo.sh")] = _fail()
    runner.results[
        ("tee", "/etc/environment")
    ] = _fail(stderr="tee: permission denied")

    manager = UbuntuEnvironmentConfigurationManager(
        runner=runner,
        config=UbuntuEnvironmentManagerConfig(elevated=True),
    )
    result = manager.set_system_env(name="JAVA_HOME", value="/opt/java")

    testbase.assert_equal(result.status, UbuntuEnvActionStatus.FAILED)
    testbase.assert_equal(result.message, "environment_file_write_failed")
    testbase.assert_equal(result.stderr, "tee: permission denied")
    return
