# Design

## Overview

This spec implements the hardware-inventory contract defined at
`automojo-sysman-common/specs/contracts/hardware-inventory/contract.md` for
Ubuntu using `dmidecode`, `lscpu`, `lsblk`, `ip`, `lspci`, and `lsusb`.

The hardware-inventory feature is implemented as a single module,
`automojo.sysman.linux.ubuntu.hardwareinventory`, that exposes an
`UbuntuHardwareInventoryManager` class.  The coordinator instantiates
`UbuntuHardwareInventoryManager` and calls its read verbs to query discrete
hardware aspects.  Every verb returns a typed `Ubuntu*` dataclass, or raises
`UbuntuHardwareInventoryQueryError` when the underlying command fails or the
captured output cannot be parsed.

This feature is strictly **read-only**.  Per the cross-OS contract:

- `UbuntuHardwareInventoryActionStatus` enumerates `SUCCESS`, `FAILED`, and
  `SKIPPED` only; there is no `DRY_RUN` member.
- `UbuntuHardwareInventoryManagerConfig` inherits `elevated` and `dry_run`
  from `FeatureConfig`, but neither field gates any verb in the current
  pilot.  Every runner invocation passes `elevated=False`.
- No verb mutates host state, so dry-run would have no observable effect.
  Documenting that the field is ignored is the chosen pattern; the alternative
  of subclassing `FeatureConfig.__post_init__` to forcibly null out `dry_run`
  was rejected because it would mask coordinator misuse rather than surface it.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).  When using
SSH, the coordinator can track reliability metrics and apply execution aspects
(persistence, retry, timeout) without any changes to
`UbuntuHardwareInventoryManager` code.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into UbuntuHardwareInventoryManager       │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│        UbuntuHardwareInventoryManager                       │
│  automojo.sysman.linux.ubuntu.hardwareinventory             │
│                                                             │
│  get_system_info()       get_cpu_info()                     │
│  get_memory_info()       list_storage_devices()             │
│  list_network_devices()  list_pci_devices()                 │
│  list_usb_devices()      get_firmware_info()                │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated=False, ...) → RunResult                 │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH connection     │
│ - local execution    │      │ - reliability metrics│
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
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

### `UbuntuHardwareInventoryManager`

**Location:** `source/packages/automojo/sysman/linux/ubuntu/hardwareinventory.py`

The primary public class.  Constructed with an `ICommandRunner` and an
`UbuntuHardwareInventoryManagerConfig`.

| Method | Returns | Description |
|---|---|---|
| `get_system_info()` | `UbuntuSystemInfo` | Run `dmidecode -t system` and parse System Information / BIOS Information handle blocks |
| `get_cpu_info()` | `UbuntuCpuInfo` | Run `lscpu -J`, JSON-decode the `lscpu` array, populate topology + flags |
| `get_memory_info()` | `UbuntuMemoryInfo` | Run `dmidecode -t memory` for per-DIMM detail and `cat /proc/meminfo` for the live aggregate |
| `list_storage_devices()` | `list[UbuntuStorageDeviceInfo]` | Run `lsblk -J -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL`, flatten parent / children |
| `list_network_devices()` | `list[UbuntuNetworkDeviceInfo]` | Run `ip -j link show`, JSON-decode, classify virtual vs. physical |
| `list_pci_devices()` | `list[UbuntuPciDeviceInfo]` | Run `lspci -mm -nn -k`, parse primary + driver continuation lines |
| `list_usb_devices()` | `list[UbuntuUsbDeviceInfo]` | Run `lsusb`, parse one record per line |
| `get_firmware_info()` | `UbuntuFirmwareInfo` | Run `dmidecode -t bios`, parse BIOS Information handle block |

### `UbuntuHardwareInventoryManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | Inherited from `FeatureConfig`; ignored — feature is read-only |
| `elevated` | `bool` | `False` | Inherited from `FeatureConfig`; reserved for selective elevation, not consulted by any verb today |

---

## Data Models

All dataclasses live alongside the manager in
`automojo.sysman.linux.ubuntu.hardwareinventory`.

