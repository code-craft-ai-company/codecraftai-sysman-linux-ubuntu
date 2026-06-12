"""
    Tests for :class:`UbuntuLocalizationManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``localectl`` / ``timedatectl`` / ``locale`` is
    invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.localizationmanagement import (
    UbuntuLocalizationActionStatus,
    UbuntuLocalizationManager,
    UbuntuLocalizationManagerConfig,
    UbuntuLocalizationQueryError,
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


def _localectl_status_payload() -> str:
    """
        Build a representative ``localectl status`` payload.
    """
    rtnval = (
        "   System Locale: LANG=en_US.UTF-8 LC_TIME=en_GB.UTF-8\n"
        "       VC Keymap: us\n"
        "      X11 Layout: us\n"
        "     X11 Variant: dvorak\n"
        "       X11 Model: pc105\n"
    )
    return rtnval


def _localectl_status_minimal() -> str:
    """
        Build a representative ``localectl status`` payload that omits
        every X11 line (typical on a console-only cloud image).
    """
    rtnval = (
        "   System Locale: LANG=C.UTF-8\n"
        "       VC Keymap: n/a\n"
    )
    return rtnval


def _locale_a_payload() -> str:
    """
        Build a representative ``locale -a`` payload.
    """
    rtnval = (
        "C\n"
        "C.UTF-8\n"
        "POSIX\n"
        "en_GB.UTF-8\n"
        "en_US.UTF-8\n"
    )
    return rtnval


def _list_timezones_payload() -> str:
    """
        Build a representative ``timedatectl list-timezones`` payload.
    """
    rtnval = (
        "Africa/Abidjan\n"
        "America/Los_Angeles\n"
        "America/New_York\n"
        "Asia/Tokyo\n"
        "Europe/London\n"
        "UTC\n"
    )
    return rtnval


def _list_keymaps_payload() -> str:
    """
        Build a representative ``localectl list-keymaps`` payload.
    """
    rtnval = (
        "us\n"
        "us-acentos\n"
        "uk\n"
        "de\n"
        "fr\n"
        "es\n"
    )
    return rtnval


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------


def test_get_locale_parses_localectl_output():
    """
        get_locale parses ``localectl status`` and derives country /
        encoding.
    """
    runner = _MockCommandRunner()
    runner.results[("localectl", "status")] = RunResult(
        exit_code=0, stdout=_localectl_status_payload(), stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(),
    )
    info = manager.get_locale()

    testbase.assert_equal(info.language, "en_US.UTF-8")
    testbase.assert_equal(info.country, "US")
    testbase.assert_equal(info.encoding, "UTF-8")
    testbase.assert_equal(info.lc_time, "en_GB.UTF-8")
    testbase.assert_equal(info.lc_messages, None)
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


def test_get_keymap_parses_localectl_output():
    """
        get_keymap parses VC + X11 lines from ``localectl status``.
    """
    runner = _MockCommandRunner()
    runner.results[("localectl", "status")] = RunResult(
        exit_code=0, stdout=_localectl_status_payload(), stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(),
    )
    info = manager.get_keymap()

    testbase.assert_equal(info.name, "us")
    testbase.assert_equal(info.layout, "us")
    testbase.assert_equal(info.variant, "dvorak")
    testbase.assert_equal(info.model, "pc105")
    return


def test_get_timezone_parses_timedatectl_output():
    """
        get_timezone parses ``timedatectl show --property=Timezone
        --value``.
    """
    runner = _MockCommandRunner()
    show_argv = (
        "timedatectl", "show", "--property=Timezone", "--value",
    )
    runner.results[show_argv] = RunResult(
        exit_code=0, stdout="America/Los_Angeles\n", stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(),
    )
    tz = manager.get_timezone()

    testbase.assert_equal(tz, "America/Los_Angeles")
    return


def test_list_supported_locales_returns_list():
    """
        list_supported_locales parses ``locale -a`` into a list.
    """
    runner = _MockCommandRunner()
    runner.results[("locale", "-a")] = RunResult(
        exit_code=0, stdout=_locale_a_payload(), stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(),
    )
    locales = manager.list_supported_locales()

    testbase.assert_equal(len(locales), 5)
    testbase.assert_equal("en_US.UTF-8" in locales, True)
    testbase.assert_equal("C.UTF-8" in locales, True)
    return


def test_get_supported_timezones_parses_list():
    """
        get_supported_timezones parses ``timedatectl list-timezones``.
    """
    runner = _MockCommandRunner()
    runner.results[("timedatectl", "list-timezones")] = RunResult(
        exit_code=0, stdout=_list_timezones_payload(), stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(),
    )
    zones = manager.get_supported_timezones()

    testbase.assert_equal("America/Los_Angeles" in zones, True)
    testbase.assert_equal("UTC" in zones, True)
    testbase.assert_equal(len(zones), 6)
    return


def test_get_supported_keymaps_returns_list():
    """
        get_supported_keymaps parses ``localectl list-keymaps``.
    """
    runner = _MockCommandRunner()
    runner.results[("localectl", "list-keymaps")] = RunResult(
        exit_code=0, stdout=_list_keymaps_payload(), stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(),
    )
    keymaps = manager.get_supported_keymaps()

    testbase.assert_equal("us" in keymaps, True)
    testbase.assert_equal("uk" in keymaps, True)
    testbase.assert_equal(len(keymaps), 6)
    return


# ----------------------------------------------------------------------
# Write: set_locale
# ----------------------------------------------------------------------


def test_set_locale_invokes_localectl():
    """
        set_locale dispatches ``localectl set-locale LANG=<value>`` when
        the current LANG differs.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("localectl", "status")] = RunResult(
        exit_code=0, stdout=_localectl_status_minimal(), stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(elevated=True),
    )
    result = manager.set_locale("en_US.UTF-8")

    testbase.assert_equal(result.status, UbuntuLocalizationActionStatus.SUCCESS)
    testbase.assert_equal(
        result.command,
        ["localectl", "set-locale", "LANG=en_US.UTF-8"],
    )
    testbase.assert_equal(result.setting, "locale")
    testbase.assert_equal(result.previous_value, "C.UTF-8")
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["localectl", "set-locale"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_set_locale_skipped_when_match():
    """
        set_locale returns SKIPPED when the current LANG already matches.
    """
    runner = _MockCommandRunner()
    runner.results[("localectl", "status")] = RunResult(
        exit_code=0, stdout=_localectl_status_payload(), stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(),
    )
    result = manager.set_locale("en_US.UTF-8")

    testbase.assert_equal(result.status, UbuntuLocalizationActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["localectl", "set-locale"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_set_locale_dry_run_short_circuits():
    """
        set_locale with dry_run=True returns DRY_RUN and does not invoke
        the runner at all.
    """
    runner = _MockCommandRunner()
    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(dry_run=True),
    )
    result = manager.set_locale("en_US.UTF-8")

    testbase.assert_equal(result.status, UbuntuLocalizationActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command,
        ["localectl", "set-locale", "LANG=en_US.UTF-8"],
    )
    return


def test_set_locale_with_lc_overrides_includes_kv_pairs():
    """
        set_locale forwards LC_* overrides as additional positional args.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("localectl", "status")] = RunResult(
        exit_code=0, stdout=_localectl_status_minimal(), stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(),
    )
    result = manager.set_locale(
        "en_US.UTF-8",
        lc_overrides={"LC_TIME": "en_GB.UTF-8"},
    )

    testbase.assert_equal(result.status, UbuntuLocalizationActionStatus.SUCCESS)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["localectl", "set-locale"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        [
            "localectl", "set-locale",
            "LANG=en_US.UTF-8", "LC_TIME=en_GB.UTF-8",
        ],
    )
    return


# ----------------------------------------------------------------------
# Write: set_keymap
# ----------------------------------------------------------------------


def test_set_keymap_invokes_localectl():
    """
        set_keymap dispatches ``localectl set-keymap <name>`` when the
        current VC keymap differs.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("localectl", "status")] = RunResult(
        exit_code=0, stdout=_localectl_status_minimal(), stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(elevated=True),
    )
    result = manager.set_keymap("us")

    testbase.assert_equal(result.status, UbuntuLocalizationActionStatus.SUCCESS)
    testbase.assert_equal(
        result.command, ["localectl", "set-keymap", "us"],
    )
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["localectl", "set-keymap"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_set_keymap_skipped_when_match():
    """
        set_keymap returns SKIPPED when the current VC keymap matches.
    """
    runner = _MockCommandRunner()
    runner.results[("localectl", "status")] = RunResult(
        exit_code=0, stdout=_localectl_status_payload(), stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(),
    )
    result = manager.set_keymap("us")

    testbase.assert_equal(result.status, UbuntuLocalizationActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["localectl", "set-keymap"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_set_keymap_with_x11_invokes_set_x11_keymap():
    """
        set_keymap dispatches ``localectl set-x11-keymap`` when an X11
        layout is supplied and differs from the current state.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("localectl", "status")] = RunResult(
        exit_code=0, stdout=_localectl_status_minimal(), stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(elevated=True),
    )
    result = manager.set_keymap("us", x11_layout="us")

    testbase.assert_equal(result.status, UbuntuLocalizationActionStatus.SUCCESS)
    x11_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["localectl", "set-x11-keymap"]
    ]
    testbase.assert_equal(len(x11_calls), 1)
    testbase.assert_equal(
        x11_calls[0]["args"], ["localectl", "set-x11-keymap", "us"],
    )
    return


# ----------------------------------------------------------------------
# Write: set_timezone
# ----------------------------------------------------------------------


def test_set_timezone_invokes_timedatectl():
    """
        set_timezone dispatches ``timedatectl set-timezone <iana>`` when
        the current timezone differs.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    show_argv = (
        "timedatectl", "show", "--property=Timezone", "--value",
    )
    runner.results[show_argv] = RunResult(
        exit_code=0, stdout="UTC\n", stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(elevated=True),
    )
    result = manager.set_timezone("America/Los_Angeles")

    testbase.assert_equal(result.status, UbuntuLocalizationActionStatus.SUCCESS)
    testbase.assert_equal(
        result.command,
        ["timedatectl", "set-timezone", "America/Los_Angeles"],
    )
    testbase.assert_equal(result.setting, "timezone")
    testbase.assert_equal(result.previous_value, "UTC")
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["timedatectl", "set-timezone"]
    ]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_set_timezone_skipped_when_match():
    """
        set_timezone returns SKIPPED when the current timezone already
        matches.
    """
    runner = _MockCommandRunner()
    show_argv = (
        "timedatectl", "show", "--property=Timezone", "--value",
    )
    runner.results[show_argv] = RunResult(
        exit_code=0, stdout="America/Los_Angeles\n", stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(),
    )
    result = manager.set_timezone("America/Los_Angeles")

    testbase.assert_equal(result.status, UbuntuLocalizationActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["timedatectl", "set-timezone"]
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_set_timezone_dry_run_short_circuits():
    """
        set_timezone with dry_run=True returns DRY_RUN without invoking
        the runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(dry_run=True),
    )
    result = manager.set_timezone("America/New_York")

    testbase.assert_equal(result.status, UbuntuLocalizationActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command,
        ["timedatectl", "set-timezone", "America/New_York"],
    )
    return


# ----------------------------------------------------------------------
# Cross-cutting: elevation, error surface
# ----------------------------------------------------------------------


def test_writes_elevate_per_policy():
    """
        Every write verb passes ``elevated=True`` when the config is
        configured to elevate; reads in the same flow stay unelevated.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[("localectl", "status")] = RunResult(
        exit_code=0, stdout=_localectl_status_minimal(), stderr="",
    )
    show_argv = (
        "timedatectl", "show", "--property=Timezone", "--value",
    )
    runner.results[show_argv] = RunResult(
        exit_code=0, stdout="UTC\n", stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(elevated=True),
    )
    manager.set_locale("en_US.UTF-8")
    manager.set_keymap("us")
    manager.set_timezone("America/Los_Angeles")

    for call in runner.calls:
        argv0 = call["args"][0]
        argv1 = call["args"][1] if len(call["args"]) > 1 else ""
        if argv0 == "localectl" and argv1 == "status":
            testbase.assert_equal(call["elevated"], False)
        elif argv0 == "timedatectl" and argv1 == "show":
            testbase.assert_equal(call["elevated"], False)
        elif argv0 == "localectl" and argv1.startswith("set-"):
            testbase.assert_equal(call["elevated"], True)
        elif argv0 == "timedatectl" and argv1 == "set-timezone":
            testbase.assert_equal(call["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read-only verbs never pass ``elevated=True`` even when the
        config is configured to elevate.
    """
    runner = _MockCommandRunner()
    runner.results[("localectl", "status")] = RunResult(
        exit_code=0, stdout=_localectl_status_payload(), stderr="",
    )
    runner.results[("locale", "-a")] = RunResult(
        exit_code=0, stdout=_locale_a_payload(), stderr="",
    )
    runner.results[("localectl", "list-keymaps")] = RunResult(
        exit_code=0, stdout=_list_keymaps_payload(), stderr="",
    )
    runner.results[("timedatectl", "list-timezones")] = RunResult(
        exit_code=0, stdout=_list_timezones_payload(), stderr="",
    )
    show_argv = (
        "timedatectl", "show", "--property=Timezone", "--value",
    )
    runner.results[show_argv] = RunResult(
        exit_code=0, stdout="UTC\n", stderr="",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(elevated=True),
    )
    manager.get_locale()
    manager.list_supported_locales()
    manager.get_keymap()
    manager.get_supported_keymaps()
    manager.get_timezone()
    manager.get_supported_timezones()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        A non-zero exit on a write maps to FAILED with the captured
        stderr / exit_code / command.
    """
    runner = _MockCommandRunner()
    show_argv = (
        "timedatectl", "show", "--property=Timezone", "--value",
    )
    runner.results[show_argv] = RunResult(
        exit_code=0, stdout="UTC\n", stderr="",
    )
    runner.results[
        ("timedatectl", "set-timezone", "Bogus/Zone")
    ] = RunResult(
        exit_code=1, stdout="",
        stderr="Failed to set time zone: Invalid or not installed time zone\n",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(elevated=True),
    )
    result = manager.set_timezone("Bogus/Zone")

    testbase.assert_equal(result.status, UbuntuLocalizationActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(
        "Invalid or not installed" in result.stderr, True,
    )
    testbase.assert_equal(
        result.command,
        ["timedatectl", "set-timezone", "Bogus/Zone"],
    )
    return


def test_get_locale_raises_on_localectl_failure():
    """
        get_locale raises :class:`UbuntuLocalizationQueryError` when
        ``localectl status`` exits non-zero.
    """
    runner = _MockCommandRunner()
    runner.results[("localectl", "status")] = RunResult(
        exit_code=1, stdout="",
        stderr="Could not connect to system bus\n",
    )

    manager = UbuntuLocalizationManager(
        runner=runner,
        config=UbuntuLocalizationManagerConfig(),
    )
    raised = False
    try:
        manager.get_locale()
    except UbuntuLocalizationQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return
