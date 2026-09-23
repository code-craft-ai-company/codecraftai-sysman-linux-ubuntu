"""
    Tests for :class:`UbuntuNetworkConfigurationManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    File I/O is exercised entirely through the runner (``cat`` / ``tee``);
    no live filesystem is touched.  No live ``ip`` / ``netplan`` /
    ``resolvectl`` / ``hostnamectl`` is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.networkconfiguration import (
    UbuntuNetworkActionStatus,
    UbuntuNetworkConfigurationManager,
    UbuntuNetworkConfigurationManagerConfig,
    UbuntuNetworkQueryError,
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


def _ip_addr_payload() -> str:
    """
        Build a representative ``ip -j addr show`` JSON payload.
    """
    payload = [
        {
            "ifindex": 1,
            "ifname": "lo",
            "operstate": "UNKNOWN",
            "mtu": 65536,
            "address": "00:00:00:00:00:00",
            "addr_info": [
                {
                    "family": "inet",
                    "local": "127.0.0.1",
                    "prefixlen": 8,
                },
                {
                    "family": "inet6",
                    "local": "::1",
                    "prefixlen": 128,
                },
            ],
        },
        {
            "ifindex": 2,
            "ifname": "eth0",
            "operstate": "UP",
            "mtu": 1500,
            "address": "52:54:00:12:34:56",
            "addr_info": [
                {
                    "family": "inet",
                    "local": "192.168.1.42",
                    "prefixlen": 24,
                },
            ],
        },
    ]
    rtnval = json.dumps(payload)
    return rtnval


def _expected_netplan_eth0() -> str:
    """
        Build the canonical rendered netplan body for eth0/24/.
    """
    rtnval = (
        "network:\n"
        "  version: 2\n"
        "  ethernets:\n"
        "    eth0:\n"
        "      addresses: [192.168.1.42/24]\n"
        "      gateway4: 192.168.1.1\n"
    )
    return rtnval


# ----------------------------------------------------------------------
# get_interfaces
# ----------------------------------------------------------------------


def test_get_interfaces_parses_ip_addr_json():
    """
        get_interfaces parses ``ip -j addr show`` JSON into one
        :class:`UbuntuInterfaceInfo` per entry.
    """
    runner = _MockCommandRunner()
    runner.results[("ip", "-j", "addr", "show")] = RunResult(
        exit_code=0,
        stdout=_ip_addr_payload(),
        stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(),
    )
    interfaces = manager.get_interfaces()

    testbase.assert_equal(len(interfaces), 2)
    by_name = {info.name: info for info in interfaces}

    lo = by_name["lo"]
    testbase.assert_equal(lo.oper_state, "UNKNOWN")
    testbase.assert_equal(lo.ipv4_addresses, ["127.0.0.1/8"])
    testbase.assert_equal(lo.ipv6_addresses, ["::1/128"])

    eth0 = by_name["eth0"]
    testbase.assert_equal(eth0.oper_state, "UP")
    testbase.assert_equal(eth0.mac_address, "52:54:00:12:34:56")
    testbase.assert_equal(eth0.ipv4_addresses, ["192.168.1.42/24"])
    testbase.assert_equal(eth0.mtu, 1500)
    return


def test_get_interfaces_does_not_elevate():
    """
        get_interfaces never passes ``elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.results[("ip", "-j", "addr", "show")] = RunResult(
        exit_code=0, stdout="[]", stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(elevated=True),
    )
    manager.get_interfaces()

    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_get_interfaces_raises_on_failure():
    """
        get_interfaces raises :class:`UbuntuNetworkQueryError` when
        ``ip`` reports a non-zero exit code.
    """
    runner = _MockCommandRunner()
    runner.results[("ip", "-j", "addr", "show")] = RunResult(
        exit_code=1, stdout="", stderr="ip: not found",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(),
    )
    raised = False
    try:
        manager.get_interfaces()
    except UbuntuNetworkQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


