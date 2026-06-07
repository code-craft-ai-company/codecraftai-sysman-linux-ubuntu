"""
    Tests for :class:`UbuntuIdentityManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``realm`` / ``sssctl`` / ``getent`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.identitymanagement import (
    UbuntuIdentityActionStatus,
    UbuntuIdentityManager,
    UbuntuIdentityManagerConfig,
    UbuntuIdentityQueryError,
    UbuntuRealmJoinSpec,
    UbuntuRealmKind,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


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


# ----------------------------------------------------------------------
# Canonical realm-list output samples
# ----------------------------------------------------------------------


_REALM_LIST_FREEIPA = (
    "example.com\n"
    "  type: kerberos\n"
    "  realm-name: EXAMPLE.COM\n"
    "  domain-name: example.com\n"
    "  configured: kerberos-member\n"
    "  server-software: ipa\n"
    "  client-software: sssd\n"
    "  required-package: sssd-tools\n"
    "  login-formats: %U\n"
    "  login-policy: allow-realm-logins\n"
    "  computer-name: testhost\n"
)


_REALM_LIST_AD = (
    "ad.example.com\n"
    "  type: kerberos\n"
    "  realm-name: AD.EXAMPLE.COM\n"
    "  domain-name: ad.example.com\n"
    "  configured: kerberos-member\n"
    "  server-software: active-directory\n"
    "  client-software: sssd\n"
    "  required-package: sssd-tools\n"
    "  login-formats: %U@ad.example.com\n"
    "  login-policy: allow-realm-logins\n"
    "  computer-name: ubuntu-host\n"
)


_REALM_LIST_MULTI = _REALM_LIST_FREEIPA + "\n" + _REALM_LIST_AD


def _realm_list_present(*, output: str) -> RunResult:
    rtnval = RunResult(exit_code=0, stdout=output, stderr="")
    return rtnval


def _realm_list_empty() -> RunResult:
    rtnval = RunResult(exit_code=0, stdout="", stderr="")
    return rtnval


# ----------------------------------------------------------------------
# join_realm
# ----------------------------------------------------------------------


def test_join_realm_invokes_realm_join():
    """
        join_realm dispatches ``realm join --user=<admin> <realm>`` and
        the password is delivered through stdin.
    """
    runner = _MockCommandRunner()
    runner.results[("realm", "list")] = _realm_list_empty()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(elevated=True),
    )
    spec = UbuntuRealmJoinSpec(
        realm="example.com",
        admin_user="admin",
        admin_password="s3cret",
    )
    result = manager.join_realm(spec=spec)

    testbase.assert_equal(result.status, UbuntuIdentityActionStatus.SUCCESS)
    join_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2 and c["args"][:2] == ["realm", "join"]
    ]
    testbase.assert_equal(len(join_calls), 1)
    testbase.assert_equal(
        join_calls[0]["args"],
        ["realm", "join", "--user=admin", "example.com"],
    )
    testbase.assert_equal(join_calls[0]["elevated"], True)
    testbase.assert_equal(join_calls[0]["stdin"], "s3cret")
    testbase.assert_equal(result.realm, "example.com")
    testbase.assert_equal(result.username, "admin")
    return


def test_join_realm_emits_optional_placement_flags():
    """
        join_realm emits ``--computer-name`` / ``--computer-ou`` /
        ``--server`` for placement overrides.
    """
    runner = _MockCommandRunner()
    runner.results[("realm", "list")] = _realm_list_empty()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(elevated=True),
    )
    spec = UbuntuRealmJoinSpec(
        realm="ad.example.com",
        admin_user="admin",
        admin_password="pw",
        computer_name="ubuntu-host",
        ou_path="OU=Servers,DC=ad,DC=example,DC=com",
        domain_controller="dc1.ad.example.com",
    )
    manager.join_realm(spec=spec)

    join_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2 and c["args"][:2] == ["realm", "join"]
    ]
    testbase.assert_equal(len(join_calls), 1)
    testbase.assert_equal(
        join_calls[0]["args"],
        [
            "realm", "join",
            "--user=admin",
            "--computer-name=ubuntu-host",
            "--computer-ou=OU=Servers,DC=ad,DC=example,DC=com",
            "--server=dc1.ad.example.com",
            "ad.example.com",
        ],
    )
    return


def test_join_realm_skipped_when_joined():
    """
        join_realm returns SKIPPED when ``realm list`` already shows
        the host joined to the requested realm.
    """
    runner = _MockCommandRunner()
    runner.results[("realm", "list")] = _realm_list_present(
        output=_REALM_LIST_FREEIPA,
    )

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(elevated=True),
    )
    spec = UbuntuRealmJoinSpec(
        realm="example.com",
        admin_user="admin",
        admin_password="s3cret",
    )
    result = manager.join_realm(spec=spec)

    testbase.assert_equal(result.status, UbuntuIdentityActionStatus.SKIPPED)
    join_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2 and c["args"][:2] == ["realm", "join"]
    ]
    testbase.assert_equal(join_calls, [])
    return


def test_join_realm_dry_run():
    """
        join_realm returns DRY_RUN under ``dry_run=True`` and never
        invokes the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(dry_run=True, elevated=True),
    )
    spec = UbuntuRealmJoinSpec(
        realm="example.com",
        admin_user="admin",
        admin_password="s3cret",
    )
    result = manager.join_realm(spec=spec)

    testbase.assert_equal(result.status, UbuntuIdentityActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    testbase.assert_equal(
        result.command,
        ["realm", "join", "--user=admin", "example.com"],
    )
    return


# ----------------------------------------------------------------------
# leave_realm
# ----------------------------------------------------------------------


def test_leave_realm_invokes_realm_leave():
    """
        leave_realm dispatches ``realm leave <realm>`` when the host
        is currently joined.
    """
    runner = _MockCommandRunner()
    runner.results[("realm", "list")] = _realm_list_present(
        output=_REALM_LIST_FREEIPA,
    )
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(elevated=True),
    )
    result = manager.leave_realm(realm="example.com")

    testbase.assert_equal(result.status, UbuntuIdentityActionStatus.SUCCESS)
    leave_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2 and c["args"][:2] == ["realm", "leave"]
    ]
    testbase.assert_equal(len(leave_calls), 1)
    testbase.assert_equal(
        leave_calls[0]["args"],
        ["realm", "leave", "example.com"],
    )
    testbase.assert_equal(leave_calls[0]["elevated"], True)
    return


