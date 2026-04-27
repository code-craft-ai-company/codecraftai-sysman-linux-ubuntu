# Implementation Plan — Process Management (Ubuntu)

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

This feature mixes read and write verbs.  Writes are dry-run-aware and
idempotent where the verb's nature permits; reads never elevate.

## Module and Models

- [x] 1. Create the
  `automojo.sysman.linux.ubuntu.processmanagement` module.

- [x] 2. Define `UbuntuProcessActionStatus` (alias of the package's
  shared `ActionStatus`: `SUCCESS / FAILED / SKIPPED / DRY_RUN`).

- [x] 3. Define `UbuntuProcessManagerConfig` inheriting from
  `FeatureConfig`, adding `default_signal: str = "TERM"` and
  `wait_poll_interval_seconds: float = 0.2`.

- [x] 4. Define `UbuntuProcessQueryError` with
  `command` / `exit_code` / `stderr` and a multi-line message built
  via `os.linesep.join`.

- [x] 5. Define data dataclasses: `UbuntuProcessInfo`,
  `UbuntuProcessSpec`, `UbuntuProcessActionResult`,
  `UbuntuProcessWaitResult`.

## Manager — Implemented

- [x] 6. Implement `__init__` (inherited; no overrides needed).

- [x] 7. Implement `list_processes(owner=None)` via
  `ps -eo <format> --no-headers` (or `ps -u <owner> -o <format>`).

- [x] 8. Implement `get_process(pid)` via
  `ps -p <pid> -o <format> --no-headers`.  Treat exit-code-non-zero
  with empty stderr as the "unknown PID" path returning `None`.

- [x] 9. Implement `find_processes_by_name(name)` via `pgrep -a`.
  Treat exit code 1 as the "no matches" path returning `[]`.

- [x] 10. Implement `kill_process(pid, signal=None)` with idempotency
  guard via `get_process`, dry-run short-circuit, default-signal
  fallback, and signal-name validation.

- [x] 11. Implement `kill_process_tree(pid, signal=None)` —
  `pkill -<sig> -P <pid>` followed by `kill -<sig> <pid>`.  Tolerate
  `pkill` exit 1 (no children); annotate detail on other non-zero.

- [x] 12. Implement `start_process(spec)` via
  `setsid sh -c '<shell-quoted argv>'` with `cwd` / `env` forwarded
  and a best-effort `pgrep` PID lookup on success.

- [x] 13. Implement `set_process_priority(pid, priority)` via
  `renice <priority> -p <pid>` with idempotency guard via
  `get_process(pid).nice == priority`.

- [x] 14. Implement `wait_for_process(pid, timeout_seconds=None)` as a
  poll loop on `kill -0 <pid>` with `time.sleep` between probes;
  return `timed_out=True` on timeout.

## Module-Level Parsers — Implemented

- [x] 15. `_parse_ps_output` (12-column `ps` row + trailing `args`).

- [x] 16. `_parse_pgrep_output` (`<pid> <argv...>` rows).

- [x] 17. `_safe_int`, `_safe_float` token coercion helpers.

- [x] 18. `_shell_escape_argv` — single-quote-escapes argv for
  `setsid sh -c`.

- [x] 19. `_best_effort_pid_lookup` — re-queries `pgrep` after
  `start_process` to populate the result `pid`.

## Tests — Implemented

- [x] 20. `test_list_processes_invokes_ps_and_parses`
- [x] 21. `test_get_process_parses_single_record`
- [x] 22. `test_get_process_returns_none_for_unknown_pid`
- [x] 23. `test_kill_process_invokes_kill`
- [x] 24. `test_kill_process_skipped_when_pid_not_found`
- [x] 25. `test_kill_process_dry_run_short_circuits`
- [x] 26. `test_kill_process_tree_kills_children_first`
- [x] 27. `test_start_process_invokes_setsid_sh`
- [x] 28. `test_start_process_dry_run_short_circuits`
- [x] 29. `test_set_process_priority_invokes_renice`
- [x] 30. `test_set_process_priority_skipped_when_already_matches`
- [x] 31. `test_find_processes_by_name_parses_pgrep_output`
- [x] 32. `test_failed_command_returns_failed_or_raises_on_read`
- [x] 33. `test_reads_never_elevate`

