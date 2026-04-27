"""
    Tests for :class:`UbuntuUserGroupManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    No live ``useradd`` / ``getent`` / ``passwd`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.usergroupmanagement import (
    UbuntuGroupSpec,
    UbuntuPrincipalKind,
    UbuntuUserChanges,
    UbuntuUserGroupActionStatus,
    UbuntuUserGroupManager,
    UbuntuUserGroupManagerConfig,
    UbuntuUserGroupQueryError,
    UbuntuUserSpec,
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


def _absent_passwd(*, name: str) -> RunResult:
    """
        Build a ``getent passwd <name>`` result that indicates the user
        does not exist.
    """
    rtnval = RunResult(exit_code=2, stdout="", stderr="")
    return rtnval


def _present_passwd(
    *,
    name: str,
    uid: int = 1001,
    gid: int = 1001,
    comment: str = "",
    home: Optional[str] = None,
    shell: str = "/bin/bash",
) -> RunResult:
    """
        Build a ``getent passwd <name>`` result that returns a single
        canonical entry for the user.
    """
    if home is None:
        home_value = "/home/{}".format(name)
    else:
        home_value = home
    line = "{}:x:{}:{}:{}:{}:{}\n".format(
        name, uid, gid, comment, home_value, shell,
    )
    rtnval = RunResult(exit_code=0, stdout=line, stderr="")
    return rtnval


def _absent_group(*, name: str) -> RunResult:
    """
        Build a ``getent group <name>`` result that indicates the group
        does not exist.
    """
    rtnval = RunResult(exit_code=2, stdout="", stderr="")
    return rtnval


def _present_group(
    *,
    name: str,
    gid: int = 1001,
    members: Optional[list[str]] = None,
) -> RunResult:
    """
        Build a ``getent group <name>`` result that returns a single
        canonical group line.
    """
    if members is None:
        members_field = ""
    else:
        members_field = ",".join(members)
    line = "{}:x:{}:{}\n".format(name, gid, members_field)
    rtnval = RunResult(exit_code=0, stdout=line, stderr="")
    return rtnval


def _passwd_status(*, locked: bool) -> RunResult:
    """
        Build a ``passwd -S <name>`` result with the requested lock
        flag in the second whitespace-delimited field.
    """
    if locked:
        flag = "L"
    else:
        flag = "P"
    line = "alice {} 2024-01-01 0 99999 7 -1\n".format(flag)
    rtnval = RunResult(exit_code=0, stdout=line, stderr="")
    return rtnval


# ----------------------------------------------------------------------
# create_user
# ----------------------------------------------------------------------


def test_create_user_constructs_useradd_argv():
    """
        create_user emits ``useradd -m -u <uid> -g <pg> -G <groups> -s
        <shell> -d <home> -c <comment> <name>`` with the expected order
        and values.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("getent", "passwd", "alice")] = _absent_passwd(name="alice")

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    spec = UbuntuUserSpec(
        name="alice",
        uid=1234,
        primary_group="staff",
        shell="/bin/bash",
        home="/home/alice",
        comment="Alice Doe",
        groups=["sudo", "docker"],
    )
    result = manager.create_user(spec=spec)

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SUCCESS)
    useradd_calls = [c for c in runner.calls if c["args"][0] == "useradd"]
    testbase.assert_equal(len(useradd_calls), 1)
    testbase.assert_equal(
        useradd_calls[0]["args"],
        [
            "useradd",
            "-m",
            "-u", "1234",
            "-g", "staff",
            "-G", "sudo,docker",
            "-s", "/bin/bash",
            "-d", "/home/alice",
            "-c", "Alice Doe",
            "alice",
        ],
    )
    testbase.assert_equal(useradd_calls[0]["elevated"], True)
    testbase.assert_equal(result.kind, UbuntuPrincipalKind.USER)
    testbase.assert_equal(result.name, "alice")
    return