# ----------------------------------------------------------------------
# configure_interface
# ----------------------------------------------------------------------


def test_configure_interface_writes_netplan_and_applies():
    """
        configure_interface writes the netplan drop-in and runs
        ``netplan apply``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("cat", "/etc/netplan/99-ccai.yaml")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(elevated=True),
    )
    result = manager.configure_interface(
        name="eth0",
        address="192.168.1.42",
        netmask="24",
        gateway="192.168.1.1",
    )

    testbase.assert_equal(result.status, UbuntuNetworkActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"], ["tee", "/etc/netplan/99-ccai.yaml"],
    )
    testbase.assert_equal(tee_calls[0]["stdin"], _expected_netplan_eth0())
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    apply_calls = [c for c in runner.calls if c["args"][0] == "netplan"]
    testbase.assert_equal(len(apply_calls), 1)
    testbase.assert_equal(apply_calls[0]["args"], ["netplan", "apply"])
    testbase.assert_equal(apply_calls[0]["elevated"], True)
    return


def test_configure_interface_skipped_when_yaml_matches():
    """
        configure_interface returns SKIPPED when the existing file
        already contains the rendered body.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/netplan/99-ccai.yaml")] = RunResult(
        exit_code=0,
        stdout=_expected_netplan_eth0(),
        stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(),
    )
    result = manager.configure_interface(
        name="eth0",
        address="192.168.1.42",
        netmask="255.255.255.0",
        gateway="192.168.1.1",
    )

    testbase.assert_equal(result.status, UbuntuNetworkActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    apply_calls = [c for c in runner.calls if c["args"][0] == "netplan"]
    testbase.assert_equal(apply_calls, [])
    return


def test_configure_interface_dry_run_short_circuits():
    """
        Dry-run returns DRY_RUN and never invokes the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(dry_run=True),
    )
    result = manager.configure_interface(
        name="eth0",
        address="192.168.1.42",
        netmask="24",
        gateway="192.168.1.1",
    )

    testbase.assert_equal(result.status, UbuntuNetworkActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command, ["tee", "/etc/netplan/99-ccai.yaml"],
    )
    return


def test_configure_interface_failed_when_apply_fails():
    """
        configure_interface returns FAILED with
        ``message="netplan_apply_failed"`` when ``netplan apply`` fails.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("cat", "/etc/netplan/99-ccai.yaml")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.results[("netplan", "apply")] = RunResult(
        exit_code=1, stdout="", stderr="netplan: invalid yaml",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(elevated=True),
    )
    result = manager.configure_interface(
        name="eth0",
        address="192.168.1.42",
        netmask="24",
        gateway=None,
    )

    testbase.assert_equal(result.status, UbuntuNetworkActionStatus.FAILED)
    testbase.assert_equal(result.message, "netplan_apply_failed")
    testbase.assert_equal(result.stderr, "netplan: invalid yaml")
    return


# ----------------------------------------------------------------------
# enable_interface / disable_interface
# ----------------------------------------------------------------------


def test_enable_interface_dispatches_ip_link_set_up():
    """
        enable_interface dispatches ``ip link set <iface> up``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("ip", "-j", "link", "show", "eth0")] = RunResult(
        exit_code=0,
        stdout=json.dumps([{"ifname": "eth0", "operstate": "DOWN"}]),
        stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(elevated=True),
    )
    result = manager.enable_interface(name="eth0")

    testbase.assert_equal(result.status, UbuntuNetworkActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls if c["args"][:3] == ["ip", "link", "set"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"], ["ip", "link", "set", "eth0", "up"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_enable_interface_skipped_when_already_up():
    """
        enable_interface returns SKIPPED when the interface is already
        up.
    """
    runner = _MockCommandRunner()
    runner.results[("ip", "-j", "link", "show", "eth0")] = RunResult(
        exit_code=0,
        stdout=json.dumps([{"ifname": "eth0", "operstate": "UP"}]),
        stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(),
    )
    result = manager.enable_interface(name="eth0")

    testbase.assert_equal(result.status, UbuntuNetworkActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls if c["args"][:3] == ["ip", "link", "set"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_disable_interface_skipped_when_already_down():
    """
        disable_interface returns SKIPPED when the interface is already
        down.
    """
    runner = _MockCommandRunner()
    runner.results[("ip", "-j", "link", "show", "eth0")] = RunResult(
        exit_code=0,
        stdout=json.dumps([{"ifname": "eth0", "operstate": "DOWN"}]),
        stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(),
    )
    result = manager.disable_interface(name="eth0")

    testbase.assert_equal(result.status, UbuntuNetworkActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls if c["args"][:3] == ["ip", "link", "set"]
    ]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# add_route / remove_route
# ----------------------------------------------------------------------


def test_add_route_dispatches_ip_route_add():
    """
        add_route dispatches ``ip route add <dest> via <gateway>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("ip", "route", "show", "10.0.0.0/24")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(elevated=True),
    )
    result = manager.add_route(
        dest="10.0.0.0/24", gateway="192.168.1.1",
    )

    testbase.assert_equal(result.status, UbuntuNetworkActionStatus.SUCCESS)
    add_calls = [c for c in runner.calls if c["args"][:3] == ["ip", "route", "add"]]
    testbase.assert_equal(len(add_calls), 1)
    testbase.assert_equal(
        add_calls[0]["args"],
        ["ip", "route", "add", "10.0.0.0/24", "via", "192.168.1.1"],
    )
    testbase.assert_equal(add_calls[0]["elevated"], True)
    return


