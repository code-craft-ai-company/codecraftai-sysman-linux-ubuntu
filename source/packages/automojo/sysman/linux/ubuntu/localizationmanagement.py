"""
    Ubuntu localization-management feature.

    Implements the cross-OS localization-management contract for Ubuntu
    using ``localectl``, ``timedatectl``, and ``locale`` (the BSD-style
    enumeration tool that ships with the ``locales`` package on
    Debian / Ubuntu).  Every system command is dispatched through the
    injected :class:`ICommandRunner`; this module never imports
    ``subprocess`` and never opens a file directly.

    Verb catalogue:
        * ``get_locale`` -> ``localectl status`` plus an optional
          ``locale`` dump (the latter is opportunistic — it surfaces
          per-component ``LC_*`` overrides when the host explicitly
          differs from the system default).
        * ``set_locale`` -> ``localectl set-locale LANG=<value>`` plus
          any ``LC_*`` flags the caller supplied.
        * ``list_supported_locales`` -> ``locale -a`` (the canonical
          enumerator on Ubuntu; ``localectl list-locales`` is also
          available but has been observed to return an empty list on
          minimal cloud images, so we anchor to ``locale -a``).
        * ``get_keymap`` -> ``localectl status`` (parses the ``VC
          Keymap`` and ``X11 Layout`` lines).
        * ``set_keymap`` -> ``localectl set-keymap <name>``.  When the
          caller supplies an X11 layout / variant / model the manager
          additionally dispatches ``localectl set-x11-keymap``.
        * ``get_timezone`` -> ``timedatectl show
          --property=Timezone --value``.
        * ``set_timezone`` -> ``timedatectl set-timezone <iana>``.
        * ``get_supported_timezones`` -> ``timedatectl list-timezones``.
        * ``get_supported_keymaps`` -> ``localectl list-keymaps``.

    File I/O policy:
        Every read / write / probe is performed through the injected
        :class:`ICommandRunner`.  This module does NOT consume an
        :class:`IFileOperator`.  ``localectl`` and ``timedatectl`` own
        their own persistence layer (``/etc/locale.conf``,
        ``/etc/vconsole.conf``, ``/etc/X11/xorg.conf.d/00-keyboard.conf``,
        ``/etc/localtime`` and ``/etc/timezone``); the manager relies on
        them to write the on-disk state.

    Elevation policy:
        Every write verb (``set_locale``, ``set_keymap``,
        ``set_timezone``) honours ``config.elevated``.  Reads
        (``get_locale``, ``get_keymap``, ``get_timezone``,
        ``list_supported_locales``, ``get_supported_keymaps``,
        ``get_supported_timezones``, every idempotency probe) never
        elevate.

    Idempotency:
        * ``set_locale`` reads ``localectl status``; when the parsed
          ``LANG`` value matches the request and every supplied
          ``LC_*`` override matches, the manager returns ``SKIPPED``.
        * ``set_keymap`` skips when the parsed ``VC Keymap`` matches
          the requested console keymap.  The X11 update is independent
          of the VC update and may dispatch even when the console
          update skips.
        * ``set_timezone`` reads
          ``timedatectl show --property=Timezone`` and skips when the
          IANA strings match exactly.
"""

import os

from dataclasses import dataclass, field
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------


_DEFAULT_LOCALECTL = "localectl"
_DEFAULT_TIMEDATECTL = "timedatectl"
_DEFAULT_LOCALE_TOOL = "locale"

# ``localectl status`` field prefixes.  Every line that the parser cares
# about begins with one of these.  The trailing colon is part of the
# match so we never confuse e.g. ``System Locale:`` with ``System
# Locale Mode:``.
_PFX_SYSTEM_LOCALE = "System Locale:"
_PFX_VC_KEYMAP = "VC Keymap:"
_PFX_X11_LAYOUT = "X11 Layout:"
_PFX_X11_VARIANT = "X11 Variant:"
_PFX_X11_MODEL = "X11 Model:"
_PFX_X11_OPTIONS = "X11 Options:"


UbuntuLocalizationActionStatus = ActionStatus


