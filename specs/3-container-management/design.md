# Design

## Concept Mapping (Docker / Podman ↔ macOS Docker Desktop ↔ Windows Docker Desktop)

The cross-OS container-management contract talks about *images* and
*containers*.  Ubuntu's engine surface is exceptionally clean: both
Docker and Podman speak the same verb set and accept identical argv,
which lets the manager reduce to a "binary swap" between the two CLIs.
The table below grounds the abstract contract in concrete Ubuntu /
macOS / Windows terms so a reader can compare the three Wave-2.1
implementations side-by-side.

| Contract concept | Ubuntu (this spec) | macOS | Windows |
|---|---|---|---|
| Engine | `docker` (default) or `podman` on PATH | `docker` (Docker Desktop or colima) or `podman` | `docker.exe` (Docker Desktop) or `podman.exe` / `wsl podman` |
| Image storage | `/var/lib/docker` (rootful Docker) or `~/.local/share/containers/storage` (rootless Podman) | Docker Desktop VM disk; colima VM disk | Docker Desktop WSL2 backend disk |
| Image reference | `<registry>/<repo>:<tag>` or `<repo>@sha256:<digest>` (digest pinned) | identical | identical |
| Container id | Engine-assigned hex id | identical | identical |
| Container name | Optional `--name`; engine generates one when omitted | identical | identical |
| Volume mount syntax | `<host>:<container>[:opts]` (`-v /srv:/data`) | identical (POSIX paths) | `<host>:<container>[:opts]` with backslash-quoted Windows paths (`-v 'C:\\srv:/data'`) |
| Port mapping syntax | `<host>:<container>[/proto]` (`-p 8080:80/tcp`) | identical | identical |
| Network selection | `--network <name>` | identical | identical |
| Restart policy | `--restart=<policy>` (`no` / `on-failure` / `always` / `unless-stopped`) | identical | identical |
| Elevation | `sudo docker ...` rootful; rootless Podman needs no sudo; Docker users in `docker` group skip sudo | None — Docker Desktop runs as the desktop user | None — Docker Desktop runs as the desktop user |

The Ubuntu and macOS implementations are nearly identical — both
target the `docker` / `podman` CLIs against a Linux container runtime
(via WSL2 / Hyperkit / colima / native).  The Windows implementation
reuses the same argv but invokes `docker.exe` and documents the
Windows path-quoting requirement for volume mounts.

---

## Overview

The container-management feature is implemented as a single module,
`automojo.sysman.linux.ubuntu.containermanagement`, that exposes an
`UbuntuContainerManager` class.  The coordinator instantiates
`UbuntuContainerManager` and calls its methods to perform discrete
container engine actions: image pull / remove / list, container
create / start / stop / remove, container list / inspect, log fetch,
and exec.

This feature **complements — and does not duplicate** — package
installation of the engine (`package-management`), system service
control of the daemon (`service-management`), or registry trust
configuration (per-OS extension).  The `UbuntuContainerManager` brings
images and containers into the desired shape; everything outside that
verb set lives in another feature.

All command execution is delegated to a command runner implementing
the `ICommandRunner` interface.  The `UbuntuContainerManager` never
directly calls `subprocess`, shell commands, or `open()`; it only
constructs argument lists and hands them to the injected runner.
This keeps business logic separate from execution and makes the
feature fully testable via mock dependencies without root access or a
live engine.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).
Under SSH the coordinator can track reliability metrics and apply
execution aspects (persistence, retry, timeout) without any changes
to the `UbuntuContainerManager` code.

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Selects engine (DOCKER or PODMAN)                        │
│  - Injects runner into UbuntuContainerManager               │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuContainerManager                           │
│  automojo.sysman.linux.ubuntu.containermanagement           │
│                                                             │
│  pull_image()           remove_image()                      │
│  list_images()                                              │
│  create_container()     start_container()                   │
│  stop_container()       remove_container()                  │
│  list_containers()      inspect_container()                 │
│  get_container_logs()   exec_in_container()                 │
└─────────┬───────────────────────────────────────────────────┘
          │ ICommandRunner
          ▼
┌─────────────────────────────────────────────────────────────┐
│               LocalCommandRunner / SSHCommandRunner         │
└─────────┬───────────────────────────────────────────────────┘
          │ optional aspect composition
          ▼
┌─────────────────────────────────────────────────────────────┐
│   PersistenceAspect  RetryAspect  TimeoutAspect             │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `UbuntuContainerManager`