def test_leave_realm_skipped_when_not_joined():
    """
        leave_realm returns SKIPPED when the host is not joined to the
        requested realm.
    """
    runner = _MockCommandRunner()
    runner.results[("realm", "list")] = _realm_list_empty()

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(elevated=True),
    )
    result = manager.leave_realm(realm="example.com")

    testbase.assert_equal(result.status, UbuntuIdentityActionStatus.SKIPPED)
    leave_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2 and c["args"][:2] == ["realm", "leave"]
    ]
    testbase.assert_equal(leave_calls, [])
    return


# ----------------------------------------------------------------------
# get_realm_status / list_joined_realms
# ----------------------------------------------------------------------


def test_get_realm_status_parses_realm_list_output():
    """
        get_realm_status parses ``realm list`` output and reports the
        joined realm + kind + computer-name.
    """
    runner = _MockCommandRunner()
    runner.results[("realm", "list")] = _realm_list_present(
        output=_REALM_LIST_FREEIPA,
    )

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(),
    )
    status = manager.get_realm_status()

    testbase.assert_equal(status.joined, True)
    testbase.assert_equal(status.current_realm, "example.com")
    testbase.assert_equal(status.realm_kind, UbuntuRealmKind.FREEIPA)
    testbase.assert_equal(status.computer_name, "testhost")
    return


