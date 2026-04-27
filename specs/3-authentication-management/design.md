# Design

## Overview

The authentication management feature is implemented as a single
module, `automojo.sysman.linux.ubuntu.authenticationmanagement`,
exposing an `UbuntuAuthenticationManager` class that wraps
`pam-auth-update`, the per-service files under `/etc/pam.d/`, and
pure-Python parsers for the PAM stack syntax through the
`ICommandRunner` abstraction.

All file reads route through the runner via `cat`; all file writes
route through the runner via `tee` with `stdin`. The
`pam-auth-update` tool itself is invoked as a normal subprocess
through the runner. No direct filesystem access occurs inside the
module.

**See Also:**
- `3-user-group-management` for account creation, password expiry,
  and SSH-key management.
- `3-ssh-server-configuration` for sshd-level configuration. Note:
  the `sshd` PAM stack at `/etc/pam.d/sshd` is the seam between the
  two features — auth-management owns the PAM-side, SSH config owns
  the daemon-side.
- `3-identity-management` for SSSD / realm join / Kerberos
  integration.

---

## Cross-OS Authentication Model Concept Mapping

The authentication-management contract abstracts over three very
different OS authentication models. Ubuntu sits at the same family as
Red Hat — both use PAM — but the *orchestrator* differs:

| Concept | Ubuntu (this spec) | RHEL | macOS | Windows |
|---|---|---|---|---|
| Stack composition tool | `pam-auth-update` | `authselect` | (manual edit) | LSA registry packages + secpol |
| Per-service file | `/etc/pam.d/<svc>` | `/etc/pam.d/<svc>` | `/etc/pam.d/<svc>` | (none — registry-driven) |
| Profile registry | `/usr/share/pam-configs/<name>` | `/usr/share/authselect/profiles/<name>` | (none) | LSA `Authentication Packages` MULTI_SZ |
| Method enable | `pam-auth-update --enable <name>` | `authselect enable-feature <name>` | edit stack file | add to MULTI_SZ |
| Method disable | `pam-auth-update --disable <name>` | `authselect disable-feature <name>` | comment line | remove from MULTI_SZ |
| Method enumeration | `pam-auth-update --list` | `authselect list-features` | parse `/etc/pam.d/` | `Get-ItemProperty 'HKLM:\...\Lsa'` |
| Validation | re-parse (no canonical tool) | `authselect check` | re-parse | `secedit /validate` |
| Subsystem identifier | PAM service name | PAM service name | PAM service name | LOGON / NETWORK / etc. |
| Stack entry shape | `module_type control module_path [args]` | identical | identical | `policy_key = policy_value` (registry / secpol INI) |
| Idempotency check | byte-equal of rendered file | byte-equal of rendered file | byte-equal of rendered file | secpol section equality |

**Pilot scope:** the Ubuntu manager focuses on PAM stack management
and `pam-auth-update` profile toggles. Out-of-scope (with a
documented seam):

- `/etc/security/pwquality.conf`, `/etc/security/faillock.conf`,
  and `/etc/security/limits.d/` — RHEL ships these with a richer
  surface but Ubuntu's defaults are simpler; managing them remains
  on the RHEL package only for now.
- `/etc/nsswitch.conf` — owned by `3-identity-management`.

---

## Architecture

```
+--------------------------------------------------------------+
|            Maintenance Coordinator                           |
|  - get_auth_config(LOGIN) -> UbuntuAuthConfig                |
|  - set_auth_config(LOGIN, declared_config) -> result         |
|  - enable_method("unix") / disable_method("krb5")            |
|  - validate_auth_config(SUDO)                                |
+----------------------------+---------------------------------+
                             |
                             v
+--------------------------------------------------------------+
|            UbuntuAuthenticationManager                       |
|  automojo.sysman.linux.ubuntu.authenticationmanagement       |
|                                                              |
|  Reads:                                                      |
|    get_auth_config(subsystem)                                |
|    list_auth_methods()                                       |
|    validate_auth_config(subsystem)                           |
|                                                              |
|  Writes:                                                     |
|    set_auth_config(subsystem, config)                        |
|    enable_method(name)                                       |
|    disable_method(name)                                      |
+----------------------------+---------------------------------+
                             |  delegates to
                             v
+--------------------------------------------------------------+
|                   ICommandRunner                             |
|  cat /etc/pam.d/<svc>          (read; not elevated)          |
|  ls -1 /usr/share/pam-configs  (read; not elevated)          |
|  pam-auth-update --list        (read; not elevated)          |
|  pam-auth-update --enable X    (write; elevated)             |
|  pam-auth-update --disable X   (write; elevated)             |
|  tee /etc/pam.d/<svc>          (write; elevated; stdin=...)  |
+--------------------------------------------------------------+
```

