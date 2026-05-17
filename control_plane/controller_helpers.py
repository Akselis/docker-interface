from __future__ import annotations

import re

import httpx
from db.models.container import ContainerStatus
from db.models.host import Host
from db.models.lab import Lab
from db.repos.container import ContainerRepository
from db.repos.host import HostRepository
from db.repos.lab import LabRepository
from db.repos.project import ProjectRepository
from fastapi import HTTPException
from lab_host_client import LabHostClient
from secret_store import get_secret_store

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"

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


def serialize_host(host: Host) -> dict[str, object]:
    return {
        "id": host.id,
        "hostname": host.hostname,
        "ip_address": host.ip_address,
        "port": host.port,
        "scheme": host.scheme,
        "status": host.status.value,
        "base_domain": host.base_domain,
        "dns_zone": host.dns_zone,
        "ingress_target": host.ingress_target,
        "cpu_total": host.cpu_total,
        "memory_total_mb": host.memory_total_mb,
        "created_at_utc": host.created_at_utc.isoformat(),
        "last_heartbeat_utc": host.last_heartbeat_utc.isoformat(),
        "api_key_registered": bool(host.api_key_secret_path),
    }


def serialize_lab(lab: Lab) -> dict[str, object]:
    return {
        "id": lab.id,
        "name": lab.name,
        "host_id": lab.host_id,
        "status": lab.status.value,
        "default_internal_network_id": lab.default_internal_network_id,
        "default_external_network_id": lab.default_external_network_id,
        "cpu_limit": lab.cpu_limit,
        "memory_limit_mb": lab.memory_limit_mb,
        "created_at_utc": lab.created_at_utc.isoformat(),
    }


def parse_memory_limit_to_mb(memory_limit: str | None) -> int | None:
    if memory_limit is None:
        return None

    match = _MEMORY_PATTERN.match(memory_limit)
    if not match:
        raise ValueError(
            "Invalid memory_limit format. Use values like '512m', '1g', or '1024mb'."
        )

    number_raw, unit_raw = match.groups()
    amount = float(number_raw)
    if amount <= 0:
        raise ValueError("memory_limit must be greater than 0")

    multiplier = _MEMORY_MULTIPLIERS.get(unit_raw.lower())
    if multiplier is None:
        raise ValueError(
            f"Unsupported memory unit '{unit_raw}'. Use b/k/m/g/t or ki/mi/gi/ti variants."
        )

    bytes_value = int(amount * multiplier)
    memory_mb = bytes_value // (1024**2)
    if memory_mb <= 0:
        raise ValueError("memory_limit must be at least 1 MiB")

    return memory_mb


def to_container_status(runtime_status: str | None) -> ContainerStatus:
    status_value = (runtime_status or "").lower()
    if status_value in {"running", "restarting"}:
        return ContainerStatus.STARTED
    if status_value in {"paused"}:
        return ContainerStatus.PAUSED
    if status_value in {"exited", "dead", "stopped"}:
        return ContainerStatus.STOPPED
    return ContainerStatus.CREATED


def extract_status_code(result: dict[str, object]) -> int:
    code = result.get("status_code")
    return code if isinstance(code, int) else 500


def extract_body(result: dict[str, object]) -> object:
    return result.get("body")


def is_compose_container(container: dict[str, object]) -> bool:
    labels_obj = container.get("labels")
    if not isinstance(labels_obj, dict):
        return False
    label_value = labels_obj.get(COMPOSE_PROJECT_LABEL)
    return isinstance(label_value, str) and bool(label_value)


def raise_for_lab_host_error(
    result: dict[str, object],
    *,
    operation: str,
    not_found_detail: str | None = None,
) -> None:
    status_code = extract_status_code(result)
    if status_code < 400:
        return

    if status_code == 404 and not_found_detail:
        raise HTTPException(status_code=404, detail=not_found_detail)

    raise HTTPException(
        status_code=502,
        detail={
            "message": f"Lab host operation failed: {operation}",
            "lab_host_status_code": status_code,
            "lab_host_body": extract_body(result),
        },
    )


def extract_container_from_body(body: object) -> dict[str, object] | None:
    if not isinstance(body, dict):
        return None
    container_obj = body.get("container")
    if isinstance(container_obj, dict):
        return container_obj
    return None


async def calculate_lab_allocated_resources(
    lab_id: int,
    container_repo: ContainerRepository,
    project_repo: ProjectRepository,
) -> tuple[float, int]:
    containers = await container_repo.list_by_lab_id(lab_id)
    projects = await project_repo.list_by_lab_id(lab_id)

    cpu_total = sum(float(c.cpu_limit or 0) for c in containers) + sum(
        float(p.cpu_limit or 0) for p in projects
    )
    memory_total_mb = sum(int(c.memory_limit_mb or 0) for c in containers) + sum(
        int(p.memory_limit_mb or 0) for p in projects
    )

    return cpu_total, memory_total_mb


async def calculate_host_allocated_resources(
    host_id: int,
    lab_repo: LabRepository,
    container_repo: ContainerRepository,
    project_repo: ProjectRepository,
) -> tuple[float, int]:
    labs = await lab_repo.list_by_host_id(host_id)

    cpu_total = 0.0
    memory_total_mb = 0

    for lab in labs:
        lab_cpu, lab_memory = await calculate_lab_allocated_resources(
            lab.id, container_repo, project_repo
        )
        cpu_total += lab_cpu
        memory_total_mb += lab_memory

    return cpu_total, memory_total_mb


