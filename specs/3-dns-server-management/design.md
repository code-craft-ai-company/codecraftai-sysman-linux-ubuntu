# Design

## Overview

The Ubuntu dns-server-management feature is implemented as a single
module,
`automojo.sysman.linux.ubuntu.dnsservermanagement`, exposing an
`UbuntuDnsServerManager` class that wraps BIND 9 (`named`) through
the `ICommandRunner` abstraction.  It manages the content of
`/etc/bind/named.conf.local` and per-zone data files under
`/etc/bind/zones/db.<name>`.

The module follows the standard manager pattern shared with every
other Layer 3 feature in this package: it consumes the
`BaseFeatureManager` mixin from
`automojo.sysman.linux.ubuntu.featurebase`, accepts an injected
runner plus an `UbuntuDnsManagerConfig`, and dispatches every shell
command through the runner.

The cross-OS contract for this feature lives in
`automojo-sysman-common/specs/contracts/dns-server-management/contract.md`.
The RHEL canonical implementation lives in
`automojo-sysman-linux-redhat/specs/3-dns-server-management/`.

**See Also:**
- `3-network-configuration` — owns DNS *client* (stub-resolver)
  configuration; orthogonal to this feature.
- `3-service-management` — `bind9.service` lifecycle (a full
  daemon restart is delegated; the manager only dispatches
  `rndc reconfig` / `rndc reload`).
- `3-firewall-management` — opening UDP/TCP 53 through `ufw` /
  nftables; out of scope here.
- `3-certificate-issuance` — TLS material for DoT / DoH listeners
  is out of scope for this pilot.

---

## Architecture

```
+-------------------------------------------------------------+
|            Maintenance Coordinator                          |
+-----------------------+-------------------------------------+
                        |
                        v
+-------------------------------------------------------------+
|       UbuntuDnsServerManager                                |
|                                                             |
|  Write verbs (idempotent + dry-run aware):                  |
|    add_zone(name, spec)                                     |
|    remove_zone(name)                                        |
|    add_record(zone, record)                                 |
|    remove_record(zone, record)                              |
|    reload_zone(name)                                        |
|                                                             |
|  Read verbs:                                                |
|    list_zones()                                             |
|    list_records(zone)                                       |
|    get_zone_status(name)                                    |
+--------------------------+----------------------------------+
                           |
                           v
                +----------+-----------+
                |     ICommandRunner   |
                +----------+-----------+
                           |
                ___________|___________
               |                       |
               v                       v
   +-------------------+    +---------------------+
   | LocalCommandRunner|    | SSHCommandRunner    |
   +---------+---------+    +-----------+---------+
             |                          |
             |     aspects compose here |
             +------------+-------------+
                          |
                          v
        +-----------------------------------+
        | PersistenceAspect, RetryAspect,   |
        | TimeoutAspect (transparent)       |
        +-----------------------------------+
```

---

## Cross-OS DNS-Server-Model Concept Mapping

The cross-OS contract uses an abstract
`(zone, record, serial)` triple plus a backend-agnostic
`zone_type` enum.  Each OS maps these abstractions onto its
native tooling:

