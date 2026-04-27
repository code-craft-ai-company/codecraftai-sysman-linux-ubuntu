# Design

## Overview

The Ubuntu ACL manager wraps the canonical POSIX ACL toolchain
(`getfacl`, `setfacl`) shipped in the Debian / Ubuntu `acl` package
and exposes the cross-OS ACL-management contract through a single
class, `UbuntuAclManager`, dispatched entirely through the injected
`ICommandRunner`.

This design document covers:

1. The cross-OS ACL-model divergence that drives the per-OS naming
   and per-OS spec divergence.
2. The Ubuntu-specific verb-to-command mapping table.
3. The principal-type model (POSIX `user` / `group` / `other` /
   `mask`).
4. Idempotency, dry-run, elevation, and error-surface behaviour.
5. Internal helpers (parsing `getfacl`, rendering `setfacl` tokens).

---

## Cross-OS ACL-Model Divergence

ACLs are nominally a single concept ("who can do what to a path"),
but the three OS families this product targets carry deeply
divergent ACL models.  Pretending the divergence does not exist
produces leaky abstractions; making the divergence explicit lets
each per-OS implementation stay honest to its native model and lets
cross-OS code carry only the intersection that actually generalises.

| Dimension | POSIX (Ubuntu / RHEL) | macOS (NFSv4-style) | Windows (NTFS / NT DACL) |
|-----------|-----------------------|---------------------|--------------------------|
| Permission set | 3 bits per principal: `r`, `w`, `x`. | ~20 fine-grained tokens (`read`, `write`, `execute`, `delete`, `append`, `readattr`, `writeattr`, `readextattr`, `writeextattr`, `readsecurity`, `writesecurity`, `chown`, `list`, `search`, `add_file`, `add_subdirectory`, `delete_child`, `file_inherit`, `directory_inherit`, `limit_inherit`, `only_inherit`). | `[FileSystemRights]` enum: `FullControl`, `Modify`, `ReadAndExecute`, `Read`, `Write`, `ListDirectory`, `ReadData`, `WriteData`, `Delete`, `Synchronize`, `ExecuteFile`, `ReadAttributes`, `WriteAttributes`, `ReadPermissions`, `ChangePermissions`, `TakeOwnership`. |
| ACE polarity | Always allow.  POSIX has no deny ACEs. | Allow or deny per ACE. | Allow or deny per ACE. |
| Inheritance | Default ACL on directories, applied on creation; no per-ACE inheritance flags. | Per-ACE flags: `file_inherit`, `directory_inherit`, `limit_inherit`, `only_inherit`. | Per-ACE flags: `OBJECT_INHERIT`, `CONTAINER_INHERIT`, plus a `NO_PROPAGATE_INHERIT` flag and a propagation flag set. |
| Effective-rights mask | A `mask::` row that clamps every named entry's effective rights. | None. | None. |
| Principal model | POSIX user / group name resolved via NSS; plus `other` and `mask` pseudo-principals. | Native user / group resolved via Directory Services; `everyone` keyword. | SID resolved via LSA, with `DOMAIN\name` and well-known SIDs (`S-1-1-0` Everyone, `S-1-5-18` SYSTEM). |
| Native tooling | `setfacl` / `getfacl` (extended-attributes-backed). | `chmod +a` / `chmod -a` / `ls -le`. | `Get-Acl` / `Set-Acl` (PowerShell, `[System.Security.AccessControl.FileSystemSecurity]`); `icacls.exe` (legacy CLI). |
| Default-ACL equivalent | First-class (the `default:` prefix). | Inheritance flags; no `default:` partition. | Inheritance flags; no `default:` partition. |

The contract papers over this divergence by reducing the ACE shape
to `principal + rights + allow + inherited + is_default`.  Each per-
OS implementation MAY (and on Ubuntu, does) carry additional fields
that have no cross-OS analogue.

