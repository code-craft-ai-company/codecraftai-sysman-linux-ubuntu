# Requirements

## Introduction

The python-environment-management feature provides the
`codecraftai-sysman-linux-ubuntu` package with the ability to deploy Python virtual
environments with a declared dependency set on Ubuntu (and Debian-family) hosts.
This spec implements the python-environment-management contract defined at
`codecraftai-sysman-common/specs/contracts/python-environment-management/contract.md`.

Every venv / pip verb is inherited unchanged from
`ccai.sysman.common.pythonenvironmentmanagementbase.PythonEnvironmentManagementBase`
(see `codecraftai-sysman-common/specs/python-environment-management-base/`).  This
package contributes exactly two things:

1. The POSIX layout — `<venv>/bin/python`, `python3` launcher, `test -d` / `rm -rf`
   probes.
2. `ensure_prerequisites()`, which installs the apt packages a stock Ubuntu image
   lacks before `python3 -m venv` can succeed, by delegating to the existing
   `UbuntuPackageManager` rather than issuing `apt-get` itself.

All system interaction happens through `ICommandRunner`; writes honour
`config.elevated`, reads never elevate.

---

## Requirements

### Requirement 1: Select the POSIX Layout

**User Story:** As a maintenance coordinator, I want the Ubuntu manager to resolve
venv paths and probes the POSIX way without any configuration.

#### Acceptance Criteria

1. THE SYSTEM SHALL return a `PosixPythonEnvironmentLayout` from `_build_layout()`.
2. THE SYSTEM SHALL resolve the environment interpreter to `<path>/bin/python` and
   the default base interpreter to `["python3"]`.
3. THE SYSTEM SHALL probe existence with `["test", "-d", <path>]` and remove with
   `["rm", "-rf", <path>]`.

---

### Requirement 2: Ensure apt Prerequisites

**User Story:** As a maintenance coordinator, I want one call that makes a stock
Ubuntu host able to create virtual environments.

#### Acceptance Criteria

1. WHEN the coordinator calls `ensure_prerequisites()` THE SYSTEM SHALL install, in
   order, `python3`, `python3-venv`, `python3-pip` via
   `UbuntuPackageManager.install_package`, sharing this manager's runner.
2. THE SYSTEM SHALL construct the `UbuntuPackageManagerConfig` with this manager's
   `dry_run` and `elevated` flags and the config's `apt_options`.
3. WHEN `config.prerequisite_packages` is set THE SYSTEM SHALL use it instead of the
   default list; WHEN the `packages` argument is supplied THE SYSTEM SHALL use it in
   preference to both.
4. WHEN an install returns `FAILED` THE SYSTEM SHALL stop and return the results
   collected so far.
5. WHEN dry-run is active THE SYSTEM SHALL return one `DRY_RUN` result per package
   and SHALL NOT invoke the runner.
6. THE SYSTEM SHALL return one `UbuntuPackageActionResult` per package attempted.

---

### Requirement 3: Accept a Plain Base Config

#### Acceptance Criteria

1. WHEN the manager is constructed with a plain `PythonEnvManagerConfig` THE SYSTEM
   SHALL behave as if `prerequisite_packages=None` and `apt_options=None`.

---

### Requirement 4: OS-Prefixed Aliases

#### Acceptance Criteria

1. THE SYSTEM SHALL export `UbuntuPythonEnvActionStatus`,
   `UbuntuPythonEnvActionResult`, `UbuntuPythonEnvDeployResult`,
   `UbuntuPythonEnvInfo`, `UbuntuPythonEnvQueryError`, `UbuntuPythonEnvSpec`,
   `UbuntuPythonPackageInfo` as aliases of the shared types, plus
   `UbuntuPythonEnvManagerConfig` and `UbuntuPythonEnvironmentManager`.
