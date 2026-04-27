# Design

## Overview

The bootloader-management feature is implemented as a single module,
`automojo.sysman.linux.ubuntu.bootloadermanagement`, that exposes a
`UbuntuBootloaderManager` class.  The maintenance coordinator
instantiates `UbuntuBootloaderManager` and calls its methods to
perform discrete OS-level bootloader-configuration actions:
enumerating boot entries, querying and setting the default entry,
adding and removing entries, and regenerating / applying the
bootloader on-disk configuration.

The coordinator drives all operations through a command runner
implementing the `ICommandRunner` interface.  All file I/O against
`/etc/default/grub`, `/etc/grub.d/40_custom`, `/boot/grub/grub.cfg`,
and `/boot/loader/entries/` flows through the same runner via `cat`,
`tee`, and `rm`.  The manager never calls `subprocess`, `os.path`, or
`open()` directly; it only constructs argument lists and stdin
payloads and passes them to the injected collaborator.  This keeps
business logic separate from execution and makes the feature fully
testable via mock collaborators without root access or a real
bootloader.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).
When using SSH, the coordinator can track reliability metrics and
apply execution aspects (persistence, retry, timeout) without any
changes to the `UbuntuBootloaderManager` code.

---

## Distinction from `boot-management` (UEFI)

This is **not** the UEFI firmware boot-entry feature.  The two
features operate on different layers and must remain independent:

```
+----------------------------------------------------------+
|                      Operator request                    |
+----------------------------------------------------------+
                            |
                +-----------+-----------+
                v                       v
+-------------------------+   +---------------------------+
|     boot-management     |   |  bootloader-management    |
|                         |   |                           |
|  Layer: FIRMWARE        |   |  Layer: OS-LEVEL          |
|  Tool : efibootmgr,     |   |  Tool : update-grub,      |
|         bcdedit /enum   |   |         grub-mkconfig,    |
|         firmware        |   |         bootctl           |
|  Store: NVRAM           |   |  Store: /boot/grub.cfg    |
|                         |   |         /boot/loader/.../|
+-------------------------+   +---------------------------+
                |                       |
                +-----------+-----------+
                            v
+----------------------------------------------------------+
|                       Boot sequence                      |
|                                                          |
|  1. Firmware reads NVRAM (boot-management territory)     |
|  2. Firmware launches the loader recorded in NVRAM       |
|  3. Loader reads its own config (THIS feature)           |
|  4. Loader launches the kernel listed in that config     |
+----------------------------------------------------------+
```

A misconfiguration in `boot-management` makes the firmware unable to
find a loader.  A misconfiguration in **this** feature makes the
loader unable to find (or unwilling to launch) the right kernel.
Both are catastrophic; neither overlaps the other.  This module never
calls `efibootmgr` and never touches `/sys/firmware/efi/efivars/`.

---

## Cross-OS Bootloader-Model Mapping

The cross-OS contract abstracts over a wide range of OS-level
bootloader implementations.  Ubuntu's two backends map onto the
contract verbs as follows:

| Cross-OS verb              | GRUB 2 native                                                | systemd-boot native                          | Idempotent? |
|----------------------------|--------------------------------------------------------------|----------------------------------------------|-------------|
| `list_entries`             | `cat /boot/grub/grub.cfg` + `menuentry` parser               | `bootctl list`                               | n/a (read)  |
| `get_default_entry`        | `GRUB_DEFAULT` from `/etc/default/grub` + grub.cfg cross-ref | `bootctl status` (Default Boot Loader Entry) | n/a (read)  |
| `set_default_entry`        | rewrite `GRUB_DEFAULT=<id>` then `update-grub`               | `bootctl set-default <id>`                   | YES         |
| `add_entry`                | append sentinel-block to `/etc/grub.d/40_custom` + `update-grub` | write `<id>.conf` to `/boot/loader/entries/` | YES     |
| `remove_entry`             | strip sentinel-block from `/etc/grub.d/40_custom` + `update-grub` | `rm /boot/loader/entries/<id>.conf`     | YES         |
| `update_bootloader_config` | `update-grub`                                                | `bootctl update`                             | YES         |
| `regenerate_config`        | `grub-mkconfig -o /boot/grub/grub.cfg`                       | `bootctl install`                            | YES         |

Comparison with sister implementations:

