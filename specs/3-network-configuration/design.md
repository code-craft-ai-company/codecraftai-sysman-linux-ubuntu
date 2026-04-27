# Design

## Overview

The network configuration feature is implemented as a single module,
`automojo.sysman.linux.ubuntu.networkconfiguration`, that exposes a
`UbuntuNetworkConfigurationManager` class.  This spec implements the
network-configuration contract defined at
`automojo-sysman-common/specs/contracts/network-configuration/contract.md` for
Ubuntu using `netplan` as the primary persistent surface (edited via an
`IFileOperator` and applied via `netplan apply`), `iproute2` (`ip` and `ip route`)
for runtime state, `systemd-resolved` / `resolvectl` for DNS, and `hostnamectl` for
the hostname.

All read paths used for idempotency guards go through `ip -j ...` JSON output,
`resolvectl`, `hostnamectl --static`, or the file operator reading
`/etc/netplan/*.yaml`; output is parsed in pure Python into `UbuntuInterfaceInfo` /
`UbuntuRouteInfo` records.  All system interaction is delegated to a command runner
implementing the `ICommandRunner` interface and to an `IFileOperator`.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner + file operator                      │
│  - Configures aspects (optional)                            │
│  - Injects both into UbuntuNetworkConfigurationManager      │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│     UbuntuNetworkConfigurationManager                       │
│  automojo.sysman.linux.ubuntu.networkconfiguration          │
│                                                             │
│  get_interfaces()    get_interface()                        │
│  configure_interface()                                      │
│  enable_interface()  disable_interface()                    │
│  add_route()         remove_route()      get_routes()       │
│  set_dns_servers()   get_dns_servers()                      │
│  set_search_domains()                                       │
│  get_hostname()      set_hostname()                         │
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
│  - PersistenceAspect  - RetryAspect  - TimeoutAspect        │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `UbuntuNetworkConfigurationManager`

**Location:** `source/packages/automojo/sysman/linux/ubuntu/networkconfiguration.py`

| Method | Signature | Description |
|---|---|---|
| `get_interfaces` | `() → list[UbuntuInterfaceInfo]` | `ip -j addr show` + `ip -j route show default` |
| `get_interface` | `(name) → UbuntuInterfaceInfo \| None` | `ip -j addr show dev <name>` |
| `configure_interface` | `(name, address, netmask, gateway) → UbuntuNetworkActionResult` | netplan YAML + `netplan apply` |
| `enable_interface` | `(name) → UbuntuNetworkActionResult` | `ip link set <name> up` |
| `disable_interface` | `(name) → UbuntuNetworkActionResult` | `ip link set <name> down` |
| `add_route` | `(dest, gateway, interface) → UbuntuNetworkActionResult` | `ip route add` |
| `remove_route` | `(dest) → UbuntuNetworkActionResult` | `ip route del` |
| `get_routes` | `() → list[UbuntuRouteInfo]` | `ip -j route show` |
| `set_dns_servers` | `(servers) → UbuntuNetworkActionResult` | `resolvectl dns` + netplan YAML |
| `get_dns_servers` | `() → list[str]` | `resolvectl dns` |
| `set_search_domains` | `(domains) → UbuntuNetworkActionResult` | `resolvectl domain` + netplan YAML |
| `get_hostname` | `() → str` | `hostnamectl --static` |
| `set_hostname` | `(name) → UbuntuNetworkActionResult` | `hostnamectl set-hostname` |

### `UbuntuNetworkConfigurationManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands / file writes are dispatched |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `netplan_path` | `str` | `/etc/netplan/99-automojo.yaml` | Managed netplan config |
| `persist_dns` | `bool` | `True` | When `True`, DNS edits update netplan + runtime |

---

## Data Models

### Result Types

```python
class UbuntuNetworkActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class UbuntuNetworkActionResult:
    status: UbuntuNetworkActionStatus
    interface: str | None
    detail: str | None
    diff: str | None                    # unified diff for file-backed writes in DRY_RUN
    message: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
```

### Interface / Route Types

```python
@dataclass
class UbuntuInterfaceInfo:
    name: str
    mac_address: str | None
    addresses: list[str]                # <addr>/<prefixlen>
    gateway: str | None
    state: str                          # "up" / "down" / "unknown"
    mtu: int | None

@dataclass
class UbuntuRouteInfo:
    destination: str                    # CIDR; "default" normalised to "0.0.0.0/0"
    gateway: str | None
    interface: str | None
    metric: int | None
```

