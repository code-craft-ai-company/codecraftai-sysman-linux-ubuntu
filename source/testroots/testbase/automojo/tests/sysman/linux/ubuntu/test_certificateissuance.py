"""
    Tests for :class:`UbuntuCertificateIssuanceManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live filesystem is touched; ``cat`` / ``ls`` / ``tee`` /
    ``cmp`` / ``openssl`` / ``certbot`` are all faked.
"""

import base64
import hashlib

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.certificateissuance import (
    UbuntuCertIssuanceActionStatus,
    UbuntuCertIssuanceManagerConfig,
    UbuntuCertIssuanceQueryError,
    UbuntuCertificateIssuanceManager,
    UbuntuCertificateRequestSpec,
    UbuntuIssuanceBackend,
    UbuntuRevocationReason,
)


# ----------------------------------------------------------------------
# Test helpers
# ----------------------------------------------------------------------


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv
        tuple.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
    calls: list = field(default_factory=list)

    def run(
        self,
        args: list,
        *,
        elevated: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        capture_output: bool = True,
    ) -> RunResult:
        call_record = {
            "args": list(args),
            "elevated": elevated,
            "stdin": stdin,
            "env": env,
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(
            exit_code=0,
            stdout="",
            stderr="",
        )
        return rtnval


def _make_pem(*, der_bytes: bytes) -> str:
    """
        Wrap raw DER bytes into a PEM CERTIFICATE envelope.
    """
    encoded = base64.b64encode(der_bytes).decode("ascii")
    body_lines: list = []
    cursor = 0
    while cursor < len(encoded):
        body_lines.append(encoded[cursor:cursor + 64])
        cursor += 64
    rtnval_lines = ["-----BEGIN CERTIFICATE-----"]
    rtnval_lines.extend(body_lines)
    rtnval_lines.append("-----END CERTIFICATE-----")
    rtnval = "\n".join(rtnval_lines) + "\n"
    return rtnval


def _expected_fingerprint(*, der_bytes: bytes) -> str:
    """
        Compute the canonical Ubuntu uppercase-with-colons fingerprint
        for the supplied DER bytes.
    """
    digest = hashlib.sha256(der_bytes).hexdigest().upper()
    chunks: list = []
    cursor = 0
    while cursor < len(digest):
        chunks.append(digest[cursor:cursor + 2])
        cursor += 2
    rtnval = ":".join(chunks)
    return rtnval


_DER_FOO = b"DER-FOO-BYTES-1234567890"
_DER_BAR = b"DER-BAR-BYTES-ABCDEFGHIJ"


def _foo_pem() -> str:
    rtnval = _make_pem(der_bytes=_DER_FOO)
    return rtnval


def _bar_pem() -> str:
    rtnval = _make_pem(der_bytes=_DER_BAR)
    return rtnval


def _foo_fp() -> str:
    rtnval = _expected_fingerprint(der_bytes=_DER_FOO)
    return rtnval


def _far_future_not_after() -> str:
    """
        Format a ``notAfter`` string ~10 years out so the renew
        threshold guard skips.
    """
    target = datetime.now(tz=timezone.utc) + timedelta(days=3650)
    rtnval = target.strftime("%b %d %H:%M:%S %Y GMT")
    return rtnval


def _near_expiry_not_after() -> str:
    """
        Format a ``notAfter`` string within a week so the renew
        threshold guard does NOT skip.
    """
    target = datetime.now(tz=timezone.utc) + timedelta(days=5)
    rtnval = target.strftime("%b %d %H:%M:%S %Y GMT")
    return rtnval


def _seed_openssl_text(
    *,
    runner: _MockCommandRunner,
    path: str,
    not_after: str,
    subject: str = "CN = example.com",
    issuer: str = "CN = Foo CA",
    sans_text: Optional[str] = None,
) -> None:
    """
        Pre-seed the runner with a canned ``openssl x509 -text`` output
        for the given path.
    """
    body_lines = [
        "subject={}".format(subject),
        "issuer={}".format(issuer),
        "notBefore=Jan 01 00:00:00 2024 GMT",
        "notAfter={}".format(not_after),
        "serial=01:23:45",
        "Certificate:",
        "    Data:",
        "        Subject Public Key Info:",
        "            Public Key Algorithm: rsaEncryption",
        "                Public-Key: (2048 bit)",
        "    X509v3 extensions:",
    ]
    if sans_text is not None:
        body_lines.append("        X509v3 Subject Alternative Name: ")
        body_lines.append("            {}".format(sans_text))
    body_lines.append("    Signature Algorithm: sha256WithRSAEncryption")
    canned = "\n".join(body_lines)
    key = (
        "openssl", "x509", "-in", path, "-noout", "-text",
        "-fingerprint", "-sha256", "-dates", "-subject", "-issuer", "-serial",
    )
    runner.results[key] = RunResult(
        exit_code=0, stdout=canned, stderr="",
    )
    return


# ----------------------------------------------------------------------
# request_certificate
# ----------------------------------------------------------------------


def test_request_certificate_invokes_certbot_with_subject():
    """
        request_certificate dispatches certbot certonly with the
        configured email, webroot, and primary domain.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/etc/letsencrypt/live/web.pem",
    )] = RunResult(exit_code=1, stdout="", stderr="")

    config = UbuntuCertIssuanceManagerConfig(
        elevated=True,
        acme_email="ops@example.com",
        webroot="/var/www/html",
    )
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    spec = UbuntuCertificateRequestSpec(
        alias="web",
        common_name="example.com",
        sans=["www.example.com"],
    )
    result = manager.request_certificate(spec=spec)

    testbase.assert_equal(result.status, UbuntuCertIssuanceActionStatus.SUCCESS)
    certbot_calls = [c for c in runner.calls if c["args"][0] == "certbot"]
    testbase.assert_equal(len(certbot_calls), 1)
    expected_argv = [
        "certbot", "certonly", "--non-interactive", "--agree-tos",
        "--email", "ops@example.com",
        "--webroot", "-w", "/var/www/html",
        "-d", "example.com",
        "-d", "www.example.com",
        "--cert-name", "web",
    ]
    testbase.assert_equal(certbot_calls[0]["args"], expected_argv)
    testbase.assert_equal(certbot_calls[0]["elevated"], True)
    return


def test_request_certificate_skipped_when_present_and_not_near_expiry():
    """
        request_certificate returns SKIPPED when the alias is already
        present and the existing cert is well clear of the renew
        threshold.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/etc/letsencrypt/live/web.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    _seed_openssl_text(
        runner=runner,
        path="/etc/letsencrypt/live/web.pem",
        not_after=_far_future_not_after(),
    )

    config = UbuntuCertIssuanceManagerConfig(
        elevated=True,
        acme_email="ops@example.com",
        webroot="/var/www/html",
        renew_threshold_days=30,
    )
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    spec = UbuntuCertificateRequestSpec(
        alias="web", common_name="example.com",
    )
    result = manager.request_certificate(spec=spec)

    testbase.assert_equal(result.status, UbuntuCertIssuanceActionStatus.SKIPPED)
    certbot_calls = [c for c in runner.calls if c["args"][0] == "certbot"]
    testbase.assert_equal(certbot_calls, [])
    return


def test_request_certificate_dry_run_short_circuits():
    """
        request_certificate with dry_run=True returns DRY_RUN and the
        runner is never invoked with certbot.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/etc/letsencrypt/live/web.pem",
    )] = RunResult(exit_code=1, stdout="", stderr="")

    config = UbuntuCertIssuanceManagerConfig(
        dry_run=True, elevated=True,
        acme_email="ops@example.com",
        webroot="/var/www/html",
    )
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    spec = UbuntuCertificateRequestSpec(
        alias="web", common_name="example.com",
    )
    result = manager.request_certificate(spec=spec)

    testbase.assert_equal(result.status, UbuntuCertIssuanceActionStatus.DRY_RUN)
    certbot_calls = [c for c in runner.calls if c["args"][0] == "certbot"]
    testbase.assert_equal(certbot_calls, [])
    testbase.assert_equal(result.command[0], "certbot")
    testbase.assert_equal(result.command[1], "certonly")
    return


def test_request_certificate_self_signed_uses_openssl():
    """
        When spec.is_self_signed=True, the openssl backend is used
        regardless of config.backend.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/etc/letsencrypt/live/dev.pem",
    )] = RunResult(exit_code=1, stdout="", stderr="")

    config = UbuntuCertIssuanceManagerConfig(
        backend=UbuntuIssuanceBackend.CERTBOT,
        acme_email="ops@example.com",
        webroot="/var/www/html",
    )
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    spec = UbuntuCertificateRequestSpec(
        alias="dev",
        common_name="dev.local",
        is_self_signed=True,
        key_length=2048,
        validity_days=90,
    )
    result = manager.request_certificate(spec=spec)

    testbase.assert_equal(result.status, UbuntuCertIssuanceActionStatus.SUCCESS)
    openssl_calls = [c for c in runner.calls if c["args"][0] == "openssl"]
    testbase.assert_equal(len(openssl_calls), 2)
    req_call = openssl_calls[0]
    testbase.assert_equal(req_call["args"][0], "openssl")
    testbase.assert_equal(req_call["args"][1], "req")
    testbase.assert_equal(req_call["args"][2], "-newkey")
    testbase.assert_equal(req_call["args"][3], "rsa:2048")
    testbase.assert_equal(req_call["args"][4], "-nodes")
    sign_call = openssl_calls[1]
    testbase.assert_equal(sign_call["args"][1], "x509")
    testbase.assert_equal(sign_call["args"][2], "-req")
    testbase.assert_equal(sign_call["args"][3], "-days")
    testbase.assert_equal(sign_call["args"][4], "90")
    return


