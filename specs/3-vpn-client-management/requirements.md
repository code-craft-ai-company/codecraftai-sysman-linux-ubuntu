# Requirements

## Introduction

The vpn-client-management feature provides the `codecraftai-sysman-linux-ubuntu`
package with the ability to install, configure, and lifecycle the **Tailscale**
client (`tailscaled`) and the **Twingate Connector** on Ubuntu hosts. This spec
implements the vpn-client-management contract defined at
`codecraftai-sysman-common/specs/contracts/vpn-client-management/contract.md` for
Ubuntu using the official Tailscale and Twingate apt repositories and
`systemctl` for service control.

A maintenance coordinator running in an external package drives all operations
by calling into this feature's public interfaces. The feature does not decide
*whether* to install a VPN client — it provides the capability to do so
reliably, idempotently, and with full auditability. All system commands are
executed through the `ICommandRunner` abstraction, with the `aspects` parameter
forwarded on every call. The companion features
[`1-command-execution`](../1-command-execution/requirements.md),
[`2-maintenance-reporting`](../2-maintenance-reporting/requirements.md),
[`3-package-management`](../3-package-management/requirements.md),
[`3-repository-management`](../3-repository-management/requirements.md), and
[`3-service-management`](../3-service-management/requirements.md) supply the
underlying runner, reporting, package, repository, and service primitives this
feature composes on top of.

---

## Requirements

### Requirement 1: Install Tailscale via the Official apt Repository

**User Story:** As a maintenance coordinator, I want to install Tailscale on an
Ubuntu host using the official apt repository, so that the `tailscale` package
is present and managed by the system package manager.

#### Acceptance Criteria

1. WHEN the coordinator calls `install_tailscale(version=None, channel="stable",
   aspects=…)` THE SYSTEM SHALL ensure the Tailscale apt repository signing key
   is present under `/usr/share/keyrings/tailscale-archive-keyring.gpg`, ensure
   the repository file is present under `/etc/apt/sources.list.d/tailscale.list`,
   run `apt-get update`, and then `apt-get install -y tailscale`, with every
   command dispatched via the runner.
2. WHEN `version` is a concrete string THE SYSTEM SHALL install
   `tailscale=<version>` so that re-runs against the same version are bit-stable.
3. WHEN `tailscale` is already installed at the requested version THE SYSTEM
   SHALL return `UbuntuVPNActionResult(status=SKIPPED, provider=TAILSCALE)`
   without invoking apt.
4. WHERE the runner is configured for elevation THE SYSTEM SHALL pass
   `elevated=True` for the keyring write, the repository file write, the
   `apt-get update`, and the `apt-get install` commands.
5. IF the apt commands fail THEN THE SYSTEM SHALL return
   `UbuntuVPNActionResult(status=FAILED, …)` populated with the exit code,
   stdout, and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and
   SHALL return `UbuntuVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 2: Install the Twingate Connector via apt

**User Story:** As a maintenance coordinator, I want to install the Twingate
Connector on an Ubuntu host using the official apt repository, so that the
`twingate-connector` package is present and managed by the system package
manager.

#### Acceptance Criteria

1. WHEN the coordinator calls `install_twingate_connector(version=None,
   aspects=…)` THE SYSTEM SHALL configure the Twingate apt repository, run
   `apt-get update`, and `apt-get install -y twingate-connector`.
2. WHEN `version` is a concrete string THE SYSTEM SHALL install
   `twingate-connector=<version>`.
3. WHEN `twingate-connector` is already installed at the requested version THE
   SYSTEM SHALL return `UbuntuVPNActionResult(status=SKIPPED, provider=TWINGATE)`.
4. WHERE the runner is configured for elevation THE SYSTEM SHALL pass
   `elevated=True` for the apt commands.
5. IF the apt commands fail THEN THE SYSTEM SHALL return
   `UbuntuVPNActionResult(status=FAILED, …)` populated with exit code,
   stdout, and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and
   SHALL return `UbuntuVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 3: Configure a Provider

**User Story:** As a maintenance coordinator, I want to write provider-specific
configuration files for the Twingate Connector (network slug, tenant token
path), so that the daemon has the inputs it needs before bring-up.

#### Acceptance Criteria

1. WHEN the coordinator calls `configure(provider=VPNProvider.TWINGATE,
   settings={"network": <slug>, "remote_network_id": <id>}, aspects=…)` THE
   SYSTEM SHALL render `/etc/twingate/connector.conf` via the runner using a
   here-document piped through `tee`, with `elevated=True` WHERE the runner is
   configured for elevation.
2. WHEN `provider=VPNProvider.TAILSCALE` THE SYSTEM SHALL return
   `UbuntuVPNActionResult(status=SKIPPED, provider=TAILSCALE,
   detail="tailscale has no on-disk configure step on ubuntu")` because the
   Tailscale client takes all configuration as CLI flags on `tailscale up`.
3. WHEN the rendered configuration matches what is already on disk THE SYSTEM
   SHALL return `UbuntuVPNActionResult(status=SKIPPED, …)` without writing.
