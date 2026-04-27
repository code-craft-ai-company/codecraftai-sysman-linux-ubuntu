"""
    Tests for :class:`UbuntuDnsServerManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult` values
    keyed by argv tuple.  No live ``rndc`` /
    ``named-checkconf`` / ``named-checkzone`` is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.dnsservermanagement import (
    UbuntuDnsActionStatus,
    UbuntuDnsManagerConfig,
    UbuntuDnsQueryError,
    UbuntuDnsRecordInfo,
    UbuntuDnsRecordSpec,
    UbuntuDnsServerManager,
    UbuntuDnsZoneInfo,
    UbuntuDnsZoneSpec,
    UbuntuDnsZoneStatus,
    _bump_soa_serial,
    _parse_zone_records,
    _render_initial_zone_file,
    _render_record_line,
    _render_zone_block,
)


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
        on success, or non-zero when ``body`` is None.
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
# add_zone
# ----------------------------------------------------------------------


def test_add_zone_invokes_rndc():
    """
        add_zone writes the rendered ``zone {}`` block to
        ``named.conf.local`` via tee, writes the zone-data file
        via tee, runs the validators, and dispatches
        ``rndc reconfig``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    _seed_cat(runner, path="/etc/bind/named.conf.local", body="")
    _seed_test_dash_e(
        runner,
        path="/etc/bind/zones/db.example.com",
        exists=False,
    )

    config = UbuntuDnsManagerConfig(elevated=True)
    manager = UbuntuDnsServerManager(runner=runner, config=config)
    spec = UbuntuDnsZoneSpec(name="example.com", zone_type="primary")

    result = manager.add_zone(name="example.com", spec=spec)
    testbase.assert_equal(
        result.status, UbuntuDnsActionStatus.SUCCESS,
    )

    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 2)
    tee_targets = [call["args"][1] for call in tee_calls]
    testbase.assert_equal(
        "/etc/bind/named.conf.local" in tee_targets, True,
    )
    testbase.assert_equal(
        "/etc/bind/zones/db.example.com" in tee_targets, True,
    )

    rndc_calls = [
        call for call in runner.calls
        if call["args"] == ["rndc", "reconfig"]
    ]
    testbase.assert_equal(len(rndc_calls), 1)
    testbase.assert_equal(rndc_calls[0]["elevated"], True)

    checkconf_calls = [
        call for call in runner.calls
        if call["args"][0] == "named-checkconf"
        and "-p" not in call["args"]
    ]
    testbase.assert_equal(len(checkconf_calls), 1)
    return


def test_add_zone_skipped_when_present():
    """
        add_zone returns SKIPPED when the sentinel block AND the
        zone-data file are already present.
    """
    runner = _MockCommandRunner()
    spec = UbuntuDnsZoneSpec(name="example.com", zone_type="primary")
    zone_file_path = "/etc/bind/zones/db.example.com"
    zone_block = _render_zone_block(
        name="example.com", spec=spec, zone_file_path=zone_file_path,
    )
    existing_local = (
        "# >>> automojo-zone:example.com >>>\n"
        + zone_block
        + "# <<< automojo-zone:example.com <<<\n"
    )
    _seed_cat(
        runner,
        path="/etc/bind/named.conf.local",
        body=existing_local,
    )
    _seed_test_dash_e(runner, path=zone_file_path, exists=True)

    manager = UbuntuDnsServerManager(
        runner=runner, config=UbuntuDnsManagerConfig(),
    )
    result = manager.add_zone(name="example.com", spec=spec)
    testbase.assert_equal(
        result.status, UbuntuDnsActionStatus.SKIPPED,
    )
    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    rndc_calls = [
        call for call in runner.calls
        if call["args"] == ["rndc", "reconfig"]
    ]
    testbase.assert_equal(len(rndc_calls), 0)
    return


def test_add_zone_dry_run():
    """
        With ``dry_run=True``, add_zone records the intended argv
        but does not invoke ``tee``, ``named-checkconf``, or
        ``rndc``.
    """
    runner = _MockCommandRunner()
    config = UbuntuDnsManagerConfig(dry_run=True)
    manager = UbuntuDnsServerManager(runner=runner, config=config)
    spec = UbuntuDnsZoneSpec(name="example.com")
    result = manager.add_zone(name="example.com", spec=spec)

    testbase.assert_equal(
        result.status, UbuntuDnsActionStatus.DRY_RUN,
    )
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    rndc_calls = [
        call for call in runner.calls
        if call["args"][0] == "rndc"
    ]
    testbase.assert_equal(len(rndc_calls), 0)
    return


