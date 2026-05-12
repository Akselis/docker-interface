from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Literal
from urllib.request import urlretrieve

import docker
from docker.errors import NotFound
from docker.models.containers import Container
from docker.models.networks import Network
from docker.types import IPAMConfig, IPAMPool
from mappings import build_container_kwargs
from models import (
    ComposeActionArgs,
    ComposeDeployArgs,
    ComposeLogsArgs,
    ComposeSourceType,
    ContainerArgs,
    NetworkConnectArgs,
    NetworkCreateArgs,
    NetworkDisconnectArgs,
    VolumeCreateArgs,
)

client = docker.from_env()
COMPOSE_PROJECTS_DIR = Path(os.getenv("COMPOSE_PROJECTS_DIR", "/tmp/lab_host/compose"))


def _project_dir(project_name: str) -> Path:
    safe_name = "".join(ch for ch in project_name if ch.isalnum() or ch in "-_")
    if safe_name != project_name or not safe_name:
        raise ValueError(
            "Invalid project_name. Use only letters, numbers, '-' and '_'."
        )
    return COMPOSE_PROJECTS_DIR / safe_name


def _compose_env(extra_env: dict[str, str] | None, project_name: str) -> dict[str, str]:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = project_name
    if extra_env:
        env.update(extra_env)
    return env


def _run_compose(
    project_name: str,
    args: list[str],
    env: dict[str, str] | None = None,
    timeout_seconds: int = 120,
) -> subprocess.CompletedProcess[str]:
    project_path = _project_dir(project_name)
    if not project_path.exists():
        raise FileNotFoundError(f"Compose project not found: {project_name}")

    cmd = ["docker", "compose", *args]
    return subprocess.run(
        cmd,
        cwd=project_path,
        env=_compose_env(env, project_name),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )


def _ensure_compose_source(args: ComposeDeployArgs) -> Path:
    project_path = _project_dir(args.project_name)
    project_path.parent.mkdir(parents=True, exist_ok=True)

    if project_path.exists():
        shutil.rmtree(project_path)
    project_path.mkdir(parents=True, exist_ok=True)

    if args.source_type == ComposeSourceType.INLINE:
        if not args.compose_content:
            raise ValueError("compose_content is required for inline source type")
        compose_path = project_path / args.compose_file
        compose_path.parent.mkdir(parents=True, exist_ok=True)
        compose_path.write_text(args.compose_content, encoding="utf-8")
        return project_path

    if args.source_type == ComposeSourceType.GIT:
        if not args.source_url:
            raise ValueError("source_url is required for git source type")
        clone_cmd = ["git", "clone", args.source_url, str(project_path)]
        clone_res = subprocess.run(
            clone_cmd, capture_output=True, text=True, check=False
        )
        if clone_res.returncode != 0:
            raise RuntimeError(f"Failed to clone git source: {clone_res.stderr}")
        if args.ref:
            checkout_res = subprocess.run(
                ["git", "checkout", args.ref],
                cwd=project_path,
                capture_output=True,
                text=True,
                check=False,
            )
            if checkout_res.returncode != 0:
                raise RuntimeError(
                    f"Failed to checkout ref '{args.ref}': {checkout_res.stderr}"
                )
        return project_path

    if args.source_type == ComposeSourceType.ARCHIVE:
        if not args.source_url:
            raise ValueError("source_url is required for archive source type")
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "package.tar.gz"
            urlretrieve(args.source_url, archive_path)
            with tarfile.open(archive_path, "r:*") as tar:
                tar.extractall(project_path)
        return project_path

    raise ValueError(f"Unsupported source type: {args.source_type}")


def deploy_compose_package(args: ComposeDeployArgs) -> dict[str, object]:
    project_path = _ensure_compose_source(args)
    compose_path = project_path / args.compose_file

    if not compose_path.exists():
        raise FileNotFoundError(f"Compose file not found: {args.compose_file}")

    if args.pull:
        pull_res = _run_compose(args.project_name, ["pull"], env=args.env)
        if pull_res.returncode != 0:
            raise RuntimeError(f"docker compose pull failed: {pull_res.stderr}")

    up_cmd = ["up", "-d"]
    if args.build:
        up_cmd.append("--build")
    up_res = _run_compose(args.project_name, up_cmd, env=args.env)
    if up_res.returncode != 0:
        raise RuntimeError(f"docker compose up failed: {up_res.stderr}")

    ps_res = _run_compose(args.project_name, ["ps", "--format", "json"], env=args.env)

    return {
        "project_name": args.project_name,
        "project_path": str(project_path),
        "compose_file": args.compose_file,
        "stdout": up_res.stdout,
        "stderr": up_res.stderr,
        "services": ps_res.stdout,
    }


