"""
    Ubuntu nfs-server-management feature.

    Implements the cross-OS nfs-server-management contract for Ubuntu.
    Ubuntu's stock NFS-server implementation ships in the
    ``nfs-kernel-server`` package; the daemon is exposed under systemd
    as ``nfs-kernel-server.service``.  This module wraps the
    ``exportfs`` tool and the on-disk content of ``/etc/exports``
    through the injected :class:`ICommandRunner` so the manager works
    identically under local and SSH execution.

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | Ubuntu tool                       |
    +=============================+===================================+
    | ``add_export``              | ``cat /etc/exports`` + parse +    |
    |                             | render + ``tee /etc/exports`` +   |
    |                             | ``exportfs -ra``                  |
    +-----------------------------+-----------------------------------+
    | ``remove_export``           | ``cat /etc/exports`` + parse +    |
    |                             | render + ``tee /etc/exports`` +   |
    |                             | ``exportfs -ra``                  |
    +-----------------------------+-----------------------------------+
    | ``list_exports``            | ``cat /etc/exports`` + parse +    |
    |                             | ``exportfs -v`` for active state  |
    +-----------------------------+-----------------------------------+
    | ``reload_exports``          | ``exportfs -ra``                  |
    +-----------------------------+-----------------------------------+
    | ``get_export_status``       | ``exportfs -v`` parse for the path|
    +-----------------------------+-----------------------------------+
    | ``set_export_options``      | ``cat`` + parse + render-with-new-|
    |                             | options + ``tee`` + ``exportfs``  |
    +-----------------------------+-----------------------------------+

    File I/O policy:
        Reads use ``cat``; writes use ``tee`` with the new content on
        stdin.  This module does NOT consume an :class:`IFileOperator`.

    Service-name caveat:
        On Debian-derived distributions (including Ubuntu) the NFS
        kernel-server systemd unit is ``nfs-kernel-server.service``,
        NOT ``nfs-server.service``.
        :class:`UbuntuNfsManagerConfig` therefore defaults
        ``service_name`` to ``"nfs-kernel-server"``.  Note that this
        manager itself NEVER calls ``systemctl`` — the unit name is
        recorded in the config purely so that callers (and downstream
        coordinators) can hand the correct unit to a service-manager
        without re-deriving the name from this module.

    Drop-in caveat:
        Ubuntu also honours drop-in files under
        ``/etc/exports.d/*.exports``.  This pilot edits the primary
        ``/etc/exports`` file only; drop-in authoring is a tracked
        follow-up.

    Cross-OS exports-syntax divergence:
        Ubuntu / Linux uses the canonical
        ``<path> <client>(<opts>) <client>(<opts>)…`` line shape
        (parenthesised, comma-separated options, options scoped per
        client).  macOS uses
        ``<path> -<opt> -<opt> <client> [<client>…]`` (dash-prefixed
        options shared across the whole line).  Windows uses no on-
        disk export file at all — shares are addressed by share-name
        through ``New-NfsShare`` / ``Set-NfsShare`` /
        ``Remove-NfsShare``.  This Ubuntu manager renders Linux-
        canonical lines exclusively.

    Idempotency model:
        Every write verb reads the current exports file, parses it,
        mutates the parsed structure in memory, re-renders the file,
        and compares to the original content.  When the rendered
        content equals the original the verb returns ``SKIPPED``
        without invoking ``tee`` or ``exportfs``.  Idempotency is
        exact-match on the rendered string, so callers who switch
        option order will see one rewrite the first time they
        reconcile and then steady-state SKIPs thereafter.

    Reload-after-change:
        Every successful mutation that touches ``/etc/exports`` is
        followed by ``exportfs -ra`` (re-export every entry).  When
        the chained ``exportfs -ra`` fails after a successful
        ``tee``, the verb maps to ``FAILED`` and the reload's argv /
        exit code / stderr are surfaced on the returned result.

    Elevation policy:
        Write verbs (``add_export``, ``remove_export``,
        ``reload_exports``, ``set_export_options``) honour
        ``config.elevated``.  Reads (``list_exports``,
        ``get_export_status``) never elevate.  The ``cat`` /
        ``exportfs -v`` probes used inside write paths to drive
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
_EXPORTFS = "exportfs"

_DEFAULT_EXPORTS_FILE = "/etc/exports"
_DEFAULT_SERVICE_NAME = "nfs-kernel-server"

_DEFAULT_CLIENT_OPTIONS = (
    "rw",
    "sync",
    "no_subtree_check",
)


UbuntuNfsActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


def _default_options_list() -> list:
    """
        Build the default per-client options list.

        :returns: A fresh list of the default options.
    """
    rtnval = list(_DEFAULT_CLIENT_OPTIONS)
    return rtnval


@dataclass
class UbuntuNfsManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuNfsServerManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param exports_file: Absolute path to the primary exports
                             file.  Default is the Ubuntu stock
                             location ``/etc/exports``.
        :param service_name: systemd unit name used by callers who
                             wish to restart the daemon.  This module
                             does NOT call ``systemctl`` directly;
                             the unit name is recorded purely for the
                             convenience of downstream coordinators.
                             Default is ``"nfs-kernel-server"``.
    """
    exports_file: str = _DEFAULT_EXPORTS_FILE
    service_name: str = _DEFAULT_SERVICE_NAME