---

## Components

### `UbuntuAuthenticationManager`

**Location:** `source/packages/automojo/sysman/linux/ubuntu/authenticationmanagement.py`

| Method | Verb kind | Tool | Elevation |
|---|---|---|---|
| `get_auth_config(subsystem)` | read | `cat /etc/pam.d/<svc>` | no |
| `set_auth_config(subsystem, config)` | write | `tee /etc/pam.d/<svc>` | yes |
| `list_auth_methods()` | read | `pam-auth-update --list`, `ls -1 /usr/share/pam-configs` | no |
| `enable_method(name)` | write | `pam-auth-update --enable <name>` | yes |
| `disable_method(name)` | write | `pam-auth-update --disable <name>` | yes |
| `validate_auth_config(subsystem)` | read | `cat /etc/pam.d/<svc>` + parser | no |

---

## Configuration

```python
@dataclass
class UbuntuAuthenticationManagerConfig(FeatureConfig):
    pam_dir: str = "/etc/pam.d"
    pam_configs_dir: str = "/usr/share/pam-configs"
    pam_auth_update: str = "pam-auth-update"
```

Inherits `dry_run` and `elevated` from `FeatureConfig`. The Ubuntu
manager defaults `elevated=False` because the constructor wires
elevation per-verb internally — every write verb passes `True`
unconditionally; reads pass `False` unconditionally. The config flag
remains as a global override for unusual deployment scenarios (e.g.
running the manager from inside an already-rooted container where
`sudo` is absent).

---

## Data Models

```python
class UbuntuAuthActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class UbuntuAuthSubsystem:
    """
        Discriminated wrapper around a PAM service-file basename.

        Use the module-level constants UBUNTU_AUTH_LOGIN /
        UBUNTU_AUTH_SUDO / UBUNTU_AUTH_SU / UBUNTU_AUTH_SSH /
        UBUNTU_AUTH_CRON for canonical subsystems; instantiate
        directly with kind=OTHER and a name when the service file
        is named something else.
    """
    kind: str         # 'LOGIN' | 'SUDO' | 'SU' | 'SSH' | 'CRON' | 'OTHER'
    name: str         # 'login' | 'sudo' | 'su' | 'sshd' | 'cron' | <free>


@dataclass
class UbuntuAuthEntry:
    module_type: str        # 'auth' | 'account' | 'password' | 'session'
    control: str            # 'required' | 'sufficient' | 'optional' | '[...]'
    module_path: str        # e.g. 'pam_unix.so'
    args: list[str]         # remaining tokens, e.g. ['try_first_pass']


@dataclass
class UbuntuAuthConfig:
    subsystem: str          # service-file basename
    entries: list[UbuntuAuthEntry]


@dataclass
class UbuntuAuthMethodInfo:
    name: str
    enabled: bool
    description: str | None = None
    module_path: str | None = None


@dataclass
class UbuntuAuthValidationResult:
    valid: bool
    errors: list[str]
    warnings: list[str]


@dataclass
class UbuntuAuthActionResult(FeatureActionResult):
    subsystem: str | None = None
    method: str | None = None
```

---

## Subsystem Mapping

| `UbuntuAuthSubsystem.kind` | Service file |
|---|---|
| `LOGIN` | `/etc/pam.d/login` |
| `SUDO` | `/etc/pam.d/sudo` |
| `SU` | `/etc/pam.d/su` |
| `SSH` | `/etc/pam.d/sshd` |
| `CRON` | `/etc/pam.d/cron` |
| `OTHER` | `/etc/pam.d/<name>` |

The manager's read / write verbs accept either an
`UbuntuAuthSubsystem` instance or a raw `str`. A raw string is
treated as `kind=OTHER, name=<string>` to allow drop-in service files
not in the canonical enum (e.g. `polkit-1`, `passwd`, `chsh`,
`gdm-password`).

---

## Key Method Designs

### `get_auth_config(subsystem)`

