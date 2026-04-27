"""
    Tests for :class:`UbuntuLogManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``journalctl`` / ``logrotate`` is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.logmanagement import (
    UbuntuLogActionStatus,
    UbuntuLogEntries,
    UbuntuLogEntry,
    UbuntuLogInfo,
    UbuntuLogManager,
    UbuntuLogManagerConfig,
    UbuntuLogQueryError,
    UbuntuRotationPolicy,
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


def _journal_lines(entries: list) -> str:
    """
        Build a multi-line ``journalctl -o json`` stdout payload from a
        list of dicts.
    """
    lines = []
    for entry in entries:
        lines.append(json.dumps(entry))
    rtnval = "\n".join(lines) + "\n"
    return rtnval


# ----------------------------------------------------------------------
# get_log
# ----------------------------------------------------------------------


def test_get_log_constructs_journalctl_command():
    """
        get_log builds the canonical ``journalctl -u <name> -n <lines>
        --no-pager -o json`` argv.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    manager.get_log(name="nginx.service", lines=50)
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        [
            "journalctl", "-u", "nginx.service", "-n", "50",
            "--no-pager", "-o", "json",
        ],
    )
    testbase.assert_equal(last_call["elevated"], False)
    return


def test_get_log_includes_since_when_provided():
    """
        get_log emits ``--since <iso>`` only when the parameter is set.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    manager.get_log(
        name="nginx.service", lines=10, since="2024-01-01T00:00:00Z",
    )
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        [
            "journalctl", "-u", "nginx.service", "-n", "10",
            "--since", "2024-01-01T00:00:00Z",
            "--no-pager", "-o", "json",
        ],
    )
    return


def test_get_log_parses_journal_json_lines():
    """
        get_log parses one JSON object per stdout line into
        UbuntuLogEntry instances.
    """
    runner = _MockCommandRunner()
    payload = _journal_lines([
        {
            "__REALTIME_TIMESTAMP": "1712345678901234",
            "PRIORITY": "6",
            "_SYSTEMD_UNIT": "nginx.service",
            "MESSAGE": "GET / 200",
        },
        {
            "__REALTIME_TIMESTAMP": "1712345678999999",
            "PRIORITY": "3",
            "_SYSTEMD_UNIT": "nginx.service",
            "MESSAGE": "upstream timed out",
        },
    ])
    runner.results[(
        "journalctl", "-u", "nginx.service", "-n", "100",
        "--no-pager", "-o", "json",
    )] = RunResult(exit_code=0, stdout=payload, stderr="")

    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    entries = manager.get_log(name="nginx.service")
    testbase.assert_equal(isinstance(entries, UbuntuLogEntries), True)
    testbase.assert_equal(entries.total_count, 2)
    first = entries.entries[0]
    testbase.assert_equal(first.unit, "nginx.service")
    testbase.assert_equal(first.priority, 6)
    testbase.assert_equal(first.message, "GET / 200")
    second = entries.entries[1]
    testbase.assert_equal(second.priority, 3)
    testbase.assert_equal(second.message, "upstream timed out")
    return


def test_get_log_collects_unparseable_lines_as_raw():
    """
        get_log preserves lines that fail to decode as JSON in the
        ``raw`` attribute and excludes them from ``entries``.
    """
    runner = _MockCommandRunner()
    payload = (
        json.dumps({"MESSAGE": "ok", "PRIORITY": "6"}) + "\n"
        + "this is not json\n"
    )
    runner.results[(
        "journalctl", "-u", "nginx.service", "-n", "100",
        "--no-pager", "-o", "json",
    )] = RunResult(exit_code=0, stdout=payload, stderr="")

    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    entries = manager.get_log(name="nginx.service")
    testbase.assert_equal(entries.total_count, 1)
    testbase.assert_equal(entries.raw, ["this is not json"])
    return


def test_get_log_raises_on_unexpected_error():
    """
        A non-zero exit with stderr raises UbuntuLogQueryError.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=2, stdout="", stderr="boom",
    )
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    raised = False
    try:
        manager.get_log(name="nginx.service")
    except UbuntuLogQueryError:
        raised = True
    testbase.assert_true(raised)
    return


# ----------------------------------------------------------------------
# tail_log
# ----------------------------------------------------------------------


def test_tail_log_yields_entries_for_finite_stream():
    """
        tail_log iterates the runner's stdout JSON lines.
    """
    runner = _MockCommandRunner()
    payload = _journal_lines([
        {"MESSAGE": "first", "PRIORITY": "6"},
        {"MESSAGE": "second", "PRIORITY": "5"},
    ])
    runner.results[(
        "journalctl", "-u", "nginx.service", "-f", "--no-pager", "-o", "json",
    )] = RunResult(exit_code=0, stdout=payload, stderr="")

    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    collected = []
    for entry in manager.tail_log(name="nginx.service", follow=True):
        collected.append(entry)

    testbase.assert_equal(len(collected), 2)
    testbase.assert_equal(collected[0].message, "first")
    testbase.assert_equal(collected[1].message, "second")
    return


