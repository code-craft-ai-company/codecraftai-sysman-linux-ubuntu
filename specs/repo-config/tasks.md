# Repository Configuration — Tasks

## Implementation Checklist

Tasks are ordered so that each item can be built and tested before the next depends on it.
Cross-references are to requirement IDs in `requirements.md`.

---

### Phase 1 — Scaffolding

- [ ] **T-01** Create the package directory structure:
  ```
  source/packages/automojo/sysman/linux/ubuntu/repoconfig/
  source/testroots/testbase/automojo/tests/sysman/linux/ubuntu/repoconfig/
  ```
  Add `__init__.py` files at every level.
  *Refs: NF-1, NF-2*

- [ ] **T-02** Create `_exceptions.py` defining `RepoCommandError`, `RepositoryNotFoundError`,
  and `ElevationRequiredError`.
  *Refs: AC-1.4, AC-2.4, AC-3.4, AC-4.4, AC-6.3*

- [ ] **T-03** Create `_models.py` defining `RepoEntry`, `RepoPolicyInfo`, and `RepoResult`
  as frozen `dataclass` instances.
  *Refs: AC-1.5, AC-2.2, AC-5.6, AC-7.2, NF-5*

- [ ] **T-04** Write `test_models.py` — verify field names, types, defaults, and that
  the dataclasses are frozen (assignment raises `FrozenInstanceError`).
  *Refs: NF-5*

---

### Phase 2 — Parsers

- [ ] **T-05** Create `_parsers.py` with a `parse_list_file(content: str, file_path: str) -> list[RepoEntry]`
  function that handles one-line `.list` format:
  - Parses `deb` and `deb-src` lines into `RepoEntry` objects.
  - Treats `# deb …` lines as `enabled=False`.
  - Extracts `signed-by=` option into `RepoEntry.signed_by` if present.
  *Refs: AC-7.2, AC-7.3*

- [ ] **T-06** Add `parse_sources_file(content: str, file_path: str) -> list[RepoEntry]`
  to `_parsers.py` for DEB822 `.sources` format:
  - Reads `Enabled:`, `Types:`, `URIs:`, `Suites:`, `Components:`, `Signed-By:` fields.
  - Absent `Enabled:` field treated as `enabled=True`.
  *Refs: AC-7.2, AC-7.3*

- [ ] **T-07** Add `render_list_entry(entry: RepoEntry) -> str` and
  `render_sources_entry(entry: RepoEntry) -> str` to `_parsers.py`.
  *Refs: AC-1.1*

- [ ] **T-08** Add `enable_list_content(content: str) -> str` and
  `disable_list_content(content: str) -> str` helpers that apply the comment-toggle
  transformation to one-line format file content.
  *Refs: AC-3.1, AC-4.1*

- [ ] **T-09** Add `enable_sources_content(content: str) -> str` and
  `disable_sources_content(content: str) -> str` helpers for DEB822 format.
  *Refs: AC-3.1, AC-4.1*

- [ ] **T-10** Write `test_parsers.py`:
  - Round-trip tests: `parse_list_file(render_list_entry(entry))` → original `RepoEntry`.
  - Round-trip tests for DEB822 format.
  - Enable/disable toggle tests for both formats.
  - Edge cases: empty file, file with only comments, multi-component entries,
    entries with `signed-by` option.
  *Refs: AC-1.2, AC-3.2, AC-4.2, AC-7.3, AC-7.5*

---

### Phase 3 — Manager

- [ ] **T-11** Create `_manager.py` with `RepoConfigManager.__init__(self, file_operator, command_runner, dry_run=False)`.
  Store all three as instance attributes.
  *Refs: NF-1, NF-2, NF-3*

- [ ] **T-12** Implement `list_repositories() -> list[RepoEntry]`:
  - Call `IFileOperator.listdir("/etc/apt/sources.list.d/")`.
  - Dispatch to `parse_list_file` or `parse_sources_file` by extension.
  - Sort result by `name`.
  *Refs: AC-7.1, AC-7.2, AC-7.3, AC-7.4, AC-7.5*

- [ ] **T-13** Implement `add_repository(entry: RepoEntry) -> RepoResult`:
  - Elevation check → `ElevationRequiredError` if needed.
  - Idempotency check against existing file content.
  - `dry_run` guard.
  - Write via `IFileOperator.write()`.
  *Refs: AC-1.1, AC-1.2, AC-1.3, AC-1.4, AC-1.5*

- [ ] **T-14** Implement `remove_repository(name: str) -> RepoResult`:
  - Elevation check.
  - Resolve `.list` / `.sources` path; return `changed=False` if absent.
  - `dry_run` guard.
  - Delete via `IFileOperator.delete()`.
  *Refs: AC-2.1, AC-2.2, AC-2.3, AC-2.4*

