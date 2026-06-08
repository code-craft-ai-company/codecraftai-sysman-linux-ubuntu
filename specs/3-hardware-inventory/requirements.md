# Requirements

## Introduction

This spec implements the hardware-inventory contract defined at
`codecraftai-sysman-common/specs/contracts/hardware-inventory/contract.md` for
Ubuntu using `dmidecode`, `lscpu`, `lsblk`, `ip`, `lspci`, and `lsusb`.

The hardware-inventory feature provides the `codecraftai-sysman-linux-ubuntu` package
with the ability to query, classify, and aggregate hardware and platform identity
information on Ubuntu systems: DMI/SMBIOS identity, CPU topology, memory
(aggregate plus per-DIMM detail), block-storage devices, network link-layer
hardware, PCI devices, USB devices, and BIOS / UEFI firmware identity.

A maintenance coordinator drives every operation by calling into this feature's
public interfaces.  The feature does not decide *what* to inventory — it provides
the capability to *do it* reliably and safely from a coordinator that has only
read intent.  All system commands are executed exclusively through the
`codecraftai-interfaces` command-runner abstraction, enabling auditing, tracing, and
test mocking without requiring a live system.

This feature is **strictly read-only**.  Every verb is inherently idempotent and
every verb is safe to repeat.  As a consequence:

- There is no dry-run distinction from normal behavior — no command issued by
  this module mutates host state, so dry-run would have no observable effect.
  The `dry_run` field is inherited from `FeatureConfig` for structural uniformity
  but is not honored.
- There is no `DRY_RUN` member on `UbuntuHardwareInventoryActionStatus`.
- Every read passes `elevated=False` to the runner regardless of the
  `elevated` config flag.  The `elevated` field is reserved for the
  selective-elevation patch documented in `tasks.md` but does not gate any
  current verb's elevation choice.

Verbs are READ-ONLY.  Failures surface as
`UbuntuHardwareInventoryQueryError`, never as a per-verb result dataclass.

---

## Requirements

### Requirement 1: Query System Identity

**User Story:** As a maintenance coordinator, I want to query DMI/SMBIOS system
identity fields (manufacturer, product name, serial, UUID, family, version, BIOS
version), so that I can distinguish physical hosts and virtual machines from a
single read.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_system_info()` THE SYSTEM SHALL invoke
   `dmidecode -t system` via the command runner with `elevated=False`.
2. THE SYSTEM SHALL parse the System Information handle block and populate the
   `manufacturer`, `product_name`, `serial_number`, `uuid`, `family`, and
   `version` fields of an `UbuntuSystemInfo` dataclass.
3. THE SYSTEM SHALL also parse the BIOS Information handle block when present
   in the same `-t system` output and populate `bios_version` and
   `firmware_version`.
4. WHEN `dmidecode` returns a non-zero exit code THE SYSTEM SHALL raise
   `UbuntuHardwareInventoryQueryError` carrying the argv, exit code, and stderr.
5. THE SYSTEM SHALL NOT prepend `sudo` or pass `elevated=True` for this call.

---

### Requirement 2: Query CPU Topology and Features

**User Story:** As a maintenance coordinator, I want to query CPU topology
(physical cores, logical cores, model, vendor, architecture, frequency, cache
sizes, feature flags), so that I can record the CPU surface in a hardware
report and reason about feature flags such as `vt-x` or `aes`.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_cpu_info()` THE SYSTEM SHALL invoke
   `lscpu -J` via the command runner with `elevated=False`.
2. THE SYSTEM SHALL JSON-decode the output and return an `UbuntuCpuInfo` whose
   `architecture`, `vendor`, `model`, `logical_cores`, `frequency_mhz`,
   `cache_l1`, `cache_l2`, `cache_l3`, and `flags` fields are populated from
   the corresponding `lscpu` entries.
3. THE SYSTEM SHALL compute `physical_cores` as `Socket(s)` × `Core(s) per
   socket` when both are present; otherwise it SHALL fall back to
   `logical_cores`.
4. WHEN `CPU MHz` is absent but `CPU max MHz` is present THE SYSTEM SHALL use
   the latter for `frequency_mhz`.
5. WHEN `lscpu` returns a non-zero exit code THE SYSTEM SHALL raise
   `UbuntuHardwareInventoryQueryError`.