def test_tail_log_follow_false_omits_dash_f_flag():
    """
        tail_log without follow does not emit the ``-f`` flag.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    list(manager.tail_log(name="nginx.service", follow=False))
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        ["journalctl", "-u", "nginx.service", "--no-pager", "-o", "json"],
    )
    return


# ----------------------------------------------------------------------
# rotate_log
# ----------------------------------------------------------------------


def test_rotate_log_uses_dropin_when_present():
    """
        rotate_log picks up ``/etc/logrotate.d/<name>`` when the
        drop-in exists.
    """
    runner = _MockCommandRunner()
    runner.results[("test", "-e", "/etc/logrotate.d/myapp")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[("logrotate", "-f", "/etc/logrotate.d/myapp")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    result = manager.rotate_log(name="myapp")
    testbase.assert_equal(result.status, UbuntuLogActionStatus.SUCCESS)
    testbase.assert_equal(result.path, "/etc/logrotate.d/myapp")
    testbase.assert_equal(
        result.command, ["logrotate", "-f", "/etc/logrotate.d/myapp"],
    )
    return


def test_rotate_log_falls_back_to_default_conf():
    """
        rotate_log falls back to /etc/logrotate.conf when the per-name
        drop-in is missing.
    """
    runner = _MockCommandRunner()
    runner.results[("test", "-e", "/etc/logrotate.d/missing")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.results[("logrotate", "-f", "/etc/logrotate.conf")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    result = manager.rotate_log(name="missing")
    testbase.assert_equal(result.status, UbuntuLogActionStatus.SUCCESS)
    testbase.assert_equal(result.path, "/etc/logrotate.conf")
    return


def test_rotate_log_dry_run_short_circuits():
    """
        rotate_log dry-run returns DRY_RUN without invoking the runner.
    """
    runner = _MockCommandRunner()
    runner.results[("test", "-e", "/etc/logrotate.d/myapp")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(dry_run=True),
    )
    result = manager.rotate_log(name="myapp")
    testbase.assert_equal(result.status, UbuntuLogActionStatus.DRY_RUN)
    rotate_calls = [
        c for c in runner.calls if c["args"][:1] == ["logrotate"]
    ]
    testbase.assert_equal(rotate_calls, [])
    return


# ----------------------------------------------------------------------
# truncate_log
# ----------------------------------------------------------------------


def test_truncate_log_invokes_journalctl_vacuum():
    """
        truncate_log dispatches ``journalctl --vacuum-size=1K``.
    """
    runner = _MockCommandRunner()
    runner.results[("journalctl", "--vacuum-size=1K")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    result = manager.truncate_log(name="nginx.service")
    testbase.assert_equal(result.status, UbuntuLogActionStatus.SUCCESS)
    testbase.assert_equal(
        result.command, ["journalctl", "--vacuum-size=1K"],
    )
    return


def test_truncate_log_dry_run_short_circuits():
    """
        truncate_log dry-run returns DRY_RUN without invoking the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(dry_run=True),
    )
    result = manager.truncate_log(name="nginx.service")
    testbase.assert_equal(result.status, UbuntuLogActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


# ----------------------------------------------------------------------
# configure_rotation
# ----------------------------------------------------------------------


def test_configure_rotation_writes_dropin():
    """
        configure_rotation writes the rendered logrotate body via tee
        when no drop-in exists.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/logrotate.d/myapp")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    policy = UbuntuRotationPolicy(
        frequency="daily",
        rotate_count=14,
        compress=True,
        delaycompress=True,
        missingok=True,
        notifempty=True,
        size=None,
    )
    result = manager.configure_rotation(name="myapp", policy=policy)
    testbase.assert_equal(result.status, UbuntuLogActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"], ["tee", "/etc/logrotate.d/myapp"],
    )
    body = write_calls[0]["stdin"]
    testbase.assert_true("/var/log/myapp.log {" in body)
    testbase.assert_true("    daily" in body)
    testbase.assert_true("    rotate 14" in body)
    testbase.assert_true("    compress" in body)
    testbase.assert_true("    delaycompress" in body)
    testbase.assert_true("    missingok" in body)
    testbase.assert_true("    notifempty" in body)
    return


def test_configure_rotation_skipped_when_content_matches():
    """
        configure_rotation returns SKIPPED when the existing drop-in
        matches the rendered content byte-for-byte.
    """
    runner = _MockCommandRunner()
    policy = UbuntuRotationPolicy(
        frequency="weekly",
        rotate_count=4,
        compress=False,
        delaycompress=False,
        missingok=True,
        notifempty=True,
        size=None,
    )
    expected_body = (
        "/var/log/myapp.log {\n"
        "    weekly\n"
        "    rotate 4\n"
        "    missingok\n"
        "    notifempty\n"
        "}\n"
    )
    runner.results[("cat", "/etc/logrotate.d/myapp")] = RunResult(
        exit_code=0, stdout=expected_body, stderr="",
    )
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    result = manager.configure_rotation(name="myapp", policy=policy)
    testbase.assert_equal(result.status, UbuntuLogActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls if c["args"][:1] == ["tee"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_configure_rotation_dry_run_short_circuits():
    """
        configure_rotation dry-run returns DRY_RUN without invoking
        the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(dry_run=True),
    )
    policy = UbuntuRotationPolicy(frequency="daily")
    result = manager.configure_rotation(name="myapp", policy=policy)
    testbase.assert_equal(result.status, UbuntuLogActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_configure_rotation_renders_size_token_when_provided():
    """
        configure_rotation emits ``size <value>`` when the policy sets
        a size threshold.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/logrotate.d/myapp")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    policy = UbuntuRotationPolicy(frequency="daily", size="100M")
    manager.configure_rotation(name="myapp", policy=policy)
    write_calls = [
        c for c in runner.calls if c["args"][:1] == ["tee"]
    ]
    body = write_calls[0]["stdin"]
    testbase.assert_true("    size 100M" in body)
    return


# ----------------------------------------------------------------------
# list_logs
# ----------------------------------------------------------------------


def test_list_logs_combines_systemd_and_logrotate_entries():
    """
        list_logs composes systemd unit names plus logrotate drop-ins.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "systemctl", "list-units", "--type=service",
        "--no-legend", "--plain",
    )] = RunResult(
        exit_code=0,
        stdout=(
            "nginx.service       loaded active running A high performance web server\n"
            "ssh.service         loaded active running OpenBSD Secure Shell server\n"
        ),
        stderr="",
    )
    runner.results[("ls", "-1", "/etc/logrotate.d")] = RunResult(
        exit_code=0,
        stdout="apt\nrsyslog\n",
        stderr="",
    )
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    listed = manager.list_logs()
    journald = [item for item in listed if item.source == "journald"]
    logrotate = [item for item in listed if item.source == "logrotate"]
    testbase.assert_equal(len(journald), 2)
    testbase.assert_equal(journald[0].name, "nginx")
    testbase.assert_equal(journald[1].name, "ssh")
    testbase.assert_equal(len(logrotate), 2)
    testbase.assert_equal(logrotate[0].name, "apt")
    testbase.assert_equal(logrotate[1].name, "rsyslog")
    testbase.assert_equal(isinstance(journald[0], UbuntuLogInfo), True)
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_pass_elevated_when_configured():
    """
        Write verbs propagate ``elevated=True`` when the config enables
        elevation.
    """
    runner = _MockCommandRunner()
    runner.results[("test", "-e", "/etc/logrotate.d/myapp")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )
    runner.default_result = RunResult(exit_code=0, stdout="", stderr="")
    manager = UbuntuLogManager(
        runner=runner,
        config=UbuntuLogManagerConfig(elevated=True),
    )
    manager.rotate_log(name="myapp")
    rotate_call = [
        c for c in runner.calls if c["args"][:1] == ["logrotate"]
    ][0]
    testbase.assert_equal(rotate_call["elevated"], True)
    return


