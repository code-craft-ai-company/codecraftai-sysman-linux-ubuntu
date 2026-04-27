# Design

## Overview

The filesystem-quotas feature is implemented as a single
module, `automojo.sysman.linux.ubuntu.filesystemquotas`,
exposing a `UbuntuFilesystemQuotasManager` class that wraps the
standard Linux quota family — `quotacheck`, `quotaon`,
`quotaoff`, `setquota`, `quota`, and `repquota` — through the
`ICommandRunner` abstraction. It also issues a `findmnt`
probe per write verb so non-mounted paths fail loudly before
any quota command is dispatched.

Ubuntu's quota tooling targets ext-family filesystems via the
shared `quota` package (`apt install quota`). XFS-specific
tooling (`xfs_quota`) and project quotas remain in the
canonical `automojo-sysman-linux-redhat` implementation; this
Ubuntu pilot intentionally targets the most common ext4
mountpoint surface, which covers the overwhelming majority of
Ubuntu deployments.

The manager follows the standard `ActionStatus` / `Config` /
`ActionResult` / `_dispatch` pattern shared with every other
Ubuntu feature. Public surface is the eight verbs of the cross-
OS contract:

| Verb | Kind | Returns |
|------|------|---------|
| `enable_quotas` | Write | `UbuntuQuotaActionResult` |
| `disable_quotas` | Write | `UbuntuQuotaActionResult` |
| `set_user_quota` | Write | `UbuntuQuotaActionResult` |
| `set_group_quota` | Write | `UbuntuQuotaActionResult` |
| `get_user_quota` | Read | `UbuntuQuotaInfo \| None` |
| `get_group_quota` | Read | `UbuntuQuotaInfo \| None` |
| `list_quotas` | Read | `list[UbuntuQuotaInfo]` |
| `get_quota_status` | Read | `UbuntuQuotaStatus` |

**See Also:**
- `automojo-sysman-common/specs/contracts/filesystem-quotas/contract.md`
  — cross-OS contract this implementation satisfies.
- `automojo-sysman-linux-redhat/specs/3-filesystem-quotas/`
  — canonical reference; covers XFS / `xfs_quota` / project
  quotas / grace-period verbs that are intentionally out of
  scope for this Ubuntu pilot.
- `3-filesystem-management` — owns mount-option management
  (`usrquota` / `grpquota`); this feature ASSUMES those options
  are already on the live mount.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│            Maintenance Coordinator                          │
│  - Declares quota limits per user / per group               │
│  - Toggles enforcement; reads current posture               │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│          UbuntuFilesystemQuotasManager                      │
│                                                             │
│  Mount validation:                                          │
│    _resolve_fstype(mountpoint) →                            │
│        UbuntuQuotaFilesystemNotMountedError on miss         │
│                                                             │
│  Enforcement state (writes):                                │
│    enable_quotas (quotacheck + quotaon)                     │
│    disable_quotas (quotaoff)                                │
│                                                             │
│  Limits (writes):                                           │
│    set_user_quota (setquota -u)                             │
│    set_group_quota (setquota -g)                            │
│                                                             │
│  Queries (reads, no elevation):                             │
│    get_user_quota (quota -u)                                │
│    get_group_quota (quota -g)                               │
│    list_quotas (repquota -ug)                               │
│    get_quota_status (quotaon -p + findmnt fstype)           │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
└─────────────────────────────────────────────────────────────┘
```

---

## Configuration

```python
@dataclass
class UbuntuFilesystemQuotasManagerConfig(FeatureConfig):
    """
        Inherits ``dry_run`` and ``elevated`` from FeatureConfig.

        :param quotacheck_format: Format string passed via
                                  ``quotacheck -F``.  Defaults to
                                  ``"vfsv0"`` (the modern format
                                  supported by ext4 / ext3 /
                                  ext2 with the Linux ``quota``
                                  package; ``vfsv1`` exists for
                                  64-bit-block deployments and
                                  may be passed by callers
                                  needing it).
    """
    quotacheck_format: str = "vfsv0"
