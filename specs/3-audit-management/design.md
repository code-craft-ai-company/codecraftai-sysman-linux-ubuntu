# Design

## Overview

The audit-management feature is implemented as a single module,
`automojo.sysman.linux.ubuntu.auditmanagement`, that exposes an
`UbuntuAuditManager` class.  This spec implements the
audit-management contract defined at
`automojo-sysman-common/specs/contracts/audit-management/contract.md`
for Ubuntu using `auditctl` for runtime rule manipulation,
`augenrules --load` for persistent rule reload, `ausearch` for
event-stream filtering, and `systemctl` for daemon lifecycle.

All system commands go through the `ICommandRunner` interface; rule
file I/O is performed via `cat` and `tee` against that same runner
rather than a separate `IFileOperator`.  No `subprocess`, no direct
`open`, and no direct `pathlib` calls are made inside the module.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH (via `SSHCommandRunner`).
When using SSH, the coordinator can track reliability metrics and
apply execution aspects (persistence, retry, timeout) without any
changes to the `UbuntuAuditManager` code.

**See Also:** [Command Runner Architecture](../1-command-execution/design.md)
for details on local/SSH execution, reliability tracking, and aspect
configuration.

---

## Cross-OS Audit-Model Concept Mapping

The three Wave-2 OSes diverge sharply in how their audit subsystems
are addressed.  The table below makes the mapping explicit so that
a single contract can be honoured by very different toolchains.

| Concept | Linux (Ubuntu / RHEL) | macOS (BSM) | Windows |
|---|---|---|---|
| Tool name | `auditctl` / `augenrules` / `ausearch` | `audit` / `praudit` / `auditreduce` | `auditpol` / `wevtutil` / `Get-WinEvent` |
| Rule shape | Line-based; one argv body per rule (e.g. `-w /etc/passwd -p wa -k passwd_changes`) | Class-based; a comma-separated list of audit-class flags in `/etc/security/audit_control` (e.g. `flags:lo,fa,pc`) | Subcategory-based; a (subcategory, success, failure) tuple per policy (e.g. `auditpol /set /subcategory:"Logon" /success:enable /failure:enable`) |
| Persistence | Drop-in file under `/etc/audit/rules.d/`, reloaded with `augenrules --load` | Edit `/etc/security/audit_control`, then `audit -s` to reload | `auditpol /set` is itself the persistent write; `auditpol /backup` exports for archival |
| Identity | `-k <key>` token | The class flag itself (e.g. `lo` is one rule) | The subcategory name |
| Status | `auditctl -s` (multi-line key/value) | `audit -s` (textual blob) | `auditpol /get /category:* /r` (CSV) |
| Search | `ausearch -k <key> -m <type> -ts <ts>` | `praudit -l /var/audit/<latest>` text grep | `Get-WinEvent -LogName Security -FilterHashtable …` |

Ubuntu sits in the **line-based / persistent-file** column.  The
manager renders a structured `UbuntuAuditRuleSpec` into the same
argv body that `auditctl` itself accepts; the `key` field acts as
the abstract `rule_id` and the deterministic match token for
idempotency and removal.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner                                      │
│  - Configures aspects (optional)                            │
│  - Injects runner + UbuntuAuditManagerConfig                │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            UbuntuAuditManager                               │
│  automojo.sysman.linux.ubuntu.auditmanagement               │
│                                                             │
│  add_rule()             remove_rule()                       │
│  list_rules()           enable_auditing()                   │
│  disable_auditing()     get_audit_status()                  │
│  search_events()                                            │
└───────────┬─────────────────────────────────────────────────┘
            │ delegates to
            ▼
┌──────────────────────────┐
│   ICommandRunner         │
│   run(args, elevated)    │
│   → RunResult            │
└──────────┬───────────────┘
           │
           ▼
  ┌──────────────────────────────────┐
  │   LocalCommandRunner   or        │
  │   SSHCommandRunner               │
  └─────────────┬────────────────────┘
                │ optional
                ▼
  ┌──────────────────────────────────┐
  │   PersistenceAspect /            │
  │   RetryAspect / TimeoutAspect    │
  └──────────────────────────────────┘
