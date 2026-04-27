# Requirements

## Introduction

The localization management feature provides the
`automojo-sysman-linux-ubuntu` package with the ability to query and
set host-wide locale, keymap, and timezone settings on Ubuntu hosts.
This spec implements the localization-management contract defined at
`automojo-sysman-common/specs/contracts/localization-management/contract.md`
for Ubuntu using `localectl` (locale and keymap), `timedatectl`
(timezone), and `locale -a` (installed-locale enumeration).

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public verbs. The feature
does not decide which locale, keymap, or timezone the host should
carry — it provides the capability to apply, observe, and idempotently
re-apply those values reliably and with full auditability. Every
system command is executed exclusively through the
`automojo-interfaces` command-runner abstraction; this module does
not import `subprocess`, does not call `open`, and does not consume an
`IFileOperator`. Persistence is owned by `localectl` (writing
`/etc/locale.conf`, `/etc/vconsole.conf`,
`/etc/X11/xorg.conf.d/00-keyboard.conf`) and `timedatectl` (writing
`/etc/localtime` and `/etc/timezone`).

The OS-prefixed types in this module are
`UbuntuLocalizationManager`, `UbuntuLocalizationManagerConfig`,
`UbuntuLocalizationActionStatus` (alias of the package-shared
`ActionStatus`), `UbuntuLocalizationActionResult`,
`UbuntuLocaleInfo`, `UbuntuKeymapInfo`, and
`UbuntuLocalizationQueryError`. There is no `UbuntuTimezoneInfo`
dataclass: `get_timezone` returns a plain `str` (the IANA timezone
name) per the per-OS verb table. The contract's abstract
`TimezoneInfo` (with `utc_offset_seconds` and `dst_active`) is owned
by the `precision-time-management` feature on Ubuntu, which exposes
the same data as part of its drift / status surface.

---

## Requirements

### Requirement 1: Query Current Locale

**User Story:** As a maintenance coordinator, I want to query the
active system locale, so that I can compare it against the declared
desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_locale()` THE SYSTEM SHALL invoke
   `localectl status` through the command runner with `elevated=False`
   and parse the `System Locale:` line.
2. THE SYSTEM SHALL return an `UbuntuLocaleInfo` carrying the parsed
   `LANG` value as `language`, the derived country / encoding tags
   when present (e.g. `"US"` and `"UTF-8"` for `"en_US.UTF-8"`),
   the verbatim `System Locale:` body as `system_default`, the
   `LC_MESSAGES` / `LC_TIME` / `LC_NUMERIC` overrides when present,
   and the full `KEY=value` mapping in `raw`.
3. WHEN `localectl` exits non-zero THE SYSTEM SHALL raise
   `UbuntuLocalizationQueryError` carrying the failed argv, exit
   code, and captured stderr.
4. THE SYSTEM SHALL NOT pass `elevated=True` to the runner.

---

### Requirement 2: Set System Locale

**User Story:** As a maintenance coordinator, I want to set the
`LANG` value (and optional `LC_*` overrides), so that the host
applies the declared user-facing language and encoding.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_locale(locale)` THE SYSTEM SHALL
   invoke `localectl set-locale LANG=<locale>` through the command
   runner.
2. WHEN the caller supplies `lc_overrides` THE SYSTEM SHALL append
   each `<KEY>=<VALUE>` token (sorted by key for deterministic argv)
   to the same `localectl set-locale` invocation.
3. WHEN the parsed `LANG` already equals the requested value AND
   every supplied `LC_*` override already matches THE SYSTEM SHALL
   return `UbuntuLocalizationActionResult(status=SKIPPED)` without
   invoking `localectl set-locale`.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner at all and SHALL populate the would-
   have-been argv on `command`.
5. WHEN the runner reports a non-zero exit on the write THE SYSTEM
   SHALL return `status=FAILED` with `exit_code`, `stdout`, `stderr`,
   and `command` populated.
6. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner for the write step.
7. THE SYSTEM SHALL populate `result.setting="locale"` on every
   returned `UbuntuLocalizationActionResult` regardless of status.

---

### Requirement 3: Enumerate Supported Locales

**User Story:** As a maintenance coordinator, I want to enumerate
locales installed on the host, so that I can validate locale values
against the host's installed glibc data.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_supported_locales()` THE SYSTEM
   SHALL invoke `locale -a` through the command runner with
   `elevated=False` and parse the one-entry-per-line output.
2. THE SYSTEM SHALL return a `list[str]` of locale names in the
   order reported by `locale -a`, with empty lines elided.
3. WHEN `locale -a` exits non-zero THE SYSTEM SHALL raise
   `UbuntuLocalizationQueryError`.

> **Tool choice rationale:** Ubuntu also ships
> `localectl list-locales`, but minimal cloud images (and any host
> where the locale archive has not yet been generated) emit an empty
> list from `localectl`. `locale -a` is the canonical, always-
> populated enumerator on Ubuntu.

---

### Requirement 4: Query Current Keymap