6. WHEN the captured stdout is not valid JSON THE SYSTEM SHALL raise
   `UbuntuHardwareInventoryQueryError` with a parse-failure message rather
   than surfacing the raw `json.JSONDecodeError`.

---

### Requirement 3: Query Memory (Aggregate and Per-DIMM)

**User Story:** As a maintenance coordinator, I want to query installed memory
and per-DIMM detail, so that I can confirm slot population, manufacturer, and
DRAM speed against a purchase order.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_memory_info()` THE SYSTEM SHALL invoke
   `dmidecode -t memory` and `cat /proc/meminfo`, in that order.
2. THE SYSTEM SHALL parse the `Physical Memory Array.Number Of Devices` line
   into `slots_total` and emit one `UbuntuMemoryModule` per `Memory Device`
   handle block whose `Size` is not `No Module Installed`.
3. THE SYSTEM SHALL set `slots_used` equal to the number of populated modules.
4. THE SYSTEM SHALL convert `dmidecode` `Size: <n> MB|GB|kB|TB` values to
   bytes for `UbuntuMemoryModule.size_bytes`.
5. THE SYSTEM SHALL convert `dmidecode` `Speed: <n> MT/s` and `Speed: <n> MHz`
   values to integer MHz for `UbuntuMemoryModule.speed_mhz`; `Unknown`
   yields `None`.
6. THE SYSTEM SHALL populate `total_bytes` and `available_bytes` from
   `MemTotal:` and `MemAvailable:` lines in `/proc/meminfo` (kB → bytes).
7. WHEN `dmidecode -t memory` fails THE SYSTEM SHALL leave the modules list
   empty and `slots_*` as `None` while still running the `/proc/meminfo` pass
   so that `total_bytes` is populated where the kernel can supply it.
8. WHEN `/proc/meminfo` cannot be read THE SYSTEM SHALL raise
   `UbuntuHardwareInventoryQueryError`.
9. THE SYSTEM SHALL NOT pass `elevated=True` for either call.

---

### Requirement 4: Enumerate Storage Devices

**User Story:** As a maintenance coordinator, I want to enumerate every block
device with name, size, type, model, serial, mountpoint, and filesystem, so
that the storage layout is captured in the inventory record.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_storage_devices()` THE SYSTEM SHALL invoke
   `lsblk -J -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL`.
2. THE SYSTEM SHALL JSON-decode the output and walk parent / `children`
   subtrees depth-first, emitting one `UbuntuStorageDeviceInfo` per node.
3. THE SYSTEM SHALL preserve the `lsblk` `name`, `type`, `model`, `serial`,
   `mountpoint`, and `fstype` fields verbatim, mapping `fstype` to
   `filesystem`.
4. THE SYSTEM SHALL coerce `size` to an integer byte count, falling back to
   the human-readable parser when the field is a string with a unit suffix.
5. WHEN `lsblk` returns a non-zero exit code THE SYSTEM SHALL raise
   `UbuntuHardwareInventoryQueryError`.
6. WHEN the captured stdout is not valid JSON THE SYSTEM SHALL raise
   `UbuntuHardwareInventoryQueryError` with a parse-failure message.

---

### Requirement 5: Enumerate Network Devices

**User Story:** As a maintenance coordinator, I want to enumerate network
interfaces with name, MAC address, link state, MTU, and a virtual / physical
classification, so that the network adapter layout can be audited.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_network_devices()` THE SYSTEM SHALL invoke
   `ip -j link show`.
2. THE SYSTEM SHALL JSON-decode the top-level list and emit one
   `UbuntuNetworkDeviceInfo` per entry.
3. THE SYSTEM SHALL populate `name`, `mac_address`, `link_state`, and `mtu`
   from `ifname`, `address`, `operstate`, and `mtu`.
4. THE SYSTEM SHALL set `is_virtual=True` when `link_type` is `loopback` or
   the `ifname` begins with one of `lo`, `docker`, `br-`, `veth`, `tun`,
   `tap`, `vir`, `dummy`, `vlan`, `bond`, or `wg`.
5. THE SYSTEM SHALL leave `speed` as `None`; the link-speed lookup is the
   responsibility of `network-configuration` and is out of scope here.
6. WHEN `ip` returns a non-zero exit code THE SYSTEM SHALL raise
   `UbuntuHardwareInventoryQueryError`.
7. WHEN the captured stdout is not valid JSON THE SYSTEM SHALL raise
   `UbuntuHardwareInventoryQueryError`.

---

### Requirement 6: Enumerate PCI Devices

**User Story:** As a maintenance coordinator, I want to enumerate PCI devices
with vendor, device, class, vendor / device IDs, and bound driver, so that I
can verify that the expected drivers are loaded for the present hardware.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_pci_devices()` THE SYSTEM SHALL invoke
   `lspci -mm -nn -k`.
