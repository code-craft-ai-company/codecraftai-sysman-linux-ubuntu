"""
    Tests for :class:`UbuntuRemoteLogForwardingManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult` values
    keyed by argv tuple.  No live ``rsyslog`` / ``systemctl`` /
    ``logger`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.remotelogforwarding import (
    UbuntuForwarderActionStatus,
    UbuntuForwarderInfo,
    UbuntuForwarderManagerConfig,
    UbuntuForwarderQueryError,
    UbuntuForwarderSpec,
    UbuntuForwarderStatus,
    UbuntuForwarderTestResult,
    UbuntuRemoteLogForwardingManager,
    _parse_drop_in,
    _render_drop_in,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every
        call and returns pre-seeded :class:`RunResult` values
        keyed by argv tuple.
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


def _seed_test_dash_e(
    runner: _MockCommandRunner,
    *,
    path: str,
    exists: bool,
) -> None:
    """
        Seed the runner so that ``test -e <path>`` reports
        existence according to ``exists``.
    """
    if exists:
        result = RunResult(exit_code=0, stdout="", stderr="")
    else:
        result = RunResult(exit_code=1, stdout="", stderr="")
    runner.results[("test", "-e", path)] = result
    return


def _seed_cat(
    runner: _MockCommandRunner,
    *,
    path: str,
    body: Optional[str],
) -> None:
    """
        Seed the runner so that ``cat <path>`` returns ``body``
        on success, or a non-zero exit when ``body`` is None.
    """
    if body is None:
        result = RunResult(
            exit_code=1, stdout="", stderr="No such file",
        )
    else:
        result = RunResult(exit_code=0, stdout=body, stderr="")
    runner.results[("cat", path)] = result
    return


# ----------------------------------------------------------------------
# add_forwarder
# ----------------------------------------------------------------------


def test_add_forwarder_invokes_tee():
    """
        add_forwarder dispatches ``tee
        /etc/rsyslog.d/automojo-fwd-<id>.conf`` with the rendered
        body on stdin and follows up with
        ``systemctl restart rsyslog``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    config = UbuntuForwarderManagerConfig(elevated=True)
    manager = UbuntuRemoteLogForwardingManager(
        runner=runner, config=config,
    )
    spec = UbuntuForwarderSpec(
        id="collector1", host="logs.example.com", port=6514,
        protocol="tcp", selector="*.*",
    )
    result = manager.add_forwarder(spec=spec)

    testbase.assert_equal(
        result.status, UbuntuForwarderActionStatus.SUCCESS,
    )
    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"],
        [
            "tee",
            "/etc/rsyslog.d/automojo-fwd-collector1.conf",
        ],
    )
    body = tee_calls[0]["stdin"]
    testbase.assert_equal(
        "*.* @@logs.example.com:6514" in body, True,
    )

    restart_calls = [
        call for call in runner.calls
        if call["args"] == ["systemctl", "restart", "rsyslog"]
    ]
    testbase.assert_equal(len(restart_calls), 1)
    testbase.assert_equal(restart_calls[0]["elevated"], True)
    return


def test_add_forwarder_skipped_when_present():
    """
        add_forwarder returns SKIPPED when the existing drop-in
        body matches the rendered desired body byte-for-byte.
    """
    runner = _MockCommandRunner()
    spec = UbuntuForwarderSpec(
        id="collector1", host="logs.example.com", port=6514,
        protocol="tcp", selector="*.*",
    )
    desired_body = _render_drop_in(spec=spec)

    path = "/etc/rsyslog.d/automojo-fwd-collector1.conf"
    _seed_cat(runner, path=path, body=desired_body)

    manager = UbuntuRemoteLogForwardingManager(
        runner=runner, config=UbuntuForwarderManagerConfig(),
    )
    result = manager.add_forwarder(spec=spec)
    testbase.assert_equal(
        result.status, UbuntuForwarderActionStatus.SKIPPED,
    )
    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


def test_add_forwarder_dry_run():
    """
        With ``dry_run=True``, add_forwarder records the
        intended argv but does not invoke ``tee`` or
        ``systemctl``.
    """
    runner = _MockCommandRunner()
    config = UbuntuForwarderManagerConfig(dry_run=True)
    manager = UbuntuRemoteLogForwardingManager(
        runner=runner, config=config,
    )
    spec = UbuntuForwarderSpec(
        id="collector1", host="logs.example.com", port=514,
    )
    result = manager.add_forwarder(spec=spec)

    testbase.assert_equal(
        result.status, UbuntuForwarderActionStatus.DRY_RUN,
    )
    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    restart_calls = [
        call for call in runner.calls
        if call["args"] == ["systemctl", "restart", "rsyslog"]
    ]
    testbase.assert_equal(len(restart_calls), 0)
    return