4. IF the file write fails THEN THE SYSTEM SHALL return
   `UbuntuVPNActionResult(status=FAILED, …)` with the runner's exit code and
   stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT write any file and SHALL
   return `UbuntuVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 4: Bring the Tunnel Up

**User Story:** As a maintenance coordinator, I want to authenticate the host
to the tailnet (Tailscale) or network (Twingate) and bring the tunnel up, so
that the device is reachable on the overlay.

#### Acceptance Criteria

1. WHEN the coordinator calls `bring_up(provider=TAILSCALE, authkey=<key>,
   hostname=<name>, advertise_tags=<tags>, advertise_routes=<routes>,
   exit_node=<bool>, aspects=…)` THE SYSTEM SHALL invoke
   `tailscale up --authkey=<key> --hostname=<name>` plus any tag, route, and
   exit-node flags, with the runner.
2. WHEN the coordinator calls `bring_up(provider=TWINGATE, authkey=<token>, …)`
   THE SYSTEM SHALL invoke `systemctl enable --now twingate-connector` after
   writing the token via `configure`.
3. WHEN `status(provider=…)` already reports `running=True`, the same
   `hostname`, and the same `advertise_tags` THE SYSTEM SHALL return
   `UbuntuVPNActionResult(status=SKIPPED, …)` without invoking the runner.
4. WHERE the runner is configured for elevation THE SYSTEM SHALL pass
   `elevated=True` for `tailscale up`, `systemctl`, and any related write.
5. IF the command fails THEN THE SYSTEM SHALL return
   `UbuntuVPNActionResult(status=FAILED, …)` with exit code, stdout, stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and
   SHALL return `UbuntuVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 5: Bring the Tunnel Down

**User Story:** As a maintenance coordinator, I want to take the host off the
overlay without uninstalling, so that the daemon remains available for later
re-use.

#### Acceptance Criteria

1. WHEN the coordinator calls `bring_down(provider=TAILSCALE, aspects=…)` THE
   SYSTEM SHALL invoke `tailscale down` via the runner.
2. WHEN the coordinator calls `bring_down(provider=TWINGATE, aspects=…)` THE
   SYSTEM SHALL invoke `systemctl stop twingate-connector`.
3. WHEN `status(provider=…)` already reports `running=False` THE SYSTEM SHALL
   return `UbuntuVPNActionResult(status=SKIPPED, …)`.
4. WHERE the runner is configured for elevation THE SYSTEM SHALL pass
   `elevated=True`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and
   SHALL return `UbuntuVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 6: Report Client Status

**User Story:** As a maintenance coordinator, I want to read the current state
of the VPN client, so that subsequent steps can branch on `running`, the
self-address, and the peer table without invoking writes.

#### Acceptance Criteria

1. WHEN the coordinator calls `status(provider=TAILSCALE, aspects=…)` THE
   SYSTEM SHALL invoke `tailscale status --json` via the runner without
   elevation and parse the JSON into a `UbuntuVPNClientStatus`.
2. WHEN `tailscale status --json` reports `BackendState=="Running"` THE SYSTEM
   SHALL populate `running=True`, `self_address` from `Self.TailscaleIPs[0]`,
   `hostname` from `Self.HostName`, `tailnet` from `MagicDNSSuffix`, and
   `peer_addresses` from the `Peer` table (hostname → first overlay IP).
3. WHEN the coordinator calls `status(provider=TWINGATE, aspects=…)` THE
   SYSTEM SHALL invoke `twingate status` via the runner and parse the output
   into `UbuntuVPNClientStatus(self_address=None, peer_addresses={})`.
4. WHEN the daemon binary is not installed THE SYSTEM SHALL return
   `UbuntuVPNClientStatus(installed=False, running=False, …)` rather than raise.
5. IF the status command fails for any other reason THEN THE SYSTEM SHALL
   raise `UbuntuVPNClientQueryError` carrying the exit code and stderr.

---

### Requirement 7: Uninstall

**User Story:** As a maintenance coordinator, I want to uninstall a VPN client
and optionally purge its state, so that the host can be returned to a clean
baseline.

#### Acceptance Criteria

1. WHEN the coordinator calls `uninstall(provider=TAILSCALE, purge=False,
   aspects=…)` THE SYSTEM SHALL stop the service and `apt-get remove -y
   tailscale`.
2. WHEN `purge=True` THE SYSTEM SHALL additionally run
   `apt-get purge -y tailscale` and remove the apt source file and keyring.
3. WHEN the coordinator calls `uninstall(provider=TWINGATE, purge=…, aspects=…)`
   THE SYSTEM SHALL apply the equivalent steps for `twingate-connector`.
4. WHEN the package is already absent THE SYSTEM SHALL return
   `UbuntuVPNActionResult(status=SKIPPED, …)`.
5. WHERE the runner is configured for elevation THE SYSTEM SHALL pass
   `elevated=True`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any command and
   SHALL return `UbuntuVPNActionResult(status=DRY_RUN, …)`.

---

### Requirement 8: Aspects Forwarding

**User Story:** As a maintenance coordinator, I want every behaviour-bearing
operation to take an `aspects` parameter, so that timeouts, retries, and
persistence are controlled at the call site.

#### Acceptance Criteria

1. THE SYSTEM SHALL accept an `aspects: Aspects` parameter on every write and
   read verb.
2. THE SYSTEM SHALL forward the `aspects` argument to every `runner.run(…)`
   call verbatim.
3. THE SYSTEM SHALL NOT apply any `@retry` or behaviour-modifying decorator.