## Gaps (specified but not implemented)

- [ ] 34. **True async-spawn for `start_process`.**  The pilot wraps
  the spawn in `setsid sh -c '...'` but the runner blocks until the
  shell returns.  A `LocalCommandRunner.spawn(...)` or a
  `daemon(3)`-style shim is needed for genuine background spawning.
  Best-effort PID recovery via `pgrep` is the placeholder.

- [ ] 35. **Per-user `start_process` privilege drop.**  `spec.user`
  is recorded on the result but does not currently switch the spawn
  user.  A future pass should issue `runuser -u <user> -- <argv>`
  (or equivalent) when `spec.user` is non-`None`.

- [ ] 36. **Recursive `kill_process_tree`.**  Today the verb reaps
  direct children only via `pkill -P`.  Walking the full descendant
  tree (e.g., via `/proc/<pid>/task/.../children`) would let us reap
  grandchildren without relying on the children re-parenting to
  init.

- [ ] 37. **Wait-for-exit code recovery.**  `wait_for_process`
  returns `exit_code=None` on Linux because only the parent process
  can recover the child's exit code.  When the manager itself is
  the parent (e.g., via the not-yet-implemented `spawn(...)`) we
  could `waitpid()` and capture the real exit code.

- [ ] 38. **`ProcessFilter` parameter set.**  The cross-OS contract
  defines a `ProcessFilter` dataclass with `owner`, `name_pattern`,
  `min_cpu_percent`, and `min_memory_bytes`.  This pilot exposes
  `owner` only via a keyword argument; the rest are filtered in
  pure Python by the caller.  Surfacing the dataclass is a follow-up.

## Tests — Gaps

- [ ] 39. Per-signal command-construction tests for the full
  `{TERM, KILL, HUP, INT, QUIT, USR1, USR2}` set; today only `TERM`
  / `KILL` are exercised explicitly.

- [ ] 40. `wait_for_process` timeout test — currently exercised only
  through code review.  A test using a fake `time.monotonic` /
  `time.sleep` fixture and a runner that always reports the PID
  alive would close this gap.

- [ ] 41. `start_process` PID-recovery test — assert that on a
  successful spawn the result `pid` is populated from a follow-up
  `pgrep -a` call.

## Spec-vs-Contract Drift

- [ ] 42. Spec-vs-contract drift discovered while authoring this
  spec:

  - The contract names the data dataclass `ProcessInfo` with
    `name` + `command_line` fields; this implementation uses
    `command` + `argv` (matching the `comm` / `args` columns from
    `ps` 1:1).  Reconciliation MAY rename `command` → `name` and
    add a `command_line` derived field in the next contract
    revision.
  - The contract defines a `ProcessFilter` dataclass; this pilot
    exposes only `owner` directly on `list_processes`.  The other
    filter knobs (`name_pattern`, `min_cpu_percent`,
    `min_memory_bytes`) are intended for a Python-side post-filter
    in the maintenance coordinator.
  - The contract's `ProcessSignal` is an enum; this pilot accepts
    plain `str` signal names and validates them against
    `_KNOWN_SIGNALS`.  Migrating to an enum is a one-pass change
    once the cross-OS enum lands in `automojo-interfaces`.
  - The contract uses `start_process(spec) → ProcessStartResult`;
    this pilot collapses that into a shared
    `UbuntuProcessActionResult` with `pid` populated on success
    rather than a separate `ProcessStartResult` dataclass.
  - The contract's `wait_for_process` returns
    `ProcessWaitResult(elapsed_seconds=...)`; this pilot's
    `UbuntuProcessWaitResult` does not surface `elapsed_seconds`.
    Adding it is a one-line change.

## Follow-ups

- [ ] 43. **Cross-OS contract conformance review**: align field
  names per the drift list above so a future generic
  `ProcessManager` facade can wrap `UbuntuProcessManager`,
  `MacosProcessManager`, and `WindowsProcessManager` uniformly.

- [ ] 44. **automojo-* reuse check**: confirm that none of the
  other Layer 3 features (notably `service-management` for
  `MainPID` lookups) re-implement process-state probes that should
  delegate to `UbuntuProcessManager.get_process`.