```python
class UbuntuHardwareInventoryActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    # DRY_RUN intentionally absent — feature is read-only.


class UbuntuHardwareInventoryQueryError(Exception):
    command: list[str]
    exit_code: int | None
    stderr: str


@dataclass
class UbuntuSystemInfo:
    manufacturer: str | None
    product_name: str | None
    serial_number: str | None
    uuid: str | None
    bios_version: str | None
    firmware_version: str | None
    family: str | None
    version: str | None


@dataclass
class UbuntuCpuInfo:
    model: str | None
    vendor: str | None
    architecture: str | None
    physical_cores: int
    logical_cores: int
    frequency_mhz: int | None
    cache_l1: int | None
    cache_l2: int | None
    cache_l3: int | None
    flags: list[str]


@dataclass
class UbuntuMemoryModule:
    slot: str | None
    size_bytes: int
    type: str | None
    speed_mhz: int | None
    manufacturer: str | None
    serial_number: str | None


@dataclass
class UbuntuMemoryInfo:
    total_bytes: int
    available_bytes: int | None
    slots_used: int | None
    slots_total: int | None
    modules: list[UbuntuMemoryModule]


@dataclass
class UbuntuStorageDeviceInfo:
    name: str
    size_bytes: int
    type: str
    model: str | None
    serial: str | None
    mountpoint: str | None
    filesystem: str | None


@dataclass
class UbuntuNetworkDeviceInfo:
    name: str
    mac_address: str | None
    link_state: str | None
    speed: str | None
    mtu: int | None
    is_virtual: bool


@dataclass
class UbuntuPciDeviceInfo:
    slot: str
    class_name: str | None
    vendor: str | None
    device: str | None
    vendor_id: str | None
    device_id: str | None
    driver: str | None


@dataclass
class UbuntuUsbDeviceInfo:
    bus: str
    device_id: str | None
    vendor_id: str | None
    product_id: str | None
    name: str | None
    serial: str | None


@dataclass
class UbuntuFirmwareInfo:
    vendor: str | None
    version: str | None
    release_date: str | None
    secure_boot: bool | None
    uefi: bool | None
```

---

## Interfaces

### `ICommandRunner` (from `automojo-interfaces`)

```python
class ICommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult: ...
```

### `RunResult` (from `automojo-interfaces`)

```python
@dataclass
class RunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
```

The manager's `_read` helper raises
`UbuntuHardwareInventoryQueryError` on any non-zero exit code so that every
verb's caller sees a single, typed error path.

---

## Command Mappings

| Verb | Argv |
|---|---|
| `get_system_info` | `["dmidecode", "-t", "system"]` |
| `get_cpu_info` | `["lscpu", "-J"]` |
| `get_memory_info` | `["dmidecode", "-t", "memory"]`, then `["cat", "/proc/meminfo"]` |
| `list_storage_devices` | `["lsblk", "-J", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL"]` |
| `list_network_devices` | `["ip", "-j", "link", "show"]` |
| `list_pci_devices` | `["lspci", "-mm", "-nn", "-k"]` |
| `list_usb_devices` | `["lsusb"]` |
| `get_firmware_info` | `["dmidecode", "-t", "bios"]` |

Every invocation passes `elevated=False`.

---

## Output Parsing per Verb

### `get_system_info` — `dmidecode -t system`

Real example (excerpt):

```
# dmidecode 3.3
Handle 0x0001, DMI type 1, 27 bytes
System Information
	Manufacturer: ACME Corp
	Product Name: Widget 9000
	Version: r1.2
	Serial Number: SN-AB-12
	UUID: 11111111-2222-3333-4444-555555555555
	Family: Widget
```

Parse rules:

- A line starting with `Handle ` opens a new block; track the current section
  by the next non-tab line (e.g., `System Information`).
- Tab-indented `<key>: <value>` lines are dispatched by `(section, key)`.
- The parser populates `manufacturer`, `product_name`, `serial_number`,
  `uuid`, `family`, and `version` from the System Information section, and
  `bios_version` / `firmware_version` from the BIOS Information section when
  that block is present in the same `-t system` output.

### `get_cpu_info` — `lscpu -J`

Real example (excerpt):

```json
{"lscpu": [
  {"field": "Architecture:", "data": "x86_64"},
  {"field": "CPU(s):", "data": "8"},
  {"field": "Vendor ID:", "data": "GenuineIntel"},
  {"field": "Model name:", "data": "Intel(R) Xeon(R) E5"},
  {"field": "Core(s) per socket:", "data": "4"},
  {"field": "Socket(s):", "data": "1"},
  {"field": "L1d cache:", "data": "32K"},
  {"field": "L2 cache:", "data": "256K"},
  {"field": "L3 cache:", "data": "8M"},
  {"field": "Flags:", "data": "fpu vme sse sse2 avx"}
]}
```