def test_create_user_system_account_adds_dash_r():
    """
        create_user with ``system=True`` adds ``-r`` and omits ``-m``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("getent", "passwd", "svc")] = _absent_passwd(name="svc")

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    spec = UbuntuUserSpec(name="svc", system=True)
    manager.create_user(spec=spec)

    useradd_calls = [c for c in runner.calls if c["args"][0] == "useradd"]
    testbase.assert_equal(len(useradd_calls), 1)
    testbase.assert_equal(useradd_calls[0]["args"][:2], ["useradd", "-r"])
    testbase.assert_true("-m" not in useradd_calls[0]["args"])
    return


def test_create_user_skipped_when_present():
    """
        create_user returns SKIPPED when ``getent passwd`` reports the
        user already exists.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd", "alice")] = _present_passwd(name="alice")

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    result = manager.create_user(spec=UbuntuUserSpec(name="alice"))

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SKIPPED)
    useradd_calls = [c for c in runner.calls if c["args"][0] == "useradd"]
    testbase.assert_equal(useradd_calls, [])
    return


# ----------------------------------------------------------------------
# delete_user
# ----------------------------------------------------------------------


def test_delete_user_runs_userdel_dash_r():
    """
        delete_user dispatches ``userdel -r <name>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("getent", "passwd", "alice")] = _present_passwd(name="alice")

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    result = manager.delete_user(name="alice")

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SUCCESS)
    userdel_calls = [c for c in runner.calls if c["args"][0] == "userdel"]
    testbase.assert_equal(len(userdel_calls), 1)
    testbase.assert_equal(userdel_calls[0]["args"], ["userdel", "-r", "alice"])
    testbase.assert_equal(userdel_calls[0]["elevated"], True)
    return


def test_delete_user_skipped_when_absent():
    """
        delete_user returns SKIPPED when the user does not exist.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd", "ghost")] = _absent_passwd(name="ghost")

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    result = manager.delete_user(name="ghost")

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SKIPPED)
    userdel_calls = [c for c in runner.calls if c["args"][0] == "userdel"]
    testbase.assert_equal(userdel_calls, [])
    return


# ----------------------------------------------------------------------
# modify_user
# ----------------------------------------------------------------------


def test_modify_user_emits_only_nonnone_flags():
    """
        modify_user emits ``usermod`` with only the changed fields.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("getent", "passwd", "alice")] = _present_passwd(
        name="alice", shell="/bin/sh",
    )
    runner.results[("id", "-Gn", "alice")] = RunResult(
        exit_code=0, stdout="alice\n", stderr="",
    )
    runner.results[("passwd", "-S", "alice")] = _passwd_status(locked=False)

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    changes = UbuntuUserChanges(shell="/bin/zsh", comment="A. Doe")
    result = manager.modify_user(name="alice", changes=changes)

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SUCCESS)
    usermod_calls = [c for c in runner.calls if c["args"][0] == "usermod"]
    testbase.assert_equal(len(usermod_calls), 1)
    testbase.assert_equal(
        usermod_calls[0]["args"],
        ["usermod", "-s", "/bin/zsh", "-c", "A. Doe", "alice"],
    )
    testbase.assert_equal(usermod_calls[0]["elevated"], True)
    return


def test_modify_user_skipped_when_state_already_matches():
    """
        modify_user returns SKIPPED when current state already matches.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd", "alice")] = _present_passwd(
        name="alice", shell="/bin/zsh",
    )
    runner.results[("id", "-Gn", "alice")] = RunResult(
        exit_code=0, stdout="alice\n", stderr="",
    )
    runner.results[("passwd", "-S", "alice")] = _passwd_status(locked=False)

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    changes = UbuntuUserChanges(shell="/bin/zsh")
    result = manager.modify_user(name="alice", changes=changes)

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SKIPPED)
    usermod_calls = [c for c in runner.calls if c["args"][0] == "usermod"]
    testbase.assert_equal(usermod_calls, [])
    return


