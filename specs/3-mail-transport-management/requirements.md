# Requirements

## Introduction

The mail-transport-management feature provides the
`automojo-sysman-linux-ubuntu` package with the ability to manage the
local mail transport agent (MTA) on Ubuntu hosts.  Ubuntu's default
MTA is **Postfix**, installed by the `postfix` package and exposed
under systemd as `postfix.service`.  This spec implements the
mail-transport-management contract defined at
`automojo-sysman-common/specs/contracts/mail-transport-management/contract.md`
for Ubuntu, scoped to the Postfix daemon exclusively.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *what* mail policy the server enforces — it
provides the capability to *apply* policy reliably, idempotently, and
with full auditability.  All system commands (`postconf`, `postqueue`,
`mailq`, `newaliases`, `sendmail`, `cat`, `tee`, `systemctl`) are
executed exclusively through the `automojo-interfaces` command-runner
abstraction.  No `subprocess`, no direct `open`, and no direct
`pathlib` calls are made inside this module.

The Ubuntu Postfix `main.cf` lives at `/etc/postfix/main.cf`; the
aliases file lives at `/etc/aliases`.  Postfix's user-facing
configuration interface is `postconf` (rather than direct `main.cf`
file edits) — every directive read uses `postconf -n`, every directive
write uses `postconf -e <key>=<value>`.  This pilot manages directives
through `postconf` exclusively rather than rewriting `main.cf` on
disk.

This feature complements — and does not duplicate — the
`postfix.service` lifecycle owned by `3-service-management`, the
SMTP-port firewall surface owned by `3-firewall-management`, and the
TLS-material surface owned by `3-certificate-trust-management` /
`3-certificate-issuance`.  The reload after `set_mta_config` is the
sole exception: it is a write through `systemctl` that is idiomatic
to "apply directive change" rather than "manage service lifecycle".

---

## Requirements

### Requirement 1: Read MTA Configuration

**User Story:** As a maintenance coordinator, I want to read the live
Postfix configuration, so that I can audit declared directives without
parsing the raw `main.cf` file.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_mta_config()` THE SYSTEM SHALL
   invoke `postconf -n` via the runner with `elevated=False`.
2. WHEN `postconf -n` exits 0 THE SYSTEM SHALL parse each `key =
   value` line into a `UbuntuMtaConfig`, mapping every recognised
   directive (`myhostname`, `mydomain`, `myorigin`, `relayhost`,
   `mynetworks`, `inet_interfaces`, `inet_protocols`,
   `mailbox_size_limit`, `message_size_limit`) to its named field.
3. WHEN a directive is integer-typed THE SYSTEM SHALL coerce the
   value with `int()`; non-numeric values fall back to `extras`.
4. WHEN a directive is not in the recognised set THE SYSTEM SHALL
   place it in the `extras` dict.
5. WHEN `postconf -n` exits non-zero AND `stderr` is non-empty THE
   SYSTEM SHALL raise `UbuntuMtaQueryError` with `command`,
   `exit_code`, and `stderr` populated.

---

### Requirement 2: Apply MTA Configuration

**User Story:** As a maintenance coordinator, I want to apply a
declared directive snapshot to live Postfix, so that the server's
routing, relay, and sizing posture matches the spec.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_mta_config(spec)` THE SYSTEM
   SHALL first call `get_mta_config()` to compare against the live
   directive set.
2. WHEN a named field on `spec` differs from the live value THE
   SYSTEM SHALL dispatch `postconf -e <key>=<value>` for that
   directive.
3. WHEN a named field on `spec` matches the live value THE SYSTEM
   SHALL NOT dispatch `postconf -e` for that directive and SHALL
   record `SKIPPED <key>=<value>` in the audit trail.
4. WHEN every named field matches THE SYSTEM SHALL return
   `status=SKIPPED` for the aggregate result and SHALL NOT invoke
   the reload.
5. WHEN at least one directive changes successfully AND
   `config.reload_after_set=True` THE SYSTEM SHALL invoke
   `systemctl reload <service_name>` once at the end.
6. WHEN any per-directive `postconf -e` exits non-zero THE SYSTEM
   SHALL return `status=FAILED` with that directive's exit code,
   stdout, and stderr; remaining directives SHALL NOT be dispatched.
7. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
8. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` for
   every per-directive `postconf -e` and the trailing `systemctl
   reload`.

---

### Requirement 3: Add an Alias

**User Story:** As a maintenance coordinator, I want to add an entry
to `/etc/aliases`, so that local mail addressed to `<local>` is
delivered to `<remote>`.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_alias(local, remote)` THE SYSTEM
   SHALL read `/etc/aliases` via `cat`, parse, mutate the in-memory
   parsed structure, re-render, and write the result via `tee
   /etc/aliases` with the rendered content on stdin.
2. WHEN the file already contains an entry for `local` whose remote
   equals `remote` THE SYSTEM SHALL return `status=SKIPPED` without
   invoking `tee`.
3. WHEN the file contains an entry for `local` with a different
   remote THE SYSTEM SHALL replace the line in the rendered content.
4. WHEN the write succeeds THE SYSTEM SHALL invoke `newaliases` to
   rebuild the compiled aliases database.
5. WHEN `newaliases` fails THE SYSTEM SHALL map the verb to
   `status=FAILED` even when the `tee` write succeeded.
6. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
7. WHEN the write fails THE SYSTEM SHALL return `status=FAILED`.

---

### Requirement 4: Remove an Alias

**User Story:** As a maintenance coordinator, I want to remove an
entry from `/etc/aliases`, so that the alias is no longer delivered.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_alias(local)` and the file
   does not exist THE SYSTEM SHALL return `status=SKIPPED`.
2. WHEN the file does not contain an entry for `local` THE SYSTEM
   SHALL return `status=SKIPPED`.
3. WHEN an entry for `local` exists THE SYSTEM SHALL drop the line,
   re-render, and write via `tee /etc/aliases`.
4. WHEN the write succeeds THE SYSTEM SHALL invoke `newaliases`.
5. WHEN `newaliases` fails THE SYSTEM SHALL map the verb to
   `status=FAILED`.
6. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 5: List Aliases

**User Story:** As a maintenance coordinator, I want to enumerate
every entry in `/etc/aliases`, so that I can audit local-mail policy.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_aliases()` and the file does
   not exist THE SYSTEM SHALL return an empty list.
2. WHEN the file exists THE SYSTEM SHALL parse each non-comment,
   non-blank line into a `UbuntuMtaAliasInfo` with `local` and
   `remote` populated.
3. WHEN a line begins with whitespace AND a previous entry exists
   THE SYSTEM SHALL fold the line as a continuation onto the
   previous entry's `remote`.
4. WHEN `list_aliases` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True`.

---

### Requirement 6: Probe Mail Delivery

**User Story:** As a maintenance coordinator, I want to send a probe
message through the local MTA, so that I can verify end-to-end
delivery.

#### Acceptance Criteria

1. WHEN the coordinator calls `test_mail_delivery(to, from_addr,
   subject)` THE SYSTEM SHALL invoke `sendmail -t` via the runner.
2. WHEN invoking `sendmail -t` THE SYSTEM SHALL pass an RFC 5322
   payload on stdin containing `Subject:`, `From:`, and `To:`
   headers, a blank line, and a fixed body.
3. WHEN the runner exits 0 THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the runner exits non-zero THE SYSTEM SHALL return
   `status=FAILED` with `exit_code` and `stderr` populated.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner; `stdout` SHALL contain the payload
   that would have been delivered.
6. `test_mail_delivery` MUST NOT skip — it is operational.

---

### Requirement 7: Read Queue Status

**User Story:** As a maintenance coordinator, I want to read the
current Postfix mail-queue status, so that I can detect stuck or
backed-up messages.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_queue_status()` THE SYSTEM SHALL
   invoke `mailq` via the runner with `elevated=False`.
2. WHEN the output contains `Mail queue is empty` THE SYSTEM SHALL
   return a zero-valued `UbuntuMtaQueueStatus`.
3. WHEN the output contains queue records THE SYSTEM SHALL parse
   each head line; queue ids ending in `*` count as `pending`,
   queue ids without a marker count as `deferred`.
4. WHEN the output contains a `-- N Kbytes in M Requests.` footer
   THE SYSTEM SHALL set `total_size_bytes` to `N * 1024`.
5. WHEN the queue is non-empty THE SYSTEM SHALL set
   `queue_age_oldest` to the arrival-time text of the first parsed
   record.
6. WHEN `mailq` exits non-zero AND `stderr` is non-empty THE SYSTEM
   SHALL raise `UbuntuMtaQueryError`.

---

### Requirement 8: Flush the Queue

**User Story:** As a maintenance coordinator, I want to attempt
immediate delivery of every queued message, so that delivery is
retried on demand.

#### Acceptance Criteria

1. WHEN the coordinator calls `flush_queue()` THE SYSTEM SHALL
   invoke `postqueue -f` via the runner.
2. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
3. WHEN the runner exits 0 THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the runner exits non-zero THE SYSTEM SHALL return
   `status=FAILED`.
5. `flush_queue` MUST NOT skip — it is operational.

---

### Requirement 9: Elevation Policy

**User Story:** As a maintenance coordinator, I want every write that
mutates the MTA's state to elevate, so that operations succeed on
standard Ubuntu hosts where the process does not run as root.

#### Acceptance Criteria

1. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` for
   every write verb (`set_mta_config`, `add_alias`, `remove_alias`,
   `test_mail_delivery`, `flush_queue`) and any chained
   ``newaliases`` / ``systemctl reload`` dispatches.
2. WHEN performing reads (`get_mta_config`, `list_aliases`,
   `get_queue_status`) THE SYSTEM SHALL pass `elevated=False`
   regardless of the config.
3. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False` on
   every path.

---

### Requirement 10: Dry-Run

**User Story:** As a maintenance coordinator, I want to preview every
mutation without dispatching, so that diff-vs-state reports can be
generated before changes go live.

#### Acceptance Criteria

1. WHEN `dry_run=True` THE SYSTEM SHALL NOT dispatch any mutating
   command (`postconf -e`, `tee`, `newaliases`, `sendmail`,
   `postqueue -f`, `systemctl reload`) and SHALL NOT rewrite
   `/etc/aliases`.
2. WHEN `dry_run=True` THE SYSTEM SHALL still allow read-only
   probes that drive idempotency decisions (`postconf -n`,
   `cat /etc/aliases`, `mailq`).
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN` for
   every mutating verb with `command` populated to the would-have-
   been argv.

---

### Requirement 11: Failure Reporting

**User Story:** As a maintenance coordinator, I want every failed
write to surface a structured result, so that downstream automation
can categorise failures without scraping logs.

#### Acceptance Criteria

1. WHEN any write verb's underlying runner call exits non-zero THE
   SYSTEM SHALL return `status=FAILED` with `exit_code`, `stdout`,
   `stderr`, and `command` populated from the runner's
   `RunResult`.
2. WHEN a chained command fails (e.g., `newaliases` after a
   successful `tee`, or `systemctl reload` after a successful
   `postconf -e`) THE SYSTEM SHALL surface the chained command's
   `command`, `exit_code`, and `stderr` on the returned result.
3. WHEN a read verb's underlying runner call exits non-zero with
   non-empty `stderr` THE SYSTEM SHALL raise `UbuntuMtaQueryError`
   carrying `command`, `exit_code`, and `stderr`.
