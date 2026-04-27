# Design

## Overview

The firewall management feature is implemented as a single module,
`automojo.sysman.linux.ubuntu.firewallmanagement`, that exposes a
`UbuntuFirewallManager` class.  This spec implements the firewall-management contract
defined at `automojo-sysman-common/specs/contracts/firewall-management/contract.md`
for Ubuntu using `ufw` (Uncomplicated Firewall).  The coordinator instantiates
`UbuntuFirewallManager` and calls its methods to perform discrete maintenance actions
against the ufw service via the `ufw` CLI.

All state queries used for idempotency guards go through `ufw status verbose` which is
parsed in pure Python into a `UbuntuZoneInfo`.  All system interaction is delegated to a
command runner implementing the `ICommandRunner` interface.  No direct file system
access or `subprocess` calls are made inside the module.

ufw rules applied through its CLI are persistent by default — they are written to
`/etc/ufw/user.rules` and `/etc/ufw/user6.rules` — so there is no separate `--permanent`
flag.  After a batch of rule changes the coordinator is expected to call `reload()` to
re-apply the ruleset to the kernel netfilter tables.

The feature works identically whether commands execute locally (via `LocalCommandRunner`)
or remotely over SSH (via `SSHCommandRunner`).  When using SSH, the coordinator can
track reliability metrics and apply execution aspects (persistence, retry, timeout)
without any changes to the `UbuntuFirewallManager` code.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md) for
details on local/SSH execution, reliability tracking, and aspect configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into UbuntuFirewallManager                │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuFirewallManager                            │
│  automojo.sysman.linux.ubuntu.firewallmanagement            │
│                                                             │
│  add_service()      remove_service()                        │
│  add_port()         remove_port()                           │
│  add_rich_rule()    remove_rich_rule()                      │
│  set_default_zone() reload()                                │
│  query_zone()       query_all_zones()                       │
│  query_state()                                              │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, capture_output) → RunResult            │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH connection     │
│ - sudo for elevation │      │ - reliability metrics│
│ - local execution    │      │ - connection retry   │
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

### `UbuntuFirewallManager`

**Location:** `source/packages/automojo/sysman/linux/ubuntu/firewallmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `add_service` | `(service, zone) → UbuntuFirewallActionResult` | Add ufw application allow rule |
| `remove_service` | `(service, zone) → UbuntuFirewallActionResult` | Remove ufw application allow rule |
| `add_port` | `(port, protocol, zone) → UbuntuFirewallActionResult` | Open port/protocol in ufw |
| `remove_port` | `(port, protocol, zone) → UbuntuFirewallActionResult` | Close port/protocol in ufw |
| `add_rich_rule` | `(rule, zone) → UbuntuFirewallActionResult` | Add raw ufw-syntax rule |
| `remove_rich_rule` | `(rule, zone) → UbuntuFirewallActionResult` | Remove raw ufw-syntax rule |
| `set_default_zone` | `(zone) → UbuntuFirewallActionResult` | No-op for ufw — returns SKIPPED for `"default"`, FAILED otherwise |
| `reload` | `() → UbuntuFirewallActionResult` | Invoke `ufw reload` |
| `query_zone` | `(zone) → UbuntuZoneInfo` | Query services, ports, rules from `ufw status verbose` |
| `query_all_zones` | `() → list[UbuntuZoneInfo]` | Single-entry list for `"default"` |
| `query_state` | `() → UbuntuFirewallState` | Query running state via `ufw status` |

### `UbuntuFirewallManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |

---

## Data Models

### Result Types

```python
class UbuntuFirewallActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

@dataclass
class UbuntuFirewallActionResult:
    status: UbuntuFirewallActionStatus
    zone: str | None
    detail: str | None          # service name, port/proto, rule string, or zone name
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
```

### Query Result Types

