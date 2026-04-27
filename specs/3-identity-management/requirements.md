# Requirements

## Introduction

The identity management feature provides the
`automojo-sysman-linux-ubuntu` package with the ability to enroll an
Ubuntu host in an identity realm — FreeIPA / Red Hat Identity
Management, Microsoft Active Directory, or a generic LDAP / Kerberos
realm — and to resolve directory-backed users and groups against the
local NSS stack.  This spec implements the cross-OS contract defined
at
`automojo-sysman-common/specs/contracts/identity-management/contract.md`
for Ubuntu using `realm` (realmd), `sssctl` (SSSD), and `getent`
(NSS).

A maintenance coordinator drives all operations by calling into this
feature's public surface.  The feature does not decide *which* realm
the host should belong to — it provides the capability to *manipulate
realm membership and inspect directory state* reliably, idempotently,
and with full auditability.  Every system command is executed
exclusively through the `automojo-interfaces` command-runner
abstraction, enabling dry-run simulation, auditing, and test mocking
without requiring a live system.  Administrator passwords passed to
`join_realm` are forwarded to `realm join` via stdin and never appear
in the argv captured on a `UbuntuIdentityActionResult`.

This feature does NOT manage local user accounts (see
`3-user-group-management`), does NOT configure PAM or pwquality (see
`3-authentication-management` once it lands on Ubuntu), and does NOT
manage CA trust anchors (see `3-certificate-trust-management`).

---

## Requirements

### Requirement 1: Join the host to a realm

**User Story:** As a maintenance coordinator, I want to join an
Ubuntu host to a FreeIPA, Active Directory, or LDAP realm using
supplied admin credentials, so that the host participates in the
declared identity directory.

#### Acceptance Criteria

1. WHEN the coordinator provides a `UbuntuRealmJoinSpec` with `realm`,
   `admin_user`, and `admin_password` THE SYSTEM SHALL build a
   `realm join --user=<admin_user> <realm>` argv (extended with
   `--computer-name=<spec.computer_name>`,
   `--computer-ou=<spec.ou_path>`, and `--server=<spec.domain_controller>`
   when those fields are set) and dispatch it through the runner with
   `elevated=True` when configured.
2. THE SYSTEM SHALL deliver `spec.admin_password` through the runner's
   `stdin` channel.  THE SYSTEM SHALL NOT place the password on argv.
3. WHEN `realm list` already shows the host joined to `spec.realm`
   THE SYSTEM SHALL skip execution and return a
   `UbuntuIdentityActionResult` with `status=SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return a `UbuntuIdentityActionResult` with
   `status=DRY_RUN` and the would-have-been argv in `command`.
5. WHEN `realm join` succeeds THE SYSTEM SHALL return a
   `UbuntuIdentityActionResult` with `status=SUCCESS`,
   `realm=spec.realm`, `username=spec.admin_user`, and the captured
   `exit_code`, `stdout`, and `stderr`.
6. WHEN `realm join` fails THE SYSTEM SHALL return a
   `UbuntuIdentityActionResult` with `status=FAILED`, the captured
   `exit_code` and `stderr`, and the sanitised argv in `command`.

---

### Requirement 2: Leave a realm

**User Story:** As a maintenance coordinator, I want to remove an
Ubuntu host from a realm, so that decommissioned or repurposed hosts
no longer authenticate against the directory.

#### Acceptance Criteria

1. WHEN the coordinator calls `leave_realm(realm)` THE SYSTEM SHALL
   dispatch `realm leave <realm>` through the runner with
   `elevated=True` when configured.
2. WHEN the caller passes `realm=None` THE SYSTEM SHALL fall back to
   `config.default_realm`; when both are `None` THE SYSTEM SHALL
   dispatch `realm leave` with no positional argument so realmd
   selects the host's primary realm.
3. WHEN `realm list` does not show the host joined to the requested
   realm THE SYSTEM SHALL skip execution and return
   `UbuntuIdentityActionResult` with `status=SKIPPED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return `status=DRY_RUN`.
5. WHEN `realm leave` succeeds THE SYSTEM SHALL return
   `status=SUCCESS` with the captured exit code and outputs.
6. WHEN `realm leave` fails THE SYSTEM SHALL return `status=FAILED`
   with the captured exit code, stderr, and argv.

---

### Requirement 3: Query realm-join status

