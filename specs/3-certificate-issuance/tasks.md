# Implementation Plan — Certificate Issuance (Ubuntu)

Checked items (`[x]`) are implemented and covered by tests.
Unchecked items (`[ ]`) identify out-of-scope or follow-up work.

## Module and Models

- [x] 1. Create the `certificateissuance` module under
  `source/packages/ccai/sysman/linux/ubuntu/`.

- [x] 2. Define `UbuntuCertIssuanceActionStatus` alias to the shared
  `ActionStatus` enum.

- [x] 3. Define `UbuntuIssuanceBackend` enum with `CERTBOT` and
  `OPENSSL` members.

- [x] 4. Define `UbuntuRevocationReason` enum with `UNSPECIFIED`,
  `KEY_COMPROMISE`, `CA_COMPROMISE`, `AFFILIATION_CHANGED`,
  `SUPERSEDED`, `CESSATION_OF_OPERATION`.

- [x] 5. Define `UbuntuCertIssuanceManagerConfig` dataclass:
  `dry_run`, `elevated`, `backend`, `certbot_cli`, `openssl_cli`,
  `cert_dir`, `acme_email`, `acme_method`, `webroot`,
  `renew_threshold_days`.

- [x] 6. Define `UbuntuCertIssuanceActionResult` dataclass extending
  `FeatureActionResult` with `alias`, `subject`, `fingerprint`.

- [x] 7. Define `UbuntuCertificateInfo` dataclass with `alias`,
  `subject`, `issuer`, `not_before`, `not_after`, `fingerprint`,
  `serial_number`, `key_algorithm`, `key_length`, `sans`, `path`,
  `is_self_signed`.

- [x] 8. Define `UbuntuCertificateRequestSpec` dataclass with
  `alias`, `common_name`, `email`, `sans`, `key_algorithm`,
  `key_length`, `validity_days`, `is_self_signed`, `webroot`.

- [x] 9. Define `UbuntuCertIssuanceQueryError` exception.

## UbuntuCertificateIssuanceManager — Implemented

- [x] 10. Implement `__init__` accepting runner + optional config.

- [x] 11. Implement `request_certificate(spec)`:
  - Renew-threshold guard via `_days_until`.
  - Dry-run honoured.
  - Backend dispatch via `_dispatch_certbot_request` /
    `_dispatch_openssl_request`.
  - Self-signed override forces openssl branch.
  - Elevation honoured for writes.

- [x] 12. Implement `renew_certificate(alias)`:
  - Renew-threshold guard.
  - Backend dispatch via `_dispatch_certbot_renew` /
    `_dispatch_openssl_renew`.
  - Alias-not-found returns FAILED.

- [x] 13. Implement `revoke_certificate(alias, reason=None)`:
  - Backend dispatch via `_dispatch_certbot_revoke` /
    `_dispatch_openssl_revoke` (the latter returns FAILED).
  - Alias-not-found returns SKIPPED.
  - Dry-run honoured.

- [x] 14. Implement `get_certificate(alias)`:
  - Resolve alias to a path under `config.cert_dir`.
  - Return `None` for unknown alias.
  - Parse via shared `_parse_openssl_x509_text` helper.
  - Reads do not elevate.

- [x] 15. Implement `list_certificates()`:
  - Backend dispatch via `_dispatch_certbot_list` /
    `_dispatch_openssl_list`.
  - Reads do not elevate.

- [x] 16. Implement `export_certificate(alias, path,
  include_private_key=False)`:
  - Idempotency: read the destination and compare it with the content
    to write.
  - Concatenates key+cert when `include_private_key=True`.
  - `tee <dest>` write with bytes on stdin.
  - Dry-run + elevation honoured.

- [x] 17. Implement `import_certificate(path, alias)`:
  - PEM validation in pure Python.
  - Fingerprint-based idempotency.
  - `tee <cert_dir>/<alias>.pem` write.
  - Dry-run + elevation honoured.

## Helpers

- [x] 18. `_parse_pem_blocks` — split into CERTIFICATE envelopes.

- [x] 19. `_compute_fingerprint` — pure-Python SHA-256 of PEM body.

- [x] 20. `_parse_openssl_x509_text` — extract subject / issuer /
  validity / serial / SANs / key info from `openssl x509 -text`.

- [x] 21. `_parse_certbot_certificates_output` — parse `certbot
  certificates` blocks.

- [x] 22. `_days_until` — parse openssl `notAfter` and compute days
  to expiry.

- [x] 23. `_format_subject` / `_format_san_args` — assemble openssl /
  certbot CLI arguments from a request spec.

## Tests — Implemented

- [x] 24. `test_request_certificate_invokes_certbot_with_subject`.

- [x] 25. `test_request_certificate_skipped_when_present_and_not_near_expiry`.

- [x] 26. `test_request_certificate_dry_run_short_circuits`.

- [x] 27. `test_request_certificate_self_signed_uses_openssl`.

- [x] 28. `test_renew_certificate_invokes_certbot`.

- [x] 29. `test_renew_certificate_skipped_when_not_near_expiry`.

- [x] 30. `test_revoke_certificate_invokes_certbot`.

- [x] 31. `test_revoke_certificate_with_reason`.

- [x] 32. `test_get_certificate_parses_x509_output`.

- [x] 33. `test_list_certificates_returns_all`.

- [x] 34. `test_export_certificate_writes_to_path`.

- [x] 35. `test_export_with_private_key_uses_pem`.

- [x] 36. `test_import_certificate_skipped_when_fingerprint_present`.

- [x] 37. `test_writes_elevate`.

- [x] 38. `test_reads_never_elevate`.

- [x] 39. `test_failed_command_surfaces_stderr`.

## Out of Scope (Contract Surface Not Yet Implemented)

- [ ] 40. `Credential` parameter on write verbs — defer until the
  cross-package `codecraftai-credentials` integration lands.

- [ ] 41. `CERTMONGER` backend — Ubuntu ships `certmonger` via apt
  but the pilot scopes to `CERTBOT` + `OPENSSL`.  Add a
  `_dispatch_certmonger_*` family in a follow-up wave.

- [ ] 42. CSR-only flow — currently the openssl backend always
  produces a CSR + self-signed cert pair.  Expose a CSR-only path
  that emits the CSR to the caller for offline CA submission.

## Follow-ups

- [ ] 43. **DNS-01 ACME challenge** — currently the certbot backend
  supports `webroot` and `standalone` only.  Add a `dns-<plugin>`
  method matching the RHEL spec.

- [ ] 44. **CRL generation for openssl backend** — would let
  `revoke_certificate` produce a usable CRL on the openssl branch.

- [ ] 45. **Multi-cert bundle import** — currently
  `import_certificate` accepts the first PEM block only; offer a
  `secondary_fingerprints` field for chain handling.
