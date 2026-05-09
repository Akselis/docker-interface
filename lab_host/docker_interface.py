from typing import Literal

import docker
from docker.models.containers import Container
from docker.models.networks import Network
from docker.types import IPAMConfig, IPAMPool

from mappings import build_container_kwargs
from models import (
    ContainerArgs,
    NetworkConnectArgs,
    NetworkCreateArgs,
    NetworkDisconnectArgs,
)

client = docker.from_env()


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


def get_network(network_id: str) -> Network:
    return client.networks.get(network_id)


def get_networks() -> list[Network]:
    return client.networks.list()


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