Parse rules:

- `lscpu -J` returns a top-level object with a `lscpu` array of
  `{"field": ..., "data": ...}` entries.  The trailing `:` on the field name
  is stripped before matching.
- `physical_cores` is computed as `Socket(s)` × `Core(s) per socket` when
  both are present and non-zero; otherwise it falls back to `logical_cores`.
- Cache size strings tolerate ``32K`` / ``32 KiB`` / ``6 MiB`` /
  ``6M (1 instance)`` shapes.  The parser splits the head digits, normalises
  the suffix, and converts to bytes.
- `frequency_mhz` prefers `CPU MHz` and falls back to `CPU max MHz` when the
  former is absent.

### `get_memory_info` — `dmidecode -t memory` + `cat /proc/meminfo`

Real example (excerpt of `dmidecode -t memory`):

```
Handle 0x0008, DMI type 16, 23 bytes
Physical Memory Array
	Number Of Devices: 4

Handle 0x0009, DMI type 17, 40 bytes
Memory Device
	Locator: DIMM_A1
	Size: 8192 MB
	Type: DDR4
	Speed: 2400 MT/s
	Manufacturer: ACME
	Serial Number: 12345

Handle 0x000A, DMI type 17, 40 bytes
Memory Device
	Locator: DIMM_A2
	Size: No Module Installed
```

`/proc/meminfo` (excerpt):

```
MemTotal:       16384000 kB
MemAvailable:   12288000 kB
```

Parse rules:

- `Number Of Devices` populates `slots_total`.
- One `UbuntuMemoryModule` per `Memory Device` block whose `Size` is not
  `No Module Installed`.
- `Size: <n> MB|GB|kB|TB` translates to bytes; `Speed: <n> MT/s` and
  `Speed: <n> MHz` translate to integer MHz; `Unknown` -> `None`.
- `slots_used` is set to the count of populated modules.
- `MemTotal:` (kB) -> bytes -> `total_bytes`; `MemAvailable:` (kB) -> bytes
  -> `available_bytes`.

### `list_storage_devices` — `lsblk -J`

Real example (excerpt):

```json
{"blockdevices": [
  {"name": "sda", "size": 500107862016, "type": "disk",
   "model": "Samsung SSD 870", "serial": "S1AB", "mountpoint": null,
   "fstype": null,
   "children": [
     {"name": "sda1", "size": 500106788864, "type": "part",
      "fstype": "ext4", "mountpoint": "/", "model": null, "serial": null}
   ]}
]}
```

Parse rules:

- Walk parent / `children` depth-first.  Each node emits one
  `UbuntuStorageDeviceInfo`.
- `size` is integer bytes when the kernel-supplied form is used; the parser
  also accepts string forms like ``"500G"`` and decodes the digit prefix +
  unit suffix to bytes.
- `mountpoint` is `None` for unmounted parents; `filesystem` mirrors `fstype`.

### `list_network_devices` — `ip -j link show`

Real example (excerpt):

```json
[
  {"ifname": "lo", "address": "00:00:00:00:00:00", "operstate": "UNKNOWN",
   "mtu": 65536, "link_type": "loopback"},
  {"ifname": "enp0s3", "address": "08:00:27:11:22:33", "operstate": "UP",
   "mtu": 1500, "link_type": "ether"},
  {"ifname": "docker0", "address": "02:42:b1:c2:d3:e4",
   "operstate": "DOWN", "mtu": 1500, "link_type": "ether"}
]
```

Parse rules:

- One record per top-level entry.  `ifname`, `address`, `operstate`, and
  `mtu` map directly.
- `is_virtual=True` when `link_type` is `loopback` or `ifname` begins with
  `lo`, `docker`, `br-`, `veth`, `tun`, `tap`, `vir`, `dummy`, `vlan`,
  `bond`, or `wg`.
- `speed` is intentionally `None`; `network-configuration` owns link-speed
  reads.

### `list_pci_devices` — `lspci -mm -nn -k`

Real example (excerpt):

```
00:00.0 "Host bridge [0600]" "Intel Corporation [8086]" "5520 I/O Hub [3406]" -r13 -p00 "Dell [1028]" "Device [02a3]"
	Driver: pcieport

00:1f.3 "SMBus [0c05]" "Intel Corporation [8086]" "PCH SMBus [a323]" -r10
	Driver: i801_smbus
	Module: i2c_i801
```