def test_add_route_skipped_when_already_present():
    """
        add_route returns SKIPPED when ``ip route show <dest>`` reports
        a matching line.
    """
    runner = _MockCommandRunner()
    runner.results[("ip", "route", "show", "10.0.0.0/24")] = RunResult(
        exit_code=0,
        stdout="10.0.0.0/24 via 192.168.1.1 dev eth0\n",
        stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(),
    )
    result = manager.add_route(
        dest="10.0.0.0/24", gateway="192.168.1.1",
    )

    testbase.assert_equal(result.status, UbuntuNetworkActionStatus.SKIPPED)
    add_calls = [c for c in runner.calls if c["args"][:3] == ["ip", "route", "add"]]
    testbase.assert_equal(add_calls, [])
    return


def test_remove_route_skipped_when_absent():
    """
        remove_route returns SKIPPED when no matching route exists.
    """
    runner = _MockCommandRunner()
    runner.results[("ip", "route", "show", "10.0.0.0/24")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(),
    )
    result = manager.remove_route(dest="10.0.0.0/24")

    testbase.assert_equal(result.status, UbuntuNetworkActionStatus.SKIPPED)
    del_calls = [c for c in runner.calls if c["args"][:3] == ["ip", "route", "del"]]
    testbase.assert_equal(del_calls, [])
    return


# ----------------------------------------------------------------------
# set_dns_servers / get_dns_servers
# ----------------------------------------------------------------------