def test_tls_spec_renders_gtls_config():
    """
        A TLS spec renders the gtls global directive plus the
        action-driver block so that ``rsyslog-gnutls`` accepts
        it.
    """
    spec = UbuntuForwarderSpec(
        id="tls1", host="secure.example.com", port=6514,
        protocol="tls", selector="*.*",
        tls_ca_cert="/etc/pki/ca-trust/source/anchors/ca.pem",
    )
    body = _render_drop_in(spec=spec)
    testbase.assert_equal(
        "$DefaultNetstreamDriver gtls" in body, True,
    )
    testbase.assert_equal(
        "$DefaultNetstreamDriverCAFile "
        "/etc/pki/ca-trust/source/anchors/ca.pem" in body,
        True,
    )
    testbase.assert_equal("$ActionSendStreamDriver gtls" in body, True)
    testbase.assert_equal(
        "$ActionSendStreamDriverMode 1" in body, True,
    )
    testbase.assert_equal(
        "$ActionSendStreamDriverAuthMode x509/name" in body, True,
    )
    testbase.assert_equal(
        "*.* @@secure.example.com:6514" in body, True,
    )
    return


def test_tls_spec_missing_ca_cert_returns_failed():
    """
        A TLS spec without ``tls_ca_cert`` is rejected before the
        runner is invoked.
    """
    runner = _MockCommandRunner()
    config = UbuntuForwarderManagerConfig()
    manager = UbuntuRemoteLogForwardingManager(
        runner=runner, config=config,
    )
    spec = UbuntuForwarderSpec(
        id="tls1", host="secure.example.com", port=6514,
        protocol="tls", selector="*.*",
        tls_ca_cert=None,
    )
    result = manager.add_forwarder(spec=spec)
    testbase.assert_equal(
        result.status, UbuntuForwarderActionStatus.FAILED,
    )
    testbase.assert_equal(
        "tls_ca_cert" in (result.message or ""), True,
    )
    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


# ----------------------------------------------------------------------
# remove_forwarder
# ----------------------------------------------------------------------


def test_remove_forwarder_invokes_rm():
    """
        remove_forwarder dispatches ``rm`` against the active
        drop-in path and follows up with
        ``systemctl restart rsyslog``.
    """
    runner = _MockCommandRunner()
    path = "/etc/rsyslog.d/automojo-fwd-collector1.conf"
    disabled_path = path + ".disabled"
    _seed_test_dash_e(runner, path=path, exists=True)
    _seed_test_dash_e(runner, path=disabled_path, exists=False)
    runner.results[("rm", path)] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    config = UbuntuForwarderManagerConfig(elevated=True)
    manager = UbuntuRemoteLogForwardingManager(
        runner=runner, config=config,
    )
    result = manager.remove_forwarder(id="collector1")
    testbase.assert_equal(
        result.status, UbuntuForwarderActionStatus.SUCCESS,
    )

    rm_calls = [
        call for call in runner.calls if call["args"][0] == "rm"
    ]
    testbase.assert_equal(len(rm_calls), 1)
    testbase.assert_equal(rm_calls[0]["args"], ["rm", path])
    testbase.assert_equal(rm_calls[0]["elevated"], True)

    restart_calls = [
        call for call in runner.calls
        if call["args"] == ["systemctl", "restart", "rsyslog"]
    ]
    testbase.assert_equal(len(restart_calls), 1)
    return


def test_remove_forwarder_skipped_when_absent():
    """
        remove_forwarder returns SKIPPED when neither the active
        nor the disabled drop-in is present.
    """
    runner = _MockCommandRunner()
    path = "/etc/rsyslog.d/automojo-fwd-ghost.conf"
    disabled_path = path + ".disabled"
    _seed_test_dash_e(runner, path=path, exists=False)
    _seed_test_dash_e(runner, path=disabled_path, exists=False)

    manager = UbuntuRemoteLogForwardingManager(
        runner=runner, config=UbuntuForwarderManagerConfig(),
    )
    result = manager.remove_forwarder(id="ghost")
    testbase.assert_equal(
        result.status, UbuntuForwarderActionStatus.SKIPPED,
    )
    rm_calls = [
        call for call in runner.calls if call["args"][0] == "rm"
    ]
    testbase.assert_equal(len(rm_calls), 0)
    return


# ----------------------------------------------------------------------
# list_forwarders
# ----------------------------------------------------------------------


