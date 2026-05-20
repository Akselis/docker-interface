from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.request import urlretrieve

import docker
import yaml
from docker.errors import APIError, NotFound
from docker.models.containers import Container
from docker.models.networks import Network
from docker.types import IPAMConfig, IPAMPool
from mappings import build_container_kwargs, summarize_container, summarize_network
from models import (
    ComposeActionArgs,
    ComposeDeployArgs,
    ComposeLogsArgs,
    ComposeSourceType,
    ContainerArgs,
    IngressEnsureArgs,
    IngressRouteUpsertArgs,
    NetworkConnectArgs,
    NetworkCreateArgs,
    NetworkDisconnectArgs,
    PortMapping,
    VolumeCreateArgs,
)

client = docker.from_env()
COMPOSE_PROJECTS_DIR = Path(os.getenv("COMPOSE_PROJECTS_DIR", "/tmp/lab_host/compose"))
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
PROJECT_META_FILENAME = ".lab_host_project.json"
INGRESS_BASE_DIR = COMPOSE_PROJECTS_DIR / "_ingress"
INGRESS_STATIC_CONFIG_FILE = INGRESS_BASE_DIR / "traefik.yml"
INGRESS_DYNAMIC_DIR = INGRESS_BASE_DIR / "dynamic"
INGRESS_DYNAMIC_CONFIG_FILE = INGRESS_DYNAMIC_DIR / "routes.yml"
INGRESS_ROUTES_STATE_FILE = INGRESS_BASE_DIR / "routes.json"

_MEMORY_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")
_MEMORY_MULTIPLIERS: dict[str, int] = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "ki": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mi": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gi": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "ti": 1024**4,
    "tib": 1024**4,
}


def _used_host_ports() -> set[int]:
    used: set[int] = set()
    for container in client.containers.list(all=True):
        attrs = container.attrs if isinstance(container.attrs, dict) else {}
        host_config = (
            attrs.get("HostConfig") if isinstance(attrs.get("HostConfig"), dict) else {}
        )
        bindings = (
            host_config.get("PortBindings")
            if isinstance(host_config.get("PortBindings"), dict)
            else {}
        )

        for binding_list in bindings.values():
            if not isinstance(binding_list, list):
                continue
            for binding in binding_list:
                if not isinstance(binding, dict):
                    continue
                host_port_obj = binding.get("HostPort")
                if isinstance(host_port_obj, str) and host_port_obj.isdigit():
                    used.add(int(host_port_obj))
    return used


def _next_available_host_port(start_port: int, used_ports: set[int]) -> int:
    port = max(1, start_port)
    while port in used_ports and port <= 65535:
        port += 1
    if port > 65535:
        raise ValueError("No available host port in range 1..65535")
    return port


def _remap_requested_ports_if_needed(
    ports: list[PortMapping] | None,
) -> tuple[list[PortMapping] | None, dict[int, int]]:
    if not ports:
        return ports, {}

    used_ports = _used_host_ports()
    remap: dict[int, int] = {}
    remapped_ports: list[PortMapping] = []

    for mapping in ports:
        desired_host_port = int(mapping.host)
        assigned_host_port = (
            _next_available_host_port(desired_host_port, used_ports)
            if desired_host_port in used_ports
            else desired_host_port
        )

        if assigned_host_port != desired_host_port:
            remap[desired_host_port] = assigned_host_port

        used_ports.add(assigned_host_port)
        remapped_ports.append(mapping.model_copy(update={"host": assigned_host_port}))

    return remapped_ports, remap


def _parse_short_port_mapping(value: str) -> tuple[str | None, int, int, str] | None:
    text = value.strip()
    if not text:
        return None

    protocol = "tcp"
    if "/" in text:
        left, proto = text.rsplit("/", 1)
        if proto:
            protocol = proto
            text = left

    parts = text.split(":")
    if len(parts) == 2:
        host_part, container_part = parts
        if host_part.isdigit() and container_part.isdigit():
            return None, int(host_part), int(container_part), protocol
        return None
    if len(parts) == 3:
        host_ip, host_part, container_part = parts
        if host_part.isdigit() and container_part.isdigit():
            return host_ip, int(host_part), int(container_part), protocol
        return None

    return None


def _render_short_port_mapping(
    host_ip: str | None,
    host_port: int,
    container_port: int,
    protocol: str,
) -> str:
    base = f"{host_port}:{container_port}"
    if host_ip:
        base = f"{host_ip}:{base}"
    if protocol and protocol != "tcp":
        return f"{base}/{protocol}"
    return base