@dataclass
class UbuntuNfsActionResult(FeatureActionResult):
    """
        Result of an nfs-server write operation.

        :param path: The export path the action targeted, or ``None``
                     for verbs (e.g., ``reload_exports``) that do not
                     target a single export.
        :param share_name: Reserved for cross-OS parity with the
                           Windows manager.  Always ``None`` on
                           Ubuntu.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    path: Optional[str] = None
    share_name: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuNfsClientSpec:
    """
        A single client matcher for an export.

        :param host: Client address matcher (hostname, CIDR,
                     wildcard, or ``"*"``).  Default is ``"*"``
                     (all clients).
        :param options: Per-client export options.  Default is the
                        Ubuntu canonical safe-default list
                        ``["rw", "sync", "no_subtree_check"]``.
    """
    host: str = "*"
    options: list = field(default_factory=_default_options_list)


@dataclass
class UbuntuNfsExportSpec:
    """
        Declarative spec for a single export.

        :param path: The filesystem path to export.
        :param clients: One or more client matchers.  Must be
                        non-empty — the contract requires at least
                        one client.
        :param share_name: Reserved for cross-OS parity with the
                           Windows manager.  Ignored on Ubuntu.
    """
    path: str
    clients: list
    share_name: Optional[str] = None


@dataclass
class UbuntuNfsExportInfo:
    """
        One parsed export entry.

        :param path: The exported filesystem path.
        :param share_name: Reserved for cross-OS parity.  Always
                           ``None`` on Ubuntu.
        :param clients: The client matchers parsed from the line.
        :param is_active: True when the export is currently
                          advertised by the daemon (i.e., the path
                          appears in ``exportfs -v`` output).
    """
    path: str
    share_name: Optional[str]
    clients: list
    is_active: bool


@dataclass
class UbuntuNfsExportStatus:
    """
        Result of :meth:`UbuntuNfsServerManager.get_export_status`.

        :param path: The export path the status describes.
        :param is_active: True when the path appears in
                          ``exportfs -v`` output.
        :param client_count: The number of distinct clients seen for
                             the path in the active export table.
        :param flags: The parenthesised options list of the first
                      client entry for the path.  Empty when
                      ``is_active`` is False.
    """
    path: str
    is_active: bool
    client_count: int
    flags: list


class UbuntuNfsQueryError(Exception):
    """
        Raised when an exports read fails for a reason other than
        the target file simply being absent.
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

        err_msg_lines = ["NFS server query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class UbuntuNfsServerManager(BaseFeatureManager):
    """
        Ubuntu nfs-server-management manager.  Reads, writes, and
        reloads ``/etc/exports`` through the injected
        :class:`ICommandRunner`.
    """

    result_class = UbuntuNfsActionResult
    status_class = UbuntuNfsActionStatus
    feature_name = "nfs-server-management"

    def __init__(
        self,
        runner,
        config: Optional[UbuntuNfsManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every dispatch.
            :param config: Manager configuration.  Defaults to a
                           fresh :class:`UbuntuNfsManagerConfig`.
        """
        if config is None:
            config = UbuntuNfsManagerConfig()
        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_exports(self) -> list:
        """
            Enumerate every export configured in ``/etc/exports`` and
            mark each one active or inactive based on the daemon's
            current export table.

            :returns: A ``list[UbuntuNfsExportInfo]``.  Empty when the
                      configuration file is missing.

            :raises UbuntuNfsQueryError: When ``exportfs -v`` exits
                                         non-zero with non-empty
                                         stderr.
        """
        path = self._typed_config().exports_file
        content = self._read_file(path=path)
        if content is None:
            rtnval: list = []
            return rtnval

        configured_exports = _parse_exports_file(content=content)
        active_paths = self._read_active_export_paths()

        rtnval = []
        for entry in configured_exports:
            if entry.path in active_paths:
                entry.is_active = True
            else:
                entry.is_active = False
            rtnval.append(entry)
        return rtnval

    def get_export_status(self, path: str) -> UbuntuNfsExportStatus:
        """
            Query the daemon for whether ``path`` is currently
            advertised.

            :param path: The export path to query.

            :returns: The :class:`UbuntuNfsExportStatus`.

            :raises UbuntuNfsQueryError: When ``exportfs -v`` exits
                                         non-zero with non-empty
                                         stderr.
        """
        active = self._invoke_exportfs_v()

        if path not in active:
            rtnval = UbuntuNfsExportStatus(
                path=path,
                is_active=False,
                client_count=0,
                flags=[],
            )
            return rtnval

        flag_list = list(active[path]["flags"])
        client_count = active[path]["client_count"]
        rtnval = UbuntuNfsExportStatus(
            path=path,
            is_active=True,
            client_count=client_count,
            flags=flag_list,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_export(
        self,
        spec: UbuntuNfsExportSpec,
    ) -> UbuntuNfsActionResult:
        """
            Add or update an export at ``spec.path``.

            Idempotent: returns ``SKIPPED`` when the file already
            contains an export at ``spec.path`` whose client matchers
            match exactly.  Honours dry-run.  After a successful
            ``tee`` the manager dispatches ``exportfs -ra``; if that
            chained reload fails the verb maps to ``FAILED`` and the
            reload's argv / exit / stderr are surfaced on the
            returned result.

            :param spec: The :class:`UbuntuNfsExportSpec`.

            :returns: The :class:`UbuntuNfsActionResult`.
        """
        target_path = self._typed_config().exports_file
        write_command = [_TEE, target_path]

        existing = self._read_file(path=target_path)
        existing_content = "" if existing is None else existing

        existing_exports = _parse_exports_file(content=existing_content)
        match_status = _match_existing_export(
            existing_exports=existing_exports, spec=spec,
        )
        if match_status == "exact":
            skipped = self._build_result(
                status=UbuntuNfsActionStatus.SKIPPED,
                path=spec.path,
                command=write_command,
                message="export already at desired state",
            )
            return skipped

        new_content = _upsert_export(
            content=existing_content, spec=spec,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuNfsActionStatus.DRY_RUN,
                path=spec.path,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code != 0:
            failed = UbuntuNfsActionResult(
                status=UbuntuNfsActionStatus.FAILED,
                detail="add_export:{}".format(spec.path),
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                path=spec.path,
                share_name=None,
            )
            return failed

        rtnval = self._invoke_exportfs_reload(
            base_action="add_export:{}".format(spec.path),
            path=spec.path,
            primary_run=run_result,
            primary_command=write_command,
        )
        return rtnval

    def remove_export(self, path: str) -> UbuntuNfsActionResult:
        """
            Remove every export line whose first token equals
            ``path``.

            Idempotent: returns ``SKIPPED`` when the file is missing
            or no line matches.  Honours dry-run.  After a successful
            ``tee`` the manager dispatches ``exportfs -ra``.

            :param path: The export path to remove.

            :returns: The :class:`UbuntuNfsActionResult`.
        """
        target_path = self._typed_config().exports_file
        write_command = [_TEE, target_path]

        existing = self._read_file(path=target_path)
        if existing is None:
            skipped = self._build_result(
                status=UbuntuNfsActionStatus.SKIPPED,
                path=path,
                command=write_command,
                message="exports file does not exist",
            )
            return skipped

        new_content = _remove_export_lines(content=existing, path=path)
        if new_content == existing:
            skipped = self._build_result(
                status=UbuntuNfsActionStatus.SKIPPED,
                path=path,
                command=write_command,
                message="export not present",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuNfsActionStatus.DRY_RUN,
                path=path,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code != 0:
            failed = UbuntuNfsActionResult(
                status=UbuntuNfsActionStatus.FAILED,
                detail="remove_export:{}".format(path),
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                path=path,
                share_name=None,
            )
            return failed

        rtnval = self._invoke_exportfs_reload(
            base_action="remove_export:{}".format(path),
            path=path,
            primary_run=run_result,
            primary_command=write_command,
        )
        return rtnval

    def reload_exports(self) -> UbuntuNfsActionResult:
        """
            Re-export every entry in ``/etc/exports`` via
            ``exportfs -ra``.

            Operational verb — never skips.  Honours dry-run.

            :returns: The :class:`UbuntuNfsActionResult`.
        """
        command = [_EXPORTFS, "-ra"]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuNfsActionStatus.DRY_RUN,
                path=None,
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
            path=None,
        )
        return rtnval

    def set_export_options(
        self,
        path: str,
        options: list,
    ) -> UbuntuNfsActionResult:
        """
            Replace the per-client options list on every client of
            the export at ``path``.

            Idempotent: returns ``SKIPPED`` when the file is missing,
            no line matches, or every client already carries exactly
            the supplied options.  Honours dry-run.

            :param path: The export path whose options should be
                         replaced.
            :param options: The new options list.  Applied to every
                            client on the matched line.

            :returns: The :class:`UbuntuNfsActionResult`.
        """
        target_path = self._typed_config().exports_file
        write_command = [_TEE, target_path]

        existing = self._read_file(path=target_path)
        if existing is None:
            skipped = self._build_result(
                status=UbuntuNfsActionStatus.SKIPPED,
                path=path,
                command=write_command,
                message="exports file does not exist",
            )
            return skipped

        new_content = _set_options_for_path(
            content=existing, path=path, options=list(options),
        )
        if new_content is None:
            skipped = self._build_result(
                status=UbuntuNfsActionStatus.SKIPPED,
                path=path,
                command=write_command,
                message="export not present",
            )
            return skipped

        if new_content == existing:
            skipped = self._build_result(
                status=UbuntuNfsActionStatus.SKIPPED,
                path=path,
                command=write_command,
                message="options already at desired value",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuNfsActionStatus.DRY_RUN,
                path=path,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code != 0:
            failed = UbuntuNfsActionResult(
                status=UbuntuNfsActionStatus.FAILED,
                detail="set_export_options:{}".format(path),
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                path=path,
                share_name=None,
            )
            return failed

        rtnval = self._invoke_exportfs_reload(
            base_action="set_export_options:{}".format(path),
            path=path,
            primary_run=run_result,
            primary_command=write_command,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> UbuntuNfsManagerConfig:
        """
            Narrow the base config to the typed subclass.

            :returns: The typed :class:`UbuntuNfsManagerConfig`.
        """
        rtnval = self._config
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat`` without elevation.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when
                      ``cat`` reports a non-zero exit code (i.e.
                      the path is missing).
        """
        command = [_CAT, path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _invoke_exportfs_v(self) -> dict:
        """
            Read the active export table via ``exportfs -v``.

            :returns: A dict mapping path to ``{"client_count": int,
                      "flags": list[str]}``.

            :raises UbuntuNfsQueryError: When ``exportfs -v`` exits
                                         non-zero with non-empty
                                         stderr.
        """
        command = [_EXPORTFS, "-v"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise UbuntuNfsQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = _parse_exportfs_v_output(text=run_result.stdout)
        return rtnval

    def _read_active_export_paths(self) -> set:
        """
            Build the set of paths the daemon currently advertises.

            :returns: A set of path strings.
        """
        active = self._invoke_exportfs_v()
        rtnval = set(active.keys())
        return rtnval

    def _invoke_exportfs_reload(
        self,
        *,
        base_action: str,
        path: Optional[str],
        primary_run,
        primary_command: list,
    ) -> UbuntuNfsActionResult:
        """
            Dispatch ``exportfs -ra`` after a successful primary
            mutation.  When the reload fails, the returned result is
            mapped to ``FAILED`` even though the primary mutation
            succeeded.

            :param base_action: The detail string identifying the
                                primary verb (e.g.,
                                ``"add_export:/srv/share"``).
            :param path: The export path the primary verb targeted.
            :param primary_run: The :class:`RunResult` from the
                                preceding ``tee`` call.
            :param primary_command: The argv that ran first.

            :returns: A :class:`UbuntuNfsActionResult` whose status
                      reflects the ``exportfs -ra`` outcome.
        """
        reload_command = [_EXPORTFS, "-ra"]
        reload_run = self._runner.run(
            list(reload_command),
            elevated=self._config.elevated,
        )
        if reload_run.exit_code != 0:
            rtnval = UbuntuNfsActionResult(
                status=UbuntuNfsActionStatus.FAILED,
                detail=base_action,
                exit_code=reload_run.exit_code,
                stdout=reload_run.stdout,
                stderr=reload_run.stderr,
                command=list(reload_command),
                path=path,
                share_name=None,
            )
            return rtnval

        rtnval = UbuntuNfsActionResult(
            status=UbuntuNfsActionStatus.SUCCESS,
            detail=base_action,
            exit_code=primary_run.exit_code,
            stdout=primary_run.stdout,
            stderr=primary_run.stderr,
            command=list(primary_command),
            path=path,
            share_name=None,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuNfsActionStatus,
        path: Optional[str],
        command: list,
        message: Optional[str] = None,
    ) -> UbuntuNfsActionResult:
        """
            Build a result without invoking the runner.

            :param status: Outcome status.
            :param path: Export path targeted, or ``None``.
            :param command: Would-have-been argv.
            :param message: Optional human-readable detail.

            :returns: The :class:`UbuntuNfsActionResult`.
        """
        if path is not None:
            detail_text = path
        else:
            detail_text = None
        rtnval = UbuntuNfsActionResult(
            status=status,
            detail=detail_text,
            command=list(command),
            path=path,
            share_name=None,
            message=message,
        )
        return rtnval

    def _wrap_runner_result(
        self,
        *,
        run_result,
        command: list,
        path: Optional[str],
    ) -> UbuntuNfsActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`UbuntuNfsActionResult`.

            :param run_result: The runner's :class:`RunResult`.
            :param command: The argv that ran.
            :param path: Export path targeted, or ``None``.

            :returns: The :class:`UbuntuNfsActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = UbuntuNfsActionStatus.SUCCESS
        else:
            mapped_status = UbuntuNfsActionStatus.FAILED

        if path is not None:
            detail_text = path
        else:
            detail_text = None

        rtnval = UbuntuNfsActionResult(
            status=mapped_status,
            detail=detail_text,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            path=path,
            share_name=None,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — exports parser / renderer
# ----------------------------------------------------------------------


def _parse_exports_file(*, content: str) -> list:
    """
        Parse an ``/etc/exports`` file body into a list of
        :class:`UbuntuNfsExportInfo`.

        Comment and blank lines are skipped.  Each remaining line is
        split on whitespace; the first token is the path, every
        remaining token is parsed by :func:`_parse_client_token`.

        Lines whose only token is the path (no clients) are skipped —
        the canonical Linux export grammar requires at least one
        client.

        :param content: The raw file content.

        :returns: A ``list[UbuntuNfsExportInfo]`` in declaration
                  order.  ``is_active`` is left at ``False`` for
                  every entry — :meth:`UbuntuNfsServerManager.list_exports`
                  populates that field after parsing.
    """
    rtnval: list = []
    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue
        tokens = stripped.split()
        if len(tokens) < 2:
            continue
        path = tokens[0]
        client_specs: list = []
        for client_token in tokens[1:]:
            parsed_client = _parse_client_token(token=client_token)
            client_specs.append(parsed_client)
        entry = UbuntuNfsExportInfo(
            path=path,
            share_name=None,
            clients=client_specs,
            is_active=False,
        )
        rtnval.append(entry)
    return rtnval


def _parse_client_token(*, token: str) -> UbuntuNfsClientSpec:
    """
        Parse a single ``<host>(<opts>)`` client token.

        When the token has no parenthesised options block the
        canonical default option set is applied.

        :param token: The client token (e.g. ``"*(rw,sync,no_subtree_check)"``).

        :returns: A populated :class:`UbuntuNfsClientSpec`.
    """
    if "(" not in token:
        rtnval = UbuntuNfsClientSpec(
            host=token,
            options=_default_options_list(),
        )
        return rtnval

    open_index = token.index("(")
    close_index = token.rfind(")")
    if close_index <= open_index:
        rtnval = UbuntuNfsClientSpec(
            host=token[:open_index],
            options=_default_options_list(),
        )
        return rtnval

    host = token[:open_index]
    opts_text = token[open_index + 1:close_index]
    if len(opts_text) == 0:
        options: list = []
    else:
        options = []
        for piece in opts_text.split(","):
            normalised = piece.strip()
            if len(normalised) == 0:
                continue
            options.append(normalised)
    rtnval = UbuntuNfsClientSpec(host=host, options=options)
    return rtnval


def _render_client_token(*, client: UbuntuNfsClientSpec) -> str:
    """
        Render a :class:`UbuntuNfsClientSpec` as a single
        ``<host>(<opt1>,<opt2>,…)`` token.

        :param client: The client spec.

        :returns: The rendered token.
    """
    if client.options is not None and len(client.options) > 0:
        opts_text = ",".join(client.options)
    else:
        opts_text = ""
    rtnval = "{}({})".format(client.host, opts_text)
    return rtnval


def _render_export_line(*, spec: UbuntuNfsExportSpec) -> str:
    """
        Render a :class:`UbuntuNfsExportSpec` as a single canonical
        ``/etc/exports`` line.

        :param spec: The export spec.  Must carry at least one
                     client.

        :returns: The rendered line (no trailing newline).

        :raises ValueError: When ``spec.clients`` is empty.
    """
    if spec.clients is None or len(spec.clients) == 0:
        err_msg_lines = ["Cannot render export line with no clients."]
        err_msg_lines.append("    PATH: {}".format(spec.path))
        err_msg = os.linesep.join(err_msg_lines)
        raise ValueError(err_msg)

    client_tokens: list = []
    for client in spec.clients:
        client_tokens.append(_render_client_token(client=client))
    rtnval = "{} {}".format(spec.path, " ".join(client_tokens))
    return rtnval


def _render_export_line_for_info(*, info: UbuntuNfsExportInfo) -> str:
    """
        Render a parsed :class:`UbuntuNfsExportInfo` back to its
        canonical line shape.  Used after option mutations.

        :param info: The parsed export entry.

        :returns: The rendered line.
    """
    spec = UbuntuNfsExportSpec(
        path=info.path,
        clients=list(info.clients),
        share_name=info.share_name,
    )
    rtnval = _render_export_line(spec=spec)
    return rtnval


def _client_specs_match(
    *,
    left: list,
    right: list,
) -> bool:
    """
        Compare two lists of :class:`UbuntuNfsClientSpec` for
        exact equality.

        :param left: First list.
        :param right: Second list.

        :returns: True when the lists match position-by-position with
                  identical host strings and identical option lists.
    """
    if len(left) != len(right):
        rtnval = False
        return rtnval
    for index, left_client in enumerate(left):
        right_client = right[index]
        if left_client.host != right_client.host:
            rtnval = False
            return rtnval
        if list(left_client.options) != list(right_client.options):
            rtnval = False
            return rtnval
    rtnval = True
    return rtnval


def _match_existing_export(
    *,
    existing_exports: list,
    spec: UbuntuNfsExportSpec,
) -> str:
    """
        Compare ``spec`` against the parsed exports list.

        :param existing_exports: The parsed
                                 :class:`UbuntuNfsExportInfo` list.
        :param spec: The :class:`UbuntuNfsExportSpec` to compare.

        :returns: ``"exact"`` when an export at ``spec.path`` already
                  has matching clients; ``"path-only"`` when the path
                  exists with different clients; ``"absent"`` when
                  the path is not present.
    """
    rtnval: str = "absent"
    for entry in existing_exports:
        if entry.path != spec.path:
            continue
        if _client_specs_match(
            left=list(entry.clients), right=list(spec.clients),
        ):
            rtnval = "exact"
            return rtnval
        rtnval = "path-only"
        return rtnval
    return rtnval


def _upsert_export(
    *,
    content: str,
    spec: UbuntuNfsExportSpec,
) -> str:
    """
        Return content with the spec's line upserted.

        Existing lines whose first token matches ``spec.path`` are
        replaced in place; otherwise a new line is appended at the
        end of the file.  Comments and blank lines are preserved.

        :param content: Existing file content.
        :param spec: The :class:`UbuntuNfsExportSpec`.

        :returns: The updated content (with a trailing newline).
    """
    rendered_line = _render_export_line(spec=spec)
    new_lines: list = []
    replaced = False
    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            new_lines.append(raw)
            continue
        if stripped.startswith("#"):
            new_lines.append(raw)
            continue
        tokens = stripped.split()
        if len(tokens) >= 1 and tokens[0] == spec.path and not replaced:
            new_lines.append(rendered_line)
            replaced = True
            continue
        if len(tokens) >= 1 and tokens[0] == spec.path and replaced:
            # A duplicate line on the same path — drop it; the upsert
            # collapses to a single rendered line at the first
            # occurrence.
            continue
        new_lines.append(raw)

    if not replaced:
        new_lines.append(rendered_line)

    body = "\n".join(new_lines)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _remove_export_lines(*, content: str, path: str) -> str:
    """
        Return content with every line whose first token equals
        ``path`` stripped.

        :param content: Existing file content.
        :param path: Export path to remove.

        :returns: The updated content.
    """
    new_lines: list = []
    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            new_lines.append(raw)
            continue
        if stripped.startswith("#"):
            new_lines.append(raw)
            continue
        tokens = stripped.split()
        if len(tokens) >= 1 and tokens[0] == path:
            continue
        new_lines.append(raw)

    body = "\n".join(new_lines)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body


def _set_options_for_path(
    *,
    content: str,
    path: str,
    options: list,
) -> Optional[str]:
    """
        Return content with every client of the export at ``path``
        carrying exactly ``options``.

        :param content: Existing file content.
        :param path: Export path whose clients should be rewritten.
        :param options: New options list.

        :returns: The rewritten content, or ``None`` when no line
                  matches ``path``.
    """
    new_lines: list = []
    matched = False
    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            new_lines.append(raw)
            continue
        if stripped.startswith("#"):
            new_lines.append(raw)
            continue
        tokens = stripped.split()
        if len(tokens) < 2:
            new_lines.append(raw)
            continue
        if tokens[0] != path:
            new_lines.append(raw)
            continue
        matched = True
        new_clients: list = []
        for client_token in tokens[1:]:
            parsed = _parse_client_token(token=client_token)
            replacement = UbuntuNfsClientSpec(
                host=parsed.host,
                options=list(options),
            )
            new_clients.append(replacement)
        new_info = UbuntuNfsExportInfo(
            path=path,
            share_name=None,
            clients=new_clients,
            is_active=False,
        )
        new_lines.append(_render_export_line_for_info(info=new_info))

    if not matched:
        rtnval: Optional[str] = None
        return rtnval

    body = "\n".join(new_lines)
    if not body.endswith("\n"):
        body = body + "\n"
    rtnval = body
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — exportfs -v parser
# ----------------------------------------------------------------------


def _parse_exportfs_v_output(*, text: str) -> dict:
    """
        Parse ``exportfs -v`` output into a path→info dict.

        ``exportfs -v`` emits one line per client per export.  The
        canonical shape is::

            /srv/share <client>(<opts>)

        For long lines the kernel sometimes wraps the output, with
        the path on its own line and the client on the next line
        prefixed with whitespace.  This parser handles both shapes.

        :param text: The captured stdout.

        :returns: A ``dict[str, dict]`` with keys
                  ``"client_count": int`` and ``"flags": list[str]``
                  per export path.  ``flags`` is taken from the
                  first client entry seen for the path.
    """
    rtnval: dict = {}
    pending_path: Optional[str] = None

    for raw in text.splitlines():
        if len(raw) == 0:
            continue

        if raw[0] in (" ", "\t"):
            # Continuation line — the previous line was just a path.
            if pending_path is None:
                continue
            client_token = raw.strip()
            _record_client_for_path(
                table=rtnval,
                path=pending_path,
                client_token=client_token,
            )
            continue

        stripped = raw.strip()
        tokens = stripped.split(None, 1)
        if len(tokens) == 1:
            # Path on its own line; the client wraps to the next.
            pending_path = tokens[0]
            if pending_path not in rtnval:
                rtnval[pending_path] = {
                    "client_count": 0,
                    "flags": [],
                }
            continue

        path = tokens[0]
        client_token = tokens[1].strip()
        pending_path = path
        _record_client_for_path(
            table=rtnval,
            path=path,
            client_token=client_token,
        )

    return rtnval


def _record_client_for_path(
    *,
    table: dict,
    path: str,
    client_token: str,
) -> None:
    """
        Record a client entry for an export path inside the
        ``exportfs -v`` parse table.

        :param table: The parse table being built.
        :param path: The export path.
        :param client_token: The client token text (e.g.,
                             ``"*(sync,wdelay,hide,...)"``).
    """
    if path not in table:
        table[path] = {
            "client_count": 0,
            "flags": [],
        }
    table[path]["client_count"] = table[path]["client_count"] + 1
    if len(table[path]["flags"]) > 0:
        return
    parsed = _parse_client_token(token=client_token)
    table[path]["flags"] = list(parsed.options)
    return