| OS      | OS-level loader        | Default-entry store                           | Apply step                  | Reboot required for change? |
|---------|------------------------|-----------------------------------------------|-----------------------------|------------------------------|
| RHEL    | GRUB 2                 | `grub2-editenv`                               | `grub2-mkconfig`            | YES                          |
| Ubuntu  | GRUB 2 / systemd-boot  | `GRUB_DEFAULT` / `bootctl set-default`        | `update-grub` / `bootctl update` | YES                     |
| macOS   | bless / iBoot          | NVRAM `efi-boot-device`                       | (firmware-managed)          | YES                          |
| Windows | BCD                    | `bcdedit /default {<guid>}`                   | (immediate, no apply step)  | YES                          |

---

## Architecture

```
+-------------------------------------------------------------+
|              Maintenance Coordinator                        |
|  - Instantiates LocalCommandRunner / SSHCommandRunner       |
|  - Configures aspects (optional)                            |
|  - Injects runner into UbuntuBootloaderManager              |
+----------------------+--------------------------------------+
                       | instantiates & injects
                       v
+-------------------------------------------------------------+
|                UbuntuBootloaderManager                      |
|  automojo.sysman.linux.ubuntu.bootloadermanagement          |
|                                                             |
|  list_entries()           get_default_entry()               |
|  set_default_entry(id)    add_entry(spec)                   |
|  remove_entry(id)         update_bootloader_config()        |
|  regenerate_config()                                        |
|                                                             |
|  Backend dispatch:                                          |
|    UbuntuBootloaderBackend.GRUB2          (default)         |
|    UbuntuBootloaderBackend.SYSTEMD_BOOT                     |
+-------------------+-----------------------------------------+
                    | delegates commands to
                    v
+-------------------------------------------------------------+
|                     ICommandRunner                          |
|     run(args, elevated, stdin, capture_output)              |
|     -> RunResult                                            |
+-------------------+-----------------------------------------+
                    |
       +------------+------------+
       v                         v
+----------------+      +-------------------+
| LocalCommand   |      | SSHCommand        |
| Runner         |      | Runner            |
+----------------+      +-------------------+
                |
                v
+-------------------------------------------------------------+
|                    Execution Aspects                        |
|  - PersistenceAspect: audit logging                         |
|  - RetryAspect: transient failure retry                     |
|  - TimeoutAspect: global timeout policy                     |
+-------------------------------------------------------------+
```

---

## Components

### `UbuntuBootloaderManager`

**Location:** `source/packages/automojo/sysman/linux/ubuntu/bootloadermanagement.py`

The primary public class.  Constructed with an `ICommandRunner` and
an `UbuntuBootloaderManagerConfig`.

| Method                                              | Description                                                              |
|-----------------------------------------------------|--------------------------------------------------------------------------|
| `list_entries()` -> `list[UbuntuBootEntryInfo]`     | Enumerate boot entries known to the active backend                       |
| `get_default_entry()` -> `UbuntuBootEntryInfo`      | Return the entry currently marked as the default                         |
| `set_default_entry(entry_id)` -> `Result`           | Persist `entry_id` as the active default (idempotent)                    |
| `add_entry(spec)` -> `Result`                       | Register a new boot entry from the supplied spec (idempotent)            |
| `remove_entry(entry_id)` -> `Result`                | Remove the bootloader entry identified by `entry_id` (idempotent)        |
| `update_bootloader_config()` -> `Result`            | Regenerate AND apply the bootloader configuration                        |
| `regenerate_config()` -> `Result`                   | Rebuild the on-disk bootloader configuration without applying it         |

### `UbuntuBootloaderManagerConfig`

A dataclass holding runtime configuration for
`UbuntuBootloaderManager`.

| Field                | Type                          | Default                       | Description                                                       |
|----------------------|-------------------------------|-------------------------------|-------------------------------------------------------------------|
| `dry_run`            | `bool`                        | `False`                       | When `True`, no mutating commands are executed                    |
| `elevated`           | `bool`                        | `False`                       | When `True`, mutating commands are dispatched with elevation      |
| `backend`            | `UbuntuBootloaderBackend`     | `GRUB2`                       | The OS-level bootloader backend to drive                          |
| `grub_defaults`      | `str`                         | `/etc/default/grub`           | Path to the GRUB defaults file                                    |
| `grub_custom`        | `str`                         | `/etc/grub.d/40_custom`       | Path to the GRUB drop-in for `add_entry` / `remove_entry`         |
| `grub_cfg`           | `str`                         | `/boot/grub/grub.cfg`         | Path to the rendered GRUB configuration                           |
| `loader_entries_dir` | `str`                         | `/boot/loader/entries`        | Directory holding systemd-boot loader entries                     |

