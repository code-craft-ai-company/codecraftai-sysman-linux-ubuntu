"""
    Ubuntu hardware-inventory feature.

    Implements the cross-OS hardware-inventory contract for Ubuntu using
    ``dmidecode``, ``lscpu``, ``lsblk``, ``ip``, ``lspci``, and ``lsusb``.
    Every verb is dispatched through an :class:`ICommandRunner` so the
    manager works identically under local and SSH execution.

    This feature is strictly read-only.  No verb mutates host state, none
    of them honour an idempotency guard, and there is intentionally no
    ``DRY_RUN`` status.  The :class:`UbuntuHardwareInventoryManagerConfig`
    inherits ``dry_run`` from :class:`FeatureConfig` for structural
    uniformity with the rest of the package, but the field is never
    consulted by any verb in this module.

    Elevation policy:
        Reads never elevate by default.  Each verb passes
        ``elevated=False`` to the runner unconditionally.  ``dmidecode``
        is the one collector that genuinely benefits from elevation on a
        non-root user; this implementation issues it unelevated and
        records the failure mode in the spec rather than introducing a
        per-collector elevation switch in this pilot batch.  The
        ``elevated`` field on the config is therefore reserved for the
        upcoming selective-elevation patch documented in
        ``specs/3-hardware-inventory/tasks.md`` and is honoured by no
        verb today.
"""

import json
import os

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants
# ----------------------------------------------------------------------

_DMIDECODE = "dmidecode"
_LSCPU = "lscpu"
_LSBLK = "lsblk"
_LSPCI = "lspci"
_LSUSB = "lsusb"
_IP = "ip"
_CAT = "cat"

_LSBLK_FIELDS = "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL"


# ----------------------------------------------------------------------
# Status enum, exception, and config
# ----------------------------------------------------------------------


class UbuntuHardwareInventoryActionStatus(Enum):
    """
        Outcome status for a hardware-inventory operation.

        This feature is read-only; the cross-OS contract drops
        ``DRY_RUN`` from the status set, so this enum carries only
        ``SUCCESS``, ``FAILED``, and ``SKIPPED``.  ``FAILED`` is recorded
        in metrics paths but reads do not produce result dataclasses;
        instead they raise :class:`UbuntuHardwareInventoryQueryError`.
    """
    SUCCESS = "success"
    FAILED  = "failed"
    SKIPPED = "skipped"


@dataclass
class UbuntuHardwareInventoryManagerConfig(FeatureConfig):
    """
        Configuration for :class:`UbuntuHardwareInventoryManager`.

        Inherits ``elevated`` and ``dry_run`` from :class:`FeatureConfig`
        for structural uniformity with the rest of the package.
        ``dry_run`` is intentionally ignored — the contract makes this
        feature read-only.  ``elevated`` is reserved for the
        selective-elevation patch documented in
        ``specs/3-hardware-inventory/tasks.md``: every verb in this
        module currently passes ``elevated=False`` to the runner so that
        a misconfigured caller cannot accidentally invoke ``sudo`` on a
        read.
    """
    pass