def test_modify_user_failed_when_user_missing():
    """
        modify_user returns FAILED when ``getent passwd`` reports the
        user does not exist.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd", "ghost")] = _absent_passwd(name="ghost")

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    changes = UbuntuUserChanges(shell="/bin/zsh")
    result = manager.modify_user(name="ghost", changes=changes)

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.FAILED)
    usermod_calls = [c for c in runner.calls if c["args"][0] == "usermod"]
    testbase.assert_equal(usermod_calls, [])
    return


# ----------------------------------------------------------------------
# get_user / list_users
# ----------------------------------------------------------------------


def test_get_user_parses_getent_id_and_passwd_status():
    """
        get_user combines ``getent passwd``, ``id -Gn``, and ``passwd -S``
        into a single :class:`UbuntuUserInfo`.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd", "alice")] = RunResult(
        exit_code=0,
        stdout="alice:x:1100:1100:Alice Doe:/home/alice:/bin/bash\n",
        stderr="",
    )
    runner.results[("id", "-Gn", "alice")] = RunResult(
        exit_code=0, stdout="alice sudo docker\n", stderr="",
    )
    runner.results[("passwd", "-S", "alice")] = _passwd_status(locked=True)

    manager = UbuntuUserGroupManager(
        runner=runner, config=UbuntuUserGroupManagerConfig(),
    )
    info = manager.get_user(name="alice")

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.name, "alice")
    testbase.assert_equal(info.uid, 1100)
    testbase.assert_equal(info.gid, 1100)
    testbase.assert_equal(info.home, "/home/alice")
    testbase.assert_equal(info.shell, "/bin/bash")
    testbase.assert_equal(info.comment, "Alice Doe")
    testbase.assert_equal(info.groups, ["alice", "sudo", "docker"])
    testbase.assert_equal(info.locked, True)
    return


def test_get_user_returns_none_when_absent():
    """
        get_user returns None when ``getent passwd`` exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd", "ghost")] = _absent_passwd(name="ghost")

    manager = UbuntuUserGroupManager(
        runner=runner, config=UbuntuUserGroupManagerConfig(),
    )
    info = manager.get_user(name="ghost")

    testbase.assert_true(info is None)
    return


def test_list_users_filters_by_uid_range():
    """
        list_users filters out entries whose UID is outside the
        configured ``regular_uid_min`` / ``regular_uid_max`` range.
    """
    output_lines = [
        "root:x:0:0:root:/root:/bin/bash",
        "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin",
        "alice:x:1000:1000:Alice:/home/alice:/bin/bash",
        "bob:x:1500:1500:Bob:/home/bob:/bin/bash",
        "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin",
    ]
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd")] = RunResult(
        exit_code=0,
        stdout="\n".join(output_lines) + "\n",
        stderr="",
    )

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(
            regular_uid_min=1000, regular_uid_max=60000,
        ),
    )
    listed = manager.list_users()

    names = [u.name for u in listed]
    testbase.assert_equal(names, ["alice", "bob"])
    return


def test_list_users_includes_system_when_configured():
    """
        list_users with ``include_system_accounts=True`` returns every
        parsed entry.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd")] = RunResult(
        exit_code=0,
        stdout=(
            "root:x:0:0:root:/root:/bin/bash\n"
            "alice:x:1000:1000:Alice:/home/alice:/bin/bash\n"
        ),
        stderr="",
    )

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(include_system_accounts=True),
    )
    listed = manager.list_users()

    names = [u.name for u in listed]
    testbase.assert_equal(names, ["root", "alice"])
    return


def test_list_users_raises_query_error_on_failure():
    """
        list_users raises :class:`UbuntuUserGroupQueryError` when
        ``getent passwd`` exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd")] = RunResult(
        exit_code=1, stdout="", stderr="getent: failure",
    )

    manager = UbuntuUserGroupManager(
        runner=runner, config=UbuntuUserGroupManagerConfig(),
    )

    raised = False
    try:
        manager.list_users()
    except UbuntuUserGroupQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# create_group / delete_group / list_groups
# ----------------------------------------------------------------------


def test_create_group_constructs_groupadd_argv():
    """
        create_group emits ``groupadd [-r] [-g <gid>] <name>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("getent", "group", "staff")] = _absent_group(name="staff")

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    result = manager.create_group(
        spec=UbuntuGroupSpec(name="staff", gid=2001, system=False),
    )

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SUCCESS)
    groupadd_calls = [c for c in runner.calls if c["args"][0] == "groupadd"]
    testbase.assert_equal(len(groupadd_calls), 1)
    testbase.assert_equal(
        groupadd_calls[0]["args"], ["groupadd", "-g", "2001", "staff"],
    )
    testbase.assert_equal(result.kind, UbuntuPrincipalKind.GROUP)
    testbase.assert_equal(groupadd_calls[0]["elevated"], True)
    return


