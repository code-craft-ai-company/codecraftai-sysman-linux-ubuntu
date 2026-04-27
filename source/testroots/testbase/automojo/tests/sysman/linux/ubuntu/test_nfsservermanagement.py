"""
    Tests for :class:`UbuntuNfsServerManager`.

    All tests use a ``_MockCommandRunner`` that records every
    ``run`` call and returns pre-seeded :class:`RunResult` values
    keyed by argv tuple.  No live ``exportfs`` / ``cat`` / ``tee``
    is invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.nfsservermanagement import (
    UbuntuNfsActionStatus,
    UbuntuNfsClientSpec,
    UbuntuNfsExportInfo,
    UbuntuNfsExportSpec,
    UbuntuNfsExportStatus,
    UbuntuNfsManagerConfig,
    UbuntuNfsQueryError,
    UbuntuNfsServerManager,
    _parse_client_token,
    _parse_exports_file,
    _parse_exportfs_v_output,
    _render_export_line,
)


# ----------------------------------------------------------------------
# Mock runner + helpers
# ----------------------------------------------------------------------


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
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 1,
) -> RunResult:
    """
        Build a failure :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


_EXPORTS_PATH = "/etc/exports"


def _seed_cat(
    runner: _MockCommandRunner,
    *,
    path: str,
    body: Optional[str],
) -> None:
    """
        Seed the runner so that ``cat <path>`` returns ``body`` on
        success, or non-zero when ``body`` is None.
    """
    if body is None:
        result = RunResult(
            exit_code=1, stdout="", stderr="No such file",
        )
    else:
        result = RunResult(exit_code=0, stdout=body, stderr="")
    runner.results[("cat", path)] = result
    return


def _seed_exportfs_v(
    runner: _MockCommandRunner,
    *,
    stdout: str,
) -> None:
    """
        Seed the ``exportfs -v`` probe.
    """
    runner.results[("exportfs", "-v")] = _ok(stdout=stdout)
    return


def _make_spec(
    *,
    path: str = "/srv/share",
    host: str = "*",
    options: Optional[list] = None,
) -> UbuntuNfsExportSpec:
    """
        Build an :class:`UbuntuNfsExportSpec` with a single client.
    """
    if options is None:
        options_list = ["rw", "sync", "no_subtree_check"]
    else:
        options_list = list(options)
    spec = UbuntuNfsExportSpec(
        path=path,
        clients=[UbuntuNfsClientSpec(host=host, options=options_list)],
    )
    return spec


# ----------------------------------------------------------------------
# add_export
# ----------------------------------------------------------------------


def test_add_export_invokes_exportfs():
    """
        add_export writes the rendered line to ``/etc/exports`` via
        ``tee`` and dispatches ``exportfs -ra`` afterwards.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_cat(runner, path=_EXPORTS_PATH, body="")
    _seed_exportfs_v(runner, stdout="")

    config = UbuntuNfsManagerConfig(elevated=True)
    manager = UbuntuNfsServerManager(runner=runner, config=config)
    spec = _make_spec(path="/srv/share")

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, UbuntuNfsActionStatus.SUCCESS)
    testbase.assert_equal(result.path, "/srv/share")

    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 2
        and call["args"][0] == "tee"
        and call["args"][1] == _EXPORTS_PATH
    ]
    testbase.assert_equal(len(tee_calls), 1)
    written = tee_calls[0]["stdin"]
    testbase.assert_equal(
        "/srv/share *(rw,sync,no_subtree_check)" in written, True,
    )

    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["exportfs", "-ra"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    testbase.assert_equal(reload_calls[0]["elevated"], True)
    return


def test_add_export_skipped_when_present():
    """
        add_export returns SKIPPED when the file already contains an
        export at the same path with identical clients.
    """
    runner = _MockCommandRunner()
    existing = "/srv/share *(rw,sync,no_subtree_check)\n"
    _seed_cat(runner, path=_EXPORTS_PATH, body=existing)

    config = UbuntuNfsManagerConfig(elevated=True)
    manager = UbuntuNfsServerManager(runner=runner, config=config)
    spec = _make_spec(path="/srv/share")

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, UbuntuNfsActionStatus.SKIPPED)

    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["exportfs", "-ra"]
    ]
    testbase.assert_equal(len(reload_calls), 0)
    return


def test_add_export_dry_run():
    """
        add_export under ``dry_run=True`` returns DRY_RUN and never
        invokes ``tee`` or ``exportfs -ra``.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path=_EXPORTS_PATH, body="")

    config = UbuntuNfsManagerConfig(elevated=True, dry_run=True)
    manager = UbuntuNfsServerManager(runner=runner, config=config)
    spec = _make_spec(path="/srv/share")

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, UbuntuNfsActionStatus.DRY_RUN)
    testbase.assert_equal(result.command[0], "tee")

    tee_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 1 and call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["exportfs", "-ra"]
    ]
    testbase.assert_equal(len(reload_calls), 0)
    return


