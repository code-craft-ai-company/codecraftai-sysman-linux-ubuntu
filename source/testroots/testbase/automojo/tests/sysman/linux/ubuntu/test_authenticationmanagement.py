"""
    Tests for :class:`UbuntuAuthenticationManager`.

    A local ``_MockCommandRunner`` records every ``run`` call and
    returns pre-seeded :class:`RunResult` values keyed by argv tuple.
    All file I/O (``cat`` / ``tee``) and tooling
    (``pam-auth-update``, ``ls``) is exercised entirely through the
    runner.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.authenticationmanagement import (
    UBUNTU_AUTH_LOGIN,
    UBUNTU_AUTH_SUDO,
    UbuntuAuthActionStatus,
    UbuntuAuthConfig,
    UbuntuAuthEntry,
    UbuntuAuthenticationManager,
    UbuntuAuthenticationManagerConfig,
    UbuntuAuthenticationQueryError,
    UbuntuAuthenticationSubsystemNotFoundError,
    _parse_pam_auth_update_list,
    _parse_pam_stack,
    _render_pam_stack,
)


# ----------------------------------------------------------------------
# Mock runner
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call
        and returns pre-seeded :class:`RunResult` values keyed by
        argv tuple.
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
    stdout: str = "", stderr: str = "", exit_code: int = 1,
) -> RunResult:
    """
        Build a failing :class:`RunResult`.
    """
    rtnval = RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
    return rtnval


_SAMPLE_PAM_LOGIN = (
    "# /etc/pam.d/login\n"
    "auth       required   pam_securetty.so\n"
    "auth       requisite  pam_nologin.so\n"
    "@include   common-auth\n"
    "account    required   pam_unix.so\n"
    "password   [success=ok default=ignore]   pam_unix.so try_first_pass\n"
    "session    optional   pam_motd.so\n"
)


_SAMPLE_PAM_AUTH_UPDATE_LIST = (
    "PAM configurations available:\n"
    "unix     Unix authentication            [Enabled]\n"
    "krb5     Kerberos authentication        [Disabled]\n"
    "sss      SSS authentication             [Enabled]\n"
)


_SAMPLE_PAM_CONFIGS_LS = "unix\nkrb5\nsss\n"

_SAMPLE_UNIX_PROFILE = (
    "Name: Unix authentication\n"
    "Default: yes\n"
    "Priority: 256\n"
    "Auth-Type: Primary\n"
    "Auth:\n"
    "        [success=end default=ignore]    pam_unix.so try_first_pass nullok\n"
    "Account-Type: Primary\n"
    "Account:\n"
    "        [success=end default=ignore]    pam_unix.so\n"
)


_SAMPLE_KRB5_PROFILE = (
    "Name: Kerberos authentication\n"
    "Default: no\n"
    "Priority: 704\n"
    "Auth-Type: Primary\n"
    "Auth:\n"
    "        [success=end default=ignore]    pam_krb5.so minimum_uid=1000\n"
)


_SAMPLE_SSS_PROFILE = (
    "Name: SSS authentication\n"
    "Default: yes\n"
    "Priority: 128\n"
    "Auth:\n"
    "        [success=end default=ignore]    pam_sss.so use_first_pass\n"
)


def _build_default_runner() -> _MockCommandRunner:
    """
        Build a runner pre-seeded with the canonical ``cat`` /
        ``ls`` / ``pam-auth-update`` outputs used across multiple
        tests.

        :returns: The mock runner.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/pam.d/login")] = _ok(
        stdout=_SAMPLE_PAM_LOGIN,
    )
    runner.results[("cat", "/etc/pam.d/sudo")] = _ok(
        stdout="auth required pam_unix.so\n",
    )
    runner.results[("pam-auth-update", "--list")] = _ok(
        stdout=_SAMPLE_PAM_AUTH_UPDATE_LIST,
    )
    runner.results[("ls", "-1", "/usr/share/pam-configs")] = _ok(
        stdout=_SAMPLE_PAM_CONFIGS_LS,
    )
    runner.results[("cat", "/usr/share/pam-configs/unix")] = _ok(
        stdout=_SAMPLE_UNIX_PROFILE,
    )
    runner.results[("cat", "/usr/share/pam-configs/krb5")] = _ok(
        stdout=_SAMPLE_KRB5_PROFILE,
    )
    runner.results[("cat", "/usr/share/pam-configs/sss")] = _ok(
        stdout=_SAMPLE_SSS_PROFILE,
    )
    return runner


