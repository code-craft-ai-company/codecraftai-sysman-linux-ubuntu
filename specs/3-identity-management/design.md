# Design

## Overview

The identity management feature is implemented as a single module,
`ccai.sysman.linux.ubuntu.identitymanagement`, that exposes a
`UbuntuIdentityManager` class.  This spec implements the cross-OS
identity-management contract defined at
`codecraftai-sysman-common/specs/contracts/identity-management/contract.md`
for Ubuntu using `realm` (realmd) for FreeIPA / Active Directory /
LDAP enrollment, `sssctl` for SSSD cache control, and `getent` (NSS)
for resolving directory-backed users and groups.

All read paths used for idempotency guards go through `realm list`;
the output is parsed in pure Python into `UbuntuRealmInfo` records.
All system interaction is delegated to a command runner implementing
the `ICommandRunner` interface.  No direct file-system access or
`subprocess` calls are made inside the module.

Administrator passwords for `join_realm` are forwarded to
`realm join` via stdin so no plaintext credential ever reaches argv,
a persistence aspect, or the returned `command` field.  This mirrors
the password-redaction pattern established by
`UbuntuUserGroupManager.set_password` (which pipes
`"<user>:<pw>\n"` through `chpasswd` on stdin) and the macOS /
Windows `<redacted>`-token pattern; on Ubuntu the redaction is
unnecessary because the password never lands on argv at all.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).

**See Also:**
- `3-user-group-management` for local-account operations.
- `3-certificate-trust-management` for the enterprise-CA anchors
  required by LDAPS / Kerberos-over-TLS setups.
- `3-service-management` (when it lands on Ubuntu) for the
  `sssd.service` lifecycle.
- `codecraftai-sysman-linux-redhat/specs/3-identity-management/` for the
  RHEL / FreeIPA reference implementation that motivated this
  cross-OS contract.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH)                       │
│  - Configures aspects (optional)                            │
│  - Injects runner into UbuntuIdentityManager                │
│  - Supplies UbuntuRealmJoinSpec with admin credentials      │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuIdentityManager                            │
│  ccai.sysman.linux.ubuntu.identitymanagement            │
│                                                             │
│  Writes:                                                    │
│    join_realm()    leave_realm()    sync_users()            │
│  Reads:                                                     │
│    get_realm_status()    list_joined_realms()               │
│    resolve_user()        resolve_group()                    │
└────────────────────────┬────────────────────────────────────┘
                         │ delegates to
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                   ICommandRunner                            │
│  run(args, elevated, stdin, capture_output) → RunResult     │
└────────────────────────┬────────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          ▼                             ▼
┌──────────────────────┐      ┌──────────────────────┐
│ LocalCommandRunner   │      │ SSHCommandRunner     │
│ - subprocess.run()   │      │ - SSH connection     │
│ - sudo for elevation │      │ - reliability metrics│
└──────────────────────┘      └──────────────────────┘
          │                             │
          └──────────────┬──────────────┘
                         │ optional
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Execution Aspects                        │
│  - PersistenceAspect: audit logging                         │
│  - RetryAspect: transient failure retry                     │
│  - TimeoutAspect: global timeout policy                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `UbuntuIdentityManager`

**Location:** `source/packages/ccai/sysman/linux/ubuntu/identitymanagement.py`

| Method | Signature | Description |
|---|---|---|
| `join_realm` | `(spec) → UbuntuIdentityActionResult` | `realm join --user=<admin> <realm>` with password on stdin |
| `leave_realm` | `(realm=None) → UbuntuIdentityActionResult` | `realm leave [<realm>]` |
| `get_realm_status` | `() → UbuntuRealmStatus` | `realm list` parsed |
| `list_joined_realms` | `() → list[UbuntuRealmInfo]` | `realm list` parsed multi-stanza |
| `sync_users` | `() → UbuntuIdentityActionResult` | `sssctl cache-expire -E` |
| `resolve_user` | `(username) → UbuntuUserResolveResult` | `getent passwd <username>` parsed |
| `resolve_group` | `(groupname) → UbuntuGroupResolveResult` | `getent group <groupname>` parsed |

