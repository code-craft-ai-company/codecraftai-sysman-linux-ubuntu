"""
    Tests for :class:`UbuntuCertificateTrustManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live filesystem is touched; ``cat`` / ``ls`` / ``tee`` /
    ``rm`` / ``openssl`` / ``update-ca-certificates`` are all faked.
"""

import base64
import hashlib

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.certificatetrustmanagement import (
    UbuntuCertificateTrustManager,
    UbuntuCertTrustActionStatus,
    UbuntuCertTrustManagerConfig,
    UbuntuCertTrustQueryError,
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


def _bar_fp() -> str:
    rtnval = _expected_fingerprint(der_bytes=_DER_BAR)
    return rtnval


# ----------------------------------------------------------------------
# add_trusted_cert
# ----------------------------------------------------------------------


def test_add_trusted_cert_invokes_tee():
    """
        add_trusted_cert writes the PEM to the system-ca dir via tee.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/tmp/foo.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[(
        "ls", "-1", "/usr/local/share/ca-certificates",
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuCertificateTrustManager(
        runner=runner,
        config=UbuntuCertTrustManagerConfig(elevated=True),
    )
    result = manager.add_trusted_cert(
        store="system-ca", path="/tmp/foo.pem",
    )

    testbase.assert_equal(result.status, UbuntuCertTrustActionStatus.SUCCESS)
    testbase.assert_equal(result.fingerprint, _foo_fp())
    testbase.assert_equal(
        result.path, "/usr/local/share/ca-certificates/foo.crt",
    )
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(
        tee_calls[0]["args"],
        ["tee", "/usr/local/share/ca-certificates/foo.crt"],
    )
    testbase.assert_equal(tee_calls[0]["stdin"], _foo_pem())
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


def test_add_trusted_cert_skipped_when_fingerprint_present():
    """
        add_trusted_cert returns SKIPPED when an existing file in the
        store has the same fingerprint.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/tmp/foo.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[(
        "ls", "-1", "/usr/local/share/ca-certificates",
    )] = RunResult(exit_code=0, stdout="existing.crt\n", stderr="")
    runner.results[(
        "cat", "/usr/local/share/ca-certificates/existing.crt",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")

    manager = UbuntuCertificateTrustManager(
        runner=runner,
        config=UbuntuCertTrustManagerConfig(),
    )
    result = manager.add_trusted_cert(
        store="system-ca", path="/tmp/foo.pem",
    )

    testbase.assert_equal(result.status, UbuntuCertTrustActionStatus.SKIPPED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    testbase.assert_equal(result.fingerprint, _foo_fp())
    testbase.assert_equal(
        result.path, "/usr/local/share/ca-certificates/existing.crt",
    )
    return


def test_add_trusted_cert_dry_run_short_circuits():
    """
        add_trusted_cert with dry_run=True returns DRY_RUN and does NOT
        invoke the tee write.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/tmp/foo.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[(
        "ls", "-1", "/usr/local/share/ca-certificates",
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuCertificateTrustManager(
        runner=runner,
        config=UbuntuCertTrustManagerConfig(dry_run=True, elevated=True),
    )
    result = manager.add_trusted_cert(
        store="system-ca", path="/tmp/foo.pem",
    )

    testbase.assert_equal(result.status, UbuntuCertTrustActionStatus.DRY_RUN)
    testbase.assert_equal(
        result.command,
        ["tee", "/usr/local/share/ca-certificates/foo.crt"],
    )
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


def test_add_trusted_cert_invalid_pem_returns_failed():
    """
        A source file that does not contain a CERTIFICATE block returns
        FAILED and dispatches no tee call.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "cat", "/tmp/garbage.pem",
    )] = RunResult(exit_code=0, stdout="this is not a cert", stderr="")

    manager = UbuntuCertificateTrustManager(
        runner=runner, config=UbuntuCertTrustManagerConfig(elevated=True),
    )
    result = manager.add_trusted_cert(
        store="system-ca", path="/tmp/garbage.pem",
    )

    testbase.assert_equal(result.status, UbuntuCertTrustActionStatus.FAILED)
    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(tee_calls, [])
    return


# ----------------------------------------------------------------------
# remove_trusted_cert
# ----------------------------------------------------------------------


def test_remove_trusted_cert_invokes_rm():
    """
        remove_trusted_cert deletes the matching file via rm -f.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "ls", "-1", "/usr/local/share/ca-certificates",
    )] = RunResult(exit_code=0, stdout="foo.crt\n", stderr="")
    runner.results[(
        "cat", "/usr/local/share/ca-certificates/foo.crt",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")

    manager = UbuntuCertificateTrustManager(
        runner=runner,
        config=UbuntuCertTrustManagerConfig(elevated=True),
    )
    result = manager.remove_trusted_cert(
        store="system-ca", fingerprint=_foo_fp(),
    )

    testbase.assert_equal(result.status, UbuntuCertTrustActionStatus.SUCCESS)
    rm_calls = [c for c in runner.calls if c["args"][0] == "rm"]
    testbase.assert_equal(len(rm_calls), 1)
    testbase.assert_equal(
        rm_calls[0]["args"],
        ["rm", "-f", "/usr/local/share/ca-certificates/foo.crt"],
    )
    testbase.assert_equal(rm_calls[0]["elevated"], True)
    return


def test_remove_trusted_cert_skipped_when_absent():
    """
        remove_trusted_cert returns SKIPPED when no file in the store
        has the requested fingerprint.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "ls", "-1", "/usr/local/share/ca-certificates",
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuCertificateTrustManager(
        runner=runner, config=UbuntuCertTrustManagerConfig(),
    )
    result = manager.remove_trusted_cert(
        store="system-ca", fingerprint=_foo_fp(),
    )

    testbase.assert_equal(result.status, UbuntuCertTrustActionStatus.SKIPPED)
    rm_calls = [c for c in runner.calls if c["args"][0] == "rm"]
    testbase.assert_equal(rm_calls, [])
    return


# ----------------------------------------------------------------------
# list_trusted_certs / get_cert_details
# ----------------------------------------------------------------------


def _seed_openssl_text(*, runner: _MockCommandRunner, path: str) -> None:
    """
        Pre-seed the runner with a canned ``openssl x509 -text`` payload
        for the given path.
    """
    canned_stdout = "\n".join([
        "subject=CN = Foo CA, O = Foo Inc",
        "issuer=CN = Foo Root, O = Foo Inc",
        "notBefore=Jan 01 00:00:00 2024 GMT",
        "notAfter=Jan 01 00:00:00 2034 GMT",
        "serial=01:23:45",
        "Certificate:",
        "    X509v3 extensions:",
        "        X509v3 Extended Key Usage:",
        "            TLS Web Server Authentication, TLS Web Client Authentication",
        "    Signature Algorithm: sha256WithRSAEncryption",
    ])
    key = (
        "openssl", "x509", "-in", path,
        "-noout", "-subject", "-issuer", "-startdate", "-enddate",
        "-serial", "-text",
    )
    runner.results[key] = RunResult(
        exit_code=0,
        stdout=canned_stdout,
        stderr="",
    )
    return


def test_list_trusted_certs_parses_output():
    """
        list_trusted_certs enumerates the store directory and parses
        each PEM file's metadata.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "ls", "-1", "/usr/local/share/ca-certificates",
    )] = RunResult(exit_code=0, stdout="foo.crt\nbar.crt\n", stderr="")
    runner.results[(
        "cat", "/usr/local/share/ca-certificates/foo.crt",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[(
        "cat", "/usr/local/share/ca-certificates/bar.crt",
    )] = RunResult(exit_code=0, stdout=_bar_pem(), stderr="")
    _seed_openssl_text(
        runner=runner, path="/usr/local/share/ca-certificates/foo.crt",
    )
    _seed_openssl_text(
        runner=runner, path="/usr/local/share/ca-certificates/bar.crt",
    )

    manager = UbuntuCertificateTrustManager(
        runner=runner, config=UbuntuCertTrustManagerConfig(),
    )
    info_list = manager.list_trusted_certs(store="system-ca")

    testbase.assert_equal(len(info_list), 2)
    fingerprints = sorted([info.fingerprint for info in info_list])
    expected = sorted([_foo_fp(), _bar_fp()])
    testbase.assert_equal(fingerprints, expected)
    testbase.assert_equal(info_list[0].subject, "CN = Foo CA, O = Foo Inc")
    testbase.assert_equal(
        info_list[0].purposes,
        ["TLS Web Server Authentication", "TLS Web Client Authentication"],
    )
    return


def test_get_cert_details_returns_full_info():
    """
        get_cert_details returns a populated UbuntuCertTrustInfo when the
        fingerprint is known.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "ls", "-1", "/usr/local/share/ca-certificates",
    )] = RunResult(exit_code=0, stdout="foo.crt\n", stderr="")
    runner.results[(
        "cat", "/usr/local/share/ca-certificates/foo.crt",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    _seed_openssl_text(
        runner=runner, path="/usr/local/share/ca-certificates/foo.crt",
    )

    manager = UbuntuCertificateTrustManager(
        runner=runner, config=UbuntuCertTrustManagerConfig(),
    )
    info = manager.get_cert_details(fingerprint=_foo_fp())

    testbase.assert_not_equal(info, None)
    testbase.assert_equal(info.fingerprint, _foo_fp())
    testbase.assert_equal(info.serial_number, "01:23:45")
    return


def test_get_cert_details_unknown_fingerprint_returns_none():
    """
        get_cert_details returns None when no certificate has the
        requested fingerprint.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "ls", "-1", "/usr/local/share/ca-certificates",
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuCertificateTrustManager(
        runner=runner, config=UbuntuCertTrustManagerConfig(),
    )
    info = manager.get_cert_details(fingerprint=_foo_fp())

    testbase.assert_equal(info, None)
    return


# ----------------------------------------------------------------------
# update_trust_store
# ----------------------------------------------------------------------


def test_update_trust_store_invokes_update_ca_certificates():
    """
        update_trust_store dispatches ``update-ca-certificates``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )

    manager = UbuntuCertificateTrustManager(
        runner=runner,
        config=UbuntuCertTrustManagerConfig(elevated=True),
    )
    result = manager.update_trust_store()

    testbase.assert_equal(result.status, UbuntuCertTrustActionStatus.SUCCESS)
    update_calls = [
        c for c in runner.calls
        if c["args"][0] == "update-ca-certificates"
    ]
    testbase.assert_equal(len(update_calls), 1)
    testbase.assert_equal(update_calls[0]["elevated"], True)
    return


# ----------------------------------------------------------------------
# Store routing / elevation / errors
# ----------------------------------------------------------------------


def test_unsupported_store_returns_failed():
    """
        A store name not in the supported map (or mapped to None)
        returns FAILED with a descriptive detail.
    """
    runner = _MockCommandRunner()
    manager = UbuntuCertificateTrustManager(
        runner=runner, config=UbuntuCertTrustManagerConfig(),
    )
    result = manager.add_trusted_cert(
        store="user-ca", path="/tmp/foo.pem",
    )

    testbase.assert_equal(result.status, UbuntuCertTrustActionStatus.FAILED)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_unsupported_store_in_list_raises():
    """
        list_trusted_certs raises UbuntuCertTrustQueryError for an
        unsupported store name.
    """
    runner = _MockCommandRunner()
    manager = UbuntuCertificateTrustManager(
        runner=runner, config=UbuntuCertTrustManagerConfig(),
    )
    raised = False
    try:
        manager.list_trusted_certs(store="user-ca")
    except UbuntuCertTrustQueryError:
        raised = True
    testbase.assert_equal(raised, True)
    return


def test_writes_pass_elevated_true_for_system_store():
    """
        With elevated=True, the system-ca write dispatches with
        elevated=True.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "cat", "/tmp/foo.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[(
        "ls", "-1", "/usr/local/share/ca-certificates",
    )] = RunResult(exit_code=0, stdout="", stderr="")

    manager = UbuntuCertificateTrustManager(
        runner=runner,
        config=UbuntuCertTrustManagerConfig(elevated=True),
    )
    manager.add_trusted_cert(store="system-ca", path="/tmp/foo.pem")

    tee_calls = [c for c in runner.calls if c["args"][0] == "tee"]
    testbase.assert_equal(len(tee_calls), 1)
    testbase.assert_equal(tee_calls[0]["elevated"], True)
    return


def test_user_store_writes_do_not_elevate():
    """
        Even with elevated=True in the config, an attempt against
        user-ca short-circuits to FAILED before any runner call (since
        Ubuntu does not support a user store).  This proves no
        elevated dispatch leaks for user-ca.
    """
    runner = _MockCommandRunner()
    manager = UbuntuCertificateTrustManager(
        runner=runner,
        config=UbuntuCertTrustManagerConfig(elevated=True),
    )
    result = manager.add_trusted_cert(store="user-ca", path="/tmp/foo.pem")

    testbase.assert_equal(result.status, UbuntuCertTrustActionStatus.FAILED)
    testbase.assert_equal(len(runner.calls), 0)
    return


def test_reads_never_elevate():
    """
        list_trusted_certs / get_cert_details never pass elevated=True
        to the runner.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "ls", "-1", "/usr/local/share/ca-certificates",
    )] = RunResult(exit_code=0, stdout="foo.crt\n", stderr="")
    runner.results[(
        "cat", "/usr/local/share/ca-certificates/foo.crt",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    _seed_openssl_text(
        runner=runner, path="/usr/local/share/ca-certificates/foo.crt",
    )

    manager = UbuntuCertificateTrustManager(
        runner=runner,
        config=UbuntuCertTrustManagerConfig(elevated=True),
    )
    manager.list_trusted_certs(store="system-ca")

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


def test_failed_command_surfaces_stderr():
    """
        When the tee write returns non-zero, the result is FAILED with
        stderr / exit_code captured.
    """
    runner = _MockCommandRunner()
    runner.results[(
        "cat", "/tmp/foo.pem",
    )] = RunResult(exit_code=0, stdout=_foo_pem(), stderr="")
    runner.results[(
        "ls", "-1", "/usr/local/share/ca-certificates",
    )] = RunResult(exit_code=0, stdout="", stderr="")
    runner.results[(
        "tee", "/usr/local/share/ca-certificates/foo.crt",
    )] = RunResult(exit_code=1, stdout="", stderr="permission denied")

    manager = UbuntuCertificateTrustManager(
        runner=runner,
        config=UbuntuCertTrustManagerConfig(elevated=True),
    )
    result = manager.add_trusted_cert(
        store="system-ca", path="/tmp/foo.pem",
    )

    testbase.assert_equal(result.status, UbuntuCertTrustActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 1)
    testbase.assert_equal(result.stderr, "permission denied")
    return
