# Requirements

## Introduction

The network configuration feature provides the `automojo-sysman-linux-ubuntu`
package with the ability to query and configure network interfaces, routes, DNS
servers, search domains, and the system hostname on Ubuntu systems.  This spec
implements the network-configuration contract defined at
`automojo-sysman-common/specs/contracts/network-configuration/contract.md` for
Ubuntu using `netplan` as the primary persistent surface (via transactional edits to
`/etc/netplan/*.yaml` through an `IFileOperator`, followed by `netplan apply`),
`iproute2` (`ip` and `ip route`) for runtime state, `systemd-resolved` / `resolvectl`
for DNS, and `hostnamectl` for the hostname.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interfaces.  The feature does not decide *which*
interfaces should be up or which IPs should be assigned — it provides the capability
to *manipulate network state* reliably, idempotently, and with full auditability.
All system commands are executed exclusively through the `automojo-interfaces`
command-runner abstraction; all file reads and writes against `/etc/netplan/*.yaml`
flow through an `IFileOperator`.

---

## Requirements

### Requirement 1: Query All Interfaces

**User Story:** As a maintenance coordinator, I want to list every network interface
so that reporting and idempotency workflows can enumerate interfaces without parsing
raw tool output.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_interfaces()` THE SYSTEM SHALL dispatch
   `ip -j addr show` through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the JSON output into a list of
   `UbuntuInterfaceInfo` records with `name`, `mac_address` (from `address`),
   `addresses` (list of `<addr>/<prefixlen>` strings from `addr_info`), `gateway`
   (resolved via `ip -j route show default`), `state` (from `operstate` lowered),
   and `mtu`.
3. WHEN the command fails THE SYSTEM SHALL raise a `UbuntuNetworkQueryError`
   carrying the exit code and stderr.

---

### Requirement 2: Query a Single Interface

**User Story:** As a maintenance coordinator, I want to query a single interface's
detail, so that downstream logic can branch on existence and state without
iterating `get_interfaces()`.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_interface(name)` THE SYSTEM SHALL dispatch
   `ip -j addr show dev <name>` through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the JSON output into a
   `UbuntuInterfaceInfo` using the same field mapping as `get_interfaces()`.
3. WHEN the command exits non-zero with stderr matching `Device "<name>" does not
   exist` THE SYSTEM SHALL return `None`.
4. WHEN the command exits non-zero for any other reason THE SYSTEM SHALL raise a
   `UbuntuNetworkQueryError`.

---

### Requirement 3: Configure an Interface

**User Story:** As a maintenance coordinator, I want to assign a static IP address,
netmask, and gateway to an interface so that persistent networking is in place and
the runtime state matches.

#### Acceptance Criteria

1. WHEN the coordinator calls `configure_interface(name, address, netmask, gateway)`
   THE SYSTEM SHALL first update the netplan configuration by reading the managed
   YAML file (`/etc/netplan/99-automojo.yaml` by default) via the file operator,
   replacing or inserting an `ethernets:` entry that sets `addresses:
   ["<address>/<prefixlen>"]`, `gateway4: <gateway>`, and disables DHCP for that
   interface, and writing it back atomically via the file operator with
   `elevated=True` when configured.  The function then dispatches `netplan apply`
   through the runner with `elevated=True`.
2. WHEN both the file write and the `netplan apply` succeed THE SYSTEM SHALL
   additionally dispatch `ip addr flush dev <name>`, `ip addr add <address>/<prefix>
   dev <name>`, and `ip route add default via <gateway> dev <name>` as a runtime-
   reconciliation pass (only when `netplan apply` returns non-zero on the runtime
   reconciliation step — `netplan apply` normally handles both surfaces).
3. WHEN the netplan YAML already contains an identical `ethernets:` entry for the
   interface THE SYSTEM SHALL skip execution and return a
   `UbuntuNetworkActionResult` with `status=SKIPPED`.
4. WHEN the file write succeeds but `netplan apply` fails THE SYSTEM SHALL return
   a `UbuntuNetworkActionResult` with `status=FAILED` and a `message` identifying
   the failing step.
5. WHEN any runtime-reconciliation `ip` command fails THE SYSTEM SHALL return a
   `UbuntuNetworkActionResult` with `status=FAILED` and a `message` identifying
   the failing step.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT write the file or execute any
   command and SHALL return a `UbuntuNetworkActionResult` with `status=DRY_RUN`
   and a unified diff of the intended YAML change in `diff`.

