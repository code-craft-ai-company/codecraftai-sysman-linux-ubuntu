# Design

## Overview

The certificate-issuance feature is implemented as a single module,
`ccai.sysman.linux.ubuntu.certificateissuance`, that exposes a
`UbuntuCertificateIssuanceManager` class.  This spec implements the
certificate-issuance contract defined at
`codecraftai-sysman-common/specs/contracts/certificate-issuance/contract.md`
for Ubuntu using two interchangeable backends:

- **certbot** — ACME issuance via the system `certbot` binary, the
  standard Let's-Encrypt-on-Ubuntu pathway.
- **openssl** — local self-signed / CSR-based issuance via the system
  `openssl` binary.  Useful for development or for an internal CA
  that does not run an ACME endpoint.

All system interaction is delegated to a command runner implementing
the `ICommandRunner` interface.  No direct filesystem access or
`subprocess` calls are made inside the module — file I/O routes
through the runner via `cat` / `tee` / `ls` / `cmp`, and certificate
metadata routes through `openssl x509 -text` invoked by the runner.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via
`SSHCommandRunner`).

**See Also:**

- [Certificate Trust Management](../3-certificate-trust-management/design.md)
  — owns the *trust* side of the surface (CA anchors).  This spec
  owns the *issuance* side.
- [Command Runner Architecture](../1-command-execution/design.md)
  — the underlying execution model.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into UbuntuCertificateIssuanceManager     │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│       UbuntuCertificateIssuanceManager                      │
│  ccai.sysman.linux.ubuntu.certificateissuance           │
│                                                             │
│  request_certificate()    renew_certificate()               │
│  revoke_certificate()     get_certificate()                 │
│  list_certificates()      export_certificate()              │
│  import_certificate()                                       │
│                                                             │
│  Internal dispatch:                                         │
│   _dispatch_certbot_request / _dispatch_openssl_request     │
│   _dispatch_certbot_renew   / _dispatch_openssl_renew       │
│   _dispatch_certbot_revoke  / _dispatch_openssl_revoke      │
│   _dispatch_certbot_list    / _dispatch_openssl_list        │
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
```

---

## Components

### `UbuntuCertificateIssuanceManager`

**Location:**
`source/packages/ccai/sysman/linux/ubuntu/certificateissuance.py`

| Method | Signature | Description |
|---|---|---|
| `request_certificate` | `(spec) → UbuntuCertIssuanceActionResult` | Issue a new certificate, dispatching to certbot or openssl |
| `renew_certificate` | `(alias) → UbuntuCertIssuanceActionResult` | Renew an existing certificate by alias |
| `revoke_certificate` | `(alias, reason=None) → UbuntuCertIssuanceActionResult` | Revoke a certificate by alias |
| `get_certificate` | `(alias) → UbuntuCertificateInfo \| None` | Parse `openssl x509 -text` for one alias |
| `list_certificates` | `() → list[UbuntuCertificateInfo]` | Enumerate every managed certificate |
| `export_certificate` | `(alias, path, include_private_key=False) → UbuntuCertIssuanceActionResult` | Copy cert (and optionally key) to a path |
| `import_certificate` | `(path, alias) → UbuntuCertIssuanceActionResult` | Bring an external cert under the alias |

### `UbuntuCertIssuanceManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit. |
| `elevated` | `bool` | `False` | When `True`, mutating commands pass `elevated=True`. |
| `backend` | `UbuntuIssuanceBackend` | `CERTBOT` | Issuance backend selector. |
| `certbot_cli` | `str` | `"certbot"` | Command name for certbot. |
| `openssl_cli` | `str` | `"openssl"` | Command name for openssl. |
| `cert_dir` | `str` | `"/etc/letsencrypt/live"` | Where managed certs live. |
| `acme_email` | `str \| None` | `None` | ACME registration email; required for certbot issuance. |
| `acme_method` | `str` | `"webroot"` | `"webroot"` or `"standalone"`. |
| `webroot` | `str \| None` | `None` | Webroot path; required when `acme_method="webroot"`. |
| `renew_threshold_days` | `int` | `30` | Skip threshold for `request` / `renew`. |

### Backend Selection Table

