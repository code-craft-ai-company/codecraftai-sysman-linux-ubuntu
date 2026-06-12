"""
    Tests for :class:`UbuntuEncryptionManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call (argv + elevation flag + stdin payload) and returns pre-seeded
    :class:`RunResult` values keyed by argv tuple.  No live
    ``cryptsetup`` binary is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.encryptionmanagement import (
    UbuntuEncryptedVolumeInfo,
    UbuntuEncryptionActionStatus,
    UbuntuEncryptionManager,
    UbuntuEncryptionManagerConfig,
    UbuntuEncryptionSpec,
    UbuntuEncryptionStatus,
    _DESTRUCTIVE_GUARD_DETAIL,
    _parse_cryptsetup_status,
    _parse_lsblk_crypto_rows,
    _parse_luks_dump,
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


# ----------------------------------------------------------------------
# Seed helpers
# ----------------------------------------------------------------------


_DEVICE = "/dev/sda3"
# The implementation derives the mapping name from the device basename
# (``/dev/sda3`` → ``sda3``), so seed helpers and argv assertions use
# the same value.
_NAME = "sda3"


_LUKS_DUMP_OUTPUT = (
    "LUKS header information for /dev/sda3\n"
    "\n"
    "Version:        2\n"
    "Cipher:         aes-xts-plain64\n"
    "MK bits:        512\n"
    "Keyslots:\n"
    "  0: luks2\n"
    "  1: luks2\n"
    "Tokens:\n"
    "Digests:\n"
)


_STATUS_ACTIVE_OUTPUT = (
    "/dev/mapper/cryptroot is active.\n"
    "  type:    LUKS2\n"
    "  cipher:  aes-xts-plain64\n"
    "  keysize: 512 bits\n"
    "  device:  /dev/sda3\n"
    "  offset:  32768 sectors\n"
    "  size:    100000 sectors\n"
    "  mode:    read/write\n"
)


def _seed_isluks(
    *,
    runner: _MockCommandRunner,
    device: str,
    is_luks: bool,
) -> None:
    """
        Seed ``cryptsetup isLuks <device>`` to the given outcome.
    """
    key = ("cryptsetup", "isLuks", device)
    if is_luks:
        runner.results[key] = RunResult(exit_code=0, stdout="", stderr="")
    else:
        runner.results[key] = RunResult(
            exit_code=1, stdout="", stderr="not a LUKS device",
        )
    return


def _seed_luksdump(
    *,
    runner: _MockCommandRunner,
    device: str,
    output: str,
) -> None:
    """
        Seed ``cryptsetup luksDump <device>`` with raw output.
    """
    key = ("cryptsetup", "luksDump", device)
    runner.results[key] = RunResult(exit_code=0, stdout=output, stderr="")
    return


def _seed_status_inactive(
    *,
    runner: _MockCommandRunner,
    name: str,
) -> None:
    """
        Seed ``cryptsetup status <name>`` to a non-zero result that
        signals "not an active mapping".
    """
    key = ("cryptsetup", "status", name)
    runner.results[key] = RunResult(
        exit_code=4, stdout="", stderr="device {} is inactive".format(name),
    )
    return


def _seed_status_active(
    *,
    runner: _MockCommandRunner,
    name: str,
    output: str,
) -> None:
    """
        Seed ``cryptsetup status <name>`` to active output.
    """
    key = ("cryptsetup", "status", name)
    runner.results[key] = RunResult(exit_code=0, stdout=output, stderr="")
    return


# ----------------------------------------------------------------------
# enable_encryption
# ----------------------------------------------------------------------


def test_enable_encryption_invokes_cryptsetup_luksformat():
    """
        enable_encryption dispatches ``cryptsetup luksFormat`` with
        the passphrase delivered via stdin.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_isluks(runner=runner, device=_DEVICE, is_luks=False)

    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(),
    )
    spec = UbuntuEncryptionSpec(passphrase="hunter2")
    result = manager.enable_encryption(volume=_DEVICE, spec=spec)

    testbase.assert_equal(result.status, UbuntuEncryptionActionStatus.SUCCESS)
    format_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["cryptsetup", "luksFormat"]
    ]
    testbase.assert_equal(len(format_calls), 1)
    testbase.assert_equal(format_calls[0]["stdin"], "hunter2")
    testbase.assert_equal(
        format_calls[0]["args"],
        [
            "cryptsetup", "luksFormat",
            "--batch-mode",
            "--type", "luks2",
            "--cipher", "aes-xts-plain64:sha256",
            "--key-size", "512",
            _DEVICE,
        ],
    )
    return


