# Requirements

## Introduction

The remote-log-forwarding feature provides the
`codecraftai-sysman-linux-ubuntu` package with the ability to ship
syslog traffic off-host through `rsyslog` forward drop-ins under
`/etc/rsyslog.d/`.  It is the cross-OS counterpart to the local
log-shaping surface owned by `3-log-management` and the
RHEL-canonical `3-remote-log-forwarding` feature.

Ubuntu ships `rsyslog` as the default syslog daemon (as of every
LTS release the package ships the same `rsyslogd` binary that RHEL
uses), so the rsyslog drop-in pattern is the only mechanism this
package targets.  When operators have replaced `rsyslog` with
`syslog-ng` the manager surfaces the discrepancy via `FAILED`
results from the post-write `systemctl restart` step, which is
captured in `tasks.md` as a follow-up.

All system commands (`systemctl restart rsyslog`, `logger`, `tee`,
`rm`, `mv`, `ls`, `cat`, `test -e`) are dispatched through the
`codecraftai-interfaces` `ICommandRunner` abstraction.  Config reads
and writes route through the runner (`cat`, `tee`).  All mutating
operations honour dry-run mode and config-driven elevation.

This feature complements — but does not duplicate — the
`3-log-management` local-journal surface and the
`3-certificate-issuance` / `3-certificate-trust-management` TLS
material surfaces.

The reference implementation for shape, idempotency, dry-run, and
result mapping lives in
`codecraftai-sysman-linux-redhat/specs/3-remote-log-forwarding/`.

---

## Requirements

### Requirement 1: Add a Forwarder

**User Story:** As a maintenance coordinator, I want to declare an
rsyslog forward drop-in so that local syslog traffic ships to a
remote collector.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_forwarder(spec)` THE SYSTEM
   SHALL render the canonical rsyslog forward stanza for
   `(host, port, protocol, selector)` and write it to
   `/etc/rsyslog.d/codecraftai-fwd-<id>.conf` via `tee` over stdin.
2. WHEN `protocol` is `tcp` THE SYSTEM SHALL emit the
   double-`@` action (`<selector> @@<host>:<port>`).
3. WHEN `protocol` is `udp` THE SYSTEM SHALL emit the single-`@`
   action (`<selector> @<host>:<port>`).
4. WHEN `protocol` is `tls` THE SYSTEM SHALL emit the
   `$DefaultNetstreamDriver gtls` global directive plus the
   `@@<host>:<port>` action and reference the supplied
   `tls_ca_cert` via `$DefaultNetstreamDriverCAFile`.
5. WHEN `tls_ca_cert` is required and not supplied THE SYSTEM
   SHALL return `FAILED` with a stderr that names the missing
   field.
6. WHEN the existing drop-in's content already matches the
   rendered desired body byte-for-byte THE SYSTEM SHALL return
   `SKIPPED`.
7. WHEN the dispatch succeeds THE SYSTEM SHALL invoke
   `systemctl restart rsyslog` so the new drop-in is loaded;
   restart failures MUST surface as `FAILED` on the mutating
   verb.

---

### Requirement 2: Remove a Forwarder

**User Story:** As a maintenance coordinator, I want to remove an
rsyslog forward drop-in so that the matching syslog stream stops
shipping off-host.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_forwarder(id)` THE SYSTEM
   SHALL dispatch `rm /etc/rsyslog.d/codecraftai-fwd-<id>.conf`
   through the runner.
2. WHEN the drop-in is absent THE SYSTEM SHALL return `SKIPPED`
   without invoking `rm`.
3. WHEN the drop-in is `.disabled`-suffixed THE SYSTEM SHALL
   include the disabled variant in the removal probe so that
   disabled forwarders can also be torn down.
4. WHEN the dispatch succeeds THE SYSTEM SHALL invoke
   `systemctl restart rsyslog`; restart failures MUST surface as
   `FAILED`.

---

### Requirement 3: List Forwarders

**User Story:** As a maintenance coordinator, I want to enumerate
the forwarders this manager owns so that I can audit drift.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_forwarders()` THE SYSTEM
   SHALL dispatch `ls -1 /etc/rsyslog.d` through the runner
   without elevation.
2. WHEN parsing the listing THE SYSTEM SHALL include only files
   whose basename starts with the `codecraftai-fwd-` prefix so that
   third-party drop-ins are not falsely claimed.
3. WHEN reading each managed drop-in THE SYSTEM SHALL parse
   `host`, `port`, `protocol`, `enabled`, and `selector_or_query`
   from the rendered stanza or its `# DISABLED` sentinel block.
4. WHEN the listing dispatch fails for any reason other than the
   directory simply being empty THE SYSTEM SHALL raise
   `UbuntuForwarderQueryError`.

---

### Requirement 4: Enable / Disable a Forwarder

**User Story:** As a maintenance coordinator, I want to suspend
and resume an existing forwarder without rewriting its body.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_forwarder(id)` THE SYSTEM
   SHALL `mv /etc/rsyslog.d/codecraftai-fwd-<id>.conf
   /etc/rsyslog.d/codecraftai-fwd-<id>.conf.disabled` so that
   rsyslog ignores the drop-in on the next restart.
2. WHEN the coordinator calls `enable_forwarder(id)` THE SYSTEM
   SHALL `mv` the `.disabled` file back to `.conf`.
3. WHEN the requested state already matches the on-disk state
   THE SYSTEM SHALL return `SKIPPED` without invoking `mv` or
   restart.
4. WHEN the rename succeeds THE SYSTEM SHALL invoke
   `systemctl restart rsyslog`; failures MUST surface as
   `FAILED`.

---

### Requirement 5: Test a Forwarder

**User Story:** As a maintenance coordinator, I want to send a
known message into the local syslog pipeline so that the
configured forwarder can be observed end-to-end on the collector.

#### Acceptance Criteria

1. WHEN the coordinator calls `test_forwarder(id)` THE SYSTEM
   SHALL dispatch
   `logger -t codecraftai-test 'forwarder-test-<timestamp>'`
   through the runner.
2. THE SYSTEM SHALL return an `UbuntuForwarderTestResult` with
   `forwarder_id` populated and `test_message_id` set to the
   timestamp portion of the dispatched message.
3. THE SYSTEM SHALL NOT raise on dispatch failure; the result's
   `status` reports `FAILED` and `stderr` carries the runner's
   stderr.
4. THE SYSTEM SHALL run the verb unconditionally — there is no
   idempotency guard for `test_forwarder`.

---

### Requirement 6: Get Forwarder Status

**User Story:** As a maintenance coordinator, I want to query a
forwarder's current state so that I can correlate it with the
collector's records.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_forwarder_status(id)` THE
   SYSTEM SHALL probe both `<prefix><id>.conf` and the
   `.disabled` variant via `test -e` and return an
   `UbuntuForwarderStatus` describing the current `enabled` flag.
2. WHEN no drop-in is found THE SYSTEM SHALL return a status
   record whose `enabled` is `False` and whose
   `last_event_time`, `error_count`, and `bytes_sent` are
   `None`.
3. THE SYSTEM SHALL NOT elevate the status probe.

---

### Requirement 7: TLS Spec Rendering

**User Story:** As a maintenance coordinator, I want TLS-secured
forwarders to render the `gtls` driver block that rsyslog
expects.

#### Acceptance Criteria

1. WHEN `protocol` is `tls` THE SYSTEM SHALL render
   `$DefaultNetstreamDriver gtls`,
   `$DefaultNetstreamDriverCAFile <path>`,
   `$ActionSendStreamDriver gtls`,
   `$ActionSendStreamDriverMode 1`, and
   `$ActionSendStreamDriverAuthMode x509/name`.
2. THE SYSTEM SHALL document in `design.md` that the
   `rsyslog-gnutls` package MUST be installed for the gtls
   driver to load; this manager does not install it.

---

### Requirement 8: Dry-Run, Elevation, and Error Reporting

**User Story:** As a maintenance coordinator, I want every write
verb to honour dry-run and to elevate predictably.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT mutate any
   drop-in and SHALL NOT dispatch `systemctl restart rsyslog`.
2. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner on every write dispatch.
3. WHEN the runner returns non-zero on a write THE SYSTEM SHALL
   return `UbuntuForwarderActionResult(status=FAILED,
   exit_code=<code>, stderr=<captured>)` without raising.
4. THE SYSTEM SHALL never elevate read verbs (`list_forwarders`,
   `get_forwarder_status`, `test_forwarder`).

---

### Requirement 9: Idempotency

**User Story:** As a maintenance coordinator, I want every write
verb to be safe to invoke repeatedly.

#### Acceptance Criteria

1. `add_forwarder` MUST `SKIP` when the existing drop-in matches
   the rendered desired body byte-for-byte.
2. `remove_forwarder` MUST `SKIP` when neither the `.conf` nor
   the `.disabled` drop-in exists.
3. `enable_forwarder` MUST `SKIP` when the `.conf` file already
   exists (and there is no `.disabled` shadow).
4. `disable_forwarder` MUST `SKIP` when only the `.disabled`
   file exists.
5. `test_forwarder` MUST run unconditionally — there is no
   idempotency guard.

---

### Requirement 10: Service Reload

**User Story:** As a maintenance coordinator, I want the manager
to reload rsyslog after every successful drop-in mutation so
that the new configuration takes effect immediately.

#### Acceptance Criteria

1. WHEN any write verb succeeds THE SYSTEM SHALL dispatch
   `systemctl restart rsyslog` through the same runner that
   handled the mutation, with `elevated=config.elevated`.
2. WHEN the restart returns non-zero THE SYSTEM SHALL surface
   the failure on the mutating verb's result with `status=FAILED`,
   the restart's `exit_code`, and the restart's `stderr`.
3. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch
   the restart.

---

### Requirement 11: Documented Limitations

**User Story:** As a maintenance coordinator, I want the
documentation to enumerate every native rsyslog feature this
pilot does not cover so that I do not file false bugs.

#### Acceptance Criteria

1. THE `design.md` SHALL list disk-spool / queue tuning,
   structured (CEE / GELF / JSON) output, RELP transport, and
   the `omrelp` / `omkafka` outputs as out of scope.
2. THE `design.md` SHALL note the `rsyslog-gnutls` package
   prerequisite for TLS forwarders.
3. THE `design.md` SHALL describe the contract drift discovered
   during implementation in a dedicated section.
