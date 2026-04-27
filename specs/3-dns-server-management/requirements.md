# Requirements

## Introduction

The dns-server-management feature provides the
`automojo-sysman-linux-ubuntu` package with the ability to declare,
audit, and reload BIND-9 zones on Ubuntu hosts.  It is the cross-OS
counterpart to the RHEL-canonical `3-dns-server-management` feature
and the macOS / Windows variants in their respective sysman
packages.

Ubuntu ships BIND through the `bind9` package (the same upstream
`named` daemon RHEL ships).  The systemd unit name on Ubuntu is
`bind9.service`, in contrast to RHEL where the unit is named
`named.service`.  The configuration root is `/etc/bind/`; managed
zone declarations live in `/etc/bind/named.conf.local` and managed
zone-data files live in `/etc/bind/zones/db.<name>` by convention.

All system commands (`rndc reconfig`, `rndc reload <zone>`,
`rndc zonestatus <zone>`, `named-checkconf`, `named-checkzone`,
`tee`, `rm`, `cat`, `test -e`) are dispatched through the
`automojo-interfaces` `ICommandRunner` abstraction.  All mutating
operations honour dry-run mode and config-driven elevation.

This feature complements — but does not duplicate — the
`3-network-configuration` DNS-client surface, which owns
`/etc/resolv.conf` and the systemd-resolved configuration.  The
DNS *server* surface (zone-data files and the
`zone "<name>" {}` declarations BIND consumes) is owned here.

The reference implementation for shape, idempotency, dry-run, and
result mapping lives in
`automojo-sysman-linux-redhat/specs/3-dns-server-management/`.

---

## Requirements

### Requirement 1: Add a Zone

**User Story:** As a maintenance coordinator, I want to declare a
new authoritative DNS zone on the host so that downstream resolvers
can begin querying it.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_zone(name, spec)` THE SYSTEM
   SHALL render the canonical
   `zone "<name>" { type <type>; file "<zone_file>"; };` declaration
   and append it (between sentinel comment lines) to
   `/etc/bind/named.conf.local` via `tee`.
2. WHEN `spec.zone_type` is `"primary"` THE SYSTEM SHALL emit
   `type master;` (BIND's legacy synonym for primary), so that the
   declaration loads on every supported BIND version.
3. WHEN `spec.zone_type` is `"secondary"` THE SYSTEM SHALL emit
   `type slave;`.
4. WHEN no override is supplied THE SYSTEM SHALL render the zone
   data file at `/etc/bind/zones/db.<name>` containing a `$TTL`
   directive, an SOA record (`ns1.<name>.` /
   `hostmaster.<name>.`), and a single NS record.
5. WHEN the on-disk state already declares the zone (sentinel
   block present) AND the zone-data file already exists THE
   SYSTEM SHALL return `SKIPPED`.
6. WHEN both writes succeed THE SYSTEM SHALL invoke
   `named-checkconf` and (when a zone-data file path is known)
   `named-checkzone <name> <file>`; validation failure SHALL
   return `FAILED` and SHALL NOT dispatch the reload command.
7. WHEN validation succeeds THE SYSTEM SHALL invoke
   `rndc reconfig` so that the new zone is loaded immediately.

---

### Requirement 2: Remove a Zone

**User Story:** As a maintenance coordinator, I want to remove a
zone declaration so that BIND stops answering for it.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_zone(name)` THE SYSTEM SHALL
   strip the sentinel-bracketed managed block from
   `/etc/bind/named.conf.local` via `tee`.
2. WHEN the zone-data file is present THE SYSTEM SHALL `rm -f`
   the file.
3. WHEN neither the sentinel block nor the zone-data file is
   present THE SYSTEM SHALL return `SKIPPED` without dispatching
   any write.
4. WHEN the writes succeed THE SYSTEM SHALL invoke
   `named-checkconf` followed by `rndc reconfig`; validation
   failure SHALL surface as `FAILED` and suppress the reload.

---

### Requirement 3: List Zones

**User Story:** As a maintenance coordinator, I want to enumerate
the zones BIND knows about so that I can audit drift between the
declared posture and the running daemon.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_zones()` THE SYSTEM SHALL
   read `/etc/bind/named.conf.local` via `cat` and parse every
   sentinel-bracketed managed block into an
   `UbuntuDnsZoneInfo` row whose `is_active` is `True`.
2. WHEN the parser finds a managed block THE SYSTEM SHALL `cat`
   the block's zone-data file (when declared) and populate
   `record_count` from the parsed records.
3. WHEN `named-checkconf -p` returns extra zone declarations the
   manager does not own THE SYSTEM SHALL include them in the
   result with `is_active=False` so coordinators can audit
   externally-declared zones.
4. WHEN `named-checkconf -p` exits non-zero with a non-empty
   stderr THE SYSTEM SHALL raise `UbuntuDnsQueryError`.
5. THE SYSTEM SHALL NOT pass `elevated=True` for any read
   dispatch.

---

### Requirement 4: Add a Resource Record

**User Story:** As a maintenance coordinator, I want to add a
resource record to a zone so that clients can resolve the new
name.

#### Acceptance Criteria

1. WHEN the coordinator calls `add_record(zone, record)` THE
   SYSTEM SHALL read the zone file via `cat`, append the rendered
   record line, bump the SOA serial to today's `YYYYMMDDNN`
   value, write the new content via `tee`, and dispatch
   `rndc reload <zone>`.
2. WHEN an identical record (same `name`, `record_type`, and
   `value`) is already present THE SYSTEM SHALL return `SKIPPED`
   without dispatching any write.
3. WHEN the existing SOA serial already targets today's date
   THE SYSTEM SHALL increment the serial by 1 instead of
   overwriting it, preserving BIND's monotonic-serial
   invariant.
4. WHEN the record's `record_type` is one of `MX` or `SRV` AND
   `priority` is set THE SYSTEM SHALL emit the priority as the
   leading rdata token.
5. WHEN the record's `record_type` is not in the supported set
   (`A`, `AAAA`, `CNAME`, `MX`, `TXT`, `SRV`, `PTR`, `NS`,
   `SOA`) THE SYSTEM SHALL return `FAILED` with a stderr
   payload that names the unsupported type.

---

### Requirement 5: Remove a Resource Record

**User Story:** As a maintenance coordinator, I want to remove a
resource record so that clients stop resolving the stale name.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_record(zone, record)` THE
   SYSTEM SHALL drop every line whose parsed
   `(name, record_type, value)` tuple matches `record`, bump
   the SOA serial, write via `tee`, and dispatch
   `rndc reload <zone>`.