def test_set_dns_servers_dispatches_resolvectl():
    """
        set_dns_servers dispatches ``resolvectl dns <iface> <servers>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("resolvectl", "dns", "eth0")] = RunResult(
        exit_code=0,
        stdout="Link 2 (eth0):\n",
        stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(elevated=True),
    )
    result = manager.set_dns_servers(
        interface="eth0",
        servers=["1.1.1.1", "8.8.8.8"],
    )

    testbase.assert_equal(result.status, UbuntuNetworkActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["resolvectl", "dns"] and len(c["args"]) > 3
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["resolvectl", "dns", "eth0", "1.1.1.1", "8.8.8.8"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_set_dns_servers_skipped_when_matching():
    """
        set_dns_servers returns SKIPPED when the current list already
        matches the desired list.
    """
    runner = _MockCommandRunner()
    runner.results[("resolvectl", "dns", "eth0")] = RunResult(
        exit_code=0,
        stdout="Link 2 (eth0): 1.1.1.1 8.8.8.8\n",
        stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(),
    )
    result = manager.set_dns_servers(
        interface="eth0", servers=["8.8.8.8", "1.1.1.1"],
    )

    testbase.assert_equal(result.status, UbuntuNetworkActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["resolvectl", "dns"] and len(c["args"]) > 3
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_get_dns_servers_parses_resolvectl_output():
    """
        get_dns_servers extracts servers from a per-link payload.
    """
    runner = _MockCommandRunner()
    runner.results[("resolvectl", "dns", "eth0")] = RunResult(
        exit_code=0,
        stdout="Link 2 (eth0): 1.1.1.1 8.8.8.8\n",
        stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(),
    )
    servers = manager.get_dns_servers(interface="eth0")

    testbase.assert_equal(servers, ["1.1.1.1", "8.8.8.8"])
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


# ----------------------------------------------------------------------
# get_hostname / set_hostname
# ----------------------------------------------------------------------


def test_get_hostname_strips_whitespace():
    """
        get_hostname returns ``stdout.strip()`` from ``hostname``.
    """
    runner = _MockCommandRunner()
    runner.results[("hostname",)] = RunResult(
        exit_code=0, stdout="ubuntu-host\n", stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(elevated=True),
    )
    name = manager.get_hostname()

    testbase.assert_equal(name, "ubuntu-host")
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_set_hostname_skipped_when_matching():
    """
        set_hostname returns SKIPPED when ``hostname`` already reports
        the desired name.
    """
    runner = _MockCommandRunner()
    runner.results[("hostname",)] = RunResult(
        exit_code=0, stdout="myhost\n", stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(),
    )
    result = manager.set_hostname(name="myhost")

    testbase.assert_equal(result.status, UbuntuNetworkActionStatus.SKIPPED)
    set_calls = [
        c for c in runner.calls if c["args"][:1] == ["hostnamectl"]
    ]
    testbase.assert_equal(set_calls, [])
    return


def test_set_hostname_dispatches_hostnamectl():
    """
        set_hostname dispatches ``hostnamectl set-hostname <name>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("hostname",)] = RunResult(
        exit_code=0, stdout="oldhost\n", stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(elevated=True),
    )
    result = manager.set_hostname(name="newhost")

    testbase.assert_equal(result.status, UbuntuNetworkActionStatus.SUCCESS)
    set_calls = [
        c for c in runner.calls if c["args"][:1] == ["hostnamectl"]
    ]
    testbase.assert_equal(len(set_calls), 1)
    testbase.assert_equal(
        set_calls[0]["args"], ["hostnamectl", "set-hostname", "newhost"],
    )
    testbase.assert_equal(set_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Cross-cutting: dry-run + elevation policy
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_every_write():
    """
        Dry-run mode returns DRY_RUN for every write verb without
        invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(dry_run=True),
    )

    cfg = manager.configure_interface(
        name="eth0", address="1.2.3.4", netmask="24", gateway=None,
    )
    enable = manager.enable_interface(name="eth0")
    disable = manager.disable_interface(name="eth0")
    add = manager.add_route(dest="10.0.0.0/24", gateway="1.1.1.1")
    rem = manager.remove_route(dest="10.0.0.0/24")
    dns = manager.set_dns_servers(interface="eth0", servers=["1.1.1.1"])
    host = manager.set_hostname(name="newhost")

    for result in (cfg, enable, disable, add, rem, dns, host):
        testbase.assert_equal(result.status, UbuntuNetworkActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_reads_never_pass_elevated_true():
    """
        Read-only verbs never pass ``elevated=True`` regardless of
        config.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="[]", stderr=""),
    )
    runner.results[("hostname",)] = RunResult(
        exit_code=0, stdout="h\n", stderr="",
    )
    runner.results[("resolvectl", "dns", "eth0")] = RunResult(
        exit_code=0, stdout="Link 2 (eth0):\n", stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(elevated=True),
    )
    manager.get_interfaces()
    manager.get_dns_servers(interface="eth0")
    manager.get_hostname()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_write_surfaces_stderr_in_result():
    """
        A non-zero exit on a write maps to FAILED with the captured
        stderr / exit_code / command.
    """
    runner = _MockCommandRunner()
    runner.results[("ip", "-j", "link", "show", "eth0")] = RunResult(
        exit_code=0,
        stdout=json.dumps([{"ifname": "eth0", "operstate": "DOWN"}]),
        stderr="",
    )
    runner.results[("ip", "link", "set", "eth0", "up")] = RunResult(
        exit_code=2, stdout="", stderr="ip: permission denied",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(elevated=True),
    )
    result = manager.enable_interface(name="eth0")

    testbase.assert_equal(result.status, UbuntuNetworkActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.stderr, "ip: permission denied")
    testbase.assert_equal(
        result.command, ["ip", "link", "set", "eth0", "up"],
    )
    return