1. Resolve the service-file basename from the supplied subsystem.
2. Run `cat /etc/pam.d/<svc>` with `elevated=False`.
3. If exit_code != 0 and stderr contains "No such file" → raise
   `UbuntuAuthenticationSubsystemNotFoundError`.
4. If exit_code != 0 for any other reason → raise
   `UbuntuAuthenticationQueryError`.
5. Parse the file content via `_parse_pam_stack`.
6. Return `UbuntuAuthConfig(subsystem=<basename>, entries=<parsed>)`.

### `set_auth_config(subsystem, config)`

1. Resolve the basename.
2. Read current via `_read_file`.
3. Render desired via `_render_pam_stack(config.entries)`.
4. If `current == desired` → return `SKIPPED`.
5. If `dry_run` → return `DRY_RUN` with `command=["tee", path]`.
6. Else dispatch `["tee", path]` with `stdin=<rendered>`,
   `elevated=True`.

### `list_auth_methods()`

1. Run `pam-auth-update --list` with `elevated=False`.
2. Parse output into `dict[str, bool]` (name → enabled).
3. Run `ls -1 /usr/share/pam-configs` with `elevated=False` to
   enumerate registered profile files.
4. For each profile name, run `cat /usr/share/pam-configs/<name>`
   and extract `Name:` and `Description:` keys via
   `_parse_pam_config_profile`.
5. Merge: every profile becomes one `UbuntuAuthMethodInfo`. Names
   that appeared in the `--list` output get `enabled` from there;
   names that did not appear get `enabled=False`.
6. When step 1 fails, fall through to step 3 only and synthesise
   every record with `enabled=False`.

### `enable_method(name)` / `disable_method(name)`

Symmetric. The probing step uses `list_auth_methods()` (cached for
the call). Idempotency rules are spelled out in §Requirements.

### `validate_auth_config(subsystem)`

1. Read the file.
2. Re-parse via `_parse_pam_stack`.
3. Walk every entry checking:
   - `module_type` ∈ {auth, account, password, session, @include}
   - `control` is non-empty
   - `module_path` is non-empty
4. For each `@include <other>` line, attempt to resolve the target
   inside `/etc/pam.d/`. Detect `@include` cycles via a depth-bounded
   visit set; report a cycle as a single error.
5. Return `UbuntuAuthValidationResult(valid=<all_ok>, errors=[...],
   warnings=[...])`.

---

## PAM Stack Parser / Renderer

The PAM stack syntax this module supports:

```
auth      required    pam_unix.so try_first_pass nullok
auth      sufficient  pam_sss.so use_first_pass
account   required    pam_unix.so
@include  common-auth
password  [success=ok default=ignore]  pam_pwquality.so retry=3
```

Parser rules:

- Lines that start with `#` (after leading whitespace) are
  comments — skipped for the structured representation.
- Blank lines are skipped.
- The first token is `module_type`. It MAY be `@include`; in that
  case the second token is captured as `module_path` and `control`
  is the empty string, with the convention recorded in the parser.
- The second token is `control`. When the second token starts with
  `[`, the parser collects tokens until the matching `]` is found
  and joins them as the literal control expression — e.g.
  `[success=ok default=ignore]`.
- The third token is `module_path`.
- All remaining tokens are `args`.

Renderer rules: emit each entry as
`<module_type> <control> <module_path> <args>` with single-space
separators. `@include` entries are rendered as `@include <module_path>`
(control is omitted because PAM grammar has no control on `@include`).

The pilot does NOT preserve comment lines across a render cycle —
comments are stripped on parse, so a `set_auth_config` round-trip
removes them. The contract treats the user's submitted
`UbuntuAuthConfig.entries` as the truth.

---

## `pam-auth-update --list` Output Parser

`pam-auth-update --list` (Ubuntu 22.04+) emits two columns separated
by tabs / runs of whitespace, with a header row:

```
PAM configurations available:
unix         Unix authentication                       [Enabled]
krb5         Kerberos authentication                   [Disabled]
sss          SSS authentication                        [Enabled]
ldap         LDAP authentication                       [Disabled]
```

Older releases emit a different format; the parser is tolerant of
both:

- Trim leading whitespace.
- Skip blank lines and any line starting with the literal `PAM`
  banner.
- The first token is the profile `name`.
- The trailing token determines the enabled state:
  `[Enabled]` / `Enabled` / `enabled` → `True`. Otherwise `False`.
- Everything between the first token and the trailing state is the
  description.

---

