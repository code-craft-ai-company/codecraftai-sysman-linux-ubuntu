# Design

## Overview

`UbuntuResourceControlManager` implements the cross-OS
resource-control-management contract using **systemd slice
units** (cgroups v2) — the same mechanism the canonical RHEL
package uses.  This document describes the module's command
shapes, idempotency probes, parser implementations, and the
mapping between the abstract verb set and Ubuntu's specific
tooling.

The manager is **stateless beyond config**.  Every public verb
takes its inputs from arguments, dispatches commands through the
injected `ICommandRunner`, and returns a result dataclass.  No
hidden disk caches; no hidden subprocess.  This is the same
architectural posture used by every other Ubuntu sysman feature
(scheduledtasks, filesystemquotas, networkconfiguration, …).

---

## Cross-OS Resource-Control Concept Mapping

The cross-OS contract speaks of "resource groups" abstractly.
Each operating system maps that abstract noun onto its own
native concept; this section spells out the mapping for Ubuntu
and shows the divergence points against macOS and Windows for
operators reading multiple sysman packages.

| Concept                   | Ubuntu (this package)                         | RHEL (canonical)                              | macOS                                          | Windows                                        |
|---------------------------|-----------------------------------------------|-----------------------------------------------|------------------------------------------------|------------------------------------------------|
| Group container           | systemd `<name>.slice`                        | systemd `<name>.slice`                        | launchd label `automojo-rc-<name>`             | logical record at `%ProgramData%\Automojo\rcgroups\<name>.json` |
| On-disk representation    | `/etc/systemd/system/<name>.slice`            | `/etc/systemd/system/<name>.slice`            | `/Library/LaunchDaemons/<label>.plist`         | `<group_dir>\<name>.json`                      |
| Kernel substrate          | cgroups v2 at `/sys/fs/cgroup/<name>.slice/`  | cgroups v2 at `/sys/fs/cgroup/<name>.slice/`  | none — per-process QoS via `taskpolicy`        | Job Objects (in-memory, kernel)                |
| CPU ceiling tool          | `systemctl set-property CPUQuota=N%`          | `systemctl set-property CPUQuota=N%`          | unsupported per-group; per-PID `taskpolicy -c` | per-PID `PriorityClass` + `ProcessorAffinity`  |
| Memory ceiling tool       | `systemctl set-property MemoryMax=N`          | `systemctl set-property MemoryMax=N`          | launchd `HardResourceLimits.RLIMIT_AS`         | unsupported via PowerShell                     |
| IO ceiling tool           | `systemctl set-property IOWeight=N` / `IOReadBandwidthMax` | same                          | unsupported per-group; per-PID `taskpolicy -d` | unsupported via PowerShell                     |
| Attach process            | write PID to `cgroup.procs`                   | write PID to `cgroup.procs`                   | `taskpolicy -p <pid>` (best-effort)            | logical record + `Get-Process` apply           |
| Detach process            | write PID to `system.slice/cgroup.procs`      | same                                          | `taskpolicy -p <pid> -c default`               | logical record drop + reset PriorityClass      |
| Usage source              | `cpu.stat`, `memory.current`, `io.stat`       | `systemd-cgtop` + cgroup files                | `top -l 1` aggregated per attached PID         | `Get-Process | Measure-Object` aggregated      |
| Persistent reload         | `systemctl daemon-reload`                     | `systemctl daemon-reload`                     | none required                                  | none required                                  |
| Idempotency anchor        | byte-equal slice file + `systemctl show` props| byte-equal slice file + drop-in compare       | byte-equal plist body                          | byte-equal JSON spec                           |

The Ubuntu mapping is **structurally identical** to RHEL.  The
file-on-disk layout, the kernel substrate, and the tool family
match, so the Ubuntu manager is the most "complete" of the
non-canonical OS implementations: every cross-OS verb has a
real, supported native equivalent.  No verb is a stub, and no
verb returns `FAILED` for "unsupported" reasons.

---

## Component Diagram