def _build_manager(
    *,
    runner: _MockCommandRunner,
    dry_run: bool = False,
    elevated: bool = True,
) -> UbuntuAuthenticationManager:
    """
        Build a manager with the supplied runner and config flags.

        :returns: The manager.
    """
    config = UbuntuAuthenticationManagerConfig(
        dry_run=dry_run,
        elevated=elevated,
    )
    rtnval = UbuntuAuthenticationManager(runner=runner, config=config)
    return rtnval


# ----------------------------------------------------------------------
# Test cases
# ----------------------------------------------------------------------


def test_get_auth_config_parses_cat_output():
    """
        ``get_auth_config`` parses ``cat`` output into a structured
        :class:`UbuntuAuthConfig`.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.get_auth_config(subsystem=UBUNTU_AUTH_LOGIN)

    testbase.assert_equal(result.subsystem, "login")
    testbase.assert_equal(len(result.entries), 6)
    testbase.assert_equal(result.entries[0].module_type, "auth")
    testbase.assert_equal(result.entries[0].control, "required")
    testbase.assert_equal(result.entries[0].module_path, "pam_securetty.so")
    testbase.assert_equal(result.entries[2].module_type, "@include")
    testbase.assert_equal(result.entries[2].module_path, "common-auth")
    return


def test_get_auth_config_raises_subsystem_not_found_when_missing():
    """
        ``get_auth_config`` raises
        :class:`UbuntuAuthenticationSubsystemNotFoundError` when
        ``cat`` reports "No such file".
    """
    runner = _build_default_runner()
    runner.results[("cat", "/etc/pam.d/missing")] = _fail(
        stderr="cat: /etc/pam.d/missing: No such file or directory",
        exit_code=1,
    )
    manager = _build_manager(runner=runner)

    raised = False
    try:
        manager.get_auth_config(subsystem="missing")
    except UbuntuAuthenticationSubsystemNotFoundError:
        raised = True
    testbase.assert_true(raised)
    return


def test_set_auth_config_invokes_tee():
    """
        ``set_auth_config`` invokes ``tee`` with the rendered content
        on stdin and ``elevated=True``.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    new_config = UbuntuAuthConfig(
        subsystem="login",
        entries=[
            UbuntuAuthEntry(
                module_type="auth",
                control="required",
                module_path="pam_unix.so",
                args=[],
            ),
        ],
    )
    result = manager.set_auth_config(
        subsystem=UBUNTU_AUTH_LOGIN, config=new_config,
    )

    testbase.assert_equal(result.status, UbuntuAuthActionStatus.SUCCESS)
    write_calls = [
        call for call in runner.calls
        if len(call["args"]) >= 2 and call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], ["tee", "/etc/pam.d/login"])
    testbase.assert_true(write_calls[0]["elevated"])
    testbase.assert_equal(
        write_calls[0]["stdin"], "auth required pam_unix.so\n",
    )
    return


def test_set_auth_config_skipped_when_match():
    """
        ``set_auth_config`` returns ``SKIPPED`` when the rendered
        content equals the live file content.
    """
    runner = _MockCommandRunner()
    rendered = "auth required pam_unix.so\n"
    runner.results[("cat", "/etc/pam.d/login")] = _ok(stdout=rendered)
    manager = _build_manager(runner=runner)

    config = UbuntuAuthConfig(
        subsystem="login",
        entries=[
            UbuntuAuthEntry(
                module_type="auth",
                control="required",
                module_path="pam_unix.so",
                args=[],
            ),
        ],
    )
    result = manager.set_auth_config(
        subsystem=UBUNTU_AUTH_LOGIN, config=config,
    )

    testbase.assert_equal(result.status, UbuntuAuthActionStatus.SKIPPED)
    write_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(write_calls), 0)
    return


