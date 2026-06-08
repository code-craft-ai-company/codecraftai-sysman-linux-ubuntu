# Design

## Overview

The maintenance-reporting feature is implemented as a single module,
`ccai.sysman.linux.ubuntu.maintenancereporting`, that exposes a
`UbuntuMaintenanceReport` class.  This spec implements the maintenance-reporting
contract defined at
`codecraftai-sysman-common/specs/contracts/maintenance-reporting/contract.md` for
Ubuntu using pure Python accumulation of action records and a single I/O egress point
— `emit_report(path, format)` — that delegates to `IFileOperator`.

The module contains no direct I/O beyond the file-operator abstraction used by
`emit_report`.  Timestamps are obtained via a `_clock` callable (defaulting to
`datetime.utcnow`) that can be replaced in tests.  Serialisation supports four
formats: JSON (standard library `json`), plain text (string formatting), YAML (via
`PyYAML`), and Markdown (string formatting).  When `PyYAML` is not importable the
reporter surfaces a typed failure on `emit_report(path, YAML)` rather than raising.

Ubuntu's default session output base is `/var/log/ccai/maintenance/<session_id>/`,
which requires the file operator to run with `elevated=True` when the calling user
is not root.  The reporter never embeds elevation policy; the caller configures the
file operator with whatever policy fits the run.

The feature works identically whether file operations execute locally (via
`UbuntuLocalFileOperator`) or remotely over SSH (via `UbuntuSSHFileOperator`).  The
reporter has no knowledge of transport — it only speaks `IFileOperator`.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md) for
details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates file operator (Local or SSH)                │
│  - Configures aspects (optional)                            │
│  - Injects file operator into UbuntuMaintenanceReport       │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│          UbuntuMaintenanceReport                            │
│  ccai.sysman.linux.ubuntu.maintenancereporting          │
│                                                             │
│  begin_session()    add_action()                            │
│  record_query()     record_error()                          │
│  end_session()      summary_counts()                        │
│  actions_by_status()  actions_by_feature()                  │
│  to_json()   to_text()   emit_report()                      │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates file I/O to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   IFileOperator                             │
│  write_text(path, content, elevated) → FileResult           │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────────┐  ┌──────────────────────────┐
│ UbuntuLocalFileOperator  │  │ UbuntuSSHFileOperator    │
│ - pathlib operations     │  │ - SFTP transfer          │
│ - local file I/O         │  │ - reliability metrics    │
│ - sudo tee for elevation │  │ - connection retry       │
└──────────────────────────┘  └──────────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect: audit logging                         │
│  - RetryAspect: transient failure retry                     │
│  - TimeoutAspect: global timeout policy                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `UbuntuMaintenanceReport`

**Location:** `source/packages/ccai/sysman/linux/ubuntu/maintenancereporting.py`

| Method | Signature | Description |
|---|---|---|
| `begin_session` | `(run_id, host, description) → None` | Initialise a fresh session |
| `add_action` | `(record) → None` | Append an action record |
| `record_query` | `(record) → None` | Alias for `add_action` for read-operation call sites |
| `record_error` | `(feature, operation, error) → None` | Shorthand for a FAILED record |
| `end_session` | `() → None` | Stamp `finished_at` and recompute `overall_status` |
| `summary_counts` | `() → UbuntuReportSummary` | Count actions by status |
| `actions_by_status` | `(*statuses) → list[UbuntuActionRecord]` | Filter by status |
| `actions_by_feature` | `(feature) → list[UbuntuActionRecord]` | Filter by feature |
| `to_json` | `(indent) → str` | Serialise to JSON string |
| `to_text` | `() → str` | Serialise to plain text |
| `emit_report` | `(path, format) → UbuntuReportWriteResult` | Serialise and write to disk |
| `default_session_path` | `(session_id) → str` | Resolve default per-session directory |

### `UbuntuMaintenanceReportConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, `emit_report` never writes |
| `output_base` | `str` | `"/var/log/ccai/maintenance"` | Base directory for default session paths |
| `default_json_indent` | `int \| None` | `2` | Indent used by `to_json` and JSON emission |

---

## Data Models

### Enums

```python
class UbuntuActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

class UbuntuReportFormat(Enum):
    JSON     = "json"
    TEXT     = "text"
    YAML     = "yaml"
    MARKDOWN = "markdown"
```

### Records

