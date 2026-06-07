# Design

## Overview

The certificate-trust-management feature is implemented as a single
module, `ccai.sysman.linux.ubuntu.certificatetrustmanagement`,
that exposes a `UbuntuCertificateTrustManager` class.  This spec
implements the certificate-trust-management contract defined at
`codecraftai-sysman-common/specs/contracts/certificate-trust-management/contract.md`
for Ubuntu using the stock Debian CA-trust toolchain
(`update-ca-certificates`) shipped with current Ubuntu LTS releases.

All system interaction is delegated to a command runner implementing
the `ICommandRunner` interface.  No direct filesystem access or
`subprocess` calls are made inside the module — file I/O routes
through the runner via `cat` / `tee` / `ls` / `rm`, and certificate
metadata routes through `openssl` invoked by the runner.  The bundle
refresh routes through `update-ca-certificates`.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via
`SSHCommandRunner`).  When using SSH, the coordinator can track
reliability metrics and apply execution aspects (persistence, retry,
timeout) without any changes to the
`UbuntuCertificateTrustManager` code.

**See Also:** [Command Runner Architecture](../1-command-execution/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into UbuntuCertificateTrustManager        │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│         UbuntuCertificateTrustManager                       │
│  ccai.sysman.linux.ubuntu.certificatetrustmanagement    │
│                                                             │
│  add_trusted_cert()       remove_trusted_cert()             │
│  list_trusted_certs()     get_cert_details()                │
│  update_trust_store()                                       │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output, stdin) → RunResult     │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH connection     │
│ - sudo for elevation │      │ - reliability metrics│
│ - local execution    │      │ - connection retry   │
└──────────────────────┘      └──────────────────────┘
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

### `UbuntuCertificateTrustManager`

**Location:**
`source/packages/ccai/sysman/linux/ubuntu/certificatetrustmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `add_trusted_cert` | `(store, path, purpose=None) → UbuntuCertTrustActionResult` | Validate + write a PEM file into the resolved store directory |
| `remove_trusted_cert` | `(store, fingerprint) → UbuntuCertTrustActionResult` | Locate by fingerprint and `rm -f` |
| `list_trusted_certs` | `(store) → list[UbuntuCertTrustInfo]` | Enumerate, fingerprint, parse metadata |
| `get_cert_details` | `(fingerprint) → UbuntuCertTrustInfo \| None` | Lookup across all configured stores |
| `update_trust_store` | `() → UbuntuCertTrustActionResult` | Run `update-ca-certificates` |

### `UbuntuCertTrustManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit |
| `elevated` | `bool` | `False` | When `True`, system-store writes pass `elevated=True` |
| `store_path_map` | `dict[str, str \| None] \| None` | `None` | Optional override of abstract store name -> directory |
| `default_store` | `str` | `"system-ca"` | Default store for verbs that omit it |
| `openssl_executable` | `str` | `"openssl"` | Command name for cert parsing |
| `update_executable` | `str` | `"update-ca-certificates"` | Command name for refresh |

The bundled defaults expose `"system-ca"` as
`/usr/local/share/ca-certificates` and `"user-ca"` as `None`
(unsupported).  Callers may override either or extend with their own
store names.

---

## Data Models

### Result Types

```python
class UbuntuCertTrustActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"

@dataclass
class UbuntuCertTrustActionResult(FeatureActionResult):
    status: UbuntuCertTrustActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    store: str | None
    fingerprint: str | None
    path: str | None
```

### Query Result Types

```python
@dataclass
class UbuntuCertTrustInfo:
    fingerprint: str
    subject: str
    issuer: str
    not_before: str
    not_after: str
    serial_number: str
    store: str
    path: str | None
    purposes: list[str]
```

### Exceptions

```python
class UbuntuCertTrustQueryError(Exception):
    """
        Raised when a read operation fails for a reason other than
        the target source simply being absent.
    """
```

`get_cert_details` returns `None` for an unknown fingerprint rather
than raising — this matches the contract's read-method invariant.

---

## Trust-Store Mapping