2. THE SYSTEM SHALL parse each primary line into `slot`, `class_name`,
   `vendor`, `device`, `vendor_id`, and `device_id`, splitting the
   bracketed `[xxxx]` and `[yyyy:zzzz]` ID tags off of the readable strings.
3. THE SYSTEM SHALL associate any subsequent `\tDriver:` continuation line
   with the most recently emitted device's `driver` field.
4. WHEN `lspci` returns a non-zero exit code THE SYSTEM SHALL raise
   `UbuntuHardwareInventoryQueryError`.

---

### Requirement 7: Enumerate USB Devices

**User Story:** As a maintenance coordinator, I want to enumerate USB devices
on the host, so that attached peripherals can be audited from a maintenance
report.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_usb_devices()` THE SYSTEM SHALL invoke
   `lsusb`.
2. THE SYSTEM SHALL parse each line of the form
   `Bus <bbb> Device <ddd>: ID <vvvv>:<pppp> <name>` and emit one
   `UbuntuUsbDeviceInfo` per parseable line.
3. THE SYSTEM SHALL populate `bus`, `device_id`, `vendor_id`, `product_id`,
   and `name`.
4. THE SYSTEM SHALL leave `serial` as `None`; per-device serials require
   `lsusb -v` and a privileged path that is out of scope here.
5. WHEN `lsusb` returns a non-zero exit code THE SYSTEM SHALL raise
   `UbuntuHardwareInventoryQueryError`.

---

### Requirement 8: Query Firmware (BIOS / UEFI)

**User Story:** As a maintenance coordinator, I want to query the BIOS / UEFI
firmware vendor, version, release date, and UEFI flag, so that firmware
upgrades can be scoped.

#### Acceptance Criteria

1. WHEN the coordinator calls `get_firmware_info()` THE SYSTEM SHALL invoke
   `dmidecode -t bios`.
2. THE SYSTEM SHALL parse the `BIOS Information` handle block and populate
   `vendor`, `version`, and `release_date`.
3. THE SYSTEM SHALL set `uefi=True` when the parsed characteristics include
   the `UEFI is supported` line; otherwise `uefi=False`.
4. THE SYSTEM SHALL leave `secure_boot` as `None`; the sysfs `SecureBoot`
   variable is the proper source and is reserved as a follow-up.
5. WHEN `dmidecode` returns a non-zero exit code THE SYSTEM SHALL raise
   `UbuntuHardwareInventoryQueryError`.

---

### Requirement 9: Read-Only Discipline

**User Story:** As a maintenance coordinator, I want every hardware-inventory
verb to be strictly read-only, so that I can run it under aspect compositions
that retry transient failures without risking host mutation.

#### Acceptance Criteria

1. THE `UbuntuHardwareInventoryActionStatus` enum SHALL contain only
   `SUCCESS`, `FAILED`, and `SKIPPED` members; `DRY_RUN` is intentionally
   absent.
2. `UbuntuHardwareInventoryManagerConfig` SHALL inherit `dry_run` and
   `elevated` from `FeatureConfig` for structural uniformity but SHALL NOT
   consult either field in any verb in the current pilot.
3. Every runner invocation in this module SHALL pass `elevated=False`.
4. No verb in this module SHALL invoke a command that mutates host state.

---

### Requirement 10: Aspect-Transparent Execution

**User Story:** As a maintenance coordinator, I want every hardware-inventory
verb to be safe under retry / persistence / timeout aspects, so that I can
collect inventory across a fleet under whatever runner configuration the
coordinator already has set up.

#### Acceptance Criteria

1. THE manager SHALL hold no state across calls beyond its injected runner and
   config.
2. Each public verb SHALL parse only its own command's output; it SHALL NOT
   read across verbs.
3. WHEN a retry aspect re-runs a transient failure THE SYSTEM SHALL produce
   the same dataclass shape from the retried success that it would have
   produced from a first-try success.
