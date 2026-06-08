"""
    Ubuntu authentication-management feature.

    Implements the cross-OS authentication-management contract for
    Ubuntu / Debian-derived hosts using ``pam-auth-update`` for
    method-level toggles, ``cat`` for PAM service-file reads, and
    ``tee`` for PAM service-file writes.  Every command is dispatched
    through the injected :class:`ICommandRunner`.

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | Ubuntu tool                       |
    +=============================+===================================+
    | ``get_auth_config``         | ``cat /etc/pam.d/<svc>`` + parse  |
    +-----------------------------+-----------------------------------+
    | ``set_auth_config``         | ``cat`` + parse + render +        |
    |                             | ``tee /etc/pam.d/<svc>``          |
    +-----------------------------+-----------------------------------+
    | ``list_auth_methods``       | ``pam-auth-update --list``        |
    |                             | merged with ``ls -1`` of the      |
    |                             | configs dir + ``cat`` of each     |
    |                             | profile file                      |
    +-----------------------------+-----------------------------------+
    | ``enable_method``           | ``pam-auth-update --enable        |
    |                             | <name>``                          |
    +-----------------------------+-----------------------------------+
    | ``disable_method``          | ``pam-auth-update --disable       |
    |                             | <name>``                          |
    +-----------------------------+-----------------------------------+
    | ``validate_auth_config``    | ``cat`` + internal parser (PAM    |
    |                             | has no canonical validator)       |
    +-----------------------------+-----------------------------------+

    File I/O policy:
        Reads use ``cat`` with ``elevated=False``; writes use
        ``tee`` with ``elevated=True`` and the new content piped on
        ``stdin``.  This module does NOT consume an
        :class:`IFileOperator`.

    Idempotency model:
        Every write reads the current file content, renders the
        desired content, and compares verbatim.  When equal the verb
        returns ``SKIPPED`` without invoking the runner for the
        write.

    Elevation policy:
        Write verbs (``set_auth_config``, ``enable_method``,
        ``disable_method``) honour ``config.elevated``.  Reads
        (``get_auth_config``, ``list_auth_methods``,
        ``validate_auth_config``) never elevate.

    Stack-syntax caveat:
        The PAM stack parser strips comments and blank lines.  A
        round-trip through ``set_auth_config`` will therefore drop
        any comments from the source file.  The ``UbuntuAuthConfig``
        the caller supplies is treated as the single source of
        truth.
"""

import os

from dataclasses import dataclass, field
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

_CAT = "cat"
_TEE = "tee"
_LS = "ls"
_PAM_AUTH_UPDATE = "pam-auth-update"

_DEFAULT_PAM_DIR = "/etc/pam.d"
_DEFAULT_PAM_CONFIGS_DIR = "/usr/share/pam-configs"

_AUTH_KIND_LOGIN = "LOGIN"
_AUTH_KIND_SUDO = "SUDO"
_AUTH_KIND_SU = "SU"
_AUTH_KIND_SSH = "SSH"
_AUTH_KIND_CRON = "CRON"
_AUTH_KIND_OTHER = "OTHER"

_VALID_MODULE_TYPES = (
    "auth",
    "account",
    "password",
    "session",
    "@include",
    "-auth",
    "-account",
    "-password",
    "-session",
)


UbuntuAuthActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Subsystem typing
# ----------------------------------------------------------------------


@dataclass
class UbuntuAuthSubsystem:
    """
        Discriminated wrapper around a PAM service-file basename.

        :param kind: One of ``LOGIN`` / ``SUDO`` / ``SU`` / ``SSH`` /
                     ``CRON`` / ``OTHER``.
        :param name: The service-file basename.  When ``kind`` is
                     ``OTHER`` callers MUST supply this; otherwise
                     it is fixed by the kind.
    """
    kind: str
    name: str


