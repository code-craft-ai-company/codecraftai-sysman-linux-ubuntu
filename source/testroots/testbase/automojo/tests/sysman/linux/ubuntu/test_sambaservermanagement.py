"""
    Tests for :class:`UbuntuSambaServerManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult` values
    keyed by argv tuple.  No live ``smbcontrol`` / ``smbpasswd`` /
    ``pdbedit`` / ``cat`` / ``tee`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.sambaservermanagement import (
    UbuntuSambaActionStatus,
    UbuntuSambaManagerConfig,
    UbuntuSambaQueryError,
    UbuntuSambaServerManager,
    UbuntuSambaShareAcl,
    UbuntuSambaShareInfo,
    UbuntuSambaShareSpec,
    UbuntuSambaUserInfo,
    _parse_pdbedit_output,
    _parse_smb_conf,
    _render_smb_conf,
)


# ----------------------------------------------------------------------
# Mock runner + helpers
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every
        ``run`` call and returns pre-seeded :class:`RunResult`
        values keyed by argv tuple.
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


def _ok(stdout: str = "", stderr: str = "") -> RunResult:
    """
        Build a successful :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr=stderr)
    return rtnval


def _fail(
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 1,
) -> RunResult:
    """
        Build a failure :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


_SMB_CONF_PATH = "/etc/samba/smb.conf"


def _seed_cat(
    runner: _MockCommandRunner,
    *,
    path: str,
    body: Optional[str],
) -> None:
    """
        Seed the runner so that ``cat <path>`` returns ``body`` on
        success, or non-zero with empty stderr (missing file) when
        ``body`` is ``None``.
    """
    if body is None:
        result = RunResult(exit_code=1, stdout="", stderr="")
    else:
        result = RunResult(exit_code=0, stdout=body, stderr="")
    runner.results[("cat", path)] = result
    return


def _seed_pdbedit_short(
    runner: _MockCommandRunner,
    *,
    stdout: str = "",
) -> None:
    """
        Seed the ``pdbedit -L`` idempotency probe used by
        ``add_samba_user`` / ``remove_samba_user``.
    """
    runner.results[("pdbedit", "-L")] = _ok(stdout=stdout)
    return


def _seed_pdbedit_verbose(
    runner: _MockCommandRunner,
    *,
    stdout: str,
) -> None:
    """
        Seed the ``pdbedit -L -v`` listing used by
        ``list_samba_users``.
    """
    runner.results[("pdbedit", "-L", "-v")] = _ok(stdout=stdout)
    return


def _make_spec(
    *,
    name: str = "public",
    path: str = "/srv/samba/public",
    description: Optional[str] = "Public share",
    read_only: bool = False,
    valid_users: Optional[list] = None,
    read_list: Optional[list] = None,
    write_list: Optional[list] = None,
    browseable: bool = True,
    guest_ok: bool = False,
) -> UbuntuSambaShareSpec:
    """
        Build a :class:`UbuntuSambaShareSpec`.
    """
    spec = UbuntuSambaShareSpec(
        name=name,
        path=path,
        description=description,
        read_only=read_only,
        valid_users=valid_users,
        read_list=read_list,
        write_list=write_list,
        browseable=browseable,
        guest_ok=guest_ok,
    )
    return spec


# ----------------------------------------------------------------------
# add_share
# ----------------------------------------------------------------------


def test_add_share_invokes_smbcontrol():
    """
        add_share writes the rendered section block to
        ``/etc/samba/smb.conf`` via ``tee`` and dispatches
        ``smbcontrol all reload-config`` afterwards.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_cat(runner, path=_SMB_CONF_PATH, body="")

    config = UbuntuSambaManagerConfig(elevated=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)
    spec = _make_spec(name="public")

    result = manager.add_share(spec=spec)

    testbase.assert_equal(result.status, UbuntuSambaActionStatus.SUCCESS)
    testbase.assert_equal(result.share_name, "public")

    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 2
        and call["args"][0] == "tee"
        and call["args"][1] == _SMB_CONF_PATH
    ]
    testbase.assert_equal(len(tee_calls), 1)
    written = tee_calls[0]["stdin"]
    testbase.assert_equal("[public]" in written, True)
    testbase.assert_equal("path = /srv/samba/public" in written, True)

    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["smbcontrol", "all", "reload-config"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    testbase.assert_equal(reload_calls[0]["elevated"], True)
    return


def test_add_share_skipped_when_present():
    """
        add_share returns SKIPPED when the file already contains a
        section with the same directives.
    """
    runner = _MockCommandRunner()
    existing = (
        "[public]\n"
        "    path = /srv/samba/public\n"
        "    comment = Public share\n"
        "    read only = no\n"
        "    browseable = yes\n"
        "    guest ok = no\n"
    )
    _seed_cat(runner, path=_SMB_CONF_PATH, body=existing)

    config = UbuntuSambaManagerConfig(elevated=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)
    spec = _make_spec(name="public")

    result = manager.add_share(spec=spec)

    testbase.assert_equal(result.status, UbuntuSambaActionStatus.SKIPPED)

    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["smbcontrol", "all", "reload-config"]
    ]
    testbase.assert_equal(len(reload_calls), 0)
    return


