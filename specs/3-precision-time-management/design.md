# Design

## Overview

The precision-time-management feature is implemented as a single
module,
`ccai.sysman.linux.ubuntu.precisiontimemanagement`, exposing a
`UbuntuPrecisionTimeManager` class that wraps either the chrony tool
family (`chronyc`, `chronyd`, `chrony.service`) or the
systemd-timesyncd tool family (`timedatectl`,
`systemd-timesyncd.service`) through the `ICommandRunner` abstraction.
The selected backend is captured on
`UbuntuPrecisionTimeManagerConfig` and bound at construction time.

The manager follows the locked-in pattern of every Layer-3 manager in
this package:

* A `BaseFeatureManager` subclass that ferries the canonical fields
  (`runner`, `config`) onto `self` and exposes `_dispatch` /
  `_wrap_result` style helpers.
* A feature-prefixed status enum, action result, and config dataclass
  vendored on top of the package's `featurebase`.
* OS-prefixed types throughout (`Ubuntu*`).
* No live filesystem access — every file read / write flows through
  the runner via `cat` and `tee`.

**See Also:**
- `3-network-configuration` — companion infrastructure feature.
- `codecraftai-sysman-linux-redhat/specs/3-precision-time-management/`
  — the RHEL reference, which targets the IEEE 1588 PTP surface and
  is intentionally not duplicated here.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│            Maintenance Coordinator                          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuPrecisionTimeManager                       │
│                                                             │
│  Reads:                                                     │
│    get_time_status(), get_time_sources(), get_drift()       │
│  Writes:                                                    │
│    add_time_source(), remove_time_source(),                 │
│    set_time_servers(), force_sync_now(),                    │
│    enable_sync(), disable_sync()                            │
└────────────────┬────────────────────────────────────────────┘
                 │
                 ▼
       ┌─────────────────────┐
       │   ICommandRunner    │
       └─────────────────────┘
                 │
        ┌────────┴─────────┐
        ▼                  ▼
   chrony backend    timesyncd backend
   (chronyc,         (timedatectl,
    chrony.service)   systemd-timesyncd)
```

---

## Configuration

```python
class UbuntuTimeBackend(Enum):
    CHRONY    = "chrony"
    TIMESYNCD = "timesyncd"
    NTPD      = "ntpd"


@dataclass
class UbuntuPrecisionTimeManagerConfig(FeatureConfig):
    backend: UbuntuTimeBackend = UbuntuTimeBackend.CHRONY
    chrony_conf: str = "/etc/chrony/chrony.conf"
    timesyncd_conf: str = "/etc/systemd/timesyncd.conf"
    service_name: Optional[str] = None
```

* `dry_run` and `elevated` are inherited from `FeatureConfig`.
* `service_name=None` (the default) means "auto-derive from
  `backend`": `chrony` for the chrony backend and
  `systemd-timesyncd` for the timesyncd backend.

---

## Backend Dispatch Table

Every verb routes through the active backend.  The full table:

| Verb               | CHRONY                                        | TIMESYNCD                                                       |
|--------------------|-----------------------------------------------|-----------------------------------------------------------------|
| `get_time_status`  | `chronyc tracking`                            | `timedatectl status`                                            |
| `get_time_sources` | `chronyc sources`                             | `timedatectl show --property=NTP --property=NTPSynchronized --property=SystemNTPServers --property=FallbackNTPServers --property=ServerName --property=ServerAddress` |
| `get_drift`        | `chronyc tracking` (parse offset/freq/jitter) | `timedatectl show --property=NTPMessage --property=ServerName`  |
| `add_time_source`  | append `pool <s> iburst` to chrony.conf, restart chrony | rewrite `NTP=` of timesyncd.conf, restart systemd-timesyncd |
| `remove_time_source` | remove `server`/`pool` line, restart chrony | rewrite `NTP=` line, restart systemd-timesyncd                  |
| `set_time_servers` | rewrite all `pool` lines, restart chrony      | rewrite `NTP=` line, restart systemd-timesyncd                  |
| `force_sync_now`   | `chronyc makestep`                            | `systemctl restart systemd-timesyncd`                           |
| `enable_sync`      | `systemctl enable --now chrony`               | `timedatectl set-ntp true`                                      |
| `disable_sync`     | `systemctl disable --now chrony`              | `timedatectl set-ntp false`                                     |

The `NTPD` backend is reserved.  Selecting it raises
`NotImplementedError` at first use.  All commands route through the
runner; the manager never invokes a subprocess directly.

---

## Data Models

```python
UbuntuPrecisionTimeActionStatus = ActionStatus  # alias


@dataclass
class UbuntuPrecisionTimeActionResult(FeatureActionResult):
    server: Optional[str] = None


@dataclass
class UbuntuTimeStatus:
    enabled: bool
    synchronized: bool
    current_server: Optional[str]
    stratum: Optional[int]
    last_sync: Optional[str]
    offset_seconds: Optional[float]


