"""
    Tests for :class:`UbuntuAclManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``getfacl`` / ``setfacl`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.aclmanagement import (
    UbuntuAceSpec,
    UbuntuAcePrincipalType,
    UbuntuAclActionStatus,
    UbuntuAclManager,
    UbuntuAclManagerConfig,
    UbuntuAclQueryError,
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


def _getfacl_full_payload() -> str:
    """
        Build a representative ``getfacl`` payload for a directory
        carrying a named user ACE, a named group ACE, a non-base mask,
        and two default ACEs.
    """
    rtnval = (
        "# file: /srv/data\n"
        "# owner: alice\n"
        "# group: staff\n"
        "user::rwx\n"
        "user:bob:r-x\n"
        "group::r-x\n"
        "group:devs:rwx\n"
        "mask::rwx\n"
        "other::r--\n"
        "default:user::rwx\n"
        "default:user:bob:r-x\n"
        "default:group::r-x\n"
        "default:mask::rwx\n"
        "default:other::---\n"
    )
    return rtnval


def _getfacl_minimal_payload() -> str:
    """
        Build a representative ``getfacl`` payload with only the three
        base ACEs and no extended entries.
    """
    rtnval = (
        "# file: /etc/example\n"
        "# owner: root\n"
        "# group: root\n"
        "user::rw-\n"
        "group::r--\n"
        "other::r--\n"
    )
    return rtnval


def _seed_full_acl(runner: _MockCommandRunner, path: str) -> None:
    """
        Seed ``getfacl <path>`` to return the full payload above.
    """
    runner.results[("getfacl", path)] = RunResult(
        exit_code=0,
        stdout=_getfacl_full_payload(),
        stderr="",
    )
    return


def _seed_minimal_acl(runner: _MockCommandRunner, path: str) -> None:
    """
        Seed ``getfacl <path>`` to return the minimal-only payload.
    """
    runner.results[("getfacl", path)] = RunResult(
        exit_code=0,
        stdout=_getfacl_minimal_payload(),
        stderr="",
    )
    return


# ----------------------------------------------------------------------
# get_acl / list_aces
# ----------------------------------------------------------------------


def test_get_acl_parses_getfacl_output():
    """
        get_acl returns an UbuntuAclInfo populated from the parsed
        getfacl output.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "/srv/data")

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(),
    )
    info = manager.get_acl(path="/srv/data")

    testbase.assert_equal(info.path, "/srv/data")
    testbase.assert_equal(info.owner, "alice")
    testbase.assert_equal(info.group, "staff")
    testbase.assert_equal(len(info.aces), 6)
    testbase.assert_equal(len(info.default_aces), 5)

    named_user = None
    for ace in info.aces:
        if ace.principal_type == UbuntuAcePrincipalType.USER and ace.principal == "bob":
            named_user = ace
            break
    testbase.assert_not_equal(named_user, None)
    testbase.assert_equal(named_user.rights, "r-x")
    return


def test_list_aces_returns_full_set():
    """
        list_aces returns regular-then-default ACEs as a flat list.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "/srv/data")

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(),
    )
    aces = manager.list_aces(path="/srv/data")

    testbase.assert_equal(len(aces), 11)
    # First six are regular; last five are default.
    for ace in aces[:6]:
        testbase.assert_equal(ace.default, False)
    for ace in aces[6:]:
        testbase.assert_equal(ace.default, True)
    return


# ----------------------------------------------------------------------
# set_acl
# ----------------------------------------------------------------------


def test_set_acl_invokes_setfacl():
    """
        set_acl dispatches setfacl -b followed by setfacl -m with the
        rendered comma-joined entry list.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_minimal_acl(runner, "/srv/data")

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(elevated=True),
    )
    spec = [
        UbuntuAceSpec(UbuntuAcePrincipalType.USER, None, "rwx"),
        UbuntuAceSpec(UbuntuAcePrincipalType.USER, "bob", "r-x"),
        UbuntuAceSpec(UbuntuAcePrincipalType.GROUP, None, "r-x"),
        UbuntuAceSpec(UbuntuAcePrincipalType.MASK, None, "rwx"),
        UbuntuAceSpec(UbuntuAcePrincipalType.OTHER, None, "r--"),
    ]
    result = manager.set_acl(path="/srv/data", acl_spec=spec)

    testbase.assert_equal(result.status, UbuntuAclActionStatus.SUCCESS)

    setfacl_calls = [c for c in runner.calls if c["args"][0] == "setfacl"]
    testbase.assert_equal(len(setfacl_calls), 2)
    testbase.assert_equal(setfacl_calls[0]["args"], ["setfacl", "-b", "/srv/data"])
    testbase.assert_equal(
        setfacl_calls[1]["args"],
        [
            "setfacl",
            "-m",
            "u::rwx,u:bob:r-x,g::r-x,m::rwx,o::r--",
            "/srv/data",
        ],
    )
    testbase.assert_equal(setfacl_calls[1]["elevated"], True)
    return


