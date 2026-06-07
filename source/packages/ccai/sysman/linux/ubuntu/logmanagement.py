"""
    Ubuntu log-management feature.

    Implements the cross-OS log-management contract for Ubuntu using
    ``journalctl`` for the systemd journal plus ``logrotate`` drop-ins
    under ``/etc/logrotate.d/`` for plain-file rotation policies.

    File I/O policy:
        Every file read / write / probe / delete is performed through the
        injected :class:`ICommandRunner` (``cat``, ``tee``, ``rm``,
        ``test -e``, ``ls -1``, ``mkdir -p``).  This module does NOT
        consume an :class:`IFileOperator`.  The drafted spec names
        ``IFileOperator`` as a collaborator; that wording is a
        forward-looking design note and the current implementation
        deviates from it pending introduction of a concrete file-operator
        abstraction in this codebase.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads and
        idempotency probes never elevate.

    Surface choices:
        * ``get_log`` issues ``journalctl -u <name> -n <lines> [--since
          <iso>] --no-pager -o json`` and parses one JSON object per
          stdout line into a :class:`UbuntuLogEntry`.
        * ``tail_log`` is implemented as a generator that consumes the
          stdout of ``journalctl -u <name> -f --no-pager -o json``
          line-by-line.  Tests bound the iterator by seeding a finite
          ``stdout`` payload.
        * ``rotate_log`` invokes ``logrotate -f /etc/logrotate.d/<name>``
          when the per-name drop-in exists, or falls back to
          ``logrotate -f /etc/logrotate.conf``.
        * ``truncate_log`` is the journalctl-managed truncation path —
          ``journalctl --vacuum-size=1K`` — which is the smallest legal
          floor accepted by current journald releases.
        * ``configure_rotation`` writes
          ``/etc/logrotate.d/<name>`` with the rendered policy via
          ``tee`` over stdin.
        * ``list_logs`` enumerates loaded systemd service units plus the
          drop-ins under :attr:`UbuntuLogManagerConfig.logrotate_d`.
"""

import json
import os

from dataclasses import dataclass, field
from typing import Iterator, Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_JOURNALCTL = "journalctl"
_LOGROTATE = "logrotate"
_SYSTEMCTL = "systemctl"

_DEFAULT_LOGROTATE_D = "/etc/logrotate.d"
_DEFAULT_LOGROTATE_CONF = "/etc/logrotate.conf"

_PRIORITY_NAMES = {
    0: "emerg",
    1: "alert",
    2: "crit",
    3: "err",
    4: "warning",
    5: "notice",
    6: "info",
    7: "debug",
}


UbuntuLogActionStatus = ActionStatus