```

---

## Data Models

```python
class UbuntuQuotaActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class UbuntuQuotaActionResult(FeatureActionResult):
    """
        Adds quota-scope fields on top of FeatureActionResult.

        :param mountpoint: The mountpoint acted upon (or None).
        :param principal: The user or group acted upon (or None).
    """
    mountpoint: str | None = None
    principal: str | None = None


class UbuntuQuotaPrincipalKind(Enum):
    """
        Enumeration of the principal flavours the manager can act on.
    """
    USER = "user"
    GROUP = "group"


@dataclass
class UbuntuQuotaInfo:
    """
        Per-principal quota record.  Field semantics mirror the
        cross-OS contract; ``inodes`` may be None on filesystems
        that do not enforce inode quotas.
    """
    mountpoint: str
    principal: str
    principal_kind: UbuntuQuotaPrincipalKind
    used_bytes: int | None
    soft_bytes: int | None
    hard_bytes: int | None
    used_inodes: int | None
    soft_inodes: int | None
    hard_inodes: int | None
    grace_period_seconds: int | None


@dataclass
class UbuntuQuotaStatus:
    """
        Mountpoint-wide enforcement posture.
    """
    mountpoint: str
    enabled: bool                         # enforcing OR accounting
    enforcing: bool                       # at least one principal kind on
    user_quotas_active: bool
    group_quotas_active: bool
    filesystem_type: str | None
```

### Exceptions

```python
class UbuntuQuotaQueryError(Exception):
    """Raised when a read fails unexpectedly."""


class UbuntuQuotaFilesystemNotMountedError(Exception):
    """
        Raised by writes when the supplied mountpoint is not
        currently mounted (per ``findmnt -n -o FSTYPE``).
    """
