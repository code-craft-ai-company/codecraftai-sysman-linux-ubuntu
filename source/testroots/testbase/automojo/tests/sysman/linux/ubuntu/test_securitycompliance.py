"""
    Tests for :class:`UbuntuSecurityComplianceManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``oscap`` is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.securitycompliance import (
    UbuntuBaselineInfo,
    UbuntuComplianceFinding,
    UbuntuComplianceProfile,
    UbuntuComplianceReport,
    UbuntuSecurityComplianceActionStatus,
    UbuntuSecurityComplianceManager,
    UbuntuSecurityComplianceManagerConfig,
    UbuntuSecurityComplianceQueryError,
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


_DATASTREAM_PATH = "/usr/share/xml/scap/ssg/content/ssg-ubuntu2204-ds.xml"


def _make_config(
    *,
    dry_run: bool = False,
    elevated: bool = False,
) -> UbuntuSecurityComplianceManagerConfig:
    """
        Build a manager config pinned to the default datastream and a
        deterministic output directory.
    """
    rtnval = UbuntuSecurityComplianceManagerConfig(
        dry_run=dry_run,
        elevated=elevated,
        datastream_path=_DATASTREAM_PATH,
        output_dir="/var/lib/automojo/compliance",
    )
    return rtnval


# ----------------------------------------------------------------------
# list_profiles
# ----------------------------------------------------------------------


def test_list_profiles_constructs_oscap_info_command():
    """
        list_profiles runs ``oscap info <datastream>`` without
        elevation.
    """
    runner = _MockCommandRunner()
    runner.results[("oscap", "info", _DATASTREAM_PATH)] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(),
    )
    manager.list_profiles()
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"], ["oscap", "info", _DATASTREAM_PATH],
    )
    testbase.assert_equal(last_call["elevated"], False)
    return


def test_list_profiles_parses_titles_and_ids():
    """
        list_profiles extracts each Title/Id pair from the Profiles
        section.
    """
    payload = (
        "Document type: Source Data Stream\n"
        "Imported: 2024-04-01\n"
        "Profiles:\n"
        "  Title: CIS Ubuntu 22.04 Level 1 Server Benchmark\n"
        "    Id: xccdf_org.ssgproject.content_profile_cis_level1_server\n"
        "  Title: CIS Ubuntu 22.04 Level 2 Server Benchmark\n"
        "    Id: xccdf_org.ssgproject.content_profile_cis_level2_server\n"
        "Referenced check files: ...\n"
    )
    runner = _MockCommandRunner()
    runner.results[("oscap", "info", _DATASTREAM_PATH)] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(),
    )
    profiles = manager.list_profiles()
    testbase.assert_equal(len(profiles), 2)
    testbase.assert_equal(
        profiles[0].id,
        "xccdf_org.ssgproject.content_profile_cis_level1_server",
    )
    testbase.assert_equal(
        profiles[0].title,
        "CIS Ubuntu 22.04 Level 1 Server Benchmark",
    )
    testbase.assert_equal(isinstance(profiles[0], UbuntuComplianceProfile), True)
    testbase.assert_equal(
        profiles[1].id,
        "xccdf_org.ssgproject.content_profile_cis_level2_server",
    )
    return


def test_list_profiles_raises_on_error():
    """
        A non-zero exit with stderr raises
        UbuntuSecurityComplianceQueryError.
    """
    runner = _MockCommandRunner()
    runner.results[("oscap", "info", _DATASTREAM_PATH)] = RunResult(
        exit_code=1, stdout="", stderr="missing datastream",
    )
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(),
    )
    raised = False
    try:
        manager.list_profiles()
    except UbuntuSecurityComplianceQueryError:
        raised = True
    testbase.assert_true(raised)
    return


# ----------------------------------------------------------------------
# run_compliance_profile
# ----------------------------------------------------------------------


def _seed_eval_result(
    *,
    runner: _MockCommandRunner,
    profile: str,
    payload: str,
    exit_code: int = 0,
) -> None:
    """
        Seed the runner with an oscap eval result regardless of the
        timestamped results / report paths.  We match purely on the
        leading argv prefix at runtime by overriding ``run``.
    """
    original_run = runner.run

    def _patched_run(
        args: list,
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output=None,
        on_error=None,
        capture_output: bool = True,
    ) -> RunResult:
        if (
            len(args) >= 5
            and args[0] == "oscap"
            and args[1] == "xccdf"
            and args[2] == "eval"
            and args[3] == "--profile"
            and args[4] == profile
            and "--remediate" not in args
        ):
            runner.calls.append({
                "args": list(args),
                "elevated": elevated,
                "stdin": stdin,
                "env": env,
            })
            rtnval = RunResult(
                exit_code=exit_code, stdout=payload, stderr="",
            )
            return rtnval
        rtnval = original_run(
            args,
            elevated=elevated,
            cwd=cwd,
            env=env,
            stdin=stdin,
            timeout=timeout,
            on_output=on_output,
            on_error=on_error,
            capture_output=capture_output,
        )
        return rtnval

    runner.run = _patched_run
    return


def test_run_compliance_profile_constructs_eval_command():
    """
        run_compliance_profile dispatches ``oscap xccdf eval --profile
        ... --results ... --report ... <datastream>`` with elevation
        when configured.
    """
    runner = _MockCommandRunner()
    profile_name = (
        "xccdf_org.ssgproject.content_profile_cis_level1_server"
    )
    _seed_eval_result(
        runner=runner,
        profile=profile_name,
        payload="",
    )
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(elevated=True),
    )
    report = manager.run_compliance_profile(profile_name=profile_name)
    eval_calls = [
        c for c in runner.calls
        if len(c["args"]) >= 4 and c["args"][:3] == [
            "oscap", "xccdf", "eval",
        ] and "--remediate" not in c["args"]
    ]
    testbase.assert_equal(len(eval_calls), 1)
    args = eval_calls[0]["args"]
    testbase.assert_equal(args[3], "--profile")
    testbase.assert_equal(args[4], profile_name)
    testbase.assert_true("--results" in args)
    testbase.assert_true("--report" in args)
    testbase.assert_equal(args[-1], _DATASTREAM_PATH)
    testbase.assert_equal(eval_calls[0]["elevated"], True)
    testbase.assert_equal(isinstance(report, UbuntuComplianceReport), True)
    return


def test_run_compliance_profile_accepts_exit_two():
    """
        oscap exit code 2 (rules failed) is treated as a successful
        evaluation.
    """
    runner = _MockCommandRunner()
    profile_name = "xccdf_profile_x"
    payload = (
        "Title\tEnsure password complexity\n"
        "Rule\txccdf_rule_password_complexity\n"
        "Result\tfail\n"
    )
    _seed_eval_result(
        runner=runner, profile=profile_name, payload=payload, exit_code=2,
    )
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(),
    )
    report = manager.run_compliance_profile(profile_name=profile_name)
    testbase.assert_equal(len(report.findings), 1)
    finding = report.findings[0]
    testbase.assert_equal(finding.status, "fail")
    testbase.assert_equal(finding.rule_id, "xccdf_rule_password_complexity")
    testbase.assert_equal(report.counts.get("fail"), 1)
    return


def test_run_compliance_profile_raises_on_unexpected_exit():
    """
        Exit codes other than 0 or 2 raise
        UbuntuSecurityComplianceQueryError.
    """
    runner = _MockCommandRunner()
    profile_name = "xccdf_profile_x"
    _seed_eval_result(
        runner=runner, profile=profile_name, payload="", exit_code=5,
    )
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(),
    )
    raised = False
    try:
        manager.run_compliance_profile(profile_name=profile_name)
    except UbuntuSecurityComplianceQueryError:
        raised = True
    testbase.assert_true(raised)
    return


def test_run_compliance_profile_parses_multiple_findings():
    """
        Multiple Title/Rule/Result groups produce multiple findings.
    """
    runner = _MockCommandRunner()
    profile_name = "xccdf_profile_x"
    payload = (
        "Title\tEnsure SSH PermitRootLogin is no\n"
        "Rule\txccdf_rule_sshd_disable_root_login\n"
        "Ident\tCCE-12345-6\n"
        "Result\tpass\n"
        "\n"
        "Title\tEnsure auditd is installed\n"
        "Rule\txccdf_rule_package_audit_installed\n"
        "Result\tfail\n"
        "\n"
        "Title\tEnsure FIPS\n"
        "Rule\txccdf_rule_fips\n"
        "Result\tnotapplicable\n"
    )
    _seed_eval_result(
        runner=runner, profile=profile_name, payload=payload, exit_code=0,
    )
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(),
    )
    report = manager.run_compliance_profile(profile_name=profile_name)
    testbase.assert_equal(len(report.findings), 3)
    testbase.assert_equal(report.findings[0].status, "pass")
    testbase.assert_equal(report.findings[1].status, "fail")
    testbase.assert_equal(report.findings[2].status, "notapplicable")
    testbase.assert_equal(report.counts.get("pass"), 1)
    testbase.assert_equal(report.counts.get("fail"), 1)
    testbase.assert_equal(report.counts.get("notapplicable"), 1)
    return


def test_compliance_finding_dataclass_fields():
    """
        UbuntuComplianceFinding carries the seven fields the contract
        requires.
    """
    finding = UbuntuComplianceFinding(
        rule_id="x",
        severity="medium",
        title="t",
        description=None,
        status="pass",
        remediation_available=False,
        remediation_automated=False,
    )
    testbase.assert_equal(finding.rule_id, "x")
    testbase.assert_equal(finding.severity, "medium")
    testbase.assert_equal(finding.status, "pass")
    return


# ----------------------------------------------------------------------
# remediate
# ----------------------------------------------------------------------


def test_remediate_constructs_command_with_profile_and_rule():
    """
        remediate parses ``<profile>:<rule>`` and dispatches the
        targeted oscap remediation command.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(elevated=True),
    )
    finding_id = (
        "xccdf_org.ssgproject.content_profile_cis_level1_server"
        ":xccdf_rule_sshd_disable_root_login"
    )
    result = manager.remediate(finding_id=finding_id)
    testbase.assert_equal(
        result.status, UbuntuSecurityComplianceActionStatus.SUCCESS,
    )
    testbase.assert_equal(
        runner.calls[-1]["args"],
        [
            "oscap", "xccdf", "eval",
            "--remediate",
            "--profile",
            "xccdf_org.ssgproject.content_profile_cis_level1_server",
            "--rule", "xccdf_rule_sshd_disable_root_login",
            _DATASTREAM_PATH,
        ],
    )
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    return