```python
@dataclass
class UbuntuZoneInfo:
    zone: str                   # always "default" on ufw
    services: list[str]
    ports: list[str]            # each entry is "<port>/<protocol>"
    rich_rules: list[str]
    interfaces: list[str]       # always [] — ufw has no per-zone interface concept
    sources: list[str]          # always [] — sources are embedded in rules
    is_default: bool            # always True

@dataclass
class UbuntuFirewallState:
    running: bool
    default_zone: str           # "default" when running, "" otherwise

class UbuntuFirewallQueryError(Exception):
    """Raised when a ufw status query fails."""
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

ufw requires no additional interfaces — rule files are managed by the ufw CLI itself.

---

## Zone Mapping

ufw has no concept of zones.  The entire firewall is a single global policy.  The
`UbuntuFirewallManager` validates the `zone` parameter as follows:

- `zone == "default"` → accepted; operation proceeds.
- `zone` is any other value → operation returns `UbuntuFirewallActionResult` with
  `status=FAILED` and `detail="ufw supports only the 'default' zone; received '<zone>'"`.

Read methods observe the same rule: `query_zone("default")` returns the parsed
`UbuntuZoneInfo`; `query_zone(other)` raises `UbuntuFirewallQueryError`.

---

## Command Mappings

### Write Commands

| Operation | Command |
|---|---|
| Add service | `ufw allow <service>` |
| Remove service | `ufw delete allow <service>` |
| Add port | `ufw allow <port>/<protocol>` |
| Remove port | `ufw delete allow <port>/<protocol>` |
| Add rich rule | `ufw <rule>` (rule must start with `allow`/`deny`/`reject`/`limit`) |
| Remove rich rule | `ufw delete <rule>` |
| Set default zone | (no command — `SKIPPED` for `"default"`, `FAILED` otherwise) |
| Reload | `ufw reload` |

All write commands pass `elevated=True` when the config enables it.

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| Query zone / state detail | `ufw status verbose` |
| Query running state | `ufw status` |

ufw itself requires root to execute even the read commands on most installations.  Per
the contract, reads MUST NOT pass `elevated=True`; a non-root runner that encounters the
resulting permission denial will surface it through the command runner and the manager
will translate it into a `UbuntuFirewallQueryError` (for `query_zone`/`query_all_zones`)
or a `UbuntuFirewallState(running=False, default_zone="")` (for `query_state`).

---

## `ufw status verbose` Output Parsing

`ufw status verbose` produces output in the following format when ufw is active:

```
Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    Anywhere
80/tcp                     ALLOW IN    Anywhere
OpenSSH                    ALLOW IN    Anywhere
Anywhere                   ALLOW IN    192.168.1.0/24
443/tcp (v6)               ALLOW IN    Anywhere (v6)
```

The parser processes this output as follows:

| Output shape | Maps to `UbuntuZoneInfo` field |
|---|---|
| First line `Status: active` / `Status: inactive` | used by `query_state()` to set `running` |
| `Default: deny (incoming), allow (outgoing)` | informational — logged only |
| `To` column value matching `<port>/<proto>` | appended to `ports` |
| `To` column value that is a non-numeric token (e.g., `OpenSSH`) | appended to `services` |
| Rows where `From` is not `Anywhere` (has a CIDR, IP, or interface) | preserved verbatim as `"allow from <from> to <to>"` (or `deny`/`reject`/`limit` if applicable) in `rich_rules` |
| IPv6 variants `(v6)` | collapsed onto the same rule — the parser de-duplicates `<port>/<proto>` across the IPv4/IPv6 rows |

The parser:

1. Splits the output into a header section (preamble) and a table section (starting at
   the `To` header and its dash separator).
2. For each table row, splits on two-or-more spaces to get `to_col`, `action_col`,
   `from_col`.
3. If `from_col` starts with `Anywhere` and `to_col` matches `^\d+/(tcp|udp)$`, appends
   to `ports`.
4. If `from_col` starts with `Anywhere` and `to_col` is a non-numeric token, appends to
   `services`.
5. Otherwise, reconstructs a ufw-syntax rule string from the action verb + `from` + `to`
   and appends to `rich_rules`.

`interfaces` and `sources` are always empty — ufw does not expose those as per-zone
lists; source constraints are folded into individual rules and surfaced through
`rich_rules`.

---

## Idempotency Guards

All idempotency checks call `query_zone("default")` first to obtain the current
`UbuntuZoneInfo`, then compare the desired state to the current state in pure Python.

| Method | Guard | Field inspected |
|---|---|---|
| `add_service()` | Service already present | `UbuntuZoneInfo.services` |
| `remove_service()` | Service not present | `UbuntuZoneInfo.services` |
| `add_port()` | `<port>/<proto>` already open | `UbuntuZoneInfo.ports` |
| `remove_port()` | `<port>/<proto>` not open | `UbuntuZoneInfo.ports` |
| `add_rich_rule()` | Normalised rule already present | `UbuntuZoneInfo.rich_rules` |
| `remove_rich_rule()` | Normalised rule not present | `UbuntuZoneInfo.rich_rules` |
| `set_default_zone()` | N/A — always `SKIPPED` or `FAILED` | — |

`reload()` has no idempotency guard — it is always executed when not in dry-run mode.

---

## Rich Rule Comparison

ufw rich rules are compared after normalising whitespace (collapsing multiple spaces to
one, stripping leading/trailing whitespace) and lower-casing the action verb to avoid
false mismatches caused by formatting differences between the coordinator-supplied rule
and the reconstructed rule from `ufw status verbose`.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UbuntuFirewallQueryError` | `query_zone()` or `query_all_zones()` returns a non-zero exit code, or when `query_zone` is invoked with a zone other than `"default"` |