def test_writes_pass_elevated_true_when_configured():
    """
        Every write verb passes ``elevated=True`` when the config is
        configured to elevate.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("cat", "/etc/netplan/99-ccai.yaml")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.results[("ip", "-j", "link", "show", "eth0")] = RunResult(
        exit_code=0,
        stdout=json.dumps([{"ifname": "eth0", "operstate": "DOWN"}]),
        stderr="",
    )
    runner.results[("ip", "route", "show", "10.0.0.0/24")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[("hostname",)] = RunResult(
        exit_code=0, stdout="oldhost\n", stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(elevated=True),
    )
    manager.configure_interface(
        name="eth0", address="1.2.3.4", netmask="24", gateway="1.1.1.1",
    )
    manager.enable_interface(name="eth0")
    manager.add_route(dest="10.0.0.0/24", gateway="1.1.1.1")
    manager.set_hostname(name="newhost")

    write_heads = (
        ["tee"],
        ["netplan", "apply"],
        ["ip", "link", "set"],
        ["ip", "route", "add"],
        ["hostnamectl"],
    )
    for call in runner.calls:
        head_args = call["args"]
        for head in write_heads:
            if head_args[: len(head)] == head:
                testbase.assert_equal(call["elevated"], True)
                break
    return


# ----------------------------------------------------------------------
# Renderer / parser tests
# ----------------------------------------------------------------------


def test_render_netplan_omits_gateway_when_none():
    """
        The internal renderer omits ``gateway4`` when ``gateway`` is
        None.  Indirect coverage: write a fresh file with no gateway,
        then assert the produced stdin payload has no ``gateway4`` line.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("cat", "/etc/netplan/99-ccai.yaml")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(),
    )
    manager.configure_interface(
        name="eth0", address="10.0.0.5", netmask="24", gateway=None,
    )

    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    body = tee_calls[0]["stdin"]
    testbase.assert_equal("gateway4" in body, False)
    testbase.assert_equal(
        "addresses: [10.0.0.5/24]" in body, True,
    )
    return


def test_dotted_quad_netmask_is_converted_to_prefix():
    """
        The renderer converts ``"255.255.255.0"`` to ``/24``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("cat", "/etc/netplan/99-ccai.yaml")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )

    manager = UbuntuNetworkConfigurationManager(
        runner=runner,
        config=UbuntuNetworkConfigurationManagerConfig(),
    )
    manager.configure_interface(
        name="eth0", address="10.0.0.5", netmask="255.255.255.0",
        gateway="10.0.0.1",
    )

    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    body = tee_calls[0]["stdin"]
    testbase.assert_equal("addresses: [10.0.0.5/24]" in body, True)
    return
