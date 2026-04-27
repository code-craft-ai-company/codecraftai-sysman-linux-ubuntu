# Design

## Overview

The mail-transport-management feature is implemented as a single
module,
`automojo.sysman.linux.ubuntu.mailtransportmanagement`, exposing a
`UbuntuMailTransportManager` class that wraps `postconf`,
`postqueue`, `mailq`, `newaliases`, and `sendmail` through the
`ICommandRunner` abstraction.  It also reads and writes
`/etc/aliases` via `cat` / `tee` and reloads `postfix.service` via
`systemctl reload postfix`.

The Ubuntu pilot targets **Postfix exclusively**.  The cross-OS
contract notes that Ubuntu may also run `exim4` or `sendmail` (the
SysV-MTA) — these MTAs are out of scope for the pilot.  Hosts running
a non-Postfix MTA will surface `FAILED` results for write verbs and
empty / synthetic shapes for reads when the Postfix CLI tools are not
on `PATH`; cross-MTA detection and dispatch is a tracked follow-up
captured in `tasks.md`.

This feature does NOT own the `postfix.service` lifecycle (start /
stop / enable / disable / status) — that lives in
`3-service-management`.  The reload after a successful
`set_mta_config` is the sole exception: it is the canonical "apply
directive change" signal for Postfix, idiomatic to this feature
rather than to the lifecycle one.

**See Also:**
- `3-service-management` — `postfix.service` lifecycle.
- `3-firewall-management` — SMTP / submission / smtps ports.
- `3-certificate-trust-management` / `3-certificate-issuance` — TLS
  key/cert material for SMTP+TLS.

---

## Architecture

```
+-----------------------------------------------------------------+
|            Maintenance Coordinator                              |
+-----------------------------------------------------------------+
                       |
                       v
+-----------------------------------------------------------------+
|          UbuntuMailTransportManager                             |
|                                                                 |
|  Reads:                                                         |
|     get_mta_config()    -> postconf -n  -> parse                |
|     list_aliases()      -> cat /etc/aliases -> parse            |
|     get_queue_status()  -> mailq -> parse                       |
|                                                                 |
|  Writes:                                                        |
|     set_mta_config()    -> postconf -e per setting +            |
|                            systemctl reload postfix             |
|     add_alias()         -> cat + render + tee + newaliases      |
|     remove_alias()      -> cat + render + tee + newaliases      |
|     test_mail_delivery()-> sendmail -t (stdin payload)          |
|     flush_queue()       -> postqueue -f                         |
+-----------------------------------------------------------------+
                       |
                       v
+-----------------------------------------------------------------+
|                ICommandRunner                                   |
+-----------------------------------------------------------------+
```

---

## Configuration

```python
@dataclass
class UbuntuMtaManagerConfig(FeatureConfig):
    aliases_file: str = "/etc/aliases"
    service_name: str = "postfix"
    reload_after_set: bool = True
```

Inherited from `FeatureConfig`:

- `dry_run: bool = False` — when True, mutations short-circuit to
  `DRY_RUN`.
- `elevated: bool = False` — when True, writes pass `elevated=True`
  to the runner.

`reload_after_set` controls whether `set_mta_config` issues the
trailing `systemctl reload <service_name>` after a successful change.
Callers integrating this manager with a higher-level "apply config,
then bounce daemons in one batch" workflow can set it to `False`.

---

## Data Models

```python
class UbuntuMtaActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class UbuntuMtaActionResult(FeatureActionResult):
    setting: str | None = None
    local: str | None = None


@dataclass
class UbuntuMtaConfig:
    myhostname: str | None = None
    mydomain: str | None = None
    myorigin: str | None = None
    relayhost: str | None = None
    mynetworks: str | None = None
    inet_interfaces: str | None = None
    inet_protocols: str | None = None
    mailbox_size_limit: int | None = None
    message_size_limit: int | None = None
    extras: dict[str, str] = field(default_factory=dict)


@dataclass
class UbuntuMtaAliasInfo:
    local: str
    remote: str


@dataclass
class UbuntuMtaQueueStatus:
    pending_count: int = 0
    deferred_count: int = 0
    total_size_bytes: int | None = None
    queue_age_oldest: str | None = None


class UbuntuMtaQueryError(Exception):
    ...
```

---

## MTA Mapping

The cross-OS contract requires every implementation to document the
default MTA, the configuration paths it manages, and the
"no-MTA-installed" detection procedure.