# ----------------------------------------------------------------------
# renew_certificate
# ----------------------------------------------------------------------


def test_renew_certificate_invokes_certbot():
    """
        renew_certificate dispatches certbot renew --cert-name when the
        existing cert is near expiry.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/etc/letsencrypt/live/web.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    _seed_openssl_text(
        runner=runner,
        path="/etc/letsencrypt/live/web.pem",
        not_after=_near_expiry_not_after(),
    )

    config = UbuntuCertIssuanceManagerConfig(elevated=True)
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    result = manager.renew_certificate(alias="web")

    testbase.assert_equal(result.status, UbuntuCertIssuanceActionStatus.SUCCESS)
    certbot_calls = [c for c in runner.calls if c["args"][0] == "certbot"]
    testbase.assert_equal(len(certbot_calls), 1)
    testbase.assert_equal(
        certbot_calls[0]["args"],
        ["certbot", "renew", "--cert-name", "web", "--non-interactive"],
    )
    testbase.assert_equal(certbot_calls[0]["elevated"], True)
    return


def test_renew_certificate_skipped_when_not_near_expiry():
    """
        renew_certificate returns SKIPPED when the existing cert is
        well clear of the renew threshold.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/etc/letsencrypt/live/web.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    _seed_openssl_text(
        runner=runner,
        path="/etc/letsencrypt/live/web.pem",
        not_after=_far_future_not_after(),
    )

    config = UbuntuCertIssuanceManagerConfig(renew_threshold_days=30)
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    result = manager.renew_certificate(alias="web")

    testbase.assert_equal(result.status, UbuntuCertIssuanceActionStatus.SKIPPED)
    certbot_calls = [c for c in runner.calls if c["args"][0] == "certbot"]
    testbase.assert_equal(certbot_calls, [])
    return