This Ubuntu spec exposes the POSIX-native subset directly.  The
cross-OS contract carries the structural ceiling; this Ubuntu spec
fills it with the concrete POSIX shape.  Where the contract's
abstract `AclPrincipalKind.EVERYONE` / `AclPrincipalKind.SYSTEM`
values exist for cross-OS ergonomics, the Ubuntu manager
intentionally exposes only the four POSIX-native types.

---

## Module Layout

```
source/packages/automojo/sysman/linux/ubuntu/aclmanagement.py
```

Contents (top-to-bottom):

1. Module docstring (verb catalogue, file-I/O policy, elevation
   policy, idempotency policy).
2. Module-level constants (binary names, flag tokens).
3. `UbuntuAclActionStatus` (alias of the package-shared
   `ActionStatus`).
4. `UbuntuAcePrincipalType` enum (`USER`, `GROUP`, `OTHER`, `MASK`).
5. `UbuntuAclManagerConfig` dataclass.
6. `UbuntuAclActionResult` dataclass.
7. `UbuntuAceInfo` / `UbuntuAceSpec` / `UbuntuAclInfo` dataclasses.
8. `UbuntuAclQueryError` exception.
9. `UbuntuAclManager` class.
10. Module-level helpers: token rendering, parsing, comparison.

---

## Verb-to-Command Mapping (Ubuntu)

| Verb | Tool invocation | Notes |
|------|-----------------|-------|
| `get_acl(path)` | `getfacl <path>` | Header lines (`# file:`, `# owner:`, `# group:`) populate `UbuntuAclInfo.owner` / `.group`. |
| `list_aces(path)` | `getfacl <path>` (re-uses `get_acl`) | Returns regular-then-default ACEs as a flat list. |
| `set_acl(path, acl_spec)` | `setfacl -b <path>` then `setfacl -m <comma-joined> <path>` | Two-step "clear then set" so leftover ACEs cannot leak through. |
| `add_ace(path, principal, rights)` | `setfacl -m <token> <path>` | Token: `[d:]<type>:<name>:<rights>`. |
| `remove_ace(path, principal)` | `setfacl -x <token> <path>` | Token: `[d:]<type>:<name>` (rights NOT included; `setfacl -x` rejects them). |
| `clear_acl(path)` | `setfacl -b <path>` | Removes all extended ACEs while preserving the three base rows. |
| `copy_acl(src, dst)` | `getfacl <src>` -> `setfacl --set-file=- <dst>` | The runner's `stdin` channel carries the captured payload; this is the canonical idiom from the `setfacl(1)` man page. |

The `setfacl --set-file=-` form is the central Ubuntu-specific
choice in this design.  POSIX ACL specs travel through `setfacl` as
a textual stream that exactly mirrors `getfacl` output, so a single
runner dispatch carries the full payload through `stdin` without
any per-ACE shelling.

---

## Token Rendering Rules

Every `setfacl -m` and `setfacl -x` token follows the POSIX `acl(5)`
short-form:

```
[d:]<type>:<name>:<rights>     # for setfacl -m
[d:]<type>:<name>              # for setfacl -x
```

| Field | Render |
|-------|--------|
| `d:` prefix | Present iff `UbuntuAceSpec.default` is `True`. |
| `<type>` | Single character: `u`, `g`, `o`, `m`. |
| `<name>` | The principal name; rendered as the empty string when `principal is None` (i.e. for base `user::`, `group::`, `other::` rows, and for the `mask::` row). |
| `<rights>` | The three-character rights string verbatim (`rwx`, `r-x`, `---`). |

`set_acl`'s comma-joined token list relies on `setfacl`'s ability to
parse multiple comma-separated entries from a single `-m` argument;
the order of regular vs. default entries within the list does not
matter — `setfacl` dispatches each one to the right partition based
on the leading `d:` prefix.

---

## Parsing `getfacl` Output

`getfacl` emits one entry per line, with three optional leading
`#`-prefixed comment lines.  Example output for a directory carrying
both extended and default ACLs:

```
# file: /srv/data
# owner: alice
# group: staff
user::rwx
user:bob:r-x
group::r-x
group:devs:rwx
mask::rwx
other::r--
default:user::rwx
default:user:bob:r-x
default:group::r-x
default:mask::rwx
default:other::---
```