def test_create_group_skipped_when_present():
    """
        create_group returns SKIPPED when the group already exists.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "group", "staff")] = _present_group(name="staff")

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    result = manager.create_group(spec=UbuntuGroupSpec(name="staff"))

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SKIPPED)
    groupadd_calls = [c for c in runner.calls if c["args"][0] == "groupadd"]
    testbase.assert_equal(groupadd_calls, [])
    return


def test_delete_group_skipped_when_absent():
    """
        delete_group returns SKIPPED when the group does not exist.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "group", "ghost")] = _absent_group(name="ghost")

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    result = manager.delete_group(name="ghost")

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SKIPPED)
    groupdel_calls = [c for c in runner.calls if c["args"][0] == "groupdel"]
    testbase.assert_equal(groupdel_calls, [])
    return


def test_list_groups_parses_multiple_lines():
    """
        list_groups parses every line including the members field.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "group")] = RunResult(
        exit_code=0,
        stdout=(
            "root:x:0:\n"
            "sudo:x:27:alice,bob\n"
            "docker:x:999:alice\n"
        ),
        stderr="",
    )

    manager = UbuntuUserGroupManager(
        runner=runner, config=UbuntuUserGroupManagerConfig(),
    )
    listed = manager.list_groups()

    by_name = {g.name: g for g in listed}
    testbase.assert_equal(by_name["root"].gid, 0)
    testbase.assert_equal(by_name["root"].members, [])
    testbase.assert_equal(by_name["sudo"].members, ["alice", "bob"])
    testbase.assert_equal(by_name["docker"].members, ["alice"])
    return


# ----------------------------------------------------------------------
# add_to_group / remove_from_group
# ----------------------------------------------------------------------


def test_add_to_group_dispatches_gpasswd_a():
    """
        add_to_group runs ``gpasswd -a <user> <group>`` when the user
        is not yet a member.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("getent", "group", "docker")] = _present_group(
        name="docker", members=["bob"],
    )

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    result = manager.add_to_group(user="alice", group="docker")

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SUCCESS)
    gpasswd_calls = [c for c in runner.calls if c["args"][0] == "gpasswd"]
    testbase.assert_equal(len(gpasswd_calls), 1)
    testbase.assert_equal(
        gpasswd_calls[0]["args"], ["gpasswd", "-a", "alice", "docker"],
    )
    testbase.assert_equal(gpasswd_calls[0]["elevated"], True)
    return


def test_add_to_group_skipped_when_already_member():
    """
        add_to_group returns SKIPPED when the user is already a member.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "group", "docker")] = _present_group(
        name="docker", members=["alice"],
    )

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    result = manager.add_to_group(user="alice", group="docker")

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SKIPPED)
    gpasswd_calls = [c for c in runner.calls if c["args"][0] == "gpasswd"]
    testbase.assert_equal(gpasswd_calls, [])
    return


def test_remove_from_group_skipped_when_not_member():
    """
        remove_from_group returns SKIPPED when the user is not a
        member.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "group", "docker")] = _present_group(
        name="docker", members=["bob"],
    )

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    result = manager.remove_from_group(user="alice", group="docker")

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SKIPPED)
    gpasswd_calls = [c for c in runner.calls if c["args"][0] == "gpasswd"]
    testbase.assert_equal(gpasswd_calls, [])
    return


# ----------------------------------------------------------------------
# set_password
# ----------------------------------------------------------------------


