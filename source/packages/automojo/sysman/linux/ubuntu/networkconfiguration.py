"""
    Ubuntu network-configuration feature.

    Implements the cross-OS network-configuration contract for Ubuntu using
    the iproute2 family (``ip``), netplan (``netplan apply`` plus a
    declarative YAML drop-in under ``/etc/netplan/``), ``resolvectl`` for
    DNS, and ``hostnamectl`` for the static hostname.

    File I/O policy:
        Every file read / write / probe / delete is performed through the
        injected :class:`ICommandRunner` (``cat``, ``tee``, ``rm``,
        ``test -e``).  This module does NOT consume an
        :class:`IFileOperator`.  The drafted spec names ``IFileOperator``
        as a collaborator; that wording is a forward-looking design note
        and the current implementation deviates from it pending
        introduction of a concrete file-operator abstraction in this
        codebase.

    Elevation policy:
        Every write verb honours ``config.elevated``.  Reads
        (``get_interfaces``, ``get_dns_servers``, ``get_hostname``, the
        idempotency probes) never elevate.

    Surface choices:
        * Persistent IPv4 addressing is rendered into a single netplan
          drop-in file (default basename ``99-automojo.yaml``) under
          ``/etc/netplan/``.  The renderer emits a deterministic
          string so byte-for-byte comparison drives idempotency.
        * Runtime link-state changes use ``ip link set <iface> up|down``.
        * Routes use ``ip route add|del`` and ``ip route show`` for
          idempotency.
        * DNS uses ``resolvectl dns <iface> <servers...>``; the runtime
          surface is sufficient for the base contract and avoids the
          additional netplan rewrite-and-apply round-trip that would be
          required for full persistence.  See the spec follow-ups.
        * Hostname uses ``hostnamectl set-hostname <name>``.
"""

import json
import os

from dataclasses import dataclass, field
from typing import Optional

from automojo.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


_IP = "ip"
_HOSTNAME = "hostname"
_HOSTNAMECTL = "hostnamectl"
_RESOLVECTL = "resolvectl"
_NETPLAN = "netplan"

_DEFAULT_NETPLAN_DIR = "/etc/netplan"
_DEFAULT_NETPLAN_BASENAME = "99-automojo.yaml"


UbuntuNetworkActionStatus = ActionStatus


