# Requirements

## Introduction

The firewall management feature provides the `automojo-sysman-linux-ubuntu` package with
the ability to manage `ufw` (Uncomplicated Firewall) on Ubuntu systems.  This spec
implements the firewall-management contract defined at
`automojo-sysman-common/specs/contracts/firewall-management/contract.md` for Ubuntu
using `ufw`.  It includes adding and removing services, ports, and rich rules, querying
firewall state, and reloading the firewall to apply permanent changes to the runtime
configuration.

A maintenance coordinator running in an external package drives all operations by calling
into this feature's public interfaces.  The feature does not decide *what* firewall rules
to apply — it provides the capability to *do it* reliably, idempotently, and with full
auditability.  All system commands are executed exclusively through the
`automojo-interfaces` command-runner abstraction, enabling dry-run simulation, auditing,
and test mocking without requiring a live system.  Because ufw has no concept of zones,
the abstract `zone` parameter is validated to the literal string `"default"` and any
other value causes the operation to return `FAILED`.

---

## Requirements

### Requirement 1: Add a Service to a Zone

**User Story:** As a maintenance coordinator, I want to add a named ufw application
profile allow rule to the default policy, so that traffic for that service is permitted
through the firewall.

#### Acceptance Criteria

1. WHEN the coordinator provides a service name and `zone="default"` THE SYSTEM SHALL
   invoke `ufw allow <service>` via the command runner with `elevated=True` when
   configured for elevation.
2. WHEN `zone` is not the literal string `"default"` THE SYSTEM SHALL NOT execute any
   command and SHALL return a `UbuntuFirewallActionResult` with status `FAILED` and a
   `detail` explaining that ufw supports only the `"default"` zone.
3. WHEN the service is already present in the current ufw ruleset THE SYSTEM SHALL skip
   execution and return a `UbuntuFirewallActionResult` with status `SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuFirewallActionResult` with
   status `SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `UbuntuFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 2: Remove a Service from a Zone

**User Story:** As a maintenance coordinator, I want to remove a named ufw application
profile allow rule from the default policy, so that traffic for that service is no longer
permitted through the firewall.

#### Acceptance Criteria

1. WHEN the coordinator provides a service name and `zone="default"` THE SYSTEM SHALL
   invoke `ufw delete allow <service>` via the command runner.
2. WHEN `zone` is not `"default"` THE SYSTEM SHALL return a `UbuntuFirewallActionResult`
   with status `FAILED` and a zone-validation `detail` message.
3. WHEN the service is not present in the current ruleset THE SYSTEM SHALL skip execution
   and return a `UbuntuFirewallActionResult` with status `SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuFirewallActionResult` with
   status `SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `UbuntuFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 3: Add a Port to a Zone

**User Story:** As a maintenance coordinator, I want to open a specific port and protocol
in ufw, so that traffic on that port is permitted through the firewall without requiring
a named application profile.

#### Acceptance Criteria

1. WHEN the coordinator provides a port number, protocol, and `zone="default"` THE
   SYSTEM SHALL invoke `ufw allow <port>/<protocol>` via the command runner.
2. WHEN `zone` is not `"default"` THE SYSTEM SHALL return a `UbuntuFirewallActionResult`
   with status `FAILED` and a zone-validation `detail` message.
3. WHEN the `<port>/<protocol>` combination is already present in the ufw ruleset THE
   SYSTEM SHALL skip execution and return a `UbuntuFirewallActionResult` with status
   `SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuFirewallActionResult` with
   status `SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `UbuntuFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 4: Remove a Port from a Zone

**User Story:** As a maintenance coordinator, I want to close a specific port and
protocol in ufw, so that traffic on that port is no longer permitted.

#### Acceptance Criteria

1. WHEN the coordinator provides a port number, protocol, and `zone="default"` THE
   SYSTEM SHALL invoke `ufw delete allow <port>/<protocol>` via the command runner.
2. WHEN `zone` is not `"default"` THE SYSTEM SHALL return a `UbuntuFirewallActionResult`
   with status `FAILED` and a zone-validation `detail` message.
3. WHEN the `<port>/<protocol>` combination is not present in the ufw ruleset THE SYSTEM
   SHALL skip execution and return a `UbuntuFirewallActionResult` with status `SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuFirewallActionResult` with
   status `SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `UbuntuFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 5: Add a Rich Rule to a Zone

**User Story:** As a maintenance coordinator, I want to add a raw ufw-syntax rich rule,
so that complex traffic filtering logic that cannot be expressed as a simple service or
port can be enforced.

#### Acceptance Criteria

1. WHEN the coordinator provides a rule string and `zone="default"` and the rule begins
   with one of the ufw action verbs `allow`, `deny`, `reject`, or `limit` THE SYSTEM
   SHALL invoke `ufw <rule>` via the command runner.
2. WHEN the rule string does not begin with a valid ufw action verb THE SYSTEM SHALL
   return a `UbuntuFirewallActionResult` with status `FAILED` and a `detail` explaining
   the required rule shape.
3. WHEN `zone` is not `"default"` THE SYSTEM SHALL return a `UbuntuFirewallActionResult`
   with status `FAILED` and a zone-validation `detail` message.
4. WHEN an identical rule (after whitespace normalisation) is already present in the
   ufw ruleset THE SYSTEM SHALL skip execution and return a `UbuntuFirewallActionResult`
   with status `SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuFirewallActionResult` with
   status `SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `UbuntuFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 6: Remove a Rich Rule from a Zone