# ----------------------------------------------------------------------
# revoke_certificate
# ----------------------------------------------------------------------


def test_revoke_certificate_invokes_certbot():
    """
        revoke_certificate dispatches certbot revoke --cert-name.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/etc/letsencrypt/live/web.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    _seed_openssl_text(
        runner=runner,
        path="/etc/letsencrypt/live/web.pem",
        not_after=_far_future_not_after(),
    )

    config = UbuntuCertIssuanceManagerConfig(elevated=True)
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    result = manager.revoke_certificate(alias="web")

    testbase.assert_equal(result.status, UbuntuCertIssuanceActionStatus.SUCCESS)
    certbot_calls = [c for c in runner.calls if c["args"][0] == "certbot"]
    testbase.assert_equal(len(certbot_calls), 1)
    testbase.assert_equal(
        certbot_calls[0]["args"],
        ["certbot", "revoke", "--cert-name", "web", "--non-interactive"],
    )
    return


def test_revoke_certificate_with_reason():
    """
        revoke_certificate appends --reason when the caller supplies an
        UbuntuRevocationReason.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/etc/letsencrypt/live/web.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    _seed_openssl_text(
        runner=runner,
        path="/etc/letsencrypt/live/web.pem",
        not_after=_far_future_not_after(),
    )

    config = UbuntuCertIssuanceManagerConfig(elevated=True)
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    result = manager.revoke_certificate(
        alias="web",
        reason=UbuntuRevocationReason.KEY_COMPROMISE,
    )

    testbase.assert_equal(result.status, UbuntuCertIssuanceActionStatus.SUCCESS)
    certbot_calls = [c for c in runner.calls if c["args"][0] == "certbot"]
    testbase.assert_equal(
        certbot_calls[0]["args"],
        [
            "certbot", "revoke", "--cert-name", "web",
            "--non-interactive", "--reason", "keycompromise",
        ],
    )
    return