def compose_up(
    project_name: str, args: ComposeActionArgs | None = None
) -> dict[str, object]:
    action = args or ComposeActionArgs()
    cmd = ["up", "-d"]
    res = _run_compose(
        project_name,
        cmd,
        env=action.env,
        timeout_seconds=action.timeout_seconds,
    )
    if res.returncode != 0:
        raise RuntimeError(f"docker compose up failed: {res.stderr}")
    return {"stdout": res.stdout, "stderr": res.stderr}


def compose_down(
    project_name: str, args: ComposeActionArgs | None = None
) -> dict[str, object]:
    action = args or ComposeActionArgs()
    cmd = ["down"]
    if action.remove_volumes:
        cmd.append("--volumes")
    if action.remove_images:
        cmd.extend(["--rmi", "all"])

    res = _run_compose(
        project_name,
        cmd,
        env=action.env,
        timeout_seconds=action.timeout_seconds,
    )
    if res.returncode != 0:
        raise RuntimeError(f"docker compose down failed: {res.stderr}")
    return {"stdout": res.stdout, "stderr": res.stderr}


def compose_pull(
    project_name: str, args: ComposeActionArgs | None = None
) -> dict[str, object]:
    action = args or ComposeActionArgs()
    res = _run_compose(
        project_name,
        ["pull"],
        env=action.env,
        timeout_seconds=action.timeout_seconds,
    )
    if res.returncode != 0:
        raise RuntimeError(f"docker compose pull failed: {res.stderr}")
    return {"stdout": res.stdout, "stderr": res.stderr}


def compose_ps(
    project_name: str, args: ComposeActionArgs | None = None
) -> dict[str, object]:
    action = args or ComposeActionArgs()
    res = _run_compose(
        project_name,
        ["ps", "--format", "json"],
        env=action.env,
        timeout_seconds=action.timeout_seconds,
    )
    if res.returncode != 0:
        raise RuntimeError(f"docker compose ps failed: {res.stderr}")
    return {"services": res.stdout}


def compose_logs(
    project_name: str, args: ComposeLogsArgs | None = None
) -> dict[str, object]:
    log_args = args or ComposeLogsArgs()
    cmd = ["logs", "--tail", str(log_args.tail)]
    if not log_args.follow:
        cmd.append("--no-follow")

    res = _run_compose(project_name, cmd, timeout_seconds=120)
    if res.returncode != 0:
        raise RuntimeError(f"docker compose logs failed: {res.stderr}")
    return {"logs": res.stdout}


def destroy_compose_project(
    project_name: str, remove_volumes: bool = False
) -> dict[str, object]:
    down_res = compose_down(
        project_name,
        ComposeActionArgs(remove_volumes=remove_volumes),
    )

    project_path = _project_dir(project_name)
    if project_path.exists():
        shutil.rmtree(project_path)

    return {
        "project_name": project_name,
        "status": "removed",
        "down": down_res,
    }


def create_container(args: ContainerArgs) -> Container:
    kwargs = build_container_kwargs(args)
    container = client.containers.run(**kwargs, detach=True)

    if args.network:
        container.reload()
        attrs = container.attrs if isinstance(container.attrs, dict) else {}
        network_settings = attrs.get("NetworkSettings")
        networks_obj = (
            network_settings.get("Networks")
            if isinstance(network_settings, dict)
            else {}
        )
        current_networks = (
            set(str(k) for k in networks_obj.keys())
            if isinstance(networks_obj, dict)
            else set()
        )

        for net in args.network:
            if net.name in current_networks:
                continue
            network = client.networks.get(net.name)
            network.connect(container=container, aliases=net.aliases)

    container.reload()
    return container


def get_container(
    container_id: str,
    filters: dict[str, str | list[str] | bool] | None = None,
) -> Container:
    container = client.containers.get(container_id)
    if not filters:
        return container

    filtered = client.containers.list(all=True, filters=filters)
    if any(c.id == container.id for c in filtered):
        return container

    raise NotFound(f"Container {container_id} does not match filters")


def get_containers(
    all_containers: bool = True,
    limit: int = -1,
    filters: dict[str, str | list[str] | bool] | None = None,
) -> list[Container]:
    return client.containers.list(
        all=all_containers,
        limit=limit,
        filters=filters,
    )


