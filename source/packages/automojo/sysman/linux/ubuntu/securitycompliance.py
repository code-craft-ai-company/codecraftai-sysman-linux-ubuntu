"""
    Ubuntu security-compliance feature.

    Implements the cross-OS security-compliance contract for Ubuntu using
    OpenSCAP (``oscap``) against the Ubuntu SCAP Security Guide
    datastream.

    File I/O policy:
        Every file read / write / probe is performed through the
        injected :class:`ICommandRunner` (``cat``, ``tee``, ``rm``,
        ``test -e``, ``mkdir -p``).  This module does NOT consume an
        :class:`IFileOperator`.  The drafted spec names ``IFileOperator``
        as a collaborator; that wording is a forward-looking design
        note and the current implementation deviates from it pending
        introduction of a concrete file-operator abstraction in this
        codebase.

    Elevation policy:
        ``run_compliance_profile``, ``remediate``, and ``set_baseline``
        honour ``config.elevated``.  ``list_profiles`` and
        ``get_baseline`` never elevate.

    Surface choices:
        * ``run_compliance_profile`` invokes ``oscap xccdf eval`` and
          parses the verbose stdout into a tolerant
          :class:`UbuntuComplianceReport`.  Reading the structured XML
          results file is left as a documented follow-up.
        * ``remediate`` invokes ``oscap xccdf eval --remediate`` against
          either a single ``--rule`` or the entire profile.
        * ``get_baseline`` reads
          ``<output_dir>/current_baseline.json`` via ``cat`` and decodes
          the JSON into a :class:`UbuntuBaselineInfo`.
        * ``set_baseline`` writes that file via ``tee`` over stdin
          when its existing JSON does not match the requested spec.
"""

import json
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


_OSCAP = "oscap"

_DEFAULT_DATASTREAM_PATH = (
    "/usr/share/xml/scap/ssg/content/ssg-ubuntu2204-ds.xml"
)
_DEFAULT_OUTPUT_DIR = "/var/lib/automojo/compliance"
_BASELINE_FILENAME = "current_baseline.json"

_RULE_PREFIX = "Rule"
_TITLE_PREFIX = "Title"
_RESULT_PREFIX = "Result"
_IDENT_PREFIX = "Ident"

_VALID_RESULT_STATES = frozenset({
    "pass", "fail", "notapplicable", "error", "unknown", "informational",
    "notchecked", "notselected", "fixed",
})


UbuntuSecurityComplianceActionStatus = ActionStatus


