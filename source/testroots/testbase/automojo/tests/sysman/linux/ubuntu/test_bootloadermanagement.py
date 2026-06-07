"""
    Tests for :class:`UbuntuBootloaderManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by
    argv.  No live ``update-grub`` / ``grub-mkconfig`` / ``bootctl`` is
    invoked.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.bootloadermanagement import (
    UbuntuBootEntryInfo,
    UbuntuBootEntrySpec,
    UbuntuBootloaderActionStatus,
    UbuntuBootloaderBackend,
    UbuntuBootloaderManager,
    UbuntuBootloaderManagerConfig,
    UbuntuBootloaderQueryError,
)


_GRUB_DEFAULTS = "/etc/default/grub"
_GRUB_CUSTOM = "/etc/grub.d/40_custom"
_GRUB_CFG = "/boot/grub/grub.cfg"
_LOADER_ENTRIES_DIR = "/boot/loader/entries"


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
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(
            exit_code=0,
            stdout="",
            stderr="",
        )
        return rtnval


def _ok(stdout: str = "") -> RunResult:
    """
        Build an exit-code-zero :class:`RunResult`.
    """
    rtnval = RunResult(
        exit_code=0,
        stdout=stdout,
        stderr="",
    )
    return rtnval


def _fail(stderr: str = "boom", exit_code: int = 1) -> RunResult:
    """
        Build a non-zero :class:`RunResult`.
    """
    rtnval = RunResult(
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
    )
    return rtnval


def _make_grub2_manager(
    *,
    runner: _MockCommandRunner,
    elevated: bool = False,
    dry_run: bool = False,
) -> UbuntuBootloaderManager:
    """
        Construct a GRUB2-backed manager wired to ``runner``.
    """
    config = UbuntuBootloaderManagerConfig(
        dry_run=dry_run,
        elevated=elevated,
        backend=UbuntuBootloaderBackend.GRUB2,
        grub_defaults=_GRUB_DEFAULTS,
        grub_custom=_GRUB_CUSTOM,
        grub_cfg=_GRUB_CFG,
        loader_entries_dir=_LOADER_ENTRIES_DIR,
    )
    rtnval = UbuntuBootloaderManager(runner=runner, config=config)
    return rtnval


def _make_systemd_manager(
    *,
    runner: _MockCommandRunner,
    elevated: bool = False,
    dry_run: bool = False,
) -> UbuntuBootloaderManager:
    """
        Construct a systemd-boot-backed manager wired to ``runner``.
    """
    config = UbuntuBootloaderManagerConfig(
        dry_run=dry_run,
        elevated=elevated,
        backend=UbuntuBootloaderBackend.SYSTEMD_BOOT,
        grub_defaults=_GRUB_DEFAULTS,
        grub_custom=_GRUB_CUSTOM,
        grub_cfg=_GRUB_CFG,
        loader_entries_dir=_LOADER_ENTRIES_DIR,
    )
    rtnval = UbuntuBootloaderManager(runner=runner, config=config)
    return rtnval


_GRUB_CFG_SAMPLE = """\
# Auto-generated grub.cfg
set default="0"
set timeout=5

menuentry 'Ubuntu' --class ubuntu --class gnu-linux {
    linux /boot/vmlinuz-6.8.0-31-generic root=UUID=abc ro quiet splash
    initrd /boot/initrd.img-6.8.0-31-generic
}
submenu 'Advanced options for Ubuntu' {
    menuentry 'Ubuntu, with Linux 6.8.0-31-generic' {
        linux /boot/vmlinuz-6.8.0-31-generic
    }
    menuentry 'Ubuntu, with Linux 6.8.0-31-generic (recovery mode)' {
        linux /boot/vmlinuz-6.8.0-31-generic single
    }
}
"""

_BOOTCTL_LIST_SAMPLE = """\
title: Ubuntu (default)
   id: ubuntu-6.8.0-31.conf
source: /boot/loader/entries/ubuntu-6.8.0-31.conf
linux: /boot/vmlinuz-6.8.0-31-generic
initrd: /boot/initrd.img-6.8.0-31-generic
options: root=UUID=abc ro quiet splash

