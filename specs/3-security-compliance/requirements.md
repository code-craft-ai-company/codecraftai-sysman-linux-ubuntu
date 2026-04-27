# Requirements

## Introduction

The security compliance feature provides the `automojo-sysman-linux-ubuntu`
package with the ability to run compliance profiles, iterate their findings,
and remediate individual findings on Ubuntu systems.  This spec implements
the security-compliance contract defined at
`automojo-sysman-common/specs/contracts/security-compliance/contract.md`
for Ubuntu using OpenSCAP (`oscap`) with the Ubuntu SCAP Security Guide
datastream, with optional AppArmor profile inspection as a secondary source.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The feature
does not decide *which* profile to apply — it provides the capability to
*evaluate and remediate* reliably, idempotently, and with full auditability.
All system commands are executed exclusively through the
`automojo-interfaces` command-runner abstraction, and all baseline-metadata
mutations are executed through the `automojo-interfaces` file-operator
abstraction.

Unlike firewall / package / service management where the verb set maps
cleanly, security-compliance is intrinsically **advisory** — the verb set
is honored across OSes but each OS's compliance tooling produces findings
with OS-native shapes.  This Ubuntu spec adopts OpenSCAP as the primary
engine and populates the contract's flexible `UbuntuComplianceFinding`
dataclass from OpenSCAP rule results.

---

## Requirements

### Requirement 1: List Available Profiles

**User Story:** As a maintenance coordinator, I want to enumerate every
OpenSCAP compliance profile available on the system, so that I can choose
one to evaluate.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_profiles()` THE SYSTEM SHALL invoke
   `oscap info <datastream-path>` via the command runner where
   `<datastream-path>` defaults to
   `/usr/share/xml/scap/ssg/content/ssg-ubuntu2204-ds.xml`.
2. WHEN the command succeeds THE SYSTEM SHALL parse the "Profiles:"
   section of stdout and SHALL return a
   `list[UbuntuComplianceProfile]` with one entry per profile (fields:
   `name`=profile ID, `description`=profile title, `checks=[]`).
3. WHEN the datastream path does not exist on disk THE SYSTEM SHALL raise
   a `UbuntuSecurityComplianceError` with a `message` recommending
   installation of the `ssg-base` or `ssg-ubuntu` package.
4. WHEN `oscap info` exits non-zero for any other reason THE SYSTEM SHALL
   raise a `UbuntuSecurityComplianceError` with the exit code and
   stderr.
5. WHEN the coordinator has configured a non-default datastream path via
   `datastream_path` in the config THE SYSTEM SHALL use that path
   instead of the default.

---

### Requirement 2: Get a Single Profile

**User Story:** As a maintenance coordinator, I want to resolve a profile
by name, so that I can inspect its description and check metadata.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_profile(profile_name)` THE SYSTEM
   SHALL invoke `list_profiles()` internally and return the matching
   entry.
2. WHEN no profile matches THE SYSTEM SHALL return `None`.
3. WHEN `list_profiles()` raises THE SYSTEM SHALL propagate the
   `UbuntuSecurityComplianceError`.

---

### Requirement 3: Run a Compliance Profile

**User Story:** As a maintenance coordinator, I want to evaluate a profile
end-to-end and receive a structured report, so that I can act on each
finding.

#### Acceptance Criteria

1. WHEN the coordinator calls `run_compliance_profile(profile_name)` THE
   SYSTEM SHALL invoke `oscap xccdf eval --profile <profile_name>
   --results <results-xml-path> --report <report-html-path>
   <datastream-path>` via the command runner with elevation.
2. WHEN the command exits 0 (fully compliant) or 2 (some non-compliance)
   THE SYSTEM SHALL read the `<results-xml-path>` via the
   `IFileOperator` and parse it into a `UbuntuComplianceReport`.
3. WHEN the command exits 1 or any other unexpected code THE SYSTEM
   SHALL raise a `UbuntuSecurityComplianceError`.
