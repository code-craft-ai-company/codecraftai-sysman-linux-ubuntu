"""
    Tests for :class:`UbuntuAuditManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``auditctl`` / ``ausearch`` / ``augenrules`` is
    invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.auditmanagement import (
    UbuntuAuditActionStatus,
    UbuntuAuditEvent,
    UbuntuAuditEventQuery,
    UbuntuAuditManager,
    UbuntuAuditManagerConfig,
    UbuntuAuditQueryError,
    UbuntuAuditRuleInfo,
    UbuntuAuditRuleSpec,
    UbuntuAuditStatus,
    _parse_auditctl_list,
    _parse_auditctl_status,
    _parse_ausearch_output,
    _render_auditctl_args,
)


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


def _seed_no_rules(runner: _MockCommandRunner) -> None:
    """
        Pre-seed the runner so that ``auditctl -l`` reports no rules
        and the manager-owned drop-in is empty.
    """
    runner.results[("auditctl", "-l")] = RunResult(
        exit_code=0, stdout="No rules\n", stderr="",
    )
    runner.results[("cat", "/etc/audit/rules.d/99-automojo.rules")] = (
        RunResult(exit_code=1, stdout="", stderr="")
    )
    return


def _seed_auditctl_status(
    runner: _MockCommandRunner,
    *,
    enabled: int,
    pid: int,
    locked: bool = False,
) -> None:
    """
        Pre-seed the runner so that ``auditctl -s`` returns a valid
        status block.  When ``locked`` is True the enabled value is
        forced to 2 (immutable mode).
    """
    if locked:
        enabled_value = 2
    else:
        enabled_value = enabled
    body_lines = [
        "enabled {}".format(enabled_value),
        "failure 1",
        "pid {}".format(pid),
        "rate_limit 0",
        "backlog_limit 8192",
        "lost 0",
        "backlog 0",
    ]
    body = "\n".join(body_lines) + "\n"
    runner.results[("auditctl", "-s")] = RunResult(
        exit_code=0, stdout=body, stderr="",
    )
    return


# ----------------------------------------------------------------------
# add_rule
# ----------------------------------------------------------------------


def test_add_rule_invokes_auditctl():
    """
        add_rule renders the spec and dispatches the canonical
        ``auditctl -w <path> -p <perms> -k <key>`` argv via the
        runner.
    """
    runner = _MockCommandRunner()
    _seed_no_rules(runner=runner)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(elevated=True),
    )
    spec = UbuntuAuditRuleSpec(
        key="passwd_changes",
        watch_path="/etc/passwd",
        permissions="wa",
        persistent=False,
    )
    result = manager.add_rule(spec=spec)

    testbase.assert_equal(result.status, UbuntuAuditActionStatus.SUCCESS)
    add_calls = [
        c for c in runner.calls if c["args"][:1] == ["auditctl"]
        and "-w" in c["args"]
    ]
    testbase.assert_equal(len(add_calls), 1)
    testbase.assert_equal(
        add_calls[0]["args"],
        ["auditctl", "-w", "/etc/passwd", "-p", "wa", "-k", "passwd_changes"],
    )
    testbase.assert_equal(add_calls[0]["elevated"], True)
    return


def test_add_rule_skipped_when_present():
    """
        add_rule returns SKIPPED when ``auditctl -l`` already reports
        a rule with the same key.
    """
    runner = _MockCommandRunner()
    runner.results[("auditctl", "-l")] = RunResult(
        exit_code=0,
        stdout="-w /etc/passwd -p wa -k passwd_changes\n",
        stderr="",
    )
    runner.results[("cat", "/etc/audit/rules.d/99-automojo.rules")] = (
        RunResult(exit_code=1, stdout="", stderr="")
    )
    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(),
    )
    spec = UbuntuAuditRuleSpec(
        key="passwd_changes",
        watch_path="/etc/passwd",
        permissions="wa",
        persistent=False,
    )
    result = manager.add_rule(spec=spec)

    testbase.assert_equal(result.status, UbuntuAuditActionStatus.SKIPPED)
    invoked_runtime = [
        c for c in runner.calls
        if c["args"][:2] == ["auditctl", "-w"]
    ]
    testbase.assert_equal(invoked_runtime, [])
    return


def test_add_rule_dry_run_short_circuits():
    """
        add_rule with dry_run=True returns DRY_RUN and never dispatches
        the runtime auditctl invocation.
    """
    runner = _MockCommandRunner()
    manager = UbuntuAuditManager(
        runner=runner,
        config=UbuntuAuditManagerConfig(dry_run=True),
    )
    spec = UbuntuAuditRuleSpec(
        key="passwd_changes",
        watch_path="/etc/passwd",
        permissions="wa",
    )
    result = manager.add_rule(spec=spec)

    testbase.assert_equal(result.status, UbuntuAuditActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_persistent_rule_writes_to_rules_d():
    """
        add_rule with persistent=True appends the rule line to the
        manager-owned drop-in via tee and then calls augenrules --load.
    """
    runner = _MockCommandRunner()
    _seed_no_rules(runner=runner)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(),
    )
    spec = UbuntuAuditRuleSpec(
        key="passwd_changes",
        watch_path="/etc/passwd",
        permissions="wa",
        persistent=True,
    )
    manager.add_rule(spec=spec)

    tee_calls = [c for c in runner.calls if c["args"][:1] == ["tee"]]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"],
        ["tee", "/etc/audit/rules.d/99-automojo.rules"],
    )
    testbase.assert_true("/etc/passwd" in tee_calls[0]["stdin"])
    testbase.assert_true("passwd_changes" in tee_calls[0]["stdin"])

    reload_calls = [
        c for c in runner.calls if c["args"][:1] == ["augenrules"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    testbase.assert_equal(
        reload_calls[0]["args"], ["augenrules", "--load"],
    )
    return


# ----------------------------------------------------------------------
# remove_rule
# ----------------------------------------------------------------------


def test_remove_rule_invokes_auditctl():
    """
        remove_rule dispatches ``auditctl -d -k <rule_id>`` for an
        existing rule.
    """
    runner = _MockCommandRunner()
    runner.results[("auditctl", "-l")] = RunResult(
        exit_code=0,
        stdout="-w /etc/passwd -p wa -k passwd_changes\n",
        stderr="",
    )
    runner.results[("cat", "/etc/audit/rules.d/99-automojo.rules")] = (
        RunResult(exit_code=1, stdout="", stderr="")
    )
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(),
    )
    result = manager.remove_rule(rule_id="passwd_changes")

    testbase.assert_equal(result.status, UbuntuAuditActionStatus.SUCCESS)
    delete_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["auditctl", "-d", "-k"]
    ]
    testbase.assert_equal(len(delete_calls), 1)
    testbase.assert_equal(
        delete_calls[0]["args"],
        ["auditctl", "-d", "-k", "passwd_changes"],
    )
    return


def test_remove_rule_skipped_when_absent():
    """
        remove_rule returns SKIPPED when the rule is present in
        neither the runtime rule set nor the drop-in.
    """
    runner = _MockCommandRunner()
    _seed_no_rules(runner=runner)

    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(),
    )
    result = manager.remove_rule(rule_id="not_there")

    testbase.assert_equal(result.status, UbuntuAuditActionStatus.SKIPPED)
    invoked = [
        c for c in runner.calls
        if c["args"][:3] == ["auditctl", "-d", "-k"]
    ]
    testbase.assert_equal(invoked, [])
    return


# ----------------------------------------------------------------------
# list_rules
# ----------------------------------------------------------------------


def test_list_rules_parses_output():
    """
        list_rules parses ``auditctl -l`` output into
        UbuntuAuditRuleInfo rows and includes any drop-in entries
        not yet applied at runtime.
    """
    runner = _MockCommandRunner()
    runner.results[("auditctl", "-l")] = RunResult(
        exit_code=0,
        stdout=(
            "-w /etc/passwd -p wa -k passwd_changes\n"
            "-a always,exit -F arch=b64 -S execve -k execve_calls\n"
        ),
        stderr="",
    )
    runner.results[("cat", "/etc/audit/rules.d/99-automojo.rules")] = (
        RunResult(
            exit_code=0,
            stdout=(
                "-w /etc/shadow -p wa -k shadow_changes\n"
                "# a comment line\n"
            ),
            stderr="",
        )
    )

    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(),
    )
    rules = manager.list_rules()

    testbase.assert_equal(len(rules), 3)
    keys = [r.key for r in rules]
    testbase.assert_true("passwd_changes" in keys)
    testbase.assert_true("execve_calls" in keys)
    testbase.assert_true("shadow_changes" in keys)

    # The runtime path for the first rule is reported with no source.
    runtime_rule = [r for r in rules if r.key == "passwd_changes"][0]
    testbase.assert_equal(runtime_rule.source, None)
    testbase.assert_equal(runtime_rule.subsystem, "path")

    # The drop-in-only rule has a source path.
    dropin_rule = [r for r in rules if r.key == "shadow_changes"][0]
    testbase.assert_equal(
        dropin_rule.source, "/etc/audit/rules.d/99-automojo.rules",
    )
    return


# ----------------------------------------------------------------------
# enable / disable
# ----------------------------------------------------------------------


def test_enable_auditing_invokes_auditctl():
    """
        enable_auditing dispatches ``auditctl -e 1`` and
        ``systemctl enable --now auditd``.
    """
    runner = _MockCommandRunner()
    _seed_auditctl_status(runner=runner, enabled=0, pid=0)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(elevated=True),
    )
    result = manager.enable_auditing()

    testbase.assert_equal(result.status, UbuntuAuditActionStatus.SUCCESS)
    primary_calls = [
        c for c in runner.calls
        if c["args"] == ["auditctl", "-e", "1"]
    ]
    testbase.assert_equal(len(primary_calls), 1)
    testbase.assert_equal(primary_calls[0]["elevated"], True)

    service_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["systemctl"]
    ]
    testbase.assert_equal(
        service_calls[0]["args"],
        ["systemctl", "enable", "--now", "auditd"],
    )
    return


def test_enable_auditing_skipped_when_enabled():
    """
        enable_auditing returns SKIPPED when ``auditctl -s`` already
        reports the subsystem enabled and running.
    """
    runner = _MockCommandRunner()
    _seed_auditctl_status(runner=runner, enabled=1, pid=1234)

    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(),
    )
    result = manager.enable_auditing()

    testbase.assert_equal(result.status, UbuntuAuditActionStatus.SKIPPED)
    invoked_enable = [
        c for c in runner.calls
        if c["args"] == ["auditctl", "-e", "1"]
    ]
    testbase.assert_equal(invoked_enable, [])
    return


def test_disable_auditing_invokes_auditctl():
    """
        disable_auditing dispatches ``auditctl -e 0`` and
        ``systemctl disable --now auditd``.
    """
    runner = _MockCommandRunner()
    _seed_auditctl_status(runner=runner, enabled=1, pid=1234)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(elevated=True),
    )
    result = manager.disable_auditing()

    testbase.assert_equal(result.status, UbuntuAuditActionStatus.SUCCESS)
    primary_calls = [
        c for c in runner.calls
        if c["args"] == ["auditctl", "-e", "0"]
    ]
    testbase.assert_equal(len(primary_calls), 1)

    service_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["systemctl"]
    ]
    testbase.assert_equal(
        service_calls[0]["args"],
        ["systemctl", "disable", "--now", "auditd"],
    )
    return


# ----------------------------------------------------------------------
# get_audit_status
# ----------------------------------------------------------------------


def test_get_audit_status_parses_auditctl_output():
    """
        get_audit_status parses ``auditctl -s`` output into an
        UbuntuAuditStatus dataclass.
    """
    runner = _MockCommandRunner()
    body = (
        "enabled 1\n"
        "failure 1\n"
        "pid 4321\n"
        "rate_limit 0\n"
        "backlog_limit 8192\n"
        "lost 0\n"
        "backlog 5\n"
    )
    runner.results[("auditctl", "-s")] = RunResult(
        exit_code=0, stdout=body, stderr="",
    )

    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(),
    )
    status = manager.get_audit_status()

    testbase.assert_equal(isinstance(status, UbuntuAuditStatus), True)
    testbase.assert_equal(status.enabled, True)
    testbase.assert_equal(status.running, True)
    testbase.assert_equal(status.pid, 4321)
    testbase.assert_equal(status.failure_mode, "1")
    testbase.assert_equal(status.backlog, 5)
    testbase.assert_equal(status.locked, False)
    return


def test_get_audit_status_detects_locked_mode():
    """
        get_audit_status flags ``enabled 2`` as immutable (locked).
    """
    runner = _MockCommandRunner()
    runner.results[("auditctl", "-s")] = RunResult(
        exit_code=0,
        stdout="enabled 2\npid 1234\n",
        stderr="",
    )

    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(),
    )
    status = manager.get_audit_status()

    testbase.assert_equal(status.locked, True)
    testbase.assert_equal(status.enabled, True)
    return


# ----------------------------------------------------------------------
# search_events
# ----------------------------------------------------------------------


def test_search_events_invokes_ausearch_with_query():
    """
        search_events builds an ausearch argv from the structured
        query.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(exit_code=1, stdout="", stderr="")

    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(),
    )
    query = UbuntuAuditEventQuery(
        key="passwd_changes",
        message_type="SYSCALL",
        since="recent",
    )
    manager.search_events(query=query)

    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        [
            "ausearch", "-k", "passwd_changes",
            "-m", "SYSCALL", "-ts", "recent",
        ],
    )
    testbase.assert_equal(last_call["elevated"], False)
    return