```

---

## Components

### `UbuntuAuditManager`

**Location:**
`source/packages/automojo/sysman/linux/ubuntu/auditmanagement.py`

| Method | Signature | Description |
|---|---|---|
| `add_rule` | `(spec: UbuntuAuditRuleSpec) → UbuntuAuditActionResult` | Render and apply a rule; persist to drop-in when `spec.persistent=True`. |
| `remove_rule` | `(rule_id: str) → UbuntuAuditActionResult` | Delete by `-k` key from runtime and drop-in. |
| `list_rules` | `() → list[UbuntuAuditRuleInfo]` | Enumerate runtime + drop-in rules. |
| `enable_auditing` | `() → UbuntuAuditActionResult` | `auditctl -e 1` + `systemctl enable --now auditd`. |
| `disable_auditing` | `() → UbuntuAuditActionResult` | `auditctl -e 0` + `systemctl disable --now auditd`. |
| `get_audit_status` | `() → UbuntuAuditStatus` | Parse `auditctl -s`. |
| `search_events` | `(query: UbuntuAuditEventQuery) → list[UbuntuAuditEvent]` | Filter via `ausearch`. |

### `UbuntuAuditManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, writes short-circuit to `DRY_RUN`. |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` to the runner. |
| `rules_d` | `str` | `/etc/audit/rules.d` | Directory holding audit rule drop-ins. |
| `default_basename` | `str` | `99-automojo.rules` | File basename for the manager-owned drop-in. |
| `service_name` | `str` | `auditd` | systemd unit name for the audit daemon. |

---

## Data Models

### Enum

```python
class UbuntuAuditActionStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"
```

This is `automojo.sysman.linux.ubuntu.featurebase.ActionStatus`
re-exported under the OS-prefixed name to keep the contract's type
table honest.

### `UbuntuAuditRuleSpec`

| Field | Type | Description |
|---|---|---|
| `key` | `str` | `-k` token; acts as the abstract `rule_id`. |
| `watch_path` | `str \| None` | Emit `-w <watch_path>` when set. |
| `permissions` | `str \| None` | Emit `-p <permissions>` when set. |
| `syscall_filter` | `str \| None` | Emit `-a <syscall_filter>` (multi-token) when set. |
| `architecture` | `str \| None` | Emit `-F arch=<architecture>` when set. |
| `raw_rule` | `str \| None` | Verbatim argv body; mutually exclusive with the structured fields. |
| `persistent` | `bool` | When `True`, append to drop-in and run `augenrules --load`. Defaults to `True`. |

### `UbuntuAuditRuleInfo`

| Field | Type | Description |
|---|---|---|
| `rule_id` | `str` | The `-k` key when present, else the full expression. |
| `key` | `str \| None` | The `-k` value when parsed. |
| `expression` | `str` | Full argv body joined with single spaces. |
| `watch_path` | `str \| None` | Path watched by the rule, when `-w` is present. |
| `permissions` | `str \| None` | Permissions mask when `-p` is present. |
| `subsystem` | `str` | `"path"`, `"syscall"`, or `"raw"`. |
| `source` | `str \| None` | Drop-in file path when the rule was observed only in the persistent store; `None` for runtime rules. |

### `UbuntuAuditStatus`

| Field | Type | Description |
|---|---|---|
| `enabled` | `bool` | True when kernel audit is enabled. |
| `running` | `bool` | True when daemon `pid` is non-zero. |
| `pid` | `int \| None` | Daemon PID. |
| `failure_mode` | `str \| None` | Failure-mode token. |
| `rate_limit` | `int \| None` | Events per second cap. |
| `backlog_limit` | `int \| None` | Backlog ceiling. |
| `backlog` | `int \| None` | Current backlog. |
| `lost_events` | `int \| None` | Cumulative lost-event count. |
| `locked` | `bool` | True when `enabled == 2` (immutable mode). |
| `raw` | `str` | Raw stdout for debuggability. |

### `UbuntuAuditEventQuery`

| Field | Type | Description |
|---|---|---|
| `key` | `str \| None` | Emit `-k <key>`. |
| `message_type` | `str \| None` | Emit `-m <type>`. |
| `since` | `str \| None` | Emit `-ts <since>`. |
| `until` | `str \| None` | Emit `-te <until>`. |
| `user` | `str \| None` | Emit `-ui <user>`. |
| `raw_filter` | `str \| None` | Verbatim filter; mutually exclusive with structured fields. |
| `limit` | `int \| None` | Cap on parsed records. |

### `UbuntuAuditEvent`

| Field | Type | Description |
|---|---|---|
| `timestamp` | `str` | `"<seconds>:<serial>"` from the audit timestamp. |
| `event_id` | `int \| None` | Serial portion of the audit timestamp. |
| `category` | `str \| None` | First `type=<TYPE>` token of the record. |
| `severity` | `str \| None` | Reserved; always `None` in the pilot. |
| `message` | `str` | First record line as a short summary. |
| `raw` | `str` | Full record text. |

### `UbuntuAuditActionResult`

Inherits from `FeatureActionResult` (status, detail, exit_code,
stdout, stderr, command) and adds:

| Field | Type | Description |
|---|---|---|
| `rule_id` | `str \| None` | Rule identifier targeted by the action. |
| `rule_key` | `str \| None` | Rule's `-k` key (mirrors `rule_id` when the spec used the structured form). |
| `message` | `str \| None` | Human-readable detail for `SKIPPED` / `FAILED`. |

### Exceptions

| Exception | Raised When |
|---|---|
| `UbuntuAuditQueryError` | A read fails unexpectedly (non-zero exit with non-empty stderr). |
| `UbuntuAuditImmutableError` | Reserved for callers who pre-flight immutable mode and want a typed exception; the manager itself returns `FAILED` rather than raising, per the contract's invariant 5. |

---

## Verb Behaviors

### `add_rule(spec)`

1. Render `spec` into an `auditctl` argv body via
   `_render_auditctl_args`.
2. Build the runtime command `["auditctl"] + argv`.
3. **Dry-run guard:** when `config.dry_run` is True, return
   `UbuntuAuditActionResult(status=DRY_RUN, command=…, rule_id=key,
   rule_key=key)`.
4. **Idempotency guard:** call `_safe_runtime_rules()` (which
   swallows `UbuntuAuditQueryError`).  When any rule's `key` matches
   `spec.key` or any rule's `expression` matches the rendered line,
   return `status=SKIPPED` without dispatching.
5. Dispatch the runtime invocation via the runner with
   `elevated=config.elevated`.
6. Map exit code 0 → `SUCCESS`, non-zero → `FAILED`.
7. **Persistence:** when `spec.persistent` is True, call
   `_append_to_dropin(rule_line)` (which `cat`s the existing file,
   skips appending when the rule line is already present, and `tee`s
   the new body), then dispatch `augenrules --load`.

### `remove_rule(rule_id)`

1. Build `["auditctl", "-d", "-k", rule_id]`.
2. **Dry-run guard:** when `config.dry_run` is True, return
   `DRY_RUN`.
3. **Idempotency guard:** when no runtime rule's key matches and the
   drop-in does not contain the substring `rule_id`, return
   `SKIPPED`.
4. Dispatch the delete invocation with `elevated=config.elevated`.
5. Map exit code 0 → `SUCCESS`, non-zero → `FAILED`.
6. Rewrite the drop-in via `_remove_from_dropin(rule_id)` (which
   filters out lines whose tokens carry `-k <rule_id>`), then run
   `augenrules --load`.

### `list_rules()`

1. Dispatch `auditctl -l` without elevation.
2. Raise `UbuntuAuditQueryError` when exit is non-zero **and**
   stderr is non-empty (a non-zero exit with empty stderr is
   `auditctl`'s "no rules" signal on some releases).
3. Parse stdout via `_parse_auditctl_list`.
4. Read the drop-in via `cat` and parse with `_parse_rules_file`.
5. Stitch persistent-only rules onto the runtime list and return.

### `enable_auditing()` / `disable_auditing()`

Both verbs share the same shape:

1. Build `["auditctl", "-e", "1"]` (enable) or `[..., "0"]`
   (disable).
2. Dry-run guard returns `DRY_RUN`.
3. Read status via `_safe_status()`.  When the desired state is
   already in effect, return `SKIPPED`.  When the subsystem is
   `locked` (`enabled == 2`), return `FAILED` with a descriptive
   message **without** dispatching.
4. Dispatch the kernel toggle.  Map zero → `SUCCESS`, non-zero →
   `FAILED`.
5. Dispatch `systemctl enable --now <service_name>` (enable) or
   `systemctl disable --now <service_name>` (disable).  When this
   step fails, downgrade the result to `FAILED`.

### `get_audit_status()`

1. Dispatch `auditctl -s` without elevation.
2. Raise `UbuntuAuditQueryError` on non-zero exit with non-empty
   stderr.
3. Parse via `_parse_auditctl_status`, which tolerates missing
   fields by setting them to `None`.

### `search_events(query)`

1. Build the argv via `_build_ausearch_argv`.  When
   `query.raw_filter` is set, its whitespace-split tokens follow
   the `ausearch` token directly.
2. Dispatch without elevation.
3. When the runner exits non-zero **and** stderr is non-empty,
   raise `UbuntuAuditQueryError`.  Non-zero exit with empty stderr
   (ausearch's no-match signal) is treated as success.
4. Parse the dashed record stream into `UbuntuAuditEvent` rows via
   `_parse_ausearch_output`, capping at `query.limit` when set.

---

## Idempotency Model

| Verb | Probe | Skip condition |
|---|---|---|
| `add_rule` | `auditctl -l` | A rule with the same `-k` key exists, or a rule whose full expression text matches the rendered line. |
| `remove_rule` | `auditctl -l` + drop-in `cat` | No rule with the matching key exists in either the runtime ruleset or the drop-in. |
| `enable_auditing` | `auditctl -s` | `enabled` is True and `pid` is non-zero. |
| `disable_auditing` | `auditctl -s` | `enabled` is False and `pid` is zero. |

All idempotency probes run under `elevated=False`, matching the
contract invariant that reads do not elevate.  The probes are
wrapped in `_safe_*` helpers that swallow `UbuntuAuditQueryError`
so that probe failure falls through to attempting the operation
(rather than masking it as a no-op).

---

## Persistence Model

Persistence is opt-in via `UbuntuAuditRuleSpec.persistent` (default
True).  The manager owns a single drop-in file under
`<rules_d>/<default_basename>`.  Each persistent add appends one
line (with a trailing newline) to that file; each persistent
remove rewrites the file with matching lines stripped.

Why a single drop-in instead of one file per rule?

- Easier observability: the user sees every rule the manager is
  responsible for in one place.
- Simpler clean-up: `rm <drop-in>` removes every manager-owned
  rule.
- Consistent with the RHEL canonical implementation's
  `write_rules_file(name, rules)` shape.

`augenrules --load` is invoked after every persistent write so
that the on-disk store and the running ruleset converge.  When the
audit subsystem is in immutable mode, `augenrules` will fail; the
manager surfaces the failure but does not crash the call chain.

---

## Elevation Policy

| Operation | `elevated` |
|---|---|
| `auditctl <add-rule-argv>` | `config.elevated` |
| `auditctl -d -k <key>` | `config.elevated` |
| `auditctl -e {0,1}` | `config.elevated` |
| `tee <drop-in>` | `config.elevated` |
| `augenrules --load` | `config.elevated` |
| `systemctl enable/disable --now auditd` | `config.elevated` |
| `auditctl -l` | `False` |
| `auditctl -s` | `False` |
| `ausearch …` | `False` |
| `cat <drop-in>` | `False` |

Reads never elevate.  Writes elevate when the config opts in.
This mirrors the contract's two-pillar elevation policy.

---

## Parsers

### `_parse_auditctl_list`

`auditctl -l` emits one rule per line in the same argv body it
itself accepts.  The parser:

- Skips blank lines and lines starting with `#`.
- Skips the literal `"No rules"` line.
- Splits each line into argv tokens.
- Extracts `-k <key>` (`_extract_key`), `-w <path>`
  (`_extract_named` with flag `"-w"`), and `-p <perms>`
  (`_extract_named` with flag `"-p"`).
