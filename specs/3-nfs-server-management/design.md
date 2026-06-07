# Design

## Overview

The Ubuntu nfs-server-management feature is implemented as a single
module, `ccai.sysman.linux.ubuntu.nfsservermanagement`, exposing
a `UbuntuNfsServerManager` class that wraps Ubuntu's
`nfs-kernel-server` exports surface (`exportfs`, `showmount`, the
content of `/etc/exports`) through the `ICommandRunner` abstraction.

Follows the standard manager pattern; delegates service lifecycle
to `3-service-management`.

**See Also:**
- `codecraftai-sysman-common/specs/contracts/nfs-server-management/contract.md`
  — the cross-OS abstract contract.
- `codecraftai-sysman-linux-redhat/specs/3-nfs-server-management/`
  — the canonical reference implementation that extends the
  contract with `nfs.conf` and `idmapd.conf` support.
- `3-service-management` — `nfs-kernel-server.service` lifecycle.
- `3-firewall-management` — opening the `nfs` / `mountd` /
  `rpc-bind` service ports.
- `3-filesystem-management` — NFS client-side mount points and
  `/etc/fstab`.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│            Maintenance Coordinator                          │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuNfsServerManager                           │
│                                                             │
│  Writes:                                                    │
│    add_export(), remove_export(),                           │
│    reload_exports(), set_export_options()                   │
│  Reads:                                                     │
│    list_exports(), get_export_status()                      │
│                                                             │
│  Helpers:                                                   │
│    _read_exports_file()                                     │
│    _write_exports_file()                                    │
│    _invoke_exportfs_reload()                                │
│    _invoke_exportfs_v()                                     │
└────────────────┬──────────────────────────┬─────────────────┘
                 │                          │
                 ▼                          ▼
      ┌─────────────────────┐   ┌─────────────────────────┐
      │   ICommandRunner    │   │  /etc/exports (file)    │
      └─────────────────────┘   └─────────────────────────┘
```

Every command — including reads — flows through the injected
`ICommandRunner`.  The manager holds no `subprocess`, `open`, or
`pathlib` references.

---

## Data Models

```python
UbuntuNfsActionStatus = ActionStatus  # alias from featurebase


@dataclass
class UbuntuNfsManagerConfig(FeatureConfig):
    exports_file: str = "/etc/exports"
    service_name: str = "nfs-kernel-server"


@dataclass
class UbuntuNfsActionResult(FeatureActionResult):
    path: str | None = None
    share_name: str | None = None
    message: str | None = None


@dataclass
class UbuntuNfsClientSpec:
    host: str = "*"
    options: list[str] = field(default_factory=lambda: [
        "rw", "sync", "no_subtree_check",
    ])


@dataclass
class UbuntuNfsExportSpec:
    path: str
    clients: list[UbuntuNfsClientSpec]
    share_name: str | None = None  # unused on Linux; preserved for
                                   # cross-OS-parity reasons


@dataclass
class UbuntuNfsExportInfo:
    path: str
    share_name: str | None
    clients: list[UbuntuNfsClientSpec]
    is_active: bool


@dataclass
class UbuntuNfsExportStatus:
    path: str
    is_active: bool
    client_count: int
    flags: list[str]


class UbuntuNfsQueryError(Exception):
    """Raised when an exports read fails for a non-recoverable reason."""
