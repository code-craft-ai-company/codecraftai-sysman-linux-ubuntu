# Requirements

## Introduction

The certificate-issuance feature provides the
`codecraftai-sysman-linux-ubuntu` package with the ability to *issue*,
*renew*, *revoke*, *export*, *import*, *get*, and *list* X.509
certificates on Ubuntu hosts.  This spec implements the
certificate-issuance contract defined at
`codecraftai-sysman-common/specs/contracts/certificate-issuance/contract.md`
for Ubuntu.

A maintenance coordinator drives all operations by calling into this
feature's public interfaces.  The feature does not decide *which*
certificates the host should hold — it provides the capability to
*apply* a declared issuance posture reliably, idempotently, and with
full auditability.  All system commands (`certbot`, `openssl`, `cat`,
`tee`, `ls`) are executed exclusively through the
`codecraftai-interfaces` command-runner abstraction.  No `subprocess`,
no direct `open`, and no direct `pathlib` calls are made inside this
module.

The Ubuntu pilot supports two issuance backends, switchable per
manager via `UbuntuCertIssuanceManagerConfig.backend`:

- `UbuntuIssuanceBackend.CERTBOT` — ACME issuance via the system
  `certbot` binary (Let's Encrypt by default).  Default.
- `UbuntuIssuanceBackend.OPENSSL` — local self-signed / CSR-based
  issuance via `openssl`.

Both backends share the same public verb surface; the manager
dispatches at the verb level via internal `_dispatch_certbot` /
`_dispatch_openssl` helpers.

This feature complements — and does not duplicate — the trust-store
surface owned by `3-certificate-trust-management` (installing /
removing CA anchors).

---

## Requirements

### Requirement 1: Request a Certificate

**User Story:** As a maintenance coordinator, I want to request a
new X.509 certificate keyed by a caller-chosen alias, so that the
host can serve TLS with a configured issuer.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `request_certificate(spec)` AND `config.backend` is
   `UbuntuIssuanceBackend.CERTBOT` AND `spec.is_self_signed` is
   `False` THE SYSTEM SHALL invoke
   `certbot certonly --non-interactive --agree-tos --email <email>
   -d <common_name> [-d <san> ...] (--webroot -w <webroot> |
   --standalone) --cert-name <alias>` via the runner.
2. WHEN `config.backend` is `UbuntuIssuanceBackend.OPENSSL` OR
   `spec.is_self_signed` is `True` THE SYSTEM SHALL invoke
   `openssl req -newkey <algorithm>:<bits> -nodes -keyout <keyout>
   -out <csrout> -subj '/CN=<common_name>'` followed by
   `openssl x509 -req -days <validity_days> -in <csrout> -signkey
   <keyout> -out <certout>` for the self-signed branch.
3. WHEN the alias already has a certificate present in the
   configured cert directory AND its remaining validity is greater
   than `config.renew_threshold_days` THE SYSTEM SHALL return
   `status=SKIPPED` with `alias`, `subject`, and `fingerprint`
   populated, and SHALL NOT dispatch any issuance command.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with
   the would-have-been `command` populated and SHALL NOT invoke the
   runner.
5. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner for the issuance command.
6. WHEN the issuance command exits non-zero THE SYSTEM SHALL return
   `status=FAILED` with `exit_code`, `stdout`, `stderr`, and
   `command` populated.
7. THE SYSTEM SHALL never place issuance secrets (certbot account
   credentials, OpenSSL passphrases) on argv; any secret bytes
   SHALL flow on `stdin`.

---

### Requirement 2: Renew a Certificate

**User Story:** As a maintenance coordinator, I want to renew an
existing certificate by alias, so that scheduled refreshes happen
without re-stating the original issuance spec.

#### Acceptance Criteria

1. WHEN `config.backend` is `UbuntuIssuanceBackend.CERTBOT` THE
   SYSTEM SHALL invoke
   `certbot renew --cert-name <alias> --non-interactive`.
2. WHEN `config.backend` is `UbuntuIssuanceBackend.OPENSSL` THE
   SYSTEM SHALL re-issue a self-signed certificate against the
   stored key and CSR via `openssl x509 -req -days
   <validity_days> -in <csrout> -signkey <keyout> -out <certout>`.
3. WHEN the alias's existing certificate's remaining validity is
   greater than `config.renew_threshold_days` THE SYSTEM SHALL
   return `status=SKIPPED`.
4. WHEN the alias is not known THE SYSTEM SHALL return
   `status=FAILED` with `detail="alias not found"`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.

---

### Requirement 3: Revoke a Certificate

**User Story:** As a maintenance coordinator, I want to revoke an
existing certificate by alias with an optional reason code, so that
compromised material is taken out of service.

#### Acceptance Criteria

1. WHEN `config.backend` is `UbuntuIssuanceBackend.CERTBOT` THE
   SYSTEM SHALL invoke
   `certbot revoke --cert-name <alias> --non-interactive` with
   an optional `--reason <reason>` flag when the caller supplies
   one.
2. WHEN `config.backend` is `UbuntuIssuanceBackend.OPENSSL` THE
   SYSTEM SHALL return `status=FAILED` with a descriptive `detail`
   noting that local-CA revocation is out of scope for the pilot
   (no CRL machinery in the OpenSSL backend).
3. WHEN the alias is not present in the configured cert directory
   THE SYSTEM SHALL return `status=SKIPPED`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. THE SYSTEM SHALL accept the reason via the
   `UbuntuRevocationReason` enum and forward its lowercase string
   value to certbot's `--reason` flag.

---

### Requirement 4: Get a Certificate

**User Story:** As a maintenance coordinator, I want to fetch
structured metadata for a single certificate by alias, so that
compliance reports can summarise the installed PKI posture.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_certificate(alias)` THE SYSTEM
   SHALL parse the corresponding certificate file via
   `openssl x509 -in <path> -noout -text -fingerprint -sha256
   -dates -subject -issuer -serial`.
2. WHEN no certificate exists for the alias THE SYSTEM SHALL return
   `None` (rather than raise).
3. WHEN parsing fails (the openssl command exits non-zero) THE
   SYSTEM SHALL raise `UbuntuCertIssuanceQueryError`.
4. WHEN the call succeeds THE SYSTEM SHALL return
   `UbuntuCertificateInfo` populated with `alias`, `subject`,
   `issuer`, `not_before`, `not_after`, `fingerprint`,
   `serial_number`, `key_algorithm`, `key_length`, `sans`, `path`,
   `is_self_signed`.
5. THE SYSTEM SHALL NOT pass `elevated=True` to the runner.

---

### Requirement 5: List Certificates

**User Story:** As a maintenance coordinator, I want to enumerate
every certificate this manager owns, so that scheduled audits can
walk the full set.

#### Acceptance Criteria

1. WHEN `config.backend` is `UbuntuIssuanceBackend.CERTBOT` THE
   SYSTEM SHALL invoke `certbot certificates` and parse the output
   into one `UbuntuCertificateInfo` per certificate block.
2. WHEN `config.backend` is `UbuntuIssuanceBackend.OPENSSL` THE
   SYSTEM SHALL enumerate `config.cert_dir` via `ls -1`, run
   `openssl x509 -in <path> -noout -text` for each `.pem` / `.crt`
   file, and return one `UbuntuCertificateInfo` per file.
3. WHEN the cert directory does not exist THE SYSTEM SHALL return
   an empty list.
4. WHEN parsing of any single file fails THE SYSTEM SHALL skip the
   file and continue (a single bad file SHALL NOT abort the list).
5. THE SYSTEM SHALL NOT pass `elevated=True` to the runner.

---

### Requirement 6: Export a Certificate

**User Story:** As a maintenance coordinator, I want to export a
certificate (and optionally its key) to a filesystem path, so that
upstream consumers can pick up the freshly-issued material.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `export_certificate(alias, path, include_private_key=False)` THE
   SYSTEM SHALL read the source certificate via `cat <src>` and
   write it to `<path>` via `tee <path>` with the bytes on stdin.
2. WHEN `include_private_key=True` THE SYSTEM SHALL concatenate the
   key PEM and certificate PEM into a single bundle and write that
   to `<path>` (PEM key + cert is the Ubuntu / Linux convention).
3. WHEN the alias is unknown THE SYSTEM SHALL return
   `status=FAILED` with `detail="alias not found"`.
4. WHEN the destination path already contains the same byte
   content (compared via `cmp <src> <dest>` or sha256) THE SYSTEM
   SHALL return `status=SKIPPED`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
6. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner for the `tee` write.
7. THE SYSTEM SHALL never include private-key bytes in the recorded
   `command` field.

---

### Requirement 7: Import a Certificate

**User Story:** As a maintenance coordinator, I want to import an
externally-issued certificate under a chosen alias, so that
unmanaged material can be brought under the manager's control.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `import_certificate(path, alias)` THE SYSTEM SHALL read the
   source via `cat`, validate it is a PEM certificate, compute its
   SHA-256 fingerprint in pure Python, and write the bytes to
   `<config.cert_dir>/<alias>.pem` via `tee`.
2. WHEN a certificate with the same fingerprint is already
   registered under the alias THE SYSTEM SHALL return
   `status=SKIPPED`.
3. WHEN the source file is not a valid PEM certificate THE SYSTEM
   SHALL return `status=FAILED`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.

---

### Requirement 8: Backend Selection

**User Story:** As a maintenance coordinator, I want to switch
between certbot and openssl issuance backends without changing the
manager's verb surface, so that the same code drives both
publicly-trusted ACME issuance and local self-signed development
issuance.

#### Acceptance Criteria

1. THE SYSTEM SHALL expose `UbuntuIssuanceBackend.CERTBOT` and
   `UbuntuIssuanceBackend.OPENSSL` enum members.
2. THE SYSTEM SHALL default `config.backend` to
   `UbuntuIssuanceBackend.CERTBOT`.
3. WHEN dispatching a write verb THE SYSTEM SHALL select the
   backend at the verb level via `_dispatch_certbot` /
   `_dispatch_openssl` private helpers; the public verbs SHALL NOT
   contain backend-specific branching beyond the dispatch call.
4. WHEN `spec.is_self_signed` is `True` AND the configured backend
   is `CERTBOT`, the request verb SHALL nevertheless dispatch via
   the openssl branch (self-signed implies local issuance).

---

### Requirement 9: Idempotency

**User Story:** As a maintenance coordinator, I want repeated
calls with identical posture to converge to the same result
without extraneous reissuance, so that scheduled runs are safe.

#### Acceptance Criteria

1. `request_certificate` SHALL return `SKIPPED` when the alias is
   already present and not within `renew_threshold_days` of expiry.
2. `renew_certificate` SHALL return `SKIPPED` under the same
   threshold.
3. `revoke_certificate` SHALL return `SKIPPED` when the alias is
   already absent.
4. `import_certificate` SHALL return `SKIPPED` when the alias
   already maps to a certificate with the same SHA-256
   fingerprint.
5. `export_certificate` SHALL return `SKIPPED` when the destination
   path already byte-matches the source.

---

### Requirement 10: Elevation, Dry-Run, Error Reporting

**User Story:** Standard cross-feature semantics.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch any
   mutating command (certbot, openssl req/x509, tee, rm).
2. WHEN `config.elevated=True` THE SYSTEM SHALL dispatch mutating
   commands elevated.  Reads SHALL NOT elevate.
3. WHEN any dispatched command fails THE SYSTEM SHALL return
   `status=FAILED` with `exit_code`, `stdout`, `stderr`, and
   `command` populated.
4. THE SYSTEM SHALL NEVER include passphrase / credential bytes in
   the recorded `command` field.

---

### Requirement 11: Configuration Surface

**User Story:** As a maintenance coordinator, I want every Ubuntu-
specific knob (binary names, cert directory, ACME email, renew
threshold, backend selection) to live on a single config dataclass.

#### Acceptance Criteria

1. THE SYSTEM SHALL expose
   `UbuntuCertIssuanceManagerConfig(FeatureConfig)` with fields:
   `backend: UbuntuIssuanceBackend = CERTBOT`,
   `certbot_cli: str = "certbot"`,
   `openssl_cli: str = "openssl"`,
   `cert_dir: str = "/etc/letsencrypt/live"`,
   `acme_email: str | None = None`,
   `acme_method: str = "webroot"` (or `"standalone"`),
   `webroot: str | None = None`,
   `renew_threshold_days: int = 30`.
2. THE SYSTEM SHALL inherit `dry_run` and `elevated` from the base
   `FeatureConfig`.