- Classifies the subsystem via `_classify_subsystem`: `"path"`
  for any `-w` token, `"syscall"` for any `-a` token, `"raw"`
  otherwise.
- Emits one `UbuntuAuditRuleInfo` per parsed line.  When `-k` is
  present, `rule_id` mirrors the key; otherwise `rule_id` mirrors
  the full line text.

### `_parse_auditctl_status`

`auditctl -s` emits `<key> <value>` pairs separated by whitespace,
one per line.  The parser walks every line, tokenises into a
two-element split, and routes recognised keys (`enabled`, `pid`,
`failure`, `rate_limit`, `backlog_limit`, `lost`, `backlog`) into
the corresponding `UbuntuAuditStatus` field.  `enabled` is
interpreted as a tri-state (`0` → disabled, `1` → enabled, `2` →
locked).  Unrecognised keys are tolerated and ignored.

### `_parse_ausearch_output`

`ausearch` separates records with a `----` line.  The parser
groups consecutive non-delimiter lines into one record body,
flushes the group on each delimiter, and emits one
`UbuntuAuditEvent` per group via `_parse_ausearch_record`.  When
the caller supplies `query.limit`, parsing stops after that many
events have been collected.

### `_parse_ausearch_record`

Walks the record body looking for the first `type=<TYPE>` line,
then extracts the `type=` value (`category`) and the `msg=audit(<ts>:<serial>):`
token (parsed into `timestamp` and `event_id`).  The first record
line is preserved as a short `message` summary; the full body is
preserved as `raw` for debuggability.