UBUNTU_AUTH_LOGIN = UbuntuAuthSubsystem(kind=_AUTH_KIND_LOGIN, name="login")
UBUNTU_AUTH_SUDO = UbuntuAuthSubsystem(kind=_AUTH_KIND_SUDO, name="sudo")
UBUNTU_AUTH_SU = UbuntuAuthSubsystem(kind=_AUTH_KIND_SU, name="su")
UBUNTU_AUTH_SSH = UbuntuAuthSubsystem(kind=_AUTH_KIND_SSH, name="sshd")
UBUNTU_AUTH_CRON = UbuntuAuthSubsystem(kind=_AUTH_KIND_CRON, name="cron")


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class UbuntuAuthenticationManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuAuthenticationManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param pam_dir: Absolute path to the PAM service-file
                        directory.  Default ``/etc/pam.d``.
        :param pam_configs_dir: Absolute path to the
                                ``pam-auth-update`` profile
                                registry directory.  Default
                                ``/usr/share/pam-configs``.
        :param pam_auth_update: Tool name (or absolute path) for
                                the ``pam-auth-update`` binary.
                                Default ``"pam-auth-update"``.
    """
    pam_dir: str = _DEFAULT_PAM_DIR
    pam_configs_dir: str = _DEFAULT_PAM_CONFIGS_DIR
    pam_auth_update: str = _PAM_AUTH_UPDATE


@dataclass
class UbuntuAuthEntry:
    """
        A single PAM stack entry.

        :param module_type: ``auth``, ``account``, ``password``,
                            ``session``, or ``@include``.  May carry
                            a leading ``-`` (e.g. ``-session``) to
                            mark optional-on-missing-module per PAM
                            grammar.
        :param control: PAM control text.  May be a simple keyword
                        (``required``, ``sufficient``, ``optional``,
                        ``requisite``) or a bracketed expression
                        (``[success=ok default=ignore]``).  For
                        ``@include`` entries this is the empty
                        string.
        :param module_path: The PAM module reference
                            (``pam_unix.so``).  For ``@include``
                            entries this carries the included
                            service-file basename.
        :param args: Remaining tokens (option flags / key=value
                     pairs).
    """
    module_type: str
    control: str
    module_path: str
    args: list = field(default_factory=list)


@dataclass
class UbuntuAuthConfig:
    """
        A PAM stack config for one subsystem.

        :param subsystem: The PAM service-file basename.
        :param entries: PAM stack entries in declaration order.
                        Order is semantically significant in PAM and
                        is preserved verbatim by the renderer.
    """
    subsystem: str
    entries: list = field(default_factory=list)


@dataclass
class UbuntuAuthMethodInfo:
    """
        A registered ``pam-auth-update`` profile.

        :param name: Profile name (matches the basename under
                     ``/usr/share/pam-configs/``).
        :param enabled: Whether the profile is currently enabled
                        in the live ``pam-auth-update --list``
                        output.
        :param description: Human-readable description from the
                            profile file's ``Name:`` field, if
                            available.
        :param module_path: Best-effort module reference parsed
                            from the profile file's ``Auth:`` /
                            ``Account:`` body, if available.
    """
    name: str
    enabled: bool
    description: Optional[str] = None
    module_path: Optional[str] = None


@dataclass
class UbuntuAuthValidationResult:
    """
        Outcome of :meth:`UbuntuAuthenticationManager.validate_auth_config`.

        :param valid: True when no structural errors were detected.
        :param errors: List of error strings.  Each error is a
                       single line summarising the issue (e.g.
                       ``"line 3: unknown module_type 'foo'"``).
        :param warnings: List of warning strings.  Warnings do not
                         flip ``valid`` to False.
    """
    valid: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


@dataclass
class UbuntuAuthActionResult(FeatureActionResult):
    """
        Result of an authentication-management write operation.

        :param subsystem: The subsystem (PAM service-file basename)
                          targeted, when applicable.
        :param method: The method (``pam-auth-update`` profile name)
                       targeted, when applicable.
        :param message: Optional human-readable detail attached to
                        ``SKIPPED`` / ``FAILED`` outcomes.
    """
    subsystem: Optional[str] = None
    method: Optional[str] = None
    message: Optional[str] = None


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class UbuntuAuthenticationQueryError(Exception):
    """
        Raised when an authentication-management read fails for a
        reason other than a recognised "not found" surface.
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

        err_msg_lines = ["Authentication query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuAuthenticationConfigError(Exception):
    """
        Raised when an :class:`UbuntuAuthConfig` would overwrite a
        managed file without explicit opt-in, or when a supplied
        ``UbuntuAuthConfig`` is malformed for the targeted
        subsystem.
    """

    def __init__(
        self,
        *,
        subsystem: str,
        detail: str,
    ) -> None:
        """
            :param subsystem: The subsystem the config targets.
            :param detail: Human-readable description.
        """
        self.subsystem = subsystem
        self.detail = detail

        err_msg_lines = ["Authentication config rejected."]
        err_msg_lines.append("    SUBSYSTEM: {}".format(subsystem))
        err_msg_lines.append("    DETAIL: {}".format(detail))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuAuthenticationSubsystemNotFoundError(Exception):
    """
        Raised when an unknown PAM subsystem (or unregistered
        ``pam-auth-update`` profile name) is referenced.
    """

    def __init__(
        self,
        *,
        subsystem: str,
        detail: str,
    ) -> None:
        """
            :param subsystem: The subsystem or method name.
            :param detail: Human-readable description.
        """
        self.subsystem = subsystem
        self.detail = detail

        err_msg_lines = ["Authentication subsystem not found."]
        err_msg_lines.append("    SUBSYSTEM: {}".format(subsystem))
        err_msg_lines.append("    DETAIL: {}".format(detail))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class UbuntuAuthenticationManager(BaseFeatureManager):
    """
        Ubuntu authentication-management manager.  Reads, writes, and
        validates per-service PAM stacks under ``/etc/pam.d/`` and
        ``pam-auth-update`` profile selection through the injected
        :class:`ICommandRunner`.
    """

    result_class = UbuntuAuthActionResult
    status_class = UbuntuAuthActionStatus
    feature_name = "authentication-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_auth_config(self, subsystem) -> UbuntuAuthConfig:
        """
            Read a PAM subsystem's stack and return it as an
            :class:`UbuntuAuthConfig`.

            :param subsystem: An :class:`UbuntuAuthSubsystem`,
                              one of the ``UBUNTU_AUTH_*`` module
                              constants, or a raw string with the
                              service-file basename.

            :returns: The :class:`UbuntuAuthConfig`.
            :raises UbuntuAuthenticationSubsystemNotFoundError:
                When the service file does not exist.
            :raises UbuntuAuthenticationQueryError:
                When the read fails for any other reason.
        """
        basename = self._resolve_subsystem(subsystem=subsystem)
        path = self._pam_path(basename=basename)
        command = [_CAT, path]

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_text = run_result.stderr or ""
            stderr_lower = stderr_text.lower()
            if "no such file" in stderr_lower or "not found" in stderr_lower:
                raise UbuntuAuthenticationSubsystemNotFoundError(
                    subsystem=basename,
                    detail="PAM service file does not exist: {}".format(path),
                )
            raise UbuntuAuthenticationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=stderr_text,
            )

        entries = _parse_pam_stack(content=run_result.stdout)
        rtnval = UbuntuAuthConfig(
            subsystem=basename,
            entries=entries,
        )
        return rtnval

    def list_auth_methods(self) -> list:
        """
            Enumerate the registered ``pam-auth-update`` profiles
            and their enabled state.

            :returns: A ``list[UbuntuAuthMethodInfo]``.
        """
        update_command = [
            self._typed_config().pam_auth_update, "--list",
        ]
        update_result = self._runner.run(
            list(update_command), elevated=False,
        )

        live_state: dict = {}
        if update_result.exit_code == 0:
            live_state = _parse_pam_auth_update_list(
                text=update_result.stdout,
            )

        configs_dir = self._typed_config().pam_configs_dir
        ls_command = [_LS, "-1", configs_dir]
        ls_result = self._runner.run(list(ls_command), elevated=False)

        registered_names: list = []
        if ls_result.exit_code == 0:
            for raw_line in ls_result.stdout.splitlines():
                stripped = raw_line.strip()
                if len(stripped) == 0:
                    continue
                registered_names.append(stripped)

        merged: dict = {}
        for name, value in live_state.items():
            enabled_flag, description_text = value
            merged[name] = (enabled_flag, description_text, None)

        for name in registered_names:
            profile_path = "{}/{}".format(configs_dir, name)
            profile_command = [_CAT, profile_path]
            profile_result = self._runner.run(
                list(profile_command), elevated=False,
            )
            if profile_result.exit_code != 0:
                continue

            parsed_keys = _parse_pam_config_profile(
                text=profile_result.stdout,
            )
            description_text = parsed_keys.get("Name")
            module_path_text = parsed_keys.get("ModulePath")

            if name in merged:
                existing_enabled, existing_desc, _ = merged[name]
                if existing_desc is None:
                    use_desc = description_text
                else:
                    use_desc = existing_desc
                merged[name] = (
                    existing_enabled, use_desc, module_path_text,
                )
            else:
                merged[name] = (
                    False, description_text, module_path_text,
                )

        rtnval: list = []
        for name in sorted(merged.keys()):
            enabled_flag, description_text, module_path_text = merged[name]
            info = UbuntuAuthMethodInfo(
                name=name,
                enabled=enabled_flag,
                description=description_text,
                module_path=module_path_text,
            )
            rtnval.append(info)
        return rtnval

    def validate_auth_config(self, subsystem) -> UbuntuAuthValidationResult:
        """
            Validate a PAM subsystem's stack syntactically.

            Re-parses the live file and walks every entry checking
            structural invariants.  PAM has no canonical validator
            (no ``pam-test`` equivalent), so this verb's coverage
            is structural only — module-level invariants
            (e.g. ``pam_unix.so`` being loadable) are out of
            scope.

            :param subsystem: As for :meth:`get_auth_config`.

            :returns: The :class:`UbuntuAuthValidationResult`.
            :raises UbuntuAuthenticationSubsystemNotFoundError:
                When the service file does not exist.
            :raises UbuntuAuthenticationQueryError:
                When the read fails for any other reason.
        """
        basename = self._resolve_subsystem(subsystem=subsystem)
        path = self._pam_path(basename=basename)
        command = [_CAT, path]

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_text = run_result.stderr or ""
            stderr_lower = stderr_text.lower()
            if "no such file" in stderr_lower or "not found" in stderr_lower:
                raise UbuntuAuthenticationSubsystemNotFoundError(
                    subsystem=basename,
                    detail="PAM service file does not exist: {}".format(path),
                )
            raise UbuntuAuthenticationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=stderr_text,
            )

        errors: list = []
        warnings: list = []
        entries = _parse_pam_stack(content=run_result.stdout)
        non_blank_input_lines = 0
        for raw_line in run_result.stdout.splitlines():
            stripped = raw_line.strip()
            if len(stripped) == 0:
                continue
            if stripped.startswith("#"):
                continue
            non_blank_input_lines += 1

        if non_blank_input_lines == 0:
            warnings.append(
                "PAM stack has no executable lines",
            )
            rtnval = UbuntuAuthValidationResult(
                valid=True, errors=errors, warnings=warnings,
            )
            return rtnval

        for index, entry in enumerate(entries):
            line_num = index + 1
            if entry.module_type not in _VALID_MODULE_TYPES:
                errors.append(
                    "line {}: unknown module_type '{}'".format(
                        line_num, entry.module_type,
                    ),
                )
            if entry.module_type != "@include" and len(entry.control) == 0:
                errors.append(
                    "line {}: missing control token".format(line_num),
                )
            if len(entry.module_path) == 0:
                errors.append(
                    "line {}: missing module_path".format(line_num),
                )

        is_valid = len(errors) == 0
        rtnval = UbuntuAuthValidationResult(
            valid=is_valid, errors=errors, warnings=warnings,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_auth_config(
        self,
        subsystem,
        config: UbuntuAuthConfig,
    ) -> UbuntuAuthActionResult:
        """
            Replace a PAM subsystem's stack with the supplied
            config.

            Idempotent: returns ``SKIPPED`` when the rendered
            desired content equals the current file content.
            Honours dry-run.

            :param subsystem: As for :meth:`get_auth_config`.
            :param config: The desired :class:`UbuntuAuthConfig`.

            :returns: The :class:`UbuntuAuthActionResult`.
        """
        basename = self._resolve_subsystem(subsystem=subsystem)
        path = self._pam_path(basename=basename)
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        rendered_content = _render_pam_stack(entries=config.entries)

        if existing_content == rendered_content:
            skipped = self._build_result(
                status=UbuntuAuthActionStatus.SKIPPED,
                subsystem=basename,
                method=None,
                command=write_command,
                message="PAM stack already at desired content",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuAuthActionStatus.DRY_RUN,
                subsystem=basename,
                method=None,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=rendered_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            subsystem=basename,
            method=None,
        )
        return rtnval

    def enable_method(self, name: str) -> UbuntuAuthActionResult:
        """
            Enable a registered ``pam-auth-update`` profile.

            Idempotent: returns ``SKIPPED`` when the profile is
            already enabled.  Raises
            :class:`UbuntuAuthenticationSubsystemNotFoundError`
            when ``name`` is not a registered profile.  Honours
            dry-run.

            :param name: The profile name.

            :returns: The :class:`UbuntuAuthActionResult`.
            :raises UbuntuAuthenticationSubsystemNotFoundError:
                When ``name`` is not registered.
        """
        command = [
            self._typed_config().pam_auth_update, "--enable", name,
        ]

        methods = self.list_auth_methods()
        registered: Optional[UbuntuAuthMethodInfo] = None
        for entry in methods:
            if entry.name == name:
                registered = entry
                break

        if registered is None:
            raise UbuntuAuthenticationSubsystemNotFoundError(
                subsystem=name,
                detail="profile not registered under pam-configs",
            )

        if registered.enabled:
            skipped = self._build_result(
                status=UbuntuAuthActionStatus.SKIPPED,
                subsystem=None,
                method=name,
                command=command,
                message="method already enabled",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuAuthActionStatus.DRY_RUN,
                subsystem=None,
                method=name,
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
            subsystem=None,
            method=name,
        )
        return rtnval

    def disable_method(self, name: str) -> UbuntuAuthActionResult:
        """
            Disable a registered ``pam-auth-update`` profile.

            Idempotent: returns ``SKIPPED`` when the profile is
            already disabled or unregistered.  Honours dry-run.

            Note: unlike :meth:`enable_method`, ``disable_method``
            does NOT raise on an unregistered name.  An unregistered
            name maps to ``SKIPPED`` (the desired end state — the
            method is not active — already holds).

            :param name: The profile name.

            :returns: The :class:`UbuntuAuthActionResult`.
        """
        command = [
            self._typed_config().pam_auth_update, "--disable", name,
        ]

        methods = self.list_auth_methods()
        registered: Optional[UbuntuAuthMethodInfo] = None
        for entry in methods:
            if entry.name == name:
                registered = entry
                break

        if registered is None:
            skipped = self._build_result(
                status=UbuntuAuthActionStatus.SKIPPED,
                subsystem=None,
                method=name,
                command=command,
                message="method not registered",
            )
            return skipped

        if not registered.enabled:
            skipped = self._build_result(
                status=UbuntuAuthActionStatus.SKIPPED,
                subsystem=None,
                method=name,
                command=command,
                message="method already disabled",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuAuthActionStatus.DRY_RUN,
                subsystem=None,
                method=name,
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
            subsystem=None,
            method=name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> UbuntuAuthenticationManagerConfig:
        """
            Narrow the base config to the typed subclass.

            :returns: The typed
                      :class:`UbuntuAuthenticationManagerConfig`.
        """
        if isinstance(self._config, UbuntuAuthenticationManagerConfig):
            rtnval = self._config
        else:
            rtnval = UbuntuAuthenticationManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _resolve_subsystem(self, *, subsystem) -> str:
        """
            Resolve the supplied subsystem reference into a service
            file basename.

            :param subsystem: An :class:`UbuntuAuthSubsystem`, a
                              string, or one of the
                              ``UBUNTU_AUTH_*`` constants.

            :returns: The basename string.
            :raises UbuntuAuthenticationSubsystemNotFoundError:
                When ``subsystem`` is None or an empty string.
        """
        if subsystem is None:
            raise UbuntuAuthenticationSubsystemNotFoundError(
                subsystem="<None>",
                detail="subsystem cannot be None",
            )

        if isinstance(subsystem, UbuntuAuthSubsystem):
            basename = subsystem.name
        elif isinstance(subsystem, str):
            basename = subsystem
        else:
            raise UbuntuAuthenticationSubsystemNotFoundError(
                subsystem=str(subsystem),
                detail="subsystem must be UbuntuAuthSubsystem or str",
            )

        if len(basename) == 0:
            raise UbuntuAuthenticationSubsystemNotFoundError(
                subsystem="<empty>",
                detail="subsystem basename is empty",
            )

        rtnval = basename
        return rtnval

    def _pam_path(self, *, basename: str) -> str:
        """
            Compose the absolute PAM service-file path for a
            basename.

            :param basename: The PAM service-file basename.

            :returns: The absolute path.
        """
        pam_dir = self._typed_config().pam_dir
        rtnval = "{}/{}".format(pam_dir, basename)
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat`` without elevation.

            :param path: The absolute path.

            :returns: The file contents on success; ``None`` when
                      ``cat`` reports a non-zero exit code (i.e.
                      the file is missing).
        """
        command = [_CAT, path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuAuthActionStatus,
        subsystem: Optional[str],
        method: Optional[str],
        command: list,
        message: Optional[str] = None,
    ) -> UbuntuAuthActionResult:
        """
            Build a result without invoking the runner.

            :param status: The outcome status.
            :param subsystem: The subsystem targeted, or None.
            :param method: The method targeted, or None.
            :param command: The would-have-been argv.
            :param message: Optional human-readable detail.

            :returns: The :class:`UbuntuAuthActionResult`.
        """
        if subsystem is not None:
            detail_text = subsystem
        elif method is not None:
            detail_text = method
        else:
            detail_text = None

        rtnval = UbuntuAuthActionResult(
            status=status,
            detail=detail_text,
            command=list(command),
            subsystem=subsystem,
            method=method,
            message=message,
        )
        return rtnval

    def _wrap_runner_result(
        self,
        *,
        run_result,
        command: list,
        subsystem: Optional[str],
        method: Optional[str],
    ) -> UbuntuAuthActionResult:
        """
            Map a :class:`RunResult` to an
            :class:`UbuntuAuthActionResult`.

            :param run_result: The runner's result.
            :param command: The argv that was dispatched.
            :param subsystem: The subsystem targeted, or None.
            :param method: The method targeted, or None.

            :returns: The :class:`UbuntuAuthActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = UbuntuAuthActionStatus.SUCCESS
        else:
            mapped_status = UbuntuAuthActionStatus.FAILED

        if subsystem is not None:
            detail_text = subsystem
        elif method is not None:
            detail_text = method
        else:
            detail_text = None

        rtnval = UbuntuAuthActionResult(
            status=mapped_status,
            detail=detail_text,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            subsystem=subsystem,
            method=method,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — PAM stack parser / renderer
# ----------------------------------------------------------------------


def _parse_pam_stack(*, content: str) -> list:
    """
        Parse PAM stack content into a list of
        :class:`UbuntuAuthEntry`.

        Comment lines (starting with ``#``) and blank lines are
        skipped.  ``[bracketed]`` control expressions are collected
        as a single token.

        :param content: The raw file content.

        :returns: A ``list[UbuntuAuthEntry]`` in declaration order.
    """
    rtnval: list = []
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue

        tokens = _tokenize_pam_line(line=stripped)
        if len(tokens) == 0:
            continue

        first_token = tokens[0]
        if first_token == "@include":
            if len(tokens) < 2:
                continue
            entry = UbuntuAuthEntry(
                module_type="@include",
                control="",
                module_path=tokens[1],
                args=[],
            )
            rtnval.append(entry)
            continue

        if len(tokens) < 3:
            continue

        module_type = tokens[0]
        control = tokens[1]
        module_path = tokens[2]
        if len(tokens) > 3:
            args_list = list(tokens[3:])
        else:
            args_list = []

        entry = UbuntuAuthEntry(
            module_type=module_type,
            control=control,
            module_path=module_path,
            args=args_list,
        )
        rtnval.append(entry)
    return rtnval


def _tokenize_pam_line(*, line: str) -> list:
    """
        Split a single PAM stack line into tokens, treating any
        ``[...]`` substring as a single token (this is how PAM's
        bracketed control expression is grammatically delimited).

        :param line: The raw line text (already stripped of
                     leading / trailing whitespace).

        :returns: A list of tokens.
    """
    rtnval: list = []
    current = []
    in_bracket = False
    for ch in line:
        if in_bracket:
            current.append(ch)
            if ch == "]":
                rtnval.append("".join(current))
                current = []
                in_bracket = False
            continue

        if ch == "[":
            if len(current) > 0:
                rtnval.append("".join(current))
                current = []
            current.append(ch)
            in_bracket = True
            continue

        if ch.isspace():
            if len(current) > 0:
                rtnval.append("".join(current))
                current = []
            continue

        current.append(ch)

    if len(current) > 0:
        rtnval.append("".join(current))
    return rtnval


def _render_pam_stack(*, entries: list) -> str:
    """
        Render a list of :class:`UbuntuAuthEntry` into PAM stack
        syntax.

        :param entries: The entries to render.

        :returns: The rendered file content (single trailing
                  newline).
    """
    lines: list = []
    for entry in entries:
        if entry.module_type == "@include":
            lines.append("@include {}".format(entry.module_path))
            continue

        parts = [entry.module_type, entry.control, entry.module_path]
        for arg in entry.args:
            parts.append(arg)
        rendered_line = " ".join(parts)
        lines.append(rendered_line)

    body = "\n".join(lines)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


# ----------------------------------------------------------------------
# Module-level helpers — pam-auth-update --list parser
# ----------------------------------------------------------------------


def _parse_pam_auth_update_list(*, text: str) -> dict:
    """
        Parse the ``pam-auth-update --list`` output.

        Output shape (Ubuntu 22.04+):

            PAM configurations available:
            unix    Unix authentication           [Enabled]
            krb5    Kerberos authentication       [Disabled]

        Older releases use a slightly different schema; the parser
        is tolerant of both.

        :param text: The raw stdout.

        :returns: A ``dict[str, tuple[bool, str]]`` keyed by name.
    """
    rtnval: dict = {}
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        lower_line = stripped.lower()
        if lower_line.startswith("pam configurations") or lower_line.startswith("pam-auth-update"):
            continue

        tokens = stripped.split(None, 1)
        if len(tokens) < 1:
            continue

        name = tokens[0]
        remainder = tokens[1] if len(tokens) == 2 else ""

        enabled_flag, description_text = _parse_pam_list_remainder(
            remainder=remainder,
        )
        rtnval[name] = (enabled_flag, description_text)
    return rtnval


def _parse_pam_list_remainder(*, remainder: str) -> tuple:
    """
        Parse the description / state portion of one
        ``pam-auth-update --list`` line.

        :param remainder: The text following the leading name token.

        :returns: ``(enabled, description)``.
    """
    text = remainder.strip()
    if len(text) == 0:
        rtnval = (False, "")
        return rtnval

    bracket_open = text.rfind("[")
    bracket_close = text.rfind("]")

    description_text = text
    state_text = ""
    if bracket_open != -1 and bracket_close > bracket_open:
        description_text = text[:bracket_open].strip()
        state_text = text[bracket_open + 1:bracket_close].strip()

    if len(state_text) == 0:
        # No bracketed state token; fall back to scanning trailing tokens.
        token_parts = text.rsplit(None, 1)
        if len(token_parts) == 2:
            tail = token_parts[1].strip().lower()
            if tail in ("enabled", "disabled"):
                description_text = token_parts[0].strip()
                state_text = tail

    enabled_flag = state_text.strip().lower() == "enabled"
    rtnval = (enabled_flag, description_text)
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — pam-configs profile-file parser
# ----------------------------------------------------------------------


def _parse_pam_config_profile(*, text: str) -> dict:
    """
        Parse a ``/usr/share/pam-configs/<name>`` profile file into
        a flat ``dict`` of the top-level keys (``Name``, ``Default``,
        ``Priority``, ``Auth-Type``, ``Auth``, ...).

        Indented continuation lines under a key are joined into the
        value with newline separators (preserves the PAM body for
        debuggability).  The first PAM-module reference seen in any
        body is exposed as the synthetic key ``ModulePath`` for
        consumption by :meth:`UbuntuAuthenticationManager.list_auth_methods`.

        :param text: Raw profile-file content.

        :returns: The key/value dict.
    """
    rtnval: dict = {}
    current_key: Optional[str] = None
    current_lines: list = []
    module_path: Optional[str] = None

    def flush() -> None:
        if current_key is None:
            return
        rtnval[current_key] = "\n".join(current_lines).strip()
        return

    for raw_line in text.splitlines():
        if len(raw_line) == 0:
            continue

        if raw_line[0].isspace():
            if current_key is not None:
                current_lines.append(raw_line.strip())
                if module_path is None:
                    module_path = _extract_module_path(text=raw_line)
            continue

        flush()
        if ":" not in raw_line:
            current_key = None
            current_lines = []
            continue

        key_part, _, value_part = raw_line.partition(":")
        current_key = key_part.strip()
        current_lines = []
        value_text = value_part.strip()
        if len(value_text) > 0:
            current_lines.append(value_text)

    flush()
    if module_path is not None:
        rtnval["ModulePath"] = module_path
    return rtnval


def _extract_module_path(*, text: str) -> Optional[str]:
    """
        Find the first ``pam_*.so`` token on a line.

        :param text: The raw line text.

        :returns: The token if found; otherwise ``None``.
    """
    for token in text.split():
        if token.startswith("pam_") and token.endswith(".so"):
            rtnval: Optional[str] = token
            return rtnval
        if ".so" in token and token.startswith("pam_"):
            rtnval = token
            return rtnval
    rtnval = None
    return rtnval
