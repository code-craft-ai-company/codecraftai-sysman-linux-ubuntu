"""
    Ubuntu certificate-trust-management feature.

    Implements the cross-OS certificate-trust-management contract for
    Ubuntu by managing CA certificate files under
    ``/usr/local/share/ca-certificates/`` and refreshing the
    consolidated bundle through ``update-ca-certificates``.  Every verb
    is dispatched through the injected :class:`ICommandRunner` so the
    manager works identically under local and SSH execution.

    Verb / tool mapping:

    +-----------------------------+-----------------------------------+
    | Verb                        | Ubuntu tool                       |
    +=============================+===================================+
    | ``add_trusted_cert``        | ``cat`` to read existing files +  |
    |                             | ``openssl x509`` to validate /    |
    |                             | fingerprint + ``tee`` to write    |
    |                             | the destination ``.crt``          |
    +-----------------------------+-----------------------------------+
    | ``remove_trusted_cert``     | ``ls`` + ``cat`` + ``openssl`` to |
    |                             | locate by fingerprint + ``rm``    |
    +-----------------------------+-----------------------------------+
    | ``list_trusted_certs``      | ``ls`` + ``cat`` + ``openssl``    |
    +-----------------------------+-----------------------------------+
    | ``get_cert_details``        | ``ls`` + ``cat`` + ``openssl``    |
    |                             | parse                             |
    +-----------------------------+-----------------------------------+
    | ``update_trust_store``      | ``update-ca-certificates``        |
    +-----------------------------+-----------------------------------+

    File I/O policy:
        Reads use ``cat`` and ``ls``; writes use ``tee`` with the new
        content on stdin.  This module does NOT consume an
        :class:`IFileOperator`.

    Trust-store mapping:
        - ``"system-ca"`` resolves to
          ``/usr/local/share/ca-certificates/`` (the location that
          ``update-ca-certificates`` consumes for locally-added
          anchors).  Files written here MUST end in ``.crt`` for
          Debian's ``update-ca-certificates`` to pick them up.
        - ``"user-ca"`` is NOT supported on Ubuntu.  The Debian CA
          tooling does not implement a per-user trust bundle.  Calls
          targeting ``"user-ca"`` return ``FAILED`` with a descriptive
          ``detail``.

    Idempotency model:
        Every store operation reads the destination directory via
        ``ls``, parses each file's fingerprint via ``openssl x509
        -fingerprint -sha256 -noout``, and compares to the supplied
        fingerprint.  ``add_trusted_cert`` returns ``SKIPPED`` when a
        file with the matching fingerprint is already present;
        ``remove_trusted_cert`` returns ``SKIPPED`` when no file with
        that fingerprint exists.  ``update_trust_store`` is operational
        and never skips.

    Fingerprint format:
        Linux convention (matching ``openssl x509 -sha256
        -fingerprint``): uppercase hex bytes joined by colons (e.g.
        ``"AB:CD:EF:..."``).  An auxiliary helper accepts the no-colon
        form and normalises both directions; matching is
        case-insensitive and colon-insensitive.

    Elevation policy:
        Writes against ``"system-ca"`` honour
        ``config.elevated`` and pass it through to the runner.
        ``"user-ca"`` does not elevate (it returns ``FAILED`` before
        dispatch anyway).  Reads do not elevate.

    PEM validation:
        Source files supplied to :meth:`add_trusted_cert` MUST parse as
        a PEM certificate via ``openssl x509 -noout -in <path>``.
        Validation failure short-circuits the write path and returns
        ``FAILED`` with the captured stderr in ``detail``.
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
_LS = "ls"
_RM = "rm"
_OPENSSL = "openssl"
_UPDATE_CA_CERTIFICATES = "update-ca-certificates"

_SYSTEM_CA_STORE = "system-ca"
_USER_CA_STORE = "user-ca"

_DEFAULT_SYSTEM_CA_DIR = "/usr/local/share/ca-certificates"

_PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
_PEM_END = "-----END CERTIFICATE-----"


UbuntuCertTrustActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class UbuntuCertTrustManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuCertificateTrustManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param store_path_map: Optional override mapping of abstract
                               store name to filesystem directory.  When
                               ``None`` the bundled defaults are used
                               (``"system-ca"`` ->
                               ``/usr/local/share/ca-certificates``).
        :param default_store: The abstract store name used by
                              ``add_trusted_cert`` /
                              ``remove_trusted_cert`` /
                              ``list_trusted_certs`` when callers do not
                              supply an explicit store argument.
                              Defaults to ``"system-ca"``.
        :param openssl_executable: Path to the ``openssl`` executable.
                                   Defaults to ``"openssl"`` (resolved
                                   through the runner's PATH).
        :param update_executable: Path to ``update-ca-certificates``.
                                  Defaults to
                                  ``"update-ca-certificates"``.
    """
    store_path_map: Optional[dict] = None
    default_store: str = _SYSTEM_CA_STORE
    openssl_executable: str = _OPENSSL
    update_executable: str = _UPDATE_CA_CERTIFICATES