**Location:**
`source/packages/automojo/sysman/linux/ubuntu/containermanagement.py`

| Method | Signature | Description |
|---|---|---|
| `pull_image` | `(ref) → UbuntuContainerActionResult` | `<engine> pull <ref>`; idempotent via `<engine> images --filter`; bypassed when `force_pull` |
| `remove_image` | `(ref) → UbuntuContainerActionResult` | `<engine> rmi <ref>`; idempotent via `<engine> images --filter` |
| `list_images` | `() → list[UbuntuImageInfo]` | `<engine> images --format '{{json .}}'`; one JSON per line |
| `create_container` | `(spec) → UbuntuContainerActionResult` | `<engine> create --name <name> [<flags...>] <image> [<argv...>]`; idempotent via `<engine> inspect <name>` |
| `start_container` | `(container_id) → UbuntuContainerActionResult` | `<engine> start <id>`; idempotent via `<engine> inspect`'s `State.Running` |
| `stop_container` | `(container_id) → UbuntuContainerActionResult` | `<engine> stop -t <timeout> <id>`; idempotent via `<engine> inspect`'s `State.Running` |
| `remove_container` | `(container_id) → UbuntuContainerActionResult` | `<engine> rm -f <id>`; idempotent via `<engine> inspect` |
| `list_containers` | `(all=False) → list[UbuntuContainerInfo]` | `<engine> ps [-a] --format '{{json .}}'`; one JSON per line |
| `inspect_container` | `(container_id) → UbuntuContainerInfo \| None` | `<engine> inspect <id>` (JSON array; element 0); returns `None` on absence |
| `get_container_logs` | `(container_id, lines=100) → UbuntuContainerLogs` | `<engine> logs --tail <lines> <id>` |
| `exec_in_container` | `(container_id, argv) → UbuntuContainerExecResult` | `<engine> exec <id> <argv...>`; never skips |

### `UbuntuContainerManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no command is dispatched; the would-have-been argv is recorded |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `engine` | `UbuntuContainerEngine` | `DOCKER` | Engine to invoke |
| `force_pull` | `bool` | `False` | When `True`, `pull_image` always invokes `pull` even when the reference is already present |
| `default_stop_timeout` | `int` | `10` | Default `-t` value passed to `<engine> stop` |

---

## Data Models

### Result Types

```python
class UbuntuContainerActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

class UbuntuContainerEngine(Enum):
    DOCKER = "docker"
    PODMAN = "podman"

@dataclass
class UbuntuContainerActionResult:
    status: UbuntuContainerActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    engine: UbuntuContainerEngine | None
    container_id: str | None
    image: str | None
    target: str | None
    message: str | None

@dataclass
class UbuntuContainerExecResult:
    status: UbuntuContainerActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None              # outer engine argv
    engine: UbuntuContainerEngine | None
    container_id: str | None
    command_argv: list[str] | None         # inner argv
    output: str | None                     # mirror of stdout
```

### Image / Container Types

```python
@dataclass
class UbuntuImageInfo:
    id: str
    repository: str | None
    tag: str | None
    digest: str | None
    size_bytes: int | None
    created: str

@dataclass
class UbuntuContainerSpec:
    name: str
    image: str
    argv: list[str] | None = None
    env: dict[str, str] | None = None
    volumes: list[str] | None = None
    ports: list[str] | None = None
    networks: list[str] | None = None
    restart_policy: str | None = None
    working_dir: str | None = None
    user: str | None = None
    detach: bool = True

@dataclass
class UbuntuContainerInfo:
    id: str
    name: str
    image: str
    status: str
    state: str                             # running / exited / created / paused
    created: str
    started: str | None
    ports: list[str]
    mounts: list[str]
    command: list[str]
    engine: UbuntuContainerEngine | None

@dataclass
class UbuntuContainerLogs:
    container_id: str
    lines: list[str]
    truncated: bool
```

### Exceptions

```python
class UbuntuContainerQueryError(Exception):
    """Raised when an inspect / list query fails unexpectedly."""

class UbuntuContainerEngineNotFoundError(Exception):
    """Raised when the configured engine binary is not on PATH."""

class UbuntuContainerNotFoundError(Exception):
    """Raised when a verb is given an unknown id and the OS surface
       requires raising rather than skipping (cross-OS-contract
       parity; not raised by the public verbs today)."""
```

---

## Engine Mapping