| Abstract store | Ubuntu-native path | Notes |
|----------------|--------------------|-------|
| `"system-ca"` | `/usr/local/share/ca-certificates/` | Files MUST end in `.crt`. |
| `"user-ca"` | `None` (unsupported) | Ubuntu lacks a per-user trust bundle.  Writes return `FAILED`. |
| Caller override | Whatever the override map says | Passed through to the runner verbatim. |

The consolidated bundle that TLS clients consult lives at
`/etc/ssl/certs/ca-certificates.crt`; this module does NOT read or
write the bundle directly — `update-ca-certificates` rebuilds it from
the anchor directories on each `update_trust_store` call.

---

## Correctness Properties

### Property 1: PEM Validation Before Write

*For any* call to `add_trusted_cert` whose source bytes do not contain
a valid PEM CERTIFICATE block, the runner SHALL NOT receive a `tee`
command and the result SHALL be `FAILED`.

**Validates: Requirements 1.4, 9.1, 9.2**

### Property 2: Fingerprint-Based Idempotency

*For any* call to `add_trusted_cert` whose certificate's SHA-256
fingerprint matches an existing file in the store directory, the
runner SHALL NOT receive a `tee` command and the result SHALL be
`SKIPPED`.  Filename equality is irrelevant — matching is purely
fingerprint-based.

**Validates: Requirements 1.5, 6.1, 6.3**

### Property 3: Remove-Absent Skip

*For any* call to `remove_trusted_cert` whose fingerprint is not
present in the store, the runner SHALL NOT receive an `rm` command
and the result SHALL be `SKIPPED`.

**Validates: Requirements 2.3, 6.2**

### Property 4: Refresh Determinism

*For any* call to `update_trust_store`, the runner SHALL receive
`update-ca-certificates` on every non-dry-run invocation, and the
result status SHALL be deterministic with respect to the runner's
exit code: zero maps to `SUCCESS`, non-zero maps to `FAILED`.

**Validates: Requirements 5.1, 5.2, 5.3, 5.5**

### Property 5: Dry-Run Safety

*For any* mutating verb invoked with `dry_run=True`, the runner SHALL
NOT be invoked with a mutating command (`tee`, `rm`,
`update-ca-certificates`).  The result status SHALL be `DRY_RUN`.

**Validates: Requirements 1.6, 2.5, 5.4**

### Property 6: Elevation Consistency

*For any* mutating operation against `"system-ca"` with
`config.elevated=True`, the runner SHALL be invoked with
`elevated=True`.  For read-only operations
(`list_trusted_certs`, `get_cert_details`), the runner SHALL be
invoked with `elevated=False` regardless of config.  For mutating
operations against `"user-ca"`, the runner SHALL NOT be invoked at
all.

**Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5**

### Property 7: Fingerprint Normalisation Round-Trip

*For any* fingerprint argument supplied in colonised or
non-colonised form (any case, with or without `"SHA256:"` prefix),
the system SHALL normalise to the same canonical Ubuntu form before
comparison.  Result objects SHALL emit fingerprints in that
canonical form.

**Validates: Requirements 8.1, 8.2, 8.3**

### Property 8: Unsupported-Store Surface

*For any* mutating verb against an unsupported store name, the
runner SHALL NOT be invoked and the result SHALL be `FAILED` with a
descriptive `detail`.  For `list_trusted_certs` against an
unsupported store name, `UbuntuCertTrustQueryError` SHALL be raised.

**Validates: Requirements 1.2, 2.2, 3.1**

### Property 9: Extension Coercion

*For any* call to `add_trusted_cert` whose source basename ends in
`.pem`, the destination filename SHALL end in `.crt` (Debian's
`update-ca-certificates` skips non-`.crt` files).  For source basenames
without an extension, `.crt` SHALL be appended verbatim.

**Validates: Requirements 1.7**

### Property 10: Read Pure Parsing

*For any* call to `list_trusted_certs` or `get_cert_details`,
fingerprint computation SHALL be performed in pure Python on the PEM
bytes; only the auxiliary metadata (subject, issuer, validity, EKU)
is delegated to `openssl x509 -text`.  Pure parsing is not
elevation-dependent.

**Validates: Requirements 3.3, 4.3, 10.4**

---

## Interfaces

### `ICommandRunner` (from `codecraftai-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
        stdin: str | None = None,
    ) -> RunResult: ...
