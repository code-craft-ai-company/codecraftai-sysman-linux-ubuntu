"""
    Ubuntu bootloader-management feature.

    Implements the cross-OS bootloader-management contract for Ubuntu.
    Ubuntu's primary OS-level bootloader is GRUB 2 (driven through
    ``update-grub``, ``grub-mkconfig``, ``grub-set-default``, and edits
    to ``/etc/default/grub`` plus drop-ins under ``/etc/grub.d/``).
    UEFI hosts that opted into ``systemd-boot`` instead are supported
    via ``bootctl`` as a secondary backend selected at config time.

    **Important distinction.**  This feature is NOT
    ``boot-management``.  ``boot-management`` (in
    ``automojo-sysman-uefi``) addresses **firmware-level UEFI boot
    entries** through ``efibootmgr`` / ``bcdedit /enum firmware``.
    This feature addresses **the OS-level bootloader configuration
    that sits between the firmware boot entry and the kernel** -- the
    GRUB menu, the systemd-boot loader entries, and the BCD
    osloader / bootmgr store.  The two layers are independent and
    must remain so.

    Verb plumbing (per ``UbuntuBootloaderBackend``):

        * ``list_entries``
            * GRUB2: ``cat /boot/grub/grub.cfg`` + parser
            * SYSTEMD_BOOT: ``bootctl list``
        * ``get_default_entry``
            * GRUB2: ``cat /etc/default/grub`` + grub.cfg cross-ref
            * SYSTEMD_BOOT: ``bootctl status``
        * ``set_default_entry``
            * GRUB2: rewrite ``GRUB_DEFAULT=<value>`` then
              ``update-grub``
            * SYSTEMD_BOOT: ``bootctl set-default <id>``
        * ``add_entry``
            * GRUB2: append a ``menuentry`` block to
              ``/etc/grub.d/40_custom`` then ``update-grub``
            * SYSTEMD_BOOT: write ``<id>.conf`` to
              ``/boot/loader/entries/``
        * ``remove_entry``
            * GRUB2: edit ``/etc/grub.d/40_custom`` removing the
              named block then ``update-grub``
            * SYSTEMD_BOOT: ``rm /boot/loader/entries/<id>.conf``
        * ``update_bootloader_config``
            * GRUB2: ``update-grub`` (alias for
              ``grub-mkconfig -o /boot/grub/grub.cfg``)
            * SYSTEMD_BOOT: ``bootctl update``
        * ``regenerate_config``
            * GRUB2: ``grub-mkconfig -o /boot/grub/grub.cfg`` (no
              install)
            * SYSTEMD_BOOT: ``bootctl install``

    File I/O policy:
        Every read / write is performed through the injected
        :class:`ICommandRunner` (``cat`` / ``tee`` / ``rm``).  This
        module does NOT consume a separate file operator so that
        dry-run, elevation, and aspect composition compose uniformly.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``list_entries``, ``get_default_entry``) and idempotency
        probes never elevate.

    Destructive nature:
        Bootloader misconfiguration can render a host unbootable.
        Callers SHOULD pair every write with a backup of
        ``/boot/grub/grub.cfg`` (GRUB2) or
        ``/boot/loader/entries/`` (systemd-boot) and SHOULD verify the
        resulting menu through ``list_entries`` before rebooting.
"""

import os

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_CAT = "cat"
_TEE = "tee"
_RM = "rm"
_UPDATE_GRUB = "update-grub"
_GRUB_MKCONFIG = "grub-mkconfig"
_GRUB_SET_DEFAULT = "grub-set-default"
_BOOTCTL = "bootctl"

_DEFAULT_GRUB_DEFAULTS = "/etc/default/grub"
_DEFAULT_GRUB_CUSTOM = "/etc/grub.d/40_custom"
_DEFAULT_GRUB_CFG = "/boot/grub/grub.cfg"
_DEFAULT_LOADER_ENTRIES_DIR = "/boot/loader/entries"

_GRUB_DEFAULT_KEY = "GRUB_DEFAULT"

_CUSTOM_BLOCK_BEGIN = "# automojo-bootloader-management:begin {entry_id}"
_CUSTOM_BLOCK_END = "# automojo-bootloader-management:end {entry_id}"


UbuntuBootloaderActionStatus = ActionStatus


class UbuntuBootloaderBackend(Enum):
    """
        Ubuntu bootloader backend selector.

        ``GRUB2`` is the historical Debian-family default and is
        present on virtually every Ubuntu host (BIOS or UEFI).
        ``SYSTEMD_BOOT`` is opt-in on UEFI hosts that explicitly chose
        the ``systemd-boot`` loader (notably Ubuntu Core / desktop
        installs that selected the loader during install).
    """
    GRUB2         = "grub2"
    SYSTEMD_BOOT  = "systemd_boot"