@dataclass
class UbuntuCertTrustActionResult(FeatureActionResult):
    """
        Result of a certificate-trust-management write operation.

        :param store: The abstract store name targeted.
        :param fingerprint: The SHA-256 fingerprint of the affected
                            certificate (Linux uppercase-with-colons
                            form), or ``None`` when not applicable.
        :param path: The on-disk path the action targeted, when
                     applicable.
    """
    store: Optional[str] = None
    fingerprint: Optional[str] = None
    path: Optional[str] = None


@dataclass
class UbuntuCertTrustInfo:
    """
        Structured metadata for a single trusted certificate.

        :param fingerprint: SHA-256 fingerprint in the Linux native
                            uppercase-with-colons form.
        :param subject: Subject DN as emitted by
                        ``openssl x509 -subject``.
        :param issuer: Issuer DN as emitted by
                       ``openssl x509 -issuer``.
        :param not_before: Validity start in the format emitted by
                           ``openssl x509 -startdate``.
        :param not_after: Validity end in the format emitted by
                          ``openssl x509 -enddate``.
        :param serial_number: Hex serial number.
        :param store: The abstract store the certificate was found in.
        :param path: The on-disk path of the certificate file, or
                     ``None`` when the source is the consolidated
                     bundle.
        :param purposes: Trust purposes extracted from the certificate
                         text, when known (e.g.
                         ``["TLS Web Server Authentication"]``).
                         Empty when the certificate text does not
                         expose them.
    """
    fingerprint: str
    subject: str
    issuer: str
    not_before: str
    not_after: str
    serial_number: str
    store: str
    path: Optional[str] = None
    purposes: list = field(default_factory=list)