| `UbuntuContainerEngine` | CLI binary | Notes |
|---|---|---|
| `DOCKER` | `docker` | Default.  Requires the Docker daemon to be running and the calling user to be in the `docker` group (or `elevated=True`). |
| `PODMAN` | `podman` | Rootless by default on modern Ubuntu.  Same argv as Docker for the verbs in this contract. |

Engine binary resolution is purely PATH-based — the manager passes the
binary name as argv[0] and lets the runner / OS resolve.  The
manager never probes the engine on construction; binary-missing
errors surface on the first verb call as a `FAILED` result with the
runner's stderr.

---

## Verb / Command Mappings

### Write Commands (elevation honoured)

| Operation | Argv |
|---|---|
| `pull_image` | `<engine> pull <ref>` |
| `remove_image` | `<engine> rmi <ref>` |
| `create_container` | `<engine> create --name <name> [--restart=<policy>] [--user <u>] [--workdir <d>] [-e <kv> ...] [-v <vol> ...] [-p <port> ...] [--network <n> ...] <image> [<argv...>]` |
| `start_container` | `<engine> start <id>` |
| `stop_container` | `<engine> stop -t <timeout> <id>` |
| `remove_container` | `<engine> rm -f <id>` |
| `exec_in_container` | `<engine> exec <id> <argv...>` |

### Read Commands (no elevation)

| Operation | Argv |
|---|---|
| `list_images` | `<engine> images --format '{{json .}}'` |
| `list_containers` | `<engine> ps [-a] --format '{{json .}}'` |
| `inspect_container` | `<engine> inspect <id>` |
| `get_container_logs` | `<engine> logs --tail <lines> <id>` |
| `image_present` probe | `<engine> images --filter 'reference=<ref>' --format '{{json .}}'` |
| `inspect_raw` probe | `<engine> inspect <id>` |

All writes pass `elevated=True` when the config enables it.  Reads
never elevate.

### Argv Construction Order for `create_container`

The `_build_create_argv` helper enforces a canonical flag order so
test assertions stay stable:

1. `<engine> create --name <name>`
2. `--restart=<policy>` (when set)
3. `--user <user>` (when set)
4. `--workdir <dir>` (when set)
5. `-e KEY=VALUE` (one per env entry, in dict-iteration order)
6. `-v <volume_token>` (one per volume)
7. `-p <port_token>` (one per port)
8. `--network <name>` (one per network)
9. `<image>`
10. `<argv...>` (the override command, if any)

---

## Output Parsing

### `<engine> images --format '{{json .}}'`

```text
{"ID":"sha256:abc","Repository":"nginx","Tag":"1.25","Digest":"sha256:def","Size":12345678,"CreatedAt":"2025-01-01T00:00:00Z"}
{"ID":"sha256:xyz","Repository":"<none>","Tag":"<none>","Digest":"<none>","Size":99,"CreatedAt":"2025-01-02T00:00:00Z"}
```

* One JSON object per line.  The parser splits on newlines and
  decodes each line independently so a malformed line does not
  poison the rest.
* `<none>` repository / tag / digest values are coerced to `None`
  so callers can distinguish "dangling" images from tagged ones.
* `Size` is reported as int by Podman and as a human string by
  Docker.  The parser tolerates both and falls back to `None` when
  unparseable.

### `<engine> ps --format '{{json .}}'`

```text
{"ID":"abc","Names":["web"],"Image":"nginx:1.25","Status":"Up 2 hours","State":"running","CreatedAt":"2025-02-01T11:55:00Z","Ports":"0.0.0.0:8080->80/tcp","Mounts":"","Command":"nginx -g daemon off;"}
```

* `Names` may be either a list (Podman) or a string (older Docker
  versions).  The parser accepts both.
* `Ports` and `Mounts` may be either a list or a comma-separated
  string.  The parser splits the string form on `,` and trims.
* `Command` may be either a list or a string.  The parser wraps a
  string in a single-element list.

### `<engine> inspect <id>`

```json
[
  {
    "Id": "abc",
    "Name": "/web",
    "Config": {
      "Image": "nginx:1.25",
      "Cmd": ["nginx", "-g", "daemon off;"]
    },
    "State": {
      "Running": true,
      "Status": "running",
      "StartedAt": "2025-02-01T12:00:00Z"
    },
    "Created": "2025-02-01T11:55:00Z",
    "NetworkSettings": {"Ports": {"80/tcp": []}},
    "Mounts": [{"Destination": "/data"}]
  }
]
```

