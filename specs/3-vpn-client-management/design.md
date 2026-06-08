# Design

## Overview

The vpn-client-management feature is implemented as a single module,
`ccai.sysman.linux.ubuntu.vpnclientmanagement`, that exposes a
`UbuntuVPNClientManager` class subclassing `VPNClientManagementBase` from
`codecraftai-sysman-common`. The manager installs and lifecycles **Tailscale**
(`tailscaled`) via the official `pkgs.tailscale.com` apt repository, and the
**Twingate Connector** via the `packages.twingate.com` apt repository.
`systemctl` controls the daemons.

All commands route through `ICommandRunner` — package operations, repository
key/sources writes via `tee`, `systemctl` calls, and `tailscale` /
`twingate` CLI invocations. Configuration files are written through the runner
using here-documents piped through `tee`. No direct subprocess use, no direct
filesystem writes.

**See Also:**
- [vpn-client-management contract](../../../codecraftai-sysman-common/specs/contracts/vpn-client-management/contract.md)
- [vpn-client-management-base](../../../codecraftai-sysman-common/specs/vpn-client-management-base/design.md)
- [1-command-execution](../1-command-execution/design.md) — runner, aspects
- [2-maintenance-reporting](../2-maintenance-reporting/design.md) — reporting
- [3-package-management](../3-package-management/design.md) — apt primitives reused
- [3-repository-management](../3-repository-management/design.md) — apt sources
- [3-service-management](../3-service-management/design.md) — `systemctl`

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (timeout, retry, persistence)         │
│  - Injects runner + UbuntuVPNClientManagerConfig            │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuVPNClientManager                           │
│  ccai.sysman.linux.ubuntu.vpnclientmanagement           │
│  inherits VPNClientManagementBase                           │
│                                                             │
│  install_tailscale()  install_twingate_connector()          │
│  configure()          bring_up()                            │
│  bring_down()         status()                              │
│  uninstall()                                                │
└────────────────────────┬────────────────────────────────────┘
                         │
        ┌────────────────┼──────────────────┐
        ▼                ▼                  ▼
┌─────────────────┐ ┌────────────┐ ┌──────────────────┐
│ apt + tee for   │ │ systemctl  │ │ tailscale /      │
│ repo, keyring,  │ │ enable /   │ │ twingate CLIs    │
│ install, purge  │ │ start /    │ │ (up/down/status) │
│                 │ │ stop       │ │                  │
└────────┬────────┘ └─────┬──────┘ └────────┬─────────┘
         │                │                 │
         └────────────────┼─────────────────┘
                          ▼
                   ICommandRunner
                          │
                 ┌────────┴────────┐
                 ▼                 ▼
        LocalCommandRunner   SSHCommandRunner
                          │
                          ▼  aspects compose
            PersistenceAspect, RetryAspect, TimeoutAspect
```

---

## Components

### `UbuntuVPNClientManager`

**Location:** `source/packages/ccai/sysman/linux/ubuntu/vpnclientmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `install_tailscale` | `(*, version, channel, aspects) → UbuntuVPNActionResult` | Configure apt repo + install `tailscale` |
| `install_twingate_connector` | `(*, version, aspects) → UbuntuVPNActionResult` | Configure apt repo + install `twingate-connector` |
| `configure` | `(*, provider, settings, aspects) → UbuntuVPNActionResult` | Render `/etc/twingate/connector.conf`; SKIPPED for Tailscale |
| `bring_up` | `(*, provider, authkey, hostname, advertise_tags, advertise_routes, exit_node, aspects) → UbuntuVPNActionResult` | `tailscale up …` or `systemctl enable --now twingate-connector` |
| `bring_down` | `(*, provider, aspects) → UbuntuVPNActionResult` | `tailscale down` or `systemctl stop twingate-connector` |
| `status` | `(*, provider, aspects) → UbuntuVPNClientStatus` | `tailscale status --json` / `twingate status` |
| `uninstall` | `(*, provider, purge, aspects) → UbuntuVPNActionResult` | `apt-get remove` / `apt-get purge` + repo cleanup |

### `UbuntuVPNClientManagerConfig`

Extends `VPNClientManagerConfig`:

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `tailscale_channel` | `str` | `"stable"` | apt channel suffix (`stable`, `unstable`) |
| `keyring_path` | `str` | `"/usr/share/keyrings/tailscale-archive-keyring.gpg"` | Where the Tailscale signing key is written |
| `sources_path` | `str` | `"/etc/apt/sources.list.d/tailscale.list"` | apt sources file for Tailscale |
| `twingate_sources_path` | `str` | `"/etc/apt/sources.list.d/twingate.list"` | apt sources file for Twingate |
| `twingate_config_path` | `str` | `"/etc/twingate/connector.conf"` | Twingate Connector config |

---

## Data Models

OS-prefixed aliases of the contract types. The shared definitions live in
`ccai.sysman.common.vpnclientmanagementbase`; this module simply re-exports
them under Ubuntu-prefixed names.

```python
UbuntuVPNActionStatus  = VPNActionStatus
UbuntuVPNActionResult  = VPNActionResult
UbuntuVPNClientStatus  = VPNClientStatus
UbuntuVPNClientQueryError = VPNClientQueryError
```

---

## Provider-Backend Mapping