def test_set_auth_config_dry_run():
    """
        ``set_auth_config`` returns ``DRY_RUN`` and does not invoke
        ``tee`` when dry-run is enabled.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner, dry_run=True)

    new_config = UbuntuAuthConfig(
        subsystem="login",
        entries=[
            UbuntuAuthEntry(
                module_type="auth",
                control="required",
                module_path="pam_unix.so",
                args=[],
            ),
        ],
    )
    result = manager.set_auth_config(
        subsystem=UBUNTU_AUTH_LOGIN, config=new_config,
    )

    testbase.assert_equal(result.status, UbuntuAuthActionStatus.DRY_RUN)
    write_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    testbase.assert_equal(len(write_calls), 0)
    testbase.assert_equal(result.command, ["tee", "/etc/pam.d/login"])
    return


def test_list_auth_methods_parses_pam_auth_update_output():
    """
        ``list_auth_methods`` parses ``pam-auth-update --list`` and
        merges the result with the registered profile files.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    methods = manager.list_auth_methods()

    names = [info.name for info in methods]
    testbase.assert_true("unix" in names)
    testbase.assert_true("krb5" in names)
    testbase.assert_true("sss" in names)

    unix_matches = [info for info in methods if info.name == "unix"]
    unix_info = unix_matches[0]
    testbase.assert_true(unix_info.enabled)
    testbase.assert_equal(unix_info.description, "Unix authentication")

    krb5_matches = [info for info in methods if info.name == "krb5"]
    krb5_info = krb5_matches[0]
    testbase.assert_false(krb5_info.enabled)
    return


def test_list_auth_methods_falls_back_to_pam_configs_when_list_fails():
    """
        When ``pam-auth-update --list`` fails the manager falls back
        to enumerating ``/usr/share/pam-configs`` and marks every
        profile ``enabled=False``.
    """
    runner = _build_default_runner()
    runner.results[("pam-auth-update", "--list")] = _fail(
        stderr="pam-auth-update: not available",
        exit_code=1,
    )
    manager = _build_manager(runner=runner)

    methods = manager.list_auth_methods()

    for info in methods:
        testbase.assert_false(info.enabled)
    names = [info.name for info in methods]
    testbase.assert_true("unix" in names)
    return


def test_enable_method_invokes_pam_auth_update():
    """
        ``enable_method`` invokes ``pam-auth-update --enable <name>``
        with elevation.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.enable_method(name="krb5")

    testbase.assert_equal(result.status, UbuntuAuthActionStatus.SUCCESS)
    enable_calls = [
        call for call in runner.calls
        if call["args"] == ["pam-auth-update", "--enable", "krb5"]
    ]
    testbase.assert_equal(len(enable_calls), 1)
    testbase.assert_true(enable_calls[0]["elevated"])
    return


def test_enable_method_skipped_when_enabled():
    """
        ``enable_method`` returns ``SKIPPED`` when the profile is
        already enabled.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.enable_method(name="unix")

    testbase.assert_equal(result.status, UbuntuAuthActionStatus.SKIPPED)
    enable_calls = [
        call for call in runner.calls
        if call["args"][:2] == ["pam-auth-update", "--enable"]
    ]
    testbase.assert_equal(len(enable_calls), 0)
    return


