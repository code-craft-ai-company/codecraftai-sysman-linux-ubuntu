# Requirements

## Introduction

The container management feature provides the
`automojo-sysman-linux-ubuntu` package with the ability to pull /
remove images, create / start / stop / remove containers, list and
inspect containers, retrieve container logs, and execute commands
inside running containers on Ubuntu systems.  This spec implements the
container-management contract defined at
`automojo-sysman-common/specs/contracts/container-management/contract.md`
for Ubuntu using either the `docker` CLI (default) or the `podman` CLI,
selected at construction time via
`UbuntuContainerManagerConfig.engine`.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *which* containers should exist, *which*
images should be pulled, or *what* environment / volume / port
configuration a container should have — it provides the capability to
*manipulate container engine state* reliably, idempotently, and with
full auditability.  All container engine commands are executed
exclusively through the `automojo-interfaces` command-runner
abstraction.

Container engine installation (the `docker.io` / `docker-ce` /
`podman` packages themselves) is NOT in scope here — that lives in
`package-management`.  Container registry authentication beyond what
the engine accepts on argv is also NOT in scope; deeper registry
configuration (mirrors, authfile management, certificate trust) is
deferred to per-OS extensions.

---

## Requirements

### Requirement 1: Pull an Image

**User Story:** As a maintenance coordinator, I want to pull a
container image by reference, so that a specific image version is
present locally before any container creation.

#### Acceptance Criteria

1. WHEN the coordinator calls `pull_image(ref)` THE SYSTEM SHALL
   dispatch `<engine> pull <ref>` through the command runner with
   `elevated=True` when configured, where `<engine>` is `docker` or
   `podman` per `UbuntuContainerManagerConfig.engine`.
2. WHEN the reference is already present locally (probed via
   `<engine> images --filter 'reference=<ref>' --format '{{json .}}'`)
   THE SYSTEM SHALL skip execution and return
   `UbuntuContainerActionResult(status=SKIPPED)` UNLESS
   `config.force_pull` is True.
3. WHEN `config.force_pull` is True THE SYSTEM SHALL always invoke
   `<engine> pull` without consulting the idempotency probe.
4. WHEN the pull succeeds THE SYSTEM SHALL return
   `UbuntuContainerActionResult(status=SUCCESS)` populated with
   `target=<ref>`, `image=<ref>`, and `engine=<engine>`.
5. WHEN the pull fails THE SYSTEM SHALL return
   `UbuntuContainerActionResult(status=FAILED)` carrying the runner's
   exit code, stdout, and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuContainerActionResult(status=DRY_RUN)` carrying the
   would-have-been argv.

---

### Requirement 2: Remove an Image

**User Story:** As a maintenance coordinator, I want to remove an
image by reference, so that decommissioned images can be reclaimed.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_image(ref)` THE SYSTEM SHALL
   dispatch `<engine> rmi <ref>` through the command runner with
   `elevated=True` when configured.
2. WHEN the reference is absent locally THE SYSTEM SHALL skip
   execution and return
   `UbuntuContainerActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `UbuntuContainerActionResult(status=SUCCESS)`.
4. WHEN the operation fails THE SYSTEM SHALL return
   `UbuntuContainerActionResult(status=FAILED)` carrying the runner's
   exit code, stdout, and stderr.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuContainerActionResult(status=DRY_RUN)`.

---

### Requirement 3: List Images

