"""
    Ubuntu package-management feature.

    Implements the cross-OS package-management contract for Ubuntu using
    ``apt-get`` for write verbs and ``dpkg`` / ``dpkg-query`` / ``apt-cache
    policy`` for reads.  Every verb is dispatched through an
    :class:`ICommandRunner` so the manager works identically under local and
    SSH execution.

    Elevation policy:
        Write verbs honour ``config.elevated`` and pass it to the runner.
        Read verbs (``is_installed``, ``get_package_info``, ``list_installed``)
        and idempotency-probe commands never elevate.

    Non-interactive execution:
        Write verbs inject ``DEBIAN_FRONTEND=noninteractive`` into the runner's
        env when ``noninteractive_env`` is True (the default), so apt-get does
        not block on debconf prompts under unattended execution.
"""

import os

from dataclasses import dataclass
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_APT_GET = "apt-get"
_APT_CACHE = "apt-cache"
_DPKG = "dpkg"
_DPKG_QUERY = "dpkg-query"

_DPKG_QUERY_FORMAT = "${Package}\t${Version}\t${Architecture}\t${Status}\n"

_INSTALLED_STATUS_PREFIX = "install ok installed"


UbuntuPackageActionStatus = ActionStatus


@dataclass
class UbuntuPackageManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuPackageManager`.  Inherits ``dry_run``
        and ``elevated`` from :class:`FeatureConfig`.

        :param apt_options: Optional list of additional flags appended to every
                            ``apt-get`` write argv.  Defaults to ``None``,
                            which is treated as no extra flags.
        :param noninteractive_env: When True (the default), the runner's env
                                   for write verbs includes
                                   ``DEBIAN_FRONTEND=noninteractive`` so
                                   ``apt-get`` will not block on debconf
                                   prompts under unattended execution.
    """
    apt_options: Optional[list[str]] = None
    noninteractive_env: bool = True


@dataclass
class UbuntuPackageActionResult(FeatureActionResult):
    """
        Result of a package-management write operation.

        :param name: The package name targeted, or ``None`` for ``upgrade_all``
                     and ``refresh_metadata``.
        :param version: The version targeted (or resolved), when known.
        :param message: Optional human-readable detail for ``SKIPPED`` or
                        ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    version: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuPackageInfo:
    """
        Information about an installed package as reported by ``dpkg``.

        :param name: Package name.
        :param version: Installed version string.
        :param architecture: Architecture identifier (e.g. ``amd64``).
        :param status: The ``Status`` line from ``dpkg -s`` /
                       ``dpkg-query``.
        :param description: Short description, or ``None`` when not
                            available.
        :param maintainer: Package maintainer, or ``None`` when not
                           available.
        :param source: ``Source`` field from ``dpkg -s``, or ``None`` when
                       not present.
    """
    name: str
    version: str
    architecture: str
    status: str
    description: Optional[str] = None
    maintainer: Optional[str] = None
    source: Optional[str] = None


class UbuntuPackageQueryError(Exception):
    """
        Raised when a package read fails for a reason other than the package
        being absent.
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

        err_msg_lines = ["Package query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuPackageManager(BaseFeatureManager):
    """
        Ubuntu apt/dpkg package manager.  Every verb is dispatched through the
        injected :class:`ICommandRunner`.
    """

    result_class = UbuntuPackageActionResult
    status_class = UbuntuPackageActionStatus
    feature_name = "package-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def install_package(
        self,
        name: str,
        version: Optional[str] = None,
    ) -> UbuntuPackageActionResult:
        """
            Install a package, optionally pinned to a specific version.

            Skipped when the package is already installed.  When ``version``
            is provided, only skipped when the installed version equals the
            requested version.

            :param name: The package name.
            :param version: Optional exact version to install
                            (``apt-get install -y <name>=<version>``).

            :returns: The :class:`UbuntuPackageActionResult`.
        """
        target = self._format_install_target(name=name, version=version)
        command = self._build_apt_get_argv(verb_args=["install", "-y", target])

        if self._config.dry_run:
            dry_result = self._build_result(
                status=UbuntuPackageActionStatus.DRY_RUN,
                name=name,
                version=version,
                command=command,
            )
            return dry_result

        existing_info = self.get_package_info(name=name)
        if existing_info is not None:
            if version is None or existing_info.version == version:
                skipped = self._build_result(
                    status=UbuntuPackageActionStatus.SKIPPED,
                    name=name,
                    version=existing_info.version,
                    command=command,
                    message="package is already installed",
                )
                return skipped

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            version=version,
            operation="install_package",
        )
        return rtnval

    def remove_package(self, name: str) -> UbuntuPackageActionResult:
        """
            Remove an installed package.  Skipped when the package is not
            installed.

            :param name: The package name.

            :returns: The :class:`UbuntuPackageActionResult`.
        """
        command = self._build_apt_get_argv(verb_args=["remove", "-y", name])

        if self._config.dry_run:
            dry_result = self._build_result(
                status=UbuntuPackageActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry_result

        if not self.is_installed(name=name):
            skipped = self._build_result(
                status=UbuntuPackageActionStatus.SKIPPED,
                name=name,
                command=command,
                message="package is not installed",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            operation="remove_package",
        )
        return rtnval

    def upgrade_package(self, name: str) -> UbuntuPackageActionResult:
        """
            Upgrade a single package to its latest available version.  Skipped
            when the installed version is already at the candidate.

            :param name: The package name.

            :returns: The :class:`UbuntuPackageActionResult`.
        """
        command = self._build_apt_get_argv(
            verb_args=["install", "-y", "--only-upgrade", name],
        )

        if self._config.dry_run:
            dry_result = self._build_result(
                status=UbuntuPackageActionStatus.DRY_RUN,
                name=name,
                command=command,
            )
            return dry_result

        installed_version, candidate_version = self._query_apt_cache_policy(
            name=name,
        )
        if installed_version is None:
            skipped = self._build_result(
                status=UbuntuPackageActionStatus.SKIPPED,
                name=name,
                command=command,
                message="package is not installed; nothing to upgrade",
            )
            return skipped

        if candidate_version is not None and installed_version == candidate_version:
            skipped = self._build_result(
                status=UbuntuPackageActionStatus.SKIPPED,
                name=name,
                version=installed_version,
                command=command,
                message="package is already at the latest available version",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            name=name,
            operation="upgrade_package",
        )
        return rtnval

    def upgrade_all(self) -> UbuntuPackageActionResult:
        """
            Upgrade every installed package to its latest available version.
            Never skips.

            :returns: The :class:`UbuntuPackageActionResult`.
        """
        command = self._build_apt_get_argv(verb_args=["upgrade", "-y"])
        rtnval = self._dispatch_write(
            command=command,
            name=None,
            operation="upgrade_all",
        )
        return rtnval

    def refresh_metadata(self) -> UbuntuPackageActionResult:
        """
            Refresh the apt package index (``apt-get update``).  Never skips.

            :returns: The :class:`UbuntuPackageActionResult`.
        """
        command = self._build_apt_get_argv(verb_args=["update"])
        rtnval = self._dispatch_write(
            command=command,
            name=None,
            operation="refresh_metadata",
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def is_installed(self, name: str) -> bool:
        """
            Return whether a package is currently installed.

            Runs ``dpkg -l <name>`` without elevation and inspects the first
            two-byte status column for the ``ii`` prefix.

            :param name: The package name.

            :returns: True when the first matching row's status begins with
                      ``ii``; False otherwise (including when ``dpkg`` exits
                      non-zero because the package is unknown).
        """
        command = [_DPKG, "-l", name]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        rtnval = self._parse_dpkg_l_installed(stdout=run_result.stdout)
        return rtnval

    def get_package_info(self, name: str) -> Optional[UbuntuPackageInfo]:
        """
            Query installed-package metadata via ``dpkg -s``.

            :param name: The package name.

            :returns: A :class:`UbuntuPackageInfo` when the package is
                      installed; ``None`` when the package is not installed
                      and ``dpkg`` reports the canonical "not installed"
                      message.
            :raises UbuntuPackageQueryError: When ``dpkg -s`` fails for any
                                             other reason.
        """
        command = [_DPKG, "-s", name]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            stdout_lower = run_result.stdout.lower()
            stderr_lower = run_result.stderr.lower()
            is_missing = (
                "not installed" in stdout_lower
                or "not installed" in stderr_lower
            )
            if is_missing:
                rtnval = None
                return rtnval
            raise UbuntuPackageQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        info = self._parse_dpkg_s_output(stdout=run_result.stdout)
        if info is None:
            rtnval = None
            return rtnval

        if not info.status.startswith(_INSTALLED_STATUS_PREFIX):
            rtnval = None
            return rtnval

        rtnval = info
        return rtnval

    def list_installed(self) -> list[UbuntuPackageInfo]:
        """
            List every installed package as reported by ``dpkg-query -W``.

            Filters to rows whose ``Status`` begins with
            ``install ok installed``.

            :returns: One :class:`UbuntuPackageInfo` per installed package.
            :raises UbuntuPackageQueryError: When the runner reports a
                                             non-zero exit code.
        """
        command = [_DPKG_QUERY, "-W", "-f=" + _DPKG_QUERY_FORMAT]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code != 0:
            raise UbuntuPackageQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = self._parse_dpkg_query_output(stdout=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _format_install_target(
        self,
        *,
        name: str,
        version: Optional[str],
    ) -> str:
        """
            Build the ``apt-get install`` target token.

            :param name: The package name.
            :param version: Optional version pin.

            :returns: ``name`` or ``name=version``.
        """
        if version is None:
            rtnval = name
            return rtnval
        rtnval = "{}={}".format(name, version)
        return rtnval

    def _build_apt_get_argv(self, *, verb_args: list[str]) -> list[str]:
        """
            Construct the full ``apt-get`` argv, appending any
            ``apt_options`` tail from the config.

            :param verb_args: The verb-specific portion of the argv (e.g.
                              ``["install", "-y", "nginx"]``).

            :returns: The full argv list starting with ``apt-get``.
        """
        rtnval: list[str] = [_APT_GET]
        rtnval.extend(verb_args)

        if self._config.apt_options is not None:
            if len(self._config.apt_options) > 0:
                rtnval.extend(self._config.apt_options)

        return rtnval

    def _dispatch_write(
        self,
        *,
        command: list[str],
        name: Optional[str],
        version: Optional[str] = None,
        operation: str,
    ) -> UbuntuPackageActionResult:
        """
            Dispatch a write command, injecting the noninteractive env when
            configured and short-circuiting on dry-run via :meth:`_dispatch`.

            :param command: The full argv.
            :param name: The package name to echo onto the result.
            :param version: The version to echo onto the result.
            :param operation: Reserved for future auto-reporting.

            :returns: The :class:`UbuntuPackageActionResult`.
        """
        env_dict = self._build_env()

        rtnval = self._dispatch_with_env(
            command=command,
            env=env_dict,
            detail=name,
            operation=operation,
            name=name,
            version=version,
        )
        return rtnval

    def _build_env(self) -> Optional[dict]:
        """
            Build the env dict passed to the runner for write verbs.

            :returns: A dict containing ``DEBIAN_FRONTEND=noninteractive`` when
                      ``noninteractive_env`` is True; ``None`` otherwise so
                      the runner inherits its ambient environment.
        """
        if self._config.noninteractive_env:
            rtnval = {"DEBIAN_FRONTEND": "noninteractive"}
            return rtnval
        rtnval = None
        return rtnval

    def _dispatch_with_env(
        self,
        *,
        command: list[str],
        env: Optional[dict],
        detail: Optional[str],
        operation: str,
        name: Optional[str],
        version: Optional[str],
    ) -> UbuntuPackageActionResult:
        """
            Dispatch a write through the runner with an explicit env dict.

            The :class:`BaseFeatureManager._dispatch` helper does not currently
            forward ``env``, so this manager invokes the runner directly while
            preserving dry-run handling and SUCCESS/FAILED status mapping.

            :param command: The full argv.
            :param env: The environment forwarded to the runner.
            :param detail: Human-readable detail for the result.
            :param operation: Reserved for future auto-reporting.
            :param name: Package name to echo on the result.
            :param version: Version to echo on the result.

            :returns: The :class:`UbuntuPackageActionResult`.
        """
        command_copy = list(command)

        # Dry-run is normally handled inside callers, but this helper guards
        # the path defensively in case a future caller forgets.
        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuPackageActionStatus.DRY_RUN,
                name=name,
                version=version,
                command=command_copy,
            )
            return dry

        run_result = self._runner.run(
            command_copy,
            elevated=self._config.elevated,
            env=env,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuPackageActionStatus.SUCCESS
        else:
            mapped_status = UbuntuPackageActionStatus.FAILED

        rtnval = UbuntuPackageActionResult(
            status=mapped_status,
            detail=detail,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=command_copy,
            name=name,
            version=version,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuPackageActionStatus,
        name: Optional[str],
        version: Optional[str] = None,
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuPackageActionResult:
        """
            Construct an :class:`UbuntuPackageActionResult` without invoking
            the runner.

            :param status: The outcome status.
            :param name: The package name to echo onto the result.
            :param version: The version to echo onto the result.
            :param command: The would-have-been argv, copied for capture.
            :param message: Human-readable detail.
            :param stderr: Synthetic stderr payload.

            :returns: The :class:`UbuntuPackageActionResult`.
        """
        rtnval = UbuntuPackageActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            version=version,
            message=message,
            stderr=stderr,
        )
        return rtnval

    def _query_apt_cache_policy(
        self,
        *,
        name: str,
    ) -> tuple:
        """
            Run ``apt-cache policy <name>`` and parse the ``Installed`` /
            ``Candidate`` fields.

            :param name: The package name.

            :returns: A 2-tuple ``(installed_version, candidate_version)``.
                      Either entry is ``None`` when the field is missing or
                      reads ``(none)``.
        """
        command = [_APT_CACHE, "policy", name]
        run_result = self._runner.run(list(command), elevated=False)

        installed: Optional[str] = None
        candidate: Optional[str] = None

        if run_result.exit_code != 0:
            rtnval = (installed, candidate)
            return rtnval

        for raw_line in run_result.stdout.splitlines():
            stripped = raw_line.strip()
            if len(stripped) == 0:
                continue
            if stripped.startswith("Installed:"):
                _, _, value = stripped.partition(":")
                normalised = value.strip()
                if normalised == "(none)":
                    installed = None
                elif len(normalised) == 0:
                    installed = None
                else:
                    installed = normalised
            elif stripped.startswith("Candidate:"):
                _, _, value = stripped.partition(":")
                normalised = value.strip()
                if normalised == "(none)":
                    candidate = None
                elif len(normalised) == 0:
                    candidate = None
                else:
                    candidate = normalised

        rtnval = (installed, candidate)
        return rtnval

    def _parse_dpkg_l_installed(self, *, stdout: str) -> bool:
        """
            Inspect ``dpkg -l`` output for a row with the ``ii`` status prefix.

            :param stdout: Raw stdout from ``dpkg -l``.

            :returns: True when at least one data row begins with ``ii``;
                      False otherwise.
        """
        rtnval = False
        for raw_line in stdout.splitlines():
            line = raw_line.rstrip()
            if len(line) == 0:
                continue
            if line.startswith("Desired="):
                continue
            if line.startswith("|"):
                continue
            if line.startswith("+"):
                continue
            # The status block is the first whitespace-delimited token.  We
            # accept rows that begin with two non-space status bytes.
            head = line[:2]
            if head == "ii":
                rtnval = True
                return rtnval
        return rtnval

    def _parse_dpkg_s_output(
        self,
        *,
        stdout: str,
    ) -> Optional[UbuntuPackageInfo]:
        """
            Parse ``dpkg -s`` output into an :class:`UbuntuPackageInfo`.

            :param stdout: Raw stdout from ``dpkg -s``.

            :returns: The :class:`UbuntuPackageInfo`, or ``None`` when the
                      required ``Package`` / ``Version`` / ``Architecture``
                      fields are missing.
        """
        fields: dict = {}
        last_key: Optional[str] = None

        for raw_line in stdout.splitlines():
            if len(raw_line) == 0:
                last_key = None
                continue
            # Continuation lines begin with whitespace; we keep only the
            # first line of each multi-line value (description, etc.) so the
            # info struct stays simple.
            if raw_line[0] == " " or raw_line[0] == "\t":
                last_key = None
                continue
            if ":" not in raw_line:
                last_key = None
                continue
            key, _, value = raw_line.partition(":")
            normalised_key = key.strip()
            normalised_value = value.strip()
            fields[normalised_key] = normalised_value
            last_key = normalised_key

        package_name = fields.get("Package", "")
        version_value = fields.get("Version", "")
        architecture_value = fields.get("Architecture", "")
        status_value = fields.get("Status", "")
        description_value = fields.get("Description", "")
        maintainer_value = fields.get("Maintainer", "")
        source_value = fields.get("Source", "")

        if len(package_name) == 0:
            rtnval = None
            return rtnval
        if len(version_value) == 0:
            rtnval = None
            return rtnval
        if len(architecture_value) == 0:
            rtnval = None
            return rtnval

        if len(description_value) == 0:
            description_final: Optional[str] = None
        else:
            description_final = description_value

        if len(maintainer_value) == 0:
            maintainer_final: Optional[str] = None
        else:
            maintainer_final = maintainer_value

        if len(source_value) == 0:
            source_final: Optional[str] = None
        else:
            source_final = source_value

        rtnval = UbuntuPackageInfo(
            name=package_name,
            version=version_value,
            architecture=architecture_value,
            status=status_value,
            description=description_final,
            maintainer=maintainer_final,
            source=source_final,
        )
        return rtnval

    def _parse_dpkg_query_output(
        self,
        *,
        stdout: str,
    ) -> list[UbuntuPackageInfo]:
        """
            Parse ``dpkg-query -W`` tab-separated output into
            :class:`UbuntuPackageInfo` rows, filtering to those whose status
            begins with ``install ok installed``.

            :param stdout: Raw stdout from ``dpkg-query -W``.

            :returns: One :class:`UbuntuPackageInfo` per installed row.
        """
        rtnval: list[UbuntuPackageInfo] = []
        for raw_line in stdout.splitlines():
            line = raw_line.rstrip("\r\n")
            if len(line) == 0:
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                continue

            package_name = parts[0]
            version_value = parts[1]
            architecture_value = parts[2]
            status_value = parts[3]

            if not status_value.startswith(_INSTALLED_STATUS_PREFIX):
                continue

            info = UbuntuPackageInfo(
                name=package_name,
                version=version_value,
                architecture=architecture_value,
                status=status_value,
                description=None,
                maintainer=None,
                source=None,
            )
            rtnval.append(info)

        return rtnval
