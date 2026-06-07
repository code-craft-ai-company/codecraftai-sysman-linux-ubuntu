# Requirements

## Introduction

The authentication management feature provides the
`codecraftai-sysman-linux-ubuntu` package with the ability to manage the
Pluggable Authentication Modules (PAM) stack on Ubuntu hosts. It
implements the cross-OS authentication-management contract defined at
`codecraftai-sysman-common/specs/contracts/authentication-management/contract.md`
for the Ubuntu / Debian-derived family.

Whereas Red Hat ships the `authselect` profile manager as the canonical
PAM-stack orchestrator, Ubuntu ships `pam-auth-update`, a Debian-native
configuration multiplexer that composes the per-service stacks under
`/etc/pam.d/` from the registered profiles in
`/usr/share/pam-configs/`. The Ubuntu authentication manager wraps
`pam-auth-update` for method-level toggles, `cat` / `tee` for direct
PAM service-file edits, and pure-Python parsers for stack inspection.
Every command is dispatched through the injected `ICommandRunner`.

A maintenance coordinator running in an external package drives all
operations by calling this feature's public verbs.  The feature does
NOT decide which authentication policy belongs on the host — it
provides the capability to read, write, enable, disable, and validate
the live PAM stack reliably, idempotently, and auditably.

This feature complements — and does not duplicate — the SSH-daemon
hardening surface owned by `3-ssh-server-configuration`, the
local-account surface owned by `3-user-group-management`, and the
enterprise-directory integration surface owned by
`3-identity-management`. PAM is the glue between them.

---

## Requirements

### Requirement 1: Read a PAM Subsystem Configuration

**User Story:** As a maintenance coordinator, I want to read the
current PAM stack for a named subsystem (`login`, `sudo`, `su`, `sshd`,
`cron`), so I can compare it against a declared baseline.

#### Acceptance Criteria

1. WHEN `get_auth_config(subsystem)` is called with a recognised
   subsystem THE SYSTEM SHALL invoke `cat /etc/pam.d/<service>` via
   the command runner with `elevated=False`.
2. WHEN the file exists THE SYSTEM SHALL parse the PAM stack into a
   list of `UbuntuAuthEntry` records preserving `module_type`,
   `control`, `module_path`, and `args` in declaration order.
3. WHEN the subsystem maps to `UbuntuAuthSubsystem.OTHER` THE SYSTEM
   SHALL accept a free-form service name via the `name` slot of the
   subsystem enum and resolve the path the same way.
4. WHEN the file is missing THE SYSTEM SHALL raise
   `UbuntuAuthenticationSubsystemNotFoundError` rather than returning
   an empty config — an absent file indicates the subsystem is not
   configured, not that it has an empty stack.
5. WHEN the runner reports a non-zero exit code for any reason other
   than file-not-found THE SYSTEM SHALL raise
   `UbuntuAuthenticationQueryError` with the captured stderr.

---

### Requirement 2: Write a PAM Subsystem Configuration

**User Story:** As a maintenance coordinator, I want to write a
declared PAM stack to `/etc/pam.d/<service>`, so I can drive the
service's authentication policy from a versioned source-of-truth.

#### Acceptance Criteria

1. WHEN `set_auth_config(subsystem, config)` is called THE SYSTEM SHALL
   first read the current file content via `cat`, render the supplied
   `UbuntuAuthConfig` into PAM stack syntax, and compare both
   verbatim.
2. WHEN the rendered content equals the current content THE SYSTEM
   SHALL return `UbuntuAuthActionResult(status=SKIPPED)` without
   invoking `tee`.
3. WHEN the rendered content differs and `config.dry_run=True` THE
   SYSTEM SHALL return `status=DRY_RUN` with the would-have-been
   `tee` argv populated.
4. WHEN the rendered content differs and dry-run is off THE SYSTEM
   SHALL invoke `tee /etc/pam.d/<service>` with `stdin=<rendered>` and
   `elevated=True`.
5. WHEN the runner exit code is non-zero THE SYSTEM SHALL return
   `status=FAILED` with the captured `exit_code`, `stdout`, `stderr`,
   and `command`.

