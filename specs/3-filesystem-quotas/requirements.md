# Requirements

## Introduction

The filesystem-quotas feature provides the
`codecraftai-sysman-linux-ubuntu` package with the ability to manage
disk quotas on Ubuntu / Debian systems for ext-family filesystems
through the standard Linux quota tooling: `quotacheck`,
`quotaon`, `quotaoff`, `setquota`, `quota`, and `repquota`. It
covers per-user and per-group block / inode quotas with
idempotent enable / disable / set semantics and read-only query
verbs for current usage and limits.

A maintenance coordinator uses this feature to declare and
enforce per-principal disk-usage policy on a mountpoint, to
report the current posture of that mountpoint, and to confirm
whether quota enforcement is currently active. Every system
command (`quotacheck`, `quotaon`, `quotaoff`, `setquota`,
`quota`, `repquota`) is dispatched through the
`codecraftai-interfaces` `ICommandRunner` abstraction so the manager
works identically under local and SSH execution. All mutating
operations honour dry-run.

This feature does NOT manage mount options. The mount-side
surface (`usrquota`, `grpquota`, `prjquota`) remains the
filesystem-management feature's responsibility; this feature
assumes the mount options are already active and surfaces a
clean failure when they are not. Project quotas, XFS-specific
tooling (`xfs_quota`), and grace-period configuration are out of
scope for this Ubuntu pilot — the feature targets the most
common ext4 deployment surface and refers callers needing those
capabilities to the canonical `codecraftai-sysman-linux-redhat`
implementation.

---

## Requirements

### Requirement 1: Enable Quota Enforcement

**User Story:** As a maintenance coordinator, I want to enable
quota enforcement on a mountpoint, so that subsequent
`setquota` calls take effect against active accounting.

#### Acceptance Criteria

1. WHEN the coordinator enables quotas on a mountpoint THE
   SYSTEM SHALL invoke `quotacheck -ug -mF vfsv0 <mountpoint>`
   to (re)build the on-disk accounting database, then
   `quotaon -ug <mountpoint>` to begin enforcement.
2. WHEN `quotaon -p <mountpoint>` already reports both user and
   group quotas as ON THE SYSTEM SHALL return
   `UbuntuQuotaActionStatus.SKIPPED` without dispatching either
   command.
3. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT invoke any
   runner command and SHALL return
   `UbuntuQuotaActionResult(status=DRY_RUN)` with the would-have-
   been argv recorded on `command`.
4. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner for both `quotacheck` and
   `quotaon`.
5. WHEN any of the dispatched commands exits non-zero THE
   SYSTEM SHALL return `FAILED` with the runner's exit code,
   captured stdout, captured stderr, and the failing argv.
6. WHEN the supplied `mountpoint` is not currently mounted THE
   SYSTEM SHALL raise `UbuntuQuotaFilesystemNotMountedError`
   before invoking any quota command.

---

### Requirement 2: Disable Quota Enforcement

**User Story:** As a maintenance coordinator, I want to disable
quota enforcement on a mountpoint without remounting, so that
operational posture can change in place.

#### Acceptance Criteria

1. WHEN the coordinator disables quotas THE SYSTEM SHALL invoke
   `quotaoff -ug <mountpoint>`.
2. WHEN `quotaon -p <mountpoint>` already reports both user and
   group quotas as OFF THE SYSTEM SHALL return `SKIPPED` without
   dispatching `quotaoff`.
3. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT invoke
   `quotaoff` and SHALL return `DRY_RUN`.
4. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.
5. WHEN `quotaoff` exits non-zero THE SYSTEM SHALL return
   `FAILED` with the captured exit code, stdout, stderr, and
   argv.

---

### Requirement 3: Set User Quota

**User Story:** As a maintenance coordinator, I want to set
soft / hard block limits for a specific user on a mountpoint,
so that disk-usage policy is enforced per principal.

#### Acceptance Criteria