---

## Data Models

### `UbuntuBootloaderActionStatus`

```python
UbuntuBootloaderActionStatus = ActionStatus  # alias

class ActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"  # already in desired state, no action needed
    DRY_RUN  = "dry_run"  # would have acted, but dry_run=True
```

### `UbuntuBootloaderActionResult`

Returned by every mutating verb.

```python
@dataclass
class UbuntuBootloaderActionResult(FeatureActionResult):
    status: ActionStatus
    detail: Optional[str]
    exit_code: Optional[int]
    stdout: Optional[str]
    stderr: Optional[str]
    command: Optional[list[str]]
    entry_id: Optional[str] = None
    requires_reboot: bool = False
    message: Optional[str] = None
```

### `UbuntuBootloaderBackend`

```python
class UbuntuBootloaderBackend(Enum):
    GRUB2         = "grub2"
    SYSTEMD_BOOT  = "systemd_boot"
```

### `UbuntuBootEntryInfo`

```python
@dataclass
class UbuntuBootEntryInfo:
    id: str
    name: str
    kernel: Optional[str] = None
    initrd: Optional[str] = None
    cmdline: Optional[str] = None
    device: Optional[str] = None
    is_default: bool = False
    boot_count: Optional[int] = None
```

### `UbuntuBootEntrySpec`

```python
@dataclass
class UbuntuBootEntrySpec:
    id: str
    name: str
    kernel: str
    initrd: Optional[str] = None
    cmdline: Optional[str] = None
    device: Optional[str] = None
```

### Exceptions

| Exception                     | Raised When                                                  |
|-------------------------------|--------------------------------------------------------------|
| `UbuntuBootloaderQueryError`  | A read fails unexpectedly, or no default can be resolved.    |

---

## Correctness Properties

### Property 1: Idempotent `set_default_entry`

For any entry id, calling `set_default_entry()` when the supplied
entry id is already the active default should return a result with
status `SKIPPED` and must not invoke `update-grub` or
`bootctl set-default`.

**Validates: Requirements 3.2, 3.3**

### Property 2: Idempotent `add_entry`

For any entry spec, calling `add_entry()` when an entry with the
same id and matching rendered body is already present should return
a result with status `SKIPPED` and must not write the file or invoke
`update-grub`.

**Validates: Requirements 4.4**

### Property 3: Idempotent `remove_entry`

For any entry id, calling `remove_entry()` when no matching entry is
present should return a result with status `SKIPPED` and must not
write the file or invoke `update-grub` / `rm`.

**Validates: Requirements 5.2, 5.4**

### Property 4: Backend Dispatch Correctness

For any verb invocation, the runner argv must match the backend
selector: `update-grub` / `grub-mkconfig` for GRUB2; `bootctl ...`
for SYSTEMD_BOOT.  No GRUB tool may appear in a SYSTEMD_BOOT
dispatch and vice versa.

**Validates: Requirements 1.1, 1.3, 6.1, 6.2, 7.1, 7.2**

### Property 5: Submenu-Qualified Identifier Round-Trip

For any GRUB `menuentry` directive nested inside one or more
`submenu` blocks, the `id` produced by `list_entries` must round-trip
through `set_default_entry` (i.e. writing the same id back to
`GRUB_DEFAULT=` and re-reading must return the same entry).

**Validates: Requirements 1.2, 2.2**

### Property 6: Dry-Run Safety

For any mutating operation with `dry_run=True`, the command runner
must never be invoked with a mutating command, the file payload must
never be `tee`'d, and the returned result's `status` must be
`DRY_RUN`.

**Validates: Requirements 10.1, 10.2, 10.3**

### Property 7: Elevation Consistency

For any mutating operation with `elevated=True` in the config, the
command runner must be invoked with `elevated=True` for both the
file-write step and the `update-grub` / `bootctl` step; for any
read-only query, elevation must never be requested.

**Validates: Requirements 9.1, 9.2, 9.3, 9.4**

### Property 8: Status Mapping Correctness