2. WHEN no matching record is on disk THE SYSTEM SHALL return
   `SKIPPED` without dispatching any write.
3. WHEN the zone-data file does not exist THE SYSTEM SHALL
   return `SKIPPED` (treating absent state as already-removed).

---

### Requirement 6: List Records

**User Story:** As a maintenance coordinator, I want to enumerate
the records in a zone so that I can audit drift.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_records(zone)` THE SYSTEM
   SHALL `cat` the zone-data file and parse every record line
   into an `UbuntuDnsRecordInfo`.
2. WHEN the SOA record is parsed THE SYSTEM SHALL populate the
   `serial` field on the row.
3. WHEN `cat` exits non-zero with a non-empty stderr THE SYSTEM
   SHALL raise `UbuntuDnsQueryError`.
4. WHEN `cat` exits non-zero with an empty stderr (the file is
   simply absent) THE SYSTEM SHALL return an empty list.

---

### Requirement 7: Reload a Zone

**User Story:** As a maintenance coordinator, I want to force a
zone reload so that BIND picks up out-of-band changes to the
zone-data file.

#### Acceptance Criteria

1. WHEN the coordinator calls `reload_zone(name)` THE SYSTEM
   SHALL dispatch `rndc reload <name>` through the runner
   unconditionally.
2. THE SYSTEM SHALL honour `config.dry_run` and elevation, but
   SHALL NOT skip on any state-based idempotency check.

---

### Requirement 8: Get Zone Status

**User Story:** As a maintenance coordinator, I want to query
the runtime state of a zone so that I can confirm BIND has
actually loaded the latest content.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_zone_status(name)` THE SYSTEM
   SHALL dispatch `rndc zonestatus <name>` through the runner.
2. THE SYSTEM SHALL parse the output into an
   `UbuntuDnsZoneStatus` whose `is_loaded` is `True` when
   `rndc zonestatus` reports the zone's name, and whose
   `serial` / `last_loaded` / `transferred_from` fields are
   populated from the corresponding output keys.
3. WHEN `rndc zonestatus` exits non-zero with a non-empty
   stderr THE SYSTEM SHALL raise `UbuntuDnsQueryError`.

---

### Requirement 9: Validate Before Apply

**User Story:** As a maintenance coordinator, I want every
mutating verb to validate the rendered configuration before
dispatching the apply command so that a bad render cannot break
the running daemon.

#### Acceptance Criteria

1. WHEN any write verb completes the file mutations BUT
   `named-checkconf` fails THE SYSTEM SHALL return `FAILED`
   without dispatching `rndc reconfig` or `rndc reload`.
2. WHEN any write verb completes the file mutations BUT
   `named-checkzone` fails THE SYSTEM SHALL return `FAILED`
   without dispatching the reload command.
3. THE `design.md` SHALL document that validation failures
   leave the file mutations on disk for forensic inspection;
   the operator is expected to fix the spec and retry.

---

### Requirement 10: Dry-Run, Elevation, and Error Reporting

**User Story:** As a maintenance coordinator, I want every write
verb to honour dry-run and to elevate predictably.

#### Acceptance Criteria

1. WHEN `config.dry_run=True` THE SYSTEM SHALL NOT mutate any
   file and SHALL NOT dispatch `rndc reconfig` /
   `rndc reload <zone>`.
2. WHEN `config.elevated=True` THE SYSTEM SHALL pass
   `elevated=True` to the runner on every write dispatch
   (including `tee`, `rm`, `named-checkconf`,
   `named-checkzone`, and `rndc`).
3. WHEN the runner returns non-zero on a write THE SYSTEM SHALL
   return `UbuntuDnsActionResult(status=FAILED,
   exit_code=<code>, stderr=<captured>)` without raising.
4. THE SYSTEM SHALL never elevate read verbs (`list_zones`,
   `list_records`, `get_zone_status`).

---

### Requirement 11: Documented Limitations

**User Story:** As a maintenance coordinator, I want the
documentation to enumerate every native BIND feature this pilot
does not cover so that I do not file false bugs.

#### Acceptance Criteria

1. THE `design.md` SHALL list DNSSEC zone signing, TSIG /
   transfer keys, RPZ (response-policy zones), views /
   split-horizon, dynamic-update ACL management, and DoT / DoH
   listener configuration as out of scope.
2. THE `design.md` SHALL describe the spec-vs-contract drift
   discovered during implementation in a dedicated section.
3. THE `design.md` SHALL note that the `bind9` systemd unit
   name on Ubuntu differs from the `named` unit name on RHEL,
   and that this manager defaults to the Ubuntu form.
