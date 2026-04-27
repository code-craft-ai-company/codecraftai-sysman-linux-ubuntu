"""
    Ubuntu filesystem-management feature.

    Implements the cross-OS filesystem-management contract for Ubuntu
    using ``mount`` / ``umount`` / ``findmnt`` / ``stat`` / ``chown``
    / ``chmod`` plus read-modify-write of ``/etc/fstab`` through the
    injected :class:`ICommandRunner`.

    File I/O policy:
        ``/etc/fstab`` is read with ``cat`` and written with ``tee``
        through the runner.  This module does NOT consume an
        :class:`IFileOperator`; the established Ubuntu pattern routes
        every read / write through the runner so dry-run, elevation,
        and aspect composition compose uniformly.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``get_mounts``, ``stat``, the idempotency probes) never
        elevate.

    Mount surface choices:
        * ``mount`` and ``umount`` use the iproute-adjacent classic
          util-linux invocations.  Persistent entries flow through
          ``/etc/fstab``.
        * ``get_mounts`` parses ``findmnt --real --json`` for richer
          information.  When ``findmnt`` is unavailable, callers can
          observe an empty list — the spec follow-up tracks the
          ``/proc/mounts`` fallback.
"""

import json
import os

from dataclasses import dataclass
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_MOUNT = "mount"
_UMOUNT = "umount"
_FINDMNT = "findmnt"
_STAT = "stat"
_CHOWN = "chown"
_CHMOD = "chmod"
_CAT = "cat"
_TEE = "tee"


UbuntuFilesystemActionStatus = ActionStatus


@dataclass
class UbuntuFilesystemManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuFilesystemManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param fstab_path: Path to the persistent mount table.
                           Defaults to ``/etc/fstab``; overridable for
                           testing and for unusual deployments.
    """
    fstab_path: str = "/etc/fstab"


@dataclass
class UbuntuFilesystemActionResult(FeatureActionResult):
    """
        Result of a filesystem-management write operation.

        :param path: The target path, when applicable.  ``None`` for
                     verbs that target a mount-table entry by device.
        :param device: The device targeted, when applicable.
        :param mountpoint: The mountpoint targeted, when applicable.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    path: Optional[str] = None
    device: Optional[str] = None
    mountpoint: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuFstabEntry:
    """
        Representation of a single ``/etc/fstab`` entry.

        :param device: Device or UUID specifier.
        :param mountpoint: Mount point path.
        :param fstype: Filesystem type.
        :param options: Mount options.  Defaults to ``"defaults"``.
        :param dump: Dump field.  Defaults to ``0``.
        :param pass_: ``fsck`` pass-number field.  Trailing underscore
                      avoids the ``pass`` keyword.  Defaults to ``0``.
    """
    device: str
    mountpoint: str
    fstype: str
    options: str = "defaults"
    dump: int = 0
    pass_: int = 0


@dataclass
class UbuntuMountInfo:
    """
        Information about an active mount as enumerated by
        :meth:`UbuntuFilesystemManager.get_mounts`.

        :param device: The backing device identifier.
        :param mountpoint: The mount point path.
        :param fstype: The filesystem type.
        :param options: The comma-separated options string.
        :param source: The raw ``source`` field reported by
                       ``findmnt``.  ``None`` when not reported.
    """
    device: str
    mountpoint: str
    fstype: str
    options: str
    source: Optional[str] = None


@dataclass
class UbuntuFileInfo:
    """
        Information returned by :meth:`UbuntuFilesystemManager.stat`.

        :param path: The absolute path queried.
        :param owner: Owner name.
        :param group: Group name.
        :param mode: Octal mode string (e.g. ``"644"``).
        :param size: Size in bytes.
        :param modified: Modification timestamp string as reported by
                         ``stat`` (the ``%y`` format).
    """
    path: str
    owner: str
    group: str
    mode: str
    size: int
    modified: str