title: Ubuntu Recovery
   id: ubuntu-recovery.conf
source: /boot/loader/entries/ubuntu-recovery.conf
linux: /boot/vmlinuz-6.8.0-31-generic
initrd: /boot/initrd.img-6.8.0-31-generic
options: single
"""

_BOOTCTL_STATUS_SAMPLE = """\
System:
     Firmware: UEFI 2.70 (American Megatrends)
  Secure Boot: enabled
   Setup Mode: user

Default Boot Loader Entry: ubuntu-6.8.0-31.conf
"""


# ----------------------------------------------------------------------
# list_entries
# ----------------------------------------------------------------------


def test_list_entries_parses_grub_cfg_output():
    """
        ``list_entries`` parses ``grub.cfg`` and returns one entry per
        ``menuentry`` directive (including submenu-qualified ones).
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _GRUB_CFG)] = _ok(stdout=_GRUB_CFG_SAMPLE)
    runner.results[("cat", _GRUB_DEFAULTS)] = _ok(stdout='GRUB_DEFAULT="0"\n')
    manager = _make_grub2_manager(runner=runner)

    entries = manager.list_entries()

    titles = [entry.name for entry in entries]
    testbase.assert_equal(len(entries), 3)
    testbase.assert_equal(titles[0], "Ubuntu")
    testbase.assert_equal(
        titles[1], "Ubuntu, with Linux 6.8.0-31-generic",
    )
    testbase.assert_equal(entries[0].is_default, True)
    testbase.assert_equal(entries[1].is_default, False)
    submenu_qualified = entries[1].id
    testbase.assert_equal(
        submenu_qualified,
        "Advanced options for Ubuntu>Ubuntu, with Linux 6.8.0-31-generic",
    )


def test_list_entries_systemd_boot_parses_bootctl_output():
    """
        ``list_entries`` parses ``bootctl list`` output when the
        backend is SYSTEMD_BOOT.
    """
    runner = _MockCommandRunner()
    runner.results[("bootctl", "list")] = _ok(stdout=_BOOTCTL_LIST_SAMPLE)
    manager = _make_systemd_manager(runner=runner)

    entries = manager.list_entries()

    testbase.assert_equal(len(entries), 2)
    testbase.assert_equal(entries[0].id, "ubuntu-6.8.0-31")
    testbase.assert_equal(entries[0].is_default, True)
    testbase.assert_equal(
        entries[0].kernel, "/boot/vmlinuz-6.8.0-31-generic",
    )
    testbase.assert_equal(entries[1].is_default, False)


# ----------------------------------------------------------------------
# get_default_entry
# ----------------------------------------------------------------------


def test_get_default_entry_resolves_id():
    """
        ``get_default_entry`` resolves the GRUB_DEFAULT token (numeric
        index, exact id, or bare title).
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _GRUB_CFG)] = _ok(stdout=_GRUB_CFG_SAMPLE)
    runner.results[("cat", _GRUB_DEFAULTS)] = _ok(
        stdout='GRUB_DEFAULT="Ubuntu"\n',
    )
    manager = _make_grub2_manager(runner=runner)

    default_entry = manager.get_default_entry()

    testbase.assert_equal(default_entry.name, "Ubuntu")
    testbase.assert_equal(default_entry.is_default, True)


def test_get_default_entry_raises_when_unresolvable():
    """
        ``get_default_entry`` raises :class:`UbuntuBootloaderQueryError`
        when no entries are present.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _GRUB_CFG)] = _ok(stdout="")
    runner.results[("cat", _GRUB_DEFAULTS)] = _ok(stdout="")
    manager = _make_grub2_manager(runner=runner)

    raised = False
    try:
        manager.get_default_entry()
    except UbuntuBootloaderQueryError:
        raised = True

    testbase.assert_equal(raised, True)


# ----------------------------------------------------------------------
# set_default_entry
# ----------------------------------------------------------------------