def test_get_realm_status_returns_unjoined_when_empty():
    """
        get_realm_status reports unjoined when ``realm list`` is empty.
    """
    runner = _MockCommandRunner()
    runner.results[("realm", "list")] = _realm_list_empty()

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(),
    )
    status = manager.get_realm_status()

    testbase.assert_equal(status.joined, False)
    testbase.assert_equal(status.current_realm, None)
    testbase.assert_equal(status.realm_kind, None)
    return


def test_list_joined_realms_returns_list():
    """
        list_joined_realms parses multiple stanzas in ``realm list``
        output.
    """
    runner = _MockCommandRunner()
    runner.results[("realm", "list")] = _realm_list_present(
        output=_REALM_LIST_MULTI,
    )

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(),
    )
    realms = manager.list_joined_realms()

    testbase.assert_equal(len(realms), 2)
    testbase.assert_equal(realms[0].realm, "example.com")
    testbase.assert_equal(realms[0].realm_kind, UbuntuRealmKind.FREEIPA)
    testbase.assert_equal(realms[1].realm, "ad.example.com")
    testbase.assert_equal(realms[1].realm_kind, UbuntuRealmKind.ACTIVE_DIRECTORY)
    return


# ----------------------------------------------------------------------
# sync_users
# ----------------------------------------------------------------------


def test_sync_users_invokes_sssctl():
    """
        sync_users dispatches ``sssctl cache-expire -E`` and elevates
        when configured.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(elevated=True),
    )
    result = manager.sync_users()

    testbase.assert_equal(result.status, UbuntuIdentityActionStatus.SUCCESS)
    sssctl_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 1 and c["args"][0] == "sssctl"
    ]
    testbase.assert_equal(len(sssctl_calls), 1)
    testbase.assert_equal(
        sssctl_calls[0]["args"],
        ["sssctl", "cache-expire", "-E"],
    )
    testbase.assert_equal(sssctl_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# resolve_user / resolve_group
# ----------------------------------------------------------------------


def test_resolve_user_parses_getent_output():
    """
        resolve_user parses a ``getent passwd`` line into a populated
        :class:`UbuntuUserResolveResult`.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd", "alice")] = RunResult(
        exit_code=0,
        stdout="alice@example.com:*:1234:5678::/home/alice:/bin/bash\n",
        stderr="",
    )

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(),
    )
    result = manager.resolve_user(username="alice")

    testbase.assert_equal(result.found, True)
    testbase.assert_equal(result.uid, 1234)
    testbase.assert_equal(result.gid, 5678)
    testbase.assert_equal(result.home, "/home/alice")
    testbase.assert_equal(result.shell, "/bin/bash")
    testbase.assert_equal(result.realm, "example.com")
    return


def test_resolve_user_local_no_realm():
    """
        resolve_user returns ``realm=None`` for local accounts that
        have no ``@`` suffix.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd", "bob")] = RunResult(
        exit_code=0,
        stdout="bob:x:1001:1001::/home/bob:/bin/bash\n",
        stderr="",
    )

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(),
    )
    result = manager.resolve_user(username="bob")

    testbase.assert_equal(result.found, True)
    testbase.assert_equal(result.realm, None)
    return


def test_resolve_user_returns_not_found_when_unknown():
    """
        resolve_user returns ``found=False`` when ``getent passwd``
        exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd", "ghost")] = RunResult(
        exit_code=2, stdout="", stderr="",
    )

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(),
    )
    result = manager.resolve_user(username="ghost")

    testbase.assert_equal(result.found, False)
    testbase.assert_equal(result.uid, None)
    testbase.assert_equal(result.realm, None)
    return