```

---

## Verb / Tool Mapping

| Verb | Ubuntu tool |
|------|-------------|
| `add_export` | `cat /etc/exports` + parse + render + `tee /etc/exports` + `exportfs -ra` |
| `remove_export` | `cat /etc/exports` + parse + render + `tee /etc/exports` + `exportfs -ra` |
| `list_exports` | `cat /etc/exports` + parse + `exportfs -v` for active state |
| `reload_exports` | `exportfs -ra` |
| `get_export_status` | `exportfs -v` parse for the path |
| `set_export_options` | `cat` + parse + render-with-new-options + `tee` + `exportfs -ra` |

Reads (`cat /etc/exports`, `exportfs -v`) never elevate.  Writes
(`tee /etc/exports`, `exportfs -ra`) elevate per `config.elevated`.

---

## Export-Path Mapping

The abstract `path` parameter maps to a single line inside
`/etc/exports`.  Multiple Ubuntu hosts may also use drop-in files
under `/etc/exports.d/*.exports`; this pilot manages the primary
file only.  Drop-in management is a tracked follow-up captured in
`tasks.md`.

| Field | Source |
|-------|--------|
| Configuration location | `/etc/exports` |
| Reload mechanism | `exportfs -ra` |
| Path-to-name normalisation | None — Ubuntu identifies exports by path directly. |

`UbuntuNfsExportSpec.share_name` is preserved for parity with the
Windows implementation (where shares are addressed by share-name,
not by path) but is ignored by every Ubuntu verb.

---

## Cross-OS Exports-Syntax Concept Map

The cross-OS contract intentionally generalises the verb shape but
not the file format.  Each OS must render exports in its native
on-disk syntax.  This table summarises the divergence — every spec
in the `nfs-server-management` family includes it for cross-reference.

| Concept | Ubuntu / Linux | macOS (Darwin) | Windows (Server for NFS) |
|---------|----------------|----------------|--------------------------|
| File path | `/etc/exports` | `/etc/exports` | n/a — share table is in the registry |
| Line shape | `<path> <client>(<opts>) <client>(<opts>)…` | `<path> -<opts> <client> [<client>…]` | n/a — `New-NfsShare -Path <path> -Name <name> -Authentication sys -Permission ReadWrite` |
| Options separator | parenthesised, comma-separated | dash-prefixed, space-separated | named cmdlet parameters |
| Per-client options | yes (each client carries its own opts) | shared across the line | yes (per-permission rules attached to the share) |
| Wildcard host | `*` | `-network <addr> -mask <mask>` (no `*`) | `-AllowRootAccess` / `-EnableUnmappedAccess` |
| Reload command | `exportfs -ra` | `nfsd update` | `Restart-Service NfsService` |
| Per-export reload | yes (re-export all) | yes (whole-table refresh) | no — service-wide restart only |

The Ubuntu manager renders Linux-canonical lines exclusively.  Lines
that already exist in the file using a different but semantically
equivalent option order are NOT collapsed: idempotency is exact-match
on the rendered string, so callers who switch option order will see
one rewrite the first time they reconcile and then steady-state
SKIPs thereafter.

---

## Configuration

```python
@dataclass
class UbuntuNfsManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuNfsServerManager`.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param exports_file: Absolute path to the primary exports
                             file.  Default ``/etc/exports``.
        :param service_name: systemd unit name used by callers who
                             wish to restart the daemon.  This module
                             does NOT call systemctl directly.
                             Default ``"nfs-kernel-server"``.
    """
    exports_file: str = "/etc/exports"
    service_name: str = "nfs-kernel-server"
```

---

## Key Method Designs

### `add_export(spec)`

```
1. Build the canonical write argv: ["tee", config.exports_file].
2. Read the current file via ``cat`` (no elevation).  None if missing.
3. Parse the existing file into list[UbuntuNfsExportInfo].
4. If an existing export at spec.path has identical clients → SKIPPED.
5. Render a new content string with the spec's line upserted (replace
   when path exists, append otherwise).
6. Dry-run? → DRY_RUN with the would-have-been argv.
7. Dispatch the ``tee`` write with the rendered content on stdin.
   FAILED on non-zero exit.
8. Dispatch ``exportfs -ra`` (the canonical "re-export all" reload).
   FAILED if non-zero — surface the reload's argv / exit / stderr.
9. SUCCESS — return the result with ``path`` populated.
```

### `remove_export(path)`

```
1. Build the write argv.
2. Read the file via ``cat``.  When the file is missing → SKIPPED.
3. Strip every line whose first token equals ``path``.
4. Compare to the original content; identical → SKIPPED.
5. Dry-run? → DRY_RUN.
6. Dispatch ``tee``.  FAILED on non-zero exit.
7. Dispatch ``exportfs -ra``.  FAILED if non-zero.
8. SUCCESS.
```

### `list_exports()`

```
1. Read /etc/exports via ``cat`` (no elevation).
   Empty list when the file is missing.
2. Parse each non-comment, non-blank line into UbuntuNfsExportInfo.
3. Read active state via ``exportfs -v`` (no elevation).
   Failure with stderr → UbuntuNfsQueryError.
4. For each parsed export, set is_active=True iff the path appears
   in the exportfs -v output.
5. Return the list.
```

### `reload_exports()`

```
1. Build ["exportfs", "-ra"].
2. Dry-run? → DRY_RUN.
3. Dispatch (elevated per config).
4. Map exit code to SUCCESS / FAILED.
```

### `get_export_status(path)`

```
1. Dispatch ``exportfs -v`` (no elevation).
   Non-zero with stderr → UbuntuNfsQueryError.
2. Parse the output.  When ``path`` is absent → status with
   is_active=False, client_count=0, flags=[].
3. When present, count distinct clients and pull flags from the
   first client's parenthesised options list.
```

### `set_export_options(path, options)`

```
1. Read the file via ``cat``.  Missing → SKIPPED.
2. Locate the line whose first token equals ``path``.  None → SKIPPED.
3. For each client on the line, replace its options list with the
   supplied list (preserving client host order).
4. Compare rendered output to existing.  Identical → SKIPPED.
5. Dry-run? → DRY_RUN.
6. Dispatch ``tee`` and chain ``exportfs -ra``.
```

---

## Export-Line Parser / Renderer

Two pure-Python helpers live at module scope:

- `_parse_exports_file(content) → list[UbuntuNfsExportInfo]`
- `_render_exports_file(exports) → str`

The parser splits each line on whitespace; the first token is the
path, every remaining token is `<host>(<opt1>,<opt2>,…)`.  Tokens
without a parenthesised options list use the default options
`["rw", "sync", "no_subtree_check"]`.

The renderer walks the parsed list and produces one canonical line
per export.  Empty client lists raise — the contract requires at
least one client per export.

A second parser, `_parse_exportfs_v_output(text)`, produces a
`dict[str, list[str]]` mapping path → flag list, used by
`get_export_status` and the active-state probe inside
`list_exports`.

---

## Dry-Run Behaviour

Dry-run disables every mutating entry point (`add_export`,
`remove_export`, `reload_exports`, `set_export_options`).  Read-only
queries — including the `cat` / `exportfs -v` probes that drive
idempotency decisions inside the write paths — run freely.

---

## Correctness Properties

1. **Idempotent exports.** `add_export` / `remove_export` /
   `set_export_options` all SKIP when the rendered file content
   already matches the requested state.
2. **Reload-after-change.** Every successful mutation that touches
   `/etc/exports` is followed by `exportfs -ra` unless the mutation
   was SKIPPED.
3. **Reload-failure visibility.** When the chained `exportfs -ra`
   fails after a successful `tee`, the verb returns FAILED and the
   reload's `command` / `exit_code` / `stderr` is on the returned
   result.
4. **Read isolation.** Read verbs never pass `elevated=True`; the
   `cat` / `exportfs -v` calls used inside write paths to drive
   idempotency also do not elevate.
5. **Service delegation.** This module never invokes `systemctl`.
   Daemon restart is the responsibility of `3-service-management`.

---

## Testing Strategy

- `_MockCommandRunner` records every call and returns pre-seeded
  `RunResult` values keyed by argv tuple.
- Parser tests exercise `_parse_exports_file` and
  `_parse_exportfs_v_output` against sample fixtures.
- Renderer tests verify canonical Linux line shape (single space
  between path and clients, comma-separated options, no leading or
  trailing whitespace).
- Idempotency tests — `add_export` / `remove_export` /
  `set_export_options` all return SKIPPED when the file content
  already matches.
- Reload failure-propagation tests verify that a failing
  `exportfs -ra` after a successful `tee` produces a FAILED result
  with the reload's argv / stderr.
- Elevation tests verify writes pass `elevated=True` and reads do
  not.
- Dry-run sweep verifies that `tee` / `exportfs -ra` are NOT
  dispatched.
- Failure-mapping sweep verifies that a non-zero `tee` produces
  FAILED with the runner's stdout / stderr / exit code.

---

## Seams With Other Features

| Seam | Owner | This Feature |
|---|---|---|
| `nfs-kernel-server.service` lifecycle | `3-service-management` | Delegated |
| Opening NFS ports | `3-firewall-management` | Coordinator opens `nfs`/`mountd`/`rpc-bind` services there |
| Client-side NFS mounts | `3-filesystem-management` | Out of scope |
| `/etc/idmapd.conf` (NFSv4 idmapping) | RHEL extension only | Out of scope for the Ubuntu pilot |
| `/etc/nfs.conf` directives | RHEL extension only | Out of scope for the Ubuntu pilot |

---

## Worked Example — Adding A New Export

The following walk-through shows what the manager dispatches for
the canonical "create a new world-readable export" flow.

```python
runner = LocalCommandRunner()
config = UbuntuNfsManagerConfig(elevated=True)
manager = UbuntuNfsServerManager(runner=runner, config=config)
spec = UbuntuNfsExportSpec(
    path="/srv/share",
    clients=[UbuntuNfsClientSpec(
        host="*",
        options=["rw", "sync", "no_subtree_check"],
    )],
)
result = manager.add_export(spec=spec)
```

The manager dispatches the following sequence:

1. `["cat", "/etc/exports"]` (`elevated=False`) — read the current
   file.  The captured stdout becomes the parsed export table
   baseline.
2. `["tee", "/etc/exports"]` (`elevated=True`, `stdin=<rendered
   content>`) — atomically rewrite the file with the new line
   appended.  Stdin contains the existing content plus
   `/srv/share *(rw,sync,no_subtree_check)\n`.
3. `["exportfs", "-ra"]` (`elevated=True`) — re-export every
   entry, picking up the new line.

If the existing file already contained
`/srv/share *(rw,sync,no_subtree_check)\n` exactly, step 1 is the
only command dispatched and the verb returns `SKIPPED` without
issuing the `tee` or `exportfs -ra` calls.

If `dry_run=True`, only the `cat` probe runs (so that idempotency
can be evaluated); the verb returns `DRY_RUN` with `command`
populated to `["tee", "/etc/exports"]`.

If the `tee` write succeeds but the chained `exportfs -ra` call
fails with `exit_code=2 stderr="exportfs: invalid"`, the verb
maps to `FAILED` and the returned result carries
`command=["exportfs", "-ra"]`, `exit_code=2`, and
`stderr="exportfs: invalid"`.  Note that the file mutation has
already occurred in this case — recovery is the caller's
responsibility (re-issue with corrected spec or roll back via
`remove_export`).

---

## Worked Example — Removing An Export Cleanly

```python
result = manager.remove_export(path="/srv/share")
```

1. `["cat", "/etc/exports"]` — read the current file.  Missing
   file → `SKIPPED`.
2. The parser strips every line whose first token equals
   `/srv/share`.  No matching line → `SKIPPED`.
3. `["tee", "/etc/exports"]` — write the truncated content.
4. `["exportfs", "-ra"]` — reload.

Note that unlike the RHEL canonical, this Ubuntu pilot does NOT
remove the exports file when the last line is stripped.  The file
is rewritten as empty rather than `rm`-ed.  This matches Debian's
package-defaults expectation: `nfs-kernel-server` ships with an
empty `/etc/exports` and removing the file would surprise users
who expect the package's configuration file to remain in place.

---

## Worked Example — Active vs. Configured

`list_exports` returns one record per configured export, with
`is_active` set based on whether the daemon currently advertises
the path.  Consider a host where the file content is

```
/srv/share *(rw,sync,no_subtree_check)
/srv/inactive 10.0.0.0/24(ro,sync,no_subtree_check)
```

and `exportfs -v` emits

```
/srv/share \t<world>(sync,wdelay,hide,no_subtree_check,sec=sys,rw)
```

(`/srv/inactive` is configured but the daemon has not picked it
up — perhaps because reload was skipped after the last edit).
`list_exports()` returns:

| `path` | `clients[0].host` | `is_active` |
|--------|------------------|-------------|
| `/srv/share` | `*` | `True` |
| `/srv/inactive` | `10.0.0.0/24` | `False` |

The active probe uses the same `_parse_exportfs_v_output` helper
that backs `get_export_status`.  Failures with non-empty stderr
raise `UbuntuNfsQueryError` rather than silently returning empty;
this matches the read-error surfacing in every other Ubuntu
manager.

---

## Edge Cases And Their Handling

| Edge case | Handling |
|-----------|----------|
| `/etc/exports` is missing | `add_export` treats existing content as `""` and writes a fresh file.  `remove_export` and `set_export_options` return `SKIPPED`. `list_exports` returns `[]`. |
| `/etc/exports` contains comment lines | Comments and blank lines are preserved verbatim through every write path. |
| Multiple lines with the same path | `_upsert_export` collapses duplicates to a single canonical line at the first occurrence; `_remove_export_lines` strips every duplicate. |
| Client token without parenthesised options | The canonical default options (`["rw", "sync", "no_subtree_check"]`) are applied. |
| Empty client list on the input spec | `_render_export_line` raises `ValueError` — the contract requires at least one client. |
| Duplicate clients on the same line | Preserved verbatim during round-trip; idempotency considers them distinct positional entries. |
| `exportfs -v` emits wrapped lines (path on its own line, client on the next indented line) | `_parse_exportfs_v_output` joins them. |

---

## Follow-Ups

- **Drop-in support.** Add `/etc/exports.d/<name>.exports` write
  paths analogous to the RHEL canonical implementation.  Track via
  `tasks.md`.
- **Per-client option diff.** Currently `add_export` performs an
  exact-match idempotency check on the full rendered line.  A
  follow-up could fold semantically-equivalent option orderings
  (e.g., `rw,sync,no_subtree_check` vs.
  `sync,rw,no_subtree_check`) into a single canonical form.
- **`showmount -e localhost` integration.** A future `showmount()`
  read verb could expose the daemon's externally-visible export
  list for cross-checking.
- **NFSv4 idmapping.** Adding `/etc/idmapd.conf` directive surface
  to bring this manager into parity with the RHEL canonical.
- **Per-export reload.** Currently every mutation triggers a full
  `exportfs -ra`.  `exportfs -i <path>` could re-export a single
  path, but the kernel does not surface a per-path failure mode
  cleanly, so the pilot trades reload precision for simpler error
  surfacing.

---

## Spec-vs-Contract Drift

This Ubuntu spec deviates from the cross-OS contract in two
documented places:

1. **Per-client option scoping.** The contract's `NfsExportSpec`
   carries `clients: list[str]` and `options: list[str]` as
   parallel lists, with options applied uniformly to every
   client.  This Ubuntu spec instead nests options inside each
   client entry via `UbuntuNfsClientSpec(host, options)`.  This
   is required because Linux exports natively scope options per
   client (`*(rw,sync) 10.0.0.0/24(ro,sync)` is a single line
   with two distinct option lists).  Callers who want uniform
   options across every client supply identical `options` on
   each client.
2. **`UbuntuNfsExportInfo` instead of `NfsExport`.** The contract
   names the parsed-export dataclass `NfsExport`; this spec uses
   `UbuntuNfsExportInfo` to match the naming convention used by
   the rest of the Ubuntu manager surface (e.g.,
   `UbuntuMtaAliasInfo`, `UbuntuDnsZoneInfo`).  The field set
   matches the contract: `path`, `clients`, plus the contract's
   abstract `active` flag (renamed `is_active` in this spec) and
   the cross-OS `share_name` field.

Both deviations are documented here for future maintainers; the
behaviour and verb shape match the contract exactly.