### `UbuntuIdentityManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no commands are executed |
| `elevated` | `bool` | `False` | When `True`, write commands pass `elevated=True` |
| `realm_executable` | `str` | `"realm"` | Path / name of the `realm` binary |
| `sssctl_executable` | `str` | `"sssctl"` | Path / name of the `sssctl` binary |
| `getent_executable` | `str` | `"getent"` | Path / name of the `getent` binary |
| `default_realm` | `str \| None` | `None` | Fallback realm for `leave_realm(None)` |

---

## Data Models

### Outcome / status

```python
class UbuntuIdentityActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class UbuntuIdentityActionResult(FeatureActionResult):
    realm: Optional[str] = None
    username: Optional[str] = None
    message: Optional[str] = None
```

`UbuntuIdentityActionStatus` is aliased to the shared
`featurebase.ActionStatus`; the per-feature alias preserves the
contract's prefixed-naming requirement without forking the enum
definition.

### Realm models

```python
class UbuntuRealmKind(Enum):
    FREEIPA          = "freeipa"
    ACTIVE_DIRECTORY = "active_directory"
    LDAP             = "ldap"
    UNKNOWN          = "unknown"


@dataclass
class UbuntuRealmJoinSpec:
    realm: str
    admin_user: str
    admin_password: str
    computer_name: Optional[str] = None
    ou_path: Optional[str] = None
    domain_controller: Optional[str] = None


@dataclass
class UbuntuRealmInfo:
    realm: str
    realm_kind: UbuntuRealmKind
    is_default: bool = False
    domain_controller: Optional[str] = None
    last_sync: Optional[str] = None


@dataclass
class UbuntuRealmStatus:
    joined: bool
    current_realm: Optional[str] = None
    realm_kind: Optional[UbuntuRealmKind] = None
    computer_name: Optional[str] = None
```

### Resolve models

```python
@dataclass
class UbuntuUserResolveResult:
    username: str
    uid: Optional[int] = None
    gid: Optional[int] = None
    home: Optional[str] = None
    shell: Optional[str] = None
    realm: Optional[str] = None
    groups: list[str] = field(default_factory=list)
    found: bool = False


@dataclass
class UbuntuGroupResolveResult:
    groupname: str
    gid: Optional[int] = None
    members: list[str] = field(default_factory=list)
    realm: Optional[str] = None
    found: bool = False
```

### Exceptions

```python
class UbuntuIdentityQueryError(Exception): ...
```

`UbuntuIdentityQueryError` is raised by `list_joined_realms` (and,
transitively, `get_realm_status`) when `realm list` exits non-zero.
`resolve_user` / `resolve_group` do NOT raise on `getent` exit code
2; they return a `found=False` result instead, so callers can
branch on existence without try/except scaffolding.

---

## Realm-Kind Mapping

The cross-OS contract defines four `RealmKind` values:
`ACTIVE_DIRECTORY`, `FREEIPA`, `OPEN_DIRECTORY`, `KERBEROS_ONLY`.
Ubuntu's `realm` tool surfaces only three back-end kinds: `ipa`,
`active-directory`, and `ldap`/`krb5`.  Ubuntu maps them as follows:

| `realm list` `server-software` | `UbuntuRealmKind` | Cross-OS contract analog |
|---|---|---|
| `ipa` | `FREEIPA` | `FREEIPA` |
| `active-directory` | `ACTIVE_DIRECTORY` | `ACTIVE_DIRECTORY` |
| `ldap` | `LDAP` | `KERBEROS_ONLY` |
| `krb5` | `LDAP` | `KERBEROS_ONLY` |
| (anything else) | `UNKNOWN` | (no analog; surfaced verbatim) |

The cross-OS contract's `OPEN_DIRECTORY` value is macOS-only and is
not modelled on Ubuntu.  The cross-OS `KERBEROS_ONLY` value is
modelled by `UbuntuRealmKind.LDAP` because realmd reports both
plain-Kerberos and LDAP realms as the same back-end kind on the
Linux side.

