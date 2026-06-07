# Requirements

## Introduction

The package-management feature provides the `codecraftai-sysman-linux-ubuntu` package
with the ability to manage apt-managed software packages on Ubuntu systems.  This
spec implements the package-management contract defined at
`codecraftai-sysman-common/specs/contracts/package-management/contract.md` for
Ubuntu using `apt-get` for writes and `dpkg` / `dpkg-query` for reads.  It covers
install/remove/upgrade, upgrade-all, installed-package queries, a packaged listing,
an upgradable listing, local `.deb` installation, and metadata refresh.

A maintenance coordinator running in an external package drives all operations by
calling into this feature's public interface.  The feature does not decide *what* to
install — it provides the capability to do so reliably, idempotently, and auditably.
All system interaction happens through `ICommandRunner`; no direct `subprocess` call
lives in the manager.  Writes pass `elevated=True` when the config allows; reads
never elevate.  Non-interactive behaviour is enforced by setting
`DEBIAN_FRONTEND=noninteractive` and passing `-y` on writes when `assume_yes=True`.

---

## Requirements

### Requirement 1: Install a Package

**User Story:** As a maintenance coordinator, I want to install an apt package at
the latest version or at a pinned version, idempotently and with dry-run support.

#### Acceptance Criteria

1. WHEN the coordinator calls `install_package(name, version=None)` THE SYSTEM SHALL
   invoke `["apt-get", "install", "-y", name]` via the runner; with a supplied
   `version` THE SYSTEM SHALL invoke `["apt-get", "install", "-y", f"{name}={version}"]`.
2. WHEN `config.assume_yes=False` THE SYSTEM SHALL omit the `-y` flag.
3. WHEN the package is already installed at any version AND `version is None` THE
   SYSTEM SHALL return `status=SKIPPED`.
4. WHEN the package is already installed at a version that matches `version` THE
   SYSTEM SHALL return `status=SKIPPED`.
5. WHEN the command succeeds THE SYSTEM SHALL return
   `UbuntuPackageActionResult(status=SUCCESS, package_name=name, version=<resolved>, …)`.
6. WHEN the command fails THE SYSTEM SHALL return `status=FAILED` with captured
   `exit_code`, `stdout`, `stderr`, and `command`.
7. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN` without
   invoking the runner.
8. WHEN the runner receives the command THE SYSTEM SHALL forward the env
   `{"DEBIAN_FRONTEND": "noninteractive"}` merged with the process environment.

---

### Requirement 2: Remove a Package

**User Story:** As a maintenance coordinator, I want to remove an installed apt
package, idempotently and with dry-run support.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_package(name)` THE SYSTEM SHALL invoke
   `["apt-get", "remove", "-y", name]` via the runner.
2. WHEN the package is not installed THE SYSTEM SHALL return `status=SKIPPED`.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 3: Upgrade a Package

**User Story:** As a maintenance coordinator, I want to upgrade a single apt
package, idempotently and with dry-run support.

#### Acceptance Criteria

1. WHEN the coordinator calls `upgrade_package(name, version=None)` THE SYSTEM
   SHALL invoke `["apt-get", "install", "-y", "--only-upgrade", name]`; with a
   supplied `version` THE SYSTEM SHALL invoke `["apt-get", "install", "-y",
   "--only-upgrade", f"{name}={version}"]`.
2. WHEN the package is not installed THE SYSTEM SHALL return `status=SKIPPED` with
   `message="package is not installed; nothing to upgrade"`.
3. WHEN the installed version already matches (or exceeds) the requested version
   THE SYSTEM SHALL return `status=SKIPPED`.
4. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
5. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
6. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.

---

### Requirement 4: Upgrade All Packages

**User Story:** As a maintenance coordinator, I want to upgrade every installed
package to its latest version in one call.

#### Acceptance Criteria

1. WHEN the coordinator calls `upgrade_all()` THE SYSTEM SHALL invoke
   `["apt-get", "upgrade", "-y"]` via the runner with
   `DEBIAN_FRONTEND=noninteractive`.
2. WHEN the command succeeds THE SYSTEM SHALL return
   `UbuntuPackageActionResult(status=SUCCESS, package_name="", …)`.
3. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN `upgrade_all` is called the idempotency guard SHALL NOT skip — apt
   already no-ops when there is nothing to upgrade.

---

### Requirement 5: Query Installed State

**User Story:** As a maintenance coordinator, I want to know whether a package is
installed and get its metadata without elevation.

#### Acceptance Criteria

1. WHEN the coordinator calls `is_installed(name)` THE SYSTEM SHALL invoke
   `["dpkg", "-l", name]` via the runner without elevation.
