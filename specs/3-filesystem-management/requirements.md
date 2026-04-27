# Requirements

## Introduction

The filesystem management feature provides the `automojo-sysman-linux-ubuntu` package
with the ability to mount and unmount devices, manage persistent mount-table entries,
create and remove directories and symbolic links, change ownership and permissions, and
inspect file / disk metadata on Ubuntu systems.  This spec implements the
filesystem-management contract defined at
`automojo-sysman-common/specs/contracts/filesystem-management/contract.md` for
Ubuntu using the standard POSIX tooling (`mount`, `umount`, `findmnt`, `stat`, `df`,
`mkdir`, `rm`, `ln`, `chmod`, `chown`) and an `IFileOperator` to edit `/etc/fstab`
transactionally.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *which*
paths should be mounted or which permissions are correct — it provides the capability
to *manipulate filesystem state* reliably, idempotently, and with full auditability.
All system commands are executed exclusively through the `automojo-interfaces`
command-runner abstraction; all file reads and writes against `/etc/fstab` flow through
an `IFileOperator` so that dry-run simulation and test mocking do not require a live
system.

---

## Requirements

### Requirement 1: Mount a Device

**User Story:** As a maintenance coordinator, I want to mount a block device or remote
share at a mount point, so that the mounted filesystem is available to the running
system.

#### Acceptance Criteria

1. WHEN the coordinator provides a `device`, `mountpoint`, optional `fstype`, and
   optional `options` THE SYSTEM SHALL construct the argv `mount` with
   `-t <fstype>` when `fstype` is set, `-o <options>` when `options` is set, and
   `<device> <mountpoint>` as the trailing positional arguments, dispatched through
   the command runner with `elevated=True` when configured.
2. WHEN the mount point is already mounted with the same device (detected via
   `findmnt -J <mountpoint>`) THE SYSTEM SHALL skip execution and return a
   `UbuntuFilesystemActionResult` with `status=SKIPPED`.
3. WHEN the mount succeeds THE SYSTEM SHALL return a `UbuntuFilesystemActionResult`
   with `status=SUCCESS`.
4. WHEN the mount fails THE SYSTEM SHALL return a `UbuntuFilesystemActionResult` with
   `status=FAILED` and the captured exit code, stdout, and stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `UbuntuFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 2: Unmount a Device

**User Story:** As a maintenance coordinator, I want to unmount a mount point,
optionally lazily, so that the device can be safely detached.

#### Acceptance Criteria

1. WHEN the coordinator calls `unmount(mountpoint, lazy=False)` THE SYSTEM SHALL
   dispatch `umount <mountpoint>` through the command runner with `elevated=True`
   when configured.
2. WHEN `lazy=True` THE SYSTEM SHALL dispatch `umount -l <mountpoint>` instead.
3. WHEN the mount point is not currently mounted (`findmnt -J <mountpoint>` exit code
   `1`) THE SYSTEM SHALL skip execution and return a `UbuntuFilesystemActionResult`
   with `status=SKIPPED`.
4. WHEN the unmount succeeds THE SYSTEM SHALL return a
   `UbuntuFilesystemActionResult` with `status=SUCCESS`.
5. WHEN the unmount fails THE SYSTEM SHALL return a `UbuntuFilesystemActionResult`
   with `status=FAILED` and the captured exit code, stdout, and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `UbuntuFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 3: Add a Persistent Mount-Table Entry

**User Story:** As a maintenance coordinator, I want to add an entry to `/etc/fstab`
so that the mount persists across reboots.

#### Acceptance Criteria

1. WHEN the coordinator provides a `UbuntuFstabEntry` THE SYSTEM SHALL read
   `/etc/fstab` via the file operator, check for an existing entry whose `device` or
   `mountpoint` matches the supplied entry, and (when no match is found) append a
   new line `<device>\t<mountpoint>\t<fstype>\t<options>\t<dump>\t<passno>` to the
   file's contents and write the result back via the file operator with
   `elevated=True` when configured.
2. WHEN an existing entry already matches the new entry exactly (all six fields)
   THE SYSTEM SHALL skip execution and return a `UbuntuFilesystemActionResult` with
   `status=SKIPPED`.
3. WHEN an existing entry matches `device` or `mountpoint` but not all fields THE
   SYSTEM SHALL replace the existing line with the new one and write the file back
   (effectively an update).
4. WHEN the write succeeds THE SYSTEM SHALL return a
   `UbuntuFilesystemActionResult` with `status=SUCCESS`.
5. WHEN the write fails THE SYSTEM SHALL return a `UbuntuFilesystemActionResult`
   with `status=FAILED` and a `detail` describing the step that failed.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT write the file and SHALL return
   a `UbuntuFilesystemActionResult` with `status=DRY_RUN` and a unified diff of the
   intended change in the result's `detail`.

---

### Requirement 4: Remove a Persistent Mount-Table Entry

