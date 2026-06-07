"""
    Ubuntu audit-management feature.

    Implements the cross-OS audit-management contract for Ubuntu using
    the Linux Audit subsystem ``auditd``.  Runtime rule changes go
    through ``auditctl``; persistent rules are written into a drop-in
    file under ``/etc/audit/rules.d/`` and reloaded with
    ``augenrules --load``.  The systemd unit ``auditd.service`` is
    toggled via ``systemctl``.  Event search delegates to ``ausearch``.

    File I/O policy:
        Every file read / write / probe is performed through the
        injected :class:`ICommandRunner` (``cat``, ``tee``, ``test -e``).
        This module does NOT consume an :class:`IFileOperator`.

    Elevation policy:
        Every write verb (``add_rule``, ``remove_rule``,
        ``enable_auditing``, ``disable_auditing``) honours
        ``config.elevated``.  Reads (``list_rules``,
        ``get_audit_status``, ``search_events``) never elevate.

    Rule-identifier mapping:
        On Ubuntu the abstract ``rule_id`` maps to the audit rule's
        ``-k <key>`` token.  When a spec omits ``key`` the manager
        synthesises a deterministic identifier from the rendered
        argv tokens.

    Surface choices:
        * ``add_rule(spec)`` renders the spec into an ``auditctl``
          argv via :func:`_render_auditctl_args`.  When persistence is
          requested the rule line is appended to
          ``<rules_d>/<default_basename>`` via ``tee`` and
          ``augenrules --load`` is invoked to apply the on-disk store.
          The runtime ``auditctl`` invocation is best-effort and is
          attempted whether or not persistence succeeded.
        * ``remove_rule(rule_id)`` issues ``auditctl -d <rule>`` for
          every matching live rule and removes the matching line from
          the drop-in.
        * ``list_rules`` parses ``auditctl -l`` output (one rule per
          line) and stitches in any rules read from the drop-in that
          have not yet been applied.
        * ``enable_auditing`` issues ``auditctl -e 1`` and
          ``systemctl enable --now auditd``.
        * ``disable_auditing`` issues ``auditctl -e 0`` and
          ``systemctl disable --now auditd``.
        * ``get_audit_status`` parses the multi-line ``auditctl -s``
          status text into a :class:`UbuntuAuditStatus`.
        * ``search_events`` builds an ``ausearch`` argv from the
          structured :class:`UbuntuAuditEventQuery` (``-k``, ``-m``,
          ``-ts``, ``-te``, ``-ui``) and parses the textual record
          stream into :class:`UbuntuAuditEvent` rows.
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


_AUDITCTL = "auditctl"
_AUGENRULES = "augenrules"
_AUSEARCH = "ausearch"
_SYSTEMCTL = "systemctl"

_DEFAULT_RULES_D = "/etc/audit/rules.d"
_DEFAULT_RULES_BASENAME = "99-ccai.rules"
_DEFAULT_SERVICE_NAME = "auditd"


UbuntuAuditActionStatus = ActionStatus


@dataclass
class UbuntuAuditManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuAuditManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param rules_d: Directory holding audit rule drop-ins.  Defaults
                        to ``/etc/audit/rules.d``.
        :param default_basename: File basename for the manager-owned
                                 drop-in.  Defaults to
                                 ``99-ccai.rules``.
        :param service_name: systemd unit name for the audit daemon.
                             Defaults to ``auditd``.
    """
    rules_d: str = _DEFAULT_RULES_D
    default_basename: str = _DEFAULT_RULES_BASENAME
    service_name: str = _DEFAULT_SERVICE_NAME


@dataclass
class UbuntuAuditActionResult(FeatureActionResult):
    """
        Result of an audit-management write operation.

        :param rule_id: The rule identifier targeted, when applicable.
        :param rule_key: The rule's ``-k`` key, when applicable.  This
                         is identical to ``rule_id`` for rules whose
                         identifier is the auditctl key, and may
                         diverge for rules synthesised from raw argv.
        :param message: Optional human-readable detail for ``SKIPPED``
                        / ``FAILED`` outcomes.
    """
    rule_id: Optional[str] = None
    rule_key: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuAuditRuleSpec:
    """
        Structured input for :meth:`UbuntuAuditManager.add_rule` /
        :meth:`UbuntuAuditManager.remove_rule`.

        Either ``raw_rule`` is supplied (verbatim auditctl argv minus
        the ``auditctl`` token itself), or the structured fields are
        populated and the manager renders the argv via
        :func:`_render_auditctl_args`.

        :param key: ``-k <key>`` identifier.  Acts as the abstract
                    ``rule_id`` and the deterministic match token for
                    idempotency / removal.
        :param watch_path: When set, emits ``-w <watch_path>``.
        :param permissions: When set, emits ``-p <permissions>``
                            (e.g., ``"wa"``, ``"rwxa"``).
        :param syscall_filter: When set, emits ``-a <syscall_filter>``
                               (e.g., ``"always,exit -F arch=b64 -S
                               execve"``).  The full action and filter
                               body is supplied verbatim.
        :param architecture: When set, emits ``-F arch=<architecture>``.
        :param raw_rule: Verbatim argv string (everything after
                         ``auditctl``) used in lieu of structured
                         rendering.  Mutually exclusive with the
                         structured fields; when both are supplied
                         the raw form wins.
        :param persistent: When True, the rule is also written into the
                           manager's drop-in under
                           :attr:`UbuntuAuditManagerConfig.rules_d`.
                           When False, the rule is applied at runtime
                           only.
    """
    key: str
    watch_path: Optional[str] = None
    permissions: Optional[str] = None
    syscall_filter: Optional[str] = None
    architecture: Optional[str] = None
    raw_rule: Optional[str] = None
    persistent: bool = True