For any operation result from the command runner, the
`UbuntuBootloaderActionResult.status` must correctly reflect the
runner's exit code: `0` maps to `SUCCESS`, non-zero maps to `FAILED`
(unless the operation was skipped or in dry-run mode), with the
exit code and captured stderr preserved on `FAILED` results.

**Validates: Requirements 3.4, 12.2**

### Property 9: No UEFI NVRAM Touch

For any verb invocation, no command argv may equal or start with
`efibootmgr`, and no path argument may resolve under
`/sys/firmware/efi/efivars/`.

**Validates: Requirements 11.1, 11.2**

### Property 10: Reboot Signalling

For any successful write, `result.requires_reboot` must be `True`
because the OS-level bootloader configuration is consulted only at
boot.

**Validates: Requirements 12.1**

### Property 11: Sentinel Bracketing for `add_entry` / `remove_entry`

For any GRUB2 `add_entry` write, the appended block must be
bracketed by `# automojo-bootloader-management:begin <id>` and
`# automojo-bootloader-management:end <id>` lines.  For any
`remove_entry` write, the excised block must be exactly the bracketed
range; lines outside the sentinels must remain untouched.

**Validates: Requirements 4.2, 5.1**

### Property 12: Quoting Preservation in `/etc/default/grub`

For any rewrite of `GRUB_DEFAULT`, the existing quoting style
(double-quoted, single-quoted, or unquoted) must be preserved when
the directive already exists.  When the directive is absent, the
appended line must use double-quoted form.

**Validates: Requirements 3.1**

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

`UbuntuBootloaderManager` depends on this interface, injected at
construction time.

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        elevated: bool = False,
        stdin: Optional[str] = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `RunResult` (from `automojo-interfaces`)

```python
@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
```

### Elevation Strategy

When `UbuntuBootloaderManagerConfig.elevated` is `True`,
`UbuntuBootloaderManager` passes `elevated=True` to
`ICommandRunner.run()` for every mutating command.  The runner is
responsible for prepending `sudo` or using whatever privilege-
escalation mechanism is appropriate for the environment.  The
manager has no knowledge of how elevation is implemented.  Read-only
queries never request elevation.

---

## Command Mappings

### GRUB 2 Backend

| Operation                                | Command / File Action                                                  |
|------------------------------------------|------------------------------------------------------------------------|
| `list_entries`                           | `cat /boot/grub/grub.cfg`                                              |
| `get_default_entry`                      | `cat /etc/default/grub` then resolve `GRUB_DEFAULT` against `grub.cfg` |
| `set_default_entry` (write)              | `tee /etc/default/grub` (rewrite `GRUB_DEFAULT="<id>"`)                |
| `set_default_entry` (apply)              | `update-grub`                                                          |
| `add_entry` (write)                      | `tee /etc/grub.d/40_custom` (append sentinel block)                    |
| `add_entry` (apply)                      | `update-grub`                                                          |
| `remove_entry` (write)                   | `tee /etc/grub.d/40_custom` (strip sentinel block)                     |
| `remove_entry` (apply)                   | `update-grub`                                                          |
| `update_bootloader_config`               | `update-grub`                                                          |
| `regenerate_config`                      | `grub-mkconfig -o /boot/grub/grub.cfg`                                 |
| Idempotency probes                       | `cat /etc/default/grub`, `cat /etc/grub.d/40_custom`                   |

### systemd-boot Backend

| Operation                  | Command                                              |
|----------------------------|------------------------------------------------------|
| `list_entries`             | `bootctl list`                                       |
| `get_default_entry`        | `bootctl status` (parse Default Boot Loader Entry)   |
| `set_default_entry`        | `bootctl set-default <id>`                           |
| `add_entry`                | `tee /boot/loader/entries/<id>.conf`                 |
| `remove_entry`             | `rm -f /boot/loader/entries/<id>.conf`               |
| `update_bootloader_config` | `bootctl update`                                     |
| `regenerate_config`        | `bootctl install`                                    |

---

## Error Handling

### Typed Exceptions

| Exception                     | Raised When                                                                                                    |
|-------------------------------|----------------------------------------------------------------------------------------------------------------|
| `UbuntuBootloaderQueryError`  | `cat /boot/grub/grub.cfg`, `bootctl list`, `bootctl status`, or default-entry resolution fails unexpectedly.   |

All other failures (write step failed, `update-grub` failed,
`bootctl set-default` failed, etc.) are surfaced as a
`UbuntuBootloaderActionResult` with `status=FAILED` carrying the
underlying `exit_code`, `stdout`, and `stderr`.  The coordinator
decides how to respond without catching exceptions for normal
operational failures.