---

## Renderers

### `_render_auditctl_args`

Emits the argv body in the canonical order
`-w / -p / -a <multi-token> / -F arch=<arch> / -k`.  When
`spec.raw_rule` is supplied it is split on whitespace and a
trailing `-k <key>` is appended only when the raw form is missing
one.  This guarantees every rule the manager produces is
addressable by its key for later removal / idempotency.

---

## Failure Mapping

| Runner outcome | Mapped status |
|---|---|
| Zero exit code | `SUCCESS` |
| Non-zero exit code | `FAILED` |
| Pre-flight skip | `SKIPPED` |
| Dry-run | `DRY_RUN` |

`UbuntuAuditQueryError` carries `command`, `exit_code`, and
`stderr` for debuggability and is the read-path counterpart to
`FAILED` results on the write path.

---

## Testing Strategy

Tests use a `_MockCommandRunner` that records every `run` call and
returns pre-seeded `RunResult` values keyed by argv tuple.  The
mock is intentionally minimal — it mimics the `ICommandRunner.run`
signature, captures `args` / `elevated` / `stdin`, and returns a
default `RunResult` when no key is registered for a given argv.

Coverage targets:

1. Each write verb dispatches the canonical argv.
2. Each write verb honours the idempotency guard (`SKIPPED`).
3. Each write verb honours dry-run (`DRY_RUN`).
4. Each read verb parses representative output.
5. Persistent writes append to the drop-in and reload via
   `augenrules`.