| Abstract concept   | Ubuntu (BIND 9)                              | macOS (BIND ships with macOS)              | Windows (DnsServer module)                  | RHEL (BIND, canonical)            |
|--------------------|----------------------------------------------|--------------------------------------------|---------------------------------------------|-----------------------------------|
| Zone declaration   | `zone "<name>" {}` block in                  | `zone "<name>" {}` block in                | `Add-DnsServerPrimaryZone -Name '<zone>'`   | `zone "<name>" {}` block in       |
|                    | `/etc/bind/named.conf.local`                 | `/etc/named.conf` (single-file convention) | `-ZoneFile '<zone>.dns'`                    | `/etc/named.conf` or              |
|                    | (sentinel-bracketed)                         | (sentinel-bracketed)                       |                                             | `/etc/named/zones.d/`             |
| Zone type          | `master` / `slave` / `forward` / `stub`      | same                                       | `Primary` / `Secondary` / `Stub`            | same                              |
| Zone-data file     | `/etc/bind/zones/db.<name>`                  | `/var/named/db.<name>`                     | `%SystemRoot%\System32\dns\<name>.dns`      | `/var/named/db.<name>`            |
| Add record         | edit zone file + bump SOA + `rndc reload`    | edit zone file + bump SOA + `rndc reload`  | `Add-DnsServerResourceRecordA` /            | edit zone file + bump SOA +       |
|                    |                                              |                                            | `-AAAA` / `-CName` / `-MX` / `-TXT` ...     | `rndc reload`                     |
| Remove record      | strip line + bump SOA + `rndc reload`        | strip line + bump SOA + `rndc reload`      | `Remove-DnsServerResourceRecord`            | strip line + bump SOA +           |
|                    |                                              |                                            |                                             | `rndc reload`                     |
| List zones         | parse `named.conf.local` +                   | parse `named.conf` +                       | `Get-DnsServerZone`                         | parse `named.conf` +              |
|                    | `named-checkconf -p`                         | `named-checkconf -p`                       |                                             | `named-checkconf -p`              |
| List records       | parse zone-data file                         | parse zone-data file                       | `Get-DnsServerResourceRecord -ZoneName ...` | parse zone-data file              |
| Reload zone        | `rndc reload <zone>`                         | `rndc reload <zone>`                       | `Sync-DnsServerZone -Name <zone>`           | `rndc reload <zone>`              |
| Zone status        | `rndc zonestatus <zone>`                     | `rndc zonestatus <zone>`                   | `Get-DnsServerZone -Name <zone>`            | `rndc zonestatus <zone>`          |
| Service unit       | `bind9.service`                              | `org.isc.named` launchd label              | DNS Server role (no systemd unit)           | `named.service`                   |

The Ubuntu, macOS, and RHEL implementations share the same
underlying daemon (`named`) and therefore the same idempotency
rules, SOA-serial bumping, and BIND-syntax rendering.  The
Windows implementation diverges entirely — it dispatches
PowerShell `DnsServer` cmdlets and parses their JSON output.

---

## Configuration

```python
@dataclass
class UbuntuDnsManagerConfig(FeatureConfig):
    named_conf_local: str = "/etc/bind/named.conf.local"
    zone_dir:         str = "/etc/bind/zones"
    service_name:     str = "bind9"
```

`dry_run` and `elevated` are inherited from `FeatureConfig`.

The `service_name` field is informational — this manager does
NOT restart the BIND daemon directly; daemon restarts are
delegated to `3-service-management`.  Operators on hosts that
have replaced the stock `bind9` unit with a custom unit can
override the field; the manager records it on the result for
audit purposes only.

---

## Data Models

```python
class UbuntuDnsActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class UbuntuDnsActionResult(FeatureActionResult):
    zone:        str | None = None
    record_name: str | None = None
    message:     str | None = None


@dataclass
class UbuntuDnsZoneSpec:
    name:              str
    zone_type:         str            = "primary"
    zone_file:         str | None     = None
    allow_update:      bool           = False
    replication_scope: str | None     = None  # AD-only; ignored


@dataclass
class UbuntuDnsZoneInfo:
    name:         str
    zone_type:    str
    zone_file:    str | None
    record_count: int | None
    is_active:    bool


@dataclass
class UbuntuDnsRecordSpec:
    name:        str
    record_type: str          # "A" | "AAAA" | "CNAME" | "MX" | ...
    value:       str
    ttl:         int | None   = None
    priority:    int | None   = None  # MX / SRV


@dataclass
class UbuntuDnsRecordInfo:
    name:        str
    record_type: str
    value:       str
    ttl:         int | None   = None
    priority:    int | None   = None
    serial:      int | None   = None


@dataclass
class UbuntuDnsZoneStatus:
    name:              str
    is_loaded:         bool
    serial:            int | None = None
    last_loaded:       str | None = None
    transferred_from:  str | None = None


class UbuntuDnsQueryError(Exception):
    """Read-side failure that is not 'absent'."""
```

`UbuntuDnsActionStatus = ActionStatus` aliases the shared
enum so that callers can write `UbuntuDnsActionStatus.SUCCESS`
while the actual values flow through `BaseFeatureManager`'s
`_dispatch` helper.

---

## Sentinel-Bracketed Block Convention

Every managed `zone "<name>" {}` declaration is wrapped between
sentinel comment lines so the parser can locate it without
disturbing operator-authored declarations:

```
# >>> automojo-zone:example.com >>>
zone "example.com" {
    type master;
    file "/etc/bind/zones/db.example.com";
};
# <<< automojo-zone:example.com <<<
```

