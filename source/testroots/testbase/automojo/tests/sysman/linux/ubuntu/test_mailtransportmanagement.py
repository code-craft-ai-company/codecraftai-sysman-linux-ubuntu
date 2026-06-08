"""
    Tests for :class:`UbuntuMailTransportManager`.

    Tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  All file I/O (``cat`` / ``tee``) and command interaction
    (``postconf`` / ``postqueue`` / ``mailq`` / ``newaliases`` /
    ``sendmail`` / ``systemctl``) is exercised entirely through the
    runner.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.mailtransportmanagement import (
    UbuntuMailTransportManager,
    UbuntuMtaActionStatus,
    UbuntuMtaAliasInfo,
    UbuntuMtaConfig,
    UbuntuMtaManagerConfig,
    UbuntuMtaQueryError,
    UbuntuMtaQueueStatus,
    _parse_aliases_file,
    _parse_mailq_output,
    _parse_postconf_n_output,
)


# ----------------------------------------------------------------------
# Mock runner + helpers
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv
        tuple.
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
    stdout: str = "", stderr: str = "", exit_code: int = 1,
) -> RunResult:
    """
        Build a failure :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


_ALIASES_PATH = "/etc/aliases"


# ----------------------------------------------------------------------
# get_mta_config
# ----------------------------------------------------------------------


def test_get_mta_config_parses_postconf_output():
    """
        get_mta_config parses ``postconf -n`` output into a
        :class:`UbuntuMtaConfig`.
    """
    runner = _MockCommandRunner()
    runner.results[("postconf", "-n")] = _ok(
        stdout=(
            "myhostname = mail.example.com\n"
            "mydomain = example.com\n"
            "myorigin = $mydomain\n"
            "relayhost = [smtp.relay.example.com]:587\n"
            "mynetworks = 127.0.0.0/8 [::1]/128\n"
            "inet_interfaces = all\n"
            "inet_protocols = ipv4\n"
            "mailbox_size_limit = 0\n"
            "message_size_limit = 10240000\n"
            "smtpd_banner = $myhostname ESMTP $mail_name\n"
        ),
    )

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(),
    )
    config = manager.get_mta_config()

    testbase.assert_equal(config.myhostname, "mail.example.com")
    testbase.assert_equal(config.mydomain, "example.com")
    testbase.assert_equal(config.myorigin, "$mydomain")
    testbase.assert_equal(config.relayhost, "[smtp.relay.example.com]:587")
    testbase.assert_equal(config.inet_interfaces, "all")
    testbase.assert_equal(config.mailbox_size_limit, 0)
    testbase.assert_equal(config.message_size_limit, 10240000)
    testbase.assert_equal(
        config.extras["smtpd_banner"], "$myhostname ESMTP $mail_name",
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


def test_get_mta_config_raises_on_failure_with_stderr():
    """
        get_mta_config raises :class:`UbuntuMtaQueryError` when
        ``postconf`` fails with non-empty stderr.
    """
    runner = _MockCommandRunner()
    runner.results[("postconf", "-n")] = _fail(
        stderr="postconf: not found", exit_code=127,
    )

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(),
    )

    raised: Optional[UbuntuMtaQueryError] = None
    try:
        manager.get_mta_config()
    except UbuntuMtaQueryError as exc:
        raised = exc
    testbase.assert_equal(raised is not None, True)
    testbase.assert_equal(raised.exit_code, 127)
    return


# ----------------------------------------------------------------------
# set_mta_config
# ----------------------------------------------------------------------


def test_set_mta_config_invokes_postconf():
    """
        set_mta_config dispatches one ``postconf -e`` per directive
        that diverges and a final ``systemctl reload postfix``.
    """
    runner = _MockCommandRunner()
    runner.results[("postconf", "-n")] = _ok(
        stdout="myhostname = old.example.com\n",
    )
    runner.default_result = _ok()

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(elevated=True),
    )
    spec = UbuntuMtaConfig(
        myhostname="new.example.com",
        myorigin="example.com",
    )
    result = manager.set_mta_config(spec=spec)

    testbase.assert_equal(result.status, UbuntuMtaActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["postconf", "-e"]
    ]
    testbase.assert_equal(len(write_calls), 2)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["postconf", "-e", "myhostname=new.example.com"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    reload_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemctl", "reload"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    testbase.assert_equal(
        reload_calls[0]["args"],
        ["systemctl", "reload", "postfix"],
    )
    return