---

### Requirement 3: List Registered Authentication Methods

**User Story:** As a maintenance coordinator, I want to enumerate the
PAM modules and `pam-auth-update` profiles registered on the host, so
I can decide which to enable or disable.

#### Acceptance Criteria

1. WHEN `list_auth_methods()` is called THE SYSTEM SHALL invoke
   `pam-auth-update --list` and parse the human-readable output into
   `UbuntuAuthMethodInfo` records.
2. THE SYSTEM SHALL also enumerate `/usr/share/pam-configs/` via
   `ls -1` and parse each profile file's `Name:` and `Description:`
   keys to populate `description` and `module_path` fields.
3. THE enabled-state column from `pam-auth-update --list` SHALL be
   parsed into the `enabled: bool` field; profiles registered but not
   enabled SHALL be reported with `enabled=False`.
4. WHEN `pam-auth-update --list` exits non-zero THE SYSTEM SHALL fall
   back to enumerating `/usr/share/pam-configs/` only and synthesise
   `UbuntuAuthMethodInfo` records with `enabled=False` for every
   profile (the fallback documents that the registered set is known
   but the live state is not).
5. THE list operation SHALL execute with `elevated=False`.

---

### Requirement 4: Enable an Authentication Method

**User Story:** As a maintenance coordinator, I want to enable a named
PAM profile so that the subsystem stacks pick up the module on the
next composition.

#### Acceptance Criteria

1. WHEN `enable_method(name)` is called THE SYSTEM SHALL probe the
   live state via `list_auth_methods()` and return
   `status=SKIPPED` when the profile is already `enabled=True`.
2. WHEN `config.dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
3. WHEN the profile is registered but disabled THE SYSTEM SHALL invoke
   `pam-auth-update --enable <name>` with `elevated=True`.
4. WHEN the supplied `name` is not registered on the host THE SYSTEM
   SHALL raise `UbuntuAuthenticationSubsystemNotFoundError` rather
   than dispatching the runner — `pam-auth-update` would otherwise
   succeed with a no-op for unknown names on some Ubuntu releases.
5. WHEN the runner exit code is non-zero THE SYSTEM SHALL return
   `status=FAILED` with the captured stderr.

---

### Requirement 5: Disable an Authentication Method

**User Story:** As a maintenance coordinator, I want to disable a named
PAM profile so that the subsystem stacks drop the module on the next
composition.

#### Acceptance Criteria

1. WHEN `disable_method(name)` is called THE SYSTEM SHALL probe the
   live state via `list_auth_methods()` and return
   `status=SKIPPED` when the profile is already `enabled=False` or
   absent.
2. WHEN `config.dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
3. WHEN the profile is registered and enabled THE SYSTEM SHALL invoke
   `pam-auth-update --disable <name>` with `elevated=True`.
4. WHEN the runner exit code is non-zero THE SYSTEM SHALL return
   `status=FAILED` with the captured stderr.

---

### Requirement 6: Validate a PAM Subsystem Configuration

**User Story:** As a maintenance coordinator, I want a syntactic
validation pass over a PAM file so that I can detect malformed lines
before deploying the change.

#### Acceptance Criteria

1. WHEN `validate_auth_config(subsystem)` is called THE SYSTEM SHALL
   read the live file via `cat /etc/pam.d/<service>` and re-parse it
   using the internal stack parser.
2. THE SYSTEM SHALL return a `UbuntuAuthValidationResult` with
   `valid: bool`, `errors: list[str]`, and `warnings: list[str]`.
3. THE pilot release SHALL document that PAM has no canonical
   validator (no `pam-test` equivalent) and that the parser-driven
   check covers structural issues only — module-level invariants
   (e.g. `pam_unix.so` being loadable) require runtime evidence and
   are not in scope for this verb.
4. UNRECOGNISED `module_type` tokens, missing `control` tokens, and
   `include` cycles SHALL be reported as `errors`. Comment-only files
   and zero-line files SHALL be reported with a single warning but
   `valid=True`.
5. THE validate operation SHALL execute with `elevated=False`.

---

### Requirement 7: Subsystem Mapping

