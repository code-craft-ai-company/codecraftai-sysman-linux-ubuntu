"""
    Ubuntu encryption-management feature.

    Implements the cross-OS encryption-management contract for Ubuntu
    by wrapping the LUKS user-space toolchain (``cryptsetup`` plus
    ``lsblk`` for volume enumeration) through the injected
    :class:`ICommandRunner`.

    Concept mapping:
        The cross-OS contract talks about *encrypted volumes* and
        *recovery keys*.  On Ubuntu those map to LUKS concepts:

        * **Encrypted volume** — a block device whose on-disk format is
          ``crypto_LUKS`` (LUKS1 or LUKS2 header).  The header carries
          up to 8 (LUKS1) or 32 (LUKS2) key slots.
        * **Active mapping** — a ``/dev/mapper/<name>`` device created
          by ``cryptsetup luksOpen <device> <name>``; the underlying
          plaintext block device that callers mount.
        * **Recovery key** — for LUKS the "recovery key" is the LUKS
          binary header itself.  ``backup_recovery_key`` calls
          ``cryptsetup luksHeaderBackup`` so the header (containing the
          encrypted master key plus every slot) can be archived to a
          file the operator preserves.
        * **Passphrase** — bytes that unlock a slot.  Passphrases are
          NEVER placed on argv; they always flow on runner ``stdin``.

        See ``specs/3-encryption-management/design.md`` for the
        cross-OS encryption-tool concept-mapping table.

    Idempotency:
        Every write verb consults a read probe before invoking the
        runner:

        * ``enable_encryption`` — skipped when ``cryptsetup isLuks``
          already reports the device as a LUKS device (a fresh
          ``luksFormat`` would erase the existing master key and all
          data).
        * ``disable_encryption`` — skipped when the device is not
          currently a LUKS device.  The disable flow is destructive
          (``cryptsetup erase`` wipes every key slot, leaving the
          ciphertext irrecoverable); it requires
          ``config.allow_destructive=True`` and otherwise returns
          FAILED with a descriptive detail.
        * ``unlock_volume`` — skipped when ``/dev/mapper/<name>``
          already exists (the mapping is already active).
        * ``rotate_key`` and ``backup_recovery_key`` — unconditional;
          rotation always changes the recorded passphrase, and a header
          backup at the time the operator asks for one is the value
          proposition of the verb.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``get_encryption_status``, ``list_encrypted_volumes``, the
        idempotency probes) never elevate.  ``cryptsetup luksDump`` and
        ``cryptsetup status`` succeed without elevation on most Ubuntu
        installations; the rare case where elevation is required falls
        out of band and surfaces as a FAILED read.

    Passphrase / recovery-key handling:
        Plaintext passphrases NEVER appear on argv.  Every passphrase
        flows through the runner's ``stdin`` parameter so the recorded
        ``command`` field on every result preserves audit fidelity
        without leaking secrets.  The single argv-style exception in
        the LUKS toolchain is ``cryptsetup luksChangeKey``, which
        accepts BOTH the existing and the new passphrase on the SAME
        ``stdin`` stream — the manager concatenates them with a
        newline and sends one combined buffer.

    Destructive operations:
        ``disable_encryption`` is destructive.  ``cryptsetup erase``
        wipes the LUKS header, including every key slot; once issued,
        no passphrase can recover the underlying ciphertext.  The
        manager refuses to dispatch the command unless
        ``config.allow_destructive=True``; otherwise it returns
        ``FAILED`` carrying
        ``detail="destructive_operation_requires_explicit_confirm"``.
"""

import json
import os

from dataclasses import dataclass, field
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level command tokens
# ----------------------------------------------------------------------


_CRYPTSETUP = "cryptsetup"
_LSBLK = "lsblk"


# Sentinel placed into the recorded ``detail`` field when the
# destructive ``disable_encryption`` verb is invoked without
# ``config.allow_destructive=True``.  Exposed as a module constant so
# tests and downstream coordinators can match against it without
# duplicating the literal.
_DESTRUCTIVE_GUARD_DETAIL = "destructive_operation_requires_explicit_confirm"


# Default cipher used by ``enable_encryption`` when the spec does not
# override.  Matches the upstream ``cryptsetup`` LUKS2 default.
_DEFAULT_CIPHER = "aes-xts-plain64:sha256"


# Default key size in BITS used by ``enable_encryption`` when the spec
# does not override.  512 bits is the LUKS2 default for the
# ``aes-xts-plain64`` cipher (XTS halves the effective AES key, so 512
# bits of key material yields 256-bit AES).
_DEFAULT_KEY_SIZE = 512