# ----------------------------------------------------------------------
# remove_zone
# ----------------------------------------------------------------------


def test_remove_zone_invokes_rndc():
    """
        remove_zone strips the sentinel block from
        ``named.conf.local`` via tee, deletes the zone-data file
        via rm, validates, and dispatches ``rndc reconfig``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    spec = UbuntuDnsZoneSpec(name="example.com")
    zone_block = _render_zone_block(
        name="example.com",
        spec=spec,
        zone_file_path="/etc/bind/zones/db.example.com",
    )
    existing_local = (
        "# >>> automojo-zone:example.com >>>\n"
        + zone_block
        + "# <<< automojo-zone:example.com <<<\n"
    )
    _seed_cat(
        runner,
        path="/etc/bind/named.conf.local",
        body=existing_local,
    )
    _seed_test_dash_e(
        runner,
        path="/etc/bind/zones/db.example.com",
        exists=True,
    )

    config = UbuntuDnsManagerConfig(elevated=True)
    manager = UbuntuDnsServerManager(runner=runner, config=config)
    result = manager.remove_zone(name="example.com")

    testbase.assert_equal(
        result.status, UbuntuDnsActionStatus.SUCCESS,
    )
    rm_calls = [
        call for call in runner.calls
        if call["args"][0] == "rm"
    ]
    testbase.assert_equal(len(rm_calls), 1)
    testbase.assert_equal(
        rm_calls[0]["args"],
        ["rm", "-f", "/etc/bind/zones/db.example.com"],
    )
    testbase.assert_equal(rm_calls[0]["elevated"], True)

    rndc_calls = [
        call for call in runner.calls
        if call["args"] == ["rndc", "reconfig"]
    ]
    testbase.assert_equal(len(rndc_calls), 1)
    return


def test_remove_zone_skipped_when_absent():
    """
        remove_zone returns SKIPPED when neither the sentinel
        block nor the zone-data file is present.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path="/etc/bind/named.conf.local", body="")
    _seed_test_dash_e(
        runner,
        path="/etc/bind/zones/db.ghost.com",
        exists=False,
    )

    manager = UbuntuDnsServerManager(
        runner=runner, config=UbuntuDnsManagerConfig(),
    )
    result = manager.remove_zone(name="ghost.com")
    testbase.assert_equal(
        result.status, UbuntuDnsActionStatus.SKIPPED,
    )
    rm_calls = [
        call for call in runner.calls
        if call["args"][0] == "rm"
    ]
    testbase.assert_equal(len(rm_calls), 0)
    return


# ----------------------------------------------------------------------
# list_zones
# ----------------------------------------------------------------------


