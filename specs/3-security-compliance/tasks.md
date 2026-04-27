# Implementation Plan — Ubuntu Security Compliance

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.linux.ubuntu.securitycompliance`
  module

- [x] 2. Define `UbuntuSecurityComplianceActionStatus` enum (SUCCESS /
  FAILED / SKIPPED / DRY_RUN)

- [ ] 3. Define `UbuntuComplianceSeverity` enum (LOW / MEDIUM / HIGH /
  CRITICAL).  Deviation: severity is currently surfaced as a string on
  :class:`UbuntuComplianceFinding` for tolerance with the stdout
  parser.  An enum can be re-introduced once the XML reader lands.

- [x] 4. Define `UbuntuSecurityComplianceManagerConfig` dataclass
  (dry_run, elevated, datastream_path, output_dir).  Spec asks for a
  separate `baseline_path` and `results_dir`; both are computed from
  `output_dir` in the implementation (`<output_dir>/current_baseline.json`
  and `<output_dir>/results-<ts>.xml`).

- [x] 5. Define `UbuntuComplianceProfile`,
  `UbuntuComplianceFinding`, `UbuntuComplianceReport` dataclasses.
  `UbuntuComplianceCheck` is deferred — `oscap info` does not surface
  per-check detail.

- [x] 6. Define `UbuntuBaselineInfo` dataclass.  The `UbuntuBaselineSpec`
  / `UbuntuBaselineInfo` split in the design has been collapsed into a
  single `UbuntuBaselineInfo` shape used both for `set_baseline(spec)`
  input and `get_baseline()` output.

- [x] 7. Define `UbuntuSecurityComplianceActionResult` dataclass with
  `profile`, `finding_id`, `target`, and `message` extras.

- [x] 8. Define `UbuntuSecurityComplianceQueryError` exception (named
  per the package convention; the design referenced
  `UbuntuSecurityComplianceError`).

## Parsing Helpers

- [x] 9. Implement `_parse_oscap_info(stdout)` extracting `Title: ...
  Id: ...` pairs.

- [ ] 10. Implement `_parse_xccdf_results(xml_text, profile_name)` —
  not yet implemented.  The current `_parse_oscap_eval` is a tolerant
  stdout parser; the structured XML reader is left as a follow-up so
  the pilot does not block on a strict XML schema.

- [ ] 11. Implement `_map_severity(attr)` mapping
  `low|medium|high|critical` to `UbuntuComplianceSeverity` — pending
  the enum reintroduction.

- [ ] 12. Implement `_is_rule_remediable(datastream_xml, rule_id)` —
  pending the XML reader.

- [x] 13. Implement `<profile>:<rule>` finding-id construction (lives
  inline in `remediate`).

- [x] 14. Implement baseline JSON parsing in `get_baseline` with a
  tolerant fall-through (returns ``None`` on corrupt JSON instead of
  raising — see Spec Deviations).

## UbuntuSecurityComplianceManager — Implemented

- [x] 15. Implement `__init__` (inherited from `BaseFeatureManager`)
  accepting runner and optional config.  No `IFileOperator`
  collaborator — file I/O is performed via `cat` / `tee` against the
  injected `ICommandRunner`.

- [x] 16. Implement `list_profiles()` — `oscap info`, raises
  `UbuntuSecurityComplianceQueryError` on non-zero exit.

- [x] 17. Implement `run_compliance_profile(profile_name)` — dispatch
  `oscap xccdf eval --profile ... --results ... --report ...
  <datastream>`, accept exit 0 or 2, parse stdout into a report.

- [x] 18. Implement `remediate(finding_id)` — split `<profile>:<rule>`,
  reject missing profile, dispatch `oscap xccdf eval --remediate
  --profile ... --rule ... <datastream>`.

- [x] 19. Implement `get_baseline()` — read JSON via `cat`, decode
  into `UbuntuBaselineInfo`, return `None` on missing or corrupt.

- [x] 20. Implement `set_baseline(spec)` — idempotency via byte-for-byte
  JSON comparison, write via `tee` + stdin.

## UbuntuSecurityComplianceManager — Gaps

- [ ] 21. `get_profile(profile_name)` enumerate + filter.

- [ ] 22. `remediate_all(report)` — iterate findings, skip compliant,
  emit not-remediable FAILED results, call `remediate` otherwise.

- [ ] 23. Set-baseline orchestration: run profile, remediate_all, then
  write JSON only after every step succeeds.  Current implementation
  persists the baseline JSON directly.

- [ ] 24. DISA STIG remediation beyond datastream coverage — out of
  scope.

- [ ] 25. AppArmor profile authoring / enforcement — out of scope.

- [ ] 26. Signed / evidence-collection report output — out of scope.

- [ ] 27. Attestation via TPM / measured boot — out of scope.

- [ ] 28. Continuous-monitoring / agent-mode — out of scope.

- [ ] 29. Remediation rollback — out of scope.

## Tests — Implemented

- [x] 30. `test_list_profiles_constructs_oscap_info_command`
- [x] 31. `test_list_profiles_parses_titles_and_ids`
- [x] 32. `test_list_profiles_raises_on_error`
- [x] 33. `test_run_compliance_profile_constructs_eval_command`
- [x] 34. `test_run_compliance_profile_accepts_exit_two`
- [x] 35. `test_run_compliance_profile_raises_on_unexpected_exit`
- [x] 36. `test_run_compliance_profile_parses_multiple_findings`
- [x] 37. `test_compliance_finding_dataclass_fields`
- [x] 38. `test_remediate_constructs_command_with_profile_and_rule`
- [x] 39. `test_remediate_dry_run_short_circuits`
- [x] 40. `test_remediate_failed_when_profile_missing`
- [x] 41. `test_remediate_failed_propagates_stderr`
- [x] 42. `test_get_baseline_returns_none_when_file_missing`
- [x] 43. `test_get_baseline_decodes_json_payload`
- [x] 44. `test_set_baseline_writes_json_via_tee`
- [x] 45. `test_set_baseline_skipped_when_content_matches`
- [x] 46. `test_set_baseline_dry_run_short_circuits`
- [x] 47. `test_reads_never_elevate`
- [x] 48. `test_writes_pass_elevated_when_configured`

## Tests — Gaps

- [ ] 49. `remediate_all` ordering and not-remediable pass-through
  tests once that verb lands.

- [ ] 50. Property-based tests covering the design's Properties 1
  through 32.

- [ ] 51. Tests against an XCCDF results-XML fixture once the XML
  reader lands.

## Spec Deviations / Revisions Needed

- [ ] 52. **`IFileOperator` collaborator**: removed in favour of the
  package-wide `ICommandRunner`-only pattern.

- [ ] 53. **Stdout parser vs XML parser**: the current implementation
  builds findings from `oscap xccdf eval` stdout.  The design assumes
  the XCCDF results XML.  This is intentional for the pilot; the spec
  should call out the stdout fallback and flag XML parsing as a
  follow-up.

- [ ] 54. **Baseline corrupt-JSON behaviour**: design says raise; the
  implementation returns ``None`` to match the cross-OS pattern of
  treating missing/unparseable persistent state as "absent".  The
  contract should specify which behaviour wins.

- [ ] 55. **Severity enum**: surfaced as a string today.  Spec asks
  for an enum.  Adopt enum once the XML reader can populate it
  reliably.

## Follow-ups

- [ ] 56. **Cross-OS contract conformance review**: verify the data
  classes match the contract exactly and that finding shapes are
  OS-shaped only where the contract permits.

- [ ] 57. **Ubuntu-version datastream path default**: document a
  selection strategy for choosing the datastream when multiple
  Ubuntu versions ship in `/usr/share/xml/scap/ssg/content/`.

- [ ] 58. **Package dependency**: document that the Ubuntu package
  depends on `ssg-base` + `ssg-ubuntu` + `libopenscap8`.
