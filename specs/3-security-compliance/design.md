# Design

## Overview

The security compliance feature is implemented as a single module,
`automojo.sysman.linux.ubuntu.securitycompliance`, that exposes a
`UbuntuSecurityComplianceManager` class.  This spec implements the
security-compliance contract defined at
`automojo-sysman-common/specs/contracts/security-compliance/contract.md`
for Ubuntu using OpenSCAP (`oscap`) against the Ubuntu SCAP Security Guide
datastream.

All OpenSCAP command invocation is delegated to an `ICommandRunner` and all
result-XML reads and baseline-JSON writes go through an `IFileOperator`.
No direct filesystem or `subprocess` calls are made inside the module.

Unlike firewall / package / service management where the verb set maps
cleanly, security-compliance is intrinsically **advisory** — the contract
verbs are honored but the `UbuntuComplianceFinding` dataclass is flexible
by design and is populated from OpenSCAP's native XCCDF rule-result format.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator (Local or SSH)       │
│  - Configures aspects (optional)                            │
│  - Injects into UbuntuSecurityComplianceManager             │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│           UbuntuSecurityComplianceManager                   │
│  automojo.sysman.linux.ubuntu.securitycompliance            │
│                                                             │
│  list_profiles()        get_profile()                       │
│  run_compliance_profile()  remediate()                      │
│  remediate_all()                                            │
│  get_baseline()         set_baseline()                      │
└──────────────┬──────────────────────────────┬───────────────┘
               │ runner                       │ file I/O
               ▼                              ▼
┌──────────────────────┐         ┌──────────────────────┐
│   ICommandRunner     │         │   IFileOperator      │
│   run(args, elev)    │         │   read / write       │
└──────────┬───────────┘         └──────────┬───────────┘
           │                                │
   ┌───────┴────────┐             ┌─────────┴────────┐
   ▼                ▼             ▼                  ▼
┌─────────────┐  ┌──────────────┐ ┌──────────────┐ ┌────────────┐
│LocalCommand │  │SSHCommand    │ │LocalFile     │ │SSHFile     │
│Runner       │  │Runner        │ │Operator      │ │Operator    │
└─────────────┘  └──────────────┘ └──────────────┘ └────────────┘
           │                                │
           └────────────┬───────────────────┘
                        │ optional
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect   - RetryAspect   - TimeoutAspect      │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `UbuntuSecurityComplianceManager`

**Location:** `source/packages/automojo/sysman/linux/ubuntu/securitycompliance.py`

| Method | Signature | Description |
|---|---|---|
| `list_profiles` | `() → list[UbuntuComplianceProfile]` | `oscap info` + parse |
| `get_profile` | `(profile_name) → UbuntuComplianceProfile \| None` | Enumerate + filter |
| `run_compliance_profile` | `(profile_name) → UbuntuComplianceReport` | `oscap xccdf eval` + XML parse |
| `remediate` | `(finding_id) → UbuntuSecurityComplianceActionResult` | `oscap xccdf eval --remediate` |
| `remediate_all` | `(report) → list[UbuntuSecurityComplianceActionResult]` | Iterate + delegate |
| `get_baseline` | `() → UbuntuBaselineInfo` | Read baseline JSON |
| `set_baseline` | `(spec) → UbuntuSecurityComplianceActionResult` | Run + remediate + write JSON |

### `UbuntuSecurityComplianceManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no mutations dispatch |
| `elevated` | `bool` | `False` | When `True`, remediations pass `elevated=True` |
| `datastream_path` | `str` | `"/usr/share/xml/scap/ssg/content/ssg-ubuntu2204-ds.xml"` | OpenSCAP datastream |
| `baseline_path` | `str` | `"/var/lib/automojo/compliance/current_baseline.json"` | Baseline JSON |
| `results_dir` | `str` | `"/var/lib/automojo/compliance/results"` | OpenSCAP results directory |

---

## Data Models

### Enums

```python
class UbuntuSecurityComplianceActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

class UbuntuComplianceSeverity(Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"
```

### Dataclasses