def test_set_mta_config_skipped_per_setting_when_match():
    """
        set_mta_config skips a per-directive dispatch when the live
        value already matches the spec, and reports the aggregate
        result as SKIPPED when every directive matches.
    """
    runner = _MockCommandRunner()
    runner.results[("postconf", "-n")] = _ok(
        stdout=(
            "myhostname = mail.example.com\n"
            "mydomain = example.com\n"
        ),
    )
    runner.default_result = _ok()

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(),
    )
    spec = UbuntuMtaConfig(
        myhostname="mail.example.com",
        mydomain="example.com",
    )
    result = manager.set_mta_config(spec=spec)

    testbase.assert_equal(result.status, UbuntuMtaActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["postconf", "-e"]
    ]
    testbase.assert_equal(len(write_calls), 0)
    reload_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemctl", "reload"]
    ]
    testbase.assert_equal(len(reload_calls), 0)
    return


def test_set_mta_config_dry_run_short_circuits():
    """
        set_mta_config returns DRY_RUN without invoking the runner
        when ``dry_run=True``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(dry_run=True),
    )
    spec = UbuntuMtaConfig(myhostname="mail.example.com")
    result = manager.set_mta_config(spec=spec)

    testbase.assert_equal(result.status, UbuntuMtaActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# add_alias
# ----------------------------------------------------------------------


def test_add_alias_invokes_tee_and_newaliases():
    """
        add_alias writes the rebuilt aliases content via ``tee`` and
        rebuilds the database via ``newaliases``.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _ALIASES_PATH)] = _ok(stdout="postmaster: root\n")
    runner.default_result = _ok()

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(elevated=True),
    )
    result = manager.add_alias(local="alice", remote="alice@example.com")

    testbase.assert_equal(result.status, UbuntuMtaActionStatus.SUCCESS)
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["args"], ["tee", _ALIASES_PATH])
    testbase.assert_equal(
        "alice: alice@example.com" in tee_calls[0]["stdin"], True,
    )
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    newaliases_calls = [
        c for c in runner.calls
        if c["args"] == ["newaliases"]
    ]
    testbase.assert_equal(len(newaliases_calls), 1)
    testbase.assert_equal(newaliases_calls[0]["elevated"], True)
    return


def test_add_alias_skipped_when_present():
    """
        add_alias returns SKIPPED when the same alias / remote pair
        already exists in the file.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _ALIASES_PATH)] = _ok(
        stdout="postmaster: root\nalice: alice@example.com\n",
    )

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(),
    )
    result = manager.add_alias(local="alice", remote="alice@example.com")

    testbase.assert_equal(result.status, UbuntuMtaActionStatus.SKIPPED)
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


def test_add_alias_dry_run_short_circuits():
    """
        add_alias returns DRY_RUN without invoking ``tee`` /
        ``newaliases`` under dry-run.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _ALIASES_PATH)] = _ok(stdout="")

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(dry_run=True),
    )
    result = manager.add_alias(local="bob", remote="bob@example.com")

    testbase.assert_equal(result.status, UbuntuMtaActionStatus.DRY_RUN)
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


# ----------------------------------------------------------------------
# remove_alias
# ----------------------------------------------------------------------


def test_remove_alias_invokes_tee_and_newaliases():
    """
        remove_alias rewrites ``/etc/aliases`` without the named
        entry and triggers ``newaliases``.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _ALIASES_PATH)] = _ok(
        stdout="postmaster: root\nalice: alice@example.com\n",
    )
    runner.default_result = _ok()

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(elevated=True),
    )
    result = manager.remove_alias(local="alice")

    testbase.assert_equal(result.status, UbuntuMtaActionStatus.SUCCESS)
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        "alice: alice@example.com" in tee_calls[0]["stdin"], False,
    )
    newaliases_calls = [
        c for c in runner.calls
        if c["args"] == ["newaliases"]
    ]
    testbase.assert_equal(len(newaliases_calls), 1)
    return


def test_remove_alias_skipped_when_absent():
    """
        remove_alias returns SKIPPED when the alias is not present.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _ALIASES_PATH)] = _ok(stdout="postmaster: root\n")

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(),
    )
    result = manager.remove_alias(local="alice")

    testbase.assert_equal(result.status, UbuntuMtaActionStatus.SKIPPED)
    tee_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


# ----------------------------------------------------------------------
# list_aliases
# ----------------------------------------------------------------------