The parser:

1. Walks every line.
2. Skips blank lines.
3. Routes `#`-prefixed lines through a small header parser that
   extracts `# owner:` / `# group:` (other headers are ignored).
4. Strips a trailing `#effective:<rights>` comment when present
   (this is `getfacl`'s annotation when a named entry's effective
   rights are clamped by the mask).
5. Detects the `default:` prefix and shifts the remaining body into
   the default-ACE partition.
6. Splits the remaining body on `:`; rejects any row whose split
   does not yield exactly three fields.
7. Translates the first field (`user` / `group` / `other` / `mask`)
   into the corresponding `UbuntuAcePrincipalType`.
8. Treats an empty middle field as `principal=None` (base entries
   and the `mask::` / `other::` rows).

The parser intentionally does not attempt to recover from truly
malformed `getfacl` output — minimal `acl` packages on Ubuntu emit
the canonical format unconditionally, and any deviation indicates a
real bug we want to surface (not paper over).

---

## Idempotency

Each write verb runs an idempotency probe BEFORE dispatching:

| Verb | Probe behaviour |
|------|----------------|
| `set_acl` | Compares the parsed current ACL against `acl_spec` on the regular and default partitions independently.  Comparisons sort each partition by `(type, principal, rights)` so source order does not matter. |
| `add_ace` | Returns `SKIPPED` when an ACE matching `(type, principal, rights, default)` is already present. |
| `remove_ace` | Returns `SKIPPED` when no ACE matches the spec's `(type, principal, default)` triple, regardless of rights. |
| `clear_acl` | Returns `SKIPPED` when the parsed ACL has no extended ACEs (no named-user / named-group rows, no non-base mask, no default ACEs). |
| `copy_acl` | Returns `SKIPPED` when the source and destination ACLs are byte-equal on both partitions.  Note: owner / group are intentionally NOT compared — `copy_acl` is about ACE state, not file ownership. |

When a probe `getfacl` invocation itself fails (e.g. transient
filesystem error), the manager falls through and dispatches the
write anyway.  The write step's failure mode then reports the
underlying condition, and the caller sees a clean `FAILED` result
rather than a half-handled exception.

---

## Dry-Run Model

`config.dry_run=True` short-circuits every write verb at the very
top of the method, BEFORE any probe runs.  This means a dry-run
invocation does NOT consult `getfacl`, does NOT touch the runner,
and reports the would-have-been argv on `command`.

The justification: a dry-run is a "what would happen if I ran this"
preview; it should make zero side effects, including read-style
ones.  Reads issued by an unrelated caller — e.g. `get_acl` or
`list_aces` directly — continue to dispatch, since reads have no
side effects.

---

## Elevation Model

| Verb | Elevation |
|------|-----------|
| `get_acl`, `list_aces` | Never elevate. |
| Idempotency probes (`getfacl` calls inside write verbs) | Never elevate. |
| `set_acl`, `add_ace`, `remove_ace`, `clear_acl`, `copy_acl` (write step) | `config.elevated`. |

POSIX ACL writes typically require root or path-ownership.  The
elevation flag is the caller's contract for "I want this manager to
run mutating commands as root via the runner's elevation
mechanism".  Reads are deliberately excluded — `getfacl` against a
world-readable path does not need elevation, and forcing elevation
would defeat the point of read-only health probes against locked-
down hosts.

---

## Error Surface

| Surface | Trigger | Behaviour |
|---------|---------|-----------|
| `UbuntuAclQueryError` | `getfacl` exits non-zero on a read (`get_acl` / `list_aces`). | Raised with the failed argv, exit code, and stderr captured on the exception. |
| `UbuntuAclActionResult(status=FAILED)` | `setfacl` exits non-zero on any write step. | Returned (NOT raised) with `exit_code`, `stdout`, `stderr`, `command` populated. |
| Idempotency-probe failures | `getfacl` exits non-zero inside a write verb. | The probe is silently treated as "no current state known"; the write proceeds.  The write's failure surface reports the underlying condition cleanly. |