```python
@dataclass
class UbuntuActionRecord:
    feature: str
    operation: str
    status: UbuntuActionStatus
    detail: str | None = None
    error: str | None = None
    duration_seconds: float | None = None
    timestamp: datetime | None = None  # auto-stamped at construction when None

@dataclass
class UbuntuReportSummary:
    total: int
    succeeded: int
    failed: int
    skipped: int
    dry_run: int

@dataclass
class UbuntuReportWriteResult:
    success: bool
    path: str
    dry_run: bool = False
    error: str | None = None

@dataclass
class UbuntuMaintenanceSession:
    run_id: str
    host: str
    description: str | None
    started_at: datetime
    finished_at: datetime | None
    overall_status: UbuntuActionStatus | None
    actions: list[UbuntuActionRecord]
```

---

## Interfaces

### `IFileOperator` (from `codecraftai-interfaces`)

```python
class IFileOperator(Protocol):
    def write_text(
        self,
        path: str,
        content: str,
        *,
        elevated: bool = False,
        encoding: str = "utf-8",
    ) -> FileResult: ...
```

---

## Default Output Paths

| Situation | Path |
|---|---|
| Default base | `/var/log/ccai/maintenance/` |
| Per-session directory | `/var/log/ccai/maintenance/<session_id>/` |
| JSON report | `/var/log/ccai/maintenance/<session_id>/report.json` |
| Text report | `/var/log/ccai/maintenance/<session_id>/report.txt` |
| YAML report | `/var/log/ccai/maintenance/<session_id>/report.yaml` |
| Markdown report | `/var/log/ccai/maintenance/<session_id>/report.md` |

The default base is overrideable via `UbuntuMaintenanceReportConfig.output_base`.  On
Ubuntu, writing to `/var/log` requires either running as root or passing
`elevated=True` to the file operator.  The reporter never embeds an elevation
decision; the coordinator is responsible for configuring the file operator with the
right policy.

---

## Filesystem Access Layer

All writes go through `IFileOperator.write_text`.  On Ubuntu, the canonical
implementation is `UbuntuLocalFileOperator`, which uses `pathlib.Path.write_text` for
unprivileged writes and `sudo tee -a <path> >/dev/null` via the shared command runner
for elevated writes.  Remote writes use `UbuntuSSHFileOperator`, which uploads via
SFTP and then repositions the file to its final path with `sudo install -m 0644` on
the remote host.

---

## Serialisation

### JSON

Produced via `json.dumps(self._to_dict(), indent=indent, default=_json_default)`.  The
private `_to_dict` method returns a plain dict:

```python
{
    "run_id": self._session.run_id,
    "host": self._session.host,
    "description": self._session.description,
    "started_at": self._session.started_at.isoformat() + "Z",
    "finished_at": self._session.finished_at.isoformat() + "Z" if ... else None,
    "overall_status": self._session.overall_status.value if ... else None,
    "summary": dataclasses.asdict(self.summary_counts()),
    "actions": [self._action_to_dict(a) for a in self._session.actions],
}
```

`_action_to_dict` converts `timestamp` to ISO-8601 UTC, `status` to its enum value,
and leaves `None`-valued fields as `None`.

### Text

```
═══════════════════════════════════════════════════
UBUNTU MAINTENANCE REPORT
═══════════════════════════════════════════════════
Run ID    : <run_id>
Host      : <host>
Desc      : <description or "(none)">
Started   : <started_at ISO 8601>
Finished  : <finished_at ISO 8601 or "in progress">
Status    : <overall_status or "in progress">

Summary   : <total> total  <succeeded> ok  <failed> failed  <skipped> skipped  <dry_run> dry-run
───────────────────────────────────────────────────
Actions
───────────────────────────────────────────────────
[SUCCESS ] package-management / install_package    installed nginx 1.18.0-0ubuntu1    0.82s
[FAILED  ] service-management / enable_service     enable nginx failed
             Error: Unit nginx.service not found
[SKIPPED ] firewall-management / add_service       http already in default zone
───────────────────────────────────────────────────
(no actions recorded)     ← only when actions list is empty
```

### YAML

Produced via `yaml.safe_dump(self._to_dict(), default_flow_style=False,
sort_keys=False)`.  When `yaml` is not importable, `emit_report(..., YAML)` returns
`UbuntuReportWriteResult(success=False, error="PyYAML is required for YAML output")`
without attempting to write.

### Markdown

