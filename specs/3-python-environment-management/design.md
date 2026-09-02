# Design

## Overview

`ccai.sysman.linux.ubuntu.pythonenvironmentmanagement` is a thin subclass of the
shared `PythonEnvironmentManagementBase`.  Every venv and pip verb, the deploy
algorithm, the requirement probe, and the read surface are documented in
`codecraftai-sysman-common/specs/python-environment-management-base/design.md`
and are not repeated here.  This document covers only what Ubuntu adds.

---

## Architecture

```
UbuntuPythonEnvironmentManager
├── _build_layout()           → PosixPythonEnvironmentLayout
├── ensure_prerequisites()    → UbuntuPackageManager.install_package(...) × N
│                                (same ICommandRunner, same dry_run / elevated)
└── (inherited)               → create_environment, remove_environment,
                                install_packages, install_requirements,
                                uninstall_packages, upgrade_pip, deploy, reads
```

---

## Components

### `UbuntuPythonEnvManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| *(contract fields)* | — | — | `dry_run`, `elevated`, `base_interpreter`, `index_url`, `extra_index_urls`, `trusted_hosts`, `pip_options`, `upgrade_pip`, `system_site_packages` |
| `prerequisite_packages` | `list[str] \| None` | `None` → `["python3", "python3-venv", "python3-pip"]` | apt packages for `ensure_prerequisites` |
| `apt_options` | `list[str] \| None` | `None` | Forwarded to `UbuntuPackageManagerConfig.apt_options` |

### `UbuntuPythonEnvironmentManager`

| Method | Description |
|---|---|
| `_build_layout()` | Returns `PosixPythonEnvironmentLayout()` |
| `ensure_prerequisites(packages=None)` | Installs each prerequisite via `UbuntuPackageManager`; stops at first `FAILED` |
| `_resolve_prerequisite_packages(packages)` | argument > config > default |
| `_build_package_manager()` | Builds `UbuntuPackageManager(runner, UbuntuPackageManagerConfig(dry_run, elevated, apt_options))` |
| `_typed_config()` | Upgrades a plain `PythonEnvManagerConfig` to the Ubuntu config |

---

## Command Mappings

| Step | Command (through the runner) | Elevated |
|---|---|---|
| Prerequisite probe | `dpkg -s <pkg>` (by `UbuntuPackageManager`) | never |
| Prerequisite install | `apt-get install -y <pkg> [apt_options]` with `DEBIAN_FRONTEND=noninteractive` | `config.elevated` |
| Create venv | `python3 -m venv [--system-site-packages] <path>` | `config.elevated` |
| Existence probe | `test -d <path>` | never |
| Validity probe | `<path>/bin/python -c "import platform; print(platform.python_version())"` | never |
| pip (any) | `<path>/bin/python -m pip --disable-pip-version-check --no-input …` | writes only |
| Remove | `rm -rf <path>` | `config.elevated` |

Why `python3-venv`: Debian packages `ensurepip` separately, so on a stock image
`python3 -m venv` exits 1 with "ensurepip is not available".  `python3-pip` is
included so a system `pip` exists for callers that create with
`--system-site-packages` and rely on the system pip.

---

## Correctness Properties

1. **Layout.** `manager.layout` is a `PosixPythonEnvironmentLayout`; venv python
   is `<path>/bin/python`; default launcher is `["python3"]`.
2. **Prerequisite defaults.** `ensure_prerequisites()` dispatches
   `apt-get install -y` for `python3`, `python3-venv`, `python3-pip` in that order,
   elevated per config, with the noninteractive env.
3. **Prerequisite precedence.** Argument list > `config.prerequisite_packages` >
   default; `apt_options` are appended.
4. **Prerequisite short-circuit.** The first `FAILED` install is the last one
   attempted.
5. **Prerequisite dry-run.** Every result is `DRY_RUN`; no runner call.
6. **Plain config.** A `PythonEnvManagerConfig` is accepted and the Ubuntu-only
   fields default.
7. **Deploy argv.** A fresh `deploy` issues `python3 -m venv`, the pip upgrade, and
   the pip install in order with `<path>/bin/python`.

---

## Module Layout

```
source/packages/ccai/sysman/linux/ubuntu/
└── pythonenvironmentmanagement.py

source/testroots/testbase/ccai/tests/sysman/linux/ubuntu/
└── test_pythonenvironmentmanagement.py
```

---

## Usage Example

```python
from ccai.sysman.linux.ubuntu.runners import UbuntuLocalCommandRunner, UbuntuLocalRunnerConfig
from ccai.sysman.linux.ubuntu.pythonenvironmentmanagement import (
    UbuntuPythonEnvironmentManager, UbuntuPythonEnvManagerConfig, UbuntuPythonEnvSpec,
)

runner = UbuntuLocalCommandRunner(UbuntuLocalRunnerConfig())
manager = UbuntuPythonEnvironmentManager(
    runner=runner, config=UbuntuPythonEnvManagerConfig(elevated=True),
)
manager.ensure_prerequisites()
result = manager.deploy(spec=UbuntuPythonEnvSpec(
    path="/opt/venvs/agent",
    requirements_files=["/opt/agent/requirements.txt"],
))
assert result.status.value in ("success", "skipped")
```

---

## Testing Strategy

The shared base suite covers the verbs.  The Ubuntu suite asserts layout selection,
the apt delegation (argv, env, elevation, precedence, short-circuit, dry-run), plain
config acceptance, and one end-to-end deploy argv sequence, all through a
`_MockCommandRunner`.