class UbuntuCertTrustQueryError(Exception):
    """
        Raised when a certificate-trust-management read fails for a
        reason other than the target source simply being absent.
    """

    def __init__(
        self,
        *,
        operation: str,
        message: str,
    ) -> None:
        """
            :param operation: The verb that triggered the failure (e.g.
                              ``"list_trusted_certs"``).
            :param message: Human-readable detail.
        """
        self.operation = operation
        self.message = message
        err_msg_lines = ["Certificate trust query command failed."]
        err_msg_lines.append("    OPERATION: {}".format(operation))
        err_msg_lines.append("    DETAIL: {}".format(message))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class UbuntuCertificateTrustManager(BaseFeatureManager):
    """
        Ubuntu certificate-trust-management manager.  Wraps the Debian
        CA-trust toolchain (``update-ca-certificates``) plus
        ``openssl`` parsing and ``tee`` / ``rm`` file ops through the
        injected :class:`ICommandRunner`.
    """

    result_class = UbuntuCertTrustActionResult
    status_class = UbuntuCertTrustActionStatus
    feature_name = "certificate-trust-management"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def add_trusted_cert(
        self,
        store: str,
        path: str,
        purpose: Optional[str] = None,
    ) -> UbuntuCertTrustActionResult:
        """
            Install a PEM certificate into a trust store and report the
            outcome.

            The destination filename is derived from the basename of
            ``path``; if it does not already end in ``.crt`` the
            extension is appended (Debian's
            ``update-ca-certificates`` only enumerates files whose
            names end in ``.crt``).

            :param store: The abstract store name (e.g.
                          ``"system-ca"``).
            :param path: Source PEM file path.
            :param purpose: Optional purpose hint recorded on the
                            result; not used by the underlying tool.

            :returns: The :class:`UbuntuCertTrustActionResult`.
        """
        store_dir = self._resolve_store_dir(store=store)
        if store_dir is None:
            failed = self._build_result(
                status=UbuntuCertTrustActionStatus.FAILED,
                store=store,
                fingerprint=None,
                path=path,
                command=None,
                detail="store '{}' is not supported on Ubuntu".format(store),
            )
            return failed

        # Read source content via cat so the runner abstraction is the
        # only path through which I/O happens.
        source_content = self._read_file(path=path)
        if source_content is None:
            failed = self._build_result(
                status=UbuntuCertTrustActionStatus.FAILED,
                store=store,
                fingerprint=None,
                path=path,
                command=[_CAT, path],
                detail="source path does not exist",
            )
            return failed

        # Validate the PEM structure entirely in-process before doing
        # any further runner work.  If the bytes do not contain a
        # CERTIFICATE block we refuse to write.
        cert_blocks = _parse_pem_blocks(content=source_content)
        if len(cert_blocks) == 0:
            failed = self._build_result(
                status=UbuntuCertTrustActionStatus.FAILED,
                store=store,
                fingerprint=None,
                path=path,
                command=None,
                detail="source file is not a valid PEM certificate",
            )
            return failed

        first_pem = cert_blocks[0]
        fingerprint = _compute_fingerprint(
            pem=first_pem,
            colonized=True,
            uppercase=True,
        )
        if fingerprint is None:
            failed = self._build_result(
                status=UbuntuCertTrustActionStatus.FAILED,
                store=store,
                fingerprint=None,
                path=path,
                command=None,
                detail="failed to compute fingerprint from PEM body",
            )
            return failed

        # Idempotency: if any existing file in the store already has
        # the same fingerprint, skip.
        existing_match = self._find_by_fingerprint(
            store_dir=store_dir,
            fingerprint=fingerprint,
        )
        if existing_match is not None:
            skipped = self._build_result(
                status=UbuntuCertTrustActionStatus.SKIPPED,
                store=store,
                fingerprint=fingerprint,
                path=existing_match,
                command=None,
                detail="certificate with same fingerprint already trusted",
            )
            return skipped

        dest_filename = _normalize_destination_filename(source_path=path)
        dest_path = "{}/{}".format(store_dir, dest_filename)
        write_command = [_TEE, dest_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuCertTrustActionStatus.DRY_RUN,
                store=store,
                fingerprint=fingerprint,
                path=dest_path,
                command=write_command,
            )
            return dry

        elevated = self._elevation_for_store(store=store)
        run_result = self._runner.run(
            list(write_command),
            elevated=elevated,
            stdin=source_content,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuCertTrustActionStatus.SUCCESS
        else:
            mapped_status = UbuntuCertTrustActionStatus.FAILED

        rtnval = UbuntuCertTrustActionResult(
            status=mapped_status,
            detail=purpose,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            store=store,
            fingerprint=fingerprint,
            path=dest_path,
        )
        return rtnval

    def remove_trusted_cert(
        self,
        store: str,
        fingerprint: str,
    ) -> UbuntuCertTrustActionResult:
        """
            Remove the certificate file in the store whose fingerprint
            matches.

            :param store: The abstract store name.
            :param fingerprint: The SHA-256 fingerprint to remove.
                                Accepted in colonised or non-colonised
                                form (case-insensitive).

            :returns: The :class:`UbuntuCertTrustActionResult`.
        """
        store_dir = self._resolve_store_dir(store=store)
        if store_dir is None:
            failed = self._build_result(
                status=UbuntuCertTrustActionStatus.FAILED,
                store=store,
                fingerprint=fingerprint,
                path=None,
                command=None,
                detail="store '{}' is not supported on Ubuntu".format(store),
            )
            return failed

        normalized = _normalize_fingerprint(value=fingerprint)
        target_path = self._find_by_fingerprint(
            store_dir=store_dir,
            fingerprint=normalized,
        )
        if target_path is None:
            skipped = self._build_result(
                status=UbuntuCertTrustActionStatus.SKIPPED,
                store=store,
                fingerprint=normalized,
                path=None,
                command=None,
                detail="no certificate with that fingerprint in store",
            )
            return skipped

        delete_command = [_RM, "-f", target_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuCertTrustActionStatus.DRY_RUN,
                store=store,
                fingerprint=normalized,
                path=target_path,
                command=delete_command,
            )
            return dry

        elevated = self._elevation_for_store(store=store)
        run_result = self._runner.run(
            list(delete_command),
            elevated=elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuCertTrustActionStatus.SUCCESS
        else:
            mapped_status = UbuntuCertTrustActionStatus.FAILED

        rtnval = UbuntuCertTrustActionResult(
            status=mapped_status,
            detail=target_path,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(delete_command),
            store=store,
            fingerprint=normalized,
            path=target_path,
        )
        return rtnval

    def update_trust_store(self) -> UbuntuCertTrustActionResult:
        """
            Refresh the consolidated trust bundle via
            ``update-ca-certificates``.

            Never skips.  Honours dry-run.  Honours
            ``config.elevated``.

            :returns: The :class:`UbuntuCertTrustActionResult`.
        """
        executable = self._typed_config().update_executable
        command = [executable]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuCertTrustActionStatus.DRY_RUN,
                store=None,
                fingerprint=None,
                path=None,
                command=command,
            )
            return dry

        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuCertTrustActionStatus.SUCCESS
        else:
            mapped_status = UbuntuCertTrustActionStatus.FAILED

        rtnval = UbuntuCertTrustActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            store=None,
            fingerprint=None,
            path=None,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def list_trusted_certs(
        self,
        store: str,
    ) -> list:
        """
            Enumerate every trusted certificate in the named store.

            Reads each ``.crt`` file in the store directory via ``cat``
            and parses metadata via ``openssl x509`` (no elevation).

            :param store: The abstract store name.

            :returns: A ``list[UbuntuCertTrustInfo]``.

            :raises UbuntuCertTrustQueryError: When the store name is
                                                not supported.
        """
        store_dir = self._resolve_store_dir(store=store)
        if store_dir is None:
            raise UbuntuCertTrustQueryError(
                operation="list_trusted_certs",
                message="store '{}' is not supported on Ubuntu".format(store),
            )

        rtnval: list = []
        entries = self._list_dir(path=store_dir)
        for entry in entries:
            if not (entry.endswith(".crt") or entry.endswith(".pem")):
                continue
            full_path = "{}/{}".format(store_dir, entry)
            info = self._read_cert_info(
                store=store,
                path=full_path,
            )
            if info is not None:
                rtnval.append(info)
        return rtnval

    def get_cert_details(
        self,
        fingerprint: str,
    ) -> Optional[UbuntuCertTrustInfo]:
        """
            Return structured metadata for the certificate whose
            fingerprint matches.  Searches every configured store.

            :param fingerprint: The SHA-256 fingerprint to look up.
                                Accepted in colonised or non-colonised
                                form (case-insensitive).

            :returns: The :class:`UbuntuCertTrustInfo`, or ``None`` when
                      no certificate with that fingerprint is found.
        """
        normalized = _normalize_fingerprint(value=fingerprint)
        store_map = self._effective_store_map()
        for store_name, store_dir in store_map.items():
            if store_dir is None:
                continue
            entries = self._list_dir(path=store_dir)
            for entry in entries:
                if not (entry.endswith(".crt") or entry.endswith(".pem")):
                    continue
                full_path = "{}/{}".format(store_dir, entry)
                info = self._read_cert_info(
                    store=store_name,
                    path=full_path,
                )
                if info is None:
                    continue
                if info.fingerprint == normalized:
                    rtnval: Optional[UbuntuCertTrustInfo] = info
                    return rtnval
        rtnval = None
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> UbuntuCertTrustManagerConfig:
        """
            Narrow the base config to the typed subclass.

            :returns: The typed
                      :class:`UbuntuCertTrustManagerConfig`.
        """
        rtnval = self._config
        return rtnval

    def _effective_store_map(self) -> dict:
        """
            Build the effective store-name to directory mapping.

            Defaults are merged with any caller-supplied overrides.
            The Ubuntu default exposes ``"system-ca"`` only;
            ``"user-ca"`` is mapped to ``None`` (meaning "unsupported,
            dispatch returns FAILED").

            :returns: ``dict[str, str | None]``.
        """
        defaults: dict = {
            _SYSTEM_CA_STORE: _DEFAULT_SYSTEM_CA_DIR,
            _USER_CA_STORE: None,
        }
        override = self._typed_config().store_path_map
        if override is None:
            rtnval = defaults
            return rtnval
        merged = dict(defaults)
        for key, value in override.items():
            merged[key] = value
        rtnval = merged
        return rtnval

    def _resolve_store_dir(self, *, store: str) -> Optional[str]:
        """
            Resolve a store name to its on-disk directory.

            :param store: The abstract store name.

            :returns: The directory path, or ``None`` when the store is
                      not supported on this OS.
        """
        store_map = self._effective_store_map()
        if store not in store_map:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = store_map[store]
        return rtnval

    def _elevation_for_store(self, *, store: str) -> bool:
        """
            Decide whether to pass ``elevated=True`` for a write
            against the given store.

            ``"system-ca"`` honours ``config.elevated``; ``"user-ca"``
            never elevates.  Other stores follow the config default.

            :param store: The abstract store name.

            :returns: The effective elevation flag.
        """
        if store == _USER_CA_STORE:
            rtnval = False
            return rtnval
        rtnval = self._config.elevated
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat`` without elevation.

            :param path: The absolute path.

            :returns: The contents on success, ``None`` when ``cat``
                      reports a non-zero exit code (i.e. the file is
                      missing).
        """
        command = [_CAT, path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _list_dir(self, *, path: str) -> list:
        """
            Enumerate a directory's entries via ``ls -1`` (no
            elevation).

            :param path: The directory path.

            :returns: A list of filenames.  Empty when the directory
                      does not exist.
        """
        command = [_LS, "-1", path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval
        rtnval = []
        for raw in run_result.stdout.splitlines():
            entry = raw.strip()
            if len(entry) == 0:
                continue
            rtnval.append(entry)
        return rtnval

    def _find_by_fingerprint(
        self,
        *,
        store_dir: str,
        fingerprint: str,
    ) -> Optional[str]:
        """
            Walk a store directory and return the path of the first
            certificate file whose fingerprint matches.

            :param store_dir: The directory to walk.
            :param fingerprint: The fingerprint to look up
                                (uppercase-with-colons).

            :returns: The absolute path, or ``None`` when no match.
        """
        normalized = _normalize_fingerprint(value=fingerprint)
        entries = self._list_dir(path=store_dir)
        for entry in entries:
            if not (entry.endswith(".crt") or entry.endswith(".pem")):
                continue
            full_path = "{}/{}".format(store_dir, entry)
            content = self._read_file(path=full_path)
            if content is None:
                continue
            blocks = _parse_pem_blocks(content=content)
            if len(blocks) == 0:
                continue
            file_fp = _compute_fingerprint(
                pem=blocks[0],
                colonized=True,
                uppercase=True,
            )
            if file_fp is None:
                continue
            if file_fp == normalized:
                rtnval: Optional[str] = full_path
                return rtnval
        rtnval = None
        return rtnval

    def _read_cert_info(
        self,
        *,
        store: str,
        path: str,
    ) -> Optional[UbuntuCertTrustInfo]:
        """
            Read a certificate file from disk and produce an
            :class:`UbuntuCertTrustInfo` by combining the
            pure-Python fingerprint with ``openssl x509`` text-mode
            metadata parsing.

            :param store: The abstract store name to record on the
                          result.
            :param path: The certificate file path.

            :returns: The :class:`UbuntuCertTrustInfo`, or ``None``
                      when the file cannot be read or parsed.
        """
        content = self._read_file(path=path)
        if content is None:
            rtnval: Optional[UbuntuCertTrustInfo] = None
            return rtnval

        blocks = _parse_pem_blocks(content=content)
        if len(blocks) == 0:
            rtnval = None
            return rtnval

        fingerprint = _compute_fingerprint(
            pem=blocks[0],
            colonized=True,
            uppercase=True,
        )
        if fingerprint is None:
            rtnval = None
            return rtnval

        text_command = [
            self._typed_config().openssl_executable,
            "x509",
            "-in",
            path,
            "-noout",
            "-subject",
            "-issuer",
            "-startdate",
            "-enddate",
            "-serial",
            "-text",
        ]
        run_result = self._runner.run(list(text_command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = None
            return rtnval

        parsed = _parse_openssl_cert_text(stdout=run_result.stdout)

        rtnval = UbuntuCertTrustInfo(
            fingerprint=fingerprint,
            subject=parsed.get("subject", ""),
            issuer=parsed.get("issuer", ""),
            not_before=parsed.get("not_before", ""),
            not_after=parsed.get("not_after", ""),
            serial_number=parsed.get("serial_number", ""),
            store=store,
            path=path,
            purposes=parsed.get("purposes", []),
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuCertTrustActionStatus,
        store: Optional[str],
        fingerprint: Optional[str],
        path: Optional[str],
        command: Optional[list],
        detail: Optional[str] = None,
    ) -> UbuntuCertTrustActionResult:
        """
            Construct an :class:`UbuntuCertTrustActionResult` without
            invoking the runner.
        """
        if command is None:
            command_copy: Optional[list] = None
        else:
            command_copy = list(command)
        rtnval = UbuntuCertTrustActionResult(
            status=status,
            detail=detail,
            command=command_copy,
            store=store,
            fingerprint=fingerprint,
            path=path,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — PEM / fingerprint / openssl text parsing
# ----------------------------------------------------------------------


def _parse_pem_blocks(*, content: str) -> list:
    """
        Split a PEM-bearing string into a list of CERTIFICATE blocks
        (each block is the full ``-----BEGIN-----`` /
        ``-----END-----`` envelope with its base64 body).

        Non-CERTIFICATE blocks (e.g. trust comments, ``CERTIFICATE
        REQUEST``) are ignored.  Lines outside any envelope are
        skipped.

        :param content: Raw file content.

        :returns: A ``list[str]``, one full PEM envelope per element.
    """
    rtnval: list = []
    in_block = False
    current_lines: list = []

    for raw in content.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped == _PEM_BEGIN:
            in_block = True
            current_lines = [line]
            continue
        if stripped == _PEM_END:
            if in_block:
                current_lines.append(line)
                block_text = "\n".join(current_lines) + "\n"
                rtnval.append(block_text)
            in_block = False
            current_lines = []
            continue
        if in_block:
            current_lines.append(line)

    return rtnval


def _compute_fingerprint(
    *,
    pem: str,
    colonized: bool,
    uppercase: bool,
) -> Optional[str]:
    """
        Compute the SHA-256 fingerprint of a PEM CERTIFICATE block.

        Decodes the body with base64, hashes the resulting DER bytes,
        and formats the digest.  Pure Python; does not invoke
        ``openssl``.

        :param pem: The full PEM envelope (may include trailing
                    whitespace).
        :param colonized: When True, format the output as
                          ``"AB:CD:EF:..."``; when False, as the
                          unbroken hex string ``"abcdef..."``.
        :param uppercase: When True, hex digits are uppercase; when
                          False, lowercase.

        :returns: The fingerprint string, or ``None`` when the PEM
                  body fails to decode.
    """
    body_lines: list = []
    in_body = False
    for raw in pem.splitlines():
        stripped = raw.strip()
        if stripped == _PEM_BEGIN:
            in_body = True
            continue
        if stripped == _PEM_END:
            in_body = False
            continue
        if in_body:
            body_lines.append(stripped)

    body = "".join(body_lines)
    if len(body) == 0:
        rtnval: Optional[str] = None
        return rtnval

    try:
        decoded = base64.b64decode(body, validate=False)
    except (ValueError, Exception):
        rtnval = None
        return rtnval

    if len(decoded) == 0:
        rtnval = None
        return rtnval

    digest = hashlib.sha256(decoded).digest()

    if uppercase:
        hex_text = digest.hex().upper()
    else:
        hex_text = digest.hex().lower()

    if colonized:
        chunks: list = []
        cursor = 0
        while cursor < len(hex_text):
            chunks.append(hex_text[cursor:cursor + 2])
            cursor += 2
        rtnval = ":".join(chunks)
        return rtnval

    rtnval = hex_text
    return rtnval


def _normalize_fingerprint(*, value: str) -> str:
    """
        Normalise a fingerprint string to the Ubuntu canonical form
        (uppercase hex bytes joined by colons).

        Accepts colonised or non-colonised input, any case.  ``"sha256
        Fingerprint=..."`` prefixes are stripped.

        :param value: The raw fingerprint string.

        :returns: The normalised string.
    """
    text = value.strip()
    lower_prefix = text.lower()
    if lower_prefix.startswith("sha256 fingerprint="):
        text = text[len("SHA256 Fingerprint="):]
    elif lower_prefix.startswith("sha256:"):
        text = text[len("SHA256:"):]
    text = text.replace(":", "").replace(" ", "").upper()

    if len(text) == 0:
        rtnval = ""
        return rtnval

    chunks: list = []
    cursor = 0
    while cursor < len(text):
        chunks.append(text[cursor:cursor + 2])
        cursor += 2
    rtnval = ":".join(chunks)
    return rtnval


def _normalize_destination_filename(*, source_path: str) -> str:
    """
        Build the destination filename for an installed anchor.

        Debian's ``update-ca-certificates`` only enumerates files whose
        names end in ``.crt``.  This helper takes the basename of
        ``source_path`` and appends ``.crt`` when not already present.

        :param source_path: The source file path.

        :returns: The destination filename (no directory component).
    """
    parts = source_path.split("/")
    basename = parts[-1] if len(parts) > 0 else source_path
    if basename.endswith(".crt"):
        rtnval = basename
        return rtnval
    if basename.endswith(".pem"):
        stem = basename[: -len(".pem")]
        rtnval = "{}.crt".format(stem)
        return rtnval
    rtnval = "{}.crt".format(basename)
    return rtnval


def _parse_openssl_cert_text(*, stdout: str) -> dict:
    """
        Parse the combined output of ``openssl x509 -subject -issuer
        -startdate -enddate -serial -text`` into a dict.

        Looks for the well-known prefixes
        (``subject=``, ``issuer=``, ``notBefore=``, ``notAfter=``,
        ``serial=``) on individual lines and the ``X509v3 Extended Key
        Usage`` block in the ``-text`` body for purposes.

        :param stdout: The captured ``openssl`` stdout.

        :returns: ``dict`` with keys ``subject``, ``issuer``,
                  ``not_before``, ``not_after``, ``serial_number``,
                  ``purposes``.
    """
    rtnval: dict = {
        "subject": "",
        "issuer": "",
        "not_before": "",
        "not_after": "",
        "serial_number": "",
        "purposes": [],
    }

    eku_marker = "X509v3 Extended Key Usage"
    in_eku = False

    for raw in stdout.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("subject="):
            rtnval["subject"] = stripped[len("subject="):].strip()
            continue
        if stripped.startswith("issuer="):
            rtnval["issuer"] = stripped[len("issuer="):].strip()
            continue
        if stripped.startswith("notBefore="):
            rtnval["not_before"] = stripped[len("notBefore="):].strip()
            continue
        if stripped.startswith("notAfter="):
            rtnval["not_after"] = stripped[len("notAfter="):].strip()
            continue
        if stripped.startswith("serial="):
            rtnval["serial_number"] = stripped[len("serial="):].strip()
            continue

        if eku_marker in stripped:
            in_eku = True
            continue

        if in_eku:
            if stripped.startswith("X509v3") or stripped.startswith("Signature"):
                in_eku = False
                continue
            if len(stripped) == 0:
                in_eku = False
                continue
            purposes_text = stripped.split(":", 1)[-1].strip()
            for chunk in purposes_text.split(","):
                chunk_clean = chunk.strip()
                if len(chunk_clean) == 0:
                    continue
                if chunk_clean not in rtnval["purposes"]:
                    rtnval["purposes"].append(chunk_clean)
            in_eku = False

    return rtnval