def test_search_events_parses_record_stream():
    """
        search_events parses the dashed record stream into
        UbuntuAuditEvent rows and respects the ``limit`` cap.
    """
    runner = _MockCommandRunner()
    payload = (
        "----\n"
        "time->Mon Apr 22 10:00:00 2024\n"
        "type=SYSCALL msg=audit(1713780000.123:42): "
        "arch=c000003e syscall=2 success=yes exit=3 a0=7f\n"
        "type=PATH msg=audit(1713780000.123:42): item=0 name=/etc/passwd\n"
        "----\n"
        "time->Mon Apr 22 10:00:01 2024\n"
        "type=USER_LOGIN msg=audit(1713780001.456:43): "
        "pid=1234 uid=0 ses=1 msg='op=login id=1000 res=success'\n"
    )
    runner.default_result = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )

    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(),
    )
    query = UbuntuAuditEventQuery(limit=5)
    events = manager.search_events(query=query)

    testbase.assert_equal(len(events), 2)
    testbase.assert_equal(events[0].category, "SYSCALL")
    testbase.assert_equal(events[0].event_id, 42)
    testbase.assert_equal(events[1].category, "USER_LOGIN")
    testbase.assert_equal(events[1].event_id, 43)
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Writes pass elevated=True to the runner when the config
        opts in.
    """
    runner = _MockCommandRunner()
    _seed_no_rules(runner=runner)
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(elevated=True),
    )
    spec = UbuntuAuditRuleSpec(
        key="watch_etc",
        watch_path="/etc",
        permissions="wa",
        persistent=False,
    )
    manager.add_rule(spec=spec)

    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["auditctl", "-w"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Reads (list_rules / get_audit_status / search_events) never
        pass elevated=True even when the config opts in.
    """
    runner = _MockCommandRunner()
    runner.results[("auditctl", "-l")] = RunResult(
        exit_code=0, stdout="No rules\n", stderr="",
    )
    runner.results[("auditctl", "-s")] = RunResult(
        exit_code=0,
        stdout="enabled 1\npid 1\nfailure 1\n",
        stderr="",
    )
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(elevated=True),
    )
    manager.list_rules()
    manager.get_audit_status()
    manager.search_events(query=UbuntuAuditEventQuery(key="probe"))

    elevated_reads = [
        c for c in runner.calls
        if c["elevated"] is True
        and (
            c["args"][:2] == ["auditctl", "-l"]
            or c["args"][:2] == ["auditctl", "-s"]
            or c["args"][:1] == ["ausearch"]
        )
    ]
    testbase.assert_equal(elevated_reads, [])
    return


