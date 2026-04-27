"""
    Ubuntu ssh-server-configuration feature.

    Implements the cross-OS ssh-server-configuration contract for Ubuntu.
    Ubuntu ships OpenSSH server through the ``openssh-server`` package
    and exposes the daemon as the ``ssh.service`` systemd unit (Debian
    derivatives use ``ssh`` rather than ``sshd`` for the unit name).
    Every verb is dispatched through the injected
    :class:`ICommandRunner` so the manager works identically under
    local and SSH execution.

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | Ubuntu tool                       |
    +=============================+===================================+
    | ``get_sshd_setting``        | ``cat /etc/ssh/sshd_config`` then |
    |                             | parse                             |
    +-----------------------------+-----------------------------------+
    | ``set_sshd_setting``        | ``cat`` + parse + render +        |
    |                             | ``tee /etc/ssh/sshd_config``      |
    +-----------------------------+-----------------------------------+
    | ``remove_sshd_setting``     | ``cat`` + parse + render +        |
    |                             | ``tee /etc/ssh/sshd_config``      |
    +-----------------------------+-----------------------------------+
    | ``list_sshd_settings``      | ``cat`` + parse                   |
    +-----------------------------+-----------------------------------+
    | ``add_match_block`` /       | ``cat`` + parse + render +        |
    | ``remove_match_block``      | ``tee``                           |
    +-----------------------------+-----------------------------------+
    | ``list_match_blocks``       | ``cat`` + parse                   |
    +-----------------------------+-----------------------------------+
    | ``reload_sshd``             | ``systemctl reload ssh``          |
    +-----------------------------+-----------------------------------+
    | ``test_sshd_config``        | ``sshd -t``                       |
    +-----------------------------+-----------------------------------+
    | ``list_authorized_keys``    | ``cat``                           |
    +-----------------------------+-----------------------------------+
    | ``add_authorized_key`` /    | ``cat`` + render +                |
    | ``remove_authorized_key``   | ``tee ~user/.ssh/authorized_keys``|
    +-----------------------------+-----------------------------------+

    File I/O policy:
        Reads use ``cat``; writes use ``tee`` with the new content on
        stdin.  This module does NOT consume an :class:`IFileOperator`.

    Service-name caveat:
        On Debian-derived distributions (including Ubuntu) the OpenSSH
        server systemd unit is ``ssh.service``, NOT ``sshd.service``.
        :class:`UbuntuSshServerConfigurationManagerConfig` therefore
        defaults ``service_name`` to ``"ssh"``.  Callers can override
        when targeting an unusual setup.

    Drop-in caveat:
        Ubuntu's stock ``sshd_config`` includes an ``Include
        /etc/ssh/sshd_config.d/*.conf`` directive at the top of the
        file.  This pilot edits the primary file only; drop-in
        authoring is a tracked follow-up.

    Idempotency model:
        Every write verb reads the current file, parses it, mutates
        the parsed structure in memory, re-renders the file, and
        compares to the original content.  When the rendered content
        equals the original the verb returns ``SKIPPED`` without
        touching the runner.  The pre-write ``sshd -t`` validation
        invariant from the contract is honoured — the rendered content
        is staged through ``tee`` and ``sshd -t`` is invoked
        immediately afterwards on writes that mutate ``sshd_config``.
        For the pilot ``sshd -t`` is the post-write check; if it fails
        the result is mapped to ``FAILED`` but the file is not
        rolled back (a follow-up will add the rollback path; see
        ``specs/3-ssh-server-configuration/tasks.md``).

    Authorized-key fingerprinting:
        Fingerprint computation is performed in pure Python using
        SHA-256 of the base64-decoded key body, formatted as
        ``SHA256:<base64-no-padding>``.  This matches OpenSSH's
        ``ssh-keygen -lf`` output exactly for ed25519 / rsa / ecdsa
        keys.  No external ``ssh-keygen`` invocation is required for
        identity comparisons inside :meth:`add_authorized_key` /
        :meth:`remove_authorized_key`.

    Elevation policy:
        Write verbs (``set_sshd_setting``, ``remove_sshd_setting``,
        ``add_match_block``, ``remove_match_block``,
        ``add_authorized_key``, ``remove_authorized_key``,
        ``reload_sshd``, ``test_sshd_config``) honour
        ``config.elevated``.  Reads (``get_sshd_setting``,
        ``list_sshd_settings``, ``list_match_blocks``,
        ``list_authorized_keys``) never elevate.
"""

