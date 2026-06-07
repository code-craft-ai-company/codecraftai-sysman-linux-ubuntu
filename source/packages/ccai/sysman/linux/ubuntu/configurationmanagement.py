"""
    Ubuntu configuration-management feature.

    Implements the cross-OS configuration-management contract for Ubuntu
    using shell-style ``KEY=VALUE`` configuration files (e.g.
    ``/etc/environment``, systemd ``EnvironmentFile`` fragments, the
    ``/etc/default/<name>`` family).  Every verb is dispatched through
    the injected :class:`ICommandRunner`.

    File-format scope:
        This pilot supports shell-style ``KEY=VALUE`` and
        ``KEY="VALUE"`` lines.  INI and YAML support is a tracked
        follow-up (see ``specs/3-configuration-management/tasks.md``).

    File I/O policy:
        Reads use ``cat``; writes use ``tee`` with the new content on
        stdin; existence probes use ``test -e``; backups use ``cp``;
        deletes use ``rm``.  This module does NOT consume an
        :class:`IFileOperator`.

    Elevation policy:
        Write verbs and ``reload_configuration`` honour
        ``config.elevated``.  Reads (``read_setting``,
        ``list_settings``) and existence probes never elevate.
"""

import os
import re

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_SHELL_KV_PATTERN = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$"
)


UbuntuConfigActionStatus = ActionStatus


