# Requirements

## Introduction

The certificate-trust-management feature provides the
`codecraftai-sysman-linux-ubuntu` package with the ability to manage the
host's CA trust posture on Ubuntu systems: installing and removing
trust anchors, listing the current trust set, retrieving structured
metadata for an individual certificate by SHA-256 fingerprint, and
refreshing the consolidated trust bundle so changes take effect for
TLS clients on the host.  This spec implements the
certificate-trust-management contract defined at
`codecraftai-sysman-common/specs/contracts/certificate-trust-management/contract.md`
for Ubuntu.

A maintenance coordinator drives all operations by calling into this
feature's public interfaces.  The feature does not decide *what* trust
posture the host enforces — it provides the capability to *apply*
posture reliably, idempotently, and with full auditability.  All
system commands (`update-ca-certificates`, `openssl`, `cat`, `tee`,
`ls`, `rm`) are executed exclusively through the
`codecraftai-interfaces` command-runner abstraction.  No `subprocess`,
no direct `open`, and no direct `pathlib` calls are made inside this
module.

The Ubuntu trust-anchor directory consumed by
`update-ca-certificates` is `/usr/local/share/ca-certificates/`;
files placed there MUST end in `.crt` to be picked up by the
consolidator.  The consolidated trust bundle that TLS clients consult
lives at `/etc/ssl/certs/ca-certificates.crt`.  Ubuntu does NOT
expose a per-user trust store equivalent to macOS's `login.keychain`;
the abstract `"user-ca"` store is therefore mapped to a
"not-supported" sentinel and writes return `FAILED`.

---

## Requirements

### Requirement 1: Add a Trusted Certificate

**User Story:** As a maintenance coordinator, I want to install a
PEM-encoded certificate as a trust anchor in a named store, so that
the host trusts certificates issued under that CA after the next
trust-store refresh.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `add_trusted_cert(store, path, purpose=None)` THE SYSTEM SHALL
   resolve the abstract `store` name to a directory path on disk via
   the configured `store_path_map` (defaulting to
   `"system-ca"` -> `/usr/local/share/ca-certificates`).
2. WHEN the resolved store directory is `None` (i.e. the store name is
   not supported on Ubuntu, including `"user-ca"`) THE SYSTEM SHALL
   return `UbuntuCertTrustActionResult` with `status=FAILED` and a
   `detail` describing the unsupported store, and SHALL NOT invoke the
   runner.
3. WHEN the source `path` cannot be read (the runner returns a
   non-zero exit on `cat <path>`) THE SYSTEM SHALL return
   `status=FAILED` with `detail="source path does not exist"`.
4. WHEN the source bytes do not contain a `-----BEGIN CERTIFICATE-----`
   block THE SYSTEM SHALL return `status=FAILED` with
   `detail="source file is not a valid PEM certificate"` and SHALL NOT
   invoke the `tee` write.
5. WHEN the SHA-256 fingerprint of the supplied certificate matches an
   existing file in the store directory THE SYSTEM SHALL return
   `status=SKIPPED` with `fingerprint` populated, `path` set to the
   pre-existing match, and SHALL NOT invoke the `tee` write.
6. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with
   `command=["tee", "<store-dir>/<basename>.crt"]` and SHALL NOT
   invoke the runner.
7. WHEN the new certificate is written THE SYSTEM SHALL build the
   destination filename from the basename of `path`, ensuring the
   suffix is `.crt` (translating `.pem` -> `.crt`, appending `.crt`
   when no extension is present).
8. WHEN `config.elevated=True` and the store is `"system-ca"` THE
   SYSTEM SHALL pass `elevated=True` to the runner for the `tee`
   write.
9. WHEN the write succeeds THE SYSTEM SHALL return `status=SUCCESS`
   with `fingerprint`, `path`, `command`, `exit_code`, `stdout`, and
   `stderr` populated.

---

### Requirement 2: Remove a Trusted Certificate

