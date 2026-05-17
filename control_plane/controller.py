from __future__ import annotations

from datetime import datetime
from typing import Literal

import httpx
from controller_helpers import (
    call_lab_host,
    enforce_resource_capacity,
    extract_body,
    extract_container_from_body,
    extract_status_code,
    fetch_runtime_container,
    is_compose_container,
    parse_memory_limit_to_mb,
    raise_for_lab_host_error,
    remove_runtime_container,
    resolve_lab_host_connection,
    serialize_host,
    serialize_lab,
    to_container_status,
)
from db.models.container import Container
from db.models.host import Host
from db.models.lab import Lab, LabStatus
from db.models.network import Network, NetworkDriver
from db.models.project import Project, ProjectNetworkMode
from db.repos.container import ContainerRepository
from db.repos.host import HostRepository
from db.repos.lab import LabRepository
from db.repos.network import NetworkRepository
from db.repos.project import ProjectRepository
from db.session import get_session
from fastapi import Depends, FastAPI, HTTPException, status
from lab_host_client import LabHostClient
from models import (
    CallLabHostRequest,
    ComposeDeployRequest,
    CreateLabRequest,
    DeployEnvironmentRequest,
    EnvironmentNetworkMode,
    NameListRequest,
    RegisterHostRequest,
)
from networking.providers import get_dns_provider, get_ingress_provider
from rabbitmq_consumer import start_consumer_thread
from secret_store import build_host_api_key_secret_path, get_secret_store
from sqlalchemy.ext.asyncio import AsyncSession

app = FastAPI()


def _safe_slug(value: str) -> str:
    slug = "".join(ch for ch in value.lower() if ch.isalnum() or ch in "-_")
    return slug.strip("-_") or "lab"


def _extract_network_from_result(
    result: dict[str, object],
) -> tuple[str, str, NetworkDriver]:
    body = extract_body(result)
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=502,
            detail="Invalid network response from lab host",
        )

    network_obj = body.get("network")
    if not isinstance(network_obj, dict):
        raise HTTPException(
            status_code=502,
            detail="Lab host network response is missing 'network' payload",
        )

    network_id_obj = network_obj.get("id")
    network_name_obj = network_obj.get("name")
    driver_obj = network_obj.get("driver")

    if not isinstance(network_id_obj, str) or not isinstance(network_name_obj, str):
        raise HTTPException(
            status_code=502,
            detail="Lab host network payload is malformed",
        )

    driver_value = str(driver_obj or "bridge").lower()
    driver = NetworkDriver.BRIDGE
    if driver_value == "overlay":
        driver = NetworkDriver.OVERLAY

    return network_id_obj, network_name_obj, driver


def _route_hostname(*, resource_name: str, lab_name: str, base_domain: str) -> str:
    return (
        f"{_safe_slug(resource_name)}-{_safe_slug(lab_name)}.{base_domain.strip('.')}"
    )


def _resolve_upstream_port_from_ports(payload_ports: object) -> int | None:
    if not isinstance(payload_ports, list):
        return None

    for port in payload_ports:
        if not isinstance(port, dict):
            continue
        host_port = port.get("host")
        protocol = str(port.get("protocol") or "").lower()
        if (
            isinstance(host_port, int)
            and 1 <= host_port <= 65535
            and protocol
            in {
                "tcp",
                "",
            }
        ):
            return host_port
    return None


async def _ensure_dns_wildcard_for_host(host: Host) -> None:
    if not host.base_domain or not host.ingress_target:
        return

    wildcard_fqdn = f"*.{host.base_domain.strip('.')}"
    zone = host.dns_zone or host.base_domain
    dns = get_dns_provider()
    await dns.ensure_wildcard(
        zone=zone,
        wildcard_fqdn=wildcard_fqdn,
        target=host.ingress_target,
    )


async def _ensure_ingress_route_for_runtime_container(
    *,
    host: Host,
    lab: Lab,
    resource_name: str,
    runtime: dict[str, object],
) -> str | None:
    if not host.base_domain:
        return None

    ports_obj = runtime.get("ports")
    upstream_port = _resolve_upstream_port_from_ports(ports_obj)
    if upstream_port is None:
        return None

    fqdn = _route_hostname(
        resource_name=resource_name,
        lab_name=lab.name,
        base_domain=host.base_domain,
    )

    ingress = get_ingress_provider()
    await ingress.ensure_route(
        hostname=fqdn,
        upstream_host=host.ip_address,
        upstream_port=upstream_port,
        metadata={
            "lab_name": lab.name,
            "host_id": str(host.id),
            "resource_name": resource_name,
        },
    )

    return fqdn


def _compose_service_name(container: dict[str, object]) -> str | None:
    labels_obj = container.get("labels")
    if not isinstance(labels_obj, dict):
        return None

    service_name = labels_obj.get("com.docker.compose.service")
    if isinstance(service_name, str) and service_name:
        return service_name
    return None


async def _delete_ingress_route_if_possible(
    *,
    host: Host,
    lab_name: str,
    resource_name: str,
) -> None:
    if not host.base_domain:
        return

    fqdn = _route_hostname(
        resource_name=resource_name,
        lab_name=lab_name,
        base_domain=host.base_domain,
    )
    ingress = get_ingress_provider()
    await ingress.delete_route(hostname=fqdn)


@app.on_event("startup")
def startup_event() -> None:
    start_consumer_thread()