`ufw status` returning non-zero in `query_state()` is **not** an error — it means ufw is
not running, which is surfaced as `UbuntuFirewallState(running=False, default_zone="")`.

All other failures from write operations are surfaced as `UbuntuFirewallActionResult`
with `status=FAILED`.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system—essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Service Addition Command Construction

*For any* service name with `zone="default"`, calling `add_service()` should construct the command `ufw allow <service>` and pass it to the command runner.

**Validates: Requirements 1.1**

### Property 2: Service Addition Zone Validation

*For any* service name with `zone != "default"`, calling `add_service()` should return `FAILED` without invoking the runner.

**Validates: Requirements 1.2**

### Property 3: Service Addition Idempotency

*For any* service that is already present in the ufw ruleset, calling `add_service()` should return `SKIPPED` without executing any commands.

**Validates: Requirements 1.3**

### Property 4: Service Removal Command Construction

*For any* service name with `zone="default"`, calling `remove_service()` should construct the command `ufw delete allow <service>` and pass it to the command runner.

**Validates: Requirements 2.1**

### Property 5: Service Removal Idempotency

*For any* service that is not present in the ufw ruleset, calling `remove_service()` should return `SKIPPED` without executing any commands.

**Validates: Requirements 2.3**

### Property 6: Port Addition Command Construction

*For any* port number, protocol, with `zone="default"`, calling `add_port()` should construct the command `ufw allow <port>/<protocol>` and pass it to the command runner.

**Validates: Requirements 3.1**

### Property 7: Port Addition Idempotency

*For any* `<port>/<protocol>` combination already open in the ufw ruleset, calling `add_port()` should return `SKIPPED` without executing any commands.

**Validates: Requirements 3.3**

### Property 8: Port Removal Command Construction

*For any* port number, protocol, with `zone="default"`, calling `remove_port()` should construct the command `ufw delete allow <port>/<protocol>` and pass it to the command runner.

**Validates: Requirements 4.1**

### Property 9: Port Removal Idempotency

*For any* `<port>/<protocol>` combination not present in the ufw ruleset, calling `remove_port()` should return `SKIPPED` without executing any commands.

**Validates: Requirements 4.3**

### Property 10: Rich Rule Addition Command Construction

*For any* rule string beginning with a valid ufw action verb, calling `add_rich_rule()` should construct the command `ufw <rule>` and pass it to the command runner.

**Validates: Requirements 5.1**

### Property 11: Rich Rule Verb Validation

*For any* rule string that does NOT begin with a valid ufw action verb, `add_rich_rule()` should return `FAILED` without invoking the runner.

**Validates: Requirements 5.2**

### Property 12: Rich Rule Addition Idempotency

*For any* rule (after whitespace normalisation) already present in the ufw ruleset, calling `add_rich_rule()` should return `SKIPPED` without executing any commands.

**Validates: Requirements 5.4**

### Property 13: Rich Rule Removal Command Construction

*For any* rule string with `zone="default"`, calling `remove_rich_rule()` should construct the command `ufw delete <rule>` and pass it to the command runner.