```
                    Maintenance coordinator
                            │
                            │  injects runner, config
                            ▼
              UbuntuResourceControlManager
                            │
                            │  .run(argv, elevated=…)
                            ▼
                      ICommandRunner
                            │
              ┌─────────────┴──────────────┐
              ▼                            ▼
       LocalCommandRunner            SSHCommandRunner
              │                            │
              └─────────────┬──────────────┘
                            ▼
                   systemctl / tee / cat / ls / rm
                            │
                            ▼
                  /etc/systemd/system/<name>.slice
                  /sys/fs/cgroup/<name>.slice/...
```

---

## Public Surface

### Class

```python
class UbuntuResourceControlManager(BaseFeatureManager):
    result_class = UbuntuResourceControlActionResult
    status_class = UbuntuResourceControlActionStatus
    feature_name = "resource-control-management"

    def create_group(self, spec: UbuntuResourceGroupSpec) -> UbuntuResourceControlActionResult: ...
    def delete_group(self, name: str) -> UbuntuResourceControlActionResult: ...
    def set_cpu_limit(self, name: str, percent: int) -> UbuntuResourceControlActionResult: ...
    def set_memory_limit(self, name: str, bytes_: int) -> UbuntuResourceControlActionResult: ...
    def set_io_limits(self, name: str, spec: UbuntuIoLimitSpec) -> UbuntuResourceControlActionResult: ...
    def attach_process(self, name: str, pid: int) -> UbuntuResourceControlActionResult: ...
    def detach_process(self, name: str, pid: int) -> UbuntuResourceControlActionResult: ...
    def list_groups(self) -> list[UbuntuResourceGroupInfo]: ...
    def get_group_usage(self, name: str) -> UbuntuResourceGroupUsage: ...
```

### Data models

```python
UbuntuResourceControlActionStatus = ActionStatus

@dataclass
class UbuntuResourceControlManagerConfig(FeatureConfig):
    slice_dir: str = "/etc/systemd/system"
    cgroup_root: str = "/sys/fs/cgroup"

@dataclass
class UbuntuResourceControlActionResult(FeatureActionResult):
    group_name: Optional[str] = None
    pid: Optional[int] = None
    message: Optional[str] = None

@dataclass
class UbuntuIoLimitSpec:
    weight: Optional[int] = None
    read_bps: Optional[int] = None
    write_bps: Optional[int] = None
    device: Optional[str] = None

@dataclass
class UbuntuResourceGroupSpec:
    name: str
    cpu_percent: Optional[int] = None
    memory_bytes: Optional[int] = None
    io_weight: Optional[int] = None
    io_read_bps: Optional[int] = None
    io_write_bps: Optional[int] = None
    attached_pids: list[int] = field(default_factory=list)

@dataclass
class UbuntuResourceGroupInfo:
    name: str
    cpu_percent: Optional[int]
    memory_bytes: Optional[int]
    io_weight: Optional[int]
    io_read_bps: Optional[int]
    io_write_bps: Optional[int]
    attached_pids: list[int]
    is_active: bool
    effective_cpu_percent: Optional[int]
    effective_memory_bytes: Optional[int]

@dataclass
class UbuntuResourceGroupUsage:
    name: str
    cpu_usage_seconds: Optional[float]
    memory_used_bytes: Optional[int]
    io_read_bytes: Optional[int]
    io_write_bytes: Optional[int]
    process_count: int

class UbuntuResourceControlQueryError(Exception):
    ...
```

The `UbuntuResourceControlActionResult` extends the canonical
`FeatureActionResult` shape with `group_name` (every verb is
group-scoped) and `pid` (populated by `attach_process` /
`detach_process` only).  `message` carries the SKIPPED /
FAILED reason token.

---

## Verb Implementations

### `create_group(spec)`

Pseudocode:

```
1. validate spec.name (regex ^[A-Za-z0-9][A-Za-z0-9._\-]*$)
2. render slice-unit body from spec
3. if config.dry_run -> DRY_RUN with the planned `tee` argv
4. existing_body = _read_file("/etc/systemd/system/<name>.slice")
5. is_active = _slice_is_active(name)
6. if existing_body == rendered_body and is_active -> SKIPPED
7. _write_file_via_tee("/etc/systemd/system/<name>.slice", rendered_body)
8. systemctl daemon-reload
9. systemctl start <name>.slice
10. SUCCESS
```

