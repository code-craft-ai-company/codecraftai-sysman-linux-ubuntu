"""
    Tests for :class:`UbuntuContainerManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``docker`` / ``podman`` is invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from automojo import testbase

from automojo.interfaces.icommandrunner import RunResult

from automojo.sysman.linux.ubuntu.containermanagement import (
    UbuntuContainerActionStatus,
    UbuntuContainerEngine,
    UbuntuContainerManager,
    UbuntuContainerManagerConfig,
    UbuntuContainerSpec,
    _build_create_argv,
    _parse_container_ps_lines,
    _parse_image_lines,
    _parse_inspect_first,
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
# Argv builders for canonical probes
# ----------------------------------------------------------------------


_DOCKER = "docker"
_PODMAN = "podman"


def _images_filter_argv(*, engine: str, ref: str) -> tuple:
    """
        Return the canonical argv for the ``image present`` probe.
    """
    rtnval = (
        engine, "images",
        "--filter", "reference={}".format(ref),
        "--format", "{{json .}}",
    )
    return rtnval


def _inspect_argv(*, engine: str, target: str) -> tuple:
    """
        Return the canonical argv for the inspect probe.
    """
    rtnval = (engine, "inspect", target)
    return rtnval


def _seed_image_present(
    *,
    runner: _MockCommandRunner,
    engine: str,
    ref: str,
    present: bool,
) -> None:
    """
        Seed the runner so the ``image present`` probe answers the
        given way.
    """
    key = _images_filter_argv(engine=engine, ref=ref)
    if present:
        payload = json.dumps({"ID": "sha256:abc", "Repository": "x"}) + "\n"
    else:
        payload = ""
    runner.results[key] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


def _seed_inspect_absent(
    *,
    runner: _MockCommandRunner,
    engine: str,
    target: str,
) -> None:
    """
        Seed the runner so ``<engine> inspect <target>`` returns a
        non-zero exit (the way both engines signal absence).
    """
    key = _inspect_argv(engine=engine, target=target)
    runner.results[key] = RunResult(
        exit_code=1, stdout="", stderr="No such object",
    )
    return


def _seed_inspect_record(
    *,
    runner: _MockCommandRunner,
    engine: str,
    target: str,
    record: dict,
) -> None:
    """
        Seed the runner so ``<engine> inspect <target>`` returns a
        JSON array containing ``record``.
    """
    key = _inspect_argv(engine=engine, target=target)
    payload = json.dumps([record])
    runner.results[key] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


# ----------------------------------------------------------------------
# pull_image
# ----------------------------------------------------------------------


def test_pull_image_invokes_engine_pull():
    """
        pull_image dispatches ``docker pull <ref>`` when the image is
        not present.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_image_present(
        runner=runner, engine=_DOCKER, ref="nginx:1.25", present=False,
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    result = manager.pull_image(ref="nginx:1.25")

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.SUCCESS)
    pull_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["docker", "pull"]
    ]
    testbase.assert_equal(len(pull_calls), 1)
    testbase.assert_equal(
        pull_calls[0]["args"], ["docker", "pull", "nginx:1.25"],
    )
    testbase.assert_equal(result.engine, UbuntuContainerEngine.DOCKER)
    return


def test_pull_image_skipped_when_already_present():
    """
        pull_image returns SKIPPED when the engine reports the image
        is already present.
    """
    runner = _MockCommandRunner()
    _seed_image_present(
        runner=runner, engine=_DOCKER, ref="nginx:1.25", present=True,
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    result = manager.pull_image(ref="nginx:1.25")

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.SKIPPED)
    pull_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["docker", "pull"]
    ]
    testbase.assert_equal(pull_calls, [])
    return


def test_pull_image_force_pull_overrides_skip():
    """
        force_pull=True suppresses the SKIPPED guard so pull always
        runs.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_image_present(
        runner=runner, engine=_DOCKER, ref="nginx:1.25", present=True,
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(force_pull=True),
    )
    result = manager.pull_image(ref="nginx:1.25")

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.SUCCESS)
    pull_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["docker", "pull"]
    ]
    testbase.assert_equal(len(pull_calls), 1)
    return


def test_pull_image_dry_run_short_circuits():
    """
        Dry-run records the would-have-been argv without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(dry_run=True),
    )
    result = manager.pull_image(ref="nginx:1.25")

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command, ["docker", "pull", "nginx:1.25"],
    )
    return