@dataclass
class UbuntuConfigManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuConfigurationManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param backup_suffix_format: ``strftime`` format used to render the
                                     timestamp portion of backup filenames.
                                     The default produces compact UTC
                                     timestamps.
        :param backup_extension: Trailing extension appended to backups
                                 (after the timestamp).  Defaults to
                                 ``.bak``.
    """
    backup_suffix_format: str = "%Y%m%dT%H%M%S"
    backup_extension: str = ".bak"


@dataclass
class UbuntuConfigActionResult(FeatureActionResult):
    """
        Result of a configuration-management write operation.

        :param file: The configuration file path the action targeted, or
                     ``None`` for ``reload_configuration``.
        :param key: The setting key targeted, or ``None`` for whole-file
                    operations.
        :param backup_path: Populated by :meth:`backup_configuration` and
                            :meth:`restore_configuration`.
        :param diff: Reserved for a future dry-run unified-diff field;
                     left ``None`` in this implementation.
        :param message: Optional human-readable detail for ``SKIPPED`` or
                        ``FAILED`` outcomes.
    """
    file: Optional[str] = None
    key: Optional[str] = None
    backup_path: Optional[str] = None
    diff: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuSettingValue:
    """
        A single configuration setting as parsed from a shell-style file.

        :param key: The setting name.
        :param value: The setting value (with surrounding double quotes
                      stripped).
        :param file: The file path the value was parsed from.
    """
    key: str
    value: str
    file: str


class UbuntuConfigQueryError(Exception):
    """
        Raised when a configuration read fails for a reason other than the
        target source simply being absent.
    """

    def __init__(
        self,
        *,
        file: str,
        message: str,
    ) -> None:
        """
            :param file: The file path that failed to read.
            :param message: Human-readable detail.
        """
        self.file = file
        self.message = message
        err_msg_lines = ["Configuration query failed."]
        err_msg_lines.append("    FILE: {}".format(file))
        err_msg_lines.append("    DETAIL: {}".format(message))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuConfigurationManager(BaseFeatureManager):
    """
        Ubuntu configuration-file manager.  Reads, writes, backs up, and
        restores shell-style ``KEY=VALUE`` configuration files through
        the injected :class:`ICommandRunner`.
    """

    result_class = UbuntuConfigActionResult
    status_class = UbuntuConfigActionStatus
    feature_name = "configuration-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def read_setting(self, file: str, key: str) -> UbuntuSettingValue:
        """
            Read a single setting from a shell-style configuration file.

            :param file: The absolute file path.
            :param key: The setting name.

            :returns: The :class:`UbuntuSettingValue`.
            :raises UbuntuConfigQueryError: When the file is absent or the
                                            key is not present.
        """
        content = self._read_file(path=file)
        if content is None:
            raise UbuntuConfigQueryError(
                file=file,
                message="file does not exist or is unreadable",
            )

        parsed = _parse_shell_kv(text=content)
        if key not in parsed:
            raise UbuntuConfigQueryError(
                file=file,
                message="key not present: {}".format(key),
            )

        rtnval = UbuntuSettingValue(
            key=key,
            value=parsed[key],
            file=file,
        )
        return rtnval

    def list_settings(self, file: str) -> dict[str, str]:
        """
            Enumerate every setting in a shell-style configuration file.

            Returns an empty dict when the file is absent.

            :param file: The absolute file path.

            :returns: A ``dict[str, str]`` of every parsed entry.
        """
        content = self._read_file(path=file)
        if content is None:
            rtnval: dict[str, str] = {}
            return rtnval

        rtnval = _parse_shell_kv(text=content)
        return rtnval

    # ------------------------------------------------------------------
    # Per-setting write verbs
    # ------------------------------------------------------------------

    def write_setting(
        self,
        file: str,
        key: str,
        value: str,
    ) -> UbuntuConfigActionResult:
        """
            Set ``key=value`` inside ``file``.

            Skips when the file already contains the exact desired pair.
            Creates the file when it does not exist.

            :param file: The absolute file path.
            :param key: The setting name.
            :param value: The desired value.

            :returns: The :class:`UbuntuConfigActionResult`.
        """
        write_command = ["tee", file]

        existing = self._read_file(path=file)
        existing_content = "" if existing is None else existing
        desired_content = _set_shell_kv(
            content=existing_content, key=key, value=value,
        )

        if existing is not None and existing == desired_content:
            skipped = self._build_result(
                status=UbuntuConfigActionStatus.SKIPPED,
                file=file,
                key=key,
                command=write_command,
                message="setting already at desired value",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuConfigActionStatus.DRY_RUN,
                file=file,
                key=key,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=desired_content,
        )
        if run_result.exit_code == 0:
            mapped_status = UbuntuConfigActionStatus.SUCCESS
        else:
            mapped_status = UbuntuConfigActionStatus.FAILED

        rtnval = UbuntuConfigActionResult(
            status=mapped_status,
            detail=key,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            file=file,
            key=key,
        )
        return rtnval

    def remove_setting(
        self,
        file: str,
        key: str,
    ) -> UbuntuConfigActionResult:
        """
            Remove ``key`` from ``file``.

            Skips when the file is absent or the key is not present.

            :param file: The absolute file path.
            :param key: The setting name.

            :returns: The :class:`UbuntuConfigActionResult`.
        """
        write_command = ["tee", file]

        existing = self._read_file(path=file)
        if existing is None:
            skipped = self._build_result(
                status=UbuntuConfigActionStatus.SKIPPED,
                file=file,
                key=key,
                command=write_command,
                message="file does not exist",
            )
            return skipped

        new_content = _remove_shell_kv(content=existing, key=key)
        if new_content == existing:
            skipped = self._build_result(
                status=UbuntuConfigActionStatus.SKIPPED,
                file=file,
                key=key,
                command=write_command,
                message="key not present in file",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuConfigActionStatus.DRY_RUN,
                file=file,
                key=key,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code == 0:
            mapped_status = UbuntuConfigActionStatus.SUCCESS
        else:
            mapped_status = UbuntuConfigActionStatus.FAILED

        rtnval = UbuntuConfigActionResult(
            status=mapped_status,
            detail=key,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            file=file,
            key=key,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Whole-file write / reload verbs
    # ------------------------------------------------------------------

    def reload_configuration(
        self,
        service: str,
    ) -> UbuntuConfigActionResult:
        """
            Reload a backing service via ``systemctl reload <service>``.

            Never skips.  Honours dry-run.

            :param service: The systemd unit name.

            :returns: The :class:`UbuntuConfigActionResult`.
        """
        command = ["systemctl", "reload", service]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuConfigActionStatus.DRY_RUN,
                file=None,
                key=None,
                command=command,
                message=service,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = UbuntuConfigActionStatus.SUCCESS
        else:
            mapped_status = UbuntuConfigActionStatus.FAILED

        rtnval = UbuntuConfigActionResult(
            status=mapped_status,
            detail=service,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            file=None,
            key=None,
        )
        return rtnval

    def backup_configuration(self, file: str) -> UbuntuConfigActionResult:
        """
            Copy ``file`` to a sibling timestamped backup.

            No idempotency: every successful invocation creates a fresh
            backup with a new timestamp suffix.  Honours dry-run.

            :param file: The absolute file path.

            :returns: The :class:`UbuntuConfigActionResult` with
                      ``backup_path`` populated on success.
        """
        backup_path = _build_backup_path(
            file=file,
            timestamp_format=self._config.backup_suffix_format,
            extension=self._config.backup_extension,
        )
        command = ["cp", "-p", file, backup_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuConfigActionStatus.DRY_RUN,
                file=file,
                key=None,
                command=command,
                backup_path=backup_path,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = UbuntuConfigActionStatus.SUCCESS
            populated_backup = backup_path
        else:
            mapped_status = UbuntuConfigActionStatus.FAILED
            populated_backup = None

        rtnval = UbuntuConfigActionResult(
            status=mapped_status,
            detail=file,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            file=file,
            key=None,
            backup_path=populated_backup,
        )
        return rtnval

    def restore_configuration(
        self,
        backup_path: str,
        target_path: str,
    ) -> UbuntuConfigActionResult:
        """
            Restore ``backup_path`` over ``target_path``.

            No idempotency: every successful invocation overwrites the
            target.  Honours dry-run.

            :param backup_path: The backup file to copy from.
            :param target_path: The destination configuration file.

            :returns: The :class:`UbuntuConfigActionResult`.
        """
        command = ["cp", "-p", backup_path, target_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuConfigActionStatus.DRY_RUN,
                file=target_path,
                key=None,
                command=command,
                backup_path=backup_path,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = UbuntuConfigActionStatus.SUCCESS
        else:
            mapped_status = UbuntuConfigActionStatus.FAILED

        rtnval = UbuntuConfigActionResult(
            status=mapped_status,
            detail=target_path,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            file=target_path,
            key=None,
            backup_path=backup_path,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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

    def _build_result(
        self,
        *,
        status: UbuntuConfigActionStatus,
        file: Optional[str],
        key: Optional[str],
        command: list[str],
        backup_path: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuConfigActionResult:
        """
            Construct an :class:`UbuntuConfigActionResult` without invoking
            the runner.

            :param status: The outcome status.
            :param file: The file path to echo on the result.
            :param key: The key to echo on the result.
            :param command: The would-have-been argv.
            :param backup_path: Populated for backup / restore actions.
            :param message: Human-readable detail.
            :param stderr: Synthetic stderr payload.

            :returns: The :class:`UbuntuConfigActionResult`.
        """
        rtnval = UbuntuConfigActionResult(
            status=status,
            detail=key if key is not None else file,
            command=list(command),
            file=file,
            key=key,
            backup_path=backup_path,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _parse_shell_kv(*, text: str) -> dict[str, str]:
    """
        Parse a shell-style ``KEY=VALUE`` file into a dict.

        Comment (``#``) and blank lines are skipped.  Surrounding double
        quotes around values are stripped.  Lines with non-identifier
        keys are silently skipped.

        :param text: Raw file content.

        :returns: A ``dict[str, str]`` of every parsed entry.
    """
    rtnval: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) == 0 or line.startswith("#"):
            continue
        match = _SHELL_KV_PATTERN.match(line)
        if match is None:
            continue
        key = match.group(1)
        raw_value = match.group(2)
        rtnval[key] = _strip_quotes(value=raw_value)
    return rtnval


def _set_shell_kv(*, content: str, key: str, value: str) -> str:
    """
        Return ``content`` updated to contain ``KEY="value"``.

        Existing lines for the same key are replaced in place.  Comment
        and blank lines are preserved verbatim.  An empty starting
        content yields a single-line file ending in ``\\n``.

        :param content: Current file content.
        :param key: The setting name.
        :param value: The desired value.

        :returns: The updated file content.
    """
    escaped = _escape_value(value=value)
    new_line = '{}="{}"'.format(key, escaped)

    if len(content) == 0:
        body = new_line + "\n"
        return body

    lines = content.splitlines()
    updated: list[str] = []
    replaced = False
    for line in lines:
        match = _SHELL_KV_PATTERN.match(line.strip())
        if match is not None and match.group(1) == key:
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


def _remove_shell_kv(*, content: str, key: str) -> str:
    """
        Return ``content`` with any matching ``KEY=`` line removed.

        :param content: Current file content.
        :param key: The setting name to remove.

        :returns: The updated file content.
    """
    if len(content) == 0:
        return content

    lines = content.splitlines()
    kept: list[str] = []
    for line in lines:
        match = _SHELL_KV_PATTERN.match(line.strip())
        if match is not None and match.group(1) == key:
            continue
        kept.append(line)

    body = "\n".join(kept)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body


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


def _build_backup_path(
    *,
    file: str,
    timestamp_format: str,
    extension: str,
) -> str:
    """
        Render a sibling timestamped backup path.

        :param file: The original file path.
        :param timestamp_format: ``strftime`` format.
        :param extension: Trailing extension (e.g. ``.bak``).

        :returns: The new backup path.
    """
    timestamp = datetime.now(timezone.utc).strftime(timestamp_format)
    rtnval = "{}.{}{}".format(file, timestamp, extension)
    return rtnval