@app.post("/hosts/register")
async def register_host(
    payload: RegisterHostRequest,
    session: AsyncSession = Depends(get_session),
):
    client = LabHostClient()

    try:
        health = await client.check_health(
            ip_address=payload.ip_address,
            port=payload.port,
            scheme=payload.scheme,
            api_key=payload.api_key,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to contact lab host at {payload.scheme}://{payload.ip_address}:{payload.port}: {exc}",
        ) from exc

    health_status_code = extract_status_code(health)
    if health_status_code == status.HTTP_401_UNAUTHORIZED:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Lab host rejected API key during registration",
        )
    if health_status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Lab host health verification failed",
                "lab_host_status_code": health_status_code,
                "lab_host_body": extract_body(health),
            },
        )

    host_repo = HostRepository(session)
    by_hostname = await host_repo.get_by_hostname(payload.hostname)
    by_ip = await host_repo.get_by_ip(payload.ip_address)

    if by_hostname and by_ip and by_hostname.id != by_ip.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Hostname and IP are already assigned to different hosts",
        )

    secret_store = get_secret_store()
    secret_path = build_host_api_key_secret_path(payload.hostname)
    try:
        await secret_store.put_secret(secret_path, payload.api_key)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to store API key in secret store: {exc}",
        ) from exc

    now = datetime.utcnow()
    existing = by_hostname or by_ip

    if existing is None:
        host = await host_repo.insert(
            Host(
                hostname=payload.hostname,
                ip_address=payload.ip_address,
                port=payload.port,
                scheme=payload.scheme,
                api_key_secret_path=secret_path,
                status=payload.status,
                base_domain=payload.base_domain,
                dns_zone=payload.dns_zone,
                ingress_target=payload.ingress_target,
                cpu_total=payload.cpu_total,
                memory_total_mb=payload.memory_total_mb,
                last_heartbeat_utc=now,
            )
        )
        await _ensure_dns_wildcard_for_host(host)
        return {"status": "registered", "host": serialize_host(host)}

    existing.hostname = payload.hostname
    existing.ip_address = payload.ip_address
    existing.port = payload.port
    existing.scheme = payload.scheme
    existing.api_key_secret_path = secret_path
    existing.status = payload.status
    existing.base_domain = payload.base_domain
    existing.dns_zone = payload.dns_zone
    existing.ingress_target = payload.ingress_target
    existing.cpu_total = payload.cpu_total
    existing.memory_total_mb = payload.memory_total_mb
    existing.last_heartbeat_utc = now
    host = await host_repo.update(existing)
    await _ensure_dns_wildcard_for_host(host)
    return {"status": "updated", "host": serialize_host(host)}