**User Story:** As a maintenance coordinator, I want to remove an entry from
`/etc/fstab` so that the mount no longer persists across reboots.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_fstab_entry(device_or_mountpoint)` THE SYSTEM
   SHALL read `/etc/fstab`, filter out any line whose first or second whitespace-
   separated field matches the supplied token, and write the result back.
2. WHEN no matching line is found THE SYSTEM SHALL skip execution and return a
   `UbuntuFilesystemActionResult` with `status=SKIPPED`.
3. WHEN the write succeeds THE SYSTEM SHALL return a
   `UbuntuFilesystemActionResult` with `status=SUCCESS`.
4. WHEN the write fails THE SYSTEM SHALL return a `UbuntuFilesystemActionResult`
   with `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write the file and SHALL return
   a `UbuntuFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 5: Query Current Mounts

**User Story:** As a maintenance coordinator, I want to list every active mount, so
that idempotency guards and reporting workflows can reason about what is currently
mounted.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_mounts()` THE SYSTEM SHALL dispatch
   `findmnt -J -l` through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the JSON output into a list of
   `UbuntuMountInfo` records with `device` (from `source`), `mountpoint` (from
   `target`), `fstype`, and `options`.
3. WHEN the command fails THE SYSTEM SHALL raise a `UbuntuFilesystemQueryError`
   carrying the exit code and stderr.

---

### Requirement 6: Create a Directory

**User Story:** As a maintenance coordinator, I want to create a directory with
specific mode, owner, and group, so that application data paths exist before the
application runs.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_directory(path, mode, owner, group)` THE SYSTEM
   SHALL dispatch `mkdir -p <path>` through the command runner with `elevated=True`
   when configured, then (when `mode` is not `None`) dispatch `chmod <mode> <path>`,
   and (when `owner` or `group` is not `None`) dispatch
   `chown <owner>:<group> <path>`.
2. WHEN the path already exists as a directory with matching mode, owner, and group
   THE SYSTEM SHALL skip execution and return a `UbuntuFilesystemActionResult` with
   `status=SKIPPED`.
3. WHEN the path exists but is not a directory THE SYSTEM SHALL return a
   `UbuntuFilesystemActionResult` with `status=FAILED` and a `detail` naming the
   existing path type.
4. WHEN every dispatched command succeeds THE SYSTEM SHALL return a
   `UbuntuFilesystemActionResult` with `status=SUCCESS`.
5. WHEN any dispatched command fails THE SYSTEM SHALL stop dispatching further
   steps and return a `UbuntuFilesystemActionResult` with `status=FAILED`, the exit
   code and stderr of the failing step.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `UbuntuFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 7: Remove a Directory

**User Story:** As a maintenance coordinator, I want to remove a directory,
optionally recursively, so that obsolete directories are cleaned up.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_directory(path, recursive=False)` THE SYSTEM
   SHALL dispatch `rmdir <path>` through the command runner with `elevated=True`
   when configured.
2. WHEN `recursive=True` THE SYSTEM SHALL dispatch `rm -rf <path>` instead.
3. WHEN the path does not exist THE SYSTEM SHALL skip execution and return a
   `UbuntuFilesystemActionResult` with `status=SKIPPED`.
4. WHEN the path exists but is not a directory THE SYSTEM SHALL return a
   `UbuntuFilesystemActionResult` with `status=FAILED`.
5. WHEN the command succeeds THE SYSTEM SHALL return a
   `UbuntuFilesystemActionResult` with `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `UbuntuFilesystemActionResult`
   with `status=FAILED` and the captured exit code and stderr.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `UbuntuFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 8: Create a Symbolic Link

**User Story:** As a maintenance coordinator, I want to create a symbolic link from
`link` to `target`, so that path indirections can be established reliably.

#### Acceptance Criteria

1. WHEN the coordinator calls `create_symlink(link, target)` THE SYSTEM SHALL
   dispatch `ln -s <target> <link>` through the command runner with `elevated=True`
   when configured.
2. WHEN `link` already exists as a symlink pointing to `target` THE SYSTEM SHALL
   skip execution and return a `UbuntuFilesystemActionResult` with `status=SKIPPED`.
3. WHEN `link` already exists as a symlink pointing to a different target THE
   SYSTEM SHALL dispatch `ln -snf <target> <link>` to atomically replace the link.
4. WHEN `link` exists but is NOT a symlink THE SYSTEM SHALL return a
   `UbuntuFilesystemActionResult` with `status=FAILED` and a `detail` noting the
   conflict.
5. WHEN the command succeeds THE SYSTEM SHALL return a
   `UbuntuFilesystemActionResult` with `status=SUCCESS`.
6. WHEN the command fails THE SYSTEM SHALL return a `UbuntuFilesystemActionResult`
   with `status=FAILED`.
7. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `UbuntuFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 9: Remove a Symbolic Link

