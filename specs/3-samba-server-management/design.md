# Design

## Overview

The Ubuntu samba-server-management feature is implemented as a single
module, `automojo.sysman.linux.ubuntu.sambaservermanagement`, exposing
a `UbuntuSambaServerManager` class that wraps Ubuntu's stock
`samba` package surface (`smbcontrol`, `smbpasswd`, `pdbedit`, the
content of `/etc/samba/smb.conf`) through the `ICommandRunner`
abstraction.

Follows the standard manager pattern; delegates service lifecycle
to `3-service-management`.

**See Also:**
- `automojo-sysman-common/specs/contracts/samba-server-management/contract.md`
  — the cross-OS abstract contract.
- `3-service-management` — `smbd.service` / `nmbd.service` lifecycle.
- `3-firewall-management` — opening SMB ports 137/138/139/445.
- `3-user-group-management` — local POSIX accounts backing Samba
  users.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│            Maintenance Coordinator                          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuSambaServerManager                         │
│                                                             │
│  Writes:                                                    │
│    add_share(), remove_share(),                             │
│    set_share_permissions(), reload_config(),                │
│    add_samba_user(), remove_samba_user()                    │
│  Reads:                                                     │
│    list_shares(), get_share(), list_samba_users()           │
│                                                             │
│  Helpers:                                                   │
│    _read_smb_conf()                                         │
│    _write_smb_conf()                                        │
│    _invoke_smbcontrol_reload()                              │
│    _invoke_pdbedit_list()                                   │
└────────────────┬──────────────────────────┬─────────────────┘
                 │                          │
                 ▼                          ▼
      ┌─────────────────────┐   ┌─────────────────────────┐
      │   ICommandRunner    │   │ /etc/samba/smb.conf     │
      └─────────────────────┘   └─────────────────────────┘
```

Every command — including reads — flows through the injected
`ICommandRunner`.  The manager holds no `subprocess`, `open`, or
`pathlib` references.

---

## Data Models

```python
UbuntuSambaActionStatus = ActionStatus  # alias from featurebase


@dataclass
class UbuntuSambaManagerConfig(FeatureConfig):
    smb_conf: str = "/etc/samba/smb.conf"
    service_name: str = "smbd"


@dataclass
class UbuntuSambaActionResult(FeatureActionResult):
    share_name: str | None = None
    username: str | None = None
    message: str | None = None


@dataclass
class UbuntuSambaShareSpec:
    name: str
    path: str
    description: str | None = None
    read_only: bool = False
    valid_users: list[str] | None = None
    read_list: list[str] | None = None
    write_list: list[str] | None = None
    browseable: bool = True
    guest_ok: bool = False


@dataclass
class UbuntuSambaShareInfo:
    name: str
    path: str
    description: str | None
    read_only: bool
    valid_users: list[str] | None
    read_list: list[str] | None
    write_list: list[str] | None
    browseable: bool
    guest_ok: bool
    is_active: bool
    connection_count: int | None


@dataclass
class UbuntuSambaShareAcl:
    read_users: list[str]
    write_users: list[str]
    full_access_users: list[str]
    denied_users: list[str]


@dataclass
class UbuntuSambaUserInfo:
    username: str
    enabled: bool
    last_password_change: str | None
    description: str | None
```

---

## Sentinel-Bracketed Sections

The `smb.conf` parser tracks section boundaries by the `[name]`
header line.  A share section starts at its header and ends at the
next non-comment line that begins with `[`.  This is the same
"sentinel-bracketed block" pattern used by the Ubuntu
`sshserverconfiguration` Match-block parser.

Reserved section names (`[global]`, `[homes]`, `[printers]`) are
recognised by the share parser and excluded from `list_shares()`
output.  The parser preserves comment lines and blank lines verbatim
inside the body of a section so that reconciliation loops do not
churn formatting.

---

## Key Method Designs

### `add_share(spec)`

1. Read `/etc/samba/smb.conf` via `cat`.
2. Parse into ordered sections.
3. If `[spec.name]` exists with directives matching `spec` exactly →
   `SKIPPED`.
4. Render the new content with the spec's section either replacing
   the existing block or appended at end of file.
5. Dry-run: `DRY_RUN` with the would-have-been argv on the result.
6. Dispatch `tee /etc/samba/smb.conf` with the new content on stdin.
7. Chain `smbcontrol all reload-config`; failure maps the result to
   `FAILED`.

### `remove_share(name)`

1. Read the file; missing → `SKIPPED`.
2. Parse and drop the `[name]` section block.
3. If nothing changed → `SKIPPED`.
4. Dry-run: `DRY_RUN`.
5. `tee` + chained reload.

### `add_samba_user(name, password)`

1. Probe `pdbedit -L`; if user already present → `SKIPPED`.
2. Dry-run: `DRY_RUN` (recorded `command` is the **sanitised** argv,
   never the password).
3. Dispatch `["smbpasswd", "-s", "-a", name]` with
   `stdin = password + "\n" + password + "\n"` (the `-s` flag reads
   the new password and confirmation off stdin).
4. The recorded `command` field is the bare argv — the password
   never appears on argv to begin with, so sanitisation is a
   defence-in-depth no-op for Ubuntu.

### `reload_config()`

Unconditional `smbcontrol all reload-config`.  Operational verb,
never skips.

---

## Dry-Run Behaviour

Dry-run disables every mutating path (smb.conf writes, smbpasswd,
smbcontrol).  Read-only queries (`cat`, `pdbedit -L`) run freely
even under dry-run because they drive idempotency decisions.

---

## Correctness Properties

1. **No passphrase on argv.** `add_samba_user` routes the password
   on stdin; the recorded `command` field is sanitised
   (defence-in-depth) and contains only `["smbpasswd", "-s", "-a",
   "<name>"]`.
2. **Block-boundary strip.** `remove_share` strips the `[name]`
   header and every directive until the next section header,
   leaving surrounding sections intact.
3. **Idempotent share definitions.** `add_share` returns `SKIPPED`
   when the existing block already carries every requested
   directive.  Order of directives within a section is normalised
   before comparison.
4. **Reload-after-change.** Every successful mutation that touches
   `/etc/samba/smb.conf` is followed by `smbcontrol all
   reload-config`.

---

## Testing Strategy

- `_MockCommandRunner` with pre-seeded `RunResult` values keyed by
  argv tuple.
- Parser tests for `pdbedit -L -v` output.
- Tests that `add_samba_user` never places the password on argv;
  verify stdin carries the expected bytes (twice).
- Tests for share CRUD block-boundary correctness.
- Dry-run, elevation, and failure-mapping sweeps.
- Sanitisation test: `add_samba_user` recorded `command` MUST NOT
  contain the password substring.

---

## Seams With Other Features

| Seam | Owner | This Feature |
|---|---|---|
| `smbd.service` / `nmbd.service` lifecycle | `3-service-management` | Delegated |
| SMB ports through the firewall | `3-firewall-management` | Out of scope here |
| Local POSIX accounts | `3-user-group-management` | Samba users must exist as POSIX users first for security modes that require it |
