"""
    Tests for :class:`UbuntuVPNClientManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by the
    argv tuple.  No live ``apt``, ``tailscale``, or ``twingate`` is
    invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from hypothesis import HealthCheck, given, settings, strategies as st

from ccai.aspects.aspects import Aspects
from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.vpnclientmanagement import (
    UbuntuVPNActionStatus,
    UbuntuVPNClientManager,
    UbuntuVPNClientManagerConfig,
    UbuntuVPNClientQueryError,
    UbuntuVPNClientStatus,
    VPNProvider,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv tuple.

        A callable can be assigned to ``result_resolver`` for cases that
        need state-dependent answers (used by the property tests for
        idempotency).
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)
    result_resolver: Optional[Callable[[tuple], Optional[RunResult]]] = None

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

        if self.result_resolver is not None:
            resolved = self.result_resolver(key)
            if resolved is not None:
                return resolved

        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


_ASPECTS = Aspects()


_DPKG_TAILSCALE = (
    "dpkg-query", "-W", "-f=${Status} ${Version}", "tailscale",
)
_DPKG_TWINGATE = (
    "dpkg-query", "-W", "-f=${Status} ${Version}", "twingate-connector",
)


def _seed_dpkg_absent(runner: _MockCommandRunner, *, pkg: str) -> None:
    """
        Seed a non-zero ``dpkg-query`` reply meaning "package absent".
    """
    key = (
        "dpkg-query", "-W", "-f=${Status} ${Version}", pkg,
    )
    runner.results[key] = RunResult(exit_code=1, stdout="", stderr="")
    return


def _seed_dpkg_installed(
    runner: _MockCommandRunner,
    *,
    pkg: str,
    version: str = "1.62.1",
) -> None:
    """
        Seed an "installed" ``dpkg-query`` reply.
    """
    key = (
        "dpkg-query", "-W", "-f=${Status} ${Version}", pkg,
    )
    runner.results[key] = RunResult(
        exit_code=0,
        stdout="install ok installed {}".format(version),
        stderr="",
    )
    return


def _make_manager(
    *,
    runner: _MockCommandRunner,
    dry_run: bool = False,
    elevated: bool = False,
) -> UbuntuVPNClientManager:
    """
        Build an :class:`UbuntuVPNClientManager` with sensible defaults.
    """
    config = UbuntuVPNClientManagerConfig(
        dry_run=dry_run,
        elevated=elevated,
    )
    rtnval = UbuntuVPNClientManager(runner=runner, config=config)
    return rtnval


# ----------------------------------------------------------------------
# install_tailscale
# ----------------------------------------------------------------------


def test_install_tailscale_runs_apt_install():
    runner = _MockCommandRunner()
    _seed_dpkg_absent(runner=runner, pkg="tailscale")
    manager = _make_manager(runner=runner)

    result = manager.install_tailscale(
        version=None, channel="stable", aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.SUCCESS
    apt_calls = [c for c in runner.calls if c["args"][:2] == ["apt-get", "install"]]
    assert len(apt_calls) == 1
    assert apt_calls[0]["args"] == [
        "apt-get", "install", "-y", "tailscale",
    ]
    return


def test_install_tailscale_skipped_when_present():
    runner = _MockCommandRunner()
    _seed_dpkg_installed(runner=runner, pkg="tailscale", version="1.62.1")
    manager = _make_manager(runner=runner)

    result = manager.install_tailscale(
        version="1.62.1", channel="stable", aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.SKIPPED
    apt_calls = [c for c in runner.calls if c["args"][:2] == ["apt-get", "install"]]
    assert len(apt_calls) == 0
    return


def test_install_tailscale_failed_on_apt_error():
    runner = _MockCommandRunner()
    _seed_dpkg_absent(runner=runner, pkg="tailscale")
    runner.results[("apt-get", "install", "-y", "tailscale")] = RunResult(
        exit_code=100, stdout="", stderr="apt broke",
    )
    manager = _make_manager(runner=runner)

    result = manager.install_tailscale(
        version=None, channel="stable", aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.FAILED
    assert result.exit_code == 100
    assert result.stderr == "apt broke"
    return


# ----------------------------------------------------------------------
# install_twingate_connector
# ----------------------------------------------------------------------


def test_install_twingate_connector_runs_apt_install():
    runner = _MockCommandRunner()
    _seed_dpkg_absent(runner=runner, pkg="twingate-connector")
    manager = _make_manager(runner=runner)

    result = manager.install_twingate_connector(
        version=None, aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.SUCCESS
    apt_calls = [c for c in runner.calls if c["args"][:2] == ["apt-get", "install"]]
    assert len(apt_calls) == 1
    assert apt_calls[0]["args"] == [
        "apt-get", "install", "-y", "twingate-connector",
    ]
    return


def test_install_twingate_connector_skipped_when_present():
    runner = _MockCommandRunner()
    _seed_dpkg_installed(runner=runner, pkg="twingate-connector")
    manager = _make_manager(runner=runner)

    result = manager.install_twingate_connector(
        version=None, aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.SKIPPED
    return


# ----------------------------------------------------------------------
# configure
# ----------------------------------------------------------------------


def test_configure_tailscale_returns_skipped():
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner)

    result = manager.configure(
        provider=VPNProvider.TAILSCALE,
        settings={"network": "demo"},
        aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.SKIPPED
    assert len(runner.calls) == 0
    return


def test_configure_twingate_writes_conf_via_tee():
    runner = _MockCommandRunner()
    # Seed cat to report file missing.
    runner.results[("cat", "/etc/twingate/connector.conf")] = RunResult(
        exit_code=1, stdout="", stderr="No such file",
    )
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.configure(
        provider=VPNProvider.TWINGATE,
        settings={"network": "demo", "remote_network_id": "rn_abc"},
        aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.SUCCESS
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    assert len(tee_calls) == 1
    assert tee_calls[0]["args"] == ["tee", "/etc/twingate/connector.conf"]
    assert "network=demo" in (tee_calls[0]["stdin"] or "")
    assert "remote_network_id=rn_abc" in (tee_calls[0]["stdin"] or "")
    assert tee_calls[0]["elevated"] is True
    return


def test_configure_twingate_skipped_when_content_matches():
    runner = _MockCommandRunner()
    body_lines = ["network=demo", "remote_network_id=rn_abc"]
    body = "\n".join(body_lines) + "\n"
    runner.results[("cat", "/etc/twingate/connector.conf")] = RunResult(
        exit_code=0, stdout=body, stderr="",
    )
    manager = _make_manager(runner=runner)

    result = manager.configure(
        provider=VPNProvider.TWINGATE,
        settings={"network": "demo", "remote_network_id": "rn_abc"},
        aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.SKIPPED
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    assert len(tee_calls) == 0
    return


# ----------------------------------------------------------------------
# bring_up / bring_down
# ----------------------------------------------------------------------


def test_bring_up_tailscale_runs_tailscale_up_with_flags():
    runner = _MockCommandRunner()
    _seed_dpkg_installed(runner=runner, pkg="tailscale")
    # status returns NeedsLogin / not running
    not_running_payload = json.dumps({"BackendState": "NeedsLogin"})
    runner.results[("tailscale", "status", "--json")] = RunResult(
        exit_code=0, stdout=not_running_payload, stderr="",
    )
    manager = _make_manager(runner=runner)

    result = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey="tskey-XYZ",
        hostname="host1",
        advertise_tags=["tag:server", "tag:db"],
        advertise_routes=["10.0.0.0/24"],
        exit_node=True,
        aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.SUCCESS
    up_calls = [c for c in runner.calls if c["args"][:2] == ["tailscale", "up"]]
    assert len(up_calls) == 1
    args = up_calls[0]["args"]
    assert "--authkey=tskey-XYZ" in args
    assert "--hostname=host1" in args
    assert "--advertise-tags=tag:server,tag:db" in args
    assert "--advertise-routes=10.0.0.0/24" in args
    assert "--advertise-exit-node" in args
    return


def test_bring_up_tailscale_skipped_when_running_with_matching_state():
    runner = _MockCommandRunner()
    _seed_dpkg_installed(runner=runner, pkg="tailscale")
    running_payload = json.dumps({
        "BackendState": "Running",
        "Self": {
            "HostName": "host1",
            "TailscaleIPs": ["100.64.0.1"],
        },
        "MagicDNSSuffix": "tail-demo.ts.net",
        "Version": "1.62.1",
        "Peer": {},
    })
    runner.results[("tailscale", "status", "--json")] = RunResult(
        exit_code=0, stdout=running_payload, stderr="",
    )
    manager = _make_manager(runner=runner)

    result = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey="tskey-XYZ",
        hostname="host1",
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.SKIPPED
    up_calls = [c for c in runner.calls if c["args"][:2] == ["tailscale", "up"]]
    assert len(up_calls) == 0
    return


def test_bring_up_twingate_runs_systemctl_enable_now():
    runner = _MockCommandRunner()
    _seed_dpkg_installed(runner=runner, pkg="twingate-connector")
    runner.results[("twingate", "status")] = RunResult(
        exit_code=0, stdout="offline", stderr="",
    )
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.bring_up(
        provider=VPNProvider.TWINGATE,
        authkey="tg-token",
        hostname=None,
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.SUCCESS
    systemctl_calls = [c for c in runner.calls if c["args"][:1] == ["systemctl"]]
    assert len(systemctl_calls) == 1
    assert systemctl_calls[0]["args"] == [
        "systemctl", "enable", "--now", "twingate-connector",
    ]
    assert systemctl_calls[0]["elevated"] is True
    return


def test_bring_down_tailscale_runs_tailscale_down():
    runner = _MockCommandRunner()
    _seed_dpkg_installed(runner=runner, pkg="tailscale")
    running_payload = json.dumps({
        "BackendState": "Running",
        "Self": {"HostName": "h", "TailscaleIPs": ["100.64.0.1"]},
        "Peer": {},
    })
    runner.results[("tailscale", "status", "--json")] = RunResult(
        exit_code=0, stdout=running_payload, stderr="",
    )
    manager = _make_manager(runner=runner)

    result = manager.bring_down(
        provider=VPNProvider.TAILSCALE, aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.SUCCESS
    down_calls = [c for c in runner.calls if c["args"] == ["tailscale", "down"]]
    assert len(down_calls) == 1
    return


def test_bring_down_twingate_runs_systemctl_stop():
    runner = _MockCommandRunner()
    _seed_dpkg_installed(runner=runner, pkg="twingate-connector")
    runner.results[("twingate", "status")] = RunResult(
        exit_code=0, stdout="online", stderr="",
    )
    manager = _make_manager(runner=runner)

    result = manager.bring_down(
        provider=VPNProvider.TWINGATE, aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.SUCCESS
    sys_calls = [
        c for c in runner.calls
        if c["args"] == ["systemctl", "stop", "twingate-connector"]
    ]
    assert len(sys_calls) == 1
    return


# ----------------------------------------------------------------------
# status
# ----------------------------------------------------------------------


_TAILSCALE_RUNNING_JSON = json.dumps({
    "BackendState": "Running",
    "Version": "1.62.1",
    "MagicDNSSuffix": "tail-demo.ts.net",
    "Self": {
        "HostName": "host1",
        "TailscaleIPs": ["100.64.0.1", "fd7a::1"],
    },
    "Peer": {
        "key1": {"HostName": "peer-a", "TailscaleIPs": ["100.64.0.2"]},
        "key2": {"HostName": "peer-b", "TailscaleIPs": ["100.64.0.3"]},
    },
})


def test_status_tailscale_parses_running_json():
    runner = _MockCommandRunner()
    _seed_dpkg_installed(runner=runner, pkg="tailscale")
    runner.results[("tailscale", "status", "--json")] = RunResult(
        exit_code=0, stdout=_TAILSCALE_RUNNING_JSON, stderr="",
    )
    manager = _make_manager(runner=runner)

    snapshot = manager.status(
        provider=VPNProvider.TAILSCALE, aspects=_ASPECTS,
    )

    assert snapshot.installed is True
    assert snapshot.running is True
    assert snapshot.self_address == "100.64.0.1"
    assert snapshot.hostname == "host1"
    assert snapshot.tailnet == "tail-demo.ts.net"
    assert snapshot.peer_addresses == {
        "peer-a": "100.64.0.2",
        "peer-b": "100.64.0.3",
    }
    return


def test_status_tailscale_returns_installed_false_when_missing():
    runner = _MockCommandRunner()
    _seed_dpkg_absent(runner=runner, pkg="tailscale")
    manager = _make_manager(runner=runner)

    snapshot = manager.status(
        provider=VPNProvider.TAILSCALE, aspects=_ASPECTS,
    )

    assert snapshot.installed is False
    assert snapshot.running is False
    return


def test_status_twingate_parses_text():
    runner = _MockCommandRunner()
    _seed_dpkg_installed(runner=runner, pkg="twingate-connector")
    runner.results[("twingate", "status")] = RunResult(
        exit_code=0, stdout="Connector is online", stderr="",
    )
    manager = _make_manager(runner=runner)

    snapshot = manager.status(
        provider=VPNProvider.TWINGATE, aspects=_ASPECTS,
    )

    assert snapshot.installed is True
    assert snapshot.running is True
    assert snapshot.self_address is None
    assert snapshot.peer_addresses == {}
    return


def test_status_tailscale_raises_on_unexpected_failure():
    runner = _MockCommandRunner()
    _seed_dpkg_installed(runner=runner, pkg="tailscale")
    runner.results[("tailscale", "status", "--json")] = RunResult(
        exit_code=2, stdout="", stderr="permission denied",
    )
    manager = _make_manager(runner=runner)

    raised = None
    try:
        manager.status(provider=VPNProvider.TAILSCALE, aspects=_ASPECTS)
    except UbuntuVPNClientQueryError as caught:
        raised = caught

    assert raised is not None
    assert raised.exit_code == 2
    return


# ----------------------------------------------------------------------
# uninstall
# ----------------------------------------------------------------------


def test_uninstall_tailscale_purge_removes_repo_files():
    runner = _MockCommandRunner()
    _seed_dpkg_installed(runner=runner, pkg="tailscale")
    manager = _make_manager(runner=runner, elevated=True)

    result = manager.uninstall(
        provider=VPNProvider.TAILSCALE, purge=True, aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.SUCCESS
    rm_calls = [c for c in runner.calls if c["args"][:2] == ["rm", "-f"]]
    rm_paths = [c["args"][2] for c in rm_calls]
    assert "/etc/apt/sources.list.d/tailscale.list" in rm_paths
    assert "/usr/share/keyrings/tailscale-archive-keyring.gpg" in rm_paths
    for rm in rm_calls:
        assert rm["elevated"] is True
    return


def test_uninstall_skipped_when_not_installed():
    runner = _MockCommandRunner()
    _seed_dpkg_absent(runner=runner, pkg="tailscale")
    manager = _make_manager(runner=runner)

    result = manager.uninstall(
        provider=VPNProvider.TAILSCALE, purge=False, aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.SKIPPED
    apt_calls = [c for c in runner.calls if c["args"][:1] == ["apt-get"]]
    assert len(apt_calls) == 0
    return


# ----------------------------------------------------------------------
# Dry-run / elevation
# ----------------------------------------------------------------------


def test_dry_run_short_circuits_every_write_verb():
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, dry_run=True)

    install_t = manager.install_tailscale(
        version=None, channel="stable", aspects=_ASPECTS,
    )
    install_tg = manager.install_twingate_connector(
        version=None, aspects=_ASPECTS,
    )
    configure_tg = manager.configure(
        provider=VPNProvider.TWINGATE,
        settings={"network": "x"},
        aspects=_ASPECTS,
    )
    up = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey="k",
        hostname=None,
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_ASPECTS,
    )
    down = manager.bring_down(
        provider=VPNProvider.TAILSCALE, aspects=_ASPECTS,
    )
    un = manager.uninstall(
        provider=VPNProvider.TAILSCALE, purge=False, aspects=_ASPECTS,
    )

    for result in (install_t, install_tg, configure_tg, up, down, un):
        assert result.status == UbuntuVPNActionStatus.DRY_RUN

    assert len(runner.calls) == 0
    return


def test_writes_pass_elevated_when_configured():
    runner = _MockCommandRunner()
    _seed_dpkg_absent(runner=runner, pkg="tailscale")
    manager = _make_manager(runner=runner, elevated=True)

    manager.install_tailscale(
        version=None, channel="stable", aspects=_ASPECTS,
    )

    apt_calls = [c for c in runner.calls if c["args"][:2] == ["apt-get", "install"]]
    assert len(apt_calls) == 1
    assert apt_calls[0]["elevated"] is True
    return


def test_reads_never_elevate():
    runner = _MockCommandRunner()
    _seed_dpkg_installed(runner=runner, pkg="tailscale")
    runner.results[("tailscale", "status", "--json")] = RunResult(
        exit_code=0, stdout=_TAILSCALE_RUNNING_JSON, stderr="",
    )
    manager = _make_manager(runner=runner, elevated=True)

    manager.status(provider=VPNProvider.TAILSCALE, aspects=_ASPECTS)

    for call in runner.calls:
        assert call["elevated"] is False
    return


# ----------------------------------------------------------------------
# Property tests (hypothesis)
# ----------------------------------------------------------------------


def _make_stateful_runner(*, pkg: str) -> _MockCommandRunner:
    """
        Build a runner whose ``dpkg-query`` reply tracks the runner's
        own apt history.  Reflects "install ∘ install = install" and
        "uninstall ∘ uninstall = uninstall".
    """
    state = {"installed": False, "version": "1.62.1"}

    def resolver(key: tuple) -> Optional[RunResult]:
        dpkg_key = (
            "dpkg-query", "-W", "-f=${Status} ${Version}", pkg,
        )
        if key == dpkg_key:
            if state["installed"] is True:
                rtnval: Optional[RunResult] = RunResult(
                    exit_code=0,
                    stdout="install ok installed {}".format(state["version"]),
                    stderr="",
                )
                return rtnval
            rtnval = RunResult(exit_code=1, stdout="", stderr="")
            return rtnval

        if (
            len(key) >= 3
            and key[0] == "apt-get"
            and key[1] == "install"
            and pkg in key[-1]
        ):
            state["installed"] = True
            install_arg = key[-1]
            if "=" in install_arg:
                _name, _sep, version_suffix = install_arg.partition("=")
                state["version"] = version_suffix
            rtnval = RunResult(exit_code=0, stdout="", stderr="")
            return rtnval

        if (
            len(key) >= 3
            and key[0] == "apt-get"
            and key[1] in ("remove", "purge")
            and key[-1] == pkg
        ):
            state["installed"] = False
            rtnval = RunResult(exit_code=0, stdout="", stderr="")
            return rtnval

        return None

    runner = _MockCommandRunner(result_resolver=resolver)
    return runner


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    channel=st.sampled_from(["stable", "unstable"]),
    version=st.one_of(st.none(), st.just("1.62.1"), st.just("1.70.0")),
)
def test_property_install_tailscale_idempotent(channel, version):
    runner = _make_stateful_runner(pkg="tailscale")
    manager = _make_manager(runner=runner)

    first = manager.install_tailscale(
        version=version, channel=channel, aspects=_ASPECTS,
    )
    second = manager.install_tailscale(
        version=version, channel=channel, aspects=_ASPECTS,
    )

    assert first.status == UbuntuVPNActionStatus.SUCCESS
    assert second.status == UbuntuVPNActionStatus.SKIPPED
    return


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    provider=st.sampled_from([VPNProvider.TAILSCALE, VPNProvider.TWINGATE]),
    purge=st.booleans(),
)
def test_property_uninstall_idempotent(provider, purge):
    pkg = "tailscale" if provider == VPNProvider.TAILSCALE else "twingate-connector"
    runner = _make_stateful_runner(pkg=pkg)
    # Seed the package as installed.
    runner.result_resolver  # ensure resolver-ref present
    # Drive the resolver into the installed=True state by issuing an
    # install through the API (so subsequent uninstall has work to do).
    manager = _make_manager(runner=runner)
    if provider == VPNProvider.TAILSCALE:
        manager.install_tailscale(
            version=None, channel="stable", aspects=_ASPECTS,
        )
    else:
        manager.install_twingate_connector(
            version=None, aspects=_ASPECTS,
        )

    first = manager.uninstall(
        provider=provider, purge=purge, aspects=_ASPECTS,
    )
    second = manager.uninstall(
        provider=provider, purge=purge, aspects=_ASPECTS,
    )

    assert first.status == UbuntuVPNActionStatus.SUCCESS
    assert second.status == UbuntuVPNActionStatus.SKIPPED
    return


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    hostname=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
        min_size=1,
        max_size=12,
    ),
    exit_node=st.booleans(),
)
def test_property_bring_up_idempotent_on_matching_state(hostname, exit_node):
    runner = _MockCommandRunner()
    _seed_dpkg_installed(runner=runner, pkg="tailscale")
    running_payload = json.dumps({
        "BackendState": "Running",
        "Self": {
            "HostName": hostname,
            "TailscaleIPs": ["100.64.0.1"],
        },
        "Peer": {},
    })
    runner.results[("tailscale", "status", "--json")] = RunResult(
        exit_code=0, stdout=running_payload, stderr="",
    )
    manager = _make_manager(runner=runner)

    result = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey="k",
        hostname=hostname,
        advertise_tags=None,
        advertise_routes=None,
        exit_node=exit_node,
        aspects=_ASPECTS,
    )

    assert result.status == UbuntuVPNActionStatus.SKIPPED
    return


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    provider=st.sampled_from([VPNProvider.TAILSCALE, VPNProvider.TWINGATE]),
    purge=st.booleans(),
)
def test_property_dry_run_never_invokes_runner(provider, purge):
    runner = _MockCommandRunner()
    manager = _make_manager(runner=runner, dry_run=True)

    if provider == VPNProvider.TAILSCALE:
        r = manager.install_tailscale(
            version=None, channel="stable", aspects=_ASPECTS,
        )
    else:
        r = manager.install_twingate_connector(
            version=None, aspects=_ASPECTS,
        )

    assert r.status == UbuntuVPNActionStatus.DRY_RUN
    assert len(runner.calls) == 0

    u = manager.uninstall(
        provider=provider, purge=purge, aspects=_ASPECTS,
    )
    assert u.status == UbuntuVPNActionStatus.DRY_RUN
    assert len(runner.calls) == 0
    return


# ----------------------------------------------------------------------
# Audit regression tests — C1 (shell injection), C3 (authkey redaction),
# S-H1 (version validation), S-H2 (signed-by keyring)
# ----------------------------------------------------------------------


import pytest


def test_ensure_tailscale_repo_uses_no_shell():
    """
        C1 regression: the keyring step must never dispatch ``bash -c``;
        curl and tee are run as separate plain argv lists.
    """
    runner = _MockCommandRunner()
    _seed_dpkg_absent(runner=runner, pkg="tailscale")
    manager = _make_manager(runner=runner)

    manager.install_tailscale(
        version=None, channel="stable", aspects=_ASPECTS,
    )

    for call in runner.calls:
        assert call["args"][0] != "bash"
        assert "-c" not in call["args"][:2]

    curl_calls = [c for c in runner.calls if c["args"][0] == "curl"]
    assert len(curl_calls) == 1
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    assert len(tee_calls) >= 1
    return


def test_ensure_tailscale_repo_rejects_metacharacter_channel():
    """
        C1 regression: a channel laden with shell metacharacters is
        rejected outright rather than interpolated into a command.
    """
    runner = _MockCommandRunner()
    _seed_dpkg_absent(runner=runner, pkg="tailscale")
    manager = _make_manager(runner=runner)

    malicious = "stable; rm -rf / #"
    with pytest.raises(ValueError):
        manager.install_tailscale(
            version=None, channel=malicious, aspects=_ASPECTS,
        )

    # No command must have carried the injected payload.
    for call in runner.calls:
        joined = " ".join(call["args"])
        assert "rm -rf" not in joined
    return


def test_ensure_tailscale_repo_rejects_metacharacter_codename():
    """
        C1 regression: an unsafe ubuntu_codename is rejected.
    """
    runner = _MockCommandRunner()
    _seed_dpkg_absent(runner=runner, pkg="tailscale")
    config = UbuntuVPNClientManagerConfig(
        ubuntu_codename="jammy$(touch /tmp/pwned)",
    )
    manager = UbuntuVPNClientManager(runner=runner, config=config)

    with pytest.raises(ValueError):
        manager.install_tailscale(
            version=None, channel="stable", aspects=_ASPECTS,
        )
    return


def test_install_tailscale_rejects_unsafe_version():
    """
        S-H1 regression: a version with shell metacharacters is rejected.
    """
    runner = _MockCommandRunner()
    _seed_dpkg_absent(runner=runner, pkg="tailscale")
    manager = _make_manager(runner=runner)

    with pytest.raises(ValueError):
        manager.install_tailscale(
            version="1.0; reboot", channel="stable", aspects=_ASPECTS,
        )
    return


def test_bring_up_redacts_authkey_in_command():
    """
        C3 regression: the Tailscale auth key must not survive into the
        returned ``VPNActionResult.command``.
    """
    runner = _MockCommandRunner()
    _seed_dpkg_installed(runner=runner, pkg="tailscale")
    runner.results[("tailscale", "status", "--json")] = RunResult(
        exit_code=0,
        stdout=json.dumps({"BackendState": "Stopped"}),
        stderr="",
    )
    manager = _make_manager(runner=runner)

    secret = "tskey-auth-SUPERSECRETVALUE"
    result = manager.bring_up(
        provider=VPNProvider.TAILSCALE,
        authkey=secret,
        hostname=None,
        advertise_tags=None,
        advertise_routes=None,
        exit_node=False,
        aspects=_ASPECTS,
    )

    assert result.command is not None
    joined = " ".join(result.command)
    assert secret not in joined
    assert "--authkey=***" in joined
    return


def test_twingate_repo_uses_signed_by_not_trusted_yes():
    """
        S-H2 regression: the Twingate apt source must use a signed-by
        keyring, never ``[trusted=yes]``.
    """
    runner = _MockCommandRunner()
    _seed_dpkg_absent(runner=runner, pkg="twingate-connector")
    manager = _make_manager(runner=runner)

    manager.install_twingate_connector(version=None, aspects=_ASPECTS)

    tee_sources_calls = [
        c for c in runner.calls
        if c["args"][0] == "tee"
        and c["args"][-1].endswith("twingate.list")
    ]
    assert len(tee_sources_calls) == 1
    body = tee_sources_calls[0]["stdin"] or ""
    assert "trusted=yes" not in body
    assert "signed-by=" in body
    return