```python
@dataclass
class UbuntuComplianceCheck:
    check_id: str
    description: str
    check_type: str          # "oval" | "ocil" | "script"
    severity: UbuntuComplianceSeverity
    parameters: dict

@dataclass
class UbuntuComplianceProfile:
    name: str
    description: str
    checks: list[UbuntuComplianceCheck]

@dataclass
class UbuntuComplianceFinding:
    finding_id: str
    check_id: str
    description: str
    severity: UbuntuComplianceSeverity
    compliant: bool
    actual: str | None
    expected: str | None
    remediable: bool
    error: str | None

@dataclass
class UbuntuComplianceReport:
    profile_name: str
    started_at: datetime
    finished_at: datetime
    findings: list[UbuntuComplianceFinding]
    total: int
    compliant: int
    non_compliant: int
    errored: int

@dataclass
class UbuntuBaselineSpec:
    name: str
    settings: dict[str, str]

@dataclass
class UbuntuBaselineInfo:
    name: str
    settings: dict[str, str]
    source: str | None

@dataclass
class UbuntuSecurityComplianceActionResult:
    status: UbuntuSecurityComplianceActionStatus
    finding_id: str | None
    target: str | None
    message: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None

class UbuntuSecurityComplianceError(Exception):
    """Framework-level failures."""
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `IFileOperator` (from `automojo-interfaces`)

```python
class IFileOperator(Protocol):
    def read_text(self, path: str, elevated: bool = False) -> str: ...
    def write_text(self, path: str, content: str, elevated: bool = False) -> None: ...
    def exists(self, path: str, elevated: bool = False) -> bool: ...