def test_remediate_dry_run_short_circuits():
    """
        remediate with dry_run=True returns DRY_RUN without invoking
        the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(dry_run=True),
    )
    result = manager.remediate(finding_id="profile:rule")
    testbase.assert_equal(
        result.status, UbuntuSecurityComplianceActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_remediate_failed_when_profile_missing():
    """
        A finding_id without a profile prefix returns FAILED without
        invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(),
    )
    result = manager.remediate(finding_id="bare_rule_id")
    testbase.assert_equal(
        result.status, UbuntuSecurityComplianceActionStatus.FAILED,
    )
    testbase.assert_equal(result.message, "missing_profile_in_finding_id")
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_remediate_failed_propagates_stderr():
    """
        A non-zero exit (other than 2) maps to FAILED with stderr
        captured.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=1, stdout="", stderr="cannot remediate",
    )
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(),
    )
    result = manager.remediate(finding_id="profile:rule")
    testbase.assert_equal(
        result.status, UbuntuSecurityComplianceActionStatus.FAILED,
    )
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "cannot remediate")
    return


# ----------------------------------------------------------------------
# get_baseline / set_baseline
# ----------------------------------------------------------------------


def test_get_baseline_returns_none_when_file_missing():
    """
        get_baseline returns None when the baseline file does not
        exist.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "cat", "/var/lib/automojo/compliance/current_baseline.json",
    )] = RunResult(exit_code=1, stdout="", stderr="")
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(),
    )
    baseline = manager.get_baseline()
    testbase.assert_equal(baseline, None)
    return


