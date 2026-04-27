# Implementation Plan — Ubuntu Command Execution

Checked items (`[x]`) are implemented and covered by tests.  Unchecked items (`[ ]`)
identify remaining work.

This batch landed the lower-level runner subpackage that the
`UbuntuCommandExecutor` (still unchecked below) will consume.  The runner work is
tracked under a separate "Runner Subpackage" section appended at the bottom.

## Module and Models

- [ ] 1. Create the `automojo.sysman.linux.ubuntu.commandexecution` module

- [ ] 2. Define `UbuntuCommandExecutionStatus` enum (SUCCESS / FAILED / SKIPPED /
  DRY_RUN)

- [ ] 3. Define `UbuntuCommandExecutorConfig` dataclass (dry_run,
  default_timeout_seconds, shell_path)

- [ ] 4. Define `UbuntuCommandSpec` dataclass (args, elevated, working_dir, env,
  timeout_seconds, label)

- [ ] 5. Define `UbuntuCommandResult` and `UbuntuSequenceResult` dataclasses with
  timing fields and elevation echo

## UbuntuCommandExecutor — Implemented

- [ ] 6. Implement `__init__` accepting an `ICommandRunner` and a
  `UbuntuCommandExecutorConfig`

- [ ] 7. Implement `run(args, *, elevated, working_dir, env, timeout_seconds)` —
  dry-run short-circuit, empty-args rejection, working-dir check, env merge, runner
  dispatch, timing, result mapping

- [ ] 8. Implement empty-args guard returning `success=False` without invoking the
  runner

- [ ] 9. Implement working-directory precondition using `pathlib.Path.is_dir()`

- [ ] 10. Implement env merge helper producing `{**os.environ, **env}` (copying even
  when `env == {}`)

- [ ] 11. Implement timeout fallback (per-call → config default → `None`) and
  `stderr` injection when runner returns `timed_out=True` with empty `stderr`

- [ ] 12. Implement `run_sequence(commands, *, stop_on_failure)` — empty-list
  short-circuit, dry-run per-spec synthesis, stop-on-failure break, `failed_at`
  computation

- [ ] 13. Implement `run_script(script, *, elevated, working_dir, env,
  timeout_seconds)` — empty-script guard, `tempfile.NamedTemporaryFile`, `chmod 0o755`,
  `try/finally` cleanup via `Path.unlink(missing_ok=True)`

- [ ] 14. Implement OSError guard around tempfile creation + `chmod`, returning
  `success=False` without invoking the runner

## UbuntuCommandExecutor — Gaps (specified but not implemented)

- [ ] 15. Streaming stdout/stderr callbacks (out of scope for the base contract on
  Ubuntu; document as a future enhancement when `apt` progress output needs real-time
  rendering)

- [ ] 16. PTY allocation for interactive commands (e.g., `passwd`) — not required by
  the contract; document that automation uses non-interactive flags

- [ ] 17. Per-command `nice` / `ionice` wrapping

- [ ] 18. Structured reporting of `sudo` failures (distinguish
  `sudo: a password is required` from `sudo: <user> is not in the sudoers file`)

## Tests — Implemented

- [ ] 19. `test_run_success_maps_zero_exit_to_success_true`
- [ ] 20. `test_run_failure_maps_nonzero_to_success_false`
- [ ] 21. `test_run_timeout_flag_surfaces_with_synthetic_stderr`
- [ ] 22. `test_run_empty_args_returns_failure_without_runner_call`
- [ ] 23. `test_run_dry_run_does_not_invoke_runner`
- [ ] 24. `test_working_dir_missing_returns_failure_without_runner_call`
- [ ] 25. `test_working_dir_none_forwards_cwd_none`
- [ ] 26. `test_env_merge_caller_wins_on_conflict`
- [ ] 27. `test_run_sequence_stop_on_failure_stops_at_first_failure`
- [ ] 28. `test_run_sequence_continue_on_failure_runs_all`
- [ ] 29. `test_run_sequence_empty_list_returns_all_succeeded_true`
- [ ] 30. `test_run_script_creates_and_deletes_tempfile`
- [ ] 31. `test_run_script_dry_run_does_not_create_tempfile`
- [ ] 32. `test_run_script_empty_script_returns_failure_without_runner_call`

