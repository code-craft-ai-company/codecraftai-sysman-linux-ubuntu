"""
    Ubuntu samba-server-management feature.

    Implements the cross-OS samba-server-management contract for
    Ubuntu.  Ubuntu's stock Samba implementation ships in the
    ``samba`` package; the daemon is exposed under systemd as
    ``smbd.service``.  This module wraps ``smbcontrol``, ``smbpasswd``,
    ``pdbedit``, and the on-disk content of ``/etc/samba/smb.conf``
    through the injected :class:`ICommandRunner` so the manager works
    identically under local and SSH execution.

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | Ubuntu tool                       |
    +=============================+===================================+
    | ``add_share``               | ``cat /etc/samba/smb.conf`` +     |
    |                             | parse + render +                  |
    |                             | ``tee /etc/samba/smb.conf`` +     |
    |                             | ``smbcontrol all reload-config``  |
    +-----------------------------+-----------------------------------+
    | ``remove_share``            | ``cat`` + parse + render +        |
    |                             | ``tee`` + ``smbcontrol``          |
    +-----------------------------+-----------------------------------+
    | ``list_shares``             | ``cat`` + parse                   |
    +-----------------------------+-----------------------------------+
    | ``get_share``               | ``cat`` + parse + filter by name  |
    +-----------------------------+-----------------------------------+
    | ``set_share_permissions``   | ``cat`` + parse + render-with-new-|
    |                             | -acl + ``tee`` + ``smbcontrol``   |
    +-----------------------------+-----------------------------------+
    | ``reload_config``           | ``smbcontrol all reload-config``  |
    +-----------------------------+-----------------------------------+
    | ``add_samba_user``          | ``pdbedit -L`` (idempotency       |
    |                             | probe) + ``smbpasswd -s -a <n>``  |
    |                             | (password via stdin, twice)       |
    +-----------------------------+-----------------------------------+
    | ``remove_samba_user``       | ``pdbedit -L`` + ``smbpasswd -x   |
    |                             | <n>``                             |
    +-----------------------------+-----------------------------------+
    | ``list_samba_users``        | ``pdbedit -L -v``                 |
    +-----------------------------+-----------------------------------+

    File I/O policy:
        Reads use ``cat``; writes use ``tee`` with the new content on
        stdin.  This module does NOT consume an :class:`IFileOperator`.

    Service-name caveat:
        Ubuntu's Samba server systemd unit is ``smbd.service``.
        :class:`UbuntuSambaManagerConfig` therefore defaults
        ``service_name`` to ``"smbd"``.  Note that this manager
        itself NEVER calls ``systemctl`` — the unit name is recorded
        in the config purely so that callers (and downstream
        coordinators) can hand the correct unit to a service-manager
        without re-deriving it.

    Drop-in caveat:
        Ubuntu also honours drop-in files under
        ``/etc/samba/smb.conf.d/*.conf``.  This pilot edits the
        primary ``/etc/samba/smb.conf`` file only; drop-in authoring
        is a tracked follow-up.

    Idempotency model:
        Every write verb reads the current ``smb.conf`` file, parses
        it into ordered sections, mutates the parsed structure in
        memory, re-renders the file, and compares to the original
        content.  When the rendered content equals the original the
        verb returns ``SKIPPED`` without invoking ``tee`` or
        ``smbcontrol``.  Idempotency for ``add_share`` is exact-match
        on the rendered section; for ``set_share_permissions`` the
        comparison is order-insensitive on the ACL principal lists.

    Sentinel-bracketed sections:
        The ``smb.conf`` parser tracks section boundaries by the
        ``[name]`` header line.  A share section starts at its
        header and ends at the next non-comment line that begins
        with ``[``.  Reserved section names (``[global]``,
        ``[homes]``, ``[printers]``) are recognised by the share
        parser and excluded from :meth:`UbuntuSambaServerManager.list_shares`
        output.

    Reload-after-change:
        Every successful mutation that touches ``smb.conf`` is
        followed by ``smbcontrol all reload-config``.  When the
        chained reload fails the verb maps to ``FAILED`` and the
        reload's argv / exit code / stderr are surfaced on the
        returned result.

    Credential handling:
        ``add_samba_user`` passes the password via runner ``stdin``
        twice (the ``-s`` flag of ``smbpasswd`` reads the new
        password and confirmation off stdin).  The recorded
        ``command`` field on the returned result is the bare argv
        ``["smbpasswd", "-s", "-a", "<name>"]`` and NEVER contains
        the password text.  This is true for both ``DRY_RUN`` and
        ``SUCCESS`` / ``FAILED`` results.

    Elevation policy:
        Write verbs (``add_share``, ``remove_share``,
        ``set_share_permissions``, ``reload_config``,
        ``add_samba_user``, ``remove_samba_user``) honour
        ``config.elevated``.  Reads (``list_shares``, ``get_share``,
        ``list_samba_users``) never elevate.  The ``cat`` /
        ``pdbedit -L`` probes used inside write paths to drive
        idempotency decisions also do not elevate.
"""

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
# Module-level constants
# ----------------------------------------------------------------------

_CAT = "cat"
_TEE = "tee"
_SMBCONTROL = "smbcontrol"
_SMBPASSWD = "smbpasswd"
_PDBEDIT = "pdbedit"

_DEFAULT_SMB_CONF = "/etc/samba/smb.conf"
_DEFAULT_SERVICE_NAME = "smbd"

_RESERVED_SECTION_NAMES = ("global", "homes", "printers")


UbuntuSambaActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class UbuntuSambaManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuSambaServerManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param smb_conf: Absolute path to the primary Samba
                         configuration file.  Default is the Ubuntu
                         stock location ``/etc/samba/smb.conf``.
        :param service_name: systemd unit name used by callers who
                             wish to restart the daemon.  This
                             module does NOT call ``systemctl``
                             directly; the unit name is recorded
                             purely for the convenience of
                             downstream coordinators.  Default is
                             ``"smbd"``.
    """
    smb_conf: str = _DEFAULT_SMB_CONF
    service_name: str = _DEFAULT_SERVICE_NAME


@dataclass
class UbuntuSambaActionResult(FeatureActionResult):
    """
        Result of a samba-server write operation.

        :param share_name: The share name the action targeted, or
                           ``None`` for verbs that do not target a
                           single share (``reload_config``,
                           ``add_samba_user``, ``remove_samba_user``).
        :param username: The Samba user the action targeted, or
                         ``None`` for share-scoped verbs.
        :param message: Optional human-readable detail for
                        ``SKIPPED`` / ``FAILED`` outcomes.
    """
    share_name: Optional[str] = None
    username: Optional[str] = None
    message: Optional[str] = None


def _empty_str_list() -> list:
    """
        Build a fresh empty list of strings.

        :returns: An empty list.
    """
    rtnval: list = []
    return rtnval


@dataclass
class UbuntuSambaShareSpec:
    """
        Declarative spec for a single Samba share.

        :param name: The SMB share name as clients see it.
        :param path: The filesystem path exposed by the share.
        :param description: Optional free-form description rendered
                            as the ``comment = <description>``
                            directive.
        :param read_only: Whether the share is read-only.  Default
                          ``False``.
        :param valid_users: Optional list of users allowed access.
                            Empty / ``None`` means all users.
        :param read_list: Optional list of users granted read-only
                          access (overrides ``valid_users`` for
                          read-only).
        :param write_list: Optional list of users granted read-write
                           access.
        :param browseable: Whether the share is browseable.  Default
                           ``True``.
        :param guest_ok: Whether guest access is permitted.  Default
                         ``False``.
    """
    name: str
    path: str
    description: Optional[str] = None
    read_only: bool = False
    valid_users: Optional[list] = None
    read_list: Optional[list] = None
    write_list: Optional[list] = None
    browseable: bool = True
    guest_ok: bool = False


@dataclass
class UbuntuSambaShareInfo:
    """
        One parsed share record.

        :param name: The share name.
        :param path: The filesystem path exposed by the share.
        :param description: The ``comment`` directive value, or
                            ``None`` when absent.
        :param read_only: The ``read only`` directive value.
        :param valid_users: The ``valid users`` directive value
                            (split on whitespace / commas), or
                            ``None`` when absent.
        :param read_list: The ``read list`` directive value, or
                          ``None`` when absent.
        :param write_list: The ``write list`` directive value, or
                           ``None`` when absent.
        :param browseable: The ``browseable`` directive value.
        :param guest_ok: The ``guest ok`` directive value.
        :param is_active: Best-effort live-state flag.  Always
                          ``True`` for shares parsed out of
                          ``smb.conf`` when the file exists; this
                          field is reserved for future
                          ``smbstatus`` integration.
        :param connection_count: Best-effort current connection
                                 count.  Always ``None`` in this
                                 pilot; reserved for future
                                 ``smbstatus`` integration.
    """
    name: str
    path: str
    description: Optional[str]
    read_only: bool
    valid_users: Optional[list]
    read_list: Optional[list]
    write_list: Optional[list]
    browseable: bool
    guest_ok: bool
    is_active: bool
    connection_count: Optional[int]


@dataclass
class UbuntuSambaShareAcl:
    """
        Share-level ACL spec passed to
        :meth:`UbuntuSambaServerManager.set_share_permissions`.

        :param read_users: Users granted read-only access.  Rendered
                           into the ``read list`` directive.
        :param write_users: Users granted read-write access.
                            Rendered into the ``write list``
                            directive.
        :param full_access_users: Users granted full access.
                                  Rendered into the ``valid users``
                                  directive (the union of the
                                  ``valid_users`` / ``write_list``
                                  / ``read_list`` semantics — see
                                  :func:`_apply_acl` for the
                                  rendering rules).
        :param denied_users: Users explicitly denied access.
                             Rendered into the ``invalid users``
                             directive.
    """
    read_users: list = field(default_factory=_empty_str_list)
    write_users: list = field(default_factory=_empty_str_list)
    full_access_users: list = field(default_factory=_empty_str_list)
    denied_users: list = field(default_factory=_empty_str_list)


@dataclass
class UbuntuSambaUserInfo:
    """
        One parsed Samba user record from ``pdbedit -L -v``.

        :param username: The user's login name.
        :param enabled: ``True`` when the account is enabled (i.e.
                        the ``D`` flag is absent from the user's
                        flag string).
        :param last_password_change: Free-form text of the last
                                     password change as reported by
                                     ``pdbedit -v``, or ``None``
                                     when the field is absent.
        :param description: The user's GECOS / description, or
                            ``None`` when absent.
    """
    username: str
    enabled: bool
    last_password_change: Optional[str]
    description: Optional[str]


class UbuntuSambaQueryError(Exception):
    """
        Raised when a Samba read fails for a reason other than the
        target file simply being absent.
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
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Samba server query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Section model used by the parser / renderer
# ----------------------------------------------------------------------