def test_add_share_dry_run():
    """
        add_share under ``dry_run=True`` returns DRY_RUN and never
        invokes ``tee`` or ``smbcontrol``.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path=_SMB_CONF_PATH, body="")

    config = UbuntuSambaManagerConfig(elevated=True, dry_run=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)
    spec = _make_spec(name="public")

    result = manager.add_share(spec=spec)

    testbase.assert_equal(result.status, UbuntuSambaActionStatus.DRY_RUN)
    testbase.assert_equal(result.command[0], "tee")

    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["smbcontrol", "all", "reload-config"]
    ]
    testbase.assert_equal(len(reload_calls), 0)
    return


# ----------------------------------------------------------------------
# remove_share
# ----------------------------------------------------------------------


def test_remove_share_invokes_smbcontrol():
    """
        remove_share strips the matching section block and chains
        ``smbcontrol all reload-config``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    existing = (
        "[global]\n"
        "    workgroup = WORKGROUP\n"
        "[public]\n"
        "    path = /srv/samba/public\n"
        "    read only = no\n"
        "[private]\n"
        "    path = /srv/samba/private\n"
        "    read only = yes\n"
    )
    _seed_cat(runner, path=_SMB_CONF_PATH, body=existing)

    config = UbuntuSambaManagerConfig(elevated=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)

    result = manager.remove_share(name="public")

    testbase.assert_equal(result.status, UbuntuSambaActionStatus.SUCCESS)

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    written = tee_calls[0]["stdin"]
    testbase.assert_equal("[public]" in written, False)
    testbase.assert_equal("[private]" in written, True)
    testbase.assert_equal("[global]" in written, True)

    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["smbcontrol", "all", "reload-config"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    return


def test_remove_share_skipped_when_absent():
    """
        remove_share returns SKIPPED when the section is not
        present.
    """
    runner = _MockCommandRunner()
    _seed_cat(
        runner,
        path=_SMB_CONF_PATH,
        body="[global]\n    workgroup = WORKGROUP\n",
    )

    config = UbuntuSambaManagerConfig(elevated=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)

    result = manager.remove_share(name="public")

    testbase.assert_equal(result.status, UbuntuSambaActionStatus.SKIPPED)

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


# ----------------------------------------------------------------------
# list_shares
# ----------------------------------------------------------------------


def test_list_shares_parses_output():
    """
        list_shares parses every non-reserved section into a
        :class:`UbuntuSambaShareInfo`.
    """
    runner = _MockCommandRunner()
    body = (
        "[global]\n"
        "    workgroup = WORKGROUP\n"
        "[homes]\n"
        "    read only = no\n"
        "[public]\n"
        "    path = /srv/samba/public\n"
        "    comment = Public share\n"
        "    read only = no\n"
        "    valid users = alice bob\n"
        "    browseable = yes\n"
        "    guest ok = no\n"
        "[private]\n"
        "    path = /srv/samba/private\n"
        "    read only = yes\n"
    )
    _seed_cat(runner, path=_SMB_CONF_PATH, body=body)

    config = UbuntuSambaManagerConfig()
    manager = UbuntuSambaServerManager(runner=runner, config=config)

    rtnval = manager.list_shares()

    testbase.assert_equal(len(rtnval), 2)
    by_name = {entry.name: entry for entry in rtnval}
    testbase.assert_equal(by_name["public"].path, "/srv/samba/public")
    testbase.assert_equal(by_name["public"].read_only, False)
    testbase.assert_equal(
        by_name["public"].valid_users, ["alice", "bob"],
    )
    testbase.assert_equal(by_name["private"].read_only, True)
    return


# ----------------------------------------------------------------------
# get_share
# ----------------------------------------------------------------------


def test_get_share_returns_correct_record():
    """
        get_share returns the matching record and ``None`` for a
        missing share.
    """
    runner = _MockCommandRunner()
    body = (
        "[public]\n"
        "    path = /srv/samba/public\n"
        "    comment = Public share\n"
        "    read only = no\n"
    )
    _seed_cat(runner, path=_SMB_CONF_PATH, body=body)

    config = UbuntuSambaManagerConfig()
    manager = UbuntuSambaServerManager(runner=runner, config=config)

    found = manager.get_share(name="public")
    testbase.assert_equal(found is not None, True)
    testbase.assert_equal(found.name, "public")
    testbase.assert_equal(found.path, "/srv/samba/public")

    missing = manager.get_share(name="absent")
    testbase.assert_equal(missing, None)
    return


# ----------------------------------------------------------------------
# set_share_permissions
# ----------------------------------------------------------------------


def test_set_share_permissions_invokes_smbcontrol():
    """
        set_share_permissions rewrites the permission directives
        and chains ``smbcontrol all reload-config``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    existing = (
        "[public]\n"
        "    path = /srv/samba/public\n"
        "    read only = no\n"
        "    valid users = alice\n"
    )
    _seed_cat(runner, path=_SMB_CONF_PATH, body=existing)

    config = UbuntuSambaManagerConfig(elevated=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)

    acl = UbuntuSambaShareAcl(
        read_users=["alice"],
        write_users=["bob"],
        full_access_users=["admin"],
        denied_users=["mallory"],
    )

    result = manager.set_share_permissions(name="public", acl=acl)

    testbase.assert_equal(result.status, UbuntuSambaActionStatus.SUCCESS)
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    written = tee_calls[0]["stdin"]
    testbase.assert_equal("valid users = admin" in written, True)
    testbase.assert_equal("read list = alice" in written, True)
    testbase.assert_equal("write list = bob" in written, True)
    testbase.assert_equal("invalid users = mallory" in written, True)

    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["smbcontrol", "all", "reload-config"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    return


def test_set_share_permissions_order_insensitive_idempotency():
    """
        set_share_permissions returns SKIPPED when the existing
        directives match the requested ACL regardless of principal
        ordering.
    """
    runner = _MockCommandRunner()
    existing = (
        "[public]\n"
        "    path = /srv/samba/public\n"
        "    read only = no\n"
        "    valid users = admin\n"
        "    read list = bob alice\n"
        "    write list = carol dave\n"
    )
    _seed_cat(runner, path=_SMB_CONF_PATH, body=existing)

    config = UbuntuSambaManagerConfig(elevated=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)

    acl = UbuntuSambaShareAcl(
        read_users=["alice", "bob"],
        write_users=["dave", "carol"],
        full_access_users=["admin"],
        denied_users=[],
    )

    result = manager.set_share_permissions(name="public", acl=acl)

    testbase.assert_equal(result.status, UbuntuSambaActionStatus.SKIPPED)
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


# ----------------------------------------------------------------------
# reload_config
# ----------------------------------------------------------------------


def test_reload_config_invokes_smbcontrol():
    """
        reload_config dispatches ``smbcontrol all reload-config``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    config = UbuntuSambaManagerConfig(elevated=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)

    result = manager.reload_config()

    testbase.assert_equal(result.status, UbuntuSambaActionStatus.SUCCESS)
    testbase.assert_equal(
        result.command, ["smbcontrol", "all", "reload-config"],
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    return


# ----------------------------------------------------------------------
# add_samba_user
# ----------------------------------------------------------------------


def test_add_samba_user_passes_password_via_stdin():
    """
        add_samba_user dispatches ``smbpasswd -s -a <name>`` with
        the password delivered on stdin (twice — ``-s`` reads new
        and confirmation off stdin).
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_pdbedit_short(runner, stdout="")

    config = UbuntuSambaManagerConfig(elevated=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)

    result = manager.add_samba_user(
        name="alice",
        password="hunter2",
    )

    testbase.assert_equal(result.status, UbuntuSambaActionStatus.SUCCESS)
    smbpasswd_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 4
            and call["args"][0] == "smbpasswd"
            and call["args"][1] == "-s"
            and call["args"][2] == "-a"
            and call["args"][3] == "alice"
        )
    ]
    testbase.assert_equal(len(smbpasswd_calls), 1)

    stdin_payload = smbpasswd_calls[0]["stdin"]
    testbase.assert_equal(stdin_payload, "hunter2\nhunter2\n")
    testbase.assert_equal(smbpasswd_calls[0]["elevated"], True)
    return


def test_add_samba_user_skipped_when_present():
    """
        add_samba_user returns SKIPPED when ``pdbedit -L`` already
        lists the user.
    """
    runner = _MockCommandRunner()
    _seed_pdbedit_short(
        runner,
        stdout=(
            "alice:1000:Alice User\n"
            "bob:1001:Bob User\n"
        ),
    )

    config = UbuntuSambaManagerConfig(elevated=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)

    result = manager.add_samba_user(name="alice", password="hunter2")

    testbase.assert_equal(result.status, UbuntuSambaActionStatus.SKIPPED)

    smbpasswd_calls = [
        call for call in runner.calls
        if call["args"][0] == "smbpasswd"
    ]
    testbase.assert_equal(len(smbpasswd_calls), 0)
    return


def test_password_redacted_in_recorded_command():
    """
        The recorded ``command`` field on the
        :class:`UbuntuSambaActionResult` for ``add_samba_user`` MUST
        NOT contain the password substring.  This is verified across
        DRY_RUN, SKIPPED, SUCCESS, and FAILED outcomes.
    """
    secret = "topSecretP@ss"

    # ---- SUCCESS path
    runner_ok = _MockCommandRunner()
    runner_ok.default_result = _ok()
    _seed_pdbedit_short(runner_ok, stdout="")
    config_ok = UbuntuSambaManagerConfig(elevated=True)
    manager_ok = UbuntuSambaServerManager(runner=runner_ok, config=config_ok)
    success_result = manager_ok.add_samba_user(
        name="alice", password=secret,
    )
    testbase.assert_equal(success_result.status, UbuntuSambaActionStatus.SUCCESS)
    recorded_success = " ".join(success_result.command)
    testbase.assert_equal(secret in recorded_success, False)

    # ---- DRY_RUN path
    runner_dry = _MockCommandRunner()
    _seed_pdbedit_short(runner_dry, stdout="")
    config_dry = UbuntuSambaManagerConfig(elevated=True, dry_run=True)
    manager_dry = UbuntuSambaServerManager(runner=runner_dry, config=config_dry)
    dry_result = manager_dry.add_samba_user(
        name="alice", password=secret,
    )
    testbase.assert_equal(dry_result.status, UbuntuSambaActionStatus.DRY_RUN)
    recorded_dry = " ".join(dry_result.command)
    testbase.assert_equal(secret in recorded_dry, False)

    # ---- FAILED path
    runner_fail = _MockCommandRunner()
    _seed_pdbedit_short(runner_fail, stdout="")
    runner_fail.results[
        ("smbpasswd", "-s", "-a", "alice")
    ] = _fail(stderr="smbpasswd: failed", exit_code=2)
    config_fail = UbuntuSambaManagerConfig(elevated=True)
    manager_fail = UbuntuSambaServerManager(
        runner=runner_fail, config=config_fail,
    )
    failed_result = manager_fail.add_samba_user(
        name="alice", password=secret,
    )
    testbase.assert_equal(failed_result.status, UbuntuSambaActionStatus.FAILED)
    recorded_failed = " ".join(failed_result.command)
    testbase.assert_equal(secret in recorded_failed, False)

    # ---- SKIPPED path
    runner_skip = _MockCommandRunner()
    _seed_pdbedit_short(runner_skip, stdout="alice:1000:Alice\n")
    config_skip = UbuntuSambaManagerConfig(elevated=True)
    manager_skip = UbuntuSambaServerManager(runner=runner_skip, config=config_skip)
    skipped_result = manager_skip.add_samba_user(
        name="alice", password=secret,
    )
    testbase.assert_equal(skipped_result.status, UbuntuSambaActionStatus.SKIPPED)
    recorded_skip = " ".join(skipped_result.command)
    testbase.assert_equal(secret in recorded_skip, False)
    return


# ----------------------------------------------------------------------
# remove_samba_user
# ----------------------------------------------------------------------


def test_remove_samba_user_invokes_smbpasswd():
    """
        remove_samba_user dispatches ``smbpasswd -x <name>`` when
        the user is present in ``pdbedit -L``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_pdbedit_short(runner, stdout="alice:1000:Alice\n")

    config = UbuntuSambaManagerConfig(elevated=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)

    result = manager.remove_samba_user(name="alice")

    testbase.assert_equal(result.status, UbuntuSambaActionStatus.SUCCESS)
    smbpasswd_calls = [
        call for call in runner.calls
        if (
            len(call["args"]) >= 3
            and call["args"][0] == "smbpasswd"
            and call["args"][1] == "-x"
            and call["args"][2] == "alice"
        )
    ]
    testbase.assert_equal(len(smbpasswd_calls), 1)
    testbase.assert_equal(smbpasswd_calls[0]["elevated"], True)
    return


def test_remove_samba_user_skipped_when_absent():
    """
        remove_samba_user returns SKIPPED when ``pdbedit -L`` does
        not list the user.
    """
    runner = _MockCommandRunner()
    _seed_pdbedit_short(runner, stdout="bob:1001:Bob\n")

    config = UbuntuSambaManagerConfig(elevated=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)

    result = manager.remove_samba_user(name="alice")

    testbase.assert_equal(result.status, UbuntuSambaActionStatus.SKIPPED)
    smbpasswd_calls = [
        call for call in runner.calls
        if call["args"][0] == "smbpasswd"
    ]
    testbase.assert_equal(len(smbpasswd_calls), 0)
    return


# ----------------------------------------------------------------------
# list_samba_users
# ----------------------------------------------------------------------


def test_list_samba_users_parses_pdbedit_output():
    """
        list_samba_users parses ``pdbedit -L -v`` blocks into
        :class:`UbuntuSambaUserInfo` records.
    """
    runner = _MockCommandRunner()
    _seed_pdbedit_verbose(
        runner,
        stdout=(
            "Unix username:        alice\n"
            "NT username:          alice\n"
            "Account Flags:        [U          ]\n"
            "Full Name:            Alice\n"
            "Account desc:         Alice's account\n"
            "Password last set:    Mon, 01 Jan 2024 00:00:00 UTC\n"
            "\n"
            "Unix username:        bob\n"
            "NT username:          bob\n"
            "Account Flags:        [DU         ]\n"
            "Full Name:            Bob\n"
            "Account desc:         \n"
            "Password last set:    Mon, 02 Jan 2024 00:00:00 UTC\n"
        ),
    )

    config = UbuntuSambaManagerConfig()
    manager = UbuntuSambaServerManager(runner=runner, config=config)

    rtnval = manager.list_samba_users()

    testbase.assert_equal(len(rtnval), 2)
    by_name = {entry.username: entry for entry in rtnval}
    testbase.assert_equal(by_name["alice"].enabled, True)
    testbase.assert_equal(
        by_name["alice"].description, "Alice's account",
    )
    testbase.assert_equal(by_name["bob"].enabled, False)
    testbase.assert_equal(by_name["bob"].description, None)
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write verbs pass ``elevated=True`` to the runner when
        ``config.elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_cat(runner, path=_SMB_CONF_PATH, body="")

    config = UbuntuSambaManagerConfig(elevated=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)

    spec = _make_spec(name="public")
    manager.add_share(spec=spec)

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["smbcontrol", "all", "reload-config"]
    ]
    testbase.assert_equal(reload_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs (``list_shares``, ``get_share``,
        ``list_samba_users``) and the ``cat`` / ``pdbedit -L``
        probes used inside write paths never pass ``elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_cat(
        runner,
        path=_SMB_CONF_PATH,
        body=(
            "[public]\n"
            "    path = /srv/samba/public\n"
            "    read only = no\n"
        ),
    )
    _seed_pdbedit_verbose(
        runner,
        stdout=(
            "Unix username:        alice\n"
            "Account Flags:        [U          ]\n"
        ),
    )

    config = UbuntuSambaManagerConfig(elevated=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)

    manager.list_shares()
    manager.get_share(name="public")
    manager.list_samba_users()

    for call in runner.calls:
        if call["args"][0] == "cat":
            testbase.assert_equal(call["elevated"], False)
        if call["args"] == ["pdbedit", "-L", "-v"]:
            testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Failure surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failing ``tee`` produces a FAILED result with the
        runner's stderr, exit code, and command attached.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path=_SMB_CONF_PATH, body="")
    runner.results[("tee", _SMB_CONF_PATH)] = _fail(
        stderr="tee: cannot open", exit_code=1,
    )

    config = UbuntuSambaManagerConfig(elevated=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)
    spec = _make_spec(name="public")

    result = manager.add_share(spec=spec)

    testbase.assert_equal(result.status, UbuntuSambaActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal("tee: cannot open" in result.stderr, True)
    testbase.assert_equal(result.command[0], "tee")

    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["smbcontrol", "all", "reload-config"]
    ]
    testbase.assert_equal(len(reload_calls), 0)
    return


def test_reload_failure_propagates():
    """
        When ``smbcontrol`` fails after a successful ``tee``, the
        verb maps to FAILED and the reload's command + stderr is on
        the result.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path=_SMB_CONF_PATH, body="")
    runner.results[("tee", _SMB_CONF_PATH)] = _ok()
    runner.results[
        ("smbcontrol", "all", "reload-config")
    ] = _fail(stderr="smbcontrol: failed", exit_code=2)

    config = UbuntuSambaManagerConfig(elevated=True)
    manager = UbuntuSambaServerManager(runner=runner, config=config)
    spec = _make_spec(name="public")

    result = manager.add_share(spec=spec)

    testbase.assert_equal(result.status, UbuntuSambaActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(
        result.command, ["smbcontrol", "all", "reload-config"],
    )
    testbase.assert_equal("smbcontrol: failed" in result.stderr, True)
    return


def test_list_shares_raises_query_error_on_unexpected_failure():
    """
        list_shares raises :class:`UbuntuSambaQueryError` when
        ``cat`` exits non-zero AND stderr is non-empty (i.e., not a
        simple missing-file case).
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _SMB_CONF_PATH)] = _fail(
        stderr="cat: permission denied", exit_code=1,
    )

    config = UbuntuSambaManagerConfig()
    manager = UbuntuSambaServerManager(runner=runner, config=config)

    raised: Optional[UbuntuSambaQueryError] = None
    try:
        manager.list_shares()
    except UbuntuSambaQueryError as exc:
        raised = exc
    testbase.assert_equal(raised is not None, True)
    testbase.assert_equal(raised.exit_code, 1)
    return


