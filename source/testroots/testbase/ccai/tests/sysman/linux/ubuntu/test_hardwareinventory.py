"""
    Tests for :class:`UbuntuHardwareInventoryManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run`` call
    and returns pre-seeded :class:`RunResult` values keyed by argv.  No
    live ``dmidecode`` / ``lscpu`` / ``lsblk`` / ``lspci`` / ``lsusb`` /
    ``ip`` is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.hardwareinventory import (
    UbuntuHardwareInventoryManager,
    UbuntuHardwareInventoryManagerConfig,
    UbuntuHardwareInventoryQueryError,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in that records every call and
        returns pre-seeded :class:`RunResult` values keyed by argv tuple.
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
            exit_code=1,
            stdout="",
            stderr="not implemented",
        )
        return rtnval


def _ok(stdout: str = "") -> RunResult:
    """
        Build a canonical success :class:`RunResult` for the mock.
    """
    rtnval = RunResult(exit_code=0, stdout=stdout, stderr="")
    return rtnval


def _fail(*, exit_code: int = 1, stderr: str = "boom") -> RunResult:
    """
        Build a canonical failure :class:`RunResult` for the mock.
    """
    rtnval = RunResult(exit_code=exit_code, stdout="", stderr=stderr)
    return rtnval


# ----------------------------------------------------------------------
# get_system_info
# ----------------------------------------------------------------------


def test_get_system_info_invokes_dmidecode_and_parses():
    """
        get_system_info() shells out to ``dmidecode -t system`` and
        parses the System Information handle block.
    """
    dmi_output = (
        "# dmidecode 3.3\n"
        "Handle 0x0001, DMI type 1, 27 bytes\n"
        "System Information\n"
        "\tManufacturer: ACME Corp\n"
        "\tProduct Name: Widget 9000\n"
        "\tVersion: r1.2\n"
        "\tSerial Number: SN-AB-12\n"
        "\tUUID: 11111111-2222-3333-4444-555555555555\n"
        "\tFamily: Widget\n"
        "\n"
    )
    runner = _MockCommandRunner()
    runner.results[("dmidecode", "-t", "system")] = _ok(stdout=dmi_output)

    manager = UbuntuHardwareInventoryManager(
        runner, UbuntuHardwareInventoryManagerConfig(),
    )
    info = manager.get_system_info()

    testbase.assert_equal(info.manufacturer, "ACME Corp")
    testbase.assert_equal(info.product_name, "Widget 9000")
    testbase.assert_equal(info.serial_number, "SN-AB-12")
    testbase.assert_equal(
        info.uuid, "11111111-2222-3333-4444-555555555555",
    )
    testbase.assert_equal(info.family, "Widget")
    testbase.assert_equal(info.version, "r1.2")
    testbase.assert_equal(runner.calls[0]["args"], ["dmidecode", "-t", "system"])
    testbase.assert_equal(runner.calls[0]["elevated"], False)
    return


# ----------------------------------------------------------------------
# get_cpu_info
# ----------------------------------------------------------------------


def test_get_cpu_info_parses_lscpu_output():
    """
        get_cpu_info() parses the JSON emitted by ``lscpu -J``.
    """
    payload = json.dumps({
        "lscpu": [
            {"field": "Architecture:", "data": "x86_64"},
            {"field": "CPU(s):", "data": "8"},
            {"field": "Vendor ID:", "data": "GenuineIntel"},
            {"field": "Model name:", "data": "Intel(R) Xeon(R) E5"},
            {"field": "CPU MHz:", "data": "2400.123"},
            {"field": "Core(s) per socket:", "data": "4"},
            {"field": "Socket(s):", "data": "1"},
            {"field": "L1d cache:", "data": "32K"},
            {"field": "L2 cache:", "data": "256K"},
            {"field": "L3 cache:", "data": "8M"},
            {"field": "Flags:", "data": "fpu vme sse sse2 avx"},
        ],
    })
    runner = _MockCommandRunner()
    runner.results[("lscpu", "-J")] = _ok(stdout=payload)

    manager = UbuntuHardwareInventoryManager(
        runner, UbuntuHardwareInventoryManagerConfig(),
    )
    info = manager.get_cpu_info()

    testbase.assert_equal(info.architecture, "x86_64")
    testbase.assert_equal(info.logical_cores, 8)
    testbase.assert_equal(info.physical_cores, 4)
    testbase.assert_equal(info.vendor, "GenuineIntel")
    testbase.assert_equal(info.model, "Intel(R) Xeon(R) E5")
    testbase.assert_equal(info.frequency_mhz, 2400)
    testbase.assert_equal(info.cache_l1, 32 * 1024)
    testbase.assert_equal(info.cache_l2, 256 * 1024)
    testbase.assert_equal(info.cache_l3, 8 * 1024 ** 2)
    testbase.assert_equal(info.flags, ["fpu", "vme", "sse", "sse2", "avx"])
    return


# ----------------------------------------------------------------------
# get_memory_info
# ----------------------------------------------------------------------


def test_get_memory_info_aggregates_modules_and_total():
    """
        get_memory_info() aggregates per-DIMM rows from
        ``dmidecode -t memory`` and merges ``MemTotal`` /
        ``MemAvailable`` from ``/proc/meminfo``.
    """
    dmi_memory = (
        "Handle 0x0008, DMI type 16, 23 bytes\n"
        "Physical Memory Array\n"
        "\tNumber Of Devices: 4\n"
        "\n"
        "Handle 0x0009, DMI type 17, 40 bytes\n"
        "Memory Device\n"
        "\tLocator: DIMM_A1\n"
        "\tSize: 8192 MB\n"
        "\tType: DDR4\n"
        "\tSpeed: 2400 MT/s\n"
        "\tManufacturer: ACME\n"
        "\tSerial Number: 12345\n"
        "\n"
        "Handle 0x000A, DMI type 17, 40 bytes\n"
        "Memory Device\n"
        "\tLocator: DIMM_A2\n"
        "\tSize: No Module Installed\n"
        "\tType: Unknown\n"
        "\tSpeed: Unknown\n"
        "\n"
    )
    proc_meminfo = (
        "MemTotal:       16384000 kB\n"
        "MemAvailable:   12288000 kB\n"
    )
    runner = _MockCommandRunner()
    runner.results[("dmidecode", "-t", "memory")] = _ok(stdout=dmi_memory)
    runner.results[("cat", "/proc/meminfo")] = _ok(stdout=proc_meminfo)

    manager = UbuntuHardwareInventoryManager(
        runner, UbuntuHardwareInventoryManagerConfig(),
    )
    info = manager.get_memory_info()

    testbase.assert_equal(info.slots_total, 4)
    testbase.assert_equal(info.slots_used, 1)
    testbase.assert_equal(len(info.modules), 1)
    testbase.assert_equal(info.modules[0].slot, "DIMM_A1")
    testbase.assert_equal(info.modules[0].size_bytes, 8192 * 1024 ** 2)
    testbase.assert_equal(info.modules[0].type, "DDR4")
    testbase.assert_equal(info.modules[0].speed_mhz, 2400)
    testbase.assert_equal(info.modules[0].manufacturer, "ACME")
    testbase.assert_equal(info.modules[0].serial_number, "12345")
    testbase.assert_equal(info.total_bytes, 16384000 * 1024)
    testbase.assert_equal(info.available_bytes, 12288000 * 1024)
    return


# ----------------------------------------------------------------------
# list_storage_devices
# ----------------------------------------------------------------------


def test_list_storage_devices_parses_lsblk_output():
    """
        list_storage_devices() flattens parent / children trees from
        ``lsblk -J``.
    """
    payload = json.dumps({
        "blockdevices": [
            {
                "name": "sda",
                "size": 500107862016,
                "type": "disk",
                "fstype": None,
                "mountpoint": None,
                "model": "Samsung SSD 870",
                "serial": "S1AB",
                "children": [
                    {
                        "name": "sda1",
                        "size": 500106788864,
                        "type": "part",
                        "fstype": "ext4",
                        "mountpoint": "/",
                        "model": None,
                        "serial": None,
                    },
                ],
            },
        ],
    })
    runner = _MockCommandRunner()
    key = (
        "lsblk", "-J", "-o",
        "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL",
    )
    runner.results[key] = _ok(stdout=payload)

    manager = UbuntuHardwareInventoryManager(
        runner, UbuntuHardwareInventoryManagerConfig(),
    )
    devices = manager.list_storage_devices()

    testbase.assert_equal(len(devices), 2)
    testbase.assert_equal(devices[0].name, "sda")
    testbase.assert_equal(devices[0].size_bytes, 500107862016)
    testbase.assert_equal(devices[0].type, "disk")
    testbase.assert_equal(devices[0].model, "Samsung SSD 870")
    testbase.assert_equal(devices[0].serial, "S1AB")
    testbase.assert_equal(devices[1].name, "sda1")
    testbase.assert_equal(devices[1].mountpoint, "/")
    testbase.assert_equal(devices[1].filesystem, "ext4")
    return


# ----------------------------------------------------------------------
# list_network_devices
# ----------------------------------------------------------------------


def test_list_network_devices_parses_ip_link_output():
    """
        list_network_devices() parses ``ip -j link show`` JSON.
    """
    payload = json.dumps([
        {
            "ifname": "lo",
            "address": "00:00:00:00:00:00",
            "operstate": "UNKNOWN",
            "mtu": 65536,
            "link_type": "loopback",
        },
        {
            "ifname": "enp0s3",
            "address": "08:00:27:11:22:33",
            "operstate": "UP",
            "mtu": 1500,
            "link_type": "ether",
        },
        {
            "ifname": "docker0",
            "address": "02:42:b1:c2:d3:e4",
            "operstate": "DOWN",
            "mtu": 1500,
            "link_type": "ether",
        },
    ])
    runner = _MockCommandRunner()
    runner.results[("ip", "-j", "link", "show")] = _ok(stdout=payload)

    manager = UbuntuHardwareInventoryManager(
        runner, UbuntuHardwareInventoryManagerConfig(),
    )
    devices = manager.list_network_devices()

    testbase.assert_equal(len(devices), 3)
    testbase.assert_equal(devices[0].name, "lo")
    testbase.assert_equal(devices[0].is_virtual, True)
    testbase.assert_equal(devices[0].mtu, 65536)
    testbase.assert_equal(devices[1].name, "enp0s3")
    testbase.assert_equal(devices[1].is_virtual, False)
    testbase.assert_equal(devices[1].mac_address, "08:00:27:11:22:33")
    testbase.assert_equal(devices[2].name, "docker0")
    testbase.assert_equal(devices[2].is_virtual, True)
    return


# ----------------------------------------------------------------------
# list_pci_devices
# ----------------------------------------------------------------------


def test_list_pci_devices_parses_lspci_output():
    """
        list_pci_devices() parses the multi-line records emitted by
        ``lspci -mm -nn -k``.
    """
    output = (
        '00:00.0 "Host bridge [0600]" "Intel Corporation [8086]" '
        '"5520 I/O Hub [3406]" -r13 -p00 "Dell [1028]" "Device [02a3]"\n'
        '\tDriver: pcieport\n'
        '\n'
        '00:1f.3 "SMBus [0c05]" "Intel Corporation [8086]" '
        '"PCH SMBus [a323]" -r10\n'
        '\tDriver: i801_smbus\n'
        '\tModule: i2c_i801\n'
        '\n'
    )
    runner = _MockCommandRunner()
    runner.results[("lspci", "-mm", "-nn", "-k")] = _ok(stdout=output)

    manager = UbuntuHardwareInventoryManager(
        runner, UbuntuHardwareInventoryManagerConfig(),
    )
    devices = manager.list_pci_devices()

    testbase.assert_equal(len(devices), 2)
    testbase.assert_equal(devices[0].slot, "00:00.0")
    testbase.assert_equal(devices[0].vendor, "Intel Corporation")
    testbase.assert_equal(devices[0].vendor_id, "8086")
    testbase.assert_equal(devices[0].class_name, "Host bridge")
    testbase.assert_equal(devices[0].device, "5520 I/O Hub")
    testbase.assert_equal(devices[0].device_id, "3406")
    testbase.assert_equal(devices[0].driver, "pcieport")
    testbase.assert_equal(devices[1].slot, "00:1f.3")
    testbase.assert_equal(devices[1].driver, "i801_smbus")
    return


# ----------------------------------------------------------------------
# list_usb_devices
# ----------------------------------------------------------------------


def test_list_usb_devices_parses_lsusb_output():
    """
        list_usb_devices() parses one record per ``lsusb`` line.
    """
    output = (
        "Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub\n"
        "Bus 001 Device 002: ID 0951:1666 Kingston Technology DataTraveler\n"
    )
    runner = _MockCommandRunner()
    runner.results[("lsusb",)] = _ok(stdout=output)

    manager = UbuntuHardwareInventoryManager(
        runner, UbuntuHardwareInventoryManagerConfig(),
    )
    devices = manager.list_usb_devices()

    testbase.assert_equal(len(devices), 2)
    testbase.assert_equal(devices[0].bus, "002")
    testbase.assert_equal(devices[0].device_id, "001")
    testbase.assert_equal(devices[0].vendor_id, "1d6b")
    testbase.assert_equal(devices[0].product_id, "0003")
    testbase.assert_equal(
        devices[0].name, "Linux Foundation 3.0 root hub",
    )
    testbase.assert_equal(devices[1].vendor_id, "0951")
    testbase.assert_equal(devices[1].product_id, "1666")
    return


# ----------------------------------------------------------------------
# get_firmware_info
# ----------------------------------------------------------------------


def test_get_firmware_info_parses_dmidecode_bios_output():
    """
        get_firmware_info() parses ``dmidecode -t bios`` BIOS Information.
    """
    output = (
        "Handle 0x0000, DMI type 0, 24 bytes\n"
        "BIOS Information\n"
        "\tVendor: American Megatrends Inc.\n"
        "\tVersion: 1.7.2\n"
        "\tRelease Date: 06/01/2023\n"
        "\tCharacteristics:\n"
        "\t\tISA is supported\n"
        "\t\tUEFI is supported\n"
        "\n"
    )
    runner = _MockCommandRunner()
    runner.results[("dmidecode", "-t", "bios")] = _ok(stdout=output)

    manager = UbuntuHardwareInventoryManager(
        runner, UbuntuHardwareInventoryManagerConfig(),
    )
    info = manager.get_firmware_info()

    testbase.assert_equal(info.vendor, "American Megatrends Inc.")
    testbase.assert_equal(info.version, "1.7.2")
    testbase.assert_equal(info.release_date, "06/01/2023")
    testbase.assert_equal(info.uefi, True)
    return


# ----------------------------------------------------------------------
# Failure paths
# ----------------------------------------------------------------------


def test_failed_command_raises_query_error():
    """
        A non-zero runner result raises
        :class:`UbuntuHardwareInventoryQueryError` carrying the argv,
        exit code, and stderr.
    """
    runner = _MockCommandRunner()
    runner.results[("lscpu", "-J")] = _fail(
        exit_code=2, stderr="lscpu: cannot read /proc/cpuinfo",
    )
    manager = UbuntuHardwareInventoryManager(
        runner, UbuntuHardwareInventoryManagerConfig(),
    )

    raised = False
    try:
        manager.get_cpu_info()
    except UbuntuHardwareInventoryQueryError as ex:
        raised = True
        testbase.assert_equal(ex.exit_code, 2)
        testbase.assert_equal(ex.command, ["lscpu", "-J"])
        testbase.assert_true("lscpu" in ex.stderr)

    testbase.assert_true(raised)
    return


def test_reads_never_elevate():
    """
        Every read verb dispatches with ``elevated=False`` regardless of
        ``config.elevated``.
    """
    runner = _MockCommandRunner()
    runner.default_result = _ok(stdout='{"lscpu": []}')
    runner.results[("lscpu", "-J")] = _ok(stdout='{"lscpu": []}')
    runner.results[("dmidecode", "-t", "system")] = _ok(stdout="")
    runner.results[("dmidecode", "-t", "memory")] = _ok(stdout="")
    runner.results[("cat", "/proc/meminfo")] = _ok(stdout="MemTotal: 1 kB\n")
    runner.results[("dmidecode", "-t", "bios")] = _ok(stdout="")
    runner.results[(
        "lsblk", "-J", "-o",
        "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL",
    )] = _ok(stdout='{"blockdevices": []}')
    runner.results[("ip", "-j", "link", "show")] = _ok(stdout="[]")
    runner.results[("lspci", "-mm", "-nn", "-k")] = _ok(stdout="")
    runner.results[("lsusb",)] = _ok(stdout="")

    config = UbuntuHardwareInventoryManagerConfig(elevated=True)
    manager = UbuntuHardwareInventoryManager(runner, config)

    manager.get_system_info()
    manager.get_cpu_info()
    manager.get_memory_info()
    manager.list_storage_devices()
    manager.list_network_devices()
    manager.list_pci_devices()
    manager.list_usb_devices()
    manager.get_firmware_info()

    elevated_calls: list = []
    for record in runner.calls:
        if record["elevated"] is True:
            elevated_calls.append(record)

    testbase.assert_equal(len(elevated_calls), 0)
    return