```

---

## Command Mappings

| Operation | Command |
|---|---|
| List profiles | `oscap info <datastream_path>` |
| Run profile | `oscap xccdf eval --profile <profile> --results <results_dir>/<profile>-<ts>.xml --report <results_dir>/<profile>-<ts>.html <datastream_path>` |
| Remediate one rule | `oscap xccdf eval --remediate --profile <profile> --rule <rule_id> <datastream_path>` |

### Read file (via `IFileOperator`)

| Operation | Path |
|---|---|
| Read results XML | `<results_dir>/<profile>-<ts>.xml` |
| Read baseline JSON | `<baseline_path>` |

### Write file (via `IFileOperator`)

| Operation | Path |
|---|---|
| Write baseline JSON | `<baseline_path>` |

---

## Output Parsing

### `oscap info` output parsing

The parser scans stdout for the `Profiles:` line and then extracts each
subsequent `Title: ... Id: ...` pair up to the blank separator line.
Each extracted pair becomes a `UbuntuComplianceProfile(name=<id>,
description=<title>, checks=[])`.  Check enumeration is intentionally
deferred to `run_compliance_profile` — OpenSCAP does not return
rule-level detail from `oscap info`.

### XCCDF results-XML parsing

Using `xml.etree.ElementTree` (stdlib), the parser walks the
`/Benchmark/TestResult` tree and extracts one `<rule-result>` per rule.
For each rule:

- `finding_id` = `<profile_name>:<rule-result idref>`.
- `check_id` = `<rule-result idref>`.
- `description` = `<rule-result>.<check>.<check-content-ref>.<name>` or,
  when available, the rule's `<title>` from the datastream (looked up by
  ID).
- `severity` = map the rule's `severity` attribute (`low|medium|high|critical`)
  to `UbuntuComplianceSeverity`.
- `compliant` = True iff `<result>` is `pass`.
- `remediable` = True iff the rule definition contains a `<fix>` child.
- `error` = the rule's `<message>` text when `<result>` is `error` or
  `unknown`.

Counts (`total`, `compliant`, `non_compliant`, `errored`) are derived
from the findings list after traversal.

---

## Idempotency Guards

| Method | Guard | Source of truth |
|---|---|---|
| `remediate` | Rule already compliant | `oscap xccdf eval --rule <id>` (re-check before remediate) |
| `set_baseline` | Current baseline equals requested spec | `get_baseline()` |

`run_compliance_profile` has no guard (it's a read).  `remediate_all`
skips `compliant=True` findings directly.

---

## Error Handling

| Exception | Raised When |
|---|---|
| `UbuntuSecurityComplianceError` | `list_profiles` or `run_compliance_profile` fails; unknown profile; corrupt baseline JSON |

Per-rule check failures are captured inside the report
(`compliant=False`, `error=...`) rather than raised.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: List-Profiles Command Construction

*For any* call to `list_profiles()`, the manager should invoke `oscap info <datastream_path>` via the runner without elevation.

**Validates: Requirements 1.1**

### Property 2: List-Profiles Parses Titles And Ids

*For any* `oscap info` output, `list_profiles()` should return one entry per `Title: ... Id: ...` pair.

**Validates: Requirements 1.2**

### Property 3: List-Profiles Missing Datastream Raises

*For any* call where the datastream path does not exist, `list_profiles()` should raise `UbuntuSecurityComplianceError` with a `ssg-base`/`ssg-ubuntu` installation hint.

**Validates: Requirements 1.3**

### Property 4: List-Profiles Other Failure Raises

*For any* non-zero `oscap info` exit that is not a missing-file case, `list_profiles()` should raise `UbuntuSecurityComplianceError`.

**Validates: Requirements 1.4**

### Property 5: Get-Profile Returns None For Unknown

*For any* unknown profile name, `get_profile()` should return `None`.

**Validates: Requirements 2.2**

### Property 6: Run-Profile Command Construction

*For any* known profile, `run_compliance_profile()` should invoke `oscap xccdf eval --profile <name> --results ... --report ... <datastream>` with elevation.

**Validates: Requirements 3.1**

### Property 7: Run-Profile Accepts Exit 0 And 2

*For any* `oscap xccdf eval` exit code of 0 or 2, the manager should parse the results XML rather than raise.

**Validates: Requirements 3.2**

### Property 8: Run-Profile Raises On Unexpected Exit

*For any* `oscap xccdf eval` exit code other than 0 or 2, the manager should raise `UbuntuSecurityComplianceError`.

**Validates: Requirements 3.3**

### Property 9: Run-Profile Finding ID Format

*For any* rule result, the produced `finding_id` should equal `<profile>:<rule_id>`.

**Validates: Requirements 3.4**

### Property 10: Run-Profile Severity Mapping

*For any* rule with `severity="low|medium|high|critical"`, the manager should produce a finding with the matching `UbuntuComplianceSeverity`.

**Validates: Requirements 3.4**

### Property 11: Run-Profile Remediable Flag From Fix Presence

*For any* rule containing a `<fix>` block, the finding's `remediable` should be `True`; otherwise `False`.

**Validates: Requirements 3.4**

### Property 12: Run-Profile Per-Rule Errors Captured Not Raised

*For any* rule whose `<result>` is `error` or `unknown`, the finding should be `compliant=False` with the error text captured; the method should NOT raise.

**Validates: Requirements 3.6**

### Property 13: Run-Profile Unknown Profile Raises Pre-Runner

*For any* profile name not found in `list_profiles()`, `run_compliance_profile()` should raise without invoking the `oscap eval` runner call.

**Validates: Requirements 3.5**

### Property 14: Run-Profile UTC Timestamps

*For any* successful report, `started_at` and `finished_at` should be UTC datetimes.

**Validates: Requirements 3.7**

### Property 15: Run-Profile Dry-Run Is A Read

*For any* run with `dry_run=True`, the manager should still execute `oscap xccdf eval`.

**Validates: Requirements 3.8**

### Property 16: Remediate Parses Finding ID

*For any* `finding_id` of the form `<profile>:<rule>`, `remediate()` should dispatch `oscap xccdf eval --remediate --profile <profile> --rule <rule>`.

**Validates: Requirements 4.1**

### Property 17: Remediate Not-Remediable Short-Circuit

*For any* finding whose rule has no `<fix>` block, `remediate()` should return `FAILED` with `message="not_remediable"` without invoking the runner.

**Validates: Requirements 4.2**

### Property 18: Remediate Idempotency

*For any* rule that is already compliant, `remediate()` should return `SKIPPED` with `message="already_compliant"` without invoking the remediate command.

**Validates: Requirements 4.3**

### Property 19: Remediate Dry-Run

*For any* `remediate()` call with `dry_run=True`, the runner's remediate call should not be invoked and the result should be `DRY_RUN`.

**Validates: Requirements 4.6**

### Property 20: Remediate-All Skips Compliant

*For any* finding with `compliant=True`, `remediate_all()` should not include a result for it.

**Validates: Requirements 5.2**

### Property 21: Remediate-All Emits Not-Remediable Failures

*For any* finding with `compliant=False, remediable=False`, `remediate_all()` should include a `FAILED message="not_remediable"` result.

**Validates: Requirements 5.3**

### Property 22: Remediate-All Order Preserved

*For any* call to `remediate_all(report)`, results should appear in the same order as `report.findings`.

**Validates: Requirements 5.5**

### Property 23: Get-Baseline Missing File Returns Empty

*For any* `get_baseline()` call where the baseline file does not exist, the result should be a `UbuntuBaselineInfo` with empty `name`, empty `settings`, and `source=None`.

**Validates: Requirements 6.2**

### Property 24: Get-Baseline Corrupt JSON Raises

*For any* malformed baseline JSON, `get_baseline()` should raise `UbuntuSecurityComplianceError` with `message` starting `"baseline_corrupt:"`.

**Validates: Requirements 6.4**

### Property 25: Set-Baseline Idempotency

*For any* `set_baseline(spec)` where `get_baseline()` already returns an equal spec, the result should be `SKIPPED`.

**Validates: Requirements 7.1**

### Property 26: Set-Baseline Ordering

*For any* `set_baseline(spec)` with non-matching current state, the manager should run `run_compliance_profile(spec.name)`, then `remediate_all(report)`, and finally write the baseline JSON.

**Validates: Requirements 7.2**

### Property 27: Set-Baseline Step-Failure Identification

*For any* `set_baseline(spec)` whose profile-run OR any remediation step fails, the manager should return `FAILED` with `message` identifying the failing step AND MUST NOT write the baseline file.

**Validates: Requirements 7.3**

### Property 28: Set-Baseline Target Field

*For any* successful `set_baseline(spec)`, the result's `target` should equal the baseline file path.

**Validates: Requirements 7.4**

### Property 29: Unsupported Check Type In Report

*For any* rule with `check_type` outside the supported set, the report should include it with `compliant=False, error="unsupported_check_type:<type>", remediable=False`; no raise.

**Validates: Requirements 8.2, 8.3**

### Property 30: Elevation For Remediation And Profile Run

*For any* `run_compliance_profile`, `remediate`, or `set_baseline` invocation when `elevated=True` is configured, the runner should be called with `elevated=True`.

**Validates: Requirements 9.1**

### Property 31: No Elevation For Profile-List And Baseline Reads

*For any* `list_profiles`, `get_profile`, or `get_baseline` call, the runner/file-operator should not be invoked with `elevated=True`.

**Validates: Requirements 9.2**

### Property 32: Dry-Run Short-Circuit For Remediation

*For any* remediation or baseline-write operation with `dry_run=True`, neither `oscap xccdf eval --remediate` nor the baseline JSON write should execute, and the returned status should be `DRY_RUN`.

**Validates: Requirements 4.6, 5.4, 7.5, 10.1, 10.4**

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/
├── __init__.py
└── securitycompliance.py      ← UbuntuSecurityComplianceManager, config,
                                  models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/ubuntu/
    └── test_securitycompliance.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.linux.ubuntu.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.linux.ubuntu.fileops import LocalFileOperator, LocalFileOperatorConfig
from automojo.sysman.linux.ubuntu.securitycompliance import (
    UbuntuSecurityComplianceManager,
    UbuntuSecurityComplianceManagerConfig,
    UbuntuBaselineSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_op = LocalFileOperator(LocalFileOperatorConfig())

config = UbuntuSecurityComplianceManagerConfig(elevated=True, dry_run=False)
sc_mgr = UbuntuSecurityComplianceManager(runner=runner, file_operator=file_op, config=config)

# Enumerate profiles
profiles = sc_mgr.list_profiles()
for p in profiles:
    print(f"{p.name}: {p.description}")

# Run a profile
report = sc_mgr.run_compliance_profile(
    profile_name="xccdf_org.ssgproject.content_profile_cis_level1_server"
)
print(f"{report.compliant}/{report.total} compliant")

# Remediate all
results = sc_mgr.remediate_all(report=report)
for r in results:
    print(r.status, r.finding_id, r.message)
```