**Validates: Requirements 6.1**

### Property 14: Rich Rule Removal Idempotency

*For any* rule not present in the ufw ruleset, calling `remove_rich_rule()` should return `SKIPPED` without executing any commands.

**Validates: Requirements 6.3**

### Property 15: Default Zone Is No-Op on "default"

*For any* call to `set_default_zone("default")`, the system should return `SKIPPED` without invoking the runner.

**Validates: Requirements 7.1**

### Property 16: Default Zone Rejects Other Values

*For any* call to `set_default_zone(zone)` with `zone != "default"`, the system should return `FAILED` without invoking the runner.

**Validates: Requirements 7.2**

### Property 17: Reload Command Construction

*For any* call to `reload()`, the system should construct the command `ufw reload` and pass it to the command runner.

**Validates: Requirements 8.1**

### Property 18: Zone Query Command Construction

*For any* call to `query_zone("default")`, the system should construct the command `ufw status verbose` and pass it to the command runner without elevation.

**Validates: Requirements 9.1**

### Property 19: Zone Query Result Parsing

*For any* successful `ufw status verbose` output, `query_zone()` should return a `UbuntuZoneInfo` with correctly parsed services, ports, and rich_rules lists.

**Validates: Requirements 9.2**

### Property 20: Invalid Zone Raises QueryError

*For any* call to `query_zone(zone)` with `zone != "default"`, the system should raise `UbuntuFirewallQueryError`.

**Validates: Requirements 9.3**

### Property 21: Query All Zones Returns Single Entry

*For any* call to `query_all_zones()`, the result should be a one-element list containing the `"default"` `UbuntuZoneInfo` with `is_default=True`.

**Validates: Requirements 10.1, 10.2**

### Property 22: Firewall State Active Parsing

*For any* `ufw status` output starting with `Status: active`, `query_state()` should return `UbuntuFirewallState(running=True, default_zone="default")`.

**Validates: Requirements 11.2**

### Property 23: Firewall State Inactive Parsing

*For any* `ufw status` output starting with `Status: inactive`, `query_state()` should return `UbuntuFirewallState(running=False, default_zone="")`.

**Validates: Requirements 11.3**

### Property 24: Firewall Not Running Handling

*For any* failed `ufw status` command, `query_state()` should return `UbuntuFirewallState(running=False, default_zone="")` without raising.

**Validates: Requirements 11.4**

### Property 25: Dry-Run Safety

*For any* write operation (`add_service`, `remove_service`, `add_port`, `remove_port`, `add_rich_rule`, `remove_rich_rule`, `set_default_zone`, `reload`) with `dry_run=True`, the command runner should never be invoked for write commands, and the result status should be `DRY_RUN`.

**Validates: Requirements 1.6, 2.6, 3.6, 4.6, 5.7, 6.6, 7.3, 8.4**

### Property 26: Elevation for Write Operations

*For any* write operation with `elevated=True` in the config, the command runner should be invoked with `elevated=True` for all write commands.

**Validates: Requirements 12.1**

### Property 27: No Elevation for Read Operations

*For any* read operation (`query_zone`, `query_all_zones`, `query_state`), the command runner should never be invoked with `elevated=True` regardless of the elevation configuration.

**Validates: Requirements 12.2**

### Property 28: Error Result Consistency

*For any* failed ufw command, the returned `UbuntuFirewallActionResult` should have `status=FAILED` and contain the exit code and stderr from the command runner.

**Validates: Requirements 1.5, 2.5, 3.5, 4.5, 5.6, 6.5, 8.3**

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/
├── __init__.py
└── firewallmanagement.py         ← UbuntuFirewallManager, config, models, exceptions

source/testroots/testbase/
└── automojo/tests/sysman/linux/ubuntu/
    └── test_firewallmanagement.py
```

---

## Usage Examples

### Local Execution (Default)

```python
from automojo.sysman.linux.ubuntu.runners import LocalCommandRunner, LocalRunnerConfig
from automojo.sysman.linux.ubuntu.firewallmanagement import (
    UbuntuFirewallManager,
    UbuntuFirewallManagerConfig,
)

