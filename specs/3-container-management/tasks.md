# Implementation Plan — Ubuntu Container Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the
  `ccai.sysman.linux.ubuntu.containermanagement` module.

- [x] 2. Define `UbuntuContainerActionStatus` (aliased to the shared
  `featurebase.ActionStatus`).

- [x] 3. Define `UbuntuContainerEngine` enum with `DOCKER` (default)
  and `PODMAN`.

- [x] 4. Define `UbuntuContainerManagerConfig` dataclass with
  `engine`, `force_pull`, and `default_stop_timeout` extras on top
  of the base `dry_run` / `elevated`.

- [x] 5. Define `UbuntuContainerActionResult` dataclass with
  `engine` / `container_id` / `image` / `target` / `message`
  extras.

- [x] 6. Define `UbuntuContainerExecResult` dataclass with
  `engine` / `container_id` / `command_argv` / `output` extras.

- [x] 7. Define `UbuntuImageInfo`, `UbuntuContainerSpec`,
  `UbuntuContainerInfo`, `UbuntuContainerLogs` dataclasses.

- [x] 8. Define `UbuntuContainerQueryError`,
  `UbuntuContainerEngineNotFoundError`,
  `UbuntuContainerNotFoundError` exceptions (defined; not yet
  raised — see deviation note).

## UbuntuContainerManager — Implemented

- [x] 9. `__init__` accepting runner + optional config (inherited
  from `BaseFeatureManager`).

- [x] 10. `pull_image(ref)` — `<engine> pull <ref>`; idempotent via
  `<engine> images --filter`; bypassed by `config.force_pull`.

- [x] 11. `remove_image(ref)` — `<engine> rmi <ref>`; idempotent via
  `<engine> images --filter`.

- [x] 12. `list_images()` — `<engine> images --format '{{json .}}'`
  (one JSON per line).

- [x] 13. `create_container(spec)` — `<engine> create --name
  <name> [<flags...>] <image> [<argv...>]`; idempotent via
  `<engine> inspect <name>`; captures the new container id from
  stdout on success.

- [x] 14. `start_container(container_id)` — `<engine> start <id>`;
  idempotent via `<engine> inspect`'s `State.Running`.

- [x] 15. `stop_container(container_id)` — `<engine> stop -t
  <timeout> <id>`; idempotent via `<engine> inspect`'s
  `State.Running` and absence.

- [x] 16. `remove_container(container_id)` — `<engine> rm -f <id>`;
  idempotent via `<engine> inspect` absence.

- [x] 17. `list_containers(all=False)` — `<engine> ps [-a]
  --format '{{json .}}'`.

- [x] 18. `inspect_container(container_id)` — `<engine> inspect
  <id>` parsing the JSON ARRAY and returning element 0; returns
  `None` on absence.

- [x] 19. `get_container_logs(container_id, lines=100)` —
  `<engine> logs --tail <lines> <id>`.

- [x] 20. `exec_in_container(container_id, argv)` — `<engine> exec
  <id> <argv...>`; never skips.

- [x] 21. `_dispatch_container_write` and `_build_result` helpers —
  thread `target` / `image` / `container_id` / `engine` through
  `BaseFeatureManager._dispatch`.

- [x] 22. `_engine_binary` helper that maps
  `UbuntuContainerEngine.DOCKER` to `docker` and
  `UbuntuContainerEngine.PODMAN` to `podman`.

## UbuntuContainerManager — Gaps (specified but not implemented)

- [ ] 23. Strict-read mode for the read verbs that raises
  `UbuntuContainerQueryError` instead of returning empty / `None`.

- [ ] 24. Explicit engine-availability probe that raises
  `UbuntuContainerEngineNotFoundError` when the binary is missing
  (currently surfaces as `FAILED`).

- [ ] 25. Honour `UbuntuContainerSpec.detach=False` by branching to
  `<engine> run` instead of always going through `create` /
  `start`.

- [ ] 26. Surface a `force` parameter on `remove_image` so callers
  can request `<engine> rmi -f <ref>`.

- [ ] 27. Spec-equivalence comparison in `create_container`'s
  idempotency guard that compares not just `Config.Image` but every
  flag the spec specified (env, volumes, ports, networks, restart
  policy, working dir, user).  Currently the guard is image-only.

- [ ] 28. Registry-login verb (`<engine> login <registry>` with the
  password streamed on stdin) so coordinators do not need a separate
  package for credential routing.

## Tests — Implemented