@app.post("/hosts/{host_id}/call")
async def call_lab_host_endpoint(
    host_id: int,
    payload: CallLabHostRequest,
    session: AsyncSession = Depends(get_session),
):
    host_repo = HostRepository(session)
    host = await host_repo.get_by_id(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"Host not found: {host_id}")

    if not host.api_key_secret_path:
        raise HTTPException(
            status_code=400,
            detail="Host has no API key secret reference configured",
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

    result = await call_lab_host(
        host=host,
        api_key=api_key,
        method=payload.method,
        endpoint_path=payload.endpoint_path,
        query=payload.query,
        json_body=payload.json_body,
        timeout_seconds=payload.timeout_seconds,
    )

    return {
        "host_id": host_id,
        "request": {
            "method": payload.method,
            "endpoint_path": payload.endpoint_path,
            "query": payload.query,
        },
        "response": result,
    }


@app.get("/labs")
async def get_labs(session: AsyncSession = Depends(get_session)):
    lab_repo = LabRepository(session)
    labs = await lab_repo.list_all()
    return {"labs": [serialize_lab(lab) for lab in labs]}


@app.post("/labs", status_code=status.HTTP_201_CREATED)
async def create_lab(
    payload: CreateLabRequest,
    session: AsyncSession = Depends(get_session),
):
    lab_repo = LabRepository(session)
    host_repo = HostRepository(session)
    network_repo = NetworkRepository(session)

    existing_lab = await lab_repo.get_by_name(payload.name)
    if existing_lab is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Lab already exists: {payload.name}",
        )

    host = await host_repo.get_by_id(payload.host_id)
    if host is None:
        raise HTTPException(
            status_code=404, detail=f"Host not found: {payload.host_id}"
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

    health = await call_lab_host(
        host=host,
        api_key=api_key,
        method="GET",
        endpoint_path="/health",
    )
    raise_for_lab_host_error(health, operation="health check")

    created = await lab_repo.insert(
        Lab(
            name=payload.name,
            host_id=payload.host_id,
            status=payload.status,
            cpu_limit=payload.cpu_limit,
            memory_limit_mb=payload.memory_limit_mb,
        )
    )

    default_internal_network_name = (
        f"lab-{_safe_slug(payload.name)}-{created.id}-internal"
    )
    default_external_network_name = (
        f"lab-{_safe_slug(payload.name)}-{created.id}-external"
    )

    created_network_ids: list[str] = []

    try:
        internal_result = await call_lab_host(
            host=host,
            api_key=api_key,
            method="POST",
            endpoint_path="/networks",
            json_body={
                "name": default_internal_network_name,
                "driver": "bridge",
                "internal": True,
                "labels": {
                    "evlab.managed_by": "control-plane",
                    "evlab.lab.id": str(created.id),
                    "evlab.lab.name": payload.name,
                    "evlab.network.role": "default-internal",
                },
            },
        )
        raise_for_lab_host_error(
            internal_result,
            operation=f"create default internal network for lab '{payload.name}'",
        )
        internal_network_id, internal_name, internal_driver = (
            _extract_network_from_result(internal_result)
        )
        created_network_ids.append(internal_network_id)
        internal_network_row = await network_repo.insert(
            Network(
                network_id=internal_network_id,
                name=internal_name,
                lab_id=created.id,
                driver=internal_driver,
            )
        )

        external_result = await call_lab_host(
            host=host,
            api_key=api_key,
            method="POST",
            endpoint_path="/networks",
            json_body={
                "name": default_external_network_name,
                "driver": "bridge",
                "internal": False,
                "labels": {
                    "evlab.managed_by": "control-plane",
                    "evlab.lab.id": str(created.id),
                    "evlab.lab.name": payload.name,
                    "evlab.network.role": "default-external",
                },
            },
        )
        raise_for_lab_host_error(
            external_result,
            operation=f"create default external network for lab '{payload.name}'",
        )
        external_network_id, external_name, external_driver = (
            _extract_network_from_result(external_result)
        )
        created_network_ids.append(external_network_id)
        external_network_row = await network_repo.insert(
            Network(
                network_id=external_network_id,
                name=external_name,
                lab_id=created.id,
                driver=external_driver,
            )
        )

        created.default_internal_network_id = internal_network_row.id
        created.default_external_network_id = external_network_row.id
        created = await lab_repo.update(created)
    except Exception:
        for network_id in created_network_ids:
            try:
                await call_lab_host(
                    host=host,
                    api_key=api_key,
                    method="DELETE",
                    endpoint_path=f"/networks/{network_id}",
                )
            except Exception:
                pass

        await lab_repo.delete_by_lab_id(created.id)
        raise

    return {"lab": serialize_lab(created)}


@app.delete("/labs/{lab_name}")
async def delete_lab(
    lab_name: str,
    session: AsyncSession = Depends(get_session),
):
    lab_repo = LabRepository(session)
    host_repo = HostRepository(session)
    project_repo = ProjectRepository(session)
    container_repo = ContainerRepository(session)
    network_repo = NetworkRepository(session)

    lab = await lab_repo.get_by_name(lab_name)
    if lab is None:
        raise HTTPException(status_code=404, detail=f"Lab not found: {lab_name}")

    host, api_key = await resolve_lab_host_connection(host_repo, lab)

    removed_projects: list[str] = []
    failed_projects: list[dict[str, object]] = []

    project_rows = await project_repo.list_by_lab_id(lab.id)
    for row in project_rows:
        result = await call_lab_host(
            host=host,
            api_key=api_key,
            method="DELETE",
            endpoint_path=f"/compose/{row.project_name}",
            query={"remove_volumes": "true"},
        )
        code = extract_status_code(result)
        if code >= 400 and code != 404:
            failed_projects.append(
                {
                    "project_name": row.project_name,
                    "status_code": code,
                    "body": extract_body(result),
                }
            )
        else:
            removed_projects.append(row.project_name)
            if isinstance(row.exposed_services, list):
                for service_name in row.exposed_services:
                    if isinstance(service_name, str) and service_name:
                        await _delete_ingress_route_if_possible(
                            host=host,
                            lab_name=lab.name,
                            resource_name=service_name,
                        )

    removed_containers: list[str] = []
    failed_containers: list[dict[str, object]] = []

    container_rows = await container_repo.list_by_lab_id(lab.id)
    for row in container_rows:
        operation_result = await remove_runtime_container(
            host=host,
            api_key=api_key,
            container_ref=row.container_id,
        )
        delete_code = extract_status_code(operation_result["delete"])
        if delete_code >= 400 and delete_code != 404:
            failed_containers.append(
                {
                    "container_name": row.name,
                    "container_id": row.container_id,
                    "status_code": delete_code,
                    "body": extract_body(operation_result["delete"]),
                }
            )
        else:
            removed_containers.append(row.name)
            await _delete_ingress_route_if_possible(
                host=host,
                lab_name=lab.name,
                resource_name=row.name,
            )

    removed_networks: list[str] = []
    failed_networks: list[dict[str, object]] = []

    network_rows = await network_repo.list_by_lab_id(lab.id)
    for row in network_rows:
        result = await call_lab_host(
            host=host,
            api_key=api_key,
            method="DELETE",
            endpoint_path=f"/networks/{row.network_id}",
        )
        code = extract_status_code(result)
        if code >= 400 and code != 404:
            failed_networks.append(
                {
                    "network_name": row.name,
                    "network_id": row.network_id,
                    "status_code": code,
                    "body": extract_body(result),
                }
            )
        else:
            removed_networks.append(row.name)

    volumes_prune = await call_lab_host(
        host=host,
        api_key=api_key,
        method="POST",
        endpoint_path="/volumes/prune",
    )

    await project_repo.delete_by_lab_id(lab.id)
    await container_repo.delete_by_lab_id(lab.id)
    await network_repo.delete_by_lab_id(lab.id)
    await lab_repo.delete_by_lab_id(lab.id)

    return {
        "status": "removed",
        "lab_name": lab_name,
        "removed_projects": removed_projects,
        "failed_projects": failed_projects,
        "removed_containers": removed_containers,
        "failed_containers": failed_containers,
        "removed_networks": removed_networks,
        "failed_networks": failed_networks,
        "volumes_prune": volumes_prune,
    }


@app.get("/lab/{lab_name}/environments")
async def get_lab_environments(
    lab_name: str,
    session: AsyncSession = Depends(get_session),
):
    lab_repo = LabRepository(session)
    host_repo = HostRepository(session)
    container_repo = ContainerRepository(session)

    lab = await lab_repo.get_by_name(lab_name)
    if lab is None:
        raise HTTPException(status_code=404, detail=f"Lab not found: {lab_name}")

    host, api_key = await resolve_lab_host_connection(host_repo, lab)
    rows = await container_repo.list_by_lab_id(lab.id)

    environments: list[dict[str, object]] = []
    for row in rows:
        runtime = await fetch_runtime_container(
            host=host,
            api_key=api_key,
            container_ref=row.container_id,
        )
        if runtime is None:
            continue
        if is_compose_container(runtime):
            continue
        environments.append(runtime)

    return {"lab": serialize_lab(lab), "environments": environments}


@app.get("/lab/{lab_name}/environments/{container_name}")
async def get_lab_environment(
    lab_name: str,
    container_name: str,
    session: AsyncSession = Depends(get_session),
):
    lab_repo = LabRepository(session)
    host_repo = HostRepository(session)
    container_repo = ContainerRepository(session)

    lab = await lab_repo.get_by_name(lab_name)
    if lab is None:
        raise HTTPException(status_code=404, detail=f"Lab not found: {lab_name}")

    row = await container_repo.get_by_name(lab.id, container_name)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Environment not found: {container_name}"
        )

    host, api_key = await resolve_lab_host_connection(host_repo, lab)
    runtime = await fetch_runtime_container(
        host=host,
        api_key=api_key,
        container_ref=row.container_id,
    )
    if runtime is None:
        raise HTTPException(
            status_code=404,
            detail=f"Environment runtime container not found: {container_name}",
        )

    if is_compose_container(runtime):
        raise HTTPException(
            status_code=400,
            detail=f"Container '{container_name}' belongs to a compose project",
        )

    return {"lab": serialize_lab(lab), "environment": runtime}