```

### `RunResult` (from `codecraftai-interfaces`)

```python
@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
```

### Elevation Strategy

When the manager is asked to mutate `"system-ca"`, it consults
`config.elevated` and forwards the flag to the runner.  When
asked to mutate `"user-ca"`, the manager short-circuits to `FAILED`
before the runner is touched (Ubuntu has no per-user store).  Reads
never elevate.  The runner is responsible for prepending `sudo`
or using whatever privilege escalation mechanism is appropriate for
the environment.

---

## Command Mappings

| Operation | Command |
|-----------|---------|
| Read source PEM | `cat <path>` |
| Enumerate store directory | `ls -1 <store-dir>` |
| Read store certificate | `cat <store-dir>/<file>` |
| Parse metadata | `openssl x509 -in <path> -noout -subject -issuer -startdate -enddate -serial -text` |
| Write store certificate | `tee <store-dir>/<file>` (content on stdin) |
| Remove store certificate | `rm -f <store-dir>/<file>` |
| Refresh consolidated bundle | `update-ca-certificates` |

The bundle path `/etc/ssl/certs/ca-certificates.crt` is NOT read
directly; `update-ca-certificates` is the only entry point that
manipulates it.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|-----------|-------------|
| `UbuntuCertTrustQueryError` | `list_trusted_certs` is called against an unsupported store name. |

All other failures surface as a `UbuntuCertTrustActionResult` with
`status=FAILED`, populated `detail`, and (when applicable)
runner-captured `exit_code` / `stdout` / `stderr` / `command`.

### Idempotency Guards

Before writing a new anchor, the manager enumerates the store
directory via `ls`, fingerprints each existing file via pure-Python
SHA-256, and compares against the supplied PEM's fingerprint.
Match -> `SKIPPED` without dispatch.  Before removing an anchor, the
same enumeration locates the target file by fingerprint; a missing
match -> `SKIPPED`.

### PEM Validation

Source bytes are split into `-----BEGIN CERTIFICATE-----` /
`-----END CERTIFICATE-----` envelopes.  Zero envelopes -> `FAILED`.
The body of the first envelope is base64-decoded; a decoding failure
-> `FAILED`.  This validation runs entirely in-process (no `openssl`
round-trip) so it is fast and deterministic.

---

## Module Layout

```
source/packages/ccai/sysman/linux/ubuntu/
├── __init__.py
└── certificatetrustmanagement.py
    ├── UbuntuCertTrustManagerConfig
    ├── UbuntuCertTrustActionResult
    ├── UbuntuCertTrustInfo
    ├── UbuntuCertTrustQueryError
    ├── UbuntuCertificateTrustManager
    └── module-level helpers
        - _parse_pem_blocks
        - _compute_fingerprint
        - _normalize_fingerprint
        - _normalize_destination_filename
        - _parse_openssl_cert_text

source/testroots/testbase/
└── ccai/tests/sysman/linux/ubuntu/
    └── test_certificatetrustmanagement.py