| Provider | Install command | Daemon | Bring-up command | Bring-down command | Status command |
|---|---|---|---|---|---|
| Tailscale | `apt-get install -y tailscale[=<version>]` | `tailscaled` (systemd unit `tailscaled.service`) | `tailscale up --authkey=… --hostname=… [--advertise-tags=…] [--advertise-routes=…] [--exit-node]` | `tailscale down` | `tailscale status --json` |
| Twingate | `apt-get install -y twingate-connector[=<version>]` | `twingate-connector.service` (systemd) | `systemctl enable --now twingate-connector` (after `configure`) | `systemctl stop twingate-connector` | `twingate status` |

---

## Command Mappings

### Repository Setup (Tailscale)

| Step | Command |
|---|---|
| Fetch signing key | `curl -fsSL https://pkgs.tailscale.com/<channel>/ubuntu/<codename>.noarmor.gpg \| tee <keyring_path>` |
| Write sources | `tee <sources_path>` with body `deb [signed-by=<keyring_path>] https://pkgs.tailscale.com/<channel>/ubuntu <codename> main` |
| Refresh apt | `apt-get update` |
| Install | `apt-get install -y tailscale[=<version>]` |

All run with `elevated=True` when the runner is configured for elevation.

### Repository Setup (Twingate)

| Step | Command |
|---|---|
| Add apt source | `tee <twingate_sources_path>` with the Twingate repo line |
| Refresh apt | `apt-get update` |
| Install | `apt-get install -y twingate-connector[=<version>]` |

### Configuration

| Provider | Path | Mechanism |
|---|---|---|
| Tailscale | (none) | Returned `SKIPPED` |
| Twingate | `<twingate_config_path>` | Render via runner heredoc piped through `tee` |

### Read Commands (no elevation)

| Operation | Command |
|---|---|
| Tailscale status | `tailscale status --json` |
| Twingate status | `twingate status` |
| Installed check | `dpkg-query -W -f='${Status} ${Version}\n' <pkg>` |

---

## Idempotency Guards

| Verb | Guard | Mechanism |
|---|---|---|
| `install_tailscale` | Package already present at requested version | `dpkg-query -W -f='${Version}' tailscale` |
| `install_twingate_connector` | Same | `dpkg-query -W -f='${Version}' twingate-connector` |
| `configure` (Twingate) | Rendered content matches on-disk content | `cat <path>` via runner, compare in Python |
| `bring_up` | `status()` already shows `running=True` and matching hostname/tags | Pre-call status query |
| `bring_down` | `status()` already shows `running=False` | Pre-call status query |
| `uninstall` | Package already absent | `dpkg-query` returns non-zero |

---

## `tailscale status --json` Parsing

The JSON payload exposes:

- `BackendState` — `"Running"`, `"Stopped"`, `"NeedsLogin"` (and others). Only
  `"Running"` maps to `running=True`.
- `Self.TailscaleIPs` — list of overlay IPs; first entry populates
  `self_address`.
- `Self.HostName` — populates `hostname`.
- `MagicDNSSuffix` — populates `tailnet`.
- `Peer` — dict keyed by node key; for each entry, take `HostName` and the
  first `TailscaleIPs[0]` for `peer_addresses`.
- `Version` — populates `version`.

The parser is pure Python on the decoded JSON.

---

## Elevation Policy

- Writes (install, configure, bring_up, bring_down, uninstall) pass
  `elevated=True` when `config.elevated=True`.
- Reads (status, installed-check) never elevate.
- The `_should_elevate` helper from the base class is the single source of
  truth.

---

## Module Layout

```
source/packages/ccai/sysman/linux/ubuntu/
├── __init__.py
└── vpnclientmanagement.py     ← UbuntuVPNClientManager, config, OS-prefixed aliases

source/testroots/testbase/
└── ccai/tests/sysman/linux/ubuntu/
    └── test_vpnclientmanagement.py
```

---

## See Also

- [vpn-client-management contract](../../../codecraftai-sysman-common/specs/contracts/vpn-client-management/contract.md)
- [vpn-client-management-base](../../../codecraftai-sysman-common/specs/vpn-client-management-base/design.md)
- [3-firewall-management](../3-firewall-management/design.md) — sibling sysman feature pattern
- [3-service-management](../3-service-management/design.md) — `systemctl` reused for Twingate
- [3-package-management](../3-package-management/design.md) — apt primitives reused

---

## Testing Strategy

All tests use a `MockCommandRunner` keyed on the command-args prefix. No test
requires a live `apt`, `tailscale`, or `twingate` installation.

**Example-based tests:**

- One command-construction test per write verb per provider.
- One idempotency test per write verb (install, configure, bring_up,
  bring_down, uninstall) proving `SKIPPED` is returned when the desired state
  is already in effect.
- One dry-run test per write verb proving the runner is not invoked.
- One parser test per provider — `tailscale status --json` mock output
  exercising `Running`, `Stopped`, and `NeedsLogin` backend states; `twingate
  status` text-output exercising connected and disconnected.
- One elevation test proving writes pass `elevated=True` and reads do not,
  when configured.
- One Twingate-configure-skip test proving the rendered content equals on-disk
  content path.

**Property tests (hypothesis):**

- `install_tailscale` idempotency — for any generated `(version, channel)`,
  invoking twice returns `SUCCESS` then `SKIPPED`.
- `uninstall` idempotency — for any provider, invoking twice returns
  `SUCCESS` then `SKIPPED`.
- `bring_up` idempotency — for any generated `(authkey, hostname, tags,
  routes, exit_node)`, when status already matches the resulting state, the
  second call returns `SKIPPED`.
- Dry-run safety — across hypothesis-generated inputs, with `dry_run=True`
  the mock runner is never invoked and every write returns
  `UbuntuVPNActionResult(status=DRY_RUN)`.