@app.get("/lab/{lab_name}/projects")
async def get_lab_projects(
    lab_name: str,
    session: AsyncSession = Depends(get_session),
):
    lab_repo = LabRepository(session)
    host_repo = HostRepository(session)
    project_repo = ProjectRepository(session)

    lab = await lab_repo.get_by_name(lab_name)
    if lab is None:
        raise HTTPException(status_code=404, detail=f"Lab not found: {lab_name}")

    host, api_key = await resolve_lab_host_connection(host_repo, lab)
    project_rows = await project_repo.list_by_lab_id(lab.id)

    projects: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []

    for row in project_rows:
        result = await call_lab_host(
            host=host,
            api_key=api_key,
            method="GET",
            endpoint_path=f"/compose/{row.project_name}/ps",
        )
        code = extract_status_code(result)
        body = extract_body(result)

        if code == 404:
            continue
        if code >= 400:
            failed.append(
                {
                    "project_name": row.project_name,
                    "status_code": code,
                    "body": body,
                }
            )
            continue

        if isinstance(body, dict):
            project_payload = body.get("project")
            if isinstance(project_payload, dict):
                projects.append(project_payload)

    return {
        "lab": serialize_lab(lab),
        "projects": projects,
        "failed": failed,
    }


@app.get("/lab/{lab_name}/projects/{project_name}")
async def get_lab_project(
    lab_name: str,
    project_name: str,
    session: AsyncSession = Depends(get_session),
):
    lab_repo = LabRepository(session)
    host_repo = HostRepository(session)
    project_repo = ProjectRepository(session)

    lab = await lab_repo.get_by_name(lab_name)
    if lab is None:
        raise HTTPException(status_code=404, detail=f"Lab not found: {lab_name}")

    project_row = await project_repo.get_by_name(lab.id, project_name)
    if project_row is None:
        raise HTTPException(
            status_code=404, detail=f"Project not found: {project_name}"
        )

    host, api_key = await resolve_lab_host_connection(host_repo, lab)
    result = await call_lab_host(
        host=host,
        api_key=api_key,
        method="GET",
        endpoint_path=f"/compose/{project_name}/ps",
    )
    raise_for_lab_host_error(
        result,
        operation=f"compose ps for '{project_name}'",
        not_found_detail=f"Project runtime not found on host: {project_name}",
    )

    body = extract_body(result)
    if not isinstance(body, dict) or not isinstance(body.get("project"), dict):
        raise HTTPException(
            status_code=502,
            detail="Invalid response from lab host for compose project query",
        )

    return {"lab": serialize_lab(lab), "project": body["project"]}