Parse rules:

- Primary line: split on whitespace honoring double-quoted runs.  Position 0
  is the slot.  Positions 1, 2, 3 are class, vendor, device — each has a
  trailing `[id]` or `[vid:did]` tag stripped off into the matching `*_id`
  field.
- Continuation lines start with a tab.  `\tDriver:` lines populate `driver`
  on the most recently emitted device.  `\tModule:` lines are accepted and
  ignored in this pilot.

### `list_usb_devices` — `lsusb`

Real example (excerpt):

```
Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
Bus 001 Device 002: ID 0951:1666 Kingston Technology DataTraveler
```

Parse rules:

- Whitespace split with a max of 7 fields preserves a multi-word
  `<vendor> <product>` description on the tail.  `bus = parts[1]`,
  `device_id = parts[3]` (trailing `:` stripped), `vendor_id:product_id`
  is the pair after `ID`, and the rest is `name`.
- `serial` requires `lsusb -v` and is left as `None` for this pilot.

### `get_firmware_info` — `dmidecode -t bios`

Real example (excerpt):

```
Handle 0x0000, DMI type 0, 24 bytes
BIOS Information
	Vendor: American Megatrends Inc.
	Version: 1.7.2
	Release Date: 06/01/2023
	Characteristics:
		ISA is supported
		UEFI is supported
```

Parse rules:

- `Vendor`, `Version`, and `Release Date` map directly.
- A `UEFI is supported` characteristic line sets `uefi=True`; absence sets
  `uefi=False`.
- `secure_boot` is intentionally `None`.  See tasks.md for the sysfs
  `SecureBoot` follow-up.

---

## Error Handling

### Typed Exception

`UbuntuHardwareInventoryQueryError` carries `command`, `exit_code`, and
`stderr`.  Its message is built from a list of lines via
`os.linesep.join()` so that command arguments, exit code, and stderr are
each on their own line for log-friendly readability.

### Error Sources

| Source | Mapped To |
|---|---|
| Non-zero exit code from runner | `UbuntuHardwareInventoryQueryError` with raw stderr |
| `json.JSONDecodeError` parsing `lscpu`, `lsblk`, or `ip` output | `UbuntuHardwareInventoryQueryError` with `message=` describing the parse failure |
| Missing field on a JSON object | Field stays at its dataclass default (e.g., `None`) — does not raise |
| Empty stdout from `dmidecode -t memory` | Modules list stays empty; `slots_total` / `slots_used` stay `None` |

### Partial Tolerance

`get_memory_info()` is the one verb that runs two commands.  When
`dmidecode -t memory` fails the modules list is left empty and `slots_*`
remain `None`, but the `/proc/meminfo` pass still runs so `total_bytes` is
populated where the kernel can supply it.  All other verbs raise on the
first failure.

---

## Correctness Properties

### Property 1: Read-Only Discipline

For every verb invocation, every runner call passes `elevated=False`,
regardless of `config.elevated`.  No verb invokes a mutating command.

### Property 2: JSON Parse Robustness

For every verb that consumes JSON (`lscpu -J`, `lsblk -J`, `ip -j`),
malformed JSON surfaces as `UbuntuHardwareInventoryQueryError` with a
parse-failure message rather than a raw `json.JSONDecodeError`.

### Property 3: Empty-Output Safety

For `dmidecode -t memory`, `lspci`, `lsusb`, and `ip -j link show`, an
empty-but-zero-exit-code response yields an empty list (or an
`UbuntuMemoryInfo` with no modules), not an exception.

### Property 4: Storage Tree Flattening

For every `lsblk` parent / children tree, `list_storage_devices()` emits the
parent before its descendants and yields exactly one record per node.

### Property 5: PCI Driver Association

For every `lspci -mm -nn -k` record, a `\tDriver:` continuation line
populates `driver` on the most recently opened primary record.  Records
with no continuation line surface `driver=None`.

### Property 6: Memory Slot Counting

For every `dmidecode -t memory` output, `slots_total` reflects the
`Number Of Devices` line on the `Physical Memory Array`, and `slots_used`
is the count of `Memory Device` blocks whose size resolved to a non-zero
byte count.

### Property 7: Single Read per Verb

For every public verb except `get_memory_info`, exactly one runner call
issues per invocation.  `get_memory_info` issues exactly two: the
`dmidecode -t memory` read and the `cat /proc/meminfo` read.