## Tests — Gaps

- [ ] 33. Test that `config.default_timeout_seconds` fallback applies when per-call
  `timeout_seconds` is `None`

- [ ] 34. Test that aspect composition (Retry flipping fail→success) preserves
  `args`, `elevated`, and `dry_run` on the result

- [ ] 35. Test that `UbuntuCommandResult.started_at`, `finished_at`, and `duration_ms`
  are `None` in dry-run results and populated otherwise

- [ ] 36. Test that `UbuntuCommandSpec` fields (`elevated`, `working_dir`, `env`,
  `timeout_seconds`) are forwarded verbatim to the per-command `run` call

## Follow-ups

- [ ] 37. **Cross-OS contract conformance review**: verify that
  `UbuntuCommandResult` / `UbuntuSequenceResult` / `UbuntuCommandSpec` field shapes
  match the cross-OS contract at
  `automojo-sysman-common/specs/contracts/command-execution/contract.md` exactly,
  including the Ubuntu-specific `sudo -n -E` elevation path.

- [ ] 38. **Runner-coupling review**: confirm that `UbuntuLocalCommandRunner` and
  `UbuntuSSHCommandRunner` share a common elevation-prepending helper so that
  `sudo -n` handling does not drift between transports.

## Runner Subpackage — Implemented in this batch

The runner subpackage at
`source/packages/automojo/sysman/linux/ubuntu/runners/` and its accompanying
tests at
`source/testroots/testbase/automojo/tests/sysman/linux/ubuntu/test_localcommandrunner.py`
landed as the foundation for the executor work above.

- [x] R1. Create the `automojo.sysman.linux.ubuntu.runners` subpackage layout
  (`__init__.py`, `models.py`, `local.py`, `ssh.py`, `aspects/`).

- [x] R2. Define `UbuntuRunResult`, `UbuntuExecutionContext`,
  `UbuntuSshReliabilityMetrics`, `UbuntuSshErrorType`, and `UbuntuRunRecord`
  data models in `runners/models.py`; re-export the cross-OS `RunResult` for
  callers.

- [x] R3. Implement `UbuntuLocalCommandRunner` over `subprocess.run` with
  elevation (`sudo -n`, plus `-E` when `env` is supplied), env merge with
  `os.environ`, stdin delivery via `input=`, timeout-to-`timed_out=True`
  mapping, on-output / on-error callbacks, and `capture_output=False` support.

- [x] R4. Implement runner-side aspect composition: `add_aspect` /
  `remove_aspect` / `aspects` snapshot; before-execute in registration order,
  after-execute in reverse, with disabled aspects skipped.

- [x] R5. Implement reliability metrics tracking on the local runner
  (`success_count`, `failure_count`, `timeout_count`, duration min / max /
  total / avg, `last_error`).

- [x] R6. Implement `UbuntuSshCommandRunner` skeleton: same public surface as
  the local runner, dry-run path, `NotImplementedError` for live SSH paths
  (no transport dependency added to `pyproject.toml`).

- [x] R7. Implement aspects: `UbuntuPersistenceAspect` (JSON-line sink),
  `UbuntuRetryAspect` (exit-code + regex-pattern decision plus
  `run_with_retries` helper), `UbuntuTimeoutAspect` (default-timeout
  injection with override toggle), and the shared `IUbuntuExecutionAspect`
  protocol.

- [x] R8. Tests in `test_localcommandrunner.py` (real subprocess plus
  `unittest.mock` for timeouts and aspects) covering: success, failure,
  stdout/stderr capture, env, cwd, stdin, elevation (with and without env),
  timeout, on-output callback, capture-off, missing executable, dry-run,
  metrics, and aspect invocation order.

- [x] R9. Tests in `test_aspects.py` covering construction and one or two
  behaviour checks for each of persistence, retry, and timeout aspects.
