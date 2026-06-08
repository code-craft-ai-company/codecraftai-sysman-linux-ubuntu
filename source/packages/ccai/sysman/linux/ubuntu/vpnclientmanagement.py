"""
    Ubuntu vpn-client-management feature.

    Implements the cross-OS vpn-client-management contract for Ubuntu
    using the official ``pkgs.tailscale.com`` apt repository for the
    Tailscale client and the ``packages.twingate.com`` apt repository
    for the Twingate Connector.  ``systemctl`` controls the daemons.

    See:
        * :doc:`/specs/contracts/vpn-client-management/contract.md`
        * :doc:`/specs/vpn-client-management-base/design.md`
        * :doc:`/specs/3-vpn-client-management/design.md`

    All commands route through :class:`ICommandRunner`.  No direct
    subprocess use, no direct filesystem writes.
"""

import json
import os
import re

from dataclasses import dataclass
from typing import Dict, List, Optional

from ccai.aspects.aspects import Aspects
from ccai.interfaces.icommandrunner import ICommandRunner, RunResult

from ccai.sysman.common.vpnactionresult import VPNActionResult
from ccai.sysman.common.vpnactionstatus import VPNActionStatus
from ccai.sysman.common.vpnclientmanagementbase import (
    VERB_KIND_WRITE,
    VPNClientManagementBase,
)
from ccai.sysman.common.vpnclientmanagerconfig import VPNClientManagerConfig
from ccai.sysman.common.vpnclientqueryerror import VPNClientQueryError
from ccai.sysman.common.vpnclientstatus import VPNClientStatus
from ccai.sysman.common.vpnprovider import VPNProvider


UbuntuVPNActionStatus = VPNActionStatus
UbuntuVPNActionResult = VPNActionResult
UbuntuVPNClientStatus = VPNClientStatus
UbuntuVPNClientQueryError = VPNClientQueryError


_TAILSCALE_PKG = "tailscale"
_TWINGATE_PKG = "twingate-connector"

# The Twingate Connector deb ships a systemd unit whose name happens to
# match the package name.  It is named explicitly here (rather than
# reusing ``_TWINGATE_PKG`` directly at the call sites) so the
# package-to-unit coupling is a documented, single-point decision.
_TWINGATE_SYSTEMD_UNIT = "twingate-connector"

# Permitted shapes for values that get interpolated into repo URLs and
# apt argv tokens.  These are deliberately strict allow-lists: a value
# that does not match is rejected outright rather than quoted, because
# no legitimate channel / codename / version contains anything outside
# this set.
_CHANNEL_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")
_CODENAME_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")
_VERSION_RE = re.compile(r"\A[A-Za-z0-9.:+~_-]+\Z")

_DEFAULT_TAILSCALE_CHANNEL = "stable"
_DEFAULT_KEYRING_PATH = "/usr/share/keyrings/tailscale-archive-keyring.gpg"
_DEFAULT_SOURCES_PATH = "/etc/apt/sources.list.d/tailscale.list"
_DEFAULT_TWINGATE_SOURCES_PATH = "/etc/apt/sources.list.d/twingate.list"
_DEFAULT_TWINGATE_CONFIG_PATH = "/etc/twingate/connector.conf"
_DEFAULT_TWINGATE_KEYRING_PATH = (
    "/usr/share/keyrings/twingate-archive-keyring.gpg"
)
_TWINGATE_KEYRING_URL = "https://packages.twingate.com/apt/gpg.key"


@dataclass
class UbuntuVPNClientManagerConfig(VPNClientManagerConfig):
    """
        Ubuntu-specific manager configuration.

        Inherits ``dry_run`` and ``elevated`` from
        :class:`VPNClientManagerConfig` (per the contract) and adds the
        repository / configuration path fields the Ubuntu implementation
        needs.

        :param tailscale_channel: apt channel suffix (``"stable"`` /
                                  ``"unstable"``).
        :param keyring_path: On-disk path for the Tailscale signing
                             key.
        :param sources_path: On-disk path for the Tailscale apt sources
                             file.
        :param twingate_sources_path: On-disk path for the Twingate apt
                                      sources file.
        :param twingate_config_path: On-disk path for the Twingate
                                     Connector configuration file.
        :param ubuntu_codename: Optional override for the Ubuntu
                                codename used in repo URLs (defaults to
                                ``"jammy"`` when not provided).
    """
    tailscale_channel: str = _DEFAULT_TAILSCALE_CHANNEL
    keyring_path: str = _DEFAULT_KEYRING_PATH
    sources_path: str = _DEFAULT_SOURCES_PATH
    twingate_sources_path: str = _DEFAULT_TWINGATE_SOURCES_PATH
    twingate_config_path: str = _DEFAULT_TWINGATE_CONFIG_PATH
    twingate_keyring_path: str = _DEFAULT_TWINGATE_KEYRING_PATH
    ubuntu_codename: str = "jammy"