This split mirrors the rest of the package: read errors raise
typed exceptions, write errors return typed results.

---

## Configuration

`UbuntuAclManagerConfig` extends `FeatureConfig` with two
PATH-overridable binary names:

| Field | Default | Purpose |
|-------|---------|---------|
| `getfacl_path` | `"getfacl"` | PATH-resolved Debian / Ubuntu binary; tests can inject a wrapper. |
| `setfacl_path` | `"setfacl"` | PATH-resolved Debian / Ubuntu binary; tests can inject a wrapper. |
| `dry_run` (inherited) | `False` | When `True`, write verbs short-circuit. |
| `elevated` (inherited) | `False` | When `True`, write steps run elevated. |

There is no other configuration: ACL state lives entirely on the
target paths, and both tools are stateless wrappers on the kernel
ACL syscalls.

---

## Aspect Composition

`UbuntuAclManager` is safe under any aspect composition on the
runner because:

1. The manager never spawns a subprocess directly — every command
   flows through `self._runner.run(...)`.
2. The manager re-queries ACL state per write rather than caching;
   retry-aspect re-dispatches do not see stale state.
3. The manager's only state is the injected runner and config; per-
   call state lives in local variables.
4. Persistence aspects on the runner cannot mutate
   `UbuntuAclActionResult` shape; the manager always builds the
   result from the captured `RunResult`.

---

## Tests

The test module `test_aclmanagement.py` covers:

* Parsing `getfacl` output for a directory carrying named-user,
  named-group, mask, and default ACEs.
* `set_acl` with a multi-entry spec dispatching the canonical
  `setfacl -b` + `setfacl -m` pair.
* `set_acl` `SKIPPED` when the parsed ACL already matches.
* `set_acl` `DRY_RUN` short-circuiting the runner.
* `add_ace` dispatching the rendered `setfacl -m` token.
* `add_ace` `SKIPPED` when the ACE is already present.
* `remove_ace` dispatching the rendered `setfacl -x` token.
* `remove_ace` `SKIPPED` when the principal is absent.
* `clear_acl` dispatching `setfacl -b` and skipping when no extended
  ACEs are present.
* `copy_acl` reading the source via `getfacl` and piping into
  `setfacl --set-file=-`.
* `copy_acl` skipping when source and destination ACLs match.
* `list_aces` returning the full flat list.
* Writes elevating, reads never elevating.
* Failed commands surfacing stderr on the result.

Total: 14+ tests, all using a `_MockCommandRunner` that records
every call and returns pre-seeded `RunResult` values keyed by argv
tuple.  No live `getfacl` / `setfacl` is invoked.

---

## Spec-vs-Contract Drift

The cross-OS contract uses the names `AclSet` (for the per-path
snapshot), `AclEntry` (for an ACE), and `AclPrincipal` (for a
principal record carrying `kind` / `name` / `sid`).  This Ubuntu
spec exposes:

* `UbuntuAclInfo` instead of `UbuntuAclSet` — the suffix is `Info`
  for symmetry with the rest of this package's read-shape types
  (`UbuntuMountInfo`, `UbuntuFileInfo`, `UbuntuLocaleInfo`).
* `UbuntuAceInfo` and `UbuntuAceSpec` instead of `UbuntuAclEntry` —
  the `Info` / `Spec` split distinguishes parsed read shapes from
  caller-supplied write shapes; `UbuntuAclEntry` would conflate the
  two.
* `UbuntuAcePrincipalType` instead of an `UbuntuAclPrincipal`
  dataclass — POSIX has no SID concept, and the principal name is
  carried directly on `UbuntuAceInfo` / `UbuntuAceSpec` rather than
  through a wrapper dataclass.

The drift is intentional and is documented here for the per-OS
implementer's reference.  Cross-OS callers that need the contract's
names will see them in the `automojo-interfaces` adapter layer when
that layer materialises; the per-OS managers favour names that
align with the rest of their package.