### Exceptions

```python
class UbuntuNetworkQueryError(Exception):
    """Raised when an ip / resolvectl / hostnamectl query fails unexpectedly."""
```

---

## Interfaces

### `ICommandRunner` / `IFileOperator`

Same shapes as the filesystem-management spec.  `IFileOperator.write_text(atomic=True)`
is used for every netplan rewrite so updates survive connection drops over SSH.

---

## Interface-Name Mapping

Ubuntu uses kernel `ifname` (`enp0s3`, `ens33`, `eth0`, `wlan0`, `bond0`, …).  The
manager treats `name` as the kernel ifname directly.  When a caller needs a
NetworkManager connection name instead, they MUST pre-resolve it; the Ubuntu
implementation does not attempt `nmcli` fallbacks because Ubuntu's server and
recent desktop installs prefer netplan / systemd-networkd as the persistent
surface.

Writes reach both runtime and persistent state:

- **Runtime:** `ip addr`, `ip link`, `ip route`, `resolvectl`.
- **Persistent:** `/etc/netplan/99-automojo.yaml` (rewritten via `IFileOperator`),
  then `netplan apply` which re-renders `/run/systemd/network/` and reconciles with
  `networkd`.

When `netplan apply` succeeds, the runtime side is already consistent and the
manager does not duplicate the `ip` commands.  The runtime-only fallback commands
documented in the Requirements section are executed only when the operator opts
into them explicitly via an optional `UbuntuNetworkConfigurationManagerConfig`
flag; this is deferred and not enabled by default.

---

## Command Mappings

### Write Commands

| Operation | Command / File operation |
|---|---|
| configure_interface | read `/etc/netplan/99-automojo.yaml` → modify → `IFileOperator.write_text(..., atomic=True)` → `netplan apply` |
| enable_interface | `ip link set <name> up` |
| disable_interface | `ip link set <name> down` |
| add_route | `ip route add <dest> via <gateway> [dev <iface>]` |
| remove_route | `ip route del <dest>` |
| set_dns_servers | `resolvectl dns <iface> <servers...>` + netplan rewrite + `netplan apply` |
| set_search_domains | `resolvectl domain <iface> <domains...>` + netplan rewrite + `netplan apply` |
| set_hostname | `hostnamectl set-hostname <name>` |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| get_interfaces | `ip -j addr show` + `ip -j route show default` |
| get_interface | `ip -j addr show dev <name>` |
| get_routes | `ip -j route show` |
| get_dns_servers | `resolvectl dns` |
| get_hostname | `hostnamectl --static` |

All writes pass `elevated=True` when the config enables it.  Reads never elevate.

---

## Output Parsing

### `ip -j addr show` output

```json
[
  {
    "ifindex": 2,
    "ifname": "enp0s3",
    "operstate": "UP",
    "mtu": 1500,
    "address": "08:00:27:aa:bb:cc",
    "addr_info": [
      {"family": "inet", "local": "10.0.2.15", "prefixlen": 24},
      {"family": "inet6", "local": "fe80::...", "prefixlen": 64, "scope": "link"}
    ]
  }
]
```

Parser rules:

- `ifname` → `name`.
- `address` → `mac_address`; absent for loopback interfaces.
- `addr_info` → `addresses` as `<local>/<prefixlen>` strings, IPv4 first.
- `operstate.lower()` → `state`.
- `mtu` → `mtu` (may be missing on loopback).
- `gateway` is filled from `ip -j route show default` matching on `dev`.

### `ip -j route show` output

```json
[
  {"dst": "default", "gateway": "10.0.2.2", "dev": "enp0s3", "prefsrc": "10.0.2.15", "metric": 100},
  {"dst": "10.0.2.0/24", "dev": "enp0s3", "prefsrc": "10.0.2.15", "scope": "link"}
]
```

`dst` is replaced with `"0.0.0.0/0"` when its value is the literal `"default"`.
Missing `gateway` or `metric` becomes `None`.

### `resolvectl dns` output

```
Global:
Link 2 (enp0s3): 8.8.8.8 8.8.4.4
Link 3 (wlan0): 1.1.1.1
```

