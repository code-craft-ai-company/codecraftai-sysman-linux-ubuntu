# Implementation Plan — Ubuntu Log Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.linux.ubuntu.logmanagement` module

- [x] 2. Define `UbuntuLogActionStatus` enum (SUCCESS / FAILED / SKIPPED
  / DRY_RUN)

- [x] 3. Define `UbuntuLogManagerConfig` dataclass (`dry_run`,
  `elevated`, `logrotate_d`, `logrotate_conf`)

- [x] 4. Define `UbuntuLogEntry` / `UbuntuLogEntries` / `UbuntuLogInfo`
  / `UbuntuLogActionResult` dataclasses

- [x] 5. Define `UbuntuRotationPolicy` dataclass and
  `UbuntuLogQueryError` exception

## UbuntuLogManager — Implemented

- [x] 6. Implement `__init__` (inherited from `BaseFeatureManager`)
  accepting `runner` and optional `config`.  No `IFileOperator`
  collaborator: file I/O is performed via `cat` / `tee` / `test -e` /
  `ls -1` against the injected `ICommandRunner` (deviation from the
  draft design — see Spec Deviations below).

- [x] 7. Implement `_parse_one_journal_line` helper using the
  documented field fall-backs (`__REALTIME_TIMESTAMP`, `PRIORITY`,
  `_SYSTEMD_UNIT`, `SYSLOG_IDENTIFIER`, `MESSAGE`).

- [x] 8. Implement `get_log(name, lines, since)` building
  `journalctl -u <name> -n <lines> [--since <iso>] --no-pager -o json`
  and parsing one JSON object per line into a
  :class:`UbuntuLogEntries`.

- [x] 9. Implement `tail_log(name, follow)` as a generator that
  consumes the runner's stdout JSON lines.

- [x] 10. Implement `list_logs()` composing `systemctl list-units
  --type=service` plus `ls -1 <logrotate_d>`.

- [x] 11. Implement `rotate_log(name)` with drop-in vs fallback
  dispatch (`logrotate -f /etc/logrotate.d/<name>` when the drop-in
  exists, otherwise `logrotate -f /etc/logrotate.conf`).

- [x] 12. Implement `truncate_log(name)` via `journalctl
  --vacuum-size=1K`.

- [x] 13. Implement `configure_rotation(name, policy)` with idempotent
  render-and-write via `tee` + stdin.

- [x] 14. Implement `_render_logrotate` rendering deterministic body
  ending with a trailing newline so byte-for-byte comparison drives
  idempotency.

## UbuntuLogManager — Gaps (specified but not implemented)

- [ ] 15. Structured parsing of non-JSON `journalctl` output (kept as
  raw lines today).

- [ ] 16. rsyslog-specific facility routing (`syslog.facility:` prefix).

- [ ] 17. Plain-file truncate path (`truncate -s 0`) — current
  implementation focuses on the journalctl-managed surface.

- [ ] 18. `remove_rotation_config(name)` and `get_rotation_config(name)`
  parsing path — pending in this iteration.

- [ ] 19. `query_disk_usage(name)` unit + file dispatch.

- [ ] 20. Logrotate include-file authoring outside `/etc/logrotate.d/`.

## Tests — Implemented

- [x] 21. `test_get_log_constructs_journalctl_command`
- [x] 22. `test_get_log_includes_since_when_provided`
- [x] 23. `test_get_log_parses_journal_json_lines`
- [x] 24. `test_get_log_collects_unparseable_lines_as_raw`
- [x] 25. `test_get_log_raises_on_unexpected_error`
- [x] 26. `test_tail_log_yields_entries_for_finite_stream`
- [x] 27. `test_tail_log_follow_false_omits_dash_f_flag`
- [x] 28. `test_rotate_log_uses_dropin_when_present`
- [x] 29. `test_rotate_log_falls_back_to_default_conf`
- [x] 30. `test_rotate_log_dry_run_short_circuits`
- [x] 31. `test_truncate_log_invokes_journalctl_vacuum`
- [x] 32. `test_truncate_log_dry_run_short_circuits`
- [x] 33. `test_configure_rotation_writes_dropin`
- [x] 34. `test_configure_rotation_skipped_when_content_matches`
- [x] 35. `test_configure_rotation_dry_run_short_circuits`
- [x] 36. `test_configure_rotation_renders_size_token_when_provided`
- [x] 37. `test_list_logs_combines_systemd_and_logrotate_entries`
- [x] 38. `test_writes_pass_elevated_when_configured`
- [x] 39. `test_reads_never_elevate`
- [x] 40. `test_failed_command_returns_failed_result_with_stderr`
- [x] 41. `test_log_entry_dataclass_round_trip`

## Tests — Gaps

- [ ] 42. Parser robustness coverage on multi-line malformed JSON
  payloads.

- [ ] 43. Tests for `remove_rotation_config` / `get_rotation_config` /
  `query_disk_usage` once those verbs land.

## Spec Deviations / Revisions Needed

- [ ] 44. **`IFileOperator` collaborator**: the draft design names
  `IFileOperator` as a constructor argument.  Following the
  package-wide pattern set by `repositorymanagement.py` and
  `networkconfiguration.py`, this implementation routes every file
  read / write / probe through the `ICommandRunner` instead.  The
  spec should be revised to reflect that.

- [ ] 45. **Verb signatures**: the contract names verbs `get_log` /
  `tail_log` / `list_logs` / `rotate_log` / `truncate_log` /
  `configure_rotation`.  The drafted Ubuntu spec also enumerates
  `remove_rotation_config`, `get_rotation_config`, and
  `query_disk_usage`; these are deferred and should be marked as
  follow-up items (see gaps above).

## Follow-ups

- [ ] 46. **Cross-OS contract conformance review**: verify that the
  shipping dataclasses match the abstract names in
  `automojo-sysman-common/specs/contracts/log-management/contract.md`.

- [ ] 47. **automojo-sysman-linux-ubuntu reuse check**: confirm that
  `3-remote-log-forwarding` does not call `journalctl` directly; it
  should delegate to `UbuntuLogManager.get_log` for read paths.
