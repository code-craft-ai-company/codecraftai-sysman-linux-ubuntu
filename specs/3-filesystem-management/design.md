# Design

## Overview

The filesystem management feature is implemented as a single module,
`automojo.sysman.linux.ubuntu.filesystemmanagement`, that exposes a
`UbuntuFilesystemManager` class.  This spec implements the filesystem-management
contract defined at
`automojo-sysman-common/specs/contracts/filesystem-management/contract.md` for
Ubuntu using the POSIX toolchain (`mount`, `umount`, `findmnt`, `stat`, `df`, `mkdir`,
`rm`, `ln`, `chmod`, `chown`) together with transactional edits to `/etc/fstab`
through an `IFileOperator`.

All read paths used for idempotency guards go through `findmnt`, `stat`, or the file
operator reading `/etc/fstab`; output is parsed in pure Python into
`UbuntuMountInfo` / `UbuntuFileInfo` / `UbuntuFstabEntry` records.  All system
interaction is delegated to a command runner implementing the `ICommandRunner`
interface and to an `IFileOperator`.  No direct Python `open()` / `subprocess` calls
are made inside the module.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).  Under SSH the
`IFileOperator` uses SFTP for `/etc/fstab` read/write so atomic replace semantics
(write-temp-then-rename) survive connection drops.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator                      │
│  - Configures aspects (optional)                            │
│  - Injects both into UbuntuFilesystemManager                │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuFilesystemManager                          │
│  automojo.sysman.linux.ubuntu.filesystemmanagement          │
│                                                             │
│  mount()              unmount()                             │
│  add_fstab_entry()    remove_fstab_entry()                  │
│  get_mounts()                                               │
│  create_directory()   remove_directory()                    │
│  create_symlink()     remove_symlink()                      │
│  chown()              chmod()                               │
│  stat()               query_disk_usage()                    │
└─────────┬───────────────────────────────┬───────────────────┘
          │ ICommandRunner                │ IFileOperator
          ▼                               ▼
┌────────────────────────┐    ┌────────────────────────────┐
│ LocalCommandRunner     │    │ LocalFileOperator          │
│ SSHCommandRunner       │    │ SSHFileOperator (SFTP)     │
└────────────────────────┘    └────────────────────────────┘
          │                               │
          └────────────────┬──────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect: audit logging                         │
│  - RetryAspect: transient failure retry                     │
│  - TimeoutAspect: global timeout policy                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `UbuntuFilesystemManager`

**Location:** `source/packages/automojo/sysman/linux/ubuntu/filesystemmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `mount` | `(device, mountpoint, fstype, options) → UbuntuFilesystemActionResult` | `mount [-t <fstype>] [-o <options>] <device> <mountpoint>` |
| `unmount` | `(mountpoint, lazy) → UbuntuFilesystemActionResult` | `umount [-l] <mountpoint>` |
| `add_fstab_entry` | `(entry) → UbuntuFilesystemActionResult` | read-modify-write `/etc/fstab` via `IFileOperator` |
| `remove_fstab_entry` | `(device_or_mountpoint) → UbuntuFilesystemActionResult` | read-modify-write `/etc/fstab` |
| `get_mounts` | `() → list[UbuntuMountInfo]` | `findmnt -J -l` |
| `create_directory` | `(path, mode, owner, group) → UbuntuFilesystemActionResult` | `mkdir -p` [+ `chmod` + `chown`] |
| `remove_directory` | `(path, recursive) → UbuntuFilesystemActionResult` | `rmdir` or `rm -rf` |
| `create_symlink` | `(link, target) → UbuntuFilesystemActionResult` | `ln -s` or `ln -snf` |
| `remove_symlink` | `(link) → UbuntuFilesystemActionResult` | `rm <link>` after `stat` guard |
| `chown` | `(path, user, group) → UbuntuFilesystemActionResult` | `chown <user>:<group>` |
| `chmod` | `(path, mode) → UbuntuFilesystemActionResult` | `chmod <octal> <path>` |
| `stat` | `(path) → UbuntuFileInfo \| None` | `stat -c '%F\|%a\|%U\|%G\|%s\|%n'` |
| `query_disk_usage` | `(path) → UbuntuDiskUsageInfo` | `df -B1 --output=size,used,avail,pcent` |

### `UbuntuFilesystemManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands / file writes are dispatched |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `fstab_path` | `str` | `/etc/fstab` | Overridable for testing |