## `/usr/share/pam-configs/<name>` Profile-File Parser

Profile files are a simple key-value format:

```
Name: Unix authentication
Default: yes
Priority: 256
Auth-Type: Primary
Auth:
        [success=end default=ignore]    pam_unix.so try_first_pass nullok
Account-Type: Primary
Account:
        [success=end default=ignore]    pam_unix.so
```

The parser only extracts the top-level `Name:` and any line up to
the first key whose body looks like an indented PAM stack reference
(used to populate `module_path` heuristically).

---

## Dry-Run Behaviour

Every mutating entry point follows the same shape:

- Read the live state (always — dry-run does not block reads).
- Compute desired state and compare for SKIPPED.
- If a write would be required AND `config.dry_run=True` → return
  `DRY_RUN` with `command` populated.

---

## Correctness Properties

1. **Idempotency.** Each write returns `SKIPPED` when the live state
   already matches the requested state, verified by re-reading
   through the runner.
2. **No silent failure.** Every non-zero runner exit surfaces as
   `status=FAILED` with `exit_code` and `stderr` populated.
3. **Dry-run safety.** No mutating command is dispatched when
   `dry_run=True`; read-only reconciliation queries always run.
4. **Subsystem typing.** Unknown subsystem strings surface as
   `UbuntuAuthenticationSubsystemNotFoundError` rather than silent
   no-ops on enable / disable.
5. **Stack ordering.** The PAM stack render preserves entry order
   verbatim — order is semantically significant in PAM.
6. **Profile registry.** `list_auth_methods` always surfaces every
   profile registered under `/usr/share/pam-configs/`, even when
   `pam-auth-update --list` fails.

---

## Testing Strategy

A local `_MockCommandRunner` dataclass mirrors the pattern used in
`test_sshserverconfiguration.py`. Tests seed `RunResult` values keyed
by argv tuple and assert:

- argv shape per write verb,
- elevation flag per verb,
- `SKIPPED` when current == desired,
- `DRY_RUN` short-circuits the runner,
- stderr surfaces in `FAILED` results,
- the PAM parser handles `@include`,
- the `pam-auth-update --list` parser handles both old and new
  formats.

No live system or `sudo` access required.

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/
└── authenticationmanagement.py        ← UbuntuAuthenticationManager + models

source/testroots/testbase/
└── automojo/tests/sysman/linux/ubuntu/
    └── test_authenticationmanagement.py
```

---

## Seams With Other Features

| Seam | Owner | This feature |
|---|---|---|
| sshd PAM stack | `3-ssh-server-configuration` | This feature owns `/etc/pam.d/sshd`; the SSH feature owns `/etc/ssh/sshd_config`. The two are independent. |
| Per-user account state | `3-user-group-management` | This feature handles the policy file (`/etc/pam.d/<svc>`); user-group handles individual accounts. |
| SSSD / Kerberos | `3-identity-management` | `3-identity-management` may invoke `enable_method("sss")` to flip the SSS PAM profile after it has provisioned the domain join. |
| Compliance snapshot | `3-security-compliance` | This feature exposes read methods; compliance reads and compares against a baseline. |

---

## Result Field Population

For every verb the result's `subsystem` and `method` fields are
populated as follows:

| Verb | `subsystem` | `method` |
|---|---|---|
| `set_auth_config` | resolved basename | `None` |
| `enable_method` | `None` | supplied profile name |
| `disable_method` | `None` | supplied profile name |

The `command` field always carries the argv that was (or would
have been) executed. The `detail` field carries the
`subsystem` value when set, otherwise the `method` value. The
`message` field is reserved for SKIPPED / FAILED reason text such
as `"PAM stack already at desired content"` or `"method already
enabled"`.

---

## `pam-auth-update` Behaviour Caveats

`pam-auth-update` is a Debian-native tool. Its behaviour varies
slightly across Ubuntu releases. The pilot accommodates the two
output schemas described in the §`pam-auth-update --list` Output
Parser section and additionally:

- **Apt hook integration.** On most installs `pam-auth-update`
  runs automatically as a `dpkg` post-install hook for any
  package that ships a `/usr/share/pam-configs/<name>` file. The
  manager's `enable_method` / `disable_method` invocations
  therefore re-trigger the same composition path the apt hook
  uses, ensuring consistency across both tools.