def _remap_compose_ports_in_payload(
    compose_payload: dict[str, Any],
) -> tuple[dict[int, int], dict[str, Any]]:
    services_obj = compose_payload.get("services")
    if not isinstance(services_obj, dict):
        return {}, compose_payload

    used_ports = _used_host_ports()
    remap: dict[int, int] = {}

    for service in services_obj.values():
        if not isinstance(service, dict):
            continue

        ports_obj = service.get("ports")
        if not isinstance(ports_obj, list):
            continue

        updated_ports: list[object] = []
        for entry in ports_obj:
            if isinstance(entry, str):
                parsed = _parse_short_port_mapping(entry)
                if parsed is None:
                    updated_ports.append(entry)
                    continue

                host_ip, host_port, container_port, protocol = parsed
                assigned_host_port = (
                    _next_available_host_port(host_port, used_ports)
                    if host_port in used_ports
                    else host_port
                )
                if assigned_host_port != host_port:
                    remap[host_port] = assigned_host_port
                used_ports.add(assigned_host_port)
                updated_ports.append(
                    _render_short_port_mapping(
                        host_ip,
                        assigned_host_port,
                        container_port,
                        protocol,
                    )
                )
                continue

            if isinstance(entry, dict):
                published_obj = entry.get("published")
                if isinstance(published_obj, int):
                    host_port = published_obj
                elif isinstance(published_obj, str) and published_obj.isdigit():
                    host_port = int(published_obj)
                else:
                    updated_ports.append(entry)
                    continue

                assigned_host_port = (
                    _next_available_host_port(host_port, used_ports)
                    if host_port in used_ports
                    else host_port
                )
                if assigned_host_port != host_port:
                    remap[host_port] = assigned_host_port

                used_ports.add(assigned_host_port)
                patched = dict(entry)
                patched["published"] = assigned_host_port
                updated_ports.append(patched)
                continue

            updated_ports.append(entry)

        service["ports"] = updated_ports

    return remap, compose_payload


def _remap_compose_ports_if_needed(compose_path: Path) -> dict[int, int]:
    try:
        payload = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except OSError:
        return {}

    if not isinstance(payload, dict):
        return {}

    remap, patched_payload = _remap_compose_ports_in_payload(payload)
    if remap:
        compose_path.write_text(
            yaml.safe_dump(patched_payload, sort_keys=False),
            encoding="utf-8",
        )
    return remap


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


def _project_metadata_path(project_path: Path) -> Path:
    return project_path / PROJECT_META_FILENAME


def _write_project_metadata(args: ComposeDeployArgs, project_path: Path) -> None:
    metadata = {
        "project_name": args.project_name,
        "compose_file": args.compose_file,
        "source_type": args.source_type.value,
        "source_url": args.source_url,
        "ref": args.ref,
        "cpu_limit": args.cpu_limit,
        "memory_limit": args.memory_limit,
    }
    _project_metadata_path(project_path).write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )


def _read_project_metadata(project_path: Path) -> dict[str, object]:
    metadata_path = _project_metadata_path(project_path)
    if not metadata_path.exists():
        return {}

    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    return payload if isinstance(payload, dict) else {}


def _compose_project_filters(
    project_name: str,
) -> dict[str, str | list[str] | bool]:
    return {"label": f"{COMPOSE_PROJECT_LABEL}={project_name}"}


def _parse_memory_limit_to_bytes(memory_limit: str) -> int:
    match = _MEMORY_PATTERN.match(memory_limit)
    if not match:
        raise ValueError(
            "Invalid memory_limit format. Use values like '512m', '1g', '1024mb'."
        )

    number_raw, unit_raw = match.groups()
    amount = float(number_raw)
    multiplier = _MEMORY_MULTIPLIERS.get(unit_raw.lower())
    if multiplier is None:
        raise ValueError(
            f"Unsupported memory unit '{unit_raw}'. Use b/k/m/g/t or ki/mi/gi/ti variants."
        )

    bytes_value = int(amount * multiplier)
    if bytes_value <= 0:
        raise ValueError("memory_limit must resolve to a positive number of bytes")

    return bytes_value


