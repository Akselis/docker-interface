import hmac
import os
from typing import Literal

from docker.errors import (
    APIError,
    ContainerError,
    DockerException,
    ImageNotFound,
    NotFound,
)
from docker.models.containers import Container
from docker.models.networks import Network
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

import docker_interface as di
from models import (
    ContainerArgs,
    NetworkConnectArgs,
    NetworkCreateArgs,
    NetworkDisconnectArgs,
)

API_KEY_ENV_VAR = "DOCKER_INTERFACE_API_KEY"
API_KEY_HEADER = "X-API-Key"


def verify_api_key(x_api_key: str = Header(..., alias=API_KEY_HEADER)) -> None:
    expected_api_key = os.getenv(API_KEY_ENV_VAR)
    if not expected_api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Server API key is not configured in {API_KEY_ENV_VAR}",
        )

    if not hmac.compare_digest(x_api_key, expected_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


app = FastAPI(dependencies=[Depends(verify_api_key)])


class ExecCommandRequest(BaseModel):
    command: str | list[str]
    workdir: str | None = None
    user: str | None = None
    environment: dict[str, str] | None = None
    privileged: bool = False


def _container_summary(container: Container, full: bool = False) -> dict[str, object]:
    attrs_obj = container.attrs
    attrs = attrs_obj if isinstance(attrs_obj, dict) else {}

    config_obj = attrs.get("Config")
    config = config_obj if isinstance(config_obj, dict) else {}

    host_config_obj = attrs.get("HostConfig")
    host_config = host_config_obj if isinstance(host_config_obj, dict) else {}

    network_settings_obj = attrs.get("NetworkSettings")
    network_settings = (
        network_settings_obj if isinstance(network_settings_obj, dict) else {}
    )

    networks_map_obj = network_settings.get("Networks")
    networks_map = networks_map_obj if isinstance(networks_map_obj, dict) else {}

    labels_obj = config.get("Labels")
    labels: dict[str, str] = {}
    if isinstance(labels_obj, dict):
        for key, value in labels_obj.items():
            if isinstance(key, str) and isinstance(value, str):
                labels[key] = value

    image_ref = (
        container.image.tags[0]
        if container.image.tags
        else str(container.image.short_id)
    )

    basic: dict[str, object] = {
        "id": str(container.id),
        "name": str(container.name),
        "image": str(image_ref),
        "status": str(container.status or "unknown"),
        "labels": labels,
        "networks": [str(name) for name in networks_map.keys()],
    }

    if not full:
        return basic

    env_entries_obj = config.get("Env")
    env_entries = env_entries_obj if isinstance(env_entries_obj, list) else []
    env_dict: dict[str, str] = {}
    for item in env_entries:
        if isinstance(item, str) and "=" in item:
            k, v = item.split("=", 1)
            env_dict[k] = v

    cmd_obj = config.get("Cmd")
    command: str | None
    if isinstance(cmd_obj, list):
        command = " ".join([part for part in cmd_obj if isinstance(part, str)])
    elif isinstance(cmd_obj, str):
        command = cmd_obj
    else:
        command = None

    port_bindings_obj = host_config.get("PortBindings")
    port_bindings = port_bindings_obj if isinstance(port_bindings_obj, dict) else {}
    ports: list[dict[str, object]] = []
    for container_port, bindings in port_bindings.items():
        if not isinstance(container_port, str) or "/" not in container_port:
            continue
        container_part, protocol = container_port.split("/", 1)
        if not container_part.isdigit() or not isinstance(bindings, list):
            continue
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            host_port = binding.get("HostPort")
            if not isinstance(host_port, str) or not host_port.isdigit():
                continue
            ports.append(
                {
                    "host": int(host_port),
                    "container": int(container_part),
                    "protocol": protocol,
                }
            )

    mounts_obj = attrs.get("Mounts")
    mounts = mounts_obj if isinstance(mounts_obj, list) else []
    storage: list[dict[str, object]] = []
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        source = mount.get("Source")
        target = mount.get("Destination")
        mount_type = mount.get("Type")
        rw = mount.get("RW")
        if (
            isinstance(source, str)
            and isinstance(target, str)
            and isinstance(mount_type, str)
        ):
            storage.append(
                {
                    "source": source,
                    "target": target,
                    "type": mount_type,
                    "read_only_mount": not bool(rw if isinstance(rw, bool) else True),
                }
            )

    security_opt_obj = host_config.get("SecurityOpt")
    security_opt = security_opt_obj if isinstance(security_opt_obj, list) else []
    seccomp_profile: str | None = None
    apparmor_profile: str | None = None
    for opt in security_opt:
        if isinstance(opt, str) and opt.startswith("seccomp="):
            seccomp_profile = opt.split("=", 1)[1]
        if isinstance(opt, str) and opt.startswith("apparmor="):
            apparmor_profile = opt.split("=", 1)[1]

    networks: list[dict[str, object]] = []
    for name, data in networks_map.items():
        aliases: list[str] | None = None
        if isinstance(data, dict):
            aliases_obj = data.get("Aliases")
            if isinstance(aliases_obj, list):
                aliases = [a for a in aliases_obj if isinstance(a, str)]
        networks.append(
            {
                "name": str(name),
                "external": None,
                "driver": None,
                "aliases": aliases,
            }
        )

    cpu_quota_obj = host_config.get("CpuQuota")
    memory_obj = host_config.get("Memory")
    pids_obj = host_config.get("PidsLimit")

    restart_policy_obj = host_config.get("RestartPolicy")
    restart_policy = restart_policy_obj if isinstance(restart_policy_obj, dict) else {}

    devices_obj = host_config.get("Devices")
    devices = devices_obj if isinstance(devices_obj, list) else []
    device_paths: list[str] = []
    for device in devices:
        if isinstance(device, dict):
            path = device.get("PathOnHost")
            if isinstance(path, str):
                device_paths.append(path)

    full_payload: dict[str, object] = {
        "image": str(config.get("Image") or image_ref),
        "name": str(container.name),
        "command": command,
        "env": env_dict or None,
        "ports": ports or None,
        "resources": {
            "cpu_count": (
                int(cpu_quota_obj / 100000)
                if isinstance(cpu_quota_obj, int) and cpu_quota_obj > 0
                else None
            ),
            "memory_limit": (
                str(memory_obj)
                if isinstance(memory_obj, int) and memory_obj > 0
                else None
            ),
            "process_limit": (
                pids_obj if isinstance(pids_obj, int) and pids_obj != -1 else None
            ),
        },
        "security": {
            "user": config.get("User") if isinstance(config.get("User"), str) else None,
            "read_only_root_fs": (
                host_config.get("ReadonlyRootfs")
                if isinstance(host_config.get("ReadonlyRootfs"), bool)
                else None
            ),
            "no_new_privileges": None,
            "capabilities_drop": (
                host_config.get("CapDrop")
                if isinstance(host_config.get("CapDrop"), list)
                else None
            ),
            "capabilities_add": (
                host_config.get("CapAdd")
                if isinstance(host_config.get("CapAdd"), list)
                else None
            ),
            "privileged": (
                host_config.get("Privileged")
                if isinstance(host_config.get("Privileged"), bool)
                else None
            ),
            "devices": device_paths or None,
            "seccomp_profile": seccomp_profile,
            "apparmor_profile": apparmor_profile,
        },
        "network": networks or None,
        "storage": storage or None,
        "labels": labels or None,
        "restart_policy": {
            "name": (
                restart_policy.get("Name")
                if isinstance(restart_policy.get("Name"), str)
                else None
            ),
            "retries": (
                restart_policy.get("MaximumRetryCount")
                if isinstance(restart_policy.get("MaximumRetryCount"), int)
                else None
            ),
            "delay": None,
        },
    }

    return full_payload


def _network_summary(network: Network, full: bool = False) -> dict[str, object]:
    attrs_obj = network.attrs
    attrs = attrs_obj if isinstance(attrs_obj, dict) else {}

    labels_obj = attrs.get("Labels")
    labels: dict[str, str] = {}
    if isinstance(labels_obj, dict):
        for k, v in labels_obj.items():
            if isinstance(k, str) and isinstance(v, str):
                labels[k] = v

    containers_obj = attrs.get("Containers")
    container_ids: list[str] = []
    if isinstance(containers_obj, dict):
        container_ids = [str(cid) for cid in containers_obj.keys()]

    summary: dict[str, object] = {
        "id": str(attrs.get("Id") or network.id),
        "name": str(attrs.get("Name") or network.name),
        "driver": str(attrs.get("Driver") or "unknown"),
        "scope": str(attrs.get("Scope") or "unknown"),
        "internal": bool(attrs.get("Internal") is True),
        "labels": labels,
        "containers": container_ids,
    }

    if not full:
        return summary

    summary["attrs"] = attrs
    return summary


@app.post("/containers")
def create_container(args: ContainerArgs):
    try:
        container = di.create_container(args)
        return {"container": _container_summary(container)}
    except ImageNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"Image not found: {exc.explanation}"
        ) from exc
    except ContainerError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Container failed during startup: {str(exc)}",
        ) from exc
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.get("/containers")
def list_containers(
    full: bool = False,
    all_containers: bool = True,
    limit: int = -1,
):
    try:
        containers = di.get_containers(all_containers=all_containers, limit=limit)
        return [{"container": _container_summary(c, full=full)} for c in containers]
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.get("/containers/{container_id}")
def get_container(container_id: str, full: bool = False):
    try:
        container = di.get_container(container_id)
        return {"container": _container_summary(container, full=full)}
    except NotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"Container not found: {container_id}"
        ) from exc
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.post("/containers/{container_id}/state/{state}")
def set_container_state(
    container_id: str, state: Literal["stop", "pause", "unpause", "start"]
):
    try:
        container = di.set_container_state(container_id, state)
        return {"container": _container_summary(container)}
    except NotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"Container not found: {container_id}"
        ) from exc
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.post("/containers/{container_id}/exec")
def exec_in_container(container_id: str, payload: ExecCommandRequest):
    try:
        exit_code, output = di.exec_in_container(
            container_id=container_id,
            command=payload.command,
            workdir=payload.workdir,
            user=payload.user,
            environment=payload.environment,
            privileged=payload.privileged,
        )
        return {
            "container_id": container_id,
            "command": payload.command,
            "exit_code": exit_code,
            "output": output,
        }
    except NotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"Container not found: {container_id}"
        ) from exc
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.delete("/containers/{container_id}")
def destroy_container(container_id: str, force: bool = True):
    try:
        di.destroy_container(container_id, force=force)
        return {"status": "removed", "container_id": container_id}
    except NotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"Container not found: {container_id}"
        ) from exc
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.post("/networks")
def create_network(args: NetworkCreateArgs):
    try:
        network = di.create_network(args)
        return {"network": _network_summary(network)}
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.get("/networks")
def list_networks(full: bool = False):
    try:
        networks = di.get_networks()
        return [{"network": _network_summary(n, full=full)} for n in networks]
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.get("/networks/{network_id}")
def get_network(network_id: str, full: bool = False):
    try:
        network = di.get_network(network_id)
        return {"network": _network_summary(network, full=full)}
    except NotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"Network not found: {network_id}"
        ) from exc
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.post("/networks/{network_id}/connect")
def connect_container_to_network(network_id: str, payload: NetworkConnectArgs):
    try:
        network = di.connect_container_to_network(network_id, payload)
        return {"network": _network_summary(network)}
    except NotFound as exc:
        raise HTTPException(
            status_code=404, detail="Network or container not found"
        ) from exc
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.post("/networks/{network_id}/disconnect")
def disconnect_container_from_network(network_id: str, payload: NetworkDisconnectArgs):
    try:
        network = di.disconnect_container_from_network(network_id, payload)
        return {"network": _network_summary(network)}
    except NotFound as exc:
        raise HTTPException(
            status_code=404, detail="Network or container not found"
        ) from exc
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.post("/networks/prune")
def prune_networks():
    try:
        result = di.prune_networks()
        return {"result": result}
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.delete("/networks/{network_id}")
def destroy_network(network_id: str):
    try:
        di.destroy_network(network_id)
        return {"status": "removed", "network_id": network_id}
    except NotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"Network not found: {network_id}"
        ) from exc
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc
