# Requirements

## Introduction

The audit management feature provides the
`codecraftai-sysman-linux-ubuntu` package with the ability to enable,
disable, query, and rule-manage the Linux Audit subsystem on
Ubuntu hosts.  This spec implements the audit-management contract
defined at
`codecraftai-sysman-common/specs/contracts/audit-management/contract.md`
for Ubuntu using `auditctl` for runtime rule manipulation,
`augenrules --load` for persistent rule reload, `ausearch` for
event-stream filtering, and `systemctl` for daemon lifecycle.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public verbs.  The feature
does not decide *which* rules belong on the host — it provides the
capability to install, remove, and observe them reliably,
idempotently, and with full auditability.  Every system command is
executed exclusively through the `codecraftai-interfaces` command-runner
abstraction; rule-file I/O is dispatched via `cat` / `tee` against
that same runner.  No `subprocess`, no direct `open`, and no direct
`pathlib` calls are made inside the module, enabling dry-run
simulation, auditing, and test mocking without requiring a live
audit subsystem.

The abstract `rule_id` on Ubuntu maps to the audit rule's `-k <key>`
token.  The structured `UbuntuAuditRuleSpec` accepts either a
high-level `(watch_path, permissions, key, ...)` shape that the
manager renders into `auditctl` argv, or a verbatim `raw_rule`
string for callers who already speak `auditctl` syntax.

---

## Requirements

### Requirement 1: Add a Rule

**User Story:** As a maintenance coordinator, I want to add an audit
rule from a structured spec, so that the audit subsystem watches a
path, syscall, or filter that my policy specifies.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_rule(spec)` and `spec` is a
   structured `UbuntuAuditRuleSpec` THE SYSTEM SHALL render the spec
   into the `auditctl` argv body via `_render_auditctl_args` and
   dispatch `auditctl <argv>` through the command runner.
2. WHEN `spec.raw_rule` is non-empty THE SYSTEM SHALL pass the
   tokenised raw form to `auditctl` after appending `-k <spec.key>`
   when the raw form is missing a key token.
3. WHEN `spec.persistent` is True THE SYSTEM SHALL append the
   rendered rule line to the manager-owned drop-in under
   `<rules_d>/<default_basename>` via `tee` and dispatch
   `augenrules --load` to refresh the persistent store.
4. WHEN a runtime rule with the same `-k` key is already present THE
   SYSTEM SHALL return `UbuntuAuditActionResult(status=SKIPPED)` and
   SHALL NOT invoke `auditctl` or `tee` or `augenrules`.
5. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner or any persistence write.
6. WHEN the runtime invocation fails THE SYSTEM SHALL return
   `status=FAILED` with `exit_code`, `stdout`, `stderr`, and
   `command` populated.
7. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to the
   runner for every write step (runtime add, drop-in tee,
   augenrules reload).

---

### Requirement 2: Remove a Rule

**User Story:** As a maintenance coordinator, I want to remove an
audit rule by its `-k` key, so that the audit subsystem stops
emitting events tagged with that key.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_rule(rule_id)` and a runtime
   rule whose `-k` key equals `rule_id` is present THE SYSTEM SHALL
   dispatch `auditctl -d -k <rule_id>` through the runner.
2. WHEN no runtime rule and no drop-in line carries `rule_id` THE
   SYSTEM SHALL return `status=SKIPPED` without dispatching the
   runner.
3. WHEN the runtime delete succeeds THE SYSTEM SHALL rewrite the
   drop-in with every line whose tokens carry `-k <rule_id>`
   stripped, via `tee`, then dispatch `augenrules --load`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
5. WHEN the runner reports a non-zero exit THE SYSTEM SHALL return
   `status=FAILED` with `stdout` / `stderr` / `exit_code` /
   `command` populated.

---

### Requirement 3: List Rules

**User Story:** As a maintenance coordinator, I want to enumerate
the active audit rule set, so that I can compare current state
against my desired baseline.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_rules()` THE SYSTEM SHALL
   dispatch `auditctl -l` through the runner without elevation.
2. WHEN `auditctl -l` exits zero THE SYSTEM SHALL parse one rule
   per non-comment / non-blank stdout line into an
   `UbuntuAuditRuleInfo` whose `expression` mirrors the line text,
   `key` carries the `-k` value when present, and `subsystem` is
   `"path"` for `-w` rules, `"syscall"` for `-a` rules, `"raw"`
   otherwise.
3. WHEN `auditctl -l` exits non-zero with non-empty stderr THE
   SYSTEM SHALL raise `UbuntuAuditQueryError` with the captured
   command, exit code, and stderr.
4. WHEN the manager-owned drop-in exists THE SYSTEM SHALL also
   parse its non-comment lines and append each rule whose
   `expression` text is not already in the runtime set, with
   `source` populated to the drop-in path so callers can
   distinguish persistent-only rules.
5. WHEN `auditctl -l` returns the literal `"No rules"` line THE
   SYSTEM SHALL treat it as the empty-rule-set sentinel.
6. WHEN `list_rules` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True` to the runner.

---

### Requirement 4: Enable Auditing

**User Story:** As a maintenance coordinator, I want to enable the
audit subsystem, so that it begins capturing events.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_auditing()` THE SYSTEM SHALL
   dispatch `auditctl -e 1` through the runner and then
   `systemctl enable --now <service_name>` (default `auditd`).
2. WHEN `auditctl -s` reports the kernel as enabled and the daemon
   as running THE SYSTEM SHALL return `status=SKIPPED` and SHALL
   NOT dispatch either invocation.
3. WHEN `auditctl -s` reports `enabled 2` (immutable mode) THE
   SYSTEM SHALL return `status=FAILED` with a descriptive `message`
   and SHALL NOT dispatch the enable invocation.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
5. WHEN either the `auditctl -e 1` or the `systemctl enable --now`
   step fails THE SYSTEM SHALL return `status=FAILED` and SHALL
   surface the failure's stdout / stderr / exit_code.
6. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to
   both the `auditctl -e 1` and `systemctl enable --now`
   invocations.

---

### Requirement 5: Disable Auditing

**User Story:** As a maintenance coordinator, I want to disable the
audit subsystem, so that the daemon stops capturing events.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_auditing()` THE SYSTEM SHALL
   dispatch `auditctl -e 0` and
   `systemctl disable --now <service_name>`.
2. WHEN `auditctl -s` reports the kernel as disabled and the
   daemon as not running THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN `auditctl -s` reports `enabled 2` THE SYSTEM SHALL return
   `status=FAILED` with a descriptive `message` and SHALL NOT
   dispatch the disable invocation.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
5. WHEN either step fails THE SYSTEM SHALL return `status=FAILED`
   with surfaced stdout / stderr / exit_code.

---

### Requirement 6: Query Audit Status

**User Story:** As a maintenance coordinator, I want to inspect the
audit subsystem's status, so that my reports describe the daemon's
current configuration.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_audit_status()` THE SYSTEM SHALL
   dispatch `auditctl -s` through the runner without elevation.
2. WHEN the runner exits zero THE SYSTEM SHALL parse the `<key>
   <value>` lines into `UbuntuAuditStatus` fields:
   `enabled`, `running` (derived from a non-zero `pid`), `pid`,
   `failure_mode`, `rate_limit`, `backlog_limit`, `backlog`,
   `lost_events`, `locked` (`enabled == 2`), and the raw text.
3. WHEN the runner exits non-zero with non-empty stderr THE SYSTEM
   SHALL raise `UbuntuAuditQueryError`.
4. WHEN any individual line cannot be parsed THE SYSTEM SHALL set
   the corresponding field to `None` rather than raise.

---

### Requirement 7: Search Events

**User Story:** As a maintenance coordinator, I want to filter
audit events by key, message type, time bound, or user, so that
post-hoc analyses can target specific incidents.

#### Acceptance Criteria

1. WHEN the coordinator calls `search_events(query)` THE SYSTEM
   SHALL dispatch `ausearch` through the runner with argv tokens
   built from the `UbuntuAuditEventQuery` fields:
   `-k <key>` / `-m <message_type>` / `-ts <since>` /
   `-te <until>` / `-ui <user>`.
2. WHEN `query.raw_filter` is non-empty THE SYSTEM SHALL pass its
   whitespace-split tokens verbatim after `ausearch`, ignoring the
   structured fields.
3. WHEN the runner exits zero THE SYSTEM SHALL parse the dashed
   record stream into `UbuntuAuditEvent` rows, capping the result
   at `query.limit` when set.
4. WHEN the runner exits non-zero with empty stderr THE SYSTEM
   SHALL treat the result as ausearch's no-match signal and return
   an empty list.
5. WHEN the runner exits non-zero with non-empty stderr THE SYSTEM
   SHALL raise `UbuntuAuditQueryError`.
6. WHEN `search_events` is invoked THE SYSTEM SHALL NOT pass
   `elevated=True` to the runner.

---

### Requirement 8: Idempotency Across Verbs

**User Story:** As a maintenance coordinator, I want every write
verb to be safely re-runnable, so that planning loops can converge
without producing spurious change events.

#### Acceptance Criteria

1. WHEN `add_rule` is called with a spec whose key already exists
   in the runtime rule set THE SYSTEM SHALL return `status=SKIPPED`.
2. WHEN `remove_rule` is called with a `rule_id` that is present in
   neither the runtime nor the drop-in store THE SYSTEM SHALL
   return `status=SKIPPED`.
3. WHEN `enable_auditing` is called and the subsystem is already
   enabled and running THE SYSTEM SHALL return `status=SKIPPED`.
4. WHEN `disable_auditing` is called and the subsystem is already
   disabled and stopped THE SYSTEM SHALL return `status=SKIPPED`.

---

### Requirement 9: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want write
operations to run with root privileges, so that `auditctl`,
`augenrules`, `tee` against `/etc/audit/rules.d/`, and `systemctl`
all succeed.

#### Acceptance Criteria

1. WHEN `elevated=True` THE SYSTEM SHALL pass `elevated=True` to
   the runner for every `add_rule`, `remove_rule`,
   `enable_auditing`, and `disable_auditing` step (runtime
   invocation, persistence write, augenrules reload, systemctl
   toggle).
2. WHEN performing reads (`list_rules`, `get_audit_status`,
   `search_events`) THE SYSTEM SHALL NOT pass `elevated=True`.
3. WHEN `elevated=False` THE SYSTEM SHALL pass `elevated=False` on
   every path.
4. WHEN a non-root runner hits a permission denial during a read
   THE SYSTEM SHALL surface the failure as
   `UbuntuAuditQueryError` rather than silently producing an
   empty result.

---

### Requirement 10: Dry-Run Semantics

**User Story:** As a maintenance coordinator, I want every write
verb to honour dry-run mode, so that plans can be validated
without modifying the audit subsystem.

#### Acceptance Criteria

1. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   for `add_rule`, `remove_rule`, `enable_auditing`, and
   `disable_auditing` without invoking the runner, the persistence
   tee, or the augenrules reload.
2. WHEN `dry_run=False` THE SYSTEM SHALL execute the normal code
   path regardless of other configuration.
3. WHEN `dry_run=True` THE SYSTEM SHALL still populate
   `command` on the result so callers can audit the would-have-been
   argv.

---

### Requirement 11: Immutable-Mode Surfacing

**User Story:** As a maintenance coordinator, I want a clear,
typed signal when the audit subsystem is in immutable mode, so
that my pipeline does not silently appear to succeed while
ignoring my changes.

#### Acceptance Criteria

1. WHEN `auditctl -s` reports `enabled 2` and the coordinator
   calls `enable_auditing` or `disable_auditing` THE SYSTEM SHALL
   return `UbuntuAuditActionResult(status=FAILED, message=...)`
   describing the locked state.
2. WHEN `add_rule` or `remove_rule` is dispatched against a
   subsystem whose `auditctl -s` reports immutable mode THE
   SYSTEM SHALL still attempt the runner invocation; the runner
   will surface the kernel-side rejection as a non-zero exit and
   the manager will return `status=FAILED` with the captured
   stderr.  Callers MAY pre-flight `get_audit_status` to detect
   `locked=True` before issuing rule writes.

---

### Requirement 12: Observability and Result Shape

**User Story:** As a maintenance coordinator, I want every action
result to carry the same fields, so that downstream reporting can
treat them uniformly.

#### Acceptance Criteria

1. WHEN any write verb returns THE SYSTEM SHALL populate
   `status`, `command`, and (when applicable) `rule_id` /
   `rule_key` on the result.
2. WHEN any write verb returns `SUCCESS` or `FAILED` THE SYSTEM
   SHALL populate `exit_code`, `stdout`, and `stderr` from the
   runner's `RunResult`.
3. WHEN any write verb returns `SKIPPED` THE SYSTEM SHALL
   populate `message` with a human-readable explanation.