1. WHEN the coordinator sets a user quota THE SYSTEM SHALL
   invoke `setquota -u <user> <soft_kb> <hard_kb> 0 0
   <mountpoint>` where `0 0` are the inode soft / hard limits
   (no inode enforcement) and the block limits are in 1-KiB
   blocks.
2. WHEN `quota -u <user>` already reports the requested
   `<soft_kb>` and `<hard_kb>` for `<mountpoint>` THE SYSTEM
   SHALL return `SKIPPED` without dispatching `setquota`.
3. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT invoke
   `setquota` and SHALL return `DRY_RUN`.
4. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.
5. WHEN `setquota` exits non-zero THE SYSTEM SHALL return
   `FAILED` with the captured exit code, stdout, stderr, and
   argv.

---

### Requirement 4: Set Group Quota

**User Story:** As a maintenance coordinator, I want to set
soft / hard block limits for a group on a mountpoint, so that
shared resources can be capped.

#### Acceptance Criteria

1. WHEN the coordinator sets a group quota THE SYSTEM SHALL
   invoke `setquota -g <group> <soft_kb> <hard_kb> 0 0
   <mountpoint>`.
2. WHEN `quota -g <group>` already reports the requested
   `<soft_kb>` and `<hard_kb>` for `<mountpoint>` THE SYSTEM
   SHALL return `SKIPPED` without dispatching `setquota`.
3. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT invoke
   `setquota` and SHALL return `DRY_RUN`.
4. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner.
5. WHEN `setquota` exits non-zero THE SYSTEM SHALL return
   `FAILED` with the captured exit code, stdout, stderr, and
   argv.

---

### Requirement 5: Query a User's Current Quota

**User Story:** As a maintenance coordinator, I want to read
the current usage and limits for a specific user on a
mountpoint, so that reports can summarise per-principal posture.

#### Acceptance Criteria

1. WHEN the coordinator queries a user quota THE SYSTEM SHALL
   invoke `quota -u <user>` (without elevation) and parse the
   per-mountpoint row into a `UbuntuQuotaInfo` carrying
   `used_bytes`, `soft_bytes`, `hard_bytes`, `used_inodes`,
   `soft_inodes`, `hard_inodes`, and `grace_period_seconds`.
2. WHEN no row matches `<mountpoint>` in the parsed output THE
   SYSTEM SHALL return `None` rather than raise.
3. WHEN `quota -u` exits with a status other than 0, 1, or 2
   THE SYSTEM SHALL raise `UbuntuQuotaQueryError`. (Status
   codes 1 and 2 are the standard `quota` exit codes for "no
   quotas defined" and "user has no quotas" respectively, both
   of which simply mean the principal has no record on this
   mount.)

---

### Requirement 6: Query a Group's Current Quota

**User Story:** As a maintenance coordinator, I want to read
the current usage and limits for a group on a mountpoint, so
that group-scoped reports can summarise posture.

#### Acceptance Criteria

1. WHEN the coordinator queries a group quota THE SYSTEM SHALL
   invoke `quota -g <group>` (without elevation) and parse the
   per-mountpoint row into a `UbuntuQuotaInfo`.
2. WHEN no row matches `<mountpoint>` in the parsed output THE
   SYSTEM SHALL return `None` rather than raise.
3. WHEN `quota -g` exits unexpectedly THE SYSTEM SHALL raise
   `UbuntuQuotaQueryError`.

---

### Requirement 7: List All Quotas on a Mountpoint

**User Story:** As a maintenance coordinator, I want to
enumerate every user and group quota active on a mountpoint, so
that compliance reports can be produced without polling each
principal individually.

#### Acceptance Criteria

1. WHEN the coordinator lists quotas THE SYSTEM SHALL invoke
   `repquota -ug <mountpoint>` (without elevation) and parse the
   output into a `list[UbuntuQuotaInfo]` covering every user
   and group entry.
2. WHEN `repquota` reports an empty body THE SYSTEM SHALL
   return an empty list.