def _apply_compose_project_limits(
    project_name: str,
    cpu_limit: float | None,
    memory_limit: str | None,
) -> None:
    if cpu_limit is None and memory_limit is None:
        return

    containers = client.containers.list(
        all=True,
        filters=_compose_project_filters(project_name),
    )
    if not containers:
        return

    container_count = len(containers)

    cpu_quotas: list[int] | None = None
    if cpu_limit is not None:
        total_quota = int(cpu_limit * 100000)
        if total_quota < container_count:
            raise ValueError(
                "cpu_limit is too low for the number of project containers"
            )
        base_quota = total_quota // container_count
        quota_remainder = total_quota % container_count
        cpu_quotas = [
            base_quota + (1 if idx < quota_remainder else 0)
            for idx in range(container_count)
        ]

    memory_limits: list[int] | None = None
    if memory_limit is not None:
        total_memory_bytes = _parse_memory_limit_to_bytes(memory_limit)
        if total_memory_bytes < container_count:
            raise ValueError(
                "memory_limit is too low for the number of project containers"
            )
        base_memory = total_memory_bytes // container_count
        memory_remainder = total_memory_bytes % container_count
        memory_limits = [
            base_memory + (1 if idx < memory_remainder else 0)
            for idx in range(container_count)
        ]

    for idx, container in enumerate(containers):
        cpu_quota = cpu_quotas[idx] if cpu_quotas is not None else None
        mem_limit = memory_limits[idx] if memory_limits is not None else None

        if cpu_quota is not None and mem_limit is not None:
            container.update(cpu_quota=cpu_quota, mem_limit=mem_limit)
        elif cpu_quota is not None:
            container.update(cpu_quota=cpu_quota)
        elif mem_limit is not None:
            container.update(mem_limit=mem_limit)


def _limits_from_metadata(project_name: str) -> tuple[float | None, str | None]:
    metadata = _read_project_metadata(_project_dir(project_name))

    cpu_limit_obj = metadata.get("cpu_limit")
    cpu_limit: float | None
    if isinstance(cpu_limit_obj, (int, float)):
        cpu_limit = float(cpu_limit_obj)
    else:
        cpu_limit = None

    memory_limit_obj = metadata.get("memory_limit")
    memory_limit = memory_limit_obj if isinstance(memory_limit_obj, str) else None

    return cpu_limit, memory_limit


def _project_snapshot(
    project_name: str,
    compose_file: str | None = None,
    extra_info: dict[str, object] | None = None,
) -> dict[str, object]:
    project_path = _project_dir(project_name)
    if not project_path.exists():
        raise FileNotFoundError(f"Compose project not found: {project_name}")

    metadata = _read_project_metadata(project_path)
    containers = client.containers.list(
        all=True,
        filters=_compose_project_filters(project_name),
    )
    networks = client.networks.list(filters=_compose_project_filters(project_name))

    info: dict[str, object] = {
        "name": project_name,
        "path": str(project_path),
        "compose_file": compose_file or metadata.get("compose_file"),
        "source_type": metadata.get("source_type"),
        "source_url": metadata.get("source_url"),
        "ref": metadata.get("ref"),
        "cpu_limit": metadata.get("cpu_limit"),
        "memory_limit": metadata.get("memory_limit"),
        "container_count": len(containers),
        "network_count": len(networks),
    }
    if extra_info:
        info.update(extra_info)

    return {
        "info": info,
        "containers": [summarize_container(c) for c in containers],
        "networks": [summarize_network(n) for n in networks],
    }


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

    _write_project_metadata(args, project_path)

    if args.pull:
        pull_res = _run_compose(args.project_name, ["pull"], env=args.env)
        if pull_res.returncode != 0:
            raise RuntimeError(f"docker compose pull failed: {pull_res.stderr}")

    up_cmd = ["up", "-d"]
    if args.build:
        up_cmd.append("--build")

    compose_port_remap: dict[int, int] = {}
    up_res = _run_compose(args.project_name, up_cmd, env=args.env)
    if up_res.returncode != 0:
        lower_err = (up_res.stderr or "").lower()
        if "port is already allocated" in lower_err:
            compose_port_remap = _remap_compose_ports_if_needed(compose_path)
            if compose_port_remap:
                up_res = _run_compose(args.project_name, up_cmd, env=args.env)

        if up_res.returncode != 0:
            raise RuntimeError(f"docker compose up failed: {up_res.stderr}")

    _apply_compose_project_limits(
        args.project_name,
        args.cpu_limit,
        args.memory_limit,
    )

    snapshot = _project_snapshot(
        args.project_name,
        compose_file=args.compose_file,
        extra_info={"status": "deployed"},
    )
    if compose_port_remap:
        snapshot["port_remap"] = {
            str(src): dst for src, dst in compose_port_remap.items()
        }

    return snapshot


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

    cpu_limit, memory_limit = _limits_from_metadata(project_name)
    _apply_compose_project_limits(project_name, cpu_limit, memory_limit)

    return _project_snapshot(project_name, extra_info={"status": "running"})


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
    return _project_snapshot(project_name, extra_info={"status": "stopped"})


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
    return _project_snapshot(project_name)


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
    return _project_snapshot(project_name)


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

    snapshot = _project_snapshot(project_name)
    snapshot["logs"] = res.stdout
    return snapshot


