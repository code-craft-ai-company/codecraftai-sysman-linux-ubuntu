# Design

## Overview

`UbuntuLocalizationManager` provides idempotent, dry-run-aware control
over a host's locale, keymap, and timezone settings on Ubuntu. It is
a thin facade over `localectl`, `timedatectl`, and `locale -a`,
dispatched through the injected `ICommandRunner`. The module imports
no `subprocess`, opens no files, and consumes no `IFileOperator`.
Persistence is owned by `localectl` (which writes
`/etc/locale.conf`, `/etc/vconsole.conf`, and the X11 keyboard drop-
in) and `timedatectl` (which writes `/etc/localtime` and
`/etc/timezone`).

The module sits in
`automojo.sysman.linux.ubuntu.localizationmanagement` and exports the
following OS-prefixed surface:

| Type | Role |
|------|------|
| `UbuntuLocalizationManager` | Manager class. |
| `UbuntuLocalizationManagerConfig` | Tunable per-tool paths plus `dry_run` / `elevated`. |
| `UbuntuLocalizationActionStatus` | Alias of the package `ActionStatus`. |
| `UbuntuLocalizationActionResult` | Result dataclass with `setting`, `previous_value`, `requires_reboot`. |
| `UbuntuLocaleInfo` | `get_locale` return shape. |
| `UbuntuKeymapInfo` | `get_keymap` return shape. |
| `UbuntuLocalizationQueryError` | Raised on read-side failures. |

`get_timezone` returns a plain `str` — the IANA timezone name — per
the per-OS verb table in the implementation kickoff. The contract's
abstract `TimezoneInfo` (with `utc_offset_seconds` and `dst_active`)
overlaps with `precision-time-management` and is not duplicated here.

## Architecture

```
                Maintenance coordinator
                        │
                        │  builds UbuntuLocalizationManager(runner, config)
                        ▼
           UbuntuLocalizationManager
                        │
                        │  .run(argv, elevated=…)
                        ▼
                ICommandRunner
                        │
              ┌─────────┴────────┐
              ▼                  ▼
       LocalCommandRunner    SSHCommandRunner
              │                  │
              └────────┬─────────┘
                       │  aspects compose here
                       ▼
            (Persistence | Retry | Timeout)
                       │
                       ▼
       localectl / timedatectl / locale
```

`UbuntuLocalizationManager` extends `BaseFeatureManager` from
`automojo.sysman.linux.ubuntu.featurebase`, which supplies the
canonical `ActionStatus`, `FeatureConfig`, `FeatureActionResult`, and
`_dispatch` helper. The localization module exposes its own
`_dispatch_localization_write` helper instead of calling `_dispatch`
directly so that the result dataclass's per-feature fields
(`setting`, `previous_value`, `requires_reboot`) are populated
explicitly and the read-before-write probe stays inside the manager.

## Verb-by-verb design

### `get_locale() -> UbuntuLocaleInfo`

Argv: `["localectl", "status"]` (elevated=False)

Parser: `_parse_localectl_status` recognises every line whose body
follows the `<Field Name>:` pattern and surfaces the parsed values
under canonical lower-case-snake-case keys. The `System Locale:`
body is then fed to `_parse_system_locale_kv`, which splits on
whitespace and partitions each token at the first `=`. Country and
encoding tags are derived from the parsed `LANG` value via
`_derive_country_and_encoding`:

| `LANG` | `country` | `encoding` |
|--------|-----------|-----------|
| `en_US.UTF-8` | `US` | `UTF-8` |
| `en_US` | `US` | `None` |
| `C.UTF-8` | `None` | `UTF-8` |
| `C` | `None` | `None` |
| `""` | `None` | `None` |

Failure surface: any non-zero exit raises
`UbuntuLocalizationQueryError(command, exit_code, stderr)`.

### `set_locale(locale, *, lc_overrides=None) -> UbuntuLocalizationActionResult`

Argv: `["localectl", "set-locale", "LANG=<locale>", ...]`

Idempotency: invokes `get_locale()` first. When the parsed `LANG`
matches AND every supplied `LC_*` override matches the parsed `raw`
mapping, returns `SKIPPED`. The probe never elevates; the write
honours `config.elevated`.