| Concept                      | Ubuntu                                |
|------------------------------|---------------------------------------|
| Default MTA                  | Postfix                               |
| Service unit                 | `postfix.service` (systemd)           |
| Primary config               | `/etc/postfix/main.cf` (managed via   |
|                              | `postconf` rather than direct edits)  |
| Aliases file                 | `/etc/aliases`                        |
| Compiled aliases DB          | `/etc/aliases.db` (rebuilt by         |
|                              | `newaliases`)                         |
| Reload signal                | `systemctl reload postfix`            |
| Detection                    | If `postconf -n` exits 127 / "command |
|                              | not found", the host has no Postfix.  |
|                              | The pilot raises `UbuntuMtaQueryError`|
|                              | for reads in that case; writes return |
|                              | `FAILED` with the runner's error.     |

---

## Key Method Designs

### `get_mta_config()`

1. Dispatch `["postconf", "-n"]` with `elevated=False`.
2. If exit non-zero AND stderr non-empty, raise
   `UbuntuMtaQueryError`.
3. Walk each line, split on `=`, place into the named field or
   `extras`.
4. Coerce integer-typed directives via `int()`; non-numeric values
   fall back to `extras`.

### `set_mta_config(spec)`

1. Honour dry-run: return `DRY_RUN`.
2. Call `get_mta_config()` to load the live directive set.  If it
   raises, surface a `FAILED` result and stop.
3. For each named field on `spec` (skipping `None`s) and each entry
   in `spec.extras`, compare against the live value:
   - If equal: record `SKIPPED <key>=<value>` in the audit trail.
   - If different: dispatch `["postconf", "-e",
     "<key>=<value>"]` with `elevated`.  On non-zero, surface
     `FAILED` immediately and stop the loop.
4. If at least one directive changed AND `config.reload_after_set`,
   dispatch `["systemctl", "reload", config.service_name]`.  On
   non-zero, surface `FAILED`.
5. If no directives changed, return `SKIPPED`; otherwise return
   `SUCCESS`.  Either way, set `stdout` to the newline-separated
   audit trail.

### `add_alias(local, remote)`

1. Read `/etc/aliases` via `cat`.
2. Parse, walk entries.  If an entry whose `local` matches with
   `remote` matches, return `SKIPPED`.
3. Build the new content via `_upsert_alias`.
4. Honour dry-run: return `DRY_RUN`.
5. Dispatch `["tee", "/etc/aliases"]` with the new content on
   stdin.  On non-zero, return `FAILED`.
6. Dispatch `["newaliases"]`.  On non-zero, return `FAILED` even
   though the `tee` succeeded.
7. Otherwise return `SUCCESS`.

### `remove_alias(local)`

1. Read `/etc/aliases`.  If missing, return `SKIPPED`.
2. Build the new content via `_remove_alias`.
3. If the new content equals the existing content (no entry
   matched), return `SKIPPED`.
4. Honour dry-run: return `DRY_RUN`.
5. Dispatch `tee` then `newaliases` (same chained pattern as
   `add_alias`).

### `list_aliases()`

1. Read `/etc/aliases` via `cat`.  Missing file -> empty list.
2. Walk each line, skipping comments and blanks; split on first
   `:`.  Continuation lines (whitespace-led) fold onto the previous
   entry's `remote`.

### `test_mail_delivery(to, from_addr, subject)`

1. Build the RFC 5322 payload via `_build_probe_message`.  The body
   is fixed (a short audit-tag line) so the contract's signature
   does not need to expose the body to callers.
2. Honour dry-run: return `DRY_RUN`; populate `stdout` with the
   payload that would have been delivered.
3. Dispatch `["sendmail", "-t"]` with the payload on stdin.

### `get_queue_status()`

1. Dispatch `["mailq"]` with `elevated=False`.  If exit non-zero
   AND stderr non-empty, raise `UbuntuMtaQueryError`.
2. If the output contains `Mail queue is empty`, return a zeroed
   status.
3. Walk each line via `_MAILQ_HEAD_PATTERN`.  Queue ids ending in
   `*` are active / pending; unmarked queue ids are deferred.
   On-hold messages (`!`) are also counted as deferred for the
   pilot — the contract surface only exposes pending vs deferred,
   so the on-hold class is folded into deferred.
4. Pull the `-- N Kbytes in M Requests.` footer for the byte count.