def set_container_state(
    container_id: str,
    state: Literal["stop", "pause", "unpause", "start"],
) -> Container:
    container = get_container(container_id)

    if state == "stop":
        container.stop()
    elif state == "pause":
        container.pause()
    elif state == "unpause":
        container.unpause()
    else:
        container.start()

    container.reload()
    return container


def destroy_container(container_id: str, force: bool = True) -> None:
    container = get_container(container_id)
    container.remove(force=force)


def exec_in_container(
    container_id: str,
    command: str | list[str],
    workdir: str | None = None,
    user: str | None = None,
    environment: dict[str, str] | None = None,
    privileged: bool = False,
) -> tuple[int, str]:
    container = get_container(container_id)

    if user is None:
        result = container.exec_run(
            cmd=command,
            stdout=True,
            stderr=True,
            demux=False,
            workdir=workdir,
            environment=environment,
            privileged=privileged,
        )
    else:
        result = container.exec_run(
            cmd=command,
            stdout=True,
            stderr=True,
            demux=False,
            workdir=workdir,
            user=user,
            environment=environment,
            privileged=privileged,
        )

    raw_output = result.output
    if isinstance(raw_output, bytes):
        output = raw_output.decode("utf-8", errors="replace")
    elif isinstance(raw_output, str):
        output = raw_output
    else:
        output = ""

    exit_code = result.exit_code if isinstance(result.exit_code, int) else -1
    return exit_code, output


def prune_containers() -> dict[str, object]:
    return client.containers.prune()


def create_network(args: NetworkCreateArgs) -> Network:
    ipam_config: IPAMConfig | None = None
    if args.ipam is not None and args.ipam.subnet is not None:
        pool = IPAMPool(
            subnet=args.ipam.subnet,
            gateway=args.ipam.gateway,
            iprange=args.ipam.ip_range,
        )
        ipam_config = IPAMConfig(pool_configs=[pool])

    return client.networks.create(
        name=args.name,
        driver=args.driver.value if args.driver is not None else None,
        internal=args.internal if args.internal is not None else False,
        attachable=args.attachable,
        labels=args.labels,
        ipam=ipam_config,
    )


def get_network(
    network_id: str,
    filters: dict[str, str | list[str] | bool] | None = None,
) -> Network:
    network = client.networks.get(network_id)
    if not filters:
        return network

    filtered = client.networks.list(filters=filters)
    if any(n.id == network.id for n in filtered):
        return network

    raise NotFound(f"Network {network_id} does not match filters")


def get_networks(
    filters: dict[str, str | list[str] | bool] | None = None,
) -> list[Network]:
    return client.networks.list(filters=filters)


def destroy_network(network_id: str) -> None:
    network = get_network(network_id)
    network.remove()


def connect_container_to_network(network_id: str, args: NetworkConnectArgs) -> Network:
    network = get_network(network_id)
    network.connect(
        container=args.container_id,
        aliases=args.aliases,
        ipv4_address=args.ipv4_address,
    )
    network.reload()
    return network


def disconnect_container_from_network(
    network_id: str, args: NetworkDisconnectArgs
) -> Network:
    network = get_network(network_id)
    network.disconnect(container=args.container_id, force=args.force)
    network.reload()
    return network


def prune_networks() -> dict[str, object]:
    return client.networks.prune()


def create_volume(args: VolumeCreateArgs) -> dict[str, object]:
    volume = client.volumes.create(
        name=args.name,
        driver=args.driver,
        labels=args.labels,
    )
    attrs = volume.attrs if isinstance(volume.attrs, dict) else {}
    return {
        "name": str(attrs.get("Name") or args.name),
        "driver": str(attrs.get("Driver") or args.driver or "local"),
        "mountpoint": str(attrs.get("Mountpoint") or ""),
        "labels": attrs.get("Labels") if isinstance(attrs.get("Labels"), dict) else {},
    }


def get_volumes() -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for volume in client.volumes.list():
        attrs = volume.attrs if isinstance(volume.attrs, dict) else {}
        result.append(
            {
                "name": str(attrs.get("Name") or ""),
                "driver": str(attrs.get("Driver") or "local"),
                "mountpoint": str(attrs.get("Mountpoint") or ""),
                "labels": attrs.get("Labels")
                if isinstance(attrs.get("Labels"), dict)
                else {},
            }
        )
    return result


def remove_volume(volume_name: str, force: bool = False) -> None:
    volume = client.volumes.get(volume_name)
    volume.remove(force=force)


def prune_volumes() -> dict[str, object]:
    return client.volumes.prune()
