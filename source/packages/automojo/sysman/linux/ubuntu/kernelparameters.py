"""
    Ubuntu kernel-parameters feature.

    Implements the cross-OS kernel-parameters contract for Ubuntu using
    ``sysctl`` for runtime reads and writes plus drop-in files under
    ``/etc/sysctl.d/`` for persistence.  Every verb is dispatched through
    an :class:`ICommandRunner` so the manager works identically under
    local and SSH execution.

    File I/O policy:
        Drop-in files are read with ``cat`` and written with ``tee``
        through the runner.  This module does NOT consume an
        :class:`IFileOperator`; the spec mentions one as a forward-looking
        collaborator but the established Ubuntu pattern routes file I/O
        through the runner so dry-run, elevation, and aspect composition
        compose uniformly.

    Elevation policy:
        ``set_parameter``, ``reload_parameters``, and
        ``remove_persisted_parameter`` honour ``config.elevated``.  Every
        read (``get_parameter``, ``list_parameters``, idempotency
        probes) never elevates.
"""

import os

from dataclasses import dataclass
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_SYSCTL = "sysctl"

_HEADER_COMMENT = "# managed by automojo-sysman-linux-ubuntu"

_ALLOWED_PREFIXES = (
    "kernel.",
    "vm.",
    "net.",
    "fs.",
    "user.",
    "abi.",
    "dev.",
    "debug.",
    "crypto.",
)


UbuntuKernelParameterActionStatus = ActionStatus


@dataclass
class UbuntuKernelParameterManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuKernelParameterManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param sysctl_d_path: Drop-in directory used for persistence.  The
                              default ``/etc/sysctl.d`` matches the upstream
                              sysctl-discovered path.
        :param default_basename: Filename (within ``sysctl_d_path``) used by
                                 :meth:`UbuntuKernelParameterManager.set_parameter`
                                 when the caller does not supply an explicit
                                 source basename.  The 99-prefix ensures the
                                 file applies last, overriding distro defaults.
    """
    sysctl_d_path: str = "/etc/sysctl.d"
    default_basename: str = "99-automojo.conf"


@dataclass
class UbuntuKernelParameterActionResult(FeatureActionResult):
    """
        Result of a kernel-parameter management write operation.

        :param name: The parameter name targeted, or ``None`` for
                     :meth:`UbuntuKernelParameterManager.reload_parameters`.
        :param value: The value written (or would-be in ``DRY_RUN``); ``None``
                      for ``reload_parameters`` and ``remove_persisted_parameter``.
        :param persistent: Whether the write targeted persistent storage in
                           addition to the runtime surface.  ``None`` for verbs
                           that do not split runtime vs persistent (e.g.
                           ``reload_parameters``).
        :param current_value: Pre-action runtime value, when known.
        :param applied_value: Post-action runtime value (or would-be on
                              ``DRY_RUN`` / ``SKIPPED``).
        :param requires_reboot: Always ``False`` on Ubuntu sysctl writes; kept
                                on the result for cross-OS contract conformance.
        :param source: The drop-in file path that was (or would have been)
                       modified for persistent writes.  ``None`` for runtime-
                       only writes.
        :param diff: Reserved for a future dry-run unified-diff field; left
                     ``None`` in this implementation.
        :param message: Optional human-readable detail for ``SKIPPED`` or
                        ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    value: Optional[str] = None
    persistent: Optional[bool] = None
    current_value: Optional[str] = None
    applied_value: Optional[str] = None
    requires_reboot: bool = False
    source: Optional[str] = None
    diff: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuParameterValue:
    """
        A single sysctl parameter and its current value.

        :param name: The dotted parameter name.
        :param value: The current string value (whitespace-stripped).
        :param is_default: Reserved tri-state describing whether the value
                           equals the kernel default.  ``None`` when not
                           computed (the default for this implementation).
    """
    name: str
    value: str
    is_default: Optional[bool] = None