### AD vs FreeIPA divergence

Ubuntu's `realm` tool unifies AD and FreeIPA enrollment under a
single CLI, but the underlying realmd back-ends differ in
substantive ways:

- **FreeIPA** uses the IPA enrollment API (`ipa-client-install`
  internally).  The host receives a Kerberos keytab signed by the
  IPA CA, an LDAP search base derived from the realm DNS suffix,
  and SSSD configuration that points at `_kerberos._tcp.<realm>`
  SRV records.
- **Active Directory** uses `adcli` internally to perform a
  `LDAP add` of a `computer` object plus a Kerberos `setpw` of the
  machine account password.  The host receives a Kerberos keytab
  signed by the AD KDC and SSSD configuration that uses
  `_ldap._tcp.<realm>` SRV records.

The cross-OS contract abstracts this divergence behind the single
`join_realm` verb.  Callers that need back-end-specific behaviour
(e.g. to enable IPA-only `--mkhomedir` semantics) must inject
`extra_options` via a follow-up batch — Phase-3 Wave 4.5 does not
expose them.

### LDAP / Kerberos-only

Ubuntu's `realm join --realm-type=krb5` flow is supported in
principle but is rare in practice.  The current implementation
emits the same `realm join --user=<admin> <realm>` argv shape for
all three back-end kinds; callers that need `--realm-type` overrides
should request a follow-up.

---

## Key Method Designs

### `join_realm(spec)`

1. Build the argv:
   `["realm", "join", "--user=<admin_user>", <optional placement>,
   "<realm>"]`.  Placement flags are `--computer-name=<name>`,
   `--computer-ou=<ou>`, and `--server=<dc>` when the corresponding
   spec fields are set.
2. If `config.dry_run` → return `DRY_RUN` with the would-have-been
   argv.
3. Call `_is_realm_joined(realm=spec.realm)`, which delegates to
   `list_joined_realms` and falls back to `False` on
   `UbuntuIdentityQueryError`.  When already joined → `SKIPPED`.
4. Dispatch the argv with `elevated=config.elevated` and
   `stdin=spec.admin_password`.  Map the runner result to a
   `UbuntuIdentityActionResult` with `realm=spec.realm` and
   `username=spec.admin_user`.

### `leave_realm(realm=None)`

1. Resolve the target realm: `realm` if not None, else
   `config.default_realm`, else `None`.
2. Build the argv: `["realm", "leave"]` plus the resolved realm as
   a positional argument when non-`None`.
3. If `config.dry_run` → return `DRY_RUN`.
4. When the resolved realm is non-`None`, call
   `_is_realm_joined(realm=resolved)`; when not joined → `SKIPPED`.
   When the resolved realm is `None` we cannot prove non-membership
   without parsing the entire `realm list`, so the call is dispatched
   unconditionally and realmd's own "not joined" handling decides
   the final exit code.
5. Dispatch with `elevated=config.elevated` (no stdin).

### `get_realm_status()` / `list_joined_realms()`

1. `list_joined_realms()` runs `realm list` (no elevation), parses
   the multi-stanza output, and returns a list of `UbuntuRealmInfo`.
2. `get_realm_status()` calls `list_joined_realms()` and:
   - Returns `UbuntuRealmStatus(joined=False)` when the list is
     empty.
   - Otherwise re-runs `realm list` to extract the `computer-name`
     line for the first stanza's realm.  (We do not cache the first
     `realm list` output because the parser does not retain attribute
     dictionaries — a small CPU cost in exchange for parser
     simplicity.  A follow-up may fold this into a single pass.)
   - Maps the first stanza's `server-software` to a
     `UbuntuRealmKind` via `_classify_realm_kind`.

### `sync_users()`

1. Build the argv: `["sssctl", "cache-expire", "-E"]`.
2. If `config.dry_run` → return `DRY_RUN`.
3. Dispatch with `elevated=config.elevated`.  No idempotency guard.

### `resolve_user(username)` / `resolve_group(groupname)`

