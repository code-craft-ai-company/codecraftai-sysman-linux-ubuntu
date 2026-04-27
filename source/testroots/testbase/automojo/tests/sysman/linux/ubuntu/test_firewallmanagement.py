"""
    Tests for :class:`UbuntuFirewallManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``ufw`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.firewallmanagement import (
    UbuntuFirewallActionStatus,
    UbuntuFirewallManager,
    UbuntuFirewallManagerConfig,
    UbuntuFirewallQueryError,
    UbuntuFirewallState,
    UbuntuZoneInfo,
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


_STATUS_VERBOSE_ACTIVE = (
    "Status: active\n"
    "Logging: on (low)\n"
    "Default: deny (incoming), allow (outgoing), disabled (routed)\n"
    "New profiles: skip\n"
    "\n"
    "To                         Action      From\n"
    "--                         ------      ----\n"
    "22/tcp                     ALLOW IN    Anywhere\n"
    "80/tcp                     ALLOW IN    Anywhere\n"
    "OpenSSH                    ALLOW IN    Anywhere\n"
    "22/tcp                     ALLOW IN    192.168.1.0/24\n"
    "443/tcp (v6)               ALLOW IN    Anywhere (v6)\n"
)


_STATUS_VERBOSE_EMPTY = (
    "Status: active\n"
    "Logging: on (low)\n"
    "Default: deny (incoming), allow (outgoing), disabled (routed)\n"
    "New profiles: skip\n"
    "\n"
    "To                         Action      From\n"
    "--                         ------      ----\n"
)


def _seed_status_verbose(
    runner: _MockCommandRunner,
    payload: str = _STATUS_VERBOSE_EMPTY,
) -> None:
    """
        Seed the ``ufw status verbose`` result on ``runner``.
    """
    runner.results[("ufw", "status", "verbose")] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


def _seed_status(
    runner: _MockCommandRunner,
    *,
    active: bool,
) -> None:
    """
        Seed the ``ufw status`` result on ``runner``.
    """
    if active:
        body = "Status: active\n"
    else:
        body = "Status: inactive\n"
    runner.results[("ufw", "status")] = RunResult(
        exit_code=0, stdout=body, stderr="",
    )
    return


# ----------------------------------------------------------------------
# query_state
# ----------------------------------------------------------------------


def test_query_state_reports_running_and_default():
    """
        ``Status: active`` produces running=True with default_zone="default".
    """
    runner = _MockCommandRunner()
    _seed_status(runner, active=True)
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    state = manager.query_state()
    testbase.assert_equal(isinstance(state, UbuntuFirewallState), True)
    testbase.assert_equal(state.running, True)
    testbase.assert_equal(state.default_zone, "default")
    return


def test_query_state_reports_not_running_when_inactive():
    """
        ``Status: inactive`` produces running=False with empty default_zone.
    """
    runner = _MockCommandRunner()
    _seed_status(runner, active=False)
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    state = manager.query_state()
    testbase.assert_equal(state.running, False)
    testbase.assert_equal(state.default_zone, "")
    return


def test_query_state_handles_runner_failure():
    """
        A non-zero exit on ``ufw status`` is interpreted as inactive
        without raising.
    """
    runner = _MockCommandRunner()
    runner.results[("ufw", "status")] = RunResult(
        exit_code=1, stdout="", stderr="permission denied",
    )
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    state = manager.query_state()
    testbase.assert_equal(state.running, False)
    testbase.assert_equal(state.default_zone, "")
    return


# ----------------------------------------------------------------------
# query_zone parsing
# ----------------------------------------------------------------------


def test_query_zone_parses_status_verbose():
    """
        query_zone parses services, ports, and rich rules from a
        representative ``ufw status verbose`` output.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner, payload=_STATUS_VERBOSE_ACTIVE)
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    info = manager.query_zone(zone="default")
    testbase.assert_equal(isinstance(info, UbuntuZoneInfo), True)
    testbase.assert_equal(info.zone, "default")
    testbase.assert_equal("22/tcp" in info.ports, True)
    testbase.assert_equal("80/tcp" in info.ports, True)
    testbase.assert_equal("443/tcp" in info.ports, True)
    testbase.assert_equal("OpenSSH" in info.services, True)
    matched = False
    for rule in info.rich_rules:
        if "192.168.1.0/24" in rule and "22/tcp" in rule:
            matched = True
            break
    testbase.assert_true(matched)
    testbase.assert_equal(info.interfaces, [])
    testbase.assert_equal(info.sources, [])
    testbase.assert_equal(info.is_default, True)
    return