class UbuntuVPNClientManager(VPNClientManagementBase):
    """
        Ubuntu concrete implementation of the vpn-client-management
        contract.

        Supports the Tailscale and Twingate providers.  Every command
        is dispatched through the injected :class:`ICommandRunner`.
    """

    feature_name: Optional[str] = "vpn-client-management"

    _SUPPORTED_PROVIDERS = (VPNProvider.TAILSCALE, VPNProvider.TWINGATE)

    def __init__(
        self,
        *,
        runner: ICommandRunner,
        config: Optional[UbuntuVPNClientManagerConfig] = None,
    ) -> None:
        """
            :param runner: Injected command runner.
            :param config: Ubuntu manager configuration.  Defaults to a
                           fresh :class:`UbuntuVPNClientManagerConfig`
                           when omitted.
        """
        if config is None:
            ubuntu_config = UbuntuVPNClientManagerConfig()
        else:
            ubuntu_config = config

        super().__init__(runner=runner, config=ubuntu_config)
        self._ubuntu_config: UbuntuVPNClientManagerConfig = ubuntu_config
        return

    # ------------------------------------------------------------------
    # Public verbs — installs
    # ------------------------------------------------------------------

    def install_tailscale(
        self,
        *,
        version: Optional[str],
        channel: str,
        aspects: Aspects,
    ) -> UbuntuVPNActionResult:
        """
            Install the Tailscale client via the official apt repo.

            :param version: Specific package version, or ``None``.
            :param channel: Tailscale release channel
                            (``"stable"``, ``"unstable"``, ...).
            :param aspects: Behaviour controls.

            :returns: The :class:`UbuntuVPNActionResult`.
        """
        if version is not None and len(version) > 0:
            _validate_token(name="version", value=version, pattern=_VERSION_RE)
            install_arg = "{}={}".format(_TAILSCALE_PKG, version)
        else:
            install_arg = _TAILSCALE_PKG
        install_command = [
            "apt-get", "install", "-y", install_arg,
        ]

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TAILSCALE,
            command=install_command,
            detail="install_tailscale",
        )
        if dry is not None:
            return dry

        installed_version = self._dpkg_installed_version(pkg=_TAILSCALE_PKG)
        if installed_version is not None:
            requested_match = (
                version is None
                or len(version) == 0
                or installed_version == version
            )
            if requested_match is True:
                rtnval = UbuntuVPNActionResult(
                    status=VPNActionStatus.SKIPPED,
                    provider=VPNProvider.TAILSCALE,
                    detail="tailscale already installed at {}".format(
                        installed_version,
                    ),
                    exit_code=None,
                    stdout=None,
                    stderr=None,
                    command=install_command,
                )
                return rtnval

        repo_result = self._ensure_tailscale_repo(
            channel=channel,
            aspects=aspects,
        )
        if repo_result.status == VPNActionStatus.FAILED:
            return repo_result

        rtnval = self._dispatch_write(
            provider=VPNProvider.TAILSCALE,
            command=install_command,
            detail="apt-get install tailscale",
            aspects=aspects,
        )
        return rtnval

    def install_twingate_connector(
        self,
        *,
        version: Optional[str],
        aspects: Aspects,
    ) -> UbuntuVPNActionResult:
        """
            Install the Twingate Connector via the official apt repo.

            :param version: Specific package version, or ``None``.
            :param aspects: Behaviour controls.

            :returns: The :class:`UbuntuVPNActionResult`.
        """
        if version is not None and len(version) > 0:
            _validate_token(name="version", value=version, pattern=_VERSION_RE)
            install_arg = "{}={}".format(_TWINGATE_PKG, version)
        else:
            install_arg = _TWINGATE_PKG
        install_command = [
            "apt-get", "install", "-y", install_arg,
        ]

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TWINGATE,
            command=install_command,
            detail="install_twingate_connector",
        )
        if dry is not None:
            return dry

        installed_version = self._dpkg_installed_version(pkg=_TWINGATE_PKG)
        if installed_version is not None:
            requested_match = (
                version is None
                or len(version) == 0
                or installed_version == version
            )
            if requested_match is True:
                rtnval = UbuntuVPNActionResult(
                    status=VPNActionStatus.SKIPPED,
                    provider=VPNProvider.TWINGATE,
                    detail="twingate-connector already installed at {}".format(
                        installed_version,
                    ),
                    exit_code=None,
                    stdout=None,
                    stderr=None,
                    command=install_command,
                )
                return rtnval

        repo_result = self._ensure_twingate_repo(aspects=aspects)
        if repo_result.status == VPNActionStatus.FAILED:
            return repo_result

        rtnval = self._dispatch_write(
            provider=VPNProvider.TWINGATE,
            command=install_command,
            detail="apt-get install twingate-connector",
            aspects=aspects,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Public verbs — configure
    # ------------------------------------------------------------------

    def configure(
        self,
        *,
        provider: VPNProvider,
        settings: Dict[str, str],
        aspects: Aspects,
    ) -> UbuntuVPNActionResult:
        """
            Write provider-specific configuration.

            Tailscale has no on-disk configure step on Ubuntu (all
            configuration is supplied to ``tailscale up``); calls with
            ``provider=TAILSCALE`` return ``SKIPPED``.

            :param provider: Target provider.
            :param settings: Provider-specific settings.
            :param aspects: Behaviour controls.

            :returns: The :class:`UbuntuVPNActionResult`.
        """
        validation = self._validate_provider(
            provider=provider,
            supported=self._SUPPORTED_PROVIDERS,
        )
        if validation is not None:
            return validation

        if provider == VPNProvider.TAILSCALE:
            rtnval = UbuntuVPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=VPNProvider.TAILSCALE,
                detail="tailscale has no on-disk configure step on ubuntu",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=None,
            )
            return rtnval

        rendered = _render_twingate_conf(settings=settings)
        config_path = self._ubuntu_config.twingate_config_path
        tee_command = ["tee", config_path]

        dry = self._short_circuit_dry_run(
            provider=VPNProvider.TWINGATE,
            command=tee_command,
            detail="configure twingate",
        )
        if dry is not None:
            return dry

        existing = self._read_file_via_runner(path=config_path)
        if existing is not None and existing == rendered:
            rtnval = UbuntuVPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=VPNProvider.TWINGATE,
                detail="twingate configuration already matches",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=tee_command,
            )
            return rtnval

        run_elevated = self._should_elevate(kind=VERB_KIND_WRITE)
        run_result: RunResult = self._runner.run(
            list(tee_command),
            elevated=run_elevated,
            stdin=rendered,
        )

        if run_result.exit_code == 0:
            mapped_status = VPNActionStatus.SUCCESS
        else:
            mapped_status = VPNActionStatus.FAILED

        rtnval = UbuntuVPNActionResult(
            status=mapped_status,
            provider=VPNProvider.TWINGATE,
            detail="wrote {}".format(config_path),
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=tee_command,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Public verbs — bring up / bring down
    # ------------------------------------------------------------------

    def bring_up(
        self,
        *,
        provider: VPNProvider,
        authkey: str,
        hostname: Optional[str],
        advertise_tags: Optional[List[str]],
        advertise_routes: Optional[List[str]],
        exit_node: bool,
        aspects: Aspects,
    ) -> UbuntuVPNActionResult:
        """
            Bring the mesh-VPN tunnel up.

            :param provider: Target provider.
            :param authkey: Vendor-issued authentication key.
            :param hostname: Hostname to register, or ``None``.
            :param advertise_tags: Tags to advertise, or ``None``.
            :param advertise_routes: Routes to advertise, or ``None``.
            :param exit_node: Whether to advertise as an exit node.
            :param aspects: Behaviour controls.

            :returns: The :class:`UbuntuVPNActionResult`.
        """
        validation = self._validate_provider(
            provider=provider,
            supported=self._SUPPORTED_PROVIDERS,
        )
        if validation is not None:
            return validation

        if provider == VPNProvider.TAILSCALE:
            command = _build_tailscale_up_command(
                authkey=authkey,
                hostname=hostname,
                advertise_tags=advertise_tags,
                advertise_routes=advertise_routes,
                exit_node=exit_node,
            )
        else:
            command = ["systemctl", "enable", "--now", _TWINGATE_SYSTEMD_UNIT]

        dry = self._short_circuit_dry_run(
            provider=provider,
            command=command,
            detail="bring_up",
        )
        if dry is not None:
            return dry

        current_status = self._status_quiet(provider=provider, aspects=aspects)
        if current_status is not None and current_status.running is True:
            hostname_match = (
                hostname is None or current_status.hostname == hostname
            )
            if hostname_match is True:
                rtnval = UbuntuVPNActionResult(
                    status=VPNActionStatus.SKIPPED,
                    provider=provider,
                    detail="tunnel already up",
                    exit_code=None,
                    stdout=None,
                    stderr=None,
                    command=command,
                )
                return rtnval

        rtnval = self._dispatch_write(
            provider=provider,
            command=command,
            detail="bring_up",
            aspects=aspects,
        )
        return rtnval

    def bring_down(
        self,
        *,
        provider: VPNProvider,
        aspects: Aspects,
    ) -> UbuntuVPNActionResult:
        """
            Bring the mesh-VPN tunnel down.

            :param provider: Target provider.
            :param aspects: Behaviour controls.

            :returns: The :class:`UbuntuVPNActionResult`.
        """
        validation = self._validate_provider(
            provider=provider,
            supported=self._SUPPORTED_PROVIDERS,
        )
        if validation is not None:
            return validation

        if provider == VPNProvider.TAILSCALE:
            command = ["tailscale", "down"]
        else:
            command = ["systemctl", "stop", _TWINGATE_SYSTEMD_UNIT]

        dry = self._short_circuit_dry_run(
            provider=provider,
            command=command,
            detail="bring_down",
        )
        if dry is not None:
            return dry

        current_status = self._status_quiet(provider=provider, aspects=aspects)
        if current_status is not None and current_status.running is False:
            rtnval = UbuntuVPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=provider,
                detail="tunnel already down",
                exit_code=None,
                stdout=None,
                stderr=None,
                command=command,
            )
            return rtnval

        rtnval = self._dispatch_write(
            provider=provider,
            command=command,
            detail="bring_down",
            aspects=aspects,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Public verbs — status
    # ------------------------------------------------------------------

    def status(
        self,
        *,
        provider: VPNProvider,
        aspects: Aspects,
    ) -> UbuntuVPNClientStatus:
        """
            Report the local daemon state.

            :param provider: Target provider.
            :param aspects: Behaviour controls.

            :returns: The :class:`UbuntuVPNClientStatus`.
            :raises UbuntuVPNClientQueryError: When the underlying query
                fails for reasons other than "not installed" or "not
                running".
        """
        validation = self._validate_provider(
            provider=provider,
            supported=self._SUPPORTED_PROVIDERS,
        )
        if validation is not None:
            err_msg_lines = ["Unsupported provider for status()."]
            err_msg_lines.append("    PROVIDED: {!r}".format(provider.value))
            err_msg = os.linesep.join(err_msg_lines)
            raise UbuntuVPNClientQueryError(
                message=err_msg,
                exit_code=None,
                stderr=None,
            )

        installed_version = self._dpkg_installed_version(
            pkg=_provider_pkg(provider=provider),
        )
        if installed_version is None:
            rtnval = UbuntuVPNClientStatus(
                provider=provider,
                installed=False,
                running=False,
            )
            return rtnval

        if provider == VPNProvider.TAILSCALE:
            command = ["tailscale", "status", "--json"]
        else:
            command = ["twingate", "status"]

        run_result: RunResult = self._runner.run(
            list(command),
            elevated=False,
        )

        if run_result.exit_code != 0:
            if provider == VPNProvider.TAILSCALE:
                stderr_text = run_result.stderr or ""
                stopped_signal = (
                    "stopped" in stderr_text.lower()
                    or "needslogin" in stderr_text.lower()
                )
                if stopped_signal is True:
                    rtnval = UbuntuVPNClientStatus(
                        provider=provider,
                        installed=True,
                        running=False,
                        version=installed_version,
                    )
                    return rtnval

            err_msg_lines = ["VPN client status command failed."]
            err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
            err_msg_lines.append("    EXIT_CODE: {}".format(run_result.exit_code))
            err_msg_lines.append("    STDERR: {}".format(run_result.stderr))
            err_msg = os.linesep.join(err_msg_lines)
            raise UbuntuVPNClientQueryError(
                message=err_msg,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        if provider == VPNProvider.TAILSCALE:
            rtnval = _parse_tailscale_status_json(
                payload=run_result.stdout,
                installed_version_fallback=installed_version,
            )
        else:
            rtnval = _parse_twingate_status_text(
                payload=run_result.stdout,
                installed_version_fallback=installed_version,
            )
        return rtnval

    # ------------------------------------------------------------------
    # Public verbs — uninstall
    # ------------------------------------------------------------------

    def uninstall(
        self,
        *,
        provider: VPNProvider,
        purge: bool,
        aspects: Aspects,
    ) -> UbuntuVPNActionResult:
        """
            Uninstall the provider daemon.

            :param provider: Target provider.
            :param purge: When True, also ``apt-get purge`` and remove
                          the apt sources / keyring files.
            :param aspects: Behaviour controls.

            :returns: The :class:`UbuntuVPNActionResult`.
        """
        validation = self._validate_provider(
            provider=provider,
            supported=self._SUPPORTED_PROVIDERS,
        )
        if validation is not None:
            return validation

        pkg = _provider_pkg(provider=provider)
        if purge is True:
            primary_command = ["apt-get", "purge", "-y", pkg]
        else:
            primary_command = ["apt-get", "remove", "-y", pkg]

        dry = self._short_circuit_dry_run(
            provider=provider,
            command=primary_command,
            detail="uninstall",
        )
        if dry is not None:
            return dry

        installed_version = self._dpkg_installed_version(pkg=pkg)
        if installed_version is None:
            rtnval = UbuntuVPNActionResult(
                status=VPNActionStatus.SKIPPED,
                provider=provider,
                detail="{} not installed".format(pkg),
                exit_code=None,
                stdout=None,
                stderr=None,
                command=primary_command,
            )
            return rtnval

        primary_result = self._dispatch_write(
            provider=provider,
            command=primary_command,
            detail="apt-get remove/purge",
            aspects=aspects,
        )
        if primary_result.status == VPNActionStatus.FAILED:
            return primary_result

        if purge is True:
            cleanup_paths = _purge_cleanup_paths(
                provider=provider,
                config=self._ubuntu_config,
            )
            for path in cleanup_paths:
                rm_command = ["rm", "-f", path]
                rm_result = self._dispatch_write(
                    provider=provider,
                    command=rm_command,
                    detail="rm {}".format(path),
                    aspects=aspects,
                )
                if rm_result.status == VPNActionStatus.FAILED:
                    return rm_result

        return primary_result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _dpkg_installed_version(self, *, pkg: str) -> Optional[str]:
        """
            Return the installed version of ``pkg`` or ``None`` when the
            package is not installed.

            :param pkg: Package name to query.

            :returns: The installed version string, or ``None``.
        """
        command = [
            "dpkg-query", "-W", "-f=${Status} ${Version}", pkg,
        ]
        run_result: RunResult = self._runner.run(
            list(command),
            elevated=False,
        )
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval

        stdout = run_result.stdout or ""
        stripped = stdout.strip()
        if len(stripped) == 0:
            rtnval = None
            return rtnval

        # dpkg-query "${Status} ${Version}" yields three status tokens
        # followed by the version, e.g. "install ok installed 1.62.1".
        tokens = stripped.split()
        if len(tokens) < 4:
            rtnval = None
            return rtnval

        status_phrase = " ".join(tokens[:3])
        if status_phrase != "install ok installed":
            rtnval = None
            return rtnval

        version_value = tokens[3]
        rtnval = version_value
        return rtnval

    def _read_file_via_runner(self, *, path: str) -> Optional[str]:
        """
            Read a file's contents via ``cat`` through the runner.

            :param path: Absolute path to the file.

            :returns: The file contents, or ``None`` if the read failed
                      (typically because the file does not yet exist).
        """
        command = ["cat", path]
        run_result: RunResult = self._runner.run(
            list(command),
            elevated=False,
        )
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _ensure_tailscale_repo(
        self,
        *,
        channel: str,
        aspects: Aspects,
    ) -> UbuntuVPNActionResult:
        """
            Ensure the Tailscale apt keyring and sources file exist and
            run ``apt-get update``.

            Each step routes through the runner with elevation honoured
            via ``_should_elevate``.

            :param channel: Tailscale release channel.
            :param aspects: Behaviour controls.

            :returns: A :class:`UbuntuVPNActionResult` describing the
                      cumulative outcome.  Returns ``SUCCESS`` when all
                      steps succeed.
        """
        codename = self._ubuntu_config.ubuntu_codename
        keyring_path = self._ubuntu_config.keyring_path
        sources_path = self._ubuntu_config.sources_path

        # Reject any channel / codename carrying shell-metacharacters or
        # URL-altering characters before they reach a repo URL.  This
        # closes the former ``bash -c`` injection vector at the source.
        _validate_token(name="channel", value=channel, pattern=_CHANNEL_RE)
        _validate_token(name="codename", value=codename, pattern=_CODENAME_RE)

        run_elevated = self._should_elevate(kind=VERB_KIND_WRITE)

        keyring_url = (
            "https://pkgs.tailscale.com/{}/ubuntu/{}.noarmor.gpg".format(
                channel, codename,
            )
        )
        # The keyring is fetched and written without a shell: curl
        # emits the key bytes on stdout, which are piped as stdin into
        # a plain ``tee`` argv.  No ``bash -c``, no interpolated
        # command string.
        curl_command = ["curl", "-fsSL", keyring_url]
        curl_result: RunResult = self._runner.run(
            list(curl_command),
            elevated=run_elevated,
        )
        if curl_result.exit_code != 0:
            rtnval = UbuntuVPNActionResult(
                status=VPNActionStatus.FAILED,
                provider=VPNProvider.TAILSCALE,
                detail="tailscale keyring fetch failed",
                exit_code=curl_result.exit_code,
                stdout=curl_result.stdout,
                stderr=curl_result.stderr,
                command=curl_command,
            )
            return rtnval

        keyring_command = ["tee", keyring_path]
        keyring_result: RunResult = self._runner.run(
            list(keyring_command),
            elevated=run_elevated,
            stdin=curl_result.stdout or "",
        )
        if keyring_result.exit_code != 0:
            rtnval = UbuntuVPNActionResult(
                status=VPNActionStatus.FAILED,
                provider=VPNProvider.TAILSCALE,
                detail="tailscale keyring write failed",
                exit_code=keyring_result.exit_code,
                stdout=keyring_result.stdout,
                stderr=keyring_result.stderr,
                command=keyring_command,
            )
            return rtnval

        sources_line = (
            "deb [signed-by={path}] "
            "https://pkgs.tailscale.com/{channel}/ubuntu {codename} main"
        ).format(
            path=keyring_path,
            channel=channel,
            codename=codename,
        )
        sources_command = ["tee", sources_path]
        sources_result: RunResult = self._runner.run(
            list(sources_command),
            elevated=run_elevated,
            stdin=sources_line + os.linesep,
        )
        if sources_result.exit_code != 0:
            rtnval = UbuntuVPNActionResult(
                status=VPNActionStatus.FAILED,
                provider=VPNProvider.TAILSCALE,
                detail="tailscale sources write failed",
                exit_code=sources_result.exit_code,
                stdout=sources_result.stdout,
                stderr=sources_result.stderr,
                command=sources_command,
            )
            return rtnval

        update_command = ["apt-get", "update"]
        update_result: RunResult = self._runner.run(
            list(update_command),
            elevated=run_elevated,
        )
        if update_result.exit_code != 0:
            rtnval = UbuntuVPNActionResult(
                status=VPNActionStatus.FAILED,
                provider=VPNProvider.TAILSCALE,
                detail="apt-get update failed",
                exit_code=update_result.exit_code,
                stdout=update_result.stdout,
                stderr=update_result.stderr,
                command=update_command,
            )
            return rtnval

        rtnval = UbuntuVPNActionResult(
            status=VPNActionStatus.SUCCESS,
            provider=VPNProvider.TAILSCALE,
            detail="tailscale repo ensured",
            exit_code=0,
            stdout=None,
            stderr=None,
            command=update_command,
        )
        return rtnval

    def _ensure_twingate_repo(
        self,
        *,
        aspects: Aspects,
    ) -> UbuntuVPNActionResult:
        """
            Ensure the Twingate apt sources file exists and run
            ``apt-get update``.

            :param aspects: Behaviour controls.

            :returns: A :class:`UbuntuVPNActionResult` describing the
                      cumulative outcome.
        """
        sources_path = self._ubuntu_config.twingate_sources_path
        keyring_path = self._ubuntu_config.twingate_keyring_path
        run_elevated = self._should_elevate(kind=VERB_KIND_WRITE)

        # Fetch and install the Twingate signing key so the apt source
        # can use ``signed-by`` instead of disabling verification with
        # ``[trusted=yes]`` (the former MITM / unsigned-package risk).
        curl_command = ["curl", "-fsSL", _TWINGATE_KEYRING_URL]
        curl_result: RunResult = self._runner.run(
            list(curl_command),
            elevated=run_elevated,
        )
        if curl_result.exit_code != 0:
            rtnval = UbuntuVPNActionResult(
                status=VPNActionStatus.FAILED,
                provider=VPNProvider.TWINGATE,
                detail="twingate keyring fetch failed",
                exit_code=curl_result.exit_code,
                stdout=curl_result.stdout,
                stderr=curl_result.stderr,
                command=curl_command,
            )
            return rtnval

        keyring_command = ["tee", keyring_path]
        keyring_result: RunResult = self._runner.run(
            list(keyring_command),
            elevated=run_elevated,
            stdin=curl_result.stdout or "",
        )
        if keyring_result.exit_code != 0:
            rtnval = UbuntuVPNActionResult(
                status=VPNActionStatus.FAILED,
                provider=VPNProvider.TWINGATE,
                detail="twingate keyring write failed",
                exit_code=keyring_result.exit_code,
                stdout=keyring_result.stdout,
                stderr=keyring_result.stderr,
                command=keyring_command,
            )
            return rtnval

        sources_line = (
            "deb [signed-by={path}] "
            "https://packages.twingate.com/apt/ /"
        ).format(path=keyring_path)
        sources_command = ["tee", sources_path]
        sources_result: RunResult = self._runner.run(
            list(sources_command),
            elevated=run_elevated,
            stdin=sources_line + os.linesep,
        )
        if sources_result.exit_code != 0:
            rtnval = UbuntuVPNActionResult(
                status=VPNActionStatus.FAILED,
                provider=VPNProvider.TWINGATE,
                detail="twingate sources write failed",
                exit_code=sources_result.exit_code,
                stdout=sources_result.stdout,
                stderr=sources_result.stderr,
                command=sources_command,
            )
            return rtnval

        update_command = ["apt-get", "update"]
        update_result: RunResult = self._runner.run(
            list(update_command),
            elevated=run_elevated,
        )
        if update_result.exit_code != 0:
            rtnval = UbuntuVPNActionResult(
                status=VPNActionStatus.FAILED,
                provider=VPNProvider.TWINGATE,
                detail="apt-get update failed",
                exit_code=update_result.exit_code,
                stdout=update_result.stdout,
                stderr=update_result.stderr,
                command=update_command,
            )
            return rtnval

        rtnval = UbuntuVPNActionResult(
            status=VPNActionStatus.SUCCESS,
            provider=VPNProvider.TWINGATE,
            detail="twingate repo ensured",
            exit_code=0,
            stdout=None,
            stderr=None,
            command=update_command,
        )
        return rtnval

    def _status_quiet(
        self,
        *,
        provider: VPNProvider,
        aspects: Aspects,
    ) -> Optional[UbuntuVPNClientStatus]:
        """
            Best-effort status probe used by the idempotency guards in
            ``bring_up`` / ``bring_down``.

            Returns ``None`` on any underlying query error so the
            caller falls back to dispatching the mutating command.

            :param provider: Target provider.
            :param aspects: Behaviour controls.

            :returns: The status snapshot, or ``None`` when the probe
                      could not produce one.
        """
        try:
            rtnval: Optional[UbuntuVPNClientStatus] = self.status(
                provider=provider,
                aspects=aspects,
            )
            return rtnval
        except UbuntuVPNClientQueryError:
            rtnval = None
            return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — command construction
# ----------------------------------------------------------------------


def _validate_token(*, name: str, value: str, pattern: "re.Pattern[str]") -> None:
    """
        Validate that an externally-supplied value safe to interpolate
        into a repo URL or an apt argv token.

        :param name: Human-readable name of the value, for error text.
        :param value: The value to validate.
        :param pattern: The compiled allow-list pattern.

        :raises ValueError: When ``value`` does not match ``pattern``.
    """
    if value is None or pattern.match(value) is None:
        err_msg_lines = ["Rejected unsafe value for apt/repo interpolation."]
        err_msg_lines.append("    FIELD: {}".format(name))
        err_msg_lines.append("    PROVIDED: {!r}".format(value))
        err_msg = os.linesep.join(err_msg_lines)
        raise ValueError(err_msg)
    return


def _provider_pkg(*, provider: VPNProvider) -> str:
    """
        Map a :class:`VPNProvider` to its deb package name.

        :param provider: Provider value.

        :returns: The deb package name.
    """
    if provider == VPNProvider.TAILSCALE:
        rtnval = _TAILSCALE_PKG
        return rtnval
    if provider == VPNProvider.TWINGATE:
        rtnval = _TWINGATE_PKG
        return rtnval

    err_msg_lines = ["Unsupported provider for package mapping."]
    err_msg_lines.append("    PROVIDED: {!r}".format(provider))
    err_msg = os.linesep.join(err_msg_lines)
    raise ValueError(err_msg)


def _purge_cleanup_paths(
    *,
    provider: VPNProvider,
    config: UbuntuVPNClientManagerConfig,
) -> List[str]:
    """
        Return the residual files to remove on ``purge=True`` uninstall.

        :param provider: Target provider.
        :param config: Manager configuration.

        :returns: A list of absolute paths to remove.
    """
    if provider == VPNProvider.TAILSCALE:
        rtnval = [config.sources_path, config.keyring_path]
        return rtnval
    if provider == VPNProvider.TWINGATE:
        rtnval = [
            config.twingate_sources_path,
            config.twingate_config_path,
            config.twingate_keyring_path,
        ]
        return rtnval

    err_msg_lines = ["Unsupported provider for purge cleanup."]
    err_msg_lines.append("    PROVIDED: {!r}".format(provider))
    err_msg = os.linesep.join(err_msg_lines)
    raise ValueError(err_msg)


def _build_tailscale_up_command(
    *,
    authkey: str,
    hostname: Optional[str],
    advertise_tags: Optional[List[str]],
    advertise_routes: Optional[List[str]],
    exit_node: bool,
) -> List[str]:
    """
        Build the ``tailscale up`` argv.

        :param authkey: Tailscale auth key.
        :param hostname: Hostname to register, or ``None``.
        :param advertise_tags: Tag list, or ``None``.
        :param advertise_routes: Subnet route list, or ``None``.
        :param exit_node: When True, advertise as an exit node.

        :returns: The argv as a list of strings.
    """
    command = ["tailscale", "up", "--authkey={}".format(authkey)]
    if hostname is not None and len(hostname) > 0:
        command.append("--hostname={}".format(hostname))
    if advertise_tags is not None and len(advertise_tags) > 0:
        tags_value = ",".join(advertise_tags)
        command.append("--advertise-tags={}".format(tags_value))
    if advertise_routes is not None and len(advertise_routes) > 0:
        routes_value = ",".join(advertise_routes)
        command.append("--advertise-routes={}".format(routes_value))
    if exit_node is True:
        command.append("--advertise-exit-node")
    rtnval = command
    return rtnval


def _render_twingate_conf(*, settings: Dict[str, str]) -> str:
    """
        Render a deterministic ``/etc/twingate/connector.conf`` body.

        Keys are sorted for stable output so the configure-idempotency
        path can compare bit-for-bit.

        :param settings: Provider-specific key/value pairs.

        :returns: The rendered configuration body.
    """
    lines: List[str] = []
    for key in sorted(settings.keys()):
        value = settings[key]
        lines.append("{key}={value}".format(key=key, value=value))
    rtnval = os.linesep.join(lines) + os.linesep
    return rtnval


# ----------------------------------------------------------------------
# Module-level helpers — status parsers
# ----------------------------------------------------------------------


def _parse_tailscale_status_json(
    *,
    payload: str,
    installed_version_fallback: Optional[str],
) -> UbuntuVPNClientStatus:
    """
        Parse ``tailscale status --json`` stdout.

        Total over backend states: any decode failure or missing field
        falls through to a default-populated status snapshot.

        :param payload: Decoded stdout.
        :param installed_version_fallback: Fallback for the version
                                           field when the JSON omits it.

        :returns: The :class:`UbuntuVPNClientStatus`.
    """
    try:
        document = json.loads(payload)
    except (ValueError, TypeError):
        rtnval = UbuntuVPNClientStatus(
            provider=VPNProvider.TAILSCALE,
            installed=True,
            running=False,
            version=installed_version_fallback,
        )
        return rtnval

    if isinstance(document, dict) is False:
        rtnval = UbuntuVPNClientStatus(
            provider=VPNProvider.TAILSCALE,
            installed=True,
            running=False,
            version=installed_version_fallback,
        )
        return rtnval

    backend_state = document.get("BackendState")
    is_running = backend_state == "Running"

    self_block = document.get("Self") or {}
    if isinstance(self_block, dict) is False:
        self_block = {}

    self_ips_value = self_block.get("TailscaleIPs") or []
    if isinstance(self_ips_value, list) is False:
        self_ips_value = []
    if len(self_ips_value) > 0:
        self_address: Optional[str] = str(self_ips_value[0])
    else:
        self_address = None

    hostname_value = self_block.get("HostName")
    if hostname_value is not None:
        hostname_str: Optional[str] = str(hostname_value)
    else:
        hostname_str = None

    tailnet_value = document.get("MagicDNSSuffix")
    if tailnet_value is not None:
        tailnet_str: Optional[str] = str(tailnet_value)
    else:
        tailnet_str = None

    version_value = document.get("Version")
    if version_value is not None:
        version_str: Optional[str] = str(version_value)
    else:
        version_str = installed_version_fallback

    peer_map: Dict[str, str] = {}
    peer_block = document.get("Peer") or {}
    if isinstance(peer_block, dict) is True:
        for _peer_key, peer_value in peer_block.items():
            if isinstance(peer_value, dict) is False:
                continue
            peer_hostname = peer_value.get("HostName")
            peer_ips = peer_value.get("TailscaleIPs") or []
            if isinstance(peer_ips, list) is False:
                continue
            if peer_hostname is None or len(peer_ips) == 0:
                continue
            peer_map[str(peer_hostname)] = str(peer_ips[0])

    rtnval = UbuntuVPNClientStatus(
        provider=VPNProvider.TAILSCALE,
        installed=True,
        running=is_running,
        self_address=self_address,
        hostname=hostname_str,
        tailnet=tailnet_str,
        peer_addresses=peer_map,
        version=version_str,
    )
    return rtnval


def _parse_twingate_status_text(
    *,
    payload: str,
    installed_version_fallback: Optional[str],
) -> UbuntuVPNClientStatus:
    """
        Parse ``twingate status`` stdout.

        Twingate has no overlay self-address concept and no peer table,
        so the resulting :class:`UbuntuVPNClientStatus` always carries
        ``self_address=None`` and ``peer_addresses={}``.

        :param payload: Decoded stdout.
        :param installed_version_fallback: Fallback for the version
                                           field.

        :returns: The :class:`UbuntuVPNClientStatus`.
    """
    text = payload or ""
    lowered = text.lower()
    if "online" in lowered or "connected" in lowered or "running" in lowered:
        is_running = True
    else:
        is_running = False

    rtnval = UbuntuVPNClientStatus(
        provider=VPNProvider.TWINGATE,
        installed=True,
        running=is_running,
        self_address=None,
        hostname=None,
        tailnet=None,
        peer_addresses={},
        version=installed_version_fallback,
    )
    return rtnval


__all__ = [
    "UbuntuVPNActionResult",
    "UbuntuVPNActionStatus",
    "UbuntuVPNClientManager",
    "UbuntuVPNClientManagerConfig",
    "UbuntuVPNClientQueryError",
    "UbuntuVPNClientStatus",
    "VPNProvider",
]