---

### Requirement 4: Enable an Interface

**User Story:** As a maintenance coordinator, I want to bring an interface up so
that traffic can flow.

#### Acceptance Criteria

1. WHEN the coordinator calls `enable_interface(name)` THE SYSTEM SHALL dispatch
   `ip link set <name> up` through the command runner with `elevated=True` when
   configured.
2. WHEN the interface is already in `state="up"` THE SYSTEM SHALL skip execution
   and return a `UbuntuNetworkActionResult` with `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuNetworkActionResult`
   with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `UbuntuNetworkActionResult`
   with `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `UbuntuNetworkActionResult` with `status=DRY_RUN`.

---

### Requirement 5: Disable an Interface

**User Story:** As a maintenance coordinator, I want to bring an interface down so
that no traffic flows.

#### Acceptance Criteria

1. WHEN the coordinator calls `disable_interface(name)` THE SYSTEM SHALL dispatch
   `ip link set <name> down` through the command runner with `elevated=True` when
   configured.
2. WHEN the interface is already in `state="down"` THE SYSTEM SHALL skip execution
   and return a `UbuntuNetworkActionResult` with `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuNetworkActionResult`
   with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `UbuntuNetworkActionResult`
   with `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `UbuntuNetworkActionResult` with `status=DRY_RUN`.

---

### Requirement 6: Add a Route

**User Story:** As a maintenance coordinator, I want to add a static route so that
traffic destined for a specific prefix flows through a chosen gateway.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_route(dest, gateway, interface)` THE SYSTEM
   SHALL dispatch `ip route add <dest> via <gateway> dev <interface>` (`dev`
   omitted when `interface` is `None`) through the command runner with
   `elevated=True` when configured.
2. WHEN an equivalent route already exists in `ip -j route show` THE SYSTEM SHALL
   skip execution and return a `UbuntuNetworkActionResult` with `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuNetworkActionResult`
   with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `UbuntuNetworkActionResult`
   with `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `UbuntuNetworkActionResult` with `status=DRY_RUN`.

---

### Requirement 7: Remove a Route

**User Story:** As a maintenance coordinator, I want to remove a static route so
that a previous indirection is revoked.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_route(dest)` THE SYSTEM SHALL dispatch
   `ip route del <dest>` through the command runner with `elevated=True` when
   configured.
2. WHEN no route for `dest` exists in `ip -j route show` THE SYSTEM SHALL skip
   execution and return a `UbuntuNetworkActionResult` with `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a `UbuntuNetworkActionResult`
   with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `UbuntuNetworkActionResult`
   with `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `UbuntuNetworkActionResult` with `status=DRY_RUN`.

---

### Requirement 8: Query All Routes

**User Story:** As a maintenance coordinator, I want to list every route so that
reporting workflows can enumerate routes without parsing raw `ip` output.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_routes()` THE SYSTEM SHALL dispatch
   `ip -j route show` through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the JSON output into a list
   of `UbuntuRouteInfo` records with `destination` (from `dst`, replacing the
   literal `"default"` with `"0.0.0.0/0"`), `gateway`, `interface` (from `dev`),
   and `metric` (from `metric` or `None`).
3. WHEN the command fails THE SYSTEM SHALL raise a `UbuntuNetworkQueryError`.

---

### Requirement 9: Set DNS Servers

**User Story:** As a maintenance coordinator, I want to set the list of DNS
servers so that name resolution uses the operator-supplied nameservers.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_dns_servers(servers)` THE SYSTEM SHALL dispatch
   `resolvectl dns <default_interface> <server1> <server2> ...` through the
   command runner with `elevated=True` when configured, where
   `<default_interface>` is the interface carrying the default route (resolved
   via `ip -j route show default`).
2. WHEN the current DNS-server list returned by `get_dns_servers()` already
   equals the supplied list (order preserved) THE SYSTEM SHALL skip execution
   and return a `UbuntuNetworkActionResult` with `status=SKIPPED`.
3. WHEN the command succeeds AND the manager is configured to persist DNS THE
   SYSTEM SHALL ALSO write the server list into the managed netplan YAML's
   `nameservers.addresses:` block for the default interface and dispatch
   `netplan apply` to make the configuration survive reboot.
4. WHEN both the runtime and persistent steps succeed THE SYSTEM SHALL return a
   `UbuntuNetworkActionResult` with `status=SUCCESS`.
5. WHEN one surface succeeds but the other fails THE SYSTEM SHALL return a
   `UbuntuNetworkActionResult` with `status=FAILED` and a `message` identifying
   the failing surface.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `UbuntuNetworkActionResult` with `status=DRY_RUN` and a unified diff
   of the intended YAML change in `diff`.

---

### Requirement 10: Query DNS Servers

**User Story:** As a maintenance coordinator, I want to retrieve the current DNS
server list, so that idempotency guards and reporting workflows can reason about
current state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_dns_servers()` THE SYSTEM SHALL dispatch
   `resolvectl dns` through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse the output into a list of
   IP-address strings in the order `resolvectl` returns them (combining per-
   interface and global lists, with duplicates removed).