@dataclass
class UbuntuLocalizationManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuLocalizationManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param localectl_path: Path / name of the ``localectl`` binary.
                               Defaults to ``"localectl"`` (PATH-resolved).
                               Tests can override to inject a wrapper or a
                               sibling build.
        :param timedatectl_path: Path / name of the ``timedatectl`` binary.
                                 Defaults to ``"timedatectl"``.
        :param locale_tool_path: Path / name of the ``locale`` binary used
                                 by :meth:`list_supported_locales`.
                                 Defaults to ``"locale"``.
    """
    localectl_path: str = _DEFAULT_LOCALECTL
    timedatectl_path: str = _DEFAULT_TIMEDATECTL
    locale_tool_path: str = _DEFAULT_LOCALE_TOOL


@dataclass
class UbuntuLocalizationActionResult(FeatureActionResult):
    """
        Result of a localization-management write operation.

        :param setting: The localization field targeted: one of
                        ``"locale"``, ``"keymap"``, or ``"timezone"``.
                        ``None`` for compound operations that span more
                        than one field.
        :param previous_value: The value read prior to the mutation, when
                               the manager was able to probe it.  Always
                               populated on ``SKIPPED`` results (it equals
                               the requested value); populated on
                               ``SUCCESS`` and ``DRY_RUN`` when the
                               idempotency probe ran cleanly; ``None``
                               when the probe failed.
        :param requires_reboot: Set when the underlying mutation requires
                                a reboot to take effect.  ``localectl``
                                writes generally do NOT — they are picked
                                up by new sessions on the next login —
                                but the field is reserved on the result
                                shape for parity with the contract's
                                Windows ``Set-WinSystemLocale`` path.
    """
    setting: Optional[str] = None
    previous_value: Optional[str] = None
    requires_reboot: bool = False


@dataclass
class UbuntuLocaleInfo:
    """
        Snapshot of the system locale state as parsed from
        ``localectl status`` and (optionally) ``locale``.

        :param language: The active ``LANG`` value (e.g.
                         ``"en_US.UTF-8"``).  May be the empty string
                         when ``localectl`` reports no system locale.
        :param country: Country code derived from ``language`` when the
                        ``language`` value contains an underscore-
                        separated region (e.g. ``"US"`` for
                        ``"en_US.UTF-8"``).  ``None`` when the parse
                        cannot derive one.
        :param encoding: Encoding tag derived from ``language`` (e.g.
                         ``"UTF-8"``).  ``None`` when ``language`` carries
                         no ``.<encoding>`` suffix.
        :param lc_messages: ``LC_MESSAGES`` override when present in the
                            system locale.  ``None`` when not set.
        :param lc_time: ``LC_TIME`` override when present.
        :param lc_numeric: ``LC_NUMERIC`` override when present.
        :param system_default: Echo of the ``System Locale:`` line as
                               emitted by ``localectl status``.  Useful
                               for callers that want the verbatim
                               original payload.
        :param raw: Full mapping of every ``KEY=value`` token parsed out
                    of the ``System Locale:`` line.  Always populated.
    """
    language: str
    country: Optional[str] = None
    encoding: Optional[str] = None
    lc_messages: Optional[str] = None
    lc_time: Optional[str] = None
    lc_numeric: Optional[str] = None
    system_default: Optional[str] = None
    raw: dict = field(default_factory=dict)


@dataclass
class UbuntuKeymapInfo:
    """
        Snapshot of the keyboard configuration parsed from
        ``localectl status``.

        :param name: The active console (VC) keymap (e.g. ``"us"``,
                     ``"uk"``).  May be the empty string when
                     ``localectl`` reports no VC keymap (e.g. on a
                     console-less cloud image).
        :param layout: The active X11 layout, when set.  ``None`` when
                       ``localectl`` reports no X11 layout.
        :param variant: The active X11 variant, when set.  ``None`` when
                        none is configured.
        :param model: The active X11 model, when set.  ``None`` when
                      none is configured.  ``localectl status`` does not
                      always print this line; it stays ``None`` when
                      absent.
        :param options: The active X11 options string, when set.
                        ``None`` when none is configured.
    """
    name: str
    layout: Optional[str] = None
    variant: Optional[str] = None
    model: Optional[str] = None
    options: Optional[str] = None


class UbuntuLocalizationQueryError(Exception):
    """
        Raised when a localization read fails for a reason other than a
        plainly empty payload (which the parsers tolerate).
    """

    def __init__(
        self,
        *,
        command: Optional[list[str]] = None,
        exit_code: Optional[int] = None,
        stderr: str = "",
        message: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed (when relevant).
            :param exit_code: The runner's exit code (when relevant).
            :param stderr: The runner's captured stderr (when relevant).
            :param message: Optional pre-built message used for cases
                            where there is no failed argv to report
                            (e.g. parse errors on otherwise-successful
                            output).
        """
        if command is None:
            self.command: list[str] = []
        else:
            self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        if message is not None:
            err_msg = message
        else:
            err_msg_lines = ["Localization query command failed."]
            err_msg_lines.append("    COMMAND: {}".format(
                " ".join(self.command),
            ))
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
            err_msg_lines.append("    STDERR: {}".format(stderr))
            err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _parse_localectl_status(*, text: str) -> dict:
    """
        Parse ``localectl status`` output into a dict.

        ``localectl status`` emits stanzas of the form::

            System Locale: LANG=en_US.UTF-8 LC_TIME=en_GB.UTF-8
                VC Keymap: us
               X11 Layout: us
              X11 Variant: dvorak
                X11 Model: pc105
              X11 Options: ctrl:nocaps

        The parser tolerates any subset of these lines and returns a
        dict whose keys are the canonical lower-case-snake-case names
        (``system_locale``, ``vc_keymap``, ``x11_layout``,
        ``x11_variant``, ``x11_model``, ``x11_options``).  Missing
        lines are simply absent from the dict.

        :param text: Raw stdout of ``localectl status``.

        :returns: A dict with whichever keys were parsed.
    """
    rtnval: dict = {}
    if text is None:
        return rtnval
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(_PFX_SYSTEM_LOCALE):
            value = line[len(_PFX_SYSTEM_LOCALE):].strip()
            rtnval["system_locale"] = value
            continue
        if line.startswith(_PFX_VC_KEYMAP):
            value = line[len(_PFX_VC_KEYMAP):].strip()
            rtnval["vc_keymap"] = value
            continue
        if line.startswith(_PFX_X11_LAYOUT):
            value = line[len(_PFX_X11_LAYOUT):].strip()
            rtnval["x11_layout"] = value
            continue
        if line.startswith(_PFX_X11_VARIANT):
            value = line[len(_PFX_X11_VARIANT):].strip()
            rtnval["x11_variant"] = value
            continue
        if line.startswith(_PFX_X11_MODEL):
            value = line[len(_PFX_X11_MODEL):].strip()
            rtnval["x11_model"] = value
            continue
        if line.startswith(_PFX_X11_OPTIONS):
            value = line[len(_PFX_X11_OPTIONS):].strip()
            rtnval["x11_options"] = value
            continue
    return rtnval


