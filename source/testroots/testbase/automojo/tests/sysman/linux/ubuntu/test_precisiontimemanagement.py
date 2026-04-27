"""
    Tests for :class:`UbuntuPrecisionTimeManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``chronyc`` / ``timedatectl`` / ``systemctl`` is
    invoked.  File I/O is exercised entirely through the runner via
    ``cat`` / ``tee``.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.precisiontimemanagement import (
    UbuntuPrecisionTimeActionStatus,
    UbuntuPrecisionTimeManager,
    UbuntuPrecisionTimeManagerConfig,
    UbuntuPrecisionTimeQueryError,
    UbuntuTimeBackend,
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


def _chronyc_tracking_payload() -> str:
    """
        Build a representative ``chronyc tracking`` payload.
    """
    rtnval = (
        "Reference ID    : C0A80001 (foo.ntp.example.com)\n"
        "Stratum         : 3\n"
        "Ref time (UTC)  : Fri Apr 24 10:30:01 2026\n"
        "System time     : 0.000045678 seconds slow of NTP time\n"
        "Last offset     : +0.000123456 seconds\n"
        "RMS offset      : 0.000234567 seconds\n"
        "Frequency       : 12.345 ppm fast\n"
        "Residual freq   : -0.001 ppm\n"
        "Skew            : 0.067 ppm\n"
        "Root delay      : 0.005678 seconds\n"
        "Root dispersion : 0.001234 seconds\n"
        "Update interval : 64.4 seconds\n"
        "Leap status     : Normal\n"
    )
    return rtnval


def _chronyc_sources_payload() -> str:
    """
        Build a representative ``chronyc sources`` payload.
    """
    rtnval = (
        "MS Name/IP address         Stratum Poll Reach LastRx Last sample"
        "                          \n"
        "==============================================================="
        "===============================\n"
        "^* foo.ntp.example.com           2   6   377    32   "
        "+12us[+18us] +/- 9ms\n"
        "^- bar.ntp.example.com           3   6   377    33   "
        "-42us[-37us] +/- 22ms\n"
        "^? baz.ntp.example.com           0   6     0     -   "
        "+0ns[   +0ns] +/- 0ns\n"
    )
    return rtnval


def _timedatectl_status_payload() -> str:
    """
        Build a representative ``timedatectl status`` payload.
    """
    rtnval = (
        "               Local time: Fri 2026-04-24 10:30:01 UTC\n"
        "           Universal time: Fri 2026-04-24 10:30:01 UTC\n"
        "                 RTC time: Fri 2026-04-24 10:30:01\n"
        "                Time zone: Etc/UTC (UTC, +0000)\n"
        "System clock synchronized: yes\n"
        "              NTP service: active\n"
        "          RTC in local TZ: no\n"
    )
    return rtnval


def _timedatectl_show_sources_payload() -> str:
    """
        Build a representative ``timedatectl show --property=...``
        payload.
    """
    rtnval = (
        "NTP=yes\n"
        "NTPSynchronized=yes\n"
        "SystemNTPServers=ntp1.example.com ntp2.example.com\n"
        "FallbackNTPServers=fallback.example.com\n"
        "ServerName=ntp1.example.com\n"
        "ServerAddress=192.0.2.10\n"
    )
    return rtnval


def _timedatectl_show_drift_payload() -> str:
    """
        Build a representative ``timedatectl show --property=NTPMessage``
        payload.
    """
    rtnval = (
        "NTPMessage={ Leap=0, Version=4, Mode=4, Stratum=2, "
        "OriginateTimestamp=..., ReceiveTimestamp=..., "
        "Offset=12345us, Delay=2ms, Jitter=1ms }\n"
        "ServerName=ntp1.example.com\n"
    )
    return rtnval


def _chrony_conf_payload(*, servers: list[str]) -> str:
    """
        Build a synthetic ``chrony.conf`` body with the given servers.
    """
    lines = ["# chrony.conf — vendored test"]
    lines.append("driftfile /var/lib/chrony/drift")
    lines.append("makestep 1.0 3")
    lines.append("rtcsync")
    for server in servers:
        lines.append("pool {} iburst".format(server))
    body = "\n".join(lines) + "\n"
    return body


def _timesyncd_conf_payload(*, servers: list[str]) -> str:
    """
        Build a synthetic ``timesyncd.conf`` body with the given
        servers.
    """
    lines = ["# timesyncd.conf — vendored test"]
    lines.append("[Time]")
    lines.append("NTP={}".format(" ".join(servers)))
    body = "\n".join(lines) + "\n"
    return body


# ----------------------------------------------------------------------
# Reads (chrony backend)
# ----------------------------------------------------------------------


def test_get_time_status_parses_chronyc_output():
    """
        get_time_status under the chrony backend parses
        ``chronyc tracking``.
    """
    runner = _MockCommandRunner()
    runner.results[("chronyc", "tracking")] = RunResult(
        exit_code=0, stdout=_chronyc_tracking_payload(), stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(
            backend=UbuntuTimeBackend.CHRONY,
        ),
    )
    status = manager.get_time_status()

    testbase.assert_equal(status.enabled, True)
    testbase.assert_equal(status.synchronized, True)
    testbase.assert_equal(status.current_server, "foo.ntp.example.com")
    testbase.assert_equal(status.stratum, 3)
    testbase.assert_equal(
        abs((status.offset_seconds or 0.0) - 0.000123456) < 1e-9, True,
    )
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_get_time_status_returns_inactive_when_chronyd_down():
    """
        get_time_status returns enabled=False when ``chronyc`` reports
        the ``506 Cannot talk to daemon`` signature.
    """
    runner = _MockCommandRunner()
    runner.results[("chronyc", "tracking")] = RunResult(
        exit_code=1, stdout="",
        stderr="506 Cannot talk to daemon\n",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(),
    )
    status = manager.get_time_status()

    testbase.assert_equal(status.enabled, False)
    testbase.assert_equal(status.synchronized, False)
    testbase.assert_equal(status.current_server, None)
    return


def test_get_time_sources_parses_chronyc_output():
    """
        get_time_sources under chrony parses ``chronyc sources``.
    """
    runner = _MockCommandRunner()
    runner.results[("chronyc", "sources")] = RunResult(
        exit_code=0, stdout=_chronyc_sources_payload(), stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(),
    )
    sources = manager.get_time_sources()

    testbase.assert_equal(len(sources), 3)
    by_server = {item.server: item for item in sources}
    testbase.assert_equal(by_server["foo.ntp.example.com"].state, "selected")
    testbase.assert_equal(
        by_server["foo.ntp.example.com"].stratum, 2,
    )
    testbase.assert_equal(
        by_server["bar.ntp.example.com"].state, "candidate",
    )
    testbase.assert_equal(
        by_server["baz.ntp.example.com"].state, "unreachable",
    )
    return


def test_get_drift_parses_chronyc_output():
    """
        get_drift under chrony parses the offset / frequency / jitter
        fields out of ``chronyc tracking``.
    """
    runner = _MockCommandRunner()
    runner.results[("chronyc", "tracking")] = RunResult(
        exit_code=0, stdout=_chronyc_tracking_payload(), stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(),
    )
    drift = manager.get_drift()

    testbase.assert_equal(
        abs(drift.offset_seconds - 0.000123456) < 1e-9, True,
    )
    testbase.assert_equal(
        abs((drift.frequency_ppm or 0.0) - 12.345) < 1e-9, True,
    )
    testbase.assert_equal(
        abs((drift.jitter_seconds or 0.0) - 0.000234567) < 1e-9, True,
    )
    testbase.assert_equal(drift.stratum, 3)
    return


# ----------------------------------------------------------------------
# Reads (timesyncd backend)
# ----------------------------------------------------------------------


def test_get_time_status_parses_timedatectl_output():
    """
        get_time_status under the timesyncd backend parses
        ``timedatectl status``.
    """
    runner = _MockCommandRunner()
    runner.results[("timedatectl", "status")] = RunResult(
        exit_code=0, stdout=_timedatectl_status_payload(), stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(
            backend=UbuntuTimeBackend.TIMESYNCD,
        ),
    )
    status = manager.get_time_status()

    testbase.assert_equal(status.enabled, True)
    testbase.assert_equal(status.synchronized, True)
    return


def test_get_time_sources_parses_timedatectl_output():
    """
        get_time_sources under timesyncd parses the ``timedatectl show``
        property block.
    """
    runner = _MockCommandRunner()
    show_argv = (
        "timedatectl", "show",
        "--property=NTP",
        "--property=NTPSynchronized",
        "--property=SystemNTPServers",
        "--property=FallbackNTPServers",
        "--property=ServerName",
        "--property=ServerAddress",
    )
    runner.results[show_argv] = RunResult(
        exit_code=0,
        stdout=_timedatectl_show_sources_payload(),
        stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(
            backend=UbuntuTimeBackend.TIMESYNCD,
        ),
    )
    sources = manager.get_time_sources()

    by_server = {item.server: item for item in sources}
    testbase.assert_equal(by_server["ntp1.example.com"].state, "selected")
    testbase.assert_equal(by_server["ntp2.example.com"].state, "candidate")
    testbase.assert_equal(
        by_server["fallback.example.com"].state, "candidate",
    )
    return


def test_get_drift_parses_timedatectl_output():
    """
        get_drift under timesyncd parses the NTPMessage blob.
    """
    runner = _MockCommandRunner()
    show_argv = (
        "timedatectl", "show",
        "--property=NTPMessage",
        "--property=ServerName",
    )
    runner.results[show_argv] = RunResult(
        exit_code=0, stdout=_timedatectl_show_drift_payload(), stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(
            backend=UbuntuTimeBackend.TIMESYNCD,
        ),
    )
    drift = manager.get_drift()

    testbase.assert_equal(
        abs(drift.offset_seconds - 12345e-6) < 1e-9, True,
    )
    testbase.assert_equal(
        abs((drift.jitter_seconds or 0.0) - 1e-3) < 1e-9, True,
    )
    return


# ----------------------------------------------------------------------
# add_time_source
# ----------------------------------------------------------------------


def test_add_time_source_invokes_chronyc():
    """
        add_time_source rewrites chrony.conf and restarts chrony.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("cat", "/etc/chrony/chrony.conf")] = RunResult(
        exit_code=0,
        stdout=_chrony_conf_payload(servers=["pool.ntp.org"]),
        stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(
            backend=UbuntuTimeBackend.CHRONY, elevated=True,
        ),
    )
    result = manager.add_time_source(server="time.example.com")

    testbase.assert_equal(result.status, UbuntuPrecisionTimeActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"], ["tee", "/etc/chrony/chrony.conf"],
    )
    testbase.assert_equal("pool time.example.com iburst" in tee_calls[0]["stdin"], True)
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    restart_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemctl", "restart"]
    ]
    testbase.assert_equal(len(restart_calls), 1)
    testbase.assert_equal(restart_calls[0]["args"], ["systemctl", "restart", "chrony"])
    return