def test_add_export_replaces_existing_path_with_different_clients():
    """
        add_export rewrites the path's line when clients differ.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    existing = "/srv/share 10.0.0.0/24(ro,sync,no_subtree_check)\n"
    _seed_cat(runner, path=_EXPORTS_PATH, body=existing)
    _seed_exportfs_v(runner, stdout="")

    config = UbuntuNfsManagerConfig(elevated=True)
    manager = UbuntuNfsServerManager(runner=runner, config=config)
    spec = _make_spec(path="/srv/share", host="*")

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, UbuntuNfsActionStatus.SUCCESS)
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    written = tee_calls[0]["stdin"]
    testbase.assert_equal(
        "*(rw,sync,no_subtree_check)" in written, True,
    )
    testbase.assert_equal(
        "10.0.0.0/24(ro" in written, False,
    )
    return


# ----------------------------------------------------------------------
# remove_export
# ----------------------------------------------------------------------


def test_remove_export_invokes_exportfs():
    """
        remove_export strips the matching line and dispatches
        ``exportfs -ra``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    existing = (
        "/srv/share *(rw,sync,no_subtree_check)\n"
        "/srv/other *(ro,sync,no_subtree_check)\n"
    )
    _seed_cat(runner, path=_EXPORTS_PATH, body=existing)

    config = UbuntuNfsManagerConfig(elevated=True)
    manager = UbuntuNfsServerManager(runner=runner, config=config)

    result = manager.remove_export(path="/srv/share")

    testbase.assert_equal(result.status, UbuntuNfsActionStatus.SUCCESS)

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    written = tee_calls[0]["stdin"]
    testbase.assert_equal("/srv/share" in written, False)
    testbase.assert_equal("/srv/other" in written, True)

    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["exportfs", "-ra"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    return


def test_remove_export_skipped_when_absent():
    """
        remove_export returns SKIPPED when no line matches the path.
    """
    runner = _MockCommandRunner()
    _seed_cat(
        runner,
        path=_EXPORTS_PATH,
        body="/srv/other *(ro,sync,no_subtree_check)\n",
    )

    config = UbuntuNfsManagerConfig(elevated=True)
    manager = UbuntuNfsServerManager(runner=runner, config=config)

    result = manager.remove_export(path="/srv/share")

    testbase.assert_equal(result.status, UbuntuNfsActionStatus.SKIPPED)

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


def test_remove_export_skipped_when_file_missing():
    """
        remove_export returns SKIPPED when /etc/exports is missing.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path=_EXPORTS_PATH, body=None)

    config = UbuntuNfsManagerConfig(elevated=True)
    manager = UbuntuNfsServerManager(runner=runner, config=config)

    result = manager.remove_export(path="/srv/share")

    testbase.assert_equal(result.status, UbuntuNfsActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# list_exports
# ----------------------------------------------------------------------


def test_list_exports_parses_exportfs_output():
    """
        list_exports merges configured exports (file content) and
        active exports (``exportfs -v`` output) into
        :class:`UbuntuNfsExportInfo` records with ``is_active`` set.
    """
    runner = _MockCommandRunner()
    _seed_cat(
        runner,
        path=_EXPORTS_PATH,
        body=(
            "# Top comment\n"
            "/srv/share *(rw,sync,no_subtree_check)\n"
            "/srv/inactive 10.0.0.0/24(ro,sync,no_subtree_check)\n"
        ),
    )
    _seed_exportfs_v(
        runner,
        stdout=(
            "/srv/share \t<world>(sync,wdelay,hide,no_subtree_check,sec=sys,rw)\n"
        ),
    )

    config = UbuntuNfsManagerConfig()
    manager = UbuntuNfsServerManager(runner=runner, config=config)

    rtnval = manager.list_exports()

    testbase.assert_equal(len(rtnval), 2)
    by_path = {entry.path: entry for entry in rtnval}
    testbase.assert_equal(by_path["/srv/share"].is_active, True)
    testbase.assert_equal(by_path["/srv/inactive"].is_active, False)
    testbase.assert_equal(
        by_path["/srv/share"].clients[0].host, "*",
    )
    return


# ----------------------------------------------------------------------
# reload_exports
# ----------------------------------------------------------------------


def test_reload_exports_invokes_exportfs():
    """
        reload_exports dispatches ``exportfs -ra``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()

    config = UbuntuNfsManagerConfig(elevated=True)
    manager = UbuntuNfsServerManager(runner=runner, config=config)

    result = manager.reload_exports()

    testbase.assert_equal(result.status, UbuntuNfsActionStatus.SUCCESS)
    testbase.assert_equal(result.command, ["exportfs", "-ra"])
    testbase.assert_equal(runner.calls[-1]["elevated"], True)
    return


# ----------------------------------------------------------------------
# get_export_status
# ----------------------------------------------------------------------


def test_get_export_status_parses_exportfs_output():
    """
        get_export_status parses ``exportfs -v`` output for the
        target path.
    """
    runner = _MockCommandRunner()
    _seed_exportfs_v(
        runner,
        stdout=(
            "/srv/share \t<world>(sync,wdelay,hide,no_subtree_check,sec=sys,rw)\n"
            "/srv/share \t10.0.0.0/24(sync,wdelay,hide,no_subtree_check,sec=sys,ro)\n"
        ),
    )

    config = UbuntuNfsManagerConfig()
    manager = UbuntuNfsServerManager(runner=runner, config=config)

    status = manager.get_export_status(path="/srv/share")

    testbase.assert_equal(status.is_active, True)
    testbase.assert_equal(status.client_count, 2)
    testbase.assert_equal("rw" in status.flags, True)
    return


def test_get_export_status_returns_inactive_for_missing_path():
    """
        get_export_status returns an inactive status for a path
        that does not appear in ``exportfs -v`` output.
    """
    runner = _MockCommandRunner()
    _seed_exportfs_v(runner, stdout="")

    config = UbuntuNfsManagerConfig()
    manager = UbuntuNfsServerManager(runner=runner, config=config)

    status = manager.get_export_status(path="/srv/missing")

    testbase.assert_equal(status.is_active, False)
    testbase.assert_equal(status.client_count, 0)
    testbase.assert_equal(status.flags, [])
    return


def test_get_export_status_raises_on_failure_with_stderr():
    """
        get_export_status raises :class:`UbuntuNfsQueryError` when
        ``exportfs -v`` exits non-zero with non-empty stderr.
    """
    runner = _MockCommandRunner()
    runner.results[("exportfs", "-v")] = _fail(
        stderr="exportfs: not found", exit_code=127,
    )

    config = UbuntuNfsManagerConfig()
    manager = UbuntuNfsServerManager(runner=runner, config=config)

    raised: Optional[UbuntuNfsQueryError] = None
    try:
        manager.get_export_status(path="/srv/share")
    except UbuntuNfsQueryError as exc:
        raised = exc
    testbase.assert_equal(raised is not None, True)
    testbase.assert_equal(raised.exit_code, 127)
    return


# ----------------------------------------------------------------------
# set_export_options
# ----------------------------------------------------------------------


def test_set_export_options_invokes_exportfs():
    """
        set_export_options rewrites the matched line with the new
        options and dispatches ``exportfs -ra``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_cat(
        runner,
        path=_EXPORTS_PATH,
        body="/srv/share *(rw,sync,no_subtree_check)\n",
    )

    config = UbuntuNfsManagerConfig(elevated=True)
    manager = UbuntuNfsServerManager(runner=runner, config=config)

    result = manager.set_export_options(
        path="/srv/share",
        options=["ro", "sync", "no_subtree_check"],
    )

    testbase.assert_equal(result.status, UbuntuNfsActionStatus.SUCCESS)
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    written = tee_calls[0]["stdin"]
    testbase.assert_equal(
        "/srv/share *(ro,sync,no_subtree_check)" in written, True,
    )

    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["exportfs", "-ra"]
    ]
    testbase.assert_equal(len(reload_calls), 1)
    return


def test_set_export_options_skipped_when_match():
    """
        set_export_options returns SKIPPED when every client already
        carries the requested options.
    """
    runner = _MockCommandRunner()
    _seed_cat(
        runner,
        path=_EXPORTS_PATH,
        body="/srv/share *(rw,sync,no_subtree_check)\n",
    )

    config = UbuntuNfsManagerConfig(elevated=True)
    manager = UbuntuNfsServerManager(runner=runner, config=config)

    result = manager.set_export_options(
        path="/srv/share",
        options=["rw", "sync", "no_subtree_check"],
    )

    testbase.assert_equal(result.status, UbuntuNfsActionStatus.SKIPPED)
    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 0)
    return


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Write verbs pass ``elevated=True`` to the runner when
        ``config.elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_cat(runner, path=_EXPORTS_PATH, body="")

    config = UbuntuNfsManagerConfig(elevated=True)
    manager = UbuntuNfsServerManager(runner=runner, config=config)

    spec = _make_spec(path="/srv/share")
    manager.add_export(spec=spec)

    tee_calls = [
        call for call in runner.calls
        if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["exportfs", "-ra"]
    ]
    testbase.assert_equal(reload_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs (``list_exports``, ``get_export_status``) and the
        ``cat`` / ``exportfs -v`` probes used inside write paths
        never pass ``elevated=True``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok()
    _seed_cat(
        runner,
        path=_EXPORTS_PATH,
        body="/srv/share *(rw,sync,no_subtree_check)\n",
    )
    _seed_exportfs_v(
        runner,
        stdout=(
            "/srv/share \t<world>(sync,no_subtree_check,rw)\n"
        ),
    )

    config = UbuntuNfsManagerConfig(elevated=True)
    manager = UbuntuNfsServerManager(runner=runner, config=config)

    manager.list_exports()
    manager.get_export_status(path="/srv/share")

    for call in runner.calls:
        if call["args"][0] == "cat":
            testbase.assert_equal(call["elevated"], False)
        if call["args"] == ["exportfs", "-v"]:
            testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Failure surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failing ``tee`` produces a FAILED result with the
        runner's stderr, exit code, and command attached.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path=_EXPORTS_PATH, body="")
    runner.results[("tee", _EXPORTS_PATH)] = _fail(
        stderr="tee: cannot open", exit_code=1,
    )

    config = UbuntuNfsManagerConfig(elevated=True)
    manager = UbuntuNfsServerManager(runner=runner, config=config)
    spec = _make_spec(path="/srv/share")

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, UbuntuNfsActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal("tee: cannot open" in result.stderr, True)
    testbase.assert_equal(result.command[0], "tee")

    reload_calls = [
        call for call in runner.calls
        if call["args"] == ["exportfs", "-ra"]
    ]
    testbase.assert_equal(len(reload_calls), 0)
    return


def test_reload_failure_propagates():
    """
        When ``exportfs -ra`` fails after a successful ``tee``, the
        verb maps to FAILED and the reload's command + stderr is on
        the result.
    """
    runner = _MockCommandRunner()
    _seed_cat(runner, path=_EXPORTS_PATH, body="")
    runner.results[("tee", _EXPORTS_PATH)] = _ok()
    runner.results[("exportfs", "-ra")] = _fail(
        stderr="exportfs: invalid", exit_code=2,
    )

    config = UbuntuNfsManagerConfig(elevated=True)
    manager = UbuntuNfsServerManager(runner=runner, config=config)
    spec = _make_spec(path="/srv/share")

    result = manager.add_export(spec=spec)

    testbase.assert_equal(result.status, UbuntuNfsActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.command, ["exportfs", "-ra"])
    testbase.assert_equal("exportfs: invalid" in result.stderr, True)
    return


# ----------------------------------------------------------------------
# Renderer / parser unit tests
# ----------------------------------------------------------------------


def test_export_line_renders_clients_correctly():
    """
        The canonical Linux export-line shape is
        ``<path> <client>(<opts>) <client>(<opts>)…``.
    """
    spec = UbuntuNfsExportSpec(
        path="/srv/share",
        clients=[
            UbuntuNfsClientSpec(
                host="10.0.0.0/24",
                options=["rw", "sync", "no_subtree_check"],
            ),
            UbuntuNfsClientSpec(
                host="*",
                options=["ro", "sync"],
            ),
        ],
    )
    rendered = _render_export_line(spec=spec)
    expected = (
        "/srv/share 10.0.0.0/24(rw,sync,no_subtree_check) *(ro,sync)"
    )
    testbase.assert_equal(rendered, expected)
    return


def test_parse_exports_file_skips_comments_and_blanks():
    """
        ``_parse_exports_file`` skips comment and blank lines.
    """
    body = (
        "# header\n"
        "\n"
        "/srv/share *(rw,sync,no_subtree_check)\n"
        "# trailing\n"
    )
    parsed = _parse_exports_file(content=body)
    testbase.assert_equal(len(parsed), 1)
    testbase.assert_equal(parsed[0].path, "/srv/share")
    testbase.assert_equal(parsed[0].clients[0].host, "*")
    return


def test_parse_client_token_handles_default_options():
    """
        A bare client token with no parens carries the canonical
        default options.
    """
    parsed = _parse_client_token(token="*")
    testbase.assert_equal(parsed.host, "*")
    testbase.assert_equal(parsed.options, ["rw", "sync", "no_subtree_check"])
    return


def test_parse_exportfs_v_output_handles_wrapped_lines():
    """
        ``_parse_exportfs_v_output`` handles both single-line and
        wrapped (path on its own line, client indented on next)
        forms.
    """
    text = (
        "/srv/share\n"
        "\t<world>(sync,wdelay,hide,no_subtree_check,sec=sys,rw)\n"
        "/srv/other 10.0.0.0/24(sync,wdelay,hide,sec=sys,ro)\n"
    )
    parsed = _parse_exportfs_v_output(text=text)
    testbase.assert_equal(parsed["/srv/share"]["client_count"], 1)
    testbase.assert_equal("rw" in parsed["/srv/share"]["flags"], True)
    testbase.assert_equal(parsed["/srv/other"]["client_count"], 1)
    testbase.assert_equal("ro" in parsed["/srv/other"]["flags"], True)
    return


# ----------------------------------------------------------------------
# Result-type sanity
# ----------------------------------------------------------------------


def test_export_info_dataclass_carries_share_name_field():
    """
        The :class:`UbuntuNfsExportInfo` dataclass exposes a
        ``share_name`` field for cross-OS parity even though Ubuntu
        always leaves it ``None``.
    """
    info = UbuntuNfsExportInfo(
        path="/srv/share",
        share_name=None,
        clients=[UbuntuNfsClientSpec()],
        is_active=False,
    )
    testbase.assert_equal(info.share_name, None)
    return


def test_export_status_dataclass_shape():
    """
        The :class:`UbuntuNfsExportStatus` dataclass exposes path,
        is_active, client_count, and flags.
    """
    status = UbuntuNfsExportStatus(
        path="/srv/share",
        is_active=False,
        client_count=0,
        flags=[],
    )
    testbase.assert_equal(status.path, "/srv/share")
    testbase.assert_equal(status.is_active, False)
    testbase.assert_equal(status.client_count, 0)
    testbase.assert_equal(status.flags, [])
    return