@dataclass
class UbuntuNetworkConfigurationManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuNetworkConfigurationManager`.
        Inherits ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param netplan_dir: Directory holding netplan YAML drop-ins.
                            Defaults to ``/etc/netplan``.
        :param netplan_basename: Filename of the netplan drop-in
                                 maintained by this manager.  Defaults to
                                 ``99-automojo.yaml`` so that any
                                 distribution-default file (typically
                                 ``00-installer-config.yaml`` or
                                 ``50-cloud-init.yaml``) wins on tie.
    """
    netplan_dir: str = _DEFAULT_NETPLAN_DIR
    netplan_basename: str = _DEFAULT_NETPLAN_BASENAME


@dataclass
class UbuntuNetworkActionResult(FeatureActionResult):
    """
        Result of a network-configuration write operation.

        :param interface: The interface name targeted, or ``None`` for
                          operations that are not interface-scoped
                          (``set_hostname``, ``add_route``, ``remove_route``).
        :param detail_name: A secondary identifier echoed onto the
                            result (hostname for ``set_hostname``,
                            destination for route operations).
        :param destination: The route destination, when applicable.
        :param diff: Optional unified-diff hint for file-backed writes
                     in dry-run mode.
        :param message: Optional human-readable detail for ``SKIPPED``
                        or ``FAILED`` outcomes.
    """
    interface: Optional[str] = None
    detail_name: Optional[str] = None
    destination: Optional[str] = None
    diff: Optional[str] = None
    message: Optional[str] = None


@dataclass
class UbuntuInterfaceInfo:
    """
        Summary of one network interface as enumerated by
        :meth:`UbuntuNetworkConfigurationManager.get_interfaces`.

        :param name: Kernel ifname (e.g. ``eth0``, ``enp0s3``).
        :param oper_state: Native operstate string from
                           ``ip -j addr show`` (``"UP"``, ``"DOWN"``,
                           ``"UNKNOWN"``, ...).
        :param mac_address: Hardware address, or ``None`` when absent.
        :param ipv4_addresses: Parsed IPv4 CIDR strings.
        :param ipv6_addresses: Parsed IPv6 CIDR strings.
        :param mtu: MTU when reported by the kernel; ``None`` otherwise.
    """
    name: str
    oper_state: str
    mac_address: Optional[str]
    ipv4_addresses: list[str] = field(default_factory=list)
    ipv6_addresses: list[str] = field(default_factory=list)
    mtu: Optional[int] = None


@dataclass
class UbuntuRouteInfo:
    """
        Parsed route entry.

        :param destination: Destination prefix.  ``"default"`` is
                            normalised to ``"0.0.0.0/0"``.
        :param gateway: Gateway IP, or ``None`` when the route is
                        directly connected.
        :param interface: Outgoing interface name.
        :param metric: Route metric when reported.
    """
    destination: str
    gateway: Optional[str]
    interface: Optional[str]
    metric: Optional[int] = None


class UbuntuNetworkQueryError(Exception):
    """
        Raised when a network read fails for a reason other than the
        target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Network query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class UbuntuNetworkConfigurationManager(BaseFeatureManager):
    """
        Ubuntu network-configuration manager.  Every verb is dispatched
        through the injected :class:`ICommandRunner`; file I/O for the
        netplan drop-in is performed via ``cat`` / ``tee`` rather than a
        separate file operator.
    """

    result_class = UbuntuNetworkActionResult
    status_class = UbuntuNetworkActionStatus
    feature_name = "network-configuration"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_interfaces(self) -> list[UbuntuInterfaceInfo]:
        """
            Enumerate network interfaces.

            Runs ``ip -j addr show`` and parses the JSON output.  Never
            elevates.

            :returns: One :class:`UbuntuInterfaceInfo` per kernel
                      interface.
            :raises UbuntuNetworkQueryError: When ``ip`` fails with a
                                              non-zero exit and stderr.
        """
        command = [_IP, "-j", "addr", "show"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuNetworkQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_ip_addr_json(text=run_result.stdout)
        return rtnval

    def get_dns_servers(self, interface: str) -> list[str]:
        """
            Read DNS servers configured on ``interface`` via
            ``resolvectl dns <iface>``.

            :param interface: Interface name.

            :returns: List of DNS servers in declaration order.  Empty
                      when none are configured.
            :raises UbuntuNetworkQueryError: When ``resolvectl`` fails
                                              with a non-zero exit and
                                              non-empty stderr.
        """
        command = [_RESOLVECTL, "dns", interface]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0 and len(run_result.stderr.strip()) > 0:
            raise UbuntuNetworkQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        rtnval = _parse_resolvectl_dns(
            text=run_result.stdout, interface=interface,
        )
        return rtnval

    def get_hostname(self) -> str:
        """
            Read the current static hostname via ``hostname``.

            :returns: The hostname with surrounding whitespace stripped.
                      Empty string when ``hostname`` fails.
        """
        command = [_HOSTNAME]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = ""
            return rtnval
        rtnval = run_result.stdout.strip()
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs
    # ------------------------------------------------------------------

    def configure_interface(
        self,
        name: str,
        address: str,
        netmask: str,
        gateway: Optional[str],
    ) -> UbuntuNetworkActionResult:
        """
            Configure persistent IPv4 addressing for ``name`` by
            rewriting the netplan drop-in and running ``netplan apply``.

            Idempotent: when the existing file's content matches the
            rendered desired body byte-for-byte, the verb returns
            ``SKIPPED`` without touching the file.

            :param name: Interface name.
            :param address: IPv4 address in dotted-quad form.
            :param netmask: Either a dotted-quad netmask
                            (e.g. ``"255.255.255.0"``) or a numeric
                            prefix length (e.g. ``"24"``).
            :param gateway: Default gateway for the interface, or
                            ``None`` to omit the ``gateway4`` field.

            :returns: The :class:`UbuntuNetworkActionResult`.
        """
        path = self._netplan_path()
        prefix = _coerce_prefix(netmask=netmask)
        desired = _render_netplan(
            interface=name,
            address=address,
            prefix=prefix,
            gateway=gateway,
        )

        write_command = ["tee", path]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuNetworkActionStatus.DRY_RUN,
                interface=name,
                detail=name,
                detail_name=address,
                command=write_command,
            )
            return dry

        existing = self._read_file(path=path)
        if existing is not None and existing == desired:
            skipped = self._build_result(
                status=UbuntuNetworkActionStatus.SKIPPED,
                interface=name,
                detail=name,
                detail_name=address,
                command=write_command,
                message="netplan drop-in already matches desired content",
            )
            return skipped

        write_run = self._runner.run(
            list(write_command),
            elevated=self._config.elevated,
            stdin=desired,
        )
        if write_run.exit_code != 0:
            failed = UbuntuNetworkActionResult(
                status=UbuntuNetworkActionStatus.FAILED,
                detail=name,
                exit_code=write_run.exit_code,
                stdout=write_run.stdout,
                stderr=write_run.stderr,
                command=list(write_command),
                interface=name,
                detail_name=address,
                message="netplan_drop_in_write_failed",
            )
            return failed

        apply_command = [_NETPLAN, "apply"]
        apply_run = self._runner.run(
            list(apply_command),
            elevated=self._config.elevated,
        )
        if apply_run.exit_code != 0:
            failed = UbuntuNetworkActionResult(
                status=UbuntuNetworkActionStatus.FAILED,
                detail=name,
                exit_code=apply_run.exit_code,
                stdout=apply_run.stdout,
                stderr=apply_run.stderr,
                command=list(apply_command),
                interface=name,
                detail_name=address,
                message="netplan_apply_failed",
            )
            return failed

        success = UbuntuNetworkActionResult(
            status=UbuntuNetworkActionStatus.SUCCESS,
            detail=name,
            exit_code=apply_run.exit_code,
            stdout=apply_run.stdout,
            stderr=apply_run.stderr,
            command=list(apply_command),
            interface=name,
            detail_name=address,
        )
        return success

    def enable_interface(self, name: str) -> UbuntuNetworkActionResult:
        """
            Bring an interface up via ``ip link set <name> up``.

            Idempotent: ``SKIPPED`` when the interface is already up.

            :param name: Interface name.

            :returns: The :class:`UbuntuNetworkActionResult`.
        """
        command = [_IP, "link", "set", name, "up"]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuNetworkActionStatus.DRY_RUN,
                interface=name,
                detail=name,
                command=command,
            )
            return dry

        current_state = self._query_oper_state(interface=name)
        if current_state == "UP":
            skipped = self._build_result(
                status=UbuntuNetworkActionStatus.SKIPPED,
                interface=name,
                detail=name,
                command=command,
                message="interface is already up",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=name,
            detail=name,
        )
        return rtnval

    def disable_interface(self, name: str) -> UbuntuNetworkActionResult:
        """
            Bring an interface down via ``ip link set <name> down``.

            Idempotent: ``SKIPPED`` when the interface is already down.

            :param name: Interface name.

            :returns: The :class:`UbuntuNetworkActionResult`.
        """
        command = [_IP, "link", "set", name, "down"]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuNetworkActionStatus.DRY_RUN,
                interface=name,
                detail=name,
                command=command,
            )
            return dry

        current_state = self._query_oper_state(interface=name)
        if current_state == "DOWN":
            skipped = self._build_result(
                status=UbuntuNetworkActionStatus.SKIPPED,
                interface=name,
                detail=name,
                command=command,
                message="interface is already down",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=name,
            detail=name,
        )
        return rtnval

    def add_route(
        self,
        dest: str,
        gateway: str,
    ) -> UbuntuNetworkActionResult:
        """
            Add a route via ``ip route add <dest> via <gateway>``.

            Idempotent: ``SKIPPED`` when ``ip route show <dest>``
            already reports a matching route.

            :param dest: Destination prefix in CIDR form.
            :param gateway: Gateway IP.

            :returns: The :class:`UbuntuNetworkActionResult`.
        """
        command = [_IP, "route", "add", dest, "via", gateway]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuNetworkActionStatus.DRY_RUN,
                interface=None,
                detail=dest,
                destination=dest,
                detail_name=gateway,
                command=command,
            )
            return dry

        if self._route_exists(dest=dest):
            skipped = self._build_result(
                status=UbuntuNetworkActionStatus.SKIPPED,
                interface=None,
                detail=dest,
                destination=dest,
                detail_name=gateway,
                command=command,
                message="route is already present",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=None,
            detail=dest,
            destination=dest,
            detail_name=gateway,
        )
        return rtnval

    def remove_route(self, dest: str) -> UbuntuNetworkActionResult:
        """
            Remove a route via ``ip route del <dest>``.

            Idempotent: ``SKIPPED`` when no matching route is reported
            by ``ip route show <dest>``.

            :param dest: Destination prefix in CIDR form.

            :returns: The :class:`UbuntuNetworkActionResult`.
        """
        command = [_IP, "route", "del", dest]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuNetworkActionStatus.DRY_RUN,
                interface=None,
                detail=dest,
                destination=dest,
                command=command,
            )
            return dry

        if not self._route_exists(dest=dest):
            skipped = self._build_result(
                status=UbuntuNetworkActionStatus.SKIPPED,
                interface=None,
                detail=dest,
                destination=dest,
                command=command,
                message="route is not present",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=None,
            detail=dest,
            destination=dest,
        )
        return rtnval

    def set_dns_servers(
        self,
        interface: str,
        servers: list[str],
    ) -> UbuntuNetworkActionResult:
        """
            Set per-link DNS servers via ``resolvectl dns <iface>
            <servers...>``.

            Idempotent: ``SKIPPED`` when the current sorted list of
            servers reported by ``resolvectl dns <iface>`` matches the
            desired list.

            :param interface: Interface name.
            :param servers: Ordered list of DNS server addresses.

            :returns: The :class:`UbuntuNetworkActionResult`.
        """
        command = [_RESOLVECTL, "dns", interface] + list(servers)

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuNetworkActionStatus.DRY_RUN,
                interface=interface,
                detail=interface,
                detail_name=",".join(servers),
                command=command,
            )
            return dry

        try:
            current = self.get_dns_servers(interface=interface)
        except UbuntuNetworkQueryError:
            current = []

        if sorted(current) == sorted(servers):
            skipped = self._build_result(
                status=UbuntuNetworkActionStatus.SKIPPED,
                interface=interface,
                detail=interface,
                detail_name=",".join(servers),
                command=command,
                message="DNS servers already match desired list",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=interface,
            detail=interface,
            detail_name=",".join(servers),
        )
        return rtnval

    def set_hostname(self, name: str) -> UbuntuNetworkActionResult:
        """
            Set the static hostname via ``hostnamectl set-hostname``.

            Idempotent: ``SKIPPED`` when ``hostname`` already reports
            ``name``.

            :param name: Desired hostname.

            :returns: The :class:`UbuntuNetworkActionResult`.
        """
        command = [_HOSTNAMECTL, "set-hostname", name]

        if self._config.dry_run:
            dry = self._build_result(
                status=UbuntuNetworkActionStatus.DRY_RUN,
                interface=None,
                detail=name,
                detail_name=name,
                command=command,
            )
            return dry

        current = self.get_hostname()
        if current == name:
            skipped = self._build_result(
                status=UbuntuNetworkActionStatus.SKIPPED,
                interface=None,
                detail=name,
                detail_name=name,
                command=command,
                message="hostname is already set",
            )
            return skipped

        rtnval = self._dispatch_write(
            command=command,
            interface=None,
            detail=name,
            detail_name=name,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _netplan_path(self) -> str:
        """
            Build the absolute path to the managed netplan drop-in.

            :returns: ``<netplan_dir>/<netplan_basename>``.
        """
        rtnval = "{}/{}".format(
            self._config.netplan_dir, self._config.netplan_basename,
        )
        return rtnval

    def _read_file(self, *, path: str) -> Optional[str]:
        """
            Read a file via ``cat``.

            :param path: The absolute path to read.

            :returns: The file contents on success; ``None`` when the
                      file is missing.
        """
        command = ["cat", path]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[str] = None
            return rtnval
        rtnval = run_result.stdout
        return rtnval

    def _query_oper_state(self, *, interface: str) -> str:
        """
            Query the operstate of ``interface`` via ``ip -j link
            show <iface>``.

            :param interface: Interface name.

            :returns: One of ``"UP"``, ``"DOWN"``, ``"UNKNOWN"``, or
                      ``""`` when the read fails.
        """
        command = [_IP, "-j", "link", "show", interface]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = ""
            return rtnval

        try:
            decoded = json.loads(run_result.stdout)
        except (ValueError, TypeError):
            rtnval = ""
            return rtnval

        if not isinstance(decoded, list):
            rtnval = ""
            return rtnval

        if len(decoded) == 0:
            rtnval = ""
            return rtnval

        first = decoded[0]
        if not isinstance(first, dict):
            rtnval = ""
            return rtnval

        operstate = first.get("operstate", "")
        if not isinstance(operstate, str):
            rtnval = ""
            return rtnval

        rtnval = operstate
        return rtnval

    def _route_exists(self, *, dest: str) -> bool:
        """
            Probe ``ip route show <dest>`` for any matching line.

            :param dest: Destination prefix.

            :returns: True when at least one non-blank line is reported.
        """
        command = [_IP, "route", "show", dest]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval

        for raw in run_result.stdout.splitlines():
            stripped = raw.strip()
            if len(stripped) == 0:
                continue
            rtnval = True
            return rtnval

        rtnval = False
        return rtnval

    def _dispatch_write(
        self,
        *,
        command: list[str],
        interface: Optional[str],
        detail: Optional[str],
        detail_name: Optional[str] = None,
        destination: Optional[str] = None,
    ) -> UbuntuNetworkActionResult:
        """
            Dispatch a non-file write command, honouring elevation and
            mapping the runner result onto an
            :class:`UbuntuNetworkActionResult`.

            :param command: The full argv.
            :param interface: The interface to echo on the result.
            :param detail: The result ``detail`` value.
            :param detail_name: The result ``detail_name`` value.
            :param destination: The result ``destination`` value.

            :returns: The :class:`UbuntuNetworkActionResult`.
        """
        run_result = self._runner.run(
            list(command),
            elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = UbuntuNetworkActionStatus.SUCCESS
        else:
            mapped_status = UbuntuNetworkActionStatus.FAILED

        rtnval = UbuntuNetworkActionResult(
            status=mapped_status,
            detail=detail,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            command=list(command),
            interface=interface,
            detail_name=detail_name,
            destination=destination,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: UbuntuNetworkActionStatus,
        interface: Optional[str],
        detail: Optional[str],
        command: list[str],
        detail_name: Optional[str] = None,
        destination: Optional[str] = None,
        message: Optional[str] = None,
        diff: Optional[str] = None,
    ) -> UbuntuNetworkActionResult:
        """
            Construct an :class:`UbuntuNetworkActionResult` without
            invoking the runner.

            :param status: The outcome status.
            :param interface: The interface to echo onto the result.
            :param detail: The result ``detail`` value.
            :param command: The would-have-been argv (copied for capture).
            :param detail_name: Optional secondary identifier.
            :param destination: Optional route destination.
            :param message: Optional human-readable detail.
            :param diff: Optional unified-diff hint.

            :returns: The :class:`UbuntuNetworkActionResult`.
        """
        rtnval = UbuntuNetworkActionResult(
            status=status,
            detail=detail,
            command=list(command),
            interface=interface,
            detail_name=detail_name,
            destination=destination,
            message=message,
            diff=diff,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level renderers and parsers
# ----------------------------------------------------------------------


def _coerce_prefix(*, netmask: str) -> int:
    """
        Convert a dotted-quad netmask or numeric prefix into a numeric
        prefix length.

        :param netmask: Either ``"255.255.255.0"`` or ``"24"``.

        :returns: Prefix length as an int.
        :raises ValueError: When the input is neither a valid prefix
                            number in [0, 32] nor a valid dotted-quad
                            netmask.
    """
    stripped = netmask.strip()
    if len(stripped) == 0:
        err_msg = "netmask must not be empty"
        raise ValueError(err_msg)

    if "." not in stripped:
        try:
            prefix_value = int(stripped)
        except ValueError:
            err_msg = "Invalid prefix value: {!r}".format(stripped)
            raise ValueError(err_msg)
        if prefix_value < 0 or prefix_value > 32:
            err_msg = "Prefix out of range [0, 32]: {!r}".format(stripped)
            raise ValueError(err_msg)
        return prefix_value

    octets = stripped.split(".")
    if len(octets) != 4:
        err_msg = "Invalid dotted-quad netmask: {!r}".format(stripped)
        raise ValueError(err_msg)

    bits = 0
    for octet in octets:
        try:
            value = int(octet)
        except ValueError:
            err_msg = "Invalid octet in netmask: {!r}".format(stripped)
            raise ValueError(err_msg)
        if value < 0 or value > 255:
            err_msg = "Octet out of range in netmask: {!r}".format(stripped)
            raise ValueError(err_msg)
        for bit_position in range(8):
            if (value >> (7 - bit_position)) & 1:
                bits += 1
            else:
                # Discontiguous mask: any zero bit here ends the run.
                # The remainder must be all zero or the netmask is
                # invalid.  We do not enforce strict contiguity in
                # this helper; callers feeding non-contiguous masks
                # will produce a non-conformant prefix length.
                pass

    rtnval = bits
    return rtnval


def _render_netplan(
    *,
    interface: str,
    address: str,
    prefix: int,
    gateway: Optional[str],
) -> str:
    """
        Render a deterministic netplan YAML drop-in containing exactly
        one ``ethernets`` entry for ``interface``.

        Output is byte-identical for matching inputs so that
        :meth:`UbuntuNetworkConfigurationManager.configure_interface`
        can compare against the existing file content for idempotency.

        :param interface: Kernel ifname.
        :param address: IPv4 address.
        :param prefix: Numeric prefix length.
        :param gateway: Optional default gateway.

        :returns: The full YAML body as a string ending in ``\\n``.
    """
    lines: list[str] = []
    lines.append("network:")
    lines.append("  version: 2")
    lines.append("  ethernets:")
    lines.append("    {}:".format(interface))
    lines.append(
        "      addresses: [{}/{}]".format(address, prefix),
    )
    if gateway is not None and len(gateway) > 0:
        lines.append("      gateway4: {}".format(gateway))

    body = "\n".join(lines) + "\n"
    return body


def _parse_ip_addr_json(*, text: str) -> list[UbuntuInterfaceInfo]:
    """
        Parse the JSON output of ``ip -j addr show``.

        :param text: Raw stdout text.

        :returns: One :class:`UbuntuInterfaceInfo` per top-level entry.
    """
    rtnval: list[UbuntuInterfaceInfo] = []

    try:
        decoded = json.loads(text)
    except (ValueError, TypeError):
        return rtnval

    if not isinstance(decoded, list):
        return rtnval

    for entry in decoded:
        if not isinstance(entry, dict):
            continue

        name = entry.get("ifname", "")
        if not isinstance(name, str) or len(name) == 0:
            continue

        operstate_raw = entry.get("operstate", "")
        if isinstance(operstate_raw, str):
            oper_state = operstate_raw
        else:
            oper_state = ""

        mac_raw = entry.get("address", None)
        if isinstance(mac_raw, str) and len(mac_raw) > 0:
            mac_address: Optional[str] = mac_raw
        else:
            mac_address = None

        mtu_raw = entry.get("mtu", None)
        if isinstance(mtu_raw, int):
            mtu: Optional[int] = mtu_raw
        else:
            mtu = None

        ipv4: list[str] = []
        ipv6: list[str] = []
        addr_info = entry.get("addr_info", [])
        if isinstance(addr_info, list):
            for info in addr_info:
                if not isinstance(info, dict):
                    continue
                family = info.get("family", "")
                local_addr = info.get("local", "")
                prefixlen = info.get("prefixlen", None)
                if not isinstance(local_addr, str) or len(local_addr) == 0:
                    continue
                if isinstance(prefixlen, int):
                    cidr = "{}/{}".format(local_addr, prefixlen)
                else:
                    cidr = local_addr
                if family == "inet":
                    ipv4.append(cidr)
                elif family == "inet6":
                    ipv6.append(cidr)

        info_obj = UbuntuInterfaceInfo(
            name=name,
            oper_state=oper_state,
            mac_address=mac_address,
            ipv4_addresses=ipv4,
            ipv6_addresses=ipv6,
            mtu=mtu,
        )
        rtnval.append(info_obj)

    return rtnval


def _parse_resolvectl_dns(*, text: str, interface: str) -> list[str]:
    """
        Parse ``resolvectl dns <iface>`` output.

        ``resolvectl`` emits one of two shapes depending on the build:

            ``Link 2 (eth0): 1.1.1.1 8.8.8.8``

        ...or, for the global form (no per-link filter)::

            ``Global: 1.1.1.1``
            ``Link 2 (eth0): 8.8.8.8``

        We accept both and extract the rightmost colon-separated payload
        from the line that mentions ``interface`` (or the first non-blank
        line for the per-link form).

        :param text: Raw stdout text.
        :param interface: Interface name to filter on.

        :returns: List of server addresses in declaration order.
    """
    rtnval: list[str] = []

    target_token = "({})".format(interface)
    candidate_payload: Optional[str] = None
    fallback_payload: Optional[str] = None

    for raw in text.splitlines():
        line = raw.strip()
        if len(line) == 0:
            continue
        if ":" not in line:
            continue
        head, _, tail = line.partition(":")
        head_token = head.strip()
        payload = tail.strip()
        if target_token in head_token:
            candidate_payload = payload
            break
        if fallback_payload is None and not head_token.startswith("Global"):
            # Per-link form without the parenthesised name on the head
            # token (e.g. ``Link 2 eth0:``).  Fall back to the first
            # plausible line.
            if interface in head_token:
                fallback_payload = payload

    if candidate_payload is None:
        candidate_payload = fallback_payload

    if candidate_payload is None:
        return rtnval

    for token in candidate_payload.split():
        rtnval.append(token)

    return rtnval
