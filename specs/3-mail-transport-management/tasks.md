# Implementation Plan

- [x] 1. Wire the module skeleton
  - Create `automojo/sysman/linux/ubuntu/mailtransportmanagement.py`
    with the module docstring, the verb / tool mapping table, and
    the imports of `ActionStatus`, `BaseFeatureManager`,
    `FeatureActionResult`, and `FeatureConfig` from `featurebase`.
  - Define module-level constants: `_CAT`, `_TEE`, `_POSTCONF`,
    `_POSTQUEUE`, `_NEWALIASES`, `_MAILQ`, `_SENDMAIL`,
    `_SYSTEMCTL`, `_DEFAULT_ALIASES_FILE`, `_DEFAULT_SERVICE_NAME`,
    `_KNOWN_MAIN_KEYS`, `_INT_KEYS`.
  - Alias `UbuntuMtaActionStatus = ActionStatus`.
  - _Requirements: -_

- [x] 2. Define data models
  - `UbuntuMtaManagerConfig(FeatureConfig)` with `aliases_file`,
    `service_name`, `reload_after_set` defaults.
  - `UbuntuMtaActionResult(FeatureActionResult)` with `setting` and
    `local`.
  - `UbuntuMtaConfig` with `myhostname`, `mydomain`, `myorigin`,
    `relayhost`, `mynetworks`, `inet_interfaces`, `inet_protocols`,
    `mailbox_size_limit`, `message_size_limit`, `extras`.
  - `UbuntuMtaAliasInfo` with `local` and `remote`.
  - `UbuntuMtaQueueStatus` with `pending_count`, `deferred_count`,
    `total_size_bytes`, `queue_age_oldest`.
  - `UbuntuMtaQueryError(Exception)` carrying `command`,
    `exit_code`, `stderr`.
  - _Requirements: 1, 2, 3, 4, 5, 7_

- [x] 3. Implement read verbs
  - `get_mta_config()` — invoke `postconf -n`, parse via
    `_parse_postconf_n_output`, raise `UbuntuMtaQueryError` on
    failure.
  - `list_aliases()` — `cat /etc/aliases` + parse via
    `_parse_aliases_file`.
  - `get_queue_status()` — `mailq` + parse via
    `_parse_mailq_output`.
  - All three read verbs MUST pass `elevated=False`.
  - _Requirements: 1, 5, 7_

- [x] 4. Implement `set_mta_config`
  - Honour dry-run.
  - Probe live config via `get_mta_config()`.
  - Walk every named field on `spec` (skipping `None`s) plus every
    entry in `spec.extras`; per-setting compare and dispatch
    `postconf -e <key>=<value>`.
  - Aggregate result: `SKIPPED` when nothing changed, `SUCCESS`
    when at least one directive changed and the optional reload
    succeeded, `FAILED` when any per-directive dispatch or the
    reload failed.
  - Trail `systemctl reload <service_name>` after at least one
    successful change AND `config.reload_after_set=True`.
  - Set `stdout` to a newline-separated audit trail
    (`SKIPPED|CHANGED|FAILED|RELOAD ...`).
  - _Requirements: 2, 9, 10, 11_

- [x] 5. Implement alias write verbs
  - `add_alias(local, remote)` — read, parse, skip when matched,
    upsert via `_upsert_alias`, write via `tee`, chain
    `newaliases`.
  - `remove_alias(local)` — read, skip when missing/absent,
    rewrite via `_remove_alias`, write via `tee`, chain
    `newaliases`.
  - Use `_invoke_newaliases` to surface `FAILED` from the chained
    rebuild.
  - Honour dry-run; honour `elevated`.
  - _Requirements: 3, 4, 9, 10, 11_

- [x] 6. Implement operational write verbs
  - `test_mail_delivery(to, from_addr, subject)` — build payload
    via `_build_probe_message`, dispatch `sendmail -t` with stdin.
  - `flush_queue()` — dispatch `postqueue -f`.
  - Both verbs MUST NOT skip; both honour dry-run.
  - _Requirements: 6, 8, 9, 10, 11_

- [x] 7. Implement parsing helpers
  - `_parse_postconf_n_output` — line-by-line `key = value`,
    split on first `=`.
  - `_apply_directive` — place a directive into the named field
    or `extras`; coerce integer types.
  - `_config_to_directive_pairs` + `_DirectivePairs` — flatten
    config to ordered key/value pairs with `get_first_value_for`.
  - `_parse_aliases_file` — line-by-line, split on first `:`,
    fold continuations.
  - `_upsert_alias` / `_remove_alias` — render new content from
    existing.
  - `_parse_mailq_output` — head-line regex, footer regex,
    empty-queue special string.
  - `_build_probe_message` — RFC 5322 payload builder.
  - _Requirements: 1, 5, 7_

- [x] 8. Author requirements doc
  - Eleven requirements covering reads, writes, idempotency, dry-run,
    elevation, and failure reporting.
  - _Requirements: -_

- [x] 9. Author design doc
  - Architecture diagram, configuration, data models, MTA
    mapping, key method designs, idempotency model, dry-run /
    elevation policy, correctness properties, testing strategy,
    seams.
  - _Requirements: -_

- [x] 10. Author test module (>= 14 tests)
  - `test_get_mta_config_parses_postconf_output`.
  - `test_get_mta_config_raises_on_failure_with_stderr`.
  - `test_set_mta_config_invokes_postconf`.
  - `test_set_mta_config_skipped_per_setting_when_match`.
  - `test_set_mta_config_dry_run_short_circuits`.
  - `test_add_alias_invokes_tee_and_newaliases`.
  - `test_add_alias_skipped_when_present`.
  - `test_add_alias_dry_run_short_circuits`.
  - `test_remove_alias_invokes_tee_and_newaliases`.
  - `test_remove_alias_skipped_when_absent`.
  - `test_list_aliases_parses_etc_aliases`.
  - `test_list_aliases_returns_empty_when_file_missing`.
  - `test_test_mail_delivery_invokes_sendmail`.
  - `test_test_mail_delivery_dry_run_short_circuits`.
  - `test_get_queue_status_parses_mailq`.
  - `test_get_queue_status_empty_queue`.
  - `test_flush_queue_invokes_postqueue`.
  - `test_writes_elevate`.
  - `test_reads_never_elevate`.
  - `test_failed_command_surfaces_stderr`.
  - `test_add_alias_failed_when_newaliases_fails`.
  - Module-level parser sanity tests for postconf / aliases / mailq.
  - _Requirements: 1-11_

- [ ] 11. Follow-ups (not part of this wave)
  - Detect the live MTA (`postconf` vs `exim4` vs `sendmail`) and
    dispatch to the appropriate command family.  The pilot
    targets Postfix exclusively.
  - Add `set_mta_config` "directives to remove" surface that
    invokes `postconf -X <key>` for fields callers want reverted
    to defaults.
  - Per-directive reload-vs-restart classifier (e.g.,
    `mailbox_size_limit` requires `restart`).
  - Surface `mailq -j` / `postqueue -j` JSON output when running
    against Postfix 3.6+ for richer queue introspection (size,
    arrival, errors per message).
  - Drop-in directive support for `/etc/postfix/main.cf.d/*.cf`
    once Postfix supports it natively.
  - Cross-feature integration: emit a `MaintenanceReport` row per
    write verb once the Ubuntu package adopts the report bus.