```

---

## Quota-Model Concept Mapping (Cross-OS)

The cross-OS contract speaks in `block_*` and `inode_*` units
expressed as bytes. Every OS has its own native unit; the
manager normalises to bytes on read and back to native units on
write.

| Concept | Cross-OS contract | Ubuntu native | Notes |
|---------|-------------------|---------------|-------|
| Soft block limit | `block_soft` (bytes) | `setquota` arg in 1-KiB blocks | Multiply by 1024 on read; divide by 1024 on write. |
| Hard block limit | `block_hard` (bytes) | `setquota` arg in 1-KiB blocks | Same as above. |
| Block usage | `block_used` (bytes) | `quota -u` reports KiB | Multiply by 1024 on read. |
| Inode soft / hard | `inode_*` (count) | `setquota` arg in counts | 0 = no inode enforcement. |
| Grace period | `grace_*` (str) | `quota -u` reports seconds | Mapped to `grace_period_seconds`. |
| Filesystem type | `fstype` | `findmnt -n -o FSTYPE` | `"ext4"`, `"ext3"`, ... |

### Inode-quota policy

The pilot defaults `setquota` inode arguments to `0 0`, i.e. no
inode enforcement. Inode quotas remain available via the
underlying tool but are not surfaced as a top-level Ubuntu
verb — the cross-OS contract names them but every common
Ubuntu deployment leaves them unset, and adding the inode
parameters to the public method signature would diverge from
the macOS / Windows peers without operational benefit.

---

## Key Method Designs

### `_resolve_fstype(mountpoint)`

1. Dispatch `["findmnt", "-n", "-o", "FSTYPE", mountpoint]`
   without elevation.
2. If exit non-zero → raise
   `UbuntuQuotaFilesystemNotMountedError`.
3. Return the stripped fstype string.

### `enable_quotas(mountpoint)`

1. Dry-run guard. Return `DRY_RUN` with the planned
   `quotacheck` argv recorded.
2. `_resolve_fstype(mountpoint)` — raises if not mounted.
3. Probe current state via `get_quota_status(mountpoint)`. If
   both `user_quotas_active` and `group_quotas_active` →
   `SKIPPED`.
4. Dispatch `["quotacheck", "-ug", "-mF", "vfsv0",
   mountpoint]`.
5. If `quotacheck` failed → return its `FAILED` result
   verbatim.
6. Dispatch `["quotaon", "-ug", mountpoint]`.
7. Return the `quotaon` result.

### `disable_quotas(mountpoint)`

1. Dry-run guard.
2. `_resolve_fstype(mountpoint)`.
3. Probe state. If both kinds already off → `SKIPPED`.
4. Dispatch `["quotaoff", "-ug", mountpoint]`.

### `set_user_quota(mountpoint, user, soft_kb, hard_kb)`

1. Dry-run guard with planned argv.
2. `_resolve_fstype(mountpoint)`.
3. `current = get_user_quota(mountpoint, user)`. If
   `current.soft_bytes == soft_kb * 1024` and
   `current.hard_bytes == hard_kb * 1024` → `SKIPPED`.
4. Dispatch `["setquota", "-u", user, str(soft_kb),
   str(hard_kb), "0", "0", mountpoint]`.

### `set_group_quota(mountpoint, group, soft_kb, hard_kb)`

Identical to `set_user_quota` but with `-g` and the group name.

### `get_user_quota(mountpoint, user)` / `get_group_quota(...)`

1. Dispatch `["quota", "-u", user]` (or `-g`) without
   elevation.
2. If exit not in `{0, 1, 2}` → raise
   `UbuntuQuotaQueryError`. Codes 1 and 2 are "no quotas
   defined" and "user has no quotas" respectively, which we
   treat as "no record" rather than failure.
3. Parse the multi-mount table; return the row whose mountpoint
   matches `mountpoint`, or `None` when no match.

### `list_quotas(mountpoint)`

1. Dispatch `["repquota", "-ug", mountpoint]` without
   elevation.
2. If exit non-zero → raise `UbuntuQuotaQueryError`.
3. Parse the User-quotas section and the Group-quotas section
   into a flat list keyed on `(principal_kind, principal)`.

### `get_quota_status(mountpoint)`

1. Dispatch `["quotaon", "-p", mountpoint]` without elevation.
2. If exit non-zero → raise `UbuntuQuotaQueryError`.
3. Parse lines of the form
   `group quota on /mnt (/dev/sda1) is on/off` into the two
   booleans.
4. Probe `findmnt -n -o FSTYPE <mountpoint>` (best-effort, no
   raise) for the `filesystem_type` field. A non-zero exit
   leaves the field `None`.
5. `enabled` is True if either kind is active. `enforcing` is
   True if either kind is active (Ubuntu accounting and
   enforcement are bound together for the common ext4 surface;
   the field exists for cross-OS parity).

---

## Parser Contracts

### `quota -u <user>` parser

Sample output:

```
Disk quotas for user alice (uid 1001):
     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace
       /dev/sda1  102400  204800  409600           1234       0       0
```

Parse rules:
- The first non-header data row whose `Filesystem` column
  matches `<mountpoint>` (or whose backing device is the device
  for `<mountpoint>`) is the row we want.
- `quota -u` reports the device, not the mountpoint, in the
  `Filesystem` column. The parser cross-references the
  filesystem column against the mountpoint by also accepting
  the literal `<mountpoint>` token (some `quota` versions
  accept `--show-mntpoint` and emit it).
- Block units are 1-KiB blocks; multiply by 1024 to get bytes.
- A trailing `*` on the `blocks` column indicates the soft
  limit is currently exceeded; parsing strips it.
- The `grace` column is `none` or a duration string like
  `7days`. The parser converts `7days` → `7 * 86400`. Anything
  unparseable → `None`.

### `repquota -ug <mountpoint>` parser

Sample output:

```
*** Report for user quotas on device /dev/sda1
Block grace time: 7days; Inode grace time: 7days
                        Block limits                File limits