**User Story:** As a maintenance coordinator, I want to enumerate
every image present locally, so that I can compare actual image state
against the declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_images()` THE SYSTEM SHALL
   dispatch `<engine> images --format '{{json .}}'` through the
   command runner WITHOUT elevation.
2. THE SYSTEM SHALL parse the response as one JSON object per line and
   emit one `UbuntuImageInfo` per entry.
3. THE SYSTEM SHALL surface `<none>` repository / tag values as
   `None` so callers can distinguish "dangling" images from tagged
   images.
4. WHEN the engine exits non-zero THE SYSTEM SHALL return an empty
   list rather than raise.

---

### Requirement 4: Create a Container

**User Story:** As a maintenance coordinator, I want to create a
container from an image with a declared spec, so that a workload is
provisioned but not yet started.

#### Acceptance Criteria

1. WHEN the coordinator provides an `UbuntuContainerSpec` with `name`
   and `image` THE SYSTEM SHALL dispatch
   `<engine> create --name <name> [<flags...>] <image> [<argv...>]`
   through the command runner with `elevated=True` when configured.
2. THE SYSTEM SHALL translate `spec.env`, `spec.volumes`, `spec.ports`,
   `spec.networks`, `spec.restart_policy`, `spec.working_dir`, and
   `spec.user` into the corresponding `-e`, `-v`, `-p`, `--network`,
   `--restart`, `--workdir`, and `--user` flags.
3. WHEN a container of the same `name` already exists with the same
   `image` (probed via `<engine> inspect <name>`) THE SYSTEM SHALL
   skip execution and return
   `UbuntuContainerActionResult(status=SKIPPED)`.
4. WHEN the operation succeeds THE SYSTEM SHALL return
   `UbuntuContainerActionResult(status=SUCCESS)` populated with
   `container_id` taken from stdout (the engine prints the new id).
5. WHEN the operation fails THE SYSTEM SHALL return
   `UbuntuContainerActionResult(status=FAILED)` carrying the runner's
   exit code, stdout, and stderr.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuContainerActionResult(status=DRY_RUN)`.

---

### Requirement 5: Start a Container

**User Story:** As a maintenance coordinator, I want to start an
existing container by id or name, so that a created container begins
running.

#### Acceptance Criteria

1. WHEN the coordinator calls `start_container(container_id)` THE
   SYSTEM SHALL dispatch `<engine> start <container_id>` through the
   command runner with `elevated=True` when configured.
2. WHEN the container is already running (probed via
   `<engine> inspect`) THE SYSTEM SHALL skip execution and return
   `UbuntuContainerActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `UbuntuContainerActionResult(status=SUCCESS)`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuContainerActionResult(status=DRY_RUN)`.

---

### Requirement 6: Stop a Container

**User Story:** As a maintenance coordinator, I want to stop a running
container with a configurable timeout, so that workloads can be quiesced
gracefully.

#### Acceptance Criteria

1. WHEN the coordinator calls `stop_container(container_id)` THE
   SYSTEM SHALL dispatch
   `<engine> stop -t <default_stop_timeout> <container_id>` through
   the command runner with `elevated=True` when configured.
2. WHEN the container is already stopped (probed via
   `<engine> inspect`) THE SYSTEM SHALL skip execution and return
   `UbuntuContainerActionResult(status=SKIPPED)`.
3. WHEN the container does not exist THE SYSTEM SHALL skip execution
   and return `UbuntuContainerActionResult(status=SKIPPED)` because
   the contract treats absent containers as "no work to do" for stop.
4. WHEN the operation succeeds THE SYSTEM SHALL return
   `UbuntuContainerActionResult(status=SUCCESS)`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuContainerActionResult(status=DRY_RUN)`.

---

### Requirement 7: Remove a Container

**User Story:** As a maintenance coordinator, I want to remove a
container by id or name, so that decommissioned workloads can be
purged.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_container(container_id)` THE
   SYSTEM SHALL dispatch `<engine> rm -f <container_id>` through the
   command runner with `elevated=True` when configured.  The `-f`
   flag is always passed so a running container is stopped and
   removed atomically.
2. WHEN no container of that id / name exists THE SYSTEM SHALL skip
   execution and return
   `UbuntuContainerActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `UbuntuContainerActionResult(status=SUCCESS)`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuContainerActionResult(status=DRY_RUN)`.

---

### Requirement 8: List Containers

