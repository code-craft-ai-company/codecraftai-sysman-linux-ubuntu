"""
    Ubuntu ACL-management feature.

    Implements the cross-OS ACL-management contract for Ubuntu using
    POSIX ACL tooling: ``getfacl`` for reads and ``setfacl`` for
    writes, both dispatched through the injected
    :class:`ICommandRunner`.  This module never imports
    ``subprocess`` and never touches the filesystem directly.

    Verb catalogue:
        * ``get_acl(path)`` -> ``getfacl --omit-header <path>`` parsed
          into a :class:`UbuntuAclInfo`.
        * ``set_acl(path, acl_spec)`` -> ``setfacl -b <path>`` followed
          by ``setfacl -m <entries> <path>`` (replaces full ACL).
        * ``add_ace(path, principal, rights)`` -> ``setfacl -m
          <principal-type>:<principal>:<rights> <path>``.
        * ``remove_ace(path, principal)`` -> ``setfacl -x
          <principal-type>:<principal> <path>``.
        * ``clear_acl(path)`` -> ``setfacl -b <path>`` (removes all
          extended ACLs, leaving base owner/group/other entries).
        * ``copy_acl(src, dst)`` -> ``getfacl <src>`` piped to
          ``setfacl --set-file=- <dst>`` via the runner's ``stdin``
          channel.
        * ``list_aces(path)`` -> parses the ``getfacl`` output into a
          flat ``list[UbuntuAceInfo]`` covering both regular and
          default ACEs.

    Elevation policy:
        Every write verb (``set_acl``, ``add_ace``, ``remove_ace``,
        ``clear_acl``, ``copy_acl``) honours ``config.elevated``.  Reads
        (``get_acl``, ``list_aces``, every idempotency probe) never
        elevate.

    Idempotency:
        * ``set_acl`` reads the current ACL; when the parsed entries
          (regular and default) are byte-equal to the requested set,
          the manager returns ``SKIPPED``.
        * ``add_ace`` reads the current ACL; when an ACE for the same
          principal-type / principal / rights / default flag is
          already present, the manager returns ``SKIPPED``.
        * ``remove_ace`` reads the current ACL; when no ACE for the
          target principal is present, the manager returns
          ``SKIPPED``.
        * ``clear_acl`` reads the current ACL; when no extended ACEs
          (i.e. no named-user / named-group / non-base mask / default
          ACEs) are present, the manager returns ``SKIPPED``.
        * ``copy_acl`` reads both source and destination ACLs; when
          they are byte-equal, the manager returns ``SKIPPED``.
"""

import os

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_GETFACL = "getfacl"
_SETFACL = "setfacl"

# ``setfacl`` flags.  ``-b`` removes all extended ACL entries while
# preserving the standard owner/group/other entries; ``-m`` modifies
# (or creates) ACL entries; ``-x`` removes named entries; ``--set-file``
# replaces the entire ACL from a file (we use ``-`` for stdin).
_SETFACL_REMOVE_ALL = "-b"
_SETFACL_MODIFY = "-m"
_SETFACL_REMOVE = "-x"
_SETFACL_SET_FILE = "--set-file=-"


UbuntuAclActionStatus = ActionStatus


class UbuntuAcePrincipalType(Enum):
    """
        POSIX ACL principal-type tags.  These map directly onto the
        single-letter prefix that ``setfacl`` and ``getfacl`` emit:
        ``user`` -> ``u``, ``group`` -> ``g``, ``other`` -> ``o``,
        ``mask`` -> ``m``.
    """
    USER = "user"
    GROUP = "group"
    OTHER = "other"
    MASK = "mask"


