"""
    Tests for :class:`UbuntuStorageManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``lsblk`` / ``lvs`` / ``vgs`` / ``pvs`` /
    ``lvcreate`` / ``vgcreate`` is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.linux.ubuntu.storagemanagement import (
    UbuntuDiskInfo,
    UbuntuPoolSpec,
    UbuntuStorageActionStatus,
    UbuntuStorageManager,
    UbuntuStorageManagerConfig,
    UbuntuVolumeInfo,
    UbuntuVolumeSpec,
    _parse_lsblk_disks,
    _parse_lsblk_partitions,
    _parse_lvm_size,
    _parse_lvs_output,
    _pv_disks_from_output,
    _pv_in_vg,
    _split_volume_id,
    _vg_present_in_output,
)


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
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


# ----------------------------------------------------------------------
# Argv builders for the canonical lvs / vgs / pvs probes
# ----------------------------------------------------------------------


_LVS_ARGV = ("lvs", "--reportformat", "json", "--units", "b")
_VGS_ARGV = ("vgs", "--reportformat", "json", "--units", "b")
_PVS_ARGV = ("pvs", "--reportformat", "json", "--units", "b")
_LSBLK_DISKS_ARGV = (
    "lsblk", "-J", "-b",
    "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL",
)
_LSBLK_PARTS_ARGV = (
    "lsblk", "-J", "-b",
    "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,KNAME",
)


def _seed_lvs(
    *,
    runner: _MockCommandRunner,
    entries: list,
) -> None:
    """
        Seed the runner with an ``lvs`` JSON response built from
        ``entries`` (list of dicts).
    """
    payload = json.dumps({"report": [{"lv": entries}]})
    runner.results[_LVS_ARGV] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


def _seed_vgs(
    *,
    runner: _MockCommandRunner,
    vg_names: list,
) -> None:
    """
        Seed the runner with a ``vgs`` JSON response listing the given
        VG names.
    """
    entries = [{"vg_name": name} for name in vg_names]
    payload = json.dumps({"report": [{"vg": entries}]})
    runner.results[_VGS_ARGV] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


def _seed_pvs(
    *,
    runner: _MockCommandRunner,
    entries: list,
) -> None:
    """
        Seed the runner with a ``pvs`` JSON response.  Each entry is a
        dict like ``{"pv_name": "/dev/sda", "vg_name": "vg0"}``.
    """
    payload = json.dumps({"report": [{"pv": entries}]})
    runner.results[_PVS_ARGV] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


# ----------------------------------------------------------------------
# list_disks
# ----------------------------------------------------------------------


def test_list_disks_invokes_lsblk_and_parses():
    """
        list_disks invokes lsblk with the expected -o columns and
        parses one UbuntuDiskInfo per type=disk row.  is_pool_member is
        derived from ``pvs``.
    """
    runner = _MockCommandRunner()
    payload = json.dumps({
        "blockdevices": [
            {
                "name": "sda",
                "size": 256000000000,
                "type": "disk",
                "fstype": None,
                "mountpoint": None,
                "model": "Samsung SSD",
                "serial": "ABC123",
                "children": [
                    {
                        "name": "sda1",
                        "size": 256000000000,
                        "type": "part",
                        "fstype": "LVM2_member",
                        "mountpoint": None,
                    },
                ],
            },
            {
                "name": "sdb",
                "size": 1000000000000,
                "type": "disk",
                "fstype": None,
                "mountpoint": None,
                "model": "WD HDD",
                "serial": "XYZ789",
            },
        ],
    })
    runner.results[_LSBLK_DISKS_ARGV] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    _seed_pvs(
        runner=runner,
        entries=[{"pv_name": "/dev/sda1", "vg_name": "vg0"}],
    )

    manager = UbuntuStorageManager(
        runner=runner, config=UbuntuStorageManagerConfig(),
    )
    disks = manager.list_disks()

    testbase.assert_equal(len(disks), 2)
    by_id = {d.id: d for d in disks}
    testbase.assert_true("/dev/sda" in by_id)
    testbase.assert_true("/dev/sdb" in by_id)
    testbase.assert_equal(by_id["/dev/sda"].size_bytes, 256000000000)
    testbase.assert_equal(by_id["/dev/sda"].model, "Samsung SSD")
    testbase.assert_equal(by_id["/dev/sda"].serial, "ABC123")
    testbase.assert_equal(by_id["/dev/sda"].is_pool_member, True)
    testbase.assert_equal(by_id["/dev/sdb"].is_pool_member, False)
    return


# ----------------------------------------------------------------------
# list_volumes
# ----------------------------------------------------------------------


def test_list_volumes_parses_output():
    """
        list_volumes returns LVs (from ``lvs``) and raw partitions
        (from ``lsblk``) joined.
    """
    runner = _MockCommandRunner()
    _seed_lvs(
        runner=runner,
        entries=[
            {
                "lv_name": "data",
                "vg_name": "vg0",
                "lv_size": "10737418240B",
                "data_percent": "25.0",
                "lv_health_status": "",
            },
            {
                "lv_name": "logs",
                "vg_name": "vg0",
                "lv_size": "5368709120B",
                "data_percent": "",
                "lv_health_status": "",
            },
        ],
    )
    parts_payload = json.dumps({
        "blockdevices": [
            {
                "name": "sda",
                "size": 256000000000,
                "type": "disk",
                "children": [
                    {
                        "name": "sda1",
                        "size": 1024000000,
                        "type": "part",
                        "fstype": "ext4",
                        "mountpoint": "/boot",
                    },
                    {
                        "name": "sda2",
                        "size": 254000000000,
                        "type": "part",
                        "fstype": "LVM2_member",
                        "mountpoint": None,
                    },
                ],
            },
        ],
    })
    runner.results[_LSBLK_PARTS_ARGV] = RunResult(
        exit_code=0, stdout=parts_payload, stderr="",
    )

    manager = UbuntuStorageManager(
        runner=runner, config=UbuntuStorageManagerConfig(),
    )
    volumes = manager.list_volumes()

    by_id = {v.id: v for v in volumes}
    testbase.assert_true("vg0/data" in by_id)
    testbase.assert_true("vg0/logs" in by_id)
    testbase.assert_true("/dev/sda1" in by_id)
    testbase.assert_true("/dev/sda2" in by_id)
    testbase.assert_equal(by_id["vg0/data"].size_bytes, 10737418240)
    testbase.assert_equal(by_id["vg0/data"].used_bytes, 2684354560)
    testbase.assert_equal(by_id["vg0/data"].pool, "vg0")
    testbase.assert_equal(by_id["vg0/logs"].used_bytes, None)
    testbase.assert_equal(by_id["/dev/sda1"].mount_point, "/boot")
    testbase.assert_equal(by_id["/dev/sda1"].filesystem, "ext4")
    return


# ----------------------------------------------------------------------
# create_volume
# ----------------------------------------------------------------------


def test_create_volume_invokes_lvcreate():
    """
        create_volume dispatches ``lvcreate -L <bytes>B -n <name>
        <vg>`` with elevation when configured.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_lvs(runner=runner, entries=[])
    spec = UbuntuVolumeSpec(name="data", pool="vg0", size_bytes=10737418240)
    manager = UbuntuStorageManager(
        runner=runner,
        config=UbuntuStorageManagerConfig(elevated=True),
    )
    result = manager.create_volume(spec=spec)

    testbase.assert_equal(result.status, UbuntuStorageActionStatus.SUCCESS)
    testbase.assert_equal(result.target, "vg0/data")
    testbase.assert_equal(result.pool, "vg0")
    testbase.assert_equal(result.volume, "data")
    write_calls = [c for c in runner.calls if c["args"][0] == "lvcreate"]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["lvcreate", "-L", "10737418240B", "-n", "data", "vg0"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_create_volume_skipped_when_already_present():
    """
        create_volume returns SKIPPED when an LV with the same name and
        size already exists in the VG.
    """
    runner = _MockCommandRunner()
    _seed_lvs(
        runner=runner,
        entries=[
            {
                "lv_name": "data",
                "vg_name": "vg0",
                "lv_size": "10737418240B",
                "data_percent": "",
                "lv_health_status": "",
            },
        ],
    )
    spec = UbuntuVolumeSpec(name="data", pool="vg0", size_bytes=10737418240)
    manager = UbuntuStorageManager(
        runner=runner,
        config=UbuntuStorageManagerConfig(elevated=True),
    )
    result = manager.create_volume(spec=spec)

    testbase.assert_equal(result.status, UbuntuStorageActionStatus.SKIPPED)
    write_calls = [c for c in runner.calls if c["args"][0] == "lvcreate"]
    testbase.assert_equal(write_calls, [])
    return


def test_create_volume_dry_run_short_circuits():
    """
        Dry-run records the would-have-been argv without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    spec = UbuntuVolumeSpec(name="data", pool="vg0", size_bytes=10737418240)
    manager = UbuntuStorageManager(
        runner=runner,
        config=UbuntuStorageManagerConfig(dry_run=True),
    )
    result = manager.create_volume(spec=spec)

    testbase.assert_equal(result.status, UbuntuStorageActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command,
        ["lvcreate", "-L", "10737418240B", "-n", "data", "vg0"],
    )
    return


# ----------------------------------------------------------------------
# delete_volume
# ----------------------------------------------------------------------


def test_delete_volume_invokes_lvremove():
    """
        delete_volume dispatches ``lvremove -f <vg>/<lv>`` and the
        SUCCESS result carries a data-loss advisory.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_lvs(
        runner=runner,
        entries=[
            {
                "lv_name": "data",
                "vg_name": "vg0",
                "lv_size": "10737418240B",
                "data_percent": "",
                "lv_health_status": "",
            },
        ],
    )
    manager = UbuntuStorageManager(
        runner=runner,
        config=UbuntuStorageManagerConfig(elevated=True),
    )
    result = manager.delete_volume(volume_id="vg0/data")

    testbase.assert_equal(result.status, UbuntuStorageActionStatus.SUCCESS)
    write_calls = [c for c in runner.calls if c["args"][0] == "lvremove"]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["lvremove", "-f", "vg0/data"],
    )
    testbase.assert_equal(write_calls[0]["elevated"], True)
    testbase.assert_true(result.message is not None)
    testbase.assert_true("destroyed" in result.message)
    return