The sentinel pair is load-bearing: `list_zones`, `add_zone`,
and `remove_zone` all consume the markers as a delimiter.
Operators who hand-edit the markers will break the manager's
idempotency probes; the recommended workflow is to leave the
markers untouched and edit either the zone-data file or the
spec passed to `add_zone`.

---

## Zone-Data File Rendering

The initial zone-data file body emitted by `add_zone` is:

```
; Generated by automojo UbuntuDnsServerManager
$TTL    3600
@       IN      SOA     ns1.<name>. hostmaster.<name>. (
                <YYYYMMDD00>        ; Serial
                3600                ; Refresh
                600                 ; Retry
                1209600             ; Expire
                86400 )             ; Minimum TTL
;
@       IN      NS      ns1.<name>.
```

The `<YYYYMMDD00>` token is today's date plus a two-digit
sequence (`00` on the first write of the day).  Every
subsequent `add_record` / `remove_record` rewrites the serial
to today's `YYYYMMDDNN` value, incrementing `NN` when the
existing serial is already today's date so that BIND's slave
servers always see a strictly-increasing serial.

Operator-authored zone-data files (for zones declared outside
the sentinel-block convention) are NOT rewritten by this
manager — `add_record` / `remove_record` parse the existing
content but only mutate the lines they recognise.  Records the
parser cannot identify are preserved verbatim.

---

## SOA Serial Bumping

The serial-bump algorithm is implemented in `_bump_soa_serial`
and follows three rules:

1. Find the first line whose text contains the case-sensitive
   token `Serial` or `serial`.
2. Replace the first integer token on that line.  When the
   existing token is already `>=` today's `YYYYMMDD00`, the
   new value is `existing + 1`; otherwise the new value is
   `today's YYYYMMDD00`.
3. Stop after the first replacement so that subsequent
   `Serial` / `serial` mentions inside comment blocks are
   not mutated.

This algorithm preserves operator-authored serials that exceed
today's date (e.g., a forward-dated emergency change) while
still guaranteeing strict monotonic increase on every write.

---

## Validate-Before-Apply

The contract requires every write verb to validate the rendered
configuration before dispatching the reload / apply command.
Ubuntu's BIND ships two validators:

- `named-checkconf` — validates the `named.conf` (and every
  file `named.conf` includes) for syntactic correctness and
  cross-block consistency.
- `named-checkzone <zone> <file>` — validates a single
  zone-data file's records against RFC 1034 / 1035 rules.

The `_validate_configuration` helper invokes both validators
in order: `named-checkconf` first, then (when both arguments
are supplied) `named-checkzone`.  A non-zero exit from either
validator flips the verb's status to `FAILED`, the reload
command is suppressed, and the file mutations remain on disk
for forensic inspection.

The contract notes that "validation failure MUST return
FAILED without applying" — Ubuntu honours this strictly.
Operators are expected to fix the spec and re-invoke the verb.

---

## Key Method Designs

### `add_zone(name, spec)`

1. Resolve the zone-data file path (`spec.zone_file` overrides
   the `<zone_dir>/db.<name>` default).
2. When `config.dry_run` → `DRY_RUN`.
3. Read `named.conf.local` via `cat`.  Probe the zone-data
   file via `test -e`.
4. When the sentinel block is present AND the zone file
   exists → `SKIPPED`.
5. Render the zone block and zone-data file body.
6. Dispatch `tee /etc/bind/named.conf.local`.  On non-zero
   exit → return `FAILED`.
7. Dispatch `tee <zone_file>`.  On non-zero exit → return
   `FAILED`.
8. Validate via `named-checkconf` then
   `named-checkzone <name> <zone_file>`.  On failure → return
   `FAILED`.
9. Dispatch `rndc reconfig`.  On non-zero exit → return
   `FAILED`; otherwise return `SUCCESS`.

### `remove_zone(name)`

1. When `config.dry_run` → `DRY_RUN`.
2. Read `named.conf.local`; probe the zone-data file.
3. When neither is present → `SKIPPED`.
4. When the sentinel block is present, dispatch `tee` with
   the stripped content.
5. When the zone-data file is present, dispatch `rm -f`.
6. Validate via `named-checkconf`.
7. Dispatch `rndc reconfig`.

### `add_record(zone, record)`

1. Validate the spec (`record_type` must be in the supported
   set).
2. When `config.dry_run` → `DRY_RUN`.
3. `cat` the zone-data file.  When absent → return `FAILED`.
4. Parse the existing records.  When an identical record is
   present → `SKIPPED`.
