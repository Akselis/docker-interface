from typing import TypedDict

from docker.types import Mount

from models import ContainerArgs, ContainerStorageMount, PortMapping


class ContainerRunKwargs(TypedDict):
    image: str
    name: str
    command: str | None
    environment: dict[str, str]
    ports: dict[str, int] | None
    cpu_quota: int
    mem_limit: str
    user: str
    read_only: bool
    privileged: bool
    cap_drop: list[str]
    cap_add: list[str]
    devices: list[str]
    security_opt: list[str]
    network_disabled: bool
    labels: dict[str, str]
    mounts: list[Mount] | None


def build_ports(ports: list[PortMapping]) -> dict[str, int] | None:
    if not ports:
        return None

    port_dict: dict[str, int] = {}

    for p in ports:
        port_key = f"{p.container}/{p.protocol}"
        port_dict[port_key] = p.host

    return port_dict


def build_mounts(storage: list[ContainerStorageMount]) -> list[Mount]:
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
    return {
        # ---------- identity ----------
        "image": args.image,
        "name": args.name,
        "command": args.command,
        # ---------- environment ----------
        "environment": args.env,
        # ---------- ports ----------
        "ports": build_ports(args.ports),
        # ---------- resource limits ----------
        "cpu_quota": args.resources.cpu_count * 100000,
        "mem_limit": args.resources.memory_limit,
        # ---------- security ----------
        "user": args.security.user,
        "read_only": args.security.read_only_root_fs,
        "privileged": args.security.privileged,
        "cap_drop": args.security.capabilities_drop,
        "cap_add": args.security.capabilities_add,
        "devices": args.security.devices,
        "security_opt": [
            f"seccomp={args.security.seccomp_profile}",
            f"apparmor={args.security.apparmor_profile}",
        ],
        "network_disabled": False,
        # ---------- labels ----------
        "labels": args.labels,
        # ---------- mounts ----------
        "mounts": build_mounts(args.storage),
    }
