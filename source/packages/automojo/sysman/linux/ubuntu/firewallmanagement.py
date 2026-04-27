"""
    Ubuntu firewall-management feature.

    Implements the cross-OS firewall-management contract for Ubuntu
    using ``ufw`` (Uncomplicated Firewall).  Every verb is dispatched
    through an :class:`ICommandRunner` so the manager works identically
    under local and SSH execution.

    Zone mapping:
        ``ufw`` has no zone concept.  This manager accepts the literal
        string ``"default"`` for every ``zone`` parameter; any other
        value yields a ``FAILED`` outcome on writes and an
        :class:`UbuntuFirewallQueryError` on reads.

    Elevation policy:
        Write verbs honour ``config.elevated``.  Reads (``query_zone``,
        ``query_all_zones``, ``query_state``) and idempotency probes
        never elevate.

    Surface choices:
        * Idempotency for every ``add_*`` / ``remove_*`` verb is
          driven by a single ``ufw status verbose`` parse — the
          resulting :class:`UbuntuZoneInfo` is consulted in pure Python
          before any mutating command is dispatched.
        * Rich-rule comparison normalises whitespace and lower-cases
          the action verb so coordinator-supplied rules can match the
          parser's reconstructed form.
        * ``set_default_zone`` is a validated no-op: ``"default"``
          short-circuits to ``SKIPPED`` and any other value to
          ``FAILED`` without invoking the runner.
"""

import os
import re

from dataclasses import dataclass, field
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_UFW = "ufw"
_DEFAULT_ZONE = "default"

_VALID_ACTION_VERBS = frozenset({"allow", "deny", "reject", "limit"})

_PORT_PATTERN = re.compile(r"^\d+/(?:tcp|udp)(?:\s*\(v6\))?$")
_NUMERIC_PORT_PATTERN = re.compile(r"^\d+(?:/(?:tcp|udp))?(?:\s*\(v6\))?$")


UbuntuFirewallActionStatus = ActionStatus


