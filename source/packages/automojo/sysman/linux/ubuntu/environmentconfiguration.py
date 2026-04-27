"""
    Ubuntu environment-configuration feature.

    Implements the cross-OS environment-configuration contract for Ubuntu
    by maintaining variables across three persistence surfaces:

    * ``/etc/environment`` — bare ``KEY=VALUE`` lines, read by PAM and
      ``systemd``.
    * ``/etc/profile.d/automojo.sh`` — shell drop-in with ``export
      KEY=VALUE`` lines, sourced by login shells.
    * Per-user profile files (``~<user>/.profile`` by default) — shell
      ``export`` lines bracketed by sentinel comments so the manager can
      replace its own block without disturbing user-authored content.

    File I/O policy:
        Every read / write / delete is performed through the injected
        :class:`ICommandRunner` (``cat`` / ``tee`` / ``rm`` / ``test`` /
        ``getent``).  This module does NOT consume an
        :class:`IFileOperator`.

    Elevation policy:
        System-scope writes honour ``config.elevated``.  User-scope writes
        currently always honour ``config.elevated`` as well; the policy
        spelled out in the design (suppress elevation for the running
        user's own profile) requires a reliable "current user" probe that
        is not yet wired into this package.  The simplification is
        documented at the call site and tracked as a follow-up in
        ``specs/3-environment-configuration/tasks.md``.
"""

import os
import re

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_DEFAULT_ENVIRONMENT_FILE = "/etc/environment"
_DEFAULT_PROFILE_D_DROPIN = "/etc/profile.d/automojo.sh"
_DEFAULT_USER_PROFILE_BASENAME = ".profile"

_PROFILE_D_SHEBANG = "#!/usr/bin/env bash"
_USER_BLOCK_BEGIN = "# --- automojo env start ---"
_USER_BLOCK_END = "# --- automojo env end ---"

_BARE_ENV_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_EXPORT_ENV_PATTERN = re.compile(
    r"^export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$"
)


UbuntuEnvActionStatus = ActionStatus


class UbuntuEnvScope(Enum):
    """
        Scope selector for environment-configuration verbs.
    """
    SYSTEM = "system"
    USER   = "user"