class UbuntuFilesystemQueryError(Exception):
    """
        Raised when a filesystem read fails for a reason other than
        the target simply being absent.
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

        err_msg_lines = ["Filesystem query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuFilesystemManager(BaseFeatureManager):
    """
        Ubuntu filesystem manager.  Every verb is dispatched through
        the injected :class:`ICommandRunner`; file I/O for ``/etc/fstab``
        is performed via ``cat`` / ``tee`` rather than a separate file
        operator.
    """

    result_class = UbuntuFilesystemActionResult
    status_class = UbuntuFilesystemActionStatus
    feature_name = "filesystem-management"

    # ------------------------------------------------------------------
    # Mount write verbs
    # ------------------------------------------------------------------

    def mount(
        self,
        device: str,
        mountpoint: str,
        fstype: str,
        options: Optional[str] = None,
    ) -> UbuntuFilesystemActionResult:
        """
            Mount ``device`` at ``mountpoint`` with type ``fstype``.

            Skipped when an active mount already covers ``mountpoint``
            with the same device.

            :param device: The device to mount.
            :param mountpoint: The target directory.
            :param fstype: The filesystem type passed through ``-t``.
            :param options: Optional mount options passed through
                            ``-o``.

            :returns: The :class:`UbuntuFilesystemActionResult`.
        """
        command: list[str] = [_MOUNT, "-t", fstype]
        if options is not None and len(options) > 0:
            command.append("-o")
            command.append(options)
        command.append(device)
        command.append(mountpoint)

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuFilesystemActionStatus.DRY_RUN,
                command=command,
                path=mountpoint,
                device=device,
                mountpoint=mountpoint,
            )
            return dry

        if self._is_already_mounted(
            device=device,
            mountpoint=mountpoint,
        ):
            skipped = self._build_result(
                status=UbuntuFilesystemActionStatus.SKIPPED,
                command=command,
                path=mountpoint,
                device=device,
                mountpoint=mountpoint,
                message="device is already mounted at mountpoint",
            )
            return skipped

        rtnval = self._dispatch_filesystem_write(
            command=command,
            path=mountpoint,
            device=device,
            mountpoint=mountpoint,
        )
        return rtnval

    def unmount(
        self,
        mountpoint: str,
    ) -> UbuntuFilesystemActionResult:
        """
            Unmount ``mountpoint``.  Skipped when the mountpoint is
            not currently mounted.

            :param mountpoint: The mount point to unmount.

            :returns: The :class:`UbuntuFilesystemActionResult`.
        """
        command = [_UMOUNT, mountpoint]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuFilesystemActionStatus.DRY_RUN,
                command=command,
                path=mountpoint,
                mountpoint=mountpoint,
            )
            return dry

        if not self._is_mountpoint_mounted(mountpoint=mountpoint):
            skipped = self._build_result(
                status=UbuntuFilesystemActionStatus.SKIPPED,
                command=command,
                path=mountpoint,
                mountpoint=mountpoint,
                message="mountpoint is not currently mounted",
            )
            return skipped

        rtnval = self._dispatch_filesystem_write(
            command=command,
            path=mountpoint,
            mountpoint=mountpoint,
        )
        return rtnval

    # ------------------------------------------------------------------
    # fstab write verbs
    # ------------------------------------------------------------------

    def add_fstab_entry(
        self,
        spec: UbuntuFstabEntry,
    ) -> UbuntuFilesystemActionResult:
        """
            Append ``spec`` to ``/etc/fstab`` when no matching entry
            already exists.

            Skipped when the file already contains a non-comment line
            whose ``device``, ``mountpoint``, ``fstype``, and
            ``options`` fields all match ``spec``.

            :param spec: The :class:`UbuntuFstabEntry` to append.

            :returns: The :class:`UbuntuFilesystemActionResult`.
        """
        rendered_line = _render_fstab_line(entry=spec)
        write_command = [_TEE, "-a", self._fstab_path()]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuFilesystemActionStatus.DRY_RUN,
                command=write_command,
                path=self._fstab_path(),
                device=spec.device,
                mountpoint=spec.mountpoint,
            )
            return dry

        existing = self._read_fstab()
        if _fstab_contains_match(content=existing, spec=spec):
            skipped = self._build_result(
                status=UbuntuFilesystemActionStatus.SKIPPED,
                command=write_command,
                path=self._fstab_path(),
                device=spec.device,
                mountpoint=spec.mountpoint,
                message="fstab already contains a matching entry",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=rendered_line,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuFilesystemActionStatus.SUCCESS
        else:
            mapped_status = UbuntuFilesystemActionStatus.FAILED

        rtnval = UbuntuFilesystemActionResult(
            status=mapped_status,
            detail=spec.mountpoint,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            path=self._fstab_path(),
            device=spec.device,
            mountpoint=spec.mountpoint,
        )
        return rtnval

    def remove_fstab_entry(
        self,
        mountpoint: str,
    ) -> UbuntuFilesystemActionResult:
        """
            Remove every ``/etc/fstab`` line whose ``mountpoint`` field
            matches ``mountpoint``.  Comments and unrelated entries are
            preserved.

            Skipped when no matching entry exists.

            :param mountpoint: The mountpoint to evict from
                               ``/etc/fstab``.

            :returns: The :class:`UbuntuFilesystemActionResult`.
        """
        write_command = [_TEE, self._fstab_path()]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuFilesystemActionStatus.DRY_RUN,
                command=write_command,
                path=self._fstab_path(),
                mountpoint=mountpoint,
            )
            return dry

        existing = self._read_fstab()
        new_content, removed = _fstab_filter_out_mountpoint(
            content=existing,
            mountpoint=mountpoint,
        )
        if not removed:
            skipped = self._build_result(
                status=UbuntuFilesystemActionStatus.SKIPPED,
                command=write_command,
                path=self._fstab_path(),
                mountpoint=mountpoint,
                message="fstab does not contain a matching entry",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuFilesystemActionStatus.SUCCESS
        else:
            mapped_status = UbuntuFilesystemActionStatus.FAILED

        rtnval = UbuntuFilesystemActionResult(
            status=mapped_status,
            detail=mountpoint,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            path=self._fstab_path(),
            mountpoint=mountpoint,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Permissions write verbs
    # ------------------------------------------------------------------

    def chown(
        self,
        path: str,
        user: str,
        group: Optional[str] = None,
        recursive: bool = False,
    ) -> UbuntuFilesystemActionResult:
        """
            Change ``path``'s owner via ``chown``.  When ``group`` is
            supplied the ``user:group`` shorthand is emitted.

            Skipped (non-recursive only) when ``stat`` shows that
            ``path`` already has the requested owner / group.
            Recursive invocations always run because per-entry
            comparison is out of scope for this verb.

            :param path: The target path.
            :param user: The desired owner name.
            :param group: Optional desired group name.
            :param recursive: True to add ``-R``.

            :returns: The :class:`UbuntuFilesystemActionResult`.
        """
        if group is not None:
            owner_token = "{}:{}".format(user, group)
        else:
            owner_token = user

        command: list[str] = [_CHOWN]
        if recursive:
            command.append("-R")
        command.append(owner_token)
        command.append(path)

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuFilesystemActionStatus.DRY_RUN,
                command=command,
                path=path,
            )
            return dry

        if not recursive:
            current = self.stat(path=path)
            if current is not None:
                owner_matches = current.owner == user
                if group is None:
                    group_matches = True
                else:
                    group_matches = current.group == group
                if owner_matches and group_matches:
                    skipped = self._build_result(
                        status=UbuntuFilesystemActionStatus.SKIPPED,
                        command=command,
                        path=path,
                        message="path already has the requested owner/group",
                    )
                    return skipped

        rtnval = self._dispatch_filesystem_write(
            command=command,
            path=path,
        )
        return rtnval

    def chmod(
        self,
        path: str,
        mode: str,
        recursive: bool = False,
    ) -> UbuntuFilesystemActionResult:
        """
            Change ``path``'s permissions via ``chmod``.

            Skipped (non-recursive only) when ``stat`` shows that
            ``path`` already has the requested mode.  Recursive
            invocations always run.

            :param path: The target path.
            :param mode: The mode passed verbatim to ``chmod`` (numeric
                         like ``"0644"`` or symbolic like
                         ``"u+rwx"``).
            :param recursive: True to add ``-R``.

            :returns: The :class:`UbuntuFilesystemActionResult`.
        """
        command: list[str] = [_CHMOD]
        if recursive:
            command.append("-R")
        command.append(mode)
        command.append(path)

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuFilesystemActionStatus.DRY_RUN,
                command=command,
                path=path,
            )
            return dry

        if not recursive:
            current = self.stat(path=path)
            if current is not None and _mode_matches(
                requested=mode,
                observed=current.mode,
            ):
                skipped = self._build_result(
                    status=UbuntuFilesystemActionStatus.SKIPPED,
                    command=command,
                    path=path,
                    message="path already has the requested mode",
                )
                return skipped

        rtnval = self._dispatch_filesystem_write(
            command=command,
            path=path,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_mounts(self) -> list[UbuntuMountInfo]:
        """
            Enumerate active mounts via ``findmnt --real --json``.

            Reads do not elevate.  When ``findmnt`` exits non-zero an
            empty list is returned — typical for empty namespaces or
            absent ``util-linux``.  Malformed JSON is treated the same
            way.

            :returns: One :class:`UbuntuMountInfo` per mount, walking
                      every nested ``children`` collection breadth-
                      first.
        """
        command = [_FINDMNT, "--real", "--json"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list[UbuntuMountInfo] = []
            return rtnval

        rtnval = _parse_findmnt_output(output=run_result.stdout)
        return rtnval

    def stat(
        self,
        path: str,
    ) -> Optional[UbuntuFileInfo]:
        """
            Return :class:`UbuntuFileInfo` for ``path`` or ``None`` when
            ``path`` does not exist.

            Runs ``stat -c '%U:%G:%a:%s:%y' <path>``.  Reads do not
            elevate.

            :param path: The absolute path to query.

            :returns: The parsed info or ``None``.
        """
        command = [_STAT, "-c", "%U:%G:%a:%s:%y", path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[UbuntuFileInfo] = None
            return rtnval

        rtnval = _parse_stat_output(output=run_result.stdout, path=path)
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fstab_path(self) -> str:
        """
            Return the configured ``/etc/fstab`` path.
        """
        config = self._config
        if isinstance(config, UbuntuFilesystemManagerConfig):
            rtnval = config.fstab_path
        else:
            rtnval = "/etc/fstab"
        return rtnval

    def _read_fstab(self) -> str:
        """
            Read ``/etc/fstab`` via ``cat``.  Reads never elevate.

            :returns: The file's contents on success; the empty string
                      when ``cat`` reports a non-zero exit code (path
                      missing or unreadable).
        """
        command = [_CAT, self._fstab_path()]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = ""
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _is_mountpoint_mounted(self, *, mountpoint: str) -> bool:
        """
            Return True when ``mountpoint`` appears in ``get_mounts``.
        """
        mounts = self.get_mounts()
        rtnval = False
        for mount in mounts:
            if mount.mountpoint == mountpoint:
                rtnval = True
                break
        return rtnval

    def _is_already_mounted(
        self,
        *,
        device: str,
        mountpoint: str,
    ) -> bool:
        """
            Return True when an active mount matches ``device`` and
            ``mountpoint``.
        """
        mounts = self.get_mounts()
        rtnval = False
        for mount in mounts:
            if mount.mountpoint != mountpoint:
                continue
            if mount.device != device:
                continue
            rtnval = True
            break
        return rtnval

    def _dispatch_filesystem_write(
        self,
        *,
        command: list[str],
        path: Optional[str] = None,
        device: Optional[str] = None,
        mountpoint: Optional[str] = None,
    ) -> UbuntuFilesystemActionResult:
        """
            Dispatch a write command and wrap the result with the
            relevant filesystem-scope fields.
        """
        rtnval = self._dispatch(
            command=command,
            detail=path,
            operation="filesystem_write",
            path=path,
            device=device,
            mountpoint=mountpoint,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuFilesystemActionStatus,
        command: list[str],
        path: Optional[str] = None,
        device: Optional[str] = None,
        mountpoint: Optional[str] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuFilesystemActionResult:
        """
            Construct a :class:`UbuntuFilesystemActionResult` without
            invoking the runner.
        """
        rtnval = UbuntuFilesystemActionResult(
            status=status,
            detail=path if path is not None else mountpoint,
            command=list(command),
            path=path,
            device=device,
            mountpoint=mountpoint,
            message=message,
            stderr=stderr,
        )
        return rtnval


def _render_fstab_line(*, entry: UbuntuFstabEntry) -> str:
    """
        Render an :class:`UbuntuFstabEntry` into a single ``\\n``-
        terminated tab-separated line.
    """
    rtnval = "{}\t{}\t{}\t{}\t{}\t{}\n".format(
        entry.device,
        entry.mountpoint,
        entry.fstype,
        entry.options,
        entry.dump,
        entry.pass_,
    )
    return rtnval


def _fstab_contains_match(
    *,
    content: str,
    spec: UbuntuFstabEntry,
) -> bool:
    """
        Return True when ``content`` already contains a non-comment
        line that matches ``spec`` on device, mountpoint, fstype, and
        options.

        :param content: Raw fstab contents.
        :param spec: The entry to check for.
    """
    rtnval = False
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue

        fields = stripped.split()
        if len(fields) < 4:
            continue

        line_device = fields[0]
        line_mountpoint = fields[1]
        line_fstype = fields[2]
        line_options = fields[3]

        if line_device != spec.device:
            continue
        if line_mountpoint != spec.mountpoint:
            continue
        if line_fstype != spec.fstype:
            continue
        if line_options != spec.options:
            continue

        rtnval = True
        break

    return rtnval


def _fstab_filter_out_mountpoint(
    *,
    content: str,
    mountpoint: str,
) -> tuple[str, bool]:
    """
        Return ``(new_content, removed)`` where ``new_content`` omits
        every non-comment line whose mountpoint field matches
        ``mountpoint`` and ``removed`` is True when at least one such
        line was filtered out.

        Comments and unrelated entries are preserved verbatim.

        :param content: Raw fstab contents.
        :param mountpoint: The mountpoint to evict.
    """
    kept_lines: list[str] = []
    removed = False
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0 or stripped.startswith("#"):
            kept_lines.append(raw_line)
            continue

        fields = stripped.split()
        if len(fields) >= 2 and fields[1] == mountpoint:
            removed = True
            continue

        kept_lines.append(raw_line)

    if len(kept_lines) == 0:
        joined = ""
    else:
        joined = "\n".join(kept_lines) + "\n"

    rtnval = (joined, removed)
    return rtnval


def _parse_findmnt_output(*, output: str) -> list[UbuntuMountInfo]:
    """
        Parse ``findmnt --json`` output into a flat list of
        :class:`UbuntuMountInfo`.

        ``findmnt`` emits a tree structure in
        ``data["filesystems"][*]["children"]``.  We walk every level
        breadth-first.

        :param output: Raw JSON stdout.

        :returns: One :class:`UbuntuMountInfo` per parsed entry.
                  Returns an empty list on any JSON parse error.
    """
    rtnval: list[UbuntuMountInfo] = []

    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return rtnval

    filesystems = data.get("filesystems", [])
    if not isinstance(filesystems, list):
        return rtnval

    queue: list[dict] = []
    for entry in filesystems:
        if isinstance(entry, dict):
            queue.append(entry)

    while len(queue) > 0:
        head = queue.pop(0)

        device_value = head.get("source", "")
        mount_value = head.get("target", "")
        fstype_value = head.get("fstype", "")
        options_value = head.get("options", "")

        info = UbuntuMountInfo(
            device=device_value,
            mountpoint=mount_value,
            fstype=fstype_value,
            options=options_value,
            source=device_value,
        )
        rtnval.append(info)

        children = head.get("children", [])
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    queue.append(child)

    return rtnval


def _parse_stat_output(
    *,
    output: str,
    path: str,
) -> Optional[UbuntuFileInfo]:
    """
        Parse ``stat -c '%U:%G:%a:%s:%y'`` output into a
        :class:`UbuntuFileInfo`.

        :param output: Raw stdout.
        :param path: The path the stat was for.
    """
    line = output.strip()
    if len(line) == 0:
        rtnval: Optional[UbuntuFileInfo] = None
        return rtnval

    # ``%y`` (modified) may itself contain colons (timezone offsets,
    # microseconds) so we split with a max count of 4.
    parts = line.split(":", 4)
    if len(parts) < 5:
        rtnval = None
        return rtnval

    try:
        size_value = int(parts[3])
    except ValueError:
        size_value = 0

    rtnval = UbuntuFileInfo(
        path=path,
        owner=parts[0],
        group=parts[1],
        mode=parts[2],
        size=size_value,
        modified=parts[4],
    )
    return rtnval


def _mode_matches(*, requested: str, observed: str) -> bool:
    """
        Return True when ``requested`` and ``observed`` mode strings
        denote the same numeric mode.

        ``stat`` reports ``%a`` as the trailing-three-digit octal
        without a leading zero.  Callers may pass numeric forms with
        or without the leading zero (``"644"`` or ``"0644"``).
        Symbolic modes (``"u+rwx"``) cannot be compared without a full
        chmod evaluator and always return False so the caller falls
        through to the actual ``chmod`` invocation.

        :param requested: The mode the caller passed.
        :param observed: The mode reported by ``stat``.
    """
    if not _is_numeric_mode(requested):
        rtnval = False
        return rtnval
    if not _is_numeric_mode(observed):
        rtnval = False
        return rtnval

    requested_int = int(requested, 8)
    observed_int = int(observed, 8)
    rtnval = requested_int == observed_int
    return rtnval


def _is_numeric_mode(token: str) -> bool:
    """
        Return True when ``token`` consists only of octal digits.

        :param token: A mode string.
    """
    if len(token) == 0:
        rtnval = False
        return rtnval

    rtnval = True
    for ch in token:
        if ch not in "01234567":
            rtnval = False
            break
    return rtnval