| Verb | Backend `CERTBOT` | Backend `OPENSSL` |
|------|-------------------|-------------------|
| `request_certificate` (non-self-signed) | `certbot certonly --non-interactive --agree-tos --email <e> -d <cn> [-d <san>] (--webroot -w <wr> \| --standalone) --cert-name <alias>` | `openssl req -newkey <alg>:<bits> -nodes -keyout <k> -out <csr> -subj '<s>'` |
| `request_certificate` (self-signed forced) | falls through to OPENSSL branch | `openssl x509 -req -days <n> -in <csr> -signkey <k> -out <cert>` |
| `renew_certificate` | `certbot renew --cert-name <alias> --non-interactive` | regenerate self-signed cert against existing key+CSR |
| `revoke_certificate` | `certbot revoke --cert-name <alias> --non-interactive [--reason <r>]` | FAILED with detail "OPENSSL backend has no CRL" |
| `get_certificate` | shared `openssl x509 -text` parse on the resolved path | shared `openssl x509 -text` parse |
| `list_certificates` | `certbot certificates` parse | enumerate `cert_dir`, `openssl x509 -text` per file |
| `export_certificate` | `cat <src>` then `tee <dest>` | same |
| `import_certificate` | `cat <src>` validate PEM, `tee <cert_dir>/<alias>.pem` | same |

The dispatch is implemented at the verb level via paired
`_dispatch_certbot_<op>` / `_dispatch_openssl_<op>` methods.  The
public verbs select between them based on `config.backend` and (for
`request_certificate`) on `spec.is_self_signed`.

---

## Data Models

```python
class UbuntuCertIssuanceActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


class UbuntuIssuanceBackend(Enum):
    CERTBOT = "certbot"
    OPENSSL = "openssl"


class UbuntuRevocationReason(Enum):
    UNSPECIFIED            = "unspecified"
    KEY_COMPROMISE         = "keycompromise"
    CA_COMPROMISE          = "cacompromise"
    AFFILIATION_CHANGED    = "affiliationchanged"
    SUPERSEDED             = "superseded"
    CESSATION_OF_OPERATION = "cessationofoperation"


@dataclass
class UbuntuCertIssuanceActionResult(FeatureActionResult):
    alias: str | None
    subject: str | None
    fingerprint: str | None


@dataclass
class UbuntuCertificateInfo:
    alias: str
    subject: str
    issuer: str
    not_before: str
    not_after: str
    fingerprint: str
    serial_number: str
    key_algorithm: str | None
    key_length: int | None
    sans: list[str]
    path: str | None
    is_self_signed: bool


@dataclass
class UbuntuCertificateRequestSpec:
    alias: str
    common_name: str
    email: str | None = None
    sans: list[str] = []
    key_algorithm: str = "RSA"
    key_length: int = 2048
    validity_days: int = 365
    is_self_signed: bool = False
    webroot: str | None = None


@dataclass
class UbuntuCertIssuanceManagerConfig(FeatureConfig):
    backend: UbuntuIssuanceBackend = UbuntuIssuanceBackend.CERTBOT
    certbot_cli: str = "certbot"
    openssl_cli: str = "openssl"
    cert_dir: str = "/etc/letsencrypt/live"
    acme_email: str | None = None
    acme_method: str = "webroot"
    webroot: str | None = None
    renew_threshold_days: int = 30


class UbuntuCertIssuanceQueryError(Exception):
    """Raised when a read operation fails unexpectedly."""
```

---

## Correctness Properties

### Property 1: Backend Dispatch

*For any* call to a write verb, the actually-dispatched argv SHALL
match the backend's documented command shape (Backend Selection
Table).  No verb SHALL contain its own ad-hoc backend branching
beyond the `_dispatch_certbot_<op>` / `_dispatch_openssl_<op>`
selector.

**Validates: Requirements 1.1, 1.2, 2.1, 2.2, 3.1, 3.2, 8.3**

### Property 2: Self-Signed Override

*For any* call to `request_certificate(spec)` where
`spec.is_self_signed=True`, the dispatched argv SHALL be the
openssl branch *regardless* of `config.backend`.

**Validates: Requirements 1.2, 8.4**