**User Story:** As a maintenance coordinator, I want to know whether
a host is currently joined to a realm and, if so, which realm and
which back-end kind, so that downstream logic can branch on
membership state without parsing `realm list` output itself.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_realm_status()` THE SYSTEM SHALL
   dispatch `realm list` through the runner without elevation.
2. WHEN `realm list` exits zero with empty stdout THE SYSTEM SHALL
   return `UbuntuRealmStatus(joined=False)` with every other field
   set to `None`.
3. WHEN `realm list` reports at least one stanza THE SYSTEM SHALL
   return `UbuntuRealmStatus(joined=True)` with `current_realm` set
   to the first stanza's realm name, `realm_kind` mapped from the
   `server-software` line via `UbuntuRealmKind`
   (`ipa` → `FREEIPA`, `active-directory` → `ACTIVE_DIRECTORY`,
   `ldap` / `krb5` → `LDAP`, anything else → `UNKNOWN`), and
   `computer_name` set to the parsed `computer-name` line when present.
4. WHEN `realm list` exits non-zero THE SYSTEM SHALL raise
   `UbuntuIdentityQueryError` carrying the argv, exit code, and
   stderr.

---

### Requirement 4: List joined realms

**User Story:** As a maintenance coordinator, I want a structured list
of every realm the host is configured for, so that multi-realm hosts
can be inspected and reconciled.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_joined_realms()` THE SYSTEM SHALL
   dispatch `realm list` through the runner without elevation and
   parse the multi-stanza output into one `UbuntuRealmInfo` per
   stanza, preserving order.
2. WHEN `realm list` returns empty stdout (no realms configured) THE
   SYSTEM SHALL return an empty list rather than raising.
3. WHEN `realm list` exits non-zero THE SYSTEM SHALL raise
   `UbuntuIdentityQueryError`.
4. EACH parsed `UbuntuRealmInfo` SHALL carry `realm`, `realm_kind`,
   `is_default` (derived from the `configured: kerberos-member` line),
   `domain_controller` (always `None` on Ubuntu — `realm list` does
   not surface a single canonical DC), and `last_sync` (always
   `None`; reserved).

---

### Requirement 5: Synchronise users from the directory

**User Story:** As a maintenance coordinator, I want to invalidate the
SSSD cache so the next NSS lookup refreshes from the directory, so
that newly-added or recently-modified directory accounts become
visible without waiting for the natural refresh interval.

#### Acceptance Criteria

1. WHEN the coordinator calls `sync_users()` THE SYSTEM SHALL
   dispatch `sssctl cache-expire -E` through the runner with
   `elevated=True` when configured.
2. THE SYSTEM SHALL NOT apply an idempotency guard.  Every call MUST
   invoke `sssctl`.
3. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return `status=DRY_RUN`.
4. WHEN `sssctl` fails THE SYSTEM SHALL return `status=FAILED` with
   the captured exit code and stderr.

---

### Requirement 6: Resolve a user

**User Story:** As a maintenance coordinator, I want to resolve a
username through the local + directory NSS, so that downstream logic
can branch on existence and the user's uid / gid / home / shell /
realm without running `getent` itself.

#### Acceptance Criteria

1. WHEN the coordinator calls `resolve_user(username)` THE SYSTEM
   SHALL dispatch `getent passwd <username>` through the runner
   without elevation.
2. WHEN `getent` exits zero THE SYSTEM SHALL parse the colon-
   separated record into a `UbuntuUserResolveResult` with `found=True`
   and populated `uid`, `gid`, `home`, `shell` fields.
3. WHEN the parsed first field contains an `@` separator THE SYSTEM
   SHALL set `realm` to the suffix after the rightmost `@`; otherwise
   `realm` SHALL be `None`.
4. WHEN `getent` exits non-zero THE SYSTEM SHALL return a
   `UbuntuUserResolveResult` with `found=False` and every other field
   carrying its default.

---

### Requirement 7: Resolve a group

**User Story:** As a maintenance coordinator, I want to resolve a
groupname through the local + directory NSS, so that downstream
logic can branch on existence and the group's gid / members / realm
without running `getent` itself.

#### Acceptance Criteria

1. WHEN the coordinator calls `resolve_group(groupname)` THE SYSTEM
   SHALL dispatch `getent group <groupname>` through the runner
   without elevation.
2. WHEN `getent` exits zero THE SYSTEM SHALL parse the colon-
   separated record into a `UbuntuGroupResolveResult` with
   `found=True`, populated `gid`, and a `members` list parsed from
   the comma-separated fourth field.