# ----------------------------------------------------------------------
# Aliases / config / result types
# ----------------------------------------------------------------------


UbuntuEncryptionActionStatus = ActionStatus


@dataclass
class UbuntuEncryptionManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuEncryptionManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param allow_destructive: When True, ``disable_encryption`` is
                                  permitted to dispatch
                                  ``cryptsetup erase`` (which is
                                  irreversible — the master key is
                                  destroyed).  Defaults to False so the
                                  destructive guard fires.
        :param default_cipher: Default ``--cipher`` token passed to
                               ``cryptsetup luksFormat`` when the spec
                               does not override.  Defaults to
                               :data:`_DEFAULT_CIPHER`.
        :param default_key_size: Default ``--key-size`` value (in BITS)
                                 passed to ``cryptsetup luksFormat`` when
                                 the spec does not override.  Defaults
                                 to :data:`_DEFAULT_KEY_SIZE`.
    """
    allow_destructive: bool = False
    default_cipher: str = _DEFAULT_CIPHER
    default_key_size: int = _DEFAULT_KEY_SIZE


@dataclass
class UbuntuEncryptionActionResult(FeatureActionResult):
    """
        Result of an encryption-management write operation.

        :param volume: Volume name (the mapping name passed to
                       ``cryptsetup luksOpen <device> <name>``), when
                       applicable.  ``None`` for verbs that operate
                       only on the underlying device.
        :param device: Underlying block-device path (``/dev/sdaN``,
                       ``/dev/nvme0n1p3``, ...) targeted by the
                       operation.
        :param requires_reboot: True when the operation modified the
                                running root filesystem's encryption
                                posture in a way that requires a
                                reboot to take effect.  Always False
                                for the LUKS verbs in this manager
                                (``cryptsetup`` operations apply
                                immediately at the device-mapper
                                layer); preserved on the dataclass so
                                the field shape mirrors the
                                ``WindowsEncryptionActionResult``
                                sibling for cross-OS coordinators.
    """
    volume: Optional[str] = None
    device: Optional[str] = None
    requires_reboot: bool = False


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class UbuntuEncryptionSpec:
    """
        Specification for :meth:`UbuntuEncryptionManager.enable_encryption`.

        :param cipher: ``--cipher`` value.  ``None`` falls back to
                       :attr:`UbuntuEncryptionManagerConfig.default_cipher`.
        :param key_size: ``--key-size`` value (in BITS).  ``None`` falls
                         back to
                         :attr:`UbuntuEncryptionManagerConfig.default_key_size`.
        :param passphrase: Plaintext passphrase used to bootstrap the
                           first LUKS key slot.  REQUIRED.  Bytes flow
                           on runner ``stdin`` and never appear on
                           argv.
        :param recovery_key_protector: Reserved.  When True (default)
                                       the manager calls
                                       :meth:`UbuntuEncryptionManager.backup_recovery_key`
                                       in a follow-up; today the verb
                                       is a no-op, exposed so the
                                       cross-OS spec shape is honoured.
        :param mount_point: Optional intended mount point for the
                            resulting plaintext mapping.  This feature
                            does NOT mount; the field is preserved so
                            coordinators can round-trip a default into
                            ``filesystem-management``.
    """
    cipher: Optional[str] = None
    key_size: Optional[int] = None
    passphrase: Optional[str] = None
    recovery_key_protector: bool = True
    mount_point: Optional[str] = None


@dataclass
class UbuntuEncryptionStatus:
    """
        Encryption status for a single device or active mapping.

        :param volume: Mapping name when an active mapping exists
                       (``/dev/mapper/<name>``), else the device path.
        :param is_encrypted: True when the underlying device carries a
                             LUKS header.
        :param is_unlocked: True when an active ``/dev/mapper/<name>``
                            mapping is reported by ``cryptsetup status``.
        :param cipher: Cipher reported by the LUKS header / mapping
                       (``aes-xts-plain64``).  ``None`` when not
                       encrypted.
        :param key_size: Key size in BITS from the LUKS header.
                         ``None`` when not encrypted.
        :param progress_percent: Reserved for ongoing reformat /
                                 reencrypt operations.  ``None`` when
                                 no operation is in flight.
        :param protector_types: Slot-state strings.  For LUKS this is
                                a list of strings of the form
                                ``"slot-<index>"`` for every enabled
                                slot.  The Windows sibling uses this
                                list to enumerate ``Password`` /
                                ``RecoveryPassword`` / ``Tpm`` /
                                ``ExternalKey`` protectors.
    """
    volume: str
    is_encrypted: bool
    is_unlocked: bool
    cipher: Optional[str] = None
    key_size: Optional[int] = None
    progress_percent: Optional[float] = None
    protector_types: list = field(default_factory=list)


@dataclass
class UbuntuEncryptedVolumeInfo:
    """
        Summary of a single encrypted volume on the host.

        :param volume: Mapping name when an active mapping exists,
                       else the device path.
        :param device: Underlying block-device path
                       (``/dev/sdaN`` / ``/dev/nvme0n1p3``).
        :param mount_point: Mount point of the unlocked mapping when
                            mounted, else ``None``.
        :param is_encrypted: Always True for entries in
                             :meth:`UbuntuEncryptionManager.list_encrypted_volumes`.
        :param is_unlocked: True when the mapping is currently active.
        :param cipher: Cipher reported by the LUKS header.
        :param key_size: Key size in bits from the LUKS header.
        :param progress_percent: Reserved.
        :param protector_types: Slot-state strings.
    """
    volume: str
    device: str
    mount_point: Optional[str] = None
    is_encrypted: bool = True
    is_unlocked: bool = False
    cipher: Optional[str] = None
    key_size: Optional[int] = None
    progress_percent: Optional[float] = None
    protector_types: list = field(default_factory=list)


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class UbuntuEncryptionQueryError(Exception):
    """
        Raised when an encryption read fails for a reason other than
        the target simply being absent from the host's encryption
        surface.
    """

    def __init__(
        self,
        *,
        command: list,
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command: list = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Encryption query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# UbuntuEncryptionManager
# ----------------------------------------------------------------------


class UbuntuEncryptionManager(BaseFeatureManager):
    """
        Ubuntu encryption manager wrapping the LUKS toolchain
        (``cryptsetup`` plus ``lsblk`` for enumeration) through an
        injected :class:`ICommandRunner`.  See the module docstring for
        the engine-mapping, idempotency, elevation, and passphrase
        handling contracts.
    """

    result_class = UbuntuEncryptionActionResult
    status_class = UbuntuEncryptionActionStatus
    feature_name = "encryption-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_encryption_status(
        self,
        volume: str,
    ) -> UbuntuEncryptionStatus:
        """
            Probe the encryption status of ``volume``.

            ``volume`` is interpreted as a device path when it begins
            with ``/`` and as a mapping name otherwise.  The probe
            consults BOTH ``cryptsetup luksDump`` (for header data) and
            ``cryptsetup status`` (for the active-mapping check) so the
            returned record reflects the full picture.

            :param volume: Device path (``/dev/sdaN``) or mapping name
                           (``cryptroot``).

            :returns: The :class:`UbuntuEncryptionStatus`.
        """
        device_path = self._resolve_device_path(volume=volume)
        mapping_name = self._resolve_mapping_name(volume=volume)

        header = self._luks_dump(device=device_path)
        active = self._cryptsetup_status(name=mapping_name)

        if header is None:
            is_encrypted = False
            cipher_value: Optional[str] = None
            key_size_value: Optional[int] = None
            protector_types: list = []
        else:
            is_encrypted = True
            cipher_value = header.get("cipher")
            key_size_value = header.get("key_size")
            slots = header.get("slots")
            if isinstance(slots, list):
                protector_types = list(slots)
            else:
                protector_types = []

        if active is None:
            is_unlocked = False
        else:
            is_unlocked = bool(active.get("active", False))

        rtnval = UbuntuEncryptionStatus(
            volume=volume,
            is_encrypted=is_encrypted,
            is_unlocked=is_unlocked,
            cipher=cipher_value,
            key_size=key_size_value,
            progress_percent=None,
            protector_types=protector_types,
        )
        return rtnval

    def list_encrypted_volumes(self) -> list:
        """
            Enumerate every block device with a LUKS header.

            Calls ``lsblk -J -o NAME,FSTYPE,TYPE,MOUNTPOINT`` and
            filters rows where ``FSTYPE=crypto_LUKS``.  For every
            matching device the manager pulls the LUKS header via
            ``luksDump`` and merges any active ``cryptsetup status``
            output to populate ``mount_point`` and ``is_unlocked``.

            :returns: A list (possibly empty) of
                      :class:`UbuntuEncryptedVolumeInfo`.
        """
        command = [
            _LSBLK,
            "-J",
            "-o", "NAME,FSTYPE,TYPE,MOUNTPOINT",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval

        rtnval = []
        device_rows = _parse_lsblk_crypto_rows(output=run_result.stdout)
        for row in device_rows:
            device_name = row.get("name", "")
            if len(device_name) == 0:
                continue
            device_path = "/dev/{}".format(device_name)
            header = self._luks_dump(device=device_path)
            if header is None:
                cipher_value: Optional[str] = None
                key_size_value: Optional[int] = None
                slot_list: list = []
            else:
                cipher_value = header.get("cipher")
                key_size_value = header.get("key_size")
                slots = header.get("slots")
                if isinstance(slots, list):
                    slot_list = list(slots)
                else:
                    slot_list = []
            mapping_name = row.get("active_mapping", "")
            if isinstance(mapping_name, str) and len(mapping_name) > 0:
                volume_label: str = mapping_name
            else:
                volume_label = device_name
            mount_point_value = row.get("mount_point")
            if (
                isinstance(mount_point_value, str)
                and len(mount_point_value) > 0
            ):
                mp_value: Optional[str] = mount_point_value
            else:
                mp_value = None
            if isinstance(mapping_name, str) and len(mapping_name) > 0:
                is_unlocked = True
            else:
                is_unlocked = False
            info = UbuntuEncryptedVolumeInfo(
                volume=volume_label,
                device=device_path,
                mount_point=mp_value,
                is_encrypted=True,
                is_unlocked=is_unlocked,
                cipher=cipher_value,
                key_size=key_size_value,
                progress_percent=None,
                protector_types=slot_list,
            )
            rtnval.append(info)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def enable_encryption(
        self,
        volume: str,
        spec: UbuntuEncryptionSpec,
    ) -> UbuntuEncryptionActionResult:
        """
            Enable LUKS encryption on ``volume`` per ``spec``.

            Dispatches::

                cryptsetup luksFormat --batch-mode \\
                    --type luks2 \\
                    --cipher <cipher> \\
                    --key-size <bits> \\
                    <device>

            with the passphrase delivered via runner ``stdin``.  After
            the format succeeds the manager opens the volume so the
            mapping is immediately available::

                cryptsetup luksOpen <device> <name>

            Idempotent: when the device is already a LUKS device
            (probed via ``cryptsetup isLuks``) the operation returns
            ``SKIPPED`` without dispatching ``luksFormat``.  This is
            the safety rail that prevents accidental data loss; an
            existing LUKS header MUST NOT be overwritten by an
            idempotent re-run.

            :param volume: Mapping name (``cryptroot``) or device path
                           (``/dev/sdaN``).  When a mapping name is
                           passed, the underlying device must be
                           resolvable via the ``mount_point`` field on
                           ``spec`` or via the same name under
                           ``/dev``.  Today the manager treats the
                           ``volume`` argument as the mapping name and
                           expects ``spec.mount_point`` to carry the
                           device path; tomorrow's enhancement is a
                           full UUID-resolver.
            :param spec: The :class:`UbuntuEncryptionSpec`.

            :returns: The :class:`UbuntuEncryptionActionResult`.
        """
        device_path = self._resolve_device_path(volume=volume)
        mapping_name = self._resolve_mapping_name(volume=volume)
        cipher = spec.cipher or self._typed_config().default_cipher
        key_size = spec.key_size or self._typed_config().default_key_size

        format_command = [
            _CRYPTSETUP, "luksFormat",
            "--batch-mode",
            "--type", "luks2",
            "--cipher", cipher,
            "--key-size", str(key_size),
            device_path,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuEncryptionActionStatus.DRY_RUN,
                command=format_command,
                volume=mapping_name,
                device=device_path,
                detail=device_path,
            )
            return dry

        passphrase = spec.passphrase
        if passphrase is None:
            failed = self._build_result(
                status=UbuntuEncryptionActionStatus.FAILED,
                command=format_command,
                volume=mapping_name,
                device=device_path,
                detail="passphrase_required",
                stderr="passphrase is required for enable_encryption",
            )
            return failed

        already_luks = self._is_luks(device=device_path)
        if already_luks:
            skipped = self._build_result(
                status=UbuntuEncryptionActionStatus.SKIPPED,
                command=format_command,
                volume=mapping_name,
                device=device_path,
                detail="device {} already encrypted".format(device_path),
            )
            return skipped

        format_result = self._dispatch_encryption_write(
            command=format_command,
            stdin=passphrase,
            volume=mapping_name,
            device=device_path,
        )
        if format_result.status != UbuntuEncryptionActionStatus.SUCCESS:
            return format_result

        open_command = [
            _CRYPTSETUP, "luksOpen",
            device_path,
            mapping_name,
        ]
        open_result = self._dispatch_encryption_write(
            command=open_command,
            stdin=passphrase,
            volume=mapping_name,
            device=device_path,
        )
        return open_result

    def disable_encryption(
        self,
        volume: str,
    ) -> UbuntuEncryptionActionResult:
        """
            Disable LUKS encryption on ``volume`` by erasing the LUKS
            header.

            Dispatches ``cryptsetup erase <device>`` after asserting
            ``config.allow_destructive=True``.  ``cryptsetup erase``
            wipes every key slot from the header; after the call
            completes, NO passphrase can recover the underlying
            ciphertext.  A fresh ``luksFormat`` is required to reuse
            the device for any encrypted purpose, and an unencrypted
            reuse requires a follow-up ``mkfs``.

            Idempotent: when the device is not currently a LUKS device
            the operation returns ``SKIPPED``.  Destructive guard:
            when ``config.allow_destructive`` is False the operation
            returns ``FAILED`` carrying
            :data:`_DESTRUCTIVE_GUARD_DETAIL` and does NOT dispatch
            the ``erase`` command.

            :param volume: Mapping name or device path.

            :returns: The :class:`UbuntuEncryptionActionResult`.
        """
        device_path = self._resolve_device_path(volume=volume)
        mapping_name = self._resolve_mapping_name(volume=volume)

        command = [_CRYPTSETUP, "erase", device_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuEncryptionActionStatus.DRY_RUN,
                command=command,
                volume=mapping_name,
                device=device_path,
                detail=device_path,
            )
            return dry

        already_luks = self._is_luks(device=device_path)
        if not already_luks:
            skipped = self._build_result(
                status=UbuntuEncryptionActionStatus.SKIPPED,
                command=command,
                volume=mapping_name,
                device=device_path,
                detail="device {} is not encrypted".format(device_path),
            )
            return skipped

        allow_destructive = self._typed_config().allow_destructive
        if not allow_destructive:
            failed = self._build_result(
                status=UbuntuEncryptionActionStatus.FAILED,
                command=command,
                volume=mapping_name,
                device=device_path,
                detail=_DESTRUCTIVE_GUARD_DETAIL,
                stderr=(
                    "disable_encryption is destructive; set "
                    "config.allow_destructive=True to proceed"
                ),
            )
            return failed

        rtnval = self._dispatch_encryption_write(
            command=command,
            stdin="YES\n",
            volume=mapping_name,
            device=device_path,
        )
        return rtnval

    def rotate_key(
        self,
        volume: str,
        old_passphrase: str,
        new_passphrase: str,
    ) -> UbuntuEncryptionActionResult:
        """
            Rotate the LUKS passphrase on ``volume``.

            Dispatches ``cryptsetup luksChangeKey <device>`` with the
            old passphrase followed by a newline, followed by the new
            passphrase, all on a single ``stdin`` payload.  This is
            the single LUKS verb that requires two passphrases on the
            same stream; the implementation joins them with a single
            ``\\n`` separator the way ``cryptsetup`` expects.

            Unconditional: rotation is always dispatched.  The contract
            does not allow a "skipped" outcome for rotation because the
            new passphrase value is opaque to the manager; the only
            way to know whether rotation was needed is to compare
            secret material, which the manager will not do.

            :param volume: Mapping name or device path.
            :param old_passphrase: The current passphrase.
            :param new_passphrase: The replacement passphrase.

            :returns: The :class:`UbuntuEncryptionActionResult`.
        """
        device_path = self._resolve_device_path(volume=volume)
        mapping_name = self._resolve_mapping_name(volume=volume)

        command = [_CRYPTSETUP, "luksChangeKey", device_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuEncryptionActionStatus.DRY_RUN,
                command=command,
                volume=mapping_name,
                device=device_path,
                detail=device_path,
            )
            return dry

        combined_stdin = "{}\n{}".format(old_passphrase, new_passphrase)
        rtnval = self._dispatch_encryption_write(
            command=command,
            stdin=combined_stdin,
            volume=mapping_name,
            device=device_path,
        )
        return rtnval

    def backup_recovery_key(
        self,
        volume: str,
        path: str,
    ) -> UbuntuEncryptionActionResult:
        """
            Back up the LUKS header to ``path``.

            Dispatches::

                cryptsetup luksHeaderBackup <device> \\
                    --header-backup-file <path>

            The exported file contains the encrypted master key plus
            every key slot; an operator with both the file and a
            current passphrase can restore access to the device after
            an accidental ``erase``.  No stdin payload is required.

            Unconditional: the verb is always dispatched.

            :param volume: Mapping name or device path.
            :param path: Destination filesystem path for the header
                         backup.  This feature does NOT create the
                         parent directory; coordinators are expected
                         to ensure the path is writable.

            :returns: The :class:`UbuntuEncryptionActionResult`.
        """
        device_path = self._resolve_device_path(volume=volume)
        mapping_name = self._resolve_mapping_name(volume=volume)

        command = [
            _CRYPTSETUP, "luksHeaderBackup",
            device_path,
            "--header-backup-file", path,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuEncryptionActionStatus.DRY_RUN,
                command=command,
                volume=mapping_name,
                device=device_path,
                detail=path,
            )
            return dry

        rtnval = self._dispatch_encryption_write(
            command=command,
            stdin=None,
            volume=mapping_name,
            device=device_path,
            detail=path,
        )
        return rtnval

    def unlock_volume(
        self,
        volume: str,
        passphrase: str,
    ) -> UbuntuEncryptionActionResult:
        """
            Unlock ``volume`` by attaching the LUKS device to a
            ``/dev/mapper/<name>`` mapping.

            Dispatches ``cryptsetup luksOpen <device> <name>`` with the
            passphrase delivered via runner ``stdin``.  Idempotent:
            when the mapping already exists (probed via
            ``cryptsetup status <name>``) the operation returns
            ``SKIPPED``.

            :param volume: Mapping name or device path.
            :param passphrase: Plaintext passphrase.  Bytes flow on
                               runner ``stdin``; never appear on argv.

            :returns: The :class:`UbuntuEncryptionActionResult`.
        """
        device_path = self._resolve_device_path(volume=volume)
        mapping_name = self._resolve_mapping_name(volume=volume)

        command = [
            _CRYPTSETUP, "luksOpen",
            device_path,
            mapping_name,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuEncryptionActionStatus.DRY_RUN,
                command=command,
                volume=mapping_name,
                device=device_path,
                detail=device_path,
            )
            return dry

        active = self._cryptsetup_status(name=mapping_name)
        if active is not None and bool(active.get("active", False)):
            skipped = self._build_result(
                status=UbuntuEncryptionActionStatus.SKIPPED,
                command=command,
                volume=mapping_name,
                device=device_path,
                detail="mapping {} is already active".format(mapping_name),
            )
            return skipped

        rtnval = self._dispatch_encryption_write(
            command=command,
            stdin=passphrase,
            volume=mapping_name,
            device=device_path,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — config, identity, dispatch
    # ------------------------------------------------------------------

    def _typed_config(self) -> UbuntuEncryptionManagerConfig:
        """
            Return ``self._config`` typed as
            :class:`UbuntuEncryptionManagerConfig`, falling back to a
            synthesised default when the caller passed a plain
            :class:`FeatureConfig`.
        """
        if isinstance(self._config, UbuntuEncryptionManagerConfig):
            rtnval = self._config
        else:
            rtnval = UbuntuEncryptionManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _resolve_device_path(self, *, volume: str) -> str:
        """
            Return the underlying block-device path for ``volume``.

            When ``volume`` already starts with ``/`` it is taken
            verbatim.  Otherwise ``/dev/<volume>`` is the conventional
            mapping; this matches typical LUKS naming where the
            mapping name and the underlying partition basename are
            distinct (``cryptroot`` vs ``sda3``).  Callers needing a
            non-default mapping should pass the device path directly
            as ``volume``.
        """
        if isinstance(volume, str) and volume.startswith("/"):
            rtnval = volume
            return rtnval
        rtnval = "/dev/{}".format(volume)
        return rtnval

    def _resolve_mapping_name(self, *, volume: str) -> str:
        """
            Return the device-mapper mapping name for ``volume``.

            When ``volume`` is a device path the basename is used as
            the mapping name (``/dev/sda3`` → ``sda3``).  Otherwise
            the value is returned verbatim.
        """
        if isinstance(volume, str) and volume.startswith("/"):
            rtnval = volume.rsplit("/", 1)[-1]
            return rtnval
        rtnval = volume
        return rtnval

    def _is_luks(self, *, device: str) -> bool:
        """
            Probe whether ``device`` carries a LUKS header.

            Calls ``cryptsetup isLuks <device>``; ``cryptsetup``
            returns 0 when the device is LUKS and non-zero otherwise.
        """
        command = [_CRYPTSETUP, "isLuks", device]
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _luks_dump(self, *, device: str) -> Optional[dict]:
        """
            Run ``cryptsetup luksDump <device>`` and parse the header.

            :returns: A dict with ``cipher``, ``key_size``, and
                      ``slots`` keys when the device is LUKS, else
                      ``None``.
        """
        command = [_CRYPTSETUP, "luksDump", device]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval
        rtnval = _parse_luks_dump(output=run_result.stdout)
        return rtnval

    def _cryptsetup_status(self, *, name: str) -> Optional[dict]:
        """
            Run ``cryptsetup status <name>`` and parse the response.

            :returns: A dict with ``active``, ``cipher``, and
                      ``device`` keys when the mapping is active,
                      else ``None``.
        """
        command = [_CRYPTSETUP, "status", name]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval
        rtnval = _parse_cryptsetup_status(output=run_result.stdout)
        return rtnval

    def _dispatch_encryption_write(
        self,
        *,
        command: list,
        stdin: Optional[str],
        volume: Optional[str] = None,
        device: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> UbuntuEncryptionActionResult:
        """
            Dispatch an encryption-management write through the runner
            and wrap the result with the ``volume`` / ``device`` /
            ``requires_reboot`` fields.
        """
        if detail is None:
            detail_value: Optional[str] = device
        else:
            detail_value = detail
        rtnval = self._dispatch(
            command=command,
            detail=detail_value,
            stdin=stdin,
            operation="encryption_write",
            volume=volume,
            device=device,
            requires_reboot=False,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuEncryptionActionStatus,
        command: list,
        volume: Optional[str] = None,
        device: Optional[str] = None,
        detail: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuEncryptionActionResult:
        """
            Construct an :class:`UbuntuEncryptionActionResult` without
            invoking the runner (used by SKIPPED / DRY_RUN / safety-rail
            paths).
        """
        rtnval = UbuntuEncryptionActionResult(
            status=status,
            detail=detail,
            command=list(command),
            volume=volume,
            device=device,
            stderr=stderr,
            requires_reboot=False,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers
# ----------------------------------------------------------------------


def _parse_luks_dump(*, output: str) -> dict:
    """
        Parse ``cryptsetup luksDump`` plain-text output into a dict.

        ``luksDump`` emits a header section containing ``Cipher name:``,
        ``Cipher mode:``, and ``MK bits:`` (LUKS1) or ``Cipher:`` and
        ``MK bits:`` keys (LUKS2), followed by per-slot blocks of the
        form ``Key Slot 0: ENABLED`` (LUKS1) or ``Keyslots: \\n  0:
        luks2`` (LUKS2).  This parser tolerates both shapes.

        :param output: Raw stdout text.

        :returns: A dict with ``cipher`` (str), ``key_size`` (int),
                  and ``slots`` (list[str]) keys.
    """
    cipher_name: Optional[str] = None
    cipher_mode: Optional[str] = None
    cipher_full: Optional[str] = None
    key_size: Optional[int] = None
    slots: list = []

    in_keyslots_section = False
    for raw_line in output.splitlines():
        line_text = raw_line.rstrip()
        stripped = line_text.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("Cipher name:"):
            cipher_name = stripped.split(":", 1)[1].strip()
            continue
        if stripped.startswith("Cipher mode:"):
            cipher_mode = stripped.split(":", 1)[1].strip()
            continue
        if stripped.startswith("Cipher:"):
            cipher_full = stripped.split(":", 1)[1].strip()
            continue
        if stripped.startswith("MK bits:"):
            try:
                key_size = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                key_size = None
            continue
        if stripped.startswith("Key Slot ") and "ENABLED" in stripped:
            slot_token = stripped.split(":", 1)[0].strip()
            slots.append(slot_token.lower().replace("key slot ", "slot-"))
            continue
        if stripped.startswith("Keyslots:"):
            in_keyslots_section = True
            continue
        if in_keyslots_section:
            if stripped.startswith("Tokens:") or stripped.startswith(
                "Digests:",
            ):
                in_keyslots_section = False
                continue
            if ":" in stripped:
                head = stripped.split(":", 1)[0].strip()
                if head.isdigit():
                    slots.append("slot-{}".format(head))

    if cipher_full is not None:
        cipher_value: Optional[str] = cipher_full
    elif cipher_name is not None and cipher_mode is not None:
        cipher_value = "{}-{}".format(cipher_name, cipher_mode)
    elif cipher_name is not None:
        cipher_value = cipher_name
    else:
        cipher_value = None

    rtnval = {
        "cipher": cipher_value,
        "key_size": key_size,
        "slots": slots,
    }
    return rtnval


def _parse_cryptsetup_status(*, output: str) -> dict:
    """
        Parse ``cryptsetup status <name>`` plain-text output.

        The first line is one of:
            ``/dev/mapper/<name> is active.``
            ``/dev/mapper/<name> is active and is in use.``
            ``/dev/mapper/<name> is inactive.``

        Followed by indented key:value pairs (``cipher:``, ``device:``,
        ``offset:``, etc).

        :param output: Raw stdout text.

        :returns: A dict with ``active`` (bool), ``cipher`` (str | None),
                  ``device`` (str | None) keys.
    """
    active = False
    cipher_value: Optional[str] = None
    device_value: Optional[str] = None

    for raw_line in output.splitlines():
        line_text = raw_line.strip()
        if len(line_text) == 0:
            continue
        if " is active" in line_text:
            active = True
            continue
        if line_text.startswith("cipher:"):
            cipher_value = line_text.split(":", 1)[1].strip()
            continue
        if line_text.startswith("device:"):
            device_value = line_text.split(":", 1)[1].strip()
            continue

    rtnval = {
        "active": active,
        "cipher": cipher_value,
        "device": device_value,
    }
    return rtnval


def _parse_lsblk_crypto_rows(*, output: str) -> list:
    """
        Parse ``lsblk -J -o NAME,FSTYPE,TYPE,MOUNTPOINT`` output and
        return one row per device whose ``FSTYPE=crypto_LUKS``.

        ``lsblk`` reports a tree under the top-level ``blockdevices``
        key.  Encrypted devices appear at any depth (typically as a
        partition under a disk).  When a LUKS device is unlocked,
        ``lsblk`` reports the active mapping as a child whose ``TYPE``
        is ``crypt``; the parser captures that child's name as the
        ``active_mapping`` field on the parent row.

        :param output: Raw stdout text from ``lsblk -J``.

        :returns: A list (possibly empty) of dicts with ``name``
                  (str), ``active_mapping`` (str), and ``mount_point``
                  (str | None) keys.
    """
    rtnval: list = []
    stripped = output.strip()
    if len(stripped) == 0:
        return rtnval
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        return rtnval
    if not isinstance(data, dict):
        return rtnval
    block_devices = data.get("blockdevices")
    if not isinstance(block_devices, list):
        return rtnval

    flat = _flatten_lsblk(devices=block_devices)
    for entry in flat:
        fstype_value = entry.get("fstype")
        if fstype_value != "crypto_LUKS":
            continue
        device_name = entry.get("name", "")
        if not isinstance(device_name, str):
            continue
        children = entry.get("children")
        active_mapping = ""
        mount_point: Optional[str] = None
        if isinstance(children, list):
            for child_entry in children:
                if not isinstance(child_entry, dict):
                    continue
                if child_entry.get("type") != "crypt":
                    continue
                child_name = child_entry.get("name", "")
                if isinstance(child_name, str) and len(child_name) > 0:
                    active_mapping = child_name
                child_mount = child_entry.get("mountpoint")
                if isinstance(child_mount, str) and len(child_mount) > 0:
                    mount_point = child_mount
                break
        row = {
            "name": device_name,
            "active_mapping": active_mapping,
            "mount_point": mount_point,
        }
        rtnval.append(row)
    return rtnval


def _flatten_lsblk(*, devices: list) -> list:
    """
        Flatten the ``lsblk -J`` tree into a single list, preserving
        each node's ``children`` field so callers can still inspect
        immediate children for the active-mapping detection.

        :param devices: A list of ``lsblk`` device records.

        :returns: A flat list (depth-first) of the same records.
    """
    rtnval: list = []
    for entry in devices:
        if not isinstance(entry, dict):
            continue
        rtnval.append(entry)
        children = entry.get("children")
        if isinstance(children, list):
            nested = _flatten_lsblk(devices=children)
            rtnval.extend(nested)
    return rtnval