def test_enable_encryption_skipped_when_already_encrypted():
    """
        enable_encryption returns SKIPPED when ``cryptsetup isLuks``
        already reports the device as LUKS.
    """
    runner = _MockCommandRunner()
    _seed_isluks(runner=runner, device=_DEVICE, is_luks=True)

    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(),
    )
    spec = UbuntuEncryptionSpec(passphrase="hunter2")
    result = manager.enable_encryption(volume=_DEVICE, spec=spec)

    testbase.assert_equal(result.status, UbuntuEncryptionActionStatus.SKIPPED)
    format_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["cryptsetup", "luksFormat"]
    ]
    testbase.assert_equal(format_calls, [])
    return


def test_enable_encryption_dry_run_short_circuits():
    """
        Dry-run records the would-have-been argv without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(dry_run=True),
    )
    spec = UbuntuEncryptionSpec(passphrase="hunter2")
    result = manager.enable_encryption(volume=_DEVICE, spec=spec)

    testbase.assert_equal(result.status, UbuntuEncryptionActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(result.command[1], "luksFormat")
    return


# ----------------------------------------------------------------------
# disable_encryption
# ----------------------------------------------------------------------


def test_disable_encryption_requires_allow_destructive():
    """
        disable_encryption returns FAILED with the destructive-guard
        detail when ``allow_destructive`` is False.
    """
    runner = _MockCommandRunner()
    _seed_isluks(runner=runner, device=_DEVICE, is_luks=True)

    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(allow_destructive=False),
    )
    result = manager.disable_encryption(volume=_DEVICE)

    testbase.assert_equal(result.status, UbuntuEncryptionActionStatus.FAILED)
    testbase.assert_equal(result.detail, _DESTRUCTIVE_GUARD_DETAIL)
    erase_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["cryptsetup", "erase"]
    ]
    testbase.assert_equal(erase_calls, [])
    return


def test_disable_encryption_invokes_cryptsetup_erase_when_allowed():
    """
        disable_encryption dispatches ``cryptsetup erase`` when
        ``allow_destructive=True`` and the device is currently LUKS.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_isluks(runner=runner, device=_DEVICE, is_luks=True)

    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(allow_destructive=True),
    )
    result = manager.disable_encryption(volume=_DEVICE)

    testbase.assert_equal(result.status, UbuntuEncryptionActionStatus.SUCCESS)
    erase_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["cryptsetup", "erase"]
    ]
    testbase.assert_equal(len(erase_calls), 1)
    testbase.assert_equal(
        erase_calls[0]["args"], ["cryptsetup", "erase", _DEVICE],
    )
    return


def test_disable_encryption_skipped_when_already_plain():
    """
        disable_encryption returns SKIPPED when the device has no LUKS
        header.
    """
    runner = _MockCommandRunner()
    _seed_isluks(runner=runner, device=_DEVICE, is_luks=False)

    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(allow_destructive=True),
    )
    result = manager.disable_encryption(volume=_DEVICE)

    testbase.assert_equal(result.status, UbuntuEncryptionActionStatus.SKIPPED)
    return


# ----------------------------------------------------------------------
# get_encryption_status
# ----------------------------------------------------------------------


def test_get_encryption_status_parses_luksdump_and_status():
    """
        get_encryption_status merges ``luksDump`` and ``cryptsetup
        status`` output into a single record.
    """
    runner = _MockCommandRunner()
    _seed_luksdump(runner=runner, device=_DEVICE, output=_LUKS_DUMP_OUTPUT)
    _seed_status_active(
        runner=runner, name=_NAME, output=_STATUS_ACTIVE_OUTPUT,
    )

    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(),
    )
    status = manager.get_encryption_status(volume=_DEVICE)

    testbase.assert_equal(status.is_encrypted, True)
    testbase.assert_equal(status.is_unlocked, True)
    testbase.assert_equal(status.cipher, "aes-xts-plain64")
    testbase.assert_equal(status.key_size, 512)
    testbase.assert_equal(len(status.protector_types), 2)
    return