def destroy_compose_project(
    project_name: str, remove_volumes: bool = False
) -> dict[str, object]:
    compose_down(
        project_name,
        ComposeActionArgs(remove_volumes=remove_volumes),
    )

    project_path = _project_dir(project_name)
    if project_path.exists():
        shutil.rmtree(project_path)

    return {
        "info": {
            "name": project_name,
            "path": str(project_path),
            "status": "removed",
            "container_count": 0,
            "network_count": 0,
        },
        "containers": [],
        "networks": [],
    }


def create_container(args: ContainerArgs) -> Container:
    remapped_ports, _ = _remap_requested_ports_if_needed(args.ports)
    effective_args = args.model_copy(update={"ports": remapped_ports})

    kwargs = build_container_kwargs(effective_args)

    last_error: APIError | None = None
    for _ in range(5):
        try:
            container = client.containers.run(**kwargs, detach=True)
            break
        except APIError as exc:
            message = str(exc).lower()
            if "port is already allocated" not in message:
                raise

            remapped_ports, _ = _remap_requested_ports_if_needed(effective_args.ports)
            effective_args = effective_args.model_copy(update={"ports": remapped_ports})
            kwargs = build_container_kwargs(effective_args)
            last_error = exc
    else:
        if last_error is not None:
            raise last_error
        raise RuntimeError("Failed to create container after retrying port remaps")

    if effective_args.network and effective_args.network[0].name != "none":
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

        for index, net in enumerate(effective_args.network):
            if index == 0:
                continue
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


def _ensure_ingress_layout() -> None:
    INGRESS_DYNAMIC_DIR.mkdir(parents=True, exist_ok=True)

    if not INGRESS_ROUTES_STATE_FILE.exists():
        INGRESS_ROUTES_STATE_FILE.write_text("{}\n", encoding="utf-8")

    if not INGRESS_DYNAMIC_CONFIG_FILE.exists():
        INGRESS_DYNAMIC_CONFIG_FILE.write_text("", encoding="utf-8")