Determinism: when `lc_overrides` is supplied, keys are sorted
before being appended to the argv so the resulting command line is
deterministic across Python dict-ordering variations. (Modern
Python preserves insertion order, but tests assert exact argv
equality so the explicit sort is load-bearing.)

`dry_run` short-circuits before the read probe, returning
`status=DRY_RUN` with the would-have-been argv on `command`.

### `list_supported_locales() -> list[str]`

Argv: `["locale", "-a"]` (elevated=False)

Parser: `_parse_locale_a` strips each line, drops empties.

Tool choice: `localectl list-locales` is also available but emits an
empty list on minimal cloud images where the locale archive has not
been generated. `locale -a` is the canonical, always-populated
enumerator on Ubuntu and matches the abstract verb's intent.

### `get_keymap() -> UbuntuKeymapInfo`

Argv: `["localectl", "status"]` (shared with `get_locale`)

Parser: same `_parse_localectl_status`. The `name` field carries the
parsed `VC Keymap` value (the empty string when absent or when
`localectl` reports `n/a`). `layout`, `variant`, `model`, and
`options` are populated only when the corresponding line was
present.

### `set_keymap(keymap, *, x11_layout=None, x11_variant=None, x11_model=None) -> UbuntuLocalizationActionResult`

Two-phase write when `x11_layout` is supplied:

1. `["localectl", "set-keymap", <keymap>]`
2. `["localectl", "set-x11-keymap", <layout>, [<model>, [<variant>]]]`

The X11 dispatch is conditional on the X11 components differing from
the parsed state. When only the variant is supplied (no model), the
manager fills `-` for the model slot, which `localectl` accepts as
"leave the model alone".

Idempotency: when the parsed `VC Keymap` and every supplied X11
component already match, returns `SKIPPED` without dispatching either
write. When only the X11 portion differs, the VC dispatch is skipped
and the X11 dispatch fires alone (the result reports the X11
command). When only the VC portion differs and X11 components match,
the VC dispatch fires and the X11 dispatch is skipped.

Failure semantics: when the VC write fails, the X11 write is NOT
attempted; the VC result is returned. When the VC write succeeds and
the X11 write fails, the X11 result (carrying its own
`exit_code` / `stderr` / `command`) is returned.

### `get_supported_keymaps() -> list[str]`

Argv: `["localectl", "list-keymaps"]` (elevated=False)

Parser: `_parse_list_lines` strips each line, drops empties.

### `get_timezone() -> str`

Argv: `["timedatectl", "show", "--property=Timezone", "--value"]`

The `--value` flag tells `timedatectl` to emit only the value (no
`Timezone=` prefix). The manager strips trailing whitespace and
returns the IANA string. Empty stdout maps to `""`.

### `set_timezone(tz) -> UbuntuLocalizationActionResult`

Argv: `["timedatectl", "set-timezone", <tz>]`

Idempotency: probes via `get_timezone()` first. Comparison is exact-
equality on the IANA string. `dry_run` short-circuits before the
probe.

### `get_supported_timezones() -> list[str]`

Argv: `["timedatectl", "list-timezones"]` (elevated=False)

## Cross-OS locale-format mapping

The cross-OS contract leaves `set_locale` taking a plain `str`. The
following table captures how that string is interpreted on each OS;
on Ubuntu we ingest the POSIX form unchanged.

| Concept | Ubuntu | macOS | Windows |
|---------|--------|-------|---------|
| Display form | `en_US.UTF-8` (POSIX) | `en_US` (BCP 47-ish) | `en-US` (BCP 47) |
| Tool surface | `localectl set-locale LANG=<…>` | `defaults write .GlobalPreferences AppleLocale '<…>'` | `Set-Culture -CultureInfo '<…>'` |
| Encoding suffix | Required for non-ASCII (`.UTF-8`) | Implicit (UTF-8 system-wide) | N/A (UTF-16 internal) |
| Country code | Underscore-separated (`en_US`) | Same as Ubuntu when used | Hyphen-separated (`en-US`) |
| Validation source | `locale -a` | `locale -a` (BSD) | `[CultureInfo]::GetCultures(AllCultures)` |

The Ubuntu manager does not auto-convert between forms; callers
working across OSes are expected to render the per-OS form
explicitly.

## Idempotency table

