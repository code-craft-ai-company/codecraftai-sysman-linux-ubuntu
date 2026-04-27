"""
    Ubuntu identity-management feature.

    Implements the cross-OS identity-management contract for Ubuntu using
    ``realm`` (realmd) for FreeIPA / Active Directory enrollment, ``sssctl``
    for SSSD cache control, and ``getent`` for resolving directory-backed
    users and groups.  Every verb is dispatched through the injected
    :class:`ICommandRunner` so the manager works identically under local
    and SSH execution.

    Verb / tool mapping:

    +-----------------------------+-------------------------------------+
    | Verb                        | Ubuntu tool                         |
    +=============================+=====================================+
    | ``join_realm``              | ``realm join --user=<admin> <r>``   |
    |                             | (password via stdin)                |
    +-----------------------------+-------------------------------------+
    | ``leave_realm``             | ``realm leave <realm>``             |
    +-----------------------------+-------------------------------------+
    | ``get_realm_status``        | ``realm list`` (parse)              |
    +-----------------------------+-------------------------------------+
    | ``list_joined_realms``      | ``realm list`` (parse, multi-realm) |
    +-----------------------------+-------------------------------------+
    | ``sync_users``              | ``sssctl cache-expire -E``          |
    +-----------------------------+-------------------------------------+
    | ``resolve_user``            | ``getent passwd <username>``        |
    +-----------------------------+-------------------------------------+
    | ``resolve_group``           | ``getent group <groupname>``        |
    +-----------------------------+-------------------------------------+

    Realm-kind model:
        Ubuntu's ``realm`` tool surfaces three back-end kinds:
        ``FREEIPA`` (Red Hat Identity Management), ``ACTIVE_DIRECTORY``
        (Microsoft AD), and ``LDAP`` (generic / Kerberos-only).  The
        ``server-software`` line of ``realm list`` is mapped onto
        :class:`UbuntuRealmKind` by :func:`_classify_realm_kind`.

    File I/O policy:
        SSSD configuration on Ubuntu lives under
        ``/etc/sssd/sssd.conf`` and ``/etc/sssd/conf.d/`` but is not
        edited by this module — ``realm join`` writes it directly and
        ``sssctl cache-expire -E`` invalidates the runtime cache.  This
        module does NOT consume an :class:`IFileOperator`.

    Elevation policy:
        Every write verb (``join_realm``, ``leave_realm``,
        ``sync_users``) honours ``config.elevated``.  Reads
        (``get_realm_status``, ``list_joined_realms``, ``resolve_user``,
        ``resolve_group``) never elevate.

    Password handling:
        ``join_realm`` pipes the admin password through the runner's
        ``stdin`` channel.  ``realm join`` reads the password from
        stdin when ``--user`` is supplied without ``--no-password``,
        and the recorded ``command`` field is the argv only — the
        password never appears on argv and never lands in audit
        captures.  ``stdin`` itself is not retained on the result.
"""

import os

from dataclasses import dataclass, field
from enum import Enum
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

_REALM = "realm"
_SSSCTL = "sssctl"
_GETENT = "getent"


UbuntuIdentityActionStatus = ActionStatus


class UbuntuRealmKind(Enum):
    """
        Identity back-end kinds visible on Ubuntu through ``realm``.

        :cvar FREEIPA: FreeIPA / Red Hat Identity Management.
        :cvar ACTIVE_DIRECTORY: Microsoft Active Directory.
        :cvar LDAP: Generic LDAP / Kerberos-only.
        :cvar UNKNOWN: ``realm list`` reported an unrecognised
                       ``server-software`` value.
    """
    FREEIPA = "freeipa"
    ACTIVE_DIRECTORY = "active_directory"
    LDAP = "ldap"
    UNKNOWN = "unknown"