3. WHEN the command fails THE SYSTEM SHALL raise a `UbuntuNetworkQueryError`.

---

### Requirement 11: Set Search Domains

**User Story:** As a maintenance coordinator, I want to set the DNS search
domains so that short-name lookups resolve under the operator-supplied domains.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_search_domains(domains)` THE SYSTEM SHALL
   dispatch `resolvectl domain <default_interface> <domain1> <domain2> ...`
   through the command runner with `elevated=True` when configured, and
   additionally update the managed netplan YAML's `nameservers.search:` block
   followed by `netplan apply`.
2. WHEN the current search-domain list already equals the supplied list (order
   preserved) THE SYSTEM SHALL skip execution and return a
   `UbuntuNetworkActionResult` with `status=SKIPPED`.
3. WHEN both surfaces succeed THE SYSTEM SHALL return a
   `UbuntuNetworkActionResult` with `status=SUCCESS`.
4. WHEN one surface fails THE SYSTEM SHALL return a `UbuntuNetworkActionResult`
   with `status=FAILED` and a `message` identifying the failing surface.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `UbuntuNetworkActionResult` with `status=DRY_RUN` and a unified diff.

---

### Requirement 12: Get Hostname

**User Story:** As a maintenance coordinator, I want to query the current system
hostname so that downstream logic can branch on it.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_hostname()` THE SYSTEM SHALL dispatch
   `hostnamectl --static` through the command runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL return the stripped stdout as the
   hostname string.
3. WHEN the command fails THE SYSTEM SHALL raise a `UbuntuNetworkQueryError`.

---

### Requirement 13: Set Hostname

**User Story:** As a maintenance coordinator, I want to set the system hostname
so that the machine reports the operator-supplied name on both the runtime and
persistent surfaces.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_hostname(name)` THE SYSTEM SHALL dispatch
   `hostnamectl set-hostname <name>` through the command runner with
   `elevated=True` when configured; this single `hostnamectl` call updates both
   the runtime hostname and the persistent `/etc/hostname` file.
2. WHEN the current static hostname already equals `<name>` THE SYSTEM SHALL skip
   execution and return a `UbuntuNetworkActionResult` with `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return a
   `UbuntuNetworkActionResult` with `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return a `UbuntuNetworkActionResult`
   with `status=FAILED` and the captured exit code and stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and SHALL
   return a `UbuntuNetworkActionResult` with `status=DRY_RUN`.

---

### Requirement 14: Elevated Privilege Execution

**User Story:** As a maintenance coordinator, I want all network write
operations to run with root privilege via `sudo`, so that mount / unmount / ACL
writes succeed on a standard system where the process does not run as root.

#### Acceptance Criteria

1. WHEN the system is configured with `elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the command runner and the file operator for every write
   (configure_interface, enable_interface, disable_interface, add_route,
   remove_route, set_dns_servers, set_search_domains, set_hostname).
2. WHEN performing read-only operations (get_interfaces, get_interface,
   get_routes, get_dns_servers, get_hostname) THE SYSTEM SHALL NOT pass
   `elevated=True`, regardless of configuration.
3. WHEN the system is running as root THE SYSTEM SHALL NOT pass `elevated=True`.
4. WHEN the elevation configuration is not set THE SYSTEM SHALL default to no
   elevation and allow the command runner or file operator to surface the
   resulting permission error as a `UbuntuNetworkActionResult` with
   `status=FAILED` for writes or as a `UbuntuNetworkQueryError` for reads.