2. WHEN `dpkg -l` exits 0 AND the output's first data line begins with `ii` THE
   SYSTEM SHALL return `True`.
3. WHEN `dpkg -l` exits non-zero OR the first data line begins with `rc` / `un` /
   any status other than `ii` THE SYSTEM SHALL return `False`.
4. WHEN the coordinator calls `get_package_info(name)` AND `dpkg -s name` exits 0
   THE SYSTEM SHALL parse the `Package`, `Version`, `Architecture`, and `Source`
   fields and return
   `UbuntuPackageInfo(name=<Package>, version=<Version>, release="",
   arch=<Architecture>, origin=<Source or "apt">)`.
5. WHEN the package is unknown (`dpkg -s` exits non-zero with
   `package '<name>' is not installed and no information is available`) THE
   SYSTEM SHALL return `None` WITHOUT raising.
6. WHEN `dpkg -s` fails with any other error THE SYSTEM SHALL raise
   `UbuntuPackageQueryError`.

---

### Requirement 6: List Installed Packages

**User Story:** As a maintenance coordinator, I want a uniform list of installed
packages for inventory.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_installed()` THE SYSTEM SHALL invoke
   `["dpkg-query", "-W", "-f=${Package}\\t${Version}\\t${Architecture}\\t${Status}\\n"]`
   via the runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse each line, filter to those
   whose `Status` ends in `installed`, and return a list of `UbuntuPackageInfo`
   objects.
3. WHEN the command fails THE SYSTEM SHALL raise `UbuntuPackageQueryError`.
4. WHEN the output is empty THE SYSTEM SHALL return an empty list without raising.

---

### Requirement 7: List Upgradable Packages

**User Story:** As a maintenance coordinator, I want the list of packages with
available upgrades.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_upgradable()` THE SYSTEM SHALL invoke
   `["apt", "list", "--upgradable"]` via the runner without elevation.
2. WHEN the command succeeds THE SYSTEM SHALL parse each non-"Listing…" line of
   the form `<name>/<repo> <available> <arch> [upgradable from: <installed>]` and
   return a list of `UbuntuPackageUpdateInfo`.
3. WHEN the command fails THE SYSTEM SHALL raise `UbuntuPackageQueryError`.
4. WHEN the output is empty (only the `Listing…` header) THE SYSTEM SHALL return
   an empty list.

---

### Requirement 8: Install From a Local `.deb`

**User Story:** As a maintenance coordinator, I want to install a packaged `.deb`
file from disk.

#### Acceptance Criteria

1. WHEN the coordinator calls `install_local(filepath)` AND the file exists THE
   SYSTEM SHALL invoke `["apt-get", "install", "-y", filepath]` via the runner.
2. WHEN the file does NOT exist THE SYSTEM SHALL raise
   `UbuntuPackageFileNotFoundError` before invoking the runner.
3. WHEN the command succeeds THE SYSTEM SHALL return `status=SUCCESS`.
4. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
5. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN` AFTER the
   file-existence check.

---

### Requirement 9: Refresh Metadata

**User Story:** As a maintenance coordinator, I want to refresh apt metadata
before making decisions about available versions.

#### Acceptance Criteria

1. WHEN the coordinator calls `refresh_metadata()` THE SYSTEM SHALL invoke
   `["apt-get", "update"]` via the runner.
2. WHEN the command succeeds THE SYSTEM SHALL return
   `UbuntuPackageActionResult(status=SUCCESS, package_name="", …)`.
3. WHEN the command fails THE SYSTEM SHALL return `status=FAILED`.
4. WHEN dry-run mode is active THE SYSTEM SHALL return `status=DRY_RUN`.
5. WHEN `refresh_metadata` is called THE SYSTEM SHALL NOT apply an idempotency
   guard — freshness heuristics are the caller's responsibility.

---

### Requirement 10: Elevation

**User Story:** As a maintenance coordinator, I want writes to pass `elevated=True`
when the config enables elevation, and reads to never elevate.

#### Acceptance Criteria

1. WHEN `config.elevated=True` AND the verb is a write (install, remove, upgrade,
   upgrade_all, install_local, refresh_metadata) THE SYSTEM SHALL pass
   `elevated=True` to the runner.
2. WHEN the verb is a read (`is_installed`, `get_package_info`, `list_installed`,
   `list_upgradable`) THE SYSTEM SHALL NEVER pass `elevated=True` regardless of
   config.
3. WHEN the idempotency-probe calls (`dpkg -l` / `dpkg -s`) are issued as part of a
   write's guard THE SYSTEM SHALL NOT pass `elevated=True` — probes never
   elevate.
4. WHEN `config.assume_yes=True` THE SYSTEM SHALL pass `-y` to every apt-get
   write invocation; when `False`, the flag is omitted.