def test_delete_volume_skipped_when_absent():
    """
        delete_volume returns SKIPPED when the LV does not exist.
    """
    runner = _MockCommandRunner()
    _seed_lvs(runner=runner, entries=[])
    manager = UbuntuStorageManager(
        runner=runner,
        config=UbuntuStorageManagerConfig(elevated=True),
    )
    result = manager.delete_volume(volume_id="vg0/data")

    testbase.assert_equal(result.status, UbuntuStorageActionStatus.SKIPPED)
    write_calls = [c for c in runner.calls if c["args"][0] == "lvremove"]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# resize_volume
# ----------------------------------------------------------------------


def test_resize_volume_invokes_lvresize():
    """
        resize_volume dispatches ``lvresize -L <bytes>B --resizefs
        <vg>/<lv>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_lvs(
        runner=runner,
        entries=[
            {
                "lv_name": "data",
                "vg_name": "vg0",
                "lv_size": "10737418240B",
                "data_percent": "",
                "lv_health_status": "",
            },
        ],
    )
    manager = UbuntuStorageManager(
        runner=runner,
        config=UbuntuStorageManagerConfig(elevated=True),
    )
    result = manager.resize_volume(
        volume_id="vg0/data", size_bytes=21474836480,
    )

    testbase.assert_equal(result.status, UbuntuStorageActionStatus.SUCCESS)
    write_calls = [c for c in runner.calls if c["args"][0] == "lvresize"]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(
        write_calls[0]["args"],
        ["lvresize", "-L", "21474836480B", "--resizefs", "vg0/data"],
    )
    return


def test_resize_volume_skipped_when_size_matches():
    """
        resize_volume returns SKIPPED when the current size equals the
        requested size within tolerance.
    """
    runner = _MockCommandRunner()
    _seed_lvs(
        runner=runner,
        entries=[
            {
                "lv_name": "data",
                "vg_name": "vg0",
                "lv_size": "10737418240B",
                "data_percent": "",
                "lv_health_status": "",
            },
        ],
    )
    manager = UbuntuStorageManager(
        runner=runner,
        config=UbuntuStorageManagerConfig(elevated=True),
    )
    result = manager.resize_volume(
        volume_id="vg0/data", size_bytes=10737418240,
    )

    testbase.assert_equal(result.status, UbuntuStorageActionStatus.SKIPPED)
    write_calls = [c for c in runner.calls if c["args"][0] == "lvresize"]
    testbase.assert_equal(write_calls, [])
    return


# ----------------------------------------------------------------------
# create_pool / delete_pool / add_to_pool / remove_from_pool
# ----------------------------------------------------------------------


def test_create_pool_invokes_pvcreate_then_vgcreate():
    """
        create_pool first runs ``pvcreate <disk>`` for each member,
        then ``vgcreate <name> <disks...>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_vgs(runner=runner, vg_names=[])
    spec = UbuntuPoolSpec(
        name="vg0", disks=["/dev/sdb", "/dev/sdc"],
    )
    manager = UbuntuStorageManager(
        runner=runner,
        config=UbuntuStorageManagerConfig(elevated=True),
    )
    result = manager.create_pool(spec=spec)

    testbase.assert_equal(result.status, UbuntuStorageActionStatus.SUCCESS)
    pvcreate_calls = [c for c in runner.calls if c["args"][0] == "pvcreate"]
    vgcreate_calls = [c for c in runner.calls if c["args"][0] == "vgcreate"]
    testbase.assert_equal(len(pvcreate_calls), 2)
    testbase.assert_equal(pvcreate_calls[0]["args"], ["pvcreate", "/dev/sdb"])
    testbase.assert_equal(pvcreate_calls[1]["args"], ["pvcreate", "/dev/sdc"])
    testbase.assert_equal(len(vgcreate_calls), 1)
    testbase.assert_equal(
        vgcreate_calls[0]["args"],
        ["vgcreate", "vg0", "/dev/sdb", "/dev/sdc"],
    )
    return


