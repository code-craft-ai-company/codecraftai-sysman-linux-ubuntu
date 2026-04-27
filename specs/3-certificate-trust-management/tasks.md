# Implementation Plan — Certificate Trust Management (Ubuntu)

Checked items (`[x]`) are implemented and covered by tests.
Unchecked items (`[ ]`) identify out-of-scope or follow-up work.

## Module and Models

- [x] 1. Create the `certificatetrustmanagement` module under
  `source/packages/automojo/sysman/linux/ubuntu/`.

- [x] 2. Define `UbuntuCertTrustActionStatus` alias to the shared
  `ActionStatus` enum.

- [x] 3. Define `UbuntuCertTrustManagerConfig` dataclass:
  `dry_run`, `elevated`, `store_path_map`, `default_store`,
  `openssl_executable`, `update_executable`.

- [x] 4. Define `UbuntuCertTrustActionResult` dataclass extending
  `FeatureActionResult` with `store`, `fingerprint`, `path`.

- [x] 5. Define `UbuntuCertTrustInfo` dataclass with
  `fingerprint`, `subject`, `issuer`, `not_before`, `not_after`,
  `serial_number`, `store`, `path`, `purposes`.

- [x] 6. Define `UbuntuCertTrustQueryError` exception.

## UbuntuCertificateTrustManager — Implemented

- [x] 7. Implement `__init__` accepting runner + optional config.

- [x] 8. Implement `add_trusted_cert(store, path, purpose=None)`:
  - Resolves store -> directory through `store_path_map`.
  - Reads source via `cat`.
  - Validates PEM in pure Python.
  - Computes SHA-256 fingerprint in pure Python.
  - Skips when fingerprint already present in store directory.
  - Honours dry-run.
  - Writes via `tee` with stdin = source bytes.
  - Honours `elevated` for `system-ca` only.

- [x] 9. Implement `remove_trusted_cert(store, fingerprint)`:
  - Locates target via `ls` + `cat` + fingerprint comparison.
  - Skips when no match.
  - Honours dry-run.
  - Removes via `rm -f`.

- [x] 10. Implement `list_trusted_certs(store)`:
  - Raises `UbuntuCertTrustQueryError` for unsupported store.
  - Enumerates `.crt` / `.pem` files via `ls`.
  - Reads each via `cat`, fingerprints in pure Python.
  - Parses metadata via `openssl x509 -text`.
  - Reads never elevate.

- [x] 11. Implement `get_cert_details(fingerprint)`:
  - Searches every supported store.
  - Returns `None` for unknown fingerprint.
  - Reads never elevate.
  - Accepts colonised / non-colonised / prefixed input.

- [x] 12. Implement `update_trust_store()`:
  - Dispatches `update-ca-certificates`.
  - Honours dry-run and elevation.
  - Never skips.

## Helpers

- [x] 13. `_parse_pem_blocks` — split content into CERTIFICATE
  envelopes.

- [x] 14. `_compute_fingerprint` — pure-Python SHA-256 of base64-decoded
  PEM body, formatted with optional colons / case.

- [x] 15. `_normalize_fingerprint` — accept various input shapes,
  return canonical Ubuntu uppercase-with-colons form.

- [x] 16. `_normalize_destination_filename` — translate
  source basename to a `.crt`-suffixed filename.

- [x] 17. `_parse_openssl_cert_text` — extract subject / issuer /
  validity / serial / EKU from `openssl x509 -text` output.

## Tests — Implemented

- [x] 18. `test_add_trusted_cert_invokes_tee`.

- [x] 19. `test_add_trusted_cert_skipped_when_fingerprint_present`.

- [x] 20. `test_add_trusted_cert_dry_run_short_circuits`.

- [x] 21. `test_add_trusted_cert_invalid_pem_returns_failed`.

- [x] 22. `test_remove_trusted_cert_invokes_rm`.

- [x] 23. `test_remove_trusted_cert_skipped_when_absent`.

- [x] 24. `test_list_trusted_certs_parses_output`.

- [x] 25. `test_get_cert_details_returns_full_info`.

- [x] 26. `test_get_cert_details_unknown_fingerprint_returns_none`.

- [x] 27. `test_update_trust_store_invokes_update_ca_certificates`.

- [x] 28. `test_unsupported_store_returns_failed`.

- [x] 29. `test_unsupported_store_in_list_raises`.

- [x] 30. `test_writes_pass_elevated_true_for_system_store`.

- [x] 31. `test_user_store_writes_do_not_elevate`.

- [x] 32. `test_reads_never_elevate`.

- [x] 33. `test_failed_command_surfaces_stderr`.

## Out of Scope (Contract Surface Not Yet Implemented)

- [ ] 34. `set_store_enabled` — Ubuntu's
  `update-ca-certificates` has no enable/disable toggle.  Defer
  to a future wave or document as a permanent gap.

- [ ] 35. `verify_certificate` — overlaps with
  `automojo-credentials`; defer behind the cross-package reuse
  follow-up tracked in the RHEL spec's task 21.

- [ ] 36. `TrustPurpose` enum — Ubuntu's bundle is
  purpose-agnostic; the manager accepts a free-form `str | None`
  on `add_trusted_cert` for now.

## Follow-ups

- [ ] 37. **Java keystore integration** — `ca-certificates-java`
  layer.  Out of scope for the pilot; will be surfaced as a
  separate `"java-ca"` store in a follow-up wave.

- [ ] 38. **Bundle-only enumeration** — `list_trusted_certs`
  currently lists only the `/usr/local/share/ca-certificates/`
  anchor directory; system anchors baked into
  `/etc/ssl/certs/ca-certificates.crt` are not enumerated.  Add a
  `"system-bundle"` virtual store that parses the consolidated
  bundle when requested.

- [ ] 39. **Multi-block file fingerprinting** — currently the
  first PEM block determines the fingerprint of a multi-cert
  bundle; offer a `secondary_fingerprints` field on
  `UbuntuCertTrustInfo` for full coverage.