@dataclass
class _SmbSection:
    """
        Internal model carrying a single parsed ``smb.conf`` section.

        Free-form preamble lines (comments / blanks before the first
        section header) are stored under a synthetic section with
        ``name=None``.

        :param name: Section name without the surrounding brackets,
                     or ``None`` for the synthetic preamble section.
        :param header_line: The verbatim ``[name]`` header line as
                            written in the file (preserves
                            whitespace), or ``None`` for the
                            preamble.
        :param body_lines: Verbatim body lines (every line after the
                           header up to the next header).  Comments
                           and blanks are preserved.
    """
    name: Optional[str]
    header_line: Optional[str]
    body_lines: list


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class UbuntuSambaServerManager(BaseFeatureManager):
    """
        Ubuntu samba-server-management manager.  Reads, writes, and
        reloads ``/etc/samba/smb.conf`` through the injected
        :class:`ICommandRunner`.
    """

    result_class = UbuntuSambaActionResult
    status_class = UbuntuSambaActionStatus
    feature_name = "samba-server-management"

    def __init__(
        self,
        runner,
        config: Optional[UbuntuSambaManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every
                           dispatch.
            :param config: Manager configuration.  Defaults to a
                           fresh :class:`UbuntuSambaManagerConfig`.
        """
        if config is None:
            config = UbuntuSambaManagerConfig()
        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_shares(self) -> list:
        """
            Enumerate every non-reserved share defined in
            ``/etc/samba/smb.conf``.

            :returns: A ``list[UbuntuSambaShareInfo]``.  Empty when
                      the configuration file is missing.

            :raises UbuntuSambaQueryError: When ``cat`` exits non-
                                           zero with non-empty
                                           stderr.
        """
        path = self._typed_config().smb_conf
        content = self._read_file_or_raise(path=path)
        if content is None:
            rtnval: list = []
            return rtnval

        sections = _parse_smb_conf(content=content)

        rtnval = []
        for section in sections:
            if section.name is None:
                continue
            normalised = section.name.strip().lower()
            if normalised in _RESERVED_SECTION_NAMES:
                continue
            info = _section_to_share_info(section=section)
            rtnval.append(info)
        return rtnval

    def get_share(self, name: str) -> Optional[UbuntuSambaShareInfo]:
        """
            Look up a single share by name.

            :param name: The share name.

            :returns: The :class:`UbuntuSambaShareInfo`, or ``None``
                      when no matching section exists or the file is
                      missing.

            :raises UbuntuSambaQueryError: When ``cat`` exits non-
                                           zero with non-empty
                                           stderr.
        """
        path = self._typed_config().smb_conf
        content = self._read_file_or_raise(path=path)
        if content is None:
            rtnval: Optional[UbuntuSambaShareInfo] = None
            return rtnval

        sections = _parse_smb_conf(content=content)
        for section in sections:
            if section.name is None:
                continue
            if section.name == name:
                rtnval = _section_to_share_info(section=section)
                return rtnval
        rtnval = None
        return rtnval

    def list_samba_users(self) -> list:
        """
            Enumerate every Samba user via ``pdbedit -L -v``.

            :returns: A ``list[UbuntuSambaUserInfo]``.  Empty when
                      ``pdbedit`` reports an empty database.

            :raises UbuntuSambaQueryError: When ``pdbedit`` exits
                                           non-zero with non-empty
                                           stderr.
        """
        command = [_PDBEDIT, "-L", "-v"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise UbuntuSambaQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = _parse_pdbedit_output(text=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — share CRUD
    # ------------------------------------------------------------------

    def add_share(
        self,
        spec: UbuntuSambaShareSpec,
    ) -> UbuntuSambaActionResult:
        """
            Add or update the share named ``spec.name``.

            Idempotent: returns ``SKIPPED`` when the file already
            contains a section ``[spec.name]`` whose rendered shape
            matches the spec exactly.  Honours dry-run.  After a
            successful ``tee`` the manager dispatches
            ``smbcontrol all reload-config``; if that chained reload
            fails the verb maps to ``FAILED`` and the reload's argv /
            exit / stderr are surfaced on the returned result.

            :param spec: The :class:`UbuntuSambaShareSpec`.

            :returns: The :class:`UbuntuSambaActionResult`.
        """
        target_path = self._typed_config().smb_conf
        write_command = [_TEE, target_path]

        existing = self._read_file(path=target_path)
        existing_content = "" if existing is None else existing
        existing_sections = _parse_smb_conf(content=existing_content)

        new_section = _share_spec_to_section(spec=spec)
        match_status = _match_existing_share(
            sections=existing_sections, new_section=new_section,
        )
        if match_status == "exact":
            skipped = self._build_result(
                status=UbuntuSambaActionStatus.SKIPPED,
                share_name=spec.name,
                username=None,
                command=write_command,
                message="share already at desired state",
            )
            return skipped

        new_sections = _upsert_section(
            sections=existing_sections, new_section=new_section,
        )
        new_content = _render_smb_conf(sections=new_sections)

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuSambaActionStatus.DRY_RUN,
                share_name=spec.name,
                username=None,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code != 0:
            failed = UbuntuSambaActionResult(
                status=UbuntuSambaActionStatus.FAILED,
                detail="add_share:{}".format(spec.name),
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                share_name=spec.name,
                username=None,
            )
            return failed

        rtnval = self._invoke_smbcontrol_reload(
            base_action="add_share:{}".format(spec.name),
            share_name=spec.name,
            primary_run=run_result,
            primary_command=write_command,
        )
        return rtnval

    def remove_share(self, name: str) -> UbuntuSambaActionResult:
        """
            Remove the share named ``name``.

            Idempotent: returns ``SKIPPED`` when the file is missing
            or contains no matching section.  Honours dry-run.
            After a successful ``tee`` the manager dispatches
            ``smbcontrol all reload-config``.

            :param name: The share name.

            :returns: The :class:`UbuntuSambaActionResult`.
        """
        target_path = self._typed_config().smb_conf
        write_command = [_TEE, target_path]

        existing = self._read_file(path=target_path)
        if existing is None:
            skipped = self._build_result(
                status=UbuntuSambaActionStatus.SKIPPED,
                share_name=name,
                username=None,
                command=write_command,
                message="smb.conf does not exist",
            )
            return skipped

        existing_sections = _parse_smb_conf(content=existing)
        new_sections = _remove_section_by_name(
            sections=existing_sections, name=name,
        )
        new_content = _render_smb_conf(sections=new_sections)

        if new_content == existing:
            skipped = self._build_result(
                status=UbuntuSambaActionStatus.SKIPPED,
                share_name=name,
                username=None,
                command=write_command,
                message="share not present",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuSambaActionStatus.DRY_RUN,
                share_name=name,
                username=None,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code != 0:
            failed = UbuntuSambaActionResult(
                status=UbuntuSambaActionStatus.FAILED,
                detail="remove_share:{}".format(name),
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                share_name=name,
                username=None,
            )
            return failed

        rtnval = self._invoke_smbcontrol_reload(
            base_action="remove_share:{}".format(name),
            share_name=name,
            primary_run=run_result,
            primary_command=write_command,
        )
        return rtnval

    def set_share_permissions(
        self,
        name: str,
        acl: UbuntuSambaShareAcl,
    ) -> UbuntuSambaActionResult:
        """
            Update the permission keys (``valid users`` / ``read
            list`` / ``write list`` / ``invalid users``) on the
            share named ``name``.

            Idempotent: returns ``SKIPPED`` when the existing
            directives already match the supplied ACL (order-
            insensitive).  Honours dry-run.  After a successful
            ``tee`` the manager dispatches
            ``smbcontrol all reload-config``.

            :param name: The share name.
            :param acl: The :class:`UbuntuSambaShareAcl`.

            :returns: The :class:`UbuntuSambaActionResult`.
        """
        target_path = self._typed_config().smb_conf
        write_command = [_TEE, target_path]

        existing = self._read_file(path=target_path)
        if existing is None:
            skipped = self._build_result(
                status=UbuntuSambaActionStatus.SKIPPED,
                share_name=name,
                username=None,
                command=write_command,
                message="smb.conf does not exist",
            )
            return skipped

        existing_sections = _parse_smb_conf(content=existing)
        target_section: Optional[_SmbSection] = None
        for section in existing_sections:
            if section.name == name:
                target_section = section
                break
        if target_section is None:
            skipped = self._build_result(
                status=UbuntuSambaActionStatus.SKIPPED,
                share_name=name,
                username=None,
                command=write_command,
                message="share not present",
            )
            return skipped

        if _acl_matches(section=target_section, acl=acl):
            skipped = self._build_result(
                status=UbuntuSambaActionStatus.SKIPPED,
                share_name=name,
                username=None,
                command=write_command,
                message="acl already at desired value",
            )
            return skipped

        updated_section = _apply_acl(section=target_section, acl=acl)
        new_sections = _replace_section(
            sections=existing_sections, updated=updated_section,
        )
        new_content = _render_smb_conf(sections=new_sections)

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuSambaActionStatus.DRY_RUN,
                share_name=name,
                username=None,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code != 0:
            failed = UbuntuSambaActionResult(
                status=UbuntuSambaActionStatus.FAILED,
                detail="set_share_permissions:{}".format(name),
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                share_name=name,
                username=None,
            )
            return failed

        rtnval = self._invoke_smbcontrol_reload(
            base_action="set_share_permissions:{}".format(name),
            share_name=name,
            primary_run=run_result,
            primary_command=write_command,
        )
        return rtnval

    def reload_config(self) -> UbuntuSambaActionResult:
        """
            Reload the running daemon's configuration via
            ``smbcontrol all reload-config``.

            Operational verb — never skips.  Honours dry-run.

            :returns: The :class:`UbuntuSambaActionResult`.
        """
        command = [_SMBCONTROL, "all", "reload-config"]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuSambaActionStatus.DRY_RUN,
                share_name=None,
                username=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            share_name=None,
            username=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — Samba user management
    # ------------------------------------------------------------------

    def add_samba_user(
        self,
        name: str,
        password: str,
    ) -> UbuntuSambaActionResult:
        """
            Register a Samba user via ``smbpasswd -s -a <name>``.
            The password is delivered via runner ``stdin`` (twice —
            ``-s`` reads the new password and confirmation off
            stdin).

            Idempotent: returns ``SKIPPED`` when ``pdbedit -L``
            already lists the user.  Honours dry-run.

            Sanitisation: the recorded ``command`` field on the
            returned result is the bare argv ``["smbpasswd", "-s",
            "-a", "<name>"]`` and NEVER contains the password text,
            on every status (``DRY_RUN``, ``SUCCESS``, ``FAILED``,
            ``SKIPPED``).

            :param name: The Samba user's login name.
            :param password: The plaintext password.  Routed via
                             stdin.

            :returns: The :class:`UbuntuSambaActionResult`.
        """
        sanitised_command = [_SMBPASSWD, "-s", "-a", name]

        existing_users = self._invoke_pdbedit_list_safe()
        if _user_in_list(users=existing_users, username=name):
            skipped = self._build_result(
                status=UbuntuSambaActionStatus.SKIPPED,
                share_name=None,
                username=name,
                command=sanitised_command,
                message="samba user already present",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuSambaActionStatus.DRY_RUN,
                share_name=None,
                username=name,
                command=sanitised_command,
            )
            return dry

        # The ``-s`` flag of smbpasswd reads the new password and a
        # confirmation copy from stdin.  We must therefore deliver
        # the password TWICE.
        stdin_payload = "{}\n{}\n".format(password, password)

        run_result = self._runner.run(
            list(sanitised_command),
            elevated=self._config.elevated,
            stdin=stdin_payload,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=sanitised_command,
            share_name=None,
            username=name,
        )
        return rtnval

    def remove_samba_user(self, name: str) -> UbuntuSambaActionResult:
        """
            Remove a Samba user via ``smbpasswd -x <name>``.

            Idempotent: returns ``SKIPPED`` when ``pdbedit -L`` does
            NOT list the user.  Honours dry-run.

            :param name: The Samba user's login name.

            :returns: The :class:`UbuntuSambaActionResult`.
        """
        command = [_SMBPASSWD, "-x", name]

        existing_users = self._invoke_pdbedit_list_safe()
        if not _user_in_list(users=existing_users, username=name):
            skipped = self._build_result(
                status=UbuntuSambaActionStatus.SKIPPED,
                share_name=None,
                username=name,
                command=command,
                message="samba user not present",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuSambaActionStatus.DRY_RUN,
                share_name=None,
                username=name,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            share_name=None,
            username=name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> UbuntuSambaManagerConfig:
        """
            Narrow the base config to the typed subclass.

            :returns: The typed
                      :class:`UbuntuSambaManagerConfig`.
        """
        rtnval = self._config
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat`` without elevation, returning
            ``None`` when the file does not exist (used by write
            paths that treat a missing file as 'no existing
            content').

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when
                      ``cat`` reports a non-zero exit code.
        """
        command = [_CAT, path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _read_file_or_raise(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat`` without elevation.  When ``cat``
            exits non-zero with non-empty stderr (i.e., something
            other than 'file does not exist'), raise
            :class:`UbuntuSambaQueryError`.  When ``cat`` exits non-
            zero with empty stderr the missing-file case is reported
            by returning ``None``.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when
                      the file is simply absent.

            :raises UbuntuSambaQueryError: When ``cat`` fails for a
                                           reason other than the
                                           file being absent.
        """
        command = [_CAT, path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code == 0:
            rtnval: Optional[str] = run_result.stdout
            return rtnval
        if len(run_result.stderr.strip()) > 0:
            raise UbuntuSambaQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = None
        return rtnval

    def _invoke_pdbedit_list_safe(self) -> list:
        """
            Run ``pdbedit -L`` to drive the ``add_samba_user`` /
            ``remove_samba_user`` idempotency probe.  Failures here
            return an empty list rather than raising — the calling
            verb will surface the failure via the subsequent
            ``smbpasswd`` dispatch.

            :returns: A list of usernames (lowercased).
        """
        command = [_PDBEDIT, "-L"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval
        rtnval = []
        for raw in run_result.stdout.splitlines():
            stripped = raw.strip()
            if len(stripped) == 0:
                continue
            # ``pdbedit -L`` emits ``<user>:<uid>:<gecos>``.
            colon = stripped.find(":")
            if colon == -1:
                rtnval.append(stripped)
                continue
            username = stripped[:colon]
            rtnval.append(username)
        return rtnval

    def _invoke_smbcontrol_reload(
        self,
        *,
        base_action: str,
        share_name: Optional[str],
        primary_run,
        primary_command: list,
    ) -> UbuntuSambaActionResult:
        """
            Dispatch ``smbcontrol all reload-config`` after a
            successful primary mutation.  When the reload fails the
            returned result is mapped to ``FAILED`` even though the
            primary mutation succeeded.

            :param base_action: The detail string identifying the
                                primary verb (e.g.,
                                ``"add_share:public"``).
            :param share_name: The share name the primary verb
                               targeted, or ``None`` for verbs that
                               do not target a single share.
            :param primary_run: The :class:`RunResult` from the
                                preceding ``tee`` call.
            :param primary_command: The argv that ran first.

            :returns: A :class:`UbuntuSambaActionResult` whose status
                      reflects the ``smbcontrol`` outcome.
        """
        reload_command = [_SMBCONTROL, "all", "reload-config"]
        reload_run = self._runner.run(
            list(reload_command),
            elevated=self._config.elevated,
        )
        if reload_run.exit_code != 0:
            rtnval = UbuntuSambaActionResult(
                status=UbuntuSambaActionStatus.FAILED,
                detail=base_action,
                exit_code=reload_run.exit_code,
                stdout=reload_run.stdout,
                stderr=reload_run.stderr,
                command=list(reload_command),
                share_name=share_name,
                username=None,
            )
            return rtnval

        rtnval = UbuntuSambaActionResult(
            status=UbuntuSambaActionStatus.SUCCESS,
            detail=base_action,
            exit_code=primary_run.exit_code,
            stdout=primary_run.stdout,
            stderr=primary_run.stderr,
            command=list(primary_command),
            share_name=share_name,
            username=None,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuSambaActionStatus,
        share_name: Optional[str],
        username: Optional[str],
        command: list,
        message: Optional[str] = None,
    ) -> UbuntuSambaActionResult:
        """
            Build a result without invoking the runner.

            :param status: Outcome status.
            :param share_name: Share name targeted, or ``None``.
            :param username: Samba user targeted, or ``None``.
            :param command: Would-have-been argv (sanitised when
                            applicable).
            :param message: Optional human-readable detail.

            :returns: The :class:`UbuntuSambaActionResult`.
        """
        if share_name is not None:
            detail_text = share_name
        elif username is not None:
            detail_text = username
        else:
            detail_text = None
        rtnval = UbuntuSambaActionResult(
            status=status,
            detail=detail_text,
            command=list(command),
            share_name=share_name,
            username=username,
            message=message,
        )
        return rtnval

    def _wrap_runner_result(
        self,
        *,
        run_result,
        command: list,
        share_name: Optional[str],
        username: Optional[str],
    ) -> UbuntuSambaActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`UbuntuSambaActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = UbuntuSambaActionStatus.SUCCESS
        else:
            mapped_status = UbuntuSambaActionStatus.FAILED

        if share_name is not None:
            detail_text = share_name
        elif username is not None:
            detail_text = username
        else:
            detail_text = None

        rtnval = UbuntuSambaActionResult(
            status=mapped_status,
            detail=detail_text,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            share_name=share_name,
            username=username,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — smb.conf parser / renderer
# ----------------------------------------------------------------------


def _parse_smb_conf(*, content: str) -> list:
    """
        Parse an ``smb.conf`` body into an ordered list of
        :class:`_SmbSection` records.

        Section headers are lines whose first non-whitespace
        character is ``[``.  Any preamble (comments / blanks
        appearing before the first header) is captured under a
        synthetic section with ``name=None``.  Comments and blank
        lines inside a section body are preserved verbatim.

        :param content: The raw file content.

        :returns: A ``list[_SmbSection]`` in declaration order.
    """
    rtnval: list = []
    current = _SmbSection(name=None, header_line=None, body_lines=[])

    for raw in content.splitlines():
        stripped = raw.strip()
        if (
            len(stripped) > 0
            and stripped.startswith("[")
            and stripped.endswith("]")
            and not stripped.startswith("#")
        ):
            # New section starts.  Flush the current one.
            if (
                current.name is not None
                or len(current.body_lines) > 0
            ):
                rtnval.append(current)
            section_name = stripped[1:-1].strip()
            current = _SmbSection(
                name=section_name,
                header_line=raw,
                body_lines=[],
            )
            continue
        current.body_lines.append(raw)

    if current.name is not None or len(current.body_lines) > 0:
        rtnval.append(current)

    return rtnval


def _render_smb_conf(*, sections: list) -> str:
    """
        Render an ordered :class:`_SmbSection` list back to a
        canonical INI body.

        :param sections: The sections to render.

        :returns: The rendered file content with a trailing newline.
    """
    parts: list = []
    for section in sections:
        if section.name is None:
            for line in section.body_lines:
                parts.append(line)
            continue
        if section.header_line is not None:
            parts.append(section.header_line)
        else:
            parts.append("[{}]".format(section.name))
        for line in section.body_lines:
            parts.append(line)

    body = "\n".join(parts)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _parse_directives(*, body_lines: list) -> dict:
    """
        Parse a section's body lines into an ordered dict of
        ``key → value`` directive strings.

        Comment and blank lines are skipped.  Keys are normalised
        to lower-case with embedded whitespace collapsed to single
        spaces (Samba treats ``read only``, ``Read Only``, and
        ``read  only`` as the same directive).  Values are stripped
        of leading / trailing whitespace.

        :param body_lines: The verbatim body lines of a parsed
                           section.

        :returns: A ``dict[str, str]`` of normalised directives.
                  Insertion order is preserved.
    """
    rtnval: dict = {}
    for raw in body_lines:
        stripped = raw.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#") or stripped.startswith(";"):
            continue
        equal_index = stripped.find("=")
        if equal_index == -1:
            continue
        key_raw = stripped[:equal_index].strip()
        value_raw = stripped[equal_index + 1:].strip()
        key_normalised = " ".join(key_raw.lower().split())
        rtnval[key_normalised] = value_raw
    return rtnval


def _section_to_share_info(*, section: _SmbSection) -> UbuntuSambaShareInfo:
    """
        Convert a parsed :class:`_SmbSection` to a
        :class:`UbuntuSambaShareInfo` record.

        :param section: The parsed section.

        :returns: The populated info record.
    """
    directives = _parse_directives(body_lines=section.body_lines)
    path_value = directives.get("path", "")
    description = directives.get("comment")
    read_only = _parse_bool_directive(value=directives.get("read only"))
    if "writable" in directives and "read only" not in directives:
        # Samba's ``writable`` is the inverse of ``read only``.
        writable_flag = _parse_bool_directive(
            value=directives.get("writable"),
        )
        read_only = not writable_flag
    valid_users = _split_user_list(value=directives.get("valid users"))
    read_list = _split_user_list(value=directives.get("read list"))
    write_list = _split_user_list(value=directives.get("write list"))
    browseable = _parse_bool_directive(
        value=directives.get("browseable"), default=True,
    )
    if "browsable" in directives and "browseable" not in directives:
        browseable = _parse_bool_directive(
            value=directives.get("browsable"), default=True,
        )
    guest_ok = _parse_bool_directive(value=directives.get("guest ok"))

    rtnval = UbuntuSambaShareInfo(
        name=section.name,
        path=path_value,
        description=description,
        read_only=read_only,
        valid_users=valid_users,
        read_list=read_list,
        write_list=write_list,
        browseable=browseable,
        guest_ok=guest_ok,
        is_active=True,
        connection_count=None,
    )
    return rtnval


def _parse_bool_directive(
    *,
    value: Optional[str],
    default: bool = False,
) -> bool:
    """
        Parse a Samba boolean directive value.

        Samba accepts ``yes`` / ``no`` / ``true`` / ``false`` /
        ``1`` / ``0``.  Comparison is case-insensitive.

        :param value: The raw directive value, or ``None`` when
                      absent.
        :param default: The value to return when the directive is
                        absent.

        :returns: The parsed boolean.
    """
    if value is None:
        rtnval = default
        return rtnval
    normalised = value.strip().lower()
    if normalised in ("yes", "true", "1", "on"):
        rtnval = True
        return rtnval
    if normalised in ("no", "false", "0", "off"):
        rtnval = False
        return rtnval
    rtnval = default
    return rtnval


def _split_user_list(*, value: Optional[str]) -> Optional[list]:
    """
        Split a Samba user-list directive (``valid users``,
        ``read list``, etc.) into a list of principals.

        Samba accepts whitespace and commas as separators.

        :param value: The raw directive value.

        :returns: A list of principal strings, or ``None`` when the
                  value is absent or empty.
    """
    if value is None:
        rtnval: Optional[list] = None
        return rtnval
    stripped = value.strip()
    if len(stripped) == 0:
        rtnval = None
        return rtnval
    normalised = stripped.replace(",", " ")
    parts = normalised.split()
    if len(parts) == 0:
        rtnval = None
        return rtnval
    rtnval = parts
    return rtnval


def _share_spec_to_section(
    *,
    spec: UbuntuSambaShareSpec,
) -> _SmbSection:
    """
        Render a :class:`UbuntuSambaShareSpec` into a
        :class:`_SmbSection`.

        :param spec: The share spec.

        :returns: The populated section.
    """
    body_lines: list = []
    body_lines.append("    path = {}".format(spec.path))
    if spec.description is not None and len(spec.description) > 0:
        body_lines.append("    comment = {}".format(spec.description))
    if spec.read_only:
        body_lines.append("    read only = yes")
    else:
        body_lines.append("    read only = no")
    if spec.browseable:
        body_lines.append("    browseable = yes")
    else:
        body_lines.append("    browseable = no")
    if spec.guest_ok:
        body_lines.append("    guest ok = yes")
    else:
        body_lines.append("    guest ok = no")
    if spec.valid_users is not None and len(spec.valid_users) > 0:
        body_lines.append(
            "    valid users = {}".format(" ".join(spec.valid_users)),
        )
    if spec.read_list is not None and len(spec.read_list) > 0:
        body_lines.append(
            "    read list = {}".format(" ".join(spec.read_list)),
        )
    if spec.write_list is not None and len(spec.write_list) > 0:
        body_lines.append(
            "    write list = {}".format(" ".join(spec.write_list)),
        )

    rtnval = _SmbSection(
        name=spec.name,
        header_line="[{}]".format(spec.name),
        body_lines=body_lines,
    )
    return rtnval


def _match_existing_share(
    *,
    sections: list,
    new_section: _SmbSection,
) -> str:
    """
        Compare ``new_section`` against the parsed sections list.

        :param sections: The parsed list of :class:`_SmbSection`.
        :param new_section: The freshly-rendered section.

        :returns: ``"exact"`` when an existing section with the same
                  name already carries identical directives;
                  ``"name-only"`` when the name exists with
                  different directives; ``"absent"`` when the name
                  is not present.
    """
    rtnval: str = "absent"
    for section in sections:
        if section.name != new_section.name:
            continue
        existing_directives = _parse_directives(
            body_lines=section.body_lines,
        )
        new_directives = _parse_directives(
            body_lines=new_section.body_lines,
        )
        if _directives_match(
            left=existing_directives, right=new_directives,
        ):
            rtnval = "exact"
            return rtnval
        rtnval = "name-only"
        return rtnval
    return rtnval


def _directives_match(*, left: dict, right: dict) -> bool:
    """
        Compare two directive maps for equality, treating list-
        valued directives (``valid users``, ``read list``, ``write
        list``, ``invalid users``) as order-insensitive sets.

        :param left: First directive map.
        :param right: Second directive map.

        :returns: True when the maps match on every key.
    """
    if set(left.keys()) != set(right.keys()):
        rtnval = False
        return rtnval

    list_valued_keys = (
        "valid users",
        "read list",
        "write list",
        "invalid users",
    )
    for key, left_value in left.items():
        right_value = right[key]
        if key in list_valued_keys:
            left_parts = sorted(left_value.replace(",", " ").split())
            right_parts = sorted(right_value.replace(",", " ").split())
            if left_parts != right_parts:
                rtnval = False
                return rtnval
            continue
        if left_value != right_value:
            rtnval = False
            return rtnval
    rtnval = True
    return rtnval


def _upsert_section(
    *,
    sections: list,
    new_section: _SmbSection,
) -> list:
    """
        Return ``sections`` with ``new_section`` either replacing an
        existing section of the same name or appended at the end.

        :param sections: The original section list.
        :param new_section: The section to upsert.

        :returns: A new list with the upserted section.
    """
    rtnval: list = []
    replaced = False
    for section in sections:
        if section.name == new_section.name and not replaced:
            rtnval.append(new_section)
            replaced = True
            continue
        if section.name == new_section.name and replaced:
            # Drop duplicate sections with the same name.
            continue
        rtnval.append(section)
    if not replaced:
        rtnval.append(new_section)
    return rtnval


def _replace_section(
    *,
    sections: list,
    updated: _SmbSection,
) -> list:
    """
        Replace the section whose name matches ``updated.name`` in
        ``sections``.

        :param sections: The original section list.
        :param updated: The replacement section.

        :returns: A new list with the section replaced.
    """
    rtnval: list = []
    for section in sections:
        if section.name == updated.name:
            rtnval.append(updated)
            continue
        rtnval.append(section)
    return rtnval


def _remove_section_by_name(
    *,
    sections: list,
    name: str,
) -> list:
    """
        Return ``sections`` with every section whose name equals
        ``name`` removed.

        :param sections: The original section list.
        :param name: The section name to drop.

        :returns: A new list with the section(s) removed.
    """
    rtnval: list = []
    for section in sections:
        if section.name == name:
            continue
        rtnval.append(section)
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — ACL idempotency / application
# ----------------------------------------------------------------------


def _acl_matches(
    *,
    section: _SmbSection,
    acl: UbuntuSambaShareAcl,
) -> bool:
    """
        Compare the existing permission directives on ``section``
        against ``acl`` (order-insensitive).

        :param section: The parsed section.
        :param acl: The desired ACL.

        :returns: True when the two carry identical principal sets.
    """
    directives = _parse_directives(body_lines=section.body_lines)

    existing_valid = _split_user_list(value=directives.get("valid users"))
    existing_read = _split_user_list(value=directives.get("read list"))
    existing_write = _split_user_list(value=directives.get("write list"))
    existing_invalid = _split_user_list(
        value=directives.get("invalid users"),
    )

    if not _principal_lists_equal(
        left=existing_valid, right=acl.full_access_users,
    ):
        rtnval = False
        return rtnval
    if not _principal_lists_equal(
        left=existing_read, right=acl.read_users,
    ):
        rtnval = False
        return rtnval
    if not _principal_lists_equal(
        left=existing_write, right=acl.write_users,
    ):
        rtnval = False
        return rtnval
    if not _principal_lists_equal(
        left=existing_invalid, right=acl.denied_users,
    ):
        rtnval = False
        return rtnval
    rtnval = True
    return rtnval


def _principal_lists_equal(
    *,
    left: Optional[list],
    right: Optional[list],
) -> bool:
    """
        Compare two principal lists order-insensitively, treating
        ``None`` and ``[]`` as equal.

        :param left: First list (may be ``None``).
        :param right: Second list (may be ``None``).

        :returns: True when the two carry identical sets.
    """
    left_set = set(left) if left is not None else set()
    right_set = set(right) if right is not None else set()
    rtnval = left_set == right_set
    return rtnval


def _apply_acl(
    *,
    section: _SmbSection,
    acl: UbuntuSambaShareAcl,
) -> _SmbSection:
    """
        Return a new :class:`_SmbSection` with the permission
        directives (``valid users`` / ``read list`` / ``write list``
        / ``invalid users``) replaced from ``acl``.

        Other directives are preserved verbatim.

        :param section: The original section.
        :param acl: The :class:`UbuntuSambaShareAcl` to apply.

        :returns: A new section with the updated ACL.
    """
    target_keys = (
        "valid users",
        "read list",
        "write list",
        "invalid users",
    )
    preserved: list = []
    for raw in section.body_lines:
        stripped = raw.strip()
        if len(stripped) == 0 or stripped.startswith("#") or stripped.startswith(";"):
            preserved.append(raw)
            continue
        equal_index = stripped.find("=")
        if equal_index == -1:
            preserved.append(raw)
            continue
        key_raw = stripped[:equal_index].strip()
        key_normalised = " ".join(key_raw.lower().split())
        if key_normalised in target_keys:
            continue
        preserved.append(raw)

    if len(acl.full_access_users) > 0:
        preserved.append(
            "    valid users = {}".format(
                " ".join(acl.full_access_users),
            ),
        )
    if len(acl.read_users) > 0:
        preserved.append(
            "    read list = {}".format(" ".join(acl.read_users)),
        )
    if len(acl.write_users) > 0:
        preserved.append(
            "    write list = {}".format(" ".join(acl.write_users)),
        )
    if len(acl.denied_users) > 0:
        preserved.append(
            "    invalid users = {}".format(
                " ".join(acl.denied_users),
            ),
        )

    rtnval = _SmbSection(
        name=section.name,
        header_line=section.header_line,
        body_lines=preserved,
    )
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — pdbedit -L parsing / user lookup
# ----------------------------------------------------------------------


def _parse_pdbedit_output(*, text: str) -> list:
    """
        Parse ``pdbedit -L -v`` output into a list of
        :class:`UbuntuSambaUserInfo`.

        ``pdbedit -L -v`` emits a multi-line block per user.  The
        canonical block shape is::

            Unix username:        <name>
            NT username:          <name>
            Account Flags:        [U          ]
            User SID:             S-1-5-...
            Primary Group SID:    S-1-5-...
            Full Name:            <gecos>
            Home Directory:       \\\\<server>\\<user>
            HomeDir Drive:
            Logon Script:
            Profile Path:         ...
            Domain:               <DOMAIN>
            Account desc:         <desc>
            Workstations:
            Munged dial:
            Logon time:           0
            Logoff time:          ...
            Kickoff time:         ...
            Password last set:    Mon, 01 Jan 1970 ...
            Password can change:  Mon, 01 Jan 1970 ...
            Password must change: never

        Blocks are separated by blank lines.  This parser is
        defensive: missing fields fall back to ``None``.

        :param text: The captured stdout.

        :returns: A list of :class:`UbuntuSambaUserInfo`.
    """
    rtnval: list = []
    blocks = _split_pdbedit_blocks(text=text)
    for block in blocks:
        info = _parse_pdbedit_block(block=block)
        if info is None:
            continue
        rtnval.append(info)
    return rtnval


def _split_pdbedit_blocks(*, text: str) -> list:
    """
        Split ``pdbedit -v`` output into per-user blocks.

        :param text: The raw stdout.

        :returns: A ``list[list[str]]``, each inner list containing
                  the lines of one block.
    """
    rtnval: list = []
    current: list = []
    for raw in text.splitlines():
        if len(raw.strip()) == 0:
            if len(current) > 0:
                rtnval.append(current)
                current = []
            continue
        current.append(raw)
    if len(current) > 0:
        rtnval.append(current)
    return rtnval


def _parse_pdbedit_block(*, block: list) -> Optional[UbuntuSambaUserInfo]:
    """
        Parse a single ``pdbedit -v`` block into a
        :class:`UbuntuSambaUserInfo`.

        :param block: The block lines.

        :returns: The user info, or ``None`` when no Unix username
                  is present.
    """
    fields: dict = {}
    for raw in block:
        colon_index = raw.find(":")
        if colon_index == -1:
            continue
        key = raw[:colon_index].strip().lower()
        value = raw[colon_index + 1:].strip()
        fields[key] = value

    username = fields.get("unix username")
    if username is None or len(username) == 0:
        rtnval: Optional[UbuntuSambaUserInfo] = None
        return rtnval

    flags_raw = fields.get("account flags", "")
    enabled = "D" not in flags_raw

    last_password_change = fields.get("password last set")
    description = fields.get("account desc")
    if description is not None and len(description) == 0:
        description = None

    rtnval = UbuntuSambaUserInfo(
        username=username,
        enabled=enabled,
        last_password_change=last_password_change,
        description=description,
    )
    return rtnval


def _user_in_list(*, users: list, username: str) -> bool:
    """
        Case-sensitive membership probe for the
        :func:`_invoke_pdbedit_list_safe` output.

        :param users: A list of usernames.
        :param username: The username to look up.

        :returns: True when the username is present.
    """
    for candidate in users:
        if candidate == username:
            rtnval = True
            return rtnval
    rtnval = False
    return rtnval