def test_failed_command_surfaces_stderr():
    """
        When the runtime auditctl exits non-zero, the result reports
        FAILED and surfaces stderr.
    """
    runner = _MockCommandRunner()
    _seed_no_rules(runner=runner)
    runner.default_result = RunResult(
        exit_code=1, stdout="", stderr="boom: rule rejected",
    )

    manager = UbuntuAuditManager(
        runner=runner, config=UbuntuAuditManagerConfig(),
    )
    spec = UbuntuAuditRuleSpec(
        key="bad_rule",
        watch_path="/nope",
        permissions="zz",
        persistent=False,
    )
    result = manager.add_rule(spec=spec)

    testbase.assert_equal(result.status, UbuntuAuditActionStatus.FAILED)
    testbase.assert_equal(result.stderr, "boom: rule rejected")
    testbase.assert_equal(result.exit_code, 1)
    return


# ----------------------------------------------------------------------
# Pure-function tests (renderers / parsers)
# ----------------------------------------------------------------------


def test_render_auditctl_args_structured():
    """
        _render_auditctl_args emits the canonical ``-w / -p / -k``
        argv for a structured spec.
    """
    spec = UbuntuAuditRuleSpec(
        key="passwd_changes",
        watch_path="/etc/passwd",
        permissions="wa",
    )
    rendered = _render_auditctl_args(spec=spec)
    testbase.assert_equal(
        rendered,
        ["-w", "/etc/passwd", "-p", "wa", "-k", "passwd_changes"],
    )
    return