def _load_ingress_routes_state() -> dict[str, dict[str, Any]]:
    _ensure_ingress_layout()

    try:
        payload = json.loads(INGRESS_ROUTES_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(payload, dict):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for hostname_obj, route_obj in payload.items():
        if not isinstance(hostname_obj, str) or not isinstance(route_obj, dict):
            continue

        upstream_host = route_obj.get("upstream_host")
        upstream_port = route_obj.get("upstream_port")
        service_scheme = route_obj.get("service_scheme")
        metadata_obj = route_obj.get("metadata")

        if not isinstance(upstream_host, str) or not isinstance(upstream_port, int):
            continue

        normalized[hostname_obj] = {
            "upstream_host": upstream_host,
            "upstream_port": upstream_port,
            "service_scheme": (
                service_scheme
                if isinstance(service_scheme, str)
                and service_scheme in {"http", "https"}
                else "http"
            ),
            "metadata": metadata_obj if isinstance(metadata_obj, dict) else {},
        }

    return normalized


def _write_ingress_routes_state(routes: dict[str, dict[str, Any]]) -> None:
    _ensure_ingress_layout()
    INGRESS_ROUTES_STATE_FILE.write_text(
        json.dumps(routes, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _ingress_route_id(hostname: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9-]", "-", hostname).strip("-").lower() or "route"
    digest = hashlib.sha1(hostname.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def _render_ingress_dynamic_config(routes: dict[str, dict[str, Any]]) -> str:
    lines: list[str] = ["http:", "  routers:"]

    if not routes:
        return ""

    for hostname in sorted(routes.keys()):
        route = routes[hostname]
        route_id = _ingress_route_id(hostname)

        lines.extend(
            [
                f"    {route_id}:",
                f'      rule: "Host(`{hostname}`)"',
                f"      service: {route_id}",
                "      entryPoints:",
                "        - web",
                "        - websecure",
            ]
        )

    lines.append("  services:")
    for hostname in sorted(routes.keys()):
        route = routes[hostname]
        route_id = _ingress_route_id(hostname)
        scheme = str(route.get("service_scheme") or "http")
        upstream_host = str(route["upstream_host"])
        upstream_port = int(route["upstream_port"])
        upstream_url = f"{scheme}://{upstream_host}:{upstream_port}"

        lines.extend(
            [
                f"    {route_id}:",
                "      loadBalancer:",
                "        servers:",
                f'          - url: "{upstream_url}"',
            ]
        )

    return "\n".join(lines) + "\n"


def _write_ingress_dynamic_config(routes: dict[str, dict[str, Any]]) -> None:
    _ensure_ingress_layout()
    INGRESS_DYNAMIC_CONFIG_FILE.write_text(
        _render_ingress_dynamic_config(routes),
        encoding="utf-8",
    )


def _render_traefik_static_config(http_port: int, https_port: int) -> str:
    return (
        "\n".join(
            [
                "entryPoints:",
                "  web:",
                f'    address: ":{http_port}"',
                "  websecure:",
                f'    address: ":{https_port}"',
                "providers:",
                "  file:",
                "    directory: /etc/traefik/dynamic",
                "    watch: true",
            ]
        )
        + "\n"
    )


def ensure_ingress_proxy(args: IngressEnsureArgs) -> dict[str, object]:
    _ensure_ingress_layout()

    try:
        existing = client.containers.get(args.container_name)
        existing.remove(force=True)
    except NotFound:
        pass

    effective_http_port = args.http_port
    effective_https_port = args.https_port
    port_remap: dict[int, int] = {}

    if args.network_mode == "host":
        used_ports = _used_host_ports()

        if effective_http_port in used_ports:
            new_http_port = _next_available_host_port(effective_http_port, used_ports)
            port_remap[effective_http_port] = new_http_port
            effective_http_port = new_http_port
        used_ports.add(effective_http_port)

        if effective_https_port in used_ports:
            new_https_port = _next_available_host_port(effective_https_port, used_ports)
            port_remap[effective_https_port] = new_https_port
            effective_https_port = new_https_port

    INGRESS_STATIC_CONFIG_FILE.write_text(
        _render_traefik_static_config(effective_http_port, effective_https_port),
        encoding="utf-8",
    )

    current_routes = _load_ingress_routes_state()
    _write_ingress_dynamic_config(current_routes)

    restart_policy: dict[str, str] = {"Name": "unless-stopped"}
    command: list[str] = ["--configFile=/etc/traefik/traefik.yml"]
    volumes: dict[str, dict[str, str]] = {
        str(INGRESS_BASE_DIR): {"bind": "/etc/traefik", "mode": "rw"},
    }

    if args.network_mode == "host":
        container = client.containers.run(
            image=args.image,
            name=args.container_name,
            detach=True,
            restart_policy=restart_policy,  # type: ignore[arg-type]
            command=command,
            volumes=volumes,
            network_mode="host",
        )
    else:
        container = client.containers.run(
            image=args.image,
            name=args.container_name,
            detach=True,
            restart_policy=restart_policy,  # type: ignore[arg-type]
            command=command,
            volumes=volumes,
            ports={
                "80/tcp": effective_http_port,
                "443/tcp": effective_https_port,
            },
        )
    container.reload()

    return {
        "container": summarize_container(container),
        "ingress": {
            "container_name": args.container_name,
            "image": args.image,
            "http_port": effective_http_port,
            "https_port": effective_https_port,
            "network_mode": args.network_mode,
            "routes_file": str(INGRESS_DYNAMIC_CONFIG_FILE),
            "routes_count": len(current_routes),
            "port_remap": {str(src): dst for src, dst in port_remap.items()},
        },
    }


def upsert_ingress_route(
    hostname: str,
    args: IngressRouteUpsertArgs,
) -> dict[str, object]:
    routes = _load_ingress_routes_state()

    route_payload: dict[str, Any] = {
        "upstream_host": args.upstream_host,
        "upstream_port": args.upstream_port,
        "service_scheme": args.service_scheme,
        "metadata": args.metadata or {},
    }
    routes[hostname] = route_payload

    _write_ingress_routes_state(routes)
    _write_ingress_dynamic_config(routes)

    return {
        "status": "updated",
        "hostname": hostname,
        "route": route_payload,
        "routes_count": len(routes),
        "routes_file": str(INGRESS_DYNAMIC_CONFIG_FILE),
    }


def remove_ingress_route(hostname: str) -> dict[str, object]:
    routes = _load_ingress_routes_state()
    removed = hostname in routes

    if removed:
        del routes[hostname]

    _write_ingress_routes_state(routes)
    _write_ingress_dynamic_config(routes)

    return {
        "status": "removed" if removed else "not_found",
        "hostname": hostname,
        "removed": removed,
        "routes_count": len(routes),
        "routes_file": str(INGRESS_DYNAMIC_CONFIG_FILE),
    }
