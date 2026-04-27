"""
    Ubuntu certificate-issuance feature.

    Implements the cross-OS certificate-issuance contract for Ubuntu by
    wrapping two interchangeable backends through the injected
    :class:`ICommandRunner`:

    - ``UbuntuIssuanceBackend.CERTBOT`` — ACME issuance via the system
      ``certbot`` binary (Let's Encrypt by default).  Default backend.
    - ``UbuntuIssuanceBackend.OPENSSL`` — local self-signed / CSR-based
      issuance via the system ``openssl`` binary.

    Verb / tool mapping:

    +------------------------+--------------------------+--------------------------+
    | Verb                   | CERTBOT backend          | OPENSSL backend          |
    +========================+==========================+==========================+
    | ``request_certificate``| ``certbot certonly``     | ``openssl req``          |
    |                        | ``--non-interactive``    | ``-newkey rsa:<bits>``   |
    |                        | ``--agree-tos``          | ``-nodes -keyout <k>``   |
    |                        | ``--email <e>``          | ``-out <csr>``           |
    |                        | ``-d <cn> [-d <san>]``   | ``-subj '/CN=<cn>'``     |
    |                        | ``(--webroot -w <wr>``   | (then for self-signed:   |
    |                        |  ``\\| --standalone)``    | ``openssl x509 -req``    |
    |                        | ``--cert-name <alias>``  | ``-days <n> -in <csr>``  |
    |                        |                          | ``-signkey <k>``         |
    |                        |                          | ``-out <cert>``)         |
    +------------------------+--------------------------+--------------------------+
    | ``renew_certificate``  | ``certbot renew``        | regenerate self-signed   |
    |                        | ``--cert-name <alias>``  | cert against existing    |
    |                        | ``--non-interactive``    | key+CSR via              |
    |                        |                          | ``openssl x509 -req``    |
    +------------------------+--------------------------+--------------------------+
    | ``revoke_certificate`` | ``certbot revoke``       | FAILED — no CRL          |
    |                        | ``--cert-name <alias>``  | machinery in scope       |
    |                        | ``--non-interactive``    |                          |
    |                        | ``[--reason <r>]``       |                          |
    +------------------------+--------------------------+--------------------------+
    | ``get_certificate``    | ``openssl x509 -text``   | ``openssl x509 -text``   |
    +------------------------+--------------------------+--------------------------+
    | ``list_certificates``  | ``certbot certificates`` | ``ls -1`` + per-file     |
    |                        | parse                    | ``openssl x509 -text``   |
    +------------------------+--------------------------+--------------------------+
    | ``export_certificate`` | ``cat <src>`` + ``tee``  | same                     |
    |                        | ``<dest>``               |                          |
    +------------------------+--------------------------+--------------------------+
    | ``import_certificate`` | ``cat <src>`` + PEM      | same                     |
    |                        | validate + ``tee``       |                          |
    |                        | ``<cert_dir>/<a>.pem``   |                          |
    +------------------------+--------------------------+--------------------------+

    Backend selection:
        Backend dispatch happens at the verb level via paired
        ``_dispatch_certbot_<op>`` / ``_dispatch_openssl_<op>`` private
        helpers.  Public verbs select between them based on
        ``config.backend`` and (for :meth:`request_certificate`) on
        ``spec.is_self_signed``.

    File I/O policy:
        Reads use ``cat`` and ``ls``; writes use ``tee`` with the new
        content on stdin.  This module does NOT consume an
        :class:`IFileOperator`.

    Idempotency model:
        - :meth:`request_certificate` returns ``SKIPPED`` when the
          alias is already present and the existing certificate's
          remaining validity is greater than
          ``config.renew_threshold_days``.
        - :meth:`renew_certificate` returns ``SKIPPED`` under the same
          threshold.
        - :meth:`revoke_certificate` returns ``SKIPPED`` when the alias
          is absent.
        - :meth:`import_certificate` returns ``SKIPPED`` when the
          source PEM's SHA-256 fingerprint matches the already-stored
          certificate under the alias.
        - :meth:`export_certificate` returns ``SKIPPED`` when the
          destination path's bytes already match what would be
          written, compared via ``cmp -s``.

    Elevation policy:
        Mutating verbs honour ``config.elevated`` and pass it
        through to the runner.  Reads do not elevate.

    Credential safety:
        No verb places passphrase / ACME credential bytes on argv.
        Self-signed openssl flows use ``-nodes`` (unencrypted key)
        and certbot reads its account credentials from
        ``/etc/letsencrypt/accounts/`` outside this manager's
        purview.
"""

import base64
import hashlib
import os

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
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
_CMP = "cmp"
_OPENSSL = "openssl"
_CERTBOT = "certbot"

_DEFAULT_CERT_DIR = "/etc/letsencrypt/live"

_PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
_PEM_END = "-----END CERTIFICATE-----"


UbuntuCertIssuanceActionStatus = ActionStatus


# ----------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------


class UbuntuIssuanceBackend(Enum):
    """
        Backend selector for :class:`UbuntuCertificateIssuanceManager`.

        :cvar CERTBOT: ACME issuance via the system ``certbot`` binary.
                       Default.
        :cvar OPENSSL: Local self-signed / CSR-based issuance via
                       ``openssl``.
    """
    CERTBOT = "certbot"
    OPENSSL = "openssl"