@dataclass
class UbuntuSecurityComplianceManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuSecurityComplianceManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param datastream_path: Absolute path to the OpenSCAP
                                datastream XML.  Defaults to the
                                ssg-ubuntu2204 datastream shipped by
                                the ``ssg-ubuntu`` package.
        :param output_dir: Directory holding the persisted baseline
                           JSON and any results / report files OpenSCAP
                           writes during a profile run.
    """
    datastream_path: str = _DEFAULT_DATASTREAM_PATH
    output_dir: str = _DEFAULT_OUTPUT_DIR


@dataclass
class UbuntuSecurityComplianceActionResult(FeatureActionResult):
    """
        Result of a security-compliance write operation.

        :param profile: The OpenSCAP profile id targeted, when
                        applicable.
        :param finding_id: The rule / finding id targeted, when
                           applicable.
        :param message: Optional human-readable detail for ``SKIPPED`` /
                        ``FAILED`` outcomes.
        :param target: Optional secondary identifier (typically the
                       baseline file path for ``set_baseline``).
    """
    profile: Optional[str] = None
    finding_id: Optional[str] = None
    message: Optional[str] = None
    target: Optional[str] = None


@dataclass
class UbuntuComplianceProfile:
    """
        One profile listed by ``oscap info``.

        :param id: Canonical profile id (e.g.
                   ``xccdf_org.ssgproject.content_profile_cis_level1_server``).
        :param title: Human-readable title.
        :param description: Optional long-form description; ``None``
                            when ``oscap info`` did not surface one.
    """
    id: str
    title: str
    description: Optional[str] = None


@dataclass
class UbuntuComplianceFinding:
    """
        Single finding parsed from ``oscap xccdf eval`` output.

        :param rule_id: The rule identifier as reported by OpenSCAP.
        :param severity: The rule severity (``low``/``medium``/``high``/
                         ``critical``); empty string when not parsed.
        :param title: Human-readable rule title; may be empty.
        :param description: Optional long-form description; ``None``
                            when not parsed.
        :param status: Result of the rule evaluation.  One of the
                       known XCCDF result states (``pass``, ``fail``,
                       ``notapplicable``, ``error``, ``unknown``,
                       ``informational``, ``notchecked``,
                       ``notselected``, ``fixed``).
        :param remediation_available: True iff the rule is known to
                                      ship a fix block; the tolerant
                                      stdout parser sets this False
                                      and reserves the True value for
                                      a future XML reader.
        :param remediation_automated: True iff a known automated fix
                                      exists; same caveat as
                                      ``remediation_available``.
    """
    rule_id: str
    severity: str
    title: str
    description: Optional[str]
    status: str
    remediation_available: bool
    remediation_automated: bool


@dataclass
class UbuntuComplianceReport:
    """
        Aggregated outcome of a profile run.

        :param profile: The profile id that was evaluated.
        :param generated_at: ISO-8601 UTC timestamp captured at the
                             moment the report was assembled.
        :param findings: One :class:`UbuntuComplianceFinding` per rule
                         observed in the OpenSCAP output.
        :param counts: Mapping of result-state name to frequency.
        :param results_path: Absolute path of the XCCDF results XML
                             file written by ``oscap xccdf eval``.
        :param report_path: Absolute path of the HTML report file
                            written by ``oscap xccdf eval``.
    """
    profile: str
    generated_at: str
    findings: list[UbuntuComplianceFinding]
    counts: dict
    results_path: str
    report_path: str


@dataclass
class UbuntuBaselineInfo:
    """
        Currently-persisted compliance baseline.

        :param profile: The profile id captured by the baseline.
        :param applied_at: ISO-8601 timestamp at which the baseline
                           was last applied.
        :param datastream_path: The datastream the baseline was
                                evaluated against.
    """
    profile: str
    applied_at: str
    datastream_path: str


class UbuntuSecurityComplianceQueryError(Exception):
    """
        Raised when a security-compliance read fails for a reason
        other than a target simply being absent.
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

        err_msg_lines = ["Security compliance query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuSecurityComplianceManager(BaseFeatureManager):
    """
        Ubuntu OpenSCAP-backed security-compliance manager.  Every
        verb is dispatched through the injected
        :class:`ICommandRunner`; baseline JSON I/O is performed via
        ``cat`` / ``tee`` rather than a separate file operator.
    """

    result_class = UbuntuSecurityComplianceActionResult
    status_class = UbuntuSecurityComplianceActionStatus
    feature_name = "security-compliance"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_profiles(self) -> list[UbuntuComplianceProfile]:
        """
            Enumerate the profiles defined in the configured
            datastream via ``oscap info <datastream>``.  Reads never
            elevate.

            :returns: One :class:`UbuntuComplianceProfile` per parsed
                      profile.
            :raises UbuntuSecurityComplianceQueryError: When ``oscap``
                exits non-zero with a non-empty stderr.
        """
        command = [_OSCAP, "info", self._config.datastream_path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise UbuntuSecurityComplianceQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_oscap_info(text=run_result.stdout)
        return rtnval

    def get_baseline(self) -> Optional[UbuntuBaselineInfo]:
        """
            Read the persisted baseline JSON via ``cat``.

            :returns: The decoded :class:`UbuntuBaselineInfo`, or
                      ``None`` when the baseline file does not exist
                      or contains an unparseable payload.
        """
        path = self._baseline_path()
        body = self._read_file(path=path)
        if body is None:
            rtnval: Optional[UbuntuBaselineInfo] = None
            return rtnval

        try:
            decoded = json.loads(body)
        except (ValueError, TypeError):
            rtnval = None
            return rtnval

        if not isinstance(decoded, dict):
            rtnval = None
            return rtnval

        profile_value = decoded.get("profile", "")
        applied_value = decoded.get("applied_at", "")
        datastream_value = decoded.get(
            "datastream_path", self._config.datastream_path,
        )

        if not isinstance(profile_value, str):
            profile_value = str(profile_value)
        if not isinstance(applied_value, str):
            applied_value = str(applied_value)
        if not isinstance(datastream_value, str):
            datastream_value = str(datastream_value)

        rtnval = UbuntuBaselineInfo(
            profile=profile_value,
            applied_at=applied_value,
            datastream_path=datastream_value,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def run_compliance_profile(
        self,
        profile_name: str,
    ) -> UbuntuComplianceReport:
        """
            Run an OpenSCAP profile and assemble a tolerant report.

            Dispatches ``oscap xccdf eval --profile <profile_name>
            --results <results_xml> --report <report_html>
            <datastream>``.  Exit codes 0 and 2 are both treated as
            successful evaluations (2 indicates that one or more rules
            failed but the run completed).  Other non-zero exits
            raise :class:`UbuntuSecurityComplianceQueryError`.

            :param profile_name: The profile id to evaluate.

            :returns: The :class:`UbuntuComplianceReport`.
            :raises UbuntuSecurityComplianceQueryError: When OpenSCAP
                exits with an unrecoverable failure.
        """
        timestamp_token = self._timestamp_token()
        results_path = "{}/results-{}.xml".format(
            self._config.output_dir, timestamp_token,
        )
        report_path = "{}/report-{}.html".format(
            self._config.output_dir, timestamp_token,
        )

        command = [
            _OSCAP, "xccdf", "eval",
            "--profile", profile_name,
            "--results", results_path,
            "--report", report_path,
            self._config.datastream_path,
        ]

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code != 0 and run_result.exit_code != 2:
            raise UbuntuSecurityComplianceQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        findings = _parse_oscap_eval(text=run_result.stdout)
        counts: dict = {}
        for finding in findings:
            current = counts.get(finding.status, 0)
            counts[finding.status] = current + 1

        rtnval = UbuntuComplianceReport(
            profile=profile_name,
            generated_at=_iso_utc_now(),
            findings=findings,
            counts=counts,
            results_path=results_path,
            report_path=report_path,
        )
        return rtnval

    def remediate(
        self,
        finding_id: str,
    ) -> UbuntuSecurityComplianceActionResult:
        """
            Remediate a single rule via ``oscap xccdf eval
            --remediate``.

            The ``finding_id`` is interpreted as ``<profile>:<rule>``;
            when the colon delimiter is absent the entire string is
            treated as a rule id and the profile defaults to the
            datastream's selected profile (an OpenSCAP requirement).
            For consistency with the contract, a missing profile
            segment results in a ``FAILED`` outcome.

            :param finding_id: The finding identifier to remediate.

            :returns: The :class:`UbuntuSecurityComplianceActionResult`.
        """
        if ":" in finding_id:
            profile_part, _, rule_part = finding_id.partition(":")
        else:
            profile_part = ""
            rule_part = finding_id

        if len(profile_part) == 0:
            failed = UbuntuSecurityComplianceActionResult(
                status=UbuntuSecurityComplianceActionStatus.FAILED,
                detail=finding_id,
                command=None,
                profile=None,
                finding_id=finding_id,
                message="missing_profile_in_finding_id",
                stderr="missing_profile_in_finding_id",
            )
            return failed

        command = [
            _OSCAP, "xccdf", "eval",
            "--remediate",
            "--profile", profile_part,
            "--rule", rule_part,
            self._config.datastream_path,
        ]

        if self._config.dry_run:
            dry = UbuntuSecurityComplianceActionResult(
                status=UbuntuSecurityComplianceActionStatus.DRY_RUN,
                detail=finding_id,
                command=list(command),
                profile=profile_part,
                finding_id=finding_id,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0 or run_result.exit_code == 2:
            mapped_status = UbuntuSecurityComplianceActionStatus.SUCCESS
        else:
            mapped_status = UbuntuSecurityComplianceActionStatus.FAILED

        rtnval = UbuntuSecurityComplianceActionResult(
            status=mapped_status,
            detail=finding_id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            profile=profile_part,
            finding_id=finding_id,
        )
        return rtnval

    def set_baseline(
        self,
        spec: UbuntuBaselineInfo,
    ) -> UbuntuSecurityComplianceActionResult:
        """
            Persist ``spec`` as the current baseline JSON.

            Skipped when the existing on-disk JSON decodes to a
            byte-identical payload.

            :param spec: The :class:`UbuntuBaselineInfo` to persist.

            :returns: The :class:`UbuntuSecurityComplianceActionResult`.
        """
        path = self._baseline_path()
        desired = _render_baseline(spec=spec)

        write_command = ["tee", path]

        if self._config.dry_run:
            dry = UbuntuSecurityComplianceActionResult(
                status=UbuntuSecurityComplianceActionStatus.DRY_RUN,
                detail=spec.profile,
                command=list(write_command),
                profile=spec.profile,
                finding_id=None,
                target=path,
            )
            return dry

        existing = self._read_file(path=path)
        if existing is not None and existing == desired:
            skipped = UbuntuSecurityComplianceActionResult(
                status=UbuntuSecurityComplianceActionStatus.SKIPPED,
                detail=spec.profile,
                command=list(write_command),
                profile=spec.profile,
                finding_id=None,
                target=path,
                message="baseline already matches desired content",
            )
            return skipped

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=desired,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuSecurityComplianceActionStatus.SUCCESS
        else:
            mapped_status = UbuntuSecurityComplianceActionStatus.FAILED

        rtnval = UbuntuSecurityComplianceActionResult(
            status=mapped_status,
            detail=spec.profile,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            profile=spec.profile,
            finding_id=None,
            target=path,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _baseline_path(self) -> str:
        """
            Build the absolute path to the persisted baseline JSON.

            :returns: ``<output_dir>/current_baseline.json``.
        """
        rtnval = "{}/{}".format(
            self._config.output_dir, _BASELINE_FILENAME,
        )
        return rtnval

    def _timestamp_token(self) -> str:
        """
            Build a filesystem-safe UTC timestamp token used for
            results and report filenames.

            :returns: ``YYYYMMDDTHHMMSSZ``.
        """
        rtnval = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
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


# ----------------------------------------------------------------------
# Module-level renderers and parsers
# ----------------------------------------------------------------------


def _iso_utc_now() -> str:
    """
        Build an ISO-8601 UTC timestamp at second precision.

        :returns: ``YYYY-MM-DDTHH:MM:SSZ``.
    """
    rtnval = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return rtnval


def _render_baseline(*, spec: UbuntuBaselineInfo) -> str:
    """
        Render a :class:`UbuntuBaselineInfo` into the canonical
        baseline JSON body.

        Output is deterministic (sorted keys, two-space indent,
        trailing newline) so that
        :meth:`UbuntuSecurityComplianceManager.set_baseline` can
        compare against existing on-disk content for idempotency.

        :param spec: The :class:`UbuntuBaselineInfo` to render.

        :returns: The full file body as a string ending in ``\\n``.
    """
    payload = {
        "profile": spec.profile,
        "applied_at": spec.applied_at,
        "datastream_path": spec.datastream_path,
    }
    body = json.dumps(payload, sort_keys=True, indent=2)
    rtnval = body + "\n"
    return rtnval


def _parse_oscap_info(*, text: str) -> list[UbuntuComplianceProfile]:
    """
        Parse the stdout of ``oscap info <datastream>``.

        ``oscap info`` emits a hierarchical tree.  The relevant section
        looks like::

            Profiles:
              Title: CIS Ubuntu 22.04 Level 1 Server Benchmark
                Id: xccdf_org.ssgproject.content_profile_cis_level1_server
              Title: CIS Ubuntu 22.04 Level 2 Server Benchmark
                Id: xccdf_org.ssgproject.content_profile_cis_level2_server

        This parser captures every (Title, Id) pair under the
        ``Profiles:`` section and emits one
        :class:`UbuntuComplianceProfile` per pair.

        :param text: Raw stdout text.

        :returns: One :class:`UbuntuComplianceProfile` per parsed
                  profile.
    """
    rtnval: list[UbuntuComplianceProfile] = []

    in_profiles_section = False
    pending_title: Optional[str] = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if len(line) == 0:
            continue
        stripped = line.strip()

        if stripped.startswith("Profiles:"):
            in_profiles_section = True
            pending_title = None
            continue

        # Sections are separated by a non-indented header line; once we
        # leave Profiles we stop appending.
        if not line.startswith(" ") and not line.startswith("\t"):
            if not stripped.startswith("Profiles:"):
                in_profiles_section = False
                pending_title = None
            continue

        if not in_profiles_section:
            continue

        if stripped.startswith("Title:"):
            _, _, value = stripped.partition(":")
            pending_title = value.strip()
        elif stripped.startswith("Id:") and pending_title is not None:
            _, _, value = stripped.partition(":")
            profile_id = value.strip()
            profile = UbuntuComplianceProfile(
                id=profile_id,
                title=pending_title,
                description=None,
            )
            rtnval.append(profile)
            pending_title = None

    return rtnval


def _parse_oscap_eval(*, text: str) -> list[UbuntuComplianceFinding]:
    """
        Tolerantly parse ``oscap xccdf eval`` verbose stdout into a
        list of :class:`UbuntuComplianceFinding`.

        OpenSCAP's verbose stdout looks roughly like::

            Title   Ensure SSH PermitRootLogin is set to no
            Rule    xccdf_org.ssgproject.content_rule_sshd_disable_root_login
            Ident   CCE-12345-6
            Result  pass

            Title   Ensure auditd is installed
            Rule    xccdf_org.ssgproject.content_rule_package_audit_installed
            Result  fail

        This parser groups consecutive ``Title``/``Rule``/``Ident``/
        ``Result`` pairs and emits one finding per ``Rule`` it
        encounters.  When richer field information is required, the
        XCCDF results XML written by ``oscap xccdf eval`` should be
        parsed instead — that is left as a documented follow-up.

        :param text: Raw stdout text.

        :returns: One :class:`UbuntuComplianceFinding` per parsed
                  rule.
    """
    rtnval: list[UbuntuComplianceFinding] = []

    pending_title = ""
    pending_rule: Optional[str] = None
    pending_severity = ""

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if len(line) == 0:
            continue

        # ``oscap`` emits two-column rows separated by two-or-more
        # spaces or a tab.  We accept both shapes.
        head, _, value_section = line.partition("\t")
        if len(value_section) == 0:
            tokens = line.split(None, 1)
            if len(tokens) == 2:
                head = tokens[0]
                value_section = tokens[1]
            else:
                head = tokens[0] if len(tokens) > 0 else ""
                value_section = ""

        head_token = head.strip()
        value_token = value_section.strip()

        if head_token == _TITLE_PREFIX:
            pending_title = value_token
        elif head_token == _RULE_PREFIX:
            pending_rule = value_token
        elif head_token == _IDENT_PREFIX:
            # CCE / CVE references are surfaced into severity when the
            # token shape suggests a severity (low/medium/high).  For
            # the pilot we leave severity empty unless explicitly
            # parsed as such.
            lowered = value_token.lower()
            if lowered in {"low", "medium", "high", "critical"}:
                pending_severity = lowered
        elif head_token == _RESULT_PREFIX:
            normalised = value_token.lower()
            if normalised not in _VALID_RESULT_STATES:
                normalised = "unknown"

            if pending_rule is None:
                rule_id = ""
            else:
                rule_id = pending_rule

            finding = UbuntuComplianceFinding(
                rule_id=rule_id,
                severity=pending_severity,
                title=pending_title,
                description=None,
                status=normalised,
                remediation_available=False,
                remediation_automated=False,
            )
            rtnval.append(finding)

            pending_title = ""
            pending_rule = None
            pending_severity = ""

    return rtnval