### `flush_queue()`

1. Honour dry-run.
2. Dispatch `["postqueue", "-f"]`.

---

## Idempotency Model

| Verb                    | Idempotency probe                      | Skip when                                  |
|-------------------------|----------------------------------------|--------------------------------------------|
| `set_mta_config`        | `postconf -n` per-directive compare    | Live value already matches spec            |
| `add_alias`             | `cat /etc/aliases` + parse             | (local, remote) pair already in file       |
| `remove_alias`          | `cat /etc/aliases` + parse             | No entry for local (or file missing)       |
| `test_mail_delivery`    | none — operational                     | Never                                      |
| `flush_queue`           | none — operational                     | Never                                      |

`set_mta_config` is per-setting idempotent — it dispatches one
`postconf -e` per directive that diverges and skips matched
directives silently.  The aggregate result reports `SKIPPED` only
when **every** directive matched.

---

## Aliases-Database Coherence

Every successful mutation of `/etc/aliases` is followed by a
`newaliases` dispatch via the `_invoke_newaliases` helper.  The
helper returns:

- `SUCCESS` when the `tee` and `newaliases` both exit 0.
- `FAILED` when `newaliases` itself fails — the verb is reported
  failed even though the text mutation succeeded.  The contract
  invariant is "alias mutations leave the compiled database in
  sync"; surfacing the `newaliases` failure is the right way to
  expose violation of that invariant to callers.

---

## Reload-After-Set

