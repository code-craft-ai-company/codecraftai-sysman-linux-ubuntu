"""
    Ubuntu mail-transport-management feature.

    Implements the cross-OS mail-transport-management contract for
    Ubuntu.  Ubuntu's default MTA is **Postfix** — installed by the
    ``postfix`` package and exposed under systemd as
    ``postfix.service``.  This module wraps the Postfix command suite
    (``postconf``, ``postqueue``, ``mailq``, ``newaliases``,
    ``sendmail``) and the on-disk aliases file (``/etc/aliases``)
    through the injected :class:`ICommandRunner` so the manager works
    identically under local and SSH execution.

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | Ubuntu tool                       |
    +=============================+===================================+
    | ``get_mta_config``          | ``postconf -n``                   |
    +-----------------------------+-----------------------------------+
    | ``set_mta_config``          | ``postconf -e <key>=<value>``     |
    |                             | per setting + ``systemctl reload``|
    +-----------------------------+-----------------------------------+
    | ``add_alias``               | ``cat`` + render +                |
    |                             | ``tee /etc/aliases`` +            |
    |                             | ``newaliases``                    |
    +-----------------------------+-----------------------------------+
    | ``remove_alias``            | ``cat`` + render +                |
    |                             | ``tee /etc/aliases`` +            |
    |                             | ``newaliases``                    |
    +-----------------------------+-----------------------------------+
    | ``list_aliases``            | ``cat /etc/aliases`` + parse      |
    +-----------------------------+-----------------------------------+
    | ``test_mail_delivery``      | ``sendmail -t`` with payload on   |
    |                             | stdin                             |
    +-----------------------------+-----------------------------------+
    | ``get_queue_status``        | ``mailq`` + parse                 |
    +-----------------------------+-----------------------------------+
    | ``flush_queue``             | ``postqueue -f``                  |
    +-----------------------------+-----------------------------------+

    File I/O policy:
        Reads use ``cat``; writes use ``tee`` with the new content on
        stdin.  This module does NOT consume an :class:`IFileOperator`.

    MTA selection caveat:
        The base contract notes that Ubuntu may also run ``exim4`` or
        ``sendmail``.  This pilot targets Postfix exclusively — it
        invokes ``postconf`` / ``postqueue`` / ``mailq`` /
        ``newaliases`` directly without first probing for the MTA in
        use.  Hosts running a non-Postfix MTA will surface ``FAILED``
        for write verbs and an empty / synthetic shape for reads.
        Cross-MTA support is a tracked follow-up captured in
        ``specs/3-mail-transport-management/tasks.md``.

    Idempotency model:
        ``set_mta_config`` reads the live ``postconf -n`` output, parses
        it into a directive map, and dispatches one ``postconf -e
        key=value`` per setting that diverges.  Settings whose live
        value already matches the spec are skipped silently and the
        per-setting argv is recorded in ``stdout`` for debuggability.
        ``add_alias`` and ``remove_alias`` read ``/etc/aliases``, parse
        it, mutate, re-render, and only invoke ``tee`` when the content
        diverges; ``newaliases`` is invoked unconditionally after any
        successful write to keep the compiled aliases database in sync.

    Aliases-database coherence:
        Every successful mutation of ``/etc/aliases`` is followed by
        ``newaliases``.  If ``newaliases`` itself fails, the verb maps
        to ``FAILED`` even when the text mutation succeeded — see
        :meth:`UbuntuMailTransportManager._invoke_newaliases` for the
        chained dispatch.

    Elevation policy:
        Write verbs (``set_mta_config``, ``add_alias``,
        ``remove_alias``, ``test_mail_delivery``, ``flush_queue``)
        honour ``config.elevated``.  Reads (``get_mta_config``,
        ``list_aliases``, ``get_queue_status``) never elevate.
"""

import os
import re

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
_POSTCONF = "postconf"
_POSTQUEUE = "postqueue"
_NEWALIASES = "newaliases"
_MAILQ = "mailq"
_SENDMAIL = "sendmail"
_SYSTEMCTL = "systemctl"

_DEFAULT_ALIASES_FILE = "/etc/aliases"
_DEFAULT_SERVICE_NAME = "postfix"