Parser rule: collect every IP on lines starting with `Global:` or `Link`, preserve
order, de-duplicate.

### `hostnamectl --static` output

```
host.example.com
```

The parser strips whitespace and returns the single-line string.

### Netplan YAML shape

The manager's managed file `/etc/netplan/99-automojo.yaml` has the following
canonical shape:

```yaml
network:
  version: 2
  renderer: networkd
  ethernets:
    enp0s3:
      dhcp4: false
      addresses: [10.0.2.50/24]
      gateway4: 10.0.2.1
      nameservers:
        addresses: [8.8.8.8, 1.1.1.1]
        search: [example.com]
```

The manager loads the file via a YAML library, mutates the Python dict, and
re-emits.  Unmanaged files (`/etc/netplan/50-cloud-init.yaml`, etc.) are never
touched — the manager only writes to the configured `netplan_path`.

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `configure_interface` | managed netplan YAML already has a matching ethernets entry | `read_text(netplan_path)` |
| `enable_interface` | current `state == "up"` | `get_interface(name)` |
| `disable_interface` | current `state == "down"` | `get_interface(name)` |
| `add_route` | equivalent route already present | `get_routes()` |
| `remove_route` | no route for `dest` | `get_routes()` |
| `set_dns_servers` | current list already matches | `get_dns_servers()` |
| `set_search_domains` | current list already matches | `resolvectl domain` |
| `set_hostname` | current static hostname already equals `<name>` | `get_hostname()` |

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UbuntuNetworkQueryError` | `ip`, `resolvectl`, or `hostnamectl` exits non-zero for a reason other than "Device does not exist" |

All write failures are surfaced as
`UbuntuNetworkActionResult(status=FAILED, ...)` carrying the runner's exit code,
stdout, stderr, and a `message` identifying the failing surface when an
apply-then-persist sequence only partially succeeds.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: get_interfaces JSON Parsing

*For any* `ip -j addr show` JSON output, `get_interfaces()` should return `UbuntuInterfaceInfo` records with `name`, `mac_address`, `addresses`, and `state` populated from the corresponding JSON fields.

**Validates: Requirements 1.1, 1.2**

### Property 2: get_interfaces Gateway Join

*For any* `ip -j route show default` result with `dev == <iface>`, the gateway from that route should appear on the `UbuntuInterfaceInfo` for that interface.

**Validates: Requirements 1.2**

### Property 3: get_interface Missing Returns None

*For any* `get_interface(name)` where `ip` exits non-zero with `Device "<name>" does not exist`, the return should be `None`.

**Validates: Requirements 2.3**

### Property 4: configure_interface Netplan Rewrite

*For any* `configure_interface(name, address, netmask, gateway)` call, the manager should rewrite the managed netplan YAML to contain `ethernets.<name>` with `addresses: ["<address>/<prefix>"]`, `gateway4: <gateway>`, `dhcp4: false`.

**Validates: Requirements 3.1**

### Property 5: configure_interface Idempotency

*For any* netplan YAML that already contains an identical `ethernets.<name>` entry, `configure_interface()` should return `SKIPPED`.

**Validates: Requirements 3.3**

### Property 6: configure_interface Failing-Step Message

*For any* netplan write succeeding but `netplan apply` failing, `configure_interface()` should return `FAILED` with `message` identifying the failing step.

**Validates: Requirements 3.4**

### Property 7: configure_interface Dry-Run Diff

*For any* `configure_interface()` in dry-run, the `diff` field should contain a unified diff of the intended YAML change.

**Validates: Requirements 3.6**

### Property 8: enable_interface Idempotency

*For any* interface already in `state="up"`, `enable_interface()` should return `SKIPPED`.

**Validates: Requirements 4.2**

### Property 9: disable_interface Idempotency

*For any* interface already in `state="down"`, `disable_interface()` should return `SKIPPED`.

**Validates: Requirements 5.2**

### Property 10: add_route Idempotency

*For any* route already present in `get_routes()` matching `(dest, gateway, interface)`, `add_route()` should return `SKIPPED`.

**Validates: Requirements 6.2**

### Property 11: remove_route Idempotency

*For any* destination with no route in `get_routes()`, `remove_route()` should return `SKIPPED`.

**Validates: Requirements 7.2**

### Property 12: get_routes JSON Parsing

*For any* `ip -j route show` JSON output, `get_routes()` should map `dst == "default"` to `destination = "0.0.0.0/0"`.

**Validates: Requirements 8.2**

### Property 13: set_dns_servers Dual-Surface Write

*For any* `set_dns_servers(servers)` call (with `persist_dns=True`), the dispatch should include both `resolvectl dns <iface> <servers...>` and a netplan YAML rewrite followed by `netplan apply`.

**Validates: Requirements 9.1, 9.3**

### Property 14: set_dns_servers Idempotency

*For any* DNS-server list already matching `get_dns_servers()`, `set_dns_servers()` should return `SKIPPED`.

**Validates: Requirements 9.2**

### Property 15: set_dns_servers Partial-Failure Message

*For any* successful runtime step but failing persistent step (or vice versa), `set_dns_servers()` should return `FAILED` with `message` identifying the failing surface.

**Validates: Requirements 9.5**

### Property 16: get_dns_servers Dedupe

*For any* `resolvectl dns` output with overlapping `Global` and per-link entries, `get_dns_servers()` should de-duplicate while preserving order.

**Validates: Requirements 10.2**

### Property 17: set_search_domains Dual-Surface Write

*For any* `set_search_domains(domains)` call, the dispatch should include both `resolvectl domain <iface> <domains...>` and a netplan YAML rewrite followed by `netplan apply`.

**Validates: Requirements 11.1**

### Property 18: set_search_domains Idempotency

*For any* search-domain list already matching current state, `set_search_domains()` should return `SKIPPED`.

**Validates: Requirements 11.2**

### Property 19: get_hostname Returns Static Hostname

*For any* successful `hostnamectl --static` call, `get_hostname()` should return the stripped stdout.

**Validates: Requirements 12.1, 12.2**

### Property 20: set_hostname Single-Command

*For any* `set_hostname(name)` call, the dispatch should be exactly one `hostnamectl set-hostname <name>` invocation — `hostnamectl` covers both runtime and persistent state.

**Validates: Requirements 13.1**

### Property 21: set_hostname Idempotency

*For any* `name` already equal to the current static hostname, `set_hostname()` should return `SKIPPED`.

**Validates: Requirements 13.2**

### Property 22: Dry-Run Safety

*For any* write operation with `dry_run=True`, the runner MUST NOT be invoked and the file operator MUST NOT perform `write_text`; the result status MUST be `DRY_RUN`.

**Validates: Requirements 3.6, 4.5, 5.5, 6.5, 7.5, 9.6, 11.5, 13.5**

### Property 23: Elevation for Writes

*For any* write with `elevated=True` in the config, the runner and file operator MUST be invoked with `elevated=True`.

**Validates: Requirements 14.1**

### Property 24: No Elevation for Reads

*For any* read (`get_interfaces`, `get_interface`, `get_routes`, `get_dns_servers`, `get_hostname`), the runner MUST NEVER be invoked with `elevated=True`.

**Validates: Requirements 14.2**

### Property 25: Unrelated Netplan Files Untouched

*For any* netplan write, only `netplan_path` is read and written — other files in `/etc/netplan/` (for example `50-cloud-init.yaml`) are never read or modified.

**Validates: Requirements 3.1**

### Property 26: Error Result Consistency

*For any* non-zero exit from a write command, the returned `UbuntuNetworkActionResult` should have `status=FAILED`, and carry the runner's exit code and stderr.

**Validates: Requirements 3.4, 4.4, 5.4, 6.4, 7.4, 9.5, 11.4, 13.4**

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/
├── __init__.py
└── networkconfiguration.py      ← UbuntuNetworkConfigurationManager, config,
                                    models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/ubuntu/
    └── test_networkconfiguration.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.linux.ubuntu.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.linux.ubuntu.fileoperators import LocalFileOperator
from automojo.sysman.linux.ubuntu.networkconfiguration import (
    UbuntuNetworkConfigurationManager,
    UbuntuNetworkConfigurationManagerConfig,
)

runner = LocalCommandRunner(LocalRunnerConfig())
file_op = LocalFileOperator()

config = UbuntuNetworkConfigurationManagerConfig(elevated=True, dry_run=False)
net_mgr = UbuntuNetworkConfigurationManager(runner=runner, file_operator=file_op,
                                            config=config)

net_mgr.configure_interface(
    name="enp0s3", address="10.0.2.50", netmask="255.255.255.0",
    gateway="10.0.2.1",
)

net_mgr.set_dns_servers(servers=["1.1.1.1", "8.8.8.8"])
net_mgr.set_search_domains(domains=["example.com"])
net_mgr.set_hostname(name="worker-01.example.com")

for iface in net_mgr.get_interfaces():
    print(f"{iface.name}\t{iface.state}\t{iface.addresses}")
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
from automojo.sysman.linux.ubuntu.networkconfiguration import (
    UbuntuNetworkConfigurationManager, UbuntuNetworkConfigurationManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-ubuntu", username="admin"))
file_op = SSHFileOperator(host="remote-ubuntu", username="admin")

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(aspect=PersistenceAspect(PersistenceAspectConfig(storage_backend=storage)))
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2)))
runner.add_aspect(aspect=TimeoutAspect(TimeoutAspectConfig(default_timeout=30.0)))

net_mgr = UbuntuNetworkConfigurationManager(
    runner=runner, file_operator=file_op,
    config=UbuntuNetworkConfigurationManagerConfig(elevated=True),
)

net_mgr.add_route(dest="172.16.0.0/12", gateway="10.0.2.2", interface="enp0s3")
```