class UbuntuRevocationReason(Enum):
    """
        Revocation reason codes accepted by
        :meth:`UbuntuCertificateIssuanceManager.revoke_certificate`.
        The string value matches certbot's ``--reason`` flag
        vocabulary.
    """
    UNSPECIFIED            = "unspecified"
    KEY_COMPROMISE         = "keycompromise"
    CA_COMPROMISE          = "cacompromise"
    AFFILIATION_CHANGED    = "affiliationchanged"
    SUPERSEDED             = "superseded"
    CESSATION_OF_OPERATION = "cessationofoperation"


# ----------------------------------------------------------------------
# Config / data models
# ----------------------------------------------------------------------


@dataclass
class UbuntuCertIssuanceManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuCertificateIssuanceManager`.
        Inherits ``dry_run`` and ``elevated`` from
        :class:`FeatureConfig`.

        :param backend: Issuance backend.  Defaults to
                        :attr:`UbuntuIssuanceBackend.CERTBOT`.
        :param certbot_cli: Command name for ``certbot``.  Defaults to
                            ``"certbot"`` (PATH-resolved).
        :param openssl_cli: Command name for ``openssl``.  Defaults to
                            ``"openssl"``.
        :param cert_dir: Directory where managed certificates live.
                         Defaults to ``"/etc/letsencrypt/live"``.
        :param acme_email: ACME registration email.  Required for the
                           certbot backend; ignored for openssl.
        :param acme_method: ACME challenge method.  ``"webroot"`` (the
                            default) or ``"standalone"``.
        :param webroot: Filesystem path for the ``webroot`` ACME
                        method.  Required when ``acme_method`` is
                        ``"webroot"``.
        :param renew_threshold_days: Skip threshold (in days) for the
                                     idempotency guard on
                                     :meth:`request_certificate` and
                                     :meth:`renew_certificate`.
                                     Defaults to ``30``.
    """
    backend: UbuntuIssuanceBackend = UbuntuIssuanceBackend.CERTBOT
    certbot_cli: str = _CERTBOT
    openssl_cli: str = _OPENSSL
    cert_dir: str = _DEFAULT_CERT_DIR
    acme_email: Optional[str] = None
    acme_method: str = "webroot"
    webroot: Optional[str] = None
    renew_threshold_days: int = 30


@dataclass
class UbuntuCertIssuanceActionResult(FeatureActionResult):
    """
        Result of a certificate-issuance write operation.

        :param alias: The caller-chosen alias acted upon.
        :param subject: The certificate subject DN, when known.
        :param fingerprint: The SHA-256 fingerprint of the affected
                            certificate (Linux uppercase-with-colons
                            form), when known.
    """
    alias: Optional[str] = None
    subject: Optional[str] = None
    fingerprint: Optional[str] = None


@dataclass
class UbuntuCertificateInfo:
    """
        Structured metadata for a single managed certificate.

        :param alias: Caller-chosen alias.
        :param subject: Certificate subject DN.
        :param issuer: Issuer DN.
        :param not_before: Validity start (openssl format).
        :param not_after: Validity end (openssl format).
        :param fingerprint: SHA-256 fingerprint
                            (uppercase-with-colons).
        :param serial_number: Hex serial number.
        :param key_algorithm: ``"RSA"``, ``"EC"``, etc., or ``None``
                              when not exposed by parsing.
        :param key_length: Key size in bits, or ``None``.
        :param sans: Subject-alternative names extracted from the
                     X.509 v3 SAN extension.
        :param path: Filesystem path of the certificate file.
        :param is_self_signed: ``True`` when subject == issuer.
    """
    alias: str
    subject: str
    issuer: str
    not_before: str
    not_after: str
    fingerprint: str
    serial_number: str
    key_algorithm: Optional[str]
    key_length: Optional[int]
    sans: list = field(default_factory=list)
    path: Optional[str] = None
    is_self_signed: bool = False


@dataclass
class UbuntuCertificateRequestSpec:
    """
        Caller-supplied issuance spec for
        :meth:`UbuntuCertificateIssuanceManager.request_certificate`.

        :param alias: Caller-chosen identifier.
        :param common_name: Certificate CN (also the primary SAN for
                            certbot).
        :param email: ACME registration email override.  When ``None``
                      the manager falls back to
                      ``config.acme_email``.
        :param sans: Additional subject-alternative DNS names.
        :param key_algorithm: ``"RSA"`` or ``"EC"``.
        :param key_length: Key size in bits (RSA) or curve nominal size
                           (EC).
        :param validity_days: Days of validity for self-signed certs.
        :param is_self_signed: When ``True``, dispatches to the openssl
                               branch regardless of config backend.
        :param webroot: Override for the certbot webroot path.  When
                        ``None`` the manager uses ``config.webroot``.
    """
    alias: str
    common_name: str
    email: Optional[str] = None
    sans: list = field(default_factory=list)
    key_algorithm: str = "RSA"
    key_length: int = 2048
    validity_days: int = 365
    is_self_signed: bool = False
    webroot: Optional[str] = None


class UbuntuCertIssuanceQueryError(Exception):
    """
        Raised when a certificate-issuance read fails for a reason
        other than the target certificate simply being absent.
    """

    def __init__(
        self,
        *,
        operation: str,
        message: str,
    ) -> None:
        """
            :param operation: The verb that triggered the failure.
            :param message: Human-readable detail.
        """
        self.operation = operation
        self.message = message
        err_msg_lines = ["Certificate issuance query command failed."]
        err_msg_lines.append("    OPERATION: {}".format(operation))
        err_msg_lines.append("    DETAIL: {}".format(message))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class UbuntuCertificateIssuanceManager(BaseFeatureManager):
    """
        Ubuntu certificate-issuance manager.  Wraps the Debian
        certbot + openssl toolchain through the injected
        :class:`ICommandRunner`.
    """

    result_class = UbuntuCertIssuanceActionResult
    status_class = UbuntuCertIssuanceActionStatus
    feature_name = "certificate-issuance"

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def request_certificate(
        self,
        spec: UbuntuCertificateRequestSpec,
    ) -> UbuntuCertIssuanceActionResult:
        """
            Issue a new X.509 certificate keyed by ``spec.alias``.

            Dispatches to the certbot or openssl backend based on
            ``config.backend`` and ``spec.is_self_signed``.  Returns
            ``SKIPPED`` when the alias is already present and the
            existing certificate has more than
            ``config.renew_threshold_days`` of validity remaining.

            :param spec: Caller-supplied
                         :class:`UbuntuCertificateRequestSpec`.

            :returns: The :class:`UbuntuCertIssuanceActionResult`.
        """
        existing_path = self._alias_path(alias=spec.alias)
        existing_info = self._read_cert_info_for_alias(
            alias=spec.alias,
            path=existing_path,
        )
        if existing_info is not None:
            days = _days_until(not_after=existing_info.not_after)
            if days is not None and days > self._typed_config().renew_threshold_days:
                skipped = self._build_result(
                    status=UbuntuCertIssuanceActionStatus.SKIPPED,
                    alias=spec.alias,
                    subject=existing_info.subject,
                    fingerprint=existing_info.fingerprint,
                    command=None,
                    detail="certificate not yet within renew threshold",
                )
                return skipped

        if spec.is_self_signed:
            rtnval = self._dispatch_openssl_request(spec=spec)
            return rtnval

        backend = self._typed_config().backend
        if backend == UbuntuIssuanceBackend.CERTBOT:
            rtnval = self._dispatch_certbot_request(spec=spec)
            return rtnval

        rtnval = self._dispatch_openssl_request(spec=spec)
        return rtnval

    def renew_certificate(
        self,
        alias: str,
    ) -> UbuntuCertIssuanceActionResult:
        """
            Renew an existing certificate identified by ``alias``.

            Returns ``SKIPPED`` when the existing certificate is more
            than ``config.renew_threshold_days`` from expiry.  Returns
            ``FAILED`` when the alias is unknown.

            :param alias: The caller-chosen alias.

            :returns: The :class:`UbuntuCertIssuanceActionResult`.
        """
        existing_path = self._alias_path(alias=alias)
        existing_info = self._read_cert_info_for_alias(
            alias=alias,
            path=existing_path,
        )
        if existing_info is None:
            failed = self._build_result(
                status=UbuntuCertIssuanceActionStatus.FAILED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=None,
                detail="alias not found",
            )
            return failed

        days = _days_until(not_after=existing_info.not_after)
        if days is not None and days > self._typed_config().renew_threshold_days:
            skipped = self._build_result(
                status=UbuntuCertIssuanceActionStatus.SKIPPED,
                alias=alias,
                subject=existing_info.subject,
                fingerprint=existing_info.fingerprint,
                command=None,
                detail="certificate not yet within renew threshold",
            )
            return skipped

        backend = self._typed_config().backend
        if backend == UbuntuIssuanceBackend.CERTBOT:
            rtnval = self._dispatch_certbot_renew(
                alias=alias, existing_info=existing_info,
            )
            return rtnval

        rtnval = self._dispatch_openssl_renew(
            alias=alias, existing_info=existing_info,
        )
        return rtnval

    def revoke_certificate(
        self,
        alias: str,
        reason: Optional[UbuntuRevocationReason] = None,
    ) -> UbuntuCertIssuanceActionResult:
        """
            Revoke an existing certificate identified by ``alias``.

            Returns ``SKIPPED`` when the alias is absent.  On the
            openssl backend returns ``FAILED`` (no CRL machinery in
            the pilot).

            :param alias: The caller-chosen alias.
            :param reason: Optional :class:`UbuntuRevocationReason`.

            :returns: The :class:`UbuntuCertIssuanceActionResult`.
        """
        existing_path = self._alias_path(alias=alias)
        existing_info = self._read_cert_info_for_alias(
            alias=alias,
            path=existing_path,
        )
        if existing_info is None:
            skipped = self._build_result(
                status=UbuntuCertIssuanceActionStatus.SKIPPED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=None,
                detail="alias not present",
            )
            return skipped

        backend = self._typed_config().backend
        if backend == UbuntuIssuanceBackend.CERTBOT:
            rtnval = self._dispatch_certbot_revoke(
                alias=alias,
                reason=reason,
                existing_info=existing_info,
            )
            return rtnval

        rtnval = self._dispatch_openssl_revoke(
            alias=alias,
            reason=reason,
            existing_info=existing_info,
        )
        return rtnval

    def export_certificate(
        self,
        alias: str,
        path: str,
        include_private_key: bool = False,
    ) -> UbuntuCertIssuanceActionResult:
        """
            Copy the certificate (and optionally its key) for
            ``alias`` to a destination path.

            :param alias: The caller-chosen alias.
            :param path: Destination filesystem path.
            :param include_private_key: When ``True``, prepend the
                                        managed private key to the
                                        output bundle (PEM key + PEM
                                        cert).  Default ``False``.

            :returns: The :class:`UbuntuCertIssuanceActionResult`.
        """
        cert_path = self._alias_path(alias=alias)
        cert_content = self._read_file(path=cert_path)
        if cert_content is None:
            failed = self._build_result(
                status=UbuntuCertIssuanceActionStatus.FAILED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=None,
                detail="alias not found",
            )
            return failed

        if include_private_key:
            key_path = self._alias_key_path(alias=alias)
            key_content = self._read_file(path=key_path)
            if key_content is None:
                failed = self._build_result(
                    status=UbuntuCertIssuanceActionStatus.FAILED,
                    alias=alias,
                    subject=None,
                    fingerprint=None,
                    command=None,
                    detail="private key not found",
                )
                return failed
            output_content = "{}{}".format(key_content, cert_content)
        else:
            output_content = cert_content

        existing_dest = self._read_file(path=path)
        if existing_dest is not None and existing_dest == output_content:
            skipped = self._build_result(
                status=UbuntuCertIssuanceActionStatus.SKIPPED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=None,
                detail="destination already matches source bytes",
            )
            return skipped

        write_command = [_TEE, path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuCertIssuanceActionStatus.DRY_RUN,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=output_content,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = UbuntuCertIssuanceActionStatus.FAILED

        rtnval = UbuntuCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            alias=alias,
            subject=None,
            fingerprint=None,
        )
        return rtnval

    def import_certificate(
        self,
        path: str,
        alias: str,
    ) -> UbuntuCertIssuanceActionResult:
        """
            Import an externally-issued certificate under ``alias``.

            Reads the source file via ``cat``, validates it as a PEM
            certificate, computes its SHA-256 fingerprint in pure
            Python, and writes it to
            ``<config.cert_dir>/<alias>.pem`` via ``tee``.

            :param path: Source file path.
            :param alias: Target alias.

            :returns: The :class:`UbuntuCertIssuanceActionResult`.
        """
        source_content = self._read_file(path=path)
        if source_content is None:
            failed = self._build_result(
                status=UbuntuCertIssuanceActionStatus.FAILED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=[_CAT, path],
                detail="source path does not exist",
            )
            return failed

        cert_blocks = _parse_pem_blocks(content=source_content)
        if len(cert_blocks) == 0:
            failed = self._build_result(
                status=UbuntuCertIssuanceActionStatus.FAILED,
                alias=alias,
                subject=None,
                fingerprint=None,
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
                status=UbuntuCertIssuanceActionStatus.FAILED,
                alias=alias,
                subject=None,
                fingerprint=None,
                command=None,
                detail="failed to compute fingerprint from PEM body",
            )
            return failed

        existing_path = self._alias_path(alias=alias)
        existing_content = self._read_file(path=existing_path)
        if existing_content is not None:
            existing_blocks = _parse_pem_blocks(content=existing_content)
            if len(existing_blocks) > 0:
                existing_fp = _compute_fingerprint(
                    pem=existing_blocks[0],
                    colonized=True,
                    uppercase=True,
                )
                if existing_fp == fingerprint:
                    skipped = self._build_result(
                        status=UbuntuCertIssuanceActionStatus.SKIPPED,
                        alias=alias,
                        subject=None,
                        fingerprint=fingerprint,
                        command=None,
                        detail="alias already maps to identical fingerprint",
                    )
                    return skipped

        write_command = [_TEE, existing_path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuCertIssuanceActionStatus.DRY_RUN,
                alias=alias,
                subject=None,
                fingerprint=fingerprint,
                command=write_command,
            )
            return dry

        run_result = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=source_content,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = UbuntuCertIssuanceActionStatus.FAILED

        rtnval = UbuntuCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(write_command),
            alias=alias,
            subject=None,
            fingerprint=fingerprint,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_certificate(
        self,
        alias: str,
    ) -> Optional[UbuntuCertificateInfo]:
        """
            Return :class:`UbuntuCertificateInfo` for ``alias``, or
            ``None`` when no certificate is registered.

            :param alias: The caller-chosen alias.

            :returns: The :class:`UbuntuCertificateInfo` or ``None``.

            :raises UbuntuCertIssuanceQueryError: When parsing the
                                                  on-disk certificate
                                                  fails unexpectedly.
        """
        cert_path = self._alias_path(alias=alias)
        rtnval = self._read_cert_info_for_alias(
            alias=alias,
            path=cert_path,
        )
        return rtnval

    def list_certificates(self) -> list:
        """
            Enumerate every certificate known to this manager.

            On the certbot backend, dispatches ``certbot certificates``
            and parses the output.  On the openssl backend, enumerates
            ``config.cert_dir`` and parses each ``.pem`` / ``.crt``
            file.

            :returns: A ``list[UbuntuCertificateInfo]``.
        """
        backend = self._typed_config().backend
        if backend == UbuntuIssuanceBackend.CERTBOT:
            rtnval = self._dispatch_certbot_list()
            return rtnval
        rtnval = self._dispatch_openssl_list()
        return rtnval

    # ------------------------------------------------------------------
    # Backend dispatch — certbot
    # ------------------------------------------------------------------

    def _dispatch_certbot_request(
        self,
        *,
        spec: UbuntuCertificateRequestSpec,
    ) -> UbuntuCertIssuanceActionResult:
        """
            Build and dispatch a ``certbot certonly`` invocation.
        """
        cfg = self._typed_config()
        email = spec.email if spec.email is not None else cfg.acme_email
        if email is None:
            failed = self._build_result(
                status=UbuntuCertIssuanceActionStatus.FAILED,
                alias=spec.alias,
                subject=None,
                fingerprint=None,
                command=None,
                detail="acme_email is required for certbot issuance",
            )
            return failed

        argv: list = [
            cfg.certbot_cli,
            "certonly",
            "--non-interactive",
            "--agree-tos",
            "--email", email,
        ]
        if cfg.acme_method == "standalone":
            argv.append("--standalone")
        else:
            webroot = spec.webroot if spec.webroot is not None else cfg.webroot
            if webroot is None:
                failed = self._build_result(
                    status=UbuntuCertIssuanceActionStatus.FAILED,
                    alias=spec.alias,
                    subject=None,
                    fingerprint=None,
                    command=None,
                    detail="webroot is required for the webroot method",
                )
                return failed
            argv.append("--webroot")
            argv.append("-w")
            argv.append(webroot)

        argv.append("-d")
        argv.append(spec.common_name)
        for san in spec.sans:
            argv.append("-d")
            argv.append(san)
        argv.append("--cert-name")
        argv.append(spec.alias)

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuCertIssuanceActionStatus.DRY_RUN,
                alias=spec.alias,
                subject=spec.common_name,
                fingerprint=None,
                command=argv,
            )
            return dry

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = UbuntuCertIssuanceActionStatus.FAILED

        rtnval = UbuntuCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(argv),
            alias=spec.alias,
            subject=spec.common_name,
            fingerprint=None,
        )
        return rtnval

    def _dispatch_certbot_renew(
        self,
        *,
        alias: str,
        existing_info: UbuntuCertificateInfo,
    ) -> UbuntuCertIssuanceActionResult:
        """
            Build and dispatch a ``certbot renew --cert-name`` argv.
        """
        cfg = self._typed_config()
        argv = [
            cfg.certbot_cli,
            "renew",
            "--cert-name", alias,
            "--non-interactive",
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuCertIssuanceActionStatus.DRY_RUN,
                alias=alias,
                subject=existing_info.subject,
                fingerprint=existing_info.fingerprint,
                command=argv,
            )
            return dry

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = UbuntuCertIssuanceActionStatus.FAILED

        rtnval = UbuntuCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(argv),
            alias=alias,
            subject=existing_info.subject,
            fingerprint=existing_info.fingerprint,
        )
        return rtnval

    def _dispatch_certbot_revoke(
        self,
        *,
        alias: str,
        reason: Optional[UbuntuRevocationReason],
        existing_info: UbuntuCertificateInfo,
    ) -> UbuntuCertIssuanceActionResult:
        """
            Build and dispatch a ``certbot revoke --cert-name`` argv.
        """
        cfg = self._typed_config()
        argv = [
            cfg.certbot_cli,
            "revoke",
            "--cert-name", alias,
            "--non-interactive",
        ]
        if reason is not None:
            argv.append("--reason")
            argv.append(reason.value)

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuCertIssuanceActionStatus.DRY_RUN,
                alias=alias,
                subject=existing_info.subject,
                fingerprint=existing_info.fingerprint,
                command=argv,
            )
            return dry

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = UbuntuCertIssuanceActionStatus.FAILED

        rtnval = UbuntuCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(argv),
            alias=alias,
            subject=existing_info.subject,
            fingerprint=existing_info.fingerprint,
        )
        return rtnval

    def _dispatch_certbot_list(self) -> list:
        """
            Dispatch ``certbot certificates`` and parse the output
            into a list of :class:`UbuntuCertificateInfo`.
        """
        cfg = self._typed_config()
        argv = [cfg.certbot_cli, "certificates"]
        run_result = self._runner.run(list(argv), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval
        rtnval = _parse_certbot_certificates_output(stdout=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Backend dispatch — openssl
    # ------------------------------------------------------------------

    def _dispatch_openssl_request(
        self,
        *,
        spec: UbuntuCertificateRequestSpec,
    ) -> UbuntuCertIssuanceActionResult:
        """
            Issue a self-signed cert via openssl ``req`` + ``x509
            -req -signkey``.
        """
        cfg = self._typed_config()
        cert_path = self._alias_path(alias=spec.alias)
        key_path = self._alias_key_path(alias=spec.alias)
        csr_path = self._alias_csr_path(alias=spec.alias)
        algorithm = spec.key_algorithm.lower()
        algorithm_token = "{}:{}".format(algorithm, spec.key_length)
        subject = _format_subject(common_name=spec.common_name)

        req_argv: list = [
            cfg.openssl_cli,
            "req",
            "-newkey", algorithm_token,
            "-nodes",
            "-keyout", key_path,
            "-out", csr_path,
            "-subj", subject,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuCertIssuanceActionStatus.DRY_RUN,
                alias=spec.alias,
                subject=spec.common_name,
                fingerprint=None,
                command=req_argv,
            )
            return dry

        req_result = self._runner.run(
            list(req_argv),
            elevated=self._config.elevated,
        )
        if req_result.exit_code != 0:
            failed = UbuntuCertIssuanceActionResult(
                status=UbuntuCertIssuanceActionStatus.FAILED,
                detail=None,
                exit_code=req_result.exit_code,
                stdout=req_result.stdout,
                stderr=req_result.stderr,
                command=list(req_argv),
                alias=spec.alias,
                subject=spec.common_name,
                fingerprint=None,
            )
            return failed

        sign_argv: list = [
            cfg.openssl_cli,
            "x509", "-req",
            "-days", str(spec.validity_days),
            "-in", csr_path,
            "-signkey", key_path,
            "-out", cert_path,
        ]
        sign_result = self._runner.run(
            list(sign_argv),
            elevated=self._config.elevated,
        )

        if sign_result.exit_code == 0:
            mapped_status = UbuntuCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = UbuntuCertIssuanceActionStatus.FAILED

        rtnval = UbuntuCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=sign_result.exit_code,
            stdout=sign_result.stdout,
            stderr=sign_result.stderr,
            command=list(sign_argv),
            alias=spec.alias,
            subject=spec.common_name,
            fingerprint=None,
        )
        return rtnval

    def _dispatch_openssl_renew(
        self,
        *,
        alias: str,
        existing_info: UbuntuCertificateInfo,
    ) -> UbuntuCertIssuanceActionResult:
        """
            Re-sign the existing CSR against the existing key.
        """
        cfg = self._typed_config()
        cert_path = self._alias_path(alias=alias)
        key_path = self._alias_key_path(alias=alias)
        csr_path = self._alias_csr_path(alias=alias)

        argv: list = [
            cfg.openssl_cli,
            "x509", "-req",
            "-days", "365",
            "-in", csr_path,
            "-signkey", key_path,
            "-out", cert_path,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuCertIssuanceActionStatus.DRY_RUN,
                alias=alias,
                subject=existing_info.subject,
                fingerprint=existing_info.fingerprint,
                command=argv,
            )
            return dry

        run_result = self._runner.run(
            list(argv),
            elevated=self._config.elevated,
        )

        if run_result.exit_code == 0:
            mapped_status = UbuntuCertIssuanceActionStatus.SUCCESS
        else:
            mapped_status = UbuntuCertIssuanceActionStatus.FAILED

        rtnval = UbuntuCertIssuanceActionResult(
            status=mapped_status,
            detail=None,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(argv),
            alias=alias,
            subject=existing_info.subject,
            fingerprint=existing_info.fingerprint,
        )
        return rtnval

    def _dispatch_openssl_revoke(
        self,
        *,
        alias: str,
        reason: Optional[UbuntuRevocationReason],
        existing_info: UbuntuCertificateInfo,
    ) -> UbuntuCertIssuanceActionResult:
        """
            Local-CA revocation is out of scope for the pilot; return
            FAILED with a descriptive detail.
        """
        rtnval = self._build_result(
            status=UbuntuCertIssuanceActionStatus.FAILED,
            alias=alias,
            subject=existing_info.subject,
            fingerprint=existing_info.fingerprint,
            command=None,
            detail="OPENSSL backend has no CRL machinery; revocation out of scope",
        )
        return rtnval

    def _dispatch_openssl_list(self) -> list:
        """
            Enumerate ``config.cert_dir`` and parse each
            ``.pem`` / ``.crt`` file.
        """
        cfg = self._typed_config()
        entries = self._list_dir(path=cfg.cert_dir)
        rtnval: list = []
        for entry in entries:
            if not (entry.endswith(".pem") or entry.endswith(".crt")):
                continue
            full_path = "{}/{}".format(cfg.cert_dir, entry)
            alias = entry
            if alias.endswith(".pem"):
                alias = alias[: -len(".pem")]
            elif alias.endswith(".crt"):
                alias = alias[: -len(".crt")]
            info = self._read_cert_info_for_alias(
                alias=alias,
                path=full_path,
            )
            if info is not None:
                rtnval.append(info)
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> UbuntuCertIssuanceManagerConfig:
        """
            Narrow the base config to the typed subclass.
        """
        rtnval = self._config
        return rtnval

    def _alias_path(self, *, alias: str) -> str:
        """
            Resolve an alias to its certificate file path under
            ``config.cert_dir``.
        """
        cfg = self._typed_config()
        rtnval = "{}/{}.pem".format(cfg.cert_dir, alias)
        return rtnval

    def _alias_key_path(self, *, alias: str) -> str:
        """
            Resolve an alias to its private-key file path.
        """
        cfg = self._typed_config()
        rtnval = "{}/{}.key".format(cfg.cert_dir, alias)
        return rtnval

    def _alias_csr_path(self, *, alias: str) -> str:
        """
            Resolve an alias to its CSR file path.
        """
        cfg = self._typed_config()
        rtnval = "{}/{}.csr".format(cfg.cert_dir, alias)
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat`` without elevation.
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

    def _read_cert_info_for_alias(
        self,
        *,
        alias: str,
        path: str,
    ) -> Optional[UbuntuCertificateInfo]:
        """
            Parse one certificate file into an
            :class:`UbuntuCertificateInfo`.  Returns ``None`` when the
            file is absent.
        """
        cfg = self._typed_config()
        cert_content = self._read_file(path=path)
        if cert_content is None:
            rtnval: Optional[UbuntuCertificateInfo] = None
            return rtnval

        blocks = _parse_pem_blocks(content=cert_content)
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
            cfg.openssl_cli,
            "x509",
            "-in", path,
            "-noout",
            "-text",
            "-fingerprint", "-sha256",
            "-dates",
            "-subject",
            "-issuer",
            "-serial",
        ]
        run_result = self._runner.run(list(text_command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = None
            return rtnval

        parsed = _parse_openssl_x509_text(stdout=run_result.stdout)
        is_self_signed = parsed.get("subject", "") == parsed.get("issuer", "")

        rtnval = UbuntuCertificateInfo(
            alias=alias,
            subject=parsed.get("subject", ""),
            issuer=parsed.get("issuer", ""),
            not_before=parsed.get("not_before", ""),
            not_after=parsed.get("not_after", ""),
            fingerprint=fingerprint,
            serial_number=parsed.get("serial_number", ""),
            key_algorithm=parsed.get("key_algorithm", None),
            key_length=parsed.get("key_length", None),
            sans=parsed.get("sans", []),
            path=path,
            is_self_signed=is_self_signed,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuCertIssuanceActionStatus,
        alias: Optional[str],
        subject: Optional[str],
        fingerprint: Optional[str],
        command: Optional[list],
        detail: Optional[str] = None,
    ) -> UbuntuCertIssuanceActionResult:
        """
            Construct an :class:`UbuntuCertIssuanceActionResult`
            without invoking the runner.
        """
        if command is None:
            command_copy: Optional[list] = None
        else:
            command_copy = list(command)
        rtnval = UbuntuCertIssuanceActionResult(
            status=status,
            detail=detail,
            command=command_copy,
            alias=alias,
            subject=subject,
            fingerprint=fingerprint,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — PEM / fingerprint
# ----------------------------------------------------------------------


def _parse_pem_blocks(*, content: str) -> list:
    """
        Split a PEM-bearing string into a list of CERTIFICATE blocks.

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

        :param pem: The full PEM envelope.
        :param colonized: When True, format with colons.
        :param uppercase: When True, uppercase hex digits.

        :returns: The fingerprint string, or ``None`` on decode
                  failure.
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


# ----------------------------------------------------------------------
# Module-level helpers — openssl x509 -text parsing
# ----------------------------------------------------------------------


def _parse_openssl_x509_text(*, stdout: str) -> dict:
    """
        Parse the combined output of ``openssl x509 -text -fingerprint
        -sha256 -dates -subject -issuer -serial``.

        :param stdout: Captured ``openssl`` stdout.

        :returns: A ``dict`` with keys ``subject``, ``issuer``,
                  ``not_before``, ``not_after``, ``serial_number``,
                  ``sans``, ``key_algorithm``, ``key_length``.
    """
    rtnval: dict = {
        "subject": "",
        "issuer": "",
        "not_before": "",
        "not_after": "",
        "serial_number": "",
        "sans": [],
        "key_algorithm": None,
        "key_length": None,
    }

    san_marker = "X509v3 Subject Alternative Name"
    in_san = False
    public_key_marker = "Public Key Algorithm:"
    rsa_size_marker = "Public-Key:"

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

        if stripped.startswith(public_key_marker):
            algo_text = stripped[len(public_key_marker):].strip()
            if "rsaEncryption" in algo_text:
                rtnval["key_algorithm"] = "RSA"
            elif "id-ecPublicKey" in algo_text:
                rtnval["key_algorithm"] = "EC"
            else:
                rtnval["key_algorithm"] = algo_text
            continue

        if stripped.startswith(rsa_size_marker):
            size_text = stripped[len(rsa_size_marker):].strip()
            digits: list = []
            for ch in size_text:
                if ch.isdigit():
                    digits.append(ch)
                elif len(digits) > 0:
                    break
            if len(digits) > 0:
                try:
                    parsed_length = int("".join(digits))
                    rtnval["key_length"] = parsed_length
                except (ValueError, Exception):
                    pass
            continue

        if san_marker in stripped:
            in_san = True
            continue

        if in_san:
            if stripped.startswith("X509v3") or stripped.startswith("Signature"):
                in_san = False
                continue
            if len(stripped) == 0:
                in_san = False
                continue
            for chunk in stripped.split(","):
                chunk_clean = chunk.strip()
                if len(chunk_clean) == 0:
                    continue
                if chunk_clean.startswith("DNS:"):
                    rtnval["sans"].append(chunk_clean[len("DNS:"):].strip())
                elif chunk_clean.startswith("IP Address:"):
                    rtnval["sans"].append(chunk_clean[len("IP Address:"):].strip())
                elif chunk_clean.startswith("URI:"):
                    rtnval["sans"].append(chunk_clean[len("URI:"):].strip())
                elif chunk_clean.startswith("email:"):
                    rtnval["sans"].append(chunk_clean[len("email:"):].strip())
                else:
                    rtnval["sans"].append(chunk_clean)
            in_san = False

    return rtnval


def _parse_certbot_certificates_output(*, stdout: str) -> list:
    """
        Parse the human-readable output of ``certbot certificates``
        into a list of :class:`UbuntuCertificateInfo`.

        ``certbot certificates`` emits a series of indented blocks
        with ``Certificate Name:``, ``Domains:``, ``Expiry Date:``,
        and ``Certificate Path:`` lines.  This parser tolerates extra
        whitespace and skips header / footer lines that have no
        ``Certificate Name:``.

        :param stdout: Captured certbot stdout.

        :returns: A ``list[UbuntuCertificateInfo]``.
    """
    rtnval: list = []
    current: dict = {
        "alias": "",
        "domains": "",
        "expiry": "",
        "path": "",
    }
    in_block = False

    def _flush(block: dict) -> Optional[UbuntuCertificateInfo]:
        if len(block.get("alias", "")) == 0:
            return None
        domains_text = block.get("domains", "")
        sans: list = []
        for token in domains_text.split():
            token_clean = token.strip()
            if len(token_clean) == 0:
                continue
            sans.append(token_clean)
        info = UbuntuCertificateInfo(
            alias=block["alias"],
            subject="",
            issuer="",
            not_before="",
            not_after=block.get("expiry", ""),
            fingerprint="",
            serial_number="",
            key_algorithm=None,
            key_length=None,
            sans=sans,
            path=block.get("path", "") if len(block.get("path", "")) > 0 else None,
            is_self_signed=False,
        )
        return info

    for raw in stdout.splitlines():
        stripped = raw.strip()
        if stripped.startswith("Certificate Name:"):
            if in_block:
                flushed = _flush(current)
                if flushed is not None:
                    rtnval.append(flushed)
            current = {
                "alias": stripped[len("Certificate Name:"):].strip(),
                "domains": "",
                "expiry": "",
                "path": "",
            }
            in_block = True
            continue
        if not in_block:
            continue
        if stripped.startswith("Domains:"):
            current["domains"] = stripped[len("Domains:"):].strip()
            continue
        if stripped.startswith("Expiry Date:"):
            current["expiry"] = stripped[len("Expiry Date:"):].strip()
            continue
        if stripped.startswith("Certificate Path:"):
            current["path"] = stripped[len("Certificate Path:"):].strip()
            continue

    if in_block:
        flushed = _flush(current)
        if flushed is not None:
            rtnval.append(flushed)

    return rtnval


def _days_until(*, not_after: str) -> Optional[int]:
    """
        Parse an openssl-format ``notAfter`` string (e.g.
        ``"Jan  1 00:00:00 2030 GMT"``) and return integer days
        between now (UTC) and that time.

        :param not_after: The not_after string from
                          ``openssl x509 -enddate``.

        :returns: Integer days remaining, or ``None`` when parsing
                  fails.
    """
    if len(not_after) == 0:
        rtnval: Optional[int] = None
        return rtnval

    formats = [
        "%b %d %H:%M:%S %Y %Z",
        "%b  %d %H:%M:%S %Y %Z",
        "%b %d %H:%M:%S %Y",
    ]
    parsed: Optional[datetime] = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(not_after, fmt)
            break
        except (ValueError, Exception):
            continue

    if parsed is None:
        rtnval = None
        return rtnval

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    now = datetime.now(tz=timezone.utc)
    delta = parsed - now
    rtnval = delta.days
    return rtnval


def _format_subject(*, common_name: str) -> str:
    """
        Build an OpenSSL ``-subj`` value for a single CN.

        :param common_name: The CN.

        :returns: A string like ``"/CN=<cn>"``.
    """
    rtnval = "/CN={}".format(common_name)
    return rtnval


def _format_san_args(*, sans: list) -> list:
    """
        Build the certbot ``-d`` argument pairs for a list of SANs.

        :param sans: SAN list.

        :returns: A flat list of ``["-d", san, "-d", san, ...]``.
    """
    rtnval: list = []
    for san in sans:
        rtnval.append("-d")
        rtnval.append(san)
    return rtnval