def test_resolve_group_parses_getent_output():
    """
        resolve_group parses a ``getent group`` line into a populated
        :class:`UbuntuGroupResolveResult`.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "group", "staff")] = RunResult(
        exit_code=0,
        stdout="staff:x:5678:alice,bob,carol\n",
        stderr="",
    )

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(),
    )
    result = manager.resolve_group(groupname="staff")

    testbase.assert_equal(result.found, True)
    testbase.assert_equal(result.gid, 5678)
    testbase.assert_equal(result.members, ["alice", "bob", "carol"])
    return


# ----------------------------------------------------------------------
# Cross-cutting invariants
# ----------------------------------------------------------------------


def test_password_redacted_in_recorded_command():
    """
        The recorded ``command`` field after ``join_realm`` MUST NOT
        contain the plaintext password; the password rides on stdin.
    """
    runner = _MockCommandRunner()
    runner.results[("realm", "list")] = _realm_list_empty()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(elevated=True),
    )
    spec = UbuntuRealmJoinSpec(
        realm="example.com",
        admin_user="admin",
        admin_password="topsecret",
    )
    result = manager.join_realm(spec=spec)

    testbase.assert_true(result.command is not None)
    for token in result.command:
        testbase.assert_true("topsecret" not in token)
    # And confirm the runner saw the password on stdin.
    join_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 2 and c["args"][:2] == ["realm", "join"]
    ]
    testbase.assert_equal(len(join_calls), 1)
    testbase.assert_equal(join_calls[0]["stdin"], "topsecret")
    return


def test_writes_elevate():
    """
        Write verbs honour ``config.elevated``.
    """
    runner = _MockCommandRunner()
    runner.results[("realm", "list")] = _realm_list_present(
        output=_REALM_LIST_FREEIPA,
    )
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(elevated=True),
    )
    manager.leave_realm(realm="example.com")
    manager.sync_users()

    elevated_writes = [
        c for c in runner.calls
        if c["elevated"] is True
        and len(c["args"]) >= 1
        and c["args"][0] in ("realm", "sssctl")
        and not (
            len(c["args"]) >= 2 and c["args"][:2] == ["realm", "list"]
        )
    ]
    testbase.assert_true(len(elevated_writes) >= 2)
    return


def test_reads_never_elevate():
    """
        Read verbs never elevate, even when ``elevated=True`` in the
        config.
    """
    runner = _MockCommandRunner()
    runner.results[("realm", "list")] = _realm_list_present(
        output=_REALM_LIST_FREEIPA,
    )
    runner.results[("getent", "passwd", "alice")] = RunResult(
        exit_code=0,
        stdout="alice:x:1234:1234::/home/alice:/bin/bash\n",
        stderr="",
    )
    runner.results[("getent", "group", "staff")] = RunResult(
        exit_code=0,
        stdout="staff:x:5678:alice\n",
        stderr="",
    )

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(elevated=True),
    )
    manager.list_joined_realms()
    manager.resolve_user(username="alice")
    manager.resolve_group(groupname="staff")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero ``realm join`` exit produces ``status=FAILED`` with
        the captured ``stderr`` and ``exit_code`` on the result.
    """
    runner = _MockCommandRunner()
    runner.results[("realm", "list")] = _realm_list_empty()
    runner.results[
        ("realm", "join", "--user=admin", "example.com")
    ] = RunResult(
        exit_code=1,
        stdout="",
        stderr="realm: Couldn't authenticate as: admin\n",
    )

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(elevated=True),
    )
    spec = UbuntuRealmJoinSpec(
        realm="example.com",
        admin_user="admin",
        admin_password="bad",
    )
    result = manager.join_realm(spec=spec)

    testbase.assert_equal(result.status, UbuntuIdentityActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_true(
        result.stderr is not None and "Couldn't authenticate" in result.stderr,
    )
    return


def test_list_joined_realms_raises_on_query_failure():
    """
        list_joined_realms raises :class:`UbuntuIdentityQueryError`
        when ``realm list`` exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("realm", "list")] = RunResult(
        exit_code=1,
        stdout="",
        stderr="realm: Tool 'realm' not available\n",
    )

    manager = UbuntuIdentityManager(
        runner=runner,
        config=UbuntuIdentityManagerConfig(),
    )

    raised = False
    try:
        manager.list_joined_realms()
    except UbuntuIdentityQueryError:
        raised = True

    testbase.assert_true(raised)
    return