4. WHEN the report is parsed THE SYSTEM SHALL populate each
   `UbuntuComplianceFinding` with fields: `finding_id`=`<profile>:<rule-id>`,
   `check_id`=`<rule-id>`, `description`=rule title, `severity` mapped
   from the OpenSCAP severity attribute, `compliant`=True for `pass`
   outcomes, `remediable` from the `<fix>` presence in the rule
   definition, `error`=None except for `error`/`unknown` outcomes where
   it contains the OpenSCAP message.
5. WHEN the profile does not exist in the datastream THE SYSTEM SHALL
   raise a `UbuntuSecurityComplianceError` with
   `message="unknown_profile: <name>"` before invoking the runner.
6. WHEN individual check evaluations fail inside OpenSCAP THE SYSTEM
   SHALL capture them in the report's findings with `compliant=False`
   and `error=<opensCAP error text>` — such failures MUST NOT raise.
7. WHEN `started_at` and `finished_at` fields are set THE SYSTEM SHALL
   use UTC datetimes corresponding to the runner's start and end
   timestamps.
8. WHEN dry-run mode is active THE SYSTEM SHALL still execute the
   profile run — compliance evaluation is a read and is not
   short-circuited by dry-run.

---

### Requirement 4: Remediate a Finding

**User Story:** As a maintenance coordinator, I want to remediate a
specific finding identified in a report, so that the system comes into
compliance.

#### Acceptance Criteria

1. WHEN the coordinator calls `remediate(finding_id)` THE SYSTEM SHALL
   split the finding ID on `:` into `<profile_name>:<rule_id>` and
   invoke `oscap xccdf eval --remediate --profile <profile_name>
   --rule <rule_id> <datastream-path>` via the command runner with
   elevation.
2. WHEN the finding is not remediable (no `<fix>` block in the rule) THE
   SYSTEM SHALL return a `UbuntuSecurityComplianceActionResult` with
   status `FAILED` and `message="not_remediable"` without invoking the
   runner.
3. WHEN the system is ALREADY compliant for the rule THE SYSTEM SHALL
   return a `UbuntuSecurityComplianceActionResult` with status
   `SKIPPED` and `message="already_compliant"` — idempotency is
   evaluated by re-running the single-rule check before issuing the
   remediate command.
4. WHEN the command succeeds THE SYSTEM SHALL return a
   `UbuntuSecurityComplianceActionResult` with status `SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a
   `UbuntuSecurityComplianceActionResult` with status `FAILED`, the
   exit code, captured stderr, and the `command`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute the
   remediation command and SHALL return a
   `UbuntuSecurityComplianceActionResult` with status `DRY_RUN`.

---

### Requirement 5: Batch Remediate

**User Story:** As a maintenance coordinator, I want to apply every
remediable finding in a report in one call, so that full-system
remediation is a single step.

#### Acceptance Criteria

1. WHEN the coordinator calls `remediate_all(report)` THE SYSTEM SHALL
   iterate `report.findings` in list order and SHALL invoke
   `remediate(finding_id)` for every `finding` whose `remediable=True`
   AND `compliant=False`.
2. WHEN a finding has `compliant=True` THE SYSTEM SHALL skip it
   silently (no result returned).
3. WHEN a finding has `remediable=False` AND `compliant=False` THE
   SYSTEM SHALL include in the returned list a
   `UbuntuSecurityComplianceActionResult` with status `FAILED` and
   `message="not_remediable"`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return a `DRY_RUN`
   result for every attempted remediation.
5. THE SYSTEM SHALL return one result per attempted remediation in the
   order findings were processed.

---

### Requirement 6: Get Baseline

**User Story:** As a maintenance coordinator, I want to read the most
recently applied baseline so that audits can report on active policy.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_baseline()` THE SYSTEM SHALL invoke
   `IFileOperator.read_text('/var/lib/automojo/compliance/current_baseline.json')`.
2. WHEN the file does not exist THE SYSTEM SHALL return a
   `UbuntuBaselineInfo` with `name=""`, `settings={}`, and
   `source=None`.