* The response is a JSON ARRAY; the parser picks element 0.
* `Name` carries a leading `/` (Docker convention); the parser
  strips it.
* `Mounts[*].Destination` is the in-container path; the parser
  collects them into the `mounts` list (the contract field is
  per-OS-flexible, and this implementation uses the destination).

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `pull_image` | Reference already locally present | `<engine> images --filter 'reference=<ref>'` |
| `remove_image` | Reference not present | `<engine> images --filter 'reference=<ref>'` |
| `create_container` | Container of same name + image exists | `<engine> inspect <name>` |
| `start_container` | `State.Running == True` | `<engine> inspect <id>` |
| `stop_container` | `State.Running == False` (or container absent) | `<engine> inspect <id>` |
| `remove_container` | Container absent | `<engine> inspect <id>` |
| `exec_in_container` | None — operational, always runs | n/a |

`pull_image`'s guard is bypassed when `config.force_pull` is True so
coordinators driving "always re-pull" workflows (e.g. floating
`:latest` tags) can opt in.

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `UbuntuContainerQueryError` | Reserved for stricter read paths added in a follow-up batch.  Wave-2.1 reads return empty / `None` on probe failure to mirror the Wave-1 Ubuntu pattern. |
| `UbuntuContainerEngineNotFoundError` | Reserved for a future strict-engine-probe mode.  Currently a missing engine surfaces as `FAILED` with the runner's stderr. |
| `UbuntuContainerNotFoundError` | Reserved for cross-OS-contract parity.  Not raised by the public verbs today (absence is treated as `SKIPPED`). |

All write failures are surfaced as
`UbuntuContainerActionResult(status=FAILED, ...)` carrying the
runner's exit code, stdout, and stderr.  Writes never raise for
routine I/O.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true
across all valid executions of a system.*

### Property 1: pull_image Argv

*For any* `pull_image(ref=R)` call in a state where `R` is not
locally present, the runner sees exactly
`["<engine>", "pull", R]`.

**Validates: Requirements 1.1**

### Property 2: pull_image Idempotency

*For any* `pull_image(ref=R)` call in a state where `R` is locally
present and `force_pull=False`, the manager MUST return SKIPPED and
NOT dispatch `pull`.

**Validates: Requirements 1.2**

### Property 3: pull_image Force Override

*For any* `pull_image(ref=R)` call with `force_pull=True`, the manager
MUST dispatch `pull` regardless of whether `R` is locally present.

**Validates: Requirements 1.3**

### Property 4: remove_image Argv

*For any* `remove_image(ref=R)`, the runner sees
`["<engine>", "rmi", R]`.

**Validates: Requirements 2.1**

### Property 5: list_images Parses JSON Lines

*For any* engine output of N JSON objects (one per line),
`list_images()` MUST return N `UbuntuImageInfo` rows whose `id`
matches the engine's `ID` field, in the engine's order.

**Validates: Requirements 3.2**

### Property 6: create_container Argv Skeleton

*For any* `UbuntuContainerSpec(name=N, image=I)`,
`create_container()` MUST dispatch an argv that begins
`["<engine>", "create", "--name", N, ...]` and ends with `[..., I]`
or `[..., I, *spec.argv]`.

**Validates: Requirements 4.1, 4.2**

### Property 7: create_container Idempotency

*For any* `create_container(spec)` where a container of
`spec.name` already exists with `Config.Image == spec.image`, the
manager MUST return SKIPPED and NOT dispatch `create`.

**Validates: Requirements 4.3**

### Property 8: start_container Idempotency

*For any* `start_container(id)` where `State.Running == True`, the
manager MUST return SKIPPED and NOT dispatch `start`.

**Validates: Requirements 5.2**

### Property 9: stop_container Argv

*For any* `stop_container(id=I)`, the runner sees
`["<engine>", "stop", "-t", str(default_stop_timeout), I]`.

**Validates: Requirements 6.1**

### Property 10: stop_container Idempotency

*For any* `stop_container(id)` where `State.Running == False` or the
container is absent, the manager MUST return SKIPPED and NOT
dispatch `stop`.

**Validates: Requirements 6.2, 6.3**

### Property 11: remove_container Argv

*For any* `remove_container(id=I)`, the runner sees
`["<engine>", "rm", "-f", I]`.

**Validates: Requirements 7.1**

### Property 12: list_containers Includes Stopped When all=True

*For any* `list_containers(all=True)`, the runner sees an argv that
contains `"-a"`.  *For any* `list_containers(all=False)`, the
runner's argv MUST NOT contain `"-a"`.