### Idempotency Guards

Before executing mutating commands and file writes,
`UbuntuBootloaderManager` reads current state (via `cat` and
`bootctl status`) to determine whether action is needed.  When the
system is already in the desired state the method returns `SKIPPED`
without invoking the apply step.  This makes all mutating operations
safe to call multiple times.

### Destructive-Change Safety

Bootloader misconfiguration can render a host unbootable.  This
module:

1. Sets `requires_reboot=True` on every successful write so the
   coordinator can surface the change to the operator.
2. Surfaces the underlying tool's stderr verbatim on `FAILED`
   results.
3. Documents in the module docstring that callers SHOULD pair every
   write with a backup of `/boot/grub/grub.cfg` (GRUB2) or
   `/boot/loader/entries/` (systemd-boot) and SHOULD verify the
   resulting menu through `list_entries` before rebooting.

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/
+-- __init__.py
+-- bootloadermanagement.py          (this feature)

source/testroots/testbase/automojo/tests/sysman/linux/ubuntu/
+-- test_bootloadermanagement.py
```

---

## Testing Strategy

### Unit Testing

- All tests use a `_MockCommandRunner` injected into
  `UbuntuBootloaderManager` at construction time.
- Tests verify the *arguments* passed to the runner and the *stdin*
  payloads, not real system state.
- Each public method has at minimum: a success path test, a skip /
  idempotency test where applicable, a failure path test, and a
  dry-run test.
- Backend-dispatch tests assert the GRUB2 vs. SYSTEMD_BOOT branching
  selects the correct argv.
- No test requires root access, a live bootloader, or a real
  `/boot/grub/grub.cfg`.

### Coverage Targets

Both backends are exercised: at least one test per verb per backend.
Submenu-qualified id round-trip is exercised by the `list_entries`
GRUB parser test.  Sentinel bracketing is exercised by the
`add_entry` and `remove_entry` GRUB tests.

---

## Usage Examples

### Local Execution (Set Default Boot Entry on a GRUB2 host)

```python
from automojo.sysman.linux.ubuntu.runners import (
    LocalCommandRunner, LocalRunnerConfig,
)
from automojo.sysman.linux.ubuntu.bootloadermanagement import (
    UbuntuBootloaderBackend,
    UbuntuBootloaderManager,
    UbuntuBootloaderManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = UbuntuBootloaderManagerConfig(
    elevated=True,
    backend=UbuntuBootloaderBackend.GRUB2,
)
manager = UbuntuBootloaderManager(runner=runner, config=config)

result = manager.set_default_entry(entry_id="Ubuntu")
print("set default: {}".format(result.status))
# requires_reboot is True on success
```

### SSH Execution (Add a Rescue Entry)

```python
from automojo.sysman.linux.ubuntu.runners import (
    SSHCommandRunner, SSHRunnerConfig,
)
from automojo.sysman.linux.ubuntu.bootloadermanagement import (
    UbuntuBootEntrySpec,
    UbuntuBootloaderManager,
    UbuntuBootloaderManagerConfig,
)

runner = SSHCommandRunner(
    SSHRunnerConfig(host="192.168.1.100", username="admin"),
)
manager = UbuntuBootloaderManager(
    runner=runner,
    config=UbuntuBootloaderManagerConfig(elevated=True),
)
spec = UbuntuBootEntrySpec(
    id="rescue-kernel",
    name="Rescue Kernel",
    kernel="/boot/vmlinuz-rescue",
    initrd="/boot/initrd.img-rescue",
    cmdline="ro single",
)
result = manager.add_entry(spec=spec)
print("add: {} (reboot: {})".format(
    result.status, result.requires_reboot,
))
```

### systemd-boot Backend

```python
from automojo.sysman.linux.ubuntu.bootloadermanagement import (
    UbuntuBootloaderBackend,
    UbuntuBootloaderManager,
    UbuntuBootloaderManagerConfig,
)

manager = UbuntuBootloaderManager(
    runner=runner,
    config=UbuntuBootloaderManagerConfig(
        elevated=True,
        backend=UbuntuBootloaderBackend.SYSTEMD_BOOT,
    ),
)
entries = manager.list_entries()
default = manager.get_default_entry()
print("default loader-conf id: {}".format(default.id))
```