def test_list_aliases_parses_etc_aliases():
    """
        list_aliases parses ``/etc/aliases`` into entries.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _ALIASES_PATH)] = _ok(
        stdout=(
            "# comment\n"
            "postmaster: root\n"
            "root: ops@example.com\n"
            "abuse: security@example.com, ops@example.com\n"
        ),
    )

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(),
    )
    aliases = manager.list_aliases()

    testbase.assert_equal(len(aliases), 3)
    testbase.assert_equal(aliases[0].local, "postmaster")
    testbase.assert_equal(aliases[0].remote, "root")
    testbase.assert_equal(aliases[1].local, "root")
    testbase.assert_equal(aliases[1].remote, "ops@example.com")
    testbase.assert_equal(aliases[2].local, "abuse")
    testbase.assert_equal(
        aliases[2].remote,
        "security@example.com, ops@example.com",
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


def test_list_aliases_returns_empty_when_file_missing():
    """
        list_aliases returns an empty list when ``cat`` reports
        non-zero (file missing).
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _ALIASES_PATH)] = _fail(
        stderr="No such file", exit_code=1,
    )

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(),
    )
    aliases = manager.list_aliases()

    testbase.assert_equal(len(aliases), 0)
    return


# ----------------------------------------------------------------------
# test_mail_delivery
# ----------------------------------------------------------------------