5. Append the rendered line; bump the SOA serial.
6. Dispatch `tee <zone_file>`.  Validate via
   `named-checkzone`.  Dispatch `rndc reload <zone>`.

### `remove_record(zone, record)`

1. When `config.dry_run` → `DRY_RUN`.
2. `cat` the zone-data file.  When absent → `SKIPPED`.
3. When no matching line is present → `SKIPPED`.
4. Drop matching lines; bump the SOA serial.
5. Dispatch `tee <zone_file>`.  Validate.  Dispatch
   `rndc reload <zone>`.

### `reload_zone(name)`

1. When `config.dry_run` → `DRY_RUN`.
2. Dispatch `rndc reload <name>` unconditionally; never
   skips.

### `list_zones()`

1. `cat` `named.conf.local`; parse every sentinel-bracketed
   managed block into an `UbuntuDnsZoneInfo` row with
   `is_active=True` and (when the zone-data file exists)
   `record_count` populated.
2. Dispatch `named-checkconf -p`; for every zone declaration
   the output reports that the manager does NOT already own
   (no sentinel block in `named.conf.local`), append an
   `UbuntuDnsZoneInfo` row with `is_active=False`.
3. `named-checkconf -p` exit codes other than 0 with a
   non-empty stderr raise `UbuntuDnsQueryError`.

### `list_records(zone)`

1. `cat` `<zone_dir>/db.<zone>`.
2. Parse every record line.  The parser is permissive: tokens
   are split on whitespace, the optional TTL token is
   detected as a leading integer, and the `IN` class token is
   required.
3. The parsed SOA record's `serial` field carries the
   integer harvested from the SOA continuation block.

### `get_zone_status(name)`

1. Dispatch `rndc zonestatus <name>`.
2. Parse the indented key-colon-value output for `name`,
   `serial`, `loaded`, and `masters`.
3. When the output reports a `name:` line → `is_loaded=True`.

---

## Dry-Run Behaviour

Dry-run disables every mutating dispatch (`tee`, `rm`,
`named-checkconf`, `named-checkzone`, `rndc reconfig`,
`rndc reload`).  Reads (`cat`, `test -e`,
`named-checkconf -p`, `rndc zonestatus`) run unconditionally.
Dry-run results report the would-have-been argv on the
result's `command` field.

---

## Idempotency

| Verb              | Idempotency rule                                         |
|-------------------|----------------------------------------------------------|
| `add_zone`        | SKIP when sentinel block AND zone-data file are present. |
| `remove_zone`     | SKIP when neither sentinel block nor zone file exists.   |
| `add_record`      | SKIP when an identical (name, type, value) is present.   |
| `remove_record`   | SKIP when no matching record is on disk.                 |
| `reload_zone`     | No idempotency guard (always dispatches).                |
| `list_zones`      | n/a (read).                                              |
| `list_records`    | n/a (read).                                              |
| `get_zone_status` | n/a (read).                                              |

---

## Elevation

| Situation                                     | Behaviour                          |
|-----------------------------------------------|------------------------------------|
| `elevated=True`, write verb (incl. validators)| Pass `elevated=True` to runner.    |
| `elevated=True`, read verb                    | Do NOT elevate.                    |
| `elevated=False`                              | Never elevate.                     |
| `rndc zonestatus <zone>`                      | Read-class verb; never elevates.   |

The default `elevated=True` is appropriate for production:
every file the manager touches lives under `/etc/bind`, which
root owns by default on Ubuntu.

---

## Service Reload

`add_zone` / `remove_zone` dispatch `rndc reconfig`; this is
strictly cheaper than a full daemon restart and tells `named`
to re-read its declaration set.  `add_record` / `remove_record`
dispatch `rndc reload <zone>`, which forces `named` to
re-read just the affected zone-data file.

Daemon restarts are NOT initiated by this manager; they are
delegated to `3-service-management`.  A `bind9.service`
restart while the manager is running could race with the
file mutations the manager is staging — operators should
restart the unit through the service-management surface
between mutations rather than calling out to it from inside
this module.

---

## Correctness Properties

1. **Sentinel discipline.** Managed zone declarations are
   always wrapped in sentinel comment lines.  Removal
   strips the entire block; add upserts in place.
2. **Validate-before-reload.** `rndc reconfig` /
   `rndc reload` never run unless `named-checkconf`
   (and, where applicable, `named-checkzone`) exit zero.
3. **Strict-monotonic SOA serial.** Every record-write bumps
   the serial by either rolling forward to today's date or
   incrementing the existing value.