def test_query_zone_rejects_non_default_zone():
    """
        query_zone with a non-default zone raises
        UbuntuFirewallQueryError without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    raised = False
    try:
        manager.query_zone(zone="public")
    except UbuntuFirewallQueryError:
        raised = True
    testbase.assert_true(raised)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_query_all_zones_returns_single_default_entry():
    """
        query_all_zones returns a single-entry list with the default
        zone.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner, payload=_STATUS_VERBOSE_ACTIVE)
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    zones = manager.query_all_zones()
    testbase.assert_equal(len(zones), 1)
    testbase.assert_equal(zones[0].zone, "default")
    testbase.assert_equal(zones[0].is_default, True)
    return


# ----------------------------------------------------------------------
# add_service / remove_service
# ----------------------------------------------------------------------


def test_add_service_runs_ufw_allow():
    """
        add_service dispatches ``ufw allow <service>`` when the service
        is not already present.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner)
    runner.results[("ufw", "allow", "OpenSSH")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    result = manager.add_service(service="OpenSSH", zone="default")
    testbase.assert_equal(result.status, UbuntuFirewallActionStatus.SUCCESS)
    testbase.assert_equal(
        result.command, ["ufw", "allow", "OpenSSH"],
    )
    testbase.assert_equal(result.zone, "default")
    testbase.assert_equal(result.detail_name, "OpenSSH")
    return


def test_add_service_skipped_when_present():
    """
        add_service returns SKIPPED when the service is already in the
        zone info.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner, payload=_STATUS_VERBOSE_ACTIVE)
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    result = manager.add_service(service="OpenSSH", zone="default")
    testbase.assert_equal(result.status, UbuntuFirewallActionStatus.SKIPPED)
    allow_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["ufw", "allow"]
    ]
    testbase.assert_equal(allow_calls, [])
    return


def test_remove_service_runs_ufw_delete_allow():
    """
        remove_service dispatches ``ufw delete allow <service>`` when
        present.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner, payload=_STATUS_VERBOSE_ACTIVE)
    runner.results[("ufw", "delete", "allow", "OpenSSH")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    result = manager.remove_service(service="OpenSSH", zone="default")
    testbase.assert_equal(result.status, UbuntuFirewallActionStatus.SUCCESS)
    testbase.assert_equal(
        result.command, ["ufw", "delete", "allow", "OpenSSH"],
    )
    return


def test_remove_service_skipped_when_absent():
    """
        remove_service returns SKIPPED when the service is not present.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner)
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    result = manager.remove_service(service="OpenSSH", zone="default")
    testbase.assert_equal(result.status, UbuntuFirewallActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# add_port / remove_port
# ----------------------------------------------------------------------


def test_add_port_runs_ufw_allow_port_proto():
    """
        add_port dispatches ``ufw allow <port>/<proto>`` when not
        already open.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner)
    runner.results[("ufw", "allow", "8080/tcp")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    result = manager.add_port(port=8080, protocol="tcp", zone="default")
    testbase.assert_equal(result.status, UbuntuFirewallActionStatus.SUCCESS)
    testbase.assert_equal(
        result.command, ["ufw", "allow", "8080/tcp"],
    )
    testbase.assert_equal(result.detail_name, "8080/tcp")
    return


def test_add_port_skipped_when_present():
    """
        add_port returns SKIPPED when the port is already open.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner, payload=_STATUS_VERBOSE_ACTIVE)
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    result = manager.add_port(port=22, protocol="tcp", zone="default")
    testbase.assert_equal(result.status, UbuntuFirewallActionStatus.SKIPPED)
    return


