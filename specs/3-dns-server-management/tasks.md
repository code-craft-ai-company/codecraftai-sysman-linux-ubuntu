# Tasks

## Pilot Scope

The pilot exposes eight verbs (`add_zone`, `remove_zone`,
`list_zones`, `add_record`, `remove_record`, `list_records`,
`reload_zone`, `get_zone_status`) through a single
`UbuntuDnsServerManager`.  Every verb is dispatched through the
injected `ICommandRunner`.

---

## Implementation Checklist

- [x] Author `requirements.md` (~340 lines) describing the eleven
      requirements that drive the implementation.
- [x] Author `design.md` describing the architecture, the
      cross-OS DNS-server-model concept-mapping table, the
      sentinel-bracketed block convention, the SOA serial
      bumping algorithm, the validate-before-apply discipline,
      the elevation policy, and the documented contract drift.
- [x] Author `dnsservermanagement.py` with:
    - [x] `UbuntuDnsActionStatus` (alias of `ActionStatus`).
    - [x] `UbuntuDnsManagerConfig` dataclass.
    - [x] `UbuntuDnsActionResult` dataclass.
    - [x] `UbuntuDnsZoneSpec` dataclass.
    - [x] `UbuntuDnsZoneInfo` dataclass.
    - [x] `UbuntuDnsRecordSpec` dataclass.
    - [x] `UbuntuDnsRecordInfo` dataclass.
    - [x] `UbuntuDnsZoneStatus` dataclass.
    - [x] `UbuntuDnsQueryError` exception.
    - [x] `UbuntuDnsServerManager` class with the eight verbs.
    - [x] Module-level renderer `_render_zone_block`.
    - [x] Module-level renderer `_render_initial_zone_file`.
    - [x] Module-level renderer `_render_record_line`.
    - [x] Module-level helper `_bump_soa_serial`.
    - [x] Module-level parser `_parse_zone_records`.
    - [x] Module-level parser `_parse_managed_zone_blocks`.
    - [x] Module-level parser `_parse_named_checkconf_p`.
    - [x] Module-level parser `_parse_rndc_zonestatus`.
- [x] Author the test module with at least 14 tests covering:
    - [x] `add_zone` argv shape.
    - [x] `add_zone` SKIPPED when sentinel block + zone file present.
    - [x] `add_zone` DRY_RUN does not invoke the runner.
    - [x] `remove_zone` argv shape (`tee` + `rm` + `rndc reconfig`).
    - [x] `remove_zone` SKIPPED when absent.
    - [x] `list_zones` parser handles managed + external blocks.
    - [x] `list_zones` raises on `named-checkconf -p` failure.
    - [x] `add_record` argv shape and serial bump.
    - [x] `add_record` SKIPPED when present.
    - [x] `remove_record` argv shape.
    - [x] `list_records` parses A / AAAA / CNAME / TXT.
    - [x] `reload_zone` invokes `rndc reload`.
    - [x] `get_zone_status` parses `rndc zonestatus`.
    - [x] Writes elevate when configured.
    - [x] Reads never elevate.
    - [x] Failed command surfaces stderr.
    - [x] SOA serial increases on record changes.
    - [x] Record-line renderer emits priority for MX records.
    - [x] Round-trip render-then-parse for records.

---

## Out-of-Scope Follow-ups

These items are intentionally deferred:

- **DNSSEC zone signing.** Adopt the RHEL implementation's
  `dnssec-policy` surface and KSK / ZSK rollover.
- **TSIG / transfer-key management.** Render `key {}`
  declarations and wire `allow-transfer { key ...; };`.
- **RPZ (response-policy zones).** Render `response-policy {}`
  blocks.
- **Views / split-horizon.** Multi-view `view "<name>" {}`
  declarations.
- **Dynamic-update ACL beyond the boolean toggle.**  Render
  `allow-update { ... key ...; ... };` from a structured
  spec field.
- **DoT / DoH listener configuration.** `tls` / `https`
  directives.
- **`get_server_status`.** The contract specifies it; the
  pilot does not implement it.  Daemon-level status is
  delegated to `3-service-management` for now.
- **Unbound / dnsmasq dispatch.** Ubuntu ships both; the pilot
  targets BIND only.  A follow-up will adopt the RHEL
  three-backend `DnsServerBackend` selector.
- **Forward / stub zone payload rendering.**  The spec accepts
  the type tokens but does not render the `forwarders {}` /
  `masters {}` payload yet.

---

## Acceptance Snapshot

`requirements.md` covers eleven requirements.  `design.md`
documents the BIND-9 zone-data model, the cross-OS concept
mapping, the SOA serial bumping algorithm, the
validate-before-apply discipline, and the spec-vs-contract
drift.  The implementation file passes the following
hand-checks:

1. Every public method has a Sphinx docstring with
   `:param`, `:returns`, and (where applicable) `:raises`.
2. Every renderer / parser is a module-level pure function with
   keyword-only parameters.
3. No nested function calls in the dispatch path.
4. No mutable default values.
5. No truthy / falsy shortcut tests.
6. Every error string is built via `os.linesep.join(lines)`.
7. The `UbuntuDnsServerManager` class definition stays
   under 800 lines including helpers; module-level functions
   live at module scope.

---

## Test Bar

- Minimum 14 tests; current count at completion: 17.
- Every test uses `_MockCommandRunner` — no live BIND
  invocations.
- The mock seeds `RunResult` values keyed by argv tuple so the
  same runner can stand in for the entire flow of an idempotent
  verb (probe + write + validate + reload).