### Property 3: Fingerprint-Based Idempotency for Import

*For any* call to `import_certificate` whose source bytes
fingerprint matches the already-stored material under the same
alias, the runner SHALL NOT receive a `tee` command and the result
SHALL be `SKIPPED`.

**Validates: Requirement 9.4**

### Property 4: Renew-Threshold Skip

*For any* call to `request_certificate` or `renew_certificate`
whose existing certificate's remaining validity exceeds
`config.renew_threshold_days`, no issuance command SHALL be
dispatched and the result SHALL be `SKIPPED`.

**Validates: Requirements 1.3, 2.3, 9.1, 9.2**

### Property 5: Dry-Run Safety

*For any* mutating verb invoked with `dry_run=True`, the runner
SHALL NOT be invoked with a mutating command.  The result status
SHALL be `DRY_RUN`.

**Validates: Requirements 1.4, 2.5, 3.4, 6.5, 7.4, 10.1**

### Property 6: Elevation Consistency

*For any* mutating verb with `config.elevated=True`, the runner
SHALL be invoked with `elevated=True`.  *For any* read verb,
`elevated=False` SHALL be passed regardless of config.

**Validates: Requirements 1.5, 2.6, 6.6, 7.5, 10.2**

### Property 7: Credential Sanitisation

*For any* verb that accepts secret bytes (passphrase, ACME account
key), the bytes SHALL flow on `stdin` only.  The recorded `command`
field SHALL never contain the secret.

**Validates: Requirements 1.7, 6.7, 10.4**

### Property 8: Export Byte-Match Skip

*For any* call to `export_certificate` whose destination path
already byte-matches the source, the runner SHALL NOT receive a
`tee` command and the result SHALL be `SKIPPED`.

**Validates: Requirement 9.5**

### Property 9: Read Pure Parsing

*For any* call to `get_certificate` or `list_certificates`,
fingerprint computation SHALL be performed in pure Python on the
PEM bytes; only the auxiliary metadata (subject, issuer, validity,
key info, SANs) SHALL be delegated to `openssl x509 -text`.  Reads
SHALL NOT elevate.

**Validates: Requirements 4.1, 4.5, 5.5**

### Property 10: Failure Capture

*For any* dispatched command that exits non-zero, the result
SHALL be `FAILED` with `exit_code`, `stdout`, `stderr`, and
`command` populated.

**Validates: Requirements 1.6, 10.3**

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

---

## Command Mappings

### Certbot Backend

| Operation | Command |
|-----------|---------|
| Issue (webroot) | `certbot certonly --non-interactive --agree-tos --email <e> --webroot -w <wr> -d <cn> [-d <san> ...] --cert-name <alias>` |
| Issue (standalone) | `certbot certonly --non-interactive --agree-tos --email <e> --standalone -d <cn> [-d <san> ...] --cert-name <alias>` |
| Renew | `certbot renew --cert-name <alias> --non-interactive` |
| Revoke | `certbot revoke --cert-name <alias> --non-interactive [--reason <r>]` |
| List | `certbot certificates` |

### Openssl Backend

| Operation | Command |
|-----------|---------|
| CSR generate | `openssl req -newkey rsa:<bits> -nodes -keyout <k> -out <csr> -subj '/CN=<cn>'` |
| Self-sign | `openssl x509 -req -days <n> -in <csr> -signkey <k> -out <cert>` |
| Inspect | `openssl x509 -in <p> -noout -text -fingerprint -sha256 -dates -subject -issuer -serial` |

### Shared

| Operation | Command |
|-----------|---------|
| Read source | `cat <path>` |
| Enumerate dir | `ls -1 <dir>` |
| Write | `tee <dest>` (content on stdin) |
| Compare | `cmp -s <src> <dest>` |

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|-----------|-------------|
| `UbuntuCertIssuanceQueryError` | A read fails unexpectedly (e.g. `openssl x509` exits non-zero on a file the manager believed it owned). |

All other failures surface as a `UbuntuCertIssuanceActionResult`
with `status=FAILED`, populated `detail`, and (when applicable)
runner-captured `exit_code` / `stdout` / `stderr` / `command`.

### Idempotency Guards

