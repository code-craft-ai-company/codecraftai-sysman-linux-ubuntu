# Design

## Overview

The Ubuntu remote-log-forwarding feature is implemented as a
single module,
`automojo.sysman.linux.ubuntu.remotelogforwarding`, exposing an
`UbuntuRemoteLogForwardingManager` class that manages rsyslog
forward drop-ins under `/etc/rsyslog.d/` through the
`ICommandRunner` abstraction.

The module follows the standard manager pattern shared with every
other Layer 3 feature in this package: it consumes the
`BaseFeatureManager` mixin from
`automojo.sysman.linux.ubuntu.featurebase`, accepts an injected
runner plus an `UbuntuForwarderManagerConfig`, and dispatches
every shell command through the runner.

The cross-OS contract for this feature lives in
`automojo-sysman-common/specs/contracts/remote-log-forwarding/contract.md`.
The RHEL canonical implementation lives in
`automojo-sysman-linux-redhat/specs/3-remote-log-forwarding/`.

**See Also:**
- `3-log-management` — local journal + logrotate surface.
- `3-certificate-issuance` / `3-certificate-trust-management` —
  TLS material referenced by `tls_ca_cert`.
- `3-service-management` — `rsyslog.service` lifecycle (the
  manager dispatches `systemctl restart rsyslog` directly to
  avoid a circular dependency with the broader service manager).

---

## Architecture

```
+-------------------------------------------------------------+
|            Maintenance Coordinator                          |
+-----------------------+-------------------------------------+
                        |
                        v
+-------------------------------------------------------------+
|       UbuntuRemoteLogForwardingManager                      |
|                                                             |
|  Write verbs (idempotent + dry-run aware):                  |
|    add_forwarder(spec)                                      |
|    remove_forwarder(id)                                     |
|    enable_forwarder(id)                                     |
|    disable_forwarder(id)                                    |
|                                                             |
|  Probe verbs:                                               |
|    test_forwarder(id)                                       |
|                                                             |
|  Read verbs:                                                |
|    list_forwarders()                                        |
|    get_forwarder_status(id)                                 |
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

## Forwarding Model — Concept Mapping

The cross-OS contract uses an abstract
`(destination_host, destination_port, transport, selector)` tuple.
Ubuntu maps it to the rsyslog `omfwd` shorthand:

| Abstract field          | rsyslog rendering                             |
|-------------------------|-----------------------------------------------|
| `destination_host`      | `<host>` portion of the action line           |
| `destination_port`      | `<port>` portion of the action line           |
| `transport=UDP`         | Single-`@` action: `*.* @host:port`           |
| `transport=TCP`         | Double-`@` action: `*.* @@host:port`          |
| `transport=TLS`         | Double-`@` action plus a `gtls` driver block  |
| `selector` (`*.*`, ...) | rsyslog facility.severity expression          |
| `tls_ca_cert`           | `$DefaultNetstreamDriverCAFile <path>`        |

Compared with the macOS and Windows implementations:

| OS      | Native concept                          | Notes                               |
|---------|-----------------------------------------|-------------------------------------|
| Ubuntu  | rsyslog drop-in under `/etc/rsyslog.d/` | This module.                        |
| macOS   | `/etc/syslog.conf` legacy syslogd       | Limited subset (legacy facility).   |
| Windows | Windows Event Forwarding (`wecutil`)    | XML subscription documents.         |
| RHEL    | rsyslog (canonical)                     | Reference implementation.           |

---

## Configuration

```python
@dataclass
class UbuntuForwarderManagerConfig(FeatureConfig):
    rsyslog_d:        str = "/etc/rsyslog.d"
    rsyslog_service:  str = "rsyslog"
    prefix:           str = "automojo-fwd-"