def _parse_system_locale_kv(*, text: str) -> dict:
    """
        Parse the ``System Locale:`` body into a ``dict[str, str]``.

        The body is whitespace-separated ``KEY=VALUE`` tokens.  Tokens
        without an ``=`` are silently skipped.  ``localectl`` does not
        quote values, so the parser does not unquote either.

        :param text: The body following ``System Locale:`` (without the
                     prefix).

        :returns: A ``dict[str, str]`` of every parsed pair.
    """
    rtnval: dict = {}
    if text is None:
        return rtnval
    stripped = text.strip()
    if len(stripped) == 0:
        return rtnval
    for token in stripped.split():
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        if len(key) == 0:
            continue
        rtnval[key] = value
    return rtnval


def _derive_country_and_encoding(*, language: str) -> tuple:
    """
        Derive ``(country, encoding)`` from a ``LANG`` value.

        Examples::

            "en_US.UTF-8" -> ("US", "UTF-8")
            "en_US"       -> ("US", None)
            "C.UTF-8"     -> (None, "UTF-8")
            "C"           -> (None, None)

        :param language: A ``LANG`` value as parsed from
                         ``localectl status``.

        :returns: A 2-tuple ``(country, encoding)``.  Either element
                  may be ``None``.
    """
    if language is None:
        rtnval = (None, None)
        return rtnval
    stripped = language.strip()
    if len(stripped) == 0:
        rtnval = (None, None)
        return rtnval

    encoding: Optional[str]
    if "." in stripped:
        head, _, encoding_text = stripped.partition(".")
        encoding = encoding_text if len(encoding_text) > 0 else None
    else:
        head = stripped
        encoding = None

    country: Optional[str]
    if "_" in head:
        _, _, country_text = head.partition("_")
        country = country_text if len(country_text) > 0 else None
    else:
        country = None

    rtnval = (country, encoding)
    return rtnval