4. **Reads never elevate.** `cat`, `test -e`,
   `named-checkconf -p`, `rndc zonestatus`.
5. **Dry-run honours.** Every mutating dispatch is gated on
   `config.dry_run is False`.

---

## Out of Scope (Documented Limitations)

The following native BIND features are NOT implemented in
this pilot.  They are recorded as follow-ups in `tasks.md`:

- **DNSSEC zone signing.** KSK / ZSK rollover, `dnssec-policy`
  declarations, NSEC3 parameter management.
- **TSIG / transfer-key management.** `key {}` declarations
  and `allow-transfer { key ...; };` rendering.
- **RPZ (response-policy zones).** RPZ rules and the
  `response-policy` block.
- **Views / split-horizon.** Multi-view `view "<name>" {}`
  declarations.
- **Dynamic-update ACL management.** Beyond the
  `allow-update { localhost; };` boolean toggle exposed by
  `UbuntuDnsZoneSpec.allow_update`.
- **DoT / DoH listener configuration.** The `tls` /
  `https` directives in BIND 9.18+.
- **DNS over QUIC.** Out of scope.
- **Forward / stub zone wiring beyond declaration.**  The
  spec accepts `zone_type="forward"` / `"stub"` but does not
  render the `forwarders {}` / `masters {}` payload — that is
  a follow-up.

---

## Testing Strategy

- `_MockCommandRunner` records every call and returns
  pre-seeded `RunResult` values keyed by argv tuple.
- One command-construction test per write verb covers the
  primary argv shape.
- One idempotency test per write verb proves `SKIPPED` when
  the on-disk state already matches the requested state.
- One dry-run test per write verb proves the runner is not
  invoked beyond the read-side probes.
- One `list_zones` parser test covers a multi-zone
  `named.conf.local` plus the `named-checkconf -p` fold-in.
- One `list_records` parser test covers A, AAAA, CNAME, MX,
  TXT, SRV records.
- One `get_zone_status` test covers both the loaded and
  unloaded paths.
- One validate-before-apply test proves the reload command
  is not dispatched when `named-checkconf` fails.
- One serial-bump test proves the SOA serial increases on
  every record-write.
- One elevation test proves writes elevate and reads do not.

---

## Spec-vs-Contract Drift

| Drift point                                               | Resolution                                                                                   |
|-----------------------------------------------------------|----------------------------------------------------------------------------------------------|
| Contract uses a `DnsServerBackend` enum (BIND / Unbound / DnsMasq). | Ubuntu pilot targets BIND only.  Unbound and dnsmasq backends are RHEL-canonical; Ubuntu ships them but this pilot does not consume them.  Listed as a follow-up. |
| Contract names `DnsZoneKind` (PRIMARY / SECONDARY / FORWARD / STUB). | The pilot uses a `zone_type: str` field on the spec because the BIND renderer takes a literal `master` / `slave` / `forward` / `stub` token.  Mapping is in `_map_zone_type`. |
| Contract specifies `DnsRecord` (no priority field).        | The pilot adds `priority: int | None` to `UbuntuDnsRecordSpec` so MX / SRV records do not have to embed the priority into `value`.  The contract's `value` model is preserved as a fall-through. |
| Contract specifies `DnsServerStatus` (running / version / zone_count). | The pilot does NOT implement `get_server_status` for this wave; daemon-level status is delegated to `3-service-management`.  Listed as a follow-up. |
| Contract specifies `DnsBackendMismatchError`.              | Ubuntu pilot is single-backend; the error is not raised.  Reserved for cross-OS parity if Unbound / dnsmasq dispatch is added later. |

The drift is intentional and documented; future work will
reconcile the pilot models with the contract once the
cross-OS implementations have stabilised.

---

## Seams With Other Features

| Seam                                | Owner                                | This Feature        |
|-------------------------------------|--------------------------------------|---------------------|
| Stub-resolver / `/etc/resolv.conf`  | `3-network-configuration`            | Out of scope here.  |
| `bind9.service` lifecycle           | `3-service-management`               | Reload only via `rndc`; restarts delegated. |
| UDP / TCP 53 firewall opening       | `3-firewall-management`              | Out of scope here.  |
| TLS material for DoT / DoH          | `3-certificate-issuance` / trust-mgmt| Out of scope here.  |
| `bind9` package install             | `3-package-management`               | Out of scope here.  |