```

`dry_run` and `elevated` are inherited from `FeatureConfig`.

The `prefix` field is load-bearing.  Every drop-in this manager
writes is named `<prefix><id>.conf`; every parser filters the
listing to that prefix.  Operators must NOT change the prefix at
runtime — the `list_forwarders` parser uses it as a sentinel to
distinguish managed drop-ins from third-party drop-ins.

---

## Data Models

```python
class UbuntuForwarderActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class UbuntuForwarderActionResult(FeatureActionResult):
    forwarder_id: str | None = None
    path:         str | None = None


@dataclass
class UbuntuForwarderSpec:
    id:           str
    host:         str
    port:         int    = 514
    protocol:     str    = "tcp"     # "tcp" | "udp" | "tls"
    selector:     str    = "*.*"
    template:     str | None = None
    tls_ca_cert:  str | None = None
    enabled:      bool   = True


@dataclass
class UbuntuForwarderInfo:
    id:                 str
    host:               str
    port:               int
    protocol:           str
    enabled:            bool
    path:               str | None
    selector_or_query:  str | None


@dataclass
class UbuntuForwarderStatus:
    id:                  str
    enabled:             bool
    last_event_time:     str | None = None
    error_count:         int | None = None
    bytes_sent:          int | None = None


@dataclass
class UbuntuForwarderTestResult(FeatureActionResult):
    forwarder_id:    str = ""
    test_message_id: str | None = None


class UbuntuForwarderQueryError(Exception):
    """Read-side failure that is not 'absent'."""