**Validates: Requirements 8.1, 8.2**

### Property 13: inspect_container Returns Element Zero

*For any* `inspect_container(id)` whose engine response is a
non-empty JSON array, the manager returns an
`UbuntuContainerInfo` derived from element 0.

**Validates: Requirements 9.2**

### Property 14: inspect_container Returns None on Absence

*For any* `inspect_container(id)` whose engine exits non-zero, the
manager returns `None`.

**Validates: Requirements 9.4**

### Property 15: get_container_logs Sets truncated

*For any* `get_container_logs(id, lines)`, the returned record's
`truncated` field MUST be True.

**Validates: Requirements 10.2**

### Property 16: exec_in_container Never Skips

*For any* `exec_in_container(id, argv)` invocation outside dry-run
mode, the manager MUST dispatch `<engine> exec` even on consecutive
identical calls.

**Validates: Requirements 11.3**

### Property 17: Engine Selection

*For any* config `engine=UbuntuContainerEngine.PODMAN`, every
verb's argv[0] MUST equal `"podman"`.  Equivalent for `DOCKER`.

**Validates: Requirements 12.1, 12.2**

### Property 18: Dry-Run Safety

*For any* write verb invoked with `dry_run=True`, the runner MUST
NOT be invoked and the result status MUST be `DRY_RUN`.

**Validates: Requirements 1.6, 2.5, 4.6, 5.4, 6.5, 7.4, 11.4**

### Property 19: Elevation for Writes

*For any* write verb with `elevated=True` in the config, every
runner invocation MUST receive `elevated=True`.

**Validates: Requirements 12.3**

### Property 20: No Elevation for Reads

*For any* read verb (and any idempotency probe), the runner MUST
NEVER be invoked with `elevated=True`, regardless of config.

**Validates: Requirements 12.4**

### Property 21: Error Surface

*For any* non-zero exit from a write command, the returned
`UbuntuContainerActionResult` MUST have `status=FAILED` and carry
the runner's exit code and stderr.

**Validates: Requirements 1.5, 2.4, 4.5**

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/
├── __init__.py
├── featurebase.py
├── containermanagement.py    ← UbuntuContainerManager + types
└── storagemanagement.py

