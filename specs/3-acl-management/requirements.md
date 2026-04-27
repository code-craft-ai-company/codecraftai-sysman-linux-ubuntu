# Requirements

## Introduction

The ACL management feature provides the
`automojo-sysman-linux-ubuntu` package with the ability to query and
mutate POSIX Access Control Lists (ACLs) on individual filesystem
paths.  This spec implements the ACL-management contract defined at
`automojo-sysman-common/specs/contracts/acl-management/contract.md`
for Ubuntu using `getfacl` (read) and `setfacl` (write) — the
canonical POSIX ACL tools shipped in the `acl` package on Debian and
Ubuntu.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public verbs.  The feature
does not decide which ACL a path should carry — it provides the
capability to apply, observe, and idempotently re-apply ACLs reliably
and with full auditability.  Every system command is executed
exclusively through the `automojo-interfaces` command-runner
abstraction; this module does not import `subprocess`, does not call
`open`, and does not consume an `IFileOperator`.  Persistence is
owned by `setfacl` itself (extended attributes on the underlying ext4
/ xfs / btrfs inode); the manager relies on `setfacl` to write the
on-disk state.

The OS-prefixed types in this module are `UbuntuAclManager`,
`UbuntuAclManagerConfig`, `UbuntuAclActionStatus` (alias of the
package-shared `ActionStatus`), `UbuntuAclActionResult`,
`UbuntuAceInfo`, `UbuntuAceSpec`, `UbuntuAclInfo`,
`UbuntuAcePrincipalType`, and `UbuntuAclQueryError`.

This feature complements — and does not duplicate — the basic-
permission surface owned by `3-filesystem-management` (`chmod`,
`chown`).  POSIX ACLs are conceptually a superset of the classic
owner / group / other model; this module treats the base entries as
read-only context and operates exclusively on extended (named) ACEs
and on default ACEs that govern inheritance.

---

## Requirements

### Requirement 1: Query the Full ACL of a Path

**User Story:** As a maintenance coordinator, I want to read the
current POSIX ACL of a file or directory, so that compliance checks
and diffs are possible.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_acl(path)` THE SYSTEM SHALL invoke
   `getfacl <path>` through the command runner with `elevated=False`
   and parse the output into an `UbuntuAclInfo`.
2. THE SYSTEM SHALL populate `UbuntuAclInfo.owner` and
   `UbuntuAclInfo.group` from the leading `# owner:` / `# group:`
   header lines emitted by `getfacl`.
3. THE SYSTEM SHALL populate `UbuntuAclInfo.aces` with every non-
   default ACE row, in source order, as `UbuntuAceInfo` records.
4. THE SYSTEM SHALL populate `UbuntuAclInfo.default_aces` with every
   `default:` row, in source order, as `UbuntuAceInfo` records.
5. WHEN `getfacl` exits non-zero THE SYSTEM SHALL raise
   `UbuntuAclQueryError` carrying the failed argv, exit code, and
   captured stderr.
6. THE SYSTEM SHALL NOT pass `elevated=True` to the runner.

---

### Requirement 2: Replace the Full ACL on a Path

**User Story:** As a maintenance coordinator, I want to replace the
entire extended ACL on a path with a declared specification, so that
access aligns deterministically with the desired posture.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_acl(path, acl_spec)` THE SYSTEM
   SHALL first invoke `setfacl -b <path>` to clear all extended
   entries, then invoke
   `setfacl -m <comma-joined entries> <path>` to install the
   declared ACEs.
2. WHEN the parsed current ACL is byte-equal to `acl_spec` THE SYSTEM
   SHALL return `status=SKIPPED` without invoking either `setfacl`
   step.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner at all and SHALL populate the would-
   have-been argv on `command`.
4. WHEN the runner reports a non-zero exit on either step THE SYSTEM
   SHALL return `status=FAILED` with `exit_code`, `stdout`, `stderr`,
   and `command` populated.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner for both `setfacl` steps.

---

### Requirement 3: Add a Single ACE

**User Story:** As a maintenance coordinator, I want to add a single
ACE for a named principal with a specific rights string, so that I
can grant access incrementally without reasserting the full ACL.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_ace(path, principal, rights)` THE
   SYSTEM SHALL invoke `setfacl -m <token> <path>` where `<token>` is
   `[d:]<type>:<name>:<rights>` rendered from the `UbuntuAceSpec`.