**User Story:** As a maintenance coordinator, I want a typed enum of
known PAM subsystems so I cannot pass a typo for a service name.

#### Acceptance Criteria

1. THE SYSTEM SHALL expose `UbuntuAuthSubsystem` with members
   `LOGIN`, `SUDO`, `SU`, `SSH`, `CRON`, `OTHER`.
2. EACH enum member except `OTHER` SHALL map to the canonical PAM
   service file name (`login`, `sudo`, `su`, `sshd`, `cron`).
3. WHEN `OTHER` is used the caller MUST also supply a `name` field on
   the `UbuntuAuthSubsystem` instance which is used as the literal
   service-file basename.
4. THE manager's verbs SHALL accept either the enum or a raw string;
   raw strings are routed through the OTHER pathway internally.

---

### Requirement 8: Idempotency

**User Story:** As a maintenance coordinator, I want every write verb
to be idempotent so I can re-run the manager without side effects.

#### Acceptance Criteria

1. THE SYSTEM SHALL return `SKIPPED` from `set_auth_config` when the
   rendered desired content equals the current file content
   verbatim.
2. THE SYSTEM SHALL return `SKIPPED` from `enable_method` when the
   profile is already enabled.
3. THE SYSTEM SHALL return `SKIPPED` from `disable_method` when the
   profile is already disabled or absent.
4. Reads (`get_auth_config`, `list_auth_methods`,
   `validate_auth_config`) SHALL never short-circuit and SHALL always
   reach the runner.

---

### Requirement 9: Elevation

**User Story:** As a maintenance coordinator, I want every PAM-stack
write to elevate while reads stay unprivileged.

#### Acceptance Criteria

1. THE SYSTEM SHALL pass `elevated=True` to the runner for every
   write verb (`set_auth_config`, `enable_method`,
   `disable_method`).
2. THE SYSTEM SHALL pass `elevated=False` to the runner for every
   read verb (`get_auth_config`, `list_auth_methods`,
   `validate_auth_config`).
3. WHEN `config.elevated=False` (an unusual configuration) THE SYSTEM
   SHALL still pass `elevated=False` for writes — the config controls
   the policy.
4. WHEN reads need to traverse a path the unprivileged caller cannot
   read (e.g. `/etc/sudoers.d/` group restrictions) THE failure
   surfaces as `UbuntuAuthenticationQueryError` and the caller is
   responsible for re-running the manager with `config.elevated=True`
   if needed — the contract carves out an explicit exception for that
   scenario.

---

### Requirement 10: Dry-Run and Error Reporting

**User Story:** As a maintenance coordinator, I want every write verb
to honour dry-run mode and surface failures with the underlying exit
code and stderr.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT invoke the runner
   for any write verb. THE SYSTEM SHALL return
   `UbuntuAuthActionResult(status=DRY_RUN, command=<would-have>)`.
2. WHEN the runner returns a non-zero exit code THE SYSTEM SHALL
   return `status=FAILED` with `exit_code`, `stdout`, `stderr`, and
   `command` populated.
3. THE SYSTEM SHALL NOT catch `SemanticError` and SHALL NOT silently
   swallow exceptions — error semantics match the rest of the
   package.
4. THE returned `UbuntuAuthActionResult` SHALL carry `subsystem` and
   `method` attributes when meaningful so downstream reports can
   route results without re-parsing the `command` argv.

---

### Requirement 11: Output Determinism

**User Story:** As a maintenance coordinator, I want renders of an
`UbuntuAuthConfig` to produce a deterministic file body so diffs are
stable across runs.

#### Acceptance Criteria

1. THE SYSTEM SHALL render entries in declaration order; entry order
   is significant in PAM stacks and MUST be preserved.
2. THE SYSTEM SHALL emit one entry per line in the form
   `<module_type> <control> <module_path> [<args>...]` separated by a
   single space.
3. THE SYSTEM SHALL terminate the file with a single trailing
   newline.
4. THE SYSTEM SHALL NOT emit any comment, marker, or banner that is
   not in the source `UbuntuAuthConfig` — the file is the user's
   declared truth.
