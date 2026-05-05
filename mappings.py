from typing import NotRequired, TypedDict

from docker.types import Mount

from models import ContainerArgs, ContainerStorageMount, PortMapping


class ContainerRunKwargs(TypedDict):
    image: str
    name: str
    command: NotRequired[str | None]
    environment: NotRequired[dict[str, str] | None]
    ports: NotRequired[dict[str, int] | None]
    cpu_quota: NotRequired[int]
    mem_limit: NotRequired[str]
    read_only: NotRequired[bool]
    privileged: NotRequired[bool]
    cap_drop: NotRequired[list[str]]
    cap_add: NotRequired[list[str]]
    devices: NotRequired[list[str]]
    security_opt: NotRequired[list[str]]
    network_disabled: NotRequired[bool]
    labels: NotRequired[dict[str, str] | None]
    mounts: NotRequired[list[Mount] | None]


def build_ports(ports: list[PortMapping] | None) -> dict[str, int] | None:
    if not ports:
        return None

    port_dict: dict[str, int] = {}

    for p in ports:
        port_key = f"{p.container}/{p.protocol}"
        port_dict[port_key] = p.host

    return port_dict


def build_mounts(storage: list[ContainerStorageMount] | None) -> list[Mount] | None:
    if not storage:
        return None

    return [
        Mount(
            type=m.type.value,
            source=m.source,
            target=m.target,
            read_only=m.read_only_mount,
        )
        for m in storage
    ]


def build_container_kwargs(args: ContainerArgs) -> ContainerRunKwargs:
    kwargs: ContainerRunKwargs = {
        # ---------- identity ----------
        "image": args.image,
        "name": args.name,
    }

    if args.command is not None:
        kwargs["command"] = args.command

    if args.env is not None:
        kwargs["environment"] = args.env

    ports = build_ports(args.ports)
    if ports is not None:
        kwargs["ports"] = ports

    if args.resources is not None:
        kwargs["cpu_quota"] = args.resources.cpu_count * 100000
        kwargs["mem_limit"] = args.resources.memory_limit

    if args.security is not None:
        kwargs["read_only"] = args.security.read_only_root_fs
        kwargs["privileged"] = args.security.privileged
        kwargs["cap_drop"] = args.security.capabilities_drop
        kwargs["cap_add"] = args.security.capabilities_add
        kwargs["devices"] = args.security.devices
        kwargs["security_opt"] = [
            *(
                [f"seccomp={args.security.seccomp_profile}"]
                if args.security.seccomp_profile != "default"
                else []
            ),
            f"apparmor={args.security.apparmor_profile}",
        ]
        kwargs["network_disabled"] = False

    if args.labels is not None:
        kwargs["labels"] = args.labels

    mounts = build_mounts(args.storage)
    if mounts is not None:
        kwargs["mounts"] = mounts

    return kwargs