# ----------------------------------------------------------------------
# Renderer / parser unit tests
# ----------------------------------------------------------------------


def test_parse_smb_conf_preserves_sections():
    """
        ``_parse_smb_conf`` carves the file into ordered sections
        keyed by header name.
    """
    body = (
        "# Top comment\n"
        "[global]\n"
        "    workgroup = WORKGROUP\n"
        "[public]\n"
        "    path = /srv/share\n"
    )
    sections = _parse_smb_conf(content=body)
    section_names = [section.name for section in sections]
    testbase.assert_equal(section_names, [None, "global", "public"])
    return


def test_render_smb_conf_round_trips():
    """
        ``_render_smb_conf`` emits the same content that
        ``_parse_smb_conf`` accepted (modulo the trailing newline
        canonicalisation).
    """
    body = (
        "[global]\n"
        "    workgroup = WORKGROUP\n"
        "[public]\n"
        "    path = /srv/share\n"
        "    read only = no\n"
    )
    sections = _parse_smb_conf(content=body)
    rendered = _render_smb_conf(sections=sections)
    testbase.assert_equal("[global]" in rendered, True)
    testbase.assert_equal("[public]" in rendered, True)
    testbase.assert_equal("workgroup = WORKGROUP" in rendered, True)
    testbase.assert_equal("path = /srv/share" in rendered, True)
    testbase.assert_equal(rendered.endswith("\n"), True)
    return


