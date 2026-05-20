from __future__ import annotations

import logging
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from controller_helpers import (
    call_lab_host,
    extract_body,
    extract_container_from_body,
    extract_status_code,
    remove_runtime_container,
)
from db.models.container import Container, ContainerStatus
from db.models.host import Host
from db.models.lab import Lab
from db.models.project import Project, ProjectDesiredState
from db.models.shared import LifetimeType
from db.repos.container import ContainerRepository
from db.repos.host import HostRepository
from db.repos.lab import LabRepository
from db.repos.project import ProjectRepository
from db.session import session_scope
from secret_store import get_secret_store

_RECONCILE_LOCK = Lock()
logger = logging.getLogger(__name__)


def _safe_slug(value: str) -> str:
    slug = "".join(ch for ch in value.lower() if ch.isalnum() or ch in "-_")
    return slug.strip("-_") or "lab"


def _route_hostname(*, resource_name: str, lab_name: str, base_domain: str) -> str:
    return (
        f"{_safe_slug(resource_name)}-{_safe_slug(lab_name)}.{base_domain.strip('.')}"
    )


def _ttl_expired(created_at: datetime, ttl_seconds: int | None) -> bool:
    if ttl_seconds is None or ttl_seconds <= 0:
        return False
    return (
        datetime.now(UTC).replace(tzinfo=None) - created_at
    ).total_seconds() >= ttl_seconds


def _runtime_status(container_payload: dict[str, object] | None) -> str | None:
    if not isinstance(container_payload, dict):
        return None
    status_obj = container_payload.get("status")
    if isinstance(status_obj, str):
        return status_obj.lower()
    return None


def _runtime_exit_code(container_payload: dict[str, object] | None) -> int | None:
    if not isinstance(container_payload, dict):
        return None
    exit_code_obj = container_payload.get("exit_code")
    if isinstance(exit_code_obj, int):
        return exit_code_obj
    return None