1. Build the argv: `["getent", "passwd", username]` (or `"group"`).
2. Dispatch without elevation.
3. On non-zero exit → return a `found=False` result.
4. On zero exit, parse the colon-separated record:
   - `passwd`: `<name>:<x>:<uid>:<gid>:<comment>:<home>:<shell>`.
   - `group`: `<name>:<x>:<gid>:<member1>,<member2>,...`.
5. Inspect the first field for an `@` realm suffix; populate the
   `realm` field when present.

### `realm list` parser

`realm list` output is grouped into stanzas, each starting with the
realm's domain name at column 0, followed by indented `key: value`
lines.  Stanzas are separated by blank lines (and the trailing
stanza may not end with one).

The parser tracks a single `current_name` / `current_attributes`
state machine:

- A column-0 line starts a new stanza.  Any pending stanza is
  flushed first.
- An indented `key: value` line extends the current stanza.
- A blank line flushes the current stanza.

Each flushed stanza is materialised by `_build_realm_info` which
reads `server-software` and `configured` to populate the kind /
default flags.

---

## Dry-Run Behaviour

- `dry_run=True` disables: `join_realm`, `leave_realm`, `sync_users`.
- `dry_run=True` allows: `get_realm_status`, `list_joined_realms`,
  `resolve_user`, `resolve_group`.

Note that the read-side calls during `join_realm` (the
`_is_realm_joined` probe) are short-circuited along with the write —
under `dry_run` the manager returns `DRY_RUN` without dispatching
*any* probe.  This is intentional: dry-run mode is cheap to invoke
and should not stall on a slow / unreachable realmd installation.

---

## Correctness Properties

1. **No plaintext password on argv.** `join_realm` routes
   `spec.admin_password` through `stdin` on the runner call.  The
   recorded `command` field carries only the argv.  A test asserts
   that no token in the recorded argv contains the password
   substring.
2. **Idempotent join / leave.** `join_realm` returns `SKIPPED` when
   `realm list` already shows the host joined; `leave_realm` returns
   `SKIPPED` when the host is not joined to the requested realm.
3. **Dry-run blocks remote contact.** When `config.dry_run=True`, no
   `realm` / `sssctl` command that would reach a KDC / DC / SSSD
   socket is dispatched.
4. **Reads do not elevate.** `list_joined_realms`,
   `get_realm_status`, `resolve_user`, and `resolve_group` always
   dispatch with `elevated=False`, even when
   `config.elevated=True`.
5. **Query-error surface is typed.** `UbuntuIdentityQueryError`
   carries the failing argv, exit code, and stderr — diagnosis-ready
   data, not silent empty results.

---

## Testing Strategy

- `_MockCommandRunner` recording every `run` call and returning
  pre-seeded `RunResult` values keyed by argv tuple.
- Canonical `realm list` output samples for FreeIPA, AD, multi-realm,
  and "no realms configured" cases.
- `getent passwd` / `getent group` synthetic stdout lines for both
  local (`bob:x:1001:1001:...`) and directory-backed
  (`alice@example.com:*:1234:5678:...`) records.
- Assertions for:
  - The exact argv shape for each verb.
  - `elevated` flag propagation through writes.
  - Non-elevation of reads.
  - `stdin=spec.admin_password` on `join_realm`.
  - Absence of the password substring from the recorded `command`
    on every result.
  - Idempotency-skip paths for `join_realm` (already joined) and
    `leave_realm` (not joined).
  - Dry-run paths returning `DRY_RUN` without invoking the runner.
  - `get_realm_status` / `list_joined_realms` parsing fidelity.
  - `resolve_user` / `resolve_group` parsing for both local and
    directory-backed records, plus the `found=False` path.
  - `UbuntuIdentityQueryError` propagation on `realm list` failure.
  - Captured `exit_code` and `stderr` on a synthetic `realm join`
    failure.

No live KDC / DC / SSSD service required.

---

## Module Layout