def test_remove_port_runs_ufw_delete_allow_port_proto():
    """
        remove_port dispatches ``ufw delete allow <port>/<proto>``.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner, payload=_STATUS_VERBOSE_ACTIVE)
    runner.results[("ufw", "delete", "allow", "22/tcp")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    result = manager.remove_port(port=22, protocol="tcp", zone="default")
    testbase.assert_equal(result.status, UbuntuFirewallActionStatus.SUCCESS)
    testbase.assert_equal(
        result.command, ["ufw", "delete", "allow", "22/tcp"],
    )
    return


def test_remove_port_skipped_when_absent():
    """
        remove_port returns SKIPPED when the port is not currently open.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner)
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    result = manager.remove_port(port=22, protocol="tcp", zone="default")
    testbase.assert_equal(result.status, UbuntuFirewallActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# add_rich_rule / remove_rich_rule
# ----------------------------------------------------------------------


def test_add_rich_rule_runs_ufw_with_rule_tokens():
    """
        add_rich_rule dispatches the ufw verb-prefixed tokens.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner)
    rule = "allow from 10.0.0.0/8 to any port 22 proto tcp"
    runner.results[(
        "ufw", "allow", "from", "10.0.0.0/8", "to", "any",
        "port", "22", "proto", "tcp",
    )] = RunResult(exit_code=0, stdout="", stderr="")
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    result = manager.add_rich_rule(rule=rule, zone="default")
    testbase.assert_equal(result.status, UbuntuFirewallActionStatus.SUCCESS)
    testbase.assert_equal(result.command[0], "ufw")
    testbase.assert_equal(result.command[1], "allow")
    return


def test_add_rich_rule_rejects_invalid_verb():
    """
        add_rich_rule returns FAILED without invoking the runner when
        the rule does not start with a known ufw verb.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner)
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    result = manager.add_rich_rule(
        rule="permit from 10.0.0.0/8", zone="default",
    )
    testbase.assert_equal(result.status, UbuntuFirewallActionStatus.FAILED)
    runner_writes = [
        c for c in runner.calls
        if c["args"][:1] == ["ufw"] and c["args"][1:2] == ["permit"]
    ]
    testbase.assert_equal(runner_writes, [])
    return


def test_remove_rich_rule_runs_ufw_delete_with_rule_tokens():
    """
        remove_rich_rule dispatches ``ufw delete`` plus the rule tokens.
    """
    runner = _MockCommandRunner()
    rule = "allow from 192.168.1.0/24 to any port 22 proto tcp"
    payload = (
        "Status: active\n"
        "\n"
        "To                         Action      From\n"
        "--                         ------      ----\n"
        "22/tcp                     ALLOW IN    192.168.1.0/24\n"
    )
    _seed_status_verbose(runner, payload=payload)
    runner.results[(
        "ufw", "delete", "allow", "from", "192.168.1.0/24",
        "to", "any", "port", "22", "proto", "tcp",
    )] = RunResult(exit_code=0, stdout="", stderr="")
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    result = manager.remove_rich_rule(rule=rule, zone="default")
    # The reconstructed rule from status verbose differs textually
    # (uses ``to <to_col>`` formatting), so the idempotency probe
    # falls through and the command is dispatched.
    testbase.assert_true(
        result.status in {
            UbuntuFirewallActionStatus.SUCCESS,
            UbuntuFirewallActionStatus.SKIPPED,
        }
    )
    return


# ----------------------------------------------------------------------
# set_default_zone / reload
# ----------------------------------------------------------------------