def test_get_encryption_status_handles_plain_device():
    """
        get_encryption_status returns ``is_encrypted=False`` when
        ``luksDump`` exits non-zero.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=1, stdout="", stderr="not a LUKS device",
        ),
    )
    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(),
    )
    status = manager.get_encryption_status(volume="/dev/sdz9")

    testbase.assert_equal(status.is_encrypted, False)
    testbase.assert_equal(status.is_unlocked, False)
    testbase.assert_equal(status.cipher, None)
    return


# ----------------------------------------------------------------------
# list_encrypted_volumes
# ----------------------------------------------------------------------


def test_list_encrypted_volumes_filters_crypto_luks():
    """
        list_encrypted_volumes returns one row per device whose
        ``FSTYPE=crypto_LUKS``.  Plain devices are filtered out.
    """
    lsblk_payload = json.dumps({
        "blockdevices": [
            {
                "name": "sda",
                "type": "disk",
                "fstype": None,
                "mountpoint": None,
                "children": [
                    {
                        "name": "sda1",
                        "type": "part",
                        "fstype": "ext4",
                        "mountpoint": "/boot",
                    },
                    {
                        "name": "sda3",
                        "type": "part",
                        "fstype": "crypto_LUKS",
                        "mountpoint": None,
                        "children": [
                            {
                                "name": "cryptroot",
                                "type": "crypt",
                                "fstype": "ext4",
                                "mountpoint": "/",
                            },
                        ],
                    },
                ],
            },
        ],
    })

    runner = _MockCommandRunner()
    runner.results[
        ("lsblk", "-J", "-o", "NAME,FSTYPE,TYPE,MOUNTPOINT")
    ] = RunResult(exit_code=0, stdout=lsblk_payload, stderr="")
    _seed_luksdump(runner=runner, device="/dev/sda3", output=_LUKS_DUMP_OUTPUT)

    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(),
    )
    volumes = manager.list_encrypted_volumes()

    testbase.assert_equal(len(volumes), 1)
    testbase.assert_equal(volumes[0].device, "/dev/sda3")
    testbase.assert_equal(volumes[0].volume, "cryptroot")
    testbase.assert_equal(volumes[0].mount_point, "/")
    testbase.assert_equal(volumes[0].is_unlocked, True)
    return


# ----------------------------------------------------------------------
# rotate_key
# ----------------------------------------------------------------------


def test_rotate_key_invokes_luksChangeKey_with_combined_stdin():
    """
        rotate_key dispatches ``cryptsetup luksChangeKey`` with both
        passphrases on a single stdin stream separated by a newline.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(),
    )
    result = manager.rotate_key(
        volume=_DEVICE,
        old_passphrase="hunter2",
        new_passphrase="correct horse",
    )

    testbase.assert_equal(result.status, UbuntuEncryptionActionStatus.SUCCESS)
    rotate_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["cryptsetup", "luksChangeKey"]
    ]
    testbase.assert_equal(len(rotate_calls), 1)
    testbase.assert_equal(rotate_calls[0]["stdin"], "hunter2\ncorrect horse")
    testbase.assert_equal(
        rotate_calls[0]["args"], ["cryptsetup", "luksChangeKey", _DEVICE],
    )
    return


# ----------------------------------------------------------------------
# backup_recovery_key
# ----------------------------------------------------------------------


def test_backup_recovery_key_writes_to_path():
    """
        backup_recovery_key dispatches ``cryptsetup luksHeaderBackup``
        with the destination path supplied via ``--header-backup-file``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(),
    )
    result = manager.backup_recovery_key(
        volume=_DEVICE, path="/var/backups/cryptroot.hdr",
    )

    testbase.assert_equal(result.status, UbuntuEncryptionActionStatus.SUCCESS)
    backup_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["cryptsetup", "luksHeaderBackup"]
    ]
    testbase.assert_equal(len(backup_calls), 1)
    testbase.assert_equal(
        backup_calls[0]["args"],
        [
            "cryptsetup", "luksHeaderBackup",
            _DEVICE,
            "--header-backup-file", "/var/backups/cryptroot.hdr",
        ],
    )
    return


# ----------------------------------------------------------------------
# unlock_volume
# ----------------------------------------------------------------------


def test_unlock_volume_passes_passphrase_via_stdin():
    """
        unlock_volume dispatches ``cryptsetup luksOpen <device> <name>``
        with the passphrase delivered via stdin.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_status_inactive(runner=runner, name=_NAME)

    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(),
    )
    result = manager.unlock_volume(volume=_DEVICE, passphrase="hunter2")

    testbase.assert_equal(result.status, UbuntuEncryptionActionStatus.SUCCESS)
    open_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["cryptsetup", "luksOpen"]
    ]
    testbase.assert_equal(len(open_calls), 1)
    testbase.assert_equal(open_calls[0]["stdin"], "hunter2")
    testbase.assert_equal(
        open_calls[0]["args"], ["cryptsetup", "luksOpen", _DEVICE, _NAME],
    )
    return


def test_unlock_volume_skipped_when_already_unlocked():
    """
        unlock_volume returns SKIPPED when ``cryptsetup status``
        reports the mapping is already active.
    """
    runner = _MockCommandRunner()
    _seed_status_active(
        runner=runner, name=_NAME, output=_STATUS_ACTIVE_OUTPUT,
    )

    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(),
    )
    result = manager.unlock_volume(volume=_DEVICE, passphrase="hunter2")

    testbase.assert_equal(result.status, UbuntuEncryptionActionStatus.SKIPPED)
    open_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["cryptsetup", "luksOpen"]
    ]
    testbase.assert_equal(open_calls, [])
    return


# ----------------------------------------------------------------------
# Passphrase / argv hygiene
# ----------------------------------------------------------------------