| Verb | Probe argv | Skip condition |
|------|-----------|---------------|
| `set_locale` | `localectl status` | parsed `LANG` == requested AND every `LC_*` override matches |
| `set_keymap` | `localectl status` | parsed `VC Keymap` == requested AND every supplied X11 component matches |
| `set_timezone` | `timedatectl show --property=Timezone --value` | parsed timezone == requested |

Reads (`get_*`, `list_*`) never have a skip condition; they
unconditionally dispatch their probe argv.

## Elevation policy

| Operation | Elevation |
|-----------|-----------|
| `localectl status` (read) | Never |
| `localectl list-keymaps` (read) | Never |
| `locale -a` (read) | Never |
| `timedatectl show --property=Timezone --value` (read) | Never |
| `timedatectl list-timezones` (read) | Never |
| `localectl set-locale` (write) | Honours `config.elevated` |
| `localectl set-keymap` (write) | Honours `config.elevated` |
| `localectl set-x11-keymap` (write) | Honours `config.elevated` |
| `timedatectl set-timezone` (write) | Honours `config.elevated` |

The elevation gate is applied at the runner boundary by the
`_dispatch_localization_write` helper. Read-side helpers
(`get_locale`, `get_keymap`, `get_timezone`, `list_supported_locales`,
`get_supported_keymaps`, `get_supported_timezones`) hard-code
`elevated=False` so that even a coordinator misconfigured with
`elevated=True` cannot accidentally elevate a read.

## Result-dataclass contract

`UbuntuLocalizationActionResult` extends the package
`FeatureActionResult` with three fields:

| Field | Type | Populated when |
|-------|------|----------------|
| `setting` | `str | None` | Always populated for verbs that target a single field (`"locale"` / `"keymap"` / `"timezone"`). `None` only on hypothetical compound results. |
| `previous_value` | `str | None` | Populated on `SUCCESS`, `SKIPPED`, and `FAILED` when the read probe ran cleanly; `None` when the probe failed (dry-run paths skip the probe entirely). |
| `requires_reboot` | `bool` | Always `False` on Ubuntu — `localectl` and `timedatectl` mutations are picked up by new sessions on the next login and require no reboot. The field is reserved on the result shape for cross-OS parity with the Windows `Set-WinSystemLocale` path. |

## Module-level helpers

* `_parse_localectl_status(text)` — line-by-line parser that
  recognises `System Locale:`, `VC Keymap:`, `X11 Layout:`,
  `X11 Variant:`, `X11 Model:`, `X11 Options:` prefixes and returns
  a dict of canonical keys.
* `_parse_system_locale_kv(text)` — whitespace-split tokeniser for
  the `System Locale:` body.
* `_derive_country_and_encoding(language)` — pure derivation of
  `(country, encoding)` from a `LANG` value.
* `_parse_locale_a(text)` — one-entry-per-line parser for
  `locale -a`.
* `_parse_list_lines(text)` — generic one-entry-per-line parser
  shared by `list-keymaps` and `list-timezones`.

Each helper is a pure function so it can be unit-tested in isolation
without needing a runner stub.

## Public-facing imports

```python
from automojo.sysman.linux.ubuntu.localizationmanagement import (
    UbuntuLocalizationManager,
    UbuntuLocalizationManagerConfig,
    UbuntuLocalizationActionStatus,
    UbuntuLocalizationActionResult,
    UbuntuLocaleInfo,
    UbuntuKeymapInfo,
    UbuntuLocalizationQueryError,
)
```

`UbuntuLocalizationActionStatus` is `ActionStatus` from
`featurebase` re-exported under the OS-prefixed name; both names
work and resolve to the same enum.

## Tool-binding constants

```python
_DEFAULT_LOCALECTL    = "localectl"
_DEFAULT_TIMEDATECTL  = "timedatectl"
_DEFAULT_LOCALE_TOOL  = "locale"
```

Each is reachable via the manager's
`UbuntuLocalizationManagerConfig`:

| Field | Default | Purpose |
|-------|---------|---------|
| `localectl_path` | `"localectl"` | Override for the locale / keymap binary. |
| `timedatectl_path` | `"timedatectl"` | Override for the timezone binary. |
| `locale_tool_path` | `"locale"` | Override for the BSD-style enumerator. |

The defaults assume each binary is on `$PATH`. Tests override these
to inject sentinel paths and assert that argv shape forwards them
through.

