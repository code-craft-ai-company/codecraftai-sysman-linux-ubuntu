"""
    Ubuntu repository-management feature.

    Implements the cross-OS repository-management contract for Ubuntu by
    managing files under ``/etc/apt/sources.list.d/`` plus the system-wide
    ``/etc/apt/sources.list`` and refreshing apt's package index via
    ``apt-get update``.

    File I/O policy:
        Like the RHEL reference, this module performs every file read /
        write / delete / rename through the injected
        :class:`ICommandRunner` (``cat``, ``tee``, ``rm``, ``mv``, ``ls``).
        It does NOT consume an :class:`IFileOperator`.  The drafted spec
        names ``IFileOperator`` as a collaborator; that wording is a
        forward-looking design note and the current implementation
        deviates from it pending introduction of a concrete file-operator
        in this codebase.

    Elevation policy:
        Write verbs and ``refresh_repositories`` honour
        ``config.elevated``.  ``list_repositories`` and reads never
        elevate.
"""

import os
import shlex

from dataclasses import dataclass, field
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_APT_GET = "apt-get"

_DEFAULT_TYPES = ["deb"]


UbuntuRepositoryActionStatus = ActionStatus


@dataclass
class UbuntuRepositoryManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuRepositoryManager`.

        :param sources_list_d: The directory enumerated for per-repository
                               source files.  Defaults to
                               ``/etc/apt/sources.list.d``.
        :param main_sources_list: The system-wide single-file sources list,
                                  parsed in addition to ``sources_list_d``
                                  by :meth:`list_repositories`.
    """
    sources_list_d: str = "/etc/apt/sources.list.d"
    main_sources_list: str = "/etc/apt/sources.list"


@dataclass
class UbuntuRepositoryActionResult(FeatureActionResult):
    """
        Result of a repository-management write operation.

        :param name: The repository name targeted (filename stem under
                     ``sources.list.d``), or ``None`` for
                     ``refresh_repositories``.
        :param path: The absolute file path the action targeted, when
                     applicable.
        :param message: Optional human-readable detail for ``SKIPPED`` or
                        ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    path: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuRepositorySpec:
    """
        Specification for an apt repository.

        :param name: Filename stem for the source file under
                     ``sources.list.d``.  Also used as the result
                     ``name`` echo.
        :param uri: Repository base URI.
        :param suites: One or more apt suites (e.g. ``["jammy"]``).
        :param components: Components within the suite (e.g.
                           ``["main", "universe"]``).
        :param types: Source types — ``["deb"]`` (the default),
                      optionally including ``"deb-src"``.
        :param signed_by: Optional absolute path to a dearmored keyring
                          rendered into the ``[signed-by=...]`` option of
                          each emitted source line.
        :param architectures: Optional list of architectures rendered as
                              ``[arch=...]`` on each emitted source line.
    """
    name: str
    uri: str
    suites: list[str]
    components: list[str]
    types: list[str] = field(default_factory=lambda: list(_DEFAULT_TYPES))
    signed_by: Optional[str] = None
    architectures: Optional[list[str]] = None


@dataclass
class UbuntuRepositoryInfo:
    """
        Summary of one apt source as enumerated by
        :meth:`UbuntuRepositoryManager.list_repositories`.

        :param name: Filename stem (without the ``.list`` /
                     ``.list.disabled`` suffix) for files under
                     ``sources.list.d``.  For lines parsed out of the main
                     ``/etc/apt/sources.list``, the synthetic name
                     ``"sources.list"`` is used.
        :param path: Absolute path the entry was read from.
        :param enabled: True when the file extension is ``.list``; False
                        when ``.list.disabled``.
        :param uri: First URI parsed from the file's source lines.
        :param suites: Suites parsed from the source line.
        :param components: Components parsed from the source line.
        :param types: Source types parsed from the file (``deb`` /
                      ``deb-src``).
    """
    name: str
    path: str
    enabled: bool
    uri: Optional[str]
    suites: list[str]
    components: list[str]
    types: list[str]


class UbuntuRepositoryQueryError(Exception):
    """
        Raised when a repository read fails for a reason other than the
        target simply being absent.
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

        err_msg_lines = ["Repository query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuRepositoryManager(BaseFeatureManager):
    """
        Ubuntu apt repository manager.  Every verb is dispatched through
        the injected :class:`ICommandRunner`; file I/O is performed via
        ``cat`` / ``tee`` / ``rm`` / ``mv`` / ``ls`` rather than a
        separate file operator.
    """

    result_class = UbuntuRepositoryActionResult
    status_class = UbuntuRepositoryActionStatus
    feature_name = "repository-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_repository(
        self,
        spec: UbuntuRepositorySpec,
    ) -> UbuntuRepositoryActionResult:
        """
            Add an apt repository by writing
            ``<sources_list_d>/<name>.list``.

            Skipped when the file already exists with byte-identical
            content.

            :param spec: The :class:`UbuntuRepositorySpec` to render.

            :returns: The :class:`UbuntuRepositoryActionResult`.
        """
        path = self._enabled_path(name=spec.name)
        desired = _render_one_line_sources(spec=spec)

        write_command = ["tee", path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuRepositoryActionStatus.DRY_RUN,
                name=spec.name,
                path=path,
                command=write_command,
            )
            return dry

        existing = self._read_file(path=path)
        if existing is not None and existing == desired:
            skipped = self._build_result(
                status=UbuntuRepositoryActionStatus.SKIPPED,
                name=spec.name,
                path=path,
                command=write_command,
                message="repository already exists with identical content",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=desired,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuRepositoryActionStatus.SUCCESS
        else:
            mapped_status = UbuntuRepositoryActionStatus.FAILED

        rtnval = UbuntuRepositoryActionResult(
            status=mapped_status,
            detail=spec.name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            name=spec.name,
            path=path,
        )
        return rtnval

    def remove_repository(
        self,
        name: str,
    ) -> UbuntuRepositoryActionResult:
        """
            Delete the source file for ``name``.

            Skipped when neither ``<name>.list`` nor ``<name>.list.disabled``
            exists.  When both exist (unusual but possible after manual
            edits), only the ``.list`` form is removed and the result
            reflects that path.

            :param name: The repository name (filename stem).

            :returns: The :class:`UbuntuRepositoryActionResult`.
        """
        enabled_path = self._enabled_path(name=name)
        disabled_path = self._disabled_path(name=name)

        target_path: Optional[str] = None
        if self._path_exists(path=enabled_path):
            target_path = enabled_path
        elif self._path_exists(path=disabled_path):
            target_path = disabled_path

        if target_path is None:
            command = ["rm", "-f", enabled_path]
            skipped = self._build_result(
                status=UbuntuRepositoryActionStatus.SKIPPED,
                name=name,
                path=enabled_path,
                command=command,
                message="repository file not found",
            )
            return skipped

        command = ["rm", "-f", target_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuRepositoryActionStatus.DRY_RUN,
                name=name,
                path=target_path,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuRepositoryActionStatus.SUCCESS
        else:
            mapped_status = UbuntuRepositoryActionStatus.FAILED

        rtnval = UbuntuRepositoryActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            path=target_path,
        )
        return rtnval

    def enable_repository(
        self,
        name: str,
    ) -> UbuntuRepositoryActionResult:
        """
            Re-enable a disabled repository by renaming
            ``<name>.list.disabled`` back to ``<name>.list``.

            Skipped when ``<name>.list`` already exists.  Returns FAILED
            when neither file variant exists.

            :param name: The repository name (filename stem).

            :returns: The :class:`UbuntuRepositoryActionResult`.
        """
        enabled_path = self._enabled_path(name=name)
        disabled_path = self._disabled_path(name=name)

        command = ["mv", disabled_path, enabled_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuRepositoryActionStatus.DRY_RUN,
                name=name,
                path=enabled_path,
                command=command,
            )
            return dry

        if self._path_exists(path=enabled_path):
            skipped = self._build_result(
                status=UbuntuRepositoryActionStatus.SKIPPED,
                name=name,
                path=enabled_path,
                command=command,
                message="repository is already enabled",
            )
            return skipped

        if not self._path_exists(path=disabled_path):
            failed = self._build_result(
                status=UbuntuRepositoryActionStatus.FAILED,
                name=name,
                path=disabled_path,
                command=command,
                message="repository_not_found",
                stderr="repository_not_found",
            )
            return failed

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuRepositoryActionStatus.SUCCESS
        else:
            mapped_status = UbuntuRepositoryActionStatus.FAILED

        rtnval = UbuntuRepositoryActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            path=enabled_path,
        )
        return rtnval

    def disable_repository(
        self,
        name: str,
    ) -> UbuntuRepositoryActionResult:
        """
            Disable a repository by renaming ``<name>.list`` to
            ``<name>.list.disabled``.

            Skipped when ``<name>.list.disabled`` already exists.  Returns
            FAILED when ``<name>.list`` does not exist.

            :param name: The repository name (filename stem).

            :returns: The :class:`UbuntuRepositoryActionResult`.
        """
        enabled_path = self._enabled_path(name=name)
        disabled_path = self._disabled_path(name=name)

        command = ["mv", enabled_path, disabled_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuRepositoryActionStatus.DRY_RUN,
                name=name,
                path=disabled_path,
                command=command,
            )
            return dry

        if self._path_exists(path=disabled_path):
            skipped = self._build_result(
                status=UbuntuRepositoryActionStatus.SKIPPED,
                name=name,
                path=disabled_path,
                command=command,
                message="repository is already disabled",
            )
            return skipped

        if not self._path_exists(path=enabled_path):
            failed = self._build_result(
                status=UbuntuRepositoryActionStatus.FAILED,
                name=name,
                path=enabled_path,
                command=command,
                message="repository_not_found",
                stderr="repository_not_found",
            )
            return failed

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuRepositoryActionStatus.SUCCESS
        else:
            mapped_status = UbuntuRepositoryActionStatus.FAILED

        rtnval = UbuntuRepositoryActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            path=disabled_path,
        )
        return rtnval

    def refresh_repositories(self) -> UbuntuRepositoryActionResult:
        """
            Refresh apt metadata via ``apt-get update``.  Never skips.

            :returns: The :class:`UbuntuRepositoryActionResult`.
        """
        command = [_APT_GET, "update"]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuRepositoryActionStatus.DRY_RUN,
                name=None,
                path=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuRepositoryActionStatus.SUCCESS
        else:
            mapped_status = UbuntuRepositoryActionStatus.FAILED

        rtnval = UbuntuRepositoryActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=None,
            path=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_repositories(self) -> list[UbuntuRepositoryInfo]:
        """
            Enumerate apt sources.

            Reads every entry under :attr:`UbuntuRepositoryManagerConfig.
            sources_list_d` whose name ends in ``.list`` or
            ``.list.disabled``, plus the system-wide
            :attr:`main_sources_list` if it exists.  Each file is parsed
            for one-line ``deb`` / ``deb-src`` entries.  Reads never
            elevate.

            :returns: One :class:`UbuntuRepositoryInfo` per parsed source
                      file.
        """
        rtnval: list[UbuntuRepositoryInfo] = []

        ls_command = ["ls", "-1", self._config.sources_list_d]
        ls_result = self._runner.run(list(ls_command), elevated=False)

        # An exit code other than zero is treated as "directory absent" and
        # leaves the listing for sources.list.d empty.  We do not raise
        # because ``ls`` is informational here.
        if ls_result.exit_code == 0:
            for raw_entry in ls_result.stdout.splitlines():
                entry = raw_entry.strip()
                if len(entry) == 0:
                    continue
                if not (entry.endswith(".list") or entry.endswith(".list.disabled")):
                    continue
                full_path = "{}/{}".format(self._config.sources_list_d, entry)
                info = self._parse_source_file_at_path(
                    path=full_path,
                    filename=entry,
                )
                if info is not None:
                    rtnval.append(info)

        # Append the system-wide /etc/apt/sources.list when present.
        main_path = self._config.main_sources_list
        if self._path_exists(path=main_path):
            info = self._parse_source_file_at_path(
                path=main_path,
                filename="sources.list",
            )
            if info is not None:
                rtnval.append(info)

        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _enabled_path(self, *, name: str) -> str:
        """
            Build the ``.list`` path for a repository ``name``.

            :param name: The repository name (filename stem).

            :returns: The absolute path
                      ``<sources_list_d>/<name>.list``.
        """
        rtnval = "{}/{}.list".format(self._config.sources_list_d, name)
        return rtnval

    def _disabled_path(self, *, name: str) -> str:
        """
            Build the ``.list.disabled`` path for a repository ``name``.

            :param name: The repository name (filename stem).

            :returns: The absolute path
                      ``<sources_list_d>/<name>.list.disabled``.
        """
        rtnval = "{}/{}.list.disabled".format(
            self._config.sources_list_d, name,
        )
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read the file at ``path`` via ``cat``.

            :param path: The absolute path to read.

            :returns: The file's contents on success; ``None`` when ``cat``
                      reports a non-zero exit code (i.e. the path is
                      missing).
        """
        command = ["cat", path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _path_exists(self, *, path: str) -> bool:
        """
            Probe a path's existence via ``test -e``.

            :param path: The absolute path to probe.

            :returns: True when ``test -e`` exits zero; False otherwise.
        """
        command = ["test", "-e", path]
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuRepositoryActionStatus,
        name: Optional[str],
        path: Optional[str],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuRepositoryActionResult:
        """
            Construct an :class:`UbuntuRepositoryActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param name: The repository name to echo onto the result.
            :param path: The file path to echo onto the result.
            :param command: The would-have-been argv, copied for capture.
            :param message: Human-readable detail.
            :param stderr: Synthetic stderr payload.

            :returns: The :class:`UbuntuRepositoryActionResult`.
        """
        rtnval = UbuntuRepositoryActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            path=path,
            message=message,
            stderr=stderr,
        )
        return rtnval

    def _parse_source_file_at_path(
        self,
        *,
        path: str,
        filename: str,
    ) -> Optional[UbuntuRepositoryInfo]:
        """
            Read and parse a single source file into an
            :class:`UbuntuRepositoryInfo`.

            :param path: The absolute path to the file.
            :param filename: The trailing filename (used to derive
                             ``name`` and ``enabled``).

            :returns: The :class:`UbuntuRepositoryInfo`, or ``None`` when
                      the file cannot be read.
        """
        contents = self._read_file(path=path)
        if contents is None:
            rtnval: Optional[UbuntuRepositoryInfo] = None
            return rtnval

        if filename.endswith(".list.disabled"):
            stem = filename[: -len(".list.disabled")]
            enabled = False
        elif filename.endswith(".list"):
            stem = filename[: -len(".list")]
            enabled = True
        elif filename == "sources.list":
            stem = "sources.list"
            enabled = True
        else:
            stem = filename
            enabled = True

        parsed = _parse_one_line_sources(content=contents)

        rtnval = UbuntuRepositoryInfo(
            name=stem,
            path=path,
            enabled=enabled,
            uri=parsed["uri"],
            suites=parsed["suites"],
            components=parsed["components"],
            types=parsed["types"],
        )
        return rtnval


def _render_one_line_sources(*, spec: UbuntuRepositorySpec) -> str:
    """
        Render an :class:`UbuntuRepositorySpec` into a one-line ``deb``
        formatted source file body.

        Output contains one line per ``(type, suite)`` cross product and is
        always terminated by a newline.

        :param spec: The :class:`UbuntuRepositorySpec` to render.

        :returns: The full file body as a single string.
    """
    options: list[str] = []
    if spec.architectures is not None and len(spec.architectures) > 0:
        options.append("arch={}".format(",".join(spec.architectures)))
    if spec.signed_by is not None and len(spec.signed_by) > 0:
        options.append("signed-by={}".format(spec.signed_by))

    if len(options) > 0:
        options_token = "[{}] ".format(" ".join(options))
    else:
        options_token = ""

    components_token = " ".join(spec.components)

    lines: list[str] = []
    for source_type in spec.types:
        for suite in spec.suites:
            line = "{} {}{} {} {}".format(
                source_type,
                options_token,
                spec.uri,
                suite,
                components_token,
            ).rstrip()
            lines.append(line)

    rtnval = "\n".join(lines) + "\n"
    return rtnval


def _parse_one_line_sources(*, content: str) -> dict:
    """
        Parse a one-line apt sources file body.

        Walks every non-comment line and accumulates the union of types,
        suites, components, and the first observed URI.

        :param content: Raw file body.

        :returns: A dict with keys ``uri`` (Optional[str]), ``suites``
                  (list[str]), ``components`` (list[str]), ``types``
                  (list[str]).
    """
    uri: Optional[str] = None
    suites: list[str] = []
    components: list[str] = []
    types: list[str] = []

    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue

        # Use shlex to safely split while honouring possible quoted tokens.
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            # A malformed line (e.g. unterminated quote) is silently skipped.
            continue

        if len(tokens) == 0:
            continue

        head = tokens[0]
        if head != "deb" and head != "deb-src":
            continue

        # Drop the [options] block.  shlex preserves the brackets but does
        # not balance them, so a multi-token form such as
        # ``[arch=amd64 signed-by=/etc/apt/keyrings/x.gpg]`` arrives as
        # several tokens.  We consume until we see the closing ']'.
        cursor = 1
        if cursor < len(tokens) and tokens[cursor].startswith("["):
            while cursor < len(tokens):
                current = tokens[cursor]
                cursor += 1
                if current.endswith("]"):
                    break

        if cursor >= len(tokens):
            continue
        line_uri = tokens[cursor]
        cursor += 1

        if cursor >= len(tokens):
            continue
        line_suite = tokens[cursor]
        cursor += 1

        line_components = tokens[cursor:]

        if head not in types:
            types.append(head)
        if uri is None:
            uri = line_uri
        if line_suite not in suites:
            suites.append(line_suite)
        for comp in line_components:
            if comp not in components:
                components.append(comp)

    rtnval = {
        "uri": uri,
        "suites": suites,
        "components": components,
        "types": types,
    }
    return rtnval