def test_set_password_pipes_stdin_and_does_not_capture_password():
    """
        set_password forwards ``"<user>:<password>\\n"`` via stdin and
        records only ``["chpasswd"]`` in the result's command field.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    result = manager.set_password(user="alice", password="hunter2")

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SUCCESS)
    chpasswd_calls = [c for c in runner.calls if c["args"][0] == "chpasswd"]
    testbase.assert_equal(len(chpasswd_calls), 1)
    testbase.assert_equal(chpasswd_calls[0]["args"], ["chpasswd"])
    testbase.assert_equal(chpasswd_calls[0]["stdin"], "alice:hunter2\n")
    testbase.assert_equal(chpasswd_calls[0]["elevated"], True)

    testbase.assert_equal(result.command, ["chpasswd"])
    testbase.assert_true("hunter2" not in " ".join(result.command))
    return


# ----------------------------------------------------------------------
# lock / unlock
# ----------------------------------------------------------------------


def test_lock_account_skipped_when_already_locked():
    """
        lock_account returns SKIPPED when ``passwd -S`` reports ``L``.
    """
    runner = _MockCommandRunner()
    runner.results[("passwd", "-S", "alice")] = _passwd_status(locked=True)

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    result = manager.lock_account(user="alice")

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SKIPPED)
    passwd_l_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["passwd", "-l"]
    ]
    testbase.assert_equal(passwd_l_calls, [])
    return


def test_unlock_account_skipped_when_not_locked():
    """
        unlock_account returns SKIPPED when ``passwd -S`` reports a
        non-``L`` flag.
    """
    runner = _MockCommandRunner()
    runner.results[("passwd", "-S", "alice")] = _passwd_status(locked=False)

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    result = manager.unlock_account(user="alice")

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SKIPPED)
    passwd_u_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["passwd", "-u"]
    ]
    testbase.assert_equal(passwd_u_calls, [])
    return


def test_lock_account_dispatches_passwd_l_when_unlocked():
    """
        lock_account dispatches ``passwd -l <user>`` when the account
        is currently unlocked.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("passwd", "-S", "alice")] = _passwd_status(locked=False)

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    result = manager.lock_account(user="alice")

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.SUCCESS)
    passwd_l_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["passwd", "-l"]
    ]
    testbase.assert_equal(len(passwd_l_calls), 1)
    testbase.assert_equal(passwd_l_calls[0]["args"], ["passwd", "-l", "alice"])
    testbase.assert_equal(passwd_l_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Dry-run / elevation / failure surfacing
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_create_user():
    """
        dry-run returns DRY_RUN without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(dry_run=True),
    )
    result = manager.create_user(spec=UbuntuUserSpec(name="alice"))

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(result.command[0], "useradd")
    return


def test_reads_never_pass_elevated_even_when_configured():
    """
        Reads must never elevate, even when the manager is configured
        with ``elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd")] = RunResult(
        exit_code=0,
        stdout="alice:x:1000:1000::/home/alice:/bin/bash\n",
        stderr="",
    )
    runner.results[("getent", "group")] = RunResult(
        exit_code=0,
        stdout="alice:x:1000:\n",
        stderr="",
    )
    runner.results[("getent", "passwd", "alice")] = _present_passwd(name="alice")
    runner.results[("id", "-Gn", "alice")] = RunResult(
        exit_code=0, stdout="alice\n", stderr="",
    )
    runner.results[("passwd", "-S", "alice")] = _passwd_status(locked=False)

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    manager.list_users()
    manager.list_groups()
    manager.get_user(name="alice")

    read_heads = (
        ("getent", "passwd"),
        ("getent", "group"),
        ("id", "-Gn"),
        ("passwd", "-S"),
    )
    for call in runner.calls:
        argv = tuple(call["args"])
        for head in read_heads:
            if argv[: len(head)] == head:
                testbase.assert_equal(call["elevated"], False)
                break
    return


def test_failed_command_surfaces_stderr_and_exit_code():
    """
        A non-zero ``useradd`` exit maps to FAILED with the captured
        stderr / exit_code / command.
    """
    runner = _MockCommandRunner()
    runner.results[("getent", "passwd", "alice")] = _absent_passwd(name="alice")
    runner.default_result = RunResult(
        exit_code=4, stdout="", stderr="useradd: UID 0 conflict",
    )

    manager = UbuntuUserGroupManager(
        runner=runner,
        config=UbuntuUserGroupManagerConfig(elevated=True),
    )
    result = manager.create_user(spec=UbuntuUserSpec(name="alice"))

    testbase.assert_equal(result.status, UbuntuUserGroupActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 4)
    testbase.assert_equal(result.stderr, "useradd: UID 0 conflict")
    testbase.assert_equal(
        result.command, ["useradd", "-m", "-s", "/bin/bash", "alice"],
    )
    return