3. WHEN `repquota` exits with a non-zero status THE SYSTEM
   SHALL raise `UbuntuQuotaQueryError`.

---

### Requirement 8: Query Quota Enforcement Status

**User Story:** As a maintenance coordinator, I want to query
whether quota enforcement is currently active on a mountpoint,
so that the idempotency guards on enable / disable have
authoritative state and so reports can summarise posture.

#### Acceptance Criteria

1. WHEN the coordinator queries the enforcement status THE
   SYSTEM SHALL invoke `quotaon -p <mountpoint>` (without
   elevation) and parse the output into a `UbuntuQuotaStatus`
   carrying `enabled`, `enforcing`, `user_quotas_active`,
   `group_quotas_active`, and the detected `filesystem_type`.
2. WHEN `quotaon -p` reports both user and group quotas as
   "off" THE SYSTEM SHALL return `UbuntuQuotaStatus(enabled=
   False, enforcing=False, user_quotas_active=False,
   group_quotas_active=False, ...)`.
3. WHEN `quotaon -p` exits with a non-zero status THE SYSTEM
   SHALL raise `UbuntuQuotaQueryError`.
4. WHEN the filesystem type is detected (via
   `findmnt -n -o FSTYPE <mountpoint>`) THE SYSTEM SHALL
   populate `filesystem_type` on the returned status.

---

### Requirement 9: Mount Validation Before Writes

**User Story:** As a maintenance coordinator, I want write
verbs to refuse a mountpoint that is not currently mounted, so
that the runner cannot dispatch a quota command against a stale
or absent path.

#### Acceptance Criteria

1. WHEN any write verb (`enable_quotas`, `disable_quotas`,
   `set_user_quota`, `set_group_quota`) is invoked with a
   mountpoint that is NOT currently mounted (per
   `findmnt -n -o FSTYPE <mountpoint>` reporting non-zero) THE
   SYSTEM SHALL raise `UbuntuQuotaFilesystemNotMountedError`
   before invoking any quota command.
2. WHEN dry-run mode is active THE SYSTEM SHALL NOT perform the
   mount-validation probe; the dry-run path short-circuits
   before any command is dispatched.

---

### Requirement 10: Dry-Run, Elevation, and Error Reporting

**User Story:** Standard dry-run / elevation / failure
semantics across every write verb.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT dispatch any
   mutating command and SHALL return
   `UbuntuQuotaActionResult(status=DRY_RUN)` with the would-
   have-been argv recorded on `command`.
2. WHEN `config.elevated=True` THE SYSTEM SHALL dispatch every
   write command with `elevated=True`. Reads MUST NEVER pass
   `elevated=True` to the runner, regardless of `config.elevated`.
3. WHEN the runner returns non-zero from a write command THE
   SYSTEM SHALL return `UbuntuQuotaActionResult(status=FAILED,
   exit_code=<code>, stdout=<captured>, stderr=<captured>,
   command=<argv>)`.

---

### Requirement 11: Idempotency Guards

**User Story:** As a maintenance coordinator, I want repeated
invocations of the same write verb to be no-ops when the
filesystem already matches the requested state, so that
configuration drift detection remains a property of the
underlying tooling rather than the manager.

#### Acceptance Criteria

1. WHEN `enable_quotas` is invoked on a mountpoint already
   reporting both user and group quotas as ON THE SYSTEM SHALL
   return `SKIPPED`.
2. WHEN `disable_quotas` is invoked on a mountpoint already
   reporting both user and group quotas as OFF THE SYSTEM SHALL
   return `SKIPPED`.
3. WHEN `set_user_quota` is invoked with `<soft_kb>` and
   `<hard_kb>` values that already match the user's current
   record THE SYSTEM SHALL return `SKIPPED`.
4. WHEN `set_group_quota` is invoked with `<soft_kb>` and
   `<hard_kb>` values that already match the group's current
   record THE SYSTEM SHALL return `SKIPPED`.