@app.post("/lab/{lab_name}/environments", status_code=status.HTTP_201_CREATED)
async def deploy_lab_environment(
    lab_name: str,
    payload: DeployEnvironmentRequest,
    session: AsyncSession = Depends(get_session),
):
    lab_repo = LabRepository(session)
    host_repo = HostRepository(session)
    container_repo = ContainerRepository(session)
    network_repo = NetworkRepository(session)

    lab = await lab_repo.get_by_name(lab_name)
    if lab is None:
        raise HTTPException(status_code=404, detail=f"Lab not found: {lab_name}")

    existing_by_name = await container_repo.get_by_name(lab.id, payload.name)

    requested_cpu = (
        float(payload.resources.cpu_count)
        if payload.resources is not None and payload.resources.cpu_count is not None
        else None
    )
    try:
        requested_memory_mb = parse_memory_limit_to_mb(
            payload.resources.memory_limit if payload.resources is not None else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing_cpu = (
        float(existing_by_name.cpu_limit)
        if existing_by_name is not None and existing_by_name.cpu_limit is not None
        else None
    )
    existing_memory_mb = (
        int(existing_by_name.memory_limit_mb)
        if existing_by_name is not None and existing_by_name.memory_limit_mb is not None
        else None
    )

    host, api_key = await resolve_lab_host_connection(host_repo, lab)
    await enforce_resource_capacity(
        lab=lab,
        host=host,
        requested_cpu=requested_cpu,
        requested_memory_mb=requested_memory_mb,
        existing_cpu=existing_cpu,
        existing_memory_mb=existing_memory_mb,
        resource_kind=f"environment '{payload.name}'",
        lab_repo=lab_repo,
        container_repo=container_repo,
        project_repo=ProjectRepository(session),
    )

    lab_host_payload = payload.model_dump(
        exclude={"lifetime_type", "time_to_live_seconds", "network_mode"}
    )

    created_private_network: Network | None = None

    if payload.network_mode == EnvironmentNetworkMode.OFFLINE:
        lab_host_payload["network"] = [{"name": "none"}]
    elif payload.network_mode == EnvironmentNetworkMode.INTERNAL_EXPOSED:
        if lab.default_internal_network_id is None:
            raise HTTPException(
                status_code=409,
                detail=f"Lab '{lab.name}' has no default internal network configured",
            )

        default_network = await network_repo.get_by_id(lab.default_internal_network_id)
        if default_network is None:
            raise HTTPException(
                status_code=409,
                detail=f"Default internal network entry not found for lab '{lab.name}'",
            )

        lab_host_payload["network"] = [{"name": default_network.name}]
    elif payload.network_mode == EnvironmentNetworkMode.EXTERNAL_EXPOSED:
        if lab.default_external_network_id is None:
            raise HTTPException(
                status_code=409,
                detail=f"Lab '{lab.name}' has no default external network configured",
            )

        default_network = await network_repo.get_by_id(lab.default_external_network_id)
        if default_network is None:
            raise HTTPException(
                status_code=409,
                detail=f"Default external network entry not found for lab '{lab.name}'",
            )

        lab_host_payload["network"] = [{"name": default_network.name}]
    else:
        is_external_private = (
            payload.network_mode == EnvironmentNetworkMode.EXTERNAL_PRIVATE
        )
        private_network_name = (
            f"lab-{_safe_slug(lab.name)}-{lab.id}-env-{_safe_slug(payload.name)}"
        )
        private_result = await call_lab_host(
            host=host,
            api_key=api_key,
            method="POST",
            endpoint_path="/networks",
            json_body={
                "name": private_network_name,
                "driver": "bridge",
                "internal": not is_external_private,
                "labels": {
                    "evlab.managed_by": "control-plane",
                    "evlab.lab.id": str(lab.id),
                    "evlab.lab.name": lab.name,
                    "evlab.network.role": "environment-private",
                    "evlab.environment.name": payload.name,
                    "evlab.environment.exposure": "external"
                    if is_external_private
                    else "internal",
                },
            },
        )
        raise_for_lab_host_error(
            private_result,
            operation=f"create private network for environment '{payload.name}'",
        )

        network_id, resolved_name, network_driver = _extract_network_from_result(
            private_result
        )
        created_private_network = await network_repo.insert(
            Network(
                network_id=network_id,
                name=resolved_name,
                lab_id=lab.id,
                driver=network_driver,
            )
        )
        lab_host_payload["network"] = [{"name": resolved_name}]

    try:
        result = await call_lab_host(
            host=host,
            api_key=api_key,
            method="POST",
            endpoint_path="/containers",
            json_body=lab_host_payload,
        )
        raise_for_lab_host_error(result, operation="create container")
    except Exception:
        if created_private_network is not None:
            try:
                await call_lab_host(
                    host=host,
                    api_key=api_key,
                    method="DELETE",
                    endpoint_path=f"/networks/{created_private_network.network_id}",
                )
            except Exception:
                pass
            await network_repo.delete_row(created_private_network)
        raise

    body = extract_body(result)
    runtime = extract_container_from_body(body)
    if runtime is None:
        raise HTTPException(
            status_code=502,
            detail="Invalid response from lab host container creation endpoint",
        )

    if is_compose_container(runtime):
        raise HTTPException(
            status_code=400,
            detail="Container was created as compose-managed. Use project endpoints instead.",
        )

    container_id_obj = runtime.get("id")
    container_name_obj = runtime.get("name")
    image_obj = runtime.get("image")

    if not (
        isinstance(container_id_obj, str)
        and isinstance(container_name_obj, str)
        and isinstance(image_obj, str)
    ):
        raise HTTPException(
            status_code=502,
            detail="Lab host returned malformed container payload",
        )

    existing = await container_repo.get_by_container_id(container_id_obj)

    now = datetime.utcnow()
    runtime_status_obj = runtime.get("status")
    mapped_status = to_container_status(
        runtime_status_obj if isinstance(runtime_status_obj, str) else None
    )

    cpu_limit_value = int(requested_cpu) if requested_cpu is not None else None
    memory_limit_mb = requested_memory_mb

    if existing is None:
        await container_repo.insert(
            Container(
                container_id=container_id_obj,
                name=container_name_obj,
                image=image_obj,
                lab_id=lab.id,
                status=mapped_status,
                cpu_limit=cpu_limit_value,
                memory_limit_mb=memory_limit_mb,
                lifetime_type=payload.lifetime_type,
                time_to_live_seconds=payload.time_to_live_seconds,
                last_seen_utc=now,
            )
        )
    else:
        existing.name = container_name_obj
        existing.image = image_obj
        existing.lab_id = lab.id
        existing.status = mapped_status
        existing.cpu_limit = cpu_limit_value
        existing.memory_limit_mb = memory_limit_mb
        existing.lifetime_type = payload.lifetime_type
        existing.time_to_live_seconds = payload.time_to_live_seconds
        existing.last_seen_utc = now
        await container_repo.update(existing)

    ingress_hostname: str | None = None
    if payload.network_mode in {
        EnvironmentNetworkMode.EXTERNAL_PRIVATE,
        EnvironmentNetworkMode.EXTERNAL_EXPOSED,
    }:
        full_runtime_result = await call_lab_host(
            host=host,
            api_key=api_key,
            method="GET",
            endpoint_path=f"/containers/{container_id_obj}",
            query={"full": "true"},
        )
        raise_for_lab_host_error(
            full_runtime_result,
            operation=f"get runtime container '{container_id_obj}'",
        )
        full_runtime_body = extract_body(full_runtime_result)
        full_runtime = extract_container_from_body(full_runtime_body)
        if isinstance(full_runtime, dict):
            ingress_hostname = await _ensure_ingress_route_for_runtime_container(
                host=host,
                lab=lab,
                resource_name=container_name_obj,
                runtime=full_runtime,
            )

    if lab.status != LabStatus.RUNNING:
        lab.status = LabStatus.RUNNING
        await lab_repo.update(lab)

    response: dict[str, object] = {"lab": serialize_lab(lab), "environment": runtime}
    if ingress_hostname is not None:
        response["ingress_hostname"] = ingress_hostname
    return response


@app.post("/lab/{lab_name}/projects", status_code=status.HTTP_201_CREATED)
async def deploy_lab_project(
    lab_name: str,
    payload: ComposeDeployRequest,
    session: AsyncSession = Depends(get_session),
):
    lab_repo = LabRepository(session)
    host_repo = HostRepository(session)
    project_repo = ProjectRepository(session)

    lab = await lab_repo.get_by_name(lab_name)
    if lab is None:
        raise HTTPException(status_code=404, detail=f"Lab not found: {lab_name}")

    row = await project_repo.get_by_name(lab.id, payload.project_name)

    requested_cpu = float(payload.cpu_limit) if payload.cpu_limit is not None else None
    try:
        requested_memory_mb = parse_memory_limit_to_mb(payload.memory_limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing_cpu = (
        float(row.cpu_limit) if row is not None and row.cpu_limit is not None else None
    )
    existing_memory_mb = (
        int(row.memory_limit_mb)
        if row is not None and row.memory_limit_mb is not None
        else None
    )

    if payload.network_mode == EnvironmentNetworkMode.OFFLINE:
        raise HTTPException(
            status_code=400,
            detail="Project network_mode 'offline' is not supported",
        )

    host, api_key = await resolve_lab_host_connection(host_repo, lab)
    await enforce_resource_capacity(
        lab=lab,
        host=host,
        requested_cpu=requested_cpu,
        requested_memory_mb=requested_memory_mb,
        existing_cpu=existing_cpu,
        existing_memory_mb=existing_memory_mb,
        resource_kind=f"project '{payload.project_name}'",
        lab_repo=lab_repo,
        container_repo=ContainerRepository(session),
        project_repo=project_repo,
    )

    result = await call_lab_host(
        host=host,
        api_key=api_key,
        method="POST",
        endpoint_path="/compose/deploy",
        json_body=payload.model_dump(),
        timeout_seconds=120,
    )
    raise_for_lab_host_error(
        result, operation=f"deploy project '{payload.project_name}'"
    )

    body = extract_body(result)
    if not isinstance(body, dict) or not isinstance(body.get("project"), dict):
        raise HTTPException(
            status_code=502,
            detail="Invalid response from lab host compose deploy endpoint",
        )

    project_payload = body["project"]
    containers_obj = project_payload.get("containers")
    project_containers = containers_obj if isinstance(containers_obj, list) else []

    network_mode = ProjectNetworkMode(payload.network_mode.value)
    if network_mode == ProjectNetworkMode.INTERNAL_EXPOSED:
        if lab.default_internal_network_id is None:
            raise HTTPException(
                status_code=409,
                detail=f"Lab '{lab.name}' has no default internal network configured",
            )
        default_internal_network = await NetworkRepository(session).get_by_id(
            lab.default_internal_network_id
        )
        if default_internal_network is None:
            raise HTTPException(
                status_code=409,
                detail=f"Default internal network entry not found for lab '{lab.name}'",
            )

        for container in project_containers:
            if not isinstance(container, dict):
                continue
            container_id = container.get("id")
            if not isinstance(container_id, str) or not container_id:
                continue
            connect_result = await call_lab_host(
                host=host,
                api_key=api_key,
                method="POST",
                endpoint_path=f"/networks/{default_internal_network.network_id}/connect",
                json_body={"container_id": container_id},
            )
            raise_for_lab_host_error(
                connect_result,
                operation=(
                    f"connect compose container '{container_id}' to internal lab network"
                ),
            )

    if network_mode == ProjectNetworkMode.EXTERNAL_EXPOSED:
        if lab.default_external_network_id is None:
            raise HTTPException(
                status_code=409,
                detail=f"Lab '{lab.name}' has no default external network configured",
            )
        default_external_network = await NetworkRepository(session).get_by_id(
            lab.default_external_network_id
        )
        if default_external_network is None:
            raise HTTPException(
                status_code=409,
                detail=f"Default external network entry not found for lab '{lab.name}'",
            )

        for container in project_containers:
            if not isinstance(container, dict):
                continue
            container_id = container.get("id")
            if not isinstance(container_id, str) or not container_id:
                continue
            connect_result = await call_lab_host(
                host=host,
                api_key=api_key,
                method="POST",
                endpoint_path=f"/networks/{default_external_network.network_id}/connect",
                json_body={"container_id": container_id},
            )
            raise_for_lab_host_error(
                connect_result,
                operation=(
                    f"connect compose container '{container_id}' to external lab network"
                ),
            )

    exposed_services = [
        item.strip()
        for item in (payload.exposed_services or [])
        if isinstance(item, str) and item.strip()
    ]

    ingress_routes: dict[str, str] = {}
    if network_mode in {
        ProjectNetworkMode.EXTERNAL_PRIVATE,
        ProjectNetworkMode.EXTERNAL_EXPOSED,
    }:
        routed_services: set[str] = set()
        for container in project_containers:
            if not isinstance(container, dict):
                continue
            service_name = _compose_service_name(container)
            if not isinstance(service_name, str) or service_name in routed_services:
                continue
            if service_name not in exposed_services:
                continue

            container_id = container.get("id")
            if not isinstance(container_id, str) or not container_id:
                continue

            full_runtime_result = await call_lab_host(
                host=host,
                api_key=api_key,
                method="GET",
                endpoint_path=f"/containers/{container_id}",
                query={"full": "true"},
            )
            raise_for_lab_host_error(
                full_runtime_result,
                operation=f"get runtime container '{container_id}'",
            )
            full_runtime_body = extract_body(full_runtime_result)
            full_runtime = extract_container_from_body(full_runtime_body)
            if not isinstance(full_runtime, dict):
                continue

            hostname = await _ensure_ingress_route_for_runtime_container(
                host=host,
                lab=lab,
                resource_name=service_name,
                runtime=full_runtime,
            )
            if hostname is not None:
                ingress_routes[service_name] = hostname
            routed_services.add(service_name)

    if row is None:
        await project_repo.insert(
            Project(
                lab_id=lab.id,
                project_name=payload.project_name,
                source_type=payload.source_type,
                source_url=payload.source_url,
                ref=payload.ref,
                compose_file=payload.compose_file,
                compose_content=payload.compose_content,
                env=payload.env,
                pull=payload.pull,
                build=payload.build,
                network_mode=network_mode,
                exposed_services=exposed_services or None,
                cpu_limit=requested_cpu,
                memory_limit_mb=requested_memory_mb,
            )
        )
    else:
        row.source_type = payload.source_type
        row.source_url = payload.source_url
        row.ref = payload.ref
        row.compose_file = payload.compose_file
        row.compose_content = payload.compose_content
        row.env = payload.env
        row.pull = payload.pull
        row.build = payload.build
        row.network_mode = network_mode
        row.exposed_services = exposed_services or None
        row.cpu_limit = requested_cpu
        row.memory_limit_mb = requested_memory_mb
        await project_repo.update(row)

    if lab.status != LabStatus.RUNNING:
        lab.status = LabStatus.RUNNING
        await lab_repo.update(lab)

    response: dict[str, object] = {
        "lab": serialize_lab(lab),
        "project": body["project"],
    }
    if ingress_routes:
        response["ingress_routes"] = ingress_routes
    return response


@app.delete("/lab/{lab_name}/environments")
async def delete_lab_environments(
    lab_name: str,
    session: AsyncSession = Depends(get_session),
):
    lab_repo = LabRepository(session)
    host_repo = HostRepository(session)
    container_repo = ContainerRepository(session)
    network_repo = NetworkRepository(session)

    lab = await lab_repo.get_by_name(lab_name)
    if lab is None:
        raise HTTPException(status_code=404, detail=f"Lab not found: {lab_name}")

    host, api_key = await resolve_lab_host_connection(host_repo, lab)
    rows = await container_repo.list_by_lab_id(lab.id)

    removed: list[str] = []
    failed: list[dict[str, object]] = []

    for row in rows:
        runtime = await fetch_runtime_container(
            host=host,
            api_key=api_key,
            container_ref=row.container_id,
        )
        if runtime is not None and is_compose_container(runtime):
            continue

        operation_result = await remove_runtime_container(
            host=host,
            api_key=api_key,
            container_ref=row.container_id,
        )
        delete_status = extract_status_code(operation_result["delete"])

        if delete_status >= 400 and delete_status != 404:
            failed.append(
                {
                    "container_name": row.name,
                    "container_id": row.container_id,
                    "status_code": delete_status,
                    "body": extract_body(operation_result["delete"]),
                }
            )
            continue

        removed.append(row.name)
        await container_repo.delete_row(row)
        await _delete_ingress_route_if_possible(
            host=host,
            lab_name=lab.name,
            resource_name=row.name,
        )

        private_network_name = (
            f"lab-{_safe_slug(lab.name)}-{lab.id}-env-{_safe_slug(row.name)}"
        )
        private_network = await network_repo.get_by_name(lab.id, private_network_name)
        if private_network is not None:
            network_result = await call_lab_host(
                host=host,
                api_key=api_key,
                method="DELETE",
                endpoint_path=f"/networks/{private_network.network_id}",
            )
            network_code = extract_status_code(network_result)
            if network_code < 400 or network_code == 404:
                await network_repo.delete_row(private_network)

    return {
        "lab": serialize_lab(lab),
        "removed": removed,
        "failed": failed,
    }


@app.delete("/lab/{lab_name}/environments/{container_name}")
async def delete_lab_environment(
    lab_name: str,
    container_name: str,
    session: AsyncSession = Depends(get_session),
):
    lab_repo = LabRepository(session)
    host_repo = HostRepository(session)
    container_repo = ContainerRepository(session)
    network_repo = NetworkRepository(session)

    lab = await lab_repo.get_by_name(lab_name)
    if lab is None:
        raise HTTPException(status_code=404, detail=f"Lab not found: {lab_name}")

    row = await container_repo.get_by_name(lab.id, container_name)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Environment not found: {container_name}"
        )

    host, api_key = await resolve_lab_host_connection(host_repo, lab)
    runtime = await fetch_runtime_container(
        host=host,
        api_key=api_key,
        container_ref=row.container_id,
    )
    if runtime is not None and is_compose_container(runtime):
        raise HTTPException(
            status_code=400,
            detail=f"Container '{container_name}' belongs to a compose project",
        )

    operation_result = await remove_runtime_container(
        host=host,
        api_key=api_key,
        container_ref=row.container_id,
    )

    delete_status = extract_status_code(operation_result["delete"])
    if delete_status >= 400 and delete_status != 404:
        raise_for_lab_host_error(
            operation_result["delete"],
            operation=f"remove environment '{container_name}'",
        )

    await container_repo.delete_row(row)
    await _delete_ingress_route_if_possible(
        host=host,
        lab_name=lab.name,
        resource_name=container_name,
    )

    private_network_name = (
        f"lab-{_safe_slug(lab.name)}-{lab.id}-env-{_safe_slug(container_name)}"
    )
    private_network = await network_repo.get_by_name(lab.id, private_network_name)
    if private_network is not None:
        network_result = await call_lab_host(
            host=host,
            api_key=api_key,
            method="DELETE",
            endpoint_path=f"/networks/{private_network.network_id}",
        )
        network_code = extract_status_code(network_result)
        if network_code < 400 or network_code == 404:
            await network_repo.delete_row(private_network)

    return {
        "lab": serialize_lab(lab),
        "status": "removed",
        "environment": container_name,
    }


@app.delete("/lab/{lab_name}/projects")
async def delete_lab_projects(
    lab_name: str,
    session: AsyncSession = Depends(get_session),
):
    lab_repo = LabRepository(session)
    host_repo = HostRepository(session)
    project_repo = ProjectRepository(session)

    lab = await lab_repo.get_by_name(lab_name)
    if lab is None:
        raise HTTPException(status_code=404, detail=f"Lab not found: {lab_name}")

    host, api_key = await resolve_lab_host_connection(host_repo, lab)
    rows = await project_repo.list_by_lab_id(lab.id)

    removed: list[str] = []
    failed: list[dict[str, object]] = []

    for row in rows:
        result = await call_lab_host(
            host=host,
            api_key=api_key,
            method="DELETE",
            endpoint_path=f"/compose/{row.project_name}",
            query={"remove_volumes": "false"},
        )
        code = extract_status_code(result)
        if code >= 400 and code != 404:
            failed.append(
                {
                    "project_name": row.project_name,
                    "status_code": code,
                    "body": extract_body(result),
                }
            )
            continue

        removed.append(row.project_name)
        await project_repo.delete_row(row)

        if isinstance(row.exposed_services, list):
            for service_name in row.exposed_services:
                if isinstance(service_name, str) and service_name:
                    await _delete_ingress_route_if_possible(
                        host=host,
                        lab_name=lab.name,
                        resource_name=service_name,
                    )

    return {
        "lab": serialize_lab(lab),
        "removed": removed,
        "failed": failed,
    }


@app.delete("/lab/{lab_name}/projects/{project_name}")
async def delete_lab_project(
    lab_name: str,
    project_name: str,
    session: AsyncSession = Depends(get_session),
):
    lab_repo = LabRepository(session)
    host_repo = HostRepository(session)
    project_repo = ProjectRepository(session)

    lab = await lab_repo.get_by_name(lab_name)
    if lab is None:
        raise HTTPException(status_code=404, detail=f"Lab not found: {lab_name}")

    row = await project_repo.get_by_name(lab.id, project_name)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Project not found: {project_name}"
        )

    host, api_key = await resolve_lab_host_connection(host_repo, lab)
    result = await call_lab_host(
        host=host,
        api_key=api_key,
        method="DELETE",
        endpoint_path=f"/compose/{project_name}",
        query={"remove_volumes": "false"},
    )

    code = extract_status_code(result)
    if code >= 400 and code != 404:
        raise_for_lab_host_error(
            result,
            operation=f"remove project '{project_name}'",
        )

    await project_repo.delete_row(row)

    if isinstance(row.exposed_services, list):
        for service_name in row.exposed_services:
            if isinstance(service_name, str) and service_name:
                await _delete_ingress_route_if_possible(
                    host=host,
                    lab_name=lab.name,
                    resource_name=service_name,
                )

    return {
        "lab": serialize_lab(lab),
        "status": "removed",
        "project": project_name,
    }