@dataclass
class UbuntuAclManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuAclManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param getfacl_path: Path / name of the ``getfacl`` binary.
                             Defaults to ``"getfacl"`` (PATH-resolved).
                             Tests can override to inject a wrapper or a
                             sibling build.
        :param setfacl_path: Path / name of the ``setfacl`` binary.
                             Defaults to ``"setfacl"``.
    """
    getfacl_path: str = _GETFACL
    setfacl_path: str = _SETFACL


@dataclass
class UbuntuAclActionResult(FeatureActionResult):
    """
        Result of an ACL-management write operation.

        :param path: The target path, when applicable.  ``None`` for
                     verbs whose write target is implicit (none in this
                     module).
        :param principal: The principal acted upon, when applicable.
                          For ``set_acl`` and ``copy_acl`` this is
                          ``None`` because the verb spans many
                          principals.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    path: Optional[str] = None
    principal: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuAceInfo:
    """
        A single parsed POSIX ACL entry.

        :param principal_type: One of :class:`UbuntuAcePrincipalType`.
        :param principal: The user or group name when
                          ``principal_type`` is ``USER`` or ``GROUP``.
                          ``None`` for ``OTHER`` and ``MASK`` (and for
                          base ``USER`` / ``GROUP`` rows where
                          ``getfacl`` emits no name — the row in that
                          case represents the file's owner / group).
        :param rights: POSIX-style rights token (e.g. ``"rwx"``,
                       ``"r-x"``, ``"---"``).  Always three characters.
        :param default: ``True`` when this entry is a default ACL
                        (directory-only inheritance prototype).
    """
    principal_type: UbuntuAcePrincipalType
    principal: Optional[str]
    rights: str
    default: bool = False


@dataclass
class UbuntuAceSpec:
    """
        Caller-supplied ACE shape used by :meth:`UbuntuAclManager.set_acl`
        and :meth:`UbuntuAclManager.add_ace` when the caller wants to
        package multiple fields up.

        :param principal_type: One of :class:`UbuntuAcePrincipalType`.
        :param principal: The user or group name.  ``None`` for
                          ``OTHER`` and ``MASK``.
        :param rights: POSIX-style rights token (``"rwx"``, ``"r-x"``,
                       ``"---"``).
        :param default: ``True`` when this entry should be added as a
                        default ACL (directory-only inheritance).
    """
    principal_type: UbuntuAcePrincipalType
    principal: Optional[str]
    rights: str
    default: bool = False


@dataclass
class UbuntuAclInfo:
    """
        Snapshot of a path's POSIX ACL.

        :param path: The path the ACL was queried for.
        :param owner: Owner principal name (``# owner:`` from the
                      ``getfacl`` header).  ``None`` when not probed.
        :param group: Primary group principal name (``# group:`` from
                      the ``getfacl`` header).  ``None`` when not
                      probed.
        :param aces: Non-default ACEs in source order.
        :param default_aces: Default ACEs in source order.  Empty for
                             non-directory paths.
    """
    path: str
    owner: Optional[str] = None
    group: Optional[str] = None
    aces: list = field(default_factory=list)
    default_aces: list = field(default_factory=list)