```markdown
# Ubuntu Maintenance Report

| Field | Value |
|---|---|
| Run ID | <run_id> |
| Host | <host> |
| Description | <description> |
| Started | <started_at> |
| Finished | <finished_at> |
| Status | <overall_status> |

## Summary

| Total | Succeeded | Failed | Skipped | Dry-Run |
|---|---|---|---|---|
| <n> | <n> | <n> | <n> | <n> |

## Actions

### 1. package-management / install_package — SUCCESS
- Detail: installed nginx 1.18.0-0ubuntu1
- Duration: 0.82s

### 2. service-management / enable_service — FAILED
- Detail: enable nginx failed
- Error: Unit nginx.service not found
```

---

## Method Logic

### `begin_session(run_id, host, description)`

Construct a new `UbuntuMaintenanceSession` with the supplied `run_id`, `host`,
`description`, `started_at = self._clock()`, `finished_at=None`,
`overall_status=None`, `actions=[]`.  Assign it to `self._session`.

### `add_action(record)` / `record_query(record)`

If `record.timestamp is None`, set it to `self._clock()`.  Append `record` to
`self._session.actions`.

### `record_error(feature, operation, error)`

Construct a `UbuntuActionRecord(feature=feature, operation=operation,
status=UbuntuActionStatus.FAILED, error=error)`; delegate to `add_action`.

### `end_session()`

Set `self._session.finished_at = self._clock()`.  Compute
`overall_status = FAILED` if any action has `status == FAILED`, else `SUCCESS`.

### `emit_report(path, format)`

1. Validate `path` (non-empty string) and `format` (member of `UbuntuReportFormat`).
2. If `config.dry_run=True`: return `UbuntuReportWriteResult(success=True, path=path,
   dry_run=True)`.
3. Serialise based on `format`:
   - `JSON` → `self.to_json(indent=config.default_json_indent)`
   - `TEXT` → `self.to_text()`
   - `YAML` → `self._to_yaml()` (may return an error result if PyYAML missing)
   - `MARKDOWN` → `self._to_markdown()`
4. Wrap `self._file_operator.write_text(path=path, content=content)` in
   `try/except Exception`.  Return `UbuntuReportWriteResult(success=True, path=path)`
   on success; return `UbuntuReportWriteResult(success=False, path=path,
   error=str(exc))` on exception.

---

## Error Handling

### Typed Exceptions

Maintenance-reporting does not define typed query exceptions.  All failures from
`emit_report` are surfaced on `UbuntuReportWriteResult`.  The one explicit exception
is `RuntimeError` from `record_error()` when no session is active (Requirement 3.2).

---

## Dry-Run Behaviour

When `UbuntuMaintenanceReportConfig.dry_run=True`:

- In-memory operations (`begin_session`, `add_action`, `record_query`, `record_error`,
  `end_session`, summary/filter/serialisation helpers) behave **identically** — they
  do not perform I/O anyway.
- `emit_report` returns `UbuntuReportWriteResult(success=True, path=path,
  dry_run=True)` without invoking the file operator.

---

## Correctness Properties

### Property 1: Begin Session Initialises State

*For any* call to `begin_session(run_id, host, description)`, the session has
`run_id=run_id`, `host=host`, `description=description`, `actions=[]`,
`finished_at=None`, `overall_status=None`, and `started_at` equal to the clock's
return value at call time.

**Validates: Requirements 1.1**

### Property 2: Begin Session No I/O

*For any* call to `begin_session`, the file operator is not invoked.

**Validates: Requirements 1.2**

### Property 3: Begin Session Replaces Prior State

*For any* second call to `begin_session`, the `actions` list is empty afterwards and
the previous session is discarded.

**Validates: Requirements 1.3**

### Property 4: Action Ordering Preserved

*For any* sequence of `add_action` calls, the `actions` list reflects insertion order.

**Validates: Requirements 2.1**

### Property 5: Timestamp Default

*For any* `UbuntuActionRecord` appended with `timestamp=None`, the stored record has
`timestamp` equal to the clock's value at `add_action` time.

**Validates: Requirements 2.2**

### Property 6: Explicit Timestamp Preserved

*For any* `UbuntuActionRecord` appended with a non-`None` `timestamp`, the stored
record retains that exact `timestamp`.

**Validates: Requirements 2.3**

### Property 7: `record_query` Equivalence

*For any* call to `record_query(record)`, the state mutation is identical to
`add_action(record)`.

**Validates: Requirements 2.5**

### Property 8: Record Error Shorthand

*For any* call to `record_error(feature, operation, error)`, the action list gains an
entry with `feature`, `operation`, `status=FAILED`, `error=error`.

**Validates: Requirements 3.1**

### Property 9: Record Error Without Session

*For any* call to `record_error` prior to `begin_session`, a `RuntimeError` is raised.

**Validates: Requirements 3.2**

### Property 10: End Session Stamps Finished