**User Story:** As a maintenance coordinator, I want to query the
active console (VC) keymap and X11 layout, so that I can compare
them against the declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_keymap()` THE SYSTEM SHALL invoke
   `localectl status` through the command runner with `elevated=False`
   and parse the `VC Keymap:`, `X11 Layout:`, `X11 Variant:`,
   `X11 Model:`, and `X11 Options:` lines.
2. THE SYSTEM SHALL return an `UbuntuKeymapInfo` whose `name` is the
   parsed `VC Keymap` value (the empty string when `localectl`
   reports `n/a`) and whose `layout` / `variant` / `model` /
   `options` fields are populated only when the corresponding line
   was present.
3. WHEN `localectl` exits non-zero THE SYSTEM SHALL raise
   `UbuntuLocalizationQueryError`.

---

### Requirement 5: Set Console (and Optional X11) Keymap

**User Story:** As a maintenance coordinator, I want to set the
virtual console keymap and (optionally) the X11 layout, so that
console and graphical sessions present the requested keyboard layout.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_keymap(keymap)` and the parsed
   `VC Keymap` differs THE SYSTEM SHALL invoke
   `localectl set-keymap <keymap>` through the runner.
2. WHEN the coordinator supplies an `x11_layout` argument THE SYSTEM
   SHALL additionally invoke `localectl set-x11-keymap <layout>
   [<model> [<variant>]]` after the VC update succeeds.
3. WHEN the parsed `VC Keymap` already matches `keymap` AND every
   supplied X11 component already matches THE SYSTEM SHALL return
   `status=SKIPPED` without invoking either write.
4. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
5. WHEN the VC write fails THE SYSTEM SHALL return that failure and
   SHALL NOT dispatch the X11 write.
6. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner for every write step.
7. THE SYSTEM SHALL populate `result.setting="keymap"` on every
   returned result.

---

### Requirement 6: Enumerate Supported Keymaps

**User Story:** As a maintenance coordinator, I want to enumerate
the VC keymaps the kernel knows about, so that I can validate
keymap values before attempting to set them.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_supported_keymaps()` THE SYSTEM
   SHALL invoke `localectl list-keymaps` with `elevated=False` and
   parse the one-entry-per-line output.
2. THE SYSTEM SHALL return a `list[str]` of keymap names in source
   order with empty lines elided.
3. WHEN the command exits non-zero THE SYSTEM SHALL raise
   `UbuntuLocalizationQueryError`.

---

### Requirement 7: Query Current Timezone

**User Story:** As a maintenance coordinator, I want to query the
active system timezone, so that I can compare it against the
declared desired state.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_timezone()` THE SYSTEM SHALL
   invoke `timedatectl show --property=Timezone --value` with
   `elevated=False`.
2. THE SYSTEM SHALL return the trimmed stdout as a plain IANA
   timezone string (e.g. `"America/Los_Angeles"`).
3. WHEN `timedatectl` exits non-zero THE SYSTEM SHALL raise
   `UbuntuLocalizationQueryError`.

---

### Requirement 8: Set Timezone

**User Story:** As a maintenance coordinator, I want to set the
system timezone, so that local-clock displays and scheduled jobs
honour the requested wall clock.

#### Acceptance Criteria

1. WHEN the coordinator calls `set_timezone(tz)` THE SYSTEM SHALL
   invoke `timedatectl set-timezone <tz>` through the runner.
2. WHEN the parsed timezone already matches the requested value
   THE SYSTEM SHALL return `status=SKIPPED` without invoking the
   runner.
3. WHEN `dry_run=True` THE SYSTEM SHALL return `status=DRY_RUN`
   without invoking the runner.
4. WHEN the runner reports a non-zero exit THE SYSTEM SHALL return
   `status=FAILED` with `exit_code`, `stdout`, `stderr`, `command`
   populated.
5. WHEN `elevated=True` is configured THE SYSTEM SHALL pass
   `elevated=True` to the runner for the write step.
6. THE SYSTEM SHALL populate `result.setting="timezone"` and
   `result.previous_value` (when the read probe succeeded) on every
   returned result.

---

### Requirement 9: Enumerate Supported Timezones

**User Story:** As a maintenance coordinator, I want to enumerate
the IANA timezones the host knows about, so that I can validate
timezone values before attempting to set them.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_supported_timezones()` THE SYSTEM
   SHALL invoke `timedatectl list-timezones` with `elevated=False`
   and parse the one-entry-per-line output.
2. THE SYSTEM SHALL return a `list[str]` of IANA timezone strings in
   the order reported by `timedatectl`, with empty lines elided.
3. WHEN the command exits non-zero THE SYSTEM SHALL raise
   `UbuntuLocalizationQueryError`.

---

### Requirement 10: Elevation and Aspect Composition

**User Story:** As a maintenance coordinator, I want every mutating
verb to honour the manager's elevation policy and to be safe under
arbitrary aspect composition on the runner, so that the manager
behaves identically against a `LocalCommandRunner`,
`SSHCommandRunner`, or any wrapped variant.

#### Acceptance Criteria

1. WHEN the manager is configured with `elevated=True` THE SYSTEM
   SHALL pass `elevated=True` to the runner on every write
   (`set_locale`, `set_keymap`, `set_timezone`).
2. WHEN the manager is configured with `elevated=False` THE SYSTEM
   SHALL never pass `elevated=True`.
3. THE SYSTEM SHALL never pass `elevated=True` on any read
   (`get_locale`, `list_supported_locales`, `get_keymap`,
   `get_supported_keymaps`, `get_timezone`,
   `get_supported_timezones`, every idempotency probe).
4. THE SYSTEM SHALL hold no state across calls beyond the injected
   config and runner.
5. THE SYSTEM SHALL be safe under any retry / persistence /
   timeout aspect composition on the runner; the manager makes no
   assumptions about how the runner dispatches commands.