def test_list_forwarders_parses_ls_output():
    """
        list_forwarders filters the ``ls`` output to managed
        prefixes, classifies enabled vs disabled drop-ins, and
        parses host / port / protocol from the body.
    """
    runner = _MockCommandRunner()
    rsyslog_d = "/etc/rsyslog.d"
    runner.results[("ls", "-1", rsyslog_d)] = RunResult(
        exit_code=0,
        stdout=(
            "automojo-fwd-collector1.conf\n"
            "automojo-fwd-collector2.conf.disabled\n"
            "50-default.conf\n"
        ),
        stderr="",
    )
    spec1 = UbuntuForwarderSpec(
        id="collector1", host="logs.example.com", port=6514,
        protocol="tcp", selector="*.*",
    )
    spec2 = UbuntuForwarderSpec(
        id="collector2", host="logs2.example.com", port=514,
        protocol="udp", selector="auth.*",
    )
    _seed_cat(
        runner,
        path="{}/automojo-fwd-collector1.conf".format(rsyslog_d),
        body=_render_drop_in(spec=spec1),
    )
    _seed_cat(
        runner,
        path=(
            "{}/automojo-fwd-collector2.conf.disabled".format(
                rsyslog_d,
            )
        ),
        body=_render_drop_in(spec=spec2),
    )

    manager = UbuntuRemoteLogForwardingManager(
        runner=runner, config=UbuntuForwarderManagerConfig(),
    )
    forwarders = manager.list_forwarders()
    testbase.assert_equal(len(forwarders), 2)

    by_id = {item.id: item for item in forwarders}
    testbase.assert_equal(
        isinstance(by_id["collector1"], UbuntuForwarderInfo), True,
    )
    testbase.assert_equal(by_id["collector1"].enabled, True)
    testbase.assert_equal(
        by_id["collector1"].host, "logs.example.com",
    )
    testbase.assert_equal(by_id["collector1"].port, 6514)
    testbase.assert_equal(by_id["collector1"].protocol, "tcp")
    testbase.assert_equal(by_id["collector1"].selector_or_query, "*.*")

    testbase.assert_equal(by_id["collector2"].enabled, False)
    testbase.assert_equal(by_id["collector2"].protocol, "udp")
    testbase.assert_equal(by_id["collector2"].port, 514)
    testbase.assert_equal(
        by_id["collector2"].selector_or_query, "auth.*",
    )
    return


def test_list_forwarders_raises_on_unexpected_failure():
    """
        list_forwarders raises UbuntuForwarderQueryError when the
        ``ls`` runner reports a non-zero exit with a non-empty
        stderr.
    """
    runner = _MockCommandRunner()
    rsyslog_d = "/etc/rsyslog.d"
    runner.results[("ls", "-1", rsyslog_d)] = RunResult(
        exit_code=2, stdout="", stderr="Permission denied",
    )

    manager = UbuntuRemoteLogForwardingManager(
        runner=runner, config=UbuntuForwarderManagerConfig(),
    )
    raised = False
    try:
        manager.list_forwarders()
    except UbuntuForwarderQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# enable_forwarder / disable_forwarder
# ----------------------------------------------------------------------


def test_enable_forwarder_invokes_mv():
    """
        enable_forwarder renames ``<path>.disabled`` ->
        ``<path>`` and follows up with
        ``systemctl restart rsyslog``.
    """
    runner = _MockCommandRunner()
    active = "/etc/rsyslog.d/automojo-fwd-collector1.conf"
    disabled = active + ".disabled"
    _seed_test_dash_e(runner, path=active, exists=False)
    _seed_test_dash_e(runner, path=disabled, exists=True)
    runner.results[("mv", disabled, active)] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = UbuntuRemoteLogForwardingManager(
        runner=runner, config=UbuntuForwarderManagerConfig(),
    )
    result = manager.enable_forwarder(id="collector1")
    testbase.assert_equal(
        result.status, UbuntuForwarderActionStatus.SUCCESS,
    )

    mv_calls = [
        call for call in runner.calls if call["args"][0] == "mv"
    ]
    testbase.assert_equal(len(mv_calls), 1)
    testbase.assert_equal(
        mv_calls[0]["args"], ["mv", disabled, active],
    )
    return