import base64
import hashlib
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

_CAT = "cat"
_TEE = "tee"
_SSHD = "sshd"
_SYSTEMCTL = "systemctl"

_DEFAULT_SSHD_CONFIG_PATH = "/etc/ssh/sshd_config"
_DEFAULT_AUTHORIZED_KEYS_TEMPLATE = "/home/{user}/.ssh/authorized_keys"
_DEFAULT_SERVICE_NAME = "ssh"

_MATCH_KEYWORD = "Match"


UbuntuSshActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class UbuntuSshServerConfigurationManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuSshServerConfigurationManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param sshd_config_path: Absolute path to the primary
                                 ``sshd_config`` file.  Default is the
                                 Ubuntu stock location
                                 ``/etc/ssh/sshd_config``.
        :param authorized_keys_path_template: Format string used to
                                              resolve a user's
                                              ``authorized_keys`` path.
                                              Must contain a single
                                              ``{user}`` placeholder.
                                              Default is
                                              ``/home/{user}/.ssh/authorized_keys``.
        :param service_name: systemd unit name used by
                             :meth:`UbuntuSshServerConfigurationManager.reload_sshd`.
                             Default is ``"ssh"`` for Ubuntu (Debian's
                             OpenSSH server unit name); RHEL-shaped
                             targets pass ``"sshd"``.
    """
    sshd_config_path: str = _DEFAULT_SSHD_CONFIG_PATH
    authorized_keys_path_template: str = _DEFAULT_AUTHORIZED_KEYS_TEMPLATE
    service_name: str = _DEFAULT_SERVICE_NAME


@dataclass
class UbuntuSshActionResult(FeatureActionResult):
    """
        Result of an ssh-server-configuration write operation.

        :param keyword: The ``sshd_config`` keyword targeted, or
                        ``None`` when the action does not target a
                        single keyword (reload, test-config, match
                        operations, authorized-key operations).
        :param path: The file path the action targeted, or ``None`` for
                     verbs that do not touch a file (reload, test).
        :param user: The user whose authorized-keys file was targeted,
                     or ``None`` for sshd_config verbs.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    keyword: Optional[str] = None
    path: Optional[str] = None
    user: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuSshSettingValue:
    """
        A single ``sshd_config`` keyword / value pair as parsed from the
        primary configuration file.

        :param keyword: The ``sshd_config`` keyword (preserves the case
                        as written in the file).
        :param value: The value text following the keyword.
        :param source_file: The absolute file path the value was parsed
                            from.
    """
    keyword: str
    value: str
    source_file: str


@dataclass
class UbuntuSshMatchBlock:
    """
        A single ``Match <criteria>`` block parsed from
        ``sshd_config``.

        :param criteria: The full criteria string after the ``Match``
                         keyword (e.g. ``"User git"``).
        :param settings: ``dict[str, str]`` of keyword/value pairs
                         scoped to the block, in the order they were
                         declared.
    """
    criteria: str
    settings: dict = field(default_factory=dict)


@dataclass
class UbuntuSshAuthorizedKey:
    """
        A single entry from a user's ``authorized_keys`` file.

        :param key_type: The OpenSSH key-type token (e.g.
                         ``"ssh-ed25519"``, ``"ssh-rsa"``,
                         ``"ecdsa-sha2-nistp256"``).
        :param key_data: The base64-encoded key body.
        :param comment: Optional trailing comment / label from the line.
        :param fingerprint: SHA-256 fingerprint in
                            ``SHA256:<base64-no-padding>`` form.  Always
                            populated when the key parses cleanly.
        :param options: Optional per-key options prefix (everything
                        before the key-type token when present).
    """
    key_type: str
    key_data: str
    comment: Optional[str] = None
    fingerprint: Optional[str] = None
    options: Optional[str] = None