The contract's verb signatures also include `recursive: bool` on
every write verb.  This Ubuntu spec intentionally drops `recursive`
from the per-OS verb signatures.  The rationale:

* The cross-OS contract was authored with a future "recursive ACL
  walk" extension in mind; on Ubuntu, that translates to `setfacl
  -R`, but recursive ACL writes are a class of operation that
  warrants a separate verb (with its own filter / report shape) and
  not a boolean flag bolted onto the per-path write path.
* Idempotency on a recursive write is qualitatively different from
  idempotency on a per-path write — comparing a single parsed ACL
  against a single requested spec is cheap; doing the same against
  a tree is potentially O(N) `getfacl` invocations and demands a
  caller-visible progress / partial-failure shape that the per-path
  result type does not carry.
* The `automojo-sysman-linux-redhat` reference impl ships
  recursion as a flag, but its broader scope (extended attributes,
  file capabilities) carries a richer result shape that this Ubuntu
  spec does not duplicate.

A follow-up spec — `3-acl-management-recursive` — will add
recursion as a first-class verb (`set_acl_recursive`,
`apply_acl_template`) once the recursive-walk reporting shape is
agreed across the OS family.

---

## Implementation Walkthrough

This section walks each verb end-to-end, showing the exact dispatch
sequence and the data flow.

### `get_acl(path)`

```
caller -> manager.get_acl(path)
    -> runner.run(["getfacl", path], elevated=False)
        -> RunResult(exit_code=0, stdout=<getfacl payload>, stderr="")
    -> _parse_getfacl_output(output, path)
        -> UbuntuAclInfo(path, owner, group, aces, default_aces)
    -> caller receives UbuntuAclInfo
```

Failure path: if `RunResult.exit_code != 0`, the manager raises
`UbuntuAclQueryError(command, exit_code, stderr)`.  The error body
includes the failed argv joined with spaces, the exit code, and the
captured stderr.

### `list_aces(path)`

```
caller -> manager.list_aces(path)
    -> manager.get_acl(path) (same as above)
    -> flat list[UbuntuAceInfo] = info.aces + info.default_aces
    -> caller receives list[UbuntuAceInfo]
```

### `set_acl(path, acl_spec)`

```
caller -> manager.set_acl(path, acl_spec)
    -> _render_setfacl_entries(spec=acl_spec)
        -> "u::rwx,u:bob:r-x,g::r-x,m::rwx,o::r--"
    -> command = ["setfacl", "-m", <rendered>, path]

    -- Dry-run gate --
    if config.dry_run:
        return UbuntuAclActionResult(status=DRY_RUN, command=...)

    -- Idempotency probe --
    current = manager.get_acl(path)  # may raise; we swallow
    if _acl_matches_spec(current, acl_spec):
        return UbuntuAclActionResult(status=SKIPPED, command=...)

    -- Clear step --
    runner.run(["setfacl", "-b", path], elevated=config.elevated)
    if exit_code != 0:
        return UbuntuAclActionResult(status=FAILED, ...)

    -- Modify step (via _dispatch) --
    return manager._dispatch(command=command, elevated=config.elevated)
```

The two-step "clear then modify" pattern is the canonical way to
express "replace the full ACL".  Without the `setfacl -b` clear,
existing ACEs not mentioned in `acl_spec` would persist — i.e.
`setfacl -m` is additive, not declarative.

### `add_ace(path, principal, rights=None)`

```
caller -> manager.add_ace(path, UbuntuAceSpec(...), rights=None)
    -> effective = _materialise_ace_spec(principal, rights)
    -> rendered = _render_single_setfacl_entry(spec=effective)
        -> "u:alice:rwx"
    -> command = ["setfacl", "-m", rendered, path]

    -- Dry-run gate --
    if config.dry_run:
        return DRY_RUN

    -- Idempotency probe --
    current = manager.get_acl(path)
    if _ace_already_present(current, effective):
        return SKIPPED

    -- Dispatch --
    return manager._dispatch(command=command, elevated=config.elevated,
                              path=path, principal=effective.principal)
