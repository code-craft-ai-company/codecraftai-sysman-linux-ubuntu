"""
    Ubuntu process-management feature.

    Implements the cross-OS process-management contract for Ubuntu using
    ``ps``, ``kill``, ``pkill``, ``pgrep``, ``renice``, and ``setsid``.
    Every verb is dispatched through an :class:`ICommandRunner` so the
    manager works identically under local and SSH execution.

    Verb / tool mapping:

    +------------------------+-----------------------------------------+
    | Verb                   | Ubuntu tool                             |
    +========================+=========================================+
    | ``list_processes``     | ``ps -eo pid,ppid,user,stat,pri,ni,vsz``|
    |                        | ``,rss,pcpu,pmem,etime,comm,args``      |
    |                        | ``--no-headers``                        |
    +------------------------+-----------------------------------------+
    | ``get_process``        | ``ps -p <pid> -o ...`` with the same    |
    |                        | format spec                             |
    +------------------------+-----------------------------------------+
    | ``kill_process``       | ``kill -<signal> <pid>``                |
    +------------------------+-----------------------------------------+
    | ``kill_process_tree``  | ``pkill -<signal> -P <pid>`` then       |
    |                        | ``kill -<signal> <pid>``                |
    +------------------------+-----------------------------------------+
    | ``start_process``      | ``setsid sh -c '<command>'`` via the    |
    |                        | runner (pilot blocks until completion)  |
    +------------------------+-----------------------------------------+
    | ``set_process_priority`` | ``renice <priority> -p <pid>``        |
    +------------------------+-----------------------------------------+
    | ``wait_for_process``   | poll ``kill -0 <pid>`` with sleep       |
    +------------------------+-----------------------------------------+
    | ``find_processes_by_name`` | ``pgrep -a <name>``                 |
    +------------------------+-----------------------------------------+

    Idempotency / dry-run / elevation:

    - ``kill_process`` / ``kill_process_tree`` SKIP when the target PID
      no longer exists (verified through ``get_process``).
    - ``set_process_priority`` SKIPs when the current ``ni`` value
      already matches the requested priority.
    - ``start_process`` is non-idempotent and never skips.
    - All writes honour ``config.dry_run`` and short-circuit to
      :attr:`UbuntuProcessActionStatus.DRY_RUN` without invoking the
      runner.
    - All writes pass ``elevated=config.elevated`` to the runner; the
      runner is responsible for applying ``sudo`` when ``elevated=True``.
    - All reads pass ``elevated=False`` regardless of config — reads
      never elevate per contract.

    Pilot limitation — start_process:
        ``setsid sh -c '<command>'`` is dispatched through the runner.
        For the pilot the runner blocks until the spawned shell exits;
        true detached / async-spawn semantics require a runner extension
        that captures the launched PID without waiting.  The
        ``UbuntuProcessActionResult.pid`` field is therefore best-effort:
        when the runner can return a successful exit code we synthesise
        a "best-known" PID by re-querying ``pgrep`` for the executable
        name; otherwise ``pid`` is ``None``.  See
        ``specs/3-process-management/tasks.md`` for the follow-up.
"""

import os
import time

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

_PS = "ps"
_KILL = "kill"
_PKILL = "pkill"
_PGREP = "pgrep"
_RENICE = "renice"
_SETSID = "setsid"
_SH = "sh"

_PS_FORMAT = (
    "pid,ppid,user,stat,pri,ni,vsz,rss,pcpu,pmem,etime,comm,args"
)

_DEFAULT_SIGNAL = "TERM"

# Subset of POSIX signal names accepted on the verb surface.  ``kill``
# happily accepts the bare name; we keep the list so callers can be
# rejected with a typed error rather than hitting a runner-side ``kill``
# error.
_KNOWN_SIGNALS = frozenset({
    "TERM", "KILL", "HUP", "INT", "QUIT", "USR1", "USR2",
})