def test_get_baseline_decodes_json_payload():
    """
        get_baseline parses the on-disk JSON into UbuntuBaselineInfo.
    """
    runner = _MockCommandRunner()
    payload = json.dumps({
        "profile": "xccdf_profile_x",
        "applied_at": "2024-04-01T00:00:00Z",
        "datastream_path": _DATASTREAM_PATH,
    })
    runner.results[(
        "cat", "/var/lib/automojo/compliance/current_baseline.json",
    )] = RunResult(exit_code=0, stdout=payload, stderr="")
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(),
    )
    baseline = manager.get_baseline()
    testbase.assert_equal(isinstance(baseline, UbuntuBaselineInfo), True)
    testbase.assert_equal(baseline.profile, "xccdf_profile_x")
    testbase.assert_equal(baseline.applied_at, "2024-04-01T00:00:00Z")
    testbase.assert_equal(baseline.datastream_path, _DATASTREAM_PATH)
    return


def test_set_baseline_writes_json_via_tee():
    """
        set_baseline writes the rendered JSON via ``tee``.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "cat", "/var/lib/automojo/compliance/current_baseline.json",
    )] = RunResult(exit_code=1, stdout="", stderr="")
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(elevated=True),
    )
    spec = UbuntuBaselineInfo(
        profile="xccdf_profile_x",
        applied_at="2024-04-01T00:00:00Z",
        datastream_path=_DATASTREAM_PATH,
    )
    result = manager.set_baseline(spec=spec)
    testbase.assert_equal(
        result.status, UbuntuSecurityComplianceActionStatus.SUCCESS,
    )
    write_calls = [
        c for c in runner.calls if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["tee", "/var/lib/automojo/compliance/current_baseline.json"],
    )
    body = write_calls[0]["stdin"]
    decoded = json.loads(body)
    testbase.assert_equal(decoded["profile"], "xccdf_profile_x")
    testbase.assert_equal(decoded["datastream_path"], _DATASTREAM_PATH)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_set_baseline_skipped_when_content_matches():
    """
        set_baseline returns SKIPPED when the existing JSON already
        matches the rendered desired content.
    """
    runner = _MockCommandRunner()
    spec = UbuntuBaselineInfo(
        profile="xccdf_profile_x",
        applied_at="2024-04-01T00:00:00Z",
        datastream_path=_DATASTREAM_PATH,
    )
    expected_body = (
        json.dumps(
            {
                "applied_at": "2024-04-01T00:00:00Z",
                "datastream_path": _DATASTREAM_PATH,
                "profile": "xccdf_profile_x",
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    runner.results[(
        "cat", "/var/lib/automojo/compliance/current_baseline.json",
    )] = RunResult(exit_code=0, stdout=expected_body, stderr="")
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(),
    )
    result = manager.set_baseline(spec=spec)
    testbase.assert_equal(
        result.status, UbuntuSecurityComplianceActionStatus.SKIPPED,
    )
    write_calls = [
        c for c in runner.calls if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_set_baseline_dry_run_short_circuits():
    """
        set_baseline dry-run returns DRY_RUN without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(dry_run=True),
    )
    spec = UbuntuBaselineInfo(
        profile="x", applied_at="t", datastream_path=_DATASTREAM_PATH,
    )
    result = manager.set_baseline(spec=spec)
    testbase.assert_equal(
        result.status, UbuntuSecurityComplianceActionStatus.DRY_RUN,
    )
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_reads_never_elevate():
    """
        list_profiles and get_baseline never pass ``elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(elevated=True),
    )
    manager.list_profiles()
    manager.get_baseline()
    elevated_reads = [c for c in runner.calls if c["elevated"] is True]
    testbase.assert_equal(elevated_reads, [])
    return


def test_writes_pass_elevated_when_configured():
    """
        Remediation propagates ``elevated=True`` when the config
        enables elevation.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    manager = UbuntuSecurityComplianceManager(
        runner=runner, config=_make_config(elevated=True),
    )
    manager.remediate(finding_id="profile:rule")
    last_call = runner.calls[-1]
    testbase.assert_equal(last_call["elevated"], True)
    return