@dataclass
class UbuntuFirewallManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuFirewallManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig` with
        no additional fields.
    """
    pass


@dataclass
class UbuntuFirewallActionResult(FeatureActionResult):
    """
        Result of a firewall-management write operation.

        :param zone: The zone targeted.  For ufw this is always the
                     literal string ``"default"`` on success; the
                     incoming value is preserved on failure so
                     callers can see what they passed in.
        :param detail_name: Secondary identifier echoed onto the
                            result (service name, ``port/proto``
                            string, or rule string).
        :param message: Optional human-readable detail for ``SKIPPED`` /
                        ``FAILED`` outcomes.
    """
    zone: Optional[str] = None
    detail_name: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuZoneInfo:
    """
        Parsed view of ``ufw status verbose``.

        :param zone: Always ``"default"`` for ufw.
        :param services: Application-profile services parsed from the
                         status table (non-numeric ``To`` column entries).
        :param ports: ``"<port>/<proto>"`` strings parsed from the
                      status table.
        :param rich_rules: Reconstructed ufw-syntax rules for table
                           rows whose ``From`` column is anything
                           other than ``Anywhere``.
        :param interfaces: Always empty — ufw has no per-zone
                           interface concept.
        :param sources: Always empty — sources are folded into rich
                        rules.
        :param is_default: Always True for ufw.
    """
    zone: str
    services: list[str] = field(default_factory=list)
    ports: list[str] = field(default_factory=list)
    rich_rules: list[str] = field(default_factory=list)
    interfaces: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    is_default: bool = True


@dataclass
class UbuntuFirewallState:
    """
        Current ``ufw`` running state.

        :param running: True when ``ufw status`` reports
                        ``Status: active``.
        :param default_zone: ``"default"`` when running, ``""``
                             otherwise.
    """
    running: bool
    default_zone: str


class UbuntuFirewallQueryError(Exception):
    """
        Raised when a firewall read fails for a reason other than the
        target simply being absent.  Also raised when ``query_zone`` is
        invoked with a zone other than ``"default"``.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.  May be empty when
                            the error is raised pre-dispatch (zone
                            validation).
            :param exit_code: The runner's exit code, or ``None`` when
                              the error was raised pre-dispatch.
            :param stderr: The runner's captured stderr, or a
                           descriptive string when raised pre-dispatch.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Firewall query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuFirewallManager(BaseFeatureManager):
    """
        Ubuntu ufw firewall manager.  Every verb is dispatched through
        the injected :class:`ICommandRunner`.
    """

    result_class = UbuntuFirewallActionResult
    status_class = UbuntuFirewallActionStatus
    feature_name = "firewall-management"

    # ------------------------------------------------------------------
    # Write verbs — services
    # ------------------------------------------------------------------

    def add_service(
        self,
        service: str,
        zone: str,
    ) -> UbuntuFirewallActionResult:
        """
            Allow a ufw application profile in the default zone.

            Idempotent: ``SKIPPED`` when the service is already
            present in the parsed ``ufw status verbose`` output.

            :param service: ufw application-profile name (e.g.
                            ``OpenSSH``).
            :param zone: Must be ``"default"``; any other value
                         returns ``FAILED``.

            :returns: The :class:`UbuntuFirewallActionResult`.
        """
        command = [_UFW, "allow", service]
        zone_result = self._reject_non_default_zone(
            zone=zone,
            command=command,
            detail_name=service,
        )
        if zone_result is not None:
            return zone_result

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuFirewallActionStatus.DRY_RUN,
                zone=zone,
                detail_name=service,
                command=command,
            )
            return dry

        info = self.query_zone(zone=zone)
        if service in info.services:
            skipped = self._build_result(
                status=UbuntuFirewallActionStatus.SKIPPED,
                zone=zone,
                detail_name=service,
                command=command,
                message="service already present",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            zone=zone,
            detail_name=service,
        )
        return rtnval

    def remove_service(
        self,
        service: str,
        zone: str,
    ) -> UbuntuFirewallActionResult:
        """
            Remove a ufw application profile from the default zone.

            Idempotent: ``SKIPPED`` when the service is not present.

            :param service: ufw application-profile name.
            :param zone: Must be ``"default"``.

            :returns: The :class:`UbuntuFirewallActionResult`.
        """
        command = [_UFW, "delete", "allow", service]
        zone_result = self._reject_non_default_zone(
            zone=zone,
            command=command,
            detail_name=service,
        )
        if zone_result is not None:
            return zone_result

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuFirewallActionStatus.DRY_RUN,
                zone=zone,
                detail_name=service,
                command=command,
            )
            return dry

        info = self.query_zone(zone=zone)
        if service not in info.services:
            skipped = self._build_result(
                status=UbuntuFirewallActionStatus.SKIPPED,
                zone=zone,
                detail_name=service,
                command=command,
                message="service not present",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            zone=zone,
            detail_name=service,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — ports
    # ------------------------------------------------------------------

    def add_port(
        self,
        port: int,
        protocol: str,
        zone: str,
    ) -> UbuntuFirewallActionResult:
        """
            Open ``port/protocol`` in the default zone.

            Idempotent: ``SKIPPED`` when the port spec is already
            present.

            :param port: Port number.
            :param protocol: ``"tcp"`` or ``"udp"``.
            :param zone: Must be ``"default"``.

            :returns: The :class:`UbuntuFirewallActionResult`.
        """
        port_spec = "{}/{}".format(port, protocol)
        command = [_UFW, "allow", port_spec]
        zone_result = self._reject_non_default_zone(
            zone=zone,
            command=command,
            detail_name=port_spec,
        )
        if zone_result is not None:
            return zone_result

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuFirewallActionStatus.DRY_RUN,
                zone=zone,
                detail_name=port_spec,
                command=command,
            )
            return dry

        info = self.query_zone(zone=zone)
        if port_spec in info.ports:
            skipped = self._build_result(
                status=UbuntuFirewallActionStatus.SKIPPED,
                zone=zone,
                detail_name=port_spec,
                command=command,
                message="port already open",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            zone=zone,
            detail_name=port_spec,
        )
        return rtnval

    def remove_port(
        self,
        port: int,
        protocol: str,
        zone: str,
    ) -> UbuntuFirewallActionResult:
        """
            Close ``port/protocol`` in the default zone.

            Idempotent: ``SKIPPED`` when the port spec is not
            currently open.

            :param port: Port number.
            :param protocol: ``"tcp"`` or ``"udp"``.
            :param zone: Must be ``"default"``.

            :returns: The :class:`UbuntuFirewallActionResult`.
        """
        port_spec = "{}/{}".format(port, protocol)
        command = [_UFW, "delete", "allow", port_spec]
        zone_result = self._reject_non_default_zone(
            zone=zone,
            command=command,
            detail_name=port_spec,
        )
        if zone_result is not None:
            return zone_result

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuFirewallActionStatus.DRY_RUN,
                zone=zone,
                detail_name=port_spec,
                command=command,
            )
            return dry

        info = self.query_zone(zone=zone)
        if port_spec not in info.ports:
            skipped = self._build_result(
                status=UbuntuFirewallActionStatus.SKIPPED,
                zone=zone,
                detail_name=port_spec,
                command=command,
                message="port not open",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            zone=zone,
            detail_name=port_spec,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — rich rules
    # ------------------------------------------------------------------

    def add_rich_rule(
        self,
        rule: str,
        zone: str,
    ) -> UbuntuFirewallActionResult:
        """
            Append a raw ufw-syntax rule to the default zone.

            ``rule`` must begin with one of the ufw action verbs
            (``allow``, ``deny``, ``reject``, ``limit``).  Idempotent:
            ``SKIPPED`` when the normalised rule already appears in
            the parsed status output.

            :param rule: ufw rule string (without the leading ``ufw``
                         token).
            :param zone: Must be ``"default"``.

            :returns: The :class:`UbuntuFirewallActionResult`.
        """
        normalised_rule = _normalise_rule(rule=rule)
        verb = _extract_verb(rule=normalised_rule)

        command = [_UFW] + normalised_rule.split()

        zone_result = self._reject_non_default_zone(
            zone=zone,
            command=command,
            detail_name=rule,
        )
        if zone_result is not None:
            return zone_result

        if verb is None:
            failed = self._build_result(
                status=UbuntuFirewallActionStatus.FAILED,
                zone=zone,
                detail_name=rule,
                command=command,
                message=(
                    "rich rule must begin with one of: "
                    + ", ".join(sorted(_VALID_ACTION_VERBS))
                ),
                stderr="invalid_rich_rule_verb",
            )
            return failed

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuFirewallActionStatus.DRY_RUN,
                zone=zone,
                detail_name=rule,
                command=command,
            )
            return dry

        info = self.query_zone(zone=zone)
        existing_normalised = [
            _normalise_rule(rule=existing) for existing in info.rich_rules
        ]
        if normalised_rule in existing_normalised:
            skipped = self._build_result(
                status=UbuntuFirewallActionStatus.SKIPPED,
                zone=zone,
                detail_name=rule,
                command=command,
                message="rich rule already present",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            zone=zone,
            detail_name=rule,
        )
        return rtnval

    def remove_rich_rule(
        self,
        rule: str,
        zone: str,
    ) -> UbuntuFirewallActionResult:
        """
            Delete a raw ufw-syntax rule from the default zone.

            Idempotent: ``SKIPPED`` when the rule is not currently
            present.

            :param rule: ufw rule string (without the leading ``ufw``
                         token).
            :param zone: Must be ``"default"``.

            :returns: The :class:`UbuntuFirewallActionResult`.
        """
        normalised_rule = _normalise_rule(rule=rule)
        command = [_UFW, "delete"] + normalised_rule.split()

        zone_result = self._reject_non_default_zone(
            zone=zone,
            command=command,
            detail_name=rule,
        )
        if zone_result is not None:
            return zone_result

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuFirewallActionStatus.DRY_RUN,
                zone=zone,
                detail_name=rule,
                command=command,
            )
            return dry

        info = self.query_zone(zone=zone)
        existing_normalised = [
            _normalise_rule(rule=existing) for existing in info.rich_rules
        ]
        if normalised_rule not in existing_normalised:
            skipped = self._build_result(
                status=UbuntuFirewallActionStatus.SKIPPED,
                zone=zone,
                detail_name=rule,
                command=command,
                message="rich rule not present",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            zone=zone,
            detail_name=rule,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — default zone / reload
    # ------------------------------------------------------------------

    def set_default_zone(
        self,
        zone: str,
    ) -> UbuntuFirewallActionResult:
        """
            Validated no-op.  Returns ``SKIPPED`` for ``"default"``
            and ``FAILED`` for any other value.  Never invokes the
            runner.

            :param zone: Zone name.

            :returns: The :class:`UbuntuFirewallActionResult`.
        """
        if zone == _DEFAULT_ZONE:
            skipped = self._build_result(
                status=UbuntuFirewallActionStatus.SKIPPED,
                zone=zone,
                detail_name=zone,
                command=[],
                message="ufw has no zone concept; default is implicit",
            )
            return skipped

        failed = self._build_result(
            status=UbuntuFirewallActionStatus.FAILED,
            zone=zone,
            detail_name=zone,
            command=[],
            message=(
                "ufw supports only the 'default' zone; received '{}'".format(
                    zone,
                )
            ),
            stderr="unsupported_zone",
        )
        return failed

    def reload(self) -> UbuntuFirewallActionResult:
        """
            Reload ufw via ``ufw reload``.  Never skips.

            :returns: The :class:`UbuntuFirewallActionResult`.
        """
        command = [_UFW, "reload"]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuFirewallActionStatus.DRY_RUN,
                zone=_DEFAULT_ZONE,
                detail_name=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuFirewallActionStatus.SUCCESS
        else:
            mapped_status = UbuntuFirewallActionStatus.FAILED

        rtnval = UbuntuFirewallActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            zone=_DEFAULT_ZONE,
            detail_name=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def query_zone(self, zone: str) -> UbuntuZoneInfo:
        """
            Parse ``ufw status verbose`` into an
            :class:`UbuntuZoneInfo`.

            :param zone: Must be ``"default"``.

            :returns: The parsed :class:`UbuntuZoneInfo`.
            :raises UbuntuFirewallQueryError: When ``zone`` is not
                ``"default"`` or when the runner returns a non-zero
                exit with non-empty stderr.
        """
        if zone != _DEFAULT_ZONE:
            raise UbuntuFirewallQueryError(
                command=[],
                exit_code=None,
                stderr=(
                    "ufw supports only the 'default' zone; received '{}'"
                    .format(zone)
                ),
            )

        command = [_UFW, "status", "verbose"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise UbuntuFirewallQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_status_verbose(text=run_result.stdout)
        return rtnval

    def query_all_zones(self) -> list[UbuntuZoneInfo]:
        """
            Return a single-entry list containing the ``"default"``
            zone info.

            :returns: ``[UbuntuZoneInfo(zone="default", ...)]``.
            :raises UbuntuFirewallQueryError: Propagated from
                :meth:`query_zone`.
        """
        info = self.query_zone(zone=_DEFAULT_ZONE)
        rtnval = [info]
        return rtnval

    def query_state(self) -> UbuntuFirewallState:
        """
            Query running state via ``ufw status``.

            ``ufw status`` returning non-zero is not an error here —
            it is interpreted as ufw being inactive (``running=False``).

            :returns: The :class:`UbuntuFirewallState`.
        """
        command = [_UFW, "status"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = UbuntuFirewallState(running=False, default_zone="")
            return rtnval

        first_line = ""
        for raw in run_result.stdout.splitlines():
            stripped = raw.strip()
            if len(stripped) > 0:
                first_line = stripped
                break

        if first_line.startswith("Status: active"):
            rtnval = UbuntuFirewallState(
                running=True, default_zone=_DEFAULT_ZONE,
            )
        else:
            rtnval = UbuntuFirewallState(running=False, default_zone="")
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _reject_non_default_zone(
        self,
        *,
        zone: str,
        command: list[str],
        detail_name: Optional[str],
    ) -> Optional[UbuntuFirewallActionResult]:
        """
            Short-circuit when ``zone`` is not ``"default"``.

            :param zone: The zone the caller supplied.
            :param command: The would-have-been argv (echoed onto the
                            failed result).
            :param detail_name: The secondary identifier to echo onto
                                the result.

            :returns: A ``FAILED`` result when ``zone`` is unsupported;
                      ``None`` when the dispatch should proceed.
        """
        if zone == _DEFAULT_ZONE:
            return None

        failed = self._build_result(
            status=UbuntuFirewallActionStatus.FAILED,
            zone=zone,
            detail_name=detail_name,
            command=command,
            message=(
                "ufw supports only the 'default' zone; received '{}'".format(
                    zone,
                )
            ),
            stderr="unsupported_zone",
        )
        return failed

    def _dispatch_write(
        self,
        *,
        command: list[str],
        zone: str,
        detail_name: Optional[str],
    ) -> UbuntuFirewallActionResult:
        """
            Dispatch a write command and map the runner result onto
            an :class:`UbuntuFirewallActionResult`.

            :param command: The full argv.
            :param zone: The zone targeted.
            :param detail_name: The secondary identifier echoed onto
                                the result.

            :returns: The :class:`UbuntuFirewallActionResult`.
        """
        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = UbuntuFirewallActionStatus.SUCCESS
        else:
            mapped_status = UbuntuFirewallActionStatus.FAILED

        rtnval = UbuntuFirewallActionResult(
            status=mapped_status,
            detail=detail_name,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            zone=zone,
            detail_name=detail_name,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuFirewallActionStatus,
        zone: Optional[str],
        detail_name: Optional[str],
        command: list[str],
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuFirewallActionResult:
        """
            Construct an :class:`UbuntuFirewallActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param zone: Zone to echo onto the result.
            :param detail_name: Secondary identifier to echo onto the
                                result.
            :param command: The would-have-been argv (copied for
                            capture).
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`UbuntuFirewallActionResult`.
        """
        rtnval = UbuntuFirewallActionResult(
            status=status,
            detail=detail_name,
            command=list(command),
            zone=zone,
            detail_name=detail_name,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level parsers and helpers
# ----------------------------------------------------------------------


def _normalise_rule(*, rule: str) -> str:
    """
        Collapse internal whitespace runs and trim ends so coordinator-
        supplied rules can match the parser's reconstructed form.

        :param rule: The input rule string.

        :returns: The normalised rule string.
    """
    tokens = rule.split()
    if len(tokens) == 0:
        rtnval = ""
        return rtnval
    head = tokens[0].lower()
    if head in _VALID_ACTION_VERBS:
        tokens[0] = head
    rtnval = " ".join(tokens)
    return rtnval


def _extract_verb(*, rule: str) -> Optional[str]:
    """
        Return the leading action verb of ``rule`` when it is one of
        the accepted ufw verbs; otherwise ``None``.

        :param rule: A normalised rule string.

        :returns: The verb, or ``None``.
    """
    tokens = rule.split()
    if len(tokens) == 0:
        rtnval: Optional[str] = None
        return rtnval
    head = tokens[0].lower()
    if head in _VALID_ACTION_VERBS:
        rtnval = head
        return rtnval
    rtnval = None
    return rtnval


def _parse_status_verbose(*, text: str) -> UbuntuZoneInfo:
    """
        Parse ``ufw status verbose`` stdout into an
        :class:`UbuntuZoneInfo`.

        Header lines (``Status:``, ``Logging:``, ``Default:``,
        ``New profiles:``) are skipped.  The rule table starts at the
        ``To`` header line and continues until end-of-input or a blank
        line followed by another header.  Each rule row is split on
        two-or-more spaces, yielding ``(to_col, action_col, from_col)``.

        :param text: Raw stdout text.

        :returns: The parsed :class:`UbuntuZoneInfo`.
    """
    info = UbuntuZoneInfo(zone=_DEFAULT_ZONE)

    in_table = False
    seen_separator = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if len(line) == 0:
            continue

        stripped = line.strip()

        if stripped.startswith("To") and not in_table:
            in_table = True
            seen_separator = False
            continue

        if in_table and not seen_separator:
            # The dash separator follows the ``To`` header row.
            if set(stripped.replace(" ", "")).issubset({"-"}):
                seen_separator = True
                continue
            # Some ufw releases skip the separator entirely; in that
            # case treat the header itself as the only preamble.
            seen_separator = True

        if not in_table:
            # Header section — skipped.
            continue

        # A row in the table.  Split on two-or-more whitespace runs.
        columns = re.split(r"\s{2,}", stripped)
        if len(columns) < 3:
            # Fallback: split on any whitespace and rejoin tail tokens.
            tokens = stripped.split()
            if len(tokens) < 3:
                continue
            to_col = tokens[0]
            action_col = tokens[1]
            from_col = " ".join(tokens[2:])
        else:
            to_col = columns[0].strip()
            action_col = columns[1].strip()
            from_col = columns[2].strip()

        action_verb = _action_from_column(column=action_col)

        if from_col.startswith("Anywhere"):
            if _NUMERIC_PORT_PATTERN.match(to_col):
                # Strip any "(v6)" suffix and de-duplicate.
                base_token = to_col.split()[0]
                if "/" in base_token:
                    if base_token not in info.ports:
                        info.ports.append(base_token)
                else:
                    if base_token not in info.ports:
                        info.ports.append(base_token)
            else:
                # Non-numeric ``To`` column — application profile.
                base_token = to_col.split(" (")[0]
                if base_token not in info.services:
                    info.services.append(base_token)
        else:
            if action_verb is None:
                rebuilt = "{} from {} to {}".format(
                    "allow", from_col, to_col,
                )
            else:
                rebuilt = "{} from {} to {}".format(
                    action_verb, from_col, to_col,
                )
            normalised = _normalise_rule(rule=rebuilt)
            if normalised not in info.rich_rules:
                info.rich_rules.append(normalised)

    return info


def _action_from_column(*, column: str) -> Optional[str]:
    """
        Map an ``Action`` column value (``ALLOW IN``, ``DENY IN``,
        ``REJECT IN``, ``LIMIT IN``) to a ufw verb.

        :param column: The column value.

        :returns: The matched verb, or ``None`` when unrecognised.
    """
    upper = column.upper()
    if upper.startswith("ALLOW"):
        rtnval: Optional[str] = "allow"
    elif upper.startswith("DENY"):
        rtnval = "deny"
    elif upper.startswith("REJECT"):
        rtnval = "reject"
    elif upper.startswith("LIMIT"):
        rtnval = "limit"
    else:
        rtnval = None
    return rtnval