def test_list_zones_parses_named_conf_local_output():
    """
        list_zones returns one row per managed sentinel block
        and folds in any operator-authored zones surfaced by
        ``named-checkconf -p``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    spec = UbuntuDnsZoneSpec(name="example.com")
    zone_block = _render_zone_block(
        name="example.com",
        spec=spec,
        zone_file_path="/etc/bind/zones/db.example.com",
    )
    existing_local = (
        "# >>> automojo-zone:example.com >>>\n"
        + zone_block
        + "# <<< automojo-zone:example.com <<<\n"
    )
    _seed_cat(
        runner,
        path="/etc/bind/named.conf.local",
        body=existing_local,
    )
    _seed_cat(
        runner,
        path="/etc/bind/zones/db.example.com",
        body=_render_initial_zone_file(name="example.com"),
    )

    runner.results[("named-checkconf", "-p")] = RunResult(
        exit_code=0,
        stdout=(
            "zone \"localhost\" {\n"
            "    type master;\n"
            "    file \"/etc/bind/db.local\";\n"
            "};\n"
            "zone \"example.com\" {\n"
            "    type master;\n"
            "    file \"/etc/bind/zones/db.example.com\";\n"
            "};\n"
        ),
        stderr="",
    )

    manager = UbuntuDnsServerManager(
        runner=runner, config=UbuntuDnsManagerConfig(),
    )
    rows = manager.list_zones()
    testbase.assert_equal(len(rows) >= 2, True)
    by_name = {row.name: row for row in rows}
    testbase.assert_equal(
        isinstance(by_name["example.com"], UbuntuDnsZoneInfo), True,
    )
    testbase.assert_equal(by_name["example.com"].is_active, True)
    testbase.assert_equal(
        by_name["example.com"].zone_file,
        "/etc/bind/zones/db.example.com",
    )
    testbase.assert_equal(by_name["localhost"].is_active, False)
    return


# ----------------------------------------------------------------------
# add_record
# ----------------------------------------------------------------------


def test_add_record_invokes_rndc():
    """
        add_record reads the zone file, appends the rendered
        record line, bumps the SOA serial, writes via tee, runs
        validators, and dispatches ``rndc reload <zone>``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    starting_body = _render_initial_zone_file(name="example.com")
    _seed_cat(
        runner,
        path="/etc/bind/zones/db.example.com",
        body=starting_body,
    )

    config = UbuntuDnsManagerConfig(elevated=True)
    manager = UbuntuDnsServerManager(runner=runner, config=config)
    record = UbuntuDnsRecordSpec(
        name="www",
        record_type="A",
        value="203.0.113.5",
        ttl=300,
    )
    result = manager.add_record(zone="example.com", record=record)
    testbase.assert_equal(
        result.status, UbuntuDnsActionStatus.SUCCESS,
    )

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"],
        ["tee", "/etc/bind/zones/db.example.com"],
    )
    testbase.assert_equal(
        "www 300 IN A 203.0.113.5" in tee_calls[0]["stdin"], True,
    )

    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["rndc", "reload", "example.com"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    testbase.assert_equal(reload_calls[0]["elevated"], True)
    return


def test_add_record_skipped_when_present():
    """
        add_record returns SKIPPED when an identical record is
        already on disk.
    """
    runner = _MockCommandRunner()
    record = UbuntuDnsRecordSpec(
        name="www",
        record_type="A",
        value="203.0.113.5",
    )
    record_line = _render_record_line(record=record)
    starting_body = (
        _render_initial_zone_file(name="example.com")
        + record_line + "\n"
    )
    _seed_cat(
        runner,
        path="/etc/bind/zones/db.example.com",
        body=starting_body,
    )

    manager = UbuntuDnsServerManager(
        runner=runner, config=UbuntuDnsManagerConfig(),
    )
    result = manager.add_record(zone="example.com", record=record)
    testbase.assert_equal(
        result.status, UbuntuDnsActionStatus.SKIPPED,
    )
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


# ----------------------------------------------------------------------
# remove_record
# ----------------------------------------------------------------------


def test_remove_record_invokes_rndc():
    """
        remove_record drops the matching line, bumps the SOA
        serial, writes via tee, validates, and dispatches
        ``rndc reload <zone>``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    record = UbuntuDnsRecordSpec(
        name="www", record_type="A", value="203.0.113.5",
    )
    starting_body = (
        _render_initial_zone_file(name="example.com")
        + _render_record_line(record=record) + "\n"
    )
    _seed_cat(
        runner,
        path="/etc/bind/zones/db.example.com",
        body=starting_body,
    )

    config = UbuntuDnsManagerConfig(elevated=True)
    manager = UbuntuDnsServerManager(runner=runner, config=config)
    result = manager.remove_record(zone="example.com", record=record)
    testbase.assert_equal(
        result.status, UbuntuDnsActionStatus.SUCCESS,
    )
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    new_body = tee_calls[0]["stdin"]
    testbase.assert_equal(
        "203.0.113.5" in new_body, False,
    )
    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["rndc", "reload", "example.com"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    return


# ----------------------------------------------------------------------
# list_records
# ----------------------------------------------------------------------


def test_list_records_parses_zone_file_output():
    """
        list_records parses the ``cat`` output for A, AAAA,
        CNAME, and TXT records.
    """
    runner = _MockCommandRunner()
    body_lines: list = []
    body_lines.append("$TTL 3600")
    body_lines.append(
        "@ IN SOA ns1.example.com. hostmaster.example.com. (",
    )
    body_lines.append("    2024042400        ; Serial")
    body_lines.append("    3600              ; Refresh")
    body_lines.append("    600               ; Retry")
    body_lines.append("    1209600           ; Expire")
    body_lines.append("    86400 )           ; Minimum")
    body_lines.append("@ IN NS ns1.example.com.")
    body_lines.append("www IN A 203.0.113.5")
    body_lines.append("ipv6 IN AAAA 2001:db8::1")
    body_lines.append("alias IN CNAME www")
    body_lines.append("@ IN TXT \"v=spf1 -all\"")
    body_text = "\n".join(body_lines) + "\n"

    _seed_cat(
        runner,
        path="/etc/bind/zones/db.example.com",
        body=body_text,
    )

    manager = UbuntuDnsServerManager(
        runner=runner, config=UbuntuDnsManagerConfig(),
    )
    records = manager.list_records(zone="example.com")
    by_type: dict = {}
    for record in records:
        by_type.setdefault(record.record_type, []).append(record)

    testbase.assert_equal("A" in by_type, True)
    testbase.assert_equal(by_type["A"][0].name, "www")
    testbase.assert_equal(by_type["A"][0].value, "203.0.113.5")
    testbase.assert_equal("AAAA" in by_type, True)
    testbase.assert_equal(by_type["AAAA"][0].value, "2001:db8::1")
    testbase.assert_equal("CNAME" in by_type, True)
    testbase.assert_equal("TXT" in by_type, True)
    return


# ----------------------------------------------------------------------
# reload_zone
# ----------------------------------------------------------------------


def test_reload_zone_invokes_rndc():
    """
        reload_zone dispatches ``rndc reload <zone>``
        unconditionally.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    config = UbuntuDnsManagerConfig(elevated=True)
    manager = UbuntuDnsServerManager(runner=runner, config=config)
    result = manager.reload_zone(name="example.com")
    testbase.assert_equal(
        result.status, UbuntuDnsActionStatus.SUCCESS,
    )
    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["rndc", "reload", "example.com"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    testbase.assert_equal(reload_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# get_zone_status
# ----------------------------------------------------------------------


def test_get_zone_status_parses_rndc_output():
    """
        get_zone_status parses the ``rndc zonestatus`` output
        for is_loaded / serial / last_loaded.
    """
    runner = _MockCommandRunner()
    runner.results[("rndc", "zonestatus", "example.com")] = RunResult(
        exit_code=0,
        stdout=(
            "name: example.com\n"
            "type: master\n"
            "files: /etc/bind/zones/db.example.com\n"
            "serial: 2024042401\n"
            "loaded: Wed, 24 Apr 2024 12:00:00 GMT\n"
        ),
        stderr="",
    )

    manager = UbuntuDnsServerManager(
        runner=runner, config=UbuntuDnsManagerConfig(),
    )
    status = manager.get_zone_status(name="example.com")
    testbase.assert_equal(
        isinstance(status, UbuntuDnsZoneStatus), True,
    )
    testbase.assert_equal(status.is_loaded, True)
    testbase.assert_equal(status.serial, 2024042401)
    testbase.assert_equal(
        "Wed, 24 Apr 2024" in (status.last_loaded or ""), True,
    )
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
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    _seed_cat(runner, path="/etc/bind/named.conf.local", body="")
    _seed_test_dash_e(
        runner,
        path="/etc/bind/zones/db.example.com",
        exists=False,
    )

    config = UbuntuDnsManagerConfig(elevated=True)
    manager = UbuntuDnsServerManager(runner=runner, config=config)
    spec = UbuntuDnsZoneSpec(name="example.com")
    manager.add_zone(name="example.com", spec=spec)

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls) >= 1, True)
    for call in tee_calls:
        testbase.assert_equal(call["elevated"], True)

    rndc_calls = [
        call for call in runner.calls
        if call["args"] == ["rndc", "reconfig"]
    ]
    testbase.assert_equal(len(rndc_calls), 1)
    testbase.assert_equal(rndc_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Reads (``cat``, ``test -e``, ``named-checkconf -p``,
        ``rndc zonestatus``) never elevate.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    config = UbuntuDnsManagerConfig(elevated=True)
    manager = UbuntuDnsServerManager(runner=runner, config=config)

    runner.results[("named-checkconf", "-p")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[("rndc", "zonestatus", "example.com")] = RunResult(
        exit_code=0,
        stdout="name: example.com\n",
        stderr="",
    )

    manager.list_zones()
    manager.list_records(zone="example.com")
    manager.get_zone_status(name="example.com")

    read_argv0_first = ("cat", "test", "named-checkconf")
    for call in runner.calls:
        argv0 = call["args"][0]
        if argv0 in read_argv0_first:
            testbase.assert_equal(call["elevated"], False)
        if (
            len(call["args"]) >= 2
            and call["args"][0] == "rndc"
            and call["args"][1] == "zonestatus"
        ):
            testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        When the underlying tee returns non-zero, the manager
        surfaces the exit code and stderr without invoking
        ``rndc reconfig``.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path="/etc/bind/named.conf.local", body="")
    _seed_test_dash_e(
        runner,
        path="/etc/bind/zones/db.example.com",
        exists=False,
    )
    runner.results[("tee", "/etc/bind/named.conf.local")] = RunResult(
        exit_code=2,
        stdout="",
        stderr="permission denied",
    )

    manager = UbuntuDnsServerManager(
        runner=runner, config=UbuntuDnsManagerConfig(),
    )
    spec = UbuntuDnsZoneSpec(name="example.com")
    result = manager.add_zone(name="example.com", spec=spec)
    testbase.assert_equal(
        result.status, UbuntuDnsActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.stderr, "permission denied")
    rndc_calls = [
        call for call in runner.calls
        if call["args"] == ["rndc", "reconfig"]
    ]
    testbase.assert_equal(len(rndc_calls), 0)
    return


def test_serial_bump_on_record_change():
    """
        After ``add_record`` the body written via tee carries an
        SOA serial strictly greater than the original.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    starting_body_lines: list = []
    starting_body_lines.append("$TTL 3600")
    starting_body_lines.append(
        "@ IN SOA ns1.example.com. hostmaster.example.com. (",
    )
    starting_body_lines.append("    1990010100        ; Serial")
    starting_body_lines.append("    3600              ; Refresh")
    starting_body_lines.append("    600               ; Retry")
    starting_body_lines.append("    1209600           ; Expire")
    starting_body_lines.append("    86400 )           ; Minimum")
    starting_body_lines.append("@ IN NS ns1.example.com.")
    starting_body = "\n".join(starting_body_lines) + "\n"
    _seed_cat(
        runner,
        path="/etc/bind/zones/db.example.com",
        body=starting_body,
    )

    record = UbuntuDnsRecordSpec(
        name="www", record_type="A", value="203.0.113.5",
    )
    manager = UbuntuDnsServerManager(
        runner=runner, config=UbuntuDnsManagerConfig(),
    )
    manager.add_record(zone="example.com", record=record)

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    new_body = tee_calls[0]["stdin"]
    testbase.assert_equal("1990010100" in new_body, False)
    return


def test_list_zones_raises_on_unexpected_failure():
    """
        list_zones raises UbuntuDnsQueryError when
        ``named-checkconf -p`` exits non-zero with non-empty
        stderr.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path="/etc/bind/named.conf.local", body="")
    runner.results[("named-checkconf", "-p")] = RunResult(
        exit_code=1,
        stdout="",
        stderr="syntax error in named.conf",
    )

    manager = UbuntuDnsServerManager(
        runner=runner, config=UbuntuDnsManagerConfig(),
    )
    raised = False
    try:
        manager.list_zones()
    except UbuntuDnsQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_render_record_line_supports_priority():
    """
        The renderer emits the priority field as the leading
        rdata token for MX records.
    """
    record = UbuntuDnsRecordSpec(
        name="@",
        record_type="MX",
        value="mail.example.com.",
        priority=10,
    )
    line = _render_record_line(record=record)
    testbase.assert_equal(
        "@ IN MX 10 mail.example.com." in line, True,
    )
    return


def test_parse_zone_records_round_trip():
    """
        Records survive a render-then-parse cycle.
    """
    record = UbuntuDnsRecordSpec(
        name="www", record_type="A", value="203.0.113.5", ttl=300,
    )
    body = (
        _render_initial_zone_file(name="example.com")
        + _render_record_line(record=record) + "\n"
    )
    parsed = _parse_zone_records(content=body)
    found = False
    for entry in parsed:
        if (
            entry.name == "www"
            and entry.record_type == "A"
            and entry.value == "203.0.113.5"
        ):
            found = True
    testbase.assert_equal(found, True)
    return


def test_bump_soa_serial_increments_when_today():
    """
        _bump_soa_serial increments the existing serial when it
        already targets today's date.
    """
    body = (
        "@ IN SOA ns1.example.com. hostmaster.example.com. (\n"
        "    9999999999      ; Serial\n"
        "    3600            ; Refresh\n"
        "    600             ; Retry\n"
        "    1209600         ; Expire\n"
        "    86400 )         ; Minimum\n"
    )
    new_body = _bump_soa_serial(content=body)
    testbase.assert_equal("9999999999" in new_body, False)
    return