**User Story:** As a maintenance coordinator, I want to remove a
trust anchor by its SHA-256 fingerprint, so that revocation is
unambiguous and OS-independent.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `remove_trusted_cert(store, fingerprint)` THE SYSTEM SHALL resolve
   the abstract `store` name as in Requirement 1.
2. WHEN the store name is unsupported THE SYSTEM SHALL return
   `status=FAILED` and SHALL NOT invoke the runner.
3. WHEN no file in the store directory has the requested fingerprint
   THE SYSTEM SHALL return `status=SKIPPED` with `fingerprint`
   populated and SHALL NOT invoke `rm`.
4. WHEN a matching file exists THE SYSTEM SHALL run
   `rm -f <matching-file>` via the runner.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` with
   `command=["rm", "-f", "<matching-file>"]` and SHALL NOT invoke the
   runner.
6. WHEN `config.elevated=True` and the store is `"system-ca"` THE
   SYSTEM SHALL pass `elevated=True` to the runner.
7. WHEN the rm succeeds THE SYSTEM SHALL return `status=SUCCESS` with
   `fingerprint`, `path`, `command`, `exit_code`, `stdout`, and
   `stderr` populated.

---

### Requirement 3: List Trusted Certificates

**User Story:** As a maintenance coordinator, I want to enumerate the
trusted certificates in a named store, so that I can audit the
declared trust posture.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_trusted_certs(store)` and the
   store name is unsupported THE SYSTEM SHALL raise
   `UbuntuCertTrustQueryError` with the operation name and detail.
2. WHEN the store directory does not exist (the runner returns
   non-zero on `ls`) THE SYSTEM SHALL return an empty list.
3. WHEN files are present THE SYSTEM SHALL enumerate every file whose
   name ends in `.crt` or `.pem`, read each via `cat`, parse the first
   PEM block's fingerprint via pure-Python SHA-256, and parse subject
   / issuer / validity / serial / extended-key-usage via
   `openssl x509 -text`.
4. THE SYSTEM SHALL return a `list[UbuntuCertTrustInfo]` with each
   entry's `store` set to the supplied store name and `path` set to
   the file's absolute path.
5. WHEN `list_trusted_certs` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True` to the runner.

---

### Requirement 4: Get Certificate Details

**User Story:** As a maintenance coordinator, I want to retrieve
structured metadata for a single certificate by its fingerprint, so
that I can make decisions about replacement or expiration.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_cert_details(fingerprint)` THE
   SYSTEM SHALL search every supported store directory for a
   certificate whose SHA-256 fingerprint matches.
2. WHEN no matching certificate is found THE SYSTEM SHALL return
   `None`.
3. WHEN a match is found THE SYSTEM SHALL return a populated
   `UbuntuCertTrustInfo` with `fingerprint`, `subject`, `issuer`,
   `not_before`, `not_after`, `serial_number`, `store`, `path`, and
   `purposes`.
4. THE SYSTEM SHALL accept the fingerprint argument in colonised or
   non-colonised form, case-insensitive, and normalise to the Ubuntu
   uppercase-with-colons form before comparison.