def test_set_default_entry_invokes_update_grub():
    """
        ``set_default_entry`` rewrites GRUB_DEFAULT then invokes
        ``update-grub``.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _GRUB_DEFAULTS)] = _ok(
        stdout='GRUB_DEFAULT="0"\n',
    )
    manager = _make_grub2_manager(runner=runner, elevated=True)

    result = manager.set_default_entry(entry_id="Ubuntu")

    testbase.assert_equal(result.status, UbuntuBootloaderActionStatus.SUCCESS)
    last_call = runner.calls[-1]
    testbase.assert_equal(last_call["args"], ["update-grub"])
    tee_calls = [
        call for call in runner.calls
        if call["args"][:1] == ["tee"]
        and call["args"][1] == _GRUB_DEFAULTS
    ]
    testbase.assert_equal(len(tee_calls), 1)
    written = tee_calls[0]["stdin"]
    testbase.assert_equal('GRUB_DEFAULT="Ubuntu"' in written, True)


def test_set_default_entry_skipped_when_already_default():
    """
        ``set_default_entry`` returns SKIPPED when GRUB_DEFAULT
        already matches the requested token.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _GRUB_DEFAULTS)] = _ok(
        stdout='GRUB_DEFAULT="Ubuntu"\n',
    )
    manager = _make_grub2_manager(runner=runner)

    result = manager.set_default_entry(entry_id="Ubuntu")

    testbase.assert_equal(result.status, UbuntuBootloaderActionStatus.SKIPPED)
    update_calls = [
        call for call in runner.calls if call["args"] == ["update-grub"]
    ]
    testbase.assert_equal(len(update_calls), 0)


def test_set_default_entry_systemd_boot_invokes_bootctl():
    """
        ``set_default_entry`` runs ``bootctl set-default`` for the
        systemd-boot backend.
    """
    runner = _MockCommandRunner()
    runner.results[("bootctl", "status")] = _ok(
        stdout="Default Boot Loader Entry: other.conf\n",
    )
    manager = _make_systemd_manager(runner=runner, elevated=True)

    result = manager.set_default_entry(entry_id="ubuntu-6.8.0-31")

    testbase.assert_equal(result.status, UbuntuBootloaderActionStatus.SUCCESS)
    last_call = runner.calls[-1]
    testbase.assert_equal(
        last_call["args"],
        ["bootctl", "set-default", "ubuntu-6.8.0-31"],
    )


# ----------------------------------------------------------------------
# add_entry
# ----------------------------------------------------------------------