@dataclass
class UbuntuAuditRuleInfo:
    """
        One audit rule as observed by :meth:`UbuntuAuditManager.list_rules`.

        :param rule_id: The rule identifier (the ``-k`` key when
                        present, or a synthetic identifier built from
                        the rendered tokens).
        :param key: The ``-k`` key if any was parsed; ``None``
                    otherwise.
        :param expression: The full ``auditctl`` argv body that
                           describes this rule, joined with single
                           spaces (the same form ``auditctl -l`` emits).
        :param watch_path: The path watched by this rule, when
                           ``-w <path>`` is present in the rule body.
        :param permissions: The permissions mask when ``-p <mask>`` is
                            present.
        :param subsystem: ``"path"`` for ``-w`` rules,
                          ``"syscall"`` for ``-a`` rules,
                          ``"raw"`` otherwise.
        :param source: On-disk origin path of the rule when it came
                       from a drop-in file; ``None`` when the rule was
                       observed via ``auditctl -l`` only.
    """
    rule_id: str
    key: Optional[str]
    expression: str
    watch_path: Optional[str]
    permissions: Optional[str]
    subsystem: str
    source: Optional[str] = None


@dataclass
class UbuntuAuditStatus:
    """
        Parsed status block from ``auditctl -s``.

        :param enabled: ``True`` when the kernel audit subsystem is
                        enabled.  Mirrors the ``enabled`` line.
        :param running: ``True`` when the ``auditd`` daemon is
                        running, inferred from a non-zero ``pid``.
        :param pid: Process ID of the running ``auditd`` daemon, or
                    ``None`` when the line is absent.
        :param failure_mode: Failure-mode integer ("0" / "1" / "2") as
                             a string, or ``None`` when absent.
        :param rate_limit: Rate-limit value (events per second), or
                           ``None`` when absent.
        :param backlog_limit: Backlog-limit value, or ``None`` when
                              absent.
        :param backlog: Current backlog count, or ``None`` when
                        absent.
        :param lost_events: Cumulative lost-event count, or ``None``
                            when absent.
        :param locked: ``True`` when the kernel audit subsystem is in
                       immutable mode (``enabled 2``).
        :param raw: The raw stdout text from ``auditctl -s`` for
                    debuggability.
    """
    enabled: bool
    running: bool
    pid: Optional[int]
    failure_mode: Optional[str]
    rate_limit: Optional[int]
    backlog_limit: Optional[int]
    backlog: Optional[int]
    lost_events: Optional[int]
    locked: bool
    raw: str


@dataclass
class UbuntuAuditEventQuery:
    """
        Structured query for :meth:`UbuntuAuditManager.search_events`.

        :param key: When set, emits ``-k <key>``.
        :param message_type: When set, emits ``-m <message_type>``
                             (e.g., ``"SYSCALL"``, ``"USER_LOGIN"``).
        :param since: When set, emits ``-ts <since>`` (ausearch native
                      time format, e.g. ``"recent"`` or
                      ``"01/01/2024 00:00:00"``).
        :param until: When set, emits ``-te <until>``.
        :param user: When set, emits ``-ui <user>``.
        :param raw_filter: When set, replaces the structured argv with
                           a verbatim string passed to ``ausearch``.
        :param limit: When set, the parser stops after collecting
                      ``limit`` events.
    """
    key: Optional[str] = None
    message_type: Optional[str] = None
    since: Optional[str] = None
    until: Optional[str] = None
    user: Optional[str] = None
    raw_filter: Optional[str] = None
    limit: Optional[int] = None


@dataclass
class UbuntuAuditEvent:
    """
        One parsed event record from ``ausearch``.

        :param timestamp: The ``msg=audit(<seconds>.<ms>:<serial>)``
                          token rendered as
                          ``"<seconds>.<ms>:<serial>"`` for stability.
                          Empty string when the timestamp could not be
                          parsed.
        :param event_id: The serial portion of the audit timestamp
                         (``<serial>``) as an integer; ``None`` when
                         the timestamp could not be parsed.
        :param category: The first ``type=<TYPE>`` token of the
                         record (e.g., ``"SYSCALL"``, ``"PATH"``,
                         ``"USER_LOGIN"``).  ``None`` when no
                         ``type=`` token is present.
        :param severity: Reserved for future severity-mapping rules;
                         ``None`` for the pilot.
        :param message: A short human-readable summary built from the
                        first record line.
        :param raw: The full record text (one or more lines) preserved
                    verbatim.
    """
    timestamp: str
    event_id: Optional[int]
    category: Optional[str]
    severity: Optional[str]
    message: str
    raw: str


