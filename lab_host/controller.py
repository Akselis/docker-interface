import hmac
import os
from contextlib import asynccontextmanager
from subprocess import TimeoutExpired
from typing import Literal

import docker_interface as di
from docker.errors import (
    APIError,
    ContainerError,
    DockerException,
    ImageNotFound,
    NotFound,
)
from fastapi import Depends, FastAPI, Header, HTTPException, status
from mappings import summarize_container, summarize_network
from models import (
    ComposeActionArgs,
    ComposeDeployArgs,
    ComposeLogsArgs,
    ContainerArgs,
    ExecCommandRequest,
    NetworkConnectArgs,
    NetworkCreateArgs,
    NetworkDisconnectArgs,
    VolumeCreateArgs,
)
from rabbitmq_publisher import start_heartbeat_publisher_thread

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


@asynccontextmanager
async def lifespan(_: FastAPI):
    start_heartbeat_publisher_thread()
    yield


app = FastAPI(
    dependencies=[Depends(verify_api_key)],
    lifespan=lifespan,
)


@app.post("/containers")
def create_container(args: ContainerArgs):
    try:
        container = di.create_container(args)
        return {"container": summarize_container(container)}
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/containers")
def list_containers(
    full: bool = False,
    all_containers: bool = True,
    limit: int = -1,
    status_filter: str | None = None,
    name_filter: str | None = None,
    label_filters: list[str] | None = None,
):
    try:
        filters: dict[str, str | list[str] | bool] = {}
        if status_filter:
            filters["status"] = status_filter
        if name_filter:
            filters["name"] = name_filter
        if label_filters:
            filters["label"] = label_filters

        containers = di.get_containers(
            all_containers=all_containers,
            limit=limit,
            filters=filters or None,
        )
        return [{"container": summarize_container(c, full=full)} for c in containers]
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.get("/containers/{container_id}")
def get_container(
    container_id: str,
    full: bool = False,
    status_filter: str | None = None,
    name_filter: str | None = None,
    label_filters: list[str] | None = None,
):
    try:
        filters: dict[str, str | list[str] | bool] = {}
        if status_filter:
            filters["status"] = status_filter
        if name_filter:
            filters["name"] = name_filter
        if label_filters:
            filters["label"] = label_filters

        container = di.get_container(container_id, filters=filters or None)
        return {"container": summarize_container(container, full=full)}
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
        return {"container": summarize_container(container)}
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


@app.post("/containers/prune")
def prune_containers():
    try:
        result = di.prune_containers()
        return {"result": result}
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
        return {"network": summarize_network(network)}
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.get("/networks")
def list_networks(
    full: bool = False,
    name_filter: str | None = None,
    driver_filter: str | None = None,
    label_filters: list[str] | None = None,
):
    try:
        filters: dict[str, str | list[str] | bool] = {}
        if name_filter:
            filters["name"] = name_filter
        if driver_filter:
            filters["driver"] = driver_filter
        if label_filters:
            filters["label"] = label_filters

        networks = di.get_networks(filters=filters or None)
        return [{"network": summarize_network(n, full=full)} for n in networks]
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.get("/networks/{network_id}")
def get_network(
    network_id: str,
    full: bool = False,
    name_filter: str | None = None,
    driver_filter: str | None = None,
    label_filters: list[str] | None = None,
):
    try:
        filters: dict[str, str | list[str] | bool] = {}
        if name_filter:
            filters["name"] = name_filter
        if driver_filter:
            filters["driver"] = driver_filter
        if label_filters:
            filters["label"] = label_filters

        network = di.get_network(network_id, filters=filters or None)
        return {"network": summarize_network(network, full=full)}
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
        return {"network": summarize_network(network)}
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
        return {"network": summarize_network(network)}
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


@app.post("/compose/deploy")
def deploy_compose(payload: ComposeDeployArgs):
    try:
        result = di.deploy_compose_package(payload)
        return {"deployment": result}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutExpired as exc:
        raise HTTPException(
            status_code=504, detail=f"Compose command timed out: {exc}"
        ) from exc


@app.post("/compose/{project_name}/up")
def compose_up(project_name: str, payload: ComposeActionArgs | None = None):
    try:
        result = di.compose_up(project_name, payload)
        return {"result": result}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutExpired as exc:
        raise HTTPException(
            status_code=504, detail=f"Compose command timed out: {exc}"
        ) from exc


@app.post("/compose/{project_name}/down")
def compose_down(project_name: str, payload: ComposeActionArgs | None = None):
    try:
        result = di.compose_down(project_name, payload)
        return {"result": result}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutExpired as exc:
        raise HTTPException(
            status_code=504, detail=f"Compose command timed out: {exc}"
        ) from exc


@app.post("/compose/{project_name}/pull")
def compose_pull(project_name: str, payload: ComposeActionArgs | None = None):
    try:
        result = di.compose_pull(project_name, payload)
        return {"result": result}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutExpired as exc:
        raise HTTPException(
            status_code=504, detail=f"Compose command timed out: {exc}"
        ) from exc


@app.get("/compose/{project_name}/ps")
def compose_ps(project_name: str):
    try:
        result = di.compose_ps(project_name)
        return {"result": result}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutExpired as exc:
        raise HTTPException(
            status_code=504, detail=f"Compose command timed out: {exc}"
        ) from exc


@app.post("/compose/{project_name}/logs")
def compose_logs(project_name: str, payload: ComposeLogsArgs | None = None):
    try:
        result = di.compose_logs(project_name, payload)
        return {"result": result}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutExpired as exc:
        raise HTTPException(
            status_code=504, detail=f"Compose command timed out: {exc}"
        ) from exc


@app.delete("/compose/{project_name}")
def destroy_compose(project_name: str, remove_volumes: bool = False):
    try:
        result = di.destroy_compose_project(project_name, remove_volumes=remove_volumes)
        return {"result": result}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TimeoutExpired as exc:
        raise HTTPException(
            status_code=504, detail=f"Compose command timed out: {exc}"
        ) from exc


@app.post("/volumes")
def create_volume(payload: VolumeCreateArgs):
    try:
        volume = di.create_volume(payload)
        return {"volume": volume}
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.get("/volumes")
def list_volumes():
    try:
        volumes = di.get_volumes()
        return {"volumes": volumes}
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.delete("/volumes/{volume_name}")
def delete_volume(volume_name: str, force: bool = False):
    try:
        di.remove_volume(volume_name, force=force)
        return {"status": "removed", "volume_name": volume_name}
    except NotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"Volume not found: {volume_name}"
        ) from exc
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc


@app.post("/volumes/prune")
def prune_volumes():
    try:
        result = di.prune_volumes()
        return {"result": result}
    except APIError as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker API error: {exc.explanation}"
        ) from exc
    except DockerException as exc:
        raise HTTPException(
            status_code=500, detail=f"Docker client error: {str(exc)}"
        ) from exc