```

`UbuntuForwarderActionStatus = ActionStatus` aliases the shared
enum so that callers can still write
`UbuntuForwarderActionStatus.SUCCESS` while the actual values
flow through `BaseFeatureManager`'s `_dispatch` helper.

---

## Drop-in Body Rendering

The renderer is a pure function: `_render_drop_in(spec)`.  It
must be deterministic so that idempotency comparisons work.

### Plain TCP / UDP

```
# automojo-fwd-<id>
# Generated by automojo UbuntuRemoteLogForwardingManager
<selector> @@<host>:<port>
```

(`@` instead of `@@` for UDP.)

### TLS

```
# automojo-fwd-<id>
# Generated by automojo UbuntuRemoteLogForwardingManager
$DefaultNetstreamDriver gtls
$DefaultNetstreamDriverCAFile <tls_ca_cert>
$ActionSendStreamDriver gtls
$ActionSendStreamDriverMode 1
$ActionSendStreamDriverAuthMode x509/name
<selector> @@<host>:<port>
```

The `rsyslog-gnutls` package MUST be present for the gtls driver
to load.  This manager does NOT install it; the
`add_forwarder` write succeeds, but the post-write
`systemctl restart rsyslog` step surfaces the failure when the
package is missing.

### Optional template

When `spec.template` is provided, the action line gains a
`;TEMPLATE` suffix:

```
<selector> @@<host>:<port>;<template>
```

Templates themselves are not authored by this manager — callers
either reference a built-in template (e.g. `RSYSLOG_SyslogProtocol23Format`)
or pre-install a `$template` definition through a separate
config drop-in (out of scope for this pilot).

---

## Key Method Designs

### `add_forwarder(spec)`

1. Validate the spec.  When `spec.protocol == "tls"` and
   `spec.tls_ca_cert` is `None`, return `FAILED` with a
   stderr that names the missing field.
2. Render the desired body via `_render_drop_in`.
3. Read the existing drop-in via `cat <prefix><id>.conf`.
4. When the existing body matches the desired body
   byte-for-byte → `SKIPPED`.
5. When `config.dry_run` → `DRY_RUN`.
6. Dispatch `tee <prefix><id>.conf` with the body on stdin.
7. On non-zero exit → return `FAILED`.
8. Dispatch `systemctl restart rsyslog`.
9. On non-zero restart → return `FAILED` with the restart's
   exit code / stderr (the drop-in is still on disk; the caller
   re-runs once the package state is corrected).

### `remove_forwarder(id)`

1. Probe both `<prefix><id>.conf` and
   `<prefix><id>.conf.disabled` via `test -e`.
2. When neither exists → `SKIPPED`.
3. When `config.dry_run` → `DRY_RUN`.
4. `rm` whichever file is present (the disabled variant has
   priority when both exist; this is also a self-healing path).
5. Dispatch `systemctl restart rsyslog`.

### `enable_forwarder(id)` / `disable_forwarder(id)`

1. Probe both file variants.
2. When the requested state matches → `SKIPPED`.
3. When `config.dry_run` → `DRY_RUN`.
4. `mv` between `<prefix><id>.conf` and
   `<prefix><id>.conf.disabled`.
5. Dispatch `systemctl restart rsyslog`.

### `list_forwarders()`

1. `ls -1 <rsyslog_d>` (no elevation).
2. Filter the output to entries that start with `<prefix>`.
3. For each matching basename, classify as `.conf` (enabled) or
   `.conf.disabled` (disabled).
4. `cat` the file body and parse `host`, `port`, `protocol`,
   and `selector_or_query` from the rendered stanza.  The
   parser is permissive: tokens are extracted regardless of
   whitespace.
5. Return a list of `UbuntuForwarderInfo`.
6. Failures other than the directory simply being empty raise
   `UbuntuForwarderQueryError`.

### `test_forwarder(id)`

1. Build the test message ID:
   `forwarder-test-<int(time.time())>`.
2. Dispatch
   `logger -t automojo-test 'forwarder-test-<timestamp>'`
   without elevation.
3. Wrap the runner result into `UbuntuForwarderTestResult`.
4. Never raises on dispatch failure.

### `get_forwarder_status(id)`

1. Probe both file variants.
2. When neither exists → return a status whose `enabled` is
   `False`.
3. Otherwise return a status describing the on-disk state.
   `last_event_time`, `error_count`, and `bytes_sent` are
   `None` for the pilot — rsyslog's `impstats` output is not
   parsed in this iteration (recorded in `tasks.md` as a
   follow-up).
4. No elevation.

---

## Dry-Run Behaviour

Dry-run disables every mutating dispatch (`tee`, `rm`, `mv`,
`systemctl restart`).  Reads (`ls`, `cat`, `test -e`) and the
`logger`-based probe run unconditionally.  Dry-run results
report the would-have-been argv on the result's `command` field.

---

## Idempotency

| Verb                    | Idempotency rule                                     |
|-------------------------|------------------------------------------------------|
| `add_forwarder`         | SKIP when on-disk body matches rendered body.        |
| `remove_forwarder`      | SKIP when neither `.conf` nor `.disabled` exists.    |
| `enable_forwarder`      | SKIP when only `.conf` exists.                       |
| `disable_forwarder`     | SKIP when only `.disabled` exists.                   |
| `test_forwarder`        | No idempotency guard (always dispatches `logger`).   |
| `list_forwarders`       | n/a (read).                                          |
| `get_forwarder_status`  | n/a (read).                                          |

---

## Elevation

| Situation                                  | Behaviour                          |
|--------------------------------------------|------------------------------------|
| `elevated=True`, write verb                | Pass `elevated=True` to runner.    |
| `elevated=True`, read verb                 | Do NOT elevate.                    |
| `elevated=False`                           | Never elevate.                     |
| `test_forwarder`                           | Never elevates (read-class verb).  |

The default `elevated=True` is appropriate for production: every
file the manager touches lives in `/etc/rsyslog.d`, which root
owns by default on Ubuntu.

---

## Service Reload

After every successful mutation the manager dispatches
`systemctl restart <rsyslog_service>` through the runner.  The
restart honours `config.elevated`.  When the restart fails the
mutating verb returns `FAILED` with the restart's exit code and
stderr — the drop-in itself is left on disk because the
operator's intent (the drop-in body) was already written.

The default service name is `rsyslog`.  Operators on hosts that
have replaced rsyslog with `syslog-ng` can override
`rsyslog_service` to point at the alternative service unit, but
the renderer itself only emits rsyslog-syntax drop-ins.

---

## Correctness Properties

1. **Deterministic rendering.** The renderer is a pure function
   of the spec; identical inputs produce identical outputs.
2. **Byte-stable idempotency.** `add_forwarder` compares the
   rendered body to the on-disk body as raw bytes.
3. **Best-effort cleanup.** `remove_forwarder` deletes the
   `.disabled` shadow when present so the listing parser
   stops reporting the forwarder.
4. **Reads never elevate.** Every parsing path goes through
   `cat`, `ls`, or `test -e` without `elevated=True`.
5. **Probe never raises.** `test_forwarder` wraps every failure
   in the result record.

---

## Out of Scope (Documented Limitations)

The following native rsyslog features are NOT implemented in
this pilot.  They are recorded as follow-ups in `tasks.md`:

- **Disk-spool / queue tuning.** rsyslog's
  `queue.type="LinkedList"` / `queue.filename` / `queue.spoolDirectory`
  / `queue.maxDiskSpace` settings.  The RHEL implementation has
  Requirement-2 coverage; Ubuntu inherits the gap from the
  pilot scope.
- **Structured output formats.** CEE / GELF / JSON renderings
  are not authored.  Callers must pre-install a
  `$template` block via a separate drop-in.
- **RELP transport.** `omrelp` is not exposed; only
  `omfwd` (UDP/TCP/TLS).
- **omkafka / omhttp outputs.** Out of scope.
- **TLS client authentication.** Only `x509/name` server-auth
  mode is rendered.  Mutual TLS via client certs is a follow-up.
- **`impstats` parsing for `last_event_time`, `error_count`,
  `bytes_sent`.** These status fields are wired into the data
  model as `None` placeholders.
- **`syslog-ng` rendering.** Only rsyslog-syntax drop-ins are
  authored.

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
- One TLS rendering test asserts the `gtls` driver block is
  written verbatim.
- One `list_forwarders` parser test covers a multi-forwarder
  directory with mixed enabled / disabled drop-ins.
- One reload-failure test proves the mutating verb returns
  `FAILED` when `systemctl restart rsyslog` fails.
- One elevation test proves writes elevate and reads do not.
- One `test_forwarder` test asserts the `logger` argv shape.

---

## Spec-vs-Contract Drift

| Drift point                                | Resolution                                          |
|--------------------------------------------|-----------------------------------------------------|
| Contract uses `ForwarderProbeResult` (reachability + latency) | This pilot returns `UbuntuForwarderTestResult` (a `FeatureActionResult` subclass) because `logger` does NOT round-trip — there is no probe to time.  The result still carries `forwarder_id` so callers can correlate.  The `latency_ms` and `reachable` fields from the contract are intentionally omitted in this pilot. |
| Contract names `ForwarderTransport` enum   | The pilot uses a `protocol: str` field on the spec because the rsyslog renderer takes a literal `tcp` / `udp` / `tls` token. |
| Contract specifies `last_sent_at` / `queue_depth` on `ForwarderInfo` | The pilot moves those fields onto `UbuntuForwarderStatus` and surfaces them as `None` placeholders pending `impstats` integration. |

The drift is intentional and documented; future work will
reconcile the pilot models with the contract once the cross-OS
implementations have stabilised.

---

## Seams With Other Features

| Seam                                | Owner                                | This Feature        |
|-------------------------------------|--------------------------------------|---------------------|
| Local journal + logrotate           | `3-log-management`                   | Out of scope here.  |
| TLS material                        | `3-certificate-issuance` / trust-mgmt| Caller installs.    |
| `rsyslog.service` lifecycle         | `3-service-management`               | Restart dispatched directly via `systemctl restart`.|
| `rsyslog-gnutls` package install    | `3-package-management`               | Out of scope here.  |