def test_render_auditctl_args_raw_appends_key():
    """
        _render_auditctl_args appends ``-k <key>`` to a raw rule body
        that omits the key token.
    """
    spec = UbuntuAuditRuleSpec(
        key="execve_calls",
        raw_rule="-a always,exit -F arch=b64 -S execve",
    )
    rendered = _render_auditctl_args(spec=spec)
    testbase.assert_true("-k" in rendered)
    testbase.assert_true("execve_calls" in rendered)
    return


def test_parse_auditctl_status_handles_partial_output():
    """
        _parse_auditctl_status tolerates missing fields and reports
        sane defaults.
    """
    body = "enabled 0\npid 0\n"
    status = _parse_auditctl_status(text=body)
    testbase.assert_equal(status.enabled, False)
    testbase.assert_equal(status.running, False)
    testbase.assert_equal(status.pid, 0)
    testbase.assert_equal(status.backlog, None)
    return


def test_parse_auditctl_list_returns_rule_info():
    """
        _parse_auditctl_list returns one UbuntuAuditRuleInfo per
        non-comment line.
    """
    text = (
        "-w /etc/passwd -p wa -k passwd_changes\n"
        "# comment\n"
        "-a always,exit -F arch=b64 -S execve -k execve_calls\n"
    )
    rules = _parse_auditctl_list(text=text)
    testbase.assert_equal(len(rules), 2)
    testbase.assert_equal(rules[0].subsystem, "path")
    testbase.assert_equal(rules[1].subsystem, "syscall")
    return


def test_parse_ausearch_output_respects_limit():
    """
        _parse_ausearch_output stops after collecting ``limit``
        records.
    """
    payload = (
        "----\n"
        "type=SYSCALL msg=audit(1.0:1): a=1\n"
        "----\n"
        "type=SYSCALL msg=audit(2.0:2): a=2\n"
        "----\n"
        "type=SYSCALL msg=audit(3.0:3): a=3\n"
    )
    events = _parse_ausearch_output(text=payload, limit=2)
    testbase.assert_equal(len(events), 2)
    testbase.assert_equal(events[0].event_id, 1)
    testbase.assert_equal(events[1].event_id, 2)
    return