The rendered slice body is:

```ini
[Unit]
Description=automojo-managed resource group: <name>

[Slice]
CPUQuota=<cpu_percent>%        # only when cpu_percent set
MemoryMax=<memory_bytes>       # only when memory_bytes set
IOWeight=<io_weight>           # only when io_weight set
```

Optional `IOReadBandwidthMax` / `IOWriteBandwidthMax` lines are
appended when the spec sets the corresponding fields **and** the
caller has supplied a `device` field on the spec.  Devices are
omitted from the unit body when absent — `IOReadBandwidthMax`
without a device is rejected by systemd, so the manager simply
skips it and surfaces the IO bps via `set_io_limits` once the
device is known.

### `delete_group(name)`

```
1. validate name
2. if config.dry_run -> DRY_RUN
3. if not _slice_file_exists(name) and not _slice_is_active(name) -> SKIPPED
4. systemctl stop <name>.slice  (informational; ignore non-zero)
5. rm /etc/systemd/system/<name>.slice
6. systemctl daemon-reload
7. SUCCESS / FAILED
```

### `set_cpu_limit(name, percent)`

```
1. validate name; validate 0 <= percent <= 100*<n_cpus_unbounded>
2. if config.dry_run -> DRY_RUN
3. current_quota = _get_show_property(name, "CPUQuotaPerSecUSec")
4. if _quota_matches(current_quota, percent) -> SKIPPED
5. systemctl set-property <name>.slice CPUQuota=<percent>%
6. SUCCESS / FAILED
```