3. WHEN the file exists THE SYSTEM SHALL parse JSON with keys `name`
   and `settings` and SHALL return a `UbuntuBaselineInfo` populated
   from those fields with `source=<file path>`.
4. WHEN the JSON is malformed THE SYSTEM SHALL raise a
   `UbuntuSecurityComplianceError` with
   `message="baseline_corrupt: <reason>"`.

---

### Requirement 7: Set Baseline

**User Story:** As a maintenance coordinator, I want to persist the active
baseline and apply it, so that a known policy is enforced and recorded.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_baseline(spec)` AND `get_baseline()`
   already returns a baseline equal to `spec` (same `name` AND same
   `settings` dict) THE SYSTEM SHALL return a
   `UbuntuSecurityComplianceActionResult` with status `SKIPPED`.
2. WHEN the baselines differ THE SYSTEM SHALL first run
   `run_compliance_profile(spec.name)` to obtain the current report,
   then invoke `remediate_all(report)`, and finally write the updated
   baseline JSON to
   `/var/lib/automojo/compliance/current_baseline.json` via the
   `IFileOperator`.
3. WHEN the profile run OR any individual remediation fails THE SYSTEM
   SHALL return a `UbuntuSecurityComplianceActionResult` with status
   `FAILED` and a `message` identifying the failing step, AND SHALL
   NOT write the baseline file.
4. WHEN the baseline file write succeeds THE SYSTEM SHALL return a
   `UbuntuSecurityComplianceActionResult` with status `SUCCESS` and
   `target=/var/lib/automojo/compliance/current_baseline.json`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT invoke any
   mutation and SHALL return a
   `UbuntuSecurityComplianceActionResult` with status `DRY_RUN`.

---

### Requirement 8: Check-Type Support

**User Story:** As a maintenance coordinator, I want to know which OpenSCAP
check types the manager understands, so that I can author profiles
confidently.

#### Acceptance Criteria

1. THE SYSTEM SHALL support the `check_type` discriminators: `oval`,
   `ocil`, and `script`.
2. WHEN `run_compliance_profile` encounters a rule with a `check_type`
   outside the supported set THE SYSTEM SHALL include the rule in the
   report with `compliant=False`, `error="unsupported_check_type:
   <type>"`, and `remediable=False`.
3. THE SYSTEM SHALL NOT raise for unsupported check types — they are
   captured in the report.

---

### Requirement 9: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all compliance
evaluations and remediations to run with the privileges required by
OpenSCAP, so that protected state is inspectable and writable.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL
   pass `elevated=True` to the command runner for
   `run_compliance_profile`, `remediate`, and `set_baseline` — audit
   reads elevate by default per the contract.
2. WHEN invoking `list_profiles` / `get_profile` / `get_baseline` THE
   SYSTEM SHALL NOT pass `elevated=True` — those reads target
   world-readable artifacts (datastream XML, baseline JSON under
   `/var/lib/automojo/compliance/`).
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass
   `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL
   default to no elevation and allow the runner to surface the
   resulting permission error as a
   `UbuntuSecurityComplianceActionResult` with `status=FAILED`.

---

### Requirement 10: Dry-Run Safety

**User Story:** As a maintenance coordinator, I want a dry-run mode that
simulates remediation without mutating the system, so that change reviews
are possible before roll-out.

#### Acceptance Criteria

1. WHEN the system is configured with `dry_run=True` THE SYSTEM SHALL
   NOT invoke `oscap xccdf eval --remediate` or any baseline JSON
   write.
2. WHEN dry-run is active THE SYSTEM SHALL populate the `command`
   field of every returned result with the argv that would have been
   executed.
3. WHEN dry-run is active THE SYSTEM SHALL still execute
   `run_compliance_profile` (reads) in order to evaluate
   idempotency and batch ordering.
4. WHEN dry-run is active THE SYSTEM SHALL return status `DRY_RUN` on
   every `UbuntuSecurityComplianceActionResult` returned from
   `remediate`, `remediate_all`, or `set_baseline`.