def _parse_locale_a(*, text: str) -> list:
    """
        Parse the output of ``locale -a`` into a list of locale names.

        ``locale -a`` emits one locale per line.  Blank lines are
        dropped.  The order is preserved.

        :param text: Raw stdout.

        :returns: A list of locale strings.
    """
    rtnval: list = []
    if text is None:
        return rtnval
    for raw in text.splitlines():
        entry = raw.strip()
        if len(entry) == 0:
            continue
        rtnval.append(entry)
    return rtnval


def _parse_list_lines(*, text: str) -> list:
    """
        Parse a one-entry-per-line listing into a list of strings.

        Used by :meth:`UbuntuLocalizationManager.get_supported_timezones`
        and :meth:`UbuntuLocalizationManager.get_supported_keymaps`.

        :param text: Raw stdout.

        :returns: A list of trimmed non-empty entries in source order.
    """
    rtnval: list = []
    if text is None:
        return rtnval
    for raw in text.splitlines():
        entry = raw.strip()
        if len(entry) == 0:
            continue
        rtnval.append(entry)
    return rtnval


# ----------------------------------------------------------------------
# UbuntuLocalizationManager
# ----------------------------------------------------------------------


class UbuntuLocalizationManager(BaseFeatureManager):
    """
        Ubuntu localization manager.  Wraps ``localectl``,
        ``timedatectl``, and ``locale`` through the injected
        :class:`ICommandRunner`.  Reads never elevate; writes elevate
        when ``config.elevated`` is True.
    """

    result_class = UbuntuLocalizationActionResult
    status_class = UbuntuLocalizationActionStatus
    feature_name = "localization-management"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_locale(self) -> UbuntuLocaleInfo:
        """
            Read the active system locale via ``localectl status``.

            :returns: The :class:`UbuntuLocaleInfo`.
            :raises UbuntuLocalizationQueryError: When ``localectl``
                                                  exits non-zero.
        """
        command = [self._typed_config().localectl_path, "status"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuLocalizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        parsed = _parse_localectl_status(text=run_result.stdout)
        system_locale_text = parsed.get("system_locale")
        kv = _parse_system_locale_kv(text=system_locale_text or "")
        language = kv.get("LANG", "")
        country, encoding = _derive_country_and_encoding(language=language)
        info = UbuntuLocaleInfo(
            language=language,
            country=country,
            encoding=encoding,
            lc_messages=kv.get("LC_MESSAGES"),
            lc_time=kv.get("LC_TIME"),
            lc_numeric=kv.get("LC_NUMERIC"),
            system_default=system_locale_text,
            raw=kv,
        )
        return info

    def list_supported_locales(self) -> list:
        """
            Enumerate locales installed on the system via ``locale -a``.

            :returns: A list of locale identifiers (e.g.
                      ``["C", "C.UTF-8", "en_US.UTF-8"]``) in the order
                      reported by ``locale -a``.
            :raises UbuntuLocalizationQueryError: When ``locale -a``
                                                  exits non-zero.
        """
        command = [self._typed_config().locale_tool_path, "-a"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuLocalizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = _parse_locale_a(text=run_result.stdout)
        return rtnval

    def get_keymap(self) -> UbuntuKeymapInfo:
        """
            Read the active keyboard configuration via
            ``localectl status``.

            :returns: The :class:`UbuntuKeymapInfo`.
            :raises UbuntuLocalizationQueryError: When ``localectl``
                                                  exits non-zero.
        """
        command = [self._typed_config().localectl_path, "status"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuLocalizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        parsed = _parse_localectl_status(text=run_result.stdout)
        info = UbuntuKeymapInfo(
            name=parsed.get("vc_keymap", ""),
            layout=parsed.get("x11_layout"),
            variant=parsed.get("x11_variant"),
            model=parsed.get("x11_model"),
            options=parsed.get("x11_options"),
        )
        return info

    def get_supported_keymaps(self) -> list:
        """
            Enumerate VC keymaps via ``localectl list-keymaps``.

            :returns: A list of keymap names in the order reported by
                      ``localectl``.
            :raises UbuntuLocalizationQueryError: When ``localectl``
                                                  exits non-zero.
        """
        command = [self._typed_config().localectl_path, "list-keymaps"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuLocalizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = _parse_list_lines(text=run_result.stdout)
        return rtnval

    def get_timezone(self) -> str:
        """
            Read the active system timezone via
            ``timedatectl show --property=Timezone --value``.

            :returns: The IANA timezone string (e.g.
                      ``"America/Los_Angeles"``).  Returns the empty
                      string when ``timedatectl`` reports no timezone.
            :raises UbuntuLocalizationQueryError: When ``timedatectl``
                                                  exits non-zero.
        """
        command = [
            self._typed_config().timedatectl_path,
            "show",
            "--property=Timezone",
            "--value",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuLocalizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = run_result.stdout.strip()
        return rtnval

    def get_supported_timezones(self) -> list:
        """
            Enumerate IANA timezones via ``timedatectl list-timezones``.

            :returns: A list of IANA timezone strings (e.g.
                      ``["Africa/Abidjan", ..., "UTC"]``) in the order
                      reported by ``timedatectl``.
            :raises UbuntuLocalizationQueryError: When ``timedatectl``
                                                  exits non-zero.
        """
        command = [
            self._typed_config().timedatectl_path,
            "list-timezones",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuLocalizationQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )
        rtnval = _parse_list_lines(text=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def set_locale(
        self,
        locale: str,
        *,
        lc_overrides: Optional[dict] = None,
    ) -> UbuntuLocalizationActionResult:
        """
            Set the system locale via ``localectl set-locale``.

            Idempotent: when the parsed ``LANG`` already matches and
            every supplied ``LC_*`` override already matches, returns
            ``SKIPPED`` without invoking ``localectl``.

            :param locale: The desired ``LANG`` value (e.g.
                           ``"en_US.UTF-8"``).
            :param lc_overrides: Optional ``LC_*`` overrides.  Keys must
                                 be ``LC_*`` token names; the manager
                                 does not validate the keys and forwards
                                 them verbatim.

            :returns: The :class:`UbuntuLocalizationActionResult`.
        """
        cli_args = ["LANG={}".format(locale)]
        if lc_overrides is not None:
            for key in sorted(lc_overrides.keys()):
                value = lc_overrides[key]
                cli_args.append("{}={}".format(key, value))

        command = [self._typed_config().localectl_path, "set-locale"]
        command.extend(cli_args)

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuLocalizationActionStatus.DRY_RUN,
                setting="locale",
                detail=locale,
                command=command,
            )
            return dry

        previous_value: Optional[str] = None
        try:
            current = self.get_locale()
            previous_value = current.language
            already_matches = current.language == locale
            if already_matches and lc_overrides is not None:
                for key, value in lc_overrides.items():
                    if current.raw.get(key) != value:
                        already_matches = False
                        break
            if already_matches:
                skipped = self._build_result(
                    status=UbuntuLocalizationActionStatus.SKIPPED,
                    setting="locale",
                    detail=locale,
                    previous_value=previous_value,
                    command=command,
                )
                return skipped
        except UbuntuLocalizationQueryError:
            previous_value = None

        rtnval = self._dispatch_localization_write(
            command=command,
            setting="locale",
            requested=locale,
            previous_value=previous_value,
        )
        return rtnval

    def set_keymap(
        self,
        keymap: str,
        *,
        x11_layout: Optional[str] = None,
        x11_variant: Optional[str] = None,
        x11_model: Optional[str] = None,
    ) -> UbuntuLocalizationActionResult:
        """
            Set the VC keymap (and optionally the X11 layout) via
            ``localectl set-keymap`` / ``localectl set-x11-keymap``.

            Idempotent: when the parsed ``VC Keymap`` already matches
            ``keymap`` (and every X11 component matches when supplied),
            returns ``SKIPPED``.  Otherwise dispatches the relevant
            update.

            When both VC and X11 updates are needed, the manager runs
            ``localectl set-keymap`` first and ``localectl set-x11-keymap``
            second.  If either invocation fails, the result reports
            ``FAILED`` with that invocation's exit / stderr / command;
            the other invocation is skipped.

            :param keymap: The desired VC keymap name.
            :param x11_layout: Optional X11 layout to apply.  When
                               supplied, the manager calls
                               ``localectl set-x11-keymap``.
            :param x11_variant: Optional X11 variant.  Ignored when
                                ``x11_layout`` is ``None``.
            :param x11_model: Optional X11 model.  Ignored when
                              ``x11_layout`` is ``None``.

            :returns: The :class:`UbuntuLocalizationActionResult`.
        """
        vc_command = [
            self._typed_config().localectl_path, "set-keymap", keymap,
        ]

        if self._config.dry_run:
            if x11_layout is None:
                dry_command = vc_command
            else:
                dry_command = list(vc_command)
                dry_command.append("&&")
                dry_command.extend(
                    self._build_x11_command(
                        layout=x11_layout,
                        variant=x11_variant,
                        model=x11_model,
                    )
                )
            dry = self._build_result(
                status=UbuntuLocalizationActionStatus.DRY_RUN,
                setting="keymap",
                detail=keymap,
                command=dry_command,
            )
            return dry

        previous_value: Optional[str] = None
        try:
            current = self.get_keymap()
            previous_value = current.name
            vc_matches = current.name == keymap
            x11_matches = True
            if x11_layout is not None:
                if current.layout != x11_layout:
                    x11_matches = False
                if x11_variant is not None and current.variant != x11_variant:
                    x11_matches = False
                if x11_model is not None and current.model != x11_model:
                    x11_matches = False
            if vc_matches and x11_matches:
                skipped = self._build_result(
                    status=UbuntuLocalizationActionStatus.SKIPPED,
                    setting="keymap",
                    detail=keymap,
                    previous_value=previous_value,
                    command=vc_command,
                )
                return skipped
        except UbuntuLocalizationQueryError:
            previous_value = None
            vc_matches = False
            x11_matches = x11_layout is None

        if not vc_matches:
            vc_result = self._dispatch_localization_write(
                command=vc_command,
                setting="keymap",
                requested=keymap,
                previous_value=previous_value,
            )
            if vc_result.status != UbuntuLocalizationActionStatus.SUCCESS:
                return vc_result
        else:
            vc_result = self._build_result(
                status=UbuntuLocalizationActionStatus.SKIPPED,
                setting="keymap",
                detail=keymap,
                previous_value=previous_value,
                command=vc_command,
            )

        if x11_layout is None or x11_matches:
            return vc_result

        x11_command = self._build_x11_command(
            layout=x11_layout,
            variant=x11_variant,
            model=x11_model,
        )
        x11_result = self._dispatch_localization_write(
            command=x11_command,
            setting="keymap",
            requested=x11_layout,
            previous_value=previous_value,
        )
        return x11_result

    def set_timezone(self, timezone: str) -> UbuntuLocalizationActionResult:
        """
            Set the system timezone via
            ``timedatectl set-timezone <iana>``.

            Idempotent: when the parsed timezone already matches the
            requested IANA string, returns ``SKIPPED`` without invoking
            ``timedatectl``.

            :param timezone: The desired IANA timezone string (e.g.
                             ``"America/Los_Angeles"``).

            :returns: The :class:`UbuntuLocalizationActionResult`.
        """
        command = [
            self._typed_config().timedatectl_path,
            "set-timezone",
            timezone,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuLocalizationActionStatus.DRY_RUN,
                setting="timezone",
                detail=timezone,
                command=command,
            )
            return dry

        previous_value: Optional[str] = None
        try:
            previous_value = self.get_timezone()
            if previous_value == timezone:
                skipped = self._build_result(
                    status=UbuntuLocalizationActionStatus.SKIPPED,
                    setting="timezone",
                    detail=timezone,
                    previous_value=previous_value,
                    command=command,
                )
                return skipped
        except UbuntuLocalizationQueryError:
            previous_value = None

        rtnval = self._dispatch_localization_write(
            command=command,
            setting="timezone",
            requested=timezone,
            previous_value=previous_value,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> UbuntuLocalizationManagerConfig:
        """
            Return the manager's config typed.  When the caller supplied
            a bare :class:`FeatureConfig`, the helper synthesises a
            :class:`UbuntuLocalizationManagerConfig` carrying the same
            ``dry_run`` / ``elevated`` flags so the path-fields land on
            their defaults.

            :returns: The typed configuration.
        """
        if isinstance(self._config, UbuntuLocalizationManagerConfig):
            rtnval = self._config
        else:
            rtnval = UbuntuLocalizationManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _build_x11_command(
        self,
        *,
        layout: str,
        variant: Optional[str],
        model: Optional[str],
    ) -> list:
        """
            Build the ``localectl set-x11-keymap`` argv.

            ``localectl set-x11-keymap`` accepts up to four positional
            arguments::

                set-x11-keymap <layout> [<model> [<variant> [<options>]]]

            We do not surface ``options`` on the public verb (the
            cross-OS contract does not expose it); when only ``layout``
            is supplied we dispatch a one-arg form.

            :param layout: The desired X11 layout.
            :param variant: Optional variant.
            :param model: Optional model.

            :returns: The argv list.
        """
        rtnval = [
            self._typed_config().localectl_path,
            "set-x11-keymap",
            layout,
        ]
        if model is not None:
            rtnval.append(model)
            if variant is not None:
                rtnval.append(variant)
        elif variant is not None:
            # ``set-x11-keymap`` requires <model> before <variant>.  When
            # the caller supplies only a variant we fill ``-`` for the
            # model slot, which ``localectl`` accepts as "leave the model
            # alone".
            rtnval.append("-")
            rtnval.append(variant)
        return rtnval

    def _dispatch_localization_write(
        self,
        *,
        command: list,
        setting: str,
        requested: str,
        previous_value: Optional[str],
    ) -> UbuntuLocalizationActionResult:
        """
            Dispatch a mutating localization command.

            :param command: The argv list.
            :param setting: One of ``"locale"``, ``"keymap"``,
                            ``"timezone"``.
            :param requested: The requested value.
            :param previous_value: The previously-read value, if any.

            :returns: The :class:`UbuntuLocalizationActionResult`.
        """
        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = UbuntuLocalizationActionStatus.SUCCESS
        else:
            mapped_status = UbuntuLocalizationActionStatus.FAILED

        rtnval = UbuntuLocalizationActionResult(
            status=mapped_status,
            detail=requested,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            setting=setting,
            previous_value=previous_value,
            requires_reboot=False,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuLocalizationActionStatus,
        setting: Optional[str],
        detail: Optional[str],
        command: list,
        previous_value: Optional[str] = None,
        exit_code: Optional[int] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        requires_reboot: bool = False,
    ) -> UbuntuLocalizationActionResult:
        """
            Construct an :class:`UbuntuLocalizationActionResult` without
            invoking the runner.  Used for ``DRY_RUN`` / ``SKIPPED`` /
            synthetic ``FAILED`` results.

            :returns: The :class:`UbuntuLocalizationActionResult`.
        """
        rtnval = UbuntuLocalizationActionResult(
            status=status,
            detail=detail,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            command=list(command),
            setting=setting,
            previous_value=previous_value,
            requires_reboot=requires_reboot,
        )
        return rtnval