### SSH Execution with Aspects

```python
from automojo.sysman.linux.ubuntu.runners import SSHCommandRunner, SSHRunnerConfig
from automojo.sysman.linux.ubuntu.fileops import SSHFileOperator, SSHFileOperatorConfig
from automojo.sysman.linux.ubuntu.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from automojo.sysman.linux.ubuntu.storage import SQLiteStorage
from automojo.sysman.linux.ubuntu.securitycompliance import (
    UbuntuSecurityComplianceManager,
    UbuntuSecurityComplianceManagerConfig,
    UbuntuBaselineSpec,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="ubuntu-node-01", username="admin"))
file_op = SSHFileOperator(SSHFileOperatorConfig(host="ubuntu-node-01", username="admin"))

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2, retryable_exit_codes=[1])))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=1800.0)))

sc_mgr = UbuntuSecurityComplianceManager(
    runner=runner,
    file_operator=file_op,
    config=UbuntuSecurityComplianceManagerConfig(elevated=True),
)

spec = UbuntuBaselineSpec(
    name="xccdf_org.ssgproject.content_profile_cis_level1_server",
    settings={"ssh.PermitRootLogin": "no", "ssh.PasswordAuthentication": "no"},
)
result = sc_mgr.set_baseline(spec=spec)
print(f"set_baseline: {result.status} {result.message}")
```