*For any* call to `end_session`, `finished_at` equals the clock's return value at call
time.

**Validates: Requirements 4.1**

### Property 11: Overall Status FAILED When Any FAILED

*For any* session whose actions include at least one FAILED record, `end_session` sets
`overall_status = FAILED`.

**Validates: Requirements 4.2**

### Property 12: Overall Status SUCCESS When No FAILED

*For any* session whose actions are all SUCCESS / SKIPPED / DRY_RUN (or empty),
`end_session` sets `overall_status = SUCCESS`.

**Validates: Requirements 4.3**

### Property 13: End Session Idempotent

*For any* second call to `end_session`, the recomputation of `overall_status` and
`finished_at` reflects the current action list.

**Validates: Requirements 4.4**

### Property 14: Summary Counts Accurate

*For any* session with N actions distributed across statuses, `summary_counts()`
returns a `UbuntuReportSummary` whose fields match the counts per status.

**Validates: Requirements 5.1**

### Property 15: Actions By Status Filter

*For any* call to `actions_by_status(s1, s2, ...)`, the result contains only records
with `status in {s1, s2, ...}`, in insertion order.

**Validates: Requirements 5.2**

### Property 16: Actions By Feature Filter

*For any* call to `actions_by_feature(f)`, the result contains only records with
`feature == f`, in insertion order.

**Validates: Requirements 5.3**

### Property 17: Reads Are Free Of I/O

*For any* read helper (`summary_counts`, `actions_by_status`, `actions_by_feature`,
`to_json`, `to_text`), the file operator is not invoked.

**Validates: Requirements 5.4**

### Property 18: JSON Round-Trips

*For any* session, `json.loads(report.to_json())` yields a dict whose keys match the
session fields and whose `actions` list length equals `len(actions)`.

**Validates: Requirements 6.1**

### Property 19: JSON Indent Forwarded

*For any* call to `to_json(indent=n)`, the resulting string's indentation matches the
JSON standard library output for that indent.

**Validates: Requirements 6.2**

### Property 20: Deterministic JSON

*For any* two consecutive calls to `to_json(indent=n)` with unchanged state, the
returned strings are byte-equal.

**Validates: Requirements 6.5**

### Property 21: Text Contains Key Fields

*For any* session, `to_text()` output contains the `run_id`, `host`,
`overall_status`, and a line per action.

**Validates: Requirements 6.6**

### Property 22: Error Indentation

*For any* action with non-empty `error`, the text output has an indented `Error: …`
line immediately below the action line.

**Validates: Requirements 6.7**

### Property 23: Emit JSON Writes Serialized Content

*For any* call to `emit_report(path, JSON)`, the file operator is invoked with
`path=path` and `content=self.to_json()`.

**Validates: Requirements 7.1**

### Property 24: Emit YAML Uses safe_dump

*For any* call to `emit_report(path, YAML)` when PyYAML is installed, the file
operator's content argument parses back via `yaml.safe_load` to a dict equivalent to
`self._to_dict()`.

**Validates: Requirements 7.2**

### Property 25: Markdown Structure

*For any* call to `emit_report(path, MARKDOWN)`, the file operator's content argument
begins with `# Ubuntu Maintenance Report` and contains a `## Summary` section and a
`## Actions` section.

**Validates: Requirements 7.3**

### Property 26: Dry-Run Skips File Operator

*For any* `emit_report` call with `config.dry_run=True`, the file operator is not
invoked and the result has `success=True`, `dry_run=True`.

**Validates: Requirements 7.4**

### Property 27: Write Exception Surface

*For any* file-operator exception during `emit_report`, the returned result has
`success=False` and `error` populated; the exception does not propagate.

**Validates: Requirements 7.5**

### Property 28: Empty Path Rejected

*For any* `emit_report("", fmt)` call, the result has `success=False` and the file
operator is not invoked.

**Validates: Requirements 7.6**

### Property 29: Unknown Format Rejected

*For any* `emit_report(path, format)` call with `format` not a `UbuntuReportFormat`
member, the result has `success=False` and the file operator is not invoked.

**Validates: Requirements 7.7**

### Property 30: Default Session Path Shape

*For any* `default_session_path(session_id)` call, the returned string equals
`/var/log/ccai/maintenance/<session_id>/` (or the overridden base followed by
`session_id` with a trailing slash).

**Validates: Requirements 8.1, 8.3**

### Property 31: Default Session Path Rejects Empty

*For any* `default_session_path("")` call, the system raises `ValueError`.

**Validates: Requirements 8.2**

