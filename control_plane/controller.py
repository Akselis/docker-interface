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
from db.models.project import Project
from db.repos.container import ContainerRepository
from db.repos.host import HostRepository
from db.repos.lab import LabRepository
from db.repos.project import ProjectRepository
from db.session import get_session
from fastapi import Depends, FastAPI, HTTPException, status
from lab_host_client import LabHostClient
from models import (
    CallLabHostRequest,
    ComposeDeployRequest,
    CreateLabRequest,
    DeployEnvironmentRequest,
    NameListRequest,
    RegisterHostRequest,
)
from rabbitmq_consumer import start_consumer_thread
from secret_store import build_host_api_key_secret_path, get_secret_store
from sqlalchemy.ext.asyncio import AsyncSession

app = FastAPI()


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
                cpu_total=payload.cpu_total,
                memory_total_mb=payload.memory_total_mb,
                last_heartbeat_utc=now,
            )
        )
        return {"status": "registered", "host": serialize_host(host)}

    existing.hostname = payload.hostname
    existing.ip_address = payload.ip_address
    existing.port = payload.port
    existing.scheme = payload.scheme
    existing.api_key_secret_path = secret_path
    existing.status = payload.status
    existing.cpu_total = payload.cpu_total
    existing.memory_total_mb = payload.memory_total_mb
    existing.last_heartbeat_utc = now
    host = await host_repo.update(existing)
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

    volumes_prune = await call_lab_host(
        host=host,
        api_key=api_key,
        method="POST",
        endpoint_path="/volumes/prune",
    )
    networks_prune = await call_lab_host(
        host=host,
        api_key=api_key,
        method="POST",
        endpoint_path="/networks/prune",
    )

    await project_repo.delete_by_lab_id(lab.id)
    await container_repo.delete_by_lab_id(lab.id)
    await lab_repo.delete_by_lab_id(lab.id)

    return {
        "status": "removed",
        "lab_name": lab_name,
        "removed_projects": removed_projects,
        "failed_projects": failed_projects,
        "removed_containers": removed_containers,
        "failed_containers": failed_containers,
        "volumes_prune": volumes_prune,
        "networks_prune": networks_prune,
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
        exclude={"lifetime_type", "time_to_live_seconds"}
    )

    result = await call_lab_host(
        host=host,
        api_key=api_key,
        method="POST",
        endpoint_path="/containers",
        json_body=lab_host_payload,
    )
    raise_for_lab_host_error(result, operation="create container")

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

    if lab.status != LabStatus.RUNNING:
        lab.status = LabStatus.RUNNING
        await lab_repo.update(lab)

    return {"lab": serialize_lab(lab), "environment": runtime}


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
        row.cpu_limit = requested_cpu
        row.memory_limit_mb = requested_memory_mb
        await project_repo.update(row)

    if lab.status != LabStatus.RUNNING:
        lab.status = LabStatus.RUNNING
        await lab_repo.update(lab)

    return {"lab": serialize_lab(lab), "project": body["project"]}


@app.delete("/lab/{lab_name}/environments")
async def delete_lab_environments(
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