def test_enable_forwarder_skipped_when_enabled():
    """
        enable_forwarder returns SKIPPED when the ``.conf``
        drop-in is already on disk and the ``.disabled`` shadow
        is absent.
    """
    runner = _MockCommandRunner()
    active = "/etc/rsyslog.d/automojo-fwd-collector1.conf"
    disabled = active + ".disabled"
    _seed_test_dash_e(runner, path=active, exists=True)
    _seed_test_dash_e(runner, path=disabled, exists=False)

    manager = UbuntuRemoteLogForwardingManager(
        runner=runner, config=UbuntuForwarderManagerConfig(),
    )
    result = manager.enable_forwarder(id="collector1")
    testbase.assert_equal(
        result.status, UbuntuForwarderActionStatus.SKIPPED,
    )
    mv_calls = [
        call for call in runner.calls if call["args"][0] == "mv"
    ]
    testbase.assert_equal(len(mv_calls), 0)
    return


def test_disable_forwarder_invokes_mv():
    """
        disable_forwarder renames ``<path>`` ->
        ``<path>.disabled`` and follows up with
        ``systemctl restart rsyslog``.
    """
    runner = _MockCommandRunner()
    active = "/etc/rsyslog.d/automojo-fwd-collector1.conf"
    disabled = active + ".disabled"
    _seed_test_dash_e(runner, path=active, exists=True)
    _seed_test_dash_e(runner, path=disabled, exists=False)
    runner.results[("mv", active, disabled)] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = UbuntuRemoteLogForwardingManager(
        runner=runner, config=UbuntuForwarderManagerConfig(),
    )
    result = manager.disable_forwarder(id="collector1")
    testbase.assert_equal(
        result.status, UbuntuForwarderActionStatus.SUCCESS,
    )
    mv_calls = [
        call for call in runner.calls if call["args"][0] == "mv"
    ]
    testbase.assert_equal(len(mv_calls), 1)
    testbase.assert_equal(
        mv_calls[0]["args"], ["mv", active, disabled],
    )
    return


# ----------------------------------------------------------------------
# test_forwarder
# ----------------------------------------------------------------------


def test_test_forwarder_invokes_logger():
    """
        test_forwarder dispatches ``logger -t automojo-test
        <message>`` without elevation and never raises on
        runner failure.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuRemoteLogForwardingManager(
        runner=runner,
        config=UbuntuForwarderManagerConfig(elevated=True),
    )
    result = manager.test_forwarder(id="collector1")
    testbase.assert_equal(
        isinstance(result, UbuntuForwarderTestResult), True,
    )
    testbase.assert_equal(
        result.status, UbuntuForwarderActionStatus.SUCCESS,
    )
    testbase.assert_equal(result.forwarder_id, "collector1")
    testbase.assert_equal(
        result.test_message_id is not None, True,
    )
    testbase.assert_equal(
        result.test_message_id.startswith("forwarder-test-"), True,
    )
    logger_calls = [
        call for call in runner.calls
        if call["args"][0] == "logger"
    ]
    testbase.assert_equal(len(logger_calls), 1)
    testbase.assert_equal(logger_calls[0]["args"][0], "logger")
    testbase.assert_equal(logger_calls[0]["args"][1], "-t")
    testbase.assert_equal(logger_calls[0]["args"][2], "automojo-test")
    testbase.assert_equal(logger_calls[0]["elevated"], False)
    return


# ----------------------------------------------------------------------
# get_forwarder_status
# ----------------------------------------------------------------------


def test_get_forwarder_status_parses_output():
    """
        get_forwarder_status reports ``enabled=True`` when only
        the active drop-in is present, ``enabled=False`` when
        only the ``.disabled`` shadow is present, and
        ``enabled=False`` when neither is present.
    """
    active = "/etc/rsyslog.d/automojo-fwd-collector1.conf"
    disabled = active + ".disabled"

    # Case 1: active present.
    runner_active = _MockCommandRunner()
    _seed_test_dash_e(runner_active, path=active, exists=True)
    _seed_test_dash_e(runner_active, path=disabled, exists=False)
    manager_active = UbuntuRemoteLogForwardingManager(
        runner=runner_active,
        config=UbuntuForwarderManagerConfig(),
    )
    status_active = manager_active.get_forwarder_status(
        id="collector1",
    )
    testbase.assert_equal(
        isinstance(status_active, UbuntuForwarderStatus), True,
    )
    testbase.assert_equal(status_active.enabled, True)

    # Case 2: only disabled present.
    runner_dis = _MockCommandRunner()
    _seed_test_dash_e(runner_dis, path=active, exists=False)
    _seed_test_dash_e(runner_dis, path=disabled, exists=True)
    manager_dis = UbuntuRemoteLogForwardingManager(
        runner=runner_dis,
        config=UbuntuForwarderManagerConfig(),
    )
    status_dis = manager_dis.get_forwarder_status(id="collector1")
    testbase.assert_equal(status_dis.enabled, False)

    # Case 3: nothing present.
    runner_absent = _MockCommandRunner()
    _seed_test_dash_e(runner_absent, path=active, exists=False)
    _seed_test_dash_e(runner_absent, path=disabled, exists=False)
    manager_absent = UbuntuRemoteLogForwardingManager(
        runner=runner_absent,
        config=UbuntuForwarderManagerConfig(),
    )
    status_absent = manager_absent.get_forwarder_status(
        id="collector1",
    )
    testbase.assert_equal(status_absent.enabled, False)
    return


# ----------------------------------------------------------------------
# Elevation, error surfacing, parser
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        With ``elevated=True``, every write dispatch sets
        ``elevated=True`` on the runner call.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    config = UbuntuForwarderManagerConfig(elevated=True)
    manager = UbuntuRemoteLogForwardingManager(
        runner=runner, config=config,
    )
    spec = UbuntuForwarderSpec(
        id="collector1", host="example.com", port=514,
    )
    manager.add_forwarder(spec=spec)
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Reads (``ls``, ``cat``, ``test -e``, ``logger``) never
        elevate.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    config = UbuntuForwarderManagerConfig(elevated=True)
    manager = UbuntuRemoteLogForwardingManager(
        runner=runner, config=config,
    )
    manager.list_forwarders()
    manager.get_forwarder_status(id="collector1")
    manager.test_forwarder(id="collector1")

    read_argv0 = ("ls", "cat", "test", "logger")
    for call in runner.calls:
        argv0 = call["args"][0]
        if argv0 in read_argv0:
            testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        When ``tee`` returns non-zero the manager surfaces the
        exit code and stderr on the result without raising and
        without invoking ``systemctl``.
    """
    runner = _MockCommandRunner()
    path = "/etc/rsyslog.d/automojo-fwd-collector1.conf"
    runner.results[("tee", path)] = RunResult(
        exit_code=2, stdout="", stderr="permission denied",
    )

    manager = UbuntuRemoteLogForwardingManager(
        runner=runner, config=UbuntuForwarderManagerConfig(),
    )
    spec = UbuntuForwarderSpec(
        id="collector1", host="example.com", port=514,
    )
    result = manager.add_forwarder(spec=spec)
    testbase.assert_equal(
        result.status, UbuntuForwarderActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.stderr, "permission denied")
    restart_calls = [
        call for call in runner.calls
        if call["args"] == ["systemctl", "restart", "rsyslog"]
    ]
    testbase.assert_equal(len(restart_calls), 0)
    return