def test_create_pool_skipped_when_already_present():
    """
        create_pool returns SKIPPED when the VG already exists.
    """
    runner = _MockCommandRunner()
    _seed_vgs(runner=runner, vg_names=["vg0"])
    spec = UbuntuPoolSpec(name="vg0", disks=["/dev/sdb"])
    manager = UbuntuStorageManager(
        runner=runner,
        config=UbuntuStorageManagerConfig(elevated=True),
    )
    result = manager.create_pool(spec=spec)

    testbase.assert_equal(result.status, UbuntuStorageActionStatus.SKIPPED)
    write_calls = [
        c for c in runner.calls if c["args"][0] in ("pvcreate", "vgcreate")
    ]
    testbase.assert_equal(write_calls, [])
    return


def test_delete_pool_invokes_vgremove():
    """
        delete_pool dispatches ``vgremove -f <name>`` and the SUCCESS
        result carries a data-loss advisory.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_vgs(runner=runner, vg_names=["vg0"])
    manager = UbuntuStorageManager(
        runner=runner,
        config=UbuntuStorageManagerConfig(elevated=True),
    )
    result = manager.delete_pool(pool_name="vg0")

    testbase.assert_equal(result.status, UbuntuStorageActionStatus.SUCCESS)
    write_calls = [c for c in runner.calls if c["args"][0] == "vgremove"]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["args"], ["vgremove", "-f", "vg0"])
    testbase.assert_true(result.message is not None)
    testbase.assert_true("destroyed" in result.message)
    return


def test_add_to_pool_invokes_pvcreate_and_vgextend():
    """
        add_to_pool first runs ``pvcreate <disk>``, then ``vgextend
        <pool> <disk>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_pvs(runner=runner, entries=[])
    manager = UbuntuStorageManager(
        runner=runner,
        config=UbuntuStorageManagerConfig(elevated=True),
    )
    result = manager.add_to_pool(pool_name="vg0", disk_id="/dev/sdb")

    testbase.assert_equal(result.status, UbuntuStorageActionStatus.SUCCESS)
    pvcreate_calls = [c for c in runner.calls if c["args"][0] == "pvcreate"]
    vgextend_calls = [c for c in runner.calls if c["args"][0] == "vgextend"]
    testbase.assert_equal(len(pvcreate_calls), 1)
    testbase.assert_equal(pvcreate_calls[0]["args"], ["pvcreate", "/dev/sdb"])
    testbase.assert_equal(len(vgextend_calls), 1)
    testbase.assert_equal(
        vgextend_calls[0]["args"], ["vgextend", "vg0", "/dev/sdb"],
    )
    return