_KNOWN_MAIN_KEYS = (
    "myhostname",
    "mydomain",
    "myorigin",
    "relayhost",
    "mynetworks",
    "inet_interfaces",
    "inet_protocols",
    "mailbox_size_limit",
    "message_size_limit",
)

_INT_KEYS = (
    "mailbox_size_limit",
    "message_size_limit",
)


UbuntuMtaActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class UbuntuMtaManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuMailTransportManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param aliases_file: Absolute path to the ``aliases`` file.
                             Default is the Ubuntu stock location
                             ``/etc/aliases``.
        :param service_name: systemd unit name used to reload the MTA
                             after a successful ``set_mta_config``.
                             Default is ``"postfix"``.
        :param reload_after_set: When True, ``set_mta_config`` invokes
                                 ``systemctl reload <service_name>``
                                 after at least one ``postconf -e``
                                 dispatch succeeds.  Default is True.
    """
    aliases_file: str = _DEFAULT_ALIASES_FILE
    service_name: str = _DEFAULT_SERVICE_NAME
    reload_after_set: bool = True


@dataclass
class UbuntuMtaActionResult(FeatureActionResult):
    """
        Result of a mail-transport write operation.

        :param setting: The Postfix directive targeted, when applicable
                        (``set_mta_config`` reports the per-setting
                        directive that diverged on the result it returns
                        for that batch).
        :param local: The local alias name targeted, when applicable
                      (``add_alias`` / ``remove_alias``).
    """
    setting: Optional[str] = None
    local: Optional[str] = None


@dataclass
class UbuntuMtaConfig:
    """
        Parsed Postfix ``main.cf`` directive snapshot.

        Exposes the most commonly tweaked directives as named fields
        and stows the rest in ``extras`` for callers who need broader
        coverage without the field surface ballooning.

        :param myhostname: ``myhostname`` directive.
        :param mydomain: ``mydomain`` directive.
        :param myorigin: ``myorigin`` directive.
        :param relayhost: ``relayhost`` directive (upstream relay).
        :param mynetworks: ``mynetworks`` directive (CIDR list).
        :param inet_interfaces: ``inet_interfaces`` directive.
        :param inet_protocols: ``inet_protocols`` directive.
        :param mailbox_size_limit: ``mailbox_size_limit`` directive
                                   (bytes).
        :param message_size_limit: ``message_size_limit`` directive
                                   (bytes).
        :param extras: Every directive that is NOT mapped to a named
                       field, preserving original key/value text.
    """
    myhostname: Optional[str] = None
    mydomain: Optional[str] = None
    myorigin: Optional[str] = None
    relayhost: Optional[str] = None
    mynetworks: Optional[str] = None
    inet_interfaces: Optional[str] = None
    inet_protocols: Optional[str] = None
    mailbox_size_limit: Optional[int] = None
    message_size_limit: Optional[int] = None
    extras: dict = field(default_factory=dict)


@dataclass
class UbuntuMtaAliasInfo:
    """
        A single ``/etc/aliases`` entry.

        :param local: The local alias (left-hand side).
        :param remote: The remote target list (right-hand side, kept
                       verbatim — comma-separated when multiple).
    """
    local: str
    remote: str


@dataclass
class UbuntuMtaQueueStatus:
    """
        Aggregated Postfix mail-queue status.

        :param pending_count: Number of active / pending messages.
        :param deferred_count: Number of deferred messages.
        :param total_size_bytes: Aggregate queue size in bytes (or
                                 ``None`` when ``mailq`` did not report
                                 it).
        :param queue_age_oldest: Arrival-time text of the oldest
                                 message (or ``None`` when the queue is
                                 empty).
    """
    pending_count: int = 0
    deferred_count: int = 0
    total_size_bytes: Optional[int] = None
    queue_age_oldest: Optional[str] = None


class UbuntuMtaQueryError(Exception):
    """
        Raised when a mail-transport read fails for an unrecoverable
        reason.
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

        err_msg_lines = ["Mail transport query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class UbuntuMailTransportManager(BaseFeatureManager):
    """
        Ubuntu mail-transport-management manager.  Reads, mutates, and
        operates the Postfix MTA through the injected
        :class:`ICommandRunner`.
    """

    result_class = UbuntuMtaActionResult
    status_class = UbuntuMtaActionStatus
    feature_name = "mail-transport-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_mta_config(self) -> UbuntuMtaConfig:
        """
            Read the live Postfix configuration via ``postconf -n``.

            ``postconf -n`` prints non-default directives in
            ``key = value`` form, one per line.  Every recognised
            directive is mapped to its named field on
            :class:`UbuntuMtaConfig`; unrecognised directives go into
            ``extras``.

            :returns: The :class:`UbuntuMtaConfig`.
            :raises UbuntuMtaQueryError: When ``postconf`` exits
                                         non-zero with non-empty stderr.
        """
        command = [_POSTCONF, "-n"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise UbuntuMtaQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_postconf_n_output(text=run_result.stdout)
        return rtnval

    def list_aliases(self) -> list:
        """
            Enumerate every entry in the aliases file.

            Reads the file via ``cat`` (no elevation) and parses each
            non-comment, non-blank line as ``local: remote``.

            :returns: A ``list[UbuntuMtaAliasInfo]`` in file order.
                      Empty when the file is missing or contains no
                      entries.
        """
        path = self._typed_config().aliases_file
        content = self._read_file(path=path)
        if content is None:
            rtnval: list = []
            return rtnval

        rtnval = _parse_aliases_file(content=content)
        return rtnval

    def get_queue_status(self) -> UbuntuMtaQueueStatus:
        """
            Read the current Postfix mail-queue status via ``mailq``.

            ``mailq`` is a Postfix-shipped alias for ``postqueue -p``.
            The output begins with a queue-id / size / arrival /
            sender block per message; each block's queue-id has a
            trailing ``*`` for active messages and ``!`` for messages
            on hold.  Deferred messages have neither marker.  The
            footer line reports the aggregate byte count.

            :returns: The :class:`UbuntuMtaQueueStatus`.
            :raises UbuntuMtaQueryError: When ``mailq`` exits non-zero
                                         with non-empty stderr.
        """
        command = [_MAILQ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise UbuntuMtaQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_mailq_output(text=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_mta_config(
        self,
        spec: UbuntuMtaConfig,
    ) -> UbuntuMtaActionResult:
        """
            Apply the directive snapshot in ``spec`` to live Postfix.

            For each named field on :class:`UbuntuMtaConfig` that is
            not None, the manager dispatches ``postconf -e
            <key>=<value>`` when the live value differs.  Settings
            already at the desired value are skipped silently.  After
            all per-setting dispatches complete, ``systemctl reload
            <service_name>`` is invoked when at least one directive
            changed AND ``config.reload_after_set`` is True.

            Idempotent at the per-setting level: the aggregate result
            reports ``SKIPPED`` when every named directive is already
            at the desired value.

            :param spec: The :class:`UbuntuMtaConfig` snapshot.

            :returns: The :class:`UbuntuMtaActionResult` summarising
                      the per-setting outcomes.  ``stdout`` is set to a
                      newline-separated audit trail of the per-setting
                      argv that ran (or would have run under dry-run).
        """
        aggregate_command: list = [_POSTCONF, "-e"]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuMtaActionStatus.DRY_RUN,
                command=aggregate_command,
                detail="set_mta_config",
                local=None,
                setting=None,
            )
            return dry

        try:
            existing = self.get_mta_config()
        except UbuntuMtaQueryError as raised:
            failed = UbuntuMtaActionResult(
                status=UbuntuMtaActionStatus.FAILED,
                detail="set_mta_config",
                command=list(aggregate_command),
                local=None,
                setting=None,
                stderr=raised.stderr,
                exit_code=raised.exit_code,
            )
            return failed

        desired_pairs = _config_to_directive_pairs(config=spec)
        existing_pairs = _config_to_directive_pairs(config=existing)

        per_setting_audit: list = []
        any_changed = False
        first_failure: Optional[UbuntuMtaActionResult] = None

        for setting_key, desired_value in desired_pairs:
            existing_value = existing_pairs.get_first_value_for(
                key=setting_key,
            )
            if existing_value is not None:
                if existing_value == desired_value:
                    per_setting_audit.append(
                        "SKIPPED {}={}".format(setting_key, desired_value),
                    )
                    continue
            per_command = [
                _POSTCONF,
                "-e",
                "{}={}".format(setting_key, desired_value),
            ]
            per_run = self._runner.run(
                list(per_command),
                elevated=self._config.elevated,
            )
            if per_run.exit_code == 0:
                any_changed = True
                per_setting_audit.append(
                    "CHANGED {}={}".format(setting_key, desired_value),
                )
                continue

            failure = UbuntuMtaActionResult(
                status=UbuntuMtaActionStatus.FAILED,
                detail="set_mta_config",
                exit_code=per_run.exit_code,
                stdout=per_run.stdout,
                stderr=per_run.stderr,
                command=list(per_command),
                setting=setting_key,
                local=None,
            )
            if first_failure is None:
                first_failure = failure
            per_setting_audit.append(
                "FAILED {}={}".format(setting_key, desired_value),
            )

        if first_failure is not None:
            first_failure.stdout = "\n".join(per_setting_audit)
            return first_failure

        # Reload the daemon when at least one directive changed.
        if any_changed and self._typed_config().reload_after_set:
            reload_command = [
                _SYSTEMCTL,
                "reload",
                self._typed_config().service_name,
            ]
            reload_run = self._runner.run(
                list(reload_command),
                elevated=self._config.elevated,
            )
            per_setting_audit.append(
                "RELOAD exit={}".format(reload_run.exit_code),
            )
            if reload_run.exit_code != 0:
                failed = UbuntuMtaActionResult(
                    status=UbuntuMtaActionStatus.FAILED,
                    detail="set_mta_config",
                    exit_code=reload_run.exit_code,
                    stdout="\n".join(per_setting_audit),
                    stderr=reload_run.stderr,
                    command=list(reload_command),
                    setting=None,
                    local=None,
                )
                return failed

        if not any_changed:
            skipped = UbuntuMtaActionResult(
                status=UbuntuMtaActionStatus.SKIPPED,
                detail="set_mta_config",
                exit_code=0,
                stdout="\n".join(per_setting_audit),
                stderr=None,
                command=aggregate_command,
                setting=None,
                local=None,
            )
            return skipped

        rtnval = UbuntuMtaActionResult(
            status=UbuntuMtaActionStatus.SUCCESS,
            detail="set_mta_config",
            exit_code=0,
            stdout="\n".join(per_setting_audit),
            stderr=None,
            command=aggregate_command,
            setting=None,
            local=None,
        )
        return rtnval

    def add_alias(
        self,
        local: str,
        remote: str,
    ) -> UbuntuMtaActionResult:
        """
            Append (or update) an entry in the aliases file.

            Idempotent: returns ``SKIPPED`` when an entry for ``local``
            with the same ``remote`` already exists.  When ``local``
            exists with a different ``remote``, the line is replaced.

            On success, ``newaliases`` is invoked to rebuild the
            compiled aliases database.

            :param local: The local alias name.
            :param remote: The remote target list.

            :returns: The :class:`UbuntuMtaActionResult`.
        """
        path = self._typed_config().aliases_file
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        existing_aliases = _parse_aliases_file(content=existing_content)

        match_found = False
        for entry in existing_aliases:
            if entry.local == local:
                if entry.remote == remote:
                    skipped = self._build_result(
                        status=UbuntuMtaActionStatus.SKIPPED,
                        detail="add_alias:{}".format(local),
                        command=write_command,
                        local=local,
                        setting=None,
                        message_text="alias already at desired value",
                    )
                    return skipped
                match_found = True
                break

        new_content = _upsert_alias(
            content=existing_content, local=local, remote=remote,
        )
        _ = match_found  # parsed solely to seed the SKIPPED branch.

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuMtaActionStatus.DRY_RUN,
                detail="add_alias:{}".format(local),
                command=write_command,
                local=local,
                setting=None,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code != 0:
            failed = UbuntuMtaActionResult(
                status=UbuntuMtaActionStatus.FAILED,
                detail="add_alias:{}".format(local),
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                local=local,
                setting=None,
            )
            return failed

        rtnval = self._invoke_newaliases(
            base_action="add_alias:{}".format(local),
            local=local,
            primary_run=run_result,
            primary_command=write_command,
        )
        return rtnval

    def remove_alias(
        self,
        local: str,
    ) -> UbuntuMtaActionResult:
        """
            Strip the entry whose left-hand side equals ``local`` from
            the aliases file.

            Idempotent: returns ``SKIPPED`` when the file is missing or
            no entry matches.

            On success, ``newaliases`` is invoked to rebuild the
            compiled aliases database.

            :param local: The local alias name to remove.

            :returns: The :class:`UbuntuMtaActionResult`.
        """
        path = self._typed_config().aliases_file
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        if existing is None:
            skipped = self._build_result(
                status=UbuntuMtaActionStatus.SKIPPED,
                detail="remove_alias:{}".format(local),
                command=write_command,
                local=local,
                setting=None,
                message_text="aliases file does not exist",
            )
            return skipped

        new_content = _remove_alias(content=existing, local=local)
        if new_content == existing:
            skipped = self._build_result(
                status=UbuntuMtaActionStatus.SKIPPED,
                detail="remove_alias:{}".format(local),
                command=write_command,
                local=local,
                setting=None,
                message_text="alias not present",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuMtaActionStatus.DRY_RUN,
                detail="remove_alias:{}".format(local),
                command=write_command,
                local=local,
                setting=None,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if run_result.exit_code != 0:
            failed = UbuntuMtaActionResult(
                status=UbuntuMtaActionStatus.FAILED,
                detail="remove_alias:{}".format(local),
                exit_code=run_result.exit_code,
                stdout=run_result.stdout,
                stderr=run_result.stderr,
                command=list(write_command),
                local=local,
                setting=None,
            )
            return failed

        rtnval = self._invoke_newaliases(
            base_action="remove_alias:{}".format(local),
            local=local,
            primary_run=run_result,
            primary_command=write_command,
        )
        return rtnval

    def test_mail_delivery(
        self,
        to: str,
        from_addr: str,
        subject: str,
    ) -> UbuntuMtaActionResult:
        """
            Hand a probe message to ``sendmail -t`` for delivery.

            The payload is built in pure Python and forwarded to the
            runner on stdin in RFC 5322 form (``Subject:`` /
            ``From:`` / ``To:`` headers, blank line, fixed body).
            ``-t`` instructs sendmail to extract recipients from the
            headers themselves.

            Operational verb — never skips.  Honours dry-run.

            :param to: The recipient address.
            :param from_addr: The envelope-from address.
            :param subject: The message subject.

            :returns: The :class:`UbuntuMtaActionResult`.
        """
        command = [_SENDMAIL, "-t"]
        payload = _build_probe_message(
            to=to, from_addr=from_addr, subject=subject,
        )
        detail_text = "test_mail_delivery:{}".format(to)

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuMtaActionStatus.DRY_RUN,
                detail=detail_text,
                command=command,
                local=None,
                setting=None,
            )
            dry.stdout = payload
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
            stdin=payload,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            detail=detail_text,
            local=None,
            setting=None,
        )
        return rtnval

    def flush_queue(self) -> UbuntuMtaActionResult:
        """
            Attempt immediate delivery of every queued message via
            ``postqueue -f``.

            Operational verb — never skips.  Honours dry-run.

            :returns: The :class:`UbuntuMtaActionResult`.
        """
        command = [_POSTQUEUE, "-f"]
        detail_text = "flush_queue"

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuMtaActionStatus.DRY_RUN,
                detail=detail_text,
                command=command,
                local=None,
                setting=None,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            detail=detail_text,
            local=None,
            setting=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> UbuntuMtaManagerConfig:
        """
            Narrow the base config to the typed subclass.

            :returns: The typed :class:`UbuntuMtaManagerConfig`.
        """
        rtnval = self._config
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat`` without elevation.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when
                      ``cat`` reports a non-zero exit code.
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
        status: UbuntuMtaActionStatus,
        detail: Optional[str],
        command: list,
        local: Optional[str],
        setting: Optional[str],
        message_text: Optional[str] = None,
    ) -> UbuntuMtaActionResult:
        """
            Build a result without invoking the runner.

            :param status: Outcome status.
            :param detail: Short identifier.
            :param command: Would-have-been argv.
            :param local: Alias local name, when applicable.
            :param setting: Postfix directive name, when applicable.
            :param message_text: Optional human-readable detail piped
                                 into ``stderr`` for SKIPPED outcomes.

            :returns: The :class:`UbuntuMtaActionResult`.
        """
        rtnval = UbuntuMtaActionResult(
            status=status,
            detail=detail,
            command=list(command),
            local=local,
            setting=setting,
        )
        if message_text is not None:
            rtnval.stderr = message_text
        return rtnval

    def _wrap_runner_result(
        self,
        *,
        run_result,
        command: list,
        detail: Optional[str],
        local: Optional[str],
        setting: Optional[str],
    ) -> UbuntuMtaActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`UbuntuMtaActionResult`.

            :param run_result: The runner's :class:`RunResult`.
            :param command: The argv that ran.
            :param detail: Short identifier.
            :param local: Alias local name, when applicable.
            :param setting: Postfix directive name, when applicable.

            :returns: The :class:`UbuntuMtaActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = UbuntuMtaActionStatus.SUCCESS
        else:
            mapped_status = UbuntuMtaActionStatus.FAILED

        rtnval = UbuntuMtaActionResult(
            status=mapped_status,
            detail=detail,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            local=local,
            setting=setting,
        )
        return rtnval

    def _invoke_newaliases(
        self,
        *,
        base_action: str,
        local: str,
        primary_run,
        primary_command: list,
    ) -> UbuntuMtaActionResult:
        """
            Rebuild the aliases database via ``newaliases`` after a
            successful aliases-file mutation.

            :param base_action: The ``add_alias`` / ``remove_alias``
                                detail string to embed.
            :param local: The alias local name.
            :param primary_run: The :class:`RunResult` from the
                                preceding ``tee`` call.
            :param primary_command: The argv that ran first.

            :returns: A :class:`UbuntuMtaActionResult` whose status
                      reflects the ``newaliases`` outcome.
        """
        newaliases_command = [_NEWALIASES]
        newaliases_run = self._runner.run(
            list(newaliases_command),
            elevated=self._config.elevated,
        )
        if newaliases_run.exit_code != 0:
            rtnval = UbuntuMtaActionResult(
                status=UbuntuMtaActionStatus.FAILED,
                detail=base_action,
                exit_code=newaliases_run.exit_code,
                stdout=newaliases_run.stdout,
                stderr=newaliases_run.stderr,
                command=list(newaliases_command),
                local=local,
                setting=None,
            )
            return rtnval

        rtnval = UbuntuMtaActionResult(
            status=UbuntuMtaActionStatus.SUCCESS,
            detail=base_action,
            exit_code=primary_run.exit_code,
            stdout=primary_run.stdout,
            stderr=primary_run.stderr,
            command=list(primary_command),
            local=local,
            setting=None,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — directive pairs & parsing
# ----------------------------------------------------------------------


class _DirectivePairs:
    """
        Ordered list of ``(key, value)`` directive pairs with a tiny
        ``get_first_value_for`` helper.  Acts as the in-memory
        representation of the per-setting deltas the manager iterates
        over when applying a config snapshot.

        We use a class rather than a plain tuple so the helper is
        self-documenting and the tests can introspect the list cleanly.
    """

    def __init__(self, *, pairs: list) -> None:
        """
            :param pairs: Ordered list of ``(key, value)`` tuples.
        """
        self._pairs: list = list(pairs)
        return

    def get_first_value_for(self, *, key: str) -> Optional[str]:
        """
            Return the value of the first pair whose key equals ``key``.

            :param key: The directive name.

            :returns: The value, or ``None`` when no such pair exists.
        """
        for pair_key, pair_value in self._pairs:
            if pair_key == key:
                rtnval: Optional[str] = pair_value
                return rtnval
        rtnval = None
        return rtnval

    def __iter__(self):
        return iter(self._pairs)

    def __len__(self) -> int:
        rtnval = len(self._pairs)
        return rtnval


def _config_to_directive_pairs(
    *,
    config: UbuntuMtaConfig,
) -> _DirectivePairs:
    """
        Flatten an :class:`UbuntuMtaConfig` into ``(key, value)``
        pairs in stable named-field-then-extras order.

        Integer fields are rendered using ``str(int_value)``; string
        fields with ``None`` values are skipped.

        :param config: The config snapshot.

        :returns: A :class:`_DirectivePairs`.
    """
    flattened: list = []
    for key in _KNOWN_MAIN_KEYS:
        attr_value = getattr(config, key)
        if attr_value is None:
            continue
        if key in _INT_KEYS:
            text_value = str(int(attr_value))
        else:
            text_value = str(attr_value)
        flattened.append((key, text_value))

    for extra_key, extra_value in config.extras.items():
        flattened.append((extra_key, str(extra_value)))

    rtnval = _DirectivePairs(pairs=flattened)
    return rtnval


def _parse_postconf_n_output(*, text: str) -> UbuntuMtaConfig:
    """
        Parse ``postconf -n`` output into an :class:`UbuntuMtaConfig`.

        Each non-comment line is of the form ``key = value``.  Leading
        and trailing whitespace around either side is stripped.  Lines
        that fail to split are ignored.

        :param text: The captured stdout.

        :returns: The :class:`UbuntuMtaConfig`.
    """
    rtnval = UbuntuMtaConfig()
    for raw in text.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        before, after = stripped.split("=", 1)
        key = before.strip()
        value = after.strip()
        _apply_directive(config=rtnval, key=key, value=value)

    return rtnval


def _apply_directive(
    *,
    config: UbuntuMtaConfig,
    key: str,
    value: str,
) -> None:
    """
        Place a single directive into the named field or ``extras``.

        :param config: The config to mutate.
        :param key: The directive name.
        :param value: The directive value.
    """
    if key in _INT_KEYS:
        try:
            int_value = int(value)
        except ValueError:
            int_value = None
        if int_value is not None:
            setattr(config, key, int_value)
            return
        # Fall through to extras when the value is non-numeric.
        config.extras[key] = value
        return

    if key in _KNOWN_MAIN_KEYS:
        setattr(config, key, value)
        return

    config.extras[key] = value
    return


# ----------------------------------------------------------------------
# Module-level helpers — aliases parser / renderer
# ----------------------------------------------------------------------


def _parse_aliases_file(*, content: str) -> list:
    """
        Parse the aliases file content into entries.

        Comment and blank lines are skipped.  Each remaining line is
        split on the first ``:``; the left-hand side is the local
        name (lower-cased per Postfix's case-insensitive matching),
        and the right-hand side is the remote target list.

        Continuation lines (starting with whitespace) are appended to
        the previous entry's remote.

        :param content: Raw file content.

        :returns: A ``list[UbuntuMtaAliasInfo]`` in file order.
    """
    rtnval: list = []
    for raw in content.splitlines():
        if len(raw.strip()) == 0:
            continue
        if raw.strip().startswith("#"):
            continue
        if raw[0] in (" ", "\t") and len(rtnval) > 0:
            previous = rtnval[-1]
            previous.remote = "{} {}".format(
                previous.remote, raw.strip(),
            )
            continue
        if ":" not in raw:
            continue
        before, after = raw.split(":", 1)
        local = before.strip()
        remote = after.strip()
        if len(local) == 0:
            continue
        rtnval.append(UbuntuMtaAliasInfo(local=local, remote=remote))
    return rtnval


def _upsert_alias(
    *,
    content: str,
    local: str,
    remote: str,
) -> str:
    """
        Return content with ``local: remote`` upserted.

        Existing entries whose left-hand side matches ``local`` are
        replaced in place; otherwise a new line is appended.

        :param content: Existing file content.
        :param local: Local alias name.
        :param remote: Remote target list.

        :returns: The updated content.
    """
    new_lines: list = []
    replaced = False
    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0 or stripped.startswith("#"):
            new_lines.append(raw)
            continue
        if ":" not in raw:
            new_lines.append(raw)
            continue
        before, _ = raw.split(":", 1)
        existing_local = before.strip()
        if existing_local == local:
            new_lines.append("{}: {}".format(local, remote))
            replaced = True
            continue
        new_lines.append(raw)

    if not replaced:
        new_lines.append("{}: {}".format(local, remote))

    body = "\n".join(new_lines)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _remove_alias(
    *,
    content: str,
    local: str,
) -> str:
    """
        Return content with every entry whose left-hand side matches
        ``local`` stripped.

        :param content: Existing file content.
        :param local: Local alias name to remove.

        :returns: The updated content.
    """
    new_lines: list = []
    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0 or stripped.startswith("#"):
            new_lines.append(raw)
            continue
        if ":" not in raw:
            new_lines.append(raw)
            continue
        before, _ = raw.split(":", 1)
        existing_local = before.strip()
        if existing_local == local:
            continue
        new_lines.append(raw)

    body = "\n".join(new_lines)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body


# ----------------------------------------------------------------------
# Module-level helpers — mailq parser
# ----------------------------------------------------------------------


_MAILQ_FOOTER_PATTERN = re.compile(
    r"--\s*(?P<bytes>\d+)\s+Kbytes\s+in\s+(?P<count>\d+)\s+Request",
    re.IGNORECASE,
)
_MAILQ_HEAD_PATTERN = re.compile(
    r"^(?P<qid>[A-Za-z0-9]+)(?P<marker>[*!]?)\s+(?P<size>\d+)\s+(?P<arrival>\S.*?\S)\s+\S+@\S+",
)


def _parse_mailq_output(*, text: str) -> UbuntuMtaQueueStatus:
    """
        Parse ``mailq`` output into a :class:`UbuntuMtaQueueStatus`.

        Postfix's ``mailq`` (= ``postqueue -p``) emits one
        head-line per message, followed by optional error-text and
        recipient lines.  Records are blank-line separated.  The
        special string ``Mail queue is empty`` indicates an empty
        queue.

        Active messages have ``*`` after the queue id; on-hold
        messages have ``!``.  Deferred messages have neither marker.

        :param text: The captured stdout.

        :returns: The :class:`UbuntuMtaQueueStatus`.
    """
    rtnval = UbuntuMtaQueueStatus()

    if "Mail queue is empty" in text:
        return rtnval

    pending = 0
    deferred = 0
    oldest_arrival: Optional[str] = None

    for raw in text.splitlines():
        match = _MAILQ_HEAD_PATTERN.match(raw)
        if match is None:
            continue
        marker = match.group("marker")
        if marker == "*":
            pending = pending + 1
        else:
            deferred = deferred + 1
        if oldest_arrival is None:
            oldest_arrival = match.group("arrival")

    rtnval.pending_count = pending
    rtnval.deferred_count = deferred
    rtnval.queue_age_oldest = oldest_arrival

    footer_match = _MAILQ_FOOTER_PATTERN.search(text)
    if footer_match is not None:
        kbytes = int(footer_match.group("bytes"))
        rtnval.total_size_bytes = kbytes * 1024

    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — probe message
# ----------------------------------------------------------------------


def _build_probe_message(
    *,
    to: str,
    from_addr: str,
    subject: str,
) -> str:
    """
        Build the RFC 5322 payload handed to ``sendmail -t`` on stdin.

        The body is fixed (a short audit-tag line) so the contract's
        signature does not need to expose the body to callers.  The
        message ends with a single trailing newline.

        :param to: Recipient address.
        :param from_addr: Sender address.
        :param subject: Subject text.

        :returns: The fully rendered message.
    """
    lines = [
        "Subject: {}".format(subject),
        "From: {}".format(from_addr),
        "To: {}".format(to),
        "",
        "codecraftai-sysman mail-transport probe",
        "",
    ]
    rtnval = "\n".join(lines)
    return rtnval