@dataclass
class UbuntuIdentityManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuIdentityManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param realm_executable: Path / name of the ``realm`` binary.
                                 Defaults to ``"realm"`` (PATH
                                 resolution).
        :param sssctl_executable: Path / name of the ``sssctl`` binary.
                                  Defaults to ``"sssctl"``.
        :param getent_executable: Path / name of the ``getent`` binary.
                                  Defaults to ``"getent"``.
        :param default_realm: When set, used by :meth:`leave_realm` and
                              :meth:`sync_users` when the caller passes
                              ``None``.  When ``None`` and the caller
                              also omits a realm, the verb operates on
                              every joined realm.
    """
    realm_executable: str = _REALM
    sssctl_executable: str = _SSSCTL
    getent_executable: str = _GETENT
    default_realm: Optional[str] = None


@dataclass
class UbuntuIdentityActionResult(FeatureActionResult):
    """
        Result of an identity-management write operation.

        :param realm: The realm acted upon, when relevant.
        :param username: The username acted upon, when relevant.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    realm: Optional[str] = None
    username: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuRealmJoinSpec:
    """
        Specification for joining a realm via ``realm join``.

        :param realm: The realm / domain name (e.g. ``"EXAMPLE.COM"``).
        :param admin_user: Administrator login used for the join.  The
                           value lands on argv via ``--user=<admin>``.
        :param admin_password: Administrator password.  Routed through
                               runner ``stdin``; never on argv.
        :param computer_name: Optional override for the host's computer
                              account name (``--computer-name=<name>``).
        :param ou_path: Optional Organizational Unit path for AD-style
                        realms (``--computer-ou=<ou>``).
        :param domain_controller: Optional DC override
                                  (``--membership-software=<host>`` is
                                  not appropriate; ``realm`` selects DCs
                                  via DNS SRV and ``--server`` is used
                                  to pin a specific DC).
    """
    realm: str
    admin_user: str
    admin_password: str
    computer_name: Optional[str] = None
    ou_path: Optional[str] = None
    domain_controller: Optional[str] = None


@dataclass
class UbuntuRealmInfo:
    """
        Information about a single realm the host is configured for.

        :param realm: Realm / domain name.
        :param realm_kind: Detected back-end kind.
        :param is_default: True when this realm is the default in
                           ``/etc/sssd/sssd.conf`` (parsed from the
                           ``realm list`` ``configured`` flag).
        :param domain_controller: First domain controller listed by
                                  ``realm list`` (``server-software``
                                  free-form), or ``None`` when absent.
        :param last_sync: Reserved for future enhancement; the Ubuntu
                          ``realm list`` output does not surface
                          last-sync timestamps so this field is always
                          ``None`` on Ubuntu.
    """
    realm: str
    realm_kind: UbuntuRealmKind
    is_default: bool = False
    domain_controller: Optional[str] = None
    last_sync: Optional[str] = None


@dataclass
class UbuntuRealmStatus:
    """
        Aggregate realm-join status of the host.

        :param joined: True when ``realm list`` reports at least one
                       configured realm.
        :param current_realm: First / default realm name, when joined.
        :param realm_kind: Kind of ``current_realm``, when joined.
        :param computer_name: Host's computer account name as reported
                              by ``realm list`` (``computer-name``
                              line), or ``None`` when absent.
    """
    joined: bool
    current_realm: Optional[str] = None
    realm_kind: Optional[UbuntuRealmKind] = None
    computer_name: Optional[str] = None


@dataclass
class UbuntuUserResolveResult:
    """
        Result of resolving a user against the local + directory NSS.

        :param username: The supplied username.
        :param uid: Numeric uid; ``None`` when unresolvable.
        :param gid: Primary numeric gid; ``None`` when unresolvable.
        :param home: Home directory; ``None`` when unresolvable.
        :param shell: Login shell; ``None`` when unresolvable.
        :param realm: Realm the user comes from, or ``None`` for local.
                      Detected by inspecting an ``@`` suffix in the
                      first ``getent passwd`` field; otherwise left
                      ``None``.
        :param groups: Reserved.  ``getent passwd`` does not enumerate
                       supplementary groups; this list is always empty
                       on Ubuntu.  Callers needing supplementary group
                       listings should call :meth:`resolve_group` with
                       known group names.
        :param found: True when ``getent passwd`` returned a record.
    """
    username: str
    uid: Optional[int] = None
    gid: Optional[int] = None
    home: Optional[str] = None
    shell: Optional[str] = None
    realm: Optional[str] = None
    groups: list[str] = field(default_factory=list)
    found: bool = False