def test_remove_from_pool_invokes_vgreduce_and_pvremove():
    """
        remove_from_pool dispatches ``vgreduce`` then ``pvremove`` and
        carries a data-loss advisory on success.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_pvs(
        runner=runner,
        entries=[{"pv_name": "/dev/sdb", "vg_name": "vg0"}],
    )
    manager = UbuntuStorageManager(
        runner=runner,
        config=UbuntuStorageManagerConfig(elevated=True),
    )
    result = manager.remove_from_pool(pool_name="vg0", disk_id="/dev/sdb")

    testbase.assert_equal(result.status, UbuntuStorageActionStatus.SUCCESS)
    vgreduce_calls = [c for c in runner.calls if c["args"][0] == "vgreduce"]
    pvremove_calls = [c for c in runner.calls if c["args"][0] == "pvremove"]
    testbase.assert_equal(len(vgreduce_calls), 1)
    testbase.assert_equal(
        vgreduce_calls[0]["args"], ["vgreduce", "vg0", "/dev/sdb"],
    )
    testbase.assert_equal(len(pvremove_calls), 1)
    testbase.assert_equal(pvremove_calls[0]["args"], ["pvremove", "/dev/sdb"])
    testbase.assert_true(result.message is not None)
    testbase.assert_true("wiped" in result.message)
    return


# ----------------------------------------------------------------------
# get_volume_info / get_disk_info
# ----------------------------------------------------------------------


def test_get_volume_info_parses_output():
    """
        get_volume_info returns the LV when ``lvs`` reports it.
    """
    runner = _MockCommandRunner()
    _seed_lvs(
        runner=runner,
        entries=[
            {
                "lv_name": "data",
                "vg_name": "vg0",
                "lv_size": "10737418240B",
                "data_percent": "50.0",
                "lv_health_status": "",
            },
        ],
    )
    runner.results[_LSBLK_PARTS_ARGV] = RunResult(
        exit_code=0, stdout=json.dumps({"blockdevices": []}), stderr="",
    )
    manager = UbuntuStorageManager(
        runner=runner, config=UbuntuStorageManagerConfig(),
    )
    info = manager.get_volume_info(volume_id="vg0/data")

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.id, "vg0/data")
    testbase.assert_equal(info.size_bytes, 10737418240)
    testbase.assert_equal(info.pool, "vg0")
    testbase.assert_equal(info.used_bytes, 5368709120)
    return


def test_get_disk_info_parses_output():
    """
        get_disk_info returns the disk row matching the given id.
    """
    runner = _MockCommandRunner()
    payload = json.dumps({
        "blockdevices": [
            {
                "name": "sdb",
                "size": 1000000000000,
                "type": "disk",
                "model": "WD HDD",
                "serial": "XYZ",
            },
        ],
    })
    runner.results[_LSBLK_DISKS_ARGV] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    _seed_pvs(runner=runner, entries=[])

    manager = UbuntuStorageManager(
        runner=runner, config=UbuntuStorageManagerConfig(),
    )
    info = manager.get_disk_info(disk_id="/dev/sdb")

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.size_bytes, 1000000000000)
    testbase.assert_equal(info.model, "WD HDD")
    testbase.assert_equal(info.is_pool_member, False)
    return


# ----------------------------------------------------------------------
# Elevation contract
# ----------------------------------------------------------------------


def test_writes_elevate():
    """
        Every write verb passes ``elevated=True`` when the config
        enables elevation.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_lvs(runner=runner, entries=[])
    _seed_vgs(runner=runner, vg_names=[])
    _seed_pvs(runner=runner, entries=[])
    manager = UbuntuStorageManager(
        runner=runner,
        config=UbuntuStorageManagerConfig(elevated=True),
    )
    spec = UbuntuVolumeSpec(name="data", pool="vg0", size_bytes=1024 * 1024 * 1024 * 10)
    manager.create_volume(spec=spec)
    write_calls = [c for c in runner.calls if c["args"][0] == "lvcreate"]
    testbase.assert_equal(len(write_calls), 1)
    testbase.assert_equal(write_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        list_disks / list_volumes / get_volume_info / get_disk_info
        never pass ``elevated=True`` even when the config requests
        elevation for writes.
    """
    runner = _MockCommandRunner()
    runner.results[_LSBLK_DISKS_ARGV] = RunResult(
        exit_code=0, stdout=json.dumps({"blockdevices": []}), stderr="",
    )
    runner.results[_LSBLK_PARTS_ARGV] = RunResult(
        exit_code=0, stdout=json.dumps({"blockdevices": []}), stderr="",
    )
    _seed_lvs(runner=runner, entries=[])
    _seed_pvs(runner=runner, entries=[])
    manager = UbuntuStorageManager(
        runner=runner,
        config=UbuntuStorageManagerConfig(elevated=True),
    )
    manager.list_disks()
    manager.list_volumes()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Error surface
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        FAILED writes carry the runner's exit code, stdout, and stderr.
    """
    runner = _MockCommandRunner()
    _seed_lvs(runner=runner, entries=[])
    runner.default_result = RunResult(
        exit_code=5, stdout="", stderr="LV creation failed",
    )
    spec = UbuntuVolumeSpec(name="data", pool="vg0", size_bytes=1024 * 1024)
    manager = UbuntuStorageManager(
        runner=runner,
        config=UbuntuStorageManagerConfig(elevated=True),
    )
    result = manager.create_volume(spec=spec)

    testbase.assert_equal(result.status, UbuntuStorageActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 5)
    testbase.assert_equal(result.stderr, "LV creation failed")
    return


# ----------------------------------------------------------------------
# Module-level parser tests
# ----------------------------------------------------------------------


def test_split_volume_id_handles_slash_separator():
    """
        ``_split_volume_id`` splits ``vg/lv`` into a tuple.
    """
    rtnval = _split_volume_id(volume_id="vg0/data")
    testbase.assert_equal(rtnval, ("vg0", "data"))
    return


def test_parse_lvm_size_strips_trailing_b():
    """
        ``_parse_lvm_size`` strips a trailing ``B`` and returns int.
    """
    rtnval = _parse_lvm_size(size_str="42B")
    testbase.assert_equal(rtnval, 42)
    return


def test_vg_present_in_output_returns_true_for_match():
    """
        ``_vg_present_in_output`` finds a VG by name.
    """
    payload = json.dumps({"report": [{"vg": [{"vg_name": "vg0"}]}]})
    rtnval = _vg_present_in_output(output=payload, vg_name="vg0")
    testbase.assert_equal(rtnval, True)
    return


def test_pv_in_vg_matches_both_fields():
    """
        ``_pv_in_vg`` requires both pv_name and vg_name to match.
    """
    payload = json.dumps({"report": [
        {"pv": [{"pv_name": "/dev/sda", "vg_name": "vg0"}]},
    ]})
    rtnval = _pv_in_vg(
        output=payload, pv_name="/dev/sda", vg_name="vg0",
    )
    testbase.assert_equal(rtnval, True)
    return


def test_pv_disks_from_output_collects_pv_names():
    """
        ``_pv_disks_from_output`` returns the set of pv_name values.
    """
    payload = json.dumps({"report": [
        {"pv": [
            {"pv_name": "/dev/sda", "vg_name": "vg0"},
            {"pv_name": "/dev/sdb", "vg_name": "vg0"},
        ]},
    ]})
    rtnval = _pv_disks_from_output(output=payload)
    testbase.assert_true("/dev/sda" in rtnval)
    testbase.assert_true("/dev/sdb" in rtnval)
    return