class UbuntuHardwareInventoryQueryError(Exception):
    """
        Raised when an inventory read fails because the runner returned
        a non-zero exit code or the parser could not interpret the
        captured output.
    """

    def __init__(
        self,
        *,
        command: list[str],
        exit_code: Optional[int],
        stderr: str,
        message: Optional[str] = None,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
            :param message: Optional override for the exception message.
                            When not None it replaces the default
                            command/exit/stderr block.
        """
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        if message is not None:
            err_msg = message
        else:
            err_msg_lines = ["Hardware inventory query command failed."]
            err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
            err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
            err_msg_lines.append("    STDERR: {}".format(stderr))
            err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


# ----------------------------------------------------------------------
# Result dataclasses
# ----------------------------------------------------------------------


@dataclass
class UbuntuSystemInfo:
    """
        DMI/SMBIOS system-identity record returned by
        :meth:`UbuntuHardwareInventoryManager.get_system_info`.
    """
    manufacturer: Optional[str] = None
    product_name: Optional[str] = None
    serial_number: Optional[str] = None
    uuid: Optional[str] = None
    bios_version: Optional[str] = None
    firmware_version: Optional[str] = None
    family: Optional[str] = None
    version: Optional[str] = None


@dataclass
class UbuntuCpuInfo:
    """
        CPU topology and feature record returned by
        :meth:`UbuntuHardwareInventoryManager.get_cpu_info`.
    """
    model: Optional[str] = None
    vendor: Optional[str] = None
    architecture: Optional[str] = None
    physical_cores: int = 0
    logical_cores: int = 0
    frequency_mhz: Optional[int] = None
    cache_l1: Optional[int] = None
    cache_l2: Optional[int] = None
    cache_l3: Optional[int] = None
    flags: list[str] = field(default_factory=list)


@dataclass
class UbuntuMemoryModule:
    """
        Per-DIMM record nested under :class:`UbuntuMemoryInfo`.
    """
    slot: Optional[str] = None
    size_bytes: int = 0
    type: Optional[str] = None
    speed_mhz: Optional[int] = None
    manufacturer: Optional[str] = None
    serial_number: Optional[str] = None


@dataclass
class UbuntuMemoryInfo:
    """
        Aggregate-memory record returned by
        :meth:`UbuntuHardwareInventoryManager.get_memory_info`.
    """
    total_bytes: int = 0
    available_bytes: Optional[int] = None
    slots_used: Optional[int] = None
    slots_total: Optional[int] = None
    modules: list[UbuntuMemoryModule] = field(default_factory=list)


@dataclass
class UbuntuStorageDeviceInfo:
    """
        One block-storage device record returned by
        :meth:`UbuntuHardwareInventoryManager.list_storage_devices`.
    """
    name: str
    size_bytes: int = 0
    type: str = ""
    model: Optional[str] = None
    serial: Optional[str] = None
    mountpoint: Optional[str] = None
    filesystem: Optional[str] = None


@dataclass
class UbuntuNetworkDeviceInfo:
    """
        One network-link record returned by
        :meth:`UbuntuHardwareInventoryManager.list_network_devices`.
    """
    name: str
    mac_address: Optional[str] = None
    link_state: Optional[str] = None
    speed: Optional[str] = None
    mtu: Optional[int] = None
    is_virtual: bool = False


@dataclass
class UbuntuPciDeviceInfo:
    """
        One PCI device record returned by
        :meth:`UbuntuHardwareInventoryManager.list_pci_devices`.
    """
    slot: str
    class_name: Optional[str] = None
    vendor: Optional[str] = None
    device: Optional[str] = None
    vendor_id: Optional[str] = None
    device_id: Optional[str] = None
    driver: Optional[str] = None


@dataclass
class UbuntuUsbDeviceInfo:
    """
        One USB device record returned by
        :meth:`UbuntuHardwareInventoryManager.list_usb_devices`.
    """
    bus: str
    device_id: Optional[str] = None
    vendor_id: Optional[str] = None
    product_id: Optional[str] = None
    name: Optional[str] = None
    serial: Optional[str] = None


@dataclass
class UbuntuFirmwareInfo:
    """
        BIOS / UEFI firmware identity returned by
        :meth:`UbuntuHardwareInventoryManager.get_firmware_info`.
    """
    vendor: Optional[str] = None
    version: Optional[str] = None
    release_date: Optional[str] = None
    secure_boot: Optional[bool] = None
    uefi: Optional[bool] = None


# ----------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------


class UbuntuHardwareInventoryManager(BaseFeatureManager):
    """
        Read-only hardware-inventory manager for Ubuntu.

        Every verb shells out to a Linux inventory tool (``dmidecode``,
        ``lscpu``, ``lsblk``, ``lspci``, ``lsusb``, ``ip``) through the
        injected :class:`ICommandRunner`.  Outputs are parsed in pure
        Python and returned as ``Ubuntu*`` dataclasses.  Every runner
        invocation passes ``elevated=False`` regardless of the config so
        that a misconfigured caller cannot accidentally elevate a read.
    """

    status_class = UbuntuHardwareInventoryActionStatus
    feature_name = "hardware-inventory"

    # ------------------------------------------------------------------
    # Read verbs
    # ------------------------------------------------------------------

    def get_system_info(self) -> UbuntuSystemInfo:
        """
            Query DMI / SMBIOS system-identity fields.

            Runs ``dmidecode -t system`` and parses the handle blocks for
            the ``System Information`` and ``BIOS Information`` sections.

            :returns: The :class:`UbuntuSystemInfo`.
            :raises UbuntuHardwareInventoryQueryError: When ``dmidecode``
                                                       fails.
        """
        command = [_DMIDECODE, "-t", "system"]
        run_result = self._read(command=command)
        info = _parse_dmidecode_system(stdout=run_result.stdout)
        return info

    def get_cpu_info(self) -> UbuntuCpuInfo:
        """
            Query CPU topology and feature flags.

            Runs ``lscpu -J`` and maps the JSON ``field`` / ``data``
            entries into :class:`UbuntuCpuInfo` slots.

            :returns: The :class:`UbuntuCpuInfo`.
            :raises UbuntuHardwareInventoryQueryError: When ``lscpu``
                                                       fails or emits
                                                       malformed JSON.
        """
        command = [_LSCPU, "-J"]
        run_result = self._read(command=command)
        info = _parse_lscpu_json(stdout=run_result.stdout, command=command)
        return info

    def get_memory_info(self) -> UbuntuMemoryInfo:
        """
            Query aggregate memory and per-DIMM records.

            Runs ``dmidecode -t memory`` for the ``Physical Memory
            Array`` and ``Memory Device`` handle blocks, then runs
            ``cat /proc/meminfo`` for the live ``MemTotal`` /
            ``MemAvailable`` aggregates.  When ``dmidecode`` fails the
            modules list is left empty and ``slots_*`` stay ``None``;
            the meminfo call still runs so callers always receive a
            usable ``total_bytes`` value where the kernel can report
            one.

            :returns: The :class:`UbuntuMemoryInfo`.
            :raises UbuntuHardwareInventoryQueryError: When the
                                                       ``/proc/meminfo``
                                                       read fails (a
                                                       prerequisite for
                                                       any usable
                                                       answer).
        """
        dmi_command = [_DMIDECODE, "-t", "memory"]
        dmi_result = self._runner.run(list(dmi_command), elevated=False)
        if dmi_result.exit_code == 0:
            info = _parse_dmidecode_memory(stdout=dmi_result.stdout)
        else:
            info = UbuntuMemoryInfo()

        meminfo_command = [_CAT, "/proc/meminfo"]
        meminfo_result = self._read(command=meminfo_command)
        _merge_proc_meminfo(info=info, stdout=meminfo_result.stdout)

        return info

    def list_storage_devices(self) -> list[UbuntuStorageDeviceInfo]:
        """
            Enumerate block-storage devices.

            Runs ``lsblk -J -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,
            MODEL,SERIAL``.  ``-J`` requests JSON output; the parser
            walks every device tree (parent + children) and emits one
            :class:`UbuntuStorageDeviceInfo` per node.

            :returns: One record per block device.
            :raises UbuntuHardwareInventoryQueryError: When ``lsblk``
                                                       fails or emits
                                                       malformed JSON.
        """
        command = [_LSBLK, "-J", "-o", _LSBLK_FIELDS]
        run_result = self._read(command=command)
        devices = _parse_lsblk_json(stdout=run_result.stdout, command=command)
        return devices

    def list_network_devices(self) -> list[UbuntuNetworkDeviceInfo]:
        """
            Enumerate network link-layer hardware.

            Runs ``ip -j link show``.  Each entry's ``ifname``,
            ``address``, ``operstate``, ``mtu``, and ``link_type`` field
            populate one :class:`UbuntuNetworkDeviceInfo`.  Loopback
            and bridge / virtual interfaces are tagged via
            ``is_virtual`` based on ``link_type``.

            :returns: One record per network link.
            :raises UbuntuHardwareInventoryQueryError: When ``ip`` fails
                                                       or emits
                                                       malformed JSON.
        """
        command = [_IP, "-j", "link", "show"]
        run_result = self._read(command=command)
        devices = _parse_ip_link_json(
            stdout=run_result.stdout, command=command,
        )
        return devices

    def list_pci_devices(self) -> list[UbuntuPciDeviceInfo]:
        """
            Enumerate PCI devices.

            Runs ``lspci -mm -nn -k``.  ``-mm`` produces a stable,
            quoted, machine-readable line per device; ``-nn`` includes
            numeric ``[vendor_id:device_id]`` tags inside the vendor /
            device strings; ``-k`` appends one ``Driver:`` /
            ``Module:`` continuation line per device when a kernel
            driver is bound.

            :returns: One record per PCI device.
            :raises UbuntuHardwareInventoryQueryError: When ``lspci``
                                                       fails.
        """
        command = [_LSPCI, "-mm", "-nn", "-k"]
        run_result = self._read(command=command)
        devices = _parse_lspci_output(stdout=run_result.stdout)
        return devices

    def list_usb_devices(self) -> list[UbuntuUsbDeviceInfo]:
        """
            Enumerate USB devices.

            Runs ``lsusb`` and parses the canonical
            ``Bus 001 Device 002: ID 1234:5678 Vendor Product`` line
            shape into :class:`UbuntuUsbDeviceInfo` records.

            :returns: One record per USB device.
            :raises UbuntuHardwareInventoryQueryError: When ``lsusb``
                                                       fails.
        """
        command = [_LSUSB]
        run_result = self._read(command=command)
        devices = _parse_lsusb_output(stdout=run_result.stdout)
        return devices

    def get_firmware_info(self) -> UbuntuFirmwareInfo:
        """
            Query BIOS / UEFI firmware identity.

            Runs ``dmidecode -t bios`` and parses the ``BIOS
            Information`` handle block.  ``UEFI`` mode is detected via
            the ``BIOS is upgradeable`` characteristic in the parser
            when present; secure-boot status is intentionally left
            ``None`` because the bios-only ``dmidecode`` block does not
            carry it (the sysfs ``SecureBoot`` variable is the proper
            source and is left for a follow-up).

            :returns: The :class:`UbuntuFirmwareInfo`.
            :raises UbuntuHardwareInventoryQueryError: When ``dmidecode``
                                                       fails.
        """
        command = [_DMIDECODE, "-t", "bios"]
        run_result = self._read(command=command)
        info = _parse_dmidecode_bios(stdout=run_result.stdout)
        return info

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read(self, *, command: list[str]) -> RunResult:
        """
            Invoke the runner with ``elevated=False`` and raise
            :class:`UbuntuHardwareInventoryQueryError` on non-zero exit.

            Read verbs share this single dispatch helper so that the
            "reads never elevate" property is enforced in exactly one
            place and so that the exception construction is centralised.

            :param command: The argv to dispatch.

            :returns: The raw :class:`RunResult` on success.
            :raises UbuntuHardwareInventoryQueryError: When the runner
                                                       reports a
                                                       non-zero exit
                                                       code.
        """
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            raise UbuntuHardwareInventoryQueryError(
                command=command,
                exit_code=run_result.exit_code,
                stderr=run_result.stderr,
            )

        return run_result


# ----------------------------------------------------------------------
# Module-level parsers
# ----------------------------------------------------------------------


def _parse_dmidecode_system(*, stdout: str) -> UbuntuSystemInfo:
    """
        Parse ``dmidecode -t system`` output.

        ``dmidecode`` emits multiple handle blocks per type filter; the
        ``System Information`` block carries the manufacturer / product
        / serial / UUID we care about.

        :param stdout: Raw stdout from ``dmidecode -t system``.

        :returns: The populated :class:`UbuntuSystemInfo`.  Missing
                  fields are left as ``None``.
    """
    info = UbuntuSystemInfo()
    current_section: Optional[str] = None

    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        if line.startswith("Handle ") or len(line.strip()) == 0:
            continue
        if not line.startswith("\t"):
            current_section = line.strip()
            continue

        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()

        if current_section == "System Information":
            if key == "Manufacturer":
                info.manufacturer = value
            elif key == "Product Name":
                info.product_name = value
            elif key == "Serial Number":
                info.serial_number = value
            elif key == "UUID":
                info.uuid = value
            elif key == "Family":
                info.family = value
            elif key == "Version":
                info.version = value
        elif current_section == "BIOS Information":
            if key == "Version":
                info.bios_version = value
            elif key == "Firmware Revision":
                info.firmware_version = value

    return info


def _parse_dmidecode_bios(*, stdout: str) -> UbuntuFirmwareInfo:
    """
        Parse ``dmidecode -t bios`` output.

        :param stdout: Raw stdout from ``dmidecode -t bios``.

        :returns: The populated :class:`UbuntuFirmwareInfo`.
    """
    info = UbuntuFirmwareInfo()
    current_section: Optional[str] = None
    saw_uefi_characteristic = False

    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        if line.startswith("Handle ") or len(line.strip()) == 0:
            continue
        if not line.startswith("\t"):
            current_section = line.strip()
            continue

        stripped = line.strip()
        if current_section == "BIOS Information":
            if stripped == "UEFI is supported":
                saw_uefi_characteristic = True
                continue
            if ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if key == "Vendor":
                info.vendor = value
            elif key == "Version":
                info.version = value
            elif key == "Release Date":
                info.release_date = value

    if saw_uefi_characteristic is True:
        info.uefi = True
    else:
        info.uefi = False

    return info


def _parse_dmidecode_memory(*, stdout: str) -> UbuntuMemoryInfo:
    """
        Parse ``dmidecode -t memory`` output.

        Builds the aggregate ``slots_total`` from the
        ``Physical Memory Array.Number Of Devices`` line and emits one
        :class:`UbuntuMemoryModule` per ``Memory Device`` block whose
        ``Size`` is not ``No Module Installed``.

        :param stdout: Raw stdout from ``dmidecode -t memory``.

        :returns: The populated :class:`UbuntuMemoryInfo`.
    """
    info = UbuntuMemoryInfo()
    current_section: Optional[str] = None
    current_module: Optional[UbuntuMemoryModule] = None

    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        if line.startswith("Handle "):
            if current_module is not None:
                _commit_memory_module(info=info, module=current_module)
                current_module = None
            current_section = None
            continue
        if len(line.strip()) == 0:
            if current_module is not None:
                _commit_memory_module(info=info, module=current_module)
                current_module = None
            current_section = None
            continue

        if not line.startswith("\t"):
            current_section = line.strip()
            if current_section == "Memory Device":
                current_module = UbuntuMemoryModule()
            continue

        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()

        if current_section == "Physical Memory Array":
            if key == "Number Of Devices":
                try:
                    info.slots_total = int(value)
                except ValueError:
                    info.slots_total = None
        elif current_section == "Memory Device" and current_module is not None:
            if key == "Size":
                size_bytes = _parse_dmidecode_size(raw=value)
                current_module.size_bytes = size_bytes
            elif key == "Locator":
                current_module.slot = value
            elif key == "Type":
                if value not in ("Unknown", ""):
                    current_module.type = value
            elif key == "Speed":
                speed_mhz = _parse_dmidecode_mhz(raw=value)
                current_module.speed_mhz = speed_mhz
            elif key == "Manufacturer":
                if len(value) > 0 and value != "Unknown":
                    current_module.manufacturer = value
            elif key == "Serial Number":
                if len(value) > 0 and value != "Unknown":
                    current_module.serial_number = value

    if current_module is not None:
        _commit_memory_module(info=info, module=current_module)

    info.slots_used = len(info.modules)
    return info


def _commit_memory_module(
    *,
    info: UbuntuMemoryInfo,
    module: UbuntuMemoryModule,
) -> None:
    """
        Append ``module`` to ``info.modules`` when its size is non-zero.

        ``dmidecode`` emits a ``Memory Device`` block for every DIMM
        slot whether populated or not.  Empty slots show ``Size: No
        Module Installed`` which the size parser maps to zero; we drop
        them here so the modules list reflects only populated DIMMs.
    """
    if module.size_bytes > 0:
        info.modules.append(module)
    return


def _parse_dmidecode_size(*, raw: str) -> int:
    """
        Decode a ``dmidecode`` ``Size`` field into bytes.

        Accepts ``"No Module Installed"`` (-> 0), ``"<n> MB"``,
        ``"<n> GB"``, and ``"<n> kB"``.  Unrecognised forms map to 0.

        :param raw: The raw value text after the ``:`` delimiter.

        :returns: The decoded size in bytes.
    """
    if raw == "No Module Installed":
        return 0

    tokens = raw.split()
    if len(tokens) < 2:
        return 0

    try:
        number = int(tokens[0])
    except ValueError:
        return 0

    unit = tokens[1].lower()
    if unit == "kb":
        bytes_value = number * 1024
    elif unit == "mb":
        bytes_value = number * 1024 ** 2
    elif unit == "gb":
        bytes_value = number * 1024 ** 3
    elif unit == "tb":
        bytes_value = number * 1024 ** 4
    else:
        bytes_value = number

    return bytes_value


def _parse_dmidecode_mhz(*, raw: str) -> Optional[int]:
    """
        Decode a ``dmidecode`` ``Speed`` value as MHz.

        ``Speed`` is reported either as ``"<n> MT/s"`` (for DDR4 / DDR5
        DIMMs) or ``"<n> MHz"`` for older parts.  Both translate
        directly to MHz.  ``"Unknown"`` and unparsable forms yield
        ``None``.
    """
    if raw == "Unknown":
        return None

    tokens = raw.split()
    if len(tokens) < 1:
        return None

    try:
        number = int(tokens[0])
    except ValueError:
        return None

    return number


def _merge_proc_meminfo(*, info: UbuntuMemoryInfo, stdout: str) -> None:
    """
        Merge ``/proc/meminfo`` aggregates onto an existing
        :class:`UbuntuMemoryInfo`.

        Only ``MemTotal`` and ``MemAvailable`` are merged; per-DIMM
        detail comes from ``dmidecode -t memory`` upstream.  Values are
        converted from kB to bytes.
    """
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        tokens = value.split()
        if len(tokens) < 1:
            continue

        try:
            number = int(tokens[0])
        except ValueError:
            continue

        if len(tokens) > 1 and tokens[1].lower() == "kb":
            bytes_value = number * 1024
        else:
            bytes_value = number

        if key == "MemTotal":
            if info.total_bytes == 0:
                info.total_bytes = bytes_value
        elif key == "MemAvailable":
            info.available_bytes = bytes_value

    return


def _parse_lscpu_json(*, stdout: str, command: list[str]) -> UbuntuCpuInfo:
    """
        Parse ``lscpu -J`` JSON into :class:`UbuntuCpuInfo`.

        :param stdout: Raw stdout from ``lscpu -J``.
        :param command: The argv that produced ``stdout`` — used for
                        the exception message when JSON parsing fails.

        :returns: The populated :class:`UbuntuCpuInfo`.
        :raises UbuntuHardwareInventoryQueryError: When ``stdout`` is
                                                   not valid JSON.
    """
    info = UbuntuCpuInfo()

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as ex:
        raise UbuntuHardwareInventoryQueryError(
            command=command,
            exit_code=0,
            stderr="",
            message="lscpu -J emitted malformed JSON: {}".format(ex),
        ) from None

    entries = data.get("lscpu", [])

    for entry in entries:
        field_name = entry.get("field", "").rstrip(":").strip()
        value = entry.get("data", "")

        if field_name == "Architecture":
            info.architecture = value
        elif field_name == "Model name":
            info.model = value
        elif field_name == "Vendor ID":
            info.vendor = value
        elif field_name == "CPU(s)":
            try:
                info.logical_cores = int(value)
            except ValueError:
                info.logical_cores = 0
        elif field_name == "CPU MHz":
            try:
                info.frequency_mhz = int(float(value))
            except ValueError:
                info.frequency_mhz = None
        elif field_name == "CPU max MHz":
            if info.frequency_mhz is None:
                try:
                    info.frequency_mhz = int(float(value))
                except ValueError:
                    info.frequency_mhz = None
        elif field_name == "L1d cache":
            info.cache_l1 = _parse_lscpu_cache_size(raw=value)
        elif field_name == "L2 cache":
            info.cache_l2 = _parse_lscpu_cache_size(raw=value)
        elif field_name == "L3 cache":
            info.cache_l3 = _parse_lscpu_cache_size(raw=value)
        elif field_name == "Flags":
            info.flags = value.split()

    # Compute physical_cores from sockets * cores-per-socket when both
    # are visible.  Otherwise fall back to logical_cores.
    cores_per_socket = 0
    sockets = 0
    for entry in entries:
        field_name = entry.get("field", "").rstrip(":").strip()
        value = entry.get("data", "")
        if field_name == "Core(s) per socket":
            try:
                cores_per_socket = int(value)
            except ValueError:
                cores_per_socket = 0
        elif field_name == "Socket(s)":
            try:
                sockets = int(value)
            except ValueError:
                sockets = 0

    if cores_per_socket > 0 and sockets > 0:
        info.physical_cores = cores_per_socket * sockets
    else:
        info.physical_cores = info.logical_cores

    return info


def _parse_lscpu_cache_size(*, raw: str) -> Optional[int]:
    """
        Decode ``lscpu`` cache size strings into bytes.

        Accepts ``"32K"``, ``"32 KiB"``, ``"6 MiB"``, ``"6M (1 instance)"``,
        and similar.  Returns ``None`` when no leading integer can be
        recovered.
    """
    if raw is None:
        return None

    tokens = raw.split()
    if len(tokens) < 1:
        return None

    head = tokens[0].rstrip(",")
    digits: list[str] = []
    suffix_chars: list[str] = []
    for char in head:
        if char.isdigit():
            digits.append(char)
        else:
            suffix_chars.append(char)

    if len(digits) == 0:
        return None

    number = int("".join(digits))
    suffix = "".join(suffix_chars).lower()

    if len(suffix) == 0 and len(tokens) > 1:
        suffix = tokens[1].lower()

    if suffix.startswith("k"):
        bytes_value = number * 1024
    elif suffix.startswith("m"):
        bytes_value = number * 1024 ** 2
    elif suffix.startswith("g"):
        bytes_value = number * 1024 ** 3
    else:
        bytes_value = number

    return bytes_value


def _parse_lsblk_json(
    *,
    stdout: str,
    command: list[str],
) -> list[UbuntuStorageDeviceInfo]:
    """
        Parse ``lsblk -J`` output into a flat list of devices.

        Walks the parent / children tree depth-first so a partition is
        emitted right after its parent disk.

        :param stdout: Raw stdout from ``lsblk -J``.
        :param command: The argv used (recorded in the exception when
                        JSON parsing fails).

        :returns: One :class:`UbuntuStorageDeviceInfo` per node.
        :raises UbuntuHardwareInventoryQueryError: When ``stdout`` is
                                                   not valid JSON.
    """
    devices: list[UbuntuStorageDeviceInfo] = []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as ex:
        raise UbuntuHardwareInventoryQueryError(
            command=command,
            exit_code=0,
            stderr="",
            message="lsblk -J emitted malformed JSON: {}".format(ex),
        ) from None

    roots = data.get("blockdevices", [])

    def _flatten(node: dict) -> None:
        size_raw = node.get("size", 0)
        if isinstance(size_raw, int):
            size_bytes = size_raw
        else:
            try:
                size_bytes = int(size_raw)
            except (TypeError, ValueError):
                size_bytes = _parse_lsblk_human_size(raw=str(size_raw))

        record = UbuntuStorageDeviceInfo(
            name=node.get("name", ""),
            size_bytes=size_bytes,
            type=node.get("type", "") or "",
            model=node.get("model"),
            serial=node.get("serial"),
            mountpoint=node.get("mountpoint"),
            filesystem=node.get("fstype"),
        )
        devices.append(record)

        for child in node.get("children", []):
            _flatten(child)
        return

    for root in roots:
        _flatten(root)

    return devices


def _parse_lsblk_human_size(*, raw: str) -> int:
    """
        Fallback decoder for ``lsblk`` ``SIZE`` values reported as
        human-readable strings (``"512G"``, ``"1.8T"``).  Returns 0 when
        the head digit-prefix cannot be recovered.
    """
    if raw is None or len(raw) == 0:
        return 0

    digits: list[str] = []
    rest_chars: list[str] = []
    saw_decimal = False
    for char in raw:
        if char.isdigit():
            digits.append(char)
        elif char == "." and saw_decimal is False:
            digits.append(char)
            saw_decimal = True
        else:
            rest_chars.append(char)

    if len(digits) == 0:
        return 0

    try:
        number = float("".join(digits))
    except ValueError:
        return 0

    suffix = "".join(rest_chars).lower()
    if suffix.startswith("k"):
        scale = 1024
    elif suffix.startswith("m"):
        scale = 1024 ** 2
    elif suffix.startswith("g"):
        scale = 1024 ** 3
    elif suffix.startswith("t"):
        scale = 1024 ** 4
    else:
        scale = 1

    bytes_value = int(number * scale)
    return bytes_value


def _parse_ip_link_json(
    *,
    stdout: str,
    command: list[str],
) -> list[UbuntuNetworkDeviceInfo]:
    """
        Parse ``ip -j link show`` JSON into network records.

        ``ip -j`` returns a top-level list of objects, one per
        interface.  The parser maps ``ifname``, ``address``,
        ``operstate``, ``mtu``, and ``link_type`` to the corresponding
        record fields.  ``link_type`` of ``"loopback"`` or
        ``"ether"`` on a typical bridge / tunnel still surfaces as
        ``is_virtual=True`` when the ``ifname`` matches a virtual
        prefix.

        :param stdout: Raw stdout from ``ip -j link show``.
        :param command: The argv used (recorded in the exception on
                        malformed JSON).

        :returns: One :class:`UbuntuNetworkDeviceInfo` per interface.
        :raises UbuntuHardwareInventoryQueryError: When ``stdout`` is
                                                   not valid JSON.
    """
    devices: list[UbuntuNetworkDeviceInfo] = []

    try:
        entries = json.loads(stdout)
    except json.JSONDecodeError as ex:
        raise UbuntuHardwareInventoryQueryError(
            command=command,
            exit_code=0,
            stderr="",
            message="ip -j link show emitted malformed JSON: {}".format(ex),
        ) from None

    if not isinstance(entries, list):
        return devices

    for entry in entries:
        ifname = entry.get("ifname", "")
        link_type = entry.get("link_type", "")
        operstate = entry.get("operstate")
        address = entry.get("address")

        mtu_raw = entry.get("mtu")
        mtu_value: Optional[int]
        if mtu_raw is None:
            mtu_value = None
        else:
            try:
                mtu_value = int(mtu_raw)
            except (TypeError, ValueError):
                mtu_value = None

        is_virtual = _is_virtual_link(ifname=ifname, link_type=link_type)

        record = UbuntuNetworkDeviceInfo(
            name=ifname,
            mac_address=address,
            link_state=operstate,
            speed=None,
            mtu=mtu_value,
            is_virtual=is_virtual,
        )
        devices.append(record)

    return devices


def _is_virtual_link(*, ifname: str, link_type: str) -> bool:
    """
        Heuristic for the ``is_virtual`` field on
        :class:`UbuntuNetworkDeviceInfo`.

        Marks loopback, bridge, tunnel, dummy, veth, vlan, and
        container-managed interfaces as virtual.  Physical NICs
        (``en*``, ``eth*``, ``wl*``) report ``False``.
    """
    if link_type == "loopback":
        return True

    virtual_prefixes = ("lo", "docker", "br-", "veth", "tun", "tap", "vir",
                        "dummy", "vlan", "bond", "wg")
    for prefix in virtual_prefixes:
        if ifname.startswith(prefix):
            return True

    return False


def _parse_lspci_output(*, stdout: str) -> list[UbuntuPciDeviceInfo]:
    """
        Parse ``lspci -mm -nn -k`` output.

        Each device record spans one ``-mm`` line followed by zero or
        more continuation lines (``	Driver:`` /
        ``	Module:``).  ``-mm`` prefixes vendor / device / class
        with quoted strings; ``-nn`` appends ``[xxxx:yyyy]`` numeric
        IDs after each.

        :param stdout: Raw stdout from ``lspci``.

        :returns: One :class:`UbuntuPciDeviceInfo` per primary record.
    """
    devices: list[UbuntuPciDeviceInfo] = []
    current: Optional[UbuntuPciDeviceInfo] = None

    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        if len(line) == 0:
            if current is not None:
                devices.append(current)
                current = None
            continue

        if line.startswith("\t"):
            stripped = line.strip()
            if current is None:
                continue
            if stripped.startswith("Driver:"):
                _, _, driver = stripped.partition(":")
                current.driver = driver.strip()
            continue

        # Primary line.  Fields are space-separated; quoted strings carry
        # vendor / class / device with optional numeric ID tags.
        if current is not None:
            devices.append(current)
            current = None

        slot, class_name, vendor, device, vendor_id, device_id = (
            _parse_lspci_primary_line(line=line)
        )
        current = UbuntuPciDeviceInfo(
            slot=slot,
            class_name=class_name,
            vendor=vendor,
            device=device,
            vendor_id=vendor_id,
            device_id=device_id,
            driver=None,
        )

    if current is not None:
        devices.append(current)

    return devices


def _parse_lspci_primary_line(*, line: str) -> tuple:
    """
        Decode a primary ``lspci -mm -nn`` line into its named fields.

        The ``-mm`` machine-readable format orders fields as:
        ``slot "Class" "Vendor" "Device" -r<rev> -p<progif>``.  ``-nn``
        appends ``[xxxx]`` to the class string and ``[yyyy:zzzz]`` to
        the vendor / device strings.

        :param line: The full primary line text.

        :returns: Tuple of ``(slot, class_name, vendor, device,
                  vendor_id, device_id)``.  Missing fields surface as
                  ``None`` (for the readable strings) or empty strings
                  (for the slot).
    """
    tokens = _split_lspci_tokens(line=line)
    if len(tokens) == 0:
        return ("", None, None, None, None, None)

    slot = tokens[0]

    class_raw = tokens[1] if len(tokens) > 1 else None
    vendor_raw = tokens[2] if len(tokens) > 2 else None
    device_raw = tokens[3] if len(tokens) > 3 else None

    class_name, _ = _strip_lspci_id(raw=class_raw)
    vendor, vendor_id = _strip_lspci_id(raw=vendor_raw)
    device, device_id = _strip_lspci_id(raw=device_raw)

    rtnval = (slot, class_name, vendor, device, vendor_id, device_id)
    return rtnval


def _split_lspci_tokens(*, line: str) -> list[str]:
    """
        Whitespace-split an ``lspci -mm`` primary line, treating
        double-quoted runs as a single token.
    """
    tokens: list[str] = []
    buf: list[str] = []
    in_quote = False

    for char in line:
        if char == '"':
            if in_quote is True:
                tokens.append("".join(buf))
                buf = []
                in_quote = False
            else:
                if len(buf) > 0:
                    tokens.append("".join(buf))
                    buf = []
                in_quote = True
            continue

        if in_quote is False and char.isspace():
            if len(buf) > 0:
                tokens.append("".join(buf))
                buf = []
            continue

        buf.append(char)

    if len(buf) > 0:
        tokens.append("".join(buf))

    return tokens


def _strip_lspci_id(*, raw):
    """
        Split an ``lspci -nn`` token like ``"Intel Corporation [8086]"``
        or ``"Device [1234:5678]"`` into ``(text, id)``.  When no
        bracketed id is present, ``id`` is ``None``.
    """
    if raw is None:
        return (None, None)

    open_index = raw.rfind("[")
    close_index = raw.rfind("]")
    if open_index >= 0 and close_index > open_index:
        text = raw[:open_index].strip()
        id_value = raw[open_index + 1:close_index]
        if len(text) == 0:
            text = None
        return (text, id_value)

    return (raw, None)


def _parse_lsusb_output(*, stdout: str) -> list[UbuntuUsbDeviceInfo]:
    """
        Parse ``lsusb`` output.

        Canonical line shape::

            Bus 001 Device 002: ID 1234:5678 Vendor Product

        :param stdout: Raw stdout from ``lsusb``.

        :returns: One :class:`UbuntuUsbDeviceInfo` per parsed line.
    """
    devices: list[UbuntuUsbDeviceInfo] = []

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if len(line) == 0:
            continue
        parts = line.split(None, 6)
        if len(parts) < 6:
            continue

        bus = parts[1]
        device_id = parts[3].rstrip(":")
        id_pair = parts[5]
        if ":" in id_pair:
            vendor_id, _, product_id = id_pair.partition(":")
        else:
            vendor_id = id_pair
            product_id = ""

        if len(parts) >= 7:
            name = parts[6]
        else:
            name = None

        record = UbuntuUsbDeviceInfo(
            bus=bus,
            device_id=device_id,
            vendor_id=vendor_id,
            product_id=product_id if len(product_id) > 0 else None,
            name=name,
            serial=None,
        )
        devices.append(record)

    return devices