`set_mta_config` issues `systemctl reload <service_name>` after at
least one `postconf -e` succeeds.  This is gated on
`config.reload_after_set` (default `True`).  Callers integrating
multi-feature batched workflows ("set every directive across every
feature, then bounce all daemons") can set it `False` and reload
manually.

The reload uses `systemctl reload`, not `restart`, because Postfix's
master process supports SIGHUP-driven re-read and there is no need
to drop established connections to apply new directives.

---

## Dry-Run Behaviour

Dry-run disables every mutating dispatch — `postconf -e`, `tee`,
`newaliases`, `sendmail -t`, `postqueue -f`, `systemctl reload`.
Read-only probes used to drive idempotency (`postconf -n`,
`cat /etc/aliases`, `mailq`) continue to run so the verb can return
a meaningful `SKIPPED` vs `DRY_RUN` distinction when the desired
state already matches.

For `set_mta_config` the pilot returns `DRY_RUN` immediately
without first probing for skip — this is consistent with the rest of
the feature family in this package.

---

## Elevation Policy

| Verb                    | Elevated when `config.elevated=True`     | Notes                                      |
|-------------------------|------------------------------------------|--------------------------------------------|
| `get_mta_config`        | NO — read                                |                                            |
| `set_mta_config`        | YES                                      | Per-directive `postconf -e` + reload       |
| `add_alias`             | YES                                      | `tee` + `newaliases`                       |
| `remove_alias`          | YES                                      | `tee` + `newaliases`                       |
| `list_aliases`          | NO — read                                |                                            |
| `test_mail_delivery`    | YES                                      | `sendmail` enqueues on behalf of root      |
| `get_queue_status`      | NO — read                                | `mailq` does not require elevation         |
| `flush_queue`           | YES                                      | `postqueue -f` requires write to spool     |

Reads NEVER pass `elevated=True` — the contract is unambiguous on
this point.

---

## Correctness Properties

1. **Idempotent directive sets.** `set_mta_config` SKIPs when every
   directive in `spec` already equals the live value.  Per-directive
   skips are recorded in the audit trail in `stdout`.
2. **Alias-database coherence.** Every successful mutation of
   `/etc/aliases` triggers `newaliases`.  `newaliases` failure
   maps the verb to `FAILED`.
3. **Reload coherence.** Successful `set_mta_config` runs trigger
   `systemctl reload` exactly once at the end (when at least one
   directive changed AND `reload_after_set=True`).
4. **Read isolation.** No read verb passes `elevated=True`; no read
   verb mutates state.
5. **Dry-run safety.** No mutating dispatch occurs when
   `dry_run=True`; idempotency probes still run.

---

## Failure Surface

Every write verb returns a `UbuntuMtaActionResult`.  On runner
non-zero the manager populates:

- `status = FAILED`
- `exit_code` = runner's exit code
- `stdout` / `stderr` = runner's captured streams
- `command` = the argv that ran (chained-command failures surface
  the chained argv, not the primary one — see `_invoke_newaliases`)

Read verbs raise `UbuntuMtaQueryError` carrying `command`,
`exit_code`, and `stderr`.

---

## Module-Level Helpers

The module exposes private helpers used by the manager and exercised
directly by parser unit tests.  These are not part of the public
surface but are stable across pilots.

| Helper                          | Role                                          |
|---------------------------------|-----------------------------------------------|
| `_parse_postconf_n_output`      | `postconf -n` -> `UbuntuMtaConfig`            |
| `_apply_directive`              | Place one directive into a config            |
| `_config_to_directive_pairs`    | Flatten config to ordered key/value pairs    |
| `_parse_aliases_file`           | `/etc/aliases` -> list of entries             |
| `_upsert_alias`                 | Render new content for `add_alias`            |
| `_remove_alias`                 | Render new content for `remove_alias`         |
| `_parse_mailq_output`           | `mailq` -> `UbuntuMtaQueueStatus`             |
| `_build_probe_message`          | Build the `sendmail -t` payload               |
| `_DirectivePairs`               | Ordered key/value list with first-match       |
|                                 | lookup                                        |

---

## Testing Strategy

- `_MockCommandRunner` records every `run` call and returns
  pre-seeded `RunResult` values keyed by argv tuple.
- Parser tests for `postconf -n`, `mailq`, and aliases-file content
  drive the helpers directly so failures localise to parsing.
- Integration tests on the manager verify:
  - `get_mta_config` parses representative output and never elevates.
  - `set_mta_config` invokes the per-setting argv, skips matched
    directives, dispatches `systemctl reload` once, and short-circuits
    under dry-run.
  - `add_alias` / `remove_alias` invoke `tee` and chain `newaliases`,
    skip when the desired state matches, and short-circuit under
    dry-run.
  - `list_aliases` parses entries and never elevates.
  - `test_mail_delivery` builds the expected RFC 5322 payload and
    invokes `sendmail -t` with stdin.
  - `get_queue_status` parses representative `mailq` output and
    handles the empty-queue special string.
  - `flush_queue` invokes `postqueue -f`.
  - Writes elevate when `config.elevated=True`; reads never elevate.
  - Failures surface `exit_code` / `stderr` on the result.
  - `add_alias` maps to `FAILED` when `newaliases` fails after a
    successful `tee`.

---

## Seams With Other Features

| Seam                              | Owner                                    | This Feature           |
|-----------------------------------|------------------------------------------|------------------------|
| `postfix.service` lifecycle       | `3-service-management`                   | Reload only            |
| SMTP / submission ports           | `3-firewall-management`                  | Out of scope here      |
| TLS cert / key for SMTP+TLS       | `3-certificate-issuance` / trust         | Caller installs first  |
| User mailbox storage paths        | `3-storage-management`                   | Out of scope here      |
| Mail-queue spool filesystem       | `3-storage-management`                   | Out of scope here      |

---

## Out of Scope

The following are intentionally excluded from this pilot.  They are
candidates for follow-on work:

- Non-Postfix MTAs (`exim4`, `sendmail`).  Detection-and-dispatch
  to alternate command suites would require a new abstraction
  layer.
- Virtual domains and transport maps (`postmap`-managed lookup
  tables).
- TLS / SASL configuration directives — these come in via the
  `extras` dict on `UbuntuMtaConfig` but the pilot does not provide
  helper accessors.
- Per-message tracing beyond `test_mail_delivery`.
- Mailbox format selection (Maildir vs mbox).
- Greylisting and anti-spam integration.
- `master.cf` per-service directive mutation.

---

## Open Questions

1. Should the pilot detect the live MTA before dispatching?  Today
   it assumes Postfix.  A `_detect_mta` probe (e.g., `which
   postconf` + `which exim4` + `which sendmail`) could let the
   manager raise a typed error early when Postfix is absent.  For
   the pilot we let the underlying tool fail with its own error.
2. Should `set_mta_config` accept a "directives to remove" surface
   alongside the snapshot?  Today the only way to revert a
   directive is to drop it from `spec` and call
   `postconf -X <key>` directly — not exposed by the pilot.
3. Should the reload be configurable per-directive?  Some directives
   (e.g., `mailbox_size_limit`) require a full restart rather than a
   reload.  The pilot uses `reload` uniformly.