def test_enable_method_raises_when_not_registered():
    """
        ``enable_method`` raises
        :class:`UbuntuAuthenticationSubsystemNotFoundError` when
        ``name`` is not registered.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    raised = False
    try:
        manager.enable_method(name="ghost-profile")
    except UbuntuAuthenticationSubsystemNotFoundError:
        raised = True
    testbase.assert_true(raised)
    return


def test_disable_method_invokes_pam_auth_update():
    """
        ``disable_method`` invokes
        ``pam-auth-update --disable <name>`` with elevation.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.disable_method(name="unix")

    testbase.assert_equal(result.status, UbuntuAuthActionStatus.SUCCESS)
    disable_calls = [
        call for call in runner.calls
        if call["args"] == ["pam-auth-update", "--disable", "unix"]
    ]
    testbase.assert_equal(len(disable_calls), 1)
    testbase.assert_true(disable_calls[0]["elevated"])
    return


def test_disable_method_skipped_when_disabled():
    """
        ``disable_method`` returns ``SKIPPED`` when the profile is
        already disabled.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.disable_method(name="krb5")

    testbase.assert_equal(result.status, UbuntuAuthActionStatus.SKIPPED)
    disable_calls = [
        call for call in runner.calls
        if call["args"][:2] == ["pam-auth-update", "--disable"]
    ]
    testbase.assert_equal(len(disable_calls), 0)
    return


def test_validate_auth_config_returns_result():
    """
        ``validate_auth_config`` returns a populated
        :class:`UbuntuAuthValidationResult` for a clean stack.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner)

    result = manager.validate_auth_config(subsystem=UBUNTU_AUTH_LOGIN)

    testbase.assert_true(result.valid)
    testbase.assert_equal(len(result.errors), 0)
    return


def test_validate_auth_config_flags_unrecognised_module_type():
    """
        ``validate_auth_config`` reports an error for an unrecognised
        module_type.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/pam.d/login")] = _ok(
        stdout="weirdtype required pam_unix.so\n",
    )
    manager = _build_manager(runner=runner)

    result = manager.validate_auth_config(subsystem=UBUNTU_AUTH_LOGIN)

    testbase.assert_false(result.valid)
    testbase.assert_greater(len(result.errors), 0)
    return


def test_writes_elevate():
    """
        ``set_auth_config`` and ``enable_method`` /
        ``disable_method`` invoke the runner with ``elevated=True``.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner, elevated=True)

    manager.set_auth_config(
        subsystem=UBUNTU_AUTH_SUDO,
        config=UbuntuAuthConfig(
            subsystem="sudo",
            entries=[
                UbuntuAuthEntry(
                    module_type="auth",
                    control="required",
                    module_path="pam_unix.so",
                    args=["nullok"],
                ),
            ],
        ),
    )
    manager.enable_method(name="krb5")

    write_calls = [
        call for call in runner.calls if call["args"][0] == "tee"
    ]
    for call in write_calls:
        testbase.assert_true(call["elevated"])

    method_calls = [
        call for call in runner.calls
        if call["args"][:2] == ["pam-auth-update", "--enable"]
    ]
    for call in method_calls:
        testbase.assert_true(call["elevated"])
    return