### Property 8: No Hidden Dry-Run

For every verb, a `dry_run=True` config does not change the runner-call
sequence.  The feature ignores the `dry_run` flag entirely.

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/
└── hardwareinventory.py                ← manager + dataclasses + parsers + exception

source/testroots/testbase/automojo/tests/sysman/linux/ubuntu/
└── test_hardwareinventory.py           ← unit tests using a MockCommandRunner
```

---

## Usage Examples

### Local execution

```python
from automojo.sysman.linux.ubuntu.runners import (
    LocalCommandRunner, LocalRunnerConfig,
)
from automojo.sysman.linux.ubuntu.hardwareinventory import (
    UbuntuHardwareInventoryManager,
    UbuntuHardwareInventoryManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = UbuntuHardwareInventoryManagerConfig()
inventory = UbuntuHardwareInventoryManager(runner, config)

system = inventory.get_system_info()
cpu = inventory.get_cpu_info()
memory = inventory.get_memory_info()
storage = inventory.list_storage_devices()
network = inventory.list_network_devices()
pci = inventory.list_pci_devices()
usb = inventory.list_usb_devices()
firmware = inventory.get_firmware_info()
```

### SSH execution

```python
from automojo.sysman.linux.ubuntu.runners import (
    SSHCommandRunner, SSHRunnerConfig,
)
from automojo.sysman.linux.ubuntu.hardwareinventory import (
    UbuntuHardwareInventoryManager,
    UbuntuHardwareInventoryManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(
    host="192.168.1.100", username="admin",
    key_filename="/home/user/.ssh/id_rsa",
))
config = UbuntuHardwareInventoryManagerConfig()
inventory = UbuntuHardwareInventoryManager(runner, config)
storage = inventory.list_storage_devices()
```

### Aspect composition

```python
from automojo.sysman.linux.ubuntu.aspects import (
    PersistenceAspect, RetryAspect, TimeoutAspect,
    PersistenceAspectConfig, RetryAspectConfig, TimeoutAspectConfig,
)
from automojo.sysman.linux.ubuntu.storage import SQLiteStorage

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-server", username="admin"))
runner.add_aspect(PersistenceAspect(PersistenceAspectConfig(
    storage_backend=SQLiteStorage("/var/log/maintenance/inventory.db"),
)))
runner.add_aspect(RetryAspect(RetryAspectConfig(
    max_retries=3, retryable_exit_codes=[255],
)))
runner.add_aspect(TimeoutAspect(TimeoutAspectConfig(default_timeout=120.0)))

inventory = UbuntuHardwareInventoryManager(
    runner, UbuntuHardwareInventoryManagerConfig(),
)
network = inventory.list_network_devices()
```

---

## Testing Strategy

### Unit Testing

- All tests use a `_MockCommandRunner` that records every `run` call and
  returns pre-seeded `RunResult` values keyed by argv tuple.
- Mocks are seeded with canned stdout from real `dmidecode -t system`,
  `dmidecode -t memory`, `dmidecode -t bios`, `lscpu -J`, `lsblk -J`,
  `ip -j link show`, `lspci -mm -nn -k`, and `lsusb` captures so the parsers
  exercise realistic shapes rather than hand-crafted minimal outputs.
- Tests verify the *arguments* passed to the runner and the
  `elevated=False` flag — not system state.
- Each public verb has at minimum a success-path test demonstrating both
  argv construction and field-level parsing.
- A failure-path test asserts that a non-zero runner result raises
  `UbuntuHardwareInventoryQueryError` carrying the argv, exit code, and
  stderr.
- A read-elevation test exercises every verb with
  `config.elevated=True` and asserts that no runner call received
  `elevated=True`.

### Test File

`source/testroots/testbase/automojo/tests/sysman/linux/ubuntu/test_hardwareinventory.py`

### Tests Implemented

1. `test_get_system_info_invokes_dmidecode_and_parses`
2. `test_get_cpu_info_parses_lscpu_output`
3. `test_get_memory_info_aggregates_modules_and_total`
4. `test_list_storage_devices_parses_lsblk_output`
5. `test_list_network_devices_parses_ip_link_output`
6. `test_list_pci_devices_parses_lspci_output`
7. `test_list_usb_devices_parses_lsusb_output`
8. `test_get_firmware_info_parses_dmidecode_bios_output`
9. `test_failed_command_raises_query_error`
10. `test_reads_never_elevate`