@app.post("/lab/{lab_name}/environments/state/{state}")
async def set_lab_environments_state(
    lab_name: str,
    state: Literal["stop", "pause", "unpause", "start"],
    payload: NameListRequest,
    session: AsyncSession = Depends(get_session),
):
    lab_repo = LabRepository(session)
    host_repo = HostRepository(session)
    container_repo = ContainerRepository(session)

    lab = await lab_repo.get_by_name(lab_name)
    if lab is None:
        raise HTTPException(status_code=404, detail=f"Lab not found: {lab_name}")

    host, api_key = await resolve_lab_host_connection(host_repo, lab)

    changed: list[dict[str, object]] = []
    not_found: list[str] = []
    failed: list[dict[str, object]] = []

    for container_name in payload.names:
        row = await container_repo.get_by_name(lab.id, container_name)
        if row is None:
            not_found.append(container_name)
            continue

        result = await call_lab_host(
            host=host,
            api_key=api_key,
            method="POST",
            endpoint_path=f"/containers/{row.container_id}/state/{state}",
        )
        code = extract_status_code(result)
        if code >= 400:
            failed.append(
                {
                    "container_name": container_name,
                    "status_code": code,
                    "body": extract_body(result),
                }
            )
            continue

        runtime_body = extract_body(result)
        runtime = extract_container_from_body(runtime_body)
        if runtime is not None:
            runtime_status_obj = runtime.get("status")
            row.status = to_container_status(
                runtime_status_obj if isinstance(runtime_status_obj, str) else None
            )
            row.last_seen_utc = datetime.utcnow()
            await container_repo.update(row)

        changed.append(
            {
                "container_name": container_name,
                "state": state,
                "response": runtime_body,
            }
        )

    return {
        "lab": serialize_lab(lab),
        "changed": changed,
        "not_found": not_found,
        "failed": failed,
    }