@dataclass
class UbuntuTimeSourceInfo:
    server: str
    state: str                          # 'selected' | 'candidate' | 'unreachable'
    stratum: Optional[int]
    offset_seconds: Optional[float]
    last_response: Optional[str]


@dataclass
class UbuntuTimeDrift:
    offset_seconds: float
    frequency_ppm: Optional[float]
    jitter_seconds: Optional[float]
    stratum: Optional[int]


class UbuntuPrecisionTimeQueryError(Exception):
    ...
```

`UbuntuPrecisionTimeActionResult` extends the canonical
`FeatureActionResult` with a single new field, `server`, that records
the target of `add_time_source` / `remove_time_source`.  Verbs that
do not target a single server populate `server=None`.

The contract's abstract `TimeStatus` field `sync_service_active` is
collapsed into `enabled` here; both fields capture the same property
("the sync service is running and configured to keep running").  The
contract's `last_sync_time: datetime` is shipped as
`last_sync: Optional[str]` because both `chronyc tracking` and
`timedatectl status` print human-formatted timestamp strings rather
than UTC ISO strings, and round-tripping into `datetime` would force
the manager to make timezone assumptions; emitting the native string
keeps responsibility at the caller.

---

## Key Method Designs

### `get_time_status()`

1. Branch on `config.backend`.
2. **Chrony**: dispatch `chronyc tracking` (no elevation).  If the
   exit code is non-zero AND stderr matches one of
   `("506 cannot talk to daemon", "no such file or directory",
   "connection refused", "could not connect")`, return
   `UbuntuTimeStatus(enabled=False, synchronized=False, ...)`.
   Otherwise raise `UbuntuPrecisionTimeQueryError`.  Otherwise parse
   the keyed-line payload, extract `Reference ID`, `Stratum`,
   `Ref time (UTC)`, `Last offset`, and `Leap status`.
3. **Timesyncd**: dispatch `timedatectl status` (no elevation).  On
   non-zero exit, raise.  Otherwise parse the keyed-line block and
   extract `System clock synchronized` (yes/no) and `NTP service`
   (active/inactive).

### `get_time_sources()`

1. Branch on `config.backend`.
2. **Chrony**: dispatch `chronyc sources`.  Skip header rows
   (separator `=` lines, leading `MS Name`).  For each remaining
   line, the first token is the `MS` mode/state pair; the second is
   the source name; the third is the stratum; the sixth is the
   last-Rx token; the seventh is the sample-offset token (parsed via
   `±<num>{ns,us,ms,s}`).  Map the second character of `MS` onto
   `selected`/`candidate`/`unreachable`.
3. **Timesyncd**: dispatch `timedatectl show --property=...`.
   Combine `SystemNTPServers` + `FallbackNTPServers` (deduplicated
   in declaration order); mark the `ServerName` entry as
   `selected`; mark the rest as `candidate`.

### `add_time_source(server)`

1. Compute `tee` command for the active config path.
2. Dry-run short-circuit: return `DRY_RUN` with the would-have-been
   `tee` command on `result.command`.
3. `cat` the existing config; parse out the current server list.
4. If `server` is already in the list, return `SKIPPED`.
5. Append `server` to the list; render the new file body via
   `_render_chrony_conf_servers` or
   `_render_timesyncd_conf_servers`.
6. `tee` the new body to the config path with `elevated=elevated`.
7. `systemctl restart <service>`; on success return `SUCCESS`,
   otherwise `FAILED` with the failing argv on `command`.

### `remove_time_source(server)`

Same shape as `add_time_source`, with the recompute step pruning
`server` from the list and the idempotency guard inverted.

### `set_time_servers(servers)`

1. Compute `tee` command.
2. Dry-run short-circuit.
3. `cat`-and-parse the current list.
4. If `sorted(current) == sorted(servers)`, return `SKIPPED`.
5. Render the supplied list, `tee`, restart.

### `force_sync_now()`

* Chrony: dispatch `chronyc makestep`.
* Timesyncd: dispatch `systemctl restart systemd-timesyncd`.

The verb deliberately has no idempotency guard — it is operational.

### `enable_sync()` / `disable_sync()`

1. Compute the lifecycle command for the active backend.
2. Dry-run short-circuit.
3. Probe current state via `systemctl is-active <service>` (chrony)
   or `timedatectl show --property=NTP --value` (timesyncd).
4. If state already matches the requested terminal state, return
   `SKIPPED`.
5. Dispatch the lifecycle command.

### `get_drift()`

* Chrony: parse `chronyc tracking`'s `Last offset`, `RMS offset`,
  `Frequency`, and `Stratum` fields.  `Frequency` includes a
  `slow`/`fast` direction token; `slow` flips the sign.
* Timesyncd: parse the `NTPMessage={ ... }` blob's `Offset` and
  `Jitter` tokens.

Daemon-down inactive heuristics apply to chrony only; timesyncd's
`timedatectl` always exits zero even when the unit is inactive.

---

## File-I/O Discipline

* All file I/O routes through the runner via `cat` / `tee`.  This
  module does not consume an `IFileOperator`.  The cross-OS contract
  permits an `IFileOperator` collaborator; this implementation
  follows the locked-in Ubuntu pattern of every other feature in
  the package.
* `_render_chrony_conf_servers` rewrites the entire `server`/`pool`
  block of the chrony config and preserves every other line
  (comments, `driftfile`, `makestep`, `allow`, etc.) verbatim.
* `_render_timesyncd_conf_servers` rewrites the active `NTP=`
  declaration in the `[Time]` section.  When the existing file does
  not contain a `[Time]` header, the renderer appends one.  When
  the existing file already contains a `[Time]` section, the
  renderer inserts the new `NTP=` line directly after the section
  header.

---

## Dry-Run Behaviour

Every write verb honours `config.dry_run`.  Dry-run returns
`status=DRY_RUN` and records the would-have-been command on
`result.command` without invoking the runner.  Dry-run does NOT
short-circuit reads.

---

## Elevation Behaviour

* Reads (`get_time_status`, `get_time_sources`, `get_drift`, the
  `cat` config-file probes, the `systemctl is-active` probe, the
  `timedatectl show --property=NTP` probe) MUST always pass
  `elevated=False`.
* Writes (`tee` against the config file, `systemctl restart`,
  `systemctl enable/disable`, `chronyc makestep`,
  `timedatectl set-ntp`) MUST honour `config.elevated`.

---

## Correctness Properties

1. **Idempotency.** `add_time_source` / `remove_time_source` /
   `set_time_servers` / `enable_sync` / `disable_sync` SKIP when
   the live state already matches the requested state.
2. **Idempotency by file body.** `add_time_source` and
   `remove_time_source` parse the existing config to determine
   membership; they do NOT rewrite an unchanged file.
3. **Reload on success.** Mutations that touch the config file
   restart the matching service on success.  A reload failure
   surfaces as `FAILED` on the mutating verb.
4. **Daemon-down resilience.** `get_time_status` and `get_drift`
   under chrony degrade to empty / inactive snapshots rather than
   raise when the daemon is down.
5. **Backend invariance.** Every verb returns the same result type
   regardless of backend; only the underlying argv changes.
6. **No subprocess.** The manager never constructs its own
   subprocess; every external call routes through the runner.

---

## Testing Strategy

- A `_MockCommandRunner` records every `run` call and returns
  pre-seeded `RunResult` values keyed by argv tuple.
- Parser tests for `chronyc tracking`, `chronyc sources`,
  `timedatectl status`, `timedatectl show --property=...`, and the
  `NTPMessage={ ... }` blob.
- Idempotency tests for `add_time_source` / `remove_time_source` /
  `set_time_servers` driven by pre-seeded `cat` output.
- Idempotency tests for `enable_sync` / `disable_sync` driven by
  pre-seeded `systemctl is-active` output.
- Dry-run tests proving the runner is never invoked.
- Elevation policy tests proving every write call passes
  `elevated=True` and every read call passes `elevated=False`.
- Daemon-down tests proving `get_time_status` returns
  `enabled=False` rather than raising.
- A "failed command surfaces stderr" test proving the captured
  exit code, stderr, and command appear on the result.

---

## Seams With Other Features

| Seam | Owner | This Feature |
|---|---|---|
| chrony service lifecycle | `3-service-management` (canonical) | `enable_sync` / `disable_sync` invoke `systemctl` directly so this verb works without a separate ServiceManager |
| timesyncd service lifecycle | `3-service-management` (canonical) | Same; `force_sync_now` issues a direct `systemctl restart` for the timesyncd backend |
| Timezone | `3-localization-management` (future) | Disjoint surface |
| Per-source PPS hardware | Out of scope | Not addressed |

---

## Spec-vs-Contract Drift

* **`sync_service_active` collapsed.** The contract's
  `TimeStatus.sync_service_active` is collapsed into `enabled` on
  `UbuntuTimeStatus`; both names denote the same property.
* **`last_sync_time` shape.** The contract types
  `last_sync_time: datetime`.  This implementation ships
  `last_sync: Optional[str]` because both backends emit
  human-formatted timestamps and converting to `datetime` would
  force timezone assumptions.  The string is the native backend
  payload.
* **`stratum` and `offset_seconds` on `UbuntuTimeStatus`.** The
  contract does not name `offset_seconds` on the status record; the
  Ubuntu type adds it because chrony exposes the field cheaply.
* **`UbuntuTimeSourceInfo` extends the contract.** Adds `stratum`
  and `last_response` because chrony's columnar output reports them.
* **`UbuntuTimeDrift` field naming.** The contract's
  `drift_ppm`/`rms_offset_seconds` are renamed to
  `frequency_ppm`/`jitter_seconds` to match the actual chrony /
  timesyncd payload semantics; an `offset_seconds` field is the
  primary drift measure.
* **No `IFileOperator`.** The contract permits an `IFileOperator`
  collaborator; this implementation follows the locked-in Ubuntu
  pattern of routing file I/O through the runner via `cat` / `tee`.