```

---

## Testing Strategy

### Unit Testing

- All tests use a `_MockCommandRunner` that records every `run` call
  and returns pre-seeded `RunResult` values keyed by argv tuple.
- Tests verify the *arguments* passed to the runner, not system
  state.
- Each public method has at minimum: a success-path test, an
  idempotency / skip test, a dry-run test, and a failure-surface
  test.
- `add_trusted_cert` has additional tests for PEM-validation failure
  and for elevated-write parameters.
- No test requires root access, live `openssl`, live
  `update-ca-certificates`, or a live `/usr/local/share/ca-certificates`
  directory.

### Test Coverage Map

| Property | Test name |
|----------|-----------|
| 1 | `test_add_trusted_cert_invalid_pem_returns_failed` |
| 2 | `test_add_trusted_cert_skipped_when_fingerprint_present` |
| 3 | `test_remove_trusted_cert_skipped_when_absent` |
| 4 | `test_update_trust_store_invokes_update_ca_certificates` |
| 5 | `test_add_trusted_cert_dry_run_short_circuits` |
| 6 | `test_writes_pass_elevated_true_for_system_store`, `test_user_store_writes_do_not_elevate`, `test_reads_never_elevate` |
| 7 | covered indirectly via `test_remove_trusted_cert_invokes_rm` (passes the canonical form) and via the helper `_normalize_fingerprint` symmetry |
| 8 | `test_unsupported_store_returns_failed`, `test_unsupported_store_in_list_raises` |
| 9 | covered by `test_add_trusted_cert_invokes_tee` (basename `foo.pem` -> destination `foo.crt`) |
| 10 | `test_list_trusted_certs_parses_output`, `test_get_cert_details_returns_full_info` |

---

## Usage Examples

### Local Execution (Install Internal CA, Refresh)

```python
from ccai.sysman.linux.ubuntu.runners import LocalCommandRunner, LocalRunnerConfig
from ccai.sysman.linux.ubuntu.certificatetrustmanagement import (
    UbuntuCertificateTrustManager,
    UbuntuCertTrustManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
manager = UbuntuCertificateTrustManager(
    runner=runner,
    config=UbuntuCertTrustManagerConfig(elevated=True, dry_run=False),
)

# Install an internal CA as a trust anchor
add_result = manager.add_trusted_cert(
    store="system-ca",
    path="/opt/pki/internal-root-ca.pem",
)
print(f"Add: {add_result.status} -> {add_result.fingerprint}")

# Refresh the consolidated bundle
refresh_result = manager.update_trust_store()
print(f"Refresh: {refresh_result.status}")
```

### SSH Execution with Aspects

```python
from ccai.sysman.linux.ubuntu.runners import SSHCommandRunner, SSHRunnerConfig
from ccai.sysman.linux.ubuntu.aspects import (
    PersistenceAspect, RetryAspect, TimeoutAspect,
    PersistenceAspectConfig, RetryAspectConfig, TimeoutAspectConfig,
)
from ccai.sysman.linux.ubuntu.storage import SQLiteStorage
from ccai.sysman.linux.ubuntu.certificatetrustmanagement import (
    UbuntuCertificateTrustManager,
    UbuntuCertTrustManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote", username="admin"))

storage = SQLiteStorage("/var/log/maintenance/commands.db")
runner.add_aspect(PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(RetryAspect(RetryAspectConfig(max_retries=3)))
runner.add_aspect(TimeoutAspect(TimeoutAspectConfig(default_timeout=300.0)))

manager = UbuntuCertificateTrustManager(
    runner=runner,
    config=UbuntuCertTrustManagerConfig(elevated=True),
)

manager.add_trusted_cert(
    store="system-ca",
    path="/opt/pki/corp-issuing-ca.pem",
)
manager.update_trust_store()
```

### Dry-Run Across Transports

```python
manager = UbuntuCertificateTrustManager(
    runner=runner,
    config=UbuntuCertTrustManagerConfig(elevated=True, dry_run=True),
)

result = manager.add_trusted_cert(
    store="system-ca",
    path="/opt/pki/corp-issuing-ca.pem",
)
assert result.status == UbuntuCertTrustActionStatus.DRY_RUN
assert result.command == ["tee", "/usr/local/share/ca-certificates/corp-issuing-ca.crt"]
```

---

## Spec-vs-Contract Drift

The contract enumerates `set_store_enabled` and `verify_certificate`
verbs on the abstract surface.  Ubuntu's
`update-ca-certificates` does not expose an enable / disable toggle
(the consolidator is always active when installed); `verify_certificate`
overlaps with `codecraftai-credentials` cert helpers and is deferred per
the RHEL spec's task 21 follow-up.  Both verbs are intentionally
NOT implemented in this wave — callers needing enable/disable should
manage the package via `repository-management` and
`package-management`, and callers needing verification should
construct their own `openssl verify` invocation against the
consolidated bundle until the dedicated verb lands in a follow-up
wave.

The contract's `TrustPurpose` enum is also not implemented as a
typed Python enum here; instead, the `purpose` parameter on
`add_trusted_cert` is a free-form `str | None` recorded on the
result.  Ubuntu's anchor directory is purpose-agnostic — every
installed anchor is trusted for every TLS purpose by the
consolidated bundle, so the typed enum has no runtime effect on the
Ubuntu surface.  Future waves may revisit if the bundle gains
purpose-scoped anchor sub-directories.