# Backwards-compatible alias matching the contract's naming.
UbuntuProcessActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class UbuntuProcessManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuProcessManager`.

        Inherits ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param default_signal: Signal name (without the ``SIG`` prefix)
                               used when ``kill_process`` /
                               ``kill_process_tree`` are called without
                               an explicit ``signal`` argument.
                               Defaults to ``"TERM"``.
        :param wait_poll_interval_seconds: Seconds between successive
                                           ``kill -0`` probes inside
                                           :meth:`UbuntuProcessManager.wait_for_process`.
    """
    default_signal: str = _DEFAULT_SIGNAL
    wait_poll_interval_seconds: float = 0.2


@dataclass
class UbuntuProcessInfo:
    """
        Per-process record returned by ``list_processes`` /
        ``get_process`` / ``find_processes_by_name``.
    """
    pid: int
    ppid: Optional[int] = None
    user: Optional[str] = None
    state: Optional[str] = None
    priority: Optional[int] = None
    nice: Optional[int] = None
    vsz: Optional[int] = None
    rss: Optional[int] = None
    cpu_percent: Optional[float] = None
    mem_percent: Optional[float] = None
    elapsed: Optional[str] = None
    command: Optional[str] = None
    argv: list = field(default_factory=list)


@dataclass
class UbuntuProcessSpec:
    """
        Spec for :meth:`UbuntuProcessManager.start_process`.

        :param executable: Absolute or PATH-resolved program path.
        :param args: Argument vector after ``executable``.
        :param working_dir: Working directory for the spawned process,
                            or ``None`` to inherit the runner's cwd.
        :param env: Environment overrides.  When ``None`` the spawned
                    process inherits the runner's environment verbatim.
        :param user: Optional username to spawn the process as.  When
                     non-``None`` and different from the current user the
                     manager elevates the underlying invocation.  Pilot
                     limitation: explicit per-user spawning is not
                     surfaced today; the field is recorded on the result
                     but not consumed by the spawn argv.
    """
    executable: str
    args: list = field(default_factory=list)
    working_dir: Optional[str] = None
    env: Optional[dict] = None
    user: Optional[str] = None


@dataclass
class UbuntuProcessActionResult(FeatureActionResult):
    """
        Outcome of a process-management write.

        :param pid: Target PID (``kill`` / ``set_priority`` / spawned
                    PID for ``start_process``).  ``None`` when no PID
                    is known (e.g., dry-run on ``start_process``).
        :param signal: Signal name dispatched (``kill_process`` /
                       ``kill_process_tree``).  ``None`` when not
                       applicable.
    """
    pid: Optional[int] = None
    signal: Optional[str] = None


@dataclass
class UbuntuProcessWaitResult:
    """
        Outcome of :meth:`UbuntuProcessManager.wait_for_process`.

        :param pid: Target PID.
        :param exit_code: Exit code of the process when known.  This is
                          left ``None`` on Linux because ``ps`` cannot
                          report the exit code of a process that the
                          caller does not own; only the parent does.
        :param timed_out: ``True`` when the wait exceeded the timeout.
    """
    pid: int
    exit_code: Optional[int] = None
    timed_out: bool = False


# ----------------------------------------------------------------------
# Exception
# ----------------------------------------------------------------------


class UbuntuProcessQueryError(Exception):
    """
        Raised when a process read fails for a reason other than the
        target PID being absent.  ``get_process`` returns ``None`` for
        an unknown PID; only unexpected failures of ``ps`` / ``pgrep``
        surface as :class:`UbuntuProcessQueryError`.
    """

    def __init__(
        self,
        *,
        command: list,
        exit_code: Optional[int],
        stderr: str,
        message: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
            :param message: Optional override for the exception message.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        if message is not None:
            err_msg = message
        else:
            err_msg_lines = ["Process query command failed."]
            err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
            err_msg_lines.append("    STDERR: {}".format(stderr))
            err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class UbuntuProcessManager(BaseFeatureManager):
    """
        Ubuntu process manager.  Wraps ``ps``, ``kill``, ``pkill``,
        ``pgrep``, ``renice``, and ``setsid`` through the injected
        :class:`ICommandRunner`.
    """

    result_class = UbuntuProcessActionResult
    status_class = UbuntuProcessActionStatus
    feature_name = "process-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_processes(
        self,
        *,
        owner: Optional[str] = None,
    ) -> list:
        """
            List running processes, optionally restricted to ``owner``.

            Runs ``ps -eo <format> --no-headers`` (or ``ps -u <owner>
            -o <format> --no-headers`` when ``owner`` is supplied).

            :param owner: Username to scope the listing to, or ``None``
                          for all users.

            :returns: A list of :class:`UbuntuProcessInfo`.
            :raises UbuntuProcessQueryError: When ``ps`` exits non-zero.
        """
        if owner is None:
            command = [_PS, "-eo", _PS_FORMAT, "--no-headers"]
        else:
            command = [_PS, "-u", owner, "-o", _PS_FORMAT, "--no-headers"]

        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuProcessQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        records = _parse_ps_output(stdout=run_result.stdout)
        return records

    def get_process(self, *, pid: int) -> Optional[UbuntuProcessInfo]:
        """
            Look up a single process by PID.

            Runs ``ps -p <pid> -o <format> --no-headers``.  An unknown
            PID surfaces as ``None`` rather than an exception, matching
            the contract.

            :param pid: Target process ID.

            :returns: The :class:`UbuntuProcessInfo` or ``None`` when
                      the PID is not running.
            :raises UbuntuProcessQueryError: When ``ps`` exits non-zero
                                             with stderr (an unknown
                                             PID exits 1 with empty
                                             stderr and is treated as
                                             ``None``).
        """
        command = [_PS, "-p", str(pid), "-o", _PS_FORMAT, "--no-headers"]
        run_result = self._runner.run(list(command), elevated=False)

        # ``ps -p <unknown>`` exits 1 with empty stdout and empty stderr;
        # only an unexpected failure (e.g., parser error) surfaces stderr.
        if run_result.exit_code != 0:
            if len(run_result.stderr.strip()) == 0:
                return None
            raise UbuntuProcessQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        records = _parse_ps_output(stdout=run_result.stdout)
        if len(records) == 0:
            return None

        first = records[0]
        return first

    def find_processes_by_name(self, *, name: str) -> list:
        """
            Find running processes matching ``name`` via ``pgrep -a``.

            ``pgrep -a`` prints one line per match in the form
            ``<pid> <argv...>``.  The parser expands those rows into
            :class:`UbuntuProcessInfo` records.  ``pgrep`` exits 1 with
            empty output when nothing matches; that maps to an empty
            list rather than an exception.

            :param name: Pattern matched against process names.  Passed
                         verbatim to ``pgrep`` — partial matches and
                         regex are accepted.

            :returns: A list of :class:`UbuntuProcessInfo`.  Empty when
                      no processes match.
            :raises UbuntuProcessQueryError: When ``pgrep`` exits with a
                                             code other than 0 / 1.
        """
        command = [_PGREP, "-a", name]
        run_result = self._runner.run(list(command), elevated=False)

        if run_result.exit_code == 1:
            # ``pgrep`` reports "no matches" as exit code 1 with empty
            # output.  That is the no-match case, not an error.
            return []

        if run_result.exit_code != 0:
            raise UbuntuProcessQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        records = _parse_pgrep_output(stdout=run_result.stdout)
        return records

    def wait_for_process(
        self,
        *,
        pid: int,
        timeout_seconds: Optional[float] = None,
    ) -> UbuntuProcessWaitResult:
        """
            Block until ``pid`` exits or ``timeout_seconds`` elapses.

            Polls ``kill -0 <pid>`` at
            :attr:`UbuntuProcessManagerConfig.wait_poll_interval_seconds`
            cadence.  ``kill -0`` is a signal-zero "test" that returns
            exit code 0 when the PID is alive and 1 when it is gone.

            :param pid: Target process ID.
            :param timeout_seconds: Maximum wait in wall-clock seconds.
                                    ``None`` waits forever.

            :returns: A :class:`UbuntuProcessWaitResult`.  ``timed_out``
                      is ``True`` when the timeout elapsed before the
                      PID disappeared.
        """
        start_monotonic = time.monotonic()
        poll_interval = self._typed_config().wait_poll_interval_seconds

        while True:
            probe_command = [_KILL, "-0", str(pid)]
            probe_result = self._runner.run(
                list(probe_command), elevated=False,
            )

            if probe_result.exit_code != 0:
                rtnval = UbuntuProcessWaitResult(
                    pid=pid, exit_code=None, timed_out=False,
                )
                return rtnval

            if timeout_seconds is not None:
                elapsed = time.monotonic() - start_monotonic
                if elapsed >= timeout_seconds:
                    rtnval = UbuntuProcessWaitResult(
                        pid=pid, exit_code=None, timed_out=True,
                    )
                    return rtnval

            time.sleep(poll_interval)

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def kill_process(
        self,
        *,
        pid: int,
        signal: Optional[str] = None,
    ) -> UbuntuProcessActionResult:
        """
            Send ``signal`` to ``pid``.  Skipped when the PID is no
            longer present.

            :param pid: Target process ID.
            :param signal: Signal name without the ``SIG`` prefix
                           (``"TERM"``, ``"KILL"``, ...).  When ``None``
                           the manager's
                           :attr:`UbuntuProcessManagerConfig.default_signal`
                           is used.

            :returns: The :class:`UbuntuProcessActionResult`.
        """
        effective_signal = self._resolve_signal(signal=signal)
        command = [_KILL, "-{}".format(effective_signal), str(pid)]

        if self._config.dry_run:
            dry_result = self._build_result(
                status=UbuntuProcessActionStatus.DRY_RUN,
                command=command,
                pid=pid,
                signal=effective_signal,
                detail=str(pid),
            )
            return dry_result

        # Idempotency guard: skip when the PID is already gone.
        existing = self.get_process(pid=pid)
        if existing is None:
            skip_result = self._build_result(
                status=UbuntuProcessActionStatus.SKIPPED,
                command=command,
                pid=pid,
                signal=effective_signal,
                detail="pid {} not present".format(pid),
            )
            return skip_result

        rtnval = self._dispatch(
            command=command,
            detail=str(pid),
            operation="kill_process",
            pid=pid,
            signal=effective_signal,
        )
        return rtnval

    def kill_process_tree(
        self,
        *,
        pid: int,
        signal: Optional[str] = None,
    ) -> UbuntuProcessActionResult:
        """
            Send ``signal`` to ``pid`` and all of its direct children.

            Implementation: ``pkill -<signal> -P <pid>`` first to deliver
            to the children, then ``kill -<signal> <pid>`` to deliver to
            the root.  Skipped when the root PID is no longer present.

            :param pid: Root PID of the subtree.
            :param signal: Signal name without the ``SIG`` prefix.

            :returns: The :class:`UbuntuProcessActionResult` reflecting
                      the outcome of the *root* kill.  Failures of the
                      child sweep are recorded on ``detail`` but do not
                      override the overall status when the root kill
                      succeeds.
        """
        effective_signal = self._resolve_signal(signal=signal)
        children_command = [
            _PKILL, "-{}".format(effective_signal), "-P", str(pid),
        ]
        root_command = [_KILL, "-{}".format(effective_signal), str(pid)]

        if self._config.dry_run:
            dry_result = self._build_result(
                status=UbuntuProcessActionStatus.DRY_RUN,
                command=root_command,
                pid=pid,
                signal=effective_signal,
                detail=str(pid),
            )
            return dry_result

        existing = self.get_process(pid=pid)
        if existing is None:
            skip_result = self._build_result(
                status=UbuntuProcessActionStatus.SKIPPED,
                command=root_command,
                pid=pid,
                signal=effective_signal,
                detail="pid {} not present".format(pid),
            )
            return skip_result

        # Children sweep first — pkill -P <pid> may exit 1 when there
        # are no children; that is not a failure for the tree kill.
        children_run = self._runner.run(
            list(children_command),
            elevated=self._config.elevated,
        )
        children_failed = (
            children_run.exit_code is not None
            and children_run.exit_code not in (0, 1)
        )

        # Root kill.
        rtnval = self._dispatch(
            command=root_command,
            detail=str(pid),
            operation="kill_process_tree",
            pid=pid,
            signal=effective_signal,
        )

        if children_failed is True and rtnval.status == UbuntuProcessActionStatus.SUCCESS:
            rtnval.detail = "{} (child sweep exit={})".format(
                rtnval.detail, children_run.exit_code,
            )
        return rtnval

    def start_process(
        self,
        *,
        spec: UbuntuProcessSpec,
    ) -> UbuntuProcessActionResult:
        """
            Spawn a new process described by ``spec``.

            Implementation: ``setsid sh -c '<command-line>'`` is
            dispatched through the runner so that the spawned process
            joins a new session and detaches from the controlling
            terminal.  Pilot limitation: the runner blocks until the
            shell exits, so ``start_process`` is effectively synchronous
            today.  See module docstring for the follow-up.

            ``spec.user`` is recorded on the result but does not
            currently switch the spawn user — the runner's
            ``elevated=config.elevated`` path is the only privilege lever
            in this pilot.

            :param spec: The :class:`UbuntuProcessSpec` describing the
                         executable, args, working directory, and env.

            :returns: The :class:`UbuntuProcessActionResult`.
        """
        argv = [spec.executable] + list(spec.args)
        command_line = _shell_escape_argv(argv=argv)
        command = [_SETSID, _SH, "-c", command_line]

        if self._config.dry_run:
            dry_result = self._build_result(
                status=UbuntuProcessActionStatus.DRY_RUN,
                command=command,
                pid=None,
                signal=None,
                detail=spec.executable,
            )
            return dry_result

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
            cwd=spec.working_dir,
            env=spec.env,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuProcessActionStatus.SUCCESS
        else:
            mapped_status = UbuntuProcessActionStatus.FAILED

        # Best-effort PID lookup: re-query pgrep for the executable name.
        pid_value: Optional[int] = None
        if mapped_status == UbuntuProcessActionStatus.SUCCESS:
            pid_value = _best_effort_pid_lookup(
                manager=self, executable=spec.executable,
            )

        rtnval = self._build_result(
            status=mapped_status,
            command=command,
            pid=pid_value,
            signal=None,
            detail=spec.executable,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
        )
        return rtnval

    def set_process_priority(
        self,
        *,
        pid: int,
        priority: int,
    ) -> UbuntuProcessActionResult:
        """
            Adjust the CPU nice value of ``pid``.

            Runs ``renice <priority> -p <pid>``.  Skipped when the
            current nice value already matches ``priority``.

            :param pid: Target process ID.
            :param priority: Desired nice value in the range -20..19.

            :returns: The :class:`UbuntuProcessActionResult`.
        """
        command = [_RENICE, str(priority), "-p", str(pid)]

        if self._config.dry_run:
            dry_result = self._build_result(
                status=UbuntuProcessActionStatus.DRY_RUN,
                command=command,
                pid=pid,
                signal=None,
                detail=str(priority),
            )
            return dry_result

        existing = self.get_process(pid=pid)
        if existing is not None and existing.nice is not None:
            if existing.nice == priority:
                skip_result = self._build_result(
                    status=UbuntuProcessActionStatus.SKIPPED,
                    command=command,
                    pid=pid,
                    signal=None,
                    detail="nice already {}".format(priority),
                )
                return skip_result

        rtnval = self._dispatch(
            command=command,
            detail=str(priority),
            operation="set_process_priority",
            pid=pid,
            signal=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_signal(self, *, signal: Optional[str]) -> str:
        """
            Validate and resolve a signal name.

            :param signal: Signal name without the ``SIG`` prefix, or
                           ``None`` to fall back to the configured
                           default signal.

            :returns: The validated signal name.
            :raises ValueError: When the signal is not a known POSIX
                                name from :data:`_KNOWN_SIGNALS`.
        """
        if signal is None:
            candidate = self._typed_config().default_signal
        else:
            candidate = signal

        normalised = candidate.upper()
        if normalised.startswith("SIG"):
            normalised = normalised[3:]

        if normalised not in _KNOWN_SIGNALS:
            err_msg_lines = ["Unsupported POSIX signal."]
            err_msg_lines.append("    REQUESTED: {}".format(candidate))
            err_msg_lines.append(
                "    KNOWN: {}".format(sorted(_KNOWN_SIGNALS)),
            )
            err_msg = os.linesep.join(err_msg_lines)
            raise ValueError(err_msg)

        return normalised

    def _build_result(
        self,
        *,
        status: UbuntuProcessActionStatus,
        command: list,
        pid: Optional[int],
        signal: Optional[str],
        detail: Optional[str] = None,
        exit_code: Optional[int] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuProcessActionResult:
        """
            Construct an :class:`UbuntuProcessActionResult` without
            dispatching.  Used for dry-run / skip / error short-circuits
            and for assembling ``start_process`` results that need a PID.
        """
        rtnval = UbuntuProcessActionResult(
            status=status,
            detail=detail,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            command=list(command),
            pid=pid,
            signal=signal,
        )
        return rtnval

    def _typed_config(self) -> UbuntuProcessManagerConfig:
        """
            Return the manager's config typed as
            :class:`UbuntuProcessManagerConfig`.
        """
        if isinstance(self._config, UbuntuProcessManagerConfig):
            rtnval = self._config
        else:
            rtnval = UbuntuProcessManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _parse_ps_output(*, stdout: str) -> list:
    """
        Parse the ``--no-headers`` output of
        ``ps -o pid,ppid,user,stat,pri,ni,vsz,rss,pcpu,pmem,etime,
        comm,args``.

        ``args`` is the only column that contains whitespace, so we
        split on the first 12 whitespace runs and keep the rest as
        ``args``.  Any line that does not parse cleanly is dropped to
        keep the parser tolerant against ``ps`` quirks (e.g., kernel
        threads with very short ``args``).
    """
    rtnval: list = []
    for raw in stdout.splitlines():
        line = raw.rstrip()
        if len(line.strip()) == 0:
            continue

        tokens = line.split(None, 12)
        if len(tokens) < 12:
            continue

        pid_raw = tokens[0]
        ppid_raw = tokens[1]
        user = tokens[2]
        state = tokens[3]
        pri_raw = tokens[4]
        ni_raw = tokens[5]
        vsz_raw = tokens[6]
        rss_raw = tokens[7]
        pcpu_raw = tokens[8]
        pmem_raw = tokens[9]
        etime = tokens[10]
        comm = tokens[11]

        if len(tokens) >= 13:
            args_text = tokens[12]
        else:
            args_text = comm

        try:
            pid_value = int(pid_raw)
        except ValueError:
            continue

        ppid_value = _safe_int(raw=ppid_raw)
        priority_value = _safe_int(raw=pri_raw)
        nice_value = _safe_int(raw=ni_raw)
        vsz_value = _safe_int(raw=vsz_raw)
        rss_value = _safe_int(raw=rss_raw)
        cpu_value = _safe_float(raw=pcpu_raw)
        mem_value = _safe_float(raw=pmem_raw)

        argv_list = args_text.split()

        record = UbuntuProcessInfo(
            pid=pid_value,
            ppid=ppid_value,
            user=user,
            state=state,
            priority=priority_value,
            nice=nice_value,
            vsz=vsz_value,
            rss=rss_value,
            cpu_percent=cpu_value,
            mem_percent=mem_value,
            elapsed=etime,
            command=comm,
            argv=argv_list,
        )
        rtnval.append(record)

    return rtnval


def _parse_pgrep_output(*, stdout: str) -> list:
    """
        Parse ``pgrep -a`` output.

        Each line has the shape ``<pid> <argv...>``.
    """
    rtnval: list = []
    for raw in stdout.splitlines():
        line = raw.rstrip()
        if len(line.strip()) == 0:
            continue

        tokens = line.split(None, 1)
        if len(tokens) < 1:
            continue

        try:
            pid_value = int(tokens[0])
        except ValueError:
            continue

        if len(tokens) >= 2:
            args_text = tokens[1]
            argv_list = args_text.split()
            if len(argv_list) > 0:
                command_value = argv_list[0]
            else:
                command_value = None
        else:
            args_text = None
            argv_list = []
            command_value = None

        record = UbuntuProcessInfo(
            pid=pid_value,
            command=command_value,
            argv=argv_list,
        )
        rtnval.append(record)

    return rtnval


def _safe_int(*, raw: str) -> Optional[int]:
    """
        Coerce a ``ps`` token to ``int``, returning ``None`` when the
        token cannot be parsed.
    """
    try:
        rtnval = int(raw)
    except (TypeError, ValueError):
        rtnval = None
    return rtnval


def _safe_float(*, raw: str) -> Optional[float]:
    """
        Coerce a ``ps`` token to ``float``, returning ``None`` when the
        token cannot be parsed.
    """
    try:
        rtnval = float(raw)
    except (TypeError, ValueError):
        rtnval = None
    return rtnval


def _shell_escape_argv(*, argv: list) -> str:
    """
        Build a single shell-quoted command line from an argv list.

        ``setsid sh -c '<line>'`` requires a single string argument.
        We escape each argv token with single quotes so embedded special
        characters do not get re-interpreted by the inner shell.
    """
    quoted_tokens: list = []
    for token in argv:
        text = str(token)
        # Escape any single quotes by closing the quote, inserting a
        # backslash-escaped quote, and re-opening.
        escaped = text.replace("'", "'\\''")
        quoted_tokens.append("'{}'".format(escaped))

    rtnval = " ".join(quoted_tokens)
    return rtnval


def _best_effort_pid_lookup(
    *,
    manager: UbuntuProcessManager,
    executable: str,
) -> Optional[int]:
    """
        Re-query ``pgrep`` for ``executable`` and return the most-recent
        PID, if any.

        Used by :meth:`UbuntuProcessManager.start_process` to fill in
        the ``pid`` field on the result for the pilot's blocking spawn
        path.  ``None`` is returned when no match is found or the lookup
        itself fails — failures here MUST NOT propagate to the caller
        because the spawn already succeeded.
    """
    try:
        records = manager.find_processes_by_name(name=executable)
    except UbuntuProcessQueryError:
        return None

    if len(records) == 0:
        return None

    # ``pgrep`` returns matches in PID order; the most-recent process is
    # at the tail.
    last = records[-1]
    return last.pid