def test_set_acl_skipped_when_match():
    """
        set_acl returns SKIPPED when the current ACL matches the
        requested spec exactly.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "/srv/data")

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(elevated=True),
    )
    spec = [
        UbuntuAceSpec(UbuntuAcePrincipalType.USER, None, "rwx"),
        UbuntuAceSpec(UbuntuAcePrincipalType.USER, "bob", "r-x"),
        UbuntuAceSpec(UbuntuAcePrincipalType.GROUP, None, "r-x"),
        UbuntuAceSpec(UbuntuAcePrincipalType.GROUP, "devs", "rwx"),
        UbuntuAceSpec(UbuntuAcePrincipalType.MASK, None, "rwx"),
        UbuntuAceSpec(UbuntuAcePrincipalType.OTHER, None, "r--"),
        UbuntuAceSpec(UbuntuAcePrincipalType.USER, None, "rwx", default=True),
        UbuntuAceSpec(UbuntuAcePrincipalType.USER, "bob", "r-x", default=True),
        UbuntuAceSpec(UbuntuAcePrincipalType.GROUP, None, "r-x", default=True),
        UbuntuAceSpec(UbuntuAcePrincipalType.MASK, None, "rwx", default=True),
        UbuntuAceSpec(UbuntuAcePrincipalType.OTHER, None, "---", default=True),
    ]
    result = manager.set_acl(path="/srv/data", acl_spec=spec)

    testbase.assert_equal(result.status, UbuntuAclActionStatus.SKIPPED)
    setfacl_calls = [c for c in runner.calls if c["args"][0] == "setfacl"]
    testbase.assert_equal(setfacl_calls, [])
    return


def test_set_acl_dry_run_short_circuits():
    """
        set_acl returns DRY_RUN without dispatching any setfacl when
        config.dry_run is True.
    """
    runner = _MockCommandRunner()
    _seed_minimal_acl(runner, "/srv/data")

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(dry_run=True, elevated=True),
    )
    spec = [
        UbuntuAceSpec(UbuntuAcePrincipalType.USER, "bob", "r-x"),
    ]
    result = manager.set_acl(path="/srv/data", acl_spec=spec)

    testbase.assert_equal(result.status, UbuntuAclActionStatus.DRY_RUN)
    testbase.assert_equal(runner.calls, [])
    return


# ----------------------------------------------------------------------
# add_ace
# ----------------------------------------------------------------------


def test_add_ace_invokes_setfacl():
    """
        add_ace dispatches setfacl -m <token> <path>.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_minimal_acl(runner, "/srv/data")

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(elevated=True),
    )
    spec = UbuntuAceSpec(UbuntuAcePrincipalType.USER, "alice", "rwx")
    result = manager.add_ace(path="/srv/data", principal=spec)

    testbase.assert_equal(result.status, UbuntuAclActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "setfacl" and c["args"][1] == "-m"
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["setfacl", "-m", "u:alice:rwx", "/srv/data"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_add_ace_skipped_when_present():
    """
        add_ace returns SKIPPED when the requested ACE is already
        present byte-for-byte.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "/srv/data")

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(elevated=True),
    )
    spec = UbuntuAceSpec(UbuntuAcePrincipalType.USER, "bob", "r-x")
    result = manager.add_ace(path="/srv/data", principal=spec)

    testbase.assert_equal(result.status, UbuntuAclActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "setfacl"
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# remove_ace
# ----------------------------------------------------------------------


def test_remove_ace_invokes_setfacl():
    """
        remove_ace dispatches setfacl -x <token> <path>.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_full_acl(runner, "/srv/data")

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(elevated=True),
    )
    spec = UbuntuAceSpec(UbuntuAcePrincipalType.USER, "bob", "")
    result = manager.remove_ace(path="/srv/data", principal=spec)

    testbase.assert_equal(result.status, UbuntuAclActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "setfacl" and c["args"][1] == "-x"
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["setfacl", "-x", "u:bob", "/srv/data"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_remove_ace_skipped_when_absent():
    """
        remove_ace returns SKIPPED when no ACE matches the principal.
    """
    runner = _MockCommandRunner()
    _seed_minimal_acl(runner, "/srv/data")

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(elevated=True),
    )
    spec = UbuntuAceSpec(UbuntuAcePrincipalType.USER, "ghost", "")
    result = manager.remove_ace(path="/srv/data", principal=spec)

    testbase.assert_equal(result.status, UbuntuAclActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "setfacl"
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# clear_acl
# ----------------------------------------------------------------------


def test_clear_acl_invokes_setfacl():
    """
        clear_acl dispatches setfacl -b <path> when extended ACEs
        exist.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_full_acl(runner, "/srv/data")

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(elevated=True),
    )
    result = manager.clear_acl(path="/srv/data")

    testbase.assert_equal(result.status, UbuntuAclActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "setfacl"
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["setfacl", "-b", "/srv/data"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_clear_acl_skipped_when_already_clear():
    """
        clear_acl returns SKIPPED when no extended ACEs exist.
    """
    runner = _MockCommandRunner()
    _seed_minimal_acl(runner, "/srv/data")

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(elevated=True),
    )
    result = manager.clear_acl(path="/srv/data")

    testbase.assert_equal(result.status, UbuntuAclActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "setfacl"
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# copy_acl
# ----------------------------------------------------------------------


def test_copy_acl_invokes_setfacl():
    """
        copy_acl reads the source ACL via getfacl and pipes it into
        setfacl --set-file=- on the destination.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_full_acl(runner, "/srv/source")
    _seed_minimal_acl(runner, "/srv/dest")

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(elevated=True),
    )
    result = manager.copy_acl(src="/srv/source", dst="/srv/dest")

    testbase.assert_equal(result.status, UbuntuAclActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "setfacl" and c["args"][1] == "--set-file=-"
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["setfacl", "--set-file=-", "/srv/dest"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    testbase.assert_equal(write_calls[0]["stdin"], _getfacl_full_payload())
    return


def test_copy_acl_skipped_when_match():
    """
        copy_acl returns SKIPPED when the destination ACL already
        matches the source ACL byte-for-byte.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "/srv/source")
    _seed_full_acl(runner, "/srv/dest")

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(elevated=True),
    )
    result = manager.copy_acl(src="/srv/source", dst="/srv/dest")

    testbase.assert_equal(result.status, UbuntuAclActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "setfacl"
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# Elevation, error surface, contract corners
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Every write verb passes elevated=True to the runner when the
        manager is configured with elevated=True.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_minimal_acl(runner, "/srv/data")

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(elevated=True),
    )
    manager.add_ace(
        path="/srv/data",
        principal=UbuntuAceSpec(UbuntuAcePrincipalType.USER, "alice", "rwx"),
    )

    write_calls = [
        c for c in runner.calls
        if c["args"][0] == "setfacl" and c["args"][1] == "-m"
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Every read verb passes elevated=False to the runner regardless
        of config.elevated.
    """
    runner = _MockCommandRunner()
    _seed_full_acl(runner, "/srv/data")

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(elevated=True),
    )
    manager.get_acl(path="/srv/data")
    manager.list_aces(path="/srv/data")

    read_calls = [c for c in runner.calls if c["args"][0] == "getfacl"]
    testbase.assert_not_equal(len(read_calls), 0)
    for call in read_calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero exit on a write surfaces FAILED with the runner's
        captured stdout / stderr / exit_code on the result.
    """
    runner = _MockCommandRunner()
    _seed_minimal_acl(runner, "/srv/data")
    failure = RunResult(
        exit_code=1,
        stdout="",
        stderr="setfacl: /srv/data: Operation not permitted",
    )
    runner.results[
        ("setfacl", "-m", "u:alice:rwx", "/srv/data")
    ] = failure

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(elevated=False),
    )
    result = manager.add_ace(
        path="/srv/data",
        principal=UbuntuAceSpec(UbuntuAcePrincipalType.USER, "alice", "rwx"),
    )

    testbase.assert_equal(result.status, UbuntuAclActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(
        result.stderr,
        "setfacl: /srv/data: Operation not permitted",
    )
    return


def test_get_acl_raises_on_failure():
    """
        get_acl raises UbuntuAclQueryError when getfacl exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("getfacl", "/missing")] = RunResult(
        exit_code=1,
        stdout="",
        stderr="getfacl: /missing: No such file or directory",
    )

    manager = UbuntuAclManager(
        runner=runner,
        config=UbuntuAclManagerConfig(),
    )
    raised = False
    try:
        manager.get_acl(path="/missing")
    except UbuntuAclQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return