# ----------------------------------------------------------------------
# get_certificate / list_certificates
# ----------------------------------------------------------------------


def test_get_certificate_parses_x509_output():
    """
        get_certificate parses the openssl x509 -text output into a
        populated UbuntuCertificateInfo.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/etc/letsencrypt/live/web.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    _seed_openssl_text(
        runner=runner,
        path="/etc/letsencrypt/live/web.pem",
        not_after=_far_future_not_after(),
        subject="CN = example.com",
        sans_text="DNS:example.com, DNS:www.example.com",
    )

    config = UbuntuCertIssuanceManagerConfig()
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    info = manager.get_certificate(alias="web")

    testbase.assert_not_equal(info, None)
    testbase.assert_equal(info.alias, "web")
    testbase.assert_equal(info.subject, "CN = example.com")
    testbase.assert_equal(info.fingerprint, _foo_fp())
    testbase.assert_equal(info.serial_number, "01:23:45")
    testbase.assert_equal(info.key_algorithm, "RSA")
    testbase.assert_equal(info.key_length, 2048)
    testbase.assert_equal(
        sorted(info.sans), sorted(["example.com", "www.example.com"]),
    )
    return


def test_list_certificates_returns_all():
    """
        list_certificates dispatches ``certbot certificates`` on the
        certbot backend and parses every certificate block.
    """
    canned = "\n".join([
        "Saving debug log to /var/log/letsencrypt/letsencrypt.log",
        "Found the following certs:",
        "  Certificate Name: web",
        "    Domains: example.com www.example.com",
        "    Expiry Date: 2030-01-01 00:00:00+00:00 (VALID: 1234 days)",
        "    Certificate Path: /etc/letsencrypt/live/web/fullchain.pem",
        "  Certificate Name: api",
        "    Domains: api.example.com",
        "    Expiry Date: 2031-01-01 00:00:00+00:00 (VALID: 1599 days)",
        "    Certificate Path: /etc/letsencrypt/live/api/fullchain.pem",
    ])
    runner = _MockCommandRunner()
    runner.results[("certbot", "certificates")] = RunResult(
        exit_code=0, stdout=canned, stderr="",
    )

    config = UbuntuCertIssuanceManagerConfig()
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    info_list = manager.list_certificates()

    testbase.assert_equal(len(info_list), 2)
    aliases = sorted([info.alias for info in info_list])
    testbase.assert_equal(aliases, ["api", "web"])
    return


# ----------------------------------------------------------------------
# export_certificate / import_certificate
# ----------------------------------------------------------------------


def test_export_certificate_writes_to_path():
    """
        export_certificate copies the cert via tee.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/etc/letsencrypt/live/web.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[("cat", "/tmp/web.pem")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )

    config = UbuntuCertIssuanceManagerConfig(elevated=True)
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    result = manager.export_certificate(
        alias="web", path="/tmp/web.pem", include_private_key=False,
    )

    testbase.assert_equal(result.status, UbuntuCertIssuanceActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["args"], ["tee", "/tmp/web.pem"])
    testbase.assert_equal(tee_calls[0]["stdin"], _foo_pem())
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