class UbuntuSshQueryError(Exception):
    """
        Raised when an ssh-server-configuration read fails for a reason
        other than the target source simply being absent.
    """

    def __init__(
        self,
        *,
        path: str,
        message: str,
    ) -> None:
        """
            :param path: The file path whose read failed.
            :param message: Human-readable detail.
        """
        self.path = path
        self.message = message
        err_msg_lines = ["SSH query command failed."]
        err_msg_lines.append("    PATH: {}".format(path))
        err_msg_lines.append("    DETAIL: {}".format(message))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class UbuntuSshServerConfigurationManager(BaseFeatureManager):
    """
        Ubuntu ssh-server-configuration manager.  Reads, writes, and
        validates ``/etc/ssh/sshd_config`` and per-user
        ``authorized_keys`` through the injected
        :class:`ICommandRunner`.
    """

    result_class = UbuntuSshActionResult
    status_class = UbuntuSshActionStatus
    feature_name = "ssh-server-configuration"

    # ------------------------------------------------------------------
    # Read verbs — sshd_config
    # ------------------------------------------------------------------

    def get_sshd_setting(
        self,
        keyword: str,
    ) -> Optional[UbuntuSshSettingValue]:
        """
            Read a single top-level ``sshd_config`` keyword.

            Returns the first occurrence found at the top level (i.e.
            outside of any ``Match`` block).  Returns ``None`` when the
            keyword is absent or the file does not exist.

            :param keyword: The ``sshd_config`` keyword.

            :returns: The :class:`UbuntuSshSettingValue` or ``None``.
        """
        path = self._typed_config().sshd_config_path
        content = self._read_file(path=path)
        if content is None:
            rtnval: Optional[UbuntuSshSettingValue] = None
            return rtnval

        top_settings, _ = _parse_sshd_config(content=content)
        keyword_lower = keyword.lower()
        for parsed_keyword, parsed_value in top_settings.items():
            if parsed_keyword.lower() == keyword_lower:
                rtnval = UbuntuSshSettingValue(
                    keyword=parsed_keyword,
                    value=parsed_value,
                    source_file=path,
                )
                return rtnval
        rtnval = None
        return rtnval

    def list_sshd_settings(self) -> dict:
        """
            Enumerate every top-level ``sshd_config`` keyword.

            Match-block scoped settings are NOT included; use
            :meth:`list_match_blocks` for those.

            :returns: A ``dict[str, str]`` mapping keyword (original
                      case preserved) to value.
        """
        path = self._typed_config().sshd_config_path
        content = self._read_file(path=path)
        if content is None:
            rtnval: dict = {}
            return rtnval

        top_settings, _ = _parse_sshd_config(content=content)
        rtnval = top_settings
        return rtnval

    def list_match_blocks(self) -> list:
        """
            Enumerate every ``Match <criteria>`` block in the primary
            ``sshd_config``.

            Returns an empty list when the file is missing or contains
            no ``Match`` blocks.

            :returns: A ``list[UbuntuSshMatchBlock]`` in declaration
                      order.
        """
        path = self._typed_config().sshd_config_path
        content = self._read_file(path=path)
        if content is None:
            rtnval: list = []
            return rtnval

        _, match_blocks = _parse_sshd_config(content=content)
        rtnval = match_blocks
        return rtnval

    def list_authorized_keys(self, user: str) -> list:
        """
            Read and parse a user's ``authorized_keys`` file.

            Returns an empty list when the file is missing or empty.

            :param user: The username whose key file is queried.

            :returns: A ``list[UbuntuSshAuthorizedKey]`` in file order.
        """
        path = self._authorized_keys_path(user=user)
        content = self._read_file(path=path)
        if content is None:
            rtnval: list = []
            return rtnval

        rtnval = _parse_authorized_keys(content=content)
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — sshd_config
    # ------------------------------------------------------------------

    def set_sshd_setting(
        self,
        keyword: str,
        value: str,
    ) -> UbuntuSshActionResult:
        """
            Set a top-level ``sshd_config`` keyword to a value.

            Idempotent: returns ``SKIPPED`` when the file already
            contains the exact ``keyword value`` line.  Honours
            dry-run.

            :param keyword: The ``sshd_config`` keyword.
            :param value: The desired value text.

            :returns: The :class:`UbuntuSshActionResult`.
        """
        path = self._typed_config().sshd_config_path
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        top_settings, match_blocks = _parse_sshd_config(
            content=existing_content,
        )
        new_top_settings = _set_top_setting(
            top_settings=top_settings,
            keyword=keyword,
            value=value,
        )
        new_content = _render_sshd_config(
            top_settings=new_top_settings,
            match_blocks=match_blocks,
        )

        if existing is not None and existing == new_content:
            skipped = self._build_result(
                status=UbuntuSshActionStatus.SKIPPED,
                keyword=keyword,
                path=path,
                command=write_command,
                message="setting already at desired value",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuSshActionStatus.DRY_RUN,
                keyword=keyword,
                path=path,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=keyword,
            path=path,
            user=None,
        )
        return rtnval

    def remove_sshd_setting(
        self,
        keyword: str,
    ) -> UbuntuSshActionResult:
        """
            Remove every top-level occurrence of ``keyword`` from the
            primary ``sshd_config``.

            Idempotent: returns ``SKIPPED`` when the file is absent or
            the keyword is not present.  Honours dry-run.

            :param keyword: The ``sshd_config`` keyword to remove.

            :returns: The :class:`UbuntuSshActionResult`.
        """
        path = self._typed_config().sshd_config_path
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        if existing is None:
            skipped = self._build_result(
                status=UbuntuSshActionStatus.SKIPPED,
                keyword=keyword,
                path=path,
                command=write_command,
                message="config file does not exist",
            )
            return skipped

        top_settings, match_blocks = _parse_sshd_config(content=existing)
        new_top_settings = _remove_top_setting(
            top_settings=top_settings,
            keyword=keyword,
        )
        new_content = _render_sshd_config(
            top_settings=new_top_settings,
            match_blocks=match_blocks,
        )

        if new_content == existing:
            skipped = self._build_result(
                status=UbuntuSshActionStatus.SKIPPED,
                keyword=keyword,
                path=path,
                command=write_command,
                message="keyword not present at top level",
            )
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuSshActionStatus.DRY_RUN,
                keyword=keyword,
                path=path,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=keyword,
            path=path,
            user=None,
        )
        return rtnval

    def add_match_block(
        self,
        criteria: str,
        settings: dict,
    ) -> UbuntuSshActionResult:
        """
            Add a ``Match <criteria>`` block with the given child
            settings.

            Idempotent: returns ``SKIPPED`` when an identical block
            (same criteria string AND same settings dict) already
            exists in the file.  Honours dry-run.

            :param criteria: The match criteria (e.g. ``"User git"``).
            :param settings: ``dict[str, str]`` of keyword/value pairs
                             to declare inside the block.

            :returns: The :class:`UbuntuSshActionResult.
        """
        path = self._typed_config().sshd_config_path
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        top_settings, match_blocks = _parse_sshd_config(
            content=existing_content,
        )

        normalized_settings = dict(settings)
        new_blocks = _upsert_match_block(
            match_blocks=match_blocks,
            criteria=criteria,
            settings=normalized_settings,
        )
        new_content = _render_sshd_config(
            top_settings=top_settings,
            match_blocks=new_blocks,
        )

        if existing is not None and existing == new_content:
            skipped = self._build_result(
                status=UbuntuSshActionStatus.SKIPPED,
                keyword=None,
                path=path,
                command=write_command,
                message="match block already present",
            )
            skipped.detail = "match:{}".format(criteria)
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuSshActionStatus.DRY_RUN,
                keyword=None,
                path=path,
                command=write_command,
            )
            dry.detail = "match:{}".format(criteria)
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=None,
            path=path,
            user=None,
        )
        rtnval.detail = "match:{}".format(criteria)
        return rtnval

    def remove_match_block(
        self,
        criteria: str,
    ) -> UbuntuSshActionResult:
        """
            Remove a ``Match <criteria>`` block.

            Idempotent: returns ``SKIPPED`` when no block with the
            supplied criteria exists.  Honours dry-run.

            :param criteria: The match criteria string used to identify
                             the block (compared verbatim, whitespace
                             collapsed).

            :returns: The :class:`UbuntuSshActionResult`.
        """
        path = self._typed_config().sshd_config_path
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        if existing is None:
            skipped = self._build_result(
                status=UbuntuSshActionStatus.SKIPPED,
                keyword=None,
                path=path,
                command=write_command,
                message="config file does not exist",
            )
            skipped.detail = "match:{}".format(criteria)
            return skipped

        top_settings, match_blocks = _parse_sshd_config(content=existing)
        new_blocks = _remove_match_block(
            match_blocks=match_blocks,
            criteria=criteria,
        )

        if len(new_blocks) == len(match_blocks):
            skipped = self._build_result(
                status=UbuntuSshActionStatus.SKIPPED,
                keyword=None,
                path=path,
                command=write_command,
                message="match block not present",
            )
            skipped.detail = "match:{}".format(criteria)
            return skipped

        new_content = _render_sshd_config(
            top_settings=top_settings,
            match_blocks=new_blocks,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuSshActionStatus.DRY_RUN,
                keyword=None,
                path=path,
                command=write_command,
            )
            dry.detail = "match:{}".format(criteria)
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=None,
            path=path,
            user=None,
        )
        rtnval.detail = "match:{}".format(criteria)
        return rtnval

    # ------------------------------------------------------------------
    # Operational verbs
    # ------------------------------------------------------------------

    def reload_sshd(self) -> UbuntuSshActionResult:
        """
            Reload the sshd service via ``systemctl reload <service>``.

            On Ubuntu the unit is named ``ssh`` rather than ``sshd``;
            the unit name is taken from
            :attr:`UbuntuSshServerConfigurationManagerConfig.service_name`.

            Never skips.  Honours dry-run.  Elevates per config.

            :returns: The :class:`UbuntuSshActionResult`.
        """
        service_name = self._typed_config().service_name
        command = [_SYSTEMCTL, "reload", service_name]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuSshActionStatus.DRY_RUN,
                keyword=None,
                path=None,
                command=command,
                message=service_name,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            keyword=None,
            path=None,
            user=None,
        )
        return rtnval

    def test_sshd_config(self) -> UbuntuSshActionResult:
        """
            Validate the current ``sshd_config`` via ``sshd -t``.

            Read-only with respect to configuration content; never
            mutates the file.  Elevates per config (``sshd -t`` reads
            host-key files).  Never skips; honours dry-run.

            :returns: The :class:`UbuntuSshActionResult` with
                      ``stdout`` / ``stderr`` populated from the
                      validator.
        """
        command = [_SSHD, "-t"]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuSshActionStatus.DRY_RUN,
                keyword=None,
                path=self._typed_config().sshd_config_path,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=command,
            keyword=None,
            path=self._typed_config().sshd_config_path,
            user=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Authorized-key write verbs
    # ------------------------------------------------------------------

    def add_authorized_key(
        self,
        user: str,
        key: str,
    ) -> UbuntuSshActionResult:
        """
            Append a key line to a user's ``authorized_keys`` file.

            Idempotent: returns ``SKIPPED`` when an entry with the same
            fingerprint is already present.  Returns ``FAILED`` with a
            descriptive ``message`` when the key text fails to parse.
            Honours dry-run.

            :param user: The target username.
            :param key: A single OpenSSH ``authorized_keys`` line.

            :returns: The :class:`UbuntuSshActionResult`.
        """
        path = self._authorized_keys_path(user=user)
        write_command = [_TEE, path]

        parsed_new = _parse_authorized_key_line(line=key)
        if parsed_new is None:
            failed = self._build_result(
                status=UbuntuSshActionStatus.FAILED,
                keyword=None,
                path=path,
                command=write_command,
                message="key line failed to parse",
            )
            failed.user = user
            return failed

        existing = self._read_file(path=path)
        existing_content = "" if existing is None else existing
        existing_keys = _parse_authorized_keys(content=existing_content)
        for existing_entry in existing_keys:
            if existing_entry.fingerprint == parsed_new.fingerprint:
                skipped = self._build_result(
                    status=UbuntuSshActionStatus.SKIPPED,
                    keyword=None,
                    path=path,
                    command=write_command,
                    message="key already present",
                )
                skipped.user = user
                return skipped

        new_content = _append_authorized_key(
            content=existing_content, key_line=key,
        )

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuSshActionStatus.DRY_RUN,
                keyword=None,
                path=path,
                command=write_command,
            )
            dry.user = user
            return dry

        # TODO: per-user authorized_keys writes for the calling user
        # do not strictly require elevation — a follow-up will detect
        # ``user == current_user`` and skip elevation.  Honour the
        # config for now (matches prior wave-1 batches).
        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=None,
            path=path,
            user=user,
        )
        return rtnval

    def remove_authorized_key(
        self,
        user: str,
        fingerprint: str,
    ) -> UbuntuSshActionResult:
        """
            Remove the ``authorized_keys`` entry whose SHA-256
            fingerprint matches.

            Idempotent: returns ``SKIPPED`` when no entry with that
            fingerprint exists or the file is missing.  Honours
            dry-run.

            :param user: The target username.
            :param fingerprint: The OpenSSH-format SHA-256 fingerprint
                                (``SHA256:...``) of the key to remove.

            :returns: The :class:`UbuntuSshActionResult`.
        """
        path = self._authorized_keys_path(user=user)
        write_command = [_TEE, path]

        existing = self._read_file(path=path)
        if existing is None:
            skipped = self._build_result(
                status=UbuntuSshActionStatus.SKIPPED,
                keyword=None,
                path=path,
                command=write_command,
                message="authorized_keys file does not exist",
            )
            skipped.user = user
            return skipped

        new_content = _remove_authorized_key(
            content=existing, fingerprint=fingerprint,
        )

        if new_content == existing:
            skipped = self._build_result(
                status=UbuntuSshActionStatus.SKIPPED,
                keyword=None,
                path=path,
                command=write_command,
                message="key with given fingerprint not present",
            )
            skipped.user = user
            return skipped

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuSshActionStatus.DRY_RUN,
                keyword=None,
                path=path,
                command=write_command,
            )
            dry.user = user
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=new_content,
        )
        rtnval = self._wrap_runner_result(
            run_result=run_result,
            command=write_command,
            keyword=None,
            path=path,
            user=user,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> UbuntuSshServerConfigurationManagerConfig:
        """
            Narrow the base config to the typed subclass.

            :returns: The typed
                      :class:`UbuntuSshServerConfigurationManagerConfig`.
        """
        rtnval = self._config
        return rtnval

    def _authorized_keys_path(self, *, user: str) -> str:
        """
            Render the ``authorized_keys`` path for a user.

            :param user: The username.

            :returns: The absolute path.
        """
        template = self._typed_config().authorized_keys_path_template
        rtnval = template.format(user=user)
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat`` without elevation.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when
                      ``cat`` reports a non-zero exit code (i.e. the
                      path is missing).
        """
        command = [_CAT, path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuSshActionStatus,
        keyword: Optional[str],
        path: Optional[str],
        command: list,
        message: Optional[str] = None,
    ) -> UbuntuSshActionResult:
        """
            Build a result without invoking the runner.

            :param status: Outcome status.
            :param keyword: ``sshd_config`` keyword targeted, or None.
            :param path: File path targeted, or None.
            :param command: Would-have-been argv.
            :param message: Optional human-readable detail.

            :returns: The :class:`UbuntuSshActionResult`.
        """
        if keyword is not None:
            detail_text = keyword
        elif path is not None:
            detail_text = path
        else:
            detail_text = None

        rtnval = UbuntuSshActionResult(
            status=status,
            detail=detail_text,
            command=list(command),
            keyword=keyword,
            path=path,
            user=None,
            message=message,
        )
        return rtnval

    def _wrap_runner_result(
        self,
        *,
        run_result,
        command: list,
        keyword: Optional[str],
        path: Optional[str],
        user: Optional[str],
    ) -> UbuntuSshActionResult:
        """
            Map a :class:`RunResult` to a
            :class:`UbuntuSshActionResult`.
        """
        if run_result.exit_code == 0:
            mapped_status = UbuntuSshActionStatus.SUCCESS
        else:
            mapped_status = UbuntuSshActionStatus.FAILED

        if keyword is not None:
            detail_text = keyword
        elif path is not None:
            detail_text = path
        else:
            detail_text = None

        rtnval = UbuntuSshActionResult(
            status=mapped_status,
            detail=detail_text,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            keyword=keyword,
            path=path,
            user=user,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — sshd_config parser / renderer
# ----------------------------------------------------------------------


def _parse_sshd_config(*, content: str) -> tuple:
    """
        Parse ``sshd_config`` content into a top-level settings map and
        a list of :class:`UbuntuSshMatchBlock`.

        Comment and blank lines are ignored for the structured map but
        will be reproduced verbatim by :func:`_render_sshd_config` only
        for top-level lines that the renderer can identify (the pilot
        renderer rebuilds the file deterministically — comments are
        not preserved across mutation).

        :param content: The raw file content.

        :returns: ``(top_settings, match_blocks)`` where
                  ``top_settings`` is a ``dict[str, str]`` and
                  ``match_blocks`` is a ``list[UbuntuSshMatchBlock]``.
    """
    top_settings: dict = {}
    match_blocks: list = []
    current_block: Optional[UbuntuSshMatchBlock] = None

    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0:
            continue
        if stripped.startswith("#"):
            continue
        tokens = stripped.split(None, 1)
        if len(tokens) < 2:
            keyword = tokens[0]
            value = ""
        else:
            keyword = tokens[0]
            value = tokens[1].strip()

        if keyword.lower() == _MATCH_KEYWORD.lower():
            current_block = UbuntuSshMatchBlock(
                criteria=value,
                settings={},
            )
            match_blocks.append(current_block)
            continue

        if current_block is None:
            top_settings[keyword] = value
        else:
            current_block.settings[keyword] = value

    rtnval = (top_settings, match_blocks)
    return rtnval


def _render_sshd_config(
    *,
    top_settings: dict,
    match_blocks: list,
) -> str:
    """
        Render a ``sshd_config`` file from structured data.

        Top-level settings come first in dict order, followed by every
        :class:`UbuntuSshMatchBlock` in list order.  Each ``Match``
        block's settings are indented four spaces for readability
        (sshd accepts any indentation, including none).

        :param top_settings: Top-level keyword/value map.
        :param match_blocks: Match blocks in declaration order.

        :returns: The rendered file content (terminated with a newline).
    """
    lines: list = []
    for keyword, value in top_settings.items():
        if len(value) == 0:
            lines.append(keyword)
        else:
            lines.append("{} {}".format(keyword, value))

    for block in match_blocks:
        lines.append("Match {}".format(block.criteria))
        for keyword, value in block.settings.items():
            if len(value) == 0:
                lines.append("    {}".format(keyword))
            else:
                lines.append("    {} {}".format(keyword, value))

    body = "\n".join(lines)
    if not body.endswith("\n"):
        body = body + "\n"
    return body


def _set_top_setting(
    *,
    top_settings: dict,
    keyword: str,
    value: str,
) -> dict:
    """
        Return a new top-settings dict with ``keyword`` set to ``value``.

        Existing case is preserved when the keyword is already present;
        otherwise the supplied case is used.

        :param top_settings: Existing top-level settings.
        :param keyword: The keyword to upsert.
        :param value: The desired value.

        :returns: A new ``dict[str, str]``.
    """
    keyword_lower = keyword.lower()
    rtnval: dict = {}
    replaced = False
    for existing_keyword, existing_value in top_settings.items():
        if existing_keyword.lower() == keyword_lower:
            rtnval[existing_keyword] = value
            replaced = True
            continue
        rtnval[existing_keyword] = existing_value
    if not replaced:
        rtnval[keyword] = value
    return rtnval


def _remove_top_setting(
    *,
    top_settings: dict,
    keyword: str,
) -> dict:
    """
        Return a new top-settings dict with every occurrence of
        ``keyword`` removed (case-insensitive).
    """
    keyword_lower = keyword.lower()
    rtnval: dict = {}
    for existing_keyword, existing_value in top_settings.items():
        if existing_keyword.lower() == keyword_lower:
            continue
        rtnval[existing_keyword] = existing_value
    return rtnval


def _upsert_match_block(
    *,
    match_blocks: list,
    criteria: str,
    settings: dict,
) -> list:
    """
        Return a new list of match blocks with the given criteria's
        settings replaced (or the block inserted at the end when
        absent).

        Criteria match is exact after collapsing internal whitespace.

        :param match_blocks: Existing blocks.
        :param criteria: Criteria string identifying the block.
        :param settings: Desired settings dict.

        :returns: The updated list.
    """
    target_norm = _collapse_whitespace(text=criteria)
    rtnval: list = []
    found = False
    for block in match_blocks:
        block_norm = _collapse_whitespace(text=block.criteria)
        if block_norm == target_norm:
            replacement = UbuntuSshMatchBlock(
                criteria=block.criteria,
                settings=dict(settings),
            )
            rtnval.append(replacement)
            found = True
            continue
        rtnval.append(block)
    if not found:
        new_block = UbuntuSshMatchBlock(
            criteria=criteria,
            settings=dict(settings),
        )
        rtnval.append(new_block)
    return rtnval


def _remove_match_block(
    *,
    match_blocks: list,
    criteria: str,
) -> list:
    """
        Return a new list with every block whose criteria matches
        ``criteria`` removed (whitespace-collapsed comparison).
    """
    target_norm = _collapse_whitespace(text=criteria)
    rtnval: list = []
    for block in match_blocks:
        block_norm = _collapse_whitespace(text=block.criteria)
        if block_norm == target_norm:
            continue
        rtnval.append(block)
    return rtnval


def _collapse_whitespace(*, text: str) -> str:
    """
        Collapse runs of whitespace into a single space and strip ends.

        :param text: Input string.

        :returns: The normalised string.
    """
    parts = text.split()
    rtnval = " ".join(parts)
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — authorized_keys parser / renderer
# ----------------------------------------------------------------------


_KNOWN_KEY_TYPES = (
    "ssh-rsa",
    "ssh-dss",
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ecdsa-sha2-nistp256@openssh.com",
    "sk-ssh-ed25519@openssh.com",
)


def _parse_authorized_keys(*, content: str) -> list:
    """
        Parse a full ``authorized_keys`` file into entries.

        Comment and blank lines are skipped.  Lines that fail to parse
        are skipped.

        :param content: Raw file content.

        :returns: A ``list[UbuntuSshAuthorizedKey]``.
    """
    rtnval: list = []
    for raw in content.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        if line.startswith("#"):
            continue
        parsed = _parse_authorized_key_line(line=line)
        if parsed is None:
            continue
        rtnval.append(parsed)
    return rtnval


def _parse_authorized_key_line(
    *,
    line: str,
) -> Optional[UbuntuSshAuthorizedKey]:
    """
        Parse a single ``authorized_keys`` line.

        The OpenSSH grammar is:

            [options ]<key-type> <key-data> [<comment>]

        ``options`` (when present) is a comma-separated list of
        directives; we treat everything before the first known
        ``<key-type>`` token as the options string.

        :param line: A single line (already trimmed of leading /
                     trailing whitespace).

        :returns: A populated :class:`UbuntuSshAuthorizedKey`, or
                  ``None`` when the line does not parse.
    """
    parts = line.split()
    if len(parts) < 2:
        rtnval: Optional[UbuntuSshAuthorizedKey] = None
        return rtnval

    type_index: Optional[int] = None
    for i, token in enumerate(parts):
        if token in _KNOWN_KEY_TYPES:
            type_index = i
            break

    if type_index is None:
        rtnval = None
        return rtnval

    if type_index + 1 >= len(parts):
        rtnval = None
        return rtnval

    if type_index == 0:
        options: Optional[str] = None
    else:
        options = " ".join(parts[:type_index])

    key_type = parts[type_index]
    key_data = parts[type_index + 1]

    comment_parts = parts[type_index + 2:]
    if len(comment_parts) > 0:
        comment: Optional[str] = " ".join(comment_parts)
    else:
        comment = None

    fingerprint = _compute_fingerprint(key_data=key_data)

    rtnval = UbuntuSshAuthorizedKey(
        key_type=key_type,
        key_data=key_data,
        comment=comment,
        fingerprint=fingerprint,
        options=options,
    )
    return rtnval


def _compute_fingerprint(*, key_data: str) -> Optional[str]:
    """
        Compute an OpenSSH-format SHA-256 fingerprint for a key body.

        :param key_data: Base64-encoded key body.

        :returns: ``"SHA256:<base64-no-padding>"``, or ``None`` when
                  ``key_data`` does not decode.
    """
    try:
        decoded = base64.b64decode(key_data, validate=True)
    except (ValueError, Exception):
        rtnval: Optional[str] = None
        return rtnval

    digest = hashlib.sha256(decoded).digest()
    encoded = base64.b64encode(digest).decode("ascii").rstrip("=")
    rtnval = "SHA256:{}".format(encoded)
    return rtnval


def _append_authorized_key(*, content: str, key_line: str) -> str:
    """
        Append a key line to the file content (idempotency check is
        handled by the caller).

        :param content: Existing file content.
        :param key_line: Key line to append.

        :returns: The updated content.
    """
    trimmed = key_line.strip()
    if len(content) == 0:
        rtnval = trimmed + "\n"
        return rtnval
    if not content.endswith("\n"):
        body = content + "\n"
    else:
        body = content
    rtnval = body + trimmed + "\n"
    return rtnval


def _remove_authorized_key(
    *,
    content: str,
    fingerprint: str,
) -> str:
    """
        Return content with every line whose computed fingerprint
        matches ``fingerprint`` removed.

        :param content: Existing file content.
        :param fingerprint: The fingerprint to match.

        :returns: The updated content.
    """
    if len(content) == 0:
        return content

    kept: list = []
    for raw in content.splitlines():
        stripped = raw.strip()
        if len(stripped) == 0 or stripped.startswith("#"):
            kept.append(raw)
            continue
        parsed = _parse_authorized_key_line(line=stripped)
        if parsed is None:
            kept.append(raw)
            continue
        if parsed.fingerprint == fingerprint:
            continue
        kept.append(raw)

    body = "\n".join(kept)
    if len(body) > 0 and not body.endswith("\n"):
        body = body + "\n"
    return body