```
source/packages/ccai/sysman/linux/ubuntu/
└── identitymanagement.py

source/testroots/testbase/
└── ccai/tests/sysman/linux/ubuntu/
    └── test_identitymanagement.py
```

---

## Seams With Other Features

| Seam | Owner | This Feature |
|---|---|---|
| Local-account creation | `3-user-group-management` | Out of scope; `resolve_user` only reads |
| Enterprise CA trust for LDAPS / Kerberos-over-TLS | `3-certificate-trust-management` | Coordinator installs the anchor first, then this feature joins |
| `sssd.service` lifecycle | `3-service-management` (when it lands) | Out of scope; coordinator restarts SSSD as needed |
| PAM / pwquality / faillock | `3-authentication-management` (RHEL today) | Out of scope; coordinator drives PAM updates separately |

---

## Cross-OS Identity-Model Concept Mapping

The cross-OS contract is intentionally lossy — every OS realises
realm membership through different primitives.  This table maps the
abstract concepts to the concrete Ubuntu surfaces:

| Cross-OS concept | Ubuntu surface |
|---|---|
| Realm join | `realm join --user=<admin> <realm>` (writes `/etc/sssd/sssd.conf`, `/etc/krb5.conf.d/`, `/etc/nsswitch.conf`) |
| Realm leave | `realm leave <realm>` (rolls back the SSSD config + machine account) |
| Realm membership probe | `realm list` (multi-stanza parse) |
| Directory cache refresh | `sssctl cache-expire -E` |
| User principal resolution | `getent passwd <user>` (NSS, which dispatches to SSSD) |
| Group principal resolution | `getent group <group>` (NSS, which dispatches to SSSD) |
| Computer / machine account | `realm list` `computer-name` line |
| Default realm | `realm list` `configured: kerberos-member` flag (first stanza) |

The Ubuntu manager exposes this table only at the verb level — there
is no `read_sssd_conf` / `set_sssd_directive` surface in this
package.  The RHEL canonical implementation exposes those file-level
verbs because the RHEL spec's coordinator workflows compose with
direct SSSD edits; the Ubuntu workflows in scope for Phase-3 Wave
4.5 do not require that depth.  When a future workflow surfaces the
need, a follow-up batch will lift the file-level verbs from RHEL
into the Ubuntu module unchanged.

---

## Deviations From the RHEL Canonical Spec

| RHEL surface | Ubuntu | Reason |
|---|---|---|
| `discover(domain) → RealmDiscovery` | Not exposed | Out of scope for Wave 4.5; `realm discover` is read-only and can be added without changing the existing verb signatures |
| `read_sssd_conf` / `write_sssd_dropin` / `set_sssd_directive` | Not exposed | SSSD config edits are owned by `realm join` itself on Ubuntu; direct edits not needed for the in-scope workflows |
| `read_krb5_conf` / `set_default_realm` / `add_kdc` / `remove_kdc` | Not exposed | Same reason as SSSD |
| `kinit` / `klist` / `kdestroy` | Not exposed | Per-user Kerberos ticket lifecycle is out of contract scope and out of Wave 4.5 scope |
| `restart_sssd` | Not exposed | Service lifecycle belongs to `3-service-management` |
| `ensure_nss_ordering` | Not exposed | NSS source ordering is owned by `3-authentication-management` (when it lands on Ubuntu) |
| `IdentityCredentialError` | Not raised | Ubuntu accepts the password as a required field on `UbuntuRealmJoinSpec`, so a missing-credential case is structurally impossible; a follow-up batch may introduce the explicit raise once a credential abstraction is wired |
| `IdentityToolNotAvailableError` | Not exposed | Tool absence surfaces as `UbuntuIdentityQueryError` from the `realm list` probe; the typed-tool-missing variant can be added when the Ubuntu coordinator workflows surface a need |

These deviations are intentional and tracked in `tasks.md`.  They do
not break cross-OS contract conformance because the contract's
required verb set is `join_realm`, `leave_realm`, `get_realm_status`,
`list_joined_realms`, `sync_users`, `resolve_user`, `resolve_group`
— all of which Ubuntu implements.