# ----------------------------------------------------------------------
# remove_image
# ----------------------------------------------------------------------


def test_remove_image_invokes_rmi():
    """
        remove_image dispatches ``docker rmi <ref>`` when the image is
        present.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_image_present(
        runner=runner, engine=_DOCKER, ref="nginx:1.25", present=True,
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    result = manager.remove_image(ref="nginx:1.25")

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.SUCCESS)
    rmi_calls = [c for c in runner.calls if c["args"][:2] == ["docker", "rmi"]]
    testbase.assert_equal(len(rmi_calls), 1)
    testbase.assert_equal(
        rmi_calls[0]["args"], ["docker", "rmi", "nginx:1.25"],
    )
    return


def test_remove_image_skipped_when_absent():
    """
        remove_image returns SKIPPED when the image is not present.
    """
    runner = _MockCommandRunner()
    _seed_image_present(
        runner=runner, engine=_DOCKER, ref="nginx:1.25", present=False,
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    result = manager.remove_image(ref="nginx:1.25")

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.SKIPPED)
    rmi_calls = [c for c in runner.calls if c["args"][:2] == ["docker", "rmi"]]
    testbase.assert_equal(rmi_calls, [])
    return


# ----------------------------------------------------------------------
# list_images
# ----------------------------------------------------------------------


def test_list_images_parses_json_lines():
    """
        list_images parses one JSON object per line into
        UbuntuImageInfo rows.
    """
    runner = _MockCommandRunner()
    payload_lines = [
        json.dumps({
            "ID": "sha256:abc",
            "Repository": "nginx",
            "Tag": "1.25",
            "Digest": "sha256:def",
            "Size": 12345678,
            "CreatedAt": "2025-01-01T00:00:00Z",
        }),
        json.dumps({
            "ID": "sha256:xyz",
            "Repository": "<none>",
            "Tag": "<none>",
            "Digest": "<none>",
            "Size": 99,
            "CreatedAt": "2025-01-02T00:00:00Z",
        }),
    ]
    runner.results[
        ("docker", "images", "--format", "{{json .}}")
    ] = RunResult(
        exit_code=0, stdout="\n".join(payload_lines), stderr="",
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    images = manager.list_images()

    testbase.assert_equal(len(images), 2)
    testbase.assert_equal(images[0].id, "sha256:abc")
    testbase.assert_equal(images[0].repository, "nginx")
    testbase.assert_equal(images[0].tag, "1.25")
    testbase.assert_equal(images[0].size_bytes, 12345678)
    testbase.assert_equal(images[1].repository, None)
    testbase.assert_equal(images[1].tag, None)
    return


# ----------------------------------------------------------------------
# create_container
# ----------------------------------------------------------------------


def test_create_container_invokes_create_with_spec():
    """
        create_container dispatches ``docker create --name <name> [-e
        ...] [-v ...] [-p ...] [--network ...] <image> [argv...]``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=0, stdout="containerabc\n", stderr="",
        ),
    )
    _seed_inspect_absent(
        runner=runner, engine=_DOCKER, target="web",
    )
    spec = UbuntuContainerSpec(
        name="web",
        image="nginx:1.25",
        argv=["nginx", "-g", "daemon off;"],
        env={"FOO": "bar"},
        volumes=["/host:/container"],
        ports=["8080:80"],
        networks=["app-net"],
        restart_policy="always",
        working_dir="/srv",
        user="nginx",
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    result = manager.create_container(spec=spec)

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.SUCCESS)
    create_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["docker", "create"]
    ]
    testbase.assert_equal(len(create_calls), 1)
    argv = create_calls[0]["args"]
    testbase.assert_true("--name" in argv)
    testbase.assert_true("web" in argv)
    testbase.assert_true("--restart=always" in argv)
    testbase.assert_true("--user" in argv)
    testbase.assert_true("nginx" in argv)
    testbase.assert_true("--workdir" in argv)
    testbase.assert_true("/srv" in argv)
    testbase.assert_true("FOO=bar" in argv)
    testbase.assert_true("/host:/container" in argv)
    testbase.assert_true("8080:80" in argv)
    testbase.assert_true("app-net" in argv)
    testbase.assert_true("nginx:1.25" in argv)
    testbase.assert_equal(result.container_id, "containerabc")
    return