### Property 32: Clock Injection

*For any* call to a method that stamps a timestamp, the stamped value equals
`self._clock()` evaluated at call time.

**Validates: Requirements 9.1, 9.2, 9.3**

---

## Module Layout

```
source/packages/ccai/sysman/linux/ubuntu/
├── __init__.py
└── maintenancereporting.py      ← UbuntuMaintenanceReport, models, config, enums

source/testroots/testbase/
└── ccai/tests/sysman/linux/ubuntu/
    └── test_maintenancereporting.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from ccai.sysman.linux.ubuntu.fileoperators import (
    UbuntuLocalFileOperator,
    UbuntuLocalFileOperatorConfig,
)
from ccai.sysman.linux.ubuntu.maintenancereporting import (
    UbuntuMaintenanceReport,
    UbuntuMaintenanceReportConfig,
    UbuntuActionRecord,
    UbuntuActionStatus,
    UbuntuReportFormat,
)

fileop = UbuntuLocalFileOperator(UbuntuLocalFileOperatorConfig())
config = UbuntuMaintenanceReportConfig(dry_run=False)
report = UbuntuMaintenanceReport(file_operator=fileop, config=config)

report.begin_session(run_id="maint-2026-04-24-001", host="ubuntu-web-01",
                     description="Monthly security updates")

report.add_action(UbuntuActionRecord(
    feature="package-management",
    operation="upgrade_all",
    status=UbuntuActionStatus.SUCCESS,
    detail="Upgraded 23 packages",
    duration_seconds=45.2,
))

report.end_session()

result = report.emit_report(
    path=f"{report.default_session_path('maint-2026-04-24-001')}report.json",
    format=UbuntuReportFormat.JSON,
)
if result.success:
    print(f"wrote {result.path}")
```

### SSH Execution with Aspects

```python
from ccai.sysman.linux.ubuntu.fileoperators import (
    UbuntuSSHFileOperator,
    UbuntuSSHFileOperatorConfig,
)
from ccai.sysman.linux.ubuntu.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from ccai.sysman.linux.ubuntu.storage import SQLiteStorage

fileop = UbuntuSSHFileOperator(
    UbuntuSSHFileOperatorConfig(host="ubuntu-prod-01", username="ops"),
)
fileop.add_aspect(aspect=PersistenceAspect(
    PersistenceAspectConfig(storage_backend=SQLiteStorage(path="/tmp/files.db")),
))
fileop.add_aspect(aspect=RetryAspect(
    RetryAspectConfig(max_retries=3),
))
fileop.add_aspect(aspect=TimeoutAspect(
    TimeoutAspectConfig(default_timeout=30.0),
))

report = UbuntuMaintenanceReport(file_operator=fileop,
                                 config=UbuntuMaintenanceReportConfig())
report.begin_session(run_id="ssh-001", host="ubuntu-prod-01", description=None)
report.record_error(feature="service-management", operation="restart",
                    error="Unit nginx.service not found")
report.end_session()
report.emit_report(path="/var/log/ccai/maintenance/ssh-001/report.yaml",
                   format=UbuntuReportFormat.YAML)
```

### Dry-Run Preview

```python
config = UbuntuMaintenanceReportConfig(dry_run=True)
report = UbuntuMaintenanceReport(file_operator=fileop, config=config)
report.begin_session(run_id="dry", host="x", description=None)
report.end_session()
result = report.emit_report(path="/tmp/whatever.json",
                            format=UbuntuReportFormat.JSON)
assert result.dry_run is True
assert result.success is True
```

---

## Testing Strategy

1. **MockFileOperator** records `write_text` calls and their `(path, content,
   elevated)` arguments for assertion.
2. A fixed `_clock` lambda is passed to the reporter in every test needing
   deterministic timestamps.
3. `to_json` output is parsed back with `json.loads` to assert field values rather
   than string-matching.
4. `to_text` output is tested by asserting key substrings appear (run_id, host,
   status labels, action lines).
5. `end_session` is tested before and after adding a failing action to verify
   `overall_status` recalculation.
6. `emit_report` tests cover all four formats, a path-empty rejection, an
   unknown-format rejection, a dry-run short-circuit, and an exception-path that
   returns `success=False`.
7. YAML tests are skipped via `pytest.importorskip("yaml")` when PyYAML is not
   present; a separate test asserts the typed failure when the import fails.
8. Markdown tests assert section headers and row counts.
9. `default_session_path` is tested for a happy path, an empty-id `ValueError`, and
   an overridden `output_base`.
10. No test requires filesystem access or live command execution.