def test_test_mail_delivery_invokes_sendmail():
    """
        test_mail_delivery hands a properly formatted RFC 5322
        message to ``sendmail -t`` on stdin.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(elevated=True),
    )
    result = manager.test_mail_delivery(
        to="ops@example.com",
        from_addr="probe@example.com",
        subject="probe",
    )

    testbase.assert_equal(result.status, UbuntuMtaActionStatus.SUCCESS)
    sendmail_calls = [
        c for c in runner.calls
        if c["args"] == ["sendmail", "-t"]
    ]
    testbase.assert_equal(len(sendmail_calls), 1)
    payload = sendmail_calls[0]["stdin"]
    testbase.assert_equal("Subject: probe" in payload, True)
    testbase.assert_equal("From: probe@example.com" in payload, True)
    testbase.assert_equal("To: ops@example.com" in payload, True)
    testbase.assert_equal(sendmail_calls[0]["elevated"], True)
    return


def test_test_mail_delivery_dry_run_short_circuits():
    """
        test_mail_delivery returns DRY_RUN without invoking
        ``sendmail`` under dry-run.
    """
    runner = _MockCommandRunner()
    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(dry_run=True),
    )
    result = manager.test_mail_delivery(
        to="ops@example.com",
        from_addr="probe@example.com",
        subject="probe",
    )

    testbase.assert_equal(result.status, UbuntuMtaActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# get_queue_status
# ----------------------------------------------------------------------


def test_get_queue_status_parses_mailq():
    """
        get_queue_status parses a representative ``mailq`` output.
    """
    runner = _MockCommandRunner()
    runner.results[("mailq",)] = _ok(
        stdout=(
            "-Queue ID-  --Size-- ----Arrival Time---- "
            "-Sender/Recipient-------\n"
            "ABC123*     1234     Mon Apr 22 09:00:00  sender@example.com\n"
            "                                          alice@example.com\n"
            "\n"
            "DEF456      4321     Mon Apr 22 09:05:00  sender@example.com\n"
            "(host smtp.example.com refused to talk to me)\n"
            "                                          bob@example.com\n"
            "\n"
            "-- 6 Kbytes in 2 Requests.\n"
        ),
    )

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(),
    )
    status = manager.get_queue_status()

    testbase.assert_equal(status.pending_count, 1)
    testbase.assert_equal(status.deferred_count, 1)
    testbase.assert_equal(status.total_size_bytes, 6 * 1024)
    testbase.assert_equal(
        status.queue_age_oldest, "Mon Apr 22 09:00:00",
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], False)
    return


def test_get_queue_status_empty_queue():
    """
        get_queue_status returns a zeroed status when ``mailq`` reports
        an empty queue.
    """
    runner = _MockCommandRunner()
    runner.results[("mailq",)] = _ok(stdout="Mail queue is empty\n")

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(),
    )
    status = manager.get_queue_status()

    testbase.assert_equal(status.pending_count, 0)
    testbase.assert_equal(status.deferred_count, 0)
    testbase.assert_equal(status.total_size_bytes is None, True)
    return


# ----------------------------------------------------------------------
# flush_queue
# ----------------------------------------------------------------------


def test_flush_queue_invokes_postqueue():
    """
        flush_queue dispatches ``postqueue -f``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(elevated=True),
    )
    result = manager.flush_queue()

    testbase.assert_equal(result.status, UbuntuMtaActionStatus.SUCCESS)
    testbase.assert_equal(runner.calls[-1]["args"], ["postqueue", "-f"])
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Every write verb passes ``elevated=True`` to the runner when
        the config requests it.
    """
    runner = _MockCommandRunner()
    runner.results[("postconf", "-n")] = _ok(stdout="")
    runner.results[("cat", _ALIASES_PATH)] = _ok(stdout="")
    runner.default_result = _ok()

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(elevated=True),
    )
    manager.set_mta_config(
        spec=UbuntuMtaConfig(myhostname="new.example.com"),
    )
    manager.add_alias(local="x", remote="y@example.com")
    manager.remove_alias(local="x")
    manager.test_mail_delivery(
        to="t@example.com", from_addr="f@example.com", subject="s",
    )
    manager.flush_queue()

    write_argv_prefixes = (
        ("postconf", "-e"),
        ("tee",),
        ("newaliases",),
        ("sendmail",),
        ("postqueue", "-f"),
        ("systemctl", "reload"),
    )
    for call in runner.calls:
        argv = tuple(call["args"])
        for prefix in write_argv_prefixes:
            if argv[:len(prefix)] == prefix:
                testbase.assert_equal(call["elevated"], True)
                break
    return


def test_reads_never_elevate():
    """
        Reads MUST NOT pass ``elevated=True`` even when the config
        requests it.
    """
    runner = _MockCommandRunner()
    runner.results[("postconf", "-n")] = _ok(stdout="myhostname = h\n")
    runner.results[("cat", _ALIASES_PATH)] = _ok(stdout="")
    runner.results[("mailq",)] = _ok(stdout="Mail queue is empty\n")

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(elevated=True),
    )
    manager.get_mta_config()
    manager.list_aliases()
    manager.get_queue_status()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Error surface
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failing write verb returns FAILED with ``stderr`` populated
        from the runner.
    """
    runner = _MockCommandRunner()
    runner.results[("postqueue", "-f")] = _fail(
        stderr="postqueue: connect: Connection refused",
        exit_code=1,
    )

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(elevated=True),
    )
    result = manager.flush_queue()

    testbase.assert_equal(result.status, UbuntuMtaActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(
        "Connection refused" in result.stderr, True,
    )
    return


def test_add_alias_failed_when_newaliases_fails():
    """
        add_alias maps to FAILED when ``tee`` succeeds but
        ``newaliases`` fails — preserving the chained-mutation
        invariant from the contract.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _ALIASES_PATH)] = _ok(stdout="postmaster: root\n")
    runner.results[("tee", _ALIASES_PATH)] = _ok()
    runner.results[("newaliases",)] = _fail(
        stderr="newaliases: fatal", exit_code=2,
    )

    manager = UbuntuMailTransportManager(
        runner=runner,
        config=UbuntuMtaManagerConfig(elevated=True),
    )
    result = manager.add_alias(local="alice", remote="alice@example.com")

    testbase.assert_equal(result.status, UbuntuMtaActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal("fatal" in result.stderr, True)
    return


# ----------------------------------------------------------------------
# Module-level parser sanity tests
# ----------------------------------------------------------------------


def test_parse_postconf_n_collects_extras():
    """
        The parser places unrecognised directives in ``extras``.
    """
    config = _parse_postconf_n_output(
        text=(
            "myhostname = host.example.com\n"
            "smtpd_use_tls = yes\n"
            "smtp_tls_security_level = may\n"
        ),
    )
    testbase.assert_equal(config.myhostname, "host.example.com")
    testbase.assert_equal(config.extras["smtpd_use_tls"], "yes")
    testbase.assert_equal(
        config.extras["smtp_tls_security_level"], "may",
    )
    return


def test_parse_aliases_handles_continuation():
    """
        The aliases parser folds continuation (whitespace-led) lines
        into the previous entry's ``remote``.
    """
    aliases = _parse_aliases_file(
        content=(
            "team: alice@example.com,\n"
            "    bob@example.com,\n"
            "    carol@example.com\n"
        ),
    )
    testbase.assert_equal(len(aliases), 1)
    testbase.assert_equal(aliases[0].local, "team")
    testbase.assert_equal("alice@example.com" in aliases[0].remote, True)
    testbase.assert_equal("carol@example.com" in aliases[0].remote, True)
    return


def test_parse_mailq_active_marker():
    """
        The mailq parser counts ``*``-marked queue ids as pending and
        unmarked queue ids as deferred.
    """
    status = _parse_mailq_output(
        text=(
            "-Queue ID-  --Size-- ----Arrival Time---- "
            "-Sender/Recipient-------\n"
            "AAA111*     100      Mon Apr 22 10:00:00  s1@example.com\n"
            "                                          r1@example.com\n"
            "\n"
            "BBB222      200      Mon Apr 22 10:05:00  s2@example.com\n"
            "                                          r2@example.com\n"
            "\n"
            "CCC333      300      Mon Apr 22 10:10:00  s3@example.com\n"
            "                                          r3@example.com\n"
            "\n"
            "-- 1 Kbytes in 3 Requests.\n"
        ),
    )
    testbase.assert_equal(status.pending_count, 1)
    testbase.assert_equal(status.deferred_count, 2)
    testbase.assert_equal(status.total_size_bytes, 1024)
    return