def test_reload_failure_surfaces_failed():
    """
        When the post-write ``systemctl restart`` returns
        non-zero, the mutating verb returns FAILED with the
        restart's exit code.
    """
    runner = _MockCommandRunner()
    path = "/etc/rsyslog.d/automojo-fwd-collector1.conf"
    runner.results[("tee", path)] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[
        ("systemctl", "restart", "rsyslog")
    ] = RunResult(
        exit_code=4, stdout="",
        stderr="Failed to restart rsyslog.service",
    )

    manager = UbuntuRemoteLogForwardingManager(
        runner=runner, config=UbuntuForwarderManagerConfig(),
    )
    spec = UbuntuForwarderSpec(
        id="collector1", host="example.com", port=514,
    )
    result = manager.add_forwarder(spec=spec)
    testbase.assert_equal(
        result.status, UbuntuForwarderActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 4)
    testbase.assert_equal(
        "rsyslog restart failed" in (result.message or ""), True,
    )
    return


def test_parse_drop_in_round_trip():
    """
        The parser inverts the renderer for plain TCP, UDP, and
        TLS specs.
    """
    cases = [
        UbuntuForwarderSpec(
            id="a", host="h1", port=514,
            protocol="tcp", selector="*.*",
        ),
        UbuntuForwarderSpec(
            id="b", host="h2", port=601,
            protocol="udp", selector="auth.*",
        ),
        UbuntuForwarderSpec(
            id="c", host="h3", port=6514,
            protocol="tls", selector="*.*",
            tls_ca_cert="/etc/pki/ca.pem",
        ),
    ]
    for spec in cases:
        body = _render_drop_in(spec=spec)
        host, port, protocol, selector = _parse_drop_in(body=body)
        testbase.assert_equal(host, spec.host)
        testbase.assert_equal(port, spec.port)
        testbase.assert_equal(protocol, spec.protocol)
        testbase.assert_equal(selector, spec.selector)
    return