5. WHEN `get_cert_details` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True` to the runner.

---

### Requirement 5: Update the Trust Store

**User Story:** As a maintenance coordinator, I want to refresh the
consolidated trust bundle, so that anchors added or removed take
effect for TLS clients on the host.

#### Acceptance Criteria

1. WHEN the coordinator calls `update_trust_store()` THE SYSTEM SHALL
   invoke `update-ca-certificates` via the runner.
2. WHEN the refresh succeeds THE SYSTEM SHALL return `status=SUCCESS`
   with `command=["update-ca-certificates"]` and `exit_code=0`.
3. WHEN the refresh fails THE SYSTEM SHALL return `status=FAILED`
   with `exit_code` and `stderr` populated.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
5. `update_trust_store` SHALL NOT skip; every non-dry-run call
   dispatches the refresh command.
6. WHEN `config.elevated=True` THE SYSTEM SHALL pass `elevated=True`
   to the runner.

---

### Requirement 6: Idempotency Across Add / Remove

**User Story:** As a maintenance coordinator, I want repeated `add` /
`remove` calls to converge to the same trust posture without
extraneous side effects.

#### Acceptance Criteria

1. WHEN `add_trusted_cert` is called with a certificate whose
   fingerprint is already present in the store THE SYSTEM SHALL
   return `status=SKIPPED` and dispatch zero `tee` commands.
2. WHEN `remove_trusted_cert` is called with a fingerprint not in the
   store THE SYSTEM SHALL return `status=SKIPPED` and dispatch zero
   `rm` commands.
3. THE SYSTEM SHALL NOT depend on filename equality for idempotency;
   matching is purely fingerprint-based so caller-renamed files are
   still recognised.

---

### Requirement 7: Trust-Store Mapping

**User Story:** As a maintenance coordinator, I want abstract store
names to map deterministically to Ubuntu-native paths, so that
cross-OS code can be written against the contract.

#### Acceptance Criteria

1. THE SYSTEM SHALL default `"system-ca"` to
   `/usr/local/share/ca-certificates`.
2. THE SYSTEM SHALL map `"user-ca"` to `None` (unsupported) by
   default.
3. THE SYSTEM SHALL accept a caller-supplied `store_path_map` that
   overrides any of the bundled defaults.
4. WHEN the override maps a known name to a directory path THE
   SYSTEM SHALL use that path for subsequent operations.
5. WHEN the override maps an unknown name to a directory path THE
   SYSTEM SHALL accept the new name and treat it like a system-style
   store (no per-user elevation override).

---

### Requirement 8: Fingerprint Normalisation

**User Story:** As a maintenance coordinator, I want fingerprint
arguments to be accepted in both colonised and non-colonised forms,
so that I can copy-paste from `openssl` output or from JSON payloads.

#### Acceptance Criteria

1. THE SYSTEM SHALL accept fingerprints in any of the following
   forms: `"AB:CD:..."`, `"abcd..."`, `"AB CD ..."`, with optional
   `"SHA256 Fingerprint="` or `"SHA256:"` prefixes.
2. THE SYSTEM SHALL normalise input to the uppercase-with-colons
   form before comparing against on-disk fingerprints.
3. THE SYSTEM SHALL emit fingerprints on result objects in the
   uppercase-with-colons form.

---

### Requirement 9: PEM Validation

**User Story:** As a maintenance coordinator, I want
`add_trusted_cert` to refuse invalid input rather than silently
write garbage, so that the trust bundle is never corrupted.

#### Acceptance Criteria

1. WHEN the source bytes do not contain a `-----BEGIN CERTIFICATE-----`
   / `-----END CERTIFICATE-----` envelope THE SYSTEM SHALL return
   `status=FAILED` and SHALL NOT invoke the `tee` write.
2. WHEN the PEM body fails to base64-decode THE SYSTEM SHALL return
   `status=FAILED` and SHALL NOT invoke the write.
3. WHEN multiple CERTIFICATE blocks are present in the source file
   THE SYSTEM SHALL fingerprint the first block; the file is written
   verbatim (multi-cert bundles are preserved).

---

### Requirement 10: Elevation Policy

**User Story:** As a maintenance coordinator, I want writes that
target system-owned trust stores to elevate, so that operations
succeed on standard hosts where the process does not run as root.

#### Acceptance Criteria

1. WHEN `config.elevated=True` and the targeted store is
   `"system-ca"` THE SYSTEM SHALL pass `elevated=True` to the runner
   for write verbs (`add_trusted_cert`, `remove_trusted_cert`).
2. WHEN `config.elevated=True` THE SYSTEM SHALL pass `elevated=True`
   to the runner for `update_trust_store`.
3. WHEN the targeted store is `"user-ca"` THE SYSTEM SHALL never
   elevate; on Ubuntu this case dispatches to `FAILED` before the
   runner is touched.
4. WHEN performing reads (`list_trusted_certs`, `get_cert_details`,
   internal `cat` / `ls`) THE SYSTEM SHALL pass `elevated=False`
   regardless of config.
5. WHEN `config.elevated=False` THE SYSTEM SHALL pass
   `elevated=False` on every write path.