def test_create_container_skipped_when_name_present_with_same_spec():
    """
        create_container returns SKIPPED when a container of the same
        name already exists with the same image.
    """
    runner = _MockCommandRunner()
    _seed_inspect_record(
        runner=runner, engine=_DOCKER, target="web",
        record={
            "Id": "abc",
            "Name": "/web",
            "Config": {"Image": "nginx:1.25"},
            "State": {"Running": False, "Status": "created"},
        },
    )
    spec = UbuntuContainerSpec(name="web", image="nginx:1.25")
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    result = manager.create_container(spec=spec)

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.SKIPPED)
    create_calls = [
        c for c in runner.calls
        if c["args"][:2] == ["docker", "create"]
    ]
    testbase.assert_equal(create_calls, [])
    return


# ----------------------------------------------------------------------
# start_container / stop_container / remove_container
# ----------------------------------------------------------------------


def test_start_container_invokes_start():
    """
        start_container dispatches ``docker start <id>`` when the
        container is not running.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_inspect_record(
        runner=runner, engine=_DOCKER, target="web",
        record={
            "Id": "abc",
            "Name": "/web",
            "Config": {"Image": "nginx:1.25"},
            "State": {"Running": False, "Status": "created"},
        },
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    result = manager.start_container(container_id="web")

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.SUCCESS)
    start_calls = [
        c for c in runner.calls if c["args"][:2] == ["docker", "start"]
    ]
    testbase.assert_equal(len(start_calls), 1)
    testbase.assert_equal(start_calls[0]["args"], ["docker", "start", "web"])
    return


def test_start_container_skipped_when_running():
    """
        start_container returns SKIPPED when State.Running is already
        True.
    """
    runner = _MockCommandRunner()
    _seed_inspect_record(
        runner=runner, engine=_DOCKER, target="web",
        record={
            "Id": "abc",
            "Name": "/web",
            "Config": {"Image": "nginx:1.25"},
            "State": {"Running": True, "Status": "running"},
        },
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    result = manager.start_container(container_id="web")

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.SKIPPED)
    start_calls = [
        c for c in runner.calls if c["args"][:2] == ["docker", "start"]
    ]
    testbase.assert_equal(start_calls, [])
    return


def test_stop_container_skipped_when_stopped():
    """
        stop_container returns SKIPPED when the container is not
        running.
    """
    runner = _MockCommandRunner()
    _seed_inspect_record(
        runner=runner, engine=_DOCKER, target="web",
        record={
            "Id": "abc",
            "Name": "/web",
            "Config": {"Image": "nginx:1.25"},
            "State": {"Running": False, "Status": "exited"},
        },
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    result = manager.stop_container(container_id="web")

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.SKIPPED)
    stop_calls = [
        c for c in runner.calls if c["args"][:2] == ["docker", "stop"]
    ]
    testbase.assert_equal(stop_calls, [])
    return


def test_stop_container_invokes_stop_with_timeout():
    """
        stop_container dispatches ``docker stop -t <timeout> <id>``
        when the container is running.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_inspect_record(
        runner=runner, engine=_DOCKER, target="web",
        record={
            "Id": "abc",
            "Name": "/web",
            "Config": {"Image": "nginx:1.25"},
            "State": {"Running": True, "Status": "running"},
        },
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(default_stop_timeout=5),
    )
    result = manager.stop_container(container_id="web")

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.SUCCESS)
    stop_calls = [
        c for c in runner.calls if c["args"][:2] == ["docker", "stop"]
    ]
    testbase.assert_equal(len(stop_calls), 1)
    testbase.assert_equal(
        stop_calls[0]["args"], ["docker", "stop", "-t", "5", "web"],
    )
    return


