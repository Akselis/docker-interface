from typing import Literal

import docker
from docker.models.containers import Container

from mappings import build_container_kwargs
from models import ContainerArgs

client = docker.from_env()


def create_container(args: ContainerArgs) -> Container:
    kwargs = build_container_kwargs(args)
    container = client.containers.run(**kwargs, detach=True)
    return container


def get_container(container_id: str) -> Container:
    return client.containers.get(container_id)


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
