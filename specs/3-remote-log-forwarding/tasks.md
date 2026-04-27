# Tasks

## Pilot Scope

The pilot exposes seven verbs (`add_forwarder`,
`remove_forwarder`, `list_forwarders`, `enable_forwarder`,
`disable_forwarder`, `test_forwarder`, `get_forwarder_status`)
through a single `UbuntuRemoteLogForwardingManager`.  Every verb
is dispatched through the injected `ICommandRunner`.

---

## Implementation Checklist

- [x] Author `requirements.md` (~270 lines) describing the
      eleven requirements that drive the implementation.
- [x] Author `design.md` describing the architecture, the
      drop-in renderer, the idempotency rules, the elevation
      policy, and the documented contract drift.
- [x] Author `remotelogforwarding.py` with:
    - [x] `UbuntuForwarderActionStatus` (alias of `ActionStatus`).
    - [x] `UbuntuForwarderManagerConfig` dataclass.
    - [x] `UbuntuForwarderActionResult` dataclass.
    - [x] `UbuntuForwarderSpec` dataclass.
    - [x] `UbuntuForwarderInfo` dataclass.
    - [x] `UbuntuForwarderStatus` dataclass.
    - [x] `UbuntuForwarderTestResult` dataclass.
    - [x] `UbuntuForwarderQueryError` exception.
    - [x] `UbuntuRemoteLogForwardingManager` class with the
          seven verbs.
    - [x] Module-level renderer `_render_drop_in`.
    - [x] Module-level parser `_parse_drop_in`.
- [x] Author the test module with at least 14 tests covering:
    - [x] `add_forwarder` argv shape (TCP).
    - [x] `add_forwarder` SKIPPED when present with matching body.
    - [x] `add_forwarder` DRY_RUN does not invoke the runner.
    - [x] `remove_forwarder` argv shape.
    - [x] `remove_forwarder` SKIPPED when absent.
    - [x] `list_forwarders` parser handles mixed enabled / disabled.
    - [x] `enable_forwarder` argv shape.
    - [x] `enable_forwarder` SKIPPED when already enabled.
    - [x] `disable_forwarder` argv shape.
    - [x] `test_forwarder` invokes `logger`.
    - [x] `get_forwarder_status` parses on-disk state.
    - [x] Writes elevate when configured.
    - [x] Reads never elevate.
    - [x] Failed command surfaces stderr.
    - [x] TLS spec renders the gtls driver block.

---

## Out-of-Scope Follow-ups

These items are intentionally deferred:

- **Disk-spool / queue tuning.** Adopt the RHEL implementation's
  Requirement-2 surface in a follow-up.
- **Structured output (CEE / GELF / JSON).** Render canonical
  templates instead of relying on the operator to pre-install
  them.
- **RELP / omkafka / omhttp transports.** Add transport-specific
  renderers gated on the spec's `protocol` field.
- **TLS client authentication (mutual TLS).** Render
  `$ActionSendStreamDriverAuthMode x509/certvalid` and the
  client certificate / key references.
- **`impstats` parsing.** Populate `last_event_time`,
  `error_count`, and `bytes_sent` on `UbuntuForwarderStatus`.
- **`syslog-ng` rendering path.** Detect the active syslog
  daemon and render `syslog-ng` drop-ins when appropriate.

---

## Acceptance Snapshot

`requirements.md` covers eleven requirements.  `design.md`
documents the rsyslog drop-in pattern, the rendering rules, the
forwarding-model concept-mapping table, the macOS limitation,
the Windows WEF model, and the spec-vs-contract drift.  The
implementation file passes the following hand-checks:

1. Every public method has a Sphinx docstring with
   `:param`, `:returns`, and (where applicable) `:raises`.
2. Every renderer / parser is a module-level pure function with
   keyword-only parameters.
3. No nested function calls in the dispatch path.
4. No mutable default values.
5. No truthy / falsy shortcut tests.
6. Every error string is built via `os.linesep.join(lines)`.
7. The `UbuntuRemoteLogForwardingManager` class definition stays
   under 350 lines; helper functions live at module scope.

---

## Test Bar

- Minimum 14 tests; current count at completion: 17.
- Every test uses `_MockCommandRunner` — no live `rsyslog`
  invocations.
- The mock seeds `RunResult` values keyed by argv tuple so the
  same runner can stand in for the entire flow of an idempotent
  verb (probe + write + restart).