@dataclass
class UbuntuGroupResolveResult:
    """
        Result of resolving a group against the local + directory NSS.

        :param groupname: The supplied groupname.
        :param gid: Numeric gid; ``None`` when unresolvable.
        :param members: Member usernames listed in
                        ``getent group``'s fourth colon-separated field.
        :param realm: Realm the group comes from, or ``None`` for local.
        :param found: True when ``getent group`` returned a record.
    """
    groupname: str
    gid: Optional[int] = None
    members: list[str] = field(default_factory=list)
    realm: Optional[str] = None
    found: bool = False


class UbuntuIdentityQueryError(Exception):
    """
        Raised when an identity-management read fails for a reason
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
        self.command: list[str] = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Identity-management query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# UbuntuIdentityManager
# ----------------------------------------------------------------------


class UbuntuIdentityManager(BaseFeatureManager):
    """
        Ubuntu identity / realm manager.  Every verb is dispatched
        through the injected :class:`ICommandRunner`.  Reads use
        ``realm list`` and ``getent``; writes use ``realm join``,
        ``realm leave``, and ``sssctl cache-expire``.
    """

    result_class = UbuntuIdentityActionResult
    status_class = UbuntuIdentityActionStatus
    feature_name = "identity-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def join_realm(
        self,
        spec: UbuntuRealmJoinSpec,
    ) -> UbuntuIdentityActionResult:
        """
            Join the host to ``spec.realm`` via ``realm join``.

            The admin password is delivered through the runner's
            ``stdin`` channel; argv carries only the realm, the
            ``--user`` flag, and any optional placement flags.
            Idempotent: when ``realm list`` already reports the host
            joined to ``spec.realm``, returns ``SKIPPED`` without
            invoking ``realm join``.

            :param spec: The :class:`UbuntuRealmJoinSpec` describing
                         the join.

            :returns: The :class:`UbuntuIdentityActionResult`.  The
                      ``command`` field is recorded as the argv only;
                      the password never lands on argv.
        """
        command = self._build_join_argv(spec=spec)

        if self._config.dry_run:
            dry = self._build_realm_result(
                status=UbuntuIdentityActionStatus.DRY_RUN,
                realm=spec.realm,
                command=command,
            )
            return dry

        joined = self._is_realm_joined(realm=spec.realm)
        if joined:
            skipped = self._build_realm_result(
                status=UbuntuIdentityActionStatus.SKIPPED,
                realm=spec.realm,
                command=command,
                message="host already joined to realm",
            )
            return skipped

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
            stdin=spec.admin_password,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuIdentityActionStatus.SUCCESS
        else:
            mapped_status = UbuntuIdentityActionStatus.FAILED

        rtnval = UbuntuIdentityActionResult(
            status=mapped_status,
            detail=spec.realm,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            realm=spec.realm,
            username=spec.admin_user,
        )
        return rtnval

    def leave_realm(
        self,
        realm: Optional[str] = None,
    ) -> UbuntuIdentityActionResult:
        """
            Leave the named realm via ``realm leave <realm>``.

            Idempotent: when the host is not joined to ``realm``,
            returns ``SKIPPED`` without invoking ``realm leave``.

            :param realm: The realm to leave.  When ``None``, the
                          configured ``default_realm`` is used; when
                          both are ``None``, ``realm leave`` is invoked
                          with no positional argument so realmd defaults
                          to its primary realm.

            :returns: The :class:`UbuntuIdentityActionResult`.
        """
        target_realm = self._resolve_realm(realm=realm)
        command = self._build_leave_argv(realm=target_realm)

        if self._config.dry_run:
            dry = self._build_realm_result(
                status=UbuntuIdentityActionStatus.DRY_RUN,
                realm=target_realm,
                command=command,
            )
            return dry

        if target_realm is not None:
            joined = self._is_realm_joined(realm=target_realm)
            if not joined:
                skipped = self._build_realm_result(
                    status=UbuntuIdentityActionStatus.SKIPPED,
                    realm=target_realm,
                    command=command,
                    message="host is not joined to realm",
                )
                return skipped

        rtnval = self._dispatch_realm_write(
            command=command,
            realm=target_realm,
        )
        return rtnval

    def sync_users(
        self,
    ) -> UbuntuIdentityActionResult:
        """
            Invalidate the SSSD user / group cache so the next NSS
            lookup refreshes from the directory.  Wraps
            ``sssctl cache-expire -E``.

            Not idempotent — every call invokes ``sssctl``.

            :returns: The :class:`UbuntuIdentityActionResult`.
        """
        sssctl_executable = self._typed_config().sssctl_executable
        command = [sssctl_executable, "cache-expire", "-E"]

        if self._config.dry_run:
            dry = self._build_realm_result(
                status=UbuntuIdentityActionStatus.DRY_RUN,
                realm=None,
                command=command,
            )
            return dry

        rtnval = self._dispatch_realm_write(
            command=command,
            realm=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_realm_status(self) -> UbuntuRealmStatus:
        """
            Return the host's current realm-join status.

            Runs ``realm list`` and parses the multi-stanza output.
            Reads do not elevate.

            :returns: The :class:`UbuntuRealmStatus`.  When the host is
                      not joined, ``joined=False`` and every other
                      field is ``None``.
            :raises UbuntuIdentityQueryError: When ``realm list`` exits
                                              with a non-zero, non-"no
                                              realms configured" code.
        """
        realms = self.list_joined_realms()
        if len(realms) == 0:
            rtnval = UbuntuRealmStatus(joined=False)
            return rtnval

        first = realms[0]
        computer_name = self._extract_computer_name(realm=first.realm)
        rtnval = UbuntuRealmStatus(
            joined=True,
            current_realm=first.realm,
            realm_kind=first.realm_kind,
            computer_name=computer_name,
        )
        return rtnval

    def list_joined_realms(self) -> list[UbuntuRealmInfo]:
        """
            Enumerate every realm the host is configured for.

            Runs ``realm list`` and parses the stanza-per-realm output.
            Reads do not elevate.  Empty stdout (the "no realms
            configured" case) returns an empty list rather than
            raising.

            :returns: Zero or more :class:`UbuntuRealmInfo` records,
                      preserving ``realm list`` ordering.
            :raises UbuntuIdentityQueryError: When ``realm list`` exits
                                              non-zero with a
                                              non-empty stderr.
        """
        realm_executable = self._typed_config().realm_executable
        command = [realm_executable, "list"]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            # ``realm list`` exits 0 with no output when no realm is
            # configured.  Any other non-zero is a query failure.
            raise UbuntuIdentityQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_realm_list_output(output=run_result.stdout)
        return rtnval

    def resolve_user(
        self,
        username: str,
    ) -> UbuntuUserResolveResult:
        """
            Resolve a username through the local + directory NSS via
            ``getent passwd <username>``.  Reads do not elevate.

            :param username: The username to resolve.

            :returns: The :class:`UbuntuUserResolveResult`.  When the
                      user does not exist, ``found=False`` and every
                      other field carries its default.
        """
        getent_executable = self._typed_config().getent_executable
        command = [getent_executable, "passwd", username]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval = UbuntuUserResolveResult(
                username=username,
                found=False,
            )
            return rtnval

        parsed = _parse_getent_passwd_line(line=run_result.stdout)
        if parsed is None:
            rtnval = UbuntuUserResolveResult(
                username=username,
                found=False,
            )
            return rtnval

        realm_value = _extract_realm_from_principal(
            principal=parsed["name"],
        )
        rtnval = UbuntuUserResolveResult(
            username=username,
            uid=parsed["uid"],
            gid=parsed["gid"],
            home=parsed["home"],
            shell=parsed["shell"],
            realm=realm_value,
            groups=[],
            found=True,
        )
        return rtnval

    def resolve_group(
        self,
        groupname: str,
    ) -> UbuntuGroupResolveResult:
        """
            Resolve a groupname through the local + directory NSS via
            ``getent group <groupname>``.  Reads do not elevate.

            :param groupname: The groupname to resolve.

            :returns: The :class:`UbuntuGroupResolveResult`.  When the
                      group does not exist, ``found=False`` and every
                      other field carries its default.
        """
        getent_executable = self._typed_config().getent_executable
        command = [getent_executable, "group", groupname]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval = UbuntuGroupResolveResult(
                groupname=groupname,
                found=False,
            )
            return rtnval

        parsed = _parse_getent_group_line(line=run_result.stdout)
        if parsed is None:
            rtnval = UbuntuGroupResolveResult(
                groupname=groupname,
                found=False,
            )
            return rtnval

        realm_value = _extract_realm_from_principal(
            principal=parsed["name"],
        )
        rtnval = UbuntuGroupResolveResult(
            groupname=groupname,
            gid=parsed["gid"],
            members=parsed["members"],
            realm=realm_value,
            found=True,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — argv builders
    # ------------------------------------------------------------------

    def _build_join_argv(
        self,
        *,
        spec: UbuntuRealmJoinSpec,
    ) -> list[str]:
        """
            Build the ``realm join`` argv from a
            :class:`UbuntuRealmJoinSpec`.  The admin password is NOT
            placed on argv — it rides on stdin.

            :param spec: The :class:`UbuntuRealmJoinSpec`.

            :returns: A new list with the full argv.
        """
        realm_executable = self._typed_config().realm_executable
        command: list[str] = [realm_executable, "join"]
        command.append("--user={}".format(spec.admin_user))

        if spec.computer_name is not None:
            command.append("--computer-name={}".format(spec.computer_name))

        if spec.ou_path is not None:
            command.append("--computer-ou={}".format(spec.ou_path))

        if spec.domain_controller is not None:
            command.append("--server={}".format(spec.domain_controller))

        command.append(spec.realm)
        return command

    def _build_leave_argv(
        self,
        *,
        realm: Optional[str],
    ) -> list[str]:
        """
            Build the ``realm leave`` argv.

            :param realm: The realm to leave; when ``None`` the argv
                          is just ``["realm", "leave"]`` and realmd
                          falls back to the host's primary realm.

            :returns: A new list with the full argv.
        """
        realm_executable = self._typed_config().realm_executable
        command: list[str] = [realm_executable, "leave"]
        if realm is not None:
            command.append(realm)
        return command

    # ------------------------------------------------------------------
    # Internal helpers — dispatch + result construction
    # ------------------------------------------------------------------

    def _typed_config(self) -> UbuntuIdentityManagerConfig:
        """
            Return ``self._config`` typed as
            :class:`UbuntuIdentityManagerConfig`, falling back to a
            synthesised default when the caller passed a plain
            :class:`FeatureConfig`.

            :returns: The typed config.
        """
        if isinstance(self._config, UbuntuIdentityManagerConfig):
            rtnval = self._config
        else:
            rtnval = UbuntuIdentityManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _resolve_realm(
        self,
        *,
        realm: Optional[str],
    ) -> Optional[str]:
        """
            Pick the realm to act on, falling back to
            ``config.default_realm`` when the caller passes ``None``.

            :param realm: The caller's realm, possibly ``None``.

            :returns: The resolved realm, or ``None`` when neither the
                      caller nor the config provided one.
        """
        if realm is not None:
            rtnval: Optional[str] = realm
            return rtnval
        rtnval = self._typed_config().default_realm
        return rtnval

    def _dispatch_realm_write(
        self,
        *,
        command: list[str],
        realm: Optional[str],
    ) -> UbuntuIdentityActionResult:
        """
            Dispatch a write command and wrap its outcome.

            :param command: The full argv.
            :param realm: The realm acted upon, when relevant.

            :returns: The :class:`UbuntuIdentityActionResult`.
        """
        rtnval = self._dispatch(
            command=command,
            detail=realm,
            operation="identity_write",
            realm=realm,
        )
        return rtnval

    def _build_realm_result(
        self,
        *,
        status: UbuntuIdentityActionStatus,
        realm: Optional[str],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuIdentityActionResult:
        """
            Construct an :class:`UbuntuIdentityActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param realm: The realm acted upon, when relevant.
            :param command: The argv to record.
            :param message: Optional human-readable detail.
            :param stderr: Optional captured stderr.

            :returns: The :class:`UbuntuIdentityActionResult`.
        """
        rtnval = UbuntuIdentityActionResult(
            status=status,
            detail=realm,
            command=list(command),
            realm=realm,
            message=message,
            stderr=stderr,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — read probes
    # ------------------------------------------------------------------

    def _is_realm_joined(
        self,
        *,
        realm: str,
    ) -> bool:
        """
            Probe ``realm list`` and look for ``realm`` in the parsed
            stanzas.

            :param realm: The realm to look for.

            :returns: True when the host is joined to ``realm``;
                      False otherwise.  Query errors collapse to False
                      so the caller's join attempt proceeds normally.
        """
        try:
            joined_realms = self.list_joined_realms()
        except UbuntuIdentityQueryError:
            rtnval = False
            return rtnval

        for info in joined_realms:
            if info.realm == realm:
                rtnval = True
                return rtnval
            # ``realm`` may match case-insensitively against the parsed
            # ``domain-name`` line; preserve the canonical compare.
            if info.realm.lower() == realm.lower():
                rtnval = True
                return rtnval

        rtnval = False
        return rtnval

    def _extract_computer_name(
        self,
        *,
        realm: str,
    ) -> Optional[str]:
        """
            Pull the ``computer-name`` line for ``realm`` from
            ``realm list``.

            :param realm: The realm of interest.

            :returns: The computer name, or ``None`` when absent.
        """
        realm_executable = self._typed_config().realm_executable
        command = [realm_executable, "list"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval

        rtnval = _extract_computer_name_for_realm(
            output=run_result.stdout,
            realm=realm,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers
# ----------------------------------------------------------------------


def _parse_realm_list_output(*, output: str) -> list[UbuntuRealmInfo]:
    """
        Parse the multi-stanza output of ``realm list``.

        ``realm list`` emits one stanza per configured realm.  The
        first non-indented line of each stanza is the realm / domain
        name; subsequent indented ``key: value`` lines describe the
        realm.  Stanzas are separated by blank lines.

        :param output: Raw stdout text.

        :returns: A list of :class:`UbuntuRealmInfo` records, one per
                  parsed stanza.
    """
    rtnval: list[UbuntuRealmInfo] = []
    current_name: Optional[str] = None
    current_attributes: dict[str, str] = {}

    for raw_line in output.splitlines():
        stripped = raw_line.rstrip("\r\n")
        if len(stripped.strip()) == 0:
            if current_name is not None:
                info = _build_realm_info(
                    realm=current_name,
                    attributes=current_attributes,
                )
                rtnval.append(info)
                current_name = None
                current_attributes = {}
            continue

        # Stanza header lines start at column 0; attribute lines are
        # indented.
        first_char = stripped[0]
        if first_char != " " and first_char != "\t":
            if current_name is not None:
                info = _build_realm_info(
                    realm=current_name,
                    attributes=current_attributes,
                )
                rtnval.append(info)
            current_name = stripped.strip()
            current_attributes = {}
            continue

        attribute_line = stripped.strip()
        if ":" not in attribute_line:
            continue
        key, _, value = attribute_line.partition(":")
        cleaned_key = key.strip()
        cleaned_value = value.strip()
        if len(cleaned_key) == 0:
            continue
        current_attributes[cleaned_key] = cleaned_value

    # Flush a trailing stanza with no terminating blank line.
    if current_name is not None:
        info = _build_realm_info(
            realm=current_name,
            attributes=current_attributes,
        )
        rtnval.append(info)

    return rtnval


def _build_realm_info(
    *,
    realm: str,
    attributes: dict,
) -> UbuntuRealmInfo:
    """
        Materialise a :class:`UbuntuRealmInfo` from a parsed stanza.

        :param realm: The realm name.
        :param attributes: The parsed key/value attribute map.

        :returns: The :class:`UbuntuRealmInfo`.
    """
    server_software = attributes.get("server-software", "").strip()
    realm_kind = _classify_realm_kind(server_software=server_software)

    configured_value = attributes.get("configured", "").strip()
    is_default = configured_value == "kerberos-member"

    rtnval = UbuntuRealmInfo(
        realm=realm,
        realm_kind=realm_kind,
        is_default=is_default,
        domain_controller=None,
        last_sync=None,
    )
    return rtnval


def _classify_realm_kind(*, server_software: str) -> UbuntuRealmKind:
    """
        Map ``realm list``'s ``server-software`` value onto a
        :class:`UbuntuRealmKind`.

        :param server_software: The verbatim ``server-software`` field.

        :returns: The mapped kind; falls back to
                  :attr:`UbuntuRealmKind.UNKNOWN` for unrecognised
                  values.
    """
    lowered = server_software.lower()
    if lowered == "ipa":
        rtnval = UbuntuRealmKind.FREEIPA
        return rtnval
    if lowered == "active-directory":
        rtnval = UbuntuRealmKind.ACTIVE_DIRECTORY
        return rtnval
    if lowered in ("ldap", "krb5"):
        rtnval = UbuntuRealmKind.LDAP
        return rtnval
    rtnval = UbuntuRealmKind.UNKNOWN
    return rtnval


def _extract_computer_name_for_realm(
    *,
    output: str,
    realm: str,
) -> Optional[str]:
    """
        Walk the parsed ``realm list`` stanzas and return the
        ``computer-name`` value for ``realm``.

        :param output: Raw stdout text.
        :param realm: The realm of interest.

        :returns: The computer name, or ``None`` when absent.
    """
    current_name: Optional[str] = None
    rtnval: Optional[str] = None

    for raw_line in output.splitlines():
        stripped = raw_line.rstrip("\r\n")
        if len(stripped.strip()) == 0:
            current_name = None
            continue

        first_char = stripped[0]
        if first_char != " " and first_char != "\t":
            current_name = stripped.strip()
            continue

        if current_name is None:
            continue
        if current_name != realm and current_name.lower() != realm.lower():
            continue

        attribute_line = stripped.strip()
        if not attribute_line.startswith("computer-name"):
            continue
        if ":" not in attribute_line:
            continue
        _, _, value = attribute_line.partition(":")
        cleaned = value.strip()
        if len(cleaned) > 0:
            rtnval = cleaned
            return rtnval

    return rtnval


def _parse_getent_passwd_line(*, line: str) -> Optional[dict]:
    """
        Parse a single ``getent passwd`` record into a dict.

        Expected colon-separated form:
            ``<name>:<x>:<uid>:<gid>:<comment>:<home>:<shell>``.

        :param line: A raw line of stdout.  May contain trailing
                     newline; only the first non-empty line is parsed.

        :returns: The parsed dict, or ``None`` when the line cannot be
                  parsed.
    """
    rtnval: Optional[dict] = None
    for raw in line.splitlines():
        stripped = raw.rstrip("\n").rstrip("\r")
        if len(stripped) == 0:
            continue

        fields = stripped.split(":")
        if len(fields) < 7:
            continue

        try:
            uid_value = int(fields[2])
        except ValueError:
            continue

        try:
            gid_value = int(fields[3])
        except ValueError:
            continue

        rtnval = {
            "name": fields[0],
            "uid": uid_value,
            "gid": gid_value,
            "comment": fields[4],
            "home": fields[5],
            "shell": fields[6],
        }
        break

    return rtnval


def _parse_getent_group_line(*, line: str) -> Optional[dict]:
    """
        Parse a single ``getent group`` record into a dict.

        Expected colon-separated form:
            ``<name>:<x>:<gid>:<member1>,<member2>,...``.

        :param line: A raw line of stdout.

        :returns: The parsed dict, or ``None`` when the line cannot be
                  parsed.
    """
    rtnval: Optional[dict] = None
    for raw in line.splitlines():
        stripped = raw.rstrip("\n").rstrip("\r")
        if len(stripped) == 0:
            continue

        fields = stripped.split(":")
        if len(fields) < 4:
            continue

        try:
            gid_value = int(fields[2])
        except ValueError:
            continue

        member_field = fields[3]
        members: list[str] = []
        if len(member_field) > 0:
            for token in member_field.split(","):
                cleaned = token.strip()
                if len(cleaned) > 0:
                    members.append(cleaned)

        rtnval = {
            "name": fields[0],
            "gid": gid_value,
            "members": members,
        }
        break

    return rtnval


def _extract_realm_from_principal(*, principal: str) -> Optional[str]:
    """
        Pull a realm suffix off a principal name when present.

        Directory-backed records produced by SSSD frequently look like
        ``alice@EXAMPLE.COM`` in the first ``getent passwd`` field.
        Local accounts have no realm suffix.

        :param principal: The first colon-separated field.

        :returns: The realm, or ``None`` when no ``@`` separator is
                  present.
    """
    at_position = principal.rfind("@")
    if at_position < 0:
        rtnval: Optional[str] = None
        return rtnval

    realm_value = principal[at_position + 1:].strip()
    if len(realm_value) == 0:
        rtnval = None
        return rtnval

    rtnval = realm_value
    return rtnval
