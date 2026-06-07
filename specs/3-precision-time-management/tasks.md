# Implementation Plan — Ubuntu Precision-Time Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify follow-ups deferred to a later iteration.

## Module and Models

- [x] 1. Create the
  `ccai.sysman.linux.ubuntu.precisiontimemanagement` module.

- [x] 2. Define `UbuntuPrecisionTimeActionStatus` (alias of
  `ActionStatus`).

- [x] 3. Define `UbuntuTimeBackend` enum (`CHRONY`, `TIMESYNCD`,
  `NTPD`).

- [x] 4. Define `UbuntuPrecisionTimeManagerConfig` dataclass with
  `dry_run`, `elevated`, `backend`, `chrony_conf`,
  `timesyncd_conf`, `service_name`.

- [x] 5. Define `UbuntuPrecisionTimeActionResult` dataclass with
  the `server` carry-through field.

- [x] 6. Define `UbuntuTimeStatus`, `UbuntuTimeSourceInfo`,
  `UbuntuTimeDrift` dataclasses.

- [x] 7. Define `UbuntuPrecisionTimeQueryError` exception.

## UbuntuPrecisionTimeManager — Implemented

- [x] 8. Implement `__init__` accepting runner + optional config.
  Spec deviation: no `IFileOperator` collaborator — file I/O goes
  through the runner via `cat` / `tee` to match the locked-in
  pattern of every other Ubuntu feature.

- [x] 9. Implement `get_time_status()` for both backends — chrony
  parses `chronyc tracking`; timesyncd parses
  `timedatectl status`.  Daemon-down stderr signatures degrade
  to `enabled=False` for chrony.

- [x] 10. Implement `get_time_sources()` for both backends —
  chrony parses `chronyc sources` columnar output; timesyncd
  parses the `timedatectl show --property=...` block.

- [x] 11. Implement `get_drift()` for both backends — chrony
  parses `chronyc tracking` offset/frequency/jitter; timesyncd
  parses the `NTPMessage={ ... }` blob.

- [x] 12. Implement `add_time_source(server)` — `cat` config,
  parse current servers, append, `tee`, restart service;
  idempotent via membership check.

- [x] 13. Implement `remove_time_source(server)` — `cat`,
  parse, remove, `tee`, restart; idempotent via membership
  check.

- [x] 14. Implement `set_time_servers(servers)` — rewrite the
  full `pool`/`NTP=` block; idempotent via sorted-list
  comparison.

- [x] 15. Implement `force_sync_now()` — `chronyc makestep` for
  chrony; `systemctl restart systemd-timesyncd` for timesyncd.

- [x] 16. Implement `enable_sync()` / `disable_sync()` — chrony
  uses `systemctl enable/disable --now`; timesyncd uses
  `timedatectl set-ntp true|false`; idempotent via the active-
  state probe.

## Renderers and Parsers — Implemented

- [x] 17. `_parse_chrony_tracking_status` — keyed-line parser
  for `Reference ID`, `Stratum`, `Ref time`, `Last offset`,
  `Leap status`.

- [x] 18. `_parse_chrony_tracking_drift` — keyed-line parser for
  `Last offset`, `RMS offset`, `Frequency`, `Stratum`.

- [x] 19. `_parse_chrony_sources` — columnar parser including
  the chrony `MS` token classification onto
  selected/candidate/unreachable.

- [x] 20. `_parse_chrony_sample_offset` — handles
  `±<num>{ns,us,ms,s}` tokens.

- [x] 21. `_parse_chrony_frequency_ppm` — handles the
  `slow`/`fast` direction token.

- [x] 22. `_parse_timedatectl_status` — keyed-line parser for
  the `timedatectl status` payload.

- [x] 23. `_parse_timedatectl_show_sources` — combines
  `SystemNTPServers` + `FallbackNTPServers` + `ServerName` into
  one selected entry plus candidates.

- [x] 24. `_parse_timedatectl_show_drift` — extracts `Offset`
  and `Jitter` from the `NTPMessage={ ... }` blob.

- [x] 25. `_parse_chrony_conf_servers` — extracts
  `server`/`pool` declarations from a chrony config body.

- [x] 26. `_render_chrony_conf_servers` — replaces the
  `server`/`pool` block with one `pool <s> iburst` line per
  requested server, preserving all unrelated lines.

- [x] 27. `_parse_timesyncd_conf_servers` — extracts the
  active `NTP=` value out of a timesyncd.conf body.

- [x] 28. `_render_timesyncd_conf_servers` — rewrites the
  `NTP=` line under the `[Time]` section, creating the section
  header when absent.

- [x] 29. `_stderr_indicates_inactive` — heuristic for the
  chrony "daemon not running" stderr signatures.

## Tests

- [x] 30. `test_get_time_status_parses_chronyc_output`
- [x] 31. `test_get_time_status_returns_inactive_when_chronyd_down`
- [x] 32. `test_get_time_sources_parses_chronyc_output`
- [x] 33. `test_get_drift_parses_chronyc_output`
- [x] 34. `test_get_time_status_parses_timedatectl_output`
- [x] 35. `test_get_time_sources_parses_timedatectl_output`
- [x] 36. `test_get_drift_parses_timedatectl_output`
- [x] 37. `test_add_time_source_invokes_chronyc`
- [x] 38. `test_add_time_source_skipped_when_present`
- [x] 39. `test_add_time_source_dry_run`
- [x] 40. `test_remove_time_source_invokes_chronyc`
- [x] 41. `test_remove_time_source_skipped_when_absent`
- [x] 42. `test_set_time_servers_invokes_chronyc`
- [x] 43. `test_set_time_servers_skipped_when_match`
- [x] 44. `test_force_sync_now_invokes_chronyc`
- [x] 45. `test_enable_sync_skipped_when_already_enabled`
- [x] 46. `test_disable_sync_invokes_systemctl`
- [x] 47. `test_writes_elevate`
- [x] 48. `test_reads_never_elevate`
- [x] 49. `test_failed_command_surfaces_stderr`
- [x] 50. `test_get_time_status_raises_on_unexpected_chronyc_failure`

## Follow-ups

- [ ] 51. Implement the `NTPD` backend.  The enum value is
  reserved; the manager raises `NotImplementedError` until the
  branch is filled in.
- [ ] 52. Replace string `last_sync` with a parsed `datetime`
  once a shared timezone-handling helper lands in the package.
- [ ] 53. Surface a unified diff on dry-run results so callers
  can audit the would-have-been content change.
- [ ] 54. Delegate service lifecycle to an injected
  `ServiceManager` once that abstraction stabilises in this
  package; the current implementation issues `systemctl`
  directly to keep the surface self-contained.