def test_passphrase_never_appears_in_recorded_argv():
    """
        Plaintext passphrase bytes never appear in any ``args`` field
        recorded by the runner; the bytes flow on stdin only.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_isluks(runner=runner, device=_DEVICE, is_luks=False)

    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(),
    )
    spec = UbuntuEncryptionSpec(passphrase="super-secret-string")
    result = manager.enable_encryption(volume=_DEVICE, spec=spec)
    manager.unlock_volume(volume=_DEVICE, passphrase="super-secret-string")
    manager.rotate_key(
        volume=_DEVICE,
        old_passphrase="super-secret-string",
        new_passphrase="new-secret",
    )

    for call in runner.calls:
        for token in call["args"]:
            testbase.assert_true(token != "super-secret-string")
            testbase.assert_true(token != "new-secret")
    for token in result.command:
        testbase.assert_true(token != "super-secret-string")
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_elevate_per_config():
    """
        Every write verb passes ``elevated=True`` to the runner when
        the config requests elevation.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_isluks(runner=runner, device=_DEVICE, is_luks=False)

    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(elevated=True),
    )
    spec = UbuntuEncryptionSpec(passphrase="hunter2")
    manager.enable_encryption(volume=_DEVICE, spec=spec)

    write_calls = [
        c for c in runner.calls
        if c["args"][:2] in (
            ["cryptsetup", "luksFormat"],
            ["cryptsetup", "luksOpen"],
        )
    ]
    testbase.assert_equal(len(write_calls), 2)
    for call in write_calls:
        testbase.assert_equal(call["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs (and idempotency probes) never pass
        ``elevated=True`` regardless of config.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=1, stdout="", stderr=""),
    )
    _seed_luksdump(runner=runner, device=_DEVICE, output=_LUKS_DUMP_OUTPUT)
    _seed_status_inactive(runner=runner, name=_NAME)

    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(elevated=True),
    )
    manager.get_encryption_status(volume=_DEVICE)

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Error surface
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        FAILED results carry the runner's exit_code and stderr.
    """
    runner = _MockCommandRunner()
    _seed_isluks(runner=runner, device=_DEVICE, is_luks=False)
    runner.default_result = RunResult(
        exit_code=2, stdout="", stderr="Cannot acquire device lock",
    )

    manager = UbuntuEncryptionManager(
        runner=runner,
        config=UbuntuEncryptionManagerConfig(),
    )
    spec = UbuntuEncryptionSpec(passphrase="hunter2")
    result = manager.enable_encryption(volume=_DEVICE, spec=spec)

    testbase.assert_equal(result.status, UbuntuEncryptionActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.stderr, "Cannot acquire device lock")
    return


# ----------------------------------------------------------------------
# Module-level parser helpers
# ----------------------------------------------------------------------


def test_parse_luks_dump_handles_luks2_output():
    """
        _parse_luks_dump extracts cipher, key size, and slots from a
        LUKS2 ``luksDump`` payload.
    """
    parsed = _parse_luks_dump(output=_LUKS_DUMP_OUTPUT)
    testbase.assert_equal(parsed["cipher"], "aes-xts-plain64")
    testbase.assert_equal(parsed["key_size"], 512)
    testbase.assert_equal(parsed["slots"], ["slot-0", "slot-1"])
    return


def test_parse_cryptsetup_status_detects_active():
    """
        _parse_cryptsetup_status sets ``active=True`` when the first
        line says so and surfaces the cipher / device fields.
    """
    parsed = _parse_cryptsetup_status(output=_STATUS_ACTIVE_OUTPUT)
    testbase.assert_equal(parsed["active"], True)
    testbase.assert_equal(parsed["cipher"], "aes-xts-plain64")
    testbase.assert_equal(parsed["device"], "/dev/sda3")
    return


def test_parse_lsblk_filters_crypto_luks_only():
    """
        _parse_lsblk_crypto_rows returns only ``crypto_LUKS`` rows.
    """
    payload = json.dumps({
        "blockdevices": [
            {
                "name": "sdb",
                "type": "disk",
                "fstype": "ext4",
                "children": [],
            },
            {
                "name": "sda",
                "type": "disk",
                "fstype": None,
                "children": [
                    {
                        "name": "sda3",
                        "type": "part",
                        "fstype": "crypto_LUKS",
                        "children": [
                            {
                                "name": "data",
                                "type": "crypt",
                                "fstype": "ext4",
                                "mountpoint": "/data",
                            },
                        ],
                    },
                ],
            },
        ],
    })
    rows = _parse_lsblk_crypto_rows(output=payload)
    testbase.assert_equal(len(rows), 1)
    testbase.assert_equal(rows[0]["name"], "sda3")
    testbase.assert_equal(rows[0]["active_mapping"], "data")
    testbase.assert_equal(rows[0]["mount_point"], "/data")
    return