**User Story:** As a maintenance coordinator, I want to enumerate
every container (running, or running + stopped), so that I can compare
actual container state against the declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_containers(all=True)` THE SYSTEM
   SHALL dispatch `<engine> ps -a --format '{{json .}}'` through the
   command runner WITHOUT elevation.
2. WHEN the coordinator calls `list_containers(all=False)` (the
   default) THE SYSTEM SHALL dispatch `<engine> ps --format '{{json .}}'`
   so only running containers are returned.
3. THE SYSTEM SHALL parse the response as one JSON object per line and
   emit one `UbuntuContainerInfo` per entry.
4. WHEN the engine exits non-zero THE SYSTEM SHALL return an empty
   list rather than raise.

---

### Requirement 9: Inspect a Container

**User Story:** As a maintenance coordinator, I want to inspect a
single container by id or name, so that I can branch on its state,
mounts, ports, and command without parsing `ps` output myself.

#### Acceptance Criteria

1. WHEN the coordinator calls `inspect_container(container_id)` THE
   SYSTEM SHALL dispatch `<engine> inspect <container_id>` through
   the command runner WITHOUT elevation.
2. THE SYSTEM SHALL parse the JSON ARRAY response and pick element 0.
3. THE SYSTEM SHALL return an `UbuntuContainerInfo` populated with
   `id`, `name` (with the engine's leading `/` stripped), `image`,
   `status`, `state`, `created`, `started`, `ports`, `mounts`, and
   `command`.
4. WHEN the container does not exist (engine exit code non-zero) THE
   SYSTEM SHALL return `None`.

---

### Requirement 10: Get Container Logs

**User Story:** As a maintenance coordinator, I want to fetch the tail
of a container's log, so that diagnostics can be retrieved without
shelling into the host.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_container_logs(container_id, lines)`
   THE SYSTEM SHALL dispatch
   `<engine> logs --tail <lines> <container_id>` through the command
   runner WITHOUT elevation.
2. THE SYSTEM SHALL return an `UbuntuContainerLogs` populated with
   `container_id`, `lines` (one entry per log line), and
   `truncated=True` (because the `--tail` flag was applied).
3. WHEN `lines` is omitted THE SYSTEM SHALL default to 100 (matching
   the contract's "advisory line limit" guidance).
4. WHEN the engine exits non-zero THE SYSTEM SHALL return an empty
   `UbuntuContainerLogs` rather than raise.

---

### Requirement 11: Execute a Command in a Container

**User Story:** As a maintenance coordinator, I want to run a command
inside a running container, so that diagnostics and admin tasks can be
performed without shelling into the host.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `exec_in_container(container_id, argv)` THE SYSTEM SHALL dispatch
   `<engine> exec <container_id> <argv...>` through the command
   runner with `elevated=True` when configured.
2. THE SYSTEM SHALL return an `UbuntuContainerExecResult` populated
   with `container_id`, `command_argv`, `output`, `exit_code`, and
   the standard `stdout` / `stderr` fields.
3. THE SYSTEM SHALL NOT skip — `exec_in_container` is operational and
   has no idempotency guard.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `UbuntuContainerExecResult(status=DRY_RUN)`.

---

### Requirement 12: Engine Selection and Elevation Policy

**User Story:** As a maintenance coordinator, I want a single
`UbuntuContainerManager` API regardless of whether the host runs
Docker or rootless Podman, so that infrastructure code does not
hard-code an engine.

#### Acceptance Criteria

1. WHEN `config.engine` is `UbuntuContainerEngine.DOCKER` THE SYSTEM
   SHALL invoke the `docker` binary for every verb.
2. WHEN `config.engine` is `UbuntuContainerEngine.PODMAN` THE SYSTEM
   SHALL invoke the `podman` binary for every verb.  Argv shape is
   identical because both engines accept the verbs and flags this
   manager constructs.
3. WHEN `config.elevated` is True THE SYSTEM SHALL pass
   `elevated=True` to the runner for every write verb (`pull_image`,
   `remove_image`, `create_container`, `start_container`,
   `stop_container`, `remove_container`, `exec_in_container`).
4. WHEN performing read-only operations (`list_images`,
   `list_containers`, `inspect_container`, `get_container_logs`, all
   idempotency probes) THE SYSTEM SHALL NEVER pass `elevated=True`.
5. WHEN `config.elevated` is False THE SYSTEM SHALL NEVER pass
   `elevated=True`.