## Failure surface

| Failure | Surface |
|---------|---------|
| `localectl status` exits non-zero | `UbuntuLocalizationQueryError` (read) |
| `localectl list-keymaps` exits non-zero | `UbuntuLocalizationQueryError` (read) |
| `locale -a` exits non-zero | `UbuntuLocalizationQueryError` (read) |
| `timedatectl show ...` exits non-zero | `UbuntuLocalizationQueryError` (read) |
| `timedatectl list-timezones` exits non-zero | `UbuntuLocalizationQueryError` (read) |
| `localectl set-locale` exits non-zero | `UbuntuLocalizationActionResult(status=FAILED)` |
| `localectl set-keymap` exits non-zero | `UbuntuLocalizationActionResult(status=FAILED)` (X11 write skipped) |
| `localectl set-x11-keymap` exits non-zero | `UbuntuLocalizationActionResult(status=FAILED)` (after VC SUCCESS) |
| `timedatectl set-timezone` exits non-zero | `UbuntuLocalizationActionResult(status=FAILED)` |
| Read probe fails on a write path | `previous_value=None`; the write still dispatches |

Reads raise (typed). Writes never raise; failures are surfaced via
the `FAILED` status carrying the captured `exit_code`, `stdout`,
`stderr`, and `command`. This matches the cross-OS contract's
distinction between read-side typed errors and write-side wrapped
results.

## Aspect-composition guarantees

The manager makes no assumptions about how the runner dispatches a
command, so any combination of `RetryAspect`, `TimeoutAspect`, and
`PersistenceAspect` is transparent:

* Retry can flip a `FAILED` to `SUCCESS` between attempts; the
  manager's idempotency probe runs once on the manager side, and
  the runner is free to re-execute the write argv as many times as
  needed without changing the manager's externally observable
  behaviour.
* Persistence aspects MUST NOT mutate the result; the manager
  returns the runner's `RunResult` fields verbatim on the
  `UbuntuLocalizationActionResult`.
* Timeout aspects raise their own errors, which propagate through
  the runner unchanged.

## Testing strategy

Tests use a `_MockCommandRunner` that records every `run` call and
returns pre-seeded `RunResult` values keyed by argv tuple. No live
`localectl` / `timedatectl` / `locale` is invoked. Coverage:

* Read parsers: `localectl status` (full and minimal), `locale -a`,
  `localectl list-keymaps`, `timedatectl list-timezones`,
  `timedatectl show --property=Timezone --value`.
* Write argv shape: locale, keymap, timezone — including
  `lc_overrides` formatting and X11 second-phase dispatch.
* Idempotency: each write skips when its probe matches.
* Dry-run: each write returns `DRY_RUN` without invoking the runner.
* Elevation: writes elevate when configured; reads never elevate.
* Failure surface: non-zero writes produce `FAILED` with stderr;
  non-zero reads raise `UbuntuLocalizationQueryError`.

The minimum 14-test bar is met by the current
`test_localizationmanagement.py` module.

## Out-of-scope notes

* **Per-user locale overrides.** The manager mutates the system
  default only; per-user `~/.config/locale.conf` is out of scope
  for this pilot.
* **Locale generation.** The manager assumes the requested locale
  is already generated. Wrapping `locale-gen` is tracked as a
  follow-up and is documented in `tasks.md`.
* **Input-method frameworks.** IBus / SCIM / IME configuration is
  explicitly out of scope per the cross-OS contract.
* **Timezone DST / offset surface.** `precision-time-management`
  owns `dst_active` and `utc_offset_seconds` on Ubuntu; this
  module exposes only the IANA name.
* **Real-time-clock (RTC) UTC vs local-time.** `timedatectl
  set-local-rtc` is owned by `precision-time-management`.

## Future-extension surface

* Adding a `regenerate_locale(locale)` verb that wraps `locale-gen
  <locale>` + `update-locale` for hosts that need to install a
  locale before applying it.
* Surfacing `localectl --no-convert` semantics on `set_keymap`
  (the manager currently always lets `localectl` pick whether to
  also convert the X11 layout from a VC change).
* Returning a richer `UbuntuTimezoneInfo` if and when the
  `precision-time-management` overlap is collapsed in a future
  contract revision.