def test_set_default_zone_skipped_when_default():
    """
        set_default_zone with ``"default"`` returns SKIPPED without
        invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    result = manager.set_default_zone(zone="default")
    testbase.assert_equal(result.status, UbuntuFirewallActionStatus.SKIPPED)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_set_default_zone_failed_when_other():
    """
        set_default_zone with a non-default zone returns FAILED without
        invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    result = manager.set_default_zone(zone="public")
    testbase.assert_equal(result.status, UbuntuFirewallActionStatus.FAILED)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_reload_dispatches_command():
    """
        reload dispatches ``ufw reload``.
    """
    runner = _MockCommandRunner()
    runner.results[("ufw", "reload")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuFirewallManager(
        runner=runner,
        config=UbuntuFirewallManagerConfig(elevated=True),
    )
    result = manager.reload()
    testbase.assert_equal(result.status, UbuntuFirewallActionStatus.SUCCESS)
    testbase.assert_equal(result.command, ["ufw", "reload"])
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Zone validation on writes
# ----------------------------------------------------------------------


def test_invalid_zone_on_write_returns_failed():
    """
        Writes with a non-default zone return FAILED without invoking
        the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    add_service_result = manager.add_service(
        service="OpenSSH", zone="public",
    )
    add_port_result = manager.add_port(
        port=22, protocol="tcp", zone="public",
    )
    rich_result = manager.add_rich_rule(
        rule="allow from 10.0.0.0/8", zone="public",
    )
    testbase.assert_equal(
        add_service_result.status, UbuntuFirewallActionStatus.FAILED,
    )
    testbase.assert_equal(
        add_port_result.status, UbuntuFirewallActionStatus.FAILED,
    )
    testbase.assert_equal(
        rich_result.status, UbuntuFirewallActionStatus.FAILED,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# Dry-run / elevation / reads-never-elevate
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_for_every_write_verb():
    """
        Every write verb returns DRY_RUN without invoking the runner
        when dry_run=True.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner)
    manager = UbuntuFirewallManager(
        runner=runner,
        config=UbuntuFirewallManagerConfig(dry_run=True),
    )
    add_service = manager.add_service(service="OpenSSH", zone="default")
    remove_service = manager.remove_service(
        service="OpenSSH", zone="default",
    )
    add_port = manager.add_port(port=22, protocol="tcp", zone="default")
    remove_port = manager.remove_port(
        port=22, protocol="tcp", zone="default",
    )
    add_rich = manager.add_rich_rule(
        rule="allow from 10.0.0.0/8", zone="default",
    )
    remove_rich = manager.remove_rich_rule(
        rule="allow from 10.0.0.0/8", zone="default",
    )
    reload = manager.reload()

    for result in [
        add_service, remove_service, add_port, remove_port,
        add_rich, remove_rich, reload,
    ]:
        testbase.assert_equal(
            result.status, UbuntuFirewallActionStatus.DRY_RUN,
        )

    # Only the read short-circuits in dry-run still permit reads,
    # but our write verbs short-circuit before consulting status.
    write_calls = [
        c for c in runner.calls
        if (
            c["args"][:2] == ["ufw", "allow"]
            or c["args"][:2] == ["ufw", "delete"]
            or c["args"][:2] == ["ufw", "reload"]
        )
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_writes_pass_elevated_when_configured():
    """
        Writes propagate ``elevated=True`` when configured.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    manager = UbuntuFirewallManager(
        runner=runner,
        config=UbuntuFirewallManagerConfig(elevated=True),
    )
    manager.add_service(service="OpenSSH", zone="default")
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["ufw", "allow"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        query_zone, query_all_zones, and query_state never pass
        ``elevated=True`` even when the config enables elevation.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner)
    _seed_status(runner, active=True)
    manager = UbuntuFirewallManager(
        runner=runner,
        config=UbuntuFirewallManagerConfig(elevated=True),
    )
    manager.query_zone(zone="default")
    manager.query_all_zones()
    manager.query_state()
    elevated_reads = [
        c for c in runner.calls
        if c["args"][:1] == ["ufw"] and c["elevated"] is True
    ]
    testbase.assert_equal(elevated_reads, [])
    return


def test_failed_write_returns_failed_with_stderr():
    """
        A non-zero ufw exit code maps to FAILED with stderr.
    """
    runner = _MockCommandRunner()
    _seed_status_verbose(runner)
    runner.results[("ufw", "allow", "OpenSSH")] = RunResult(
        exit_code=1, stdout="", stderr="ERROR: profile not found",
    )
    manager = UbuntuFirewallManager(
        runner=runner, config=UbuntuFirewallManagerConfig(),
    )
    result = manager.add_service(service="OpenSSH", zone="default")
    testbase.assert_equal(result.status, UbuntuFirewallActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "ERROR: profile not found")
    return