systemd reports `CPUQuotaPerSecUSec` in microseconds-per-second
shape (e.g. `500000us` for 50%); the parser converts back to a
percent for the comparison.  When `percent` is None (caller
clearing the cap) the manager dispatches
`CPUQuota=` with an empty value (systemd's documented "remove
the cap" idiom).

### `set_memory_limit(name, bytes_)`

```
1. if config.dry_run -> DRY_RUN
2. current = _get_show_property(name, "MemoryMax")
3. if int(current) == bytes_ -> SKIPPED
4. systemctl set-property <name>.slice MemoryMax=<bytes_>
5. SUCCESS / FAILED
```

### `set_io_limits(name, io_spec)`

The IoLimitSpec carries any combination of `weight`, `read_bps`,
`write_bps`, and `device`.  The manager dispatches **one
`systemctl set-property` per non-None field**, batched into a
single argv.  Idempotency: every requested key must already
match the slice's reported value or the verb runs the dispatch.

### `attach_process(name, pid)`

```
1. validate name; pid > 0
2. if config.dry_run -> DRY_RUN
3. existing_pids = _read_cgroup_procs(name)
4. if pid in existing_pids -> SKIPPED
5. tee --append /sys/fs/cgroup/<name>.slice/cgroup.procs  (stdin=str(pid))
6. SUCCESS / FAILED
```

The cgroup substrate accepts a single PID per write to
`cgroup.procs`; the kernel atomically moves the task to the
requested cgroup, removing it from its previous cgroup as a
side effect.

### `detach_process(name, pid)`

Same shape as `attach_process` except the destination is
`/sys/fs/cgroup/system.slice/cgroup.procs` (the system default
cgroup).  The idempotency guard inspects the slice's
`cgroup.procs` to confirm the PID is present before dispatching;
absent PIDs return `SKIPPED`.

### `list_groups()`

```
1. ls -1 /sys/fs/cgroup/
2. select entries ending in ".slice"
3. for each: systemctl show <name>.slice --property=...
4. parse `key=value` pairs
5. read /sys/fs/cgroup/<name>.slice/cgroup.procs for attached_pids
6. assemble UbuntuResourceGroupInfo per slice
7. return list
```

### `get_group_usage(name)`

```
1. cat /sys/fs/cgroup/<name>.slice/cpu.stat
2. parse usage_usec; usage_seconds = usec / 1_000_000
3. cat /sys/fs/cgroup/<name>.slice/memory.current
4. cat /sys/fs/cgroup/<name>.slice/io.stat ; sum rbytes / wbytes
5. cat /sys/fs/cgroup/<name>.slice/cgroup.procs ; count lines
6. assemble UbuntuResourceGroupUsage; return
```

When any individual `cat` returns a non-zero exit AND a
non-empty stderr, the manager raises
`UbuntuResourceControlQueryError` with the failing argv and
stderr.  When the very first `cat` exits non-zero (the cgroup
directory does not exist), the manager raises with
`detail="unknown_group"`.

---

## Idempotency

| Verb              | Probe                                                                                  | SKIPPED When                                                       |
|-------------------|----------------------------------------------------------------------------------------|--------------------------------------------------------------------|
| `create_group`    | `cat /etc/systemd/system/<name>.slice` + `systemctl is-active <name>.slice`            | rendered body matches AND `is-active` reports `"active"`           |
| `delete_group`    | `test -e /etc/systemd/system/<name>.slice`                                             | file absent AND slice not active                                   |
| `set_cpu_limit`   | `systemctl show <name>.slice --property=CPUQuotaPerSecUSec`                            | parsed percent equals the requested percent                        |
| `set_memory_limit`| `systemctl show <name>.slice --property=MemoryMax`                                     | parsed integer equals the requested bytes                          |
| `set_io_limits`   | `systemctl show <name>.slice --property=IOWeight,IOReadBandwidthMax,IOWriteBandwidthMax` | every requested key already matches                              |
| `attach_process`  | `cat /sys/fs/cgroup/<name>.slice/cgroup.procs`                                         | PID already present                                                |
| `detach_process`  | `cat /sys/fs/cgroup/<name>.slice/cgroup.procs`                                         | PID NOT present                                                    |

Every probe is dispatched with `elevated=False`.  Probes are
tolerated to fail (file not found, slice not loaded, etc.); a
failed probe is read as "the requested state does not yet hold"
and the verb falls through to its mutating step.

---

## Elevation

The manager honours `config.elevated`:

| Method group | `elevated` value passed to runner |
|--------------|------------------------------------|
| writes (`create_group`, `delete_group`, `set_cpu_limit`, `set_memory_limit`, `set_io_limits`, `attach_process`, `detach_process`) | `config.elevated` |
| reads (`list_groups`, `get_group_usage`)                                                                                          | always `False`    |
| internal probes (`cat`, `ls`, `systemctl show`, `test -e`)                                                                        | always `False`    |

`config.elevated` defaults to `False` per the canonical
`FeatureConfig` posture; orchestration code that runs in a
privileged context flips it to `True` once.

---

## Error Reporting

Every write verb funnels failures through the same surface:

```python
UbuntuResourceControlActionResult(
    status=UbuntuResourceControlActionStatus.FAILED,
    detail=name,
    exit_code=run_result.exit_code,
    stdout=run_result.stdout,
    stderr=run_result.stderr,
    command=list(failing_argv),
    group_name=name,
    pid=pid_or_none,
    message="<short_reason_token>",
)
```

The `message` field carries one of:

- `"slice_unit_write_failed"` — `tee` returned non-zero.
- `"daemon_reload_failed"` — `systemctl daemon-reload` returned non-zero.
- `"slice_start_failed"` — `systemctl start <name>.slice` returned non-zero.
- `"slice_stop_failed"` — `systemctl stop <name>.slice` returned non-zero.
- `"slice_unit_delete_failed"` — `rm` returned non-zero.
- `"set_property_failed"` — `systemctl set-property` returned non-zero.
- `"cgroup_procs_write_failed"` — `tee --append` to cgroup.procs returned non-zero.
- `"slice cgroup not present"` — pre-flight detected the slice cgroup directory was missing.

These short tokens are mechanical; the human-readable
explanation lives on `stderr` and `detail`.

---

## Internal helpers

```python
def _validate_name(name: str) -> None
def _validate_pid(pid: int) -> None
def _slice_path(name: str) -> str
def _cgroup_dir(name: str) -> str
def _cgroup_procs_path(name: str) -> str
def _render_slice_body(spec: UbuntuResourceGroupSpec) -> str

def _read_file(path: str) -> Optional[str]
def _path_exists(path: str) -> bool
def _slice_is_active(name: str) -> bool
def _read_cgroup_procs(name: str) -> list[int]
def _get_show_property(name: str, *props: str) -> dict[str, str]

def _parse_show_output(stdout: str) -> dict[str, str]
def _parse_cpu_quota(raw: str) -> Optional[int]
def _parse_memory_max(raw: str) -> Optional[int]
def _parse_cpu_stat(stdout: str) -> Optional[float]
def _parse_memory_current(stdout: str) -> Optional[int]
def _parse_io_stat(stdout: str) -> tuple[int, int]
```

Each helper has a sphinx docstring; `_parse_*` helpers are
stand-alone module-level pure functions so the test module
imports them directly for parser-only tests.

---

## Test plan

- 14+ pytest functions in
  `source/testroots/testbase/automojo/tests/sysman/linux/ubuntu/test_resourcecontrolmanagement.py`.
- Every test uses a `_MockCommandRunner` that records every
  `run` call and returns pre-seeded `RunResult` values keyed by
  argv tuple.  No live `systemctl` / `tee` / `cat` runs.
- Coverage:

| Test                                                  | Covers                                                  |
|-------------------------------------------------------|---------------------------------------------------------|
| `test_create_group_invokes_systemctl`                 | unit-file write + `daemon-reload` + `start`             |
| `test_create_group_skipped_when_present`              | byte-equal body + active                                |
| `test_create_group_dry_run`                           | DRY_RUN short-circuit                                   |
| `test_delete_group_invokes_systemctl`                 | `stop` + `rm` + `daemon-reload`                         |
| `test_delete_group_skipped_when_absent`               | absent file -> SKIPPED                                  |
| `test_set_cpu_limit_invokes_systemctl`                | `set-property CPUQuota=N%`                              |
| `test_set_cpu_limit_skipped_when_match`               | `CPUQuotaPerSecUSec` already matches                    |
| `test_set_memory_limit_invokes_systemctl`             | `set-property MemoryMax=N`                              |
| `test_set_io_limits_invokes_systemctl`                | `IOWeight` + bandwidth combos                           |
| `test_attach_process_invokes_tee`                     | `tee --append cgroup.procs`                             |
| `test_attach_process_skipped_when_present`            | PID already in cgroup.procs                             |
| `test_detach_process_invokes_tee`                     | `tee --append system.slice/cgroup.procs`                |
| `test_list_groups_returns_list`                       | enumerate slices + parse show props                     |
| `test_get_group_usage_parses_cpu_stat`                | parse `cpu.stat` / `memory.current` / `io.stat`         |
| `test_writes_elevate`                                 | `elevated=True` reaches the runner for writes           |
| `test_reads_never_elevate`                            | reads always run with `elevated=False`                  |
| `test_failed_command_surfaces_stderr`                 | non-zero exit -> FAILED with stderr captured            |

---

## Out of Scope

- Nested slice hierarchies beyond a flat single level under
  `/etc/systemd/system/`.
- cgroup v1 compatibility — Ubuntu 22.04+ defaults to v2 and
  Wave-5.3 targets v2.  Hosts booted with
  `systemd.unified_cgroup_hierarchy=0` will surface kernel-side
  errors verbatim through the failed argv on the result.
- Per-device IO weighting beyond `IOReadBandwidthMax` /
  `IOWriteBandwidthMax`.
- Pressure-stall-information (PSI) reporting — orthogonal to
  the cross-OS contract.
- CPU affinity / pinning — owned by `3-process-management`.
- Transient scope creation via `systemd-run --scope` — the
  RHEL package retains that as an extension; the cross-OS
  pilot uses persistent slices only.