def test_add_time_source_skipped_when_present():
    """
        add_time_source returns SKIPPED when the server is already
        configured.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/chrony/chrony.conf")] = RunResult(
        exit_code=0,
        stdout=_chrony_conf_payload(servers=["time.example.com"]),
        stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(),
    )
    result = manager.add_time_source(server="time.example.com")

    testbase.assert_equal(result.status, UbuntuPrecisionTimeActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


def test_add_time_source_dry_run():
    """
        add_time_source returns DRY_RUN and never invokes the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(dry_run=True),
    )
    result = manager.add_time_source(server="time.example.com")

    testbase.assert_equal(result.status, UbuntuPrecisionTimeActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command, ["tee", "/etc/chrony/chrony.conf"],
    )
    return


# ----------------------------------------------------------------------
# remove_time_source
# ----------------------------------------------------------------------


def test_remove_time_source_invokes_chronyc():
    """
        remove_time_source rewrites chrony.conf and restarts chrony.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("cat", "/etc/chrony/chrony.conf")] = RunResult(
        exit_code=0,
        stdout=_chrony_conf_payload(
            servers=["a.example.com", "b.example.com"],
        ),
        stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.remove_time_source(server="a.example.com")

    testbase.assert_equal(result.status, UbuntuPrecisionTimeActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    body = tee_calls[0]["stdin"]
    testbase.assert_equal("a.example.com" in body, False)
    testbase.assert_equal("b.example.com" in body, True)
    return


def test_remove_time_source_skipped_when_absent():
    """
        remove_time_source returns SKIPPED when the server is not
        present.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/chrony/chrony.conf")] = RunResult(
        exit_code=0,
        stdout=_chrony_conf_payload(servers=["b.example.com"]),
        stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(),
    )
    result = manager.remove_time_source(server="a.example.com")

    testbase.assert_equal(result.status, UbuntuPrecisionTimeActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


# ----------------------------------------------------------------------
# set_time_servers
# ----------------------------------------------------------------------


def test_set_time_servers_invokes_chronyc():
    """
        set_time_servers rewrites chrony.conf with the desired list and
        restarts chrony.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("cat", "/etc/chrony/chrony.conf")] = RunResult(
        exit_code=0,
        stdout=_chrony_conf_payload(servers=["a.example.com"]),
        stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.set_time_servers(
        servers=["b.example.com", "c.example.com"],
    )

    testbase.assert_equal(result.status, UbuntuPrecisionTimeActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    body = tee_calls[0]["stdin"]
    testbase.assert_equal("a.example.com" in body, False)
    testbase.assert_equal("pool b.example.com iburst" in body, True)
    testbase.assert_equal("pool c.example.com iburst" in body, True)
    return


def test_set_time_servers_skipped_when_match():
    """
        set_time_servers returns SKIPPED when the current sorted list
        equals the desired sorted list.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/chrony/chrony.conf")] = RunResult(
        exit_code=0,
        stdout=_chrony_conf_payload(
            servers=["a.example.com", "b.example.com"],
        ),
        stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(),
    )
    result = manager.set_time_servers(
        servers=["b.example.com", "a.example.com"],
    )

    testbase.assert_equal(result.status, UbuntuPrecisionTimeActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


# ----------------------------------------------------------------------
# force_sync_now / get_drift
# ----------------------------------------------------------------------


def test_force_sync_now_invokes_chronyc():
    """
        force_sync_now under the chrony backend dispatches
        ``chronyc makestep``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.force_sync_now()

    testbase.assert_equal(result.status, UbuntuPrecisionTimeActionStatus.SUCCESS)
    testbase.assert_equal(result.command, ["chronyc", "makestep"])
    testbase.assert_equal(runner.calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# enable_sync / disable_sync
# ----------------------------------------------------------------------


def test_enable_sync_skipped_when_already_enabled():
    """
        enable_sync returns SKIPPED when ``systemctl is-active`` already
        reports the service is running.
    """
    runner = _MockCommandRunner()
    runner.results[("systemctl", "is-active", "chrony")] = RunResult(
        exit_code=0, stdout="active\n", stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(),
    )
    result = manager.enable_sync()

    testbase.assert_equal(result.status, UbuntuPrecisionTimeActionStatus.SKIPPED)
    enable_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemctl", "enable"]
    ]
    testbase.assert_equal(enable_calls, [])
    return


def test_disable_sync_invokes_systemctl():
    """
        disable_sync dispatches ``systemctl disable --now chrony``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("systemctl", "is-active", "chrony")] = RunResult(
        exit_code=0, stdout="active\n", stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.disable_sync()

    testbase.assert_equal(result.status, UbuntuPrecisionTimeActionStatus.SUCCESS)
    disable_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["systemctl", "disable"]
    ]
    testbase.assert_equal(len(disable_calls), 1)
    testbase.assert_equal(
        disable_calls[0]["args"],
        ["systemctl", "disable", "--now", "chrony"],
    )
    testbase.assert_equal(disable_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Cross-cutting: elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Every write verb passes ``elevated=True`` when the config is
        configured to elevate.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("cat", "/etc/chrony/chrony.conf")] = RunResult(
        exit_code=0,
        stdout=_chrony_conf_payload(servers=["a.example.com"]),
        stderr="",
    )
    runner.results[("systemctl", "is-active", "chrony")] = RunResult(
        exit_code=3, stdout="inactive\n", stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(elevated=True),
    )
    manager.add_time_source(server="b.example.com")
    manager.force_sync_now()
    manager.enable_sync()

    # The is-active probe is a read; it must NOT elevate.
    for call in runner.calls:
        if call["args"][:2] == ["systemctl", "is-active"]:
            testbase.assert_equal(call["elevated"], False)
        if call["args"][0] == "tee":
            testbase.assert_equal(call["elevated"], True)
        if call["args"][:2] == ["systemctl", "restart"]:
            testbase.assert_equal(call["elevated"], True)
        if call["args"][:2] == ["systemctl", "enable"]:
            testbase.assert_equal(call["elevated"], True)
        if call["args"][0] == "chronyc" and call["args"][1] == "makestep":
            testbase.assert_equal(call["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read-only verbs never pass ``elevated=True``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("chronyc", "tracking")] = RunResult(
        exit_code=0, stdout=_chronyc_tracking_payload(), stderr="",
    )
    runner.results[("chronyc", "sources")] = RunResult(
        exit_code=0, stdout=_chronyc_sources_payload(), stderr="",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(elevated=True),
    )
    manager.get_time_status()
    manager.get_time_sources()
    manager.get_drift()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero exit on a write maps to FAILED with the captured
        stderr / exit_code / command.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("chronyc", "makestep")] = RunResult(
        exit_code=2, stdout="", stderr="503 No such command",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(elevated=True),
    )
    result = manager.force_sync_now()

    testbase.assert_equal(result.status, UbuntuPrecisionTimeActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.stderr, "503 No such command")
    testbase.assert_equal(result.command, ["chronyc", "makestep"])
    return


def test_get_time_status_raises_on_unexpected_chronyc_failure():
    """
        get_time_status raises :class:`UbuntuPrecisionTimeQueryError`
        when ``chronyc`` fails for a reason other than the daemon being
        down.
    """
    runner = _MockCommandRunner()
    runner.results[("chronyc", "tracking")] = RunResult(
        exit_code=1, stdout="",
        stderr="chronyc: command not found",
    )

    manager = UbuntuPrecisionTimeManager(
        runner=runner,
        config=UbuntuPrecisionTimeManagerConfig(),
    )
    raised = False
    try:
        manager.get_time_status()
    except UbuntuPrecisionTimeQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return