**User Story:** As a maintenance coordinator, I want to remove a symbolic link so
that an obsolete indirection is undone.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_symlink(link)` THE SYSTEM SHALL dispatch
   `rm <link>` through the command runner with `elevated=True` when configured,
   but only after verifying via `stat` that the path is a symbolic link.
2. WHEN the link does not exist THE SYSTEM SHALL skip execution and return a
   `UbuntuFilesystemActionResult` with `status=SKIPPED`.
3. WHEN the path exists but is NOT a symbolic link THE SYSTEM SHALL return a
   `UbuntuFilesystemActionResult` with `status=FAILED` and a `detail` naming the
   actual path type.
4. WHEN the command succeeds THE SYSTEM SHALL return a
   `UbuntuFilesystemActionResult` with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `UbuntuFilesystemActionResult`
   with `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `UbuntuFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 10: Change Ownership

**User Story:** As a maintenance coordinator, I want to change the owner and group
of a path, so that file-system ACLs match the running service's identity.

#### Acceptance Criteria

1. WHEN the coordinator calls `chown(path, user, group)` THE SYSTEM SHALL dispatch
   `chown <user>:<group> <path>` through the command runner (with `:<group>`
   omitted when `group` is `None` and `<user>` omitted when `user` is `None` —
   resulting in `:<group>`), with `elevated=True` when configured.
2. WHEN the current owner and group already match the requested values THE SYSTEM
   SHALL skip execution and return a `UbuntuFilesystemActionResult` with
   `status=SKIPPED`.
3. WHEN the path does not exist THE SYSTEM SHALL return a
   `UbuntuFilesystemActionResult` with `status=FAILED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a
   `UbuntuFilesystemActionResult` with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `UbuntuFilesystemActionResult`
   with `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `UbuntuFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 11: Change Mode

**User Story:** As a maintenance coordinator, I want to change the POSIX mode of a
path, so that file-system permissions match the policy for the managed service.

#### Acceptance Criteria

1. WHEN the coordinator calls `chmod(path, mode)` THE SYSTEM SHALL dispatch
   `chmod <octal_mode> <path>` through the command runner with `elevated=True` when
   configured, where `<octal_mode>` is the 3- or 4-digit octal string formatted
   from the integer argument.
2. WHEN the current mode already equals the requested mode THE SYSTEM SHALL skip
   execution and return a `UbuntuFilesystemActionResult` with `status=SKIPPED`.
3. WHEN the path does not exist THE SYSTEM SHALL return a
   `UbuntuFilesystemActionResult` with `status=FAILED`.
4. WHEN the command succeeds THE SYSTEM SHALL return a
   `UbuntuFilesystemActionResult` with `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return a `UbuntuFilesystemActionResult`
   with `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `UbuntuFilesystemActionResult` with `status=DRY_RUN`.

---

### Requirement 12: Stat a Path

**User Story:** As a maintenance coordinator, I want to retrieve metadata about a
path, so that downstream logic can branch on type, mode, owner, group, and size
without having to parse raw `stat` output.

#### Acceptance Criteria

1. WHEN the coordinator calls `stat(path)` THE SYSTEM SHALL dispatch
   `stat -c '%F|%a|%U|%G|%s|%n' <path>` through the command runner without
   elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the pipe-separated output into
   a `UbuntuFileInfo` with `path_type` (from `%F`), `mode` (from `%a`), `owner`
   (from `%U`), `group` (from `%G`), `size_bytes` (from `%s`), and `path` (from
   `%n`).
3. WHEN `stat` exits with a non-zero code whose stderr mentions `No such file or
   directory` THE SYSTEM SHALL return `None`.
4. WHEN `stat` exits non-zero for any other reason THE SYSTEM SHALL raise a
   `UbuntuFilesystemQueryError`.

---

### Requirement 13: Query Disk Usage

**User Story:** As a maintenance coordinator, I want to query disk-usage for a
path's filesystem, so that capacity-planning workflows can branch on current
utilisation.

#### Acceptance Criteria

1. WHEN the coordinator calls `query_disk_usage(path)` THE SYSTEM SHALL dispatch
   `df -B1 --output=size,used,avail,pcent <path>` through the command runner
   without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the header-plus-one-line output
   into a `UbuntuDiskUsageInfo` with `path`, `total_bytes`, `used_bytes`,
   `available_bytes`, and `use_percent`.
3. WHEN the command fails THE SYSTEM SHALL raise a `UbuntuFilesystemQueryError`.

---

### Requirement 14: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all filesystem write
operations to run with root privilege via `sudo`, so that mount / unmount /
chmod / chown / fstab edits succeed on a standard system where the process does
not run as root.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner and the file operator for every write
   (mount, unmount, add_fstab_entry, remove_fstab_entry, create_directory,
   remove_directory, create_symlink, remove_symlink, chown, chmod).
2. WHEN performing read-only operations (get_mounts, stat, query_disk_usage) THE
   SYSTEM SHALL NOT pass `elevated=True`, regardless of configuration.
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no
   elevation and allow the command runner or file operator to surface the
   resulting permission error as a `UbuntuFilesystemActionResult` with
   `status=FAILED` for writes or as a `UbuntuFilesystemQueryError` for reads.