# Create local runner
runner = LocalCommandRunner(LocalRunnerConfig())

# Inject into firewall manager
config = UbuntuFirewallManagerConfig(elevated=True, dry_run=False)
fw_mgr = UbuntuFirewallManager(runner=runner, config=config)

# Add services to the default zone
result = fw_mgr.add_service(service="OpenSSH", zone="default")
print(f"Add service: {result.status}")

result = fw_mgr.add_service(service="Apache Full", zone="default")
print(f"Add service: {result.status}")

# Open custom port
port_result = fw_mgr.add_port(port=8080, protocol="tcp", zone="default")
print(f"Add port: {port_result.status}")

# Add rich rule for specific source
rich_rule = "allow from 192.168.1.0/24 to any port 22 proto tcp"
rule_result = fw_mgr.add_rich_rule(rule=rich_rule, zone="default")
print(f"Add rich rule: {rule_result.status}")

# Apply changes
reload_result = fw_mgr.reload()
print(f"Reload: {reload_result.status}")

# Query zone state
zone_info = fw_mgr.query_zone(zone="default")
print(f"Services: {zone_info.services}")
print(f"Ports: {zone_info.ports}")
```

### SSH Execution with Aspects

```python
from automojo.sysman.linux.ubuntu.runners import SSHCommandRunner, SSHRunnerConfig
from automojo.sysman.linux.ubuntu.aspects import (
    PersistenceAspect,
    RetryAspect,
    TimeoutAspect,
    PersistenceAspectConfig,
    RetryAspectConfig,
    TimeoutAspectConfig,
)
from automojo.sysman.linux.ubuntu.storage import SQLiteStorage
from automojo.sysman.linux.ubuntu.firewallmanagement import (
    UbuntuFirewallManager,
    UbuntuFirewallManagerConfig,
)

runner = SSHCommandRunner(SSHRunnerConfig(host="remote-ubuntu", username="admin"))

storage = SQLiteStorage(path="/var/log/maintenance/commands.db")
persistence = PersistenceAspect(PersistenceAspectConfig(storage_backend=storage))
runner.add_aspect(aspect=persistence)

retry = RetryAspect(RetryAspectConfig(
    max_retries=3,
    retryable_exit_codes=[1],
    retryable_error_patterns=["ERROR: problem running ufw"],
))
runner.add_aspect(aspect=retry)

timeout = TimeoutAspect(TimeoutAspectConfig(default_timeout=60.0))
runner.add_aspect(aspect=timeout)

fw_mgr = UbuntuFirewallManager(
    runner=runner,
    config=UbuntuFirewallManagerConfig(elevated=True),
)

fw_mgr.add_service(service="OpenSSH", zone="default")
fw_mgr.add_port(port=443, protocol="tcp", zone="default")
fw_mgr.reload()

state = fw_mgr.query_state()
print(f"ufw running: {state.running}")
```

### Dry-Run Mode

```python
# Dry-run works identically for local and SSH
config = UbuntuFirewallManagerConfig(elevated=True, dry_run=True)
fw_mgr = UbuntuFirewallManager(runner=runner, config=config)

result = fw_mgr.remove_service(service="OpenSSH", zone="default")
assert result.status == UbuntuFirewallActionStatus.DRY_RUN
print("Would have removed OpenSSH from ufw ruleset")
```

---

## Testing Strategy

- All tests use a `MockCommandRunner` keyed on command args prefix.
- `ufw status verbose` mock responses drive all idempotency guards for service, port,
  and rich rule methods.
- `ufw status` mocks drive `query_state()` tests, including the `Status: active`,
  `Status: inactive`, and non-zero-exit cases.
- Rich rule normalisation is tested in pure Python with representative rule strings.
- `ufw status verbose` parsing is tested with representative multi-line output strings
  including mixed services, ports, and rich rules, and with both the active and inactive
  headers.
- A dedicated test proves that `set_default_zone("default")` returns `SKIPPED` without
  invoking the runner, and that `set_default_zone("other")` returns `FAILED` without
  invoking the runner.
- A dedicated test proves the rich-rule verb validation rejects rules that do not begin
  with `allow`, `deny`, `reject`, or `limit`.
- No test requires root access or a live `ufw` installation.