@dataclass
class UbuntuEnvironmentManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuEnvironmentConfigurationManager`.
        Inherits ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param environment_file: System-scope key=value file path.
                                 Defaults to ``/etc/environment``.
        :param profile_d_drop_in: System-scope shell drop-in path.
                                  Defaults to
                                  ``/etc/profile.d/automojo.sh``.
        :param user_profile_basename: Basename of the user-scope profile
                                      file written within each user's home
                                      directory.  Defaults to
                                      ``.profile``.
    """
    environment_file: str = _DEFAULT_ENVIRONMENT_FILE
    profile_d_drop_in: str = _DEFAULT_PROFILE_D_DROPIN
    user_profile_basename: str = _DEFAULT_USER_PROFILE_BASENAME


@dataclass
class UbuntuEnvActionResult(FeatureActionResult):
    """
        Result of an environment-configuration write operation.

        :param name: The variable name targeted.
        :param value: The value written, or ``None`` for unset operations.
        :param scope: The scope targeted.
        :param user: The username (USER scope only); ``None`` for system
                     scope.
        :param path: The file path that was (or would have been) modified.
                     For system-scope writes that touch both files, the
                     path of the most recently mutated file is recorded.
        :param message: Optional human-readable detail for ``SKIPPED`` or
                        ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    value: Optional[str] = None
    scope: Optional[UbuntuEnvScope] = None
    user: Optional[str] = None
    path: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuEnvValue:
    """
        A single environment variable value.

        :param name: The variable name.
        :param value: The variable value.
        :param scope: The scope the variable was read from.
        :param user: The username for USER scope; ``None`` otherwise.
        :param source: The file path the value was parsed from.
    """
    name: str
    value: str
    scope: UbuntuEnvScope
    user: Optional[str] = None
    source: Optional[str] = None


class UbuntuEnvQueryError(Exception):
    """
        Raised when an environment read fails for a reason other than the
        target variable simply being absent.
    """

    def __init__(self, *, message: str) -> None:
        """
            :param message: Human-readable detail.
        """
        self.message = message
        err_msg_lines = ["Environment configuration query failed."]
        err_msg_lines.append("    DETAIL: {}".format(message))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuEnvironmentConfigurationManager(BaseFeatureManager):
    """
        Ubuntu environment-configuration manager.  Maintains
        ``/etc/environment``, ``/etc/profile.d/automojo.sh``, and per-user
        profile files in lockstep, dispatching every operation through the
        injected :class:`ICommandRunner`.
    """

    result_class = UbuntuEnvActionResult
    status_class = UbuntuEnvActionStatus
    feature_name = "environment-configuration"

    # ------------------------------------------------------------------
    # System-scope write verbs
    # ------------------------------------------------------------------

    def set_system_env(
        self,
        name: str,
        value: str,
    ) -> UbuntuEnvActionResult:
        """
            Set a system-scope environment variable in lockstep across
            ``/etc/environment`` and the ``profile.d`` drop-in.

            Skips when both files already contain the desired value.

            :param name: The variable name.
            :param value: The variable value.

            :returns: The :class:`UbuntuEnvActionResult`.
        """
        env_path = self._config.environment_file
        profile_path = self._config.profile_d_drop_in

        env_existing = self._read_file(path=env_path)
        profile_existing = self._read_file(path=profile_path)

        env_current = "" if env_existing is None else env_existing
        profile_current = "" if profile_existing is None else profile_existing

        env_desired = _set_bare_env_line(
            content=env_current, name=name, value=value,
        )
        profile_desired = _set_export_line(
            content=profile_current, name=name, value=value,
        )

        env_matches = (env_existing is not None and env_existing == env_desired)
        profile_matches = (
            profile_existing is not None and profile_existing == profile_desired
        )

        if env_matches and profile_matches:
            skipped = self._build_result(
                status=UbuntuEnvActionStatus.SKIPPED,
                name=name,
                value=value,
                scope=UbuntuEnvScope.SYSTEM,
                user=None,
                path=env_path,
                command=["tee", env_path],
                message="value already set in both system files",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuEnvActionStatus.DRY_RUN,
                name=name,
                value=value,
                scope=UbuntuEnvScope.SYSTEM,
                user=None,
                path=env_path,
                command=["tee", env_path],
            )
            return dry

        last_path = env_path
        if not env_matches:
            env_run = self._write_file(path=env_path, content=env_desired)
            if env_run.exit_code != 0:
                failed = UbuntuEnvActionResult(
                    status=UbuntuEnvActionStatus.FAILED,
                    detail=name,
                    exit_code=env_run.exit_code,
                    stdout=env_run.stdout,
                    stderr=env_run.stderr,
                    command=["tee", env_path],
                    name=name,
                    value=value,
                    scope=UbuntuEnvScope.SYSTEM,
                    user=None,
                    path=env_path,
                    message="environment_file_write_failed",
                )
                return failed

        if not profile_matches:
            profile_run = self._write_file(
                path=profile_path, content=profile_desired,
            )
            last_path = profile_path
            if profile_run.exit_code != 0:
                failed = UbuntuEnvActionResult(
                    status=UbuntuEnvActionStatus.FAILED,
                    detail=name,
                    exit_code=profile_run.exit_code,
                    stdout=profile_run.stdout,
                    stderr=profile_run.stderr,
                    command=["tee", profile_path],
                    name=name,
                    value=value,
                    scope=UbuntuEnvScope.SYSTEM,
                    user=None,
                    path=profile_path,
                    message="profile_d_write_failed",
                )
                return failed

        success = UbuntuEnvActionResult(
            status=UbuntuEnvActionStatus.SUCCESS,
            detail=name,
            command=["tee", last_path],
            name=name,
            value=value,
            scope=UbuntuEnvScope.SYSTEM,
            user=None,
            path=last_path,
        )
        return success

    def unset_system_env(self, name: str) -> UbuntuEnvActionResult:
        """
            Remove a system-scope environment variable from both files.

            Skips when the variable is not present in either file.  When
            the ``profile.d`` drop-in becomes empty (only the shebang and
            blank lines remain) it is removed entirely.

            :param name: The variable name.

            :returns: The :class:`UbuntuEnvActionResult`.
        """
        env_path = self._config.environment_file
        profile_path = self._config.profile_d_drop_in

        env_existing = self._read_file(path=env_path)
        profile_existing = self._read_file(path=profile_path)

        env_current = "" if env_existing is None else env_existing
        profile_current = "" if profile_existing is None else profile_existing

        env_after = _remove_bare_env_line(content=env_current, name=name)
        profile_after = _remove_export_line(content=profile_current, name=name)

        env_changed = (env_existing is not None and env_after != env_current)
        profile_changed = (
            profile_existing is not None and profile_after != profile_current
        )

        if not env_changed and not profile_changed:
            skipped = self._build_result(
                status=UbuntuEnvActionStatus.SKIPPED,
                name=name,
                value=None,
                scope=UbuntuEnvScope.SYSTEM,
                user=None,
                path=env_path,
                command=["tee", env_path],
                message="variable not present in either system file",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuEnvActionStatus.DRY_RUN,
                name=name,
                value=None,
                scope=UbuntuEnvScope.SYSTEM,
                user=None,
                path=env_path,
                command=["tee", env_path],
            )
            return dry

        last_path = env_path
        last_command = ["tee", env_path]
        if env_changed:
            env_run = self._write_file(path=env_path, content=env_after)
            if env_run.exit_code != 0:
                failed = UbuntuEnvActionResult(
                    status=UbuntuEnvActionStatus.FAILED,
                    detail=name,
                    exit_code=env_run.exit_code,
                    stdout=env_run.stdout,
                    stderr=env_run.stderr,
                    command=["tee", env_path],
                    name=name,
                    value=None,
                    scope=UbuntuEnvScope.SYSTEM,
                    user=None,
                    path=env_path,
                    message="environment_file_write_failed",
                )
                return failed

        if profile_changed:
            if _profile_is_empty(content=profile_after):
                delete_run = self._delete_file(path=profile_path)
                last_path = profile_path
                last_command = ["rm", "-f", profile_path]
                if delete_run.exit_code != 0:
                    failed = UbuntuEnvActionResult(
                        status=UbuntuEnvActionStatus.FAILED,
                        detail=name,
                        exit_code=delete_run.exit_code,
                        stdout=delete_run.stdout,
                        stderr=delete_run.stderr,
                        command=last_command,
                        name=name,
                        value=None,
                        scope=UbuntuEnvScope.SYSTEM,
                        user=None,
                        path=profile_path,
                        message="profile_d_delete_failed",
                    )
                    return failed
            else:
                profile_run = self._write_file(
                    path=profile_path, content=profile_after,
                )
                last_path = profile_path
                last_command = ["tee", profile_path]
                if profile_run.exit_code != 0:
                    failed = UbuntuEnvActionResult(
                        status=UbuntuEnvActionStatus.FAILED,
                        detail=name,
                        exit_code=profile_run.exit_code,
                        stdout=profile_run.stdout,
                        stderr=profile_run.stderr,
                        command=last_command,
                        name=name,
                        value=None,
                        scope=UbuntuEnvScope.SYSTEM,
                        user=None,
                        path=profile_path,
                        message="profile_d_write_failed",
                    )
                    return failed

        success = UbuntuEnvActionResult(
            status=UbuntuEnvActionStatus.SUCCESS,
            detail=name,
            command=last_command,
            name=name,
            value=None,
            scope=UbuntuEnvScope.SYSTEM,
            user=None,
            path=last_path,
        )
        return success

    # ------------------------------------------------------------------
    # System-scope read verbs
    # ------------------------------------------------------------------

    def get_system_env(self, name: str) -> Optional[UbuntuEnvValue]:
        """
            Read a system-scope environment variable.

            ``/etc/environment`` takes precedence over the profile.d drop-in.

            :param name: The variable name.

            :returns: The :class:`UbuntuEnvValue`, or ``None`` when the
                      variable is not present in either source.
        """
        env_path = self._config.environment_file
        profile_path = self._config.profile_d_drop_in

        env_content = self._read_file(path=env_path)
        if env_content is not None:
            env_parsed = _parse_bare_env(text=env_content)
            if name in env_parsed:
                rtnval: Optional[UbuntuEnvValue] = UbuntuEnvValue(
                    name=name,
                    value=env_parsed[name],
                    scope=UbuntuEnvScope.SYSTEM,
                    user=None,
                    source=env_path,
                )
                return rtnval

        profile_content = self._read_file(path=profile_path)
        if profile_content is not None:
            profile_parsed = _parse_export_env(text=profile_content)
            if name in profile_parsed:
                rtnval = UbuntuEnvValue(
                    name=name,
                    value=profile_parsed[name],
                    scope=UbuntuEnvScope.SYSTEM,
                    user=None,
                    source=profile_path,
                )
                return rtnval

        rtnval = None
        return rtnval

    def list_system_env(self) -> dict[str, str]:
        """
            Enumerate every system-scope variable across both files.

            ``/etc/environment`` takes precedence on conflict.  An empty
            dict is returned when both files are missing.

            :returns: A merged ``dict[str, str]``.
        """
        rtnval: dict[str, str] = {}

        profile_content = self._read_file(path=self._config.profile_d_drop_in)
        if profile_content is not None:
            for key, value in _parse_export_env(text=profile_content).items():
                rtnval[key] = value

        env_content = self._read_file(path=self._config.environment_file)
        if env_content is not None:
            for key, value in _parse_bare_env(text=env_content).items():
                rtnval[key] = value

        return rtnval

    # ------------------------------------------------------------------
    # User-scope write verbs
    # ------------------------------------------------------------------

    def set_user_env(
        self,
        user: str,
        name: str,
        value: str,
    ) -> UbuntuEnvActionResult:
        """
            Set a user-scope environment variable in the user's profile.

            The value is rendered as ``export NAME="value"`` inside a
            sentinel-bracketed block so the manager can replace its own
            content without disturbing user-authored content elsewhere in
            the file.

            Skips when the block already contains the desired line.

            Elevation note:
                This implementation always passes ``config.elevated`` to
                the runner.  The design spec calls for suppressing
                elevation when the target user equals the running user,
                but we do not yet have a reliable way to discover the
                running user that works under both local and SSH execution.
                See ``specs/3-environment-configuration/tasks.md`` follow-
                ups.

            :param user: The username.
            :param name: The variable name.
            :param value: The variable value.

            :returns: The :class:`UbuntuEnvActionResult`.
        """
        try:
            home_dir = self._resolve_home(user=user)
        except UbuntuEnvQueryError as exc:
            failed = self._build_result(
                status=UbuntuEnvActionStatus.FAILED,
                name=name,
                value=value,
                scope=UbuntuEnvScope.USER,
                user=user,
                path=None,
                command=["getent", "passwd", user],
                message="user not found: {}".format(user),
                stderr=exc.message,
            )
            return failed

        path = self._user_profile_path(home_dir=home_dir)

        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        desired_content = _set_user_block_export(
            content=existing_content, name=name, value=value,
        )

        if existing is not None and existing == desired_content:
            skipped = self._build_result(
                status=UbuntuEnvActionStatus.SKIPPED,
                name=name,
                value=value,
                scope=UbuntuEnvScope.USER,
                user=user,
                path=path,
                command=["tee", path],
                message="value already set in user profile",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuEnvActionStatus.DRY_RUN,
                name=name,
                value=value,
                scope=UbuntuEnvScope.USER,
                user=user,
                path=path,
                command=["tee", path],
            )
            return dry

        run_result = self._write_file(path=path, content=desired_content)
        if run_result.exit_code == 0:
            mapped_status = UbuntuEnvActionStatus.SUCCESS
        else:
            mapped_status = UbuntuEnvActionStatus.FAILED

        rtnval = UbuntuEnvActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=["tee", path],
            name=name,
            value=value,
            scope=UbuntuEnvScope.USER,
            user=user,
            path=path,
        )
        return rtnval

    def unset_user_env(
        self,
        user: str,
        name: str,
    ) -> UbuntuEnvActionResult:
        """
            Remove a user-scope environment variable from the user's
            profile.

            Skips when the file is missing or the variable is not present
            inside the managed block.

            :param user: The username.
            :param name: The variable name.

            :returns: The :class:`UbuntuEnvActionResult`.
        """
        try:
            home_dir = self._resolve_home(user=user)
        except UbuntuEnvQueryError as exc:
            failed = self._build_result(
                status=UbuntuEnvActionStatus.FAILED,
                name=name,
                value=None,
                scope=UbuntuEnvScope.USER,
                user=user,
                path=None,
                command=["getent", "passwd", user],
                message="user not found: {}".format(user),
                stderr=exc.message,
            )
            return failed

        path = self._user_profile_path(home_dir=home_dir)

        existing = self._read_file(path=path)
        if existing is None:
            skipped = self._build_result(
                status=UbuntuEnvActionStatus.SKIPPED,
                name=name,
                value=None,
                scope=UbuntuEnvScope.USER,
                user=user,
                path=path,
                command=["tee", path],
                message="user profile file does not exist",
            )
            return skipped

        new_content = _remove_user_block_export(
            content=existing, name=name,
        )
        if new_content == existing:
            skipped = self._build_result(
                status=UbuntuEnvActionStatus.SKIPPED,
                name=name,
                value=None,
                scope=UbuntuEnvScope.USER,
                user=user,
                path=path,
                command=["tee", path],
                message="variable not present in user profile",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuEnvActionStatus.DRY_RUN,
                name=name,
                value=None,
                scope=UbuntuEnvScope.USER,
                user=user,
                path=path,
                command=["tee", path],
            )
            return dry

        run_result = self._write_file(path=path, content=new_content)
        if run_result.exit_code == 0:
            mapped_status = UbuntuEnvActionStatus.SUCCESS
        else:
            mapped_status = UbuntuEnvActionStatus.FAILED

        rtnval = UbuntuEnvActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=["tee", path],
            name=name,
            value=None,
            scope=UbuntuEnvScope.USER,
            user=user,
            path=path,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_home(self, *, user: str) -> str:
        """
            Resolve a username's home directory via ``getent passwd``.

            :param user: The username.

            :returns: Absolute home-directory path.
            :raises UbuntuEnvQueryError: When ``getent`` reports a non-
                                          zero exit code or no row is
                                          returned.
        """
        command = ["getent", "passwd", user]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuEnvQueryError(
                message="getent passwd {} returned non-zero".format(user),
            )

        for line in run_result.stdout.splitlines():
            stripped = line.strip()
            if len(stripped) == 0:
                continue
            fields = stripped.split(":")
            if len(fields) < 6:
                continue
            home = fields[5]
            return home

        raise UbuntuEnvQueryError(
            message="getent returned no row for {}".format(user),
        )

    def _user_profile_path(self, *, home_dir: str) -> str:
        """
            Build the absolute path to the user's profile file.

            :param home_dir: The user's home directory.

            :returns: ``<home_dir>/<user_profile_basename>``.
        """
        rtnval = "{}/{}".format(
            home_dir, self._config.user_profile_basename,
        )
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat``.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when ``cat``
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

    def _write_file(self, *, path: str, content: str):
        """
            Write a file via ``tee`` with ``content`` on stdin.

            :param path: The absolute path to write.
            :param content: The full file content.

            :returns: The runner's :class:`RunResult`.
        """
        command = ["tee", path]
        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
            stdin=content,
        )
        return run_result

    def _delete_file(self, *, path: str):
        """
            Delete a file via ``rm -f``.

            :param path: The absolute path to delete.

            :returns: The runner's :class:`RunResult`.
        """
        command = ["rm", "-f", path]
        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        return run_result

    def _build_result(
        self,
        *,
        status: UbuntuEnvActionStatus,
        name: Optional[str],
        value: Optional[str],
        scope: Optional[UbuntuEnvScope],
        user: Optional[str],
        path: Optional[str],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuEnvActionResult:
        """
            Construct an :class:`UbuntuEnvActionResult` without invoking
            the runner.

            :param status: The outcome status.
            :param name: The variable name to echo on the result.
            :param value: The would-be value.
            :param scope: The scope targeted.
            :param user: The username for USER scope.
            :param path: The file path the action targeted.
            :param command: The would-have-been argv.
            :param message: Human-readable detail.
            :param stderr: Synthetic stderr payload.

            :returns: The :class:`UbuntuEnvActionResult`.
        """
        rtnval = UbuntuEnvActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            value=value,
            scope=scope,
            user=user,
            path=path,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers and renderers
# ----------------------------------------------------------------------


def _parse_bare_env(*, text: str) -> dict[str, str]:
    """
        Parse a ``KEY=VALUE`` style file into a dict.

        Comment lines (``#``) and blank lines are skipped.  Surrounding
        double quotes around values are stripped.

        :param text: Raw file content.

        :returns: A ``dict[str, str]`` of every parsed entry.
    """
    rtnval: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) == 0 or line.startswith("#"):
            continue
        match = _BARE_ENV_PATTERN.match(line)
        if match is None:
            continue
        name = match.group(1)
        raw_value = match.group(2)
        rtnval[name] = _strip_quotes(value=raw_value)
    return rtnval


def _parse_export_env(*, text: str) -> dict[str, str]:
    """
        Parse a shell-style ``export KEY=VALUE`` file into a dict.

        :param text: Raw file content.

        :returns: A ``dict[str, str]`` of every parsed entry.
    """
    rtnval: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) == 0 or line.startswith("#"):
            continue
        match = _EXPORT_ENV_PATTERN.match(line)
        if match is None:
            continue
        name = match.group(1)
        raw_value = match.group(2)
        rtnval[name] = _strip_quotes(value=raw_value)
    return rtnval


def _strip_quotes(*, value: str) -> str:
    """
        Strip a single pair of surrounding double quotes from ``value``.

        :param value: Raw value text.

        :returns: The unquoted (and unescaped) value.
    """
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        inner = value[1:-1]
        unescaped = inner.replace('\\"', '"').replace("\\\\", "\\")
        return unescaped
    return value


def _escape_value(*, value: str) -> str:
    """
        Escape a value for double-quoted shell rendering.

        :param value: The raw value.

        :returns: The escaped value (no surrounding quotes).
    """
    rtnval = value.replace("\\", "\\\\").replace('"', '\\"')
    return rtnval


def _set_bare_env_line(*, content: str, name: str, value: str) -> str:
    """
        Return ``content`` updated to contain ``NAME="value"``.

        Existing lines for the same name are replaced in place.  Comment
        and blank lines are preserved verbatim.  An empty starting content
        yields a single-line file ending in ``\\n``.

        :param content: Current file content.
        :param name: The variable name.
        :param value: The desired value.

        :returns: The updated file content.
    """
    escaped = _escape_value(value=value)
    new_line = '{}="{}"'.format(name, escaped)

    if len(content) == 0:
        body = new_line + "\n"
        return body

    lines = content.splitlines()
    updated: list[str] = []
    replaced = False
    for line in lines:
        match = _BARE_ENV_PATTERN.match(line.strip())
        if match is not None and match.group(1) == name:
            updated.append(new_line)
            replaced = True
            continue
        updated.append(line)

    if not replaced:
        updated.append(new_line)

    body = "\n".join(updated)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _remove_bare_env_line(*, content: str, name: str) -> str:
    """
        Return ``content`` with any matching ``NAME=`` line removed.

        :param content: Current file content.
        :param name: The variable name to remove.

        :returns: The updated file content.
    """
    if len(content) == 0:
        return content

    lines = content.splitlines()
    kept: list[str] = []
    for line in lines:
        match = _BARE_ENV_PATTERN.match(line.strip())
        if match is not None and match.group(1) == name:
            continue
        kept.append(line)

    body = "\n".join(kept)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body


def _set_export_line(*, content: str, name: str, value: str) -> str:
    """
        Return ``content`` updated to contain ``export NAME="value"``.

        For empty content, a fresh shebang-prefixed body is emitted.

        :param content: Current file content.
        :param name: The variable name.
        :param value: The desired value.

        :returns: The updated file content.
    """
    escaped = _escape_value(value=value)
    new_line = 'export {}="{}"'.format(name, escaped)

    if len(content) == 0:
        body = "{}\n{}\n".format(_PROFILE_D_SHEBANG, new_line)
        return body

    lines = content.splitlines()
    updated: list[str] = []
    replaced = False
    for line in lines:
        match = _EXPORT_ENV_PATTERN.match(line.strip())
        if match is not None and match.group(1) == name:
            updated.append(new_line)
            replaced = True
            continue
        updated.append(line)

    if not replaced:
        updated.append(new_line)

    body = "\n".join(updated)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _remove_export_line(*, content: str, name: str) -> str:
    """
        Return ``content`` with any matching ``export NAME=`` line
        removed.

        :param content: Current file content.
        :param name: The variable name to remove.

        :returns: The updated file content.
    """
    if len(content) == 0:
        return content

    lines = content.splitlines()
    kept: list[str] = []
    for line in lines:
        match = _EXPORT_ENV_PATTERN.match(line.strip())
        if match is not None and match.group(1) == name:
            continue
        kept.append(line)

    body = "\n".join(kept)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body


def _profile_is_empty(*, content: str) -> bool:
    """
        Return True when a profile.d file contains only the shebang and
        blanks.

        :param content: File content.

        :returns: True when no actionable lines remain.
    """
    for raw in content.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        if line.startswith("#!"):
            continue
        if line.startswith("#"):
            continue
        return False
    return True


def _set_user_block_export(
    *,
    content: str,
    name: str,
    value: str,
) -> str:
    """
        Return ``content`` updated so the automojo-managed block contains
        an ``export NAME="value"`` line for ``name``.

        When the block is missing, a fresh sentinel-bracketed block is
        appended at the end of the file.  When the block exists, the
        target variable is replaced in place inside it; other lines
        outside the block are preserved verbatim.

        :param content: Current file content.
        :param name: The variable name.
        :param value: The desired value.

        :returns: The updated file content.
    """
    escaped = _escape_value(value=value)
    new_line = 'export {}="{}"'.format(name, escaped)

    block_start_idx, block_end_idx = _find_user_block(content=content)

    if block_start_idx is None or block_end_idx is None:
        if len(content) == 0:
            base = ""
        elif content.endswith("\n"):
            base = content
        else:
            base = content + "\n"
        block_lines = [
            _USER_BLOCK_BEGIN,
            new_line,
            _USER_BLOCK_END,
        ]
        body = base + "\n".join(block_lines) + "\n"
        return body

    lines = content.splitlines()
    updated: list[str] = list(lines)

    replaced = False
    for idx in range(block_start_idx + 1, block_end_idx):
        match = _EXPORT_ENV_PATTERN.match(updated[idx].strip())
        if match is not None and match.group(1) == name:
            updated[idx] = new_line
            replaced = True
            break

    if not replaced:
        updated.insert(block_end_idx, new_line)

    body = "\n".join(updated)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _remove_user_block_export(*, content: str, name: str) -> str:
    """
        Return ``content`` with any matching ``export NAME=`` line removed
        from the automojo-managed block.

        When the block becomes empty, the sentinel comments are also
        removed so a future run produces a clean file.

        :param content: Current file content.
        :param name: The variable name to remove.

        :returns: The updated file content.
    """
    block_start_idx, block_end_idx = _find_user_block(content=content)
    if block_start_idx is None or block_end_idx is None:
        return content

    lines = content.splitlines()
    new_block: list[str] = []
    removed = False
    for idx in range(block_start_idx + 1, block_end_idx):
        match = _EXPORT_ENV_PATTERN.match(lines[idx].strip())
        if match is not None and match.group(1) == name:
            removed = True
            continue
        new_block.append(lines[idx])

    if not removed:
        return content

    head = lines[:block_start_idx]
    tail = lines[block_end_idx + 1:]

    if len(new_block) == 0:
        merged = head + tail
    else:
        merged = head + [_USER_BLOCK_BEGIN] + new_block + [_USER_BLOCK_END] + tail

    body = "\n".join(merged)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body


def _find_user_block(*, content: str) -> tuple:
    """
        Locate the start/end line indices of the automojo-managed block.

        :param content: File content.

        :returns: ``(begin_index, end_index)`` line tuple, or
                  ``(None, None)`` when the block is missing.
    """
    lines = content.splitlines()
    begin_idx: Optional[int] = None
    end_idx: Optional[int] = None
    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if begin_idx is None and stripped == _USER_BLOCK_BEGIN:
            begin_idx = idx
            continue
        if begin_idx is not None and stripped == _USER_BLOCK_END:
            end_idx = idx
            break
    rtnval = (begin_idx, end_idx)
    return rtnval