**User Story:** As a maintenance coordinator, I want to remove a raw ufw-syntax rich
rule, so that a previously applied complex traffic rule is revoked.

#### Acceptance Criteria

1. WHEN the coordinator provides a rule string and `zone="default"` THE SYSTEM SHALL
   invoke `ufw delete <rule>` via the command runner.
2. WHEN `zone` is not `"default"` THE SYSTEM SHALL return a `UbuntuFirewallActionResult`
   with status `FAILED` and a zone-validation `detail` message.
3. WHEN the rule is not present in the current ruleset THE SYSTEM SHALL skip execution
   and return a `UbuntuFirewallActionResult` with status `SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuFirewallActionResult` with
   status `SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `UbuntuFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 7: Set the Default Zone

**User Story:** As a maintenance coordinator, I want a uniform `set_default_zone` API
so that mixed-OS orchestration is not forced to special-case ufw.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_default_zone("default")` THE SYSTEM SHALL NOT execute
   any command and SHALL return a `UbuntuFirewallActionResult` with status `SKIPPED` and
   a `detail` noting that ufw has no switchable default zone concept.
2. WHEN the coordinator calls `set_default_zone(zone)` with any value other than
   `"default"` THE SYSTEM SHALL return a `UbuntuFirewallActionResult` with status
   `FAILED` and a zone-validation `detail` message.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuFirewallActionResult` with status `DRY_RUN` regardless of the `zone` value.
4. WHEN the coordinator relies on the return value to drive subsequent steps THE SYSTEM
   SHALL consistently return either `SKIPPED` or `FAILED` (never `SUCCESS`) for this
   method, so behavior is deterministic.

---

### Requirement 8: Reload the Firewall

**User Story:** As a maintenance coordinator, I want to reload ufw so that rule changes
are re-evaluated against the kernel netfilter tables.

#### Acceptance Criteria

1. WHEN the coordinator requests a firewall reload THE SYSTEM SHALL invoke `ufw reload`
   via the command runner.
2. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuFirewallActionResult` with
   status `SUCCESS`.
3. WHEN the command fails THE SYSTEM SHALL return a `UbuntuFirewallActionResult` with
   status `FAILED`, the exit code, and the captured stderr.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL return
   a `UbuntuFirewallActionResult` with status `DRY_RUN`.

---

### Requirement 9: Query Zone State

**User Story:** As a maintenance coordinator, I want to query the current state of ufw
including its active rules and ports, so that I can make conditional decisions about
what changes are needed without taking any action.

#### Acceptance Criteria

1. WHEN the coordinator provides `zone="default"` THE SYSTEM SHALL invoke
   `ufw status verbose` via the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuZoneInfo` record with
   `zone="default"`, the services list, ports list, and rich rules list parsed from the
   output, and empty `interfaces` and `sources` lists.
3. WHEN `zone` is not `"default"` THE SYSTEM SHALL raise a `UbuntuFirewallQueryError`
   explaining that ufw supports only the `"default"` zone.
4. WHEN `ufw status verbose` fails with a permission error THE SYSTEM SHALL raise a
   `UbuntuFirewallQueryError` carrying the exit code and stderr (reads do NOT elevate
   per contract, so non-root invocations may observe this).

---

### Requirement 10: Query All Zones

**User Story:** As a maintenance coordinator, I want to retrieve a uniform list of
zones, so that cross-OS orchestration iterates without special-casing ufw.

#### Acceptance Criteria

1. WHEN the coordinator requests all zones THE SYSTEM SHALL invoke `ufw status verbose`
   via the command runner and return a single-entry list containing the `"default"`
   `UbuntuZoneInfo`.
2. WHEN the command succeeds THE SYSTEM SHALL populate `is_default=True` on the returned
   `UbuntuZoneInfo` entry.
3. WHEN the command fails THE SYSTEM SHALL raise a `UbuntuFirewallQueryError` with the
   exit code and captured stderr.

---

### Requirement 11: Query UFW State

**User Story:** As a maintenance coordinator, I want to query whether ufw is active,
so that I can verify the firewall is running before applying rule changes.

#### Acceptance Criteria

1. WHEN the coordinator requests the firewall state THE SYSTEM SHALL invoke `ufw status`
   via the command runner without elevation.
2. WHEN the command succeeds and the output begins with the literal `Status: active`
   THE SYSTEM SHALL return a `UbuntuFirewallState` with `running=True` and
   `default_zone="default"`.
3. WHEN the command succeeds and the output begins with `Status: inactive` THE SYSTEM
   SHALL return a `UbuntuFirewallState` with `running=False` and `default_zone=""`.
4. WHEN `ufw status` exits non-zero THE SYSTEM SHALL return a `UbuntuFirewallState` with
   `running=False` and `default_zone=""` per the contract's firewall-not-running rule.

---

### Requirement 12: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all firewall write commands to run
with the privileges required by `ufw`, so that rule changes succeed on a standard system
where the process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner for all write operations (add/remove service,
   port, rich rule, set default zone, reload).
2. WHEN performing read-only operations (query zone, list all zones, get state) THE
   SYSTEM SHALL NOT pass `elevated=True`, regardless of config.
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no elevation
   and allow the command runner to surface the resulting permission error as a
   `UbuntuFirewallActionResult` with `status=FAILED` for writes or as a
   `UbuntuFirewallQueryError` for reads.