async def _delete_ingress_route_if_possible(
    *,
    host: Host,
    api_key: str,
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
    await call_lab_host(
        host=host,
        api_key=api_key,
        method="DELETE",
        endpoint_path=f"/ingress/routes/{fqdn}",
    )


async def _resolve_host_api_key(host: Host) -> str | None:
    if not host.api_key_secret_path:
        return None

    secret_store = get_secret_store()
    try:
        value = await secret_store.get_secret(host.api_key_secret_path)
    except Exception:
        return None

    return value if isinstance(value, str) and value else None


async def _fetch_container_summary(
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
    code = extract_status_code(result)
    if code == 404 or code >= 400:
        return None

    body = extract_body(result)
    runtime = extract_container_from_body(body)
    return runtime if isinstance(runtime, dict) else None


async def _remove_container_row_and_runtime(
    *,
    host: Host,
    api_key: str,
    lab: Lab,
    container_repo: ContainerRepository,
    container_row: Container,
) -> None:
    logger.info(
        "Reconciler removing container lab=%s name=%s container_id=%s",
        lab.name,
        container_row.name,
        container_row.container_id,
    )
    await remove_runtime_container(
        host=host,
        api_key=api_key,
        container_ref=container_row.container_id,
    )
    await _delete_ingress_route_if_possible(
        host=host,
        api_key=api_key,
        lab_name=lab.name,
        resource_name=container_row.name,
    )
    await container_repo.delete_row(container_row)


async def _reconcile_container(
    *,
    host: Host,
    api_key: str,
    lab: Lab,
    container_repo: ContainerRepository,
    row: Container,
    runtime_by_id: dict[str, dict[str, object]],
) -> None:
    runtime = runtime_by_id.get(row.container_id)
    if runtime is None:
        runtime = await _fetch_container_summary(
            host=host,
            api_key=api_key,
            container_ref=row.container_id,
        )

    if row.lifetime_type == LifetimeType.SESSION:
        return

    if row.lifetime_type == LifetimeType.EPHEMERAL and _ttl_expired(
        row.created_at_utc,
        row.time_to_live_seconds,
    ):
        logger.info(
            "Reconciler TTL expired for container lab=%s name=%s",
            lab.name,
            row.name,
        )
        await _remove_container_row_and_runtime(
            host=host,
            api_key=api_key,
            lab=lab,
            container_repo=container_repo,
            container_row=row,
        )
        return

    if row.lifetime_type == LifetimeType.SINGLE_USE:
        status_value = _runtime_status(runtime)
        exit_code = _runtime_exit_code(runtime)
        if runtime is None:
            await container_repo.delete_row(row)
            await _delete_ingress_route_if_possible(
                host=host,
                api_key=api_key,
                lab_name=lab.name,
                resource_name=row.name,
            )
            return
        if status_value in {"exited", "dead", "stopped"} and exit_code == 0:
            logger.info(
                "Reconciler single-use completion for container lab=%s name=%s",
                lab.name,
                row.name,
            )
            await _remove_container_row_and_runtime(
                host=host,
                api_key=api_key,
                lab=lab,
                container_repo=container_repo,
                container_row=row,
            )
            return

    desired = row.status
    status_value = _runtime_status(runtime)

    if desired == ContainerStatus.STARTED:
        if status_value not in {"running", "restarting"}:
            logger.info(
                "Reconciler starting container lab=%s name=%s", lab.name, row.name
            )
            await call_lab_host(
                host=host,
                api_key=api_key,
                method="POST",
                endpoint_path=f"/containers/{row.container_id}/state/start",
            )
    elif desired == ContainerStatus.STOPPED:
        if status_value in {"running", "restarting", "paused"}:
            logger.info(
                "Reconciler stopping container lab=%s name=%s", lab.name, row.name
            )
            await call_lab_host(
                host=host,
                api_key=api_key,
                method="POST",
                endpoint_path=f"/containers/{row.container_id}/state/stop",
            )
    elif desired == ContainerStatus.PAUSED:
        if status_value == "running":
            logger.info(
                "Reconciler pausing container lab=%s name=%s", lab.name, row.name
            )
            await call_lab_host(
                host=host,
                api_key=api_key,
                method="POST",
                endpoint_path=f"/containers/{row.container_id}/state/pause",
            )


def _compose_containers_from_ps(result: dict[str, object]) -> list[dict[str, object]]:
    body = extract_body(result)
    if not isinstance(body, dict):
        return []
    project_obj = body.get("project")
    if not isinstance(project_obj, dict):
        return []
    containers_obj = project_obj.get("containers")
    if not isinstance(containers_obj, list):
        return []
    return [item for item in containers_obj if isinstance(item, dict)]


async def _remove_project_row_and_runtime(
    *,
    host: Host,
    api_key: str,
    lab: Lab,
    project_repo: ProjectRepository,
    project_row: Project,
) -> None:
    logger.info(
        "Reconciler removing project lab=%s project=%s",
        lab.name,
        project_row.project_name,
    )
    await call_lab_host(
        host=host,
        api_key=api_key,
        method="DELETE",
        endpoint_path=f"/compose/{project_row.project_name}",
        query={"remove_volumes": "false"},
    )

    for service_name in project_repo.exposed_service_names(project_row):
        await _delete_ingress_route_if_possible(
            host=host,
            api_key=api_key,
            lab_name=lab.name,
            resource_name=service_name,
        )

    await project_repo.delete_row(project_row)


def _all_single_use_success(containers: list[dict[str, object]]) -> bool:
    if not containers:
        return False

    for item in containers:
        status_obj = item.get("status")
        exit_code_obj = item.get("exit_code")
        status_value = str(status_obj).lower() if isinstance(status_obj, str) else ""
        if status_value not in {"exited", "dead", "stopped"}:
            return False
        if not isinstance(exit_code_obj, int) or exit_code_obj != 0:
            return False

    return True


async def _reconcile_project(
    *,
    host: Host,
    api_key: str,
    lab: Lab,
    project_repo: ProjectRepository,
    row: Project,
) -> None:
    ps_result = await call_lab_host(
        host=host,
        api_key=api_key,
        method="GET",
        endpoint_path=f"/compose/{row.project_name}/ps",
    )
    ps_code = extract_status_code(ps_result)
    containers = _compose_containers_from_ps(ps_result)

    if row.lifetime_type == LifetimeType.SESSION:
        return

    if row.lifetime_type == LifetimeType.EPHEMERAL and _ttl_expired(
        row.created_at_utc,
        row.time_to_live_seconds,
    ):
        logger.info(
            "Reconciler TTL expired for project lab=%s project=%s",
            lab.name,
            row.project_name,
        )
        await _remove_project_row_and_runtime(
            host=host,
            api_key=api_key,
            lab=lab,
            project_repo=project_repo,
            project_row=row,
        )
        return

    if row.lifetime_type == LifetimeType.SINGLE_USE and _all_single_use_success(
        containers
    ):
        logger.info(
            "Reconciler single-use completion for project lab=%s project=%s",
            lab.name,
            row.project_name,
        )
        await _remove_project_row_and_runtime(
            host=host,
            api_key=api_key,
            lab=lab,
            project_repo=project_repo,
            project_row=row,
        )
        return

    if row.desired_state == ProjectDesiredState.RUNNING:
        should_up = ps_code == 404
        if not should_up:
            running_exists = any(
                isinstance(item.get("status"), str)
                and str(item.get("status")).lower() in {"running", "restarting"}
                for item in containers
            )
            should_up = not running_exists

        if should_up:
            logger.info(
                "Reconciler bringing project up lab=%s project=%s",
                lab.name,
                row.project_name,
            )
            await call_lab_host(
                host=host,
                api_key=api_key,
                method="POST",
                endpoint_path=f"/compose/{row.project_name}/up",
                timeout_seconds=120,
            )

    elif row.desired_state == ProjectDesiredState.STOPPED:
        if ps_code < 400 and containers:
            logger.info(
                "Reconciler bringing project down lab=%s project=%s",
                lab.name,
                row.project_name,
            )
            await call_lab_host(
                host=host,
                api_key=api_key,
                method="POST",
                endpoint_path=f"/compose/{row.project_name}/down",
                timeout_seconds=120,
            )


async def _reconcile_for_host(
    *,
    host: Host,
    lab_repo: LabRepository,
    container_repo: ContainerRepository,
    project_repo: ProjectRepository,
    runtime_by_id: dict[str, dict[str, object]],
) -> None:
    api_key = await _resolve_host_api_key(host)
    if not api_key:
        return

    labs = await lab_repo.list_by_host_id(host.id)

    for lab in labs:
        container_rows = await container_repo.list_by_lab_id(lab.id)
        for row in container_rows:
            await _reconcile_container(
                host=host,
                api_key=api_key,
                lab=lab,
                container_repo=container_repo,
                row=row,
                runtime_by_id=runtime_by_id,
            )

        project_rows = await project_repo.list_by_lab_id(lab.id)
        for row in project_rows:
            await _reconcile_project(
                host=host,
                api_key=api_key,
                lab=lab,
                project_repo=project_repo,
                row=row,
            )


async def reconcile_heartbeat_payload(payload: dict[str, Any]) -> None:
    if not _RECONCILE_LOCK.acquire(blocking=False):
        logger.debug("Reconciliation skipped: lock already held")
        return

    try:
        async with session_scope() as session:
            host_repo = HostRepository(session)
            lab_repo = LabRepository(session)
            container_repo = ContainerRepository(session)
            project_repo = ProjectRepository(session)

            runtime_by_id: dict[str, dict[str, object]] = {}
            containers_obj = payload.get("containers")
            if isinstance(containers_obj, list):
                for item in containers_obj:
                    if not isinstance(item, dict):
                        continue
                    cid = item.get("id")
                    if isinstance(cid, str) and cid:
                        runtime_by_id[cid] = item

            host = await host_repo.resolve_from_heartbeat_payload(payload)
            if host is not None:
                host = await host_repo.mark_online(host)
                logger.info("Reconciler processing heartbeat host=%s", host.hostname)
                await _reconcile_for_host(
                    host=host,
                    lab_repo=lab_repo,
                    container_repo=container_repo,
                    project_repo=project_repo,
                    runtime_by_id=runtime_by_id,
                )
                return

            hosts = await host_repo.list_all()
            logger.info("Reconciler periodic run hosts=%s", len(hosts))
            for row in hosts:
                await _reconcile_for_host(
                    host=row,
                    lab_repo=lab_repo,
                    container_repo=container_repo,
                    project_repo=project_repo,
                    runtime_by_id={},
                )
    finally:
        _RECONCILE_LOCK.release()