@dataclass
class UbuntuLogManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuLogManager`.  Inherits ``dry_run``
        and ``elevated`` from :class:`FeatureConfig`.

        :param logrotate_d: Directory holding logrotate drop-ins.
                            Defaults to ``/etc/logrotate.d``.
        :param logrotate_conf: Fallback logrotate config used when no
                               per-name drop-in exists.  Defaults to
                               ``/etc/logrotate.conf``.
    """
    logrotate_d: str = _DEFAULT_LOGROTATE_D
    logrotate_conf: str = _DEFAULT_LOGROTATE_CONF


@dataclass
class UbuntuLogActionResult(FeatureActionResult):
    """
        Result of a log-management write operation.

        :param name: The log source name targeted.
        :param path: The file path the action targeted, when applicable
                     (logrotate drop-in path or plain-file log path).
        :param message: Optional human-readable detail for ``SKIPPED`` /
                        ``FAILED`` outcomes.
    """
    name: Optional[str] = None
    path: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuLogEntry:
    """
        Single parsed entry from ``journalctl -o json``.

        :param timestamp: ISO-8601 timestamp string assembled from the
                          ``__REALTIME_TIMESTAMP`` field; ``None`` when
                          the field is missing or unparseable.
        :param priority: Integer syslog priority from ``PRIORITY``;
                         ``None`` when the field is missing or
                         unparseable.
        :param unit: Originating systemd unit (``_SYSTEMD_UNIT``), or
                     ``SYSLOG_IDENTIFIER`` when the unit field is
                     absent.  May be ``None``.
        :param message: The ``MESSAGE`` field; empty string when
                        absent.
    """
    timestamp: Optional[str]
    priority: Optional[int]
    unit: Optional[str]
    message: str


@dataclass
class UbuntuLogEntries:
    """
        Wrapper around a list of :class:`UbuntuLogEntry` plus query
        metadata.

        :param source: The log source identifier the query targeted
                       (typically the unit name).
        :param entries: The parsed entries in the order they were
                        emitted by ``journalctl``.
        :param total_count: ``len(entries)``; recorded explicitly for
                            convenience and to match the contract's
                            metadata shape.
        :param raw: Raw stdout lines preserved for debuggability —
                    populated only when at least one line failed to
                    parse.
    """
    source: str
    entries: list[UbuntuLogEntry]
    total_count: int
    raw: list[str] = field(default_factory=list)


@dataclass
class UbuntuLogInfo:
    """
        One row from :meth:`UbuntuLogManager.list_logs`.

        :param name: Log source name.  For systemd units this is the
                     unit name with the ``.service`` suffix stripped;
                     for logrotate drop-ins it is the filename stem
                     under :attr:`UbuntuLogManagerConfig.logrotate_d`.
        :param source: ``"journald"`` for systemd units, ``"logrotate"``
                       for drop-in files.
        :param size_bytes: Reserved for future ``stat`` integration.
                           ``None`` for the current pilot — neither
                           ``journalctl --list-boots`` nor the
                           ``ls -1`` enumeration produces a size
                           column.
        :param last_rotated: Reserved for future ``stat`` integration.
                             ``None`` for the current pilot.
    """
    name: str
    source: str
    size_bytes: Optional[int]
    last_rotated: Optional[str]


@dataclass
class UbuntuRotationPolicy:
    """
        Rotation policy rendered into a logrotate drop-in.

        :param frequency: ``"daily"`` / ``"weekly"`` / ``"monthly"``.
        :param rotate_count: Number of rotated copies to keep.
        :param compress: When True, emit ``compress``.
        :param delaycompress: When True, emit ``delaycompress``.
        :param missingok: When True, emit ``missingok``.
        :param notifempty: When True, emit ``notifempty``.
        :param size: Optional size threshold (e.g. ``"100M"``) emitted
                     as a ``size <value>`` line.
    """
    frequency: str
    rotate_count: int = 7
    compress: bool = True
    delaycompress: bool = True
    missingok: bool = True
    notifempty: bool = True
    size: Optional[str] = None


class UbuntuLogQueryError(Exception):
    """
        Raised when a log read fails for a reason other than the source
        simply being absent.
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

        err_msg_lines = ["Log query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuLogManager(BaseFeatureManager):
    """
        Ubuntu log manager.  Every verb is dispatched through the
        injected :class:`ICommandRunner`; file I/O for logrotate
        drop-ins is performed via ``cat`` / ``tee`` / ``rm`` /
        ``test -e`` / ``ls -1`` rather than a separate file operator.
    """

    result_class = UbuntuLogActionResult
    status_class = UbuntuLogActionStatus
    feature_name = "log-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_log(
        self,
        name: str,
        lines: int = 100,
        since: Optional[str] = None,
    ) -> UbuntuLogEntries:
        """
            Retrieve recent journal entries for a systemd unit.

            Runs ``journalctl -u <name> -n <lines> [--since <iso>]
            --no-pager -o json`` and parses one JSON object per stdout
            line.  Reads never elevate.

            :param name: The systemd unit name (e.g. ``nginx.service``).
            :param lines: Maximum number of entries to return.  Defaults
                          to 100.
            :param since: Optional ISO-8601 lower bound passed to
                          ``--since``.  ``None`` omits the flag.

            :returns: The :class:`UbuntuLogEntries` describing the
                      result.
            :raises UbuntuLogQueryError: When ``journalctl`` exits
                                          non-zero with a non-empty
                                          stderr.
        """
        command = [
            _JOURNALCTL,
            "-u", name,
            "-n", str(lines),
        ]
        if since is not None and len(since) > 0:
            command.append("--since")
            command.append(since)
        command.append("--no-pager")
        command.append("-o")
        command.append("json")

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise UbuntuLogQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        entries, raw = _parse_journal_json(text=run_result.stdout)
        rtnval = UbuntuLogEntries(
            source=name,
            entries=entries,
            total_count=len(entries),
            raw=raw,
        )
        return rtnval

    def tail_log(
        self,
        name: str,
        follow: bool = True,
    ) -> Iterator[UbuntuLogEntry]:
        """
            Stream journal entries for a systemd unit.

            Runs ``journalctl -u <name> [-f] --no-pager -o json`` and
            yields one :class:`UbuntuLogEntry` per JSON line emitted by
            the runner.  Tests bound the iterator by seeding a finite
            ``stdout`` payload; in production the ``-f`` flag yields
            entries until the runner is closed.

            :param name: The systemd unit name.
            :param follow: When True, attaches the ``-f`` flag.  When
                           False, yields the backlog and terminates.

            :returns: A generator of :class:`UbuntuLogEntry`.  Never
                      elevates.
        """
        command = [_JOURNALCTL, "-u", name]
        if follow:
            command.append("-f")
        command.append("--no-pager")
        command.append("-o")
        command.append("json")

        run_result = self._runner.run(list(command), elevated=False)

        for raw_line in run_result.stdout.splitlines():
            stripped = raw_line.strip()
            if len(stripped) == 0:
                continue
            entry = _parse_one_journal_line(line=stripped)
            if entry is not None:
                yield entry

        return

    def list_logs(self) -> list[UbuntuLogInfo]:
        """
            Enumerate available log sources.

            Combines ``systemctl list-units --type=service --no-legend``
            (each unit becomes a ``"journald"`` source) with an
            ``ls -1 <logrotate_d>`` listing of every file under the
            configured logrotate drop-in directory (each becomes a
            ``"logrotate"`` source).  Reads never elevate.

            :returns: One :class:`UbuntuLogInfo` per discovered source.
        """
        rtnval: list[UbuntuLogInfo] = []

        units_command = [
            _SYSTEMCTL, "list-units",
            "--type=service", "--no-legend", "--plain",
        ]
        units_result = self._runner.run(list(units_command), elevated=False)
        if units_result.exit_code == 0:
            for raw_line in units_result.stdout.splitlines():
                line = raw_line.strip()
                if len(line) == 0:
                    continue
                parts = line.split(None, 1)
                if len(parts) == 0:
                    continue
                unit_name = parts[0]
                if unit_name.endswith(".service"):
                    base_name = unit_name[: -len(".service")]
                else:
                    base_name = unit_name
                info = UbuntuLogInfo(
                    name=base_name,
                    source="journald",
                    size_bytes=None,
                    last_rotated=None,
                )
                rtnval.append(info)

        ls_command = ["ls", "-1", self._config.logrotate_d]
        ls_result = self._runner.run(list(ls_command), elevated=False)
        if ls_result.exit_code == 0:
            for raw_entry in ls_result.stdout.splitlines():
                entry = raw_entry.strip()
                if len(entry) == 0:
                    continue
                info = UbuntuLogInfo(
                    name=entry,
                    source="logrotate",
                    size_bytes=None,
                    last_rotated=None,
                )
                rtnval.append(info)

        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def rotate_log(self, name: str) -> UbuntuLogActionResult:
        """
            Force-rotate a log via ``logrotate``.

            Uses ``logrotate -f <logrotate_d>/<name>`` when the per-name
            drop-in exists; falls back to ``logrotate -f <logrotate_conf>``
            otherwise.  No idempotency guard: rotation is always
            attempted in non-dry-run mode.

            :param name: The log source name (drop-in stem).

            :returns: The :class:`UbuntuLogActionResult`.
        """
        drop_in_path = self._dropin_path(name=name)

        if self._path_exists(path=drop_in_path):
            target_path = drop_in_path
        else:
            target_path = self._config.logrotate_conf

        command = [_LOGROTATE, "-f", target_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuLogActionStatus.DRY_RUN,
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
            mapped_status = UbuntuLogActionStatus.SUCCESS
        else:
            mapped_status = UbuntuLogActionStatus.FAILED

        rtnval = UbuntuLogActionResult(
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

    def truncate_log(self, name: str) -> UbuntuLogActionResult:
        """
            Truncate the journald-managed archive down to its minimum
            footprint via ``journalctl --vacuum-size=1K``.

            No idempotency guard.  In non-dry-run mode this verb always
            invokes the runner.

            :param name: Echoed onto the result; not part of the
                         dispatched argv (``--vacuum-size`` operates on
                         the journal as a whole).

            :returns: The :class:`UbuntuLogActionResult`.
        """
        command = [_JOURNALCTL, "--vacuum-size=1K"]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuLogActionStatus.DRY_RUN,
                name=name,
                path=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuLogActionStatus.SUCCESS
        else:
            mapped_status = UbuntuLogActionStatus.FAILED

        rtnval = UbuntuLogActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            name=name,
            path=None,
        )
        return rtnval

    def configure_rotation(
        self,
        name: str,
        policy: UbuntuRotationPolicy,
    ) -> UbuntuLogActionResult:
        """
            Write a logrotate drop-in for ``name`` containing the
            rendered ``policy``.

            Skipped when the existing drop-in's content matches the
            rendered desired body byte-for-byte.

            :param name: The log source name (drop-in stem).
            :param policy: The :class:`UbuntuRotationPolicy` to render.

            :returns: The :class:`UbuntuLogActionResult`.
        """
        path = self._dropin_path(name=name)
        desired = _render_logrotate(name=name, policy=policy)

        write_command = ["tee", path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuLogActionStatus.DRY_RUN,
                name=name,
                path=path,
                command=write_command,
            )
            return dry

        existing = self._read_file(path=path)
        if existing is not None and existing == desired:
            skipped = self._build_result(
                status=UbuntuLogActionStatus.SKIPPED,
                name=name,
                path=path,
                command=write_command,
                message="rotation policy already matches desired content",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=desired,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuLogActionStatus.SUCCESS
        else:
            mapped_status = UbuntuLogActionStatus.FAILED

        rtnval = UbuntuLogActionResult(
            status=mapped_status,
            detail=name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            name=name,
            path=path,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dropin_path(self, *, name: str) -> str:
        """
            Build the absolute path to the logrotate drop-in for
            ``name``.

            :param name: The log source name (drop-in stem).

            :returns: ``<logrotate_d>/<name>``.
        """
        rtnval = "{}/{}".format(self._config.logrotate_d, name)
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

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat``.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when the
                      file is missing.
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
        status: UbuntuLogActionStatus,
        name: Optional[str],
        path: Optional[str],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuLogActionResult:
        """
            Construct an :class:`UbuntuLogActionResult` without invoking
            the runner.

            :param status: The outcome status.
            :param name: The log source name to echo onto the result.
            :param path: The file path to echo onto the result.
            :param command: The would-have-been argv (copied for capture).
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`UbuntuLogActionResult`.
        """
        rtnval = UbuntuLogActionResult(
            status=status,
            detail=name,
            command=list(command),
            name=name,
            path=path,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level renderers and parsers
# ----------------------------------------------------------------------


def _render_logrotate(
    *,
    name: str,
    policy: UbuntuRotationPolicy,
) -> str:
    """
        Render a :class:`UbuntuRotationPolicy` into a logrotate drop-in
        body.

        Output is byte-stable for matching inputs so that
        :meth:`UbuntuLogManager.configure_rotation` can compare against
        the existing file content for idempotency.

        :param name: The log source name.  The wrapper expression in
                     the rendered body is ``/var/log/<name>.log``.
        :param policy: The :class:`UbuntuRotationPolicy` to render.

        :returns: The full file body as a string ending in ``\\n``.
    """
    log_path = "/var/log/{}.log".format(name)

    lines: list[str] = []
    lines.append("{} {{".format(log_path))
    lines.append("    {}".format(policy.frequency))
    lines.append("    rotate {}".format(policy.rotate_count))
    if policy.size is not None and len(policy.size) > 0:
        lines.append("    size {}".format(policy.size))
    if policy.compress:
        lines.append("    compress")
    if policy.delaycompress:
        lines.append("    delaycompress")
    if policy.missingok:
        lines.append("    missingok")
    if policy.notifempty:
        lines.append("    notifempty")
    lines.append("}")

    rtnval = "\n".join(lines) + "\n"
    return rtnval


def _parse_journal_json(*, text: str) -> tuple:
    """
        Parse the line-oriented JSON output of ``journalctl -o json``.

        Walks every non-empty line; lines that fail to decode as a JSON
        object are appended to a ``raw`` collector and skipped from the
        parsed entries.

        :param text: Raw stdout text.

        :returns: A two-tuple ``(entries, raw)`` where ``entries`` is a
                  list of :class:`UbuntuLogEntry` and ``raw`` is the
                  list of unparseable lines.
    """
    entries: list[UbuntuLogEntry] = []
    raw: list[str] = []

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue

        entry = _parse_one_journal_line(line=stripped)
        if entry is None:
            raw.append(stripped)
            continue
        entries.append(entry)

    rtnval = (entries, raw)
    return rtnval


def _parse_one_journal_line(*, line: str) -> Optional[UbuntuLogEntry]:
    """
        Parse a single ``journalctl -o json`` line into a
        :class:`UbuntuLogEntry`.

        :param line: One JSON line.

        :returns: The :class:`UbuntuLogEntry`, or ``None`` when the line
                  cannot be decoded as a JSON object.
    """
    try:
        decoded = json.loads(line)
    except (ValueError, TypeError):
        rtnval: Optional[UbuntuLogEntry] = None
        return rtnval

    if not isinstance(decoded, dict):
        rtnval = None
        return rtnval

    timestamp_raw = decoded.get("__REALTIME_TIMESTAMP", None)
    timestamp: Optional[str] = None
    if isinstance(timestamp_raw, str) and len(timestamp_raw) > 0:
        timestamp = timestamp_raw
    elif isinstance(timestamp_raw, int):
        timestamp = str(timestamp_raw)

    priority_raw = decoded.get("PRIORITY", None)
    priority: Optional[int] = None
    if isinstance(priority_raw, int):
        priority = priority_raw
    elif isinstance(priority_raw, str):
        try:
            priority = int(priority_raw)
        except ValueError:
            priority = None

    unit_raw = decoded.get("_SYSTEMD_UNIT", None)
    if isinstance(unit_raw, str) and len(unit_raw) > 0:
        unit: Optional[str] = unit_raw
    else:
        ident = decoded.get("SYSLOG_IDENTIFIER", None)
        if isinstance(ident, str) and len(ident) > 0:
            unit = ident
        else:
            unit = None

    message_raw = decoded.get("MESSAGE", "")
    if isinstance(message_raw, str):
        message = message_raw
    elif isinstance(message_raw, list):
        # journalctl emits MESSAGE as a list of byte values when the
        # payload is non-UTF-8.  We coerce to a string for the entry
        # surface.
        message = " ".join(str(item) for item in message_raw)
    else:
        message = str(message_raw)

    rtnval = UbuntuLogEntry(
        timestamp=timestamp,
        priority=priority,
        unit=unit,
        message=message,
    )
    return rtnval


def _priority_name(*, priority: int) -> str:
    """
        Map a numeric syslog priority to its canonical name.

        :param priority: Integer priority in [0, 7].

        :returns: The canonical priority name; ``"unknown"`` for
                  out-of-range values.
    """
    if priority in _PRIORITY_NAMES:
        rtnval = _PRIORITY_NAMES[priority]
    else:
        rtnval = "unknown"
    return rtnval