### Dry-Run Mode With Diff

```python
config = UbuntuNetworkConfigurationManagerConfig(elevated=True, dry_run=True)
net_mgr = UbuntuNetworkConfigurationManager(runner=runner, file_operator=file_op,
                                            config=config)

result = net_mgr.configure_interface(
    name="enp0s3", address="10.0.2.99", netmask="255.255.255.0",
    gateway="10.0.2.1",
)
assert result.status == UbuntuNetworkActionStatus.DRY_RUN
print(result.diff)  # unified diff of the netplan YAML change
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on argv prefix and a
  `MockFileOperator`.
- `ip -j addr show` and `ip -j route show` JSON mocks drive `get_interfaces`,
  `get_interface`, `get_routes`, and every interface-level idempotency guard.
- `resolvectl dns` and `resolvectl domain` text mocks drive DNS and
  search-domain idempotency.
- `hostnamectl --static` text mocks drive hostname idempotency.
- Netplan YAML round-trip tests assert that rewriting the managed file
  preserves unrelated keys (e.g. `renderer`, existing `ethernets` entries for
  other interfaces) and that the emission is deterministic.
- A dedicated test proves that `set_hostname` dispatches exactly one command —
  namely `hostnamectl set-hostname <name>` — and that its `hostnamectl` call
  updates both runtime and persistent hostname in one go.
- Dry-run tests prove every write short-circuits to `DRY_RUN` without invoking
  the runner or performing `write_text`, and that `configure_interface` /
  `set_dns_servers` / `set_search_domains` populate a unified diff.
- Elevation tests prove writes pass `elevated=True` when configured and reads
  never do.
- Dual-surface failure tests cover: runtime step succeeding but persistent
  step failing; persistent step succeeding but runtime step failing.
- No test requires root access or a live netplan installation.

---

## Scope Mapping

| Contract verb | Ubuntu implementation |
|---|---|
| `get_interfaces` | `ip -j addr show` + default-gateway join |
| `get_interface` | `ip -j addr show dev <name>` |
| `configure_interface` | netplan YAML rewrite + `netplan apply` |
| `enable_interface` | `ip link set <name> up` |
| `disable_interface` | `ip link set <name> down` |
| `add_route` | `ip route add` |
| `remove_route` | `ip route del` |
| `get_routes` | `ip -j route show` |
| `set_dns_servers` | `resolvectl dns` + netplan YAML + `netplan apply` |
| `get_dns_servers` | `resolvectl dns` |
| `set_search_domains` | `resolvectl domain` + netplan YAML + `netplan apply` |
| `get_hostname` | `hostnamectl --static` |
| `set_hostname` | `hostnamectl set-hostname` |

---

## Extensions Beyond the Contract

None in this Ubuntu implementation.  VLAN / bridge / bond / Wi-Fi configuration
and NetworkManager-profile authoring are intentionally deferred; when added they
will live behind their own feature flags so a coordinator writing against the
contract does not have to special-case Ubuntu.