def test_remove_container_invokes_rm_force():
    """
        remove_container dispatches ``docker rm -f <id>`` when the
        container exists.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_inspect_record(
        runner=runner, engine=_DOCKER, target="web",
        record={
            "Id": "abc",
            "Name": "/web",
            "Config": {"Image": "nginx:1.25"},
            "State": {"Running": True, "Status": "running"},
        },
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    result = manager.remove_container(container_id="web")

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.SUCCESS)
    rm_calls = [c for c in runner.calls if c["args"][:2] == ["docker", "rm"]]
    testbase.assert_equal(len(rm_calls), 1)
    testbase.assert_equal(rm_calls[0]["args"], ["docker", "rm", "-f", "web"])
    return


# ----------------------------------------------------------------------
# list_containers / inspect_container
# ----------------------------------------------------------------------


def test_list_containers_with_all_includes_stopped():
    """
        list_containers(all=True) adds the ``-a`` flag.
    """
    runner = _MockCommandRunner()
    line = json.dumps({
        "ID": "abc",
        "Names": ["web"],
        "Image": "nginx:1.25",
        "Status": "Exited (0) 5 minutes ago",
        "State": "exited",
        "CreatedAt": "2025-01-01T00:00:00Z",
        "Ports": "0.0.0.0:8080->80/tcp",
        "Mounts": "",
        "Command": "nginx",
    })
    runner.results[
        ("docker", "ps", "-a", "--format", "{{json .}}")
    ] = RunResult(exit_code=0, stdout=line, stderr="")
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    containers = manager.list_containers(all=True)

    testbase.assert_equal(len(containers), 1)
    testbase.assert_equal(containers[0].id, "abc")
    testbase.assert_equal(containers[0].name, "web")
    testbase.assert_equal(containers[0].state, "exited")
    return


def test_inspect_container_parses_json_array():
    """
        inspect_container parses ``docker inspect`` (JSON array) and
        returns the first element as UbuntuContainerInfo.
    """
    runner = _MockCommandRunner()
    _seed_inspect_record(
        runner=runner, engine=_DOCKER, target="web",
        record={
            "Id": "abc",
            "Name": "/web",
            "Config": {
                "Image": "nginx:1.25",
                "Cmd": ["nginx", "-g", "daemon off;"],
            },
            "State": {
                "Running": True,
                "Status": "running",
                "StartedAt": "2025-02-01T12:00:00Z",
            },
            "Created": "2025-02-01T11:55:00Z",
            "NetworkSettings": {"Ports": {"80/tcp": []}},
            "Mounts": [{"Destination": "/data"}],
        },
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    info = manager.inspect_container(container_id="web")

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.id, "abc")
    testbase.assert_equal(info.name, "web")
    testbase.assert_equal(info.image, "nginx:1.25")
    testbase.assert_equal(info.state, "running")
    testbase.assert_equal(info.started, "2025-02-01T12:00:00Z")
    testbase.assert_true("80/tcp" in info.ports)
    testbase.assert_true("/data" in info.mounts)
    return


# ----------------------------------------------------------------------
# get_container_logs / exec_in_container
# ----------------------------------------------------------------------


def test_get_container_logs_invokes_logs_with_tail():
    """
        get_container_logs dispatches ``docker logs --tail <n> <id>``
        and parses lines.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("docker", "logs", "--tail", "50", "web")
    ] = RunResult(
        exit_code=0,
        stdout="line1\nline2\nline3\n",
        stderr="",
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    logs = manager.get_container_logs(container_id="web", lines=50)

    testbase.assert_equal(logs.container_id, "web")
    testbase.assert_equal(logs.lines, ["line1", "line2", "line3"])
    testbase.assert_equal(logs.truncated, True)
    return


def test_exec_in_container_invokes_exec():
    """
        exec_in_container dispatches ``docker exec <id> <argv...>``
        and surfaces output / exit_code.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=0, stdout="hello\n", stderr="",
        ),
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    result = manager.exec_in_container(
        container_id="web", argv=["echo", "hello"],
    )

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.SUCCESS)
    exec_calls = [
        c for c in runner.calls if c["args"][:2] == ["docker", "exec"]
    ]
    testbase.assert_equal(len(exec_calls), 1)
    testbase.assert_equal(
        exec_calls[0]["args"],
        ["docker", "exec", "web", "echo", "hello"],
    )
    testbase.assert_equal(result.command_argv, ["echo", "hello"])
    testbase.assert_equal(result.output, "hello\n")
    return


# ----------------------------------------------------------------------
# Engine selection
# ----------------------------------------------------------------------


def test_engine_selection_dispatches_to_podman_when_configured():
    """
        Configuring engine=PODMAN routes every verb through ``podman``
        instead of ``docker``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_image_present(
        runner=runner, engine=_PODMAN, ref="alpine:3", present=False,
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(
            engine=UbuntuContainerEngine.PODMAN,
        ),
    )
    result = manager.pull_image(ref="alpine:3")

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.SUCCESS)
    pull_calls = [
        c for c in runner.calls if c["args"][:2] == ["podman", "pull"]
    ]
    testbase.assert_equal(len(pull_calls), 1)
    testbase.assert_equal(result.engine, UbuntuContainerEngine.PODMAN)
    docker_calls = [c for c in runner.calls if c["args"][0] == "docker"]
    testbase.assert_equal(docker_calls, [])
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_pass_elevated_per_config():
    """
        Every write verb passes ``elevated=True`` to the runner when
        the config requests elevation.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_image_present(
        runner=runner, engine=_DOCKER, ref="nginx:1.25", present=False,
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(elevated=True),
    )
    manager.pull_image(ref="nginx:1.25")

    pull_calls = [
        c for c in runner.calls if c["args"][:2] == ["docker", "pull"]
    ]
    testbase.assert_equal(len(pull_calls), 1)
    testbase.assert_equal(pull_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs (and idempotency probes) never pass
        ``elevated=True`` regardless of config.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("docker", "images", "--format", "{{json .}}")
    ] = RunResult(exit_code=0, stdout="", stderr="")
    runner.results[
        ("docker", "ps", "--format", "{{json .}}")
    ] = RunResult(exit_code=0, stdout="", stderr="")
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(elevated=True),
    )
    manager.list_images()
    manager.list_containers()

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
    _seed_image_present(
        runner=runner, engine=_DOCKER, ref="nginx:1.25", present=False,
    )
    runner.default_result = RunResult(
        exit_code=125, stdout="", stderr="docker daemon not reachable",
    )
    manager = UbuntuContainerManager(
        runner=runner,
        config=UbuntuContainerManagerConfig(),
    )
    result = manager.pull_image(ref="nginx:1.25")

    testbase.assert_equal(result.status, UbuntuContainerActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 125)
    testbase.assert_equal(result.stderr, "docker daemon not reachable")
    return


# ----------------------------------------------------------------------
# Module-level parser tests
# ----------------------------------------------------------------------


def test_parse_image_lines_handles_multi_line_payload():
    """
        ``_parse_image_lines`` parses one JSON object per line.
    """
    payload = "\n".join([
        json.dumps({"ID": "1", "Repository": "a", "Tag": "1"}),
        json.dumps({"ID": "2", "Repository": "b", "Tag": "2"}),
    ])
    images = _parse_image_lines(output=payload)

    testbase.assert_equal(len(images), 2)
    testbase.assert_equal(images[0].id, "1")
    testbase.assert_equal(images[1].id, "2")
    return


def test_parse_inspect_first_returns_element_zero():
    """
        ``_parse_inspect_first`` returns element 0 of the JSON array.
    """
    payload = json.dumps([
        {"Id": "first"},
        {"Id": "second"},
    ])
    rtnval = _parse_inspect_first(output=payload)

    testbase.assert_true(rtnval is not None)
    testbase.assert_equal(rtnval.get("Id"), "first")
    return


def test_parse_container_ps_lines_handles_string_ports():
    """
        ``_parse_container_ps_lines`` parses comma-separated ports
        strings into a list.
    """
    payload = json.dumps({
        "ID": "abc",
        "Names": ["web"],
        "Image": "nginx",
        "Status": "Up",
        "State": "running",
        "Ports": "0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp",
    })
    rows = _parse_container_ps_lines(
        output=payload, engine=UbuntuContainerEngine.DOCKER,
    )

    testbase.assert_equal(len(rows), 1)
    testbase.assert_equal(len(rows[0].ports), 2)
    return


def test_build_create_argv_orders_flags_consistently():
    """
        ``_build_create_argv`` puts ``--name`` first, then optional
        flags, then the image, then the override argv.
    """
    spec = UbuntuContainerSpec(
        name="db",
        image="postgres:16",
        argv=["postgres"],
        env={"POSTGRES_PASSWORD": "x"},
    )
    argv = _build_create_argv(engine_binary="docker", spec=spec)

    testbase.assert_equal(argv[0], "docker")
    testbase.assert_equal(argv[1], "create")
    testbase.assert_equal(argv[2], "--name")
    testbase.assert_equal(argv[3], "db")
    testbase.assert_equal(argv[-2], "postgres:16")
    testbase.assert_equal(argv[-1], "postgres")
    return