def test_add_entry_invokes_update_grub():
    """
        ``add_entry`` appends a sentinel-bracketed block to
        ``/etc/grub.d/40_custom`` then invokes ``update-grub``.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _GRUB_CUSTOM)] = _ok(stdout="")
    manager = _make_grub2_manager(runner=runner, elevated=True)

    spec = UbuntuBootEntrySpec(
        id="rescue-kernel",
        name="Rescue Kernel",
        kernel="/boot/vmlinuz-rescue",
        initrd="/boot/initrd.img-rescue",
        cmdline="ro quiet single",
        device="hd0,gpt2",
    )
    result = manager.add_entry(spec=spec)

    testbase.assert_equal(result.status, UbuntuBootloaderActionStatus.SUCCESS)
    tee_calls = [
        call for call in runner.calls
        if call["args"][:1] == ["tee"]
        and call["args"][1] == _GRUB_CUSTOM
    ]
    testbase.assert_equal(len(tee_calls), 1)
    written = tee_calls[0]["stdin"]
    testbase.assert_equal(
        "codecraftai-bootloader-management:begin rescue-kernel" in written,
        True,
    )
    testbase.assert_equal("menuentry 'Rescue Kernel'" in written, True)
    testbase.assert_equal(
        "linux /boot/vmlinuz-rescue ro quiet single" in written, True,
    )
    last_call = runner.calls[-1]
    testbase.assert_equal(last_call["args"], ["update-grub"])


def test_add_entry_skipped_when_present():
    """
        ``add_entry`` skips when an entry with the same id is already
        present in ``/etc/grub.d/40_custom``.
    """
    existing_block = (
        "# codecraftai-bootloader-management:begin rescue-kernel\n"
        "menuentry 'Rescue Kernel' {\n"
        "    linux /boot/vmlinuz-rescue\n"
        "}\n"
        "# codecraftai-bootloader-management:end rescue-kernel\n"
    )
    runner = _MockCommandRunner()
    runner.results[("cat", _GRUB_CUSTOM)] = _ok(stdout=existing_block)
    manager = _make_grub2_manager(runner=runner)

    spec = UbuntuBootEntrySpec(
        id="rescue-kernel",
        name="Rescue Kernel",
        kernel="/boot/vmlinuz-rescue",
    )
    result = manager.add_entry(spec=spec)

    testbase.assert_equal(result.status, UbuntuBootloaderActionStatus.SKIPPED)
    update_calls = [
        call for call in runner.calls if call["args"] == ["update-grub"]
    ]
    testbase.assert_equal(len(update_calls), 0)


def test_add_entry_dry_run_short_circuits():
    """
        ``add_entry`` returns DRY_RUN without invoking the runner when
        ``config.dry_run`` is True.
    """
    runner = _MockCommandRunner()
    manager = _make_grub2_manager(runner=runner, dry_run=True)

    spec = UbuntuBootEntrySpec(
        id="rescue-kernel",
        name="Rescue Kernel",
        kernel="/boot/vmlinuz-rescue",
    )
    result = manager.add_entry(spec=spec)

    testbase.assert_equal(result.status, UbuntuBootloaderActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)


def test_add_entry_systemd_boot_invokes_tee():
    """
        ``add_entry`` writes a loader-conf file via ``tee`` for the
        systemd-boot backend.
    """
    runner = _MockCommandRunner()
    runner.default_result = _fail(stderr="No such file or directory")
    runner.results[("bootctl", "list")] = _ok(stdout="")
    manager = _make_systemd_manager(runner=runner, elevated=True)

    spec = UbuntuBootEntrySpec(
        id="rescue-kernel",
        name="Rescue Kernel",
        kernel="/boot/vmlinuz-rescue",
        initrd="/boot/initrd.img-rescue",
        cmdline="ro quiet",
    )
    runner.results[
        ("cat", "{}/{}.conf".format(_LOADER_ENTRIES_DIR, spec.id))
    ] = _fail(stderr="No such file or directory")
    runner.results[
        ("tee", "{}/{}.conf".format(_LOADER_ENTRIES_DIR, spec.id))
    ] = _ok(stdout="")
    result = manager.add_entry(spec=spec)

    testbase.assert_equal(result.status, UbuntuBootloaderActionStatus.SUCCESS)
    tee_calls = [
        call for call in runner.calls
        if call["args"][:1] == ["tee"]
        and call["args"][1].endswith("rescue-kernel.conf")
    ]
    testbase.assert_equal(len(tee_calls), 1)
    body = tee_calls[0]["stdin"]
    testbase.assert_equal("title    Rescue Kernel" in body, True)
    testbase.assert_equal("linux    /boot/vmlinuz-rescue" in body, True)


# ----------------------------------------------------------------------
# remove_entry
# ----------------------------------------------------------------------


def test_remove_entry_invokes_update_grub():
    """
        ``remove_entry`` strips the sentinel-bracketed block then
        invokes ``update-grub``.
    """
    existing_block = (
        "menuentry 'Existing' { linux /boot/vmlinuz }\n"
        "# codecraftai-bootloader-management:begin rescue-kernel\n"
        "menuentry 'Rescue Kernel' {\n"
        "    linux /boot/vmlinuz-rescue\n"
        "}\n"
        "# codecraftai-bootloader-management:end rescue-kernel\n"
    )
    runner = _MockCommandRunner()
    runner.results[("cat", _GRUB_CUSTOM)] = _ok(stdout=existing_block)
    manager = _make_grub2_manager(runner=runner, elevated=True)

    result = manager.remove_entry(entry_id="rescue-kernel")

    testbase.assert_equal(result.status, UbuntuBootloaderActionStatus.SUCCESS)
    tee_calls = [
        call for call in runner.calls
        if call["args"][:1] == ["tee"]
        and call["args"][1] == _GRUB_CUSTOM
    ]
    testbase.assert_equal(len(tee_calls), 1)
    rewritten = tee_calls[0]["stdin"]
    testbase.assert_equal(
        "codecraftai-bootloader-management:begin rescue-kernel" in rewritten,
        False,
    )
    testbase.assert_equal("menuentry 'Existing'" in rewritten, True)


def test_remove_entry_skipped_when_absent():
    """
        ``remove_entry`` returns SKIPPED when no matching block is
        present.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _GRUB_CUSTOM)] = _ok(
        stdout="menuentry 'Other' {}\n",
    )
    manager = _make_grub2_manager(runner=runner)

    result = manager.remove_entry(entry_id="missing-id")

    testbase.assert_equal(result.status, UbuntuBootloaderActionStatus.SKIPPED)