def test_export_with_private_key_uses_pem():
    """
        export_certificate with include_private_key=True concatenates
        the key + cert into the output bundle.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/etc/letsencrypt/live/web.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    key_pem = "-----BEGIN PRIVATE KEY-----\nFAKEKEYBYTES\n-----END PRIVATE KEY-----\n"
    runner.results[(
        "cat", "/etc/letsencrypt/live/web.key",
    )] = RunResult(exit_code=0, stdout=key_pem, stderr="")
    runner.results[("cat", "/tmp/bundle.pem")] = RunResult(
        exit_code=1, stdout="", stderr="",
    )

    config = UbuntuCertIssuanceManagerConfig(elevated=True)
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    result = manager.export_certificate(
        alias="web", path="/tmp/bundle.pem", include_private_key=True,
    )

    testbase.assert_equal(result.status, UbuntuCertIssuanceActionStatus.SUCCESS)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    expected_payload = "{}{}".format(key_pem, _foo_pem())
    testbase.assert_equal(tee_calls[0]["stdin"], expected_payload)
    return


def test_import_certificate_skipped_when_fingerprint_present():
    """
        import_certificate returns SKIPPED when the alias already maps
        to a certificate with the same fingerprint.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/tmp/web.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[(
        "cat", "/etc/letsencrypt/live/web.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")

    config = UbuntuCertIssuanceManagerConfig(elevated=True)
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    result = manager.import_certificate(
        path="/tmp/web.pem", alias="web",
    )

    testbase.assert_equal(result.status, UbuntuCertIssuanceActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    testbase.assert_equal(result.fingerprint, _foo_fp())
    return


# ----------------------------------------------------------------------
# Elevation / failure surfaces
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Mutating verbs pass elevated=True when config.elevated=True.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/etc/letsencrypt/live/web.pem",
    )] = RunResult(exit_code=1, stdout="", stderr="")

    config = UbuntuCertIssuanceManagerConfig(
        elevated=True,
        acme_email="ops@example.com",
        webroot="/var/www/html",
    )
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    spec = UbuntuCertificateRequestSpec(alias="web", common_name="example.com")
    manager.request_certificate(spec=spec)

    certbot_calls = [c for c in runner.calls if c["args"][0] == "certbot"]
    testbase.assert_equal(len(certbot_calls), 1)
    testbase.assert_equal(certbot_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        list_certificates / get_certificate never pass elevated=True
        even when config.elevated=True.
    """
    canned = "\n".join([
        "  Certificate Name: web",
        "    Domains: example.com",
        "    Expiry Date: 2030-01-01 00:00:00+00:00",
        "    Certificate Path: /etc/letsencrypt/live/web/fullchain.pem",
    ])
    runner = _MockCommandRunner()
    runner.results[("certbot", "certificates")] = RunResult(
        exit_code=0, stdout=canned, stderr="",
    )

    config = UbuntuCertIssuanceManagerConfig(elevated=True)
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    manager.list_certificates()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        When certbot exits non-zero, FAILED is returned with stderr /
        exit_code captured.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/etc/letsencrypt/live/web.pem",
    )] = RunResult(exit_code=1, stdout="", stderr="")
    runner.results[(
        "certbot", "certonly", "--non-interactive", "--agree-tos",
        "--email", "ops@example.com",
        "--webroot", "-w", "/var/www/html",
        "-d", "example.com",
        "--cert-name", "web",
    )] = RunResult(
        exit_code=2, stdout="", stderr="ACME challenge failed",
    )

    config = UbuntuCertIssuanceManagerConfig(
        elevated=True,
        acme_email="ops@example.com",
        webroot="/var/www/html",
    )
    manager = UbuntuCertificateIssuanceManager(runner=runner, config=config)
    spec = UbuntuCertificateRequestSpec(
        alias="web", common_name="example.com",
    )
    result = manager.request_certificate(spec=spec)

    testbase.assert_equal(result.status, UbuntuCertIssuanceActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 2)
    testbase.assert_equal(result.stderr, "ACME challenge failed")
    return


def test_query_error_exception_is_constructible():
    """
        UbuntuCertIssuanceQueryError is constructible with operation +
        message and stringifies cleanly.
    """
    err = UbuntuCertIssuanceQueryError(
        operation="list_certificates",
        message="parser failed",
    )
    rendered = str(err)
    testbase.assert_equal("list_certificates" in rendered, True)
    testbase.assert_equal("parser failed" in rendered, True)
    return