2. WHEN the parsed ACL already contains an ACE with the same
   principal-type / principal / rights / default-flag THE SYSTEM
   SHALL return `status=SKIPPED` without invoking `setfacl`.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
4. WHEN the runner reports a non-zero exit THE SYSTEM SHALL return
   `status=FAILED` with the captured runner state on the result.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner for the write.
6. THE SYSTEM SHALL populate `result.principal` with the spec's
   `principal` value on every returned result.

---

### Requirement 4: Remove ACEs by Principal

**User Story:** As a maintenance coordinator, I want to remove every
ACE belonging to a named principal, so that access can be revoked
without enumerating each rights string.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_ace(path, principal)` THE SYSTEM
   SHALL invoke `setfacl -x <token> <path>` where `<token>` is
   `[d:]<type>:<name>` rendered from the `UbuntuAceSpec`.  The
   `rights` field on the spec is intentionally ignored — `setfacl
   -x` accepts only the type / name pair.
2. WHEN the parsed ACL does NOT contain any ACE matching the spec's
   principal-type / principal / default-flag THE SYSTEM SHALL return
   `status=SKIPPED` without invoking `setfacl`.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
4. WHEN the runner reports a non-zero exit THE SYSTEM SHALL return
   `status=FAILED`.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner.

---

### Requirement 5: Clear All Extended ACEs

**User Story:** As a maintenance coordinator, I want to remove every
extended ACE from a path, so that access reverts to the classic
owner / group / other model.

#### Acceptance Criteria

1. WHEN the coordinator calls `clear_acl(path)` THE SYSTEM SHALL
   invoke `setfacl -b <path>`.
2. WHEN the parsed ACL has no extended ACEs (no named-user, no
   named-group, no non-base mask, no default ACEs) THE SYSTEM SHALL
   return `status=SKIPPED` without invoking `setfacl`.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
4. WHEN the runner reports a non-zero exit THE SYSTEM SHALL return
   `status=FAILED`.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner.
6. THE SYSTEM SHALL NOT remove the three base ACL rows
   (`user::<perm>`, `group::<perm>`, `other::<perm>`); those are
   owned by the classic mode bits and are mutated through
   `filesystem-management.chmod`.

---

### Requirement 6: Copy ACL Between Paths

**User Story:** As a maintenance coordinator, I want to copy the ACL
from one path onto another, so that I can replicate access posture
between siblings without re-rendering the full spec.

#### Acceptance Criteria

1. WHEN the coordinator calls `copy_acl(src, dst)` THE SYSTEM SHALL
   first invoke `getfacl <src>` (with `elevated=False`) and capture
   the stdout payload, then dispatch `setfacl --set-file=- <dst>`
   with the captured payload as `stdin`.
2. WHEN `getfacl` on `src` exits non-zero THE SYSTEM SHALL return
   `status=FAILED` populated with the read step's runner state and
   SHALL NOT invoke `setfacl`.
3. WHEN the parsed source and destination ACLs are byte-equal on
   both regular and default ACEs THE SYSTEM SHALL return
   `status=SKIPPED`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner for the `setfacl` step.  The
   `getfacl` read step never elevates.

---

### Requirement 7: Enumerate ACEs for a Path

**User Story:** As a maintenance coordinator, I want to enumerate
every ACE attached to a single path, so that compliance reports can
summarise per-path posture.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_aces(path)` THE SYSTEM SHALL
   invoke `getfacl <path>` and return a flat `list[UbuntuAceInfo]`
   covering both the regular and default ACEs.
2. THE SYSTEM SHALL emit regular ACEs first (in source order),
   followed by default ACEs (in source order).
3. WHEN `getfacl` exits non-zero THE SYSTEM SHALL raise
   `UbuntuAclQueryError`.
4. THE SYSTEM SHALL NOT pass `elevated=True` to the runner.

---

### Requirement 8: Default-ACL Support

**User Story:** As a maintenance coordinator, I want to read and
mutate default ACEs (the directory-only inheritance prototype), so
that newly-created entries inherit the declared access.

#### Acceptance Criteria

1. WHEN an `UbuntuAceSpec` carries `default=True` THE SYSTEM SHALL
   render the `setfacl -m` / `setfacl -x` token with a leading `d:`
   prefix.
2. WHEN parsing `getfacl` output THE SYSTEM SHALL set `default=True`
   on every `UbuntuAceInfo` that derives from a `default:`-prefixed
   row.