User            used    soft    hard  grace    used  soft  hard  grace
----------------------------------------------------------------------
root      --       8       0       0              2     0     0
alice     --   102400  204800  409600           1234     0     0
*** Report for group quotas on device /dev/sda1
Block grace time: 7days; Inode grace time: 7days
                        Block limits                File limits
Group           used    soft    hard  grace    used  soft  hard  grace
----------------------------------------------------------------------
staff     --      16       0       0              4     0     0
```

Parse rules:
- Each `*** Report for <kind> quotas on device <dev>` line
  starts a new section.
- The principal-kind enum value is set per section.
- Skip header rows (column titles, dash separators, grace-time
  banner).
- The first column is the principal name. The second column is
  a `--` / `+-` / `-+` / `++` over-grace flag (ignored). Block
  used / soft / hard follow in 1-KiB blocks. Then optional
  block-grace, then inode used / soft / hard, then optional
  inode-grace.

### `quotaon -p <mountpoint>` parser

Sample output:

```
group quota on /mnt/data (/dev/sdb1) is off
user quota on /mnt/data (/dev/sdb1) is on
```

Parse rules:
- Lowercase first token: `user` / `group`.
- Last token: `on` / `off`.

---

## Dry-Run Behaviour

Dry-run short-circuits each write verb at the top of the method
body, before mount validation. The `command` field on the
returned `UbuntuQuotaActionResult` records the would-have-been
argv. Reads are not gated by dry-run.

---

## Elevation Policy

| Verb | `elevated` |
|------|-----------|
| `enable_quotas` | `config.elevated` |
| `disable_quotas` | `config.elevated` |
| `set_user_quota` | `config.elevated` |
| `set_group_quota` | `config.elevated` |
| `get_user_quota` | `False` |
| `get_group_quota` | `False` |
| `list_quotas` | `False` |
| `get_quota_status` | `False` |
| `_resolve_fstype` (mount probe) | `False` |

`config.elevated` defaults to False at the FeatureConfig level.
Operators wiring this manager into a maintenance coordinator
must set `elevated=True` on the config; the manager itself does
not assume privilege.

---

## Idempotency

The manager guards every write verb with a state probe that
costs at most one extra runner call:

| Verb | Probe | Skip condition |
|------|-------|----------------|
| `enable_quotas` | `get_quota_status(mountpoint)` | `user_quotas_active and group_quotas_active` |
| `disable_quotas` | `get_quota_status(mountpoint)` | `not user_quotas_active and not group_quotas_active` |
| `set_user_quota` | `get_user_quota(mountpoint, user)` | `current.soft_bytes == soft_kb*1024 and current.hard_bytes == hard_kb*1024` |
| `set_group_quota` | `get_group_quota(mountpoint, group)` | Same as user. |

The probe runs after the dry-run guard but before the actual
mutation, so dry-run paths never spend a round-trip on the
probe.

---

## Correctness Properties

1. **Mount validation precedes mutation.** Every write verb
   calls `_resolve_fstype(mountpoint)` before any quota
   command. A non-mounted path raises
   `UbuntuQuotaFilesystemNotMountedError`.
2. **Idempotent enable / disable.** Repeated invocations on a
   mountpoint already in the requested state return `SKIPPED`
   without dispatching the underlying command.
3. **Idempotent set.** Setting limits that already match the
   current record is a `SKIPPED` no-op.
4. **No elevation on reads.** Every read verb passes
   `elevated=False` to the runner regardless of
   `config.elevated`.
5. **Dry-run never dispatches.** Every write verb's dry-run
   path returns `DRY_RUN` with no runner call.
6. **Two-call enable.** Successful `enable_quotas` always
   results in exactly two runner calls (`quotacheck` then
   `quotaon`) plus one read for the idempotency probe and one
   for `findmnt`. A `quotacheck` failure short-circuits before
   `quotaon`.
7. **Unknown principal returns None.** `get_user_quota` /
   `get_group_quota` for an unknown principal return `None`,
   never raise. The parser's "no row matches mountpoint" path
   is the canonical no-record response.

---

## Filesystem-Mapping Table

| (fstype, target) | Supported? | Notes |
|------------------|------------|-------|
| `(ext4, USER)` | Yes | Canonical surface. |
| `(ext4, GROUP)` | Yes | Canonical surface. |
| `(ext3, USER)` | Yes | Same tooling. |
| `(ext3, GROUP)` | Yes | Same tooling. |
| `(ext2, USER)` | Yes (legacy) | `vfsv0` accounting works on ext2; the modern `quotacheck` flag set still applies. |
| `(xfs, *)` | No | XFS uses `xfs_quota`; out of scope for the Ubuntu pilot. Callers needing XFS should use `automojo-sysman-linux-redhat`. |
| `(btrfs, *)` | No | BTRFS quotas use `btrfs qgroup`; out of scope. |
| `(zfs, *)` | No | ZFS quotas use `zfs set userquota`; out of scope. |
| `(*, PROJECT)` | No | Project quotas are XFS-only on Linux; out of scope. |

The manager does NOT enforce this table — it dispatches the
ext-family tooling unconditionally. Callers running against
non-ext filesystems will surface the underlying tool's failure
through `FAILED` exit codes, which the manager reports verbatim
on the `UbuntuQuotaActionResult`.

---

## Testing Strategy

- `_MockCommandRunner` recording every call and returning
  pre-seeded `RunResult` values keyed by argv tuple.
- One command-construction test per write verb (assert the
  exact argv).
- One idempotency test per write verb (seed the probe with the
  matching state; assert `SKIPPED` and no mutation call).
- One dry-run test per write verb (assert no runner call).
- One parser test per `quota`, `repquota`, `quotaon -p` output.
- One unknown-principal test for `get_user_quota` /
  `get_group_quota` (assert `None`).
- One elevation-on-writes test (assert
  `call["elevated"] is True` for the mutation call).
- One no-elevation-on-reads test (assert
  `call["elevated"] is False` for every read).
- One mount-validation test (`findmnt` non-zero → raise
  `UbuntuQuotaFilesystemNotMountedError`).
- One failure-surface test (mutation command non-zero →
  `FAILED` with stderr captured).

No live `quota` / `repquota` / `quotaon` is invoked at any
point.

---

## Seams With Other Features

| Seam | Owner | This Feature |
|------|-------|--------------|
| Mount options (`usrquota`, `grpquota`) | `3-filesystem-management` | Coordinator sets mount options there first; this feature assumes they are active. A missing option surfaces as `quotaon` exit-code failure. |
| Target accounts / groups | `3-user-group-management` | Caller ensures names are valid; this feature does not verify principal existence. |
| Reporting / auditing | `2-maintenance-reporting` (future) | Out of scope for this pilot; the standard `feature_name = "filesystem-quotas"` hook leaves the door open. |
| XFS project quotas | `automojo-sysman-linux-redhat` | Canonical reference; not implemented in the Ubuntu pilot. |

---

## Out of Scope (Documented Drift From Contract)

The cross-OS contract names project quotas, grace-period
configuration, and per-target enable / disable distinctions.
This Ubuntu pilot intentionally narrows scope to:

- USER + GROUP only; PROJECT returns FAILED is not implemented
  (the manager simply does not expose a project verb).
- No grace-period setter; the read path surfaces
  `grace_period_seconds` on `UbuntuQuotaInfo` for reporting.
- `enable_quotas` / `disable_quotas` toggle BOTH user and
  group enforcement together. The contract's per-target lists
  are not surfaced — callers wanting fine-grained control
  should use the canonical RHEL implementation.

These deviations are purposeful: they keep the Ubuntu pilot
small and aligned with the macOS / Windows peers, which face
similar limitations in their native tooling.