### Dry-Run Mode

```python
config = UbuntuSecurityComplianceManagerConfig(elevated=True, dry_run=True)
sc_mgr = UbuntuSecurityComplianceManager(runner=runner, file_operator=file_op, config=config)

# Profile run still executes (it's a read).  Remediation is simulated.
report = sc_mgr.run_compliance_profile(
    profile_name="xccdf_org.ssgproject.content_profile_cis_level1_server"
)
results = sc_mgr.remediate_all(report=report)
for r in results:
    assert r.status == UbuntuSecurityComplianceActionStatus.DRY_RUN
```

---

## Testing Strategy

### Unit Testing

- Tests use a `MockCommandRunner` and `MockFileOperator`.
- `oscap info` fixture strings drive `list_profiles` parser tests
  including the empty-datastream, missing-datastream, and multi-profile
  cases.
- XCCDF results-XML fixture strings drive `run_compliance_profile`
  parser tests including `pass`, `fail`, `error`, and `unknown` rule
  outcomes and rules with/without `<fix>` blocks.
- Baseline JSON fixtures drive `get_baseline` / `set_baseline` tests,
  including the corrupt-JSON branch.
- `datastream_path` and `baseline_path` are overridden in tests to
  synthetic locations.
- Dry-run tests assert zero `--remediate` runner calls and zero
  `write_text` calls.
- A dedicated test covers `remediate_all` ordering and the
  not-remediable pass-through.
- A dedicated test covers the unsupported-check-type pass-through.
- No test requires a live OpenSCAP installation.

### Property-Based Testing

The implementation uses `hypothesis` for property-based testing.  Each
correctness property is implemented as a property-based test that:

- Generates random but valid inputs (profile names, rule IDs, severity
  strings, finding lists).
- Executes the operation under test against mocks.
- Verifies the property holds across all generated inputs.
- Runs at least 100 iterations per property test.

Property-based tests are tagged as
`# Feature: security-compliance (ubuntu), Property N: <property description>`.

---

## Scope Mapping

| Contract verb | Ubuntu implementation | Notes |
|---|---|---|
| `list_profiles` | `oscap info <datastream>` | Parses Title/Id pairs. |
| `get_profile` | Enumerate + filter | `None` on miss. |
| `run_compliance_profile` | `oscap xccdf eval` + XML parse | Exit 0 or 2 both parse. |
| `remediate` | `oscap xccdf eval --remediate --rule` | Re-check then dispatch. |
| `remediate_all` | Iterate + delegate | Order preserved. |
| `get_baseline` | JSON read via file operator | Missing → empty. |
| `set_baseline` | Run + remediate_all + write JSON | Fails pre-write on any step failure. |

---

## Extensions beyond the contract

- **OpenSCAP as the engine.**  The contract's check types (`oval`, `ocil`,
  `script`) are honored by dispatching to OpenSCAP's native check handlers.
- **Baseline persistence location.**  Baseline JSON lives at
  `/var/lib/automojo/compliance/current_baseline.json`, managed via the
  `IFileOperator` extension.
- **Results directory.**  Each profile run writes result XML and an HTML
  report under `/var/lib/automojo/compliance/results/` for auditability.

## Out of Scope

- **DISA STIG remediation automation beyond what the datastream provides**
  — the contract allows this flexibility; Ubuntu ships the CIS and
  stig-like profiles via `ssg-ubuntu`.
- **AppArmor profile authoring / enforcement** — the manager can read
  AppArmor state via `apparmor_status` for diagnostic findings in future
  revisions, but profile authoring is intentionally excluded.
- **Signed / evidence-collection report output** — the HTML report is
  emitted by OpenSCAP without signing.
- **Attestation via TPM / measured boot** — out of scope.
- **Continuous-monitoring / agent-mode** — the manager is invoked ad-hoc
  by the coordinator.
- **Remediation rollback** — OpenSCAP itself does not offer rollback.
- **Cross-OS parity with Windows `secedit` findings or macOS
  `profiles` findings** — these are intentionally OS-shaped by the
  contract; this spec does not attempt to reconcile shapes.
- **Feature-gap disclosure**: security-compliance is the single most
  OS-divergent feature.  Each OS's compliance tooling produces findings
  with native shapes.  The `UbuntuComplianceFinding` dataclass is
  deliberately flexible to absorb that; cross-OS callers MUST treat
  findings as advisory and MUST NOT rely on a universal `check_type`
  enumeration beyond what the contract names.