class UbuntuAuditQueryError(Exception):
    """
        Raised when an audit read fails for an unrecoverable reason.
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

        err_msg_lines = ["Audit query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuAuditImmutableError(Exception):
    """
        Raised when a write is attempted while the kernel audit
        subsystem is in immutable mode (``auditctl -s`` reports
        ``enabled 2``).
    """

    def __init__(self, *, detail: str) -> None:
        """
            :param detail: Human-readable description of the locked
                           subsystem and the rejected operation.
        """
        self.detail = detail
        err_msg_lines = ["Audit subsystem is in immutable mode."]
        err_msg_lines.append("    DETAIL: {}".format(detail))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuAuditManager(BaseFeatureManager):
    """
        Ubuntu audit-management manager.  Every verb is dispatched
        through the injected :class:`ICommandRunner`; rule-file I/O
        flows through ``cat`` / ``tee`` / ``test -e`` rather than a
        separate file operator.
    """

    result_class = UbuntuAuditActionResult
    status_class = UbuntuAuditActionStatus
    feature_name = "audit-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_rules(self) -> list:
        """
            Enumerate currently-applied audit rules.

            Runs ``auditctl -l`` and parses one rule per line.  When
            the manager-owned drop-in exists, any line in that file
            that has not been applied at runtime is also included
            with ``source`` populated so callers can distinguish
            persistent-only rules from runtime-applied rules.

            :returns: One :class:`UbuntuAuditRuleInfo` per parsed
                      rule.
            :raises UbuntuAuditQueryError: When ``auditctl -l`` exits
                                            non-zero with a non-empty
                                            stderr.
        """
        list_command = [_AUDITCTL, "-l"]
        list_result = self._runner.run(list(list_command), elevated=False)
        if list_result.exit_code != 0 and len(list_result.stderr.strip()) > 0:
            raise UbuntuAuditQueryError(
                command=list_command,
                exit_code=list_result.exit_code,
                stderr=list_result.stderr,
            )

        runtime_rules = _parse_auditctl_list(text=list_result.stdout)

        persistent_path = self._dropin_path()
        persistent_text = self._read_file(path=persistent_path)
        persistent_rules: list = []
        if persistent_text is not None:
            persistent_rules = _parse_rules_file(
                text=persistent_text, source=persistent_path,
            )

        rtnval: list = []
        seen_expressions: set = set()
        for rule in runtime_rules:
            seen_expressions.add(rule.expression)
            rtnval.append(rule)
        for rule in persistent_rules:
            if rule.expression in seen_expressions:
                continue
            rtnval.append(rule)
        return rtnval

    def get_audit_status(self) -> UbuntuAuditStatus:
        """
            Run ``auditctl -s`` and parse the multi-line status
            payload.

            :returns: The :class:`UbuntuAuditStatus`.
            :raises UbuntuAuditQueryError: When ``auditctl -s`` exits
                                            non-zero with a non-empty
                                            stderr.
        """
        command = [_AUDITCTL, "-s"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise UbuntuAuditQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_auditctl_status(text=run_result.stdout)
        return rtnval

    def search_events(
        self,
        query: UbuntuAuditEventQuery,
    ) -> list:
        """
            Search audit records via ``ausearch``.

            :param query: The :class:`UbuntuAuditEventQuery` describing
                          the filter.

            :returns: One :class:`UbuntuAuditEvent` per parsed record,
                      capped at ``query.limit`` when the limit is set.
            :raises UbuntuAuditQueryError: When ``ausearch`` exits
                                            non-zero with a non-empty
                                            stderr (exit code ``1``
                                            with empty stderr is
                                            ``ausearch``'s no-match
                                            signal and is treated as
                                            a successful empty
                                            result).
        """
        command = _build_ausearch_argv(query=query)
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            stderr_stripped = run_result.stderr.strip()
            if len(stderr_stripped) > 0:
                raise UbuntuAuditQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )

        events = _parse_ausearch_output(
            text=run_result.stdout, limit=query.limit,
        )
        return events

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_rule(
        self,
        spec: UbuntuAuditRuleSpec,
    ) -> UbuntuAuditActionResult:
        """
            Add an audit rule.

            Renders ``spec`` into an ``auditctl`` argv via
            :func:`_render_auditctl_args` and dispatches the runtime
            invocation.  When ``spec.persistent`` is True the rule
            line is also appended to the manager-owned drop-in and
            ``augenrules --load`` is invoked to apply the persistent
            store.

            Idempotency: when a rule with the same key already exists
            in the runtime rule set, the verb returns ``SKIPPED``
            without invoking either the runtime add or the persistence
            write.

            :param spec: The :class:`UbuntuAuditRuleSpec` to apply.

            :returns: The :class:`UbuntuAuditActionResult`.
        """
        rule_args = _render_auditctl_args(spec=spec)
        rule_line = " ".join(rule_args)
        runtime_command = [_AUDITCTL] + list(rule_args)

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuAuditActionStatus.DRY_RUN,
                rule_id=spec.key,
                rule_key=spec.key,
                command=runtime_command,
            )
            return dry

        existing_rules = self._safe_runtime_rules()
        if _rule_already_present(rules=existing_rules, key=spec.key,
                                 expression=rule_line):
            skipped = self._build_result(
                status=UbuntuAuditActionStatus.SKIPPED,
                rule_id=spec.key,
                rule_key=spec.key,
                command=runtime_command,
                message="rule already present in runtime rule set",
            )
            return skipped

        run_result = self._runner.run(
            list(runtime_command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuAuditActionStatus.SUCCESS
        else:
            mapped_status = UbuntuAuditActionStatus.FAILED

        # Persistence: append the rule line to the drop-in when
        # spec.persistent is True.  We do this regardless of whether
        # the runtime add succeeded so that next-boot policy is
        # consistent with the caller's intent.
        if spec.persistent:
            self._append_to_dropin(rule_line=rule_line)
            reload_command = [_AUGENRULES, "--load"]
            self._runner.run(
                list(reload_command),
                elevated=self._config.elevated,
            )

        rtnval = UbuntuAuditActionResult(
            status=mapped_status,
            detail=spec.key,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(runtime_command),
            rule_id=spec.key,
            rule_key=spec.key,
        )
        return rtnval

    def remove_rule(
        self,
        rule_id: str,
    ) -> UbuntuAuditActionResult:
        """
            Remove an audit rule by ``rule_id`` (the ``-k`` key).

            For each runtime rule whose key matches ``rule_id`` the
            manager issues ``auditctl -d`` against the original argv
            with the leading action token swapped from ``-w`` /
            ``-a`` to ``-W`` / ``-d`` as appropriate.  When no
            matching runtime rule is present the verb returns
            ``SKIPPED``.

            :param rule_id: The ``-k`` key of the rule to remove.

            :returns: The :class:`UbuntuAuditActionResult`.
        """
        delete_command = [_AUDITCTL, "-d", "-k", rule_id]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuAuditActionStatus.DRY_RUN,
                rule_id=rule_id,
                rule_key=rule_id,
                command=delete_command,
            )
            return dry

        existing_rules = self._safe_runtime_rules()
        match_found = False
        for rule in existing_rules:
            if rule.key == rule_id:
                match_found = True
                break

        if not match_found:
            # Also probe the on-disk drop-in.  When the rule lives
            # only in the persistent store we still want to remove
            # it from the drop-in to honour the caller's intent.
            persistent_text = self._read_file(path=self._dropin_path())
            if persistent_text is None or rule_id not in persistent_text:
                skipped = self._build_result(
                    status=UbuntuAuditActionStatus.SKIPPED,
                    rule_id=rule_id,
                    rule_key=rule_id,
                    command=delete_command,
                    message="rule not present in runtime or persistent store",
                )
                return skipped

        run_result = self._runner.run(
            list(delete_command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuAuditActionStatus.SUCCESS
        else:
            mapped_status = UbuntuAuditActionStatus.FAILED

        # Persistence: rewrite the drop-in with any matching line
        # removed, then ``augenrules --load`` to refresh.
        self._remove_from_dropin(rule_id=rule_id)
        reload_command = [_AUGENRULES, "--load"]
        self._runner.run(
            list(reload_command),
            elevated=self._config.elevated,
        )

        rtnval = UbuntuAuditActionResult(
            status=mapped_status,
            detail=rule_id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(delete_command),
            rule_id=rule_id,
            rule_key=rule_id,
        )
        return rtnval

    def enable_auditing(self) -> UbuntuAuditActionResult:
        """
            Enable the audit subsystem.

            Issues ``auditctl -e 1`` to flip the kernel-side enable
            bit and ``systemctl enable --now <service_name>`` to
            ensure the daemon is started and persists across reboot.

            Idempotency: when ``auditctl -s`` reports the kernel as
            already enabled and the daemon already has a non-zero
            PID, the verb returns ``SKIPPED``.

            :returns: The :class:`UbuntuAuditActionResult`.
        """
        primary_command = [_AUDITCTL, "-e", "1"]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuAuditActionStatus.DRY_RUN,
                rule_id=None,
                rule_key=None,
                command=primary_command,
            )
            return dry

        status = self._safe_status()
        if status is not None and status.enabled and status.running:
            skipped = self._build_result(
                status=UbuntuAuditActionStatus.SKIPPED,
                rule_id=None,
                rule_key=None,
                command=primary_command,
                message="audit subsystem already enabled and running",
            )
            return skipped

        if status is not None and status.locked:
            failed = self._build_result(
                status=UbuntuAuditActionStatus.FAILED,
                rule_id=None,
                rule_key=None,
                command=primary_command,
                message="audit subsystem is in immutable mode",
                stderr="audit subsystem is in immutable mode",
            )
            return failed

        run_result = self._runner.run(
            list(primary_command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuAuditActionStatus.SUCCESS
        else:
            mapped_status = UbuntuAuditActionStatus.FAILED

        # systemctl enable --now <service> is best-effort but its
        # outcome is folded into the final mapped_status so that a
        # systemctl failure is observable by callers.
        service_command = [
            _SYSTEMCTL, "enable", "--now", self._config.service_name,
        ]
        service_result = self._runner.run(
            list(service_command),
            elevated=self._config.elevated,
        )
        if service_result.exit_code != 0:
            mapped_status = UbuntuAuditActionStatus.FAILED

        rtnval = UbuntuAuditActionResult(
            status=mapped_status,
            detail="enable",
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(primary_command),
            rule_id=None,
            rule_key=None,
        )
        return rtnval

    def disable_auditing(self) -> UbuntuAuditActionResult:
        """
            Disable the audit subsystem.

            Issues ``auditctl -e 0`` and
            ``systemctl disable --now <service_name>``.

            Idempotency: when ``auditctl -s`` reports the kernel as
            already disabled (``enabled 0``) and the daemon as not
            running, the verb returns ``SKIPPED``.

            :returns: The :class:`UbuntuAuditActionResult`.
        """
        primary_command = [_AUDITCTL, "-e", "0"]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuAuditActionStatus.DRY_RUN,
                rule_id=None,
                rule_key=None,
                command=primary_command,
            )
            return dry

        status = self._safe_status()
        if status is not None and (not status.enabled) and (not status.running):
            skipped = self._build_result(
                status=UbuntuAuditActionStatus.SKIPPED,
                rule_id=None,
                rule_key=None,
                command=primary_command,
                message="audit subsystem already disabled and stopped",
            )
            return skipped

        if status is not None and status.locked:
            failed = self._build_result(
                status=UbuntuAuditActionStatus.FAILED,
                rule_id=None,
                rule_key=None,
                command=primary_command,
                message="audit subsystem is in immutable mode",
                stderr="audit subsystem is in immutable mode",
            )
            return failed

        run_result = self._runner.run(
            list(primary_command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuAuditActionStatus.SUCCESS
        else:
            mapped_status = UbuntuAuditActionStatus.FAILED

        service_command = [
            _SYSTEMCTL, "disable", "--now", self._config.service_name,
        ]
        service_result = self._runner.run(
            list(service_command),
            elevated=self._config.elevated,
        )
        if service_result.exit_code != 0:
            mapped_status = UbuntuAuditActionStatus.FAILED

        rtnval = UbuntuAuditActionResult(
            status=mapped_status,
            detail="disable",
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(primary_command),
            rule_id=None,
            rule_key=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dropin_path(self) -> str:
        """
            Build the absolute path to the manager-owned drop-in.

            :returns: ``<rules_d>/<default_basename>``.
        """
        rtnval = "{}/{}".format(
            self._config.rules_d, self._config.default_basename,
        )
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat``.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when the
                      file is missing or unreadable.
        """
        command = ["cat", path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _safe_runtime_rules(self) -> list:
        """
            Return the runtime rule list, swallowing
            :class:`UbuntuAuditQueryError` so write paths do not
            propagate read failures.

            :returns: A list of :class:`UbuntuAuditRuleInfo`; empty
                      when the read failed.
        """
        try:
            rtnval = self.list_rules()
        except UbuntuAuditQueryError:
            rtnval = []
        return rtnval

    def _safe_status(self) -> Optional[UbuntuAuditStatus]:
        """
            Return the parsed status, swallowing
            :class:`UbuntuAuditQueryError` so write paths can fall
            through to attempting the operation.

            :returns: The parsed :class:`UbuntuAuditStatus` or
                      ``None`` when the read failed.
        """
        try:
            rtnval: Optional[UbuntuAuditStatus] = self.get_audit_status()
        except UbuntuAuditQueryError:
            rtnval = None
        return rtnval

    def _append_to_dropin(self, *, rule_line: str) -> None:
        """
            Append ``rule_line`` to the manager-owned drop-in.

            When the line is already present in the existing file,
            this is a no-op.  Writes elevate per
            :attr:`UbuntuAuditManagerConfig.elevated`.

            :param rule_line: The rule body to append (everything
                              after the ``auditctl`` token).
        """
        path = self._dropin_path()
        existing = self._read_file(path=path)
        if existing is None:
            existing = ""

        for raw_line in existing.splitlines():
            if raw_line.strip() == rule_line.strip():
                return

        if len(existing) > 0 and not existing.endswith("\n"):
            existing = existing + "\n"
        new_body = existing + rule_line + "\n"

        write_command = ["tee", path]
        self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_body,
        )
        return

    def _remove_from_dropin(self, *, rule_id: str) -> None:
        """
            Rewrite the manager-owned drop-in with every line whose
            rendered tokens contain ``-k <rule_id>`` removed.

            :param rule_id: The ``-k`` key to strip.
        """
        path = self._dropin_path()
        existing = self._read_file(path=path)
        if existing is None:
            return

        kept_lines: list = []
        for raw_line in existing.splitlines():
            stripped = raw_line.strip()
            if len(stripped) == 0:
                kept_lines.append(raw_line)
                continue
            tokens = stripped.split()
            if _tokens_have_key(tokens=tokens, key=rule_id):
                continue
            kept_lines.append(raw_line)

        new_body = "\n".join(kept_lines)
        if len(new_body) > 0 and not new_body.endswith("\n"):
            new_body = new_body + "\n"

        write_command = ["tee", path]
        self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_body,
        )
        return

    def _build_result(
        self,
        *,
        status: UbuntuAuditActionStatus,
        rule_id: Optional[str],
        rule_key: Optional[str],
        command: list,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuAuditActionResult:
        """
            Construct an :class:`UbuntuAuditActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param rule_id: The rule identifier to echo onto the
                            result.
            :param rule_key: The rule key to echo onto the result.
            :param command: The would-have-been argv (copied for
                            capture).
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`UbuntuAuditActionResult`.
        """
        rtnval = UbuntuAuditActionResult(
            status=status,
            detail=rule_id,
            command=list(command),
            rule_id=rule_id,
            rule_key=rule_key,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level renderers and parsers
# ----------------------------------------------------------------------


def _render_auditctl_args(*, spec: UbuntuAuditRuleSpec) -> list:
    """
        Render a :class:`UbuntuAuditRuleSpec` into the ``auditctl``
        argv body (everything after the ``auditctl`` token).

        Precedence:
            1. When ``spec.raw_rule`` is set the function returns its
               whitespace-split tokens unchanged (with a trailing
               ``-k <key>`` only when the raw form is missing one).
            2. Otherwise the structured fields are emitted in the
               canonical order ``-w / -p / -a / -F arch=... / -k``.

        :param spec: The :class:`UbuntuAuditRuleSpec` to render.

        :returns: The argv as a list of strings.
    """
    if spec.raw_rule is not None and len(spec.raw_rule) > 0:
        tokens = spec.raw_rule.split()
        # Ensure the rule carries the spec's key; auditctl uses
        # ``-k`` to match the rule for deletion.
        if not _tokens_have_key(tokens=tokens, key=spec.key):
            tokens.append("-k")
            tokens.append(spec.key)
        rtnval = tokens
        return rtnval

    rendered: list = []
    if spec.watch_path is not None and len(spec.watch_path) > 0:
        rendered.append("-w")
        rendered.append(spec.watch_path)
    if spec.permissions is not None and len(spec.permissions) > 0:
        rendered.append("-p")
        rendered.append(spec.permissions)
    if spec.syscall_filter is not None and len(spec.syscall_filter) > 0:
        rendered.append("-a")
        # syscall_filter may be multi-token (e.g., "always,exit -F
        # arch=b64 -S execve"); split on whitespace so the runner
        # receives separate argv entries.
        for token in spec.syscall_filter.split():
            rendered.append(token)
    if spec.architecture is not None and len(spec.architecture) > 0:
        rendered.append("-F")
        rendered.append("arch={}".format(spec.architecture))
    rendered.append("-k")
    rendered.append(spec.key)

    rtnval = rendered
    return rtnval


def _tokens_have_key(*, tokens: list, key: str) -> bool:
    """
        Probe a token list for ``-k <key>``.

        :param tokens: The argv tokens.
        :param key: The key to find.

        :returns: ``True`` when the tokens contain ``-k`` immediately
                  followed by ``key``; ``False`` otherwise.
    """
    rtnval = False
    index = 0
    while index < len(tokens) - 1:
        if tokens[index] == "-k" and tokens[index + 1] == key:
            rtnval = True
            return rtnval
        index = index + 1
    return rtnval


def _extract_key(*, tokens: list) -> Optional[str]:
    """
        Extract the value following a ``-k`` token.

        :param tokens: The argv tokens.

        :returns: The key string when present; ``None`` otherwise.
    """
    rtnval: Optional[str] = None
    index = 0
    while index < len(tokens) - 1:
        if tokens[index] == "-k":
            rtnval = tokens[index + 1]
            return rtnval
        index = index + 1
    return rtnval


def _extract_named(*, tokens: list, flag: str) -> Optional[str]:
    """
        Extract the value following a flag token.

        :param tokens: The argv tokens.
        :param flag: The flag name to search for (e.g., ``"-w"``).

        :returns: The value when present; ``None`` otherwise.
    """
    rtnval: Optional[str] = None
    index = 0
    while index < len(tokens) - 1:
        if tokens[index] == flag:
            rtnval = tokens[index + 1]
            return rtnval
        index = index + 1
    return rtnval


def _classify_subsystem(*, tokens: list) -> str:
    """
        Classify a rule's subsystem from its argv tokens.

        :param tokens: The argv tokens.

        :returns: ``"path"`` when ``-w`` is present, ``"syscall"``
                  when ``-a`` is present, ``"raw"`` otherwise.
    """
    if "-w" in tokens:
        rtnval = "path"
        return rtnval
    if "-a" in tokens:
        rtnval = "syscall"
        return rtnval
    rtnval = "raw"
    return rtnval


def _parse_auditctl_list(*, text: str) -> list:
    """
        Parse the stdout of ``auditctl -l``.

        Each line of output is one rule body in the same form
        ``auditctl`` itself accepts (no leading ``auditctl`` token).
        Comments / blank lines are skipped.  When stdout is the
        single-line message ``No rules`` the parser returns an empty
        list.

        :param text: Raw stdout text.

        :returns: One :class:`UbuntuAuditRuleInfo` per parsed rule.
    """
    rtnval: list = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if len(line) == 0:
            continue
        if line.startswith("#"):
            continue
        if line == "No rules":
            continue
        tokens = line.split()
        key = _extract_key(tokens=tokens)
        watch_path = _extract_named(tokens=tokens, flag="-w")
        permissions = _extract_named(tokens=tokens, flag="-p")
        subsystem = _classify_subsystem(tokens=tokens)

        if key is not None:
            rule_id = key
        else:
            rule_id = line

        info = UbuntuAuditRuleInfo(
            rule_id=rule_id,
            key=key,
            expression=line,
            watch_path=watch_path,
            permissions=permissions,
            subsystem=subsystem,
            source=None,
        )
        rtnval.append(info)
    return rtnval


def _parse_rules_file(*, text: str, source: str) -> list:
    """
        Parse the contents of a rules.d drop-in file.

        Lines beginning with ``#`` and blank lines are skipped.
        Other lines are treated as rule bodies in ``auditctl`` form.

        :param text: The file contents.
        :param source: The on-disk path of the file (echoed onto
                       each :class:`UbuntuAuditRuleInfo`).

        :returns: One :class:`UbuntuAuditRuleInfo` per parsed line.
    """
    rtnval: list = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if len(line) == 0:
            continue
        if line.startswith("#"):
            continue
        tokens = line.split()
        key = _extract_key(tokens=tokens)
        watch_path = _extract_named(tokens=tokens, flag="-w")
        permissions = _extract_named(tokens=tokens, flag="-p")
        subsystem = _classify_subsystem(tokens=tokens)

        if key is not None:
            rule_id = key
        else:
            rule_id = line

        info = UbuntuAuditRuleInfo(
            rule_id=rule_id,
            key=key,
            expression=line,
            watch_path=watch_path,
            permissions=permissions,
            subsystem=subsystem,
            source=source,
        )
        rtnval.append(info)
    return rtnval


def _rule_already_present(
    *,
    rules: list,
    key: str,
    expression: str,
) -> bool:
    """
        Idempotency probe.

        Returns ``True`` when any rule in ``rules`` already matches
        either by key or by exact expression text.

        :param rules: The list of :class:`UbuntuAuditRuleInfo` to
                      probe.
        :param key: The key to match.
        :param expression: The full argv body (joined with single
                           spaces).

        :returns: ``True`` when a match is present; ``False``
                  otherwise.
    """
    rtnval = False
    for rule in rules:
        if rule.key is not None and rule.key == key:
            rtnval = True
            return rtnval
        if rule.expression == expression:
            rtnval = True
            return rtnval
    return rtnval


def _parse_auditctl_status(*, text: str) -> UbuntuAuditStatus:
    """
        Parse the multi-line ``auditctl -s`` output.

        ``auditctl -s`` emits one ``<key> <value>`` pair per line
        (whitespace-separated).  Recognised keys: ``enabled``,
        ``failure``, ``pid``, ``rate_limit``, ``backlog_limit``,
        ``lost``, ``backlog``.  Unknown keys are tolerated and ignored.

        :param text: Raw stdout text.

        :returns: The :class:`UbuntuAuditStatus`.
    """
    enabled_value: Optional[int] = None
    pid_value: Optional[int] = None
    failure_value: Optional[str] = None
    rate_limit_value: Optional[int] = None
    backlog_limit_value: Optional[int] = None
    backlog_value: Optional[int] = None
    lost_value: Optional[int] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if len(line) == 0:
            continue
        tokens = line.split(None, 1)
        if len(tokens) < 2:
            continue
        key_token = tokens[0].strip().lower()
        value_token = tokens[1].strip()

        if key_token == "enabled":
            try:
                enabled_value = int(value_token)
            except ValueError:
                enabled_value = None
        elif key_token == "pid":
            try:
                pid_value = int(value_token)
            except ValueError:
                pid_value = None
        elif key_token == "failure":
            failure_value = value_token
        elif key_token == "rate_limit":
            try:
                rate_limit_value = int(value_token)
            except ValueError:
                rate_limit_value = None
        elif key_token == "backlog_limit":
            try:
                backlog_limit_value = int(value_token)
            except ValueError:
                backlog_limit_value = None
        elif key_token == "backlog":
            try:
                backlog_value = int(value_token)
            except ValueError:
                backlog_value = None
        elif key_token == "lost":
            try:
                lost_value = int(value_token)
            except ValueError:
                lost_value = None

    if enabled_value is None:
        enabled_bool = False
    elif enabled_value == 0:
        enabled_bool = False
    else:
        enabled_bool = True

    if enabled_value is not None and enabled_value == 2:
        locked_bool = True
    else:
        locked_bool = False

    if pid_value is not None and pid_value > 0:
        running_bool = True
    else:
        running_bool = False

    rtnval = UbuntuAuditStatus(
        enabled=enabled_bool,
        running=running_bool,
        pid=pid_value,
        failure_mode=failure_value,
        rate_limit=rate_limit_value,
        backlog_limit=backlog_limit_value,
        backlog=backlog_value,
        lost_events=lost_value,
        locked=locked_bool,
        raw=text,
    )
    return rtnval


def _build_ausearch_argv(*, query: UbuntuAuditEventQuery) -> list:
    """
        Build the ``ausearch`` argv from a structured query.

        :param query: The :class:`UbuntuAuditEventQuery`.

        :returns: The argv as a list of strings.
    """
    if query.raw_filter is not None and len(query.raw_filter) > 0:
        rtnval = [_AUSEARCH] + query.raw_filter.split()
        return rtnval

    command = [_AUSEARCH]
    if query.key is not None and len(query.key) > 0:
        command.append("-k")
        command.append(query.key)
    if query.message_type is not None and len(query.message_type) > 0:
        command.append("-m")
        command.append(query.message_type)
    if query.since is not None and len(query.since) > 0:
        command.append("-ts")
        command.append(query.since)
    if query.until is not None and len(query.until) > 0:
        command.append("-te")
        command.append(query.until)
    if query.user is not None and len(query.user) > 0:
        command.append("-ui")
        command.append(query.user)
    rtnval = command
    return rtnval


def _parse_ausearch_output(
    *,
    text: str,
    limit: Optional[int],
) -> list:
    """
        Parse ``ausearch`` output into :class:`UbuntuAuditEvent`
        records.

        ``ausearch`` separates records with a ``----`` delimiter line
        followed by a ``time->`` header.  Each record's body consists
        of one or more ``type=<TYPE> msg=audit(<ts>:<serial>): ...``
        lines.  This parser groups consecutive non-delimiter lines
        into a single record and emits one
        :class:`UbuntuAuditEvent` per group.

        :param text: Raw stdout text.
        :param limit: Optional cap on the number of events returned.

        :returns: One :class:`UbuntuAuditEvent` per parsed record,
                  capped at ``limit`` when set.
    """
    rtnval: list = []
    if len(text.strip()) == 0:
        return rtnval

    current_lines: list = []
    delimiter = "----"

    def _flush() -> None:
        if len(current_lines) == 0:
            return
        body = "\n".join(current_lines)
        event = _parse_ausearch_record(body=body)
        rtnval.append(event)
        current_lines.clear()
        return

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.strip() == delimiter:
            _flush()
            if limit is not None and len(rtnval) >= limit:
                return rtnval
            continue
        current_lines.append(line)

    _flush()
    if limit is not None and len(rtnval) > limit:
        rtnval = rtnval[:limit]
    return rtnval


def _parse_ausearch_record(*, body: str) -> UbuntuAuditEvent:
    """
        Parse a single ``ausearch`` record body into an
        :class:`UbuntuAuditEvent`.

        :param body: The record text (one or more lines).

        :returns: The :class:`UbuntuAuditEvent`.
    """
    timestamp = ""
    serial: Optional[int] = None
    category: Optional[str] = None
    message_line = body.splitlines()[0] if len(body.splitlines()) > 0 else ""

    # Find the first line that starts with type=
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if len(line) == 0:
            continue
        if not line.startswith("type="):
            continue

        tokens = line.split()
        for token in tokens:
            if token.startswith("type="):
                _, _, value = token.partition("=")
                if category is None:
                    category = value
            if token.startswith("msg=audit("):
                # token like msg=audit(1700000000.123:456):
                _, _, payload = token.partition("(")
                payload = payload.rstrip("):").rstrip(")")
                if ":" in payload:
                    ts_part, _, serial_part = payload.partition(":")
                    timestamp = "{}:{}".format(ts_part, serial_part)
                    try:
                        serial = int(serial_part)
                    except ValueError:
                        serial = None
                else:
                    timestamp = payload
                    serial = None
        break

    rtnval = UbuntuAuditEvent(
        timestamp=timestamp,
        event_id=serial,
        category=category,
        severity=None,
        message=message_line,
        raw=body,
    )
    return rtnval