def test_parse_pdbedit_output_handles_blocks():
    """
        ``_parse_pdbedit_output`` splits multi-block output and
        tracks the disabled flag.
    """
    text = (
        "Unix username:        alice\n"
        "Account Flags:        [U          ]\n"
        "Account desc:         Alice\n"
        "\n"
        "Unix username:        bob\n"
        "Account Flags:        [DU         ]\n"
        "Account desc:         Bob\n"
    )
    parsed = _parse_pdbedit_output(text=text)
    testbase.assert_equal(len(parsed), 2)
    by_name = {entry.username: entry for entry in parsed}
    testbase.assert_equal(by_name["alice"].enabled, True)
    testbase.assert_equal(by_name["bob"].enabled, False)
    return


# ----------------------------------------------------------------------
# Result-type sanity
# ----------------------------------------------------------------------


def test_share_info_dataclass_shape():
    """
        :class:`UbuntuSambaShareInfo` exposes the contracted
        fields.
    """
    info = UbuntuSambaShareInfo(
        name="public",
        path="/srv/samba/public",
        description=None,
        read_only=False,
        valid_users=None,
        read_list=None,
        write_list=None,
        browseable=True,
        guest_ok=False,
        is_active=True,
        connection_count=None,
    )
    testbase.assert_equal(info.name, "public")
    testbase.assert_equal(info.is_active, True)
    testbase.assert_equal(info.connection_count, None)
    return


def test_user_info_dataclass_shape():
    """
        :class:`UbuntuSambaUserInfo` exposes the contracted fields.
    """
    info = UbuntuSambaUserInfo(
        username="alice",
        enabled=True,
        last_password_change=None,
        description=None,
    )
    testbase.assert_equal(info.username, "alice")
    testbase.assert_equal(info.enabled, True)
    return