async def enforce_resource_capacity(
    *,
    lab: Lab,
    host: Host,
    requested_cpu: float | None,
    requested_memory_mb: int | None,
    existing_cpu: float | None,
    existing_memory_mb: int | None,
    resource_kind: str,
    lab_repo: LabRepository,
    container_repo: ContainerRepository,
    project_repo: ProjectRepository,
) -> None:
    lab_cpu_used, lab_memory_used = await calculate_lab_allocated_resources(
        lab.id,
        container_repo,
        project_repo,
    )

    requested_cpu_value = float(requested_cpu or 0)
    requested_memory_value = int(requested_memory_mb or 0)
    existing_cpu_value = float(existing_cpu or 0)
    existing_memory_value = int(existing_memory_mb or 0)

    lab_cpu_after = lab_cpu_used - existing_cpu_value + requested_cpu_value
    lab_memory_after = lab_memory_used - existing_memory_value + requested_memory_value

    if lab.cpu_limit is not None and lab_cpu_after > float(lab.cpu_limit):
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"Insufficient CPU capacity in lab '{lab.name}' for {resource_kind}",
                "lab_cpu_limit": float(lab.cpu_limit),
                "lab_cpu_used": lab_cpu_used,
                "requested_cpu": requested_cpu_value,
                "resulting_cpu": lab_cpu_after,
            },
        )

    if lab.memory_limit_mb is not None and lab_memory_after > int(lab.memory_limit_mb):
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"Insufficient memory capacity in lab '{lab.name}' for {resource_kind}",
                "lab_memory_limit_mb": int(lab.memory_limit_mb),
                "lab_memory_used_mb": lab_memory_used,
                "requested_memory_mb": requested_memory_value,
                "resulting_memory_mb": lab_memory_after,
            },
        )

    host_cpu_used, host_memory_used = await calculate_host_allocated_resources(
        host.id,
        lab_repo,
        container_repo,
        project_repo,
    )

    host_cpu_after = host_cpu_used - existing_cpu_value + requested_cpu_value
    host_memory_after = (
        host_memory_used - existing_memory_value + requested_memory_value
    )

    if host_cpu_after > float(host.cpu_total):
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"Insufficient CPU capacity on host '{host.hostname}' for {resource_kind}",
                "host_cpu_total": float(host.cpu_total),
                "host_cpu_used": host_cpu_used,
                "requested_cpu": requested_cpu_value,
                "resulting_cpu": host_cpu_after,
            },
        )

    if host_memory_after > int(host.memory_total_mb):
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"Insufficient memory capacity on host '{host.hostname}' for {resource_kind}",
                "host_memory_total_mb": int(host.memory_total_mb),
                "host_memory_used_mb": host_memory_used,
                "requested_memory_mb": requested_memory_value,
                "resulting_memory_mb": host_memory_after,
            },
        )


async def resolve_lab_host_connection(
    host_repo: HostRepository,
    lab: Lab,
) -> tuple[Host, str]:
    host = await host_repo.get_by_id(lab.host_id)
    if host is None:
        raise HTTPException(
            status_code=404,
            detail=f"Host not found for lab '{lab.name}'",
        )

    if not host.api_key_secret_path:
        raise HTTPException(
            status_code=400,
            detail=f"Host '{host.hostname}' has no API key secret reference configured",
        )

    secret_store = get_secret_store()
    try:
        api_key = await secret_store.get_secret(host.api_key_secret_path)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch API key from secret store: {exc}",
        ) from exc

    return host, api_key


async def call_lab_host(
    *,
    host: Host,
    api_key: str,
    method: str,
    endpoint_path: str,
    query: dict[str, str] | None = None,
    json_body: object | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, object]:
    client = LabHostClient()
    try:
        return await client.call(
            ip_address=host.ip_address,
            port=host.port,
            scheme=host.scheme,
            api_key=api_key,
            method=method,
            endpoint_path=endpoint_path,
            query=query,
            json_body=json_body,
            timeout_seconds=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Lab host request failed: {exc}",
        ) from exc


async def fetch_runtime_container(
    *,
    host: Host,
    api_key: str,
    container_ref: str,
) -> dict[str, object] | None:
    result = await call_lab_host(
        host=host,
        api_key=api_key,
        method="GET",
        endpoint_path=f"/containers/{container_ref}",
    )
    status_code = extract_status_code(result)
    if status_code == 404:
        return None
    raise_for_lab_host_error(
        result,
        operation=f"get container '{container_ref}'",
    )

    return extract_container_from_body(extract_body(result))


async def remove_runtime_container(
    *,
    host: Host,
    api_key: str,
    container_ref: str,
) -> dict[str, dict[str, object]]:
    stop_result = await call_lab_host(
        host=host,
        api_key=api_key,
        method="POST",
        endpoint_path=f"/containers/{container_ref}/state/stop",
    )

    delete_result = await call_lab_host(
        host=host,
        api_key=api_key,
        method="DELETE",
        endpoint_path=f"/containers/{container_ref}",
        query={"force": "false"},
    )

    delete_status = extract_status_code(delete_result)
    if delete_status >= 400 and delete_status != 404:
        delete_result = await call_lab_host(
            host=host,
            api_key=api_key,
            method="DELETE",
            endpoint_path=f"/containers/{container_ref}",
            query={"force": "true"},
        )

    return {
        "stop": stop_result,
        "delete": delete_result,
    }