class UbuntuKernelParameterQueryError(Exception):
    """
        Raised when a parameter read fails for a reason other than the
        parameter simply being unknown.
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

        err_msg_lines = ["Kernel parameter query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuKernelParameterManager(BaseFeatureManager):
    """
        Ubuntu sysctl kernel-parameter manager.  Every verb is dispatched
        through the injected :class:`ICommandRunner`; persistence file I/O
        is performed via ``cat`` / ``tee`` rather than a separate file
        operator.
    """

    result_class = UbuntuKernelParameterActionResult
    status_class = UbuntuKernelParameterActionStatus
    feature_name = "kernel-parameters"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_parameter(self, name: str) -> Optional[UbuntuParameterValue]:
        """
            Read the current runtime value of a sysctl parameter.

            Runs ``sysctl -n <name>`` without elevation.  Returns ``None``
            when the parameter is not present; raises
            :class:`UbuntuKernelParameterQueryError` for unexpected
            failures.

            :param name: The dotted parameter name.

            :returns: The :class:`UbuntuParameterValue`, or ``None`` when
                      the parameter does not exist.
            :raises UbuntuKernelParameterQueryError: When the runner reports
                                                     an unexpected failure.
        """
        command = [_SYSCTL, "-n", name]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code == 0:
            value = run_result.stdout.strip()
            rtnval: Optional[UbuntuParameterValue] = UbuntuParameterValue(
                name=name,
                value=value,
                is_default=None,
            )
            return rtnval

        stderr = run_result.stderr or ""
        if _is_cannot_stat(stderr=stderr):
            rtnval = None
            return rtnval

        raise UbuntuKernelParameterQueryError(
            command=command,
            exit_code=run_result.exit_code,
            stderr=stderr,
        )

    def list_parameters(self) -> dict[str, str]:
        """
            Enumerate every sysctl parameter and its current value.

            Runs ``sysctl -a --ignore`` without elevation.  Returns a
            ``dict`` of dotted name to string value.

            :returns: A ``dict[str, str]`` of every parsed parameter.
            :raises UbuntuKernelParameterQueryError: When the runner reports
                                                     a non-zero exit code.
        """
        command = [_SYSCTL, "-a", "--ignore"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuKernelParameterQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr or "",
            )

        rtnval = _parse_sysctl_a(stdout=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_parameter(
        self,
        name: str,
        value: str,
        persistent: bool = True,
        source_basename: Optional[str] = None,
    ) -> UbuntuKernelParameterActionResult:
        """
            Set a sysctl parameter at runtime and (optionally) persist it.

            When ``persistent=True``, the manager writes ``<name> = <value>``
            into ``<sysctl_d_path>/<source_basename>`` (defaulting to the
            configured ``default_basename``) AND applies the value at runtime
            via ``sysctl -w``.  When ``persistent=False``, only the runtime
            apply is performed.

            Idempotency:
                ``persistent=False`` skips when the runtime value already
                matches.  ``persistent=True`` skips only when BOTH the
                runtime value matches AND the persisted line already matches.

            :param name: The dotted parameter name.
            :param value: The desired string value.
            :param persistent: When True (default), update the drop-in file
                               in addition to the runtime surface.
            :param source_basename: Override for the drop-in filename.
                                    Defaults to
                                    :attr:`UbuntuKernelParameterManagerConfig.default_basename`.

            :returns: The :class:`UbuntuKernelParameterActionResult`.
        """
        if not _is_allowlisted(name=name):
            rejected = self._build_result(
                status=UbuntuKernelParameterActionStatus.FAILED,
                name=name,
                value=value,
                persistent=persistent,
                command=[_SYSCTL, "-w", "{}={}".format(name, value)],
                message="parameter_not_supported: {}".format(name),
                stderr="parameter_not_supported: {}".format(name),
            )
            return rejected

        runtime_command = [_SYSCTL, "-w", "{}={}".format(name, value)]

        if persistent:
            rtnval = self._set_parameter_persistent(
                name=name,
                value=value,
                source_basename=source_basename,
                runtime_command=runtime_command,
            )
            return rtnval

        rtnval = self._set_parameter_runtime_only(
            name=name,
            value=value,
            runtime_command=runtime_command,
        )
        return rtnval

    def reload_parameters(self) -> UbuntuKernelParameterActionResult:
        """
            Reload every persisted sysctl drop-in via ``sysctl --system``.

            Never skips.  Honours dry-run.

            :returns: The :class:`UbuntuKernelParameterActionResult`.
        """
        command = [_SYSCTL, "--system"]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuKernelParameterActionStatus.DRY_RUN,
                name=None,
                value=None,
                persistent=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = UbuntuKernelParameterActionStatus.SUCCESS
        else:
            mapped_status = UbuntuKernelParameterActionStatus.FAILED

        rtnval = UbuntuKernelParameterActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=None,
            value=None,
            persistent=None,
        )
        return rtnval

    def remove_persisted_parameter(
        self,
        name: str,
        source_basename: Optional[str] = None,
    ) -> UbuntuKernelParameterActionResult:
        """
            Remove a parameter from its persisted drop-in file.

            Reads the current contents of
            ``<sysctl_d_path>/<source_basename>``, drops the matching line,
            and writes the new content via ``tee``.  Skips when the file
            does not exist or the parameter is not present.

            :param name: The dotted parameter name.
            :param source_basename: Override for the drop-in filename.
                                    Defaults to
                                    :attr:`UbuntuKernelParameterManagerConfig.default_basename`.

            :returns: The :class:`UbuntuKernelParameterActionResult`.
        """
        basename = source_basename
        if basename is None:
            basename = self._config.default_basename
        path = self._dropin_path(basename=basename)
        write_command = ["tee", path]

        existing = self._read_file(path=path)
        if existing is None or len(existing) == 0:
            skipped = self._build_result(
                status=UbuntuKernelParameterActionStatus.SKIPPED,
                name=name,
                value=None,
                persistent=True,
                command=write_command,
                source=path,
                message="drop-in file is absent or empty",
            )
            return skipped

        new_content = _remove_sysctl_line(content=existing, name=name)
        if new_content == existing:
            skipped = self._build_result(
                status=UbuntuKernelParameterActionStatus.SKIPPED,
                name=name,
                value=None,
                persistent=True,
                command=write_command,
                source=path,
                message="parameter not present in drop-in",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuKernelParameterActionStatus.DRY_RUN,
                name=name,
                value=None,
                persistent=True,
                command=write_command,
                source=path,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code == 0:
            mapped_status = UbuntuKernelParameterActionStatus.SUCCESS
        else:
            mapped_status = UbuntuKernelParameterActionStatus.FAILED

        rtnval = UbuntuKernelParameterActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            name=name,
            value=None,
            persistent=True,
            source=path,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — set_parameter
    # ------------------------------------------------------------------

    def _set_parameter_runtime_only(
        self,
        *,
        name: str,
        value: str,
        runtime_command: list[str],
    ) -> UbuntuKernelParameterActionResult:
        """
            Apply ``sysctl -w`` without touching any persistent file.

            :param name: The parameter name.
            :param value: The desired value.
            :param runtime_command: Pre-built ``["sysctl", "-w", ...]`` argv.

            :returns: The :class:`UbuntuKernelParameterActionResult`.
        """
        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuKernelParameterActionStatus.DRY_RUN,
                name=name,
                value=value,
                persistent=False,
                command=runtime_command,
                applied_value=value,
            )
            return dry

        current_info = self.get_parameter(name=name)
        if current_info is not None:
            current_value = current_info.value
        else:
            current_value = None

        if current_value is not None and current_value == value:
            skipped = self._build_result(
                status=UbuntuKernelParameterActionStatus.SKIPPED,
                name=name,
                value=value,
                persistent=False,
                command=runtime_command,
                current_value=current_value,
                applied_value=value,
                message="runtime value already matches",
            )
            return skipped

        run_result = self._runner.run(
            list(runtime_command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = UbuntuKernelParameterActionStatus.SUCCESS
        else:
            mapped_status = UbuntuKernelParameterActionStatus.FAILED

        rtnval = UbuntuKernelParameterActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(runtime_command),
            name=name,
            value=value,
            persistent=False,
            current_value=current_value,
            applied_value=value,
        )
        return rtnval

    def _set_parameter_persistent(
        self,
        *,
        name: str,
        value: str,
        source_basename: Optional[str],
        runtime_command: list[str],
    ) -> UbuntuKernelParameterActionResult:
        """
            Persist a parameter into a drop-in file and apply at runtime.

            :param name: The parameter name.
            :param value: The desired value.
            :param source_basename: Override drop-in filename, or ``None``
                                    to use the configured default.
            :param runtime_command: Pre-built ``["sysctl", "-w", ...]`` argv.

            :returns: The :class:`UbuntuKernelParameterActionResult`.
        """
        basename = source_basename
        if basename is None:
            basename = self._config.default_basename
        path = self._dropin_path(basename=basename)
        write_command = ["tee", path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuKernelParameterActionStatus.DRY_RUN,
                name=name,
                value=value,
                persistent=True,
                command=write_command,
                source=path,
                applied_value=value,
            )
            return dry

        existing = self._read_file(path=path)
        if existing is None:
            existing_content = ""
        else:
            existing_content = existing
        desired_content = _set_sysctl_line(
            content=existing_content,
            name=name,
            value=value,
        )

        current_info = self.get_parameter(name=name)
        if current_info is not None:
            current_value = current_info.value
        else:
            current_value = None

        file_matches = (existing_content == desired_content)
        runtime_matches = (
            current_value is not None and current_value == value
        )

        if file_matches and runtime_matches:
            skipped = self._build_result(
                status=UbuntuKernelParameterActionStatus.SKIPPED,
                name=name,
                value=value,
                persistent=True,
                command=write_command,
                source=path,
                current_value=current_value,
                applied_value=value,
                message="runtime and drop-in already match",
            )
            return skipped

        if not file_matches:
            file_run = self._runner.run(
                list(write_command),
                elevated=self._config.elevated,
                stdin=desired_content,
            )
            if file_run.exit_code != 0:
                failed = UbuntuKernelParameterActionResult(
                    status=UbuntuKernelParameterActionStatus.FAILED,
                    detail=name,
                    exit_code=file_run.exit_code,
                    stdout=file_run.stdout,
                    stderr=file_run.stderr,
                    command=list(write_command),
                    name=name,
                    value=value,
                    persistent=True,
                    source=path,
                    current_value=current_value,
                    applied_value=None,
                    message="persist_failed",
                )
                return failed

        runtime_run = self._runner.run(
            list(runtime_command),
            elevated=self._config.elevated,
        )
        if runtime_run.exit_code != 0:
            failed = UbuntuKernelParameterActionResult(
                status=UbuntuKernelParameterActionStatus.FAILED,
                detail=name,
                exit_code=runtime_run.exit_code,
                stdout=runtime_run.stdout,
                stderr=runtime_run.stderr,
                command=list(runtime_command),
                name=name,
                value=value,
                persistent=True,
                source=path,
                current_value=current_value,
                applied_value=None,
                message="runtime_apply_failed",
            )
            return failed

        rtnval = UbuntuKernelParameterActionResult(
            status=UbuntuKernelParameterActionStatus.SUCCESS,
            detail=name,
            exit_code=runtime_run.exit_code,
            stdout=runtime_run.stdout,
            stderr=runtime_run.stderr,
            command=list(runtime_command),
            name=name,
            value=value,
            persistent=True,
            source=path,
            current_value=current_value,
            applied_value=value,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers — file I/O
    # ------------------------------------------------------------------

    def _dropin_path(self, *, basename: str) -> str:
        """
            Build the absolute drop-in path for a given basename.

            :param basename: Filename within the drop-in directory.

            :returns: The absolute path
                      ``<sysctl_d_path>/<basename>``.
        """
        rtnval = "{}/{}".format(self._config.sysctl_d_path, basename)
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

    def _build_result(
        self,
        *,
        status: UbuntuKernelParameterActionStatus,
        name: Optional[str],
        value: Optional[str],
        persistent: Optional[bool],
        command: list[str],
        current_value: Optional[str] = None,
        applied_value: Optional[str] = None,
        source: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuKernelParameterActionResult:
        """
            Construct an :class:`UbuntuKernelParameterActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param name: The parameter name to echo on the result.
            :param value: The would-be applied value.
            :param persistent: Whether the action targeted persistence.
            :param command: The would-have-been argv, copied for capture.
            :param current_value: Pre-action runtime value, when known.
            :param applied_value: Post-action runtime value.
            :param source: The drop-in file path, when applicable.
            :param message: Human-readable detail.
            :param stderr: Synthetic stderr payload.

            :returns: The :class:`UbuntuKernelParameterActionResult`.
        """
        rtnval = UbuntuKernelParameterActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            value=value,
            persistent=persistent,
            current_value=current_value,
            applied_value=applied_value,
            source=source,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _is_allowlisted(*, name: str) -> bool:
    """
        Return True when ``name`` begins with a recognised sysctl prefix.

        :param name: The dotted parameter name.

        :returns: True when the prefix is allowlisted; False otherwise.
    """
    for prefix in _ALLOWED_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


def _is_cannot_stat(*, stderr: str) -> bool:
    """
        Detect the "cannot stat" stderr emitted by ``sysctl -n`` for an
        unknown parameter.

        :param stderr: Captured stderr text.

        :returns: True when the stderr indicates an unknown parameter.
    """
    lower = stderr.lower()
    if "cannot stat" in lower:
        return True
    if "no such file or directory" in lower:
        return True
    if "unknown key" in lower:
        return True
    return False


def _parse_sysctl_a(*, stdout: str) -> dict[str, str]:
    """
        Parse the output of ``sysctl -a`` into a name-to-value dict.

        Lines without a ``=`` separator are silently skipped.

        :param stdout: Captured stdout text.

        :returns: A ``dict[str, str]`` of every parsed parameter.
    """
    rtnval: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if len(line) == 0:
            continue
        if "=" not in line:
            continue
        key_part, _, value_part = line.partition("=")
        key = key_part.strip()
        value = value_part.strip()
        if len(key) == 0:
            continue
        rtnval[key] = value
    return rtnval


def _set_sysctl_line(*, content: str, name: str, value: str) -> str:
    """
        Return drop-in content updated to contain ``<name> = <value>``.

        Existing lines for the same parameter are replaced in place.
        Comment and blank lines are preserved verbatim.  An empty starting
        ``content`` yields a header-prefixed file.

        :param content: Current drop-in file content.
        :param name: The parameter name.
        :param value: The desired value.

        :returns: The updated drop-in content terminated with a newline.
    """
    new_line = "{} = {}".format(name, value)

    if len(content) == 0:
        body = "{}\n{}\n".format(_HEADER_COMMENT, new_line)
        return body

    lines = content.splitlines()
    updated: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or len(stripped) == 0:
            updated.append(line)
            continue
        if "=" not in stripped:
            updated.append(line)
            continue
        key_part, _, _ = stripped.partition("=")
        existing_name = key_part.strip()
        if existing_name == name:
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


def _remove_sysctl_line(*, content: str, name: str) -> str:
    """
        Return drop-in content with any line matching ``name`` removed.

        Comment and blank lines are preserved.  When the file becomes
        comment-only the original content is returned (caller decides
        what to do).

        :param content: Current drop-in file content.
        :param name: The parameter name to remove.

        :returns: The updated drop-in content.
    """
    lines = content.splitlines()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or len(stripped) == 0:
            kept.append(line)
            continue
        if "=" not in stripped:
            kept.append(line)
            continue
        key_part, _, _ = stripped.partition("=")
        existing_name = key_part.strip()
        if existing_name == name:
            continue
        kept.append(line)

    body = "\n".join(kept)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body