# ----------------------------------------------------------------------
# update_bootloader_config / regenerate_config
# ----------------------------------------------------------------------


def test_update_bootloader_config_invokes_update_grub():
    """
        ``update_bootloader_config`` runs ``update-grub`` for GRUB2
        backends.
    """
    runner = _MockCommandRunner()
    manager = _make_grub2_manager(runner=runner, elevated=True)

    result = manager.update_bootloader_config()

    testbase.assert_equal(result.status, UbuntuBootloaderActionStatus.SUCCESS)
    testbase.assert_equal(runner.calls[-1]["args"], ["update-grub"])
    testbase.assert_equal(runner.calls[-1]["elevated"], True)


def test_regenerate_config_invokes_grub_mkconfig():
    """
        ``regenerate_config`` runs ``grub-mkconfig -o <grub_cfg>`` for
        GRUB2 backends.
    """
    runner = _MockCommandRunner()
    manager = _make_grub2_manager(runner=runner, elevated=True)

    result = manager.regenerate_config()

    testbase.assert_equal(result.status, UbuntuBootloaderActionStatus.SUCCESS)
    testbase.assert_equal(
        runner.calls[-1]["args"],
        ["grub-mkconfig", "-o", _GRUB_CFG],
    )


def test_regenerate_config_systemd_boot_invokes_bootctl_install():
    """
        ``regenerate_config`` runs ``bootctl install`` for the
        systemd-boot backend.
    """
    runner = _MockCommandRunner()
    manager = _make_systemd_manager(runner=runner, elevated=True)

    result = manager.regenerate_config()

    testbase.assert_equal(result.status, UbuntuBootloaderActionStatus.SUCCESS)
    testbase.assert_equal(
        runner.calls[-1]["args"], ["bootctl", "install"],
    )


# ----------------------------------------------------------------------
# Elevation policy
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Writes pass ``elevated=True`` to the runner when
        ``config.elevated`` is True.
    """
    runner = _MockCommandRunner()
    manager = _make_grub2_manager(runner=runner, elevated=True)

    manager.update_bootloader_config()

    last_call = runner.calls[-1]
    testbase.assert_equal(last_call["args"], ["update-grub"])
    testbase.assert_equal(last_call["elevated"], True)


def test_reads_never_elevate():
    """
        Reads (``cat`` for grub.cfg / GRUB defaults / 40_custom and
        ``bootctl list``) never pass ``elevated=True`` regardless of
        config.
    """
    runner = _MockCommandRunner()
    runner.results[("cat", _GRUB_CFG)] = _ok(stdout=_GRUB_CFG_SAMPLE)
    runner.results[("cat", _GRUB_DEFAULTS)] = _ok(stdout='GRUB_DEFAULT="0"\n')
    manager = _make_grub2_manager(runner=runner, elevated=True)

    manager.list_entries()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)


# ----------------------------------------------------------------------
# Failed-command surfacing
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        A failed ``update-grub`` invocation surfaces the runner stderr
        on a FAILED result.
    """
    runner = _MockCommandRunner()
    runner.results[("update-grub",)] = _fail(
        stderr="grub-mkconfig: command not found",
        exit_code=127,
    )
    manager = _make_grub2_manager(runner=runner, elevated=True)

    result = manager.update_bootloader_config()

    testbase.assert_equal(result.status, UbuntuBootloaderActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 127)
    testbase.assert_equal(
        "grub-mkconfig: command not found" in (result.stderr or ""),
        True,
    )