---

## Data Models

### Result Types

```python
class UbuntuFilesystemActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class UbuntuFilesystemActionResult:
    status: UbuntuFilesystemActionStatus
    path: str                           # target path or mount point
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
```

### Mount / Fstab / File / DiskUsage Types

```python
@dataclass
class UbuntuMountInfo:
    device: str
    mountpoint: str
    fstype: str
    options: str

@dataclass
class UbuntuFstabEntry:
    device: str
    mountpoint: str
    fstype: str
    options: str                        # default "defaults"
    dump: int                           # default 0
    passno: int                         # default 0

@dataclass
class UbuntuFileInfo:
    path: str
    path_type: str                      # e.g. "regular file", "directory", "symbolic link"
    mode: str                           # octal string, e.g. "755"
    owner: str
    group: str
    size_bytes: int

@dataclass
class UbuntuDiskUsageInfo:
    path: str
    total_bytes: int
    used_bytes: int
    available_bytes: int
    use_percent: int
```

### Exceptions

```python
class UbuntuFilesystemQueryError(Exception):
    """Raised when a findmnt / stat / df query fails unexpectedly."""
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `IFileOperator` (from `automojo-interfaces`)

```python
class IFileOperator(Protocol):
    def read_text(self, path: str, elevated: bool = False) -> str: ...
    def write_text(
        self, path: str, content: str, elevated: bool = False, atomic: bool = True,
    ) -> None: ...
    def exists(self, path: str, elevated: bool = False) -> bool: ...
```

`add_fstab_entry` and `remove_fstab_entry` use `read_text` and `write_text(atomic=True)`
so the fstab rewrite is a write-temp-then-rename sequence.

---

## Mount-Table Mapping

Ubuntu uses `/etc/fstab` as its persistent mount table, identical to RHEL.  The
`UbuntuFstabEntry` dataclass maps 1:1 to the six whitespace-separated fields in a
fstab line.  The manager preserves comment lines (lines starting with `#`) and
blank lines verbatim during the read-modify-write cycle so unrelated entries and
operator annotations survive updates.

---

## Command Mappings

### Write Commands

| Operation | Command |
|---|---|
| mount | `mount [-t <fstype>] [-o <options>] <device> <mountpoint>` |
| unmount | `umount [-l] <mountpoint>` |
| create_directory | `mkdir -p <path>` (+ `chmod` + `chown` when `mode` / `owner` / `group` set) |
| remove_directory | `rmdir <path>` or `rm -rf <path>` |
| create_symlink | `ln -s <target> <link>` (or `ln -snf` when `link` is an existing symlink to a different target) |
| remove_symlink | `rm <link>` |
| chown | `chown <user>:<group> <path>` |
| chmod | `chmod <octal> <path>` |
| add_fstab_entry | `IFileOperator.read_text(/etc/fstab)` → modify → `IFileOperator.write_text(..., atomic=True)` |
| remove_fstab_entry | `IFileOperator.read_text(/etc/fstab)` → filter → `IFileOperator.write_text(..., atomic=True)` |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| get_mounts | `findmnt -J -l` |
| stat | `stat -c '%F\|%a\|%U\|%G\|%s\|%n' <path>` |
| query_disk_usage | `df -B1 --output=size,used,avail,pcent <path>` |
| directory / symlink existence checks | `stat <path>` (see `stat` verb) |

All writes pass `elevated=True` when the config enables it.  Reads never elevate.