```

### `remove_ace(path, principal)`

Same shape as `add_ace`, except:

* The token rendered is `[d:]<type>:<name>` (no rights).
* The idempotency check is `_ace_principal_present` (does the
  principal have ANY ACE?) rather than `_ace_already_present`.
* `setfacl -x` is dispatched instead of `setfacl -m`.

### `clear_acl(path)`

```
caller -> manager.clear_acl(path)
    -> command = ["setfacl", "-b", path]

    -- Dry-run gate --
    if config.dry_run:
        return DRY_RUN

    -- Idempotency probe --
    current = manager.get_acl(path)
    if not _has_extended_aces(current):
        return SKIPPED

    -- Dispatch --
    return manager._dispatch(command=command, elevated=config.elevated)
```

`_has_extended_aces` returns `True` when the parsed ACL has any
named-user / named-group / non-base-mask / default ACE.  The three
base rows (`user::<rwx>`, `group::<rwx>`, `other::<rwx>`) are not
"extended" — they are the file's classic mode bits in ACL form.

### `copy_acl(src, dst)`

```
caller -> manager.copy_acl(src, dst)
    -> command = ["setfacl", "--set-file=-", dst]

    -- Dry-run gate --
    if config.dry_run:
        return DRY_RUN

    -- Source read --
    runner.run(["getfacl", src], elevated=False)
    if exit_code != 0:
        return FAILED  # cannot copy what we cannot read

    -- Idempotency probe (best-effort) --
    src_info = _parse_getfacl_output(stdout, src)
    dst_info = manager.get_acl(dst)
    if _acl_infos_equal(src_info, dst_info):
        return SKIPPED

    -- Dispatch with stdin --
    return manager._dispatch(command=command, elevated=config.elevated,
                              stdin=<captured getfacl stdout>,
                              path=dst)
```

The `--set-file=-` invocation is the single-runner-call form of the
canonical shell idiom `getfacl src | setfacl --set-file=- dst`.
The runner's `stdin` channel carries the captured payload so the
manager never needs to spawn a shell pipeline.

---

## Naming and Layout Conventions

The Ubuntu sysman package uses snake_case file names with no
separator (`aclmanagement.py`, `filesystemmanagement.py`,
`localizationmanagement.py`).  Public types are PascalCase prefixed
with `Ubuntu`.  Private module helpers are snake_case prefixed
with a single leading underscore.

The `_dispatch` helper inherited from `BaseFeatureManager` accepts
arbitrary `**extra` kwargs that are forwarded to `_wrap_result`,
which in turn passes them to the result class constructor.  This
manager uses the `path` and `principal` extras so the per-result
fields are populated automatically without a manual `_wrap_result`
override.

---

## Backwards Compatibility

This is a new feature; there are no backwards-compatibility
constraints.  The cross-OS contract pins the public verb names
(`get_acl`, `set_acl`, `add_ace`, `remove_ace`, `clear_acl`,
`copy_acl`, `list_aces`); we follow them.  The OS-prefixed type
names are pinned by this spec; future additions go through new
specs (`3-acl-management-recursive`, `3-acl-template-system`,
etc.).

---

## Performance Notes

POSIX ACL operations are cheap relative to other sysman work — a
typical `getfacl` invocation completes in under 5 ms on a
populated filesystem and `setfacl` in under 10 ms.  The per-write
idempotency probe doubles the dispatch count but stays well under
20 ms total per write.  No caching is performed across calls
because:

1. ACL state on a given path can be mutated by other actors between
   calls, and a stale cache would silently serve incorrect "current"
   state to the idempotency probe.
2. The per-call cost is too low to warrant the cache-invalidation
   complexity.
3. The runner-based dispatch model cleanly composes with retry /
   timeout aspects; a manager-level cache would short-circuit those
   aspects in surprising ways.

For callers that do need batch behaviour (e.g. set the same ACL on
a hundred sibling paths), the recommended pattern is to render the
shared ACL once and call `set_acl` per path.  A future
`apply_acl_template` verb may collapse those into a single
`setfacl -R` invocation behind a richer reporting shape.