@app.post("/lab/{lab_name}/projects/state/{state}")
async def set_lab_projects_state(
    lab_name: str,
    state: Literal["up", "down", "pull", "start", "stop"],
    payload: NameListRequest,
    session: AsyncSession = Depends(get_session),
):
    lab_repo = LabRepository(session)
    host_repo = HostRepository(session)
    project_repo = ProjectRepository(session)

    lab = await lab_repo.get_by_name(lab_name)
    if lab is None:
        raise HTTPException(status_code=404, detail=f"Lab not found: {lab_name}")

    host, api_key = await resolve_lab_host_connection(host_repo, lab)

    action_by_state = {
        "up": "up",
        "down": "down",
        "pull": "pull",
        "start": "up",
        "stop": "down",
    }
    action = action_by_state[state]

    changed: list[dict[str, object]] = []
    not_found: list[str] = []
    failed: list[dict[str, object]] = []

    for project_name in payload.names:
        row = await project_repo.get_by_name(lab.id, project_name)
        if row is None:
            not_found.append(project_name)
            continue

        result = await call_lab_host(
            host=host,
            api_key=api_key,
            method="POST",
            endpoint_path=f"/compose/{project_name}/{action}",
            timeout_seconds=120,
        )
        code = extract_status_code(result)
        if code >= 400:
            failed.append(
                {
                    "project_name": project_name,
                    "status_code": code,
                    "body": extract_body(result),
                }
            )
            continue

        changed.append(
            {
                "project_name": project_name,
                "state": state,
                "response": extract_body(result),
            }
        )

    return {
        "lab": serialize_lab(lab),
        "changed": changed,
        "not_found": not_found,
        "failed": failed,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