def test_reads_never_elevate():
    """
        get_log and list_logs never pass ``elevated=True`` even with
        elevation enabled in the config.
    """
    runner = _MockCommandRunner()
    runner.default_result = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    manager = UbuntuLogManager(
        runner=runner,
        config=UbuntuLogManagerConfig(elevated=True),
    )
    manager.get_log(name="nginx.service")
    manager.list_logs()
    elevated_reads = [
        c for c in runner.calls
        if c["elevated"] is True
    ]
    testbase.assert_equal(elevated_reads, [])
    return


def test_failed_command_returns_failed_result_with_stderr():
    """
        A non-zero exit on a write maps to FAILED with stderr captured.
    """
    runner = _MockCommandRunner()
    runner.results[("test", "-e", "/etc/logrotate.d/myapp")] = RunResult(
        exit_code=0, stdout="", stderr="",
    )
    runner.results[("logrotate", "-f", "/etc/logrotate.d/myapp")] = RunResult(
        exit_code=1, stdout="", stderr="error reading config",
    )
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    result = manager.rotate_log(name="myapp")
    testbase.assert_equal(result.status, UbuntuLogActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "error reading config")
    return


def test_log_entry_dataclass_round_trip():
    """
        UbuntuLogEntry retains the fields constructed from a journal
        line.
    """
    payload = json.dumps({
        "__REALTIME_TIMESTAMP": "1712345678901234",
        "PRIORITY": "4",
        "_SYSTEMD_UNIT": "kernel",
        "MESSAGE": "warning text",
    })
    runner = _MockCommandRunner()
    runner.results[(
        "journalctl", "-u", "kernel", "-n", "100",
        "--no-pager", "-o", "json",
    )] = RunResult(exit_code=0, stdout=payload + "\n", stderr="")
    manager = UbuntuLogManager(
        runner=runner, config=UbuntuLogManagerConfig(),
    )
    entries = manager.get_log(name="kernel")
    testbase.assert_equal(entries.total_count, 1)
    entry = entries.entries[0]
    testbase.assert_equal(isinstance(entry, UbuntuLogEntry), True)
    testbase.assert_equal(entry.priority, 4)
    testbase.assert_equal(entry.unit, "kernel")
    testbase.assert_equal(entry.timestamp, "1712345678901234")
    return