---

## Output Parsing

### `findmnt -J -l` output

```json
{
  "filesystems": [
    { "target": "/", "source": "/dev/mapper/vg0-root", "fstype": "ext4",
      "options": "rw,relatime,errors=remount-ro" },
    { "target": "/boot", "source": "/dev/sda2", "fstype": "ext2",
      "options": "rw,relatime" }
  ]
}
```

The parser calls `json.loads` and maps `target` → `mountpoint`, `source` → `device`,
`fstype` → `fstype`, `options` → `options`.

### `stat -c` output

```
directory|755|root|root|4096|/etc
```

The parser splits on `|` and returns:

- `path_type = "directory"`
- `mode = "755"`
- `owner = "root"`
- `group = "root"`
- `size_bytes = 4096`
- `path = "/etc"`

When stderr contains `No such file or directory`, `stat()` returns `None`.

### `df -B1 --output=...` output

```
       Size        Used       Avail Use%
 10485760000  3145728000  7340032000  30%
```

The parser skips the header line, splits the data line on whitespace, strips the
trailing `%` from `pcent`, and builds a `UbuntuDiskUsageInfo`.

### `/etc/fstab` line format

```
UUID=abcd-1234  /mnt/data  ext4  defaults,noatime  0  2
# managed by automojo
//server/share  /mnt/share  cifs  credentials=/etc/smbcred,_netdev  0  0
```

The parser:

1. Walks the file line-by-line, preserving comments and blank lines as opaque
   tokens in an ordered list.
2. For each non-comment, non-blank line, splits on whitespace into up to six
   fields; defaults `dump=0` and `passno=0` when absent.
3. Builds a `UbuntuFstabEntry` per line.

Write-back reverses the process and joins with `\t` separators, appending a single
`\n` at the end of the file.

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `mount` | mount already exists for `(device, mountpoint)` | `get_mounts()` |
| `unmount` | mount point not currently mounted | `get_mounts()` |
| `add_fstab_entry` | matching line already present | `read_text(/etc/fstab)` |
| `remove_fstab_entry` | no matching line | `read_text(/etc/fstab)` |
| `create_directory` | directory exists with matching mode / owner / group | `stat(path)` |
| `remove_directory` | path does not exist | `stat(path)` |
| `create_symlink` | existing symlink with matching target | `stat(link)` + `readlink` (via `stat -c '%N'`) |
| `remove_symlink` | link does not exist | `stat(link)` |
| `chown` | current owner / group already matches | `stat(path)` |
| `chmod` | current mode already matches | `stat(path)` |

### Type-Conflict Detection

- `create_symlink(link, target)` where `stat(link).path_type != "symbolic link"`
  → `FAILED` with `detail="<link> exists and is not a symlink"`.
- `remove_symlink(link)` where `stat(link).path_type != "symbolic link"` →
  `FAILED` with the same shape.
- `create_directory(path)` where `stat(path).path_type != "directory"` →
  `FAILED` with `detail="<path> exists and is not a directory"`.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UbuntuFilesystemQueryError` | `findmnt` / `df` exits non-zero; `stat` exits non-zero for a reason other than "no such file or directory" |

All write failures are surfaced as
`UbuntuFilesystemActionResult(status=FAILED, ...)` carrying the runner's exit code,
stdout, and stderr.  Writes never raise for routine I/O.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: mount Command Construction

*For any* `(device, mountpoint, fstype, options)` tuple, `mount()` should build a `mount` argv with `-t` and `-o` present only when those arguments are not `None`.

**Validates: Requirements 1.1**

### Property 2: mount Idempotency

*For any* `(device, mountpoint)` already present in `get_mounts()`, `mount()` should return `SKIPPED`.

**Validates: Requirements 1.2**

### Property 3: unmount Lazy Flag

*For any* `unmount(mountpoint, lazy=True)` call, the argv should include `-l`; for `lazy=False`, it should not.

**Validates: Requirements 2.1, 2.2**

### Property 4: unmount Idempotency

*For any* mountpoint absent from `get_mounts()`, `unmount()` should return `SKIPPED`.

**Validates: Requirements 2.3**

### Property 5: add_fstab_entry Idempotency

*For any* `UbuntuFstabEntry` whose six fields match an existing `/etc/fstab` line, `add_fstab_entry()` should return `SKIPPED` without writing the file.

**Validates: Requirements 3.2**

### Property 6: add_fstab_entry Update-on-Mismatch

*For any* `UbuntuFstabEntry` whose `device` or `mountpoint` matches an existing line but whose other fields differ, `add_fstab_entry()` should replace the existing line with the new one in a single atomic write.

**Validates: Requirements 3.3**

### Property 7: add_fstab_entry Preserves Unrelated Content

*For any* `add_fstab_entry()` call, unrelated lines (comments, blank lines, other entries) MUST be preserved verbatim in the output.

**Validates: Requirements 3.1 (read-modify-write semantics)**

### Property 8: remove_fstab_entry Idempotency

*For any* token that matches no line in `/etc/fstab`, `remove_fstab_entry()` should return `SKIPPED` without writing the file.

**Validates: Requirements 4.2**

### Property 9: get_mounts JSON Parsing

*For any* `findmnt -J -l` JSON output, `get_mounts()` should return a list of `UbuntuMountInfo` matching the `target`/`source`/`fstype`/`options` fields.

**Validates: Requirements 5.1, 5.2**

### Property 10: create_directory Orchestration

*For any* `(path, mode, owner, group)` with all three set, `create_directory()` should dispatch `mkdir -p <path>`, `chmod <mode> <path>`, `chown <owner>:<group> <path>` in that order.

**Validates: Requirements 6.1**

### Property 11: create_directory Idempotency

*For any* existing directory whose `stat` already matches `(mode, owner, group)`, `create_directory()` should return `SKIPPED`.

**Validates: Requirements 6.2**

### Property 12: create_directory Type Conflict

*For any* existing non-directory path, `create_directory()` should return `FAILED`.

**Validates: Requirements 6.3**

### Property 13: remove_directory Recursive Flag

*For any* `remove_directory(path, recursive=True)`, the argv should be `rm -rf <path>`; for `recursive=False`, `rmdir <path>`.

**Validates: Requirements 7.1, 7.2**

### Property 14: create_symlink Existing-Match Idempotency

*For any* existing symlink at `link` already pointing to `target`, `create_symlink()` should return `SKIPPED`.

**Validates: Requirements 8.2**

### Property 15: create_symlink Atomic Replace

*For any* existing symlink at `link` pointing to a different target, `create_symlink()` should dispatch `ln -snf <target> <link>` rather than `ln -s`.

**Validates: Requirements 8.3**

### Property 16: create_symlink Non-Symlink Conflict

*For any* existing non-symlink at `link`, `create_symlink()` should return `FAILED`.

**Validates: Requirements 8.4**

### Property 17: remove_symlink Type Check

*For any* existing non-symlink at `link`, `remove_symlink()` should return `FAILED` without dispatching `rm`.

**Validates: Requirements 9.3**

### Property 18: chown Idempotency

*For any* `(path, user, group)` whose current `stat` already matches, `chown()` should return `SKIPPED`.

**Validates: Requirements 10.2**

### Property 19: chmod Idempotency

*For any* `(path, mode)` whose current `stat.mode` already matches, `chmod()` should return `SKIPPED`.

**Validates: Requirements 11.2**

### Property 20: stat Output Parsing

*For any* `stat -c '%F|%a|%U|%G|%s|%n'` line, `stat()` should populate a `UbuntuFileInfo` with the correct fields.

**Validates: Requirements 12.1, 12.2**

### Property 21: stat Missing Returns None

*For any* path whose `stat` invocation fails with "No such file or directory", `stat()` should return `None`.

**Validates: Requirements 12.3**

### Property 22: query_disk_usage Parsing

*For any* `df -B1 --output=...` output, `query_disk_usage()` should populate a `UbuntuDiskUsageInfo` with integer byte counts and an integer `use_percent` (stripped of `%`).

**Validates: Requirements 13.1, 13.2**

### Property 23: query_disk_usage Failure

*For any* non-zero `df` exit, `query_disk_usage()` should raise `UbuntuFilesystemQueryError`.

**Validates: Requirements 13.3**

### Property 24: Dry-Run Safety

*For any* write operation with `dry_run=True`, the runner MUST NOT be invoked and the file operator MUST NOT perform `write_text`; the result status MUST be `DRY_RUN`.

**Validates: Requirements 1.5, 2.6, 3.6, 4.5, 6.6, 7.7, 8.7, 9.6, 10.6, 11.6**

### Property 25: Dry-Run Diff for add_fstab_entry

*For any* `add_fstab_entry()` call in dry-run, the result's `detail` MUST contain a unified diff of the intended change so operators can review it without applying it.

**Validates: Requirements 3.6**

### Property 26: Elevation for Writes

*For any* write with `elevated=True` in the config, the runner and file operator MUST be invoked with `elevated=True`.

**Validates: Requirements 14.1**

### Property 27: No Elevation for Reads

*For any* read operation (`get_mounts`, `stat`, `query_disk_usage`), the runner MUST NEVER be invoked with `elevated=True`.

**Validates: Requirements 14.2**

### Property 28: Error Result Consistency

*For any* non-zero exit from a write command, the returned `UbuntuFilesystemActionResult` should have `status=FAILED`, and carry the runner's exit code and stderr.

**Validates: Requirements 1.4, 2.5, 3.5, 4.4, 6.5, 7.6, 8.6, 9.5, 10.5, 11.5**

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/
├── __init__.py
└── filesystemmanagement.py      ← UbuntuFilesystemManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/ubuntu/
    └── test_filesystemmanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.linux.ubuntu.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.linux.ubuntu.fileoperators import LocalFileOperator
from automojo.sysman.linux.ubuntu.filesystemmanagement import (
    UbuntuFilesystemManager,
    UbuntuFilesystemManagerConfig,
    UbuntuFstabEntry,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_op = LocalFileOperator()

config = UbuntuFilesystemManagerConfig(elevated=True, dry_run=False)
fs_mgr = UbuntuFilesystemManager(runner=runner, file_operator=file_op, config=config)

# Mount an NFS share
result = fs_mgr.mount(
    device="nfs-server:/export/data",
    mountpoint="/mnt/data",
    fstype="nfs",
    options="rw,relatime,nofail",
)
print(f"mount: {result.status}")

# Persist the mount
entry = UbuntuFstabEntry(
    device="nfs-server:/export/data",
    mountpoint="/mnt/data",
    fstype="nfs",
    options="rw,relatime,nofail,_netdev",
    dump=0,
    passno=0,
)
fs_mgr.add_fstab_entry(entry=entry)

# Ensure application data directory with strict perms
fs_mgr.create_directory(path="/var/lib/myapp", mode=0o750, owner="myapp",
                        group="myapp")

# Swap the symlink atomically
fs_mgr.create_symlink(link="/opt/myapp/current", target="/opt/myapp/releases/2025-04")

# Inspect
info = fs_mgr.stat(path="/var/lib/myapp")
print(f"path_type={info.path_type} mode={info.mode} owner={info.owner}")

usage = fs_mgr.query_disk_usage(path="/var/lib/myapp")
print(f"disk {usage.use_percent}%")
```

