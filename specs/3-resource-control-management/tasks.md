# Tasks

This document tracks the implementation work for
`UbuntuResourceControlManager`.  Tasks marked `[x]` are landed
on the wave-5.3 branch.

---

## Implementation tasks

- [x] Stub the `ccai/sysman/linux/ubuntu/resourcecontrolmanagement.py`
  module with the public surface defined in `design.md`.
- [x] Implement the `UbuntuResourceControlActionStatus`,
  `UbuntuResourceControlActionResult`,
  `UbuntuResourceControlManagerConfig`,
  `UbuntuIoLimitSpec`,
  `UbuntuResourceGroupSpec`,
  `UbuntuResourceGroupInfo`,
  `UbuntuResourceGroupUsage`, and
  `UbuntuResourceControlQueryError` types.
- [x] Implement the `_validate_name` and `_validate_pid` helpers.
- [x] Implement `_render_slice_body` honouring optional fields.
- [x] Implement `_read_file`, `_path_exists`, `_slice_is_active`,
  `_read_cgroup_procs`, and `_get_show_property` probes.
- [x] Implement `_parse_show_output`, `_parse_cpu_quota`,
  `_parse_memory_max`, `_parse_cpu_stat`,
  `_parse_memory_current`, and `_parse_io_stat` parsers as pure
  module-level functions.
- [x] Implement `create_group` with idempotency guard and
  daemon-reload on success.
- [x] Implement `delete_group` with `stop` + `rm` +
  `daemon-reload`.
- [x] Implement `set_cpu_limit` with the `CPUQuotaPerSecUSec`
  match probe.
- [x] Implement `set_memory_limit` with the `MemoryMax` match
  probe.
- [x] Implement `set_io_limits` honouring `weight`, `read_bps`,
  `write_bps`, and the optional `device` field.
- [x] Implement `attach_process` writing to
  `/sys/fs/cgroup/<name>.slice/cgroup.procs`.
- [x] Implement `detach_process` writing to
  `/sys/fs/cgroup/system.slice/cgroup.procs`.
- [x] Implement `list_groups` enumerating
  `/sys/fs/cgroup/*.slice/`.
- [x] Implement `get_group_usage` reading `cpu.stat`,
  `memory.current`, `io.stat`, and `cgroup.procs`.

---

## Test tasks

- [x] Land
  `source/testroots/testbase/ccai/tests/sysman/linux/ubuntu/test_resourcecontrolmanagement.py`
  with at least 14 pytest functions.
- [x] `test_create_group_invokes_systemctl` — `tee` + `daemon-reload`
  + `start <name>.slice` argv shape.
- [x] `test_create_group_skipped_when_present` — byte-equal slice
  body and active slice -> SKIPPED.
- [x] `test_create_group_dry_run` — `DRY_RUN` and no runner call.
- [x] `test_delete_group_invokes_systemctl` — `stop` + `rm` +
  `daemon-reload` argv shape.
- [x] `test_delete_group_skipped_when_absent` — missing slice
  file -> SKIPPED.
- [x] `test_set_cpu_limit_invokes_systemctl` — `CPUQuota=N%`.
- [x] `test_set_cpu_limit_skipped_when_match` — `CPUQuotaPerSecUSec`
  already matches.
- [x] `test_set_memory_limit_invokes_systemctl` — `MemoryMax=N`.
- [x] `test_set_io_limits_invokes_systemctl` — `IOWeight=N`.
- [x] `test_attach_process_invokes_tee` — `tee --append
  /sys/fs/cgroup/<name>.slice/cgroup.procs` with the PID on stdin.
- [x] `test_attach_process_skipped_when_present` — PID already in
  `cgroup.procs`.
- [x] `test_detach_process_invokes_tee` — `tee --append
  /sys/fs/cgroup/system.slice/cgroup.procs`.
- [x] `test_list_groups_returns_list` — multiple slice entries
  parsed.
- [x] `test_get_group_usage_parses_cgroup_files` — `cpu.stat` /
  `memory.current` / `io.stat` parsed end-to-end.
- [x] `test_writes_elevate` — `elevated=True` reaches the runner
  for writes when configured.
- [x] `test_reads_never_elevate` — reads always run with
  `elevated=False`.
- [x] `test_failed_command_surfaces_stderr` — non-zero exit
  surfaces stderr / exit_code on FAILED.

---

## Documentation tasks

- [x] `requirements.md` — 12 requirements covering the cross-OS
  contract verbs.
- [x] `design.md` — cross-OS resource-control-model concept-mapping
  table + verb implementation pseudocode.
- [x] `tasks.md` — this file.

---

## Follow-up tasks

These are intentionally deferred and tracked outside Wave-5.3.

- [ ] cgroup v1 compatibility shim — out of scope; v1 hosts
  surface kernel errors verbatim.
- [ ] Nested slice hierarchies (`<parent>.slice/<child>.slice`).
- [ ] Pressure-stall-information (PSI) per-slice reporting via
  `/sys/fs/cgroup/<name>.slice/cpu.pressure`.
- [ ] Transient scope creation via `systemd-run --scope` for
  one-shot bounded commands.
- [ ] cgroup-namespaces / unprivileged-cgroup managers.
- [ ] Aggregate `list_groups` query that issues a single
  `systemctl show` for every detected slice instead of one per
  slice.
