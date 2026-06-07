"""
    Ubuntu dns-server-management feature.

    Implements the cross-OS dns-server-management contract for Ubuntu
    using the BIND 9 daemon (``named``) — Ubuntu ships BIND through the
    ``bind9`` package and exposes the daemon as the ``bind9.service``
    systemd unit.  Every verb is dispatched through the injected
    :class:`ICommandRunner` so the manager works identically under
    local and SSH execution.

    Verb / tool mapping:

    +---------------------+-------------------------------------------+
    | Verb                | Ubuntu tool                               |
    +=====================+===========================================+
    | ``add_zone``        | append sentinel-bracketed ``zone {}``     |
    |                     | block to ``/etc/bind/named.conf.local``,  |
    |                     | render the zone-data file at              |
    |                     | ``/etc/bind/zones/db.<name>``, validate   |
    |                     | with ``named-checkconf`` /                |
    |                     | ``named-checkzone``, then                 |
    |                     | ``rndc reconfig``.                        |
    +---------------------+-------------------------------------------+
    | ``remove_zone``     | strip sentinel-bracketed block from       |
    |                     | ``named.conf.local`` + ``rm`` zone file + |
    |                     | ``rndc reconfig``.                        |
    +---------------------+-------------------------------------------+
    | ``list_zones``      | parse sentinel-bracketed blocks from      |
    |                     | ``named.conf.local``;                     |
    |                     | ``named-checkconf -p`` for the full set.  |
    +---------------------+-------------------------------------------+
    | ``add_record`` /    | edit zone-data file, bump SOA serial, and |
    | ``remove_record``   | dispatch ``rndc reload <zone>``.          |
    +---------------------+-------------------------------------------+
    | ``list_records``    | ``cat`` zone-data file then parse.        |
    +---------------------+-------------------------------------------+
    | ``reload_zone``     | ``rndc reload <zone>`` (unconditional).   |
    +---------------------+-------------------------------------------+
    | ``get_zone_status`` | ``rndc zonestatus <zone>`` then parse.    |
    +---------------------+-------------------------------------------+

    File I/O policy:
        Reads use ``cat``; writes use ``tee`` with the new content on
        stdin; deletions use ``rm``.  This module does NOT consume an
        :class:`IFileOperator`.

    Sentinel-bracketed block convention:
        Every managed ``zone "<name>" {}`` declaration in
        ``/etc/bind/named.conf.local`` is wrapped between
        ``# >>> codecraftai-zone:<name> >>>`` and
        ``# <<< codecraftai-zone:<name> <<<`` comment lines so that
        :meth:`UbuntuDnsServerManager.list_zones` and
        :meth:`UbuntuDnsServerManager.remove_zone` can locate the
        block without disturbing operator-authored declarations.

    SOA serial bumping:
        Every successful :meth:`UbuntuDnsServerManager.add_record`,
        :meth:`UbuntuDnsServerManager.remove_record` invocation
        rewrites the SOA record's serial to today's
        ``YYYYMMDDNN`` value (with ``NN`` incremented when an existing
        serial already targets today).  BIND's slave servers rely on
        this monotonic increase to detect refreshes — failing to bump
        is a common operator error.

    Idempotency:
        * ``add_zone``: when the rendered zone-data file already exists
          on disk AND the ``named.conf.local`` block already contains
          the canonical ``zone {}`` declaration, the verb returns
          ``SKIPPED``.
        * ``remove_zone``: when neither the zone file nor the
          ``named.conf.local`` block is present, the verb returns
          ``SKIPPED``.
        * ``add_record``: when an identical record (same name, type,
          and value) is already present in the zone file, the verb
          returns ``SKIPPED``.
        * ``remove_record``: when no matching record is on disk,
          the verb returns ``SKIPPED``.
        * ``reload_zone`` and ``get_zone_status`` never skip.

    Elevation policy:
        Write verbs (``add_zone``, ``remove_zone``, ``add_record``,
        ``remove_record``, ``reload_zone``) honour
        ``config.elevated``.  Reads (``list_zones``,
        ``list_records``, ``get_zone_status``) never elevate.

    Validate-before-apply:
        The contract requires that when a configuration validator
        exists, writes MUST run it before dispatching the apply
        command.  Ubuntu's BIND ships ``named-checkconf`` and
        ``named-checkzone``; the manager invokes them after writing
        the new content but before issuing the ``rndc`` reload.  A
        validation failure flips the verb's status to ``FAILED`` and
        suppresses the reload dispatch.
"""

import datetime
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
_RM = "rm"
_TEST = "test"
_RNDC = "rndc"
_NAMED_CHECKCONF = "named-checkconf"
_NAMED_CHECKZONE = "named-checkzone"

_DEFAULT_NAMED_CONF_LOCAL = "/etc/bind/named.conf.local"
_DEFAULT_ZONE_DIR = "/etc/bind/zones"
_DEFAULT_SERVICE_NAME = "bind9"

_SENTINEL_OPEN_TEMPLATE = "# >>> codecraftai-zone:{name} >>>"
_SENTINEL_CLOSE_TEMPLATE = "# <<< codecraftai-zone:{name} <<<"

_DEFAULT_TTL = 3600
_DEFAULT_SOA_REFRESH = 3600
_DEFAULT_SOA_RETRY = 600
_DEFAULT_SOA_EXPIRE = 1209600
_DEFAULT_SOA_MINIMUM = 86400

_VALID_RECORD_TYPES = (
    "A", "AAAA", "CNAME", "MX", "TXT", "SRV", "PTR", "NS", "SOA",
)


UbuntuDnsActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class UbuntuDnsManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuDnsServerManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param named_conf_local: Path to the ``named.conf.local``
                                 declaration file.  Defaults to
                                 ``/etc/bind/named.conf.local``
                                 (Ubuntu / Debian convention).
        :param zone_dir: Directory holding zone-data files.
                         Defaults to ``/etc/bind/zones``.
        :param service_name: ``systemctl`` unit name for the BIND
                             daemon.  Defaults to ``"bind9"``
                             (Debian unit name).
    """
    named_conf_local: str = _DEFAULT_NAMED_CONF_LOCAL
    zone_dir: str = _DEFAULT_ZONE_DIR
    service_name: str = _DEFAULT_SERVICE_NAME


@dataclass
class UbuntuDnsActionResult(FeatureActionResult):
    """
        Result of an :class:`UbuntuDnsServerManager` write verb.

        :param zone: The DNS zone the action targeted, when
                     applicable.
        :param record_name: The owner-name portion of the record
                            the action targeted, when applicable.
        :param message: Optional human-readable detail for
                        ``SKIPPED`` / ``FAILED`` outcomes.
    """
    zone: Optional[str] = None
    record_name: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuDnsZoneSpec:
    """
        Declarative specification for a BIND zone.

        :param name: The zone name (e.g. ``"example.com"``).
        :param zone_type: BIND zone type token.  Defaults to
                          ``"primary"``; the renderer maps this to
                          ``master`` for compatibility with older
                          ``named.conf`` consumers.
        :param zone_file: Optional override for the on-disk
                          zone-data file path.  When ``None``, the
                          manager uses
                          ``<zone_dir>/db.<name>``.
        :param allow_update: When ``True``, the rendered
                             ``zone {}`` declaration includes
                             ``allow-update { localhost; };`` so
                             that dynamic-update tooling
                             (``nsupdate``) can mutate the zone.
                             Defaults to ``False``.
        :param replication_scope: Reserved for cross-OS parity
                                  with the Windows AD-integrated
                                  zones.  Ubuntu / BIND ignores
                                  this field; the renderer
                                  records it on the result for
                                  audit purposes only.
    """
    name: str
    zone_type: str = "primary"
    zone_file: Optional[str] = None
    allow_update: bool = False
    replication_scope: Optional[str] = None


@dataclass
class UbuntuDnsZoneInfo:
    """
        One row from
        :meth:`UbuntuDnsServerManager.list_zones`.

        :param name: The zone name.
        :param zone_type: BIND zone type token (``"primary"``,
                          ``"secondary"``, ``"forward"``,
                          ``"stub"``).
        :param zone_file: Path to the zone-data file when
                          declared; ``None`` for forward / stub
                          zones.
        :param record_count: Number of resource records in the
                             zone-data file when the parser could
                             read it; ``None`` otherwise.
        :param is_active: True when the manager could find the
                          declaration in
                          ``named.conf.local``; False when the
                          row was synthesised from
                          ``named-checkconf -p`` output for an
                          operator-authored declaration the
                          manager does not own.
    """
    name: str
    zone_type: str
    zone_file: Optional[str]
    record_count: Optional[int]
    is_active: bool


@dataclass
class UbuntuDnsRecordSpec:
    """
        Declarative specification for a single resource record.

        :param name: Owner-name token (e.g. ``"www"``,
                     ``"mail"``, or ``"@"`` for the apex).
        :param record_type: One of ``"A"``, ``"AAAA"``,
                            ``"CNAME"``, ``"MX"``, ``"TXT"``,
                            ``"SRV"``, ``"PTR"``, ``"NS"``, or
                            ``"SOA"``.
        :param value: Right-hand side rdata text in canonical
                      BIND format (e.g. ``"203.0.113.5"`` for an
                      A record, or
                      ``"10 mail.example.com."`` for an MX
                      record).
        :param ttl: Optional TTL in seconds.  ``None`` falls back
                    to the zone's default TTL (``$TTL``).
        :param priority: Optional priority field for MX / SRV
                         records.  When set, the renderer emits
                         it as the leading rdata token.  Most
                         callers embed the priority in ``value``
                         instead.
    """
    name: str
    record_type: str
    value: str
    ttl: Optional[int] = None
    priority: Optional[int] = None


@dataclass
class UbuntuDnsRecordInfo:
    """
        One row from
        :meth:`UbuntuDnsServerManager.list_records`.

        :param name: Owner-name token.
        :param record_type: Record type token.
        :param value: Rdata text.
        :param ttl: TTL when the parser could extract it;
                    ``None`` otherwise.
        :param priority: Priority for MX / SRV records when the
                         parser could extract it; ``None``
                         otherwise.
        :param serial: SOA serial parsed from the zone-data
                       file's SOA record; ``None`` for non-SOA
                       rows.
    """
    name: str
    record_type: str
    value: str
    ttl: Optional[int] = None
    priority: Optional[int] = None
    serial: Optional[int] = None


@dataclass
class UbuntuDnsZoneStatus:
    """
        Result of
        :meth:`UbuntuDnsServerManager.get_zone_status`.

        :param name: The zone the status describes.
        :param is_loaded: True when ``rndc zonestatus`` reports
                          the zone is loaded.
        :param serial: SOA serial parsed from the
                       ``rndc zonestatus`` output.
        :param last_loaded: ISO-8601 timestamp of the last load,
                            when reported.
        :param transferred_from: Master server address for
                                 secondary zones, when reported.
    """
    name: str
    is_loaded: bool
    serial: Optional[int] = None
    last_loaded: Optional[str] = None
    transferred_from: Optional[str] = None


class UbuntuDnsQueryError(Exception):
    """
        Raised when a dns-server read fails for a reason other
        than the target simply being absent.
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

        err_msg_lines = ["DNS server query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# UbuntuDnsServerManager
# ----------------------------------------------------------------------


class UbuntuDnsServerManager(BaseFeatureManager):
    """
        Ubuntu BIND-9 DNS-server manager.

        Every verb is dispatched through the injected
        :class:`ICommandRunner`.  File I/O is performed via
        ``cat`` / ``tee`` / ``rm`` rather than a separate file
        operator.
    """

    result_class = UbuntuDnsActionResult
    status_class = UbuntuDnsActionStatus
    feature_name = "dns-server-management"

    def __init__(
        self,
        runner,
        config: Optional[UbuntuDnsManagerConfig] = None,
    ) -> None:
        """
            :param runner: The command runner used by every
                           dispatch.
            :param config: Manager configuration.  Defaults to a
                           fresh
                           :class:`UbuntuDnsManagerConfig` when
                           omitted.
        """
        if config is None:
            config = UbuntuDnsManagerConfig()

        super().__init__(runner=runner, config=config)
        return

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_zone(
        self,
        name: str,
        spec: UbuntuDnsZoneSpec,
    ) -> UbuntuDnsActionResult:
        """
            Declare a new zone.

            Renders the canonical ``zone "<name>" {}`` declaration
            into ``/etc/bind/named.conf.local`` between sentinel
            comment lines and writes a stub zone-data file at
            ``<zone_dir>/db.<name>``.  After validation
            (``named-checkconf`` then ``named-checkzone``) the
            verb dispatches ``rndc reconfig``.

            Idempotent: returns ``SKIPPED`` when the sentinel
            block is already present in ``named.conf.local`` AND
            the zone-data file already exists on disk.

            :param name: The zone name (e.g. ``"example.com"``).
            :param spec: The :class:`UbuntuDnsZoneSpec`.

            :returns: The :class:`UbuntuDnsActionResult`.
        """
        cfg = self._typed_config()
        zone_file_path = self._zone_file_path(name=name, spec=spec)
        write_command = [_TEE, cfg.named_conf_local]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuDnsActionStatus.DRY_RUN,
                zone=name,
                record_name=None,
                command=write_command,
            )
            return dry

        existing_local = self._read_file(path=cfg.named_conf_local)
        existing_local_text = "" if existing_local is None else existing_local
        block_present = _sentinel_block_present(
            content=existing_local_text, name=name,
        )
        zone_file_present = self._path_exists(path=zone_file_path)

        if block_present and zone_file_present:
            skipped = self._build_result(
                status=UbuntuDnsActionStatus.SKIPPED,
                zone=name,
                record_name=None,
                command=write_command,
                message="zone already declared and zone file present",
            )
            return skipped

        zone_block = _render_zone_block(
            name=name, spec=spec, zone_file_path=zone_file_path,
        )
        new_local = _upsert_sentinel_block(
            content=existing_local_text,
            name=name,
            block_body=zone_block,
        )

        zone_file_body = _render_initial_zone_file(name=name)

        # Step 1: write the named.conf.local content.
        local_write_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_local,
        )
        if local_write_result.exit_code != 0:
            failed = self._wrap_failure(
                run_result=local_write_result,
                command=write_command,
                zone=name,
                record_name=None,
                message="tee named.conf.local failed",
            )
            return failed

        # Step 2: write the zone-data file.
        zone_write_command = [_TEE, zone_file_path]
        zone_write_result = self._runner.run(
            list(zone_write_command),
            elevated=self._config.elevated,
            stdin=zone_file_body,
        )
        if zone_write_result.exit_code != 0:
            failed = self._wrap_failure(
                run_result=zone_write_result,
                command=zone_write_command,
                zone=name,
                record_name=None,
                message="tee zone file failed",
            )
            return failed

        # Step 3: validate via named-checkconf and named-checkzone.
        validation_failure = self._validate_configuration(
            zone_name=name, zone_file_path=zone_file_path,
        )
        if validation_failure is not None:
            failed = self._build_result(
                status=UbuntuDnsActionStatus.FAILED,
                zone=name,
                record_name=None,
                command=write_command,
                message=validation_failure,
                stderr=validation_failure,
            )
            return failed

        # Step 4: dispatch rndc reconfig.
        reconfig_command = [_RNDC, "reconfig"]
        reconfig_result = self._runner.run(
            list(reconfig_command),
            elevated=self._config.elevated,
        )
        if reconfig_result.exit_code != 0:
            failed = self._wrap_failure(
                run_result=reconfig_result,
                command=reconfig_command,
                zone=name,
                record_name=None,
                message="rndc reconfig failed after add_zone",
            )
            return failed

        rtnval = UbuntuDnsActionResult(
            status=UbuntuDnsActionStatus.SUCCESS,
            detail=name,
            exit_code=reconfig_result.exit_code,
            stdout=reconfig_result.stdout,
            stderr=reconfig_result.stderr,
            command=list(reconfig_command),
            zone=name,
            record_name=None,
        )
        return rtnval

    def remove_zone(self, name: str) -> UbuntuDnsActionResult:
        """
            Remove a zone declaration and its on-disk zone-data
            file.

            Strips the sentinel-bracketed block from
            ``named.conf.local`` and ``rm``s the zone-data file
            at ``<zone_dir>/db.<name>``.  After validation
            (``named-checkconf``) the verb dispatches
            ``rndc reconfig``.

            Idempotent: returns ``SKIPPED`` when neither the
            sentinel block nor the zone-data file is present.

            :param name: The zone name to remove.

            :returns: The :class:`UbuntuDnsActionResult`.
        """
        cfg = self._typed_config()
        zone_file_path = "{}/db.{}".format(cfg.zone_dir, name)
        rm_command = [_RM, "-f", zone_file_path]
        write_command = [_TEE, cfg.named_conf_local]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuDnsActionStatus.DRY_RUN,
                zone=name,
                record_name=None,
                command=write_command,
            )
            return dry

        existing_local = self._read_file(path=cfg.named_conf_local)
        existing_local_text = "" if existing_local is None else existing_local
        block_present = _sentinel_block_present(
            content=existing_local_text, name=name,
        )
        zone_file_present = self._path_exists(path=zone_file_path)

        if not block_present and not zone_file_present:
            skipped = self._build_result(
                status=UbuntuDnsActionStatus.SKIPPED,
                zone=name,
                record_name=None,
                command=write_command,
                message="zone declaration and zone file already absent",
            )
            return skipped

        if block_present:
            new_local = _remove_sentinel_block(
                content=existing_local_text, name=name,
            )
            local_write_result = self._runner.run(
                list(write_command),
                elevated=self._config.elevated,
                stdin=new_local,
            )
            if local_write_result.exit_code != 0:
                failed = self._wrap_failure(
                    run_result=local_write_result,
                    command=write_command,
                    zone=name,
                    record_name=None,
                    message="tee named.conf.local failed",
                )
                return failed

        if zone_file_present:
            rm_result = self._runner.run(
                list(rm_command),
                elevated=self._config.elevated,
            )
            if rm_result.exit_code != 0:
                failed = self._wrap_failure(
                    run_result=rm_result,
                    command=rm_command,
                    zone=name,
                    record_name=None,
                    message="rm of zone file failed",
                )
                return failed

        validation_failure = self._validate_configuration(
            zone_name=None, zone_file_path=None,
        )
        if validation_failure is not None:
            failed = self._build_result(
                status=UbuntuDnsActionStatus.FAILED,
                zone=name,
                record_name=None,
                command=write_command,
                message=validation_failure,
                stderr=validation_failure,
            )
            return failed

        reconfig_command = [_RNDC, "reconfig"]
        reconfig_result = self._runner.run(
            list(reconfig_command),
            elevated=self._config.elevated,
        )
        if reconfig_result.exit_code != 0:
            failed = self._wrap_failure(
                run_result=reconfig_result,
                command=reconfig_command,
                zone=name,
                record_name=None,
                message="rndc reconfig failed after remove_zone",
            )
            return failed

        rtnval = UbuntuDnsActionResult(
            status=UbuntuDnsActionStatus.SUCCESS,
            detail=name,
            exit_code=reconfig_result.exit_code,
            stdout=reconfig_result.stdout,
            stderr=reconfig_result.stderr,
            command=list(reconfig_command),
            zone=name,
            record_name=None,
        )
        return rtnval

    def add_record(
        self,
        zone: str,
        record: UbuntuDnsRecordSpec,
    ) -> UbuntuDnsActionResult:
        """
            Add a resource record to ``zone``.

            Reads the zone-data file via ``cat``, parses the SOA
            record, appends the rendered record line, bumps the
            SOA serial, writes the new content via ``tee``, and
            dispatches ``rndc reload <zone>``.

            Idempotent: returns ``SKIPPED`` when an identical
            record (same ``name``, ``record_type``, and
            ``value``) is already present.

            :param zone: The zone name.
            :param record: The :class:`UbuntuDnsRecordSpec`.

            :returns: The :class:`UbuntuDnsActionResult`.
        """
        cfg = self._typed_config()
        zone_file_path = "{}/db.{}".format(cfg.zone_dir, zone)
        write_command = [_TEE, zone_file_path]

        validation_failure = _validate_record(record=record)
        if validation_failure is not None:
            failed = self._build_result(
                status=UbuntuDnsActionStatus.FAILED,
                zone=zone,
                record_name=record.name,
                command=write_command,
                message=validation_failure,
                stderr=validation_failure,
            )
            return failed

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuDnsActionStatus.DRY_RUN,
                zone=zone,
                record_name=record.name,
                command=write_command,
            )
            return dry

        existing = self._read_file(path=zone_file_path)
        if existing is None:
            failed = self._build_result(
                status=UbuntuDnsActionStatus.FAILED,
                zone=zone,
                record_name=record.name,
                command=write_command,
                message="zone file not found",
                stderr="zone_file_missing",
            )
            return failed

        existing_records = _parse_zone_records(content=existing)
        if _record_already_present(records=existing_records, record=record):
            skipped = self._build_result(
                status=UbuntuDnsActionStatus.SKIPPED,
                zone=zone,
                record_name=record.name,
                command=write_command,
                message="record already present",
            )
            return skipped

        new_content = _append_record_line(
            content=existing, record=record,
        )
        new_content = _bump_soa_serial(content=new_content)

        write_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if write_result.exit_code != 0:
            failed = self._wrap_failure(
                run_result=write_result,
                command=write_command,
                zone=zone,
                record_name=record.name,
                message="tee zone file failed",
            )
            return failed

        validation_failure = self._validate_configuration(
            zone_name=zone, zone_file_path=zone_file_path,
        )
        if validation_failure is not None:
            failed = self._build_result(
                status=UbuntuDnsActionStatus.FAILED,
                zone=zone,
                record_name=record.name,
                command=write_command,
                message=validation_failure,
                stderr=validation_failure,
            )
            return failed

        reload_command = [_RNDC, "reload", zone]
        reload_result = self._runner.run(
            list(reload_command),
            elevated=self._config.elevated,
        )
        if reload_result.exit_code != 0:
            failed = self._wrap_failure(
                run_result=reload_result,
                command=reload_command,
                zone=zone,
                record_name=record.name,
                message="rndc reload failed after add_record",
            )
            return failed

        rtnval = UbuntuDnsActionResult(
            status=UbuntuDnsActionStatus.SUCCESS,
            detail=record.name,
            exit_code=reload_result.exit_code,
            stdout=reload_result.stdout,
            stderr=reload_result.stderr,
            command=list(reload_command),
            zone=zone,
            record_name=record.name,
        )
        return rtnval

    def remove_record(
        self,
        zone: str,
        record: UbuntuDnsRecordSpec,
    ) -> UbuntuDnsActionResult:
        """
            Remove a resource record from ``zone``.

            Reads the zone-data file, drops every line whose
            (name, type, value) tuple matches ``record``, bumps
            the SOA serial, and dispatches
            ``rndc reload <zone>``.

            Idempotent: returns ``SKIPPED`` when no matching
            record is on disk.

            :param zone: The zone name.
            :param record: The :class:`UbuntuDnsRecordSpec` to
                           remove.

            :returns: The :class:`UbuntuDnsActionResult`.
        """
        cfg = self._typed_config()
        zone_file_path = "{}/db.{}".format(cfg.zone_dir, zone)
        write_command = [_TEE, zone_file_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuDnsActionStatus.DRY_RUN,
                zone=zone,
                record_name=record.name,
                command=write_command,
            )
            return dry

        existing = self._read_file(path=zone_file_path)
        if existing is None:
            skipped = self._build_result(
                status=UbuntuDnsActionStatus.SKIPPED,
                zone=zone,
                record_name=record.name,
                command=write_command,
                message="zone file does not exist",
            )
            return skipped

        existing_records = _parse_zone_records(content=existing)
        if not _record_already_present(
            records=existing_records, record=record,
        ):
            skipped = self._build_result(
                status=UbuntuDnsActionStatus.SKIPPED,
                zone=zone,
                record_name=record.name,
                command=write_command,
                message="record not present",
            )
            return skipped

        new_content = _strip_record_line(
            content=existing, record=record,
        )
        new_content = _bump_soa_serial(content=new_content)

        write_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        if write_result.exit_code != 0:
            failed = self._wrap_failure(
                run_result=write_result,
                command=write_command,
                zone=zone,
                record_name=record.name,
                message="tee zone file failed",
            )
            return failed

        validation_failure = self._validate_configuration(
            zone_name=zone, zone_file_path=zone_file_path,
        )
        if validation_failure is not None:
            failed = self._build_result(
                status=UbuntuDnsActionStatus.FAILED,
                zone=zone,
                record_name=record.name,
                command=write_command,
                message=validation_failure,
                stderr=validation_failure,
            )
            return failed

        reload_command = [_RNDC, "reload", zone]
        reload_result = self._runner.run(
            list(reload_command),
            elevated=self._config.elevated,
        )
        if reload_result.exit_code != 0:
            failed = self._wrap_failure(
                run_result=reload_result,
                command=reload_command,
                zone=zone,
                record_name=record.name,
                message="rndc reload failed after remove_record",
            )
            return failed

        rtnval = UbuntuDnsActionResult(
            status=UbuntuDnsActionStatus.SUCCESS,
            detail=record.name,
            exit_code=reload_result.exit_code,
            stdout=reload_result.stdout,
            stderr=reload_result.stderr,
            command=list(reload_command),
            zone=zone,
            record_name=record.name,
        )
        return rtnval

    def reload_zone(self, name: str) -> UbuntuDnsActionResult:
        """
            Dispatch ``rndc reload <zone>`` unconditionally.

            Honours dry-run and elevation; never skips.

            :param name: The zone name to reload.

            :returns: The :class:`UbuntuDnsActionResult`.
        """
        command = [_RNDC, "reload", name]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuDnsActionStatus.DRY_RUN,
                zone=name,
                record_name=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_run_result(
            run_result=run_result,
            command=command,
            zone=name,
            record_name=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_zones(self) -> list:
        """
            Enumerate every managed zone.

            Reads ``named.conf.local`` via ``cat``, parses every
            sentinel-bracketed managed block, and probes the
            corresponding zone-data file to compute
            ``record_count``.  Then dispatches
            ``named-checkconf -p`` to fold in any operator-authored
            zone declarations the manager does not own.

            :returns: One :class:`UbuntuDnsZoneInfo` per zone.

            :raises UbuntuDnsQueryError: When
                                         ``named-checkconf -p``
                                         exits non-zero with
                                         non-empty stderr.
        """
        cfg = self._typed_config()
        rtnval: list = []

        local_content = self._read_file(path=cfg.named_conf_local)
        if local_content is not None:
            managed_blocks = _parse_managed_zone_blocks(
                content=local_content,
            )
            for managed in managed_blocks:
                zone_file = managed.get("zone_file")
                record_count: Optional[int] = None
                if zone_file is not None and len(zone_file) > 0:
                    body = self._read_file(path=zone_file)
                    if body is not None:
                        records = _parse_zone_records(content=body)
                        record_count = len(records)

                info = UbuntuDnsZoneInfo(
                    name=managed["name"],
                    zone_type=managed.get("zone_type", "primary"),
                    zone_file=zone_file,
                    record_count=record_count,
                    is_active=True,
                )
                rtnval.append(info)

        # Fold in any operator-authored zones surfaced by
        # ``named-checkconf -p``.
        checkconf_command = [_NAMED_CHECKCONF, "-p"]
        checkconf_result = self._runner.run(
            list(checkconf_command), elevated=False,
        )
        if (
            checkconf_result.exit_code != 0
            and len(checkconf_result.stderr.strip()) > 0
        ):
            raise UbuntuDnsQueryError(
                command=checkconf_command,
                exit_code=checkconf_result.exit_code,
                stderr=checkconf_result.stderr,
            )

        existing_names = set()
        for managed_info in rtnval:
            existing_names.add(managed_info.name)

        external_zones = _parse_named_checkconf_p(
            content=checkconf_result.stdout,
        )
        for external in external_zones:
            external_name = external["name"]
            if external_name in existing_names:
                continue
            external_info = UbuntuDnsZoneInfo(
                name=external_name,
                zone_type=external.get("zone_type", "primary"),
                zone_file=external.get("zone_file"),
                record_count=None,
                is_active=False,
            )
            rtnval.append(external_info)
        return rtnval

    def list_records(self, zone: str) -> list:
        """
            Enumerate the resource records in a zone.

            Reads ``<zone_dir>/db.<zone>`` via ``cat`` and parses
            every record line into a
            :class:`UbuntuDnsRecordInfo`.

            :param zone: The zone name.

            :returns: One :class:`UbuntuDnsRecordInfo` per record.

            :raises UbuntuDnsQueryError: When ``cat`` exits
                                         non-zero with non-empty
                                         stderr.
        """
        cfg = self._typed_config()
        zone_file_path = "{}/db.{}".format(cfg.zone_dir, zone)

        cat_command = [_CAT, zone_file_path]
        run_result = self._runner.run(list(cat_command), elevated=False)
        if run_result.exit_code != 0:
            if len(run_result.stderr.strip()) > 0:
                raise UbuntuDnsQueryError(
                    command=cat_command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            rtnval: list = []
            return rtnval

        rtnval = _parse_zone_records(content=run_result.stdout)
        return rtnval

    def get_zone_status(self, name: str) -> UbuntuDnsZoneStatus:
        """
            Query a zone's runtime status via
            ``rndc zonestatus <zone>``.

            :param name: The zone name.

            :returns: The :class:`UbuntuDnsZoneStatus`.

            :raises UbuntuDnsQueryError: When ``rndc zonestatus``
                                         exits non-zero with
                                         non-empty stderr.
        """
        command = [_RNDC, "zonestatus", name]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            if len(run_result.stderr.strip()) > 0:
                raise UbuntuDnsQueryError(
                    command=command,
                    exit_code=run_result.exit_code,
                    stderr=run_result.stderr,
                )
            rtnval = UbuntuDnsZoneStatus(
                name=name,
                is_loaded=False,
                serial=None,
                last_loaded=None,
                transferred_from=None,
            )
            return rtnval

        parsed = _parse_rndc_zonestatus(text=run_result.stdout)
        rtnval = UbuntuDnsZoneStatus(
            name=name,
            is_loaded=parsed["is_loaded"],
            serial=parsed.get("serial"),
            last_loaded=parsed.get("last_loaded"),
            transferred_from=parsed.get("transferred_from"),
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> UbuntuDnsManagerConfig:
        """
            Return the manager's config typed as
            :class:`UbuntuDnsManagerConfig`.

            :returns: The active config typed.
        """
        if isinstance(self._config, UbuntuDnsManagerConfig):
            rtnval = self._config
        else:
            rtnval = UbuntuDnsManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _zone_file_path(
        self,
        *,
        name: str,
        spec: UbuntuDnsZoneSpec,
    ) -> str:
        """
            Resolve the on-disk zone-data file path for a zone.

            :param name: The zone name.
            :param spec: The :class:`UbuntuDnsZoneSpec`.

            :returns: The absolute zone-data file path.
        """
        if spec.zone_file is not None and len(spec.zone_file) > 0:
            rtnval = spec.zone_file
            return rtnval
        cfg = self._typed_config()
        rtnval = "{}/db.{}".format(cfg.zone_dir, name)
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat``.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when
                      the file is missing or unreadable.
        """
        command = [_CAT, path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _path_exists(self, *, path: str) -> bool:
        """
            Probe a path's existence via ``test -e``.

            :param path: The absolute path to probe.

            :returns: True when ``test -e`` exits zero; False
                      otherwise.
        """
        command = [_TEST, "-e", path]
        run_result = self._runner.run(list(command), elevated=False)
        rtnval = run_result.exit_code == 0
        return rtnval

    def _validate_configuration(
        self,
        *,
        zone_name: Optional[str],
        zone_file_path: Optional[str],
    ) -> Optional[str]:
        """
            Run ``named-checkconf`` and (when both arguments are
            supplied) ``named-checkzone <zone> <file>`` against
            the proposed configuration.

            :param zone_name: The zone name to validate, or
                              ``None`` to skip the per-zone
                              check.
            :param zone_file_path: The zone-data file to
                                   validate, or ``None`` to skip.

            :returns: ``None`` when validation passes; an
                      error-message string when it fails.
        """
        checkconf_command = [_NAMED_CHECKCONF]
        checkconf_result = self._runner.run(
            list(checkconf_command),
            elevated=self._config.elevated,
        )
        if checkconf_result.exit_code != 0:
            rtnval: Optional[str] = (
                "named-checkconf failed: {}".format(
                    checkconf_result.stderr,
                )
            )
            return rtnval

        if zone_name is None or zone_file_path is None:
            rtnval = None
            return rtnval

        checkzone_command = [
            _NAMED_CHECKZONE, zone_name, zone_file_path,
        ]
        checkzone_result = self._runner.run(
            list(checkzone_command),
            elevated=self._config.elevated,
        )
        if checkzone_result.exit_code != 0:
            rtnval = "named-checkzone failed: {}".format(
                checkzone_result.stderr,
            )
            return rtnval

        rtnval = None
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuDnsActionStatus,
        zone: Optional[str],
        record_name: Optional[str],
        command: list,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> UbuntuDnsActionResult:
        """
            Construct an :class:`UbuntuDnsActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param zone: The zone to echo onto the result.
            :param record_name: The record owner-name to echo
                                onto the result.
            :param command: The would-have-been argv.
            :param message: Optional human-readable detail.
            :param stderr: Optional synthetic stderr payload.

            :returns: The :class:`UbuntuDnsActionResult`.
        """
        rtnval = UbuntuDnsActionResult(
            status=status,
            detail=zone,
            command=list(command),
            zone=zone,
            record_name=record_name,
            message=message,
            stderr=stderr,
        )
        return rtnval

    def _wrap_failure(
        self,
        *,
        run_result,
        command: list,
        zone: Optional[str],
        record_name: Optional[str],
        message: str,
    ) -> UbuntuDnsActionResult:
        """
            Map a non-zero :class:`RunResult` to a ``FAILED``
            :class:`UbuntuDnsActionResult`.
        """
        rtnval = UbuntuDnsActionResult(
            status=UbuntuDnsActionStatus.FAILED,
            detail=zone,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            zone=zone,
            record_name=record_name,
            message=message,
        )
        return rtnval

    def _wrap_run_result(
        self,
        *,
        run_result,
        command: list,
        zone: Optional[str],
        record_name: Optional[str],
    ) -> UbuntuDnsActionResult:
        """
            Map a :class:`RunResult` to an
            :class:`UbuntuDnsActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = UbuntuDnsActionStatus.SUCCESS
        else:
            mapped_status = UbuntuDnsActionStatus.FAILED

        rtnval = UbuntuDnsActionResult(
            status=mapped_status,
            detail=zone,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            zone=zone,
            record_name=record_name,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — sentinel block management
# ----------------------------------------------------------------------


def _sentinel_open(*, name: str) -> str:
    """
        Render the sentinel-open comment line for ``name``.

        :param name: The zone name.

        :returns: The comment line.
    """
    rtnval = _SENTINEL_OPEN_TEMPLATE.format(name=name)
    return rtnval


def _sentinel_close(*, name: str) -> str:
    """
        Render the sentinel-close comment line for ``name``.

        :param name: The zone name.

        :returns: The comment line.
    """
    rtnval = _SENTINEL_CLOSE_TEMPLATE.format(name=name)
    return rtnval


def _sentinel_block_present(*, content: str, name: str) -> bool:
    """
        Probe whether ``content`` already contains a managed
        block for ``name``.

        :param content: The full ``named.conf.local`` body.
        :param name: The zone name.

        :returns: True when the open / close sentinel lines are
                  both present.
    """
    open_line = _sentinel_open(name=name)
    close_line = _sentinel_close(name=name)
    rtnval = open_line in content and close_line in content
    return rtnval


def _upsert_sentinel_block(
    *,
    content: str,
    name: str,
    block_body: str,
) -> str:
    """
        Replace the existing sentinel block for ``name`` with
        ``block_body``, or append a new block when no managed
        block exists yet.

        :param content: The full ``named.conf.local`` body.
        :param name: The zone name.
        :param block_body: The replacement zone block content
                           (without the sentinel lines).

        :returns: The new file content.
    """
    open_line = _sentinel_open(name=name)
    close_line = _sentinel_close(name=name)

    if not _sentinel_block_present(content=content, name=name):
        if len(content) > 0 and not content.endswith("\n"):
            new_content = content + "\n"
        else:
            new_content = content
        new_content = new_content + open_line + "\n"
        new_content = new_content + block_body
        if not block_body.endswith("\n"):
            new_content = new_content + "\n"
        new_content = new_content + close_line + "\n"
        rtnval = new_content
        return rtnval

    new_lines: list = []
    inside = False
    inserted = False
    for raw_line in content.splitlines():
        if raw_line == open_line:
            inside = True
            new_lines.append(open_line)
            for body_line in block_body.splitlines():
                new_lines.append(body_line)
            inserted = True
            continue
        if raw_line == close_line:
            inside = False
            new_lines.append(close_line)
            continue
        if inside:
            continue
        new_lines.append(raw_line)
    if not inserted:
        new_lines.append(open_line)
        for body_line in block_body.splitlines():
            new_lines.append(body_line)
        new_lines.append(close_line)
    rtnval = "\n".join(new_lines)
    if len(rtnval) > 0 and not rtnval.endswith("\n"):
        rtnval = rtnval + "\n"
    return rtnval


def _remove_sentinel_block(*, content: str, name: str) -> str:
    """
        Strip the managed block for ``name`` from ``content``.

        :param content: The full ``named.conf.local`` body.
        :param name: The zone name.

        :returns: The new file content.
    """
    open_line = _sentinel_open(name=name)
    close_line = _sentinel_close(name=name)

    new_lines: list = []
    inside = False
    for raw_line in content.splitlines():
        if raw_line == open_line:
            inside = True
            continue
        if raw_line == close_line:
            inside = False
            continue
        if inside:
            continue
        new_lines.append(raw_line)
    rtnval = "\n".join(new_lines)
    if len(rtnval) > 0 and not rtnval.endswith("\n"):
        rtnval = rtnval + "\n"
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — zone block / zone file rendering
# ----------------------------------------------------------------------


def _render_zone_block(
    *,
    name: str,
    spec: UbuntuDnsZoneSpec,
    zone_file_path: str,
) -> str:
    """
        Render the ``zone "<name>" {}`` declaration for
        ``named.conf.local``.

        BIND accepts ``master`` / ``slave`` as legacy synonyms of
        ``primary`` / ``secondary``; the renderer maps the
        spec's ``zone_type`` to the legacy form for maximum
        compatibility with older ``named.conf`` consumers.

        :param name: The zone name.
        :param spec: The :class:`UbuntuDnsZoneSpec`.
        :param zone_file_path: The on-disk zone-data file path.

        :returns: The rendered block (no leading / trailing
                  sentinel lines).
    """
    type_token = _map_zone_type(zone_type=spec.zone_type)

    body_lines: list = []
    body_lines.append("zone \"{}\" {{".format(name))
    body_lines.append("    type {};".format(type_token))
    body_lines.append("    file \"{}\";".format(zone_file_path))
    if spec.allow_update:
        body_lines.append("    allow-update { localhost; };")
    body_lines.append("};")
    rtnval = "\n".join(body_lines) + "\n"
    return rtnval


def _map_zone_type(*, zone_type: str) -> str:
    """
        Map the abstract zone type to the BIND token.

        :param zone_type: The spec's ``zone_type``.

        :returns: The BIND token.
    """
    lowered = zone_type.lower()
    if lowered == "primary":
        rtnval = "master"
        return rtnval
    if lowered == "secondary":
        rtnval = "slave"
        return rtnval
    if lowered in ("forward", "stub", "master", "slave"):
        rtnval = lowered
        return rtnval
    rtnval = "master"
    return rtnval


def _render_initial_zone_file(*, name: str) -> str:
    """
        Render an empty zone-data file body for ``name``.

        The initial body contains a ``$TTL`` directive, a stub
        SOA record (``ns1.<name>.`` as the primary nameserver
        and ``hostmaster.<name>.`` as the responsible person),
        and a single ``NS`` record pointing back at ``ns1``.

        :param name: The zone name.

        :returns: The zone-data file body.
    """
    today_serial = _today_serial()

    lines: list = []
    lines.append("; Generated by ccai UbuntuDnsServerManager")
    lines.append("$TTL    {}".format(_DEFAULT_TTL))
    lines.append(
        "@       IN      SOA     ns1.{name}. hostmaster.{name}. (".format(
            name=name,
        ),
    )
    lines.append("                {}        ; Serial".format(today_serial))
    lines.append(
        "                {}             ; Refresh".format(
            _DEFAULT_SOA_REFRESH,
        ),
    )
    lines.append(
        "                {}              ; Retry".format(
            _DEFAULT_SOA_RETRY,
        ),
    )
    lines.append(
        "                {}          ; Expire".format(
            _DEFAULT_SOA_EXPIRE,
        ),
    )
    lines.append(
        "                {} )           ; Minimum TTL".format(
            _DEFAULT_SOA_MINIMUM,
        ),
    )
    lines.append(";")
    lines.append("@       IN      NS      ns1.{}.".format(name))
    rtnval = "\n".join(lines) + "\n"
    return rtnval


def _today_serial() -> int:
    """
        Return today's BIND-style YYYYMMDD00 serial.

        :returns: The integer serial.
    """
    today = datetime.date.today()
    rtnval = int("{:04d}{:02d}{:02d}00".format(
        today.year, today.month, today.day,
    ))
    return rtnval


def _bump_soa_serial(*, content: str) -> str:
    """
        Increment the SOA serial in ``content`` to today's
        ``YYYYMMDDNN`` value, or to ``serial + 1`` when the
        existing serial already targets today's date.

        :param content: The zone-data file body.

        :returns: The updated file body.
    """
    target_serial = _today_serial()
    new_lines: list = []
    seen_serial = False
    for raw_line in content.splitlines():
        if seen_serial is True:
            new_lines.append(raw_line)
            continue
        stripped = raw_line.strip()
        if "Serial" not in stripped and "serial" not in stripped:
            new_lines.append(raw_line)
            continue
        # Find the first integer token on the line.
        candidate_lines = _replace_first_integer(
            line=raw_line, target=target_serial,
        )
        new_lines.append(candidate_lines)
        seen_serial = True
    rtnval = "\n".join(new_lines)
    if len(rtnval) > 0 and not rtnval.endswith("\n"):
        rtnval = rtnval + "\n"
    return rtnval


def _replace_first_integer(*, line: str, target: int) -> str:
    """
        Replace the first integer token in ``line`` with
        ``target``, bumping the existing value when it is already
        ``>= target``.

        :param line: The line to mutate.
        :param target: The desired minimum serial.

        :returns: The mutated line.
    """
    tokens: list = []
    replaced = False
    for token in line.split():
        if replaced:
            tokens.append(token)
            continue
        try:
            current_value = int(token)
        except ValueError:
            tokens.append(token)
            continue
        if current_value >= target:
            new_value = current_value + 1
        else:
            new_value = target
        tokens.append(str(new_value))
        replaced = True
    if not replaced:
        rtnval = line
        return rtnval
    rtnval = " ".join(tokens)
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — record rendering and parsing
# ----------------------------------------------------------------------


def _validate_record(*, record: UbuntuDnsRecordSpec) -> Optional[str]:
    """
        Validate a record spec before the renderer runs.

        :param record: The :class:`UbuntuDnsRecordSpec`.

        :returns: ``None`` when valid; an error-message string
                  when not.
    """
    if record.name is None or len(record.name) == 0:
        rtnval: Optional[str] = "record.name is required"
        return rtnval
    if record.record_type is None or len(record.record_type) == 0:
        rtnval = "record.record_type is required"
        return rtnval
    if record.record_type not in _VALID_RECORD_TYPES:
        rtnval = "record.record_type must be one of {}".format(
            list(_VALID_RECORD_TYPES),
        )
        return rtnval
    if record.value is None or len(record.value) == 0:
        rtnval = "record.value is required"
        return rtnval
    rtnval = None
    return rtnval


def _render_record_line(*, record: UbuntuDnsRecordSpec) -> str:
    """
        Render a record into a single zone-file line.

        :param record: The :class:`UbuntuDnsRecordSpec`.

        :returns: The rendered line (no trailing newline).
    """
    if record.ttl is not None:
        ttl_token = " {}".format(record.ttl)
    else:
        ttl_token = ""
    if record.priority is not None:
        rdata_text = "{} {}".format(record.priority, record.value)
    else:
        rdata_text = record.value
    rtnval = "{name}{ttl} IN {rtype} {rdata}".format(
        name=record.name,
        ttl=ttl_token,
        rtype=record.record_type,
        rdata=rdata_text,
    )
    return rtnval


def _append_record_line(
    *,
    content: str,
    record: UbuntuDnsRecordSpec,
) -> str:
    """
        Append a rendered record line to ``content``.

        :param content: The current zone-data file body.
        :param record: The :class:`UbuntuDnsRecordSpec`.

        :returns: The updated file body.
    """
    line = _render_record_line(record=record)
    if len(content) == 0:
        rtnval = line + "\n"
        return rtnval
    if not content.endswith("\n"):
        body = content + "\n"
    else:
        body = content
    rtnval = body + line + "\n"
    return rtnval


def _strip_record_line(
    *,
    content: str,
    record: UbuntuDnsRecordSpec,
) -> str:
    """
        Drop every line from ``content`` whose parsed
        (name, record_type, value) tuple matches ``record``.

        :param content: The current zone-data file body.
        :param record: The :class:`UbuntuDnsRecordSpec`.

        :returns: The updated file body.
    """
    new_lines: list = []
    for raw_line in content.splitlines():
        parsed = _parse_record_line(line=raw_line)
        if parsed is None:
            new_lines.append(raw_line)
            continue
        if (
            parsed.name == record.name
            and parsed.record_type == record.record_type
            and _normalise_rdata(value=parsed.value)
            == _normalise_rdata(value=record.value)
        ):
            continue
        new_lines.append(raw_line)
    rtnval = "\n".join(new_lines)
    if len(rtnval) > 0 and not rtnval.endswith("\n"):
        rtnval = rtnval + "\n"
    return rtnval


def _normalise_rdata(*, value: str) -> str:
    """
        Collapse run of whitespace inside an rdata value.

        :param value: The raw rdata text.

        :returns: The normalised text.
    """
    parts = value.split()
    rtnval = " ".join(parts)
    return rtnval


def _record_already_present(
    *,
    records: list,
    record: UbuntuDnsRecordSpec,
) -> bool:
    """
        Probe whether ``records`` already contains ``record``.

        Comparison is over (name, record_type, value) with
        whitespace-collapsed value text.

        :param records: A list of
                        :class:`UbuntuDnsRecordInfo`.
        :param record: The :class:`UbuntuDnsRecordSpec`.

        :returns: True when an identical record is present.
    """
    target_value = _normalise_rdata(value=record.value)
    rtnval = False
    for existing in records:
        if existing.name != record.name:
            continue
        if existing.record_type != record.record_type:
            continue
        if _normalise_rdata(value=existing.value) != target_value:
            continue
        rtnval = True
        return rtnval
    return rtnval


def _parse_zone_records(*, content: str) -> list:
    """
        Parse a zone-data file body into a list of
        :class:`UbuntuDnsRecordInfo`.

        Comment lines, blank lines, ``$TTL`` directives, and
        multi-line SOA continuations are skipped — the SOA
        itself is exposed as a single row carrying the parsed
        serial.

        :param content: The zone-data file body.

        :returns: A list of :class:`UbuntuDnsRecordInfo`.
    """
    rtnval: list = []
    serial_value: Optional[int] = None
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith(";"):
            # Comment line; harvest the SOA serial annotation
            # if present so subsequent record rows carry it.
            continue
        if stripped.startswith("$"):
            continue
        # Approximate the SOA serial as the first integer token
        # on a line that mentions "Serial" — see _bump_soa_serial.
        if "Serial" in raw_line or "serial" in raw_line:
            for token in stripped.split():
                try:
                    serial_value = int(token)
                    break
                except ValueError:
                    continue
            continue
        if "(" in stripped or ")" in stripped:
            # SOA continuation lines.
            continue

        parsed = _parse_record_line(line=raw_line)
        if parsed is None:
            continue
        if parsed.record_type == "SOA":
            parsed.serial = serial_value
        rtnval.append(parsed)
    return rtnval


def _parse_record_line(*, line: str) -> Optional[UbuntuDnsRecordInfo]:
    """
        Parse a single zone-data line into a
        :class:`UbuntuDnsRecordInfo`.

        Permissive: tokens are split on whitespace, the optional
        TTL token is detected as a leading integer, and the
        ``IN`` class token is required.

        :param line: A zone-data line.

        :returns: A populated
                  :class:`UbuntuDnsRecordInfo`, or ``None`` when
                  the line does not parse.
    """
    stripped = line.strip()
    if len(stripped) == 0:
        rtnval: Optional[UbuntuDnsRecordInfo] = None
        return rtnval
    if stripped.startswith(";"):
        rtnval = None
        return rtnval

    tokens = stripped.split()
    if len(tokens) < 4:
        rtnval = None
        return rtnval

    name_token = tokens[0]
    cursor = 1
    ttl_value: Optional[int] = None
    try:
        ttl_value = int(tokens[cursor])
        cursor = cursor + 1
    except ValueError:
        ttl_value = None

    if cursor >= len(tokens):
        rtnval = None
        return rtnval

    class_token = tokens[cursor]
    if class_token.upper() != "IN":
        rtnval = None
        return rtnval
    cursor = cursor + 1

    if cursor >= len(tokens):
        rtnval = None
        return rtnval

    rtype_token = tokens[cursor].upper()
    cursor = cursor + 1
    if rtype_token not in _VALID_RECORD_TYPES:
        rtnval = None
        return rtnval

    rdata_tokens = tokens[cursor:]
    if len(rdata_tokens) == 0:
        rtnval = None
        return rtnval

    priority_value: Optional[int] = None
    if rtype_token in ("MX", "SRV"):
        try:
            priority_value = int(rdata_tokens[0])
            rdata_tokens = rdata_tokens[1:]
        except ValueError:
            priority_value = None

    rdata_text = " ".join(rdata_tokens)

    rtnval = UbuntuDnsRecordInfo(
        name=name_token,
        record_type=rtype_token,
        value=rdata_text,
        ttl=ttl_value,
        priority=priority_value,
        serial=None,
    )
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — named.conf.local / named-checkconf parsing
# ----------------------------------------------------------------------


def _parse_managed_zone_blocks(*, content: str) -> list:
    """
        Parse every sentinel-bracketed managed zone block in
        ``content``.

        :param content: The ``named.conf.local`` body.

        :returns: A list of dicts with keys ``name``,
                  ``zone_type``, and ``zone_file``.
    """
    rtnval: list = []
    open_marker = "# >>> codecraftai-zone:"
    close_marker = "# <<< codecraftai-zone:"

    current_name: Optional[str] = None
    current_lines: list = []
    for raw_line in content.splitlines():
        if raw_line.startswith(open_marker):
            after = raw_line[len(open_marker):]
            tokens = after.split()
            if len(tokens) > 0:
                current_name = tokens[0]
            current_lines = []
            continue
        if raw_line.startswith(close_marker):
            if current_name is not None:
                parsed_type, parsed_file = _parse_zone_block_body(
                    block_lines=current_lines,
                )
                rtnval.append({
                    "name": current_name,
                    "zone_type": parsed_type,
                    "zone_file": parsed_file,
                })
            current_name = None
            current_lines = []
            continue
        if current_name is not None:
            current_lines.append(raw_line)
    return rtnval


def _parse_zone_block_body(*, block_lines: list) -> tuple:
    """
        Parse a managed zone block's body for ``type`` and
        ``file``.

        :param block_lines: The block's content lines.

        :returns: A two-tuple ``(zone_type, zone_file)``.
                  Either field may be ``None`` when not declared.
    """
    zone_type_token = "primary"
    zone_file_token: Optional[str] = None
    for raw in block_lines:
        stripped = raw.strip().rstrip(";")
        if stripped.startswith("type "):
            type_value = stripped[len("type "):].strip()
            if type_value == "master":
                zone_type_token = "primary"
            elif type_value == "slave":
                zone_type_token = "secondary"
            else:
                zone_type_token = type_value
            continue
        if stripped.startswith("file "):
            file_value = stripped[len("file "):].strip()
            zone_file_token = file_value.strip("\"")
            continue
    rtnval = (zone_type_token, zone_file_token)
    return rtnval


def _parse_named_checkconf_p(*, content: str) -> list:
    """
        Parse ``named-checkconf -p`` output for top-level zone
        declarations.

        ``named-checkconf -p`` emits a fully-resolved
        ``named.conf`` document.  This parser walks the file
        looking for ``zone "<name>" {`` headers.

        :param content: The stdout of ``named-checkconf -p``.

        :returns: A list of dicts with keys ``name``,
                  ``zone_type``, ``zone_file``.
    """
    rtnval: list = []
    if content is None or len(content) == 0:
        return rtnval

    inside_zone = False
    current_name: Optional[str] = None
    current_lines: list = []
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not inside_zone:
            if stripped.startswith("zone ") and stripped.endswith("{"):
                middle = stripped[len("zone "):-len("{")].strip()
                # The zone name is in double quotes; drop them.
                name_token = middle.strip().rstrip().split()[0]
                current_name = name_token.strip("\"")
                current_lines = []
                inside_zone = True
                continue
        else:
            if stripped == "};":
                if current_name is not None:
                    parsed_type, parsed_file = _parse_zone_block_body(
                        block_lines=current_lines,
                    )
                    rtnval.append({
                        "name": current_name,
                        "zone_type": parsed_type,
                        "zone_file": parsed_file,
                    })
                inside_zone = False
                current_name = None
                current_lines = []
                continue
            current_lines.append(raw_line)
    return rtnval


def _parse_rndc_zonestatus(*, text: str) -> dict:
    """
        Parse ``rndc zonestatus`` output into a dict.

        ``rndc zonestatus`` emits an indented key-colon-value
        report, e.g. ::

            name: example.com
            type: master
            files: /etc/bind/zones/db.example.com
            serial: 2024042401
            loaded: Wed, 24 Apr 2024 12:00:00 GMT

        :param text: The raw stdout from ``rndc zonestatus``.

        :returns: A dict with keys ``is_loaded``, ``serial``,
                  ``last_loaded``, ``transferred_from``.
    """
    rtnval: dict = {
        "is_loaded": False,
        "serial": None,
        "last_loaded": None,
        "transferred_from": None,
    }
    if text is None or len(text) == 0:
        return rtnval

    # Default to loaded when "name:" is reported.
    saw_name = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if len(stripped) == 0:
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key_clean = key.strip().lower()
        value_clean = value.strip()
        if key_clean == "name":
            saw_name = True
            continue
        if key_clean == "serial":
            try:
                rtnval["serial"] = int(value_clean)
            except ValueError:
                rtnval["serial"] = None
            continue
        if key_clean == "loaded":
            rtnval["last_loaded"] = value_clean
            continue
        if key_clean in ("masters", "primaries"):
            rtnval["transferred_from"] = value_clean
            continue
    if saw_name:
        rtnval["is_loaded"] = True
    return rtnval