- [ ] **T-15** Implement `enable_repository(name: str) -> RepoResult`:
  - Resolve path; raise `RepositoryNotFoundError` if absent.
  - Read, transform, idempotency check, `dry_run` guard, write.
  *Refs: AC-3.1, AC-3.2, AC-3.3, AC-3.4*

- [ ] **T-16** Implement `disable_repository(name: str) -> RepoResult`:
  - Same pattern as T-15 with disable transformation.
  *Refs: AC-4.1, AC-4.2, AC-4.3, AC-4.4*

- [ ] **T-17** Implement `import_key(key_data: str, keyring_name: str) -> RepoResult`:
  - Elevation check.
  - `IFileOperator.makedirs("/etc/apt/keyrings/")`.
  - Run `gpg --dearmor` via `ICommandRunner.run(["gpg", "--dearmor"], stdin=key_data)`.
  - Idempotency check against existing keyring content.
  - `dry_run` guard.
  - Write binary via `IFileOperator.write_bytes()`.
  *Refs: AC-5.1, AC-5.2, AC-5.3, AC-5.4, AC-5.5, AC-5.6*

- [ ] **T-18** Implement `refresh_index() -> RepoResult`:
  - Elevation check.
  - `dry_run` guard.
  - `ICommandRunner.run(["apt-get", "update"])`.
  - Non-zero exit → raise `RepoCommandError`.
  *Refs: AC-6.1, AC-6.2, AC-6.3, AC-6.4*

- [ ] **T-19** Implement `get_repo_status(name: str) -> RepoPolicyInfo | None`:
  - `ICommandRunner.run(["apt-cache", "policy"])`.
  - Parse output for the named repository.
  - Return `RepoPolicyInfo` or `None`.
  - Non-zero exit → raise `RepoCommandError`.
  *Refs: AC-8.1, AC-8.2, AC-8.3*

- [ ] **T-20** Populate `__init__.py` to export:
  `RepoConfigManager`, `RepoEntry`, `RepoPolicyInfo`, `RepoResult`,
  `RepoCommandError`, `RepositoryNotFoundError`, `ElevationRequiredError`.

---

### Phase 4 — Unit Tests for Manager

- [ ] **T-21** Write `test_manager.py` — `add_repository` tests:
  - Normal add (file absent → changed=True).
  - Idempotent add (identical content already present → changed=False).
  - Dry-run add (no file written).
  - Elevation error (fake operator raises `PermissionError`).
  *Refs: AC-1.1 – AC-1.5*

- [ ] **T-22** Write `test_manager.py` — `remove_repository` tests:
  - Normal remove (file exists → changed=True).
  - Remove when absent (→ changed=False).
  - Dry-run remove.
  *Refs: AC-2.1 – AC-2.4*

- [ ] **T-23** Write `test_manager.py` — `enable_repository` / `disable_repository` tests:
  - Enable already-disabled repository.
  - Enable already-enabled repository (idempotent).
  - Disable already-enabled repository.
  - Disable already-disabled repository (idempotent).
  - Dry-run for both operations.
  - `RepositoryNotFoundError` when file absent.
  *Refs: AC-3.1 – AC-3.4, AC-4.1 – AC-4.4*

- [ ] **T-24** Write `test_manager.py` — `import_key` tests:
  - Normal import (keyring absent → changed=True).
  - Idempotent import (identical keyring present → changed=False).
  - Dry-run import.
  - `gpg --dearmor` command failure → `RepoCommandError`.
  *Refs: AC-5.1 – AC-5.6*

- [ ] **T-25** Write `test_manager.py` — `refresh_index` tests:
  - Successful `apt-get update` run.
  - Dry-run (command not invoked).
  - Non-zero exit → `RepoCommandError` with stderr.
  *Refs: AC-6.1 – AC-6.4*

- [ ] **T-26** Write `test_manager.py` — `list_repositories` tests:
  - Mixed `.list` and `.sources` files in fake directory.
  - Empty directory → empty list.
  - Disabled entries present → `enabled=False` in result.
  - Sort order verified.
  *Refs: AC-7.1 – AC-7.5*

- [ ] **T-27** Write `test_manager.py` — `get_repo_status` tests:
  - Repository found in `apt-cache policy` output → `RepoPolicyInfo` returned.
  - Repository not found → `None` returned.
  - Command failure → `RepoCommandError`.
  *Refs: AC-8.1 – AC-8.3*

---

### Phase 5 — Integration Verification

- [ ] **T-28** Run the full test suite for this module:
  ```bash
  python -m pytest source/testroots/testbase/automojo/tests/sysman/linux/ubuntu/repoconfig/ -v
  ```
  All tests must pass.

- [ ] **T-29** Verify the module is importable in isolation (no subprocess side-effects):
  ```bash
  python -c "from automojo.sysman.linux.ubuntu.repoconfig import RepoConfigManager; print('OK')"
  ```
  *Refs: NF-6*

- [ ] **T-30** Confirm `pyproject.toml` includes the `automojo.sysman.linux.ubuntu` package
  root and that `poetry check` reports no errors.