def test_reads_never_elevate():
    """
        ``get_auth_config``, ``list_auth_methods``, and
        ``validate_auth_config`` invoke the runner with
        ``elevated=False``.
    """
    runner = _build_default_runner()
    manager = _build_manager(runner=runner, elevated=True)

    manager.get_auth_config(subsystem=UBUNTU_AUTH_LOGIN)
    manager.list_auth_methods()
    manager.validate_auth_config(subsystem=UBUNTU_AUTH_LOGIN)

    for call in runner.calls:
        args = call["args"]
        if args[0] in ("cat", "ls"):
            testbase.assert_false(call["elevated"])
        if args[:2] == ["pam-auth-update", "--list"]:
            testbase.assert_false(call["elevated"])
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero runner exit on a write surfaces as
        ``status=FAILED`` with ``stderr`` populated.
    """
    runner = _build_default_runner()
    runner.results[("tee", "/etc/pam.d/login")] = _fail(
        stderr="tee: permission denied",
        exit_code=1,
    )
    manager = _build_manager(runner=runner)

    new_config = UbuntuAuthConfig(
        subsystem="login",
        entries=[
            UbuntuAuthEntry(
                module_type="auth",
                control="required",
                module_path="pam_unix.so",
                args=[],
            ),
        ],
    )
    result = manager.set_auth_config(
        subsystem=UBUNTU_AUTH_LOGIN, config=new_config,
    )

    testbase.assert_equal(result.status, UbuntuAuthActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_true("permission denied" in result.stderr)
    return


def test_pam_stack_parser_handles_includes():
    """
        ``_parse_pam_stack`` produces a single ``@include`` entry for
        an ``@include common-auth`` directive.
    """
    content = "@include common-auth\nauth required pam_unix.so\n"
    entries = _parse_pam_stack(content=content)

    testbase.assert_equal(len(entries), 2)
    testbase.assert_equal(entries[0].module_type, "@include")
    testbase.assert_equal(entries[0].module_path, "common-auth")
    testbase.assert_equal(entries[0].control, "")
    return


def test_pam_stack_parser_handles_bracketed_control():
    """
        ``_parse_pam_stack`` collects a ``[bracketed]`` control into
        a single token.
    """
    content = (
        "password [success=ok default=ignore] pam_unix.so try_first_pass\n"
    )
    entries = _parse_pam_stack(content=content)

    testbase.assert_equal(len(entries), 1)
    testbase.assert_equal(entries[0].module_type, "password")
    testbase.assert_equal(entries[0].control, "[success=ok default=ignore]")
    testbase.assert_equal(entries[0].module_path, "pam_unix.so")
    testbase.assert_equal(entries[0].args, ["try_first_pass"])
    return


def test_render_round_trip_is_stable():
    """
        ``_parse_pam_stack`` followed by ``_render_pam_stack`` yields
        content that re-parses to the same entries.
    """
    original = (
        "auth required pam_unix.so\n"
        "@include common-auth\n"
        "password [success=ok default=ignore] pam_unix.so try_first_pass\n"
    )
    entries = _parse_pam_stack(content=original)
    rendered = _render_pam_stack(entries=entries)
    re_parsed = _parse_pam_stack(content=rendered)

    testbase.assert_equal(len(entries), len(re_parsed))
    for original_entry, parsed_entry in zip(entries, re_parsed):
        testbase.assert_equal(
            original_entry.module_type, parsed_entry.module_type,
        )
        testbase.assert_equal(
            original_entry.control, parsed_entry.control,
        )
        testbase.assert_equal(
            original_entry.module_path, parsed_entry.module_path,
        )
        testbase.assert_equal(original_entry.args, parsed_entry.args)
    return


def test_pam_auth_update_list_parser_handles_old_format():
    """
        ``_parse_pam_auth_update_list`` is tolerant of both the
        modern bracketed-state output and an older trailing-token
        schema.
    """
    old_text = (
        "unix    Unix authentication                Enabled\n"
        "krb5    Kerberos authentication            Disabled\n"
    )
    result = _parse_pam_auth_update_list(text=old_text)

    testbase.assert_true("unix" in result)
    testbase.assert_true("krb5" in result)
    unix_enabled, _ = result["unix"]
    krb5_enabled, _ = result["krb5"]
    testbase.assert_true(unix_enabled)
    testbase.assert_false(krb5_enabled)
    return


def test_query_error_raised_for_unexpected_failure():
    """
        A ``cat`` failure with non-"not found" stderr surfaces as
        :class:`UbuntuAuthenticationQueryError`.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", "/etc/pam.d/login")] = _fail(
        stderr="cat: I/O error reading file",
        exit_code=2,
    )
    manager = _build_manager(runner=runner)

    raised = False
    try:
        manager.get_auth_config(subsystem=UBUNTU_AUTH_LOGIN)
    except UbuntuAuthenticationQueryError:
        raised = True
    testbase.assert_true(raised)
    return