@dataclass
class UbuntuBootloaderManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuBootloaderManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param backend: The :class:`UbuntuBootloaderBackend` to drive.
                        Defaults to ``GRUB2``.
        :param grub_defaults: Path to the GRUB defaults file.  Default
                              ``/etc/default/grub``.
        :param grub_custom: Path to the GRUB drop-in file used for
                            ``add_entry`` / ``remove_entry``.  Default
                            ``/etc/grub.d/40_custom``.
        :param grub_cfg: Path to the rendered GRUB configuration.
                         Default ``/boot/grub/grub.cfg``.
        :param loader_entries_dir: Directory holding systemd-boot
                                   loader entries.  Default
                                   ``/boot/loader/entries``.
    """
    backend: UbuntuBootloaderBackend = UbuntuBootloaderBackend.GRUB2
    grub_defaults: str = _DEFAULT_GRUB_DEFAULTS
    grub_custom: str = _DEFAULT_GRUB_CUSTOM
    grub_cfg: str = _DEFAULT_GRUB_CFG
    loader_entries_dir: str = _DEFAULT_LOADER_ENTRIES_DIR


@dataclass
class UbuntuBootloaderActionResult(FeatureActionResult):
    """
        Result of a bootloader-management write operation.

        :param entry_id: The bootloader entry identifier acted upon,
                         when applicable (the GRUB entry index / name
                         or the systemd-boot loader-conf basename).
        :param requires_reboot: ``True`` when the change takes effect
                                only after the next boot.  GRUB2
                                writes set this to ``True`` because the
                                running kernel does not consult
                                ``grub.cfg``; systemd-boot writes also
                                require a reboot.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    entry_id: Optional[str] = None
    requires_reboot: bool = False
    message: Optional[str] = None


@dataclass
class UbuntuBootEntryInfo:
    """
        Snapshot of a single bootloader entry.

        :param id: Native identifier.  For GRUB2 this is the
                   ``menuentry`` title (used by ``GRUB_DEFAULT=``).
                   For systemd-boot this is the loader-conf basename
                   (without the ``.conf`` suffix).
        :param name: Human-readable display title.  May be the same
                     as ``id`` when no separate display name exists.
        :param kernel: Kernel image path when reported by the source.
        :param initrd: Initramfs path when reported.
        :param cmdline: Kernel command line when reported.
        :param device: Source device hint when reported (e.g.
                       ``hd0,gpt2`` from GRUB or ``LABEL=BOOT`` from
                       systemd-boot).  Free-form text.
        :param is_default: ``True`` when the entry is the active
                           default.
        :param boot_count: ``bootctl list`` reports a ``priority``
                           integer for some entries; populated when
                           known.  GRUB2 does not surface this field.
    """
    id: str
    name: str
    kernel: Optional[str] = None
    initrd: Optional[str] = None
    cmdline: Optional[str] = None
    device: Optional[str] = None
    is_default: bool = False
    boot_count: Optional[int] = None


@dataclass
class UbuntuBootEntrySpec:
    """
        Desired bootloader entry shape for ``add_entry``.

        :param id: Caller-chosen identifier.  Used as the
                   ``menuentry`` title for GRUB2 and as the
                   loader-conf basename for systemd-boot.
        :param name: Display title.  GRUB2 uses ``name`` as the
                     ``title`` of the ``menuentry`` block; systemd-boot
                     stores it as the ``title`` directive.
        :param kernel: Absolute path to the kernel image.
        :param initrd: Absolute path to the initramfs image.
        :param cmdline: Kernel command line, space-separated.
        :param device: GRUB2 source device (e.g. ``hd0,gpt2``) or
                       systemd-boot ``machine-id`` / ``LABEL`` hint.
                       Optional.
    """
    id: str
    name: str
    kernel: str
    initrd: Optional[str] = None
    cmdline: Optional[str] = None
    device: Optional[str] = None