6. Writes elevate when configured; reads never elevate.
7. Failed runner exits surface as `FAILED` results with stderr
   intact.
8. Pure-function tests cover the renderer and the parsers in
   isolation so regressions in argv shape or parser tolerance
   surface as targeted failures.

---

## Spec Deviations

- **`IFileOperator` collaborator.** The cross-OS contract names an
  `IFileOperator` as the route for rule-file I/O.  Following the
  package-wide pattern set by `repositorymanagement.py` and
  `logmanagement.py`, this implementation routes every file read /
  write through the `ICommandRunner` (`cat` / `tee`).  The contract
  should be revised to reflect that the file-operator collaborator
  is optional.

- **`reload_rules` verb.** The cross-OS contract enumerates a
  `reload_rules` verb.  This implementation folds the reload step
  into `add_rule` / `remove_rule` (each persistent write is
  followed by `augenrules --load`).  Surfacing `reload_rules` as a
  standalone public verb is deferred — see `tasks.md` follow-ups.

- **`AuditImmutableError` exception.** The contract names an
  `AuditImmutableError` exception; this manager returns
  `status=FAILED` with `message="audit subsystem is in immutable
  mode"` instead, matching the contract's preference for typed
  results over typed exceptions on the write path.  The exception
  type is exposed for callers who want to raise it themselves.