3. THE SYSTEM SHALL maintain regular and default ACEs in distinct
   `UbuntuAclInfo` collections; idempotency comparisons SHALL
   partition the comparison along the same dimension.
4. WHEN the requested ACE list mixes regular and default entries THE
   SYSTEM SHALL emit a single comma-joined `setfacl -m` token list,
   relying on `setfacl`'s native parsing to dispatch each entry to
   the correct partition.

---

### Requirement 9: Principal-Type Coverage

**User Story:** As a maintenance coordinator, I want every POSIX
ACL principal type to be representable, so that I can express the
full POSIX ACL model.

#### Acceptance Criteria

1. THE SYSTEM SHALL expose `UbuntuAcePrincipalType` with values
   `USER`, `GROUP`, `OTHER`, and `MASK`.
2. THE SYSTEM SHALL render `USER` as `u`, `GROUP` as `g`, `OTHER`
   as `o`, and `MASK` as `m` for both `setfacl -m` and `setfacl -x`
   tokens, matching the POSIX `acl(5)` short-form.
3. THE SYSTEM SHALL parse `getfacl`'s long-form prefixes
   (`user`, `group`, `other`, `mask`) into the corresponding enum
   value.
4. THE SYSTEM SHALL NOT expose Windows-style `EVERYONE` / `SYSTEM`
   on Ubuntu; cross-OS callers passing those values are owned by
   the contract layer and SHALL fail outside this feature.

---

### Requirement 10: Elevation, Dry-Run, and Aspect Composition

**User Story:** As a maintenance coordinator, I want every mutating
verb to honour the manager's elevation policy and to be safe under
arbitrary aspect composition on the runner, so that the manager
behaves identically against a `LocalCommandRunner`,
`SSHCommandRunner`, or any wrapped variant.

#### Acceptance Criteria

1. WHEN the manager is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the runner on every write
   (`set_acl`, `add_ace`, `remove_ace`, `clear_acl`, `copy_acl`).
2. WHEN the manager is configured with `elevated=False` THE SYSTEM
   SHALL never pass `elevated=True`.
3. THE SYSTEM SHALL never pass `elevated=True` on any read
   (`get_acl`, `list_aces`, every idempotency probe, the source-
   side read of `copy_acl`).
4. WHEN `dry_run=True` THE SYSTEM SHALL never invoke the runner for
   a mutating verb.
5. THE SYSTEM SHALL hold no state across calls beyond the injected
   config and runner.
6. THE SYSTEM SHALL be safe under any retry / persistence /
   timeout aspect composition on the runner; the manager makes no
   assumptions about how the runner dispatches commands.

---

### Requirement 11: Error Reporting

**User Story:** As an operator, I want failed ACL writes to surface
the underlying tool's error output, so that I can diagnose problems
without re-running commands manually.

#### Acceptance Criteria

1. WHEN any `setfacl` invocation exits non-zero THE SYSTEM SHALL
   return `UbuntuAclActionResult` populated with `status=FAILED`,
   `exit_code`, `stdout`, `stderr`, and `command`.
2. WHEN `getfacl` exits non-zero on a read verb THE SYSTEM SHALL
   raise `UbuntuAclQueryError` carrying the failed argv, exit code,
   and captured stderr.
3. WHEN `getfacl` exits non-zero on an idempotency probe inside a
   write verb THE SYSTEM SHALL fall through and dispatch the write
   anyway; the write step's failure mode then reports the underlying
   condition.

---

### Requirement 12: No Direct Filesystem Access

**User Story:** As an architect, I want the ACL manager to stay
within the runner-driven dispatch model, so that aspect composition
and SSH-execution semantics work uniformly.

#### Acceptance Criteria

1. THE SYSTEM SHALL NOT import `subprocess`, `os.system`, or any
   process-spawning module other than indirectly through the
   command runner.
2. THE SYSTEM SHALL NOT call `open()` to read or write any path.
3. THE SYSTEM SHALL NOT consume an `IFileOperator`; ACL state on
   Ubuntu lives in extended attributes on the inode, which `setfacl`
   manipulates directly.
4. THE SYSTEM SHALL surface every command (success and failure)
   through an `UbuntuAclActionResult` (write) or a parsed
   `UbuntuAclInfo` / raised `UbuntuAclQueryError` (read).