class UbuntuBootloaderQueryError(Exception):
    """
        Raised when a bootloader-management read fails for a reason
        other than the target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Bootloader query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Module-level helpers -- GRUB2 parsing and rewriting
# ----------------------------------------------------------------------


def _parse_grub_cfg_menuentries(*, content: str) -> list[UbuntuBootEntryInfo]:
    """
        Parse ``menuentry`` lines from a rendered ``grub.cfg`` body.

        Only the top-level ``menuentry`` directives are extracted;
        nested ``submenu`` entries are returned with their parent
        title-prefixed identifier (e.g.
        ``"Advanced options for Ubuntu>Ubuntu, with Linux 6.8.0"``).
        This matches the format that ``GRUB_DEFAULT=`` accepts when
        ``GRUB_DEFAULT=saved`` is not in use.

        :param content: The ``grub.cfg`` body.

        :returns: The list of :class:`UbuntuBootEntryInfo`, in order.
    """
    rtnval: list[UbuntuBootEntryInfo] = []
    submenu_stack: list[str] = []
    pending_submenu: Optional[str] = None

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("submenu "):
            title = _extract_quoted_argument(line=line)
            if title is None:
                continue
            pending_submenu = title
            continue
        if line == "}":
            if len(submenu_stack) > 0:
                submenu_stack.pop()
            continue
        if line.startswith("menuentry "):
            title = _extract_quoted_argument(line=line)
            if title is None:
                continue
            if pending_submenu is not None:
                submenu_stack.append(pending_submenu)
                pending_submenu = None
            if len(submenu_stack) > 0:
                qualified_id = ">".join(submenu_stack + [title])
            else:
                qualified_id = title
            entry = UbuntuBootEntryInfo(
                id=qualified_id,
                name=title,
                kernel=None,
                initrd=None,
                cmdline=None,
                device=None,
                is_default=False,
                boot_count=None,
            )
            rtnval.append(entry)
            continue

    return rtnval


def _extract_quoted_argument(*, line: str) -> Optional[str]:
    """
        Extract the first single- or double-quoted argument from a
        GRUB ``menuentry`` / ``submenu`` line.

        :param line: The stripped line.

        :returns: The unquoted argument text, or ``None`` when no
                  quoted argument is present.
    """
    double_open = line.find('"')
    single_open = line.find("'")
    if double_open == -1 and single_open == -1:
        rtnval: Optional[str] = None
        return rtnval

    if double_open == -1:
        opening = single_open
        quote = "'"
    elif single_open == -1:
        opening = double_open
        quote = '"'
    elif single_open < double_open:
        opening = single_open
        quote = "'"
    else:
        opening = double_open
        quote = '"'

    closing = line.find(quote, opening + 1)
    if closing == -1:
        rtnval = None
        return rtnval

    rtnval = line[opening + 1:closing]
    return rtnval


def _parse_grub_defaults(*, content: str) -> dict:
    """
        Parse ``KEY=value`` lines from a shell-sourced
        ``/etc/default/grub`` body.  Single- or double-quoted values
        are unquoted.

        :param content: The file body.

        :returns: A dict of ``KEY`` to unquoted string value.
    """
    rtnval: dict = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if len(line) == 0 or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key_part, _, value_part = line.partition("=")
        key = key_part.strip()
        value = value_part.strip()
        if len(value) >= 2:
            first = value[0]
            last = value[-1]
            if (first == "'" and last == "'") or (first == '"' and last == '"'):
                value = value[1:-1]
        rtnval[key] = value
    return rtnval


def _apply_grub_defaults_keys(
    *,
    content: str,
    updates: dict,
) -> str:
    """
        Apply a set of ``KEY=value`` updates to a shell-sourced
        ``/etc/default/grub`` body, preserving comments and ordering.

        Existing matching keys are replaced in place; absent keys are
        appended at the bottom.  Quoting style of the existing entry
        is preserved when present (single-quoted, double-quoted, or
        unquoted); when appending a new entry, the value is double-
        quoted.

        :param content: Original file body.
        :param updates: Mapping of ``KEY`` to desired raw string value
                        (without quotes).

        :returns: The rewritten file body, ending with a newline.
    """
    pending = dict(updates)
    lines = content.splitlines()
    rewritten: list[str] = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if len(stripped) == 0 or stripped.startswith("#"):
            rewritten.append(raw_line)
            continue
        if "=" not in stripped:
            rewritten.append(raw_line)
            continue

        key_part, _, value_part = stripped.partition("=")
        key = key_part.strip()
        if key not in pending:
            rewritten.append(raw_line)
            continue

        new_value = pending.pop(key)
        existing_value = value_part.strip()
        rendered = _render_grub_value(
            new_value=new_value,
            existing_value=existing_value,
        )
        rewritten.append("{}={}".format(key, rendered))

    for key, value in pending.items():
        rendered = _render_grub_value(
            new_value=value,
            existing_value="",
        )
        rewritten.append("{}={}".format(key, rendered))

    body = "\n".join(rewritten)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _render_grub_value(
    *,
    new_value: str,
    existing_value: str,
) -> str:
    """
        Render ``new_value`` using the same quoting style as
        ``existing_value`` when one is detectable.

        :param new_value: Raw replacement value.
        :param existing_value: The existing right-hand side, possibly
                               quoted.

        :returns: The formatted right-hand side.
    """
    if len(existing_value) >= 2:
        first = existing_value[0]
        last = existing_value[-1]
        if first == "'" and last == "'":
            escaped = new_value.replace("'", "'\"'\"'")
            rtnval = "'{}'".format(escaped)
            return rtnval
        if first == '"' and last == '"':
            escaped = new_value.replace('"', '\\"')
            rtnval = '"{}"'.format(escaped)
            return rtnval

    if len(existing_value) == 0:
        escaped = new_value.replace('"', '\\"')
        rtnval = '"{}"'.format(escaped)
        return rtnval

    rtnval = new_value
    return rtnval


def _render_custom_block(*, spec: UbuntuBootEntrySpec) -> str:
    """
        Render a single GRUB ``menuentry`` block for the
        ``/etc/grub.d/40_custom`` drop-in file.

        The block is bracketed by sentinel comments so that
        ``remove_entry`` can excise it without disturbing other
        entries authored by the operator.

        :param spec: The entry to render.

        :returns: The block body, terminated by a trailing newline.
    """
    begin_marker = _CUSTOM_BLOCK_BEGIN.format(entry_id=spec.id)
    end_marker = _CUSTOM_BLOCK_END.format(entry_id=spec.id)

    body_lines: list[str] = []
    body_lines.append(begin_marker)
    body_lines.append(
        "menuentry '{}' {{".format(spec.name.replace("'", "'\"'\"'"))
    )
    if spec.device is not None and len(spec.device) > 0:
        body_lines.append("    set root='{}'".format(spec.device))

    if spec.cmdline is not None and len(spec.cmdline) > 0:
        body_lines.append(
            "    linux {} {}".format(spec.kernel, spec.cmdline)
        )
    else:
        body_lines.append("    linux {}".format(spec.kernel))

    if spec.initrd is not None and len(spec.initrd) > 0:
        body_lines.append("    initrd {}".format(spec.initrd))

    body_lines.append("}")
    body_lines.append(end_marker)
    rtnval = "\n".join(body_lines) + "\n"
    return rtnval


def _has_custom_block(*, content: str, entry_id: str) -> bool:
    """
        Return ``True`` when the supplied body contains a sentinel-
        bracketed block whose identifier matches ``entry_id``.

        :param content: The ``/etc/grub.d/40_custom`` body.
        :param entry_id: The entry id.

        :returns: ``True`` when the block is present.
    """
    begin_marker = _CUSTOM_BLOCK_BEGIN.format(entry_id=entry_id)
    rtnval = begin_marker in content
    return rtnval


def _strip_custom_block(*, content: str, entry_id: str) -> str:
    """
        Remove a sentinel-bracketed block from
        ``/etc/grub.d/40_custom``.

        :param content: The original body.
        :param entry_id: The entry id whose block should be stripped.

        :returns: The body with the block removed.  When no matching
                  block is present, the body is returned unchanged.
    """
    begin_marker = _CUSTOM_BLOCK_BEGIN.format(entry_id=entry_id)
    end_marker = _CUSTOM_BLOCK_END.format(entry_id=entry_id)

    lines = content.splitlines()
    rewritten: list[str] = []
    inside = False
    for raw_line in lines:
        stripped = raw_line.strip()
        if not inside and stripped == begin_marker:
            inside = True
            continue
        if inside and stripped == end_marker:
            inside = False
            continue
        if inside:
            continue
        rewritten.append(raw_line)

    body = "\n".join(rewritten)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body


# ----------------------------------------------------------------------
# Module-level helpers -- systemd-boot
# ----------------------------------------------------------------------


def _parse_bootctl_list(*, text: str) -> list[UbuntuBootEntryInfo]:
    """
        Parse the human-readable output of ``bootctl list``.

        ``bootctl list`` emits stanzas separated by blank lines.  Each
        stanza carries lines such as::

            title: Ubuntu (current)
                  id: ubuntu-6.8.0-31.conf
              source: /boot/loader/entries/ubuntu-6.8.0-31.conf
              linux: /boot/vmlinuz-6.8.0-31-generic
             initrd: /boot/initrd.img-6.8.0-31-generic
            options: root=UUID=... ro quiet splash

        A stanza whose ``title`` ends in ``(default)`` is the current
        default.  The ``id`` field is the loader-conf basename and is
        used as the native entry identifier.

        :param text: The raw stdout text.

        :returns: One :class:`UbuntuBootEntryInfo` per stanza.
    """
    rtnval: list[UbuntuBootEntryInfo] = []
    current: dict = {}

    def _flush(buffer: dict) -> None:
        if len(buffer) == 0:
            return
        raw_title = buffer.get("title", "")
        is_default = "(default)" in raw_title
        clean_title = raw_title
        for marker in ("(default)", "(selected)", "(reported)"):
            clean_title = clean_title.replace(marker, "")
        clean_title = clean_title.strip()

        entry_id = buffer.get("id") or clean_title
        if entry_id.endswith(".conf"):
            entry_id = entry_id[:-len(".conf")]

        info = UbuntuBootEntryInfo(
            id=entry_id,
            name=clean_title,
            kernel=buffer.get("linux"),
            initrd=buffer.get("initrd"),
            cmdline=buffer.get("options"),
            device=buffer.get("source"),
            is_default=is_default,
            boot_count=None,
        )
        rtnval.append(info)
        return

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if len(line) == 0:
            _flush(current)
            current = {}
            continue
        if ":" not in line:
            continue
        key_part, _, value_part = line.partition(":")
        key = key_part.strip().lower()
        value = value_part.strip()
        if key in ("title", "id", "source", "linux", "initrd", "options"):
            current[key] = value

    _flush(current)
    return rtnval


def _parse_bootctl_status_default(*, text: str) -> Optional[str]:
    """
        Parse ``bootctl status`` for the ``Default Boot Loader Entry``
        line and return the entry id (loader-conf basename without
        the ``.conf`` suffix).

        :param text: Raw stdout.

        :returns: The entry id, or ``None`` when the line is absent.
    """
    rtnval: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if lowered.startswith("default boot loader entry:"):
            _, _, value_part = line.partition(":")
            value = value_part.strip()
            if value.endswith(".conf"):
                value = value[:-len(".conf")]
            if len(value) > 0:
                rtnval = value
            return rtnval
        if lowered.startswith("id:") and rtnval is None:
            # Some bootctl versions emit a stanza-style block for the
            # default entry; honour the first ``id:`` line as a fallback.
            _, _, value_part = line.partition(":")
            value = value_part.strip()
            if value.endswith(".conf"):
                value = value[:-len(".conf")]
            if len(value) > 0:
                rtnval = value
    return rtnval


def _render_loader_conf(*, spec: UbuntuBootEntrySpec) -> str:
    """
        Render a systemd-boot loader entry body suitable for
        ``/boot/loader/entries/<id>.conf``.

        :param spec: The entry to render.

        :returns: The file body, terminated by a trailing newline.
    """
    body_lines: list[str] = []
    body_lines.append("title    {}".format(spec.name))
    body_lines.append("linux    {}".format(spec.kernel))
    if spec.initrd is not None and len(spec.initrd) > 0:
        body_lines.append("initrd   {}".format(spec.initrd))
    if spec.cmdline is not None and len(spec.cmdline) > 0:
        body_lines.append("options  {}".format(spec.cmdline))
    if spec.device is not None and len(spec.device) > 0:
        body_lines.append("machine-id {}".format(spec.device))
    rtnval = "\n".join(body_lines) + "\n"
    return rtnval


# ----------------------------------------------------------------------
# UbuntuBootloaderManager
# ----------------------------------------------------------------------


class UbuntuBootloaderManager(BaseFeatureManager):
    """
        Ubuntu OS-level bootloader manager.

        Dispatches every verb through the injected
        :class:`ICommandRunner`.  The :class:`UbuntuBootloaderBackend`
        carried by the config selects between GRUB2 and systemd-boot
        plumbing.  Writes (``set_default_entry``, ``add_entry``,
        ``remove_entry``, ``update_bootloader_config``,
        ``regenerate_config``) honour ``config.elevated``.  Reads
        (``list_entries``, ``get_default_entry``) and idempotency
        probes always pass ``elevated=False``.
    """

    result_class = UbuntuBootloaderActionResult
    status_class = UbuntuBootloaderActionStatus
    feature_name = "bootloader-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_entries(self) -> list[UbuntuBootEntryInfo]:
        """
            Return the list of bootloader entries known to the active
            backend.

            :returns: A list of :class:`UbuntuBootEntryInfo`.
            :raises UbuntuBootloaderQueryError: When the backend
                                                command fails for a
                                                reason other than the
                                                config simply being
                                                missing.
        """
        if self._config.backend == UbuntuBootloaderBackend.GRUB2:
            rtnval = self._list_entries_grub2()
            return rtnval
        rtnval = self._list_entries_systemd_boot()
        return rtnval

    def get_default_entry(self) -> UbuntuBootEntryInfo:
        """
            Return the entry currently marked as the default.

            :returns: The :class:`UbuntuBootEntryInfo` whose
                      ``is_default`` field is ``True``.
            :raises UbuntuBootloaderQueryError: When no entries exist
                                                or the default cannot
                                                be resolved.
        """
        entries = self.list_entries()
        if self._config.backend == UbuntuBootloaderBackend.GRUB2:
            default_token = self._read_grub_default_token()
            resolved = _resolve_grub_default(
                entries=entries,
                token=default_token,
            )
        else:
            default_token = self._read_systemd_boot_default()
            resolved = _resolve_systemd_boot_default(
                entries=entries,
                token=default_token,
            )

        if resolved is None:
            raise UbuntuBootloaderQueryError(
                command=["<resolve-default-entry>"],
                exit_code=None,
                stderr="no_default_entry_resolved",
            )

        rtnval = resolved
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_default_entry(self, entry_id: str) -> UbuntuBootloaderActionResult:
        """
            Persist ``entry_id`` as the active default.

            :param entry_id: The entry identifier as reported by
                             :meth:`list_entries`.

            :returns: The :class:`UbuntuBootloaderActionResult`.
        """
        action = "set_default_entry"
        if self._config.backend == UbuntuBootloaderBackend.GRUB2:
            rtnval = self._set_default_entry_grub2(
                entry_id=entry_id, action=action,
            )
            return rtnval
        rtnval = self._set_default_entry_systemd_boot(
            entry_id=entry_id, action=action,
        )
        return rtnval

    def add_entry(
        self,
        spec: UbuntuBootEntrySpec,
    ) -> UbuntuBootloaderActionResult:
        """
            Register a new bootloader entry described by ``spec``.

            :param spec: The :class:`UbuntuBootEntrySpec`.

            :returns: The :class:`UbuntuBootloaderActionResult`.
        """
        action = "add_entry"
        if self._config.backend == UbuntuBootloaderBackend.GRUB2:
            rtnval = self._add_entry_grub2(spec=spec, action=action)
            return rtnval
        rtnval = self._add_entry_systemd_boot(spec=spec, action=action)
        return rtnval

    def remove_entry(self, entry_id: str) -> UbuntuBootloaderActionResult:
        """
            Remove the bootloader entry identified by ``entry_id``.

            :param entry_id: The entry identifier as reported by
                             :meth:`list_entries`.

            :returns: The :class:`UbuntuBootloaderActionResult`.
        """
        action = "remove_entry"
        if self._config.backend == UbuntuBootloaderBackend.GRUB2:
            rtnval = self._remove_entry_grub2(
                entry_id=entry_id, action=action,
            )
            return rtnval
        rtnval = self._remove_entry_systemd_boot(
            entry_id=entry_id, action=action,
        )
        return rtnval

    def update_bootloader_config(self) -> UbuntuBootloaderActionResult:
        """
            Regenerate AND apply the bootloader configuration.  On
            GRUB2 this is ``update-grub``; on systemd-boot this is
            ``bootctl update``.

            :returns: The :class:`UbuntuBootloaderActionResult`.
        """
        action = "update_bootloader_config"
        if self._config.backend == UbuntuBootloaderBackend.GRUB2:
            command = [_UPDATE_GRUB]
        else:
            command = [_BOOTCTL, "update"]

        rtnval = self._dispatch(
            command=command,
            detail=action,
            operation=action,
            entry_id=None,
            requires_reboot=True,
        )
        return rtnval

    def regenerate_config(self) -> UbuntuBootloaderActionResult:
        """
            Rebuild the on-disk bootloader configuration without
            applying it.  On GRUB2 this is ``grub-mkconfig -o
            <grub_cfg>``; on systemd-boot this is the idempotent
            ``bootctl install``.

            :returns: The :class:`UbuntuBootloaderActionResult`.
        """
        action = "regenerate_config"
        if self._config.backend == UbuntuBootloaderBackend.GRUB2:
            command = [_GRUB_MKCONFIG, "-o", self._config.grub_cfg]
        else:
            command = [_BOOTCTL, "install"]

        rtnval = self._dispatch(
            command=command,
            detail=action,
            operation=action,
            entry_id=None,
            requires_reboot=True,
        )
        return rtnval

    # ------------------------------------------------------------------
    # GRUB2 backend
    # ------------------------------------------------------------------

    def _list_entries_grub2(self) -> list[UbuntuBootEntryInfo]:
        """
            ``cat`` and parse ``grub.cfg``, then mark the default
            entry based on the ``GRUB_DEFAULT`` token.
        """
        cfg_text = self._read_grub_cfg()
        entries = _parse_grub_cfg_menuentries(content=cfg_text)
        default_token = self._read_grub_default_token()
        resolved = _resolve_grub_default(
            entries=entries,
            token=default_token,
        )
        if resolved is not None:
            for entry in entries:
                if entry.id == resolved.id:
                    entry.is_default = True

        rtnval = entries
        return rtnval

    def _set_default_entry_grub2(
        self,
        *,
        entry_id: str,
        action: str,
    ) -> UbuntuBootloaderActionResult:
        """
            Edit ``GRUB_DEFAULT`` in ``/etc/default/grub`` then run
            ``update-grub``.  Skipped when the requested entry is
            already the active default.
        """
        update_command = [_UPDATE_GRUB]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuBootloaderActionStatus.DRY_RUN,
                detail=action,
                command=list(update_command),
                entry_id=entry_id,
                requires_reboot=True,
            )
            return dry

        defaults_text = self._read_grub_defaults()
        parsed = _parse_grub_defaults(content=defaults_text)
        current_token = parsed.get(_GRUB_DEFAULT_KEY, "").strip()
        if current_token == entry_id:
            skipped = self._build_result(
                status=UbuntuBootloaderActionStatus.SKIPPED,
                detail=action,
                command=list(update_command),
                entry_id=entry_id,
                requires_reboot=False,
                message="GRUB_DEFAULT already matches the requested entry",
            )
            return skipped

        new_body = _apply_grub_defaults_keys(
            content=defaults_text,
            updates={_GRUB_DEFAULT_KEY: entry_id},
        )
        write_result = self._write_grub_defaults(content=new_body)
        if write_result.status == UbuntuBootloaderActionStatus.FAILED:
            write_result.detail = action
            write_result.entry_id = entry_id
            return write_result

        rtnval = self._dispatch(
            command=update_command,
            detail=action,
            operation="set_default_entry_grub2",
            entry_id=entry_id,
            requires_reboot=True,
        )
        return rtnval

    def _add_entry_grub2(
        self,
        *,
        spec: UbuntuBootEntrySpec,
        action: str,
    ) -> UbuntuBootloaderActionResult:
        """
            Append a sentinel-bracketed ``menuentry`` block to
            ``/etc/grub.d/40_custom`` then run ``update-grub``.
        """
        update_command = [_UPDATE_GRUB]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuBootloaderActionStatus.DRY_RUN,
                detail=action,
                command=list(update_command),
                entry_id=spec.id,
                requires_reboot=True,
            )
            return dry

        custom_text = self._read_grub_custom()
        if _has_custom_block(content=custom_text, entry_id=spec.id):
            skipped = self._build_result(
                status=UbuntuBootloaderActionStatus.SKIPPED,
                detail=action,
                command=list(update_command),
                entry_id=spec.id,
                requires_reboot=False,
                message="entry already present in /etc/grub.d/40_custom",
            )
            return skipped

        block = _render_custom_block(spec=spec)
        if len(custom_text) > 0 and not custom_text.endswith("\n"):
            new_body = custom_text + "\n" + block
        else:
            new_body = custom_text + block

        write_result = self._write_grub_custom(content=new_body)
        if write_result.status == UbuntuBootloaderActionStatus.FAILED:
            write_result.detail = action
            write_result.entry_id = spec.id
            return write_result

        rtnval = self._dispatch(
            command=update_command,
            detail=action,
            operation="add_entry_grub2",
            entry_id=spec.id,
            requires_reboot=True,
        )
        return rtnval

    def _remove_entry_grub2(
        self,
        *,
        entry_id: str,
        action: str,
    ) -> UbuntuBootloaderActionResult:
        """
            Excise the sentinel-bracketed block from
            ``/etc/grub.d/40_custom`` then run ``update-grub``.
        """
        update_command = [_UPDATE_GRUB]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuBootloaderActionStatus.DRY_RUN,
                detail=action,
                command=list(update_command),
                entry_id=entry_id,
                requires_reboot=True,
            )
            return dry

        custom_text = self._read_grub_custom()
        if not _has_custom_block(content=custom_text, entry_id=entry_id):
            skipped = self._build_result(
                status=UbuntuBootloaderActionStatus.SKIPPED,
                detail=action,
                command=list(update_command),
                entry_id=entry_id,
                requires_reboot=False,
                message="entry not present in /etc/grub.d/40_custom",
            )
            return skipped

        new_body = _strip_custom_block(
            content=custom_text, entry_id=entry_id,
        )
        write_result = self._write_grub_custom(content=new_body)
        if write_result.status == UbuntuBootloaderActionStatus.FAILED:
            write_result.detail = action
            write_result.entry_id = entry_id
            return write_result

        rtnval = self._dispatch(
            command=update_command,
            detail=action,
            operation="remove_entry_grub2",
            entry_id=entry_id,
            requires_reboot=True,
        )
        return rtnval

    def _read_grub_cfg(self) -> str:
        """
            Read the rendered ``grub.cfg`` body via ``cat``.  Reads
            never elevate.

            :returns: The body, or ``""`` on read failure.
        """
        command = [_CAT, self._config.grub_cfg]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_lower = (run_result.stderr or "").lower()
            if "no such file" in stderr_lower:
                rtnval = ""
                return rtnval
            raise UbuntuBootloaderQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )
        rtnval = run_result.stdout
        return rtnval

    def _read_grub_defaults(self) -> str:
        """
            Read ``/etc/default/grub`` via ``cat``.  Reads never
            elevate.

            :returns: The body, or ``""`` when missing.
        """
        command = [_CAT, self._config.grub_defaults]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = ""
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _read_grub_custom(self) -> str:
        """
            Read ``/etc/grub.d/40_custom`` via ``cat``.  Reads never
            elevate.

            :returns: The body, or ``""`` when missing.
        """
        command = [_CAT, self._config.grub_custom]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = ""
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _read_grub_default_token(self) -> str:
        """
            Return the ``GRUB_DEFAULT`` token from
            ``/etc/default/grub``.  Empty string when absent.

            :returns: The token.
        """
        defaults_text = self._read_grub_defaults()
        parsed = _parse_grub_defaults(content=defaults_text)
        rtnval = parsed.get(_GRUB_DEFAULT_KEY, "").strip()
        return rtnval

    def _write_grub_defaults(
        self,
        *,
        content: str,
    ) -> UbuntuBootloaderActionResult:
        """
            Persist ``content`` to ``/etc/default/grub`` via ``tee``.
        """
        command = [_TEE, self._config.grub_defaults]
        rtnval = self._dispatch(
            command=command,
            detail="write_grub_defaults",
            stdin=content,
            operation="write_grub_defaults",
            entry_id=None,
            requires_reboot=False,
        )
        return rtnval

    def _write_grub_custom(
        self,
        *,
        content: str,
    ) -> UbuntuBootloaderActionResult:
        """
            Persist ``content`` to ``/etc/grub.d/40_custom`` via
            ``tee``.
        """
        command = [_TEE, self._config.grub_custom]
        rtnval = self._dispatch(
            command=command,
            detail="write_grub_custom",
            stdin=content,
            operation="write_grub_custom",
            entry_id=None,
            requires_reboot=False,
        )
        return rtnval

    # ------------------------------------------------------------------
    # systemd-boot backend
    # ------------------------------------------------------------------

    def _list_entries_systemd_boot(self) -> list[UbuntuBootEntryInfo]:
        """
            Run ``bootctl list`` and parse the output.
        """
        command = [_BOOTCTL, "list"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_lower = (run_result.stderr or "").lower()
            if "no entries" in stderr_lower or "no such file" in stderr_lower:
                rtnval: list[UbuntuBootEntryInfo] = []
                return rtnval
            raise UbuntuBootloaderQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )
        rtnval = _parse_bootctl_list(text=run_result.stdout)
        return rtnval

    def _read_systemd_boot_default(self) -> Optional[str]:
        """
            Run ``bootctl status`` and parse for the default entry id.

            :returns: The entry id (loader-conf basename without
                      ``.conf``), or ``None`` when absent.
        """
        command = [_BOOTCTL, "status"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = _parse_bootctl_status_default(text=run_result.stdout)
        return rtnval

    def _set_default_entry_systemd_boot(
        self,
        *,
        entry_id: str,
        action: str,
    ) -> UbuntuBootloaderActionResult:
        """
            Run ``bootctl set-default <id>``.  Skipped when the
            requested entry is already the active default.
        """
        command = [_BOOTCTL, "set-default", entry_id]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuBootloaderActionStatus.DRY_RUN,
                detail=action,
                command=list(command),
                entry_id=entry_id,
                requires_reboot=True,
            )
            return dry

        current = self._read_systemd_boot_default()
        if current == entry_id:
            skipped = self._build_result(
                status=UbuntuBootloaderActionStatus.SKIPPED,
                detail=action,
                command=list(command),
                entry_id=entry_id,
                requires_reboot=False,
                message="bootctl already reports this entry as default",
            )
            return skipped

        rtnval = self._dispatch(
            command=command,
            detail=action,
            operation="set_default_entry_systemd_boot",
            entry_id=entry_id,
            requires_reboot=True,
        )
        return rtnval

    def _add_entry_systemd_boot(
        self,
        *,
        spec: UbuntuBootEntrySpec,
        action: str,
    ) -> UbuntuBootloaderActionResult:
        """
            Write ``<id>.conf`` to ``/boot/loader/entries/`` via
            ``tee``.
        """
        target_path = "{}/{}.conf".format(
            self._config.loader_entries_dir, spec.id,
        )
        write_command = [_TEE, target_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuBootloaderActionStatus.DRY_RUN,
                detail=action,
                command=list(write_command),
                entry_id=spec.id,
                requires_reboot=True,
            )
            return dry

        existing_text = self._read_loader_entry(entry_id=spec.id)
        rendered = _render_loader_conf(spec=spec)
        if existing_text == rendered:
            skipped = self._build_result(
                status=UbuntuBootloaderActionStatus.SKIPPED,
                detail=action,
                command=list(write_command),
                entry_id=spec.id,
                requires_reboot=False,
                message="loader entry already matches requested spec",
            )
            return skipped

        rtnval = self._dispatch(
            command=write_command,
            detail=action,
            stdin=rendered,
            operation="add_entry_systemd_boot",
            entry_id=spec.id,
            requires_reboot=True,
        )
        return rtnval

    def _remove_entry_systemd_boot(
        self,
        *,
        entry_id: str,
        action: str,
    ) -> UbuntuBootloaderActionResult:
        """
            Run ``rm /boot/loader/entries/<id>.conf``.
        """
        target_path = "{}/{}.conf".format(
            self._config.loader_entries_dir, entry_id,
        )
        rm_command = [_RM, "-f", target_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuBootloaderActionStatus.DRY_RUN,
                detail=action,
                command=list(rm_command),
                entry_id=entry_id,
                requires_reboot=True,
            )
            return dry

        existing_text = self._read_loader_entry(entry_id=entry_id)
        if len(existing_text) == 0:
            skipped = self._build_result(
                status=UbuntuBootloaderActionStatus.SKIPPED,
                detail=action,
                command=list(rm_command),
                entry_id=entry_id,
                requires_reboot=False,
                message="loader entry already absent",
            )
            return skipped

        rtnval = self._dispatch(
            command=rm_command,
            detail=action,
            operation="remove_entry_systemd_boot",
            entry_id=entry_id,
            requires_reboot=True,
        )
        return rtnval

    def _read_loader_entry(self, *, entry_id: str) -> str:
        """
            Read a systemd-boot loader entry by id via ``cat``.

            :param entry_id: The entry id (loader-conf basename
                             without the ``.conf`` suffix).

            :returns: The body, or ``""`` when missing.
        """
        target_path = "{}/{}.conf".format(
            self._config.loader_entries_dir, entry_id,
        )
        command = [_CAT, target_path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = ""
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_result(
        self,
        *,
        status: UbuntuBootloaderActionStatus,
        detail: Optional[str],
        command: Optional[list[str]],
        entry_id: Optional[str] = None,
        requires_reboot: bool = False,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
        exit_code: Optional[int] = None,
    ) -> UbuntuBootloaderActionResult:
        """
            Construct a :class:`UbuntuBootloaderActionResult` without
            dispatching.

            :param status: The outcome status.
            :param detail: Short identifier of the action.
            :param command: Would-have-been argv (or executed argv).
            :param entry_id: The entry identifier acted upon.
            :param requires_reboot: ``True`` when the change takes
                                    effect only after the next boot.
            :param message: Optional human-readable explanation.
            :param stderr: Optional synthetic stderr payload.
            :param exit_code: Optional exit code.

            :returns: The :class:`UbuntuBootloaderActionResult`.
        """
        if command is None:
            command_copy: Optional[list[str]] = None
        else:
            command_copy = list(command)
        rtnval = UbuntuBootloaderActionResult(
            status=status,
            detail=detail,
            command=command_copy,
            entry_id=entry_id,
            requires_reboot=requires_reboot,
            message=message,
            stderr=stderr,
            exit_code=exit_code,
        )
        return rtnval


# ----------------------------------------------------------------------
# Default-entry resolution helpers
# ----------------------------------------------------------------------


def _resolve_grub_default(
    *,
    entries: list[UbuntuBootEntryInfo],
    token: str,
) -> Optional[UbuntuBootEntryInfo]:
    """
        Resolve the GRUB ``GRUB_DEFAULT`` token to a concrete entry.

        Supports three forms:

            * Numeric index (e.g. ``"0"``).
            * Submenu-qualified title (e.g.
              ``"Ubuntu>Ubuntu, with Linux 6.8.0"``) -- exact match.
            * Bare title -- matched against ``entry.name``.

        Returns ``None`` when no match is found or when the token is
        empty / ``"saved"`` (which routes through ``grub-set-default``
        rather than ``GRUB_DEFAULT=`` and is not resolvable from the
        defaults file alone).

        :param entries: The full entry list as returned by
                        :func:`_parse_grub_cfg_menuentries`.
        :param token: The ``GRUB_DEFAULT`` value.

        :returns: The matched :class:`UbuntuBootEntryInfo`, or
                  ``None``.
    """
    if len(entries) == 0:
        rtnval: Optional[UbuntuBootEntryInfo] = None
        return rtnval
    if len(token) == 0 or token == "saved":
        rtnval = None
        return rtnval

    if token.isdigit():
        index = int(token)
        if 0 <= index < len(entries):
            rtnval = entries[index]
            return rtnval
        rtnval = None
        return rtnval

    for entry in entries:
        if entry.id == token:
            rtnval = entry
            return rtnval
    for entry in entries:
        if entry.name == token:
            rtnval = entry
            return rtnval

    rtnval = None
    return rtnval


def _resolve_systemd_boot_default(
    *,
    entries: list[UbuntuBootEntryInfo],
    token: Optional[str],
) -> Optional[UbuntuBootEntryInfo]:
    """
        Resolve the systemd-boot default token to a concrete entry.

        :param entries: The list as returned by
                        :func:`_parse_bootctl_list`.
        :param token: The ``Default Boot Loader Entry`` token.

        :returns: The matched :class:`UbuntuBootEntryInfo`, or
                  ``None``.
    """
    if len(entries) == 0:
        rtnval: Optional[UbuntuBootEntryInfo] = None
        return rtnval

    if token is not None and len(token) > 0:
        for entry in entries:
            if entry.id == token:
                rtnval = entry
                return rtnval

    for entry in entries:
        if entry.is_default:
            rtnval = entry
            return rtnval

    rtnval = None
    return rtnval