### SSH Execution with Aspects

```python
from automojo.sysman.linux.ubuntu.runners import SSHCommandRunner, SSHRunnerConfig
from automojo.sysman.linux.ubuntu.fileoperators import SSHFileOperator
from automojo.sysman.linux.ubuntu.aspects import (
    PersistenceAspect, RetryAspect, TimeoutAspect,
    PersistenceAspectConfig, RetryAspectConfig, TimeoutAspectConfig,
)
from automojo.sysman.linux.ubuntu.storage import SQLiteStorage
from automojo.sysman.linux.ubuntu.filesystemmanagement import (
    UbuntuFilesystemManager, UbuntuFilesystemManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-ubuntu", username="admin"))
file_op = SSHFileOperator(host="remote-ubuntu", username="admin")

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2)))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0)))

fs_mgr = UbuntuFilesystemManager(
    runner=runner,
    file_operator=file_op,
    config=UbuntuFilesystemManagerConfig(elevated=True),
)

for m in fs_mgr.get_mounts():
    print(f"{m.device}\t{m.mountpoint}\t{m.fstype}")
```

### Dry-Run Mode With Diff

```python
config = UbuntuFilesystemManagerConfig(elevated=True, dry_run=True)
fs_mgr = UbuntuFilesystemManager(runner=runner, file_operator=file_op, config=config)

entry = UbuntuFstabEntry(
    device="/dev/sdb1", mountpoint="/backup", fstype="ext4",
    options="defaults,noatime", dump=0, passno=2,
)
result = fs_mgr.add_fstab_entry(entry=entry)
assert result.status == UbuntuFilesystemActionStatus.DRY_RUN
print(result.detail)  # unified diff of the would-be fstab change
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on argv prefix and a `MockFileOperator`
  that mirrors the real `IFileOperator` surface.
- `findmnt -J -l` JSON mock responses drive `mount` / `unmount` idempotency guards.
- `stat -c` pipe-separated mock responses drive `create_directory`,
  `create_symlink`, `remove_symlink`, `chown`, `chmod` idempotency and type-conflict
  tests.
- `/etc/fstab` read-modify-write semantics are tested against representative
  multi-line fstab files that include comments and multiple entries, asserting
  that unrelated content survives the rewrite.
- Dry-run tests prove every write short-circuits to `DRY_RUN` without invoking the
  runner or performing `write_text`, and that `add_fstab_entry` in dry-run populates
  a unified diff.
- Elevation tests prove writes pass `elevated=True` when configured and reads never
  do.
- Symlink conflict tests cover: non-symlink at `link` (both create and remove),
  symlink with matching target (create idempotent), symlink with different target
  (atomic replace with `ln -snf`).
- Parser tests cover `findmnt -J` (including missing `options` field),
  `stat -c` (files, directories, symlinks), and `df -B1` (header-plus-one-line
  output).
- No test requires root access or a live fstab.

---

## Scope Mapping

| Contract verb | Ubuntu implementation |
|---|---|
| `mount` | `mount [-t <fstype>] [-o <options>] <device> <mountpoint>` |
| `unmount` | `umount [-l] <mountpoint>` |
| `add_fstab_entry` | `IFileOperator` read-modify-write of `/etc/fstab` |
| `remove_fstab_entry` | `IFileOperator` read-modify-write of `/etc/fstab` |
| `get_mounts` | `findmnt -J -l` |
| `create_directory` | `mkdir -p` + `chmod` + `chown` |
| `remove_directory` | `rmdir` or `rm -rf` |
| `create_symlink` | `ln -s` or `ln -snf` |
| `remove_symlink` | `rm` (after `stat` type guard) |
| `chown` | `chown <user>:<group>` |
| `chmod` | `chmod <octal>` |
| `stat` | `stat -c` |
| `query_disk_usage` | `df -B1 --output=size,used,avail,pcent` |

---

## Extensions Beyond the Contract

None in this Ubuntu implementation.  `mkfs`, LVM / LUKS encryption, ACL / xattr /
AppArmor profile manipulation, and quotas are intentionally deferred; when added they
will live behind their own feature flags so a coordinator writing against the
contract does not have to special-case Ubuntu.