class UbuntuAclQueryError(Exception):
    """
        Raised when an ACL read fails for a reason other than the
        target simply being absent (callers asking for an absent path
        get the underlying ``getfacl`` failure surfaced verbatim — we
        do NOT try to translate that into ``None``).
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

        err_msg_lines = ["ACL query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuAclManager(BaseFeatureManager):
    """
        Ubuntu ACL manager.  Every verb is dispatched through the
        injected :class:`ICommandRunner`; the manager never imports
        ``subprocess`` and never touches the filesystem directly.
    """

    result_class = UbuntuAclActionResult
    status_class = UbuntuAclActionStatus
    feature_name = "acl-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_acl(self, path: str) -> UbuntuAclInfo:
        """
            Return a :class:`UbuntuAclInfo` snapshot of ``path``'s
            current ACL.

            Runs ``getfacl <path>`` (without ``--omit-header``) so the
            owner / group fields can be populated from the leading
            comment lines.

            :param path: The absolute path to query.

            :returns: The parsed :class:`UbuntuAclInfo`.

            :raises: :class:`UbuntuAclQueryError`
        """
        command = [self._getfacl_path(), path]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise UbuntuAclQueryError(
                command=list(command),
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_getfacl_output(
            output=run_result.stdout,
            path=path,
        )
        return rtnval

    def list_aces(self, path: str) -> list:
        """
            Return every ACE attached to ``path`` as a flat list in the
            order ``getfacl`` emitted them.  Includes both regular and
            default ACEs.

            :param path: The absolute path to query.

            :returns: A ``list[UbuntuAceInfo]``.

            :raises: :class:`UbuntuAclQueryError`
        """
        info = self.get_acl(path=path)

        rtnval: list = []
        for ace in info.aces:
            rtnval.append(ace)
        for ace in info.default_aces:
            rtnval.append(ace)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_acl(
        self,
        path: str,
        acl_spec: list,
    ) -> UbuntuAclActionResult:
        """
            Replace the full ACL on ``path`` with ``acl_spec``.

            Implemented as ``setfacl -b <path>`` followed by ``setfacl
            -m <comma-joined entries> <path>``.  Skipped when the
            current parsed ACL is already byte-equal to ``acl_spec``.

            :param path: The target path.
            :param acl_spec: A list of :class:`UbuntuAceSpec`.

            :returns: The :class:`UbuntuAclActionResult`.
        """
        rendered_entries = _render_setfacl_entries(spec=acl_spec)
        command = [
            self._setfacl_path(),
            _SETFACL_MODIFY,
            rendered_entries,
            path,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuAclActionStatus.DRY_RUN,
                command=command,
                path=path,
            )
            return dry

        try:
            current = self.get_acl(path=path)
        except UbuntuAclQueryError:
            current = None

        if current is not None and _acl_matches_spec(
            current=current,
            desired=acl_spec,
        ):
            skipped = self._build_result(
                status=UbuntuAclActionStatus.SKIPPED,
                command=command,
                path=path,
                message="path ACL already matches the requested spec",
            )
            return skipped

        clear_command = [
            self._setfacl_path(),
            _SETFACL_REMOVE_ALL,
            path,
        ]
        clear_result = self._runner.run(
            list(clear_command),
            elevated=self._config.elevated,
        )
        if clear_result.exit_code != 0:
            failed = UbuntuAclActionResult(
                status=UbuntuAclActionStatus.FAILED,
                detail=path,
                exit_code=clear_result.exit_code,
                stdout=clear_result.stdout,
                stderr=clear_result.stderr,
                command=list(clear_command),
                path=path,
                message="setfacl -b failed before set",
            )
            return failed

        rtnval = self._dispatch_acl_write(
            command=command,
            path=path,
        )
        return rtnval

    def add_ace(
        self,
        path: str,
        principal: UbuntuAceSpec,
        rights: Optional[str] = None,
    ) -> UbuntuAclActionResult:
        """
            Add a single ACE to ``path``.

            Two calling shapes are accepted for callers' convenience:

            * Pass a fully-populated :class:`UbuntuAceSpec` as
              ``principal``.  ``rights`` MUST be ``None`` in that case.
            * Pass a :class:`UbuntuAceSpec` as ``principal`` whose
              ``rights`` is the empty string and a ``rights`` argument.
              The argument overrides the spec's ``rights`` field.

            Skipped when an ACE matching ``principal_type`` /
            ``principal`` / ``rights`` / ``default`` is already
            present.

            :param path: The target path.
            :param principal: The :class:`UbuntuAceSpec` to add.
            :param rights: Optional override for ``principal.rights``.

            :returns: The :class:`UbuntuAclActionResult`.
        """
        effective_spec = _materialise_ace_spec(
            principal=principal,
            rights_override=rights,
        )
        rendered = _render_single_setfacl_entry(spec=effective_spec)

        command = [
            self._setfacl_path(),
            _SETFACL_MODIFY,
            rendered,
            path,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuAclActionStatus.DRY_RUN,
                command=command,
                path=path,
                principal=effective_spec.principal,
            )
            return dry

        try:
            current = self.get_acl(path=path)
        except UbuntuAclQueryError:
            current = None

        if current is not None and _ace_already_present(
            current=current,
            spec=effective_spec,
        ):
            skipped = self._build_result(
                status=UbuntuAclActionStatus.SKIPPED,
                command=command,
                path=path,
                principal=effective_spec.principal,
                message="ACE is already present on the target path",
            )
            return skipped

        rtnval = self._dispatch_acl_write(
            command=command,
            path=path,
            principal=effective_spec.principal,
        )
        return rtnval

    def remove_ace(
        self,
        path: str,
        principal: UbuntuAceSpec,
    ) -> UbuntuAclActionResult:
        """
            Remove all ACEs matching ``principal_type`` and
            ``principal`` from ``path``.

            Skipped when no matching ACE is currently present.

            :param path: The target path.
            :param principal: The :class:`UbuntuAceSpec`.  Only
                              ``principal_type``, ``principal``, and
                              ``default`` are consulted; ``rights`` is
                              ignored because ``setfacl -x`` accepts
                              only the type / name pair.

            :returns: The :class:`UbuntuAclActionResult`.
        """
        rendered = _render_setfacl_remove_token(spec=principal)

        command = [
            self._setfacl_path(),
            _SETFACL_REMOVE,
            rendered,
            path,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuAclActionStatus.DRY_RUN,
                command=command,
                path=path,
                principal=principal.principal,
            )
            return dry

        try:
            current = self.get_acl(path=path)
        except UbuntuAclQueryError:
            current = None

        if current is not None and not _ace_principal_present(
            current=current,
            spec=principal,
        ):
            skipped = self._build_result(
                status=UbuntuAclActionStatus.SKIPPED,
                command=command,
                path=path,
                principal=principal.principal,
                message="no matching ACE to remove",
            )
            return skipped

        rtnval = self._dispatch_acl_write(
            command=command,
            path=path,
            principal=principal.principal,
        )
        return rtnval

    def clear_acl(self, path: str) -> UbuntuAclActionResult:
        """
            Remove every extended (non-base) ACE from ``path``.

            Skipped when the parsed ACL contains no extended entries
            (no named-user / named-group rows, no non-default mask, no
            default ACEs).

            :param path: The target path.

            :returns: The :class:`UbuntuAclActionResult`.
        """
        command = [
            self._setfacl_path(),
            _SETFACL_REMOVE_ALL,
            path,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuAclActionStatus.DRY_RUN,
                command=command,
                path=path,
            )
            return dry

        try:
            current = self.get_acl(path=path)
        except UbuntuAclQueryError:
            current = None

        if current is not None and not _has_extended_aces(info=current):
            skipped = self._build_result(
                status=UbuntuAclActionStatus.SKIPPED,
                command=command,
                path=path,
                message="path has no extended ACL entries to clear",
            )
            return skipped

        rtnval = self._dispatch_acl_write(
            command=command,
            path=path,
        )
        return rtnval

    def copy_acl(
        self,
        src: str,
        dst: str,
    ) -> UbuntuAclActionResult:
        """
            Copy the ACL from ``src`` to ``dst``.

            Implemented as a single pipeline through the runner: a
            ``getfacl <src>`` read followed by ``setfacl --set-file=-
            <dst>`` whose stdin is the captured ``getfacl`` output.
            Skipped when the destination ACL is already byte-equal to
            the source ACL (compared on the parsed ACE lists).

            :param src: Source path to copy from.
            :param dst: Destination path to copy onto.

            :returns: The :class:`UbuntuAclActionResult`.
        """
        write_command = [
            self._setfacl_path(),
            _SETFACL_SET_FILE,
            dst,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuAclActionStatus.DRY_RUN,
                command=write_command,
                path=dst,
            )
            return dry

        # Read source ACL first.  Failure is fatal -- we cannot copy a
        # source we cannot read.
        read_command = [self._getfacl_path(), src]
        read_result = self._runner.run(
            list(read_command),
            elevated=False,
        )
        if read_result.exit_code != 0:
            failed = UbuntuAclActionResult(
                status=UbuntuAclActionStatus.FAILED,
                detail=src,
                exit_code=read_result.exit_code,
                stdout=read_result.stdout,
                stderr=read_result.stderr,
                command=list(read_command),
                path=src,
                message="getfacl on source path failed",
            )
            return failed

        try:
            src_info = _parse_getfacl_output(
                output=read_result.stdout,
                path=src,
            )
        except UbuntuAclQueryError:
            src_info = None

        try:
            dst_info = self.get_acl(path=dst)
        except UbuntuAclQueryError:
            dst_info = None

        if (
            src_info is not None
            and dst_info is not None
            and _acl_infos_equal(left=src_info, right=dst_info)
        ):
            skipped = self._build_result(
                status=UbuntuAclActionStatus.SKIPPED,
                command=write_command,
                path=dst,
                message="destination ACL already matches source ACL",
            )
            return skipped

        rtnval = self._dispatch_acl_write(
            command=write_command,
            path=dst,
            stdin=read_result.stdout,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _getfacl_path(self) -> str:
        """
            Return the configured ``getfacl`` path.
        """
        config = self._config
        if isinstance(config, UbuntuAclManagerConfig):
            rtnval = config.getfacl_path
        else:
            rtnval = _GETFACL
        return rtnval

    def _setfacl_path(self) -> str:
        """
            Return the configured ``setfacl`` path.
        """
        config = self._config
        if isinstance(config, UbuntuAclManagerConfig):
            rtnval = config.setfacl_path
        else:
            rtnval = _SETFACL
        return rtnval

    def _dispatch_acl_write(
        self,
        *,
        command: list,
        path: Optional[str] = None,
        principal: Optional[str] = None,
        stdin: Optional[str] = None,
    ) -> UbuntuAclActionResult:
        """
            Dispatch a write command and wrap the result with the
            ACL-scope fields.
        """
        rtnval = self._dispatch(
            command=command,
            detail=path,
            stdin=stdin,
            operation="acl_write",
            path=path,
            principal=principal,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuAclActionStatus,
        command: list,
        path: Optional[str] = None,
        principal: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuAclActionResult:
        """
            Construct an :class:`UbuntuAclActionResult` without
            invoking the runner.
        """
        rtnval = UbuntuAclActionResult(
            status=status,
            detail=path if path is not None else principal,
            command=list(command),
            path=path,
            principal=principal,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _principal_type_letter(principal_type: UbuntuAcePrincipalType) -> str:
    """
        Return the single-character POSIX ACL prefix for a principal
        type.  Used to render ``setfacl`` argument tokens and to parse
        ``getfacl`` output rows.

        :param principal_type: The principal type.

        :returns: ``"u"``, ``"g"``, ``"o"``, or ``"m"``.
    """
    if principal_type == UbuntuAcePrincipalType.USER:
        rtnval = "u"
        return rtnval
    if principal_type == UbuntuAcePrincipalType.GROUP:
        rtnval = "g"
        return rtnval
    if principal_type == UbuntuAcePrincipalType.OTHER:
        rtnval = "o"
        return rtnval
    if principal_type == UbuntuAcePrincipalType.MASK:
        rtnval = "m"
        return rtnval
    err_msg = "Unknown UbuntuAcePrincipalType: {}".format(principal_type)
    raise ValueError(err_msg)


def _principal_type_from_word(word: str) -> Optional[UbuntuAcePrincipalType]:
    """
        Translate a ``getfacl`` row prefix (``"user"`` / ``"group"`` /
        ``"other"`` / ``"mask"``) to a :class:`UbuntuAcePrincipalType`.
        Returns ``None`` for unknown prefixes so the caller can skip
        the row.

        :param word: The first colon-separated token.
    """
    if word == "user":
        rtnval: Optional[UbuntuAcePrincipalType] = UbuntuAcePrincipalType.USER
        return rtnval
    if word == "group":
        rtnval = UbuntuAcePrincipalType.GROUP
        return rtnval
    if word == "other":
        rtnval = UbuntuAcePrincipalType.OTHER
        return rtnval
    if word == "mask":
        rtnval = UbuntuAcePrincipalType.MASK
        return rtnval
    rtnval = None
    return rtnval


def _materialise_ace_spec(
    *,
    principal: UbuntuAceSpec,
    rights_override: Optional[str],
) -> UbuntuAceSpec:
    """
        Return a normalised :class:`UbuntuAceSpec`, applying any
        ``rights_override`` from the caller.  We never mutate the
        caller's spec.

        :param principal: The caller-supplied spec.
        :param rights_override: Optional overriding rights string.
    """
    if rights_override is not None:
        effective_rights = rights_override
    else:
        effective_rights = principal.rights

    rtnval = UbuntuAceSpec(
        principal_type=principal.principal_type,
        principal=principal.principal,
        rights=effective_rights,
        default=principal.default,
    )
    return rtnval


def _render_single_setfacl_entry(*, spec: UbuntuAceSpec) -> str:
    """
        Render a single :class:`UbuntuAceSpec` into a ``setfacl -m``
        token.

        Examples (no ``default``)::

            UbuntuAceSpec(USER, "alice", "rwx") -> "u:alice:rwx"
            UbuntuAceSpec(GROUP, "wheel", "r-x") -> "g:wheel:r-x"
            UbuntuAceSpec(OTHER, None, "r--") -> "o::r--"
            UbuntuAceSpec(MASK, None, "rwx") -> "m::rwx"

        With ``default=True`` the entry is prefixed with ``"d:"`` so
        ``setfacl`` records it as the inheritance prototype.

        :param spec: The ACE.
    """
    letter = _principal_type_letter(spec.principal_type)
    if spec.principal is None:
        principal_token = ""
    else:
        principal_token = spec.principal

    body = "{}:{}:{}".format(letter, principal_token, spec.rights)

    if spec.default:
        rtnval = "d:" + body
    else:
        rtnval = body
    return rtnval


def _render_setfacl_remove_token(*, spec: UbuntuAceSpec) -> str:
    """
        Render a :class:`UbuntuAceSpec` into a ``setfacl -x`` argument
        token.  Unlike ``-m``, ``-x`` does NOT carry rights -- it
        accepts only ``[d:]<type>:<name>``.

        :param spec: The ACE whose principal is to be removed.
    """
    letter = _principal_type_letter(spec.principal_type)
    if spec.principal is None:
        principal_token = ""
    else:
        principal_token = spec.principal

    body = "{}:{}".format(letter, principal_token)

    if spec.default:
        rtnval = "d:" + body
    else:
        rtnval = body
    return rtnval


def _render_setfacl_entries(*, spec: list) -> str:
    """
        Render a list of :class:`UbuntuAceSpec` into a single comma-
        joined ``setfacl -m`` argument.

        :param spec: The list of ACEs.
    """
    rendered_parts: list = []
    for ace in spec:
        rendered_parts.append(_render_single_setfacl_entry(spec=ace))

    rtnval = ",".join(rendered_parts)
    return rtnval


def _parse_getfacl_output(
    *,
    output: str,
    path: str,
) -> UbuntuAclInfo:
    """
        Parse the output of ``getfacl <path>`` into a
        :class:`UbuntuAclInfo`.

        ``getfacl`` output for a single path looks like::

            # file: tmp/example
            # owner: alice
            # group: staff
            user::rwx
            user:bob:r-x
            group::r-x
            group:devs:rwx
            mask::rwx
            other::r--
            default:user::rwx
            default:user:bob:r-x

        Without ``--omit-header`` the three leading ``#`` lines are
        present; with it they are not.  We accept either shape.

        :param output: The captured stdout.
        :param path: The path the read was for (used to populate
                     :class:`UbuntuAclInfo.path` even when the leading
                     ``# file:`` line is absent).

        :returns: The parsed :class:`UbuntuAclInfo`.
    """
    info = UbuntuAclInfo(path=path)

    for raw_line in output.splitlines():
        stripped = raw_line.strip()

        if len(stripped) == 0:
            continue

        if stripped.startswith("#"):
            _parse_getfacl_header_line(line=stripped, info=info)
            continue

        ace = _parse_getfacl_ace_line(line=stripped)
        if ace is None:
            continue

        if ace.default:
            info.default_aces.append(ace)
        else:
            info.aces.append(ace)

    rtnval = info
    return rtnval


def _parse_getfacl_header_line(
    *,
    line: str,
    info: UbuntuAclInfo,
) -> None:
    """
        Parse a single ``# key: value`` ``getfacl`` header line and
        update ``info`` in-place.

        :param line: The header line (with leading ``#``).
        :param info: The :class:`UbuntuAclInfo` being populated.
    """
    body = line.lstrip("#").strip()
    if ":" not in body:
        return

    key, _, value = body.partition(":")
    key_clean = key.strip().lower()
    value_clean = value.strip()

    if key_clean == "owner":
        info.owner = value_clean
        return
    if key_clean == "group":
        info.group = value_clean
        return
    return


def _parse_getfacl_ace_line(*, line: str) -> Optional[UbuntuAceInfo]:
    """
        Parse a non-comment ``getfacl`` ACE line into a
        :class:`UbuntuAceInfo`.  Lines may include a trailing
        ``#effective:rwx`` comment indicating mask-clamped effective
        rights; the comment is stripped.  Returns ``None`` for
        unrecognised lines.

        :param line: The non-comment line.
    """
    # Strip a trailing ``#effective:...`` comment when present.
    hash_index = line.find("#")
    if hash_index >= 0:
        body = line[:hash_index].strip()
    else:
        body = line.strip()

    if len(body) == 0:
        rtnval: Optional[UbuntuAceInfo] = None
        return rtnval

    if body.startswith("default:"):
        is_default = True
        body = body[len("default:"):]
    else:
        is_default = False

    parts = body.split(":")
    if len(parts) != 3:
        rtnval = None
        return rtnval

    type_word = parts[0]
    name_token = parts[1]
    rights_token = parts[2]

    principal_type = _principal_type_from_word(word=type_word)
    if principal_type is None:
        rtnval = None
        return rtnval

    if len(name_token) == 0:
        principal_value: Optional[str] = None
    else:
        principal_value = name_token

    rtnval = UbuntuAceInfo(
        principal_type=principal_type,
        principal=principal_value,
        rights=rights_token,
        default=is_default,
    )
    return rtnval


def _ace_already_present(
    *,
    current: UbuntuAclInfo,
    spec: UbuntuAceSpec,
) -> bool:
    """
        Return True when the parsed ACL already contains an ACE that
        matches ``spec`` on principal-type, principal, rights, and
        default-flag.

        :param current: The current :class:`UbuntuAclInfo`.
        :param spec: The desired ACE.
    """
    if spec.default:
        target_list = current.default_aces
    else:
        target_list = current.aces

    rtnval = False
    for ace in target_list:
        if ace.principal_type != spec.principal_type:
            continue
        if ace.principal != spec.principal:
            continue
        if ace.rights != spec.rights:
            continue
        rtnval = True
        break
    return rtnval


def _ace_principal_present(
    *,
    current: UbuntuAclInfo,
    spec: UbuntuAceSpec,
) -> bool:
    """
        Return True when the parsed ACL contains an ACE for the same
        principal-type / principal / default-flag, regardless of the
        rights string.

        :param current: The current :class:`UbuntuAclInfo`.
        :param spec: The principal whose presence we are testing.
    """
    if spec.default:
        target_list = current.default_aces
    else:
        target_list = current.aces

    rtnval = False
    for ace in target_list:
        if ace.principal_type != spec.principal_type:
            continue
        if ace.principal != spec.principal:
            continue
        rtnval = True
        break
    return rtnval


def _has_extended_aces(*, info: UbuntuAclInfo) -> bool:
    """
        Return True when ``info`` contains any extended (non-base)
        ACE.  Base ACEs are the three rows ``user::``, ``group::``, and
        ``other::``; everything else is "extended".

        :param info: The parsed ACL.
    """
    if len(info.default_aces) > 0:
        rtnval = True
        return rtnval

    rtnval = False
    for ace in info.aces:
        # Named user / named group rows are extended.
        if ace.principal_type == UbuntuAcePrincipalType.USER and ace.principal is not None:
            rtnval = True
            break
        if ace.principal_type == UbuntuAcePrincipalType.GROUP and ace.principal is not None:
            rtnval = True
            break
        if ace.principal_type == UbuntuAcePrincipalType.MASK:
            rtnval = True
            break
    return rtnval


def _acl_matches_spec(
    *,
    current: UbuntuAclInfo,
    desired: list,
) -> bool:
    """
        Return True when the parsed ACL is byte-equal to the requested
        ``desired`` spec.  Comparison is order-insensitive within the
        regular-vs-default partitions.

        :param current: The current :class:`UbuntuAclInfo`.
        :param desired: A list of :class:`UbuntuAceSpec`.
    """
    desired_regular: list = []
    desired_default: list = []
    for spec in desired:
        normalised = (
            spec.principal_type,
            spec.principal,
            spec.rights,
        )
        if spec.default:
            desired_default.append(normalised)
        else:
            desired_regular.append(normalised)

    current_regular: list = []
    for ace in current.aces:
        normalised = (
            ace.principal_type,
            ace.principal,
            ace.rights,
        )
        current_regular.append(normalised)

    current_default: list = []
    for ace in current.default_aces:
        normalised = (
            ace.principal_type,
            ace.principal,
            ace.rights,
        )
        current_default.append(normalised)

    desired_regular_sorted = sorted(desired_regular, key=_ace_sort_key)
    desired_default_sorted = sorted(desired_default, key=_ace_sort_key)
    current_regular_sorted = sorted(current_regular, key=_ace_sort_key)
    current_default_sorted = sorted(current_default, key=_ace_sort_key)

    if desired_regular_sorted != current_regular_sorted:
        rtnval = False
        return rtnval

    if desired_default_sorted != current_default_sorted:
        rtnval = False
        return rtnval

    rtnval = True
    return rtnval


def _ace_sort_key(item) -> tuple:
    """
        Stable sort key for an ACE comparison tuple
        ``(principal_type, principal, rights)``.  ``principal`` may be
        ``None``; we coerce that to the empty string so the key is
        always orderable.

        :param item: The 3-tuple.
    """
    type_value, principal_value, rights_value = item
    if principal_value is None:
        principal_text = ""
    else:
        principal_text = principal_value
    rtnval = (type_value.value, principal_text, rights_value)
    return rtnval


def _acl_infos_equal(
    *,
    left: UbuntuAclInfo,
    right: UbuntuAclInfo,
) -> bool:
    """
        Return True when two :class:`UbuntuAclInfo` snapshots carry
        identical ACE lists (regular and default).  Owner / group /
        path are intentionally NOT compared -- :meth:`copy_acl` is
        about ACE state, not file ownership.

        :param left: One snapshot.
        :param right: The other snapshot.
    """
    left_regular = sorted(
        ((a.principal_type, a.principal, a.rights) for a in left.aces),
        key=_ace_sort_key,
    )
    right_regular = sorted(
        ((a.principal_type, a.principal, a.rights) for a in right.aces),
        key=_ace_sort_key,
    )

    left_default = sorted(
        ((a.principal_type, a.principal, a.rights) for a in left.default_aces),
        key=_ace_sort_key,
    )
    right_default = sorted(
        ((a.principal_type, a.principal, a.rights) for a in right.default_aces),
        key=_ace_sort_key,
    )

    if left_regular != right_regular:
        rtnval = False
        return rtnval
    if left_default != right_default:
        rtnval = False
        return rtnval
    rtnval = True
    return rtnval