source/testroots/testbase/automojo/tests/sysman/linux/ubuntu/
└── test_containermanagement.py
```

---

## Usage Examples

### Local Execution (Default — Docker)

```python
from automojo.sysman.linux.ubuntu.runners import (
    LocalCommandRunner,
    LocalRunnerConfig,
)
from automojo.sysman.linux.ubuntu.containermanagement import (
    UbuntuContainerEngine,
    UbuntuContainerManager,
    UbuntuContainerManagerConfig,
    UbuntuContainerSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = UbuntuContainerManagerConfig(
    engine=UbuntuContainerEngine.DOCKER,
    elevated=False,
    dry_run=False,
)
containers = UbuntuContainerManager(runner=runner, config=config)

# Pull, create, and start an nginx container
containers.pull_image(ref="nginx:1.25")
spec = UbuntuContainerSpec(
    name="web",
    image="nginx:1.25",
    ports=["8080:80"],
    restart_policy="unless-stopped",
)
containers.create_container(spec=spec)
containers.start_container(container_id="web")

# Inspect runtime state
info = containers.inspect_container(container_id="web")
print(info.state, info.started, info.ports)

# Tail the last 50 log lines
logs = containers.get_container_logs(container_id="web", lines=50)
for line in logs.lines:
    print(line)

# Exec a diagnostic command
exec_result = containers.exec_in_container(
    container_id="web", argv=["nginx", "-V"],
)
print(exec_result.stderr)   # nginx prints its build info on stderr
```

### Switching to Podman

```python
config = UbuntuContainerManagerConfig(
    engine=UbuntuContainerEngine.PODMAN,
)
containers = UbuntuContainerManager(runner=runner, config=config)
# Same verbs; argv[0] becomes "podman" instead of "docker".
```

### Dry-Run Mode

```python
config = UbuntuContainerManagerConfig(dry_run=True)
containers = UbuntuContainerManager(runner=runner, config=config)

result = containers.remove_container(container_id="web")
assert result.status == UbuntuContainerActionStatus.DRY_RUN
print(result.command)   # ["docker", "rm", "-f", "web"]
```

### SSH Execution with Aspects

```python
from automojo.sysman.linux.ubuntu.runners import (
    SSHCommandRunner,
    SSHRunnerConfig,
)
from automojo.sysman.linux.ubuntu.aspects import (
    PersistenceAspect,
    PersistenceAspectConfig,
    RetryAspect,
    RetryAspectConfig,
)
from automojo.sysman.linux.ubuntu.storage import SQLiteStorage

runner = SSHCommandRunner(
    SSHRunnerConfig(host="docker-host-1", username="admin"),
)
storage_db = SQLiteStorage(path="/var/log/maintenance/commands.db")
runner.add_aspect(
    aspect=PersistenceAspect(
        PersistenceAspectConfig(storage_backend=storage_db),
    ),
)
runner.add_aspect(aspect=RetryAspect(RetryAspectConfig(max_retries=2)))

containers = UbuntuContainerManager(
    runner=runner,
    config=UbuntuContainerManagerConfig(),
)
for c in containers.list_containers(all=True):
    print(c.id, c.name, c.state)
```

---

## Testing Strategy

* All tests use a `MockCommandRunner` keyed on argv tuples.  Both
  write argv assertions and idempotency-probe seeding share the same
  fixture builders (`_seed_image_present`, `_seed_inspect_record`,
  `_seed_inspect_absent`).
* Argv-construction tests assert the exact argv shape for every
  write verb: `["docker", "pull", "<ref>"]`,
  `["docker", "rm", "-f", "<id>"]`, etc.
* Idempotency tests cover every verb's SKIPPED path (image present /
  absent, container present / absent, container running / stopped).
* Dry-run tests prove the runner is not invoked and the recorded
  `command` carries the would-have-been argv.
* Engine-selection tests prove that switching to `PODMAN` flips
  argv[0] for every verb.
* Elevation tests cover every write verb passing `elevated=True` and
  every read verb passing `elevated=False`, regardless of config.
* Parser tests cover `images`, `ps`, and `inspect` JSON shapes
  including the `<none>` repository / tag edge cases and the
  string-vs-list `Names` / `Ports` / `Mounts` / `Command` variants.
* Failure surface is tested by seeding the runner with a non-zero
  exit; the result MUST be `FAILED` with stderr captured.
* No test requires root access or a live engine.

---

## Scope Mapping

| Contract verb | Ubuntu implementation |
|---|---|
| `pull_image` | `<engine> pull <ref>` |
| `remove_image` | `<engine> rmi <ref>` |
| `list_images` | `<engine> images --format '{{json .}}'` |
| `create_container` | `<engine> create --name <name> [<flags...>] <image> [<argv...>]` |
| `start_container` | `<engine> start <id>` |
| `stop_container` | `<engine> stop -t <timeout> <id>` |
| `remove_container` | `<engine> rm -f <id>` |
| `list_containers` | `<engine> ps [-a] --format '{{json .}}'` |
| `inspect_container` | `<engine> inspect <id>` (JSON array; element 0) |
| `get_container_logs` | `<engine> logs --tail <lines> <id>` |
| `exec_in_container` | `<engine> exec <id> <argv...>` |

---

## Extensions Beyond the Contract

None in this Ubuntu implementation.  Image builds (`docker build` /
`podman build`), Compose / Quadlet orchestration, Pod constructs,
network and volume management beyond the references in
`UbuntuContainerSpec`, registry auth (`docker login` / `podman login`),
registry mirror configuration, and Kubernetes integration (kind /
minikube / k3s) are intentionally deferred.  When added they will live
behind their own feature flags so a coordinator writing against the
contract does not have to special-case Ubuntu.

---

## Future Considerations

* Add a strict-read mode that raises `UbuntuContainerQueryError`
  instead of returning empty lists / `None`.  The exception is
  defined; only the call sites need to elect the strict variant.
* Surface a `force` parameter on `remove_image` so callers can
  request `<engine> rmi -f <ref>` (currently a dependent container
  causes the unforced rmi to fail; the failure surfaces as
  `FAILED`).
* Honour `UbuntuContainerSpec.detach` by branching to `<engine> run`
  (with its blocking semantics) when False, instead of always going
  through the create / start flow.
* Add a registry-login verb under the same manager so coordinators
  can avoid layering an extra package just for `docker login` /
  `podman login`.  Credential routing through stdin is in the
  cross-OS contract.
* Add an explicit engine-availability probe so
  `UbuntuContainerEngineNotFoundError` becomes a real exception
  rather than a documented contingency.