- **`--package` / `--remove` flags.** These flags exist for
  package-installer integration (`--package` accepts an
  unprefixed profile name and assumes default-yes; `--remove`
  removes a registered profile entirely). The pilot does NOT
  expose them — the cross-OS contract treats `enable` / `disable`
  as a stable per-method toggle, not a registry mutation. Future
  revisions may add a `register_method` / `unregister_method`
  pair if the contract grows that surface.
- **`--force` flag.** `pam-auth-update --force <name>` overrides
  the user's interactive consent. The pilot's `enable_method`
  / `disable_method` already pass `--enable` / `--disable`
  noninteractively (these flags do not prompt). `--force` is
  reserved for the rare case where a profile's metadata says it
  should not be enabled by default; the pilot does not expose it.

---

## Profile-File Heuristics

The `_parse_pam_config_profile` helper extracts a synthetic
`ModulePath` key by scanning indented body lines for a
`pam_*.so` token. This is a heuristic — profile files can
reference multiple modules across `Auth:`, `Account:`,
`Password:`, and `Session:` sections, and the heuristic captures
only the first. Callers that need the full module reference
should run `cat /usr/share/pam-configs/<name>` themselves and
parse the body via the structured key extraction the helper
performs.

The helper does NOT attempt to resolve `@include` directives that
appear inside profile-file bodies; those directives are processed
at composition time by `pam-auth-update` and the manager's read
verbs do not need to follow them.

---

## File Locations Quick Reference

| Path | Role |
|---|---|
| `/etc/pam.d/login` | Login PAM stack. |
| `/etc/pam.d/sudo` | sudo PAM stack. |
| `/etc/pam.d/su` | su PAM stack. |
| `/etc/pam.d/sshd` | SSH daemon PAM stack. |
| `/etc/pam.d/cron` | cron PAM stack. |
| `/etc/pam.d/common-auth` | Composition target produced by `pam-auth-update`. |
| `/etc/pam.d/common-account` | Composition target produced by `pam-auth-update`. |
| `/etc/pam.d/common-password` | Composition target produced by `pam-auth-update`. |
| `/etc/pam.d/common-session` | Composition target produced by `pam-auth-update`. |
| `/usr/share/pam-configs/` | Profile registry consumed by `pam-auth-update`. |
| `/etc/security/pwquality.conf` | Password quality policy (out of scope for this pilot). |
| `/etc/security/faillock.conf` | Failed-login lockout policy (out of scope for this pilot). |
| `/etc/security/limits.d/` | Per-user resource limits (out of scope for this pilot). |
| `/etc/nsswitch.conf` | NSS source ordering (owned by `3-identity-management`). |

---

## Drift From the Cross-OS Contract

- **`apply_changes` verb.** The contract defines an `apply_changes`
  verb for OSes with a staging / commit model (RHEL's `authselect
  apply-changes`). Ubuntu's `pam-auth-update` is immediate — the
  tool re-composes `/etc/pam.d/` files on every `--enable` /
  `--disable` invocation. The Ubuntu manager therefore omits the
  `apply_changes` verb. Coordinators that target multiple OSes
  should treat `apply_changes` as a no-op call on Ubuntu.
- **`get_method_status` verb.** The contract defines a
  `get_method_status(name)` read. Ubuntu's `pam-auth-update --list`
  is the only addressable method-state surface and there is no
  cheap per-method probe — every list call enumerates every
  registered profile. The Ubuntu manager exposes the data via
  `list_auth_methods()` only; callers that want a single record
  filter the list. This matches the depth of prior pilot batches
  (e.g. `auditmanagement.list_rules()`).
- **`profile` field on `UbuntuAuthConfig`.** The contract's
  `AuthConfig` has a `profile` slot for OSes that select a profile
  template. Ubuntu's PAM stack does not have profile templates —
  `pam-auth-update` drives selection by enabling individual modules
  rather than picking a profile. The pilot's `UbuntuAuthConfig`
  therefore omits `profile` and uses an explicit `entries` list as
  the truth. Future revisions may add an optional `profile`
  attribute when the Debian project ships profile-template support.
- **`features` and `directives` fields on `UbuntuAuthConfig`.** The
  contract defines `features: list[str]` and `directives: dict[str,
  str]`. Ubuntu's PAM stack expresses both through individual
  `UbuntuAuthEntry` records (`pam_pwquality.so retry=3` etc.). The
  pilot collapses both into the `entries` list. Cross-OS callers
  should construct `entries` from their structured directive map.