3. WHEN the parsed first field contains an `@` separator THE SYSTEM
   SHALL set `realm` to the suffix after the rightmost `@`; otherwise
   `realm` SHALL be `None`.
4. WHEN `getent` exits non-zero THE SYSTEM SHALL return a
   `UbuntuGroupResolveResult` with `found=False`.

---

### Requirement 8: Dry-run, elevation, and credential handling

**User Story:** As a maintenance coordinator, I want every mutating
operation to respect dry-run, run with declared elevation, surface
failures structurally, and route credentials through stdin only.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT contact a KDC,
   domain controller, or SSSD instance and SHALL NOT dispatch any
   mutating command.
2. WHEN `config.elevated=True` THE SYSTEM SHALL dispatch every write
   verb (`join_realm`, `leave_realm`, `sync_users`) with
   `elevated=True` to the runner.
3. WHEN `config.elevated=True` and the coordinator calls a read verb
   (`get_realm_status`, `list_joined_realms`, `resolve_user`,
   `resolve_group`) THE SYSTEM SHALL still dispatch the underlying
   probe with `elevated=False`.
4. THE SYSTEM SHALL NEVER pass plain-text passwords on the argv.  The
   admin password MUST ride on the runner's `stdin` channel.
5. WHEN the runner returns a non-zero exit code on a write THE SYSTEM
   SHALL return a `UbuntuIdentityActionResult` with `status=FAILED`
   and the captured `exit_code` and `stderr`.

---

### Requirement 9: Realm-kind classification

**User Story:** As a maintenance coordinator, I want directory back-
end kinds reported in a normalised enum, so that platform-agnostic
logic does not have to interpret realmd's free-form
`server-software` field.

#### Acceptance Criteria

1. WHEN the parsed `server-software` value is `ipa` THE SYSTEM SHALL
   classify the realm as `UbuntuRealmKind.FREEIPA`.
2. WHEN the parsed `server-software` value is `active-directory` THE
   SYSTEM SHALL classify the realm as
   `UbuntuRealmKind.ACTIVE_DIRECTORY`.
3. WHEN the parsed `server-software` value is `ldap` or `krb5` THE
   SYSTEM SHALL classify the realm as `UbuntuRealmKind.LDAP`.
4. WHEN the parsed `server-software` value is anything else THE
   SYSTEM SHALL classify the realm as `UbuntuRealmKind.UNKNOWN`
   rather than raising.

---

### Requirement 10: Configurable executables

**User Story:** As a test author, I want to inject alternate
executable paths for `realm`, `sssctl`, and `getent`, so that
container / chroot test rigs can swap in stubs.

#### Acceptance Criteria

1. THE `UbuntuIdentityManagerConfig` dataclass SHALL expose
   `realm_executable` (default `"realm"`),
   `sssctl_executable` (default `"sssctl"`), and
   `getent_executable` (default `"getent"`) overrides.
2. EVERY argv built by the manager SHALL use the configured
   executable name as its argv[0].

---

### Requirement 11: Default realm fallback

**User Story:** As a maintenance coordinator that operates on a
single-realm host, I want to omit the realm argument on
`leave_realm` and `sync_users`, so that callers do not have to look
up the realm name out-of-band.

#### Acceptance Criteria

1. THE `UbuntuIdentityManagerConfig` dataclass SHALL expose a
   `default_realm: str | None = None` field.
2. WHEN `leave_realm(realm=None)` is called and
   `config.default_realm` is set THE SYSTEM SHALL substitute the
   configured value.
3. WHEN `leave_realm(realm=None)` is called and
   `config.default_realm` is `None` THE SYSTEM SHALL dispatch
   `realm leave` without a positional argument and SHALL NOT consult
   `realm list` for an idempotency check.

---

### Requirement 12: Query-error surface

**User Story:** As a maintenance coordinator, I want unexpected
read failures to surface as a typed exception rather than silent
empty data, so that broken realmd installations do not look like
"unjoined" hosts.

#### Acceptance Criteria

1. THE `UbuntuIdentityQueryError` exception SHALL be raised by every
   read verb when its underlying probe exits non-zero with no
   parseable empty-state output.
2. THE exception SHALL carry the failing argv, exit code, and stderr
   for diagnostics.
3. `resolve_user` and `resolve_group` SHALL NOT raise on
   `getent` exit code 2 (target absent); they SHALL return a
   `found=False` result instead.