Before requesting / renewing, the manager parses the existing
certificate's `notAfter` (via `openssl x509 -enddate`) and
compares time-to-expiry against
`config.renew_threshold_days`.  Above threshold -> `SKIPPED`.

Before importing, the manager fingerprints the source PEM and
compares against the pre-existing alias-to-fingerprint mapping.
Match -> `SKIPPED`.

Before exporting, the manager runs `cmp -s` between the source and
destination paths.  Match -> `SKIPPED`.

---

## Module Layout

```
source/packages/ccai/sysman/linux/ubuntu/
├── __init__.py
└── certificateissuance.py
    ├── UbuntuCertIssuanceActionStatus       (alias to ActionStatus)
    ├── UbuntuIssuanceBackend                (enum)
    ├── UbuntuRevocationReason               (enum)
    ├── UbuntuCertIssuanceActionResult       (dataclass)
    ├── UbuntuCertificateInfo                (dataclass)
    ├── UbuntuCertificateRequestSpec         (dataclass)
    ├── UbuntuCertIssuanceManagerConfig      (dataclass)
    ├── UbuntuCertIssuanceQueryError         (exception)
    ├── UbuntuCertificateIssuanceManager     (manager class)
    └── module-level helpers
        - _parse_pem_blocks
        - _compute_fingerprint
        - _parse_openssl_x509_text
        - _parse_certbot_certificates_output
        - _days_until
        - _format_subject
        - _format_san_args

source/testroots/testbase/
└── ccai/tests/sysman/linux/ubuntu/
    └── test_certificateissuance.py
```

---

## Testing Strategy

### Unit Testing

- All tests use a `_MockCommandRunner` that records every `run`
  call and returns pre-seeded `RunResult` values keyed by argv
  tuple.
- Tests verify the *arguments* passed to the runner, not system
  state.
- Each public verb has at minimum: a success-path test, an
  idempotency / skip test, a dry-run test, and a failure-surface
  test.
- Both backends are exercised: `request_certificate` has a CERTBOT
  variant and an OPENSSL self-signed variant; `list_certificates`
  has both.
- No test requires root access, live `certbot`, live `openssl`,
  or a live `/etc/letsencrypt`.

### Test Coverage Map

| Property | Test name |
|----------|-----------|
| 1, 8 | `test_request_certificate_invokes_certbot_with_subject` |
| 2 | `test_request_certificate_self_signed_uses_openssl` |
| 3 | `test_import_certificate_skipped_when_fingerprint_present` |
| 4 | `test_request_certificate_skipped_when_present_and_not_near_expiry`, `test_renew_certificate_skipped_when_not_near_expiry` |
| 5 | `test_request_certificate_dry_run_short_circuits` |
| 6 | `test_writes_elevate`, `test_reads_never_elevate` |
| 7 | covered by `test_passphrase_redacted_in_recorded_command` (sister batch on Windows) |
| 8 | `test_export_certificate_skipped_when_byte_matches` |
| 9 | `test_get_certificate_parses_x509_output`, `test_list_certificates_returns_all` |
| 10 | `test_failed_command_surfaces_stderr` |

---

## Spec-vs-Contract Drift

The contract enumerates a `Credential` parameter on every write
verb.  The Ubuntu pilot does not yet thread credentials through the
public surface — `certbot` reads its account credentials from
`/etc/letsencrypt/accounts/`, and self-signed openssl flows use
`-nodes` (no key passphrase).  Threading
`codecraftai-credentials.Credential` into the verb signatures is
deferred to the cross-package credential follow-up tracked in the
RHEL `3-certificate-issuance` task list.

The contract also enumerates `CertificateIssuer.CERTMONGER` as a
supported issuer on Ubuntu.  Ubuntu ships `certmonger` via apt but
it is not the dominant pathway in current Ubuntu deployments; the
pilot deliberately scopes to the `CERTBOT` and `OPENSSL` backends
to mirror the RHEL pilot's coverage on the Ubuntu surface.  Adding
a `CERTMONGER` backend is tracked as a follow-up task.

The `UbuntuRevocationReason` enum is surfaced for the certbot
revoke flow; the openssl backend returns `FAILED` with a
descriptive `detail` (no CRL machinery in scope for the pilot).