- [x] 29. `test_pull_image_invokes_engine_pull`
- [x] 30. `test_pull_image_skipped_when_already_present`
- [x] 31. `test_pull_image_force_pull_overrides_skip`
- [x] 32. `test_pull_image_dry_run_short_circuits`
- [x] 33. `test_remove_image_invokes_rmi`
- [x] 34. `test_remove_image_skipped_when_absent`
- [x] 35. `test_list_images_parses_json_lines`
- [x] 36. `test_create_container_invokes_create_with_spec`
- [x] 37. `test_create_container_skipped_when_name_present_with_same_spec`
- [x] 38. `test_start_container_invokes_start`
- [x] 39. `test_start_container_skipped_when_running`
- [x] 40. `test_stop_container_invokes_stop_with_timeout`
- [x] 41. `test_stop_container_skipped_when_stopped`
- [x] 42. `test_remove_container_invokes_rm_force`
- [x] 43. `test_list_containers_with_all_includes_stopped`
- [x] 44. `test_inspect_container_parses_json_array`
- [x] 45. `test_get_container_logs_invokes_logs_with_tail`
- [x] 46. `test_exec_in_container_invokes_exec`
- [x] 47. `test_engine_selection_dispatches_to_podman_when_configured`
- [x] 48. `test_writes_pass_elevated_per_config`
- [x] 49. `test_reads_never_elevate`
- [x] 50. `test_failed_command_surfaces_stderr`
- [x] 51. Module-level parser helpers
  (`_parse_image_lines`, `_parse_container_ps_lines`,
  `_parse_inspect_first`, `_build_create_argv`).

## Tests — Gaps

- [ ] 52. Digest-pinned-reference idempotency test — the contract
  calls out digest matching as a stricter guard than tag matching.
- [ ] 53. Engine-not-found exception test once the explicit probe
  lands.
- [ ] 54. `exec_in_container` non-zero exit test that asserts
  `status=FAILED` and `output` carries the captured stdout.
- [ ] 55. Property-based test for the JSON-line parser against
  randomised image-list output.

## Follow-ups

- [ ] 56. **Cross-OS contract conformance review**: verify
  `UbuntuContainerActionResult` field shapes match the cross-OS
  contract at
  `codecraftai-sysman-common/specs/contracts/container-management/contract.md`.

- [ ] 57. **codecraftai-* reuse check**: confirm that no other feature
  in `codecraftai-sysman-linux-ubuntu` invokes `docker` or `podman`
  directly; all such calls should go through
  `UbuntuContainerManager`.

## Spec deviations from this implementation

- **Engine identifier set is narrower than the contract.**  The
  cross-OS contract enumerates `PODMAN`, `DOCKER`, `CONTAINERD`,
  `DOCKER_DESKTOP`, and `COLIMA`.  This Ubuntu implementation only
  ships `DOCKER` and `PODMAN` because they are the only two engines
  shipped in Ubuntu's standard repositories.  The other identifiers
  are valid for macOS / Windows.

- **`create_container` idempotency is image-only.**  When a
  container of the same name exists with a *different* image the
  manager returns FAILED via the engine's own duplicate-name error;
  when the image matches but the rest of the spec differs the
  manager returns SKIPPED rather than recreating.  A full
  spec-equivalence comparison is reserved for follow-up task 27.

- **`stop_container` returns SKIPPED when the container is absent.**
  The contract reserves SKIPPED for the "already stopped" case; we
  extend it to absence to match Wave-1's "no work to do" treatment.
  A coordinator that needs to distinguish the two cases should call
  `inspect_container` first.

- **No strict-read mode.**  Read verbs return empty lists /
  `None` when their underlying command fails, mirroring the
  Wave-1 Ubuntu pattern.  The
  `UbuntuContainerQueryError` exception is defined and reserved
  for a future strict-read variant.

- **`get_container_logs.truncated` is always True.**  The manager
  always passes `--tail` so the caller's record always reflects a
  truncated query.  A future enhancement could detect
  "actual lines < requested lines" and flip the flag to False; the
  present implementation prefers the simpler always-True semantic
  over a guess.

- **Registry credentials are out of scope.**  `pull_image` accepts
  only the reference; coordinators that need authenticated pulls
  must run `docker login` / `podman login` ahead of time through
  another feature, or via a future registry-login verb on this
  manager (see follow-up task 28).

- **No engine-availability probe on construction.**  A missing
  engine surfaces as `FAILED` on the first verb call rather than as
  `UbuntuContainerEngineNotFoundError` raised from the constructor.
